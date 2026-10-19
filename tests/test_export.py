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
        assert row["actual"] == "人工复核通过"
        assert observation == dict(marker="Tester A", status="pass", recorded_at=row["executed_at"])


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


def test_manual_exports_follow_tw_contract_and_submit_when_available(board, tmp_path):
    """SOP-22-P02: pass, fail and blocked exports satisfy the actual tw result reader."""
    from datetime import datetime, timezone
    from helpers import IDS
    from tw_helpers import prepare_tw_project, tw_command

    from helpers import demo, push_body
    document = demo()
    for node in document["sops"]:
        for scenario in node["scenarios"]:
            scenario.update(initial_state=["初始状态"], steps=["执行操作"])
    pushed, _ = push(board, push_body(document))
    exports = []
    for number, (sid, status) in enumerate([(IDS[0], "pass"), (IDS[2], "fail"), (IDS[2], "blocked")], 1):
        body = mark_body(pushed, status, scenario=sid, fingerprint=pushed["fingerprints"][sid])
        if status != "pass":
            body.update(defect_url="https://github.com/demo-org/demo-app/issues/9", note="等数据")
        assert board.request("POST", BASE + "/marks", body)[0] == 200
        code, raw, _ = board.request("GET", BASE + f"/rounds/{number}/export", auth=False)
        assert code == 200
        directory = tmp_path / f"export-{number}"
        with ZipFile(BytesIO(raw)) as archive:
            archive.extractall(directory)
        path = directory / "results.jsonl"
        row = json.loads(path.read_text())
        expected = {"scenario", "status", "actual", "evidence", "executor", "executed_at"}
        if status != "pass":
            expected.update(["finding", "note"])
        assert set(row) == expected
        assert row["status"] == status and row["executor"] == "Tester A"
        assert row["actual"] == ("人工复核通过" if status == "pass" else
            f"人工复核不通过（{'失败' if status == 'fail' else '受阻'}），缺陷 https://github.com/demo-org/demo-app/issues/9")
        assert datetime.fromisoformat(row["executed_at"]).tzinfo is not None
        assert row["evidence"] == [f"evidence/1-observation-{number}.json"]
        observation = json.loads((directory / row["evidence"][0]).read_text())
        assert set(observation) == ({"marker", "status", "recorded_at"} if status == "pass" else
                                    {"marker", "status", "defect_url", "note", "recorded_at"})
        assert observation["recorded_at"] == row["executed_at"]
        exports.append(path)
    command = tw_command()
    if command is None:
        print("tw unavailable: checked all required result fields and observation evidence")
        return
    run, log = prepare_tw_project(tmp_path, pushed["content"], command)
    # Baseline results are outside the operation under test. They let one-scenario
    # rounds be submitted while satisfying tw's complete-pending-set requirement.
    baseline = tmp_path / "baseline.jsonl"
    baseline.write_text("\n".join(json.dumps(dict(scenario=sid, status="pass", actual="Fixture baseline",
        evidence=[str(exports[0].parent.relative_to(tmp_path) / "evidence/1-observation-1.json")],
        executor="Fixture Tester", executed_at=datetime.now(timezone.utc).isoformat())) for sid in IDS) + "\n")
    run("team", "scenarios", "submit", "--task", "EXPORT-CONTRACT", "--results", str(baseline))
    for path in exports:
        output = json.loads(run("team", "scenarios", "submit", "--task", "EXPORT-CONTRACT", "--results", str(path)))
        assert output["ok"] is True
        assert output["round_statistics"]["executed"] == 1
        print("tw team scenarios submit:", json.dumps({k: output[k] for k in ("ok", "round", "round_statistics")}, ensure_ascii=False))
    print("tw command log:", log)
