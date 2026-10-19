"""I5：实际 board.js 在隔离 DOM 中轮询，另连接真实 HTTP 验证主体更新。"""

import pytest

from helpers import BASE, IDS, push, round_body, upload
from test_client import run_page_client
from test_marks import mark_body
from test_pages import WEB


@pytest.mark.parametrize("suffix", ["", "/scenarios", "/cases", "/progress", "/s/N1-P01"])
def test_poll_unchanged_for_thirty_seconds_never_fetches_or_replaces_page(board, suffix):
    """SOP-25-P08：十次三秒节拍都为 304，页面对象不替换。"""
    push(board)
    run_page_client(board, "poll-unchanged", WEB + suffix)


@pytest.mark.parametrize("ending", ["cancel", "submit", "reject", "multiple", "empty", "note", "typing-during-fetch"])
def test_poll_preserves_drafts_until_submit_or_cancel(board, ending):
    """SOP-25-N01、SOP-21-N06：草稿保护、空栏刷新与取回期间输入。"""
    push(board)
    run_page_client(board, "poll-draft-" + ending, WEB + "/scenarios?filter=unreviewed")


@pytest.mark.parametrize("suffix", ["", "/scenarios?filter=unreviewed", "/cases", "/progress", "/s/N1-P01"])
def test_poll_refreshes_real_service_page_and_rebinds_controls_within_five_seconds(board, suffix):
    """SOP-25-P03/P08：远端标记/上传后取回当前 URL，身份与筛选仍可用。"""
    from cases_helpers import cases_body
    pushed, _ = push(board)
    assert board.request("POST", BASE + "/rounds", round_body(pushed, upload(board)))[0] == 200
    mutation = cases_body() if suffix == "/cases" else mark_body(pushed)
    action = "/cases" if suffix == "/cases" else "/marks"
    run_page_client(board, "poll-real", WEB + suffix, network=True,
                    context=dict(mutation=mutation, mutationPath=BASE + action))
