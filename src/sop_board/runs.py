"""运行：会话经 CLI 准备与提交场景执行；人工复测标注。

一次运行绑定一个检出、准备时的产品快照与执行者。结果按 检出 + 场景指纹 + 快照 登记；
运行中途内容变化的场景只作废这些场景；执行期间产品文件变化的路径记在结果上。
"""
from datetime import datetime, timedelta, timezone
from importlib.resources import files
import json
import os
from pathlib import Path
from string import Template
import shutil
import subprocess

import yaml

from . import config, daemon, gitops, guards, scenarios, status
from .defect_links import normalize as normalize_defect
from .entries import identities as entry_identities, web_evidence
from .errors import APIError, reject
from .store import dumps, now

REASONS = {'new': '未执行', 'source-changed': '产品变化待判定', 'failed': '上次不通过', 'rerun': '须实际执行'}
PENDING_STATES = {'not-run': 'new', 'retest': 'source-changed', 'fail': 'failed', 'blocked': 'failed', 'skip': 'failed'}


def session_id(explicit):
    """执行者：--session，其次启动器注入的 SOP_BOARD_SESSION，再次 Codex 或 Claude Code 自己的会话编号。"""
    value = (explicit or os.environ.get('SOP_BOARD_SESSION') or os.environ.get('CODEX_THREAD_ID')
             or os.environ.get('CLAUDE_CODE_SESSION_ID'))
    if not value or not value.strip():
        reject(422, 'session-required', 'session', '取不到执行者会话编号：请用 --session <会话编号> 指定（Claude Code 用会话的 session_id）')
    return value.strip()


def run_dir(project, work_id):
    return project.data / 'runs' / str(work_id)


def web_entry(entry):
    return isinstance(entry, dict) and entry.get('surface') == 'web' and entry.get('driver') in (None, 'playwright-acceptance')


def scope_for(context, scenes, derived, requested, fix=None):
    """返回 [{scenario, reason}]。requested：'pending'、'all' 或场景列表。"""
    items = derived['items']
    if fix is not None:
        keys = [scenarios.key(p['file'], p['scenario']) for p in fix['problems']]
        missing = [k for k in keys if k not in scenes]
        if missing:
            reject(409, 'fix-scenario-missing', 'fix', '修复的问题场景已不在当前场景文件中：' + '、'.join(missing))
        return [dict(scenario=k, reason='rerun') for k in keys]
    if requested in (['pending'], ['all']):
        result = []
        for key in sorted(scenes):
            info = items[key]
            if info['state'] == 'draft':
                continue
            if requested == ['all']:
                result.append(dict(scenario=key, reason='rerun'))
            elif info['state'] in PENDING_STATES:
                reason = PENDING_STATES[info['state']]
                if info['state'] == 'retest' and info['reason'].startswith('修复已关闭'):
                    reason = 'rerun'
                result.append(dict(scenario=key, reason=reason))
        return result
    try:
        keys = scenarios.resolve(requested, scenes)
    except ValueError as error:
        reject(422, 'scenario-not-found', 'scope', str(error))
    drafts = [k for k in keys if items[k]['state'] == 'draft']
    if drafts:
        reject(409, 'draft-not-runnable', 'scope', '草稿场景确认后才能执行：' + '、'.join(drafts))
    result = []
    for key in keys:
        info = items[key]
        reason = PENDING_STATES.get(info['state'], 'rerun')
        if info['state'] == 'retest' and info['reason'].startswith('修复已关闭'):
            reason = 'rerun'
        result.append(dict(scenario=key, reason=reason))
    return result


