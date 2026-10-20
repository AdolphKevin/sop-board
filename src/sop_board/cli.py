"""工作区看板的命令行入口：启动、搭建指引、自检。"""
import argparse
from http.server import ThreadingHTTPServer
from importlib.resources import files
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


def main():
    argv = sys.argv[1:]
    if argv and argv[0] == 'playwright':
        from .playwright import command
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
    parser = argparse.ArgumentParser(description={'serve': '启动工作区 SOP 验证协作看板', 'check': '自检工作区适配', 'init': '登记工作区名称和仓库'}[action])
    parser.add_argument('workspace', nargs=None if action == 'init' else '?', help='工作区名称')
    if action == 'init':
        parser.add_argument('repositories', nargs='+', type=Path)
    parser.add_argument('--data', type=Path)
    if action == 'serve':
        parser.add_argument('--port', type=int, default=8787)
        parser.add_argument('--lan', action='store_true')
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
            for number, step in enumerate(result['steps'], 1):
                print(f"{number}. {step['name']}：{step['status']} · {step['message']}", flush=True)
                if step['log']:
                    print('   日志：' + step['log'], flush=True)
            print('自检报告：' + result['path'], flush=True)
            return 0 if result['ok'] else 1
        server = ThreadingHTTPServer(('0.0.0.0' if args.lan else '127.0.0.1', args.port), Handler)
        server.daemon_threads = False
        store = Store(workspace.data)
        with store.transaction() as db:
            db.execute('INSERT INTO workspaces(key,root,name) VALUES (?,?,?) '
                       'ON CONFLICT(key) DO UPDATE SET root=excluded.root,name=excluded.name',
                       (workspace.name, str(workspace.data), workspace.name))
        service = server.service = Service(store, workspace, environment)
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
