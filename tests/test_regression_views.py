"""SOP-BOARD-1 场景版本 4：页面可见结果和轮次导出。"""

from io import BytesIO
import json
import re
from zipfile import ZipFile

from helpers import BASE, IDS, demo, push, push_body, round_body, upload
from html_helpers import page
from test_marks import mark_body
from test_pages import WEB
from test_reads import get
from test_statistics import assert_ratio


def assert_same_origin_resources(board, document):
    """核对 HTML 加载的资源与打包样式、脚本中均无外网地址。"""
    for tag, field in (("script", "src"), ("img", "src"), ("link", "href"),
                       ("iframe", "src"), ("source", "src"), ("object", "data")):
        for element in document.find_all(tag):
            url = element.attrs.get(field)
            if url is None:
                continue
            assert url.startswith("/") and not url.startswith("//"), url
            status, raw, _ = board.request("GET", url, auth=False)
            assert status == 200
            if tag in ("script", "link"):
                assert b"https://" not in raw and b"http://" not in raw


def test_detail_content_history_and_three_evidence_previews(board):
    """SOP-06-P04：详情正文、指纹历史、图片、200 行文本与 ZIP 原文下载。"""
    doc = demo()
    scenario = doc["sops"][0]["scenarios"][0]
    scenario["forbidden_effects"] = ["禁止丢失原始数据"]
    pushed, _ = push(board, push_body(doc))
    image = upload(board, b"\x89PNG\r\n\x1a\nimage bytes", "截图.png")
    lines = [f"TEXT-LINE-{i:03d}" for i in range(1, 301)]
    text = upload(board, ("\n".join(lines) + "\n").encode(), "300-lines.txt")
    output = BytesIO()
    with ZipFile(output, "w") as archive:
        archive.writestr("original.txt", b"original zip contents")
    raw_zip = output.getvalue()
    binary = upload(board, raw_zip, "附件.zip")
    body = round_body(pushed, image, statuses=("pass",))
    body["results"][0]["evidence"] = [image, text, binary]
    assert board.request("POST", BASE + "/rounds", body)[0] == 200
    detail = page(board, WEB + "/s/" + IDS[0])
    assert "验证入口：" + scenario["verify"] in detail.text
    group = detail.one("section", **{"data-fingerprint": pushed["fingerprints"][IDS[0]]})
    assert "当前内容" in group.text
    for field, label in (("initial_state", "初始状态"), ("steps", "操作步骤"),
                         ("expected", "预期结果"), ("forbidden_effects", "禁止产生的影响")):
        assert label in group.text
        assert all(value in group.text for value in scenario[field])
    assert "第 1 轮 · Agent · 通过" in group.one("article", **{"class": "result"}).text
    assert detail.one("img", src="/api/v1/evidence/" + image["sha256"]).attrs["alt"] == "截图.png"
    assert detail.one("pre", **{"data-evidence": text["sha256"]}).text.splitlines() == lines[:200]
    assert all(line not in detail.text for line in lines[200:])
    link = detail.one("a", href="/api/v1/evidence/" + binary["sha256"])
    assert "download" in link.attrs
    assert get(board, link.attrs["href"]) == raw_zip
    assert_same_origin_resources(board, detail)


def test_empty_then_blocked_and_skip_page_rates_and_progress(board):
    """SOP-06-P05：空样本显示无数据，受阻与跳过不进入通过率分母。"""
    pushed, _ = push(board)
    overview, summary = page(board, WEB), get(board, BASE)
    for key in ("agent_pass_rate", "manual_pass_rate", "retest_coverage", "first_pass_rate"):
        assert overview.one("div", id="metric-" + key).one("dd").text == "无数据"
        assert_ratio(summary["statistics"][key], 0, 0)
    assert overview.one("div", id="metric-executed_progress").one("dd").text == "0/4"
    assert_ratio(summary["progress"]["executed"], 0, 4)
    body = round_body(pushed, upload(board), statuses=("pass", "blocked", "skip"))
    assert board.request("POST", BASE + "/rounds", body)[0] == 200
    overview, summary = page(board, WEB), get(board, BASE)
    for key in ("agent_pass_rate", "first_pass_rate"):
        assert overview.one("div", id="metric-" + key).one("dd").text == "1/1"
        assert_ratio(summary["statistics"][key], 1, 1)
    assert overview.one("div", id="metric-executed_progress").one("dd").text == "3/4"
    assert_ratio(summary["progress"]["executed"], 3, 4)
    assert overview.one("tr", **{"data-round": "1"}).one("td", **{"data-field": "pass-rate"}).text == "1/3"
    assert_ratio(summary["rounds"][0]["pass_rate"], 1, 3)
    assert summary["queues"]["issues"] == [IDS[1], IDS[2]]
    assert [a.attrs["data-scenario"] for a in overview.one("section", id="queue-issues").find_all("a")] == [IDS[1], IDS[2]]