def previous_pass(db, checkout_id, scene):
    """同一指纹上最近的通过结果（影响判定与保留通过的依据）。"""
    row = db.execute("SELECT * FROM results WHERE checkout=? AND file=? AND scenario=? AND fingerprint=? AND kind='agent' "
                     "AND status='pass' ORDER BY id DESC LIMIT 1",
                     (checkout_id, scene['file'], scene['id'], scene['fingerprint'])).fetchone()
    if row is None:
        return None
    evidence = [dict(name=e['name'], sha256=e['sha256']) for e in
                db.execute('SELECT name, sha256 FROM result_evidence WHERE result=? ORDER BY ordinal', (row['id'],))]
    work = db.execute('SELECT snapshot FROM work WHERE id=?', (row['work'],)).fetchone() if row['work'] else None
    commits = {alias: info.get('commit') for alias, info in json.loads(work['snapshot']).get('repos', {}).items()} if work else {}
    return dict(result=row['id'], executed_at=row['executed_at'], product_digest=row['product_digest'],
                product_changed=json.loads(row['product_changed']), note=row['note'], evidence=evidence, commits=commits)


def changes_since(checkout, previous, snapshot):
    """每个仓库自上次通过时提交以来的改动（含当前未提交改动）。"""
    commits = {}
    for item in previous.values():
        for alias, commit in item['commits'].items():
            if commit:
                commits.setdefault(alias, set()).add(commit)
    result = {}
    for alias, root in checkout.paths.items():
        entry = dict(current=snapshot['repos'][alias]['commit'], previous=[],
                     uncommitted=[p[len(alias) + 1:] for p in snapshot['changed'] if p.startswith(alias + '/')])
        for commit in sorted(commits.get(alias, ())):
            try:
                diff = os.fsdecode(gitops.git(root, 'diff', '--name-status', commit, '--'))
            except subprocess.SubprocessError:
                diff = '旧提交不可读取，不能仅据此判定不受影响'
            entry['previous'].append(dict(commit=commit, files=diff))
        result[alias] = entry
    return result


def write_brief(directory, work_id, scope, entries, browser_problems, fixtures_declared):
    counts = {}
    for item in scope:
        counts[item['reason']] = counts.get(item['reason'], 0) + 1
    lines = [f'# 运行 #{work_id} 速查卡', '', '先读本卡与 prompt.md；参考文档只在遇到对应情况时再读。', '', '## 本轮范围', '',
             f"- {len(scope)} 个场景：" + '，'.join(f"{REASONS[k]} {v}" for k, v in sorted(counts.items())) + '（明细见 pending.json）。']
    for name, entry in sorted(entries.items()):
        extra = ''
        if isinstance(entry, dict) and entry.get('surface') == 'web':
            policy = web_evidence(entry)
            extra = f"，录屏{'开' if policy['video'] else '关'}、trace {'开' if policy['trace'] else '关'}"
        lines.append(f"- 入口 {name}（{entry.get('surface')}）：证据 {'、'.join(entry.get('evidence', []))}{extra}")
    if browser_problems:
        lines += ['', '## 浏览器问题', '']
        lines += [f'- 身份 {identity}：{problem}。用这个身份的 Web 场景直接写 blocked，finding 照抄这句。'
                  for identity, problem in sorted(browser_problems.items())]
    lines += ['', '## 前置数据', '']
    if fixtures_declared:
        lines += [f'- 无关前置数据用 sop-board fixtures run <配方> --run {work_id} --scenario <场景>；没有合适配方且 adhoc 为 true 时才用 fixtures exec。',
                  '- real_only 对象与被验收流程应产生的对象不得写库代替；准备不了就记受阻，finding 写缺什么。']
    else:
        lines.append('- 项目没有声明前置数据配方：前置不满足时记受阻，finding 写明缺什么，不自行写库。')
    lines += ['', '## 时间上限与停止', '',
              '- 单场景 4 分钟、定位或操作 15 秒、导航 30 秒（模板缺省）。单个场景累计 15 分钟或 4 次仍无结论时记下证据转下一个。',
              '- 业务断言失败只记该场景失败并继续；连接异常、授权等待、状态不明时停止本轮，剩余场景写 not-executed 行（带 finding）。']
    (directory / 'brief.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')


