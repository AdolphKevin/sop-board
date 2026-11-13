"""运行：会话经 CLI 准备与提交场景执行；人工复测标注。

一次运行绑定一个检出、准备时的产品快照与执行者。结果按 检出 + 场景指纹 登记，并记下准备时的快照；
运行中途内容变化的场景只作废这些场景；执行期间产品文件变化的路径记在结果上，只作版本记录。
"""
from datetime import datetime, timedelta, timezone
from importlib.resources import files
import json
import os
from pathlib import Path
from string import Template
import shutil

import yaml

from . import config, daemon, gitops, guards, scenarios, status, workdirs
from .defect_links import normalize as normalize_defect
from .entries import identities as entry_identities, web_evidence
from .errors import APIError, reject
from .store import dumps, now

REASONS = {'new': '未执行', 'guard-failed': '守护未通过', 'failed': '上次不通过', 'rerun': '须实际执行'}
PENDING_STATES = {'not-run': 'new', 'retest': 'guard-failed', 'fail': 'failed', 'blocked': 'failed', 'skip': 'failed'}
RESULT_FIELDS = {'scenario', 'status', 'actual', 'executed_at', 'evidence', 'finding', 'note', 'executor', 'session'}


def session_id(explicit):
    """执行者：--session，其次启动器注入的 SOP_BOARD_SESSION，再次 Codex 或 Claude Code 自己的会话编号。"""
    value = (explicit or os.environ.get('SOP_BOARD_SESSION') or os.environ.get('CODEX_THREAD_ID')
             or os.environ.get('CLAUDE_CODE_SESSION_ID'))
    if not value or not value.strip():
        reject(422, 'session-required', 'session', '取不到执行者会话编号：请用 --session <会话编号> 指定（Claude Code 用会话的 session_id）')
    return value.strip()


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
                result.append(dict(scenario=key, reason=pending_reason(info)))
        return result
    try:
        keys = scenarios.resolve(requested, scenes)
    except ValueError as error:
        reject(422, 'scenario-not-found', 'scope', str(error))
    drafts = [k for k in keys if items[k]['state'] == 'draft']
    if drafts:
        reject(409, 'draft-not-runnable', 'scope', '草稿场景确认后才能执行：' + '、'.join(drafts))
    return [dict(scenario=key, reason=pending_reason(items[key])) for key in keys]


def pending_reason(info):
    """状态 → 本轮原因；修复关闭导致的待复测须实际执行（rerun）。"""
    if info.get('cause') == 'fix-closed':
        return 'rerun'
    return PENDING_STATES.get(info['state'], 'rerun')


