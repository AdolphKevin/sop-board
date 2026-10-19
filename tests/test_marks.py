import json
import sqlite3

import pytest

from helpers import BASE, IDS, KEY, assert_error, push, push_body, round_body, snapshot, upload
from test_reads import get


def mark_body(pushed, status="pass", **overrides):
    body = dict(scenario=IDS[0], fingerprint=pushed["fingerprints"][IDS[0]], status=status,
                actual="人工核对结果", marker="Tester A", environment="本机隔离环境")
    body.update(overrides)
    return body


def test_manual_mark_creates_observation_and_keeps_agent_result(board):
    """I2, D1, D2: manual round shares numbering; observation is actual evidence."""
    pushed, _ = push(board)
    body = round_body(pushed, upload(board))
    assert board.request("POST", BASE + "/rounds", body)[0] == 200
    status, response, _ = board.request("POST", BASE + "/marks", mark_body(pushed))
    assert status == 200 and response["round"] == 2, response
    assert response["queues"] == dict(pending=[], retest=["N2-P01"], issues=["N1-N01", "N2-N01"])
    manual = get(board, BASE + "/rounds/2")
    assert manual["kind"] == "manual"
    row = manual["results"][0]
    assert row["executor"] == "Tester A" and row["independence"] == "undeclared"
    assert row["evidence"][0]["name"] == "observation-2.json"
    sha = row["evidence"][0]["sha256"]
    observation = json.loads((board.data / "evidence" / sha[:2] / sha).read_bytes())
    assert observation == dict(marker="Tester A", environment="本机隔离环境", status="pass",
                               actual="人工核对结果", executed_at=row["executed_at"])
    assert board.sql("SELECT COUNT(*) FROM results") == [(5,)]
    assert get(board, BASE + "/rounds/1")["results"][0]["status"] == "pass"


@pytest.mark.parametrize("kind", ["missing-link", "foreign-link", "marker", "actual", "status", "blocked",
                                  "skip", "evidence", "stale", "unknown"])
def test_invalid_marks_do_not_create_rounds_or_observation(board, kind):
    """I2: defect-link-required, scenario-stale, all manual validation branches."""
    pushed, _ = push(board)
    body = mark_body(pushed)
    code = "mark-invalid"
    if kind in ("missing-link", "foreign-link"):
        body["status"] = "fail"
        if kind == "foreign-link":
            body["defect_url"] = "https://example.com/bug/1"
        code = "defect-link-required"
    elif kind in ("marker", "actual"):
        body[kind] = " "
    elif kind == "status":
        body["status"] = "unknown"
    elif kind in ("blocked", "skip"):
        body["status"] = kind
    elif kind == "evidence":
        body["evidence"] = [dict(name="missing.txt", sha256="0" * 64)]
    elif kind == "stale":
        body["fingerprint"] = "0" * 64
        code = "scenario-stale"
    else:
        body["scenario"] = "unknown"
        code = "scenario-stale"
    before = snapshot(board)
    assert_error(board.request("POST", BASE + "/marks", body), 409 if code == "scenario-stale" else 422, code)
    assert snapshot(board) == before


def test_project_defect_prefix_default_custom_and_unset(board):
    """I2: project-mapped prefix and settings validation."""
    pushed, _ = push(board)
    path = f"/api/v1/projects/{KEY}"
    before = snapshot(board)
    assert_error(board.request("PUT", path, dict(defect_prefix="http://bugs.example/")), 422, "project-invalid")
    assert snapshot(board) == before
    assert board.request("PUT", path, dict(defect_prefix="https://bugs.example/"))[0] == 200
    assert board.sql("SELECT defect_prefix FROM projects") == [("https://bugs.example/",)]
    body = mark_body(pushed, "fail", defect_url="https://bugs.example/1")
    assert board.request("POST", BASE + "/marks", body)[0] == 200
    assert get(board, BASE + "/rounds/1")["results"][0]["finding"] == "https://bugs.example/1"
    # Re-pushing may update the name, but must preserve user settings.
    push(board, pushed)
    assert board.sql("SELECT defect_prefix FROM projects") == [("https://bugs.example/",)]
    other = push_body(project=dict(id="Internal / Demo_应用", name="内部项目"))
    other_base = "/api/v1/projects/internal-demo/batches/demo"
    assert board.request("PUT", other_base + "/scenario-file", other)[0] == 200
    before = snapshot(board)
    assert_error(board.request("POST", other_base + "/marks", mark_body(other, "fail", defect_url="https://bugs.example/1")),
                 422, "defect-prefix-unset")
    assert snapshot(board) == before
    assert board.request("PUT", "/api/v1/projects/internal-demo", dict(defect_prefix="https://bugs.example/"))[0] == 200
    assert board.request("POST", other_base + "/marks", mark_body(other, "fail", defect_url="https://bugs.example/2"))[0] == 200


def test_manual_supplied_evidence_and_blocked_skip(board):
    """I2: no generated observation when evidence is supplied; finding required."""
    pushed, _ = push(board)
    evidence = upload(board)
    for number, status in enumerate(("pass", "fail", "blocked", "skip"), 1):
        body = mark_body(pushed, status, evidence=[evidence], finding="Q1",
                         defect_url="https://github.com/demo-org/demo-app/issues/1")
        assert board.request("POST", BASE + "/marks", body)[0] == 200
        result = get(board, BASE + f"/rounds/{number}")["results"][0]
        assert result["evidence"] == [evidence]
        if status == "fail":
            assert result["finding"] == body["defect_url"]
        else:
            assert result["finding"] == "Q1"
    assert board.sql("SELECT COUNT(*) FROM evidence") == [(1,)]


@pytest.mark.parametrize("operation", ["mark", "upload"])
def test_storage_failure_rolls_back_rows_and_new_evidence_files(board, operation):
    """I2, D1: database fault after file creation rolls back the entire request."""
    pushed, _ = push(board)
    table = "results" if operation == "mark" else "evidence"
    with sqlite3.connect(board.data / "board.sqlite3") as db:
        db.execute(f"CREATE TRIGGER reject_test_insert BEFORE INSERT ON {table} BEGIN SELECT RAISE(ABORT, 'injected storage failure'); END")
    before = snapshot(board)
    if operation == "mark":
        response = board.request("POST", BASE + "/marks", mark_body(pushed))
    else:
        response = board.request("POST", "/api/v1/evidence", b"new evidence")
    assert_error(response, 500, "storage-error")
    assert snapshot(board) == before
    with sqlite3.connect(board.data / "board.sqlite3") as db:
        db.execute("DROP TRIGGER reject_test_insert")
    assert board.request("POST", BASE + "/marks", mark_body(pushed))[1]["round"] == 1
