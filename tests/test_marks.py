import json
import sqlite3

import pytest

from helpers import BASE, IDS, KEY, assert_error, push, push_body, round_body, snapshot, upload
from test_reads import get


def mark_body(pushed, status="pass", **overrides):
    body = dict(scenario=IDS[0], fingerprint=pushed["fingerprints"][IDS[0]], status=status,
                marker="Tester A")
    body.update(overrides)
    return body


def test_manual_mark_creates_observation_and_keeps_agent_result(board):
    """I2, D1, D2: manual round shares numbering; observation is actual evidence."""
    pushed, _ = push(board)
    body = round_body(pushed, upload(board))
    assert board.request("POST", BASE + "/rounds", body)[0] == 200
    status, response, _ = board.request("POST", BASE + "/marks", mark_body(pushed))
    assert status == 200 and response["round"] == 2, response
    assert response["scenario"]["scenario"] == IDS[0]
    assert response["scenario"]["status"] == "pass"
    assert response["scenario"]["queue"] == "reviewed"
    assert response["scenario"]["latest_agent"]["status"] == "pass"
    assert response["scenario"]["latest_manual"]["executor"] == "Tester A"
    assert response["filters"] == {"all": 4, "unreviewed": 3, "agent-pass": 2, "fail": 1, "blocked": 1}
    manual = get(board, BASE + "/rounds/2")
    assert manual["kind"] == "manual"
    row = manual["results"][0]
    assert row["executor"] == "Tester A" and row["independence"] == "undeclared"
    assert row["evidence"][0]["name"] == "observation-2.json"
    sha = row["evidence"][0]["sha256"]
    observation = json.loads((board.data / "evidence" / sha[:2] / sha).read_bytes())
    assert row["actual"] == "人工复核通过"
    assert observation == dict(marker="Tester A", status="pass", recorded_at=row["executed_at"])
    assert board.sql("SELECT kind,submitted_by FROM rounds WHERE number=2") == [("manual", "Tester A")]
    assert board.sql("SELECT status,actual,executor,independence FROM results WHERE round_id=(SELECT id FROM rounds WHERE number=2)") == [
        ("pass", "人工复核通过", "Tester A", "undeclared")]
    assert board.sql("SELECT COUNT(*) FROM results") == [(5,)]
    assert get(board, BASE + "/rounds/1")["results"][0]["status"] == "pass"


@pytest.mark.parametrize("changes,field", [
    ({"status": "fail"}, "defect_url"), ({"status": "blocked"}, "defect_url"),
    ({"status": "fail", "defect_url": " "}, "defect_url"),
    ({"status": "blocked", "defect_url": 4}, "defect_url"),
    ({"defect_url": "https://github.com/demo-org/demo-app/issues/1"}, "defect_url"),
    ({"defect_url": ""}, "defect_url"), ({"marker": " "}, "marker"),
    ({"status": "unknown"}, "status"), ({"status": "skip"}, "status"),
    ({"note": None}, "note"), ({"note": []}, "note"), ({"note": 1}, "note"),
    ({"marker": ""}, "marker"), ({"status": "fail", "defect_url": ""}, "defect_url"),
    ({"status": "blocked", "defect_url": "   "}, "defect_url"),
    ({"status": "fail", "defect_url": None}, "defect_url"), ({"other": 1}, "other"),
])
def test_invalid_marks_do_not_create_rounds_or_observation(board, changes, field):
    """SOP-22-N02, I2: invalid input is rejected without rows or files."""
    pushed, _ = push(board)
    before = snapshot(board)
    problems = assert_error(board.request("POST", BASE + "/marks", mark_body(pushed, **changes)), 422, "mark-invalid")
    assert field in [p["field"] for p in problems]
    assert snapshot(board) == before


def test_legacy_mark_fields_are_each_named_and_rejected(board):
    """SOP-22-N01: all four retired fields must be identified together."""
    pushed, _ = push(board)
    body = mark_body(pushed, actual="旧结果", environment="旧环境", finding="旧问题", evidence=[])
    before = snapshot(board)
    problems = assert_error(board.request("POST", BASE + "/marks", body), 422, "mark-invalid")
    assert {p["field"] for p in problems} == {"actual", "environment", "finding", "evidence"}
    assert snapshot(board) == before