def write_brief(directory, work_id, scope, entries, browser_problems, fixtures_declared, environment=None, invalid_files=()):
    environment = environment or {}
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
    if invalid_files:
        lines += ['', '## 未纳入的不合格文件', '', '在报告中转述以下未纳入范围的文件及问题：', '']
        lines += [f"- {p['file']}：{p['field']}（行 {p.get('line')}）：{p['problem']}" for p in invalid_files]
    missing = {name: var for name, var in (environment.get('missing_address_env') or {}).items() if name in entries}
    if missing:
        lines += ['', '## 入口地址缺失', '']
        lines += [f'- 入口 {name}：环境没有给出 {var}。用这个入口的场景写 not-executed，finding 写“入口 {name} 的地址 {var} 本机环境没有给出”。'
                  for name, var in sorted(missing.items())]
    services = environment.get('services') or []
    if services:
        lines += ['', '## 服务', '', '准备时各服务的状态（详见 services.json；执行中用 `sop-board env status --json` 看当前状态）：', '']
        for item in services:
            text = f"- {item['name']}（{item['about']}）：{item['label']}"
            if item.get('stoppable'):
                text += '，可停'
            if item.get('log'):
                text += f"，日志 {item['log']}"
            lines.append(text)
        lines += ['',
                  '按场景起点与步骤用到的业务能力，对照括号里的业务说明找到对应的服务：',
                  '',
                  '- 用到的服务本机未搭建、已停止或未上报：写 not-executed，finding 写“<服务>（<业务说明>）本机未搭建”等实际状态。不启动服务、不登记仓库、不改配置。',
                  '- 用到的服务异常退出（准备前或执行中）：写 blocked，finding 附 `env status` 给出的日志末尾，并 `sop-board attention add --kind 环境问题` 登记。',
                  '- 场景起点要求某个依赖不可用：先 `sop-board env stop <服务>` 停掉对应的可停服务，再执行；结果写好后 `sop-board env start <服务>`，或交给提交时自动恢复。'
                  'executed_at 写服务停着期间的实际执行时刻。对应的服务本机未搭建时写 not-executed；不可停，或依赖是外部共享的中间件时写 blocked 并写明原因。',
                  '- 不用到这些服务的场景照常执行。']
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
              '- 业务断言失败只记该场景失败并继续。',
              '- 等人处理的事（真实账号或未声明账号的登录、人机验证、只发到使用者本人设备的验证码、身份使用次数到上限），不论出现在开始前还是执行中：'
              '该场景写 not-executed，finding 写需要人做什么，并 `sop-board attention add` 登记（identity record 返回 identity-limit 时已自动登记）；'
              '不用这件事的场景照常执行。'
              '声明的测试账号未登录时直接用凭据变量登录，不算等人处理。',
              '- 连接异常、状态不明时停止本轮，剩余场景写 not-executed 行（带 finding）。']
    (directory / 'brief.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')


def invalid_files(found):
    return [dict(p, file=str(info['path'])) for info in found['files'].values() for p in info['problems']]


def validate_scope(context, requested, fix=None):
    """准备与启动共用的只读前置检查。"""
    from .cli import scene_board
    found, snapshot, derived = scene_board(context)
    names = {value.split('#', 1)[0] for value in requested if '#' in value}
    problems = [dict(p, file=str(info['path'])) for name, info in found['files'].items()
                if name in names for p in info['problems']]
    if problems:
        raise APIError(422, 'scenario-file-invalid', problems)
    scope = scope_for(context, found['scenarios'], derived, requested, fix)
    if not scope:
        raise APIError(409, 'nothing-to-run', [dict(field='scope', problem='没有需要执行的场景')] + invalid_files(found))
    with context.store.local() as db:
        running = db.execute("SELECT id, session FROM local_work WHERE checkout=? AND kind='run' AND status='prepared'",
                             (context.checkout.id,)).fetchone()
    if running:
        reject(409, 'run-prepared', 'run', f"检出 {context.checkout.name} 已有准备好的运行 #{running['id']}（会话 {running['session']}）；"
               f"提交或放弃后再准备：sop-board run abandon --run {running['id']}")
    return found, snapshot, derived, scope


