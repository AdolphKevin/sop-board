import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import shutil
import time
from urllib.error import HTTPError
from urllib.request import Request, build_opener, ProxyHandler

import pytest


ROOT = Path(__file__).resolve().parents[1]
# This value belongs only to isolated test processes; never inherit a user's token.
TEST_ENV = {k: v for k, v in os.environ.items() if k not in ("SOP_BOARD_TOKEN", "JIRA_URL", "JIRA_EMAIL", "JIRA_API_TOKEN", "JIRA_TIMEOUT_SECONDS")}
TEST_ENV["SOP_BOARD_TOKEN"] = "isolated-test"
OPENER = build_opener(ProxyHandler({}))


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Board:
    def __init__(self, data, port, log, host=None, command=None, env=None):
        self.data, self.port, self.log = data, port, log
        self.host = host
        self.command = command or [str(ROOT / ".venv/bin/sop-board")]
        self.env = {**TEST_ENV, **(env or {})}
        self.process = None
        self.start()

    def start(self):
        args = [*self.command, "--data", str(self.data), "--port", str(self.port)]
        if self.host:
            args.extend(["--host", self.host])
        self.output = self.log.open("ab")
        self.process = subprocess.Popen(args, env=self.env, stdout=self.output,
                                        stderr=subprocess.STDOUT, cwd=ROOT)
        until = time.monotonic() + 8
        while time.monotonic() < until:
            if self.process.poll() is not None:
                break
            try:
                status, _, _ = self.request("GET", "/", auth=False)
                if status == 200:
                    return
            except OSError:
                time.sleep(0.02)
        self.stop()
        pytest.fail("服务未就绪：\n" + self.log.read_text())

    def stop(self):
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
                pytest.fail(f"服务未能停止，进程号 {self.process.pid}")
        self.output.close()

    def request(self, method, path, body=None, *, auth=True, headers=None):
        headers = dict(headers or {})
        if auth:
            headers.setdefault("Authorization", "Bearer isolated-test")
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode()
            headers["Content-Type"] = "application/json"
        req = Request(f"http://127.0.0.1:{self.port}{path}", data=body,
                      headers=headers, method=method)
        try:
            response = OPENER.open(req, timeout=10)
        except HTTPError as error:
            response = error
        with response:
            raw = response.read()
            content = (json.loads(raw) if raw and "application/json" in
                       response.headers.get("Content-Type", "") else raw)
            return response.status, content, response.headers

    def sql(self, statement, parameters=()):
        with sqlite3.connect(self.data / "board.sqlite3") as db:
            return db.execute(statement, parameters).fetchall()


@pytest.fixture
def board_factory(tmp_path):
    boards = []

    def make(host=None, command=None, env=None):
        index = len(boards)
        board = Board(tmp_path / f"data-{index}", free_port(),
                      tmp_path / f"server-{index}.log", host, command, env)
        boards.append(board)
        return board

    yield make
    for board in boards:
        try:
            board.stop()
        finally:
            shutil.rmtree(board.data)


@pytest.fixture
def board(board_factory):
    return board_factory()
