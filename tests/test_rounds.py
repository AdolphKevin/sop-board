from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib

import pytest

from helpers import BASE, IDS, assert_error, demo, push, push_body, round_body, snapshot, upload


def test_agent_round_stores_results_evidence_and_statistics(board):
    """I2, D1, D2; SOP-03-P01: 2 pass, 1 fail, 1 blocked."""
    pushed, _ = push(board)
    evidence = [upload(board, f"evidence-{i}".encode(), f"evidence-{i}.txt") for i in range(4)]
    body = round_body(pushed, evidence[0])
    for row, item in zip(body["results"], evidence):
        row["evidence"] = [item]
    status, response, _ = board.request("POST", BASE + "/rounds", body)
    assert status == 200, response
    assert response["round"] == 1
    assert response["statistics"]["agent_pass_rate"] == dict(numerator=2, denominator=3, rate=2/3)
    assert response["statistics"]["first_pass_rate"] == dict(numerator=2, denominator=3, rate=2/3)
    assert response["queues"] == dict(pending=[], retest=["N1-P01", "N2-P01"], issues=["N1-N01", "N2-N01"])
    assert board.sql("SELECT number,kind,implementer,submitted_by FROM rounds") == [(1, "agent", "Zhang San", "开发岗")]
    assert board.sql("SELECT scenario,status,independence FROM results ORDER BY id") == [
        (IDS[0], "pass", "independent"), (IDS[1], "fail", "independent"),
        (IDS[2], "pass", "independent"), (IDS[3], "blocked", "independent")]
    assert board.sql("SELECT name,sha256 FROM result_evidence ORDER BY result_id,ordinal") == [
        (e["name"], e["sha256"]) for e in evidence]


def test_round_reports_all_result_problems_and_rolls_back(board):
    """I2; SOP-03-N01: all four errors, including missing uploaded evidence."""
    pushed, _ = push(board)
    body = round_body(pushed, upload(board))
    body["results"][0]["evidence"] = []
    body["results"][1]["evidence"][0]["sha256"] = "0" * 64
    body["results"][2]["executed_at"] = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    body["results"][3].pop("finding")
    before = snapshot(board)
    problems = assert_error(board.request("POST", BASE + "/rounds", body), 422, "round-invalid")
    assert {p["field"] for p in problems} == {
        "results[0].evidence", "results[1].evidence[0].sha256", "results[2].executed_at", "results[3].finding"}
    assert "evidence-missing" in str(problems)
    assert snapshot(board) == before


@pytest.mark.parametrize("kind", ["empty", "duplicate", "status", "fail-evidence", "skip-finding", "naive-time",
                                  "bad-time", "future-time", "actual", "executor", "evidence-shape", "key", "product"])
def test_invalid_round_fields_leave_no_data(board, kind):
    """I2: individual validation branches not covered by the four-error example."""
    pushed, _ = push(board)
    body = round_body(pushed, upload(board))
    row = body["results"][0]
    if kind == "empty":
        body["results"] = []
    elif kind == "duplicate":
        body["results"].append(deepcopy(row))
    elif kind == "status":
        row["status"] = "unknown"
    elif kind == "fail-evidence":
        row.update(status="fail", evidence=[])
    elif kind == "skip-finding":
        row.update(status="skip", finding=" ")
    elif kind == "naive-time":
        row["executed_at"] = "2026-01-01T00:00:00"
    elif kind == "bad-time":
        row["executed_at"] = "yesterday"
    elif kind == "future-time":
        row["executed_at"] = (datetime.now(timezone.utc) + timedelta(minutes=6)).isoformat()
    elif kind in ("actual", "executor"):
        row[kind] = " "
    elif kind == "evidence-shape":
        row["evidence"] = ["path.txt"]
    elif kind == "key":
        body["idempotency_key"] = " "
    else:
        body["product"]["dirty"] = "false"
    before = snapshot(board)
    assert_error(board.request("POST", BASE + "/rounds", body), 422, "round-invalid")
    assert snapshot(board) == before


