from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy

from helpers import BASE, push, push_body, round_body, upload
from test_marks import mark_body
from test_reads import get


def test_concurrent_duplicate_upload_push_and_idempotent_round(board):
    """I2, D1: same evidence, same file and same idempotency key create once."""
    body = push_body()
    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(lambda _: board.request("PUT", BASE + "/scenario-file", body), range(16)))
    assert all(r[0] == 200 and r[1]["version"] == 1 for r in responses)
    assert sum(not r[1]["unchanged"] for r in responses) == 1
    with ThreadPoolExecutor(max_workers=8) as pool:
        evidences = list(pool.map(lambda _: upload(board, b"same bytes"), range(16)))
    assert len({e["sha256"] for e in evidences}) == 1
    round_ = round_body(body, evidences[0])
    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(lambda _: board.request("POST", BASE + "/rounds", round_), range(16)))
    assert all(r[0] == 200 and r[1]["round"] == 1 for r in responses)
    assert board.sql("SELECT COUNT(*) FROM versions") == [(1,)]
    assert board.sql("SELECT COUNT(*) FROM evidence") == [(1,)]
    assert board.sql("SELECT COUNT(*) FROM rounds") == [(1,)]
    assert board.sql("SELECT COUNT(*) FROM results") == [(4,)]


def test_concurrent_agent_and_manual_rounds_share_sequence_and_invalid_requests_rollback(board):
    """D1: concurrent successful/failed requests and reads preserve consistent batches."""
    pushed, _ = push(board)
    evidence = upload(board)
    tasks = []
    for i in range(6):
        body = round_body(pushed, evidence, key=f"run-{i}")
        tasks.append(("/rounds", body))
        tasks.append(("/marks", mark_body(pushed)))
        invalid = deepcopy(body)
        invalid["idempotency_key"] = f"bad-{i}"
        invalid["results"][-1].pop("finding")
        tasks.append(("/rounds", invalid))
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda task: board.request("POST", BASE + task[0], task[1]), tasks))
    successes = [r[1]["round"] for r in results if r[0] == 200]
    assert sorted(successes) == list(range(1, 13))
    assert sum(r[0] == 422 for r in results) == 6
    assert board.sql("SELECT kind,COUNT(*) FROM rounds GROUP BY kind ORDER BY kind") == [("agent", 6), ("manual", 6)]
    assert board.sql("SELECT COUNT(*) FROM results") == [(30,)]
    assert board.sql("SELECT COUNT(*) FROM evidence") == [(7,)]
    assert len([p for p in (board.data / "evidence").rglob("*") if p.is_file()]) == 7
    summary = get(board, BASE)
    assert len(summary["rounds"]) == 12 and summary["scenario_count"] == 4
