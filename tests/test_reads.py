import pytest

from helpers import BASE, IDS, KEY, assert_error, push, round_body, upload


def get(board, path):
    status, body, _ = board.request("GET", path, auth=False)
    assert status == 200, body
    return body


def test_empty_batch_ratios_and_scenario_read_contract(board):
    """I3, D2; SOP-02-P01: zero denominators and all pending scenarios."""
    assert get(board, "/api/v1/projects") == dict(ok=True, projects=[])
    pushed, _ = push(board)
    summary = get(board, BASE)
    assert summary["version"] == 1 and summary["scenario_count"] == 4
    assert summary["queues"] == dict(pending=IDS, retest=[], issues=[])
    assert summary["queue_counts"] == dict(pending=4, retest=0, issues=0)
    for key in ("agent_pass_rate", "manual_pass_rate", "retest_coverage", "first_pass_rate", "reviewed_progress"):
        assert summary["statistics"][key] == dict(numerator=0, denominator=0, rate=None)
    assert summary["progress"] == dict(executed=dict(numerator=0, denominator=4, rate=0),
                                      reviewed=dict(numerator=0, denominator=0, rate=None))
    assert summary["rounds"] == [] and summary["misjudgments"] == []
    scenarios = get(board, BASE + "/scenarios")["scenarios"]
    assert [s["scenario"] for s in scenarios] == IDS
    for s in scenarios:
        assert s["fingerprint"] == pushed["fingerprints"][s["scenario"]]
        assert s["queue"] == "pending" and s["latest_agent"] is None and s["latest_manual"] is None
        assert s["verify"] == "自定义入口" and s["regression"] is None
        assert {"node", "title", "polarity", "priority"} <= s.keys()
    project = get(board, "/api/v1/projects")["projects"][0]
    assert (project["key"], project["id"], project["name"]) == (KEY, "github.com/demo-org/demo-app", "演示项目")
    assert project["batches"][0]["statistics"] == summary["statistics"]


def test_read_round_and_restart_preserve_all_data(board):
    """I3, D1; SOP-01-P02, SOP-03-P01: submit, inspect, restart, compare."""
    pushed, _ = push(board)
    evidence = upload(board)
    body = round_body(pushed, evidence)
    assert board.request("POST", BASE + "/rounds", body)[0] == 200
    summary = get(board, BASE)
    assert summary["rounds"][0]["pass_rate"] == dict(numerator=2, denominator=4, rate=0.5)
    result = get(board, BASE + "/rounds/1")
    assert result["round"] == 1 and result["kind"] == "agent" and result["version"] == 1
    assert result["product"] == body["product"]
    assert result["implementer"] == "Zhang San" and result["submitted_by"] == "开发岗"
    for row, original in zip(result["results"], body["results"]):
        assert row.items() >= original.items()
        assert row["independence"] == "independent"
    board.stop()
    board.start()
    assert get(board, BASE) == summary
    assert get(board, BASE + "/rounds/1") == result
    assert board.sql("SELECT COUNT(*) FROM versions") == [(1,)]
    assert board.sql("SELECT COUNT(*) FROM rounds") == [(1,)]
    assert board.request("GET", "/api/v1/evidence/" + evidence["sha256"], auth=False)[1] == b"test evidence"


@pytest.mark.parametrize("path,code", [("/api/v1/projects/missing/batches/demo", "project-not-found"),
    (f"/api/v1/projects/{KEY}/batches/missing", "batch-not-found"),
    (BASE + "/rounds/99", "round-not-found"), ("/api/v1/evidence/" + "0" * 64, "evidence-not-found")])
def test_read_not_found_is_structured(board, path, code):
    """I3: all four specified resource-not-found codes without authentication."""
    push(board)
    assert_error(board.request("GET", path, auth=False), 404, code)
