"""执行实际 board.js，通过隔离的本机 HTTP 服务核对复核操作与最终数据。"""

import pytest

from helpers import BASE, IDS, assert_error, push, push_body, round_body, snapshot, upload
from html_helpers import page
from review_helpers import F7_API, F7_IDS, F7_WEB, prepare_seven, seven_document
from test_client import run_page_client
from test_pages import WEB
from test_reads import get


def prepare_demo(board):
    pushed, _ = push(board)
    assert board.request("POST", BASE + "/rounds", round_body(pushed, upload(board)))[0] == 200
    return pushed


@pytest.mark.parametrize("query", ["", "?filter=fail", "?filter=xyz"])
def test_seven_filters_lists_counts_selected_link_and_shareable_addresses(board, query):
    """SOP-21-P09: all seven/never-run/skip/manual-override cases exercise every filter."""
    prepare_seven(board)
    before = snapshot(board)
    run_page_client(board, "seven-filters", F7_WEB + "/scenarios" + query)
    assert snapshot(board) == before


def test_seven_filters_and_history_after_content_change_and_removal(board):
    """SOP-21-N04: changed content clears current columns; removed results remain only in history."""
    prepare_seven(board)
    agent = get(board, F7_API + "/rounds/1")
    original_results = board.sql("SELECT * FROM results ORDER BY id")
    document = seven_document()
    document["sops"][0]["scenarios"][1]["expected"] = ["修改 N1-P02 的独立预期"]
    document["sops"][1]["scenarios"].pop()
    document["sops"][1]["negative_exempt"] = "用于验证移除历史"
    code, response, _ = board.request("PUT", F7_API + "/scenario-file", push_body(document, path="f7.yaml"))
    assert code == 200 and response["changed"] == ["N1-P02"] and response["removed"] == ["N2-N01"]
    run_page_client(board, "seven-filters", F7_WEB + "/scenarios", context={"changed": True})
    listing = page(board, F7_WEB + "/scenarios")
    current = listing.one("article", **{"data-scenario": "N1-P02"})
    assert current.one("div", **{"data-latest-agent": ""}).text == ""
    assert current.one("div", **{"data-latest-manual": ""}).text == ""
    assert current.one("p", **{"data-queue": ""}).text == "所在队列：待执行"
    assert not listing.find_all("article", **{"data-scenario": "N2-N01"})
    detail = page(board, F7_WEB + "/s/N1-P02")
    assert "场景已变更" in detail.text
    groups = detail.find_all("section", **{"data-fingerprint": agent["results"][0]["fingerprint"]})
    assert len(groups) == 1 and "第 1 轮 · Agent · 通过" in groups[0].text
    removed = page(board, F7_WEB + "/s/N2-N01")
    assert "已从场景文件移除" in removed.text and "第 1 轮 · Agent · 跳过" in removed.text
    assert board.sql("SELECT * FROM results ORDER BY id") == original_results
    assert get(board, F7_API + "/rounds/1") == agent


def test_seven_one_click_pass_updates_cards_counts_history_and_rows(board_factory):
    """SOP-21-P10: actual script submits both an Agent pass and a never-run scenario."""
    board = board_factory(env={"TZ": "Asia/Shanghai"})
    prepare_seven(board)
    original = get(board, F7_API + "/rounds/1")
    run_page_client(board, "real-seven-pass", F7_WEB + "/scenarios", network=True)
    for number, sid in [(4, "N1-P02"), (5, "N1-P01")]:
        manual = get(board, F7_API + f"/rounds/{number}")
        assert manual["kind"] == "manual" and len(manual["results"]) == 1
        row = manual["results"][0]
        assert (row["scenario"], row["status"], row["actual"], row["executor"], row["independence"]) == (
            sid, "pass", "人工复核通过", "张三", "undeclared")
    detail = page(board, F7_WEB + "/s/N1-P02")
    assert "第 4 轮 · 人工 · 通过" in detail.text and "张三" in detail.text
    assert get(board, F7_API + "/rounds/1") == original
    assert board.sql("SELECT COUNT(*) FROM rounds WHERE kind='manual'") == [(4,)]


