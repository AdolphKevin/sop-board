"""sop-board 命令行：所有 Agent 工作与本机操作的唯一入口。

除 status 的文字输出与 guide 外，命令都输出 JSON：成功退出 0，业务失败退出 1（ok: false），参数错误退出 2。
"""
import argparse
from dataclasses import dataclass
from importlib import metadata
from importlib.resources import files
import json
from pathlib import Path
import re
import subprocess
import sys

from . import checkouts, config, gitops, scenarios, status
from .errors import APIError
from .store import Store


@dataclass
class Context:
    project: config.Project
    store: Store
    checkout: checkouts.Checkout


GROUPS = {
    '搭建': {'init': '登记项目与仓库', 'guide': '完整搭建指引', 'check': '校验配置、拉起环境并核对浏览器运行时',
             'operator': '查看或设置本机操作者', 'skills': '安装随包技能到 Codex 与 Claude Code',
             'serve': '在终端启动看板守护进程（网页、环境、浏览器、无头会话）', 'status': '查看场景状态与待处理事项'},
    '检出': {'checkout': '列出、查看、建立或移除 main 与 worktree 检出'},
    '执行': {'env': '按检出启停产品环境', 'browser': '安装与管理 Chrome for Testing',
             'run': '准备、提交、启动执行会话', 'retest': '产品变化后运行守护测试', 'mark': '登记人工复测结论',
             'launch': '查看或停止本机无头会话'},
    '修复与沉淀': {'fix': '修复、独立复验与关闭', 'distill': '把独立通过的场景沉淀为守护测试'},
    '编写': {'inbox': '处理测试同学上传的用例与 SOP', 'merge': '合并用例到场景文件', 'ask': '向测试同学提问',
             'answers': '读取答复', 'sop': '编写业务 SOP', 'draft': '草稿批注与确认'},
    '其他': {'atlas': '交互地图', 'specs': 'Web spec 保真检查', 'fixtures': '前置数据配方', 'identity': '身份使用计数',
             'attention': '待人处理', 'report': '导出报告与证据', 'playwright': 'Web 驱动指引与结果转换'},
}


def version():
    try:
        distribution = metadata.distribution('sop-board')
        direct = json.loads(distribution.read_text('direct_url.json') or '{}')
        source = direct.get('url') or str(distribution.locate_file(''))
        return f'sop-board {distribution.version}\n安装来源：{source}\n代码位置：{Path(__file__).resolve().parent}'
    except metadata.PackageNotFoundError:
        root = Path(__file__).resolve().parents[2]
        found = re.search(r'^version = "([^"]+)"', (root / 'pyproject.toml').read_text(), re.M)
        return f'sop-board {found[1] if found else "?"}\n安装来源：源码 {root}'


def emit(value):
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def fail(code, problems, status=1):
    emit(dict(ok=False, error_code=code, problems=problems))
    return status


def common(parser):
    parser.add_argument('--project', '-p', help='项目名称（缺省按当前目录所在仓库查找）')
    parser.add_argument('--data', help='项目数据目录所在的根目录（缺省 ~/.sop-board）')
    parser.add_argument('--checkout', '-c', help='检出名称（缺省按当前目录识别，main 为登记路径）')
    if not any(action.dest == 'json' for action in parser._actions):
        parser.add_argument('--json', action='store_true', help='输出 JSON（除 status 外的命令总是输出 JSON）')
    return parser


def project_name(args):
    return getattr(args, 'name_or_project', None) or getattr(args, 'project', None)


def load(args, *, require=True):
    data = config.resolve_data(project_name(args), getattr(args, 'data', None))
    project = config.Project(data)
    if require:
        project.require()
    store = Store(project.data)
    checkout = checkouts.current(store, project, name=getattr(args, 'checkout', None))
    return Context(project, store, checkout)


def scene_board(context):
    """当前检出的场景、快照与状态。"""
    from . import guards
    project, checkout = context.project, context.checkout
    found = scenarios.scan(checkout.scenarios_dir(project), set(project.entries))
    validity = guards.validity(context, found['scenarios'])
    snapshot = gitops.snapshot(checkout.paths, project.exclusions(guards.test_exclusions(context)))
    with context.store.connection() as db:
        derived = status.board(db, checkout.id, found['scenarios'], snapshot['product_digest'], guard_validity=validity)
    return found, snapshot, derived


# ---- 命令 ----

def cmd_init(args):
    data, identities = config.initialize(args.name, args.repos, args.data)
    Store(data)
    emit(dict(ok=True, project=args.name, data=str(data), repositories=identities,
              next=f'在 {data}/config.yaml 写好 scenarios、skill、entries 与 adapter，启动 sop-board serve 后运行 sop-board check'))
    return 0


