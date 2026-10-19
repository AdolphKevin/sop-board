"""SOP-BOARD-1 场景版本 4：命令入口与重启后的数据。"""

import hashlib
import signal
import socket
import subprocess
import time

import pytest

from conftest import OPENER, ROOT, TEST_ENV, free_port
from helpers import BASE, push, round_body, upload
from test_marks import mark_body
from test_reads import get


def test_signals_preserve_round_and_continue_numbering(board):
    """SOP-01-P03：两种停止信号释放端口，保留首轮并接续轮次号。"""
    pushed, _ = push(board)
    body = round_body(pushed, upload(board))
    assert board.request("POST", BASE + "/rounds", body)[1]["round"] == 1
    original = get(board, BASE + "/rounds/1")
    assert board.sql("PRAGMA journal_mode") == [("wal",)]
    for stop_signal in (signal.SIGINT, signal.SIGTERM):
        board.process.send_signal(stop_signal)
        board.process.wait(timeout=5)
        with socket.socket() as probe:
            assert probe.connect_ex(("127.0.0.1", board.port)) != 0
        board.stop()
        assert board.sql("PRAGMA integrity_check") == [("ok",)]
        board.start()
        assert get(board, BASE + "/rounds/1") == original
    body["idempotency_key"] = "after-two-restarts"
    status, response, _ = board.request("POST", BASE + "/rounds", body)
    assert status == 200 and response["round"] == 2
    assert board.sql("SELECT number FROM rounds ORDER BY number") == [(1,), (2,)]
    assert board.sql("PRAGMA integrity_check") == [("ok",)]


def test_default_port_prints_only_loopback_address(tmp_path):
    """SOP-01-P04：省略监听参数时实际使用 8787，且不打印内网地址。"""
    data, log = tmp_path / "data", tmp_path / "startup.log"
    with socket.socket() as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", 8787))
            port_free = True
        except OSError:
            port_free = False
    if not port_free:
        # 本机已有服务占用 8787 时，缺省启动因 8787 被占用而失败，同样证明缺省端口为 8787。
        result = subprocess.run([str(ROOT / ".venv/bin/sop-board"), "--data", str(data)], env=TEST_ENV, cwd=ROOT,
                                capture_output=True, text=True, timeout=10)
        assert result.returncode == 1
        assert "8787" in result.stderr
        assert "内网地址" not in result.stdout + result.stderr
        assert not data.exists()
        return
    with log.open("wb") as output:
        process = subprocess.Popen([str(ROOT / ".venv/bin/sop-board"), "--data", str(data)],
                                   env=TEST_ENV, cwd=ROOT, stdout=output, stderr=output)
        try:
            until = time.monotonic() + 5
            while not log.read_text() and process.poll() is None and time.monotonic() < until:
                time.sleep(0.02)
            assert log.read_text().splitlines() == ["看板地址：http://127.0.0.1:8787/"]
            with OPENER.open("http://127.0.0.1:8787/", timeout=3) as response:
                assert response.status == 200
        finally:
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


def test_restart_preserves_evidence_and_manual_round(board):
    """SOP-01-P05：同目录重启后证据摘要与人工轮次逐项保持。"""
    pushed, _ = push(board)
    evidence = upload(board, b"persistent evidence", "before.txt")
    assert board.request("POST", BASE + "/rounds", round_body(pushed, evidence))[0] == 200
    assert board.request("POST", BASE + "/marks", mark_body(pushed))[1]["round"] == 2
    original = get(board, BASE + "/rounds/2")
    path = "/api/v1/evidence/" + evidence["sha256"]
    first = get(board, path)
    assert hashlib.sha256(first).hexdigest() == evidence["sha256"]
    board.stop()
    board.start()
    assert hashlib.sha256(get(board, path)).hexdigest() == hashlib.sha256(first).hexdigest()
    restored = get(board, BASE + "/rounds/2")
    assert restored["kind"] == "manual"
    assert restored == original


@pytest.mark.parametrize("kind", ["empty-token", "readonly-parent", "plain-file", "occupied-empty"])
def test_startup_failures_leave_paths_unchanged(tmp_path, kind):
    """SOP-01-N02：四种启动失败均报原因，不打印地址或改动指定路径。"""
    env, port = dict(TEST_ENV), free_port()
    data, parent = tmp_path / "data", tmp_path / "ro-parent"
    occupied = socket.socket()
    if kind == "empty-token":
        env["SOP_BOARD_TOKEN"] = ""
        reason = "SOP_BOARD_TOKEN"
    elif kind == "readonly-parent":
        parent.mkdir(mode=0o500)
        data = parent / "data"
        reason = str(data)
    elif kind == "plain-file":
        data.write_bytes(b"keep plain file unchanged")
        reason = str(data)
    else:
        data.mkdir()
        occupied.bind(("127.0.0.1", port))
        occupied.listen()
        reason = str(port)
    try:
        argv = [str(ROOT / ".venv/bin/sop-board"), "--data", str(data), "--port", str(port)]
        process = subprocess.Popen(argv, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            stdout, stderr = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
            pytest.fail("启动失败的进程未退出")
        result = subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)
        assert result.returncode == 1
        assert "看板地址" not in result.stdout
        assert reason in result.stderr
        if kind in ("empty-token", "readonly-parent"):
            assert not data.exists()
        elif kind == "plain-file":
            assert "不是目录" in result.stderr
            assert data.read_bytes() == b"keep plain file unchanged"
            assert process.poll() == 1
            import os
            with pytest.raises(ProcessLookupError):
                os.kill(process.pid, 0)
            with socket.socket() as listener:
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                listener.bind(("127.0.0.1", port))
                listener.listen()
        else:
            assert data.is_dir() and list(data.iterdir()) == []
    finally:
        occupied.close()
        if parent.exists():
            parent.chmod(0o700)
