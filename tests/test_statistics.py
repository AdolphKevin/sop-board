import pytest

from helpers import BASE, IDS, demo, push, push_body, round_body, upload
from test_marks import mark_body
from test_reads import get


def assert_ratio(actual, numerator, denominator):
    assert actual == dict(numerator=numerator, denominator=denominator,
                          rate=numerator/denominator if denominator else None)


def submit(board, pushed, evidence, key, statuses=("pass", "fail", "pass", "blocked"), selected=None):
    body = round_body(pushed, evidence, statuses, key)
    if selected is not None:
        body["results"] = [r for r in body["results"] if r["scenario"] in selected]
    status, response, _ = board.request("POST", BASE + "/rounds", body)
    assert status == 200, response
    return response


def mark(board, pushed, scenario, status):
    body = mark_body(pushed, status, scenario=scenario, fingerprint=pushed["fingerprints"][scenario],
                     finding="Q1", defect_url="https://github.com/demo-org/demo-app/issues/1")
    code, response, _ = board.request("POST", BASE + "/marks", body)
    assert code == 200, response
    return response


def test_latest_results_include_evidence_for_future_pages(board):
    """I3: latest results retain the evidence references available on the round."""
    pushed, _ = push(board)
    evidence = upload(board)
    submit(board, pushed, evidence, "first")
    mark(board, pushed, IDS[0], "pass")
    scenario = get(board, BASE + "/scenarios")["scenarios"][0]
    assert scenario["latest_agent"]["evidence"] == [evidence]
    assert scenario["latest_manual"]["evidence"][0]["name"] == "observation-2.json"


def test_manual_rates_progress_misjudgments_and_submission_order(board):
    """D2: every metric, each manual status, repeated misjudgments, recent A."""
    pushed, _ = push(board)
    evidence = upload(board)
    submit(board, pushed, evidence, "first")
    mark(board, pushed, IDS[0], "pass")
    mark(board, pushed, IDS[2], "fail")
    summary = get(board, BASE)
    s = summary["statistics"]
    for key, n, d in [("agent_pass_rate", 2, 3), ("manual_pass_rate", 1, 2),
                      ("retest_coverage", 2, 2), ("first_pass_rate", 2, 3),
                      ("executed_progress", 4, 4), ("reviewed_progress", 2, 2)]:
        assert_ratio(s[key], n, d)
    assert summary["queue_counts"] == dict(pending=0, retest=0, issues=3)
    assert summary["misjudgments"] == [dict(scenario=IDS[2], round=3,
        defect_url="https://github.com/demo-org/demo-app/issues/1", agent_executor="Li Si", marker="Tester A")]
    mark(board, pushed, IDS[2], "fail")
    assert len(get(board, BASE)["misjudgments"]) == 2
    mark(board, pushed, IDS[0], "skip")
    mark(board, pushed, IDS[2], "blocked")
    summary = get(board, BASE)
    assert_ratio(summary["statistics"]["manual_pass_rate"], 0, 0)
    assert_ratio(summary["statistics"]["retest_coverage"], 2, 2)
    assert summary["queue_counts"] == dict(pending=0, retest=0, issues=4)
    # Later submission wins even though its execution timestamp is older.
    body = round_body(pushed, evidence, key="second")
    body["results"] = body["results"][:1]
    body["results"][0]["executed_at"] = "2000-01-01T00:00:00Z"
    assert board.request("POST", BASE + "/rounds", body)[0] == 200
    summary = get(board, BASE)
    assert summary["queues"]["retest"] == [IDS[0]]
    assert_ratio(summary["statistics"]["retest_coverage"], 1, 2)
    assert_ratio(summary["statistics"]["manual_pass_rate"], 0, 0)
    assert len(summary["misjudgments"]) == 2
    for item in summary["rounds"]:
        assert_ratio(item["pass_rate"], 2 if item["number"] == 1 else
                     1 if item["number"] in (2, 7) else 0, 4 if item["number"] == 1 else 1)


@pytest.mark.parametrize("regression,expected", [(None, "pending"),
    ({"pending": "Q1"}, "pending"), ({"manual": "手动", "owner": "A", "trigger": "变化"}, "pending"),
    ({"check": "unit", "cases": ["test_example"]}, "retest")])