def cmd_guide(args):
    print(files('sop_board').joinpath('guide.md').read_text(encoding='utf-8'))
    return 0


def cmd_operator(args):
    if args.action == 'set':
        if not args.name:
            return fail('operator-name-required', [dict(field='name', problem='请写出姓名')], 2)
        emit(dict(ok=True, operator=config.set_operator(' '.join(args.name))))
        return 0
    name = config.operator()
    emit(dict(ok=True, operator=name, source='sop-board operator' if config.operator_path().is_file() else 'git config user.name',
              hint=None if name else '运行 sop-board operator set <姓名>'))
    return 0


def cmd_checkout(args):
    if args.action == 'create':
        if not args.name:
            return fail('checkout-name-required', [dict(field='name', problem='请写出检出名称')], 2)
        project = config.Project(config.resolve_data(args.project, args.data)).require()
        store = Store(project.data)
        log = project.data / 'checkouts' / (args.name + '.log')
        created = checkouts.build(project, store, args.name, aliases=args.repo or None, base=args.base_ref, log=log)
        emit(dict(ok=True, checkout=created.public(local=True), log=str(log)))
        return 0
    context = load(args, require=False)
    with context.store.connection() as db:
        rows = checkouts.active(db)
    if args.action == 'list':
        emit(dict(ok=True, current=context.checkout.name, checkouts=[
            dict(c.public(local=True), offset=c.offset(context.project)) for c in rows]))
        return 0
    name = args.name or context.checkout.name
    target = next((c for c in rows if c.name == name), None)
    if target is None:
        return fail('checkout-not-found', [dict(field='name', problem=f'检出不存在：{name}')])
    if args.action == 'remove':
        from . import envs
        envs.require_stopped(context, target)
        log = context.project.data / 'checkouts' / (target.name + '-cleanup.log')
        checkouts.destroy(context.project, context.store, target, log=log)
        emit(dict(ok=True, removed=target.name, log=str(log)))
        return 0
    directory = target.scenarios_dir(context.project)
    emit(dict(ok=True, checkout=dict(target.public(local=True), offset=target.offset(context.project),
                                     scenarios=str(directory) if directory else None,
                                     branches={a: gitops.branch(p) for a, p in target.paths.items()})))
    return 0


def cmd_status(args):
    context = load(args, require=False)
    project = context.project
    result = dict(ok=True, project=project.name, operator=config.operator(), checkout=context.checkout.public(local=True),
                  configured=project.configured, problems=project.problems)
    if project.configured:
        found, snapshot, derived = scene_board(context)
        result.update(snapshot=dict(product_digest=snapshot['product_digest'], dirty=snapshot['dirty'],
                                    changed=snapshot['changed'][:50]),
                      counts=derived['counts'], distill=derived['distill'], review=derived['review'], total=derived['total'],
                      queues={state: sorted(k for k, v in derived['items'].items() if v['state'] == state)
                              for state in ('retest', 'fail', 'blocked', 'not-run')},
                      distill_queue=sorted(k for k, v in derived['items'].items() if v['distill']),
                      review_queue=sorted(k for k, v in derived['items'].items() if v['review']),
                      invalid_files={name: info['problems'] for name, info in found['files'].items() if info['problems']})
        from . import daemon
        result['daemon'] = dict(daemon.describe(project), command=project.serve_command())
        from . import ledger
        with context.store.connection() as db:
            result['attention'] = ledger.open_attention(db, context.checkout.id, local=True)
    if args.json:
        emit(result)
        return 0
    print(f"项目 {project.name} · 检出 {context.checkout.name} · 操作者 {result['operator'] or '未设置'}")
    if not project.configured:
        print('守护进程启动命令：' + project.serve_command())
        print('配置不合格：')
        for item in project.problems:
            print(f"  {item['field']}：{item['problem']}")
        return 0
    counts = result['counts']
    parts = [f"{status.LABELS[s]} {counts[s]}" for s in status.ORDER if counts[s]] + [
        f"待沉淀 {result['distill']}", f"待人工复核 {result['review']}"]
    print(f"场景 {result['total']} 条：" + '；'.join(parts))
    for name, problems in result['invalid_files'].items():
        print(f'场景文件 {name} 不合格：' + '；'.join(p['problem'] for p in problems[:3]))
    daemon_state = result['daemon']
    print('看板守护进程：' + (f"运行中 {daemon_state['url']}" if daemon_state['running'] else f'未运行（运行 {project.serve_command()}）'))
    for item in result['attention']:
        print(f"待人处理 #{item['id']} {item['kind']}：{item['text']}")
    return 0


