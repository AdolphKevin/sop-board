"""场景来源只信任本批合并落地记录；陌生版本保留历史，等待人处理。"""
import json
from pathlib import Path
from urllib.parse import quote

import yaml

from .errors import reject


EXPLANATION = '由用例生成的场景会在下一次合并时按用例重建，手改不会保留；要改措辞或预期，请改用例后按条增改。'
HANDLING = '可用 merge restore 还原，人工重新合并（merge prepare --full），或改用例后按条增改（cases put）。'


def scenes(raw):
    return {s['id']: s for n in (yaml.safe_load(raw) or {}).get('sops', []) for s in n['scenarios']}


def differences(before, after):
    old, new = scenes(before), scenes(after)
    return dict(before_count=len(old), after_count=len(new), added=sorted(new.keys() - old.keys()),
                removed=sorted(old.keys() - new.keys()), changed=sorted(k for k in old.keys() & new.keys() if old[k] != new[k]))


def landed_versions(db, batch, data):
    """收据关联版本；兼容旧版写入者标记，不把普通 PUT 或相同编号当作合并。"""
    for row in db.execute("SELECT t.id,t.created_at,r.registered_at,r.result_json FROM tasks t JOIN task_receipts r ON r.task=t.id "
                          "AND r.attempt=t.attempt AND r.phase='merge-land' WHERE t.workspace=? AND t.batch=? "
                          "AND t.kind='merge' ORDER BY t.id DESC", (batch['workspace'], batch['name'])):
        receipt = json.loads(row['result_json'])
        vid = receipt.get('check', {}).get('landed_version')
        version = (db.execute('SELECT * FROM versions WHERE batch=? AND id=?', (batch['id'], vid)).fetchone() if vid else
                   db.execute('SELECT * FROM versions WHERE batch=? AND pushed_by=? AND pushed_at>=? AND pushed_at<=? '
                              'ORDER BY version DESC LIMIT 1',
                              (batch['id'], f"看板合并 #{row['id']}", row['created_at'], row['registered_at'])).fetchone())
        if version is None and not vid:
            # 旧 push 内容未变时只更新配对，版本可能仍由工作区或更早合并写入。
            try:
                raw = (Path(data) / 'tasks' / str(row['id']) / 'scenario-file.yaml').read_bytes().decode('utf-8')
            except (OSError, UnicodeError):
                continue
            version = db.execute('SELECT * FROM versions WHERE batch=? AND content=? AND pushed_at<=? '
                                 'ORDER BY version DESC LIMIT 1', (batch['id'], raw, row['registered_at'])).fetchone()
        if version:
            yield dict(version, task=row['id'])


def pairings(db, version):
    return [dict(case=r['case_id'], case_sha256=r['case_sha256'], scenarios=[r['scenario']],
                 kind=r['kind'], kind_reason=r['kind_reason'])
            for r in db.execute('SELECT * FROM case_pairings WHERE version=? ORDER BY case_id', (version['id'],))]


def edited(db, batch, data, version=None):
    if not db.execute('SELECT 1 FROM case_uploads WHERE batch=?', (batch['id'],)).fetchone():
        return None
    landed = list(landed_versions(db, batch, data))
    version = version or db.execute('SELECT * FROM versions WHERE batch=? ORDER BY version DESC LIMIT 1', (batch['id'],)).fetchone()
    if not landed or not version or any(v['content'] == version['content'] for v in landed):
        return None
    baseline = landed[0]
    diff = differences(baseline['content'], version['content'])
    generated = {p['scenarios'][0] for p in pairings(db, baseline)}
    groups = {label: {k: [sid for sid in diff[k] if (sid in generated) == belongs]
                      for k in ('added', 'removed', 'changed')}
              for label, belongs in (('generated', True), ('other', False))}
    lines = []
    for key, label in (('generated', '由用例生成'), ('other', '非用例场景')):
        lines.append(label + '：' + '；'.join(title + ' ' + ('、'.join(groups[key][kind]) or '无')
                     for kind, title in (('added', '新增'), ('removed', '删除'), ('changed', '改动'))))
    return dict(baseline=baseline, diff=diff, groups=groups, reason='。'.join(lines) + '。' + EXPLANATION + HANDLING)


def require_clean(db, batch, data):
    state = edited(db, batch, data)
    if state:
        reject(409, 'scenario-file-edited', 'scenarios', state['reason'])


def attention(db, key, origin, data):
    result = []
    for row in db.execute('SELECT * FROM batches WHERE workspace=? ORDER BY name', (key,)):
        state = edited(db, row, data)
        if state:
            result.append(dict(kind='scenario-edited', batch=row['name'], reason=state['reason'],
                               url=origin + f"/w/{quote(key, safe='')}/b/{quote(row['name'], safe='')}/merges", differences=state['groups']))
    return result


def restore(service, key, name):
    from .service import fingerprint
    from .repositories import read_sop_sources
    workspace = service.workspace
    with service.workspace_lock:
        with service.store.connection() as db:
            batch = service.batch(db, key, name)
            baseline = next(landed_versions(db, batch, service.store.data), None)
            if baseline is None:
                reject(409, 'merge-baseline-missing', 'batch', '本批没有可还原的落地场景版本')
            current = service.latest_version(db, batch)
            sop = service.latest_sop_upload(db, batch)
        path = workspace.scenario_path(name)
        raw = baseline['content']
        info = workspace.product(workspace.aliases(yaml.safe_load(raw)))
        previous = path.read_bytes() if path.exists() else None
        before = previous.decode('utf-8') if previous is not None else current['content'] if current else ''
        # 摘要计算期间编辑器可能保存；登记将被覆盖的实际字节，而非较早的读取。
        if previous is not None and (not current or before != current['content']):
            document = yaml.safe_load(before)
            edited_info = workspace.product(workspace.aliases(document))
            service.push(key, name, dict(workspace=dict(name=key), content=before, path=workspace.display_path(path),
                pushed_by='还原前登记', sop_sources=read_sop_sources(workspace, before, sop),
                **edited_info, fingerprints={sid: fingerprint(s) for sid, s in scenes(before).items()}), auto_execute=False)
        diff = differences(before, raw)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix('.yaml.tmp')
        temporary.write_text(raw, encoding='utf-8')
        try:
            actual = path.read_bytes() if path.exists() else None
            if actual != previous:
                reject(409, 'scenario-file-changed', 'scenarios', '还原期间场景文件再次变化，未覆盖新内容；请重试 merge restore')
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
        try:
            result = service.push(key, name, dict(workspace=dict(name=key), content=raw, path=workspace.display_path(path),
                pushed_by='还原落地场景', sop_sources=read_sop_sources(workspace, raw, sop), **info,
                fingerprints={sid: fingerprint(s) for sid, s in scenes(raw).items()}), auto_execute=False)
        except Exception:
            # 登记失败只恢复本次写入；编辑器已再次保存时保留它的新内容。
            if path.exists() and path.read_bytes() == raw.encode('utf-8'):
                if previous is not None:
                    path.write_bytes(previous)
                else:
                    path.unlink()
            raise
        return dict(result, restored_from=baseline['task'], overwritten=sorted(set(diff['added'] + diff['removed'] + diff['changed'])))
