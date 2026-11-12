"""守护：产品仓库 <场景目录>/regression.yaml 登记场景到低成本测试的关联。

守护有效（缺一不可）：场景存在且指纹相同；测试文件是仓库内的普通文件且摘要一致；coverage 逐条按顺序覆盖
全部 expected 与 forbidden_effects 且文字与当前一致；检出能力证据存在；acceptance 是同指纹的独立真实入口通过；
没有登记过漏检。只覆盖部分预期的守护不写进索引。
"""
import hashlib
from pathlib import Path
import re

import yaml

from . import gitops
from .config import strict_yaml
from .scenarios import GUARD_INDEX, split_key
from .store import now

LAYERS = ('unit', 'api', 'component', 'browser')
COMMAND_ID = re.compile(r'[a-z0-9][a-z0-9_-]*\Z')
SHA256 = re.compile(r'[0-9a-f]{64}\Z')


def index_path_for(project, checkout):
    directory = checkout.scenarios_dir(project)
    return directory / GUARD_INDEX if directory else None


def index_path(context):
    return index_path_for(context.project, context.checkout)


def _relative(value):
    return (isinstance(value, str) and value.strip() and '\0' not in value and not Path(value).is_absolute()
            and '..' not in Path(value).parts)


def validate_index(value):
    """结构校验；返回问题列表。"""
    problems = []

    def problem(field, message):
        problems.append(dict(field=field, problem=message))
    if not isinstance(value, dict) or value.get('version') != 1:
        return [dict(field='version', problem='须为 1')]
    for extra in value.keys() - {'version', 'commands', 'guards'}:
        problem(extra, '格式外字段')
    commands = value.get('commands', {})
    if not isinstance(commands, dict):
        problem('commands', '须为命令编号到命令的对象')
        commands = {}
    for name, command in commands.items():
        field = f'commands.{name}'
        if not COMMAND_ID.fullmatch(str(name)) or not isinstance(command, dict):
            problem(field, '命令编号须为小写编号，声明须为对象')
            continue
        for extra in command.keys() - {'repo', 'cwd', 'argv', 'timeout', 'browser', 'env'}:
            problem(f'{field}.{extra}', '格式外字段')
        if not isinstance(command.get('repo'), str):
            problem(field + '.repo', '须为仓库别名')
        if 'cwd' in command and not (_relative(command['cwd']) or command['cwd'] == '.'):
            problem(field + '.cwd', '须为仓内相对目录')
        argv = command.get('argv')
        if not isinstance(argv, list) or not argv or not all(isinstance(x, str) and x and '\0' not in x for x in argv):
            problem(field + '.argv', '须为非空参数数组，不经过 shell')
        if type(command.get('timeout', 900)) is not int or not 1 <= command.get('timeout', 900) <= 7200:
            problem(field + '.timeout', '须为 1～7200 秒')
        if 'browser' in command and not isinstance(command['browser'], str):
            problem(field + '.browser', '须为身份编号')
        if 'env' in command and (not isinstance(command['env'], dict) or not all(
                re.fullmatch('[A-Z_][A-Z0-9_]*', k) and not k.startswith('SOP_') and isinstance(v, str)
                for k, v in command['env'].items())):
            problem(field + '.env', '须为变量名到文字的对象，不能使用 SOP_ 开头的变量')
    guards = value.get('guards', {})
    if not isinstance(guards, dict):
        problem('guards', '须为场景键到守护的对象')
        guards = {}
    for name, guard in guards.items():
        field = f'guards.{name}'
        try:
            split_key(name)
        except ValueError as error:
            problem(field, str(error))
            continue
        if not isinstance(guard, dict):
            problem(field, '须为对象')
            continue
        for extra in guard.keys() - {'fingerprint', 'acceptance', 'tests', 'coverage', 'detection', 'ledger'}:
            problem(f'{field}.{extra}', '格式外字段')
        if not isinstance(guard.get('fingerprint'), str) or not SHA256.fullmatch(guard['fingerprint']):
            problem(field + '.fingerprint', '须为场景指纹')
        acceptance = guard.get('acceptance')
        if not isinstance(acceptance, dict) or type(acceptance.get('result')) is not int:
            problem(field + '.acceptance', '须含独立通过的结果编号 result')
        tests = guard.get('tests')
        ids = set()
        if not isinstance(tests, list) or not tests:
            problem(field + '.tests', '须为非空测试数组')
            tests = []
        for index, test in enumerate(tests):
            where = f'{field}.tests[{index}]'
            if not isinstance(test, dict):
                problem(where, '须为对象')
                continue
            for extra in test.keys() - {'id', 'repo', 'path', 'sha256', 'name', 'layer', 'command'}:
                problem(f'{where}.{extra}', '格式外字段')
            if not isinstance(test.get('id'), str) or not COMMAND_ID.fullmatch(test['id']) or test['id'] in ids:
                problem(where + '.id', '须为不重复的小写编号')
            ids.add(test.get('id'))
            if not _relative(test.get('path')):
                problem(where + '.path', '须为仓内相对路径')
            if not isinstance(test.get('sha256'), str) or not SHA256.fullmatch(test['sha256']):
                problem(where + '.sha256', '须为测试文件的 sha256')
            if not isinstance(test.get('name'), str) or not test['name'].strip():
                problem(where + '.name', '须为测试名')
            if test.get('layer') not in LAYERS:
                problem(where + '.layer', '应为 ' + '、'.join(LAYERS))
            if test.get('command') not in commands:
                problem(where + '.command', '须为已声明的命令编号')
            if not isinstance(test.get('repo'), str):
                problem(where + '.repo', '须为仓库别名')
        coverage = guard.get('coverage')
        if not isinstance(coverage, dict) or coverage.keys() != {'expected', 'forbidden_effects'}:
            problem(field + '.coverage', '须含 expected 与 forbidden_effects')
        else:
            for part in ('expected', 'forbidden_effects'):
                items = coverage[part]
                if not isinstance(items, list) or not all(
                        isinstance(item, dict) and type(item.get('index')) is int and isinstance(item.get('text'), str)
                        and isinstance(item.get('tests'), list) and item['tests'] and set(item['tests']) <= ids
                        for item in items):
                    problem(f'{field}.coverage.{part}', '每项须含 index、text 与非空的已声明测试编号')
        detection = guard.get('detection')
        if (not isinstance(detection, dict) or detection.get('method') not in ('mutation', 'known-defect')
                or not isinstance(detection.get('assertion'), str) or not isinstance(detection.get('evidence'), str)
                or not SHA256.fullmatch(detection['evidence'])):
            problem(field + '.detection', '须含 method（mutation 或 known-defect）、assertion 与证据 sha256')
        if not isinstance(guard.get('ledger', []), list):
            problem(field + '.ledger', '须为数组')
    return problems