def cmd_serve(args):
    from . import server
    project = config.Project(config.resolve_data(project_name(args), args.data))
    return server.serve(project, port=args.port, lan=args.lan)


def daemon_call(context, path, body=None, timeout=900):
    from . import daemon
    return daemon.call(context.project, 'POST', path, dict(body or {}, checkout=context.checkout.name), timeout=timeout)


def cmd_env(args):
    context = load(args)
    if args.action == 'status' and args.all:
        from . import daemon
        emit(daemon.call(context.project, 'POST', '/local/env/status', {}))
        return 0
    result = daemon_call(context, '/local/env/' + args.action)
    emit(result)
    return 0 if result.get('ok') else 1


def cmd_check(args):
    """校验配置；守护进程在运行时拉起当前检出的环境并探测就绪；核对浏览器运行时。"""
    from . import daemon
    context = load(args, require=False)
    project = context.project
    result = dict(ok=True, project=project.name, checkout=context.checkout.name, steps=[])

    def step(name, ok, detail):
        result['steps'].append(dict(step=name, ok=ok, detail=detail))
        result['ok'] = result['ok'] and ok
    step('配置', project.configured, project.problems or '合格')
    if project.configured:
        state = daemon.describe(project)
        if not state['running']:
            step('环境', False, f'看板守护进程没有运行：先运行 sop-board serve --project {project.name}')
        else:
            try:
                environment = daemon_call(context, '/local/env/up')['environment']
                missing = [entry.get('address_env') for entry in project.entries.values()
                           if entry.get('address_env') and entry['address_env'] not in environment.get('address_env', {})]
                step('环境', not missing, environment if not missing else f'就绪，但缺少入口地址变量：{"、".join(missing)}')
            except (APIError, ValueError) as error:
                step('环境', False, getattr(error, 'problems', None) or str(error))
        for extra in CHECKS:
            extra(context, step)
    emit(result)
    return 0 if result['ok'] else 1


def check_browser(context, step):
    from . import browsers
    info = browsers.runtime_info()
    if info is None:
        step('浏览器运行时', False, f'没有安装：运行 sop-board browser install（安装到 {browsers.runtime_dir()}）')
    else:
        step('浏览器运行时', True, dict(playwright=info['playwright'], browser=f"{info['title']} {info['browser_version']}",
                                    revision=info['revision'], executable=info['executable']))


CHECKS = [check_browser]


def cmd_run(args):
    from . import runs
    context = load(args)
    if args.action == 'prepare':
        scope = args.scope or ['pending']
        if args.fix is not None and args.scope:
            return fail('scope-invalid', [dict(field='scope', problem='--fix 复验的范围由修复决定，不能再写 --scope')], 2)
        emit(runs.prepare(context, scope, session=args.session, fix_id=args.fix))
    elif args.action == 'submit':
        emit(runs.submit(context, args.run, implementer=args.implementer))
    elif args.action == 'abandon':
        emit(runs.abandon(context, args.run))
    elif args.action == 'launch':
        from . import daemon
        scope = args.scope or ['pending']
        if args.fix is not None:
            from . import fixes
            fix = fixes.load(context, args.fix)
            scope = [scenarios.key(p['file'], p['scenario']) for p in json.loads(fix['problems'])]
        elif scope == ['pending']:
            from .runs import scope_for
            found, snapshot, derived = scene_board(context)
            scope = [item['scenario'] for item in scope_for(context, found['scenarios'], derived, ['pending'])]
            if not scope:
                return fail('nothing-to-run', [dict(field='scope', problem='没有需要执行的场景')])
        emit(daemon.call(context.project, 'POST', '/local/launch', dict(
            checkout=context.checkout.name, kind='run', scope=scope, implementer=args.implementer, fix=args.fix,
            requested_by=config.operator() or '本机')))
    elif args.action == 'wait':
        return wait(context, launch=args.launch, run=args.run, timeout=args.timeout)
    return 0


