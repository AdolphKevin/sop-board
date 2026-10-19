"""I6：测试岗结果文件的真实接口导出、导入及事务边界。"""

from copy import deepcopy
from datetime import datetime
import json
from zoneinfo import ZoneInfo

import pytest
import yaml

from cases_helpers import CASES_BASE, upload_cases
from helpers import BASE, assert_error, push, push_body, snapshot, upload
from html_helpers import page
from progress_helpers import P5_API, P5_IDS, P5_WEB, flow_cases, flow_document, prepare_five
from review_helpers import seven_mark
from test_reads import get


def exported(board, base=P5_API):
    status, raw, headers = board.request("GET", base + "/results.yaml", auth=False)
    assert status == 200, raw
    assert headers["Content-Type"] == "text/yaml; charset=utf-8"
    assert "attachment" in headers["Content-Disposition"]
    return yaml.safe_load(raw)


def test_results_export_fields_local_run_time_case_intersection_and_current_latest_results(board_factory):
    """SOP-26-P02/P05/P07：固定数字、最新人工优先、本地时间及指纹失效。"""
    board = board_factory(env={"JIRA_URL": "https://acme.atlassian.net", "TZ": "Asia/Shanghai"})
    pushed = prepare_five(board, supplement=True)
    before = snapshot(board)
    start = datetime.now(ZoneInfo("Asia/Shanghai")).replace(microsecond=0)
    result = exported(board)
    end = datetime.now(ZoneInfo("Asia/Shanghai"))
    assert set(result) == {"run_id", "round", "environment", "results"}
    stamp = datetime.strptime(result["run_id"], "p5-%Y%m%d%H%M%S").replace(tzinfo=ZoneInfo("Asia/Shanghai"))
    assert start <= stamp <= end
    assert result["round"] == 3 and result["environment"] == "sop-board 导出；产品提交 c0ffee1"
    rows = result["results"]
    assert len(rows) == 3 and [r["case_id"] for r in rows] == P5_IDS[:3]
    manual_pass = get(board, P5_API + "/rounds/2")["results"][0]
    manual_fail = get(board, P5_API + "/rounds/3")["results"][0]
    assert rows[0] == dict(case_id=P5_IDS[0], status="passed", actual_result="人工复核通过", executor="张三", tested_at=manual_pass["executed_at"])
    assert rows[1] == dict(case_id=P5_IDS[1], status="failed", actual_result="人工复核不通过（失败），缺陷 https://acme.atlassian.net/browse/ABC-1",
                          executor="张三", tested_at=manual_fail["executed_at"], jira_issue="ABC-1", notes="复现两次")
    assert rows[2] == dict(case_id=P5_IDS[2], status="passed", actual_result="实际结果 " + P5_IDS[2], executor="Li Si", tested_at="2026-10-18T10:00:00+08:00")
    for row in rows:
        assert datetime.fromisoformat(row["tested_at"]).tzinfo is not None
    assert snapshot(board) == before
    document = yaml.safe_load(pushed["content"])
    document["sops"][0]["scenarios"][0]["expected"] = ["新预期"]
    assert board.request("PUT", P5_API + "/scenario-file", push_body(document))[0] == 200
    assert exported(board)["results"] == rows[1:]


