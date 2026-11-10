"""修复：会话在当前检出改产品代码 → 另一会话独立复验 → 开发者提交后关闭。

- fix prepare：认领当前检出不通过的场景（同一检出同时只有一个进行中的修复），记录准备时的产品文件清单。
- fix submit：读取 fix.json（根因与是否为产品问题），按文件清单算出修复改动的文件。
- run prepare --fix N：复验的实现者自动为修复会话；全部问题场景经独立执行通过才算复验通过。
- fix close：修复改动的文件已提交，且内容与独立复验时一致，才关闭。
"""
import json
from pathlib import Path

from . import config, gitops, guards, scenarios
from .errors import reject
from .store import dumps, loads, now

OPEN = ('open', 'submitted', 'verified')


def fix_dir(project, fix_id):
    return project.data / 'fixes' / str(fix_id)


def load(context, fix_id):
    with context.store.connection() as db:
        row = db.execute('SELECT * FROM fixes WHERE id=?', (fix_id,)).fetchone()
    if row is None:
        reject(404, 'fix-not-found', 'fix', f'修复不存在：#{fix_id}')
    return dict(row)


def manifest(context, checkout):
    return gitops.snapshot(checkout.paths, context.project.exclusions(guards.test_exclusions(context)), files=True)['files']


def prepare(context, scope, *, session):
    from importlib.resources import files
    from string import Template
    from . import runs
    from .cli import scene_board
    executor = runs.session_id(session)
    operator = config.require_operator()
    found, snapshot, derived = scene_board(context)
    scenes = found['scenarios']
    failing = sorted(k for k, v in derived['items'].items() if v['state'] in ('fail', 'blocked'))
    keys = scenarios.resolve(scope, scenes) if scope else failing
    not_failing = [k for k in keys if k not in failing]
    if not_failing:
        reject(409, 'fix-scenario-invalid', 'scope', '这些场景当前不是失败或受阻，不能作为修复问题：' + '、'.join(not_failing))
    if not keys:
        reject(409, 'nothing-to-fix', 'scope', '当前检出没有失败或受阻的场景')
    with context.store.connection() as db:
        active = db.execute("SELECT id, session FROM fixes WHERE checkout=? AND status='open'", (context.checkout.id,)).fetchone()
        if active:
            reject(409, 'fix-open', 'fix', f"检出 {context.checkout.name} 已有进行中的修复 #{active['id']}（会话 {active['session']}）")
        owned = {}
        for row in db.execute("SELECT id, problems FROM fixes WHERE checkout=? AND status IN ('submitted','verified')", (context.checkout.id,)):
            for problem in json.loads(row['problems']):
                owned[scenarios.key(problem['file'], problem['scenario'])] = row['id']
        taken = [f'{k}（修复 #{owned[k]}）' for k in keys if k in owned]
        if taken:
            reject(409, 'fix-already-owned', 'scope', '这些场景已在其他修复中：' + '、'.join(taken))
        problems = []
        for key in keys:
            scene = scenes[key]
            rows = db.execute('SELECT * FROM results WHERE checkout=? AND file=? AND scenario=? AND fingerprint=? ORDER BY id DESC LIMIT 5',
                              (context.checkout.id, scene['file'], scene['id'], scene['fingerprint'])).fetchall()
            problems.append(dict(file=scene['file'], scenario=scene['id'], fingerprint=scene['fingerprint'], title=scene['title'],
                                 results=[dict(kind=r['kind'], status=r['status'], actual=r['actual'], finding=r['finding'],
                                               defect=r['defect'], executed_at=r['executed_at'],
                                               evidence=[dict(name=e['name'], path=str(context.store.evidence_path(e['sha256'])))
                                                         for e in db.execute('SELECT name, sha256 FROM result_evidence WHERE result=?', (r['id'],))])
                                          for r in rows]))
    base = manifest(context, context.checkout)
    with context.store.transaction() as db:
        cursor = db.execute("INSERT INTO fixes(checkout,session,operator,status,dir,problems,base_files,created_at) "
                            "VALUES (?,?,?,'open','',?,?,?)",
                            (context.checkout.id, executor, operator, dumps(problems), dumps(base), now()))
        fix_id = cursor.lastrowid
    directory = fix_dir(context.project, fix_id)
    directory.mkdir(parents=True)
    import yaml
    (directory / 'problems.json').write_text(dumps(problems), encoding='utf-8')
    (directory / 'scenarios.yaml').write_text(yaml.safe_dump([dict(scenes[k], key=k) for k in keys], allow_unicode=True, sort_keys=False),
                                              encoding='utf-8')
    (directory / 'repos.json').write_text(dumps({a: str(p) for a, p in context.checkout.paths.items()}), encoding='utf-8')
    (directory / 'scenario-files.json').write_text(dumps(scenario_digests(context, context.checkout)), encoding='utf-8')
    prompt = Template(files('sop_board').joinpath('prompts', 'fix.md').read_text(encoding='utf-8')).substitute(
        fix=fix_id, directory=directory, checkout=context.checkout.name, skill=context.project.skill)
    (directory / 'prompt.md').write_text(prompt, encoding='utf-8')
    with context.store.transaction() as db:
        db.execute('UPDATE fixes SET dir=? WHERE id=?', (str(directory), fix_id))
    return dict(ok=True, fix=fix_id, checkout=context.checkout.name, directory=str(directory), prompt=str(directory / 'prompt.md'),
                answer=str(directory / 'fix.json'), problems=keys, session=executor)


