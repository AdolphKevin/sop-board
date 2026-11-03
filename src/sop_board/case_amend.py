"""按条增改：先核对全部输入，再原子登记上传、决定与合并。"""
import hashlib
import json

from .cases import load_cases
from .errors import APIError, reject
from .service import canonical, now
from .validation import validate_batch


def put(service, key, name, body, *, selected=(), reason, before_write=None, preview=False):
    if before_write:
        # 排队替换沿用现有会话操作；先作无副作用的完整校验，避免非法输入取消旧任务。
        with service.workspace_lock:
            checked = put(service, key, name, body, selected=selected, reason=reason, preview=True)
            if not any(c['action'] != 'unchanged' for c in checked['cases']):
                return checked
            before_write()
            return put(service, key, name, body, selected=selected, reason=reason)
    validate_batch(name)
    if not isinstance(reason, str) or not reason.strip():
        reject(422, 'cases-invalid', 'reason', '请填写按条增改的依据原文')
    if len(set(selected)) != len(selected):
        reject(422, 'cases-invalid', 'case', '--case 编号不能重复')
    with service.workspace_lock, service.store.transaction() as db:
        if not db.execute('SELECT 1 FROM workspaces WHERE key=?', (key,)).fetchone():
            reject(404, 'workspace-not-found', 'workspace', '工作区不存在')
        batch = db.execute('SELECT * FROM batches WHERE workspace=? AND name=?', (key, name)).fetchone()
        upload = db.execute("SELECT * FROM case_uploads WHERE batch=? AND filename NOT LIKE '合并澄清%' ORDER BY number DESC LIMIT 1",
                            (batch['id'],)).fetchone() if batch else None
        metadata = json.loads(upload['metadata']) if upload else None
        if metadata and 'tags' in metadata:
            metadata['tags'] = [tag for tag in metadata['tags'] if tag != 'sop-draft']
        try:
            document = load_cases(body, inherited_metadata=metadata)
        except APIError as error:
            if metadata is None and any(p['problem'] == '缺少字段：metadata' for p in error.problems):
                reject(422, 'cases-invalid', 'metadata', '本批还没有用例上传，片段缺少 metadata；请带上 metadata 或改用 cases upload')
            raise
        available = {c['id']: c for c in document['test_cases']}
        missing = set(selected) - available.keys()
        if missing:
            reject(422, 'cases-invalid', 'case', '编号不在文件中：' + '、'.join(sorted(missing)))
        chosen = [available[cid] for cid in selected] if selected else document['test_cases']
        current = {c['id']: c for c in service.case_union(db, batch)} if batch else {}
        results, changes = [], []
        for case in chosen:
            cid, prior = case['id'], current.get(case['id'])
            action = 'added' if prior is None else 'unchanged' if prior == case else 'updated'
            source = db.execute('SELECT u.number,u.filename,u.uploaded_by,EXISTS('
                'SELECT 1 FROM merge_case_updates m WHERE m.batch=v.batch AND m.upload=v.upload AND m.case_id=v.case_id) AS clarification '
                'FROM case_versions v JOIN case_uploads u ON u.batch=v.batch AND u.number=v.upload '
                'WHERE v.batch=? AND v.case_id=? ORDER BY v.upload DESC LIMIT 1', (batch['id'], cid)).fetchone() if prior else None
            results.append(dict(case=cid, action=action,
                fields=sorted(k for k in case.keys() | (prior or {}).keys() if case.get(k) != (prior or {}).get(k)),
                previous={k: source[k] for k in ('number', 'filename', 'uploaded_by')} if source else None,
                overrides_clarification=bool(source and source['clarification'] and action == 'updated')))
            if action != 'unchanged':
                changes.append(case)
        if not changes:
            return dict(ok=True, cases=results, upload=None, task=None, notice='所选用例均未变化，没有登记上传、决定或合并')
        if preview:
            return dict(cases=results)
        db.execute("INSERT OR IGNORE INTO batches(workspace,name,path,product_digest) VALUES (?,?,'','')", (key, name))
        batch = service.batch(db, key, name)
        number = db.execute('SELECT COALESCE(MAX(number),0)+1 FROM case_uploads WHERE batch=?', (batch['id'],)).fetchone()[0]
        added = sum(r['action'] == 'added' for r in results)
        db.execute('INSERT INTO case_uploads(batch,number,filename,uploaded_by,uploaded_at,sha256,metadata,case_count,added,updated) '
                   'VALUES (?,?,?,?,?,?,?,?,?,?)', (batch['id'], number, '按条增改：' + body['filename'], body['uploaded_by'], now(),
                   hashlib.sha256(body['content'].encode()).hexdigest(), canonical(document['metadata']), len(changes), added, len(changes) - added))
        for case in changes:
            db.execute('INSERT INTO case_versions(batch,case_id,upload,body) VALUES (?,?,?,?)',
                       (batch['id'], case['id'], number, canonical(case)))
        # 原话保持原样；补充登记位置，不能用会话标识代替署名。
        db.execute('INSERT INTO decisions(workspace,batch,text,author,at,source) VALUES (?,?,?,?,?,?)',
                   (key, name, reason + f'\n上传 #{number}；用例：' + '、'.join(c['id'] for c in changes), body['uploaded_by'], now(), 'case-amend'))
        merge = service.merge_callback(key, name, trigger='按条增改', connection=db) if service.merge_callback else None
        notice = '已登记新增与改写的用例'
        if any(r['overrides_clarification'] for r in results):
            notice += '；本次覆盖了合并澄清改写，下一轮合并仍按已采纳的答复核对'
        return dict(ok=True, cases=results, upload=number, task=merge['task'] if merge else None, notice=notice)
