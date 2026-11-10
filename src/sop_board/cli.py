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
    parser.add_argument('--data', help='项目数据目录')
    parser.add_argument('--checkout', '-c', help='检出名称（缺省按当前目录识别，main 为登记路径）')
    return parser


def load(args, *, require=True):
    data = config.resolve_data(getattr(args, 'project', None), getattr(args, 'data', None))
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
    emit(dict(ok=True, project=args.name, data=str(data), repositories=identities,
              next=f'在 {data}/config.yaml 写好 scenarios、skill、entries 与 adapter 后运行 sop-board check --project {args.name}'))
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
        created = checkouts.build(project, store, args.name, aliases=args.repo or None, base=args.base, log=log)
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
                      counts=derived['counts'], distill=derived['distill'], total=derived['total'],
                      queues={state: sorted(k for k, v in derived['items'].items() if v['state'] == state)
                              for state in ('retest', 'fail', 'blocked', 'not-run')},
                      distill_queue=sorted(k for k, v in derived['items'].items() if v['distill']),
                      invalid_files={name: info['problems'] for name, info in found['files'].items() if info['problems']})
        from . import daemon
        result['daemon'] = daemon.describe(project)
        from . import ledger
        with context.store.connection() as db:
            result['attention'] = ledger.open_attention(db, context.checkout.id, local=True)
    if args.json:
        emit(result)
        return 0
    print(f"项目 {project.name} · 检出 {context.checkout.name} · 操作者 {result['operator'] or '未设置'}")
    if not project.configured:
        print('配置不合格：')
        for item in project.problems:
            print(f"  {item['field']}：{item['problem']}")
        return 0
    counts = result['counts']
    print(f"场景 {result['total']} 条：" + '；'.join(f"{status.LABELS[s]} {counts[s]}" for s in status.ORDER if counts[s])
          + f"；待沉淀 {result['distill']}")
    for name, problems in result['invalid_files'].items():
        print(f'场景文件 {name} 不合格：' + '；'.join(p['problem'] for p in problems[:3]))
    daemon_state = result['daemon']
    print('看板守护进程：' + (f"运行中 {daemon_state['url']}" if daemon_state['running'] else '未运行（在终端运行 sop-board serve）'))
    for item in result['attention']:
        print(f"待人处理 #{item['id']} {item['kind']}：{item['text']}")
    return 0


def cmd_serve(args):
    from . import server
    project = config.Project(config.resolve_data(args.project, args.data))
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


CHECKS = []


def cmd_skills(args):
    from .skill_install import install
    return install()


# ---- 解析 ----

def parser():
    root = argparse.ArgumentParser(prog='sop-board', add_help=False)
    sub = root.add_subparsers(dest='command')

    p = sub.add_parser('init', help=GROUPS['搭建']['init'])
    p.add_argument('name')
    p.add_argument('repos', nargs='+')
    p.add_argument('--data')
    p.set_defaults(handler=cmd_init)

    sub.add_parser('guide').set_defaults(handler=cmd_guide)

    p = sub.add_parser('operator')
    p.add_argument('action', nargs='?', choices=('show', 'set'), default='show')
    p.add_argument('name', nargs='*')
    p.set_defaults(handler=cmd_operator)

    p = common(sub.add_parser('checkout'))
    p.add_argument('action', choices=('list', 'show', 'create', 'remove'))
    p.add_argument('name', nargs='?')
    p.add_argument('--repo', action='append', help='create：只为这些仓库建立 worktree（可重复），其余回落到 main')
    p.add_argument('--base', help='create：新分支的起点（缺省为各仓 HEAD）')
    p.set_defaults(handler=cmd_checkout)

    p = common(sub.add_parser('status'))
    p.add_argument('--json', action='store_true')
    p.set_defaults(handler=cmd_status)

    p = sub.add_parser('serve')
    p.add_argument('--project', '-p')
    p.add_argument('--data')
    p.add_argument('--port', type=int, default=8787)
    p.add_argument('--lan', action='store_true', help='同时在内网地址上开放（内网只能查看、复测标注、答复、批注、上传）')
    p.set_defaults(handler=cmd_serve)

    p = common(sub.add_parser('env'))
    p.add_argument('action', choices=('up', 'down', 'status'))
    p.add_argument('--all', action='store_true', help='status：列出全部检出')
    p.set_defaults(handler=cmd_env)

    p = common(sub.add_parser('check'))
    p.set_defaults(handler=cmd_check)

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
    except APIError as error:
        return fail(error.code, error.problems)
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        return fail('invalid', [dict(field='command', problem=str(error))])


if __name__ == '__main__':
    sys.exit(main())