def load_index(path):
    """读取守护索引；不存在时返回空索引。结构不合格抛出 ValueError。"""
    if path is None or not path.exists():
        return dict(version=1, commands={}, guards={})
    if path.is_symlink() or not path.is_file():
        raise ValueError(f'{GUARD_INDEX} 须为普通文件')
    try:
        value = strict_yaml(path.read_text(encoding='utf-8'))
    except (OSError, ValueError, yaml.YAMLError) as error:
        raise ValueError(f'{GUARD_INDEX} 无法读取：{error}') from error
    problems = validate_index(value)
    if problems:
        raise ValueError(f'{GUARD_INDEX} 不合格：' + '；'.join(f"{p['field']} {p['problem']}" for p in problems[:5]))
    value.setdefault('commands', {})
    value.setdefault('guards', {})
    return value


def tests_digest(guard):
    return gitops.digest(sorted((t['repo'], t['path'], t['sha256'], t['name']) for t in guard['tests']))


def test_file(checkout, test):
    root = checkout.paths.get(test['repo'])
    if root is None:
        raise ValueError(f"测试仓库 {test['repo']} 不在检出中")
    path = root / test['path']
    if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root.resolve()):
        raise ValueError(f"测试文件不存在或越界：{test['repo']}:{test['path']}")
    return path


def invalid(reason):
    return dict(valid=False, reason=reason)


def missing_repos(project, guard, commands):
    """守护引用、但本机项目没有登记的仓库别名。"""
    aliases = []
    for test in guard['tests']:
        command = commands.get(test['command']) or {}
        for alias in (command.get('repo'), test.get('repo')):
            if alias and alias not in project.repos and alias not in aliases:
                aliases.append(alias)
    return aliases