def test_results_export_status_mapping_non_jira_notes_and_zero_round(board_factory):
    """SOP-26-P06、I6 空轮次：所有状态，备注与非 Jira 链接用分号分隔。"""
    board = board_factory(env={"JIRA_URL": "https://acme.atlassian.net"})
    ids = [f"TC_DEMO_FLOW_{i:03}" for i in range(4, 8)]
    doc = flow_document(ids, batch="map")
    pushed = push_body(doc)
    target = P5_API.replace("/p5", "/map")
    assert board.request("PUT", target + "/scenario-file", pushed)[0] == 200
    upload_cases(board, flow_cases(ids), base=target)
    assert exported(board, target)["round"] == 0 and exported(board, target)["results"] == []
    import hashlib
    evidence = upload(board)
    results = [dict(scenario=sid, fingerprint=pushed["fingerprints"][sid], status=status, actual="核对", executor="Li Si",
                    executed_at="2026-10-18T10:00:00+08:00", evidence=[evidence], finding="环境原因" if status in ("blocked", "skip") else "")
               for sid, status in zip(ids, ["fail", "blocked", "skip", "pass"])]
    body = dict(idempotency_key="map-agent", submitted_by="开发岗", product=dict(commit="c1", digest="product-1", dirty=False),
                scenario_file_sha256=hashlib.sha256(pushed["content"].encode()).hexdigest(), results=results)
    assert board.request("POST", target + "/rounds", body)[0] == 200
    link = "https://github.com/demo-org/demo-app/issues/7"
    assert board.request("POST", target + "/marks", seven_mark(pushed, ids[3], "fail", defect_url=link))[0] == 200
    rows = exported(board, target)["results"]
    assert [r["status"] for r in rows] == ["failed", "blocked", "skipped", "failed"]
    assert rows[-1]["notes"] == "缺陷链接 " + link and "jira_issue" not in rows[-1]
    assert board.request("POST", target + "/marks", seven_mark(pushed, ids[3], "fail", defect_url=link, note="另附说明"))[0] == 200
    assert exported(board, target)["results"][-1]["notes"] == "缺陷链接 " + link + "；另附说明"
    upload_cases(board)
    empty = exported(board, CASES_BASE)
    assert empty["round"] == 0 and empty["results"] == []
    assert empty["environment"] == "sop-board 导出；产品提交 无"


def import_document(ids=P5_IDS[:3], *, naive=False):
    return dict(run_id="r-1", round=1, environment="测试环境 v2", results=[
        dict(case_id=sid, status=status, actual_result="原文实际结果 " + sid, executor="王五",
             tested_at="2026-10-19T10:00:00" + ("" if naive else "+08:00"),
             **(dict(jira_issue="ABC-7", notes="必现", duration_ms=120, evidence=["local-evidence.txt"]) if status == "failed" else {}))
        for sid, status in zip(ids, ["passed", "failed", "skipped"])])


def imported(board, document, *, base=P5_API, uploaded_by="张三", auth=True):
    return board.request("POST", base + "/results", dict(content=yaml.safe_dump(document, allow_unicode=True, sort_keys=False), uploaded_by=uploaded_by), auth=auth)


@pytest.mark.parametrize("naive", [False, True])
def test_import_manual_round_preserves_source_fields_local_time_evidence_and_misjudgment(board_factory, naive):
    """SOP-26-P03/P08：执行者/实际结果不取上传人，证据含上传版本及每项原文。"""
    board = board_factory(env={"JIRA_URL": "https://acme.atlassian.net", "TZ": "Asia/Shanghai"})
    prepare_five(board)
    doc = import_document(naive=naive)
    status, result, _ = imported(board, doc)
    assert status == 200, result
    assert result == dict(ok=True, round=4, imported=2, skipped=[P5_IDS[2]])
    stored = get(board, P5_API + "/rounds/4")
    assert stored["kind"] == "manual" and stored["submitted_by"] == "张三"
    assert len(stored["results"]) == 2
    for index, row in enumerate(stored["results"]):
        assert row["scenario"] == P5_IDS[index] and row["executor"] == "王五"
        assert row["executed_at"] == "2026-10-19T10:00:00+08:00"
        assert row["actual"] == doc["results"][index]["actual_result"]
        assert row["independence"] == "undeclared"
        assert len(row["evidence"]) == 1 and row["evidence"][0]["name"] == "import-4.json"
        evidence = board.request("GET", "/api/v1/evidence/" + row["evidence"][0]["sha256"])[1]
        if isinstance(evidence, bytes):
            evidence = json.loads(evidence)
        assert evidence == dict(uploaded_by="张三", run_id="r-1", round=1, environment="测试环境 v2",
                                latest_upload=1, result=doc["results"][index])
    assert stored["results"][1]["finding"] == "https://acme.atlassian.net/browse/ABC-7"
    assert stored["results"][1]["note"] == "必现"
    assert board.sql("SELECT scenario,executor,executed_at FROM results WHERE round_id=(SELECT id FROM rounds WHERE number=4)") == [
        (P5_IDS[0], "王五", "2026-10-19T10:00:00+08:00"), (P5_IDS[1], "王五", "2026-10-19T10:00:00+08:00")]
    detail = page(board, P5_WEB + "/s/" + P5_IDS[0])
    assert "2026-10-19 10:00:00 +08:00" in detail.text
    summary = get(board, P5_API)
    assert summary["misjudgments"][-1] == dict(scenario=P5_IDS[1], round=4, defect_url="https://acme.atlassian.net/browse/ABC-7", agent_executor="Li Si", marker="王五")
    root = page(board, P5_WEB + "/scenarios")
    assert "已复核通过" in root.one("article", **{"data-scenario": P5_IDS[0]}).text
    assert "所在队列：问题" in root.one("article", **{"data-scenario": P5_IDS[1]}).text
    assert "TC_DEMO_FLOW_003" not in [r["scenario"] for r in stored["results"]]


