import pytest

from helpers import BASE, IDS, KEY, assert_error, demo, push, push_body, round_body, snapshot, upload
from html_helpers import page
from test_marks import mark_body
from test_reads import get


WEB = f"/p/{KEY}/b/demo"


def test_home_lists_projects_and_batches_without_token(board):
    """I1 首页：空状态以及项目、身份与可访问的批次链接。"""
    assert "还没有项目" in page(board, "/").text
    push(board)
    home = page(board, "/")
    assert "演示项目" in home.text and "github.com/demo-org/demo-app" in home.text
    assert home.one("a", href=WEB).text.strip() == "demo"


@pytest.mark.parametrize("state", ["new", "agent", "manual-fail"])
def test_batch_page_matches_all_metrics_queues_rounds_and_misjudgments(board, state):
    """I1、D2；SOP-04-N01、SOP-06-P01：页面与接口数值一致，零分母显示无数据。"""
    pushed, _ = push(board)
    if state != "new":
        assert board.request("POST", BASE + "/rounds", round_body(pushed, upload(board)))[0] == 200
    if state == "manual-fail":
        body = mark_body(pushed, "fail", scenario=IDS[2], fingerprint=pushed["fingerprints"][IDS[2]],
                         defect_url="https://github.com/demo-org/demo-app/issues/1")
        assert board.request("POST", BASE + "/marks", body)[0] == 200
    doc = page(board, WEB)
    api = get(board, BASE)
    expected = {"new": ["无数据", "无数据", "无数据", "无数据", "0/4", "无数据"],
                "agent": ["2/3", "无数据", "0/2", "2/3", "4/4", "0/2"],
                "manual-fail": ["2/3", "0/1", "1/2", "2/3", "4/4", "1/2"]}[state]
    for (key, label), literal in zip([
        ("agent_pass_rate", "Agent 验收通过率"), ("manual_pass_rate", "人工复测通过率"),
        ("retest_coverage", "复测覆盖率"), ("first_pass_rate", "一次通过率"),
        ("executed_progress", "已执行进度"), ("reviewed_progress", "已复核进度")], expected):
        metric = doc.one("div", id="metric-" + key)
        assert metric.one("dt").text == label
        value = metric.one("dd").text
        assert value == literal
        rate = api["statistics"][key]
        assert value == (f"{rate['numerator']}/{rate['denominator']}" if rate["denominator"] else "无数据")
    for key in ("pending", "issues", "retest"):
        section = doc.one("section", id="queue-" + key)
        assert section.one("span", **{"data-count": key}).text == str(api["queue_counts"][key])
        assert [a.attrs["data-scenario"] for a in section.find_all("a")] == api["queues"][key]
        for a in section.find_all("a"):
            assert a.attrs["href"] == WEB + "/s/" + a.attrs["data-scenario"]
    assert doc.one("span", id="misjudgment-count").text == str(len(api["misjudgments"]))
    if state != "new":
        row = doc.one("tr", **{"data-round": "1"})
        assert row.one("td", **{"data-field": "pass-rate"}).text == "2/4"
        assert "开发岗" in row.text
    if state == "manual-fail":
        assert "Li Si" in doc.one("section", id="misjudgments").text
        assert "Tester A" in doc.one("section", id="misjudgments").text
        assert doc.one("section", id="misjudgments").one("a", href=body["defect_url"]).text == body["defect_url"]