def test_seven_fail_and_blocked_update_normalized_links_misjudgments_and_rates(board_factory):
    """SOP-21-P11: exact seven-scenario filter/rate/misjudgment changes and preserved Agent rows."""
    board = board_factory(env={"JIRA_URL": "https://acme.atlassian.net", "TZ": "Asia/Shanghai"})
    prepare_seven(board)
    original = get(board, F7_API + "/rounds/1")
    assert get(board, F7_API)["statistics"]["manual_pass_rate"] == {"numerator": 1, "denominator": 2, "rate": 0.5}
    run_page_client(board, "real-seven-nonpass", F7_WEB + "/scenarios", network=True)
    summary = get(board, F7_API)
    assert summary["statistics"]["manual_pass_rate"] == {"numerator": 1, "denominator": 3, "rate": 1 / 3}
    assert summary["statistics"]["agent_pass_rate"] == {"numerator": 3, "denominator": 4, "rate": 0.75}
    assert summary["misjudgments"] == [
        {"scenario": "N1-P02", "round": 4, "defect_url": "https://acme.atlassian.net/browse/ABC-123", "agent_executor": "Li Si", "marker": "张三"},
        {"scenario": "N2-P02", "round": 3, "defect_url": "https://github.com/demo-org/demo-app/issues/1", "agent_executor": "Li Si", "marker": "Tester A"}]
    fail = get(board, F7_API + "/rounds/4")["results"][0]
    blocked = get(board, F7_API + "/rounds/5")["results"][0]
    assert fail["finding"] == "https://acme.atlassian.net/browse/ABC-123" and "note" not in fail
    assert blocked["finding"] == "https://acme.atlassian.net/browse/ABC-124" and blocked["note"] == "测试环境无法登录"
    overview = page(board, F7_WEB)
    assert overview.one("div", id="metric-manual_pass_rate").one("dd").text == "1/3"
    assert overview.one("div", id="metric-agent_pass_rate").one("dd").text == "3/4"
    assert "N1-P02" in overview.one("section", id="misjudgments").text
    assert "N1-P01" not in overview.one("section", id="misjudgments").text
    assert get(board, F7_API + "/rounds/1") == original


def test_empty_and_whitespace_links_cancel_without_requests_or_persistence(board):
    """SOP-21-N01: two cards retain independent drafts and no state/quantity changes."""
    prepare_demo(board)
    before = snapshot(board)
    run_page_client(board, "real-empty-cancel", WEB + "/scenarios", network=True)
    assert snapshot(board) == before


@pytest.mark.parametrize("status,sid", [("pass", IDS[1]), ("fail", IDS[2])])
def test_detail_script_uses_same_controls_and_appends_actual_http_result(board_factory, status, sid):
    """SOP-21-P05: both detail-page actions use the real API without navigation."""
    board = board_factory(env={"JIRA_URL": "https://acme.atlassian.net"})
    prepare_demo(board)
    original = get(board, BASE + "/rounds/1")
    run_page_client(board, "real-detail-" + status, WEB + "/s/" + sid, network=True, context={"sid": sid})
    row = get(board, BASE + "/rounds/2")["results"][0]
    assert (row["scenario"], row["status"], row["executor"]) == (sid, status, "张三")
    if status == "fail":
        assert row["finding"] == "https://acme.atlassian.net/browse/ABC-77" and "note" not in row
    assert get(board, BASE + "/rounds/1") == original


def test_rejection_sequence_keeps_all_content_until_success_without_extra_rounds(board):
    """SOP-21-N02/N05: unauthorized, invalid defect, then success; no earlier writes."""
    prepare_demo(board)
    original = get(board, BASE + "/rounds/1")
    run_page_client(board, "real-retry", WEB + "/scenarios", network=True, context={"api": BASE})
    assert board.sql("SELECT COUNT(*) FROM rounds WHERE kind='manual'") == [(1,)]
    row = get(board, BASE + "/rounds/2")["results"][0]
    assert row["finding"] == "https://github.com/demo-org/demo-app/issues/7" and row["note"] == "截图见附件"
    assert get(board, BASE + "/rounds/1") == original


def test_stale_submission_preserves_draft_and_has_no_manual_side_effects(board_factory):
    """SOP-21-N06 T1 part: another HTTP push makes the draft stale; code/reason and text stay."""
    board = board_factory(env={"JIRA_URL": "https://acme.atlassian.net"})
    prepare_seven(board)
    original_results = board.sql("SELECT * FROM results ORDER BY id")
    document = seven_document()
    document["sops"][1]["scenarios"][0]["expected"] = ["另一终端修改的预期"]
    mutation = push_body(document, path="f7.yaml")
    run_page_client(board, "real-stale", F7_WEB + "/scenarios", network=True, context={"api": F7_API, "mutation": mutation})
    assert board.sql("SELECT COUNT(*) FROM rounds") == [(3,)]
    assert board.sql("SELECT * FROM results ORDER BY id") == original_results


def test_identity_restore_and_change_are_used_by_actual_mark_requests(board):
    """SOP-21-P12/N03: missing token cannot write; reload and changed marker affect the new rows."""
    prepare_demo(board)
    run_page_client(board, "real-identity", WEB + "/scenarios", network=True)
    assert board.sql("SELECT submitted_by FROM rounds WHERE kind='manual' ORDER BY number") == [("张三",), ("李四",)]
    assert get(board, BASE + "/rounds/2")["results"][0]["executor"] == "张三"
    assert get(board, BASE + "/rounds/3")["results"][0]["executor"] == "李四"
