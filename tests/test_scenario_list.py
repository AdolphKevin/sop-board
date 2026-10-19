"""SOP-21: HTTP pages expose the current scenario file and review summary."""

import sqlite3

import pytest

from helpers import BASE, IDS, demo, push, push_body, round_body, upload
from html_helpers import page
from test_pages import WEB
from test_regression_views import assert_same_origin_resources
from test_marks import mark_body
from test_reads import get


def test_scenario_list_groups_all_current_scenarios_fields_results_and_navigation(board):
    """SOP-21-P01/P02, I1: fields come from the file; filters use current results."""
    document = demo()
    document["sops"][0]["title"] = "登录检查"
    first = document["sops"][0]["scenarios"][0]
    first.update(refs=["需求第六节"], forbidden_effects=["不得新增订单"],
                 regression={"check": "unit", "cases": ["test_login"]})
    pushed, _ = push(board, push_body(document))
    assert board.request("POST", BASE + "/rounds", round_body(pushed, upload(board)))[0] == 200
    listing = page(board, WEB + "/scenarios?filter=agent-pass")
    nav = listing.one("nav", **{"aria-label": "批次导航"})
    assert [(a.text, a.attrs["href"]) for a in nav.find_all("a")] == [("总览", WEB), ("SOP 场景", WEB + "/scenarios"), ("用例", WEB + "/cases"), ("进度", WEB + "/progress")]
    assert nav.one("a", href=WEB + "/scenarios").attrs["aria-current"] == "page"
    assert page(board, WEB).one("nav", **{"aria-label": "批次导航"}).one("a", href=WEB).attrs["aria-current"] == "page"
    assert [s.attrs["data-node"] for s in listing.find_all("section", **{"class": "scenario-node"})] == ["N1", "N2"]
    node = listing.one("section", **{"data-node": "N1"})
    assert "登录检查" in node.text and "执行者" in node.text and "HTTP" in node.text
    for sid, source, status, queue in zip(IDS, [s for n in document["sops"] for s in n["scenarios"]],
                                         ["通过", "失败", "通过", "受阻"], ["复测", "问题", "复测", "问题"]):
        card = listing.one("article", **{"data-scenario": sid})
        assert card.one("a", href=WEB + "/s/" + sid).text == sid
        for value in (source["title"], source["priority"], source["actor"], source["verify"],
                      *source["initial_state"], *source["steps"], *source["expected"], *source["forbidden_effects"], *source.get("refs", [])):
            assert value in card.text
        assert ("正向" if source["polarity"] == "positive" else "反向") in card.text
        assert status in card.one("div", **{"data-latest-agent": ""}).text
        assert "Li Si" in card.one("div", **{"data-latest-agent": ""}).text
        assert queue in card.one("p", **{"data-queue": ""}).text
        assert card.one("div", **{"data-latest-manual": ""}).text == ""
        assert card.find_all("time")
    assert "test_login" in listing.one("article", **{"data-scenario": IDS[0]}).text
    for name, count in [("all", 4), ("unreviewed", 4), ("agent-pass", 2), ("fail", 1), ("blocked", 1)]:
        assert listing.one("a", **{"data-filter": name}).one("span").text == str(count)
    assert_same_origin_resources(board, listing)


def test_scenario_page_without_version_explains_case_only_batch(board):
    """I1: a case-only batch has a clear next step, without making a fake version."""
    push(board)
    with sqlite3.connect(board.data / "board.sqlite3") as db:
        db.execute("INSERT INTO batches(project,name,path,product_digest) VALUES (?,?,?,?)",
                   ("github-com-demo-org-demo-app", "cases-only", "", ""))
    doc = page(board, WEB.rsplit("/", 1)[0] + "/cases-only/scenarios")
    assert "尚无场景，测试岗上传用例后由合并生成" in doc.text
    links = doc.find_all("a", href=WEB.rsplit("/", 1)[0] + "/cases-only/cases")
    assert [link.text for link in links] == ["用例", "用例页"]
    assert not doc.find_all("form")


def test_filters_count_current_fingerprints_and_latest_submission_order(board):
    """SOP-21-P02, SOP-05-P01: old manual/Agent results never inflate current filters."""
    pushed, _ = push(board)
    assert board.request("POST", BASE + "/rounds", round_body(pushed, upload(board)))[0] == 200
    code, response, _ = board.request("POST", BASE + "/marks", mark_body(pushed, "fail",
        defect_url="https://github.com/demo-org/demo-app/issues/9"))
    assert code == 200
    assert response["filters"] == {"all": 4, "unreviewed": 3, "agent-pass": 2, "fail": 2, "blocked": 1}
    body = round_body(pushed, upload(board), statuses=("pass",), key="later-agent")
    body["results"][0]["executed_at"] = "2000-01-01T00:00:00Z"
    assert board.request("POST", BASE + "/rounds", body)[0] == 200
    assert get(board, BASE + "/scenarios")["filters"] == {"all": 4, "unreviewed": 3, "agent-pass": 2, "fail": 1, "blocked": 1}
    document = demo()
    document["sops"][0]["scenarios"][0]["expected"] = ["当前新预期"]
    push(board, push_body(document))
    assert get(board, BASE + "/scenarios")["filters"] == {"all": 4, "unreviewed": 4, "agent-pass": 1, "fail": 1, "blocked": 1}
    listing = page(board, WEB + "/scenarios")
    card = listing.one("article", **{"data-scenario": IDS[0]})
    assert "当前新预期" in card.text
    assert card.one("div", **{"data-latest-manual": ""}).text == ""
    assert card.one("div", **{"data-latest-agent": ""}).text == ""
    document["sops"][1]["scenarios"].pop()
    document["sops"][1]["negative_exempt"] = "只保留正向场景以验证移除"
    push(board, push_body(document))
    assert get(board, BASE + "/scenarios")["filters"] == {"all": 3, "unreviewed": 3, "agent-pass": 1, "fail": 1, "blocked": 0}
    assert [c.attrs["data-scenario"] for c in page(board, WEB + "/scenarios").find_all("article", **{"class": "scenario-card"})] == IDS[:3]