@pytest.mark.parametrize("kind", ["file", "fingerprint", "removed", "unknown"])
def test_round_rejects_stale_file_or_scenario(board, kind):
    """I2; SOP-03-N02: current file and current scenario identity are both required."""
    old, _ = push(board)
    evidence = upload(board)
    doc = demo()
    doc["sops"][0]["scenarios"][0]["expected"] = ["changed"]
    doc["sops"][1]["scenarios"].pop()
    doc["sops"][1]["negative_exempt"] = "不适用"
    latest, _ = push(board, push_body(doc))
    body = round_body(old, evidence)
    body["results"] = body["results"][:1]
    if kind != "file":
        body["scenario_file_sha256"] = hashlib.sha256(latest["content"].encode()).hexdigest()
    if kind in ("removed", "unknown"):
        body["results"][0]["scenario"] = "N2-N01" if kind == "removed" else "UNKNOWN"
    before = snapshot(board)
    problems = assert_error(board.request("POST", BASE + "/rounds", body), 409,
                            "scenario-file-stale" if kind == "file" else "scenario-stale")
    if kind != "file":
        assert body["results"][0]["scenario"] in str(problems)
    assert snapshot(board) == before


def test_idempotency_replay_conflict_and_replay_after_new_version(board):
    """I2; SOP-03-N03: canonical JSON equality and replay before stale checks."""
    pushed, _ = push(board)
    body = round_body(pushed, upload(board))
    first = board.request("POST", BASE + "/rounds", body)
    assert first[0] == 200, first[1]
    before = snapshot(board)
    reordered = dict(reversed(list(body.items())))
    assert board.request("POST", BASE + "/rounds", reordered)[1]["round"] == 1
    assert snapshot(board) == before
    conflict = deepcopy(body)
    conflict["results"][0]["actual"] = "changed"
    assert_error(board.request("POST", BASE + "/rounds", conflict), 409, "idempotency-conflict")
    assert snapshot(board) == before
    push(board, push_body(content=pushed["content"] + "\n# new file version\n"))
    before = snapshot(board)
    assert board.request("POST", BASE + "/rounds", body)[1]["round"] == 1
    assert snapshot(board) == before


@pytest.mark.parametrize("implementer,executor,expected", [("  ZHANG\t San ", "zhang  san", "self-check"),
                                                        ("Straße", "STRASSE", "self-check"),
                                                        ("A", "B", "independent"), (None, "B", "undeclared")])
def test_independence_normalizes_names(board, implementer, executor, expected):
    """I2: Unicode case folding and whitespace normalization."""
    pushed, _ = push(board)
    body = round_body(pushed, upload(board))
    body["results"] = body["results"][:1]
    body["results"][0]["executor"] = executor
    if implementer is None:
        body.pop("implementer")
    else:
        body["implementer"] = implementer
    assert board.request("POST", BASE + "/rounds", body)[0] == 200
    assert board.sql("SELECT independence FROM results") == [(expected,)]


def test_batches_scope_versions_round_numbers_and_idempotency_keys(board):
    """D1, I3: independent batches can both have version/round 1 and the same key."""
    from test_reads import get
    first, _ = push(board)
    evidence = upload(board)
    assert board.request("POST", BASE + "/rounds", round_body(first, evidence))[0] == 200
    second = push_body(content=first["content"].replace("batch: demo", "batch: other"))
    other_base = BASE.removesuffix("demo") + "other"
    assert board.request("PUT", other_base + "/scenario-file", second)[1]["version"] == 1
    assert board.request("POST", other_base + "/rounds", round_body(second, evidence))[1]["round"] == 1
    assert get(board, other_base + "/rounds/1")["version"] == 1
    assert get(board, other_base)["rounds"][0]["version"] == 1
    assert get(board, BASE)["rounds"][0]["version"] == 1
    assert board.sql("SELECT COUNT(*) FROM rounds") == [(2,)]
