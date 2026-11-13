"""修复：会话在当前检出改产品代码 → 另一会话独立复验 → 开发者提交后关闭。

- fix prepare：认领当前检出不通过的场景（同一检出同时只有一个进行中的修复），记录准备时的产品文件清单。
- fix submit：读取 fix.json（根因与是否为产品问题），按文件清单算出修复改动的文件。
- run prepare --fix N：复验的实现者自动为修复会话；全部问题场景经独立执行通过才算复验通过。
- fix close：修复改动的文件已提交，且内容与独立复验时一致，才关闭。

修复记录分两部分（已定决策 21）：问题场景、修复会话与操作者、状态、关闭时间、产出机器与检出在业务记录库，各看板共享，
修复关闭使场景转待复测的规则对所有看板生效；修复目录、文件基线与复验摘要在本机。fix submit、run prepare --fix、
fix close、fix abandon 只能在产出修复的机器上执行，其他机器返回 fix-not-local。
"""
import json
from pathlib import Path

from . import config, gitops, guards, scenarios, workdirs
from .errors import reject
from .store import dumps, loads, now

OPEN = ('open', 'submitted', 'verified')


def load(context, fix_id):
    """修复记录：业务部分加本机部分（只有产出机器上才有）。"""
    store = context.store
    with store.records() as db:
        row = db.one('SELECT * FROM fixes WHERE id=? AND product=?', (fix_id, store.product))
        if row is None:
            reject(404, 'fix-not-found', 'fix', f'修复不存在：#{fix_id}')
        fix = dict(row)
        fix['machine_name'] = db.value('SELECT name FROM machines WHERE id=?', (fix['machine'],)) or fix['machine']
    with store.local() as db:
        local = db.execute('SELECT * FROM local_fixes WHERE fix=? AND product=?', (fix_id, store.product)).fetchone()
    fix['local'] = dict(local) if local else None
    return fix


def require_local(context, fix):
    """修复操作只在产出修复的机器上进行。"""
    if fix['machine'] != context.store.machine['id'] or fix['local'] is None:
        reject(409, 'fix-not-local', 'fix', f"修复 #{fix['id']} 产出于 {fix['machine_name']} 的机器的 {fix['checkout']} 检出，"
                                            '只能在那台机器的那个检出上提交、复验、关闭或放弃；要在本机验证，等修复提交推送后用 '
                                            'sop-board run prepare --scope <场景> 执行（结果是共享结论，但不关闭这条修复）')
    from . import checkouts
    with context.store.local() as db:
        return checkouts.by_id(db, fix['local']['checkout'])


def manifest(context, checkout):
    return gitops.snapshot(checkout.paths, context.project.exclusions(guards.test_exclusions(context)), files=True)['files']


def prepare(context, scope, *, session):
    # 与设置切换共用本机锁：业务记录与本机修复登记完成前不能换库。
    with context.store.lock:
        return _prepare(context, scope, session=session)


