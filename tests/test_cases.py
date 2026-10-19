"""SOP-BOARD-2 I3/D1：真实 HTTP、SQLite 与页面的用例协作。"""

import hashlib
import json
from copy import deepcopy

import yaml

from cases_helpers import CASES_BASE, CASE_IDS, cases_body, cases_document, upload_cases
from helpers import BASE, KEY, assert_error, demo, push, push_body, snapshot
from test_reads import get


def test_cases_upload_creates_only_batch_without_scenario_version(board):
    """SOP-23-P01：只凭合格用例建批次，不影响已推送的批次。"""
    push(board)
    original = board.sql("SELECT * FROM batches")
    body = cases_body()
    assert upload_cases(board, body) == dict(ok=True, upload=1, added=3, updated=0, unchanged=0, union_count=3)
    assert board.sql("SELECT * FROM batches WHERE name='demo'") == original
    assert board.sql("SELECT project,name,path,product_digest FROM batches WHERE name='cases-only'") == [
        (KEY, "cases-only", "", "")]
    assert board.sql("SELECT COUNT(*) FROM versions v JOIN batches b ON b.id=v.batch WHERE b.name='cases-only'") == [(0,)]
    rows = board.sql("SELECT number,filename,uploaded_by,sha256,metadata,case_count,added,updated FROM case_uploads")
    assert len(rows) == 1
    assert rows[0][:4] == (1, "cases-a.yaml", "张三", hashlib.sha256(body['content'].encode()).hexdigest())
    assert json.loads(rows[0][4]) == cases_document()["metadata"]
    assert rows[0][5:] == (3, 3, 0)
    assert [json.loads(row[0]) for row in board.sql("SELECT body FROM case_versions ORDER BY id")] == cases_document()["test_cases"]


def test_cases_union_last_upload_wins_download_roundtrip_and_raw_idempotency(board):
    """SOP-23-P02/P04：并集、完整覆盖、原文幂等及下载后再次上传。"""
    push(board)
    first = cases_body()
    upload_cases(board, first)
    before = snapshot(board)
    repeated = upload_cases(board, {**first, "filename": "renamed.yaml", "uploaded_by": "另一人"})
    assert repeated == dict(ok=True, upload=1, added=0, updated=0, unchanged=3, union_count=3, unchanged_upload=True)
    assert snapshot(board) == before
    document = cases_document()
    document["metadata"]["owner"] = "李四"
    document["test_cases"].pop(1)
    changed = document["test_cases"][0]
    changed.update(title="新登录标题", steps=["新的登录步骤"])
    del changed["auto"]
    new = deepcopy(document["test_cases"][1])
    new.update(id="TC_DEMO_ORDER_002", title="第二笔订单")
    document["test_cases"].append(new)
    assert upload_cases(board, {**cases_body(document), "filename": "cases-b.yaml"}) == dict(
        ok=True, upload=2, added=1, updated=1, unchanged=1, union_count=4)
    result = get(board, CASES_BASE + "/cases")
    expected = [changed, cases_document()["test_cases"][1], document["test_cases"][1], new]
    assert result["cases"] == expected
    assert result["coverage"] == dict(numerator=0, denominator=4, rate=0.0)
    assert [(u["upload"], u["filename"], u["uploaded_by"], u["case_count"], u["added"], u["updated"])
            for u in result["uploads"]] == [(1, "cases-a.yaml", "张三", 3, 3, 0), (2, "cases-b.yaml", "张三", 3, 1, 1)]
    status, raw, headers = board.request("GET", CASES_BASE + "/cases.yaml", auth=False)
    assert status == 200 and headers["Content-Type"].startswith("text/yaml")
    assert "attachment" in headers["Content-Disposition"]
    assert yaml.safe_load(raw) == dict(metadata=document["metadata"], test_cases=expected)
    assert upload_cases(board, dict(filename="union.yaml", uploaded_by="张三", content=raw.decode()),
                        base=CASES_BASE.replace("cases-only", "roundtrip"))["union_count"] == 4
    # 与最新原文比较；再次上传历史原文确实生成下一序号。
    assert upload_cases(board, first) == dict(ok=True, upload=3, added=0, updated=1, unchanged=2, union_count=4)
    assert upload_cases(board, {**first, "content": first["content"] + "\n"}) == dict(
        ok=True, upload=4, added=0, updated=0, unchanged=3, union_count=4)


def test_cases_coverage_uses_only_current_same_id_scenarios_and_zero_denominator(board):
    """SOP-23-P03：同编号覆盖，未上传时 rate=null，移除后不再覆盖。"""
    document = demo()
    document["sops"][0]["scenarios"][0]["id"] = CASE_IDS[0]
    push(board, push_body(document))
    assert get(board, BASE + "/cases") == dict(uploads=[], cases=[], coverage=dict(numerator=0, denominator=0, rate=None))
    upload_cases(board, base=BASE)
    assert get(board, BASE + "/cases")["coverage"] == dict(numerator=1, denominator=3, rate=1 / 3)
    document["sops"][0]["scenarios"][0]["id"] = "N1-P01"
    push(board, push_body(document))
    assert get(board, BASE + "/cases")["coverage"] == dict(numerator=0, denominator=3, rate=0.0)


def test_cases_auth_missing_project_and_invalid_upload_never_create_batch(board):
    """SOP-23-N02/N07：认证、项目检查与失败上传不建项目或批次。"""
    push(board)
    before = snapshot(board)
    assert_error(board.request("POST", CASES_BASE + "/cases", cases_body(), auth=False), 401, "unauthorized")
    assert_error(board.request("POST", CASES_BASE.replace(KEY, "no-such-project") + "/cases", cases_body()),
                 404, "project-not-found")
    bad = cases_body()
    bad["content"] = bad["content"].replace(CASE_IDS[0], "TC_1")
    assert_error(board.request("POST", CASES_BASE + "/cases", bad), 422, "cases-invalid")
    assert snapshot(board) == before
    assert [b["name"] for b in get(board, "/api/v1/projects")["projects"][0]["batches"]] == ["demo"]
    assert b"cases-only" not in get(board, "/")


def test_scenario_file_download_is_latest_raw_and_cases_only_is_not_found(board):
    """SOP-23-N03：下载最新场景原文字节；尚无场景有专用错误。"""
    push(board)
    upload_cases(board)
    assert_error(board.request("GET", CASES_BASE + "/scenario-file", auth=False), 404, "scenario-file-not-found")
    body = push_body(commit="c2")
    body["content"] += "# 第二次推送的原文\r\n"
    push(board, body)
    status, raw, headers = board.request("GET", BASE + "/scenario-file", auth=False)
    assert status == 200 and headers["Content-Type"].startswith("text/yaml")
    assert raw == body["content"].encode()