def prepare(context, requested, *, session=None, fix_id=None, launch=None):
    project, checkout, store = context.project, context.checkout, context.store
    launch = launch or (int(os.environ['SOP_BOARD_LAUNCH']) if os.environ.get('SOP_BOARD_LAUNCH', '').isdigit() else None)
    executor = session_id(session)
    operator = config.require_operator()
    from .cli import scene_board
    found, snapshot, derived = scene_board(context)
    scenes = found['scenarios']
    fix = None
    if fix_id is not None:
        from . import fixes
        fix = fixes.for_verify(context, fix_id, executor)
    scope = scope_for(context, scenes, derived, requested, fix)
    if not scope:
        reject(409, 'nothing-to-run', 'scope', '没有需要执行的场景')
    with store.connection() as db:
        running = db.execute("SELECT id, session FROM work WHERE checkout=? AND kind='run' AND status='prepared'",
                             (checkout.id,)).fetchone()
    if running:
        reject(409, 'run-prepared', 'run', f"检出 {checkout.name} 已有准备好的运行 #{running['id']}（会话 {running['session']}）；"
                                           f"提交或放弃后再准备：sop-board run abandon --run {running['id']}")
    environment = daemon.call(project, 'POST', '/local/env/up', dict(checkout=checkout.name), timeout=900)['environment']
    snapshot = gitops.snapshot(checkout.paths, project.exclusions(guards.test_exclusions(context)), files=True)
    selected = [scenes[item['scenario']] for item in scope]
    entries = {name: project.entries[name] for name in sorted({scene['verify'] for scene in selected})}
    with store.transaction() as db:
        cursor = db.execute("INSERT INTO work(kind,checkout,status,session,operator,implementer,fix,launch,dir,scope,created_at) "
                            "VALUES ('run',?,'prepared',?,?,?,?,?,'',?,?)",
                            (checkout.id, executor, operator, fix['session'] if fix else None, fix_id, launch,
                             dumps(scope), now()))
        work_id = cursor.lastrowid
    directory = run_dir(project, work_id)
    holder = f'run:{work_id}'
    try:
        browsers, browser_problems = {}, {}
        identities = sorted({identity for name, entry in entries.items() if web_entry(entry) for identity in entry_identities(entry)})
        if identities:
            leased = daemon.call(project, 'POST', '/local/browser/lease',
                                 dict(checkout=checkout.name, identities=identities, holder=holder), timeout=120)['browsers']
            browsers = {k: v['endpoint'] for k, v in leased.items() if 'endpoint' in v}
            browser_problems = {k: v['problem'] for k, v in leased.items() if 'problem' in v}
        directory.mkdir(parents=True, exist_ok=False)
        materialize(context, directory, work_id, scope, scenes, entries, environment, snapshot, browsers, browser_problems, fix)
    except BaseException:
        with store.transaction() as db:
            db.execute('UPDATE browser_instances SET holder=NULL WHERE holder=?', (holder,))
            db.execute('DELETE FROM work WHERE id=?', (work_id,))
        shutil.rmtree(directory, ignore_errors=True)
        raise
    product = {k: v for k, v in snapshot.items() if k != 'files'}
    with store.transaction() as db:
        db.execute('UPDATE work SET dir=?, snapshot=?, product_digest=? WHERE id=?',
                   (str(directory), dumps(product), snapshot['product_digest'], work_id))
    return dict(ok=True, run=work_id, checkout=checkout.name, directory=str(directory), prompt=str(directory / 'prompt.md'),
                outputs=dict(results=str(directory / 'results.jsonl'), impact=str(directory / 'impact.json')),
                session=executor, scope=scope, browser_problems=browser_problems,
                environment=dict(state=environment['state'], address_env=environment.get('address_env', {})))


