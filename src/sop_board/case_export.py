"""用例导出与回写（D99）：按某次上传的格式和顺序导出答复改写后的用例，可写回会话上传时记录的原文件。

导出只取该次上传包含的用例，每条换成看板当前的最新版本（含合并澄清的改写）；SOP 草稿确认生成的用例是另一次上传，单独导出。
回写只在原文件自上传后未改动、且不计入产品快照（已在 snapshot_exclude、场景目录内、被 Git 忽略或不在任何仓库内）时进行。
"""
import hashlib
import json
from pathlib import Path
import subprocess

import yaml

from .cases import load_cases

CASE_ORDER = ('id', 'title', 'description', 'priority', 'type', 'preconditions', 'steps', 'expected_results', 'tags', 'auto')
METADATA_ORDER = ('module', 'feature', 'owner', 'last_reviewed', 'tags')


def ordered(value, order):
    return {key: value[key] for key in sorted(value, key=lambda k: (order.index(k) if k in order else len(order), k))}


def record_source(db, batch_id, upload, path):
    db.execute('INSERT OR IGNORE INTO case_upload_sources(batch,upload,path) VALUES (?,?,?)', (batch_id, upload, str(Path(path).resolve())))


def snapshot_free(workspace, path):
    """回写目标是否不计入产品快照；返回 (是否可写, 说明)。"""
    path = Path(path).resolve()
    for alias, root in workspace.repos.items():
        root = Path(root).resolve()
        if not path.is_relative_to(root):
            continue
        relative = path.relative_to(root)
        exclusions = [Path(p) for p in workspace.snapshot_exclude.get(alias, [])]
        scenarios = Path(workspace.scenarios).resolve()
        if scenarios.is_relative_to(root):
            exclusions.append(scenarios.relative_to(root))
        if any(relative.is_relative_to(x) for x in exclusions):
            return True, ''
        if subprocess.run(['git', '-C', str(root), 'check-ignore', '-q', str(relative)], stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL).returncode == 0:
            return True, ''
        return False, (f'{path} 在仓库 {alias} 内且计入产品快照，回写会改变产品内容；请使用者先在 snapshot_exclude 声明 '
                       f'{alias}: [{relative.as_posix()}]（只放不参与运行的材料），或用 --out 导出后由测试同事替换')
    return True, ''


def export(service, workspace_key, batch_name, *, upload=None, out=None, write_back=False):
    if bool(out) == bool(write_back):
        raise ValueError('请用 --out <文件> 导出，或用 --write-back 写回原上传文件（二选一）')
    with service.store.connection() as db:
        batch = service.batch(db, workspace_key, batch_name)
        uploads = [dict(r) for r in db.execute('SELECT * FROM case_uploads WHERE batch=? ORDER BY number', (batch['id'],))]
        if not uploads:
            raise ValueError('该批次没有上传过用例')
        if upload is None:
            people = [u for u in uploads if not u['filename'].startswith('合并澄清')]
            if not people:
                raise ValueError('该批次只有合并澄清生成的上传，请用 --upload 指定')
            chosen = people[-1]
        else:
            chosen = next((u for u in uploads if u['number'] == upload), None)
            if chosen is None:
                raise ValueError(f'上传 {upload} 不存在；可选：' + '、'.join(f"{u['number']}（{u['filename']}）" for u in uploads))
        original = [json.loads(r['body']) for r in db.execute('SELECT body FROM case_versions WHERE batch=? AND upload=? ORDER BY id',
                                                                 (batch['id'], chosen['number']))]
        latest = {c['id']: c for c in service.case_union(db, batch)}
        source = db.execute('SELECT * FROM case_upload_sources WHERE batch=? AND upload=?', (batch['id'], chosen['number'])).fetchone()
    cases = [latest.get(case['id'], case) for case in original]
    changed = [case['id'] for case, before in zip(cases, original) if case != before]
    metadata = ordered(json.loads(chosen['metadata']), METADATA_ORDER)
    orders = {}
    if write_back:
        if source is None:
            raise ValueError('该上传没有记录原文件路径（经看板网页上传或在本功能之前上传）；请用 --out 导出后由测试同事替换')
        target = Path(source['path'])
        if target.is_symlink() or not target.is_file():
            raise ValueError(f'原上传文件不存在或不是普通文件：{target}')
        raw = target.read_bytes()
        if hashlib.sha256(raw).hexdigest() not in (chosen['sha256'], source['written_sha256']):
            raise ValueError(f'原文件在上传或上次回写后已改动：{target}；请先重新上传或用 --out 导出后人工合并')
        allowed, reason = snapshot_free(service.workspace, target)
        if not allowed:
            raise ValueError(reason)
        document = yaml.safe_load(raw.decode('utf-8'))
        metadata = document['metadata']
        orders = {item['id']: list(item) for item in document['test_cases'] if isinstance(item, dict) and 'id' in item}
    else:
        target = Path(out).expanduser().resolve()
        if target.exists():
            raise ValueError(f'导出文件已存在：{target}；请换一个路径')
    rows = []
    for case in cases:
        order = orders.get(case['id'])
        rows.append({key: case[key] for key in order if key in case} | {key: case[key] for key in case if key not in order}
                    if order else ordered(case, CASE_ORDER))
    text = yaml.safe_dump(dict(metadata=metadata, test_cases=rows), allow_unicode=True, sort_keys=False, width=4096)
    load_cases(dict(filename=target.name, uploaded_by='导出', content=text))
    temporary = target.with_name(target.name + '.sop-board-tmp')
    temporary.write_text(text, encoding='utf-8')
    temporary.replace(target)
    if write_back:
        with service.store.transaction() as db:
            db.execute('UPDATE case_upload_sources SET written_sha256=? WHERE batch=? AND upload=?',
                       (hashlib.sha256(text.encode('utf-8')).hexdigest(), batch['id'], chosen['number']))
    return dict(ok=True, path=str(target), upload=chosen['number'], filename=chosen['filename'], cases=len(rows), changed=changed,
                written_back=bool(write_back),
                notice='已按原上传的用例顺序输出；YAML 重新排版，原文件中的注释不保留' if write_back else
                       '已按原上传的格式和顺序导出；交给测试同事替换原文件前请核对 changed 列出的改写')
