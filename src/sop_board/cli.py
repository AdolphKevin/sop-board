"""本机服务的命令行入口。"""

import argparse
from http.server import ThreadingHTTPServer
import os
from pathlib import Path
import signal
import sqlite3
import sys

from .service import Service, project_key
from .storage import Store
from .tasks import Tasks, parse_timeout
from .http import Handler
from .jira import Jira
from .network import local_ipv4_addresses
from .repositories import authorize, Discovery


def prepare_store(data):
    created_directories = []
    parent = data
    while not (parent.exists() or parent.is_symlink()):
        created_directories.append(parent)
        parent = parent.parent
    sqlite_files = [data / ("board.sqlite3" + suffix) for suffix in ("", "-wal", "-shm", "-journal")]
    new_files = [path for path in sqlite_files if not (path.exists() or path.is_symlink())]
    try:
        if data.exists() and not data.is_dir():
            raise NotADirectoryError(f"{data} 不是目录")
        data.mkdir(parents=True, exist_ok=True)
        if not os.access(data, os.W_OK):
            raise PermissionError("目录不可写")
        return Store(data)
    except BaseException:
        # A restrictive umask can make our own new directory inaccessible.
        for directory in reversed(created_directories):
            if directory.is_dir():
                directory.chmod(0o700)
        for path in new_files:
            if path.is_file():
                path.unlink()
        for directory in created_directories:
            if directory.is_dir():
                directory.rmdir()
        raise


def main():
    parser = argparse.ArgumentParser(description="启动 SOP 验证协作看板")
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--writable-repo", action="append", default=[])
    args = parser.parse_args()
    try:
        codex_timeout = parse_timeout(os.environ.get("SOP_BOARD_CODEX_TIMEOUT"))
        repos = authorize(args.writable_repo)
        data_path = args.data.resolve()
        if any(data_path == path or path in data_path.parents for path in repos.values()):
            raise ValueError('数据目录不能位于授权仓库内')
    except ValueError as error:
        print(f"启动失败：{error}", file=sys.stderr)
        return 1
    try:
        server = ThreadingHTTPServer((args.host, args.port), Handler)
        server.daemon_threads = False
    except OSError as error:
        print(f"启动失败：无法监听端口 {args.port}：{error}", file=sys.stderr)
        return 1
    stopping = False
    discovery = None
    tasks = None
    try:
        try:
            store = prepare_store(args.data)
        except (OSError, sqlite3.Error) as error:
            print(f"启动失败：数据目录 {args.data} 无法创建或写入：{error}", file=sys.stderr)
            return 1
        server.service = Service(store, jira_url=os.environ.get("JIRA_URL", ""),
                                 writable_repos={project_key(k): p for k, p in repos.items()})
        with store.transaction() as db:
            for identity, path in repos.items():
                prefix = f"https://{identity}/issues/" if identity.startswith("github.com/") else None
                db.execute("INSERT OR IGNORE INTO projects VALUES (?,?,?,?)",
                           (project_key(identity), identity, path.name, prefix))
        server.jira = Jira(os.environ.get("JIRA_URL", ""), os.environ.get("JIRA_EMAIL", ""),
                           os.environ.get("JIRA_API_TOKEN", ""),
                           timeout=os.environ.get("JIRA_TIMEOUT_SECONDS", "10"))
        server.service.jira = server.jira
        tasks = server.tasks = Tasks(server.service, timeout=codex_timeout)
        discovery = Discovery(server.service, repos)
        discovery.scan()
        discovery.thread.start()
        server.timeout = 0.1

        def stop(signum, frame):
            nonlocal stopping
            stopping = True

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        display_host = "127.0.0.1" if args.host == "0.0.0.0" else args.host
        for identity, path in repos.items():
            print(f"可写授权：{identity}（{path}）", flush=True)
        print(f"看板地址：http://{display_host}:{server.server_port}/", flush=True)
        if args.host == "0.0.0.0":
            for address in local_ipv4_addresses():
                print(f"内网地址：http://{address}:{server.server_port}/", flush=True)
        while not stopping:
            server.handle_request()
    finally:
        stopping = True
        server.server_close()
        if tasks:
            tasks.close()
        if discovery and discovery.thread.is_alive():
            discovery.close()
    return 0
