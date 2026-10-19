"""I5：经真实 HTTP 核对条件请求、执行状态与独立预期的进度。"""

from copy import deepcopy

import pytest

from cases_helpers import upload_cases
from helpers import BASE, IDS, KEY, assert_error, demo, push, push_body, round_body, upload
from html_helpers import page
from test_marks import mark_body
from test_pages import WEB


def test_state_etag_tracks_version_round_and_upload_but_not_identical_upload(board):
    """SOP-25-P04：304 不带正文；三种数据变化分别改变 ETag。"""
    pushed, _ = push(board)
    status, data, headers = board.request("GET", BASE + "/state", auth=False)
    assert status == 200, data
    assert data == dict(ok=True, etag=headers["ETag"])
    etag = data["etag"]

    def unchanged():
        status, body, headers = board.request("GET", BASE + "/state", auth=False,
                                             headers={"If-None-Match": etag})
        assert status == 304 and body == b""
        assert headers["ETag"] == etag

    def changed():
        status, data, headers = board.request("GET", BASE + "/state", auth=False,
                                             headers={"If-None-Match": etag})
        assert status == 200 and data["etag"] != etag
        assert data["etag"] == headers["ETag"]
        return data["etag"]

    unchanged()
    assert board.request("POST", BASE + "/marks", mark_body(pushed))[0] == 200
    etag = changed()
    upload_cases(board, base=BASE)
    etag = changed()
    upload_cases(board, base=BASE)
    unchanged()
    document = demo()
    document["sops"][0]["scenarios"][0]["expected"] = ["变更后的独立预期"]
    push(board, push_body(document))
    etag = changed()
    unchanged()
    board.stop()
    board.start()
    unchanged()


@pytest.mark.parametrize("suffix", ["state", "report.md", "results.yaml"])
def test_new_reads_reject_missing_project_and_batch(board, suffix):
    """SOP-25-P04、SOP-26-N02：缺失资源有明确的 404。"""
    assert_error(board.request("GET", f"/api/v1/projects/missing/batches/none/{suffix}", auth=False),
                 404, "project-not-found")
    push(board)
    assert_error(board.request("GET", BASE.replace("/demo", "/missing") + "/" + suffix, auth=False),
                 404, "batch-not-found")


def test_agent_state_home_and_overview_include_local_submission_and_current_counts(board_factory):
    """SOP-25-P01/P05：用例批次、待执行、测完（含跳过）三种状态。"""
    board = board_factory(env={"TZ": "Asia/Shanghai"})
    push(board)
    target = BASE.replace("/demo", "/b6")
    web = WEB.replace("/demo", "/b6")
    upload_cases(board, base=target)

    def assert_state(label):
        assert label in page(board, "/").one("li", **{"data-batch": "b6"}).text
        assert label in page(board, web).one(id="agent-state").text

    assert_state("尚无场景")
    document = demo()
    document["metadata"]["batch"] = "b6"
    extra = deepcopy(document["sops"][0]["scenarios"][0])
    extra["id"] = "N1-P02"
    document["sops"][0]["scenarios"].append(extra)
    pushed = push_body(document)
    assert board.request("PUT", target + "/scenario-file", pushed)[0] == 200
    assert_state("有待执行 5 条")
    body = round_body(pushed, upload(board))
    body["results"].append({**body["results"][0], "scenario": "N1-P02", "status": "skip", "finding": "依赖未部署"})
    assert board.request("POST", target + "/rounds", body)[0] == 200
    assert_state("Agent 已测完")
    from test_page_times import expected_local_time
    submitted = board.sql("SELECT submitted_at FROM rounds")[0][0]
    for element in [page(board, "/").one("li", **{"data-batch": "b6"}), page(board, web).one(id="agent-state")]:
        assert expected_local_time(submitted, "Asia/Shanghai") in element.text
        assert "通过 2、失败 1、受阻 1、跳过 1" in element.text
    # Manual submissions do not become the latest Agent submission.
    assert board.request("POST", target + "/marks", mark_body(pushed))[0] == 200
    assert page(board, web).one(id="agent-state").one("time").attrs["datetime"] == submitted


def test_progress_columns_navigation_and_five_scenario_metrics(board_factory):
    """SOP-25-P06、SOP-21-P07：两列并排，页面/API 对照独立数字。"""
    from progress_helpers import P5_API, P5_WEB, prepare_five
    board = board_factory(env={"JIRA_URL": "https://acme.atlassian.net"})
    prepare_five(board)
    root = page(board, P5_WEB + "/progress")
    columns = root.one(**{"class": "progress-columns"})
    assert [s.one("h2").text for s in columns.find_all("section")] == ["开发侧", "测试侧"]
    expected = [("pending", "待执行", "0"), ("executed_progress", "已执行进度", "5/5"),
                ("agent_pass_rate", "Agent 验收通过率", "3/4"), ("first_pass_rate", "一次通过率", "3/4"),
                ("retest", "待复核", "1"), ("reviewed_progress", "已复核进度", "2/3"),
                ("manual_pass_rate", "人工复测通过率", "1/2"), ("misjudgments", "误判", "1"),
                ("coverage", "用例覆盖", "3/4")]
    for key, label, value in expected:
        metric = root.one(id="metric-" + key)
        assert metric.one("dt").text == label
        assert metric.one("dd").text == value
    summary = board.request("GET", P5_API, auth=False)[1]
    for key, n, d in [("executed_progress", 5, 5), ("agent_pass_rate", 3, 4), ("first_pass_rate", 3, 4),
                      ("reviewed_progress", 2, 3), ("manual_pass_rate", 1, 2)]:
        assert summary["statistics"][key] == dict(numerator=n, denominator=d, rate=n/d)
    assert board.request("GET", P5_API + "/cases")[1]["coverage"] == dict(numerator=3, denominator=4, rate=.75)
    for suffix in ["", "/scenarios", "/cases", "/progress", "/merges"]:
        nav = page(board, P5_WEB + suffix).one("nav", **{"aria-label": "批次导航"})
        assert [a.attrs["href"] for a in nav.find_all("a")] == [P5_WEB + s for s in ["", "/scenarios", "/cases", "/progress", "/merges"]]
        assert nav.one("a", **{"aria-current": "page"}).attrs["href"] == P5_WEB + suffix
    css = board.request("GET", "/assets/board.css")[1].decode()
    assert ".progress-columns" in css and "repeat(2, 1fr)" in css