def materialize(context, directory, work_id, scope, scenes, entries, environment, snapshot, browsers, browser_problems, fix):
    project, checkout = context.project, context.checkout
    reasons = {item['scenario']: item['reason'] for item in scope}
    selected = [dict(scenes[key], key=key, reason=reasons[key]) for key in reasons]
    (directory / 'evidence').mkdir()
    (directory / 'scenarios.yaml').write_text(yaml.safe_dump(
        [{k: v for k, v in scene.items() if k not in ('status',)} for scene in selected],
        allow_unicode=True, sort_keys=False), encoding='utf-8')
    (directory / 'pending.json').write_text(dumps(scope), encoding='utf-8')
    addresses = environment.get('address_env', {})
    (directory / 'entries.json').write_text(dumps(dict(entries=entries, addresses={
        name: addresses.get(entry.get('address_env')) for name, entry in entries.items() if entry.get('address_env')})), encoding='utf-8')
    (directory / 'repos.json').write_text(dumps({alias: str(path) for alias, path in checkout.paths.items()}), encoding='utf-8')
    (directory / 'snapshot.json').write_text(dumps({k: v for k, v in snapshot.items() if k != 'files'}), encoding='utf-8')
    (directory / 'product-files.json').write_text(dumps(snapshot['files']), encoding='utf-8')
    with context.store.connection() as db:
        previous = {item['key']: previous_pass(db, checkout.id, item) for item in selected if item['reason'] == 'source-changed'}
    previous = {k: v for k, v in previous.items() if v}
    (directory / 'previous-results.json').write_text(dumps(previous), encoding='utf-8')
    (directory / 'changes.json').write_text(dumps(changes_since(checkout, previous, snapshot)), encoding='utf-8')
    web = [scene for scene in selected if web_entry(entries[scene['verify']])]
    env_lines = {**addresses}
    runtime = None
    if web:
        from . import browsers as broker
        from .playwright import write_scope
        runtime = broker.runtime_info()
        write_scope(directory / 'scope.json', project.name, checkout.name, web, entries, directory / 'scenarios.yaml')
        identities = {name: [dict(item, endpoint=browsers.get(item['id'])) for item in entry.get('identities', [])]
                      or [dict(id='default', label='default', endpoint=browsers.get('default'))]
                      for name, entry in entries.items() if web_entry(entry)}
        (directory / 'identities.json').write_text(dumps(dict(entries=identities)), encoding='utf-8')
        maps = atlas_excerpt(context, entries)
        if maps:
            (directory / 'atlas.json').write_text(dumps(dict(entries=maps)), encoding='utf-8')
            env_lines['PW_ATLAS'] = str(directory / 'atlas.json')
            if len(maps) == 1:
                env_lines['PW_ATLAS_ENTRY'] = maps[0]['entry']
        endpoint = browsers.get('default') or next(iter(browsers.values()), None)
        if endpoint:
            env_lines['PW_CDP_ENDPOINT'] = endpoint
        env_lines.update(PW_IDENTITIES=str(directory / 'identities.json'), PW_SCOPE=str(directory / 'scope.json'),
                         PW_SCENARIO_FILE=str(directory / 'scenarios.yaml'), PW_OUT=str(directory / 'playwright'))
        if runtime:
            env_lines.update(PLAYWRIGHT_BROWSERS_PATH=runtime['browsers'], PW_NODE_ROOT=str(Path(runtime['node_modules']).parent))
            (directory / 'node_modules').symlink_to(runtime['node_modules'], target_is_directory=True)
    env_lines.update(SOP_BOARD_RUN=str(work_id), SOP_BOARD_CHECKOUT=checkout.name)
    (directory / 'run.env').write_text(''.join(f"{name}={value}\n" for name, value in sorted(env_lines.items())), encoding='utf-8')
    write_brief(directory, work_id, scope, entries, browser_problems, project.fixtures is not None)
    if (directory / 'atlas.json').exists():
        with (directory / 'brief.md').open('a', encoding='utf-8') as brief:
            brief.write('\n## 交互地图\n\n- atlas.json 是仓库中的交互地图（PW_ATLAS）：用模板 atlas.mjs 按页面与控件操作；'
                        'stale 列出的页面已过期，以实际页面为准。地图不提供预期。找不到时只读探测，并在结果 note 写明地图缺口。\n')
    from .entries import web_skill
    prompt = Template(files('sop_board').joinpath('prompts', 'run.md').read_text(encoding='utf-8')).substitute(
        run=work_id, directory=directory, skill=project.skill, web_skill=web_skill() / 'SKILL.md',
        checkout=checkout.name, verify_note=('这是修复 #%d 的独立复验：逐条实际执行，不能保留旧通过。' % fix['id']) if fix else '')
    (directory / 'prompt.md').write_text(prompt, encoding='utf-8')