def scenario_digests(context, checkout):
    found = scenarios.scan(checkout.scenarios_dir(context.project))
    return {name: info['sha256'] for name, info in found['files'].items()}


def changed(base, now_files):
    return gitops.changed_between(base, now_files)


def submit(context, fix_id):
    fix = load(context, fix_id)
    if fix['status'] != 'open':
        reject(409, 'fix-state-invalid', 'fix', f"修复 #{fix_id} 的状态是 {fix['status']}，不能提交")
    from . import checkouts
    with context.store.connection() as db:
        checkout = checkouts.by_id(db, fix['checkout'])
    directory = Path(fix['dir'])
    try:
        answer = json.loads((directory / 'fix.json').read_text(encoding='utf-8'))
    except (OSError, ValueError) as error:
        reject(422, 'fix-answer-invalid', 'fix.json', f'fix.json 无法读取：{error}')
    if (not isinstance(answer, dict) or type(answer.get('product_problem')) is not bool
            or not isinstance(answer.get('root_cause'), str) or not answer['root_cause'].strip()):
        reject(422, 'fix-answer-invalid', 'fix.json', 'fix.json 须含 root_cause（文字）与 product_problem（布尔值）')
    changes = changed(loads(fix['base_files']), manifest(context, checkout))
    edited = scenario_digests(context, checkout) != json.loads((directory / 'scenario-files.json').read_text(encoding='utf-8'))
    if edited:
        reject(409, 'scenario-file-protected', 'scenarios', '修复期间场景文件被改动；修复只改产品代码，场景由审核方修改')
    if answer['product_problem'] and not changes:
        reject(422, 'fix-no-changes', 'fix', '判定为产品问题，但检出中没有产品文件的改动')
    status = 'submitted' if answer['product_problem'] else 'abandoned'
    with context.store.transaction() as db:
        db.execute('UPDATE fixes SET status=?, answer=?, changes=?, ended_at=? WHERE id=?',
                   (status, dumps(answer), dumps(changes), now() if status == 'abandoned' else None, fix_id))
        if not answer['product_problem']:
            db.execute('INSERT INTO attention(checkout,kind,text,scenarios,created_by,created_at) VALUES (?,?,?,?,?,?)',
                       (checkout.id, '其他', f"修复 #{fix_id} 判定为非产品问题：{answer['root_cause']}",
                        dumps([scenarios.key(p['file'], p['scenario']) for p in loads(fix['problems'])]), fix['session'], now()))
    result = dict(ok=True, fix=fix_id, status=status, changes=changes)
    if status == 'submitted':
        result['next'] = f'由未参与修复的新会话运行 sop-board run prepare --fix {fix_id} 独立复验'
    else:
        result['next'] = '非产品问题已登记到待人处理，交使用者处理'
    return result