def test_product_change_pending_requires_regression_check(board, regression, expected):
    """D2: stale product pass with/without check; manual latest overrides staleness."""
    doc = demo()
    if regression is not None:
        doc["sops"][0]["scenarios"][0]["regression"] = regression
    pushed, _ = push(board, push_body(doc))
    submit(board, pushed, upload(board), "first")
    pushed["product_digest"] = "product-2"
    push(board, pushed)
    items = {r["scenario"]: r for r in get(board, BASE + "/scenarios")["scenarios"]}
    assert items[IDS[0]]["queue"] == expected
    assert items[IDS[1]]["queue"] == "issues"  # old product failures stay issues
    assert items[IDS[2]]["queue"] == "pending"
    assert items[IDS[3]]["queue"] == "issues"
    mark(board, pushed, IDS[0], "pass")
    assert get(board, BASE + "/scenarios")["scenarios"][0]["queue"] == "reviewed"


def test_changed_and_removed_scenarios_exclude_old_results_keep_history(board):
    """D1, D2: history retained; current fingerprints alone contribute to statistics."""
    pushed, _ = push(board)
    submit(board, pushed, upload(board), "first")
    mark(board, pushed, IDS[0], "pass")
    mark(board, pushed, IDS[2], "fail")
    doc = demo()
    doc["sops"][0]["scenarios"][0]["expected"] = ["changed content"]
    changed, _ = push(board, push_body(doc))
    summary = get(board, BASE)
    assert summary["queues"]["pending"] == [IDS[0]]
    assert_ratio(summary["statistics"]["agent_pass_rate"], 1, 2)
    assert_ratio(summary["statistics"]["manual_pass_rate"], 0, 1)
    assert_ratio(summary["statistics"]["executed_progress"], 3, 4)
    doc["sops"][1]["scenarios"].pop()
    doc["sops"][1]["negative_exempt"] = "不适用"
    push(board, push_body(doc))
    summary = get(board, BASE)
    assert summary["scenario_count"] == 3 and IDS[3] not in str(summary["queues"])
    assert_ratio(summary["statistics"]["executed_progress"], 2, 3)
    assert len(get(board, BASE + "/rounds/1")["results"]) == 4
    assert board.sql("SELECT COUNT(*) FROM results") == [(6,)]
    # Restoring the identical content fingerprint restores the matching history.
    push(board, pushed)
    summary = get(board, BASE)
    assert summary["queues"]["pending"] == []
    assert_ratio(summary["statistics"]["manual_pass_rate"], 1, 2)


def test_first_result_blocked_is_not_replaced_and_empty_batch_has_null_rates(board):
    """D2: first means first submission, including blocked/skip; all zero denominators."""
    pushed, _ = push(board)
    evidence = upload(board)
    first = submit(board, pushed, evidence, "blocked-first", ("blocked", "skip", "blocked", "skip"))
    assert first["queues"] == dict(pending=[], retest=[], issues=IDS)
    submit(board, pushed, evidence, "pass-second", ("pass", "pass", "pass", "pass"))
    summary = get(board, BASE)
    assert_ratio(summary["statistics"]["agent_pass_rate"], 4, 4)
    assert_ratio(summary["statistics"]["first_pass_rate"], 0, 0)
    doc = demo()
    doc["sops"] = []
    push(board, push_body(doc))
    summary = get(board, BASE)
    assert summary["scenario_count"] == 0 and summary["queues"] == dict(pending=[], retest=[], issues=[])
    assert all(value == dict(numerator=0, denominator=0, rate=None) for value in summary["statistics"].values())
    assert summary["misjudgments"] == []


def test_manual_before_agent_and_agent_failure_do_not_count_as_retests_or_misjudgments(board):
    """D2: review denominator requires the latest Agent pass and a later manual result."""
    pushed, _ = push(board)
    mark(board, pushed, IDS[0], "pass")
    summary = get(board, BASE)
    assert_ratio(summary["statistics"]["manual_pass_rate"], 0, 0)
    assert_ratio(summary["statistics"]["executed_progress"], 1, 4)
    evidence = upload(board)
    submit(board, pushed, evidence, "agent-fail", ("fail", "fail", "pass", "skip"), [IDS[0]])
    mark(board, pushed, IDS[0], "fail")
    summary = get(board, BASE)
    assert summary["misjudgments"] == []
    assert_ratio(summary["statistics"]["retest_coverage"], 0, 0)
    submit(board, pushed, evidence, "agent-pass", selected=[IDS[0]])
    summary = get(board, BASE)
    assert_ratio(summary["statistics"]["manual_pass_rate"], 0, 0)
    assert_ratio(summary["statistics"]["retest_coverage"], 0, 1)
    assert_ratio(summary["statistics"]["first_pass_rate"], 0, 1)
    assert summary["queues"]["retest"] == [IDS[0]]
    mark(board, pushed, IDS[0], "pass")
    summary = get(board, BASE)
    assert_ratio(summary["statistics"]["manual_pass_rate"], 1, 1)
    assert_ratio(summary["statistics"]["retest_coverage"], 1, 1)
