"""SOP-BOARD-1 场景版本 4：结果历史、轮次校验与统计。"""

from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from helpers import BASE, IDS, assert_error, demo, push, push_body, round_body, snapshot, upload
from test_marks import mark_body
from test_reads import get
from test_statistics import assert_ratio


def scenarios_by_id(board):
    return {row["scenario"]: row for row in get(board, BASE + "/scenarios")["scenarios"]}


def test_round_reads_expose_normalized_and_undeclared_independence(board):
    """SOP-03-P03：轮次读取体现姓名大小写、空白归一化及未声明实现者。"""
    pushed, _ = push(board)
    body = round_body(pushed, upload(board), statuses=("pass", "pass", "pass"))
    body["implementer"] = "Alice  Smith"
    for row, name in zip(body["results"], (" alice smith ", "ALICE\tSMITH", "Bob Lee")):
        row["executor"] = name
    assert board.request("POST", BASE + "/rounds", body)[0] == 200
    first = get(board, BASE + "/rounds/1")["results"]
    assert [(row["scenario"], row["independence"]) for row in first] == [
        (IDS[0], "self-check"), (IDS[1], "self-check"), (IDS[2], "independent")]
    body.pop("implementer")
    body["idempotency_key"] = "undeclared"
    body["results"] = body["results"][:1]
    assert board.request("POST", BASE + "/rounds", body)[0] == 200
    assert get(board, BASE + "/rounds/2")["results"][0]["independence"] == "undeclared"


def test_later_agent_submission_reopens_retest_and_preserves_misjudgment(board):
    """SOP-03-P05：较晚提交的旧执行时间仍回到复测，误判归属保留。"""
    pushed, _ = push(board)
    body = round_body(pushed, upload(board), statuses=("pass",))
    body["results"][0].update(executor="sess-a", executed_at=(
        datetime.now(timezone.utc) - timedelta(hours=1)).isoformat())
    assert board.request("POST", BASE + "/rounds", body)[1]["round"] == 1
    link = "https://github.com/demo-org/demo-app/issues/1"
    assert board.request("POST", BASE + "/marks", mark_body(pushed, "fail", defect_url=link))[1]["round"] == 2
    assert get(board, BASE + "/rounds/2")["kind"] == "manual"
    body["idempotency_key"] = "later-submission"
    body["results"][0].update(executor="sess-b", executed_at=(
        datetime.now(timezone.utc) - timedelta(hours=2)).isoformat())
    assert board.request("POST", BASE + "/rounds", body)[1]["round"] == 3
    summary = get(board, BASE)
    assert summary["queues"]["retest"] == [IDS[0]]
    assert IDS[0] not in summary["queues"]["issues"]
    assert scenarios_by_id(board)[IDS[0]]["queue"] == "retest"
    assert summary["misjudgments"] == [dict(scenario=IDS[0], round=2, defect_url=link,
                                           agent_executor="sess-a", marker="Tester A")]
    assert_ratio(summary["statistics"]["manual_pass_rate"], 0, 0)
    assert_ratio(summary["statistics"]["retest_coverage"], 0, 1)


def test_product_change_with_new_scenario_respects_regression_and_manual_pass(board):
    """SOP-03-P06：产品变化与新增场景同时发生时，五条场景进入各自要求的队列。"""
    doc = demo()
    doc["sops"][1]["scenarios"][0]["regression"] = dict(check="unit", cases=["tests::x"])
    pushed, _ = push(board, push_body(doc, product_digest="P1"))
    body = round_body(pushed, upload(board), statuses=("pass", "pass", "pass", "blocked"))
    body["product"]["digest"] = "P1"
    assert board.request("POST", BASE + "/rounds", body)[0] == 200
    manual = mark_body(pushed, scenario=IDS[1], fingerprint=pushed["fingerprints"][IDS[1]])
    assert board.request("POST", BASE + "/marks", manual)[0] == 200
    added = deepcopy(doc["sops"][0]["scenarios"][0])
    added["id"] = "N1-P02"
    doc["sops"][0]["scenarios"].append(added)
    push(board, push_body(doc, product_digest="P2"))
    assert {key: row["queue"] for key, row in scenarios_by_id(board).items()} == {
        IDS[0]: "pending", IDS[1]: "reviewed", IDS[2]: "retest", IDS[3]: "issues", "N1-P02": "pending"}