def wait(context, *, launch=None, run=None, timeout=1800):
    """等待启动结束或运行提交；0 表示已结束（看输出中的状态），3 表示超时。"""
    import time
    from . import launcher
    deadline = time.monotonic() + timeout if timeout else None
    while True:
        with context.store.connection() as db:
            if launch is None and run is None:
                row = db.execute('SELECT id FROM launches WHERE checkout=? ORDER BY id DESC LIMIT 1', (context.checkout.id,)).fetchone()
                if row is None:
                    return fail('launch-not-found', [dict(field='launch', problem='这个检出还没有启动记录')])
                launch = row['id']
            if launch is not None:
                row = db.execute('SELECT * FROM launches WHERE id=?', (launch,)).fetchone()
                if row is None:
                    return fail('launch-not-found', [dict(field='launch', problem=f'启动不存在：#{launch}')], 2)
                runs_ = [dict(r) for r in db.execute('SELECT id, kind, status, session, result FROM work WHERE launch=?', (launch,))]
                done = row['status'] != 'running'
                value = dict(launch=launcher.public(row, runs_, local=True),
                             results=[dict(run=r['id'], status=r['status'], result=json.loads(r['result']) if r['result'] else None) for r in runs_])
            else:
                row = db.execute('SELECT * FROM work WHERE id=?', (run,)).fetchone()
                if row is None:
                    return fail('run-not-found', [dict(field='run', problem=f'运行不存在：#{run}')], 2)
                done = row['status'] != 'prepared'
                value = dict(run=row['id'], status=row['status'], result=json.loads(row['result']) if row['result'] else None)
        if done:
            emit(dict(ok=True, **value))
            return 0
        if deadline and time.monotonic() > deadline:
            emit(dict(ok=True, timeout=True, **value))
            return 3
        time.sleep(2)


def cmd_launch(args):
    from . import daemon, launcher
    context = load(args)
    if args.action == 'stop':
        emit(daemon.call(context.project, 'POST', '/local/launch/stop', dict(launch=args.launch)))
        return 0
    with context.store.connection() as db:
        rows = db.execute('SELECT * FROM launches WHERE checkout=? ORDER BY id DESC LIMIT 20', (context.checkout.id,)).fetchall()
        items = [launcher.public(row, [dict(r) for r in db.execute('SELECT id, kind, status, session FROM work WHERE launch=?', (row['id'],))],
                                 local=True) for row in rows]
    emit(dict(ok=True, checkout=context.checkout.name, launches=items))
    return 0


def cmd_mark(args):
    from . import runs
    context = load(args)
    by = args.as_name or config.require_operator()
    emit(runs.mark(context, args.scenario, args.verdict, by=by, defect=args.defect, note=args.note))
    return 0


def cmd_browser(args):
    from . import browsers, daemon
    if args.action == 'install':
        info = browsers.install(log=lambda text: print(text, file=sys.stderr))
        emit(dict(ok=True, runtime=info))
        return 0
    context = load(args)
    body = dict(checkout=context.checkout.name, identity=args.identity if args.action == 'status' else (args.identity or 'default'))
    if args.action == 'status':
        result = daemon.call(context.project, 'POST', '/local/browser/status', body) if daemon.describe(context.project)['running'] \
            else dict(ok=True, checkout=context.checkout.name, runtime=browsers.runtime_info(), browsers=[], daemon='未运行')
        emit(result)
        return 0
    if args.action == 'open':
        body.update(headed=args.headed, reset_crash=args.reset_crash, url=args.url)
    emit(daemon.call(context.project, 'POST', f'/local/browser/{args.action}', body, timeout=120))
    return 0


def cmd_report(args):
    from . import reports
    emit(reports.export(load(args), file=args.file, zip_evidence=args.zip, out=args.out))
    return 0


def cmd_retest(args):
    from . import daemon
    context = load(args)
    emit(daemon.call(context.project, 'POST', '/local/retest', dict(
        checkout=context.checkout.name, scenarios=args.scenario or None, dry_run=args.dry_run,
        operator=config.operator() or '本机'), timeout=7200))
    return 0


def cmd_distill(args):
    from . import daemon, guards
    context = load(args)
    if args.action == 'prepare':
        emit(guards.distill_prepare(context, args.scenario, session=args.session))
        return 0
    report = args.report
    work = args.work
    if work is None:
        with context.store.connection() as db:
            row = db.execute("SELECT id, dir FROM work WHERE checkout=? AND kind='distill' AND status='prepared'",
                             (context.checkout.id,)).fetchone()
        if row:
            work = row['id']
            report = report or str(Path(row['dir']) / 'guard.json')
    if not report:
        return fail('report-required', [dict(field='report', problem='请用 --report 给出 guard.json')], 2)
    emit(daemon.call(context.project, 'POST', '/local/distill/submit', dict(
        checkout=context.checkout.name, report=str(Path(report).resolve()), work=work,
        operator=config.operator() or '本机'), timeout=7200))
    return 0