def atlas_excerpt(context, entries):
    """本轮 Web 入口在仓库中的交互地图（含过期页面），供模板 atlas.mjs 按页面与控件操作。"""
    from . import atlas
    result = []
    for name, entry in entries.items():
        if not (web_entry(entry) and isinstance(entry.get('atlas'), dict) and entry.get('spec_library')):
            continue
        target, _ = atlas.paths(context.project, context.checkout, name)
        if not target.is_file():
            continue
        document = json.loads(target.read_text(encoding='utf-8'))
        stale = atlas.stale(context, name) or []
        result.append(dict(entry=name, pages=document['pages'],
                           index=[dict(id=page['id'], title=page['title']) for page in document['pages']],
                           stale=stale))
    return result


# ---- 提交 ----

def load_work(context, run_id, kind='run'):
    with context.store.connection() as db:
        row = db.execute('SELECT * FROM work WHERE id=? AND kind=?', (run_id, kind)).fetchone()
    if row is None:
        reject(404, 'run-not-found', 'run', f'运行不存在：#{run_id}')
    if row['checkout'] != context.checkout.id:
        from . import checkouts
        with context.store.connection() as db:
            other = checkouts.by_id(db, row['checkout'])
        context.checkout = other
    return dict(row)


def _strict_json(raw):
    def pairs(items):
        result = {}
        for name, value in items:
            if name in result:
                raise ValueError('重复字段：' + name)
            result[name] = value
        return result
    return json.loads(raw, object_pairs_hook=pairs, parse_constant=lambda v: (_ for _ in ()).throw(ValueError('非有限数字')))