def check(context, db, scene_key, scene, guard, commands, registrations=()):
    """单个守护的判定（由本检出的 regression.yaml 与测试文件现算，三条查业务记录库）：
    {valid, reason}，另有 unsynced（别人已沉淀、本机未同步）或 unrunnable（引用的仓库本机没有登记）。"""
    product = context.store.product
    if guard is None:
        if registrations:
            latest = registrations[-1]
            return dict(valid=False, unsynced=dict(by=latest['distilled_by'], at=latest['registered_at']),
                        reason=f"已由 {latest['distilled_by']} 沉淀，本机未同步")
        return invalid('没有守护')
    if guard['fingerprint'] != scene['fingerprint']:
        return invalid('场景内容已变化，守护过期')
    if registrations and tests_digest(guard) not in {r['tests_digest'] for r in registrations}:
        latest = registrations[-1]
        return dict(valid=False, unsynced=dict(by=latest['distilled_by'], at=latest['registered_at']),
                    reason=f"已由 {latest['distilled_by']} 沉淀，本机未同步（本检出的守护测试摘要不同）")
    absent = missing_repos(context.project, guard, commands)
    if absent:
        return dict(valid=False, unrunnable=absent, reason='本机无法运行：没有登记的仓库别名 ' + '、'.join(absent))
    for test in guard['tests']:
        command = commands.get(test['command'])
        if command is None or command['repo'] not in context.checkout.paths:
            return invalid(f"命令 {test['command']} 不可用")
        try:
            path = test_file(context.checkout, test)
        except ValueError as error:
            return invalid(str(error))
        if hashlib.sha256(path.read_bytes()).hexdigest() != test['sha256']:
            return invalid(f"测试文件已改动：{test['repo']}:{test['path']}（守护的测试文件摘要不一致）")
    for part in ('expected', 'forbidden_effects'):
        items = guard['coverage'][part]
        if [(item['index'], item['text']) for item in items] != list(enumerate(scene[part])):
            return invalid(f'coverage 没有逐条覆盖当前的 {part}')
    if not db.one('SELECT 1 FROM evidence WHERE product=? AND sha256=?', (product, guard['detection']['evidence'])):
        return invalid('检出能力证据在证据索引里没有记录')
    row = db.one('SELECT * FROM results WHERE id=? AND product=?', (guard['acceptance']['result'], product))
    file, scenario = split_key(scene_key)
    if (row is None or row['kind'] != 'agent' or row['status'] != 'pass' or row['independence'] != 'independent'
            or row['fingerprint'] != scene['fingerprint'] or row['file'] != file or row['scenario'] != scenario):
        return invalid('acceptance 不是同指纹的独立真实入口通过')
    if db.one('SELECT 1 FROM guard_misses WHERE product=? AND file=? AND scenario=? AND fingerprint=? AND tests_digest=?',
              (product, file, scenario, scene['fingerprint'], tests_digest(guard))):
        return invalid('守护已因漏检失效，须重新沉淀')
    return dict(valid=True, reason='')


def registrations_of(db, product):
    """沉淀登记：(文件, 场景, 指纹) → [登记]（按登记先后）。"""
    result = {}
    for row in db.execute('SELECT * FROM distillations WHERE product=? ORDER BY id', (product,)):
        result.setdefault((row['file'], row['scenario'], row['fingerprint']), []).append(dict(row))
    return result


def validity(context, scenes, db):
    """场景键 → 守护判定。索引不合格时全部无效并给出原因。db 是业务记录库。"""
    try:
        index = load_index(index_path(context))
    except ValueError as error:
        return {key: invalid(str(error)) for key in scenes}
    registered = registrations_of(db, context.store.product)
    result = {}
    for key, scene in scenes.items():
        result[key] = check(context, db, key, scene, index['guards'].get(key), index['commands'],
                            registered.get((scene['file'], scene['id'], scene['fingerprint']), []))
    return result


def test_exclusions(context):
    return test_exclusions_for(context.project, context.checkout)


def test_exclusions_for(project, checkout):
    """守护测试文件不计入产品快照：沉淀测试不会让已有通过变成待复测。"""
    try:
        index = load_index(index_path_for(project, checkout))
    except ValueError:
        return {}
    result = {}
    for guard in index['guards'].values():
        for test in guard['tests']:
            result.setdefault(test['repo'], []).append(test['path'])
    return result


def after_result(db, store, checkout_name, scene, verdict, result_id):
    """真实入口或人工复测不通过、而该场景同一指纹上本机本检出最近一次守护运行是通过（其后本检出没有更晚的通过）时，
    记一条漏检：该守护失效，直到重新沉淀。漏检只在同一台机器的同一检出内判定（守护运行与失败出自同一份工作副本）；
    最近一次守护运行未通过时，守护已经发现了问题，不算漏检。登记后按 文件 + 场景 + 指纹 + 测试摘要 共享。"""
    if verdict != 'fail':
        return
    product, machine = store.product, store.machine['id']
    run = db.one('SELECT * FROM guard_runs WHERE product=? AND machine=? AND checkout=? AND file=? AND scenario=? AND fingerprint=? '
                 'ORDER BY seq DESC LIMIT 1', (product, machine, checkout_name, scene['file'], scene['id'], scene['fingerprint']))
    if run is None or run['status'] != 'pass':
        return
    passed = db.one("SELECT seq FROM results WHERE product=? AND machine=? AND checkout=? AND file=? AND scenario=? AND fingerprint=? "
                    "AND status='pass' AND id<>? ORDER BY seq DESC LIMIT 1",
                    (product, machine, checkout_name, scene['file'], scene['id'], scene['fingerprint'], result_id))
    if passed and passed['seq'] > run['seq']:
        return
    db.execute('INSERT INTO guard_misses(product,file,scenario,fingerprint,tests_digest,result,machine,checkout,recorded_at) '
               'VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING',
               (product, scene['file'], scene['id'], scene['fingerprint'], run['tests_digest'], result_id, machine, checkout_name, now()))


# ---- 运行守护命令（retest 与 distill submit 共用；在守护进程中执行，不启动 AI） ----

import json as _json
import os
import signal
import subprocess
import xml.etree.ElementTree as ElementTree


def _norm(name):
    return name.replace('::', '.').replace('/', '.').replace('.py.', '.').strip('.')


