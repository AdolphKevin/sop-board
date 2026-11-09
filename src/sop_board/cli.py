"""工作区看板的命令行入口：启动、搭建指引、自检。"""
import argparse
from http.server import ThreadingHTTPServer
from importlib.resources import files
from importlib import metadata
import json
import os
from pathlib import Path
import signal
import sqlite3
import sys

from .adapters import Environment
from .workspace import Workspace, initialize
from .service import Service
from .storage import Store
from .tasks import Tasks, parse_timeout
from .http import Handler
from .jira import Jira
from .network import local_ipv4_addresses
from .repositories import Discovery


COMMAND_GROUPS = {
    '看板': {'<工作区>': '启动看板，可加 --lan 与 --paused', 'status': '查看批次、就绪情况和待处理事项',
             'check': '检查工作区与运行环境', 'run': '管理连续执行', 'plan': '查看执行计划'},
    '会话入口': {'sop': '登记或编写业务 SOP', 'draft': '生成、修订与确认草稿', 'cases': '上传、预览增改或导出用例',
                 'merge': '合并用例、答复缺口或还原场景', 'execute': '准备与提交场景执行', 'fix': '修复、独立复验与关闭',
                 'task': '列出、查看、等待或取消任务', 'atlas': '梳理交互地图与场景绑定', 'distill': '登记回归测试关联'},
    '台账': {'attention': '登记与处理待办事项', 'confirmer': '查看或设置默认业务确认人', 'issue': '维护问题台账',
             'decision': '登记与查看人的决定', 'identity': '管理执行身份与使用记录', 'fixtures': '准备隔离数据并留痕'},
    '安装': {'init': '登记工作区与仓库', 'guide': '读取完整搭建指引', 'playwright': '读取 Web 驱动指引与转换执行材料',
             'skills': '安装随包技能到两个客户端'},
}


def version():
    try:
        distribution = metadata.distribution('sop-board')
        direct = json.loads(distribution.read_text('direct_url.json') or '{}')
        source = direct.get('url') or str(distribution.locate_file(''))
        return f"sop-board {distribution.version}\n安装来源：{source}\n代码位置：{Path(__file__).resolve().parent}"
    except metadata.PackageNotFoundError:
        # 直接从源码运行时仍可报告该检出的包版本。
        import re
        root = Path(__file__).resolve().parents[2]
        package_version = re.search(r'^version = "([^"]+)"', (root / 'pyproject.toml').read_text(), re.M)[1]
        return f'sop-board {package_version}\n安装来源：源码 {root}'