def read_results(directory, scope, scenes, *, partial):
    path = directory / 'results.jsonl'
    if not path.is_file() or path.is_symlink():
        raise ValueError('results.jsonl 不存在')
    by_id = {}
    for key in scope:
        by_id.setdefault(key.split('#', 1)[1], []).append(key)
    rows = {}
    for number, line in enumerate(path.read_text(encoding='utf-8').splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = _strict_json(line)
        except ValueError as error:
            raise ValueError(f'results.jsonl 第 {number} 行不是合格的 JSON：{error}') from error
        if not isinstance(row, dict):
            raise ValueError(f'results.jsonl 第 {number} 行应为对象')
        value = row.get('scenario')
        key = value if value in scope else (by_id.get(value, [None])[0] if len(by_id.get(value, [])) == 1 else None)
        if key is None:
            raise ValueError(f'结果不在本轮范围内：{value}')
        if key in rows:
            raise ValueError(f'结果重复：{key}')
        rows[key] = row
    return rows


def validate_row(directory, key, row, reason, previous, impact):
    status_ = row.get('status')
    if status_ == 'not-executed':
        if not isinstance(row.get('finding'), str) or not row['finding'].strip() or row.get('evidence'):
            raise ValueError(f'{key} 的未执行标记须写 finding，且不附证据')
        return
    if status_ not in ('pass', 'fail', 'blocked', 'skip'):
        raise ValueError(f'{key} 的 status 应为 pass、fail、blocked、skip 或 not-executed')
    if not isinstance(row.get('actual'), str) or not row['actual'].strip():
        raise ValueError(f'{key} 缺少 actual')
    try:
        stamp = datetime.fromisoformat(str(row.get('executed_at', '')).replace('Z', '+00:00'))
        if stamp.utcoffset() is None or stamp > datetime.now(timezone.utc) + timedelta(minutes=5):
            raise ValueError()
    except ValueError:
        raise ValueError(f'{key} 的 executed_at 须为带时区的实际执行时间') from None
    if status_ in ('blocked', 'skip') and not (isinstance(row.get('finding'), str) and row['finding'].strip()):
        raise ValueError(f'{key} 为 {status_} 时须写 finding')
    kept = row.get('kept', False)
    if type(kept) is not bool:
        raise ValueError(f'{key} 的 kept 须为布尔值')
    if kept:
        if reason != 'source-changed' or previous is None or status_ != 'pass':
            raise ValueError(f'{key} 不符合按影响判定保留通过的条件：只有产品变化待判定、且同一内容此前通过的场景可以保留')
        if not isinstance(row.get('note'), str) or len(row['note'].strip()) < 4:
            raise ValueError(f'{key} 保留通过须在 note 写明改动范围、依赖关系与不受影响的理由')
    if key in impact and impact[key] == kept:
        raise ValueError(f'{key} 的 impact.json 判定与结果不一致：' + ('判定受影响，必须实际执行' if impact[key] else '判定不受影响，必须保留通过'))
    evidence = row.get('evidence', [])
    if not isinstance(evidence, list):
        raise ValueError(f'{key} 的 evidence 应为路径数组')
    base = (directory / 'evidence').resolve()
    for name in evidence:
        if not isinstance(name, str) or Path(name).is_absolute() or '..' in Path(name).parts or not name.startswith('evidence/'):
            raise ValueError(f'{key} 的证据须为 evidence/ 下的相对路径')
        path = directory / name
        if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(base):
            raise ValueError(f'{key} 的证据文件不存在或越界：{name}')
        if path.stat().st_size > 64 * 1024 * 1024:
            raise ValueError(f'{key} 的证据超过 64 MiB：{name}')
    if status_ in ('pass', 'fail') and not kept and not evidence:
        raise ValueError(f'{key} 实际执行的 pass、fail 至少要有一份真实证据')


def read_impact(directory, changed):
    if not changed:
        return {}
    path = directory / 'impact.json'
    if not path.is_file() or path.is_symlink():
        raise ValueError('、'.join(sorted(changed)) + ' 缺少 impact.json 影响判定')
    decisions = _strict_json(path.read_text(encoding='utf-8'))
    if not isinstance(decisions, list):
        raise ValueError('impact.json 应为数组')
    impact = {}
    for item in decisions:
        if not isinstance(item, dict) or item.get('scenario') not in changed or item['scenario'] in impact:
            raise ValueError(f'impact.json 场景重复或不在待判定范围内：{item.get("scenario") if isinstance(item, dict) else item}')
        if type(item.get('affected')) is not bool or not isinstance(item.get('reason'), str) or not item['reason'].strip():
            raise ValueError(f"{item['scenario']} 的 impact.json 须有布尔值 affected 与非空 reason")
        impact[item['scenario']] = item['affected']
    missing = changed - impact.keys()
    if missing:
        raise ValueError('、'.join(sorted(missing)) + ' 缺少 impact.json 影响判定')
    return impact


def submit(context, run_id, *, implementer=None):
    from . import checkouts
    work = load_work(context, run_id)
    if work['status'] != 'prepared':
        reject(409, 'run-not-prepared', 'run', f"运行 #{run_id} 的状态是 {work['status']}，不能提交")
    project, store = context.project, context.store
    with store.connection() as db:
        checkout = checkouts.by_id(db, work['checkout'])
    context.checkout = checkout
    directory = Path(work['dir'])
    scope = {item['scenario']: item['reason'] for item in json.loads(work['scope'])}
    prepared = {item['key']: item for item in yaml.safe_load((directory / 'scenarios.yaml').read_text(encoding='utf-8'))}
    previous = json.loads((directory / 'previous-results.json').read_text(encoding='utf-8'))
    fixing = work['fix'] is not None
    rows = read_results(directory, scope, prepared, partial=not fixing)
    changed = {key for key, reason in scope.items() if reason == 'source-changed' and key in previous}
    current = scenarios.scan(checkout.scenarios_dir(project), set(project.entries))['scenarios']
    voided = sorted(key for key in scope if key not in current or current[key]['fingerprint'] != prepared[key]['fingerprint'])
    impact = read_impact(directory, changed - set(voided))
    crashed = crash_findings(context, work, prepared)
    not_executed, recorded = [], []
    for key in scope:
        if key in voided:
            continue
        row = rows.get(key)
        if key in crashed and (row is None or row.get('status') != 'pass'):
            rows[key] = row = dict(scenario=key, status='blocked', actual=crashed[key], finding=crashed[key],
                                   executed_at=now(), evidence=[e for e in (row or {}).get('evidence', [])
                                                                if isinstance(e, str)])
            continue
        if row is None:
            raise ValueError(f'{key} 缺少结果行')
        if row.get('status') == 'not-executed' and fixing:
            raise ValueError(f'{key}：独立复验须完整执行，不接受未执行标记')
        if row.get('kept') and scope[key] == 'rerun':
            raise ValueError(f'{key} 须实际执行并保存新证据，不能保留旧通过')
        validate_row(directory, key, row, scope[key], previous.get(key), impact)
    before = json.loads((directory / 'product-files.json').read_text(encoding='utf-8'))
    after = gitops.snapshot(checkout.paths, project.exclusions(guards.test_exclusions(context)), files=True)['files']
    product_changed = gitops.changed_between(before, after)
    executor = work['session']
    implementer = work['implementer'] or implementer or os.environ.get('SOP_BOARD_IMPLEMENTER') or None
    independence = status.independence(executor, implementer)
    with store.transaction() as db:
        for key, row in rows.items():
            if key in voided or key not in scope:
                continue
            if row.get('status') == 'not-executed':
                not_executed.append(dict(scenario=key, finding=row['finding']))
                continue
            scene = prepared[key]
            reused = previous[key]['result'] if row.get('kept') else None
            cursor = db.execute(
                'INSERT INTO results(work,checkout,file,scenario,fingerprint,product_digest,product_changed,kind,status,actual,'
                'finding,note,reused_from,executor,implementer,independence,executed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (run_id, checkout.id, scene['file'], scene['id'], scene['fingerprint'], work['product_digest'],
                 dumps(product_changed), 'agent', row['status'], row['actual'], row.get('finding'), row.get('note'),
                 reused, executor, implementer, independence, row['executed_at']))
            result_id = cursor.lastrowid
            evidence = [(Path(name).name, (directory / name).read_bytes()) for name in row.get('evidence', [])]
            if reused:
                evidence = [(e['name'], store.read_evidence(e['sha256'])) for e in previous[key]['evidence']] + evidence
            for ordinal, (name, raw) in enumerate(evidence):
                sha = store.save_evidence(db, raw, name)
                db.execute('INSERT INTO result_evidence VALUES (?,?,?,?)', (result_id, ordinal, name, sha))
            recorded.append(dict(scenario=key, status=row['status'], result=result_id, kept=bool(reused)))
            guards.after_result(db, checkout, scene, row['status'], result_id)
        summary = dict(recorded=recorded, voided=voided, not_executed=not_executed, product_changed=product_changed,
                       browser_crashes=sorted(crashed), independence=independence)
        db.execute("UPDATE work SET status='submitted', ended_at=?, result=?, implementer=? WHERE id=?",
                   (now(), dumps(summary), implementer, run_id))
        db.execute('UPDATE browser_instances SET holder=NULL WHERE holder=?', (f'run:{run_id}',))
        if fixing:
            from . import fixes
            fixes.after_verify(db, work, recorded)
    return dict(ok=True, run=run_id, checkout=checkout.name, **summary)