def cmd_fix(args):
    from . import fixes
    context = load(args)
    if args.action == 'prepare':
        emit(fixes.prepare(context, args.scenario, session=args.session))
        return 0
    fix_id = args.fix if args.fix is not None else fixes.current(context, args.action)
    if fix_id is None:
        return fail('fix-not-found', [dict(field='fix', problem='这个检出没有可以操作的修复；用 --fix 指定编号')])
    emit({'submit': fixes.submit, 'close': fixes.close, 'abandon': fixes.abandon}[args.action](context, fix_id))
    return 0


def cmd_specs(args):
    from . import specs
    context = load(args)
    repo = context.checkout.paths.get(args.repo)
    if repo is None:
        return fail('repo-invalid', [dict(field='repo', problem=f'检出中没有仓库 {args.repo}')], 2)
    ledger = json.loads(Path(args.ledger).read_text(encoding='utf-8')) if args.ledger else []
    report = specs.check_file(repo, args.path, ledger=ledger)
    emit(dict(ok=not report['violation'], **report))
    return 0 if not report['violation'] else 1


def signer(args):
    return getattr(args, 'as_name', None) or config.require_operator()


def cmd_inbox(args):
    from . import authoring
    context = load(args, require=False)
    if args.action == 'add':
        if not args.file:
            return fail('file-required', [dict(field='file', problem='请给出文件路径')], 2)
        path = Path(args.file).expanduser()
        try:
            content = path.read_text(encoding='utf-8')
        except UnicodeDecodeError:
            return fail('upload-invalid', [dict(field='content', problem='文件不是 UTF-8 文本')])
        emit(authoring.inbox_add(context.store, args.kind, path.name, content, by=signer(args)))
    elif args.action == 'list':
        with context.store.connection() as db:
            emit(dict(ok=True, inbox=authoring.inbox_rows(db, status=None if args.all else 'pending')))
    elif args.action == 'show':
        emit(dict(ok=True, item=authoring.inbox_show(context.store, int(args.file))))
    else:
        emit(authoring.inbox_close(context.store, int(args.file), 'merged' if args.action == 'done' else 'rejected', args.note))
    return 0


def cmd_merge(args):
    from . import authoring
    context = load(args)
    if args.action == 'prepare':
        emit(authoring.merge_prepare(context, args.inbox or []))
    else:
        emit(authoring.merge_submit(context, args.work))
    return 0


def cmd_ask(args):
    from . import authoring
    context = load(args, require=False)
    emit(authoring.ask(context.store, args.about, args.text, by=signer(args), work=args.work))
    return 0


def cmd_answers(args):
    from . import authoring
    context = load(args, require=False)
    emit(dict(ok=True, answers=authoring.answers(context.store, about=args.about, unanswered=args.unanswered)))
    return 0


def cmd_sop(args):
    from . import authoring
    context = load(args)
    if args.action == 'prepare':
        emit(authoring.sop_prepare(context, args.source or []))
    else:
        if not args.file:
            return fail('file-required', [dict(field='file', problem='请用 --file 给出 SOP 文件')], 2)
        emit(authoring.sop_submit(context, args.file, by=signer(args), work_id=args.work))
    return 0


def cmd_draft(args):
    from . import authoring
    context = load(args)
    if args.action == 'annotations':
        emit(dict(ok=True, annotations=authoring.annotations(context.store, file=args.target, include_resolved=args.all)))
    elif args.action == 'resolve':
        emit(authoring.resolve_annotation(context.store, int(args.target), reject_it=args.reject, note=args.note))
    else:
        emit(authoring.draft_confirm(context, args.target, by=signer(args)))
    return 0


def cmd_atlas(args):
    from . import atlas
    context = load(args)
    if args.action == 'prepare':
        emit(atlas.prepare(context, args.entry, mode=args.mode))
    elif args.action == 'submit':
        emit(atlas.submit(context, args.entry, work_id=args.work))
    else:
        entries = [args.entry] if args.entry else list(atlas.web_entries(context.project))
        emit(dict(ok=True, atlas={entry: atlas.stale(context, entry) for entry in entries}))
    return 0


def cmd_fixtures(args):
    from . import ledger
    context = load(args)
    if args.action == 'run':
        if len(args.rest) != 1:
            return fail('recipe-required', [dict(field='recipe', problem='请给出一个配方名')], 2)
        emit(ledger.fixture_run(context.project, context.store, context.checkout, recipe=args.rest[0], params=args.param or [],
                                run=args.run, scenario=args.scenario, by=signer(args)))
    else:
        emit(ledger.fixture_run(context.project, context.store, context.checkout, argv=args.rest, reason=args.reason,
                                run=args.run, scenario=args.scenario, by=signer(args)))
    return 0