def junit_cases(path):
    """junit XML → [(全名, 状态)]，状态为 passed/failed/skipped。"""
    try:
        root = ElementTree.parse(path).getroot()
    except (OSError, ElementTree.ParseError):
        return []
    cases = []
    for case in root.iter('testcase'):
        name, classname = case.get('name', ''), case.get('classname', '')
        status = 'passed'
        for child in case:
            if child.tag in ('failure', 'error'):
                status = 'failed'
            elif child.tag == 'skipped' and status == 'passed':
                status = 'skipped'
        cases.append((f'{classname}.{name}' if classname else name, name, status))
    return cases


def find_case(cases, declared):
    """在执行清单中找声明的测试名：完整名相同、以它结尾（pytest 的 文件::函数 或 模块.函数），或测试标题相同。"""
    wanted = _norm(declared)
    for full, name, status in cases:
        full_n = _norm(full)
        if full_n == wanted or full_n.endswith('.' + wanted) or name == declared:
            return status
    return None


def playwright_cli():
    from .browsers import runtime_info
    info = runtime_info()
    return str(Path(info['node_modules']) / '@playwright' / 'test' / 'cli.js') if info else 'playwright'


def execute(project, checkout, name, command, out, extra_env):
    """运行一条守护命令；返回 dict(code, log, junit, timed_out)。只用 argv，不经 shell，超时结束进程组。"""
    repo = checkout.paths.get(command['repo'])
    if repo is None:
        raise ValueError(f"命令 {name} 的仓库 {command['repo']} 不在检出中")
    cwd = (repo / command.get('cwd', '.')).resolve()
    if not (cwd == repo.resolve() or cwd.is_relative_to(repo.resolve())) or not cwd.is_dir():
        raise ValueError(f'命令 {name} 的工作目录不在仓库内')
    junit = out / f'{name}.junit.xml'
    log = out / f'{name}.log'
    argv = [part.replace('{junit}', str(junit)).replace('{playwright}', playwright_cli()) for part in command['argv']]
    env = {k: v for k, v in os.environ.items() if not k.startswith('SOP_')}
    env.update(command.get('env', {}))
    env.update(extra_env)
    env['PLAYWRIGHT_JUNIT_OUTPUT_NAME'] = str(junit)
    # 测试运行不应改变产品快照；不让 Python 往仓库里写字节码缓存。
    env.setdefault('PYTHONDONTWRITEBYTECODE', '1')
    timed_out = False
    with log.open('wb') as stream:
        stream.write(('$ ' + ' '.join(argv) + f'\n(cwd {cwd})\n').encode())
        stream.flush()
        process = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=stream, stderr=subprocess.STDOUT,
                                   start_new_session=True)
        try:
            code = process.wait(timeout=command.get('timeout', 900))
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            code = process.wait()
    return dict(code=code, log=log, junit=junit, timed_out=timed_out)


def run_guards(app, checkout, index, keys, *, holder):
    """为这些场景运行守护引用的命令（每条命令一次）；返回 场景键 → (通过, 原因, 输出证据)。"""
    from . import gitops as _gitops
    project, store = app.project, app.store
    commands = {}
    for key in keys:
        for test in index['guards'][key]['tests']:
            commands.setdefault(test['command'], index['commands'][test['command']])
    from . import workdirs
    out = workdirs.create(project.data / 'retests', f"{datetime_stamp()}-{checkout.name}")
    before = _gitops.snapshot(checkout.paths, project.exclusions(test_exclusions_for(project, checkout)))['product_digest']
    notes = circumstances(app, checkout)
    results = {}
    try:
        for name, command in commands.items():
            extra = dict(app.envs.status(checkout).get('address_env', {}))
            if command.get('browser'):
                environment = app.envs.up(checkout)
                extra.update(environment.get('address_env', {}))
                leased = app.browsers.lease(checkout, [command['browser']], holder)[command['browser']]
                if 'endpoint' not in leased:
                    results[name] = dict(code=None, problem=leased['problem'])
                    continue
                from .browsers import runtime_info
                info = runtime_info()
                extra.update(PW_CDP_ENDPOINT=leased['endpoint'], PLAYWRIGHT_BROWSERS_PATH=info['browsers'],
                             PW_NODE_ROOT=str(Path(info['node_modules']).parent))
            results[name] = execute(project, checkout, name, command, out, extra)
    finally:
        app.browsers.release(holder)
    after = _gitops.snapshot(checkout.paths, project.exclusions(test_exclusions_for(project, checkout)))['product_digest']
    outputs = {}
    # 原始输出存为证据：文件进本机证据目录，证据索引进业务记录库。
    with store.records_tx() as db:
        for name, result in results.items():
            items = []
            for path in (result.get('log'), result.get('junit')):
                if path and Path(path).is_file():
                    items.append(dict(name=Path(path).name, sha256=store.save_evidence(db, Path(path).read_bytes(), Path(path).name)))
            outputs[name] = items
    verdicts = {}
    for key in keys:
        problems = []
        for test in index['guards'][key]['tests']:
            result = results[test['command']]
            if result.get('problem'):
                problems.append(f"命令 {test['command']} 的浏览器不可用：{result['problem']}")
                continue
            if result['timed_out']:
                problems.append(f"命令 {test['command']} 超时")
                continue
            if result['code'] != 0:
                problems.append(f"命令 {test['command']} 退出码 {result['code']}")
            status = find_case(junit_cases(result['junit']), test['name'])
            if status is None:
                problems.append(f"测试 {test['name']} 没有出现在执行清单中")
            elif status != 'passed':
                problems.append(f"测试 {test['name']} {'被跳过' if status == 'skipped' else '未通过'}")
        if before != after:
            problems.append('运行期间产品内容发生变化')
        evidence = [item for test in index['guards'][key]['tests'] for item in outputs.get(test['command'], [])]
        # 不判断失败原因：未通过时如实附上当时的情况（没在运行的服务、工作区是否有未提交改动），交真实入口复测时判断。
        reason = '；'.join(list(dict.fromkeys(problems)) + notes) if problems else ''
        verdicts[key] = (not problems, reason, list({e['sha256']: e for e in evidence}.values()))
    return verdicts, before


