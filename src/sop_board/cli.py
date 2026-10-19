"""本机服务的命令行入口。"""

import argparse
from http.server import ThreadingHTTPServer
import os
from pathlib import Path
import signal
import sqlite3
import sys

from .service import Service
from .storage import Store
from .http import Handler
from .network import local_ipv4_addresses


class Stopped(BaseException):
    """让主线程退出监听循环，并等待已接收的请求结束。"""


def prepare_store(data):
    created_directories = []
    parent = data
    while not (parent.exists() or parent.is_symlink()):
        created_directories.append(parent)
        parent = parent.parent
    sqlite_files = [data / ("board.sqlite3" + suffix) for suffix in ("", "-wal", "-shm", "-journal")]
    new_files = [path for path in sqlite_files if not (path.exists() or path.is_symlink())]
    try:
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
    args = parser.parse_args()
    token = os.environ.get("SOP_BOARD_TOKEN", "")
    if not token.strip():
        print("启动失败：缺少非空 SOP_BOARD_TOKEN", file=sys.stderr)
        return 1
    try:
        server = ThreadingHTTPServer((args.host, args.port), Handler)
        server.daemon_threads = False
    except OSError as error:
        print(f"启动失败：无法监听端口 {args.port}：{error}", file=sys.stderr)
        return 1
    try:
        try:
            store = prepare_store(args.data)
        except (OSError, sqlite3.Error) as error:
            print(f"启动失败：数据目录 {args.data} 无法创建或写入：{error}", file=sys.stderr)
            return 1
        server.service = Service(store)
        server.token = token

        def stop(signum, frame):
            raise Stopped()

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        display_host = "127.0.0.1" if args.host == "0.0.0.0" else args.host
        print(f"看板地址：http://{display_host}:{server.server_port}/", flush=True)
        if args.host == "0.0.0.0":
            for address in local_ipv4_addresses():
                print(f"内网地址：http://{address}:{server.server_port}/", flush=True)
        server.serve_forever(poll_interval=0.1)
    except Stopped:
        pass
    finally:
        server.server_close()
    return 0
