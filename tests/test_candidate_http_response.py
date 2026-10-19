"""候选就绪只核对本次 HTTP 响应，不追随重定向。"""
from candidate_helpers import start_entry
from codex_helpers import fake_codex
from execution_helpers import checkout, execution_board, start_execution, settle, rounds
from test_execution_candidates import select_entries
from test_writable_repos import repo


def test_redirect_response_is_ready_without_following_location(board_factory, repo, fake_codex, tmp_path):
    """SOP-43-P02：收到 302 即就绪，重定向目标不需要可访问。"""
    checkout(repo)
    preset = start_entry(repo, tmp_path)
    preset.write_text('{"status":302}')
    select_entries(repo, ['api'] * 4)
    board = execution_board(board_factory, fake_codex, repo)
    done = settle(board, start_execution(board), timeout=3)
    assert done['status'] == '已完成' and all(r['status'] == 'pass' for r in rounds(board, done)[0]['results']), done