def main():
    argv = sys.argv[1:]
    if argv == ['--version']:
        print(version())
        return 0
    if argv and argv[0] in ('--help', '-h'):
        print('用法：sop-board <工作区> [--data 目录] [--port 端口] [--lan] [--paused]\n'
              '      sop-board <子命令> --help\n      sop-board --version')
        for title, commands in COMMAND_GROUPS.items():
            print('\n' + title)
            for command, description in commands.items():
                print(f'  {command:14} {description}')
        print('\n启动参数：--port 默认 8787；--lan 允许内网只读访问；--paused 暂停自动任务。')
        return 0
    if argv and argv[0] == 'attention':
        from .attention import command
        return command(argv[1:])
    if argv and argv[0] == 'confirmer':
        from .session import confirmer_command
        return confirmer_command(argv[1:])
    if argv and argv[0] in ('issue', 'decision'):
        from .issues import command
        return command(argv)
    if argv and argv[0] == 'identity':
        from .identities import command
        return command(argv[1:])
    if argv and argv[0] in ('run', 'plan'):
        from .plans import command
        return command(argv)
    if argv and argv[0] == 'atlas':
        from .atlas import command
        return command(argv[1:])
    if argv and argv[0] == 'fixtures':
        from .fixtures import command
        return command(argv[1:])
    if argv and argv[0] in ('status', 'task', 'draft', 'cases', 'merge', 'execute', 'fix', 'sop'):
        from .session import command
        return command(argv)
    if argv and argv[0] == 'playwright':
        from .playwright import command
        return command(argv[1:])
    if argv and argv[0] == 'skills':
        from .skill_install import command
        return command(argv[1:])
    if argv and argv[0] == 'distill':
        from .distill import command
        return command(argv[1:])
    action = argv.pop(0) if argv and argv[0] in ('guide', 'check', 'init') else 'serve'
    if action == 'guide':
        if argv:
            print('用法：sop-board guide', file=sys.stderr)
            return 2
        sys.stdout.write(files('sop_board').joinpath('guide.md').read_text(encoding='utf-8'))
        from .playwright import guide
        sys.stdout.write('\n' + guide())
        return 0
    parser = argparse.ArgumentParser(description={'serve': '启动 Flow Board 流程验收看板', 'check': '自检工作区适配', 'init': '登记工作区名称和仓库'}[action])
    parser.add_argument('workspace', nargs=None if action == 'init' else '?', help='工作区名称')
    if action == 'init':
        parser.add_argument('repositories', nargs='+', type=Path)
    parser.add_argument('--data', type=Path)
    if action == 'serve':
        parser.add_argument('--port', type=int, default=8787)
        parser.add_argument('--lan', action='store_true')
        parser.add_argument('--paused', action='store_true', help='启动前暂停自动任务（验收或排查时避免一启动就派发任务），之后在页面上恢复')
    args = parser.parse_args(argv)
    environment = tasks = discovery = server = None
    try:
        if action == 'init':
            data, identities = initialize(args.workspace, args.repositories, args.data)
            print('数据目录：' + str(data))
            for alias, identity in identities.items():
                print(alias + '：' + identity)
            print('继续搭建：')
            for line in Workspace(data=data).setup_instructions():
                print(line)
            return 0
        timeout = parse_timeout(os.environ.get('SOP_BOARD_CODEX_TIMEOUT'))
        workspace = Workspace(args.workspace, args.data)
        environment = Environment(workspace)
        if action == 'check':
            result = environment.check()
            if result.get('message'):
                print(result['message'],flush=True)
            for credential in result['credentials']:
                # 前置数据的凭据借用入口结构登记，显示时不冒充入口名称。
                owner = '入口 ' + credential['entry'] if credential['entry'] in workspace.entries else '前置数据'
                print(f"{owner} · {credential['name']}：{credential['status']}", flush=True)
            for number, step in enumerate(result['steps'], 1):
                print(f"{number}. {step['name']}：{step['status']} · {step['message']}", flush=True)
                if step['log']:
                    print('   日志：' + step['log'], flush=True)
            if result.get('path'):
                print('自检报告：' + result['path'], flush=True)
            else:
                print('自检报告未能保存；已记录的自检结果保持不变',flush=True)
                if not environment.process and not environment.port_locks:
                    # check 已释放资源；目录不可写时不在 finally 再写一次环境状态。
                    environment = None
            return 0 if result['ok'] else 1
        server = ThreadingHTTPServer(('0.0.0.0' if args.lan else '127.0.0.1', args.port), Handler)
        server.daemon_threads = False
        store = Store(workspace.data)
        with store.transaction() as db:
            db.execute('INSERT INTO workspaces(key,root,name) VALUES (?,?,?) '
                       'ON CONFLICT(key) DO UPDATE SET root=excluded.root,name=excluded.name',
                       (workspace.name, str(workspace.data), workspace.name))
            db.execute("INSERT INTO settings(key,value) VALUES ('board_url',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                       (f'http://127.0.0.1:{server.server_port}',))
        service = server.service = Service(store, workspace, environment)
        if args.paused:
            service.set_automation_paused(True)
            print('自动任务已暂停；在看板页面恢复后才派发任务', flush=True)
        server.jira = service.jira = Jira(os.environ.get('JIRA_EMAIL', ''),
                                        os.environ.get('JIRA_API_TOKEN', ''), timeout=os.environ.get('JIRA_TIMEOUT_SECONDS', '10'))
        if environment.approved():
            try:
                with environment.operation:
                    environment.ensure_baseline()
            except (OSError, ValueError) as error:
                print('环境未就绪：' + str(error), file=sys.stderr, flush=True)
        tasks = server.tasks = Tasks(service, timeout=timeout)
        discovery = Discovery(service)
        discovery.scan()
        discovery.thread.start()
        stopping = False
        def stop(signum, frame):
            nonlocal stopping
            stopping = True
        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        server.timeout = .1
        print('工作区：' + workspace.name, flush=True)
        print('数据目录：' + str(workspace.data), flush=True)
        print(f'看板地址：http://127.0.0.1:{server.server_port}/', flush=True)
        if args.lan:
            for address in local_ipv4_addresses():
                print(f'内网地址：http://{address}:{server.server_port}/', flush=True)
        while not stopping:
            server.handle_request()
        return 0
    except (OSError, ValueError, sqlite3.Error) as error:
        print(('无法登记工作区：' if action == 'init' else '无法运行看板：') + str(error), file=sys.stderr)
        return 1
    finally:
        if environment:
            environment.stopping = True
        if server:
            server.server_close()
        if discovery and discovery.thread.is_alive():
            discovery.close()
        if tasks:
            tasks.close()
        if environment:
            environment.close()
