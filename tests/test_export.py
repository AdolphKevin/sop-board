from io import BytesIO
import json
from zipfile import ZipFile

import pytest

from helpers import BASE, KEY, assert_error, push, round_body, snapshot, upload
from test_marks import mark_body
from html_helpers import page
from test_pages import WEB


def test_agent_round_export_contains_only_contract_fields_and_relative_evidence(board):
    """I3；SOP-06-P02：ZIP 文件清单、逐行字段、原始字节和相对路径。"""
    pushed, _ = push(board)
    raw_files = [("one.png", b"\x89PNG\r\nimage"), ("two.txt", "中文证据".encode()),
                 ("three.txt", b"third"), ("four.txt", b"blocked")]
    evidence = [upload(board, raw, name) for name, raw in raw_files]
    body = round_body(pushed, evidence[0])
    body["results"][0].update(session="independent-session", note="保留备注")
    for row, item in zip(body["results"], evidence):
        row["evidence"] = [item]
    assert board.request("POST", BASE + "/rounds", body)[0] == 200
    before = snapshot(board)
    status, raw, headers = board.request("GET", BASE + "/rounds/1/export", auth=False)
    assert status == 200, raw
    assert headers["Content-Type"] == "application/zip"
    assert "attachment" in headers["Content-Disposition"]
    with ZipFile(BytesIO(raw)) as archive:
        names = [f"evidence/{i}-{name}" for i, (name, _) in enumerate(raw_files, 1)]
        assert archive.namelist() == names + ["results.jsonl"]
        content = archive.read("results.jsonl")
        assert content.endswith(b"\n")
        rows = [json.loads(line) for line in content.decode().splitlines()]
        assert len(rows) == 4
        for row, expected, path, (_, original) in zip(rows, body["results"], names, raw_files):
            keys = {"scenario", "status", "actual", "evidence", "executed_at", "executor"}
            keys.update(k for k in ("session", "finding", "note") if k in expected)
            assert set(row) == keys
            assert row == {**{k: expected[k] for k in keys - {"evidence"}}, "evidence": [path]}
            assert archive.read(path) == original
    assert snapshot(board) == before


@pytest.mark.parametrize("path,code", [(BASE + "/rounds/99/export", "round-not-found"),
    (f"/api/v1/projects/{KEY}/batches/absent/rounds/1/export", "batch-not-found"),
    ("/api/v1/projects/absent/batches/demo/rounds/1/export", "project-not-found")])
def test_export_missing_resources_returns_specific_404(board, path, code):
    """I3；SOP-06-N01：不存在的轮次、批次与项目。"""
    push(board)
    assert_error(board.request("GET", path, auth=False), 404, code)


def test_manual_export_includes_generated_observation_and_page_link(board):
    """I1、I3；SOP-06-P02：人工观察记录随轮次导出，页面可下载。"""
    pushed, _ = push(board)
    assert board.request("POST", BASE + "/marks", mark_body(pushed))[0] == 200
    link = page(board, WEB).one("a", href=BASE + "/rounds/1/export")
    assert "导出" in link.text
    status, raw, _ = board.request("GET", link.attrs["href"], auth=False)
    assert status == 200
    with ZipFile(BytesIO(raw)) as archive:
        assert archive.namelist() == ["evidence/1-observation-1.json", "results.jsonl"]
        row = json.loads(archive.read("results.jsonl"))
        assert set(row) == {"scenario", "status", "actual", "evidence", "executed_at", "executor"}
        observation = json.loads(archive.read(row["evidence"][0]))
        assert observation == dict(marker="Tester A", environment="本机隔离环境", status="pass",
                                   actual="人工核对结果", executed_at=row["executed_at"])


def test_export_sanitizes_paths_and_numbers_repeated_evidence_references(board):
    """I3：不可信文件名不得越过 ZIP 的 evidence 目录，重复引用各有明确路径。"""
    pushed, _ = push(board)
    evidence = upload(board, b"same evidence", "first.txt")
    body = round_body(pushed, evidence)
    names = ["../../outside.txt", r"C:\temp\image.png", "same.txt", "same.txt"]
    for row, name in zip(body["results"], names):
        row["evidence"] = [dict(name=name, sha256=evidence["sha256"])]
    assert board.request("POST", BASE + "/rounds", body)[0] == 200
    status, raw, _ = board.request("GET", BASE + "/rounds/1/export", auth=False)
    assert status == 200
    with ZipFile(BytesIO(raw)) as archive:
        expected = ["evidence/1-outside.txt", "evidence/2-image.png", "evidence/3-same.txt", "evidence/4-same.txt"]
        assert archive.namelist() == expected + ["results.jsonl"]
        rows = [json.loads(line) for line in archive.read("results.jsonl").splitlines()]
        assert [r["evidence"] for r in rows] == [[name] for name in expected]
        assert all(archive.read(name) == b"same evidence" for name in expected)