def circumstances(app, checkout):
    """守护运行开始时的情况：声明了服务时列出没在运行的服务，以及各仓库工作区是否有未提交改动。"""
    from . import gitops as _gitops
    notes = []
    if app.project.adapter.get('services'):
        environment = app.envs.status(checkout)
        down = environment.get('not_running', [])
        if environment.get('state') != 'ready':
            notes.append('当时本检出的环境没有运行，声明的服务都没在运行')
        elif down:
            notes.append('当时本机没在运行的服务：' + '、'.join(f"{s['name']}（{s['about']}，{s['label']}）" for s in down))
        else:
            notes.append('当时声明的服务都在运行')
    dirty = _gitops.uncommitted(checkout.paths)
    notes.append('工作区有未提交改动（' + '、'.join(f'{alias} {count} 处' for alias, count in dirty.items()) + '）'
                 if dirty else '工作区没有未提交改动')
    return notes


def datetime_stamp():
    from datetime import datetime
    return datetime.now().strftime('%Y%m%d-%H%M%S-%f')


def record_runs(store, checkout, scenes, index, verdicts, digest, operator, *, branch):
    """守护运行写进业务记录库：带统一序号、产出机器、检出与分支。"""
    with store.records_tx() as db:
        for key, (ok, reason, evidence) in verdicts.items():
            scene = scenes[key]
            db.execute('INSERT INTO guard_runs(product,seq,machine,checkout,branch,file,scenario,fingerprint,product_digest,'
                       'tests_digest,status,reason,output,operator,ran_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                       (store.product, db.next_seq(), store.machine['id'], checkout.name, branch, scene['file'], scene['id'],
                        scene['fingerprint'], digest, tests_digest(index['guards'][key]), 'pass' if ok else 'fail', reason or None,
                        _json.dumps(evidence, ensure_ascii=False), operator, now()))


def retest(app, checkout, *, scenarios_=None, dry_run=False, operator):
    """运行有效守护发现代码回归：缺省取全部非草稿场景，只运行守护有效的。
    每个场景记一条守护运行；通过不改变状态，未通过的场景在本检出转为待复测（见 status.derive）。
    别人已沉淀而本机未同步的列在 not_guarded；引用的仓库本机没有登记的列为“本机无法运行”（cannot_run），都不运行、不写记录。"""
    from .checkouts import branch_of
    from .cli import Context, scene_board
    context = Context(app.project, app.store, checkout)
    found, snapshot, derived = scene_board(context)
    scenes = found['scenarios']
    index = load_index(index_path(context))
    if scenarios_:
        from .scenarios import resolve
        keys = resolve(scenarios_, scenes)
    else:
        keys = sorted(k for k, v in derived['items'].items() if v['state'] != 'draft')
    runnable = [k for k in keys if derived['items'][k]['guard']['valid']]
    cannot = {k: derived['items'][k]['guard']['reason'] for k in keys if derived['items'][k]['guard'].get('unrunnable')}
    skipped = {k: derived['items'][k]['guard']['reason'] for k in keys if k not in runnable and k not in cannot}
    plan = {}
    for key in runnable:
        for test in index['guards'][key]['tests']:
            plan.setdefault(test['command'], dict(argv=index['commands'][test['command']]['argv'],
                                                  repo=index['commands'][test['command']]['repo'], scenarios=[]))
            if key not in plan[test['command']]['scenarios']:
                plan[test['command']]['scenarios'].append(key)
    result = dict(ok=True, checkout=checkout.name, dry_run=dry_run, scenarios=runnable, commands=plan,
                  not_guarded=skipped, cannot_run=cannot)
    if dry_run or not runnable:
        return result
    verdicts, digest = run_guards(app, checkout, index, runnable, holder=f'retest:{checkout.name}')
    record_runs(app.store, checkout, scenes, index, verdicts, digest, operator, branch=branch_of(app.project, checkout))
    result['results'] = {k: dict(status='pass' if ok else 'fail', label='守护运行通过' if ok else '守护运行未通过，转为待复测',
                                 reason=reason) for k, (ok, reason, _) in verdicts.items()}
    passed = sum(1 for ok, _, _ in verdicts.values() if ok)
    result['summary'] = (f'守护运行通过 {passed}，未通过 {len(verdicts) - passed}（转为待复测），没有有效守护 {len(skipped)}'
                         + (f'，本机无法运行 {len(cannot)}' if cannot else ''))
    return result