def test_home_link_queues_round_kinds_and_misjudgment_details(board):
    """SOP-06-P07：从首页进入总览，核对队列、两轮种类和误判四项内容。"""
    pushed, _ = push(board)
    assert board.request("POST", BASE + "/rounds", round_body(pushed, upload(board)))[0] == 200
    link = "https://github.com/demo-org/demo-app/issues/5"
    body = mark_body(pushed, "fail", scenario=IDS[2], fingerprint=pushed["fingerprints"][IDS[2]], defect_url=link)
    assert board.request("POST", BASE + "/marks", body)[0] == 200
    home = page(board, "/")
    assert "演示项目" in home.text
    batch_link = home.one("a", href=WEB)
    assert batch_link.text == "demo"
    overview = page(board, batch_link.attrs["href"])
    for name, expected in (("retest", [IDS[0]]), ("issues", [IDS[1], IDS[2], IDS[3]]), ("pending", [])):
        section = overview.one("section", id="queue-" + name)
        assert [a.attrs["data-scenario"] for a in section.find_all("a")] == expected
    for number, kind, rate in ((1, "Agent", "2/4"), (2, "人工", "0/1")):
        row = overview.one("tr", **{"data-round": str(number)})
        assert row.one("th").text == f"第 {number} 轮"
        assert row.find_all("td")[0].text == kind
        assert row.one("td", **{"data-field": "pass-rate"}).text == rate
    findings = overview.one("section", id="misjudgments")
    assert findings.one("span", id="misjudgment-count").text == "1"
    row = findings.one("tbody").one("tr")
    assert [cell.text for cell in row.find_all("td")] == [IDS[2], link, "Li Si", "Tester A"]
    assert row.one("a", href=link).text == link
    assert_same_origin_resources(board, home)
    assert_same_origin_resources(board, overview)


def test_shanghai_html_times_and_original_api_export_values(board_factory):
    """SOP-06-P08：以服务端 HTML 文本代替两种浏览器时区，核对上海时间与原值。"""
    board = board_factory(env={"TZ": "Asia/Shanghai"})
    pushed, _ = push(board)
    body = round_body(pushed, upload(board))
    body["results"] = [body["results"][0], body["results"][2]]
    times = [("2026-10-18T16:30:05.123456+00:00", "2026-10-19 00:30:05 +08:00"),
             ("2026-10-18T20:00:00-05:00", "2026-10-19 09:00:00 +08:00")]
    for row, (original, _) in zip(body["results"], times):
        row["executed_at"] = original
    assert board.request("POST", BASE + "/rounds", body)[0] == 200
    for row, (original, literal) in zip(body["results"], times):
        detail = page(board, WEB + "/s/" + row["scenario"])
        group = detail.one("section", **{"data-fingerprint": pushed["fingerprints"][row["scenario"]]})
        assert group.one("time", datetime=original).text == literal
        for element in detail.find_all("time"):
            assert re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} \+08:00", element.text)
    expected = [row["executed_at"] for row in body["results"]]
    assert [row["executed_at"] for row in get(board, BASE + "/rounds/1")["results"]] == expected
    raw = get(board, BASE + "/rounds/1/export")
    with ZipFile(BytesIO(raw)) as archive:
        assert [json.loads(line)["executed_at"] for line in archive.read("results.jsonl").splitlines()] == expected


def test_export_omits_absent_optional_fields_and_preserves_evidence_and_times(board):
    """SOP-06-P10：导出省略未提供的可选字段与空值，保持证据和时间原文。"""
    pushed, _ = push(board)
    raw_evidence = b"unchanged export evidence\x00\xff"
    evidence = upload(board, raw_evidence, "original.bin")
    body = round_body(pushed, evidence)
    body["results"] = [body["results"][0], body["results"][3]]
    body["results"][0].pop("finding")
    body["results"][0].update(session="session-one", note="备注原文")
    assert board.request("POST", BASE + "/rounds", body)[0] == 200
    with ZipFile(BytesIO(get(board, BASE + "/rounds/1/export"))) as archive:
        rows = [json.loads(line) for line in archive.read("results.jsonl").splitlines()]
        assert len(rows) == 2
        assert rows[0]["session"] == "session-one" and rows[0]["note"] == "备注原文"
        assert "finding" not in rows[0]
        assert rows[1]["finding"] == body["results"][1]["finding"]
        assert "session" not in rows[1] and "note" not in rows[1]
        for row, original in zip(rows, body["results"]):
            assert all(value is not None for value in row.values())
            assert row["executed_at"] == original["executed_at"]
            assert row["evidence"]
            for path in row["evidence"]:
                assert path in archive.namelist()
                assert archive.read(path) == raw_evidence