def test_scenario_history_groups_fingerprints_and_preserves_removed_scenarios(board):
    """I1、D1；SOP-04-P01、SOP-05-P01、SOP-05-N01：旧内容与移除历史仍可查看。"""
    pushed, _ = push(board)
    assert board.request("POST", BASE + "/rounds", round_body(pushed, upload(board)))[0] == 200
    assert board.request("POST", BASE + "/marks", mark_body(pushed))[0] == 200
    detail = page(board, WEB + "/s/" + IDS[0])
    assert "N1-P01" in detail.text and "自定义入口" in detail.text
    assert "初始" in detail.text and "预期结果" in detail.text
    group = detail.one("section", **{"data-fingerprint": pushed["fingerprints"][IDS[0]]})
    assert "Li Si" in group.text and "Tester A" in group.text
    assert len(group.find_all("article", **{"class": "result"})) == 2
    doc = demo()
    doc["sops"][0]["scenarios"][0]["expected"] = ["修改后的业务预期"]
    changed, _ = push(board, push_body(doc))
    detail = page(board, WEB + "/s/" + IDS[0])
    old = detail.one("section", **{"data-fingerprint": pushed["fingerprints"][IDS[0]]})
    current = detail.one("section", **{"data-fingerprint": changed["fingerprints"][IDS[0]]})
    assert "场景已变更" in old.text
    assert "Li Si" in old.text and "Tester A" in old.text
    assert "修改后的业务预期" in current.text and "尚无执行结果" in current.text
    assert "已从场景文件移除" not in detail.text
    doc["sops"].pop(0)
    push(board, push_body(doc))
    archived = page(board, WEB + "/s/" + IDS[0])
    assert "已从场景文件移除" in archived.text
    assert "Li Si" in archived.text and "Tester A" in archived.text
    assert "修改后的业务预期" in archived.text
    assert board.sql("SELECT COUNT(*) FROM results") == [(5,)]


def test_image_evidence_is_displayed_inline(board):
    """I1：图片证据以同源图片直接预览。"""
    pushed, _ = push(board)
    evidence = upload(board, b"\x89PNG\r\n\x1a\nexample", "截图.png")
    body = round_body(pushed, evidence)
    assert board.request("POST", BASE + "/rounds", body)[0] == 200
    detail = page(board, WEB + "/s/" + IDS[0])
    image = detail.one("img", src="/api/v1/evidence/" + evidence["sha256"])
    assert image.attrs["alt"] == "截图.png"


def test_text_evidence_shows_only_first_200_lines_and_escapes_markup(board):
    """I1：文本只预览前 200 行，并按文字显示可能包含的 HTML。"""
    pushed, _ = push(board)
    lines = [f"LINE-{i:03d} <script>untrusted</script>" for i in range(1, 206)]
    evidence = upload(board, ("\r\n".join(lines) + "\r\n").encode(), "记录.txt")
    assert board.request("POST", BASE + "/rounds", round_body(pushed, evidence))[0] == 200
    detail = page(board, WEB + "/s/" + IDS[0])
    preview = detail.one("pre", **{"data-evidence": evidence["sha256"]})
    assert preview.text.splitlines() == lines[:200]
    assert not preview.find_all("script")
    assert "LINE-201" not in detail.text


def test_binary_evidence_has_download_link_and_original_bytes(board):
    """I1、I3：其他证据提供原始字节下载。"""
    pushed, _ = push(board)
    raw = b"\x00\xffbinary evidence"
    evidence = upload(board, raw, "附件.bin")
    assert board.request("POST", BASE + "/rounds", round_body(pushed, evidence))[0] == 200
    detail = page(board, WEB + "/s/" + IDS[0])
    link = detail.one("a", href="/api/v1/evidence/" + evidence["sha256"])
    assert "download" in link.attrs and "附件.bin" in link.text
    assert board.request("GET", link.attrs["href"], auth=False)[1] == raw


def test_mark_form_identifies_current_scenario_and_requires_identity_actual_environment(board):
    """I1；SOP-04-P01：表单提交人工标记，身份与当前内容明确。"""
    pushed, _ = push(board)
    detail = page(board, WEB + "/s/" + IDS[0])
    form = detail.one("form", id="mark-form")
    assert form.attrs["action"] == BASE + "/marks"
    assert form.attrs["method"] == "post"
    assert form.one("input", name="scenario").attrs["value"] == IDS[0]
    assert form.one("input", name="fingerprint").attrs["value"] == pushed["fingerprints"][IDS[0]]
    for name in ("marker", "token", "environment"):
        assert "required" in form.one("input", name=name).attrs
    assert form.one("input", name="token").attrs["type"] == "password"
    assert "required" in form.one("textarea", name="actual").attrs
    assert [option.attrs["value"] for option in form.one("select", name="status").find_all("option")] == ["pass", "fail", "blocked", "skip"]
    assert form.one("input", name="defect_url").attrs["type"] == "url"
    assert form.one("button", id="save-identity").text == "保存标记人与令牌"
    assert detail.one("p", id="mark-message").attrs["role"] == "status"