def _prepare(context, scope, *, session):
    from importlib.resources import files
    from string import Template
    from . import runs
    from .checkouts import branch_of
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
    store = context.store
    book = derived['book']
    # 共享的修复记录只引用证据的名称与 sha256，不带任何一台机器的本机路径；本机的 problems.json 另外写明证据在本机的位置。
    problems = []
    with store.records() as db:
        for key in keys:
            scene = scenes[key]
            rows = book.select((scene['file'], scene['id'], scene['fingerprint'])).records[:5]
            problems.append(dict(file=scene['file'], scenario=scene['id'], fingerprint=scene['fingerprint'], title=scene['title'],
                                 results=[dict(kind=r['kind'], status=r['status'], actual=r['actual'], finding=r['finding'],
                                               defect=r['defect'], executed_at=r['executed_at'],
                                               machine=book.machine_name(r['machine']), branch=r['branch'],
                                               evidence=[dict(name=e['name'], sha256=e['sha256'])
                                                         for e in db.all('SELECT name, sha256 FROM result_evidence WHERE result=? ORDER BY ordinal',
                                                                         (r['id'],))])
                                          for r in rows]))
    base = manifest(context, context.checkout)
    with store.records_tx() as db:
        active = db.one("SELECT id, session FROM fixes WHERE product=? AND machine=? AND checkout=? AND status='open'",
                        (store.product, store.machine['id'], context.checkout.name))
        if active:
            reject(409, 'fix-open', 'fix', f"检出 {context.checkout.name} 已有进行中的修复 #{active['id']}（会话 {active['session']}）")
        owned = {}
        for row in db.all("SELECT id, problems FROM fixes WHERE product=? AND machine=? AND checkout=? AND status IN ('submitted','verified')",
                          (store.product, store.machine['id'], context.checkout.name)):
            for problem in json.loads(row['problems']):
                owned[scenarios.key(problem['file'], problem['scenario'])] = row['id']
        taken = [f'{k}（修复 #{owned[k]}）' for k in keys if k in owned]
        if taken:
            reject(409, 'fix-already-owned', 'scope', '这些场景已在其他修复中：' + '、'.join(taken))
        fix_id = db.insert('INSERT INTO fixes(product,machine,checkout,branch,session,operator,status,problems,created_at) '
                           "VALUES (?,?,?,?,?,?,'open',?,?)",
                           (store.product, store.machine['id'], context.checkout.name, branch_of(context.project, context.checkout),
                            executor, operator, dumps(problems), now()))
    directory = workdirs.create(context.project.data / 'fixes', fix_id)
    with store.local_tx() as db:
        db.execute('INSERT INTO local_fixes(fix, product, checkout, dir, base_files) VALUES (?,?,?,?,?)',
                   (fix_id, store.product, context.checkout.id, str(directory), dumps(base)))
    import yaml
    (directory / 'problems.json').write_text(dumps(local_problems(store, problems)), encoding='utf-8')
    (directory / 'scenarios.yaml').write_text(yaml.safe_dump([dict(scenes[k], key=k) for k in keys], allow_unicode=True, sort_keys=False),
                                              encoding='utf-8')
    (directory / 'repos.json').write_text(dumps({a: str(p) for a, p in context.checkout.paths.items()}), encoding='utf-8')
    (directory / 'scenario-files.json').write_text(dumps(scenario_digests(context, context.checkout)), encoding='utf-8')
    prompt = Template(files('sop_board').joinpath('prompts', 'fix.md').read_text(encoding='utf-8')).substitute(
        fix=fix_id, directory=directory, checkout=context.checkout.name, skill=context.project.skill)
    (directory / 'prompt.md').write_text(prompt, encoding='utf-8')
    return dict(ok=True, fix=fix_id, checkout=context.checkout.name, directory=str(directory), prompt=str(directory / 'prompt.md'),
                answer=str(directory / 'fix.json'), problems=keys, session=executor)


def local_problems(store, problems):
    """修复会话读的 problems.json：在共享的问题记录上给每份证据补上本机路径；本机没有的写明在哪台机器上。"""
    result = []
    for problem in problems:
        results = []
        for row in problem['results']:
            evidence = [dict(item, path=str(store.evidence_path(item['sha256'])) if store.has_evidence(item['sha256'])
                             else f"证据在 {row['machine']} 的机器上") for item in row['evidence']]
            results.append(dict(row, evidence=evidence))
        result.append(dict(problem, results=results))
    return result


def scenario_digests(context, checkout):
    found = scenarios.scan(checkout.scenarios_dir(context.project))
    return {name: info['sha256'] for name, info in found['files'].items()}


def changed(base, now_files):
    return gitops.changed_between(base, now_files)