# ---- 沉淀 ----

def distill_eligible(context):
    from .cli import scene_board
    found, snapshot, derived = scene_board(context)
    return found, derived


def require_passing(derived, keys):
    """沉淀只接收当前状态为通过的场景，不改变已有守护的有效性。"""
    from .errors import reject
    failed = [f"{key} 当前的状态是{derived['items'][key]['label']}"
              for key in keys if key in derived['items'] and derived['items'][key]['state'] != 'pass']
    if failed:
        reject(409, 'distill-not-passing', 'scope', '不能沉淀：' + '；'.join(failed))


def distill_prepare(context, scope, *, session):
    from importlib.resources import files
    from string import Template
    import yaml
    from . import config, runs
    from .scenarios import resolve
    from .store import dumps
    executor = runs.session_id(session)
    operator = config.require_operator()
    found, derived = distill_eligible(context)
    scenes = found['scenarios']
    keys = resolve(scope, scenes) if scope else sorted(k for k, v in derived['items'].items() if v['distill'])
    require_passing(derived, keys)
    not_ready = [k for k in keys if not derived['items'][k]['distill']]
    if not_ready:
        from .errors import reject
        reject(409, 'not-distillable', 'scope', '这些场景没有独立真实入口通过或已有有效守护，不能沉淀：' + '、'.join(not_ready))
    if not keys:
        from .errors import reject
        reject(409, 'nothing-to-distill', 'scope', '没有待沉淀的场景')
    from .status import independent_pass
    from . import works
    book = derived['book']
    with context.store.records() as db:
        acceptance = {}
        for key in keys:
            scene = scenes[key]
            row = independent_pass(book.select((scene['file'], scene['id'], scene['fingerprint'])).records)
            acceptance[key] = dict(result=row['id'], executor=row['executor'], executed_at=row['executed_at'],
                                   machine=book.machine_name(row['machine']), branch=row['branch'],
                                   run_dir=works.local_dir(context.store, db, row['work']))
    with context.store.local_tx() as db:
        running = db.execute("SELECT id FROM local_work WHERE checkout=? AND kind='distill' AND status='prepared'",
                             (context.checkout.id,)).fetchone()
        if running:
            from .errors import reject
            reject(409, 'distill-prepared', 'work', f"检出 {context.checkout.name} 已有进行中的沉淀 #{running['id']}")
        cursor = db.execute("INSERT INTO local_work(kind,checkout,status,session,operator,dir,scope,created_at) "
                            "VALUES ('distill',?,'prepared',?,?,'',?,?)",
                            (context.checkout.id, executor, operator, dumps([dict(scenario=k) for k in keys]), now()))
        work_id = cursor.lastrowid
    from . import workdirs
    directory = workdirs.create(context.project.data / 'distills', work_id)
    (directory / 'scenarios.yaml').write_text(yaml.safe_dump([dict(scenes[k], key=k) for k in keys], allow_unicode=True,
                                                             sort_keys=False), encoding='utf-8')
    (directory / 'acceptance.json').write_text(dumps(acceptance), encoding='utf-8')
    (directory / 'repos.json').write_text(dumps({a: str(p) for a, p in context.checkout.paths.items()}), encoding='utf-8')
    skill = Path(__file__).parent / 'skills' / 'regression-distill' / 'SKILL.md'
    prompt = Template(files('sop_board').joinpath('prompts', 'distill.md').read_text(encoding='utf-8')).substitute(
        work=work_id, directory=directory, skill=skill, index=index_path(context), checkout=context.checkout.name)
    (directory / 'prompt.md').write_text(prompt, encoding='utf-8')
    with context.store.local_tx() as db:
        db.execute('UPDATE local_work SET dir=? WHERE id=?', (str(directory), work_id))
    return dict(ok=True, work=work_id, directory=str(directory), prompt=str(directory / 'prompt.md'),
                report=str(directory / 'guard.json'), scenarios=keys, acceptance={k: v['result'] for k, v in acceptance.items()})


def browser_fidelity(store, db, checkout, test, acceptance, ledger):
    """浏览器层 spec 的保真检查：基线为 git HEAD 中的同一 spec 及其本地导入；HEAD 还没有时，以产生独立通过的那份 spec 为基线
    （独立通过出自别的机器时，本机没有那份运行目录，不设基线）。"""
    from . import specs, works
    repo = checkout.paths[test['repo']]
    if specs.in_head(repo, test['path']):
        return specs.check_file(repo, test['path'], ledger=ledger)
    directory = works.local_dir(store, db, acceptance['work'])
    candidates = sorted(Path(directory).rglob(Path(test['path']).name)) if directory else []
    if candidates:
        return specs.check(repo / test['path'], repo, baseline=candidates[0], baseline_root=Path(directory), ledger=ledger)
    return specs.check(repo / test['path'], repo, ledger=ledger)