def for_verify(context, fix_id, executor):
    fix = load(context, fix_id)
    if fix['status'] not in ('submitted', 'verified'):
        reject(409, 'fix-state-invalid', 'fix', f"修复 #{fix_id} 的状态是 {fix['status']}，只有已提交修复输出的修复可以复验")
    if fix['checkout'] != context.checkout.id:
        reject(409, 'fix-checkout-mismatch', 'fix', '复验须在修复所在的检出进行')
    return dict(id=fix['id'], session=fix['session'], problems=loads(fix['problems']))


def after_verify(db, work, recorded):
    """复验提交后：全部问题场景独立通过时，记录复验运行与修复文件的复验内容。"""
    fix = db.execute('SELECT * FROM fixes WHERE id=?', (work['fix'],)).fetchone()
    if fix is None or fix['status'] not in ('submitted', 'verified'):
        return
    problems = {scenarios.key(p['file'], p['scenario']) for p in loads(fix['problems'])}
    passed = {item['scenario'] for item in recorded if item['status'] == 'pass' and not item['kept']}
    independent = work['session'] != fix['session']
    if problems <= passed and independent:
        files = json.loads((Path(work['dir']) / 'product-files.json').read_text(encoding='utf-8'))
        verified = {}
        for path in loads(fix['changes']):
            alias, _, relative = path.partition('/')
            verified[path] = files.get(alias, {}).get(relative)
        db.execute("UPDATE fixes SET status='verified', verified_work=?, verified_files=? WHERE id=?",
                   (work['id'], dumps(verified), fix['id']))


def close(context, fix_id):
    from . import checkouts
    fix = load(context, fix_id)
    changes = loads(fix['changes'], [])
    if fix['status'] == 'closed':
        return dict(ok=True, fix=fix_id, status='closed', duplicate=True)
    if fix['status'] != 'verified':
        reject(409, 'fix-validation-required', 'fix', f"修复 #{fix_id} 还没有独立复验通过（由修复会话以外的会话运行 run prepare --fix {fix_id}）；"
                                                      '修复改动的文件：' + '、'.join(changes))
    with context.store.connection() as db:
        checkout = checkouts.by_id(db, fix['checkout'])
    verified = loads(fix['verified_files'], {})
    uncommitted, different = [], []
    for path in changes:
        alias, _, relative = path.partition('/')
        root = checkout.paths[alias]
        current = gitops.manifest(root).get(relative)
        committed = gitops.head_manifest(root).get(relative)
        if current != committed:
            uncommitted.append(path)
        elif committed != verified.get(path):
            different.append(path)
    if uncommitted:
        reject(409, 'fix-commit-required', 'files', '修复改动的文件还没有提交：' + '、'.join(uncommitted))
    if different:
        reject(409, 'fix-content-changed', 'files', '提交的内容与独立复验时不一致，须重新复验：' + '、'.join(different))
    with context.store.transaction() as db:
        db.execute("UPDATE fixes SET status='closed', ended_at=?, close=? WHERE id=?",
                   (now(), dumps(dict(closed_by=config.operator(), verified_work=fix['verified_work'])), fix_id))
    return dict(ok=True, fix=fix_id, status='closed', verified_run=fix['verified_work'], files=changes)


def abandon(context, fix_id):
    fix = load(context, fix_id)
    if fix['status'] not in OPEN:
        reject(409, 'fix-state-invalid', 'fix', f"修复 #{fix_id} 的状态是 {fix['status']}")
    with context.store.transaction() as db:
        db.execute("UPDATE fixes SET status='abandoned', ended_at=? WHERE id=?", (now(), fix_id))
    return dict(ok=True, fix=fix_id, status='abandoned')
