import hashlib
import signal
import socket

import pytest

from shutdown_helpers import repeated_signals_during_write


@pytest.mark.parametrize("first,second", [
    (signal.SIGTERM, signal.SIGTERM), (signal.SIGTERM, signal.SIGINT),
    (signal.SIGINT, signal.SIGTERM), (signal.SIGINT, signal.SIGINT),
], ids=["term-term", "term-int", "int-term", "int-int"])
def test_repeated_signals_finish_pending_write_and_exit_cleanly(board, first, second):
    """W4、I1：关闭期间重复信号不打断待提交请求，退出码 0 且无回溯。"""
    result = repeated_signals_during_write(board, first, second)
    assert result["code"] == 0, result["log"]
    assert "Traceback" not in result["log"] and "Stopped" not in result["log"]
    assert result["status"] == 200 and result["body"]["ok"] is True
    assert board.sql("PRAGMA integrity_check") == [("ok",)]
    assert board.sql("SELECT name FROM evidence ORDER BY name") == [("before.txt",), ("shutdown.txt",)]
    with socket.socket() as probe:
        assert probe.connect_ex(("127.0.0.1", board.port)) != 0
    board.start()
    sha = hashlib.sha256(result["pending"]).hexdigest()
    assert board.request("GET", "/api/v1/evidence/" + sha, auth=False)[1] == result["pending"]