def distill_submit(app, checkout, report_path, *, operator, work_id=None):
    """核对沉淀报告，按 retest 规则实际运行一次，全部合格后合并进 regression.yaml（不提交）。"""
    import yaml
    from .cli import Context, scene_board
    from .errors import reject
    from . import specs
    context = Context(app.project, app.store, checkout)
    work = None
    if work_id:
        with app.store.local() as db:
            work = db.execute("SELECT * FROM local_work WHERE id=? AND kind='distill'", (work_id,)).fetchone()
        if work is None or work['status'] != 'prepared':
            reject(409, 'work-state-invalid', 'work', f'沉淀 #{work_id} 不是进行中的沉淀')
    report_path = Path(report_path)
    try:
        report = strict_yaml(report_path.read_text(encoding='utf-8'))
    except (OSError, ValueError, yaml.YAMLError) as error:
        reject(422, 'report-invalid', 'report', f'沉淀报告无法读取：{error}')
    if not isinstance(report, dict) or not isinstance(report.get('guards'), dict) or not report['guards']:
        reject(422, 'report-invalid', 'report', '沉淀报告须含 guards')
    found, snapshot, derived = scene_board(context)
    scenes = found['scenarios']
    require_passing(derived, report['guards'])
    path = index_path(context)
    index = load_index(path)
    commands = dict(index['commands'])
    for name, command in (report.get('commands') or {}).items():
        if name in commands and commands[name] != command:
            reject(422, 'report-invalid', f'commands.{name}', '与 regression.yaml 中同名命令不同；换一个命令编号')
        commands[name] = command
    problems, entries = [], {}
    product = app.store.product
    with app.store.records() as db:
        for key, guard in report['guards'].items():
            if key not in scenes:
                problems.append(f'{key}：场景不存在')
                continue
            scene = scenes[key]
            if not isinstance(guard, dict):
                problems.append(f'{key}：守护须为对象')
                continue
            result_id = (guard.get('acceptance') or {}).get('result')
            row = db.one('SELECT * FROM results WHERE id=? AND product=?', (result_id, product)) if type(result_id) is int else None
            if (row is None or row['kind'] != 'agent' or row['status'] != 'pass' or row['independence'] != 'independent'
                    or row['reused_from'] or row['fingerprint'] != scene['fingerprint'] or row['file'] != scene['file']
                    or row['scenario'] != scene['id']):
                problems.append(f'{key}：acceptance 不是同指纹的独立真实入口通过')
                continue
            tests = []
            for test in guard.get('tests') or []:
                if not isinstance(test, dict) or test.get('repo') not in checkout.paths or not _relative(test.get('path')):
                    problems.append(f'{key}：测试须写明检出中的仓库与仓内相对路径')
                    continue
                try:
                    file = test_file(checkout, test)
                except ValueError as error:
                    problems.append(f'{key}：{error}')
                    continue
                tests.append(dict(id=test.get('id'), repo=test['repo'], path=test['path'],
                                  sha256=hashlib.sha256(file.read_bytes()).hexdigest(), name=test.get('name'),
                                  layer=test.get('layer'), command=test.get('command')))
            entry = dict(fingerprint=scene['fingerprint'],
                         acceptance=dict(result=row['id'], executor=row['executor'], product_digest=row['product_digest']),
                         tests=tests, coverage=guard.get('coverage'), detection=None, ledger=guard.get('ledger', []))
            misses = db.all('SELECT * FROM guard_misses WHERE product=? AND file=? AND scenario=? AND fingerprint=?',
                            (product, scene['file'], scene['id'], scene['fingerprint']))
            if misses and tests:
                # 漏检之后的独立通过：比较序号（漏检时记下的失败结果的序号）。
                miss_seq = max((db.value('SELECT seq FROM results WHERE id=?', (m['result'],)) or 0) for m in misses)
                if row['seq'] <= miss_seq:
                    problems.append(f'{key}：曾经漏检，acceptance 不是漏检之后的独立通过')
                    continue
                if tests_digest(entry) in {m['tests_digest'] for m in misses}:
                    problems.append(f'{key}：曾经漏检，测试摘要未变化；先补上能发现该问题的断言')
                    continue
            coverage = guard.get('coverage') if isinstance(guard.get('coverage'), dict) else {}
            for part in ('expected', 'forbidden_effects'):
                given = {item.get('index'): item for item in coverage.get(part) or [] if isinstance(item, dict)}
                for position, text in enumerate(scene[part]):
                    item = given.get(position)
                    if item is None or item.get('text') != text or not item.get('tests'):
                        problems.append(f'{key}：未覆盖 {part} 第 {position} 条（{text}）')
            detection = guard.get('detection') or {}
            evidence = detection.get('evidence')
            evidence_file = (report_path.parent / evidence) if isinstance(evidence, str) and not Path(evidence).is_absolute() else Path(str(evidence))
            if detection.get('method') not in ('mutation', 'known-defect') or not detection.get('assertion') or not evidence_file.is_file():
                problems.append(f'{key}：detection 须写明 method、assertion 与检出能力证据文件')
            else:
                # 检出能力证据：文件进本机证据目录，索引进业务记录库（别的机器据此判守护有效，文件留在本机）。
                with app.store.records_tx() as tx:
                    sha = app.store.save_evidence(tx, evidence_file.read_bytes(), evidence_file.name)
                entry['detection'] = dict(method=detection['method'], assertion=detection['assertion'], evidence=sha)
            for test in tests:
                if test['layer'] == 'browser':
                    try:
                        specs.qualified(browser_fidelity(app.store, db, checkout, test, row, entry['ledger']))
                    except ValueError as error:
                        problems.append(f"{key}：{test['path']} {error}")
            entries[key] = entry
    if problems:
        reject(422, 'distill-invalid', 'report', '；'.join(problems))
    merged = dict(version=1, commands=commands, guards=dict(index['guards'], **entries))
    structure = validate_index(merged)
    if structure:
        reject(422, 'distill-invalid', 'report', '；'.join(f"{p['field']} {p['problem']}" for p in structure[:8]))
    verdicts, digest = run_guards(app, checkout, merged, list(entries), holder=f'distill:{checkout.name}')
    failed = {k: v[1] for k, v in verdicts.items() if not v[0]}
    if failed:
        reject(422, 'distill-tests-failed', 'tests', '守护命令实际运行未通过：' + '；'.join(f'{k} {r}' for k, r in failed.items()))
    _, _, current = scene_board(context)
    require_passing(current, entries)
    path.write_text(yaml.safe_dump(merged, allow_unicode=True, sort_keys=False), encoding='utf-8')
    from .checkouts import branch_of
    branch = branch_of(app.project, checkout)
    record_runs(app.store, checkout, scenes, merged, verdicts, digest, operator, branch=branch)
    # 沉淀登记（场景、指纹、测试摘要、沉淀人、机器、时间）写进业务记录库：别的机器据此显示“已由 <沉淀人> 沉淀，本机未同步”。
    from . import works
    from .store import dumps
    with app.store.records_tx() as db:
        record = None
        if work is not None:
            record, _ = works.publish(db, app.store, work, checkout=checkout.name, branch=branch,
                                      result=dict(guards=sorted(entries)))
        for key, entry in entries.items():
            scene = scenes[key]
            db.execute('INSERT INTO distillations(product,work,file,scenario,fingerprint,tests_digest,distilled_by,machine,checkout,'
                       'branch,registered_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                       (app.store.product, record, scene['file'], scene['id'], scene['fingerprint'], tests_digest(entry), operator,
                        app.store.machine['id'], checkout.name, branch, now()))
    if work is not None:
        with app.store.local_tx() as db:
            db.execute("UPDATE local_work SET status='submitted', ended_at=?, result=?, record=? WHERE id=? AND kind='distill'",
                       (now(), dumps(dict(guards=sorted(entries))), record, work_id))
    return dict(ok=True, checkout=checkout.name, index=str(path), guards=sorted(entries),
                runs={k: '实际运行通过' for k in verdicts}, next='测试文件与 regression.yaml 为未提交改动，请审核后提交')