def test_progress_empty_denominators_and_three_pending_scenarios(board):
    """SOP-25-P07：没有结果/用例时，零分母写无数据。"""
    document = demo()
    document["sops"][1]["scenarios"].pop()
    document["sops"][1]["negative_exempt"] = "无反向路径"
    push(board, push_body(document))
    root = page(board, WEB + "/progress")
    for key, value in [("pending", "3"), ("executed_progress", "0/3"), ("retest", "0"), ("misjudgments", "0"),
                       ("agent_pass_rate", "无数据"), ("first_pass_rate", "无数据"),
                       ("reviewed_progress", "无数据"), ("manual_pass_rate", "无数据"), ("coverage", "无数据")]:
        assert root.one(id="metric-" + key).one("dd").text == value


def test_seven_scenario_statistics_queues_and_overview_follow_d2(board):
    """SOP-25-P09：七场景所有指标、队列、轮次与误判使用独立预期。"""
    from review_helpers import F7_API, F7_WEB, prepare_seven
    prepare_seven(board)
    summary = board.request("GET", F7_API)[1]
    assert summary["queues"] == dict(pending=["N1-P01"], issues=["N1-N01", "N1-N02", "N2-P02", "N2-N01"], retest=["N1-P02"])
    assert summary["queue_counts"] == dict(pending=1, issues=4, retest=1)
    for key, n, d in [("agent_pass_rate", 3, 4), ("manual_pass_rate", 1, 2), ("retest_coverage", 2, 3),
                      ("first_pass_rate", 3, 4), ("executed_progress", 6, 7), ("reviewed_progress", 2, 3)]:
        assert summary["statistics"][key] == dict(numerator=n, denominator=d, rate=n/d)
        assert page(board, F7_WEB).one(id="metric-" + key).one("dd").text == f"{n}/{d}"
    assert [r["pass_rate"] for r in summary["rounds"]] == [dict(numerator=3, denominator=6, rate=.5),
        dict(numerator=1, denominator=1, rate=1), dict(numerator=0, denominator=1, rate=0)]
    assert summary["misjudgments"] == [dict(scenario="N2-P02", round=3, defect_url="https://github.com/demo-org/demo-app/issues/1",
                                           agent_executor="Li Si", marker="Tester A")]
    scenarios = {s["scenario"]: s for s in board.request("GET", F7_API + "/scenarios")[1]["scenarios"]}
    assert scenarios["N2-P01"]["queue"] == "reviewed"
    assert scenarios["N2-P01"]["latest_agent"]["status"] == scenarios["N2-P01"]["latest_manual"]["status"] == "pass"
    assert scenarios["N2-P02"]["latest_agent"]["status"] == "pass" and scenarios["N2-P02"]["latest_manual"]["status"] == "fail"
    for queue, ids in summary["queues"].items():
        assert all(scenarios[sid]["queue"] == queue for sid in ids)


def test_demo_agent_state_and_progress_match_four_scenario_expected_values(board):
    """SOP-25-P01/P02：演示四场景的状态、提交时间、进度和覆盖率。"""
    pushed, _ = push(board)
    for root in [page(board, "/").one("li", **{"data-batch": "demo"}), page(board, WEB).one(id="agent-state")]:
        assert "有待执行 4 条" in root.text
    assert board.request("POST", BASE + "/rounds", round_body(pushed, upload(board)))[0] == 200
    for root in [page(board, "/").one("li", **{"data-batch": "demo"}), page(board, WEB).one(id="agent-state")]:
        assert "Agent 已测完" in root.text and "通过 2、失败 1、受阻 1、跳过 0" in root.text
    assert board.request("POST", BASE + "/marks", mark_body(pushed))[0] == 200
    upload_cases(board, base=BASE)
    root = page(board, WEB + "/progress")
    for key, value in [("pending", "0"), ("executed_progress", "4/4"), ("agent_pass_rate", "2/3"),
                       ("first_pass_rate", "2/3"), ("retest", "1"), ("reviewed_progress", "1/2"),
                       ("manual_pass_rate", "1/1"), ("misjudgments", "0"), ("coverage", "0/3")]:
        assert root.one(id="metric-" + key).one("dd").text == value
