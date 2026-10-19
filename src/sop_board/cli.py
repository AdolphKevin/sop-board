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


class Stopped(BaseException):
    """让主线程退出监听循环，并等待已接收的请求结束。"""


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
        args.data.mkdir(parents=True, exist_ok=True)
        if not os.access(args.data, os.W_OK):
            raise PermissionError("目录不可写")
        store = Store(args.data)
    except (OSError, sqlite3.Error) as error:
        print(f"启动失败：数据目录 {args.data} 无法创建或写入：{error}", file=sys.stderr)
        return 1
    try:
        server = ThreadingHTTPServer((args.host, args.port), Handler)
        server.daemon_threads = False
        server.service = Service(store)
        server.token = token
    except OSError as error:
        print(f"启动失败：无法监听端口 {args.port}：{error}", file=sys.stderr)
        return 1

    def stop(signum, frame):
        raise Stopped()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    print(f"看板地址：http://{args.host}:{server.server_port}/", flush=True)
    if args.host == "0.0.0.0":
        print("其他成员可使用本机内网地址访问看板。", flush=True)
    try:
        server.serve_forever(poll_interval=0.1)
    except Stopped:
        pass
    finally:
        server.server_close()
    return 0