def prepare(context, requested, *, session=None, fix_id=None, launch=None):
    project, checkout, store = context.project, context.checkout, context.store
    launch = launch or (int(os.environ['SOP_BOARD_LAUNCH']) if os.environ.get('SOP_BOARD_LAUNCH', '').isdigit() else None)
    executor = session_id(session)
    operator = config.require_operator()
    fix = None
    if fix_id is not None:
        from . import fixes
        fix = fixes.for_verify(context, fix_id, executor)
    found, snapshot, derived, scope = validate_scope(context, requested, fix)
    scenes = found['scenarios']
    environment = daemon.call(project, 'POST', '/local/env/up', dict(checkout=checkout.name), timeout=900)['environment']
    snapshot = gitops.snapshot(checkout.paths, project.exclusions(guards.test_exclusions(context)), files=True)
    selected = [scenes[item['scenario']] for item in scope]
    entries = {name: project.entries[name] for name in sorted({scene['verify'] for scene in selected})}
    from .checkouts import branch_of
    with store.local_tx() as db:
        cursor = db.execute("INSERT INTO local_work(kind,checkout,status,session,operator,implementer,fix,launch,dir,scope,branch,"
                            "created_at) VALUES ('run',?,'prepared',?,?,?,?,?,'',?,?,?)",
                            (checkout.id, executor, operator, fix['session'] if fix else None, fix_id, launch,
                             dumps(scope), branch_of(project, checkout), now()))
        work_id = cursor.lastrowid
    directory = None
    holder = f'run:{work_id}'
    try:
        browsers, browser_problems = {}, {}
        identities = sorted({identity for name, entry in entries.items() if web_entry(entry) for identity in entry_identities(entry)})
        if identities:
            leased = daemon.call(project, 'POST', '/local/browser/lease',
                                 dict(checkout=checkout.name, identities=identities, holder=holder), timeout=120)['browsers']
            browsers = {k: v['endpoint'] for k, v in leased.items() if 'endpoint' in v}
            browser_problems = {k: v['problem'] for k, v in leased.items() if 'problem' in v}
        directory = workdirs.create(project.data / 'runs', work_id)
        materialize(context, directory, work_id, scope, scenes, entries, environment, snapshot, browsers, browser_problems, fix, invalid_files(found))
    except BaseException:
        with store.local_tx() as db:
            db.execute('UPDATE browser_instances SET holder=NULL WHERE holder=?', (holder,))
            db.execute('DELETE FROM local_work WHERE id=?', (work_id,))
        if directory is not None:
            shutil.rmtree(directory, ignore_errors=True)
        raise
    product = {k: v for k, v in snapshot.items() if k != 'files'}
    with store.local_tx() as db:
        db.execute('UPDATE local_work SET dir=?, snapshot=?, product_digest=? WHERE id=?',
                   (str(directory), dumps(product), snapshot['product_digest'], work_id))
    # 部分就绪：adapter 输出就绪即可执行；没在运行的服务与缺失的入口地址只列出，由执行会话按场景判断。
    return dict(ok=True, run=work_id, checkout=checkout.name, directory=str(directory), prompt=str(directory / 'prompt.md'),
                outputs=dict(results=str(directory / 'results.jsonl')),
                session=executor, scope=scope, browser_problems=browser_problems, invalid_files=invalid_files(found),
                environment=dict(state=environment['state'], address_env=environment.get('address_env', {})),
                services=public_services(environment.get('services', [])),
                not_running=environment.get('not_running', []),
                missing_address_env=environment.get('missing_address_env', {}))


def public_services(services):
    return [{k: v for k, v in item.items() if k in ('name', 'about', 'stoppable', 'state', 'label', 'log')} for item in services]