def test_invalid_import_collects_five_problems_and_rolls_back_every_row_and_file(board):
    """SOP-26-N01：五处问题同时返回，首条合格结果也不能落库。"""
    ids = ["TC_DEMO_LOGIN_001", "TC_DEMO_LOGIN_002", "TC_DEMO_ORDER_001", "TC_DEMO_ORDER_002"]
    target = BASE
    assert board.request("PUT", target + "/scenario-file", push_body(flow_document(ids, "demo")))[0] == 200
    doc = import_document(ids[:3])
    doc.pop("environment")
    doc["results"][1].pop("jira_issue")
    doc["results"][2]["status"] = "blocked"
    doc["results"].extend([{**doc["results"][0], "case_id": ids[3]}, {**doc["results"][0], "case_id": "TC_DEMO_NONE_001"}])
    doc["results"][3].pop("actual_result")
    before = snapshot(board)
    problems = assert_error(imported(board, doc, base=target), 422, "results-invalid")
    assert {p["field"] for p in problems} == {"environment", "results[1].jira_issue", "results[2].jira_issue", "results[3].actual_result", "results[4].case_id"}
    assert len(problems) == 5
    assert "TC_DEMO_NONE_001" in str(problems)
    assert snapshot(board) == before


def test_import_required_top_fields_unconfigured_jira_auth_and_all_skipped_are_atomic(board):
    """SOP-26-N03、I6：逐项缺字段、Jira 单号无配置、鉴权、全部跳过。"""
    assert board.request("PUT", P5_API + "/scenario-file", push_body(flow_document()))[0] == 200
    doc = import_document()
    before = snapshot(board)
    missing = {k: v for k, v in doc.items() if k not in ("round", "environment")}
    missing["results"] = [doc["results"][0]]
    problems = assert_error(imported(board, missing), 422, "results-invalid")
    assert {p["field"] for p in problems} == {"round", "environment"}
    problems = assert_error(imported(board, doc), 422, "results-invalid")
    assert [p["field"] for p in problems] == ["results[1].jira_issue"]
    assert "完整链接" in problems[0]["problem"]
    assert_error(imported(board, doc, auth=False), 401, "unauthorized")
    assert snapshot(board) == before
    doc["results"] = [doc["results"][2]]
    assert imported(board, doc)[:2] == (200, dict(ok=True, round=None, imported=0, skipped=[P5_IDS[2]]))
    assert snapshot(board) == before


@pytest.mark.parametrize("field,value", [
    ("run_id", " "), ("round", True), ("round", -1), ("environment", []), ("results", {}),
    ("results[0]", None), ("results[0].case_id", []), ("results[0].status", "pass"),
    ("results[0].status", []), ("results[0].actual_result", ""), ("results[0].executor", 4),
    ("results[0].tested_at", "2026-99-01T00:00:00"), ("results[0].tested_at", "2026-10-19"),
    ("results[0].notes", {}), ("results[0].duration_ms", -1), ("results[0].evidence", [4]),
    ("results[0].jira_issue", ""), ("results[0].extra", "unknown"), ("extra", "unknown"),
])
def test_import_invalid_field_types_report_field_without_side_effects(board, field, value):
    """SOP-26-N01/N03：格式及类型逐项核对，不留下轮次或证据。"""
    assert board.request("PUT", P5_API + "/scenario-file", push_body(flow_document()))[0] == 200
    doc = import_document()
    doc["results"] = doc["results"][:1]
    if field == "results[0]":
        doc["results"][0] = value
    elif field.startswith("results[0]."):
        doc["results"][0][field.split(".", 1)[1]] = value
    else:
        doc[field] = value
    before = snapshot(board)
    problems = assert_error(imported(board, doc), 422, "results-invalid")
    assert field in [p["field"] for p in problems]
    assert snapshot(board) == before


