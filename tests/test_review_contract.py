"""修订 3 的人工复核契约；预期来自场景 v2，不从当前实现派生。"""

from datetime import datetime, timezone
from io import BytesIO
import json
from zipfile import ZipFile

import pytest

from html_helpers import page
from review_helpers import F7_API, F7_WEB, prepare_seven, seven_mark
from test_reads import get


def test_seven_pass_summary_round_and_sparse_recorded_observation(board):
    """SOP-22-P03: exact round, current counts, server times and only valued observation keys."""
    pushed = prepare_seven(board)
    original = get(board, F7_API + "/rounds/1")
    t0 = datetime.now(timezone.utc)
    code, response, _ = board.request("POST", F7_API + "/marks", seven_mark(pushed, "N1-P02"))
    t1 = datetime.now(timezone.utc)
    assert code == 200 and response["ok"] is True and response["round"] == 4, response
    assert response["scenario"]["scenario"] == "N1-P02"
    assert response["scenario"]["queue"] == "reviewed"
    assert response["scenario"]["latest_manual"]["executor"] == "张三"
    counts = {"all": 7, "unreviewed": 4, "agent-pass": 3, "fail": 2, "blocked": 1}
    assert response["filters"] == counts
    listing = page(board, F7_WEB + "/scenarios")
    for key, count in counts.items():
        assert listing.one("a", **{"data-filter": key}).one("span").text == str(count)
    manual = get(board, F7_API + "/rounds/4")
    assert manual["kind"] == "manual" and len(manual["results"]) == 1
    row = manual["results"][0]
    assert (row["executor"], row["independence"], row["actual"]) == ("张三", "undeclared", "人工复核通过")
    assert "finding" not in row and "note" not in row
    assert t0 <= datetime.fromisoformat(row["executed_at"]) <= t1
    evidence = row["evidence"][0]
    assert evidence["name"] == "observation-4.json"
    raw, _ = _download_observation(board, evidence)
    observation = json.loads(raw)
    assert observation == {"marker": "张三", "status": "pass", "recorded_at": row["executed_at"]}
    assert t0 <= datetime.fromisoformat(observation["recorded_at"]) <= t1
    with ZipFile(BytesIO(get(board, F7_API + "/rounds/4/export"))) as archive:
        exported = json.loads(archive.read("results.jsonl"))
        assert exported["evidence"] == ["evidence/1-observation-4.json"]
        assert archive.read(exported["evidence"][0]) == raw
    assert get(board, F7_API + "/rounds/1") == original
    assert board.sql("SELECT COUNT(*) FROM results WHERE round_id=(SELECT id FROM rounds WHERE number=4)") == [(1,)]


def _download_observation(board, evidence):
    from conftest import OPENER
    with OPENER.open(f"http://127.0.0.1:{board.port}/api/v1/evidence/{evidence['sha256']}", timeout=5) as response:
        assert response.status == 200
        return response.read(), response.headers


def test_nonpass_normalization_sparse_observations_and_export_contract(board_factory):
    """SOP-22-P01/P04: all three new results and observation/ZIP bytes are checked exactly."""
    from helpers import BASE, IDS, push, round_body, upload
    from test_marks import mark_body
    board = board_factory(env={"JIRA_URL": "https://acme.atlassian.net"})
    pushed, _ = push(board)
    assert board.request("POST", BASE + "/rounds", round_body(pushed, upload(board)))[0] == 200
    original = get(board, BASE + "/rounds/1")
    for number, (sid, status, supplied, note, issue) in enumerate([
        (IDS[2], "fail", "ABC-123", "下单按钮无响应", "ABC-123"),
        (IDS[0], "blocked", "https://acme.atlassian.net/browse/ABC-124", None, "ABC-124"),
        (IDS[1], "blocked", "ABC-10", None, "ABC-10"),
    ], 2):
        body = mark_body(pushed, status, scenario=sid, fingerprint=pushed["fingerprints"][sid], marker="张三", defect_url=supplied)
        if note is not None:
            body["note"] = note
        t0 = datetime.now(timezone.utc)
        code, response, _ = board.request("POST", BASE + "/marks", body)
        t1 = datetime.now(timezone.utc)
        assert code == 200 and {"ok", "round", "scenario", "filters"} <= response.keys()
        assert response["round"] == number and response["ok"] is True
        manual = get(board, BASE + f"/rounds/{number}")
        assert manual["kind"] == "manual" and len(manual["results"]) == 1
        row = manual["results"][0]
        link = "https://acme.atlassian.net/browse/" + issue
        expected_actual = f"人工复核不通过（{'失败' if status == 'fail' else '受阻'}），缺陷 {link}"
        assert (row["scenario"], row["status"], row["actual"], row["finding"], row["executor"], row["independence"]) == (
            sid, status, expected_actual, link, "张三", "undeclared")
        assert t0 <= datetime.fromisoformat(row["executed_at"]) <= t1
        if note is None:
            assert "note" not in row
        else:
            assert row["note"] == note
        raw, _ = _download_observation(board, row["evidence"][0])
        expected = dict(marker="张三", status=status, defect_url=link, recorded_at=row["executed_at"])
        if note is not None:
            expected["note"] = note
        assert json.loads(raw) == expected
        with ZipFile(BytesIO(get(board, BASE + f"/rounds/{number}/export"))) as archive:
            assert archive.namelist() == [f"evidence/1-observation-{number}.json", "results.jsonl"]
            lines = archive.read("results.jsonl").decode().splitlines()
            assert len(lines) == 1
            exported = json.loads(lines[0])
            expected_row = {k: row[k] for k in ("scenario", "status", "actual", "finding", "executor", "executed_at")}
            if note is not None:
                expected_row["note"] = note
            expected_row["evidence"] = [f"evidence/1-observation-{number}.json"]
            assert exported == expected_row
            assert archive.read(exported["evidence"][0]) == raw
    assert get(board, BASE + "/rounds/1") == original
    assert board.sql("SELECT COUNT(*) FROM rounds WHERE kind='manual'") == [(3,)]