def materialize(context, directory, work_id, scope, scenes, entries, environment, snapshot, browsers, browser_problems, fix, invalid_files=()):
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
    # 准备时各服务的状态与业务说明，供执行会话把场景起点对应到服务；提交时据此区分运行期间才异常退出的服务。
    (directory / 'services.json').write_text(json.dumps(dict(
        at=now(), environment=environment.get('state'), services=environment.get('services', []),
        not_running=environment.get('not_running', []), missing_address_env=environment.get('missing_address_env', {})),
        ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
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
    write_brief(directory, work_id, scope, entries, browser_problems, project.fixtures is not None, environment, invalid_files)
    if (directory / 'atlas.json').exists():
        with (directory / 'brief.md').open('a', encoding='utf-8') as brief:
            brief.write('\n## 交互地图\n\n- atlas.json 是仓库中的交互地图（PW_ATLAS）：用模板 atlas.mjs 按页面与控件操作；'
                        'stale 列出的页面已过期，以实际页面为准。地图不提供预期。找不到时只读探测，并在结果 note 写明地图缺口。\n')
    from .entries import web_skill
    prompt = Template(files('sop_board').joinpath('prompts', 'run.md').read_text(encoding='utf-8')).substitute(
        run=work_id, directory=directory, skill=project.skill, web_skill=web_skill() / 'SKILL.md',
        checkout=checkout.name, verify_note=('这是修复 #%d 的独立复验：逐条完整执行，不能保留旧通过，也不接受 not-executed；'
                                 '本机缺少条件时停下报告缺什么。' % fix['id']) if fix else '')
    (directory / 'prompt.md').write_text(prompt, encoding='utf-8')
    from .redaction import clean_task_output
    clean_task_output(directory, ())


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
    """本机运行（本机状态库）；运行属于另一个检出时切到那个检出。"""
    with context.store.local() as db:
        row = db.execute('SELECT * FROM local_work WHERE id=? AND kind=?', (run_id, kind)).fetchone()
        if row is None:
            reject(404, 'run-not-found', 'run', f'运行不存在：#{run_id}')
        if row['checkout'] != context.checkout.id:
            from . import checkouts
            context.checkout = checkouts.by_id(db, row['checkout'])
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


def validate_row(directory, key, row):
    extra = sorted(set(row) - RESULT_FIELDS)
    if extra:
        raise ValueError(f'{key} 的结果行有表外字段：' + '、'.join(extra) + '；每条场景都须实际执行，不能保留旧通过')
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
    if status_ in ('pass', 'fail') and not evidence:
        raise ValueError(f'{key} 实际执行的 pass、fail 至少要有一份真实证据')


INTERRUPTIONS = 'interruptions.json'


def note_interruption(directory, *, start=None, end=None, error=None):
    """在运行目录记下“环境随看板重启中断”：旧守护进程交接时写开始，新守护进程重新拉起环境后写结束（已定决策 28）。"""
    path = Path(directory) / INTERRUPTIONS
    items = interruptions(directory)
    if start:
        items.append({'reason': 'board-restart', 'text': '环境随看板重启中断', 'from': start, 'to': None})
    if end:
        for item in items:
            if item.get('to') is None:
                item['to'] = end
                if error:
                    item['error'] = error
    temporary = path.with_suffix('.json.tmp')
    temporary.write_text(json.dumps(items, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    os.replace(temporary, path)


def interruptions(directory):
    try:
        items = json.loads((Path(directory) / INTERRUPTIONS).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return []
    return items if isinstance(items, list) else []


def submit(context, run_id, *, implementer=None, implemented_by=None):
    """提交运行，依次三步（已定决策 21）：
    1. 证据文件存进本机证据目录（按内容寻址，幂等）；
    2. 一个事务把运行记录、结果与证据索引写进业务记录库；运行以“机器标识 + 本机库标识 + 本机运行编号”唯一，
       已登记过（第三步失败后重新提交）就跳过这一步；这一步失败时运行保持 prepared，结果留在运行目录；
    3. 更新本机运行状态、释放租约、恢复停着的服务。"""
    from . import checkouts, works
    work = load_work(context, run_id)
    if work['status'] != 'prepared':
        reject(409, 'run-not-prepared', 'run', f"运行 #{run_id} 的状态是 {work['status']}，不能提交")
    project, store = context.project, context.store
    with store.local() as db:
        checkout = checkouts.by_id(db, work['checkout'])
    context.checkout = checkout
    directory = Path(work['dir'])
    scope = {item['scenario']: item['reason'] for item in json.loads(work['scope'])}
    prepared = {item['key']: item for item in yaml.safe_load((directory / 'scenarios.yaml').read_text(encoding='utf-8'))}
    fixing = work['fix'] is not None
    rows = read_results(directory, scope, prepared, partial=not fixing)
    current = scenarios.scan(checkout.scenarios_dir(project), set(project.entries))['scenarios']
    voided = sorted(key for key in scope if key not in current or current[key]['fingerprint'] != prepared[key]['fingerprint'])
    crashed = crash_findings(context, work, prepared)
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
        validate_row(directory, key, row)
    before = json.loads((directory / 'product-files.json').read_text(encoding='utf-8'))
    after = gitops.snapshot(checkout.paths, project.exclusions(guards.test_exclusions(context)), files=True)['files']
    product_changed = gitops.changed_between(before, after)
    executor = work['session']
    if work['implementer']:
        # 修复复验：实现者自动取修复会话。
        implementer, implemented_by = work['implementer'], None
    else:
        implementer = implementer or (None if implemented_by else os.environ.get('SOP_BOARD_IMPLEMENTER')) or None
        implemented_by = implemented_by or (None if implementer else os.environ.get('SOP_BOARD_IMPLEMENTED_BY')) or None
    # 跨机器验收：实现者是别的操作者，记独立验收（与本机操作者相同的已在命令行以参数错误拒绝）。
    independence = 'independent' if implemented_by else status.independence(executor, implementer)
    operator = config.operator() or work['operator']
    branch = work['branch']
    with store.local() as db:
        events = [dict(r) for r in db.execute('SELECT * FROM service_events WHERE work=? ORDER BY id', (run_id,))]
    executed = {key: row for key, row in rows.items()
                if key in scope and key not in voided and row.get('status') != 'not-executed' and row.get('executed_at')}
    while_stopped = scenarios_while_stopped(events, executed)
    if while_stopped:
        with store.local_tx() as db:
            for event_id, keys in while_stopped.items():
                db.execute('UPDATE service_events SET scenarios=? WHERE id=?', (dumps(keys), event_id))
        for event in events:
            event['scenarios'] = dumps(while_stopped.get(event['id'], json.loads(event['scenarios'])))
    # 第一步：证据文件进本机证据目录。
    files_of = {}
    for key, row in rows.items():
        if key in voided or key not in scope or row.get('status') == 'not-executed':
            continue
        files_of[key] = []
        for name in row.get('evidence', []):
            raw = (directory / name).read_bytes()
            sha = store.put_evidence(raw, name)
            files_of[key].append((Path(name).name, sha, store.evidence_path(sha).stat().st_size))
    not_executed = [dict(scenario=key, finding=row['finding'], fingerprint=prepared[key]['fingerprint'])
                    for key, row in rows.items() if key in scope and key not in voided and row.get('status') == 'not-executed']
    summary = dict(recorded=[], voided=voided, not_executed=not_executed, product_changed=product_changed,
                   browser_crashes=sorted(crashed), independence=independence, submitted_by=operator,
                   implementer=implemented_by or implementer)
    interrupted = interruptions(directory)
    if interrupted:
        summary['interruptions'] = interrupted
    # 第二步：一个事务登记运行记录、结果与证据索引。
    with store.records_tx() as db:
        record, fresh = works.publish(db, store, work, checkout=checkout.name, branch=branch, result=summary,
                                      services=[service_event(e) for e in events], implementer=implementer,
                                      implemented_by=implemented_by, independence=independence)
        if fresh:
            for key, row in rows.items():
                if key in voided or key not in scope or row.get('status') == 'not-executed':
                    continue
                scene = prepared[key]
                result_id = db.insert(
                    'INSERT INTO results(product,seq,work,machine,checkout,branch,file,scenario,fingerprint,product_digest,'
                    'product_changed,kind,status,actual,finding,note,executor,implementer,implemented_by,independence,executed_at) '
                    'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (store.product, db.next_seq(), record, store.machine['id'], checkout.name, branch, scene['file'], scene['id'],
                     scene['fingerprint'], work['product_digest'], dumps(product_changed), 'agent', row['status'], row['actual'],
                     row.get('finding'), row.get('note'), executor, implementer, implemented_by, independence, row['executed_at']))
                for ordinal, (name, sha, size) in enumerate(files_of.get(key, [])):
                    store.index_evidence(db, sha, size, name)
                    db.execute('INSERT INTO result_evidence(result, ordinal, name, sha256) VALUES (?,?,?,?)', (result_id, ordinal, name, sha))
                summary['recorded'].append(dict(scenario=key, status=row['status'], result=result_id))
                guards.after_result(db, store, checkout.name, scene, row['status'], result_id)
            db.execute('UPDATE work SET result=? WHERE id=?', (dumps(summary), record))
            if fixing:
                from . import fixes
                fixes.after_verify(db, store, work, summary['recorded'])
        else:
            summary['recorded'] = [dict(scenario=scenarios.key(r['file'], r['scenario']), status=r['status'], result=r['id'])
                                   for r in db.all('SELECT id, file, scenario, status FROM results WHERE work=? ORDER BY id', (record,))]
            summary['resubmitted'] = True
    # 第三步：本机运行状态、租约与停着的服务。
    with store.local_tx() as db:
        db.execute("UPDATE local_work SET status='submitted', ended_at=?, result=?, implementer=?, record=? WHERE id=?",
                   (now(), dumps(summary), implementer or implemented_by, record, run_id))
        db.execute('UPDATE browser_instances SET holder=NULL WHERE holder=?', (f'run:{run_id}',))
    if fixing:
        from . import fixes
        fixes.after_verify_local(context, work, summary['recorded'])
    # 结果登记之后再恢复停着的服务：等待服务恢复期间不占用数据库。
    services = finish_services(context, work, reason='提交', operator=operator, crashes=True)
    if services:
        summary.update(services)
        with store.local_tx() as db:
            db.execute('UPDATE local_work SET result=? WHERE id=?', (dumps(summary), run_id))
        # 自动恢复的记录补进业务记录里的服务停启记录；连不上共享库时只留在本机，不影响已登记的结果。
        try:
            with store.records_tx() as db:
                db.execute('UPDATE work SET services=? WHERE id=?',
                           (dumps([service_event(e) for e in services.get('service_events', [])]), record))
        except APIError as error:
            summary['services_problem'] = '；'.join(p.get('problem', '') for p in error.problems)
    return dict(ok=True, run=run_id, checkout=checkout.name, **summary)


def service_event(event):
    """服务停启记录的业务部分：谁、什么时候、停启了哪个服务、停着期间执行了哪些场景。"""
    scenarios_ = event['scenarios']
    return dict(service=event['service'], action=event['action'], ok=bool(event['ok']), operator=event['operator'],
                at=event['at'], note=event.get('note'), scenarios=json.loads(scenarios_) if isinstance(scenarios_, str) else scenarios_)


def scenarios_while_stopped(events, rows):
    """每条停止事件 → 服务停着期间执行的场景（executed_at 落在停止与下一次启动或恢复之间；按秒比较，执行时间通常只精确到秒）。"""
    from .status import instant
    result = {}
    for index, event in enumerate(events):
        if event['action'] != 'stop':
            continue
        start = instant(event['at']).replace(microsecond=0)
        end = next((instant(e['at']) for e in events[index + 1:]
                    if e['service'] == event['service'] and e['action'] in ('start', 'restore')), None)
        hit = []
        for key, row in rows.items():
            try:
                moment = instant(row['executed_at'])
            except ValueError:
                continue
            if start <= moment and (end is None or moment <= end):
                hit.append(key)
        result[event['id']] = sorted(hit)
    return result


def finish_services(context, work, *, reason, operator, crashes=False):
    """运行结束（提交或放弃）时：恢复本轮停着的服务并记录；提交时另列出异常退出的服务（区分准备前与运行期间）。

    返回要并入运行摘要的字段；项目没有声明服务、本轮也没有停过服务时返回空。"""
    from . import envs
    project, checkout, store = context.project, context.checkout, context.store
    declared = project.adapter.get('services') or {}
    with store.local() as db:
        stopped_any = db.execute("SELECT 1 FROM service_events WHERE work=? AND action='stop' LIMIT 1", (work['id'],)).fetchone()
    if not declared and not stopped_any:
        return {}
    result, problems = dict(restored=[]), []
    current = None
    try:
        if stopped_any:
            result['restored'] = daemon.call(project, 'POST', '/local/env/restore', dict(
                checkout=checkout.name, run=work['id'], operator=operator, reason=reason), timeout=1800)['restored']
        if crashes and declared:
            current = daemon.call(project, 'POST', '/local/env/status', dict(checkout=checkout.name))['environments'][0]
    except daemon.DaemonUnavailable as error:
        problems.append(str(error))
        if stopped_any:
            result['restored'] = envs.withdraw(project, store, checkout, work['id'], operator=operator, reason=reason)
    except APIError as error:
        problems.append('；'.join(p.get('problem', '') for p in error.problems))
    if crashes and declared:
        try:
            prepared = {s['name']: s['state'] for s in json.loads((Path(work['dir']) / 'services.json').read_text(encoding='utf-8'))['services']}
        except (OSError, ValueError, KeyError, TypeError):
            prepared = {}
        result['crashed_services'] = [dict(service=s['name'], about=s['about'], log=s.get('log'),
                                           during_run=prepared.get(s['name']) != 'crashed')
                                      for s in (current or {}).get('services', []) if s['state'] == 'crashed']
    with store.local() as db:
        result['service_events'] = [dict(service=r['service'], action=r['action'], ok=bool(r['ok']), operator=r['operator'],
                                         at=r['at'], note=r['note'], scenarios=json.loads(r['scenarios']))
                                    for r in db.execute('SELECT * FROM service_events WHERE work=? ORDER BY id', (work['id'],))]
    if problems:
        result['services_problem'] = '；'.join(problems)
    return result


def crash_findings(context, work, prepared):
    """租约期间浏览器异常退出：受影响身份的 Web 场景 → 受阻说明。"""
    project = context.project
    holder = f"run:{work['id']}"
    with context.store.local() as db:
        rows = list(db.execute('SELECT identity, crashes FROM browser_instances WHERE checkout=?', (work['checkout'],)))
    crashed = {}
    for row in rows:
        for crash in json.loads(row['crashes']):
            if crash.get('holder') == holder and crash['at'] >= work['created_at']:
                from .browsers import exit_description
                text = exit_description(crash['at'], crash.get('code'), crash.get('signal'), crash['log'])
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
    # 放弃的运行只在本机：不写业务记录。
    with context.store.local_tx() as db:
        db.execute("UPDATE local_work SET status='abandoned', ended_at=? WHERE id=?", (now(), run_id))
        db.execute('UPDATE browser_instances SET holder=NULL WHERE holder=?', (f'run:{run_id}',))
    services = finish_services(context, work, reason='放弃', operator=config.operator() or work['operator'])
    if services.get('service_events'):
        with context.store.local_tx() as db:
            db.execute('UPDATE local_work SET result=? WHERE id=?', (dumps(dict(abandoned_by=config.operator() or work['operator'], **services)), run_id))
    return dict(ok=True, run=run_id, status='abandoned', **services)


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
    store = context.store
    from .checkouts import branch_of
    book = derived['book']
    # of_result：本看板当时针对的那条 Agent 结果（当前指纹上参与推导的最新一条 Agent 结果）。
    latest = next((r for r in book.select((scene['file'], scene['id'], scene['fingerprint'])).records if r['kind'] == 'agent'), None)
    with store.records_tx() as db:
        result_id = db.insert(
            'INSERT INTO results(product,seq,machine,checkout,branch,file,scenario,fingerprint,product_digest,kind,status,actual,'
            'defect,note,of_result,marked_by,executed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (store.product, db.next_seq(), store.machine['id'], context.checkout.name, branch_of(context.project, context.checkout),
             scene['file'], scene['id'], scene['fingerprint'], snapshot['product_digest'], 'manual', verdict,
             '人工复测' + status.MANUAL[verdict], defect, note, latest['id'] if latest else None, by, now()))
        guards.after_result(db, store, context.checkout.name, scene, verdict, result_id)
    return dict(ok=True, scenario=key, status=verdict, label=status.MANUAL[verdict], result=result_id,
                marked_by=by, defect=defect, note=note)