def crash_findings(context, work, prepared):
    """租约期间浏览器异常退出：受影响身份的 Web 场景 → 受阻说明。"""
    project = context.project
    holder = f"run:{work['id']}"
    with context.store.connection() as db:
        rows = list(db.execute('SELECT identity, crashes FROM browser_instances WHERE checkout=?', (work['checkout'],)))
    crashed = {}
    for row in rows:
        for crash in json.loads(row['crashes']):
            if crash.get('holder') == holder and crash['at'] >= work['created_at']:
                moment = datetime.fromisoformat(crash['at']).astimezone().strftime('%Y-%m-%d %H:%M:%S')
                text = f"浏览器进程 {moment} 因 {crash.get('signal') or '退出码 ' + str(crash.get('code'))} 退出，日志 {crash['log']}"
                report = crash.get('report')
                if not report:
                    from .browsers import crash_report
                    report = crash_report(datetime.fromisoformat(crash['at']).timestamp() - 30)
                if report:
                    text += f"，崩溃报告 {report}"
                crashed[row['identity']] = text
    result = {}
    for key, scene in prepared.items():
        entry = project.entries.get(scene['verify'], {})
        if web_entry(entry):
            hit = [crashed[i] for i in entry_identities(entry) if i in crashed]
            if hit:
                result[key] = hit[0]
    return result


