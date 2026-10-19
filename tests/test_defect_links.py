"""SOP-BOARD-2 I4.1–4: normalization via HTTP, with exact persisted links."""

import json

import pytest

from helpers import BASE, assert_error, push, push_body, snapshot
from test_marks import mark_body
from test_reads import get


@pytest.mark.parametrize("status", ["fail", "blocked"])
@pytest.mark.parametrize("supplied,expected", [
    (" ABC-1 ", "https://example.atlassian.net/browse/ABC-1"),
    ("https://example.atlassian.net/browse/ABC-1?focusedCommentId=2#x", "https://example.atlassian.net/browse/ABC-1"),
    (" A_B2-001 ", "https://example.atlassian.net/browse/A_B2-001"),
    (" HTTPS://GITHUB.COM/DEMO-ORG/DEMO-APP/ISSUES/7?x=1#keep ", "HTTPS://GITHUB.COM/DEMO-ORG/DEMO-APP/ISSUES/7?x=1#keep"),
])
def test_jira_and_project_links_normalize_for_fail_and_blocked(board_factory, status, supplied, expected):
    """SOP-24-P01, I4.1–3: Jira wins; project prefixes ignore case and preserve content."""
    board = board_factory(env={"JIRA_URL": "https://example.atlassian.net///"})
    pushed, _ = push(board)
    code, result, _ = board.request("POST", BASE + "/marks", mark_body(pushed, status, defect_url=supplied))
    assert code == 200, result
    row = get(board, BASE + "/rounds/1")["results"][0]
    assert row["finding"] == expected
    assert board.sql("SELECT finding FROM results") == [(expected,)]
    evidence = row["evidence"][0]
    raw = (board.data / "evidence" / evidence["sha256"][:2] / evidence["sha256"]).read_bytes()
    assert json.loads(raw)["defect_url"] == expected


@pytest.mark.parametrize("status", ["fail", "blocked"])
@pytest.mark.parametrize("supplied", ["https://example.com/bug/1", "abc-1", "https://example.atlassian.net.evil/browse/ABC-1",
    "https://example.atlassian.net/browse/ABC-1/extra", "https://example.atlassian.net/browse/abc-1",
    "ABC1", "https://other.atlassian.net/browse/ABC-1", "https://example.atlassian.net/jira/ABC-1",
    "http://github.com/demo-org/demo-app/issues/1"])
def test_invalid_defect_links_explain_accepted_forms_and_leave_no_data(board_factory, supplied, status):
    """SOP-24-N01: neither arbitrary sites nor malformed Jira issue keys are accepted."""
    board = board_factory(env={"JIRA_URL": "https://example.atlassian.net"})
    pushed, _ = push(board)
    before = snapshot(board)
    problems = assert_error(board.request("POST", BASE + "/marks", mark_body(pushed, status, defect_url=supplied)),
                            422, "defect-link-invalid")
    assert problems[0]["field"] == "defect_url"
    assert "单号" in problems[0]["problem"] and "https://example.atlassian.net/browse/" in problems[0]["problem"]
    assert "https://github.com/demo-org/demo-app/issues/" in problems[0]["problem"]
    assert snapshot(board) == before


def test_without_jira_key_requires_full_link_and_github_prefix_still_works(board):
    """SOP-24-N02: no implicit Jira host; GitHub links remain supported."""
    pushed, _ = push(board)
    before = snapshot(board)
    problems = assert_error(board.request("POST", BASE + "/marks", mark_body(pushed, "fail", defect_url="ABC-1")),
                            422, "defect-link-invalid")
    assert "完整链接" in problems[0]["problem"]
    assert snapshot(board) == before
    assert board.sql("SELECT COUNT(*) FROM rounds WHERE kind='manual'") == [(0,)]
    link = "https://github.com/demo-org/demo-app/issues/3"
    assert board.request("POST", BASE + "/marks", mark_body(pushed, "fail", defect_url=link))[0] == 200
    assert board.sql("SELECT finding FROM results") == [(link,)]


def test_jira_without_project_prefix_and_old_links_survive_restart(board_factory):
    """SOP-24-P06: summaries, history, result bytes and observation survive configuration changes."""
    from io import BytesIO
    from zipfile import ZipFile
    from helpers import IDS, round_body, upload
    from html_helpers import page
    from test_pages import WEB

    board = board_factory()
    pushed, _ = push(board)
    assert board.request("POST", BASE + "/rounds", round_body(pushed, upload(board)))[0] == 200
    agent = get(board, BASE + "/rounds/1")
    old = "https://github.com/demo-org/demo-app/issues/7?keep=yes#old"
    assert board.request("POST", BASE + "/marks", mark_body(pushed, "fail", scenario=IDS[2],
                        fingerprint=pushed["fingerprints"][IDS[2]], defect_url=old))[0] == 200
    before = get(board, BASE + "/rounds/2")
    with ZipFile(BytesIO(get(board, BASE + "/rounds/2/export"))) as archive:
        exported = {name: archive.read(name) for name in archive.namelist()}
    board.stop()
    board.env["JIRA_URL"] = "https://acme.atlassian.net/"
    board.start()
    assert get(board, BASE + "/rounds/2") == before
    assert get(board, BASE + "/rounds/1") == agent
    scenario = next(r for r in get(board, BASE + "/scenarios")["scenarios"] if r["scenario"] == IDS[2])
    assert scenario["latest_manual"]["finding"] == old
    assert board.sql("SELECT finding FROM results WHERE round_id=(SELECT id FROM rounds WHERE number=2)") == [(old,)]
    for path in (WEB + "/scenarios", WEB + "/s/" + IDS[2]):
        assert page(board, path).one("a", href=old).text == old
    with ZipFile(BytesIO(get(board, BASE + "/rounds/2/export"))) as archive:
        assert {name: archive.read(name) for name in archive.namelist()} == exported
    other = push_body(project=dict(id="internal/demo", name="内部项目"))
    other_base = "/api/v1/projects/internal-demo/batches/demo"
    assert board.request("PUT", other_base + "/scenario-file", other)[0] == 200
    assert board.request("POST", other_base + "/marks", mark_body(other, "blocked", defect_url="ABC-1"))[0] == 200
    assert get(board, other_base + "/rounds/1")["results"][0]["finding"] == "https://acme.atlassian.net/browse/ABC-1"


def test_default_prefix_matching_ignores_project_identity_case(board):
    """SOP-24-P04: a mixed-case GitHub identity accepts the lowercase issue URL."""
    pushed, _ = push(board, push_body(project=dict(id="GitHub.com/Demo_Org/Demo.App", name="演示应用")))
    link = "https://github.com/demo_org/demo.app/issues/1"
    code, result, _ = board.request("POST", BASE + "/marks", mark_body(pushed, "fail", defect_url=link))
    assert code == 200, result
    assert board.sql("SELECT finding FROM results") == [(link,)]