def submit(context, fix_id):
    fix = load(context, fix_id)
    checkout = require_local(context, fix)
    if fix['status'] != 'open':
        reject(409, 'fix-state-invalid', 'fix', f"修复 #{fix_id} 的状态是 {fix['status']}，不能提交")
    directory = Path(fix['local']['dir'])
    try:
        answer = json.loads((directory / 'fix.json').read_text(encoding='utf-8'))
    except (OSError, ValueError) as error:
        reject(422, 'fix-answer-invalid', 'fix.json', f'fix.json 无法读取：{error}')
    if (not isinstance(answer, dict) or type(answer.get('product_problem')) is not bool
            or not isinstance(answer.get('root_cause'), str) or not answer['root_cause'].strip()):
        reject(422, 'fix-answer-invalid', 'fix.json', 'fix.json 须含 root_cause（文字）与 product_problem（布尔值）')
    context.checkout = checkout
    changes = changed(loads(fix['local']['base_files']), manifest(context, checkout))
    edited = scenario_digests(context, checkout) != json.loads((directory / 'scenario-files.json').read_text(encoding='utf-8'))
    if edited:
        reject(409, 'scenario-file-protected', 'scenarios', '修复期间场景文件被改动；修复只改产品代码，场景由审核方修改')
    if answer['product_problem'] and not changes:
        reject(422, 'fix-no-changes', 'fix', '判定为产品问题，但检出中没有产品文件的改动')
    status = 'submitted' if answer['product_problem'] else 'abandoned'
    store = context.store
    with store.records_tx() as db:
        updated = db.execute("UPDATE fixes SET status=?, answer=?, ended_at=? WHERE id=? AND status='open'",
                             (status, dumps(answer), now() if status == 'abandoned' else None, fix_id)).rowcount
        if not updated:
            reject(409, 'fix-state-invalid', 'fix', f'修复 #{fix_id} 的状态已经改变')
        if not answer['product_problem']:
            from .ledger import insert_attention
            insert_attention(db, store, checkout.name, '其他', f"修复 #{fix_id} 判定为非产品问题：{answer['root_cause']}",
                             by=fix['session'], scenarios=[scenarios.key(p['file'], p['scenario']) for p in loads(fix['problems'])])
    with store.local_tx() as db:
        db.execute('UPDATE local_fixes SET changes=? WHERE fix=?', (dumps(changes), fix_id))
    result = dict(ok=True, fix=fix_id, status=status, changes=changes)
    if status == 'submitted':
        result['next'] = f'由未参与修复的新会话运行 sop-board run prepare --fix {fix_id} 独立复验'
    else:
        result['next'] = '非产品问题已登记到待人处理，交使用者处理'
    return result


def for_verify(context, fix_id, executor):
    fix = load(context, fix_id)
    checkout = require_local(context, fix)
    if fix['status'] not in ('submitted', 'verified'):
        reject(409, 'fix-state-invalid', 'fix', f"修复 #{fix_id} 的状态是 {fix['status']}，只有已提交修复输出的修复可以复验")
    if checkout.id != context.checkout.id:
        reject(409, 'fix-checkout-mismatch', 'fix', f'复验须在修复所在的检出 {checkout.name} 进行')
    return dict(id=fix['id'], session=fix['session'], problems=loads(fix['problems']))


def after_verify(db, store, work, recorded):
    """复验提交后（业务记录库的同一事务）：全部问题场景独立通过时，修复记为已复验。"""
    fix = db.one('SELECT * FROM fixes WHERE id=?', (work['fix'],))
    if fix is None or fix['status'] not in ('submitted', 'verified'):
        return
    problems = {scenarios.key(p['file'], p['scenario']) for p in loads(fix['problems'])}
    passed = {item['scenario'] for item in recorded if item['status'] == 'pass'}
    if problems <= passed and work['session'] != fix['session']:
        db.execute("UPDATE fixes SET status='verified' WHERE id=?", (fix['id'],))


