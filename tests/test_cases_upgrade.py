"""D1：用 SOP-BOARD-1 的真实服务建立目录，再启动当前版本。"""

import hashlib
from io import BytesIO
import sqlite3
import subprocess
from zipfile import ZipFile

import pytest

from cases_helpers import CASES_BASE, cases_body, upload_cases
from conftest import OPENER, ROOT, TEST_ENV, free_port
from helpers import BASE, IDS, push, push_body, round_body, snapshot, upload
from html_helpers import page
from test_client import run_page_client
from test_pages import WEB
from test_reads import get


@pytest.fixture
def legacy_board(board_factory, tmp_path):
    # 固定到 SOP-BOARD-1 的最后一次代码提交；只读取历史，不改变当前分支。
    revision = "b718f2f"
    paths = subprocess.run(["git", "ls-tree", "-r", "--name-only", revision, "src/sop_board"],
                           cwd=ROOT, capture_output=True, text=True, check=True).stdout.splitlines()
    assert paths, "升级测试需要保留 SOP-BOARD-1 的 Git 历史"
    source = tmp_path / "legacy"
    for name in paths:
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(subprocess.run(["git", "show", revision + ":" + name], cwd=ROOT,
                                       capture_output=True, check=True).stdout)
    python = str(ROOT / ".venv/bin/python")
    old_command = [python, "-c", "from sop_board.cli import main; raise SystemExit(main())"]
    board = board_factory(command=old_command, env={"PYTHONPATH": str(source / "src")})
    pushed, _ = push(board)
    second = push_body(commit="c2")
    second["content"] += "# 第二个原文版本\n"
    push(board, second)
    assert board.request("POST", BASE + "/rounds", round_body(second, upload(board)))[0] == 200
    old_mark = dict(scenario=IDS[0], fingerprint=pushed["fingerprints"][IDS[0]], status="fail",
                    marker="旧版测试同事", actual="旧版人工发现问题", environment="本地测试",
                    defect_url="https://github.com/demo-org/demo-app/issues/1")
    assert board.request("POST", BASE + "/marks", old_mark)[0] == 200
    return board


def assert_original_fields(original, current):
    """T1 明确扩展了读取字段；每个旧字段、旧数组顺序和值仍须不变。"""
    if isinstance(original, dict):
        assert isinstance(current, dict)
        for key, value in original.items():
            assert key in current
            assert_original_fields(value, current[key])
    elif isinstance(original, list):
        assert len(original) == len(current)
        for before, after in zip(original, current):
            assert_original_fields(before, after)
    else:
        assert original == current


def switch_to_current(board):
    board.stop()
    board.command = [str(ROOT / ".venv/bin/sop-board")]
    board.env.pop("PYTHONPATH", None)
    board.start()


def evidence_bytes(board, sha):
    with OPENER.open(f"http://127.0.0.1:{board.port}/api/v1/evidence/{sha}", timeout=5) as response:
        return response.read()


def test_old_directory_upgrade_preserves_all_rows_reads_exports_evidence_and_accepts_new_writes(legacy_board):
    """SOP-01-P06：旧数据不变，升级后上传与就地复核可继续。"""
    board = legacy_board
    original = snapshot(board)
    reads = {path: get(board, path) for path in (BASE, BASE + "/scenarios", BASE + "/rounds/1", BASE + "/rounds/2")}
    exports = {}
    for number in (1, 2):
        with ZipFile(BytesIO(get(board, BASE + f"/rounds/{number}/export"))) as archive:
            exports[number] = {name: archive.read(name) for name in archive.namelist()}
    evidence = {sha: evidence_bytes(board, sha) for sha, in board.sql("SELECT sha256 FROM evidence")}
    switch_to_current(board)
    after = snapshot(board)
    # SOP-BOARD-5 D1 增加修复任务、问题与尝试；原有数据、导出和证据仍逐项不变。
    assert set(after[0]) - set(original[0]) == {"case_uploads", "case_versions", "tasks", "batch_entries", "node_numbers", "sop_versions", "check_rounds", "settings", "fix_tasks", "fix_problems", "fix_attempts"}
    assert all(after[0][table] == rows for table, rows in original[0].items())
    assert after[1] == original[1]
    for path, response in reads.items():
        assert_original_fields(response, get(board, path))
    for number, files in exports.items():
        with ZipFile(BytesIO(get(board, BASE + f"/rounds/{number}/export"))) as archive:
            assert {name: archive.read(name) for name in archive.namelist()} == files
    for sha, raw in evidence.items():
        assert hashlib.sha256(evidence_bytes(board, sha)).hexdigest() == sha
        assert evidence_bytes(board, sha) == raw
    link = "https://github.com/demo-org/demo-app/issues/1"
    assert page(board, WEB + "/scenarios").one("a", href=link)
    assert link in page(board, WEB + "/s/" + IDS[0]).text
    assert upload_cases(board, base=BASE)["upload"] == 1
    run_page_client(board, "real-detail-pass", WEB + "/s/" + IDS[0], network=True, context={"sid": IDS[0], "expectedRound": 3})
    assert get(board, BASE + "/rounds/3")["kind"] == "manual"


def test_restart_preserves_uploads_union_and_manual_result(board):
    """SOP-01-P07（T2 部分）：重启保留两次上传及人工结果。"""
    pushed, _ = push(board)
    upload_cases(board, base=BASE)
    body = cases_body()
    body["content"] += "# 第二次上传\n"
    upload_cases(board, body, base=BASE)
    from test_marks import mark_body
    assert board.request("POST", BASE + "/marks", mark_body(pushed))[0] == 200
    original = {path: get(board, path) for path in (BASE + "/cases", BASE + "/scenarios")}
    before = snapshot(board)
    board.stop()
    board.start()
    for path, response in original.items():
        assert get(board, path) == response
    assert snapshot(board) == before
    assert "人工 · 通过" in page(board, WEB + "/scenarios").text


@pytest.mark.parametrize("reason", ["missing-token", "schema-conflict"])
def test_failed_old_directory_upgrade_leaves_every_file_byte_unchanged(legacy_board, reason):
    """SOP-01-N03/D1：启动或补表失败，不新增文件、不部分建表。"""
    board = legacy_board
    board.stop()
    if reason == "schema-conflict":
        with sqlite3.connect(board.data / "board.sqlite3") as db:
            db.execute("CREATE INDEX case_versions ON projects(name)")
        db.close()
    before = {str(p.relative_to(board.data)): p.read_bytes() for p in board.data.rglob("*") if p.is_file()}
    env = dict(TEST_ENV)
    if reason == "missing-token":
        env.pop("SOP_BOARD_TOKEN")
    result = subprocess.run([str(ROOT / ".venv/bin/sop-board"), "--data", str(board.data), "--port", str(free_port())],
                            cwd=ROOT, env=env, capture_output=True, text=True, timeout=5)
    assert result.returncode == 1 and "看板地址" not in result.stdout
    assert "SOP_BOARD_TOKEN" in result.stderr if reason == "missing-token" else "case_versions" in result.stderr
    assert {str(p.relative_to(board.data)): p.read_bytes() for p in board.data.rglob("*") if p.is_file()} == before