@pytest.mark.parametrize("kind", ["naive-time", "duplicate", "empty"])
def test_invalid_time_duplicate_and_empty_results_report_precise_problems(board, kind):
    """SOP-03-N05：无时区、重复与空结果均拒绝，字段与重复编号可定位。"""
    pushed, _ = push(board)
    body = round_body(pushed, upload(board), statuses=("pass",))
    if kind == "naive-time":
        body["results"][0]["executed_at"] = "2020-01-01T00:00:00"
    elif kind == "duplicate":
        body["results"].append(deepcopy(body["results"][0]))
    else:
        body["results"] = []
    before = snapshot(board)
    problems = assert_error(board.request("POST", BASE + "/rounds", body), 422, "round-invalid")
    if kind == "naive-time":
        assert any(p["field"] == "results[0].executed_at" for p in problems)
    elif kind == "duplicate":
        assert any(p["field"] == "results[1].scenario" and IDS[0] in p["problem"]
                   and "重复" in p["problem"] for p in problems)
    assert board.sql("SELECT COUNT(*) FROM rounds") == [(0,)]
    assert snapshot(board) == before


def test_four_minutes_future_time_is_accepted_and_preserved(board):
    """SOP-03-P07：允许四分钟时钟偏差，读取仍保留提交的时间原文。"""
    pushed, _ = push(board)
    body = round_body(pushed, upload(board), statuses=("pass",))
    future = (datetime.now(timezone.utc) + timedelta(minutes=4)).isoformat()
    body["results"][0]["executed_at"] = future
    status, response, _ = board.request("POST", BASE + "/rounds", body)
    assert status == 200 and response["ok"] is True
    assert get(board, BASE + f"/rounds/{response['round']}")["results"][0]["executed_at"] == future


def test_invalid_status_skip_reason_and_six_minutes_future_report_all_fields(board):
    """SOP-03-N06：同时报告非法状态与缺少跳过原因，六分钟偏差也不落库。"""
    pushed, _ = push(board)
    body = round_body(pushed, upload(board))
    body["results"] = [body["results"][0], body["results"][3]]
    body["results"][0]["status"] = "passed"
    body["results"][1]["status"] = "skip"
    body["results"][1].pop("finding")
    before = snapshot(board)
    problems = assert_error(board.request("POST", BASE + "/rounds", body), 422, "round-invalid")
    assert {p["field"] for p in problems} >= {"results[0].status", "results[1].finding"}
    assert snapshot(board) == before
    body["results"] = body["results"][:1]
    body["results"][0].update(status="pass", executed_at=(
        datetime.now(timezone.utc) + timedelta(minutes=6)).isoformat())
    problems = assert_error(board.request("POST", BASE + "/rounds", body), 422, "round-invalid")
    assert any(p["field"] == "results[0].executed_at" for p in problems)
    assert board.sql("SELECT COUNT(*) FROM rounds") == [(0,)]
    assert snapshot(board) == before


def test_first_fail_then_pass_keeps_first_pass_rate_zero(board):
    """SOP-03-P08：重跑通过提升当前通过率，但首轮失败继续计入一次通过率。"""
    pushed, _ = push(board)
    evidence = upload(board)
    for number, status in enumerate(("fail", "pass"), 1):
        body = round_body(pushed, evidence, statuses=(status,), key=f"attempt-{number}")
        assert board.request("POST", BASE + "/rounds", body)[1]["round"] == number
    summary = get(board, BASE)
    assert_ratio(summary["statistics"]["agent_pass_rate"], 1, 1)
    assert_ratio(summary["statistics"]["first_pass_rate"], 0, 1)
    assert [(r["number"], r["pass_rate"]) for r in summary["rounds"]] == [
        (1, dict(numerator=0, denominator=1, rate=0)), (2, dict(numerator=1, denominator=1, rate=1))]


def test_metadata_edits_keep_results_and_restored_content_reuses_matching_history(board):
    """SOP-05-P02：标题与优先级保留历史，改动正文待执行，改回后恢复原结果。"""
    original, _ = push(board)
    assert board.request("POST", BASE + "/rounds", round_body(original, upload(board)))[0] == 200
    assert board.request("POST", BASE + "/marks", mark_body(original))[0] == 200
    previous = scenarios_by_id(board)[IDS[0]]
    doc = demo()
    scenario = doc["sops"][0]["scenarios"][0]
    scenario.update(title="新标题", priority="P2")
    assert push(board, push_body(doc))[1]["version"] == 2
    current = scenarios_by_id(board)[IDS[0]]
    for field in ("fingerprint", "latest_agent", "latest_manual", "queue"):
        assert current[field] == previous[field]
    assert current["queue"] == "reviewed"
    scenario["expected"] = ["新预期"]
    assert push(board, push_body(doc))[1]["version"] == 3
    assert scenarios_by_id(board)[IDS[0]]["queue"] == "pending"
    scenario["expected"] = demo()["sops"][0]["scenarios"][0]["expected"]
    assert push(board, push_body(doc))[1]["version"] == 4
    restored = scenarios_by_id(board)[IDS[0]]
    assert restored["fingerprint"] == original["fingerprints"][IDS[0]]
    assert restored["latest_agent"] == previous["latest_agent"]
    assert restored["latest_agent"]["number"] == 1 and restored["latest_agent"]["status"] == "pass"
    assert restored["latest_manual"] == previous["latest_manual"]
    assert restored["latest_manual"]["status"] == "pass" and restored["queue"] == "reviewed"
    assert all(IDS[0] not in queue for queue in get(board, BASE)["queues"].values())