def test_seven_scenario_fields_current_summaries_manual_labels_and_navigation(board_factory):
    """SOP-21-P07/P08: complete fields, current-result columns, manual wording and batch navigation."""
    import re
    from review_helpers import F7_API, F7_IDS, F7_WEB, prepare_seven, seven_document, seven_mark
    board = board_factory(env={"TZ": "Asia/Shanghai", "JIRA_URL": "https://acme.atlassian.net"})
    pushed = prepare_seven(board)
    code, response, _ = board.request("POST", F7_API + "/marks", seven_mark(pushed, "N1-P02", "fail", defect_url="ABC-1"))
    assert code == 200, response
    listing = page(board, F7_WEB + "/scenarios")
    for path, active in [(F7_WEB, F7_WEB), (F7_WEB + "/scenarios", F7_WEB + "/scenarios")]:
        doc = page(board, path)
        nav = doc.one("nav", **{"aria-label": "批次导航"})
        assert [(a.text, a.attrs["href"]) for a in nav.find_all("a")] == [("总览", F7_WEB), ("SOP 场景", F7_WEB + "/scenarios"), ("用例", F7_WEB + "/cases"), ("进度", F7_WEB + "/progress")]
        assert [a.attrs["href"] for a in nav.find_all("a") if "aria-current" in a.attrs] == [active]
        assert_same_origin_resources(board, doc)
    expected_queues = {"N1-P01": "待执行", "N1-P02": "问题", "N1-N01": "问题", "N1-N02": "问题",
                       "N2-P01": "已复核通过", "N2-P02": "问题", "N2-N01": "问题"}
    for node in seven_document()["sops"]:
        group = listing.one("section", **{"data-node": node["id"]})
        assert group.one("h2").text == node["id"] + " · " + node["title"]
        assert "角色：" + node["actor"] + " · 入口：" + node["entry"] in group.text
        for source in node["scenarios"]:
            sid = source["id"]
            card = group.one("article", **{"data-scenario": sid})
            assert card.one("a", href=F7_WEB + "/s/" + sid).text == sid
            for value in [source["title"], source["priority"], source["actor"], source["verify"],
                          *source["initial_state"], *source["steps"], *source["expected"], *source["forbidden_effects"], *source["refs"]]:
                assert value in card.text
            assert ("正向" if source["polarity"] == "positive" else "反向") in card.text
            assert card.one("p", **{"data-queue": ""}).text == "所在队列：" + expected_queues[sid]
    first = listing.one("article", **{"data-scenario": "N1-P01"})
    assert first.one("div", **{"data-latest-agent": ""}).text == ""
    assert first.one("div", **{"data-latest-manual": ""}).text == ""
    failed = listing.one("article", **{"data-scenario": "N1-N01"})
    assert failed.one("div", **{"data-latest-manual": ""}).text == ""
    reviewed = listing.one("article", **{"data-scenario": "N1-P02"})
    assert "Agent · 通过 · 执行者：Li Si" in reviewed.one("div", **{"data-latest-agent": ""}).text
    assert reviewed.one("div", **{"data-latest-agent": ""}).one("time").text == "2020-01-15 08:09:10 +08:00"
    assert "人工 · 不通过（失败） · 标记人：张三" in reviewed.one("div", **{"data-latest-manual": ""}).text
    assert reviewed.one("a", href="https://acme.atlassian.net/browse/ABC-1")
    for time in listing.find_all("time"):
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} \+08:00", time.text)
    assert "tests.test_demo::test_n2" in listing.one("article", **{"data-scenario": "N2-P01"}).text
    detail = page(board, F7_WEB + "/s/N1-P02")
    assert "第 4 轮 · 人工 · 不通过（失败）" in detail.text
    assert "第 1 轮 · Agent · 通过" in detail.text


def test_empty_current_result_columns_do_not_show_history_or_placeholder(board):
    """SOP-21-P08/N04: absent current results leave both summary columns empty."""
    push(board)
    card = page(board, WEB + "/scenarios").one("article", **{"data-scenario": IDS[0]})
    assert card.one("div", **{"data-latest-agent": ""}).text == ""
    assert card.one("div", **{"data-latest-manual": ""}).text == ""


@pytest.mark.parametrize("status,label", [("pass", "通过"), ("fail", "不通过（失败）"), ("blocked", "不通过（受阻）")])
def test_manual_result_wording_in_list_and_detail_history(board, status, label):
    """SOP-21-P05/P08/P11: manual wording is identical in summary and history."""
    pushed, _ = push(board)
    extra = {} if status == "pass" else {"defect_url": "https://github.com/demo-org/demo-app/issues/1"}
    assert board.request("POST", BASE + "/marks", mark_body(pushed, status, **extra))[0] == 200
    listing = page(board, WEB + "/scenarios")
    manual = listing.one("article", **{"data-scenario": IDS[0]}).one("div", **{"data-latest-manual": ""})
    assert manual.one("p").text.startswith("人工 · " + label + " · 标记人：Tester A")
    detail = page(board, WEB + "/s/" + IDS[0])
    assert detail.one("article", **{"class": "result"}).one("h3").text == "第 1 轮 · 人工 · " + label