def abandon(context, run_id):
    work = load_work(context, run_id)
    if work['status'] != 'prepared':
        reject(409, 'run-not-prepared', 'run', f"运行 #{run_id} 的状态是 {work['status']}")
    with context.store.transaction() as db:
        db.execute("UPDATE work SET status='abandoned', ended_at=? WHERE id=?", (now(), run_id))
        db.execute('UPDATE browser_instances SET holder=NULL WHERE holder=?', (f'run:{run_id}',))
    return dict(ok=True, run=run_id, status='abandoned')


# ---- 人工复测 ----

def mark(context, scene_key, verdict, *, by=None, defect=None, note=None):
    """测试同学（或本机操作者）对场景当前内容的复测结论：通过、不通过、阻塞或需求变更。

    复测人与缺陷链接都可不填；不填复测人时记为未署名。
    """
    from .cli import scene_board
    if verdict not in status.MANUAL:
        reject(422, 'mark-invalid', 'verdict', '复测结论只能是通过、不通过、阻塞或需求变更')
    if by is not None and (not isinstance(by, str) or len(by) > 64):
        reject(422, 'mark-invalid', 'by', '复测人姓名须为文字，最长 64 字')
    if note is not None and (not isinstance(note, str) or len(note) > 4000):
        reject(422, 'mark-invalid', 'note', '说明须为文字，最长 4000 字')
    by = (by or '').strip() or None
    note = (note or '').strip() or None
    defect = None if verdict == 'pass' else normalize_defect(defect)
    found, snapshot, derived = scene_board(context)
    try:
        key = scenarios.resolve([scene_key], found['scenarios'])[0]
    except ValueError as error:
        reject(404, 'scenario-not-found', 'scenario', str(error))
    scene = found['scenarios'][key]
    if scene.get('status') == 'draft':
        reject(409, 'draft-not-runnable', 'scenario', '草稿场景确认后才能复测')
    with context.store.transaction() as db:
        latest = db.execute("SELECT id FROM results WHERE checkout=? AND file=? AND scenario=? AND fingerprint=? AND kind='agent' "
                            'ORDER BY id DESC LIMIT 1', (context.checkout.id, scene['file'], scene['id'], scene['fingerprint'])).fetchone()
        cursor = db.execute(
            'INSERT INTO results(checkout,file,scenario,fingerprint,product_digest,kind,status,actual,defect,note,of_result,'
            'marked_by,executed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (context.checkout.id, scene['file'], scene['id'], scene['fingerprint'], snapshot['product_digest'], 'manual',
             verdict, '人工复测' + status.MANUAL[verdict], defect, note, latest['id'] if latest else None, by, now()))
        guards.after_result(db, context.checkout, scene, verdict, cursor.lastrowid)
    return dict(ok=True, scenario=key, status=verdict, label=status.MANUAL[verdict], result=cursor.lastrowid,
                marked_by=by, defect=defect, note=note)