def after_verify_local(context, work, recorded):
    """复验提交后（本机）：记下复验运行与修复文件在复验时的内容，供 fix close 核对。"""
    store = context.store
    with store.records() as db:
        fix = db.one('SELECT * FROM fixes WHERE id=?', (work['fix'],))
    if fix is None or fix['status'] != 'verified':
        return
    with store.local_tx() as db:
        local = db.execute('SELECT * FROM local_fixes WHERE fix=?', (fix['id'],)).fetchone()
        if local is None:
            return
        files = json.loads((Path(work['dir']) / 'product-files.json').read_text(encoding='utf-8'))
        verified = {}
        for path in loads(local['changes'], []):
            alias, _, relative = path.partition('/')
            verified[path] = files.get(alias, {}).get(relative)
        db.execute('UPDATE local_fixes SET verified_work=?, verified_files=? WHERE fix=?', (work['id'], dumps(verified), fix['id']))


def close(context, fix_id):
    fix = load(context, fix_id)
    checkout = require_local(context, fix)
    local = fix['local']
    changes = loads(local['changes'], [])
    if fix['status'] == 'closed':
        return dict(ok=True, fix=fix_id, status='closed', duplicate=True)
    if fix['status'] != 'verified' or not local['verified_files']:
        reject(409, 'fix-validation-required', 'fix', f"修复 #{fix_id} 还没有独立复验通过（由修复会话以外的会话运行 run prepare --fix {fix_id}）；"
                                                      '修复改动的文件：' + '、'.join(changes))
    verified = loads(local['verified_files'], {})
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
    with context.store.records_tx() as db:
        # 修复关闭是结论：取统一序号，按修复所在检出的分支归属，各看板据此把问题场景转为待复测。
        db.execute("UPDATE fixes SET status='closed', seq=?, ended_at=?, close=? WHERE id=? AND status='verified'",
                   (db.next_seq(), now(), dumps(dict(closed_by=config.operator(), verified_work=local['verified_work'])), fix_id))
    return dict(ok=True, fix=fix_id, status='closed', verified_run=local['verified_work'], files=changes)


def current(context, action):
    """省略 --fix 时：本机当前检出里与操作相符的最近一个修复。"""
    states = {'submit': ('open',), 'close': ('verified', 'submitted'), 'abandon': OPEN}[action]
    store = context.store
    with store.records() as db:
        row = db.one(f"SELECT id FROM fixes WHERE product=? AND machine=? AND checkout=? AND status IN ({','.join('?' * len(states))}) "
                     'ORDER BY id DESC LIMIT 1', (store.product, store.machine['id'], context.checkout.name, *states))
    return row['id'] if row else None


def abandon(context, fix_id):
    fix = load(context, fix_id)
    require_local(context, fix)
    if fix['status'] not in OPEN:
        reject(409, 'fix-state-invalid', 'fix', f"修复 #{fix_id} 的状态是 {fix['status']}")
    with context.store.records_tx() as db:
        db.execute(f"UPDATE fixes SET status='abandoned', ended_at=? WHERE id=? AND status IN ({','.join('?' * len(OPEN))})",
                   (now(), fix_id, *OPEN))
    return dict(ok=True, fix=fix_id, status='abandoned')


def active(db, store):
    """产品内进行中的修复（各机器都能看到）：编号、状态、问题场景、产出机器与检出。"""
    names = {row['id']: row['name'] for row in db.execute('SELECT id, name FROM machines')}
    return [dict(id=row['id'], status=row['status'], scenarios=[scenarios.key(p['file'], p['scenario']) for p in json.loads(row['problems'])],
                 machine=names.get(row['machine'], row['machine']), checkout=row['checkout'], operator=row['operator'],
                 here=row['machine'] == store.machine['id'], created_at=row['created_at'])
            for row in db.execute(f"SELECT * FROM fixes WHERE product=? AND status IN ({','.join('?' * len(OPEN))}) ORDER BY id",
                                  (store.product, *OPEN))]