@pytest.mark.parametrize("content", ["", "[]", "run_id: [", "run_id: x\nrun_id: y", "run_id: &a [*a]"])
def test_import_malformed_or_ambiguous_yaml_is_rejected_without_data(board, content):
    """SOP-26-N01：空文件、错误 YAML、重复字段及循环别名均被拒绝。"""
    push(board)
    before = snapshot(board)
    assert_error(board.request("POST", BASE + "/results", dict(content=content, uploaded_by="张三")), 422, "results-invalid")
    assert snapshot(board) == before


def test_import_duplicate_case_and_unknown_skipped_are_rejected_together(board):
    """SOP-26-N01：跳过项仍需核对场景，重复编号不能写半轮。"""
    assert board.request("PUT", P5_API + "/scenario-file", push_body(flow_document()))[0] == 200
    doc = import_document()
    doc["results"] = [doc["results"][0], deepcopy(doc["results"][0]), {**doc["results"][2], "case_id": "TC_DEMO_NONE_001"}]
    before = snapshot(board)
    problems = assert_error(imported(board, doc), 422, "results-invalid")
    assert {p["field"] for p in problems} == {"results[1].case_id", "results[2].case_id"}
    assert snapshot(board) == before


def test_import_second_round_keeps_latest_union_upload_and_raw_unquoted_timestamp(board_factory):
    """SOP-26-P03/P08：第 2 轮导入证据用当前上传序号，保留原始未带时区时间。"""
    import hashlib
    board = board_factory(env={"JIRA_URL": "https://acme.atlassian.net", "TZ": "Asia/Shanghai"})
    target = P5_API.replace("/p5", "/imp")
    doc = flow_document(P5_IDS[:3], batch="imp")
    pushed = push_body(doc)
    assert board.request("PUT", target + "/scenario-file", pushed)[0] == 200
    evidence = upload(board)
    rows = [dict(scenario=sid, fingerprint=pushed["fingerprints"][sid], status="pass", actual="Agent 实际结果", executor="Li Si",
                 executed_at="2026-10-18T10:00:00+08:00", evidence=[evidence]) for sid in P5_IDS[:3]]
    assert board.request("POST", target + "/rounds", dict(idempotency_key="imp", submitted_by="开发岗", results=rows,
        product=dict(commit="c1", digest="product-1", dirty=False), scenario_file_sha256=hashlib.sha256(pushed["content"].encode()).hexdigest()))[0] == 200
    upload_cases(board, flow_cases(P5_IDS[:3]), base=target)
    original = import_document(naive=True)
    content = yaml.safe_dump(original, allow_unicode=True).replace("'2026-10-19T10:00:00'", "2026-10-19T10:00:00")
    assert "tested_at: 2026-10-19T10:00:00" in content
    response = board.request("POST", target + "/results", dict(content=content, uploaded_by="张三"))
    assert response[:2] == (200, dict(ok=True, round=2, imported=2, skipped=[P5_IDS[2]]))
    results = get(board, target + "/rounds/2")["results"]
    for index, row in enumerate(results):
        assert row["evidence"][0]["name"] == "import-2.json"
        record = board.request("GET", "/api/v1/evidence/" + row["evidence"][0]["sha256"])[1]
        assert record == dict(uploaded_by="张三", run_id="r-1", round=1, environment="测试环境 v2", latest_upload=1, result=original["results"][index])
    changed_cases = flow_cases(P5_IDS[:3])
    changed_cases["content"] += "\n"
    assert upload_cases(board, changed_cases, base=target)["upload"] == 2
    result = imported(board, original, base=target)
    assert result[1]["round"] == 3
    sha = get(board, target + "/rounds/3")["results"][0]["evidence"][0]["sha256"]
    assert board.request("GET", "/api/v1/evidence/" + sha)[1]["latest_upload"] == 2