def cmd_identity(args):
    from . import ledger
    context = load(args)
    if args.action == 'record':
        emit(ledger.identity_record(context.project, context.store, context.checkout, args.identity, args.kind,
                                    by=signer(args), work=args.run))
    else:
        emit(dict(ok=True, identities=ledger.identity_status(context.project, context.store, context.checkout)))
    return 0


def cmd_attention(args):
    from . import ledger
    context = load(args, require=False)
    if args.action == 'list':
        with context.store.connection() as db:
            emit(dict(ok=True, attention=ledger.open_attention(db, context.checkout.id, local=True)))
    elif args.action == 'add':
        emit(ledger.attention_add(context.store, context.checkout.id, args.kind, args.text, by=signer(args),
                                  scenarios=args.scenario or [], work=args.run))
    else:
        emit(ledger.attention_resolve(context.store, args.id, by=signer(args), note=args.note))
    return 0


def cmd_skills(args):
    from .skill_install import install
    return install()


# ---- 解析 ----

def parser():
    root = argparse.ArgumentParser(prog='sop-board', add_help=False)
    sub = root.add_subparsers(dest='command')

    p = sub.add_parser('init', help=GROUPS['搭建']['init'])
    p.add_argument('--json', action='store_true')
    p.add_argument('name')
    p.add_argument('repos', nargs='+')
    p.add_argument('--data', help='项目数据目录所在的根目录（缺省 ~/.sop-board），项目建在 <根目录>/<名称>')
    p.set_defaults(handler=cmd_init)

    sub.add_parser('guide').set_defaults(handler=cmd_guide)

    p = sub.add_parser('operator')
    p.add_argument('--json', action='store_true')
    p.add_argument('action', nargs='?', choices=('show', 'set'), default='show')
    p.add_argument('name', nargs='*')
    p.set_defaults(handler=cmd_operator)

    p = common(sub.add_parser('checkout'))
    p.add_argument('action', choices=('list', 'show', 'create', 'remove'))
    p.add_argument('name', nargs='?')
    p.add_argument('--repo', action='append', help='create：只为这些仓库建立 worktree（可重复），其余回落到 main')
    p.add_argument('--from', dest='base_ref', help='create：新分支的起点（缺省为各仓 HEAD）')
    p.set_defaults(handler=cmd_checkout)

    p = common(sub.add_parser('status'))
    p.add_argument('name_or_project', nargs='?', metavar='项目')
    p.set_defaults(handler=cmd_status)

    p = sub.add_parser('serve')
    p.add_argument('--json', action='store_true')
    p.add_argument('name_or_project', nargs='?', metavar='项目')
    p.add_argument('--project', '-p')
    p.add_argument('--data', help='项目数据目录所在的根目录（缺省 ~/.sop-board）')
    p.add_argument('--port', type=int, default=8787)
    p.add_argument('--lan', action='store_true', help='同时在内网地址上开放（内网只能查看、复测标注、答复、批注、上传）')
    p.set_defaults(handler=cmd_serve)

    p = common(sub.add_parser('env'))
    p.add_argument('action', choices=('up', 'down', 'restart', 'status'))
    p.add_argument('--all', action='store_true', help='status：列出全部检出')
    p.set_defaults(handler=cmd_env)

    p = common(sub.add_parser('check'))
    p.set_defaults(handler=cmd_check)

    p = common(sub.add_parser('run'))
    p.add_argument('action', choices=('prepare', 'submit', 'abandon', 'launch', 'wait'))
    p.add_argument('--scope', nargs='+', help='prepare：pending（缺省）、all，或场景（<文件>#<编号>，编号唯一时可省文件）')
    p.add_argument('--session', help='prepare：执行者会话编号（缺省取 SOP_BOARD_SESSION、CODEX_THREAD_ID 或 CLAUDE_CODE_SESSION_ID）')
    p.add_argument('--fix', type=int, help='prepare / launch：修复的独立复验，范围为该修复的问题场景')
    p.add_argument('--run', type=int, help='submit / abandon / wait：运行编号')
    p.add_argument('--implementer', help='submit / launch：实现者会话编号；不填记为独立性未声明')
    p.add_argument('--launch', type=int, help='wait：启动编号（缺省为本检出最近一次启动）')
    p.add_argument('--timeout', type=int, default=1800, help='wait：最长等待秒数，0 不限时（缺省 1800）')
    p.set_defaults(handler=cmd_run)

    p = common(sub.add_parser('launch'))
    p.add_argument('action', choices=('status', 'stop'))
    p.add_argument('--launch', type=int)
    p.set_defaults(handler=cmd_launch)

    p = common(sub.add_parser('mark'))
    p.add_argument('scenario', help='<文件>#<编号>，编号唯一时可省文件')
    p.add_argument('verdict', choices=('pass', 'fail', 'blocked', 'changed'),
                   help='pass 通过、fail 不通过、blocked 阻塞、changed 需求变更')
    p.add_argument('--defect', help='可选：缺陷链接或编号')
    p.add_argument('--note', help='可选：说明（阻塞原因、需求怎样变了等）')
    p.add_argument('--as', dest='as_name', metavar='姓名', help='别人复测时写他的姓名；缺省为本机操作者')
    p.set_defaults(handler=cmd_mark)

    p = common(sub.add_parser('browser'))
    p.add_argument('action', choices=('install', 'status', 'open', 'release', 'close'),
                   help='release：真人完成后交还，窗口保持打开、之后的运行直接沿用；close：结束浏览器')
    p.add_argument('--identity', help='身份编号（open/release/close 缺省 default；status 缺省列出全部）')
    p.add_argument('--headed', action='store_true', help='open：打开有界面的窗口并持有真人租约（真人登录、人机验证、二次验证）')
    p.add_argument('--reset-crash', action='store_true', help='open：清除 crashed 状态并重新启动')
    p.add_argument('--url', help='open：打开的地址（缺省为该检出的 Web 入口地址）')
    p.set_defaults(handler=cmd_browser)

    p = common(sub.add_parser('report'))
    p.add_argument('--file', help='只导出这个场景文件')
    p.add_argument('--zip', action='store_true', help='同时打包所引证据')
    p.add_argument('--out', help='输出目录（缺省为数据目录 reports/ 下的新目录）')
    p.set_defaults(handler=cmd_report)

    p = common(sub.add_parser('retest'))
    p.add_argument('--scenario', nargs='+', help='只运行这些场景的守护（缺省为全部待复测且守护有效的场景）')
    p.add_argument('--dry-run', action='store_true', help='只列出场景与将运行的命令')
    p.set_defaults(handler=cmd_retest)

    p = common(sub.add_parser('distill'))
    p.add_argument('action', choices=('prepare', 'submit'))
    p.add_argument('scenario', nargs='*', help='prepare：要沉淀的场景（缺省为全部待沉淀）')
    p.add_argument('--session', help='prepare：沉淀会话编号')
    p.add_argument('--report', help='submit：guard.json（缺省为进行中沉淀目录里的 guard.json）')
    p.add_argument('--work', type=int, help='submit：沉淀编号')
    p.set_defaults(handler=cmd_distill)

    p = common(sub.add_parser('fix'))
    p.add_argument('action', choices=('prepare', 'submit', 'close', 'abandon'))
    p.add_argument('scenario', nargs='*', help='prepare：问题场景（缺省为当前检出全部失败或受阻的场景）')
    p.add_argument('--session', help='prepare：修复会话编号')
    p.add_argument('--fix', type=int)
    p.set_defaults(handler=cmd_fix)

    p = common(sub.add_parser('specs'))
    p.add_argument('action', choices=('check',))
    p.add_argument('--repo', required=True, help='spec 所在仓库别名')
    p.add_argument('path', help='仓内相对路径')
    p.add_argument('--ledger', help='账本 JSON 数组（改写或撤销断言的理由）')
    p.set_defaults(handler=cmd_specs)

    p = common(sub.add_parser('inbox'))
    p.add_argument('action', choices=('add', 'list', 'show', 'done', 'reject'))
    p.add_argument('file', nargs='?', help='add：文件路径；show/done/reject：收件箱编号')
    p.add_argument('--kind', choices=('cases', 'sop'), default='cases')
    p.add_argument('--all', action='store_true', help='list：包含已合并与已拒绝')
    p.add_argument('--note')
    p.add_argument('--as', dest='as_name', metavar='姓名', help='别人上传时写他的姓名')
    p.set_defaults(handler=cmd_inbox)

    p = common(sub.add_parser('merge'))
    p.add_argument('action', choices=('prepare', 'submit'))
    p.add_argument('--inbox', type=int, action='append', help='prepare：收件箱编号（可重复；缺省为全部待合并用例）')
    p.add_argument('--work', type=int)
    p.set_defaults(handler=cmd_merge)

    p = common(sub.add_parser('ask'))
    p.add_argument('--about', required=True, help='inbox:<编号>、<文件>#<场景编号> 或场景文件名')
    p.add_argument('--text', required=True)
    p.add_argument('--work', type=int)
    p.add_argument('--as', dest='as_name', metavar='姓名')
    p.set_defaults(handler=cmd_ask)

    p = common(sub.add_parser('answers'))
    p.add_argument('--about')
    p.add_argument('--unanswered', action='store_true')
    p.set_defaults(handler=cmd_answers)

    p = common(sub.add_parser('sop'))
    p.add_argument('action', choices=('prepare', 'submit'))
    p.add_argument('--source', action='append', help='prepare：资料文件（可重复）')
    p.add_argument('--file', help='submit：场景目录下的 SOP Markdown')
    p.add_argument('--work', type=int)
    p.add_argument('--as', dest='as_name', metavar='姓名', help='别人确认时写他的姓名')
    p.set_defaults(handler=cmd_sop)

    p = common(sub.add_parser('draft'))
    p.add_argument('action', choices=('annotations', 'resolve', 'confirm'))
    p.add_argument('target', nargs='?', help='annotations：场景文件名（可省）；resolve：批注编号；confirm：草稿文件名')
    p.add_argument('--all', action='store_true', help='annotations：包含已处理的批注')
    p.add_argument('--reject', action='store_true', help='resolve：不采纳')
    p.add_argument('--note')
    p.add_argument('--as', dest='as_name', metavar='姓名', help='confirm：别人确认时写他的姓名')
    p.set_defaults(handler=cmd_draft)

    p = common(sub.add_parser('atlas'))
    p.add_argument('action', choices=('prepare', 'submit', 'stale'))
    p.add_argument('--entry', help='Web 入口名称')
    p.add_argument('--mode', choices=('full', 'stale'), default='full')
    p.add_argument('--work', type=int)
    p.set_defaults(handler=cmd_atlas)

    p = common(sub.add_parser('fixtures'))
    p.add_argument('action', choices=('run', 'exec'))
    p.add_argument('rest', nargs='*', metavar='配方或命令', help='run：配方名；exec：-- 之后的命令')
    p.add_argument('--param', action='append', help='run：参数 名=值（可重复）')
    p.add_argument('--run', type=int, help='所属运行编号（留痕写进运行目录）')
    p.add_argument('--scenario')
    p.add_argument('--reason', help='exec：临时写入的理由')
    p.add_argument('--as', dest='as_name', metavar='姓名')
    p.set_defaults(handler=cmd_fixtures)

    p = common(sub.add_parser('identity'))
    p.add_argument('action', choices=('record', 'status'))
    p.add_argument('--identity')
    p.add_argument('--kind', choices=('login', 'code'))
    p.add_argument('--run', type=int)
    p.add_argument('--as', dest='as_name', metavar='姓名')
    p.set_defaults(handler=cmd_identity)

    p = common(sub.add_parser('attention'))
    p.add_argument('action', choices=('list', 'add', 'resolve'))
    p.add_argument('--kind', default='其他')
    p.add_argument('--text')
    p.add_argument('--scenario', action='append')
    p.add_argument('--run', type=int)
    p.add_argument('id', nargs='?', type=int, help='resolve：待人处理编号')
    p.add_argument('--note')
    p.add_argument('--as', dest='as_name', metavar='姓名')
    p.set_defaults(handler=cmd_attention)

    p = sub.add_parser('skills')
    p.add_argument('action', choices=('install',))
    p.set_defaults(handler=cmd_skills)

    for module in EXTENSIONS:
        module.register(sub, common)
    return root


