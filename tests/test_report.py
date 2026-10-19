"""I5：报告区块、数据与稳定导出，经真实服务核对。"""

import re

from cases_helpers import CASES_BASE, CASE_IDS, upload_cases
from helpers import BASE, push, push_body, snapshot
from html_helpers import page
from progress_helpers import P5_API, P5_WEB, prepare_five
from test_reads import get


def report(board, base):
    status, raw, headers = board.request("GET", base + "/report.md", auth=False)
    assert status == 200, raw
    assert headers["Content-Type"] == "text/markdown; charset=utf-8"
    assert "attachment" in headers["Content-Disposition"]
    return raw.decode()


def test_report_sections_five_scenarios_and_all_metrics_are_independent_literals(board_factory):
    """SOP-26-P01/P04：区块次序、全部字段、误判、受阻原因与独立分数。"""
    board = board_factory(env={"JIRA_URL": "https://acme.atlassian.net"})
    prepare_five(board)
    before = snapshot(board)
    first = report(board, P5_API)
    assert re.findall(r"^## (.+)$", first, re.M) == ["头部", "用例覆盖", "Agent 统计", "人工复测", "修复记录", "回归覆盖", "合并记录", "未验证范围"]
    for field in ["项目：演示项目（github.com/demo-org/demo-app）", "批次：p5", "场景版本：1", "推送提交号：c0ffee1",
                  "用例并集的最新上传序号：1", "批次产品指纹：D1", "生成时间："]:
        assert field in first
    assert "| TC_DEMO_FLOW_001 | 用例 TC_DEMO_FLOW_001 | TC_DEMO_FLOW_001 | 通过 | 通过 |" in first
    assert "| TC_DEMO_FLOW_002 | 用例 TC_DEMO_FLOW_002 | TC_DEMO_FLOW_002 | 通过 | 不通过（失败） |" in first
    assert "| TC_DEMO_FLOW_003 | 用例 TC_DEMO_FLOW_003 | TC_DEMO_FLOW_003 | 通过 | 尚无人工结果 |" in first
    unmatched = first.split("### 尚无对应场景\n", 1)[1].split("## Agent 统计", 1)[0]
    assert "TC_DEMO_FLOW_009" in unmatched
    for text in ["用例覆盖：3/4", "Agent 验收通过率：3/4", "一次通过率：3/4", "人工复测通过率：1/2", "复测覆盖率：2/3",
                 "| 第 1 轮 | Agent | 3/5 |", "| 第 2 轮 | 人工 | 1/1 |", "| 第 3 轮 | 人工 | 0/1 |",
                 "| TC_DEMO_FLOW_002 | https://acme.atlassian.net/browse/ABC-1 | Li Si | 张三 |",
                 "| TC_DEMO_FLOW_005 | 受阻 | 测试环境不可用 |"]:
        assert text in first
    assert first.count("暂无记录") == 3
    link = page(board, P5_WEB).one("a", href=P5_API + "/report.md")
    assert link.text == "导出报告"
    second = report(board, P5_API)
    assert re.sub(r"生成时间：[^\n]+", "生成时间：", first) == re.sub(r"生成时间：[^\n]+", "生成时间：", second)
    assert snapshot(board) == before
    # All observable numbers are also checked against the public data and progress page.
    summary = get(board, P5_API)
    for key, n, d in [("agent_pass_rate", 3, 4), ("first_pass_rate", 3, 4), ("manual_pass_rate", 1, 2), ("retest_coverage", 2, 3)]:
        assert summary["statistics"][key]["numerator"] == n and summary["statistics"][key]["denominator"] == d
        if key != "retest_coverage":
            assert page(board, P5_WEB + "/progress").one(id="metric-" + key).one("dd").text == f"{n}/{d}"


def test_report_cases_only_and_scenarios_only_write_absent_values_and_no_data(board):
    """SOP-26-N02：缺失头字段写无，空分母写无数据，未配对用例单列。"""
    push(board)
    upload_cases(board)
    text = report(board, CASES_BASE)
    for field in ["场景版本：无", "推送提交号：无", "批次产品指纹：无", "用例并集的最新上传序号：1"]:
        assert field in text
    unmatched = text.split("### 尚无对应场景\n", 1)[1].split("## Agent 统计", 1)[0]
    assert all(sid in unmatched for sid in CASE_IDS)
    for field in ["Agent 验收通过率", "一次通过率", "人工复测通过率", "复测覆盖率"]:
        assert field + "：无数据" in text
    text = report(board, BASE)
    assert "用例并集的最新上传序号：无" in text
    assert "用例覆盖：无数据" in text
    assert "## 未验证范围\n\n无" in text


def test_report_login_cases_and_skipped_scope_are_listed_with_latest_results(board_factory):
    """SOP-26-P01：两条登录用例配对，订单用例单列，跳过原因也进入未验证范围。"""
    import hashlib
    from cases_helpers import CASE_IDS
    from helpers import upload
    from progress_helpers import flow_document
    from review_helpers import seven_mark
    board = board_factory(env={"JIRA_URL": "https://acme.atlassian.net"})
    ids = CASE_IDS[:2] + ["N2-P01", "N2-N01"]
    pushed = push_body(flow_document(ids, batch="demo"))
    push(board, pushed)
    evidence = upload(board)
    results = [dict(scenario=sid, fingerprint=pushed["fingerprints"][sid], status=status, actual="实际结果", executor="Li Si",
                    executed_at="2026-10-18T10:00:00+08:00", evidence=[evidence], finding="依赖未部署" if status == "skip" else "")
               for sid, status in zip(ids, ["pass", "pass", "pass", "skip"])]
    assert board.request("POST", BASE + "/rounds", dict(idempotency_key="login", submitted_by="开发岗", results=results,
        product=dict(commit="c1", digest="product-1", dirty=False), scenario_file_sha256=hashlib.sha256(pushed["content"].encode()).hexdigest()))[0] == 200
    assert board.request("POST", BASE + "/marks", seven_mark(pushed, ids[1], "fail", defect_url="ABC-1"))[0] == 200
    upload_cases(board, base=BASE)
    text = report(board, BASE)
    assert "TC_DEMO_LOGIN_001 | 登录成功 | TC_DEMO_LOGIN_001 | 通过 | 尚无人工结果" in text
    assert "TC_DEMO_LOGIN_002 | 登录失败 | TC_DEMO_LOGIN_002 | 通过 | 不通过（失败）" in text
    assert "TC_DEMO_ORDER_001" in text.split("### 尚无对应场景")[1]
    assert "| N2-N01 | 跳过 | 依赖未部署 |" in text