def test_removed_scenario_manual_mark_is_rejected_without_writes(board):
    """SOP-05-N03：已移除场景不能人工标记，也不产生轮次或观察记录。"""
    original, _ = push(board)
    doc = demo()
    doc["sops"][1]["scenarios"].pop()
    doc["sops"][1]["negative_exempt"] = "演示不适用"
    push(board, push_body(doc))
    before = snapshot(board)
    body = mark_body(original, scenario=IDS[3], fingerprint=original["fingerprints"][IDS[3]])
    problems = assert_error(board.request("POST", BASE + "/marks", body), 409, "scenario-stale")
    assert IDS[3] in str(problems)
    assert board.sql("SELECT COUNT(*) FROM rounds") == [(0,)]
    assert snapshot(board) == before


def test_populated_scenario_and_round_read_fields(board):
    """SOP-06-P06：有结果时仍返回全部场景字段、回归声明及两类轮次详情。"""
    doc = demo()
    regression = dict(check="unit", cases=["tests::x"])
    doc["sops"][1]["scenarios"][0]["regression"] = regression
    pushed, _ = push(board, push_body(doc))
    body = round_body(pushed, upload(board))
    assert board.request("POST", BASE + "/rounds", body)[0] == 200
    assert board.request("POST", BASE + "/marks", mark_body(pushed))[0] == 200
    scenarios = scenarios_by_id(board)
    assert set(scenarios) == set(IDS)
    for node in doc["sops"]:
        for scenario in node["scenarios"]:
            row = scenarios[scenario["id"]]
            assert {"scenario", "node", "title", "polarity", "priority", "verify", "fingerprint",
                    "queue", "latest_agent", "latest_manual", "regression"} <= row.keys()
            assert row["node"] == node["id"]
            for field in ("title", "polarity", "priority", "verify"):
                assert row[field] == scenario[field]
            assert row["fingerprint"] == pushed["fingerprints"][scenario["id"]]
            assert row["latest_agent"]["status"] == next(
                r["status"] for r in body["results"] if r["scenario"] == scenario["id"])
            assert row["regression"] == scenario.get("regression")
            assert row["queue"] == {IDS[0]: "reviewed", IDS[1]: "issues", IDS[2]: "retest", IDS[3]: "issues"}[scenario["id"]]
            if scenario["id"] == IDS[0]:
                assert row["latest_manual"]["status"] == "pass"
            else:
                assert row["latest_manual"] is None
    agent, manual = get(board, BASE + "/rounds/1"), get(board, BASE + "/rounds/2")
    assert agent["kind"] == "agent" and len(agent["results"]) == 4
    assert agent["product"] == body["product"]
    assert agent["submitted_by"] == body["submitted_by"] and agent["implementer"] == body["implementer"]
    assert datetime.fromisoformat(agent["submitted_at"]).tzinfo is not None
    for row, expected in zip(agent["results"], body["results"]):
        assert row.items() >= expected.items()
    assert manual["kind"] == "manual" and len(manual["results"]) == 1
    assert manual["results"][0]["scenario"] == IDS[0] and manual["results"][0]["status"] == "pass"


def test_manual_blocked_counts_coverage_without_pass_rate_or_misjudgment(board):
    """SOP-06-P09：人工受阻进入问题队列并计入覆盖率，未形成失败误判。"""
    pushed, _ = push(board)
    assert board.request("POST", BASE + "/rounds", round_body(pushed, upload(board)))[0] == 200
    assert board.request("POST", BASE + "/marks", mark_body(pushed, "blocked", defect_url="https://github.com/demo-org/demo-app/issues/3"))[0] == 200
    summary = get(board, BASE)
    assert IDS[0] in summary["queues"]["issues"]
    assert scenarios_by_id(board)[IDS[0]]["queue"] == "issues"
    assert_ratio(summary["statistics"]["retest_coverage"], 1, 2)
    assert_ratio(summary["statistics"]["manual_pass_rate"], 0, 0)
    assert summary["misjudgments"] == []
