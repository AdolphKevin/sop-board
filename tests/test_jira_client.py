"""SOP-24：在 Node.js 隔离页面中运行真实 board.js。"""

import pytest

from helpers import BASE, IDS, demo, push, push_body, snapshot
from jira_helpers import mock_jira
from test_client import run_page_client
from test_marks import mark_body
from test_pages import WEB


@pytest.mark.parametrize("suffix", ["/scenarios", "/s/N1-P01", "/s/N2-P01"])
def test_jira_client_shows_summary_beside_each_jira_link_only(board_factory, suffix):
    """SOP-24-P02、SOP-21-P08：列表、详情、历史中的 Jira 链接显示摘要，GitHub 不请求。"""
    with mock_jira() as jira:
        board = board_factory(env=jira.configured_env())
        pushed, _ = push(board)
        for sid, link in [(IDS[0], "ABC-1"), (IDS[2], "https://github.com/demo-org/demo-app/issues/4")]:
            assert board.request("POST", BASE + "/marks", mark_body(
                pushed, "fail", scenario=sid, fingerprint=pushed["fingerprints"][sid], defect_url=link))[0] == 200
        before = snapshot(board)
        run_page_client(board, "jira-display", WEB + suffix, network=True,
                        context={"jiraBase": jira.url, "expectedJiraLinks": 0 if suffix.endswith("N2-P01") else 1 if suffix == "/scenarios" else 2})
        assert snapshot(board) == before
        assert all(request[0] == "/rest/api/3/issue/ABC-1?fields=summary,status" for request in jira.requests)


@pytest.mark.parametrize("suffix", ["/scenarios", "/s/N1-P01"])
def test_jira_client_loads_new_links_after_in_place_review(board_factory, suffix):
    """SOP-24-P02、SOP-21-P08：就地提交后新增的最新结果与历史也显示摘要。"""
    with mock_jira() as jira:
        board = board_factory(env=jira.configured_env())
        push(board)
        run_page_client(board, "jira-live", WEB + suffix, network=True,
                        context={"jiraBase": jira.url, "expectedJiraLinks": 1 if suffix == "/scenarios" else 2})
        assert board.sql("SELECT status,finding FROM results") == [("fail", jira.url + "/browse/ABC-1")]


@pytest.mark.parametrize("failure", ["404", "500", "timeout", "disconnect", "unconfigured", "network", "json"])
def test_jira_client_failure_leaves_filter_and_review_working(board_factory, failure):
    """SOP-24-N03/N04/N05：读取失败在链接旁提示，内容、筛选与通过仍正常。"""
    with mock_jira() as jira:
        if failure in ("404", "500"):
            jira.replies["ABC-1"] = (int(failure), {}, 0)
        elif failure == "timeout":
            jira.replies["ABC-1"] = (200, {}, 0.6)
        env = {**jira.configured_env(), "JIRA_TIMEOUT_SECONDS": "0.15"}
        if failure == "unconfigured":
            env.pop("JIRA_API_TOKEN")
        board = board_factory(env=env)
        pushed, _ = push(board)
        assert board.request("POST", BASE + "/marks", mark_body(pushed, "fail", defect_url="ABC-1"))[0] == 200
        context = dict(jiraBase=jira.url, expectedJiraLinks=1, expectedLabel="Jira 读取失败",
                       jiraFailure=failure)
        if failure != "disconnect":
            run_page_client(board, "jira-failure-review", WEB + "/scenarios?filter=all", network=True, context=context)
    if failure == "disconnect":
        run_page_client(board, "jira-failure-review", WEB + "/scenarios?filter=all", network=True, context=context)
    assert board.sql("SELECT status FROM results ORDER BY id") == [("fail",), ("pass",)]


def test_jira_client_loads_removed_scenario_history_without_review_identity(board_factory):
    """SOP-24-P02：移除场景只有历史，仍对其中的 Jira 链接显示摘要。"""
    with mock_jira() as jira:
        board = board_factory(env=jira.configured_env())
        pushed, _ = push(board)
        assert board.request("POST", BASE + "/marks", mark_body(
            pushed, "fail", scenario=IDS[1], fingerprint=pushed["fingerprints"][IDS[1]], defect_url="ABC-1"))[0] == 200
        document = demo()
        document["sops"][0]["scenarios"].pop()
        document["sops"][0]["negative_exempt"] = "用于核对已移除场景的历史"
        push(board, push_body(document))
        run_page_client(board, "jira-display", WEB + "/s/" + IDS[1], network=True,
                        context={"jiraBase": jira.url, "expectedJiraLinks": 1})


def test_jira_client_displays_untrusted_summary_as_text(board_factory):
    """SOP-24-P02/N06：摘要中的标记只是可见文字，不创建网页元素。"""
    with mock_jira() as jira:
        title = "<img src=x onerror=alert(1)>"
        jira.replies["ABC-1"] = (200, dict(fields=dict(summary=title, status=dict(name="<script>关闭</script>"))), 0)
        board = board_factory(env=jira.configured_env())
        pushed, _ = push(board)
        assert board.request("POST", BASE + "/marks", mark_body(pushed, "fail", defect_url="ABC-1"))[0] == 200
        run_page_client(board, "jira-display", WEB + "/scenarios", network=True,
                        context={"jiraBase": jira.url, "expectedJiraLinks": 1,
                                 "expectedLabel": title + " · <script>关闭</script>"})
