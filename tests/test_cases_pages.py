"""用例页与没有场景版本的批次页面。"""

from copy import deepcopy

from cases_helpers import CASES_BASE, CASES_WEB, CASE_IDS, cases_body, cases_document, upload_cases
from helpers import BASE, KEY, demo, push, push_body
from html_helpers import page
from test_pages import WEB
from test_reads import get


def test_cases_only_home_overview_and_scenarios_show_empty_state_and_cases_link(board):
    """SOP-23-P01：没有场景时所有入口可读，不显示复核按钮。"""
    push(board)
    upload_cases(board)
    summary = get(board, CASES_BASE)
    assert summary["version"] is None and summary["pushed_at"] is None
    assert summary["scenario_file_sha256"] is None and summary["product_digest"] == ""
    assert "尚无场景" in page(board, "/").one("li", **{"data-batch": "cases-only"}).text
    assert page(board, "/").one("a", href=CASES_WEB).text == "cases-only"
    for url in (CASES_WEB, CASES_WEB + "/scenarios"):
        document = page(board, url)
        assert "尚无场景，测试岗上传用例后由合并生成" in document.text
        assert document.find_all("a", href=CASES_WEB + "/cases")
        assert not document.find_all("form", **{"class": "review-form"})
        assert not document.find_all("article", **{"class": "scenario-card"})


def test_cases_page_groups_details_upload_history_local_time_and_current_coverage(board_factory):
    """SOP-23-P03/P05：分组、字段、记录、对应场景和下载链接。"""
    board = board_factory(env={"TZ": "Asia/Shanghai"})
    scenarios = demo()
    for i in (0, 1):
        scenarios["sops"][0]["scenarios"][i]["id"] = CASE_IDS[i]
    push(board, push_body(scenarios))
    cases = cases_document()
    misc = deepcopy(cases["test_cases"][0])
    misc.update(id="TC_DEMO_MISC_001", title="杂项 <script>", tags=["happy-path"])
    cases["test_cases"].append(misc)
    upload_cases(board, {**cases_body(cases), "filename": "cases-c.yaml"}, base=BASE)
    document = page(board, WEB + "/cases")
    assert [s.one("h2").text for s in document.find_all("section", **{"class": "case-group"})] == ["登录", "订单", "未分组"]
    for case in cases["test_cases"]:
        card = document.one("article", **{"data-case": case["id"]})
        for value in [case["id"], case["title"], case["priority"], "功能", *case["preconditions"], *case["steps"], *case["expected_results"]]:
            assert value in card.text
        if case["id"] in CASE_IDS[:2]:
            assert card.one("a", href=WEB + "/s/" + case["id"]).text == "已有对应场景"
        else:
            assert "尚无对应场景" in card.text
    history = document.one("tr", **{"data-upload": "1"})
    assert [cell.text for cell in history.find_all("td")][:2] == ["cases-c.yaml", "张三"]
    assert [cell.text for cell in history.find_all("td")][-3:] == ["4", "4", "0"]
    assert history.one("time").text.endswith("+08:00") and "." not in history.one("time").text
    assert document.one("a", href=BASE + "/cases.yaml").text == "下载用例并集"
    form = document.one("form", **{"id": "cases-upload"})
    assert form.attrs["action"] == BASE + "/cases"
    assert form.one("input", name="file").attrs["type"] == "file"
    assert document.one("section", id="review-identity")
    assert not document.find_all("script", src="<script>")
    raw = get(board, WEB + "/cases").decode()
    assert "&lt;script&gt;" in raw and "http://" not in raw and "https://" not in raw


def test_cases_navigation_is_shared_and_only_current_page_is_highlighted(board):
    """SOP-21-P07/SOP-33-P12：含新增 SOP 链接的共用导航，当前页单独高亮。"""
    push(board)
    for url, label in [(WEB, "总览"), (WEB + "/scenarios", "SOP 场景"), (WEB + "/cases", "用例"), (WEB + "/progress", "进度"), (WEB + "/merges", "合并")]:
        nav = page(board, url).one("nav", **{"aria-label": "批次导航"})
        assert [(a.text, a.attrs["href"]) for a in nav.find_all("a")] == [
            ("总览", WEB), ("SOP 场景", WEB + "/scenarios"), ("用例", WEB + "/cases"), ("进度", WEB + "/progress"), ("合并", WEB + "/merges"), ("SOP", WEB + "/sop")]
        assert nav.one("a", **{"aria-current": "page"}).text == label
