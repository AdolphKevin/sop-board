import signal
import socket
import subprocess

import pytest

from conftest import OPENER, ROOT, TEST_ENV, free_port


def test_startup_home_wal_and_shutdown(board):
    """I1, D1; SOP-01-P01: real CLI, home readiness, SQLite WAL, SIGTERM."""
    status, body, _ = board.request("GET", "/", auth=False)
    assert status == 200 and b"sop-board" in body
    assert board.sql("PRAGMA journal_mode") == [("wal",)]
    assert f"看板地址：http://127.0.0.1:{board.port}/" in board.log.read_text()
    board.stop()
    assert board.process.returncode == 0
    with socket.socket() as sock:
        assert sock.connect_ex(("127.0.0.1", board.port)) != 0


@pytest.mark.parametrize("kind", ["token", "port", "directory", "unwritable"])
def test_startup_failure_exits_one_with_reason(tmp_path, kind):
    """I1; SOP-01-N01."""
    env = dict(TEST_ENV)
    data, port = tmp_path / "data", free_port()
    occupied = socket.socket()
    reason = ""
    if kind == "token":
        env.pop("SOP_BOARD_TOKEN")
        reason = "SOP_BOARD_TOKEN"
    elif kind == "port":
        occupied.bind(("127.0.0.1", port))
        occupied.listen()
        reason = str(port)
    elif kind == "directory":
        data.write_text("a file, not a directory")
        reason = str(data)
    else:
        data.mkdir(mode=0o500)
        reason = str(data)
    try:
        result = subprocess.run([str(ROOT / ".venv/bin/sop-board"), "--data",
                                 str(data), "--port", str(port)], env=env,
                                capture_output=True, text=True, timeout=5)
    finally:
        occupied.close()
        if kind == "unwritable":
            data.chmod(0o700)
    assert "Traceback" not in result.stderr
    assert result.returncode == 1
    assert reason in result.stderr


def test_explicit_network_binding_and_interrupt(board_factory):
    """I1; SOP-01-P01: bind address comes from the real listening socket."""
    board = board_factory("0.0.0.0")
    assert "内网" in board.log.read_text()
    board.process.send_signal(signal.SIGINT)
    board.process.wait(timeout=5)
    assert board.process.returncode == 0


def test_default_binding_refuses_other_local_interface(board_factory):
    """I1; SOP-01-P01: the real LAN interface is denied/allowed by host."""
    # UDP connect selects the local route without sending a packet.
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as route:
        route.connect(("192.0.2.1", 80))
        address = route.getsockname()[0]
    assert not address.startswith("127."), "此用例需要本机具备内网接口"
    board = board_factory()
    with pytest.raises(OSError):
        OPENER.open(f"http://{address}:{board.port}/", timeout=1)
    shared = board_factory("0.0.0.0")
    with OPENER.open(f"http://{address}:{shared.port}/", timeout=2) as response:
        assert response.status == 200
