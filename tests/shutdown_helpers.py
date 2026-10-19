"""在真实写请求等待数据库锁时，确定性地核对关闭过程。"""

import http.client
import json
import socket
import sqlite3
import time

from helpers import upload


def repeated_signals_during_write(board, first, second, send_signal=None):
    upload(board, b"already committed", "before.txt")
    pending = b"commit this request before exiting"
    blocker = sqlite3.connect(board.data / "board.sqlite3")
    client = None
    response_status, response_body = None, None
    try:
        blocker.execute("BEGIN IMMEDIATE")
        client = socket.create_connection(("127.0.0.1", board.port), timeout=5)
        # The fixed token belongs exclusively to the isolated test fixture.
        headers = ("POST /api/v1/evidence HTTP/1.0\r\n"
                   "Authorization: Bearer isolated-test\r\n"
                   "X-Evidence-Name: shutdown.txt\r\n"
                   f"Content-Length: {len(pending)}\r\n\r\n")
        client.sendall(headers.encode() + pending)
        # A second accepted request proves the pending worker was started.
        assert board.request("GET", "/", auth=False)[0] == 200
        send_signal = send_signal or board.process.send_signal
        send_signal(first)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            with socket.socket() as probe:
                probe.settimeout(0.1)
                if probe.connect_ex(("127.0.0.1", board.port)) != 0:
                    break
            time.sleep(0.01)
        else:
            raise AssertionError("首次信号后监听没有关闭")
        assert board.process.poll() is None, "尚有请求未完成，进程不应已退出"
        send_signal(second)
        blocker.rollback()
        with http.client.HTTPResponse(client) as response:
            try:
                response.begin()
                response_status = response.status
                response_body = json.loads(response.read())
            except (OSError, http.client.HTTPException):
                # Keep the exit code and traceback as the primary failure.
                pass
        code = board.process.wait(timeout=5)
    finally:
        blocker.close()
        if client is not None:
            client.close()
    return dict(code=code, status=response_status, body=response_body,
                log=board.log.read_text(), pending=pending)