# ---- 守护进程挂载与接口 ----

def attach(app):
    pass


def _register():
    from .server import json_response, route

    @route('POST', '/local/retest', 'token')
    def retest_route(app, request):
        checkout = app.checkout(request.json_field('checkout'))
        return json_response(retest(app, checkout, scenarios_=request.json_field('scenarios'),
                                    dry_run=bool(request.json_field('dry_run')),
                                    operator=request.json_field('operator') or '本机'))

    @route('POST', '/local/distill/submit', 'token')
    def distill_route(app, request):
        checkout = app.checkout(request.json_field('checkout'))
        return json_response(distill_submit(app, checkout, request.json_field('report'),
                                            operator=request.json_field('operator') or '本机', work_id=request.json_field('work')))

    @route('POST', '/api/retest', 'local')
    def retest_button(app, request):
        from . import config
        checkout = app.checkout(request.json_field('checkout'))
        key = request.json_field('scenario')
        return json_response(retest(app, checkout, scenarios_=[key] if key else None,
                                    operator=(config.operator() or '本机') + '（工作台）'))

    @route('POST', '/api/distill', 'local')
    def distill_button(app, request):
        from . import config
        checkout = app.checkout(request.json_field('checkout'))
        key = request.json_field('scenario')
        return json_response(dict(ok=True, launch=app.launcher.start(
            checkout, 'distill', [key], requested_by=(config.operator() or '本机') + '（工作台）')))


_register()