EXTENSIONS = []


def usage():
    lines = ['用法：sop-board <命令> [参数]    sop-board <命令> --help    sop-board --version',
             '项目与检出缺省按当前目录识别；可用 --project/--data 与 --checkout 指定。']
    for title, commands in GROUPS.items():
        lines.append('\n' + title)
        lines += [f'  {name:10} {text}' for name, text in commands.items()]
    return '\n'.join(lines)


def daemon_module():
    from . import daemon
    return daemon


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv in ([], ['--help'], ['-h'], ['help']):
        print(usage())
        return 0
    if argv == ['--version']:
        print(version())
        return 0
    if argv[0] == 'playwright':
        from .playwright import command
        return command(argv[1:])
    args = parser().parse_args(argv)
    if not getattr(args, 'handler', None):
        print(usage())
        return 2
    try:
        return args.handler(args)
    except config.ProjectNotFound as error:
        return fail('project-not-found', [dict(field='project', problem=str(error))])
    except config.ProjectFormat as error:
        return fail('project-format', [dict(field='project', problem=str(error))])
    except daemon_module().DaemonUnavailable as error:
        return fail('daemon-not-running', [dict(field='daemon', problem=str(error))])
    except APIError as error:
        return fail(error.code, error.problems)
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        return fail('invalid', [dict(field='command', problem=str(error))])


if __name__ == '__main__':
    sys.exit(main())