@pytest.mark.parametrize("field,value", [("fingerprint", "0" * 64), ("scenario", "unknown")])
def test_stale_marks_name_scenario_or_fingerprint_without_writes(board, field, value):
    """SOP-22-N03, I2: stale fields use the mark contract, not results[0]."""
    pushed, _ = push(board)
    before = snapshot(board)
    problems = assert_error(board.request("POST", BASE + "/marks", mark_body(pushed, **{field: value})),
                            409, "scenario-stale")
    assert [p["field"] for p in problems] == [field]
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
                 422, "defect-link-invalid")
    assert snapshot(board) == before
    problems = assert_error(board.request("POST", other_base + "/marks", mark_body(other, "blocked", defect_url="ABC-1")),
                            422, "defect-link-invalid")
    assert "需" in problems[0]["problem"] and "缺陷链接前缀或配置 JIRA_URL" in problems[0]["problem"]
    assert snapshot(board) == before
    assert board.request("PUT", "/api/v1/projects/internal-demo", dict(defect_prefix="https://bugs.example/"))[0] == 200
    assert board.request("POST", other_base + "/marks", mark_body(other, "fail", defect_url="https://bugs.example/2"))[0] == 200


@pytest.mark.parametrize("status,label", [("fail", "失败"), ("blocked", "受阻")])
def test_manual_nonpass_generates_exact_result_finding_and_observation(board, status, label):
    """SOP-22-P01: every nonpass gets a generated observation, never caller evidence."""
    from datetime import datetime, timezone
    from io import BytesIO
    from zipfile import ZipFile

    pushed, _ = push(board)
    agent_body = round_body(pushed, upload(board))
    assert board.request("POST", BASE + "/rounds", agent_body)[0] == 200
    agent_before = get(board, BASE + "/rounds/1")
    link = "https://github.com/demo-org/demo-app/issues/9"
    before_time = datetime.now(timezone.utc)
    code, response, _ = board.request("POST", BASE + "/marks", mark_body(pushed, status, defect_url=link, note="等数据"))
    assert code == 200, response
    manual = get(board, BASE + "/rounds/2")
    assert manual["kind"] == "manual" and len(manual["results"]) == 1
    row = manual["results"][0]
    assert row["actual"] == f"人工复核不通过（{label}），缺陷 {link}"
    assert (row["finding"], row["note"], row["executor"], row["independence"]) == (link, "等数据", "Tester A", "undeclared")
    assert before_time <= datetime.fromisoformat(row["executed_at"]) <= datetime.now(timezone.utc)
    assert row["evidence"][0]["name"] == "observation-2.json"
    sha = row["evidence"][0]["sha256"]
    raw = (board.data / "evidence" / sha[:2] / sha).read_bytes()
    assert json.loads(raw) == dict(marker="Tester A", status=status, defect_url=link, note="等数据", recorded_at=row["executed_at"])
    assert board.sql("SELECT status,actual,finding,note FROM results WHERE round_id=(SELECT id FROM rounds WHERE number=2)") == [
        (status, f"人工复核不通过（{label}），缺陷 {link}", link, "等数据")]
    assert response["scenario"]["latest_manual"]["finding"] == link
    assert response["scenario"]["queue"] == "issues"
    assert response["filters"] == {"all": 4, "unreviewed": 3, "agent-pass": 2,
                                    "fail": 2 if status == "fail" else 1, "blocked": 2 if status == "blocked" else 1}
    assert get(board, BASE + "/rounds/1") == agent_before
    with ZipFile(BytesIO(get(board, BASE + "/rounds/2/export"))) as archive:
        exported = json.loads(archive.read("results.jsonl"))
        assert exported == {**{k: row[k] for k in ("scenario", "status", "actual", "finding", "note", "executor", "executed_at")},
                            "evidence": ["evidence/1-observation-2.json"]}
        assert archive.read(exported["evidence"][0]) == raw


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


def test_mark_response_provides_server_local_time_for_in_place_rendering(board_factory):
    """I1, SOP-06-P03: a browser in another zone must display the server's time."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    board = board_factory(env={"TZ": "Asia/Kathmandu"})
    pushed, _ = push(board)
    code, response, _ = board.request("POST", BASE + "/marks", mark_body(pushed))
    assert code == 200, response
    row = response["scenario"]["latest_manual"]
    expected = datetime.fromisoformat(row["executed_at"]).astimezone(ZoneInfo("Asia/Kathmandu")).strftime("%Y-%m-%d %H:%M:%S") + " +05:45"
    assert row["executed_at_display"] == expected