@pytest.mark.parametrize("status", ["pass", "fail", "blocked"])
def test_empty_note_is_omitted_from_observation(board, status):
    """I2 revision 3: an explicitly empty optional note has no observation key."""
    from helpers import BASE, push
    from test_marks import mark_body
    pushed, _ = push(board)
    extra = {} if status == "pass" else {"defect_url": "https://github.com/demo-org/demo-app/issues/1"}
    code, response, _ = board.request("POST", BASE + "/marks", mark_body(pushed, status, note="", **extra))
    assert code == 200, response
    row = get(board, BASE + "/rounds/1")["results"][0]
    observation = json.loads(_download_observation(board, row["evidence"][0])[0])
    assert "note" not in observation
    assert set(observation) == ({"marker", "status", "recorded_at"} if status == "pass" else
                                {"marker", "status", "defect_url", "recorded_at"})


@pytest.mark.parametrize("kind", ["unknown-scenario", "missing-token", "wrong-token"])
def test_unknown_scenario_and_unauthorized_marks_never_add_rows_or_observations(board, kind):
    """SOP-22-N04: precise public errors and no data or evidence side effects."""
    from helpers import BASE, assert_error, push, round_body, snapshot, upload
    from test_marks import mark_body
    pushed, _ = push(board)
    assert board.request("POST", BASE + "/rounds", round_body(pushed, upload(board)))[0] == 200
    body = mark_body(pushed, marker="张三")
    headers = {"Authorization": "Bearer wrong-isolated-value"} if kind == "wrong-token" else {}
    if kind == "unknown-scenario":
        body["scenario"] = "N9-P99"
    before = snapshot(board)
    problems = assert_error(board.request("POST", BASE + "/marks", body, auth=kind == "unknown-scenario", headers=headers),
                            409 if kind == "unknown-scenario" else 401,
                            "scenario-stale" if kind == "unknown-scenario" else "unauthorized")
    assert [p["field"] for p in problems] == (["scenario"] if kind == "unknown-scenario" else ["Authorization"])
    assert snapshot(board) == before


def test_changed_fingerprint_and_removed_scenario_reject_marks_without_writes(board):
    """SOP-22-N03/SOP-21-N07: actual version changes, archived history and exact stale fields."""
    from helpers import assert_error, push_body, snapshot
    from review_helpers import seven_document
    pushed = prepare_seven(board)
    agent = get(board, F7_API + "/rounds/1")
    document = seven_document()
    document["sops"][0]["scenarios"][1]["expected"] = ["新内容，旧指纹不再有效"]
    document["sops"][1]["scenarios"].pop()
    document["sops"][1]["negative_exempt"] = "验证删除后的历史"
    assert board.request("PUT", F7_API + "/scenario-file", push_body(document, path="f7.yaml"))[0] == 200
    archived = page(board, F7_WEB + "/s/N2-N01")
    assert "已从场景文件移除" in archived.text and "第 1 轮 · Agent · 跳过" in archived.text
    assert not archived.find_all("button") and not archived.find_all("form")
    before = snapshot(board)
    for sid, field in [("N1-P02", "fingerprint"), ("N2-N01", "scenario")]:
        problems = assert_error(board.request("POST", F7_API + "/marks", seven_mark(pushed, sid)), 409, "scenario-stale")
        assert [p["field"] for p in problems] == [field]
        assert snapshot(board) == before
    assert get(board, F7_API + "/rounds/1") == agent