def test_styles_and_script_are_served_from_packaged_same_origin_assets(board):
    """I1；SOP-06-P01：样式与原生脚本随包提供，不加载外网资源。"""
    doc = page(board, "/")
    css = doc.one("link", rel="stylesheet").attrs["href"]
    script = doc.one("script").attrs
    assert css == "/assets/board.css" and script["src"] == "/assets/board.js"
    assert "defer" in script
    for url, content_type in [(css, "text/css"), (script["src"], "text/javascript")]:
        status, raw, headers = board.request("GET", url, auth=False)
        assert status == 200 and raw
        assert headers["Content-Type"].startswith(content_type)
        assert b"https://" not in raw and b"http://" not in raw
    assert board.request("GET", "/assets/../cli.py", auth=False)[0] == 404


def test_removed_scenario_has_no_form_for_obsolete_content(board):
    """I1、I2；SOP-05-N01：移除后保留详情，只对当前场景提供标记。"""
    push(board)
    doc = demo()
    doc["sops"].pop(0)
    push(board, push_body(doc))
    archived = page(board, WEB + "/s/" + IDS[0])
    assert "已从场景文件移除" in archived.text
    assert not archived.find_all("form")


def test_mark_form_http_rejection_then_success_updates_page_without_rewriting_agent(board):
    """I1、I2、D2；SOP-04-P01、SOP-04-N01、SOP-04-N02：真实接口拒绝与保存。"""
    pushed, _ = push(board)
    assert board.request("POST", BASE + "/rounds", round_body(pushed, upload(board)))[0] == 200
    form = page(board, WEB + "/s/" + IDS[2]).one("form", id="mark-form")
    body = dict(scenario=form.one("input", name="scenario").attrs["value"],
                fingerprint=form.one("input", name="fingerprint").attrs["value"],
                status="fail", actual="人工发现不符", marker="Tester A", environment="本机测试")
    before = snapshot(board)
    for defect in (None, "https://example.com/bugs/1"):
        if defect:
            body["defect_url"] = defect
        assert_error(board.request("POST", form.attrs["action"], body), 422, "defect-link-required")
        assert snapshot(board) == before
    body["defect_url"] = "https://github.com/demo-org/demo-app/issues/1"
    assert_error(board.request("POST", form.attrs["action"], body, auth=False,
                 headers={"Authorization": "Bearer wrong-local-value"}), 401, "unauthorized")
    assert snapshot(board) == before
    response = board.request("POST", form.attrs["action"], body)
    assert response[0] == 200 and response[1]["round"] == 2
    detail = page(board, WEB + "/s/" + IDS[2])
    assert "人工发现不符" in detail.text and "Li Si" in detail.text and "Tester A" in detail.text
    assert len(detail.find_all("article", **{"class": "result"})) == 2
    summary = page(board, WEB)
    assert summary.one("div", id="metric-manual_pass_rate").one("dd").text == "0/1"
    assert summary.one("span", id="misjudgment-count").text == "1"


def test_untrusted_page_text_is_escaped_and_active_evidence_is_sandboxed(board):
    """I1：页面文字不执行脚本，原始 HTML 附件不能读取页面保存的令牌。"""
    dangerous = "<script>untrusted()</script>"
    doc = demo()
    doc["sops"][0]["scenarios"][0]["title"] = dangerous
    doc["sops"][0]["scenarios"][0]["expected"] = [dangerous]
    pushed, _ = push(board, push_body(doc, project=dict(id="github.com/demo-org/demo-app", name=dangerous)))
    evidence = upload(board, dangerous.encode(), "untrusted.html")
    assert board.request("POST", BASE + "/rounds", round_body(pushed, evidence))[0] == 200
    for url in ("/", WEB + "/s/" + IDS[0]):
        rendered = page(board, url)
        assert dangerous in rendered.text
        assert all(s.attrs.get("src") == "/assets/board.js" for s in rendered.find_all("script"))
    status, raw, headers = board.request("GET", "/api/v1/evidence/" + evidence["sha256"], auth=False)
    assert status == 200 and raw == dangerous.encode()
    assert "sandbox" in headers.get("Content-Security-Policy", "")
    assert "allow-scripts" not in headers["Content-Security-Policy"]
