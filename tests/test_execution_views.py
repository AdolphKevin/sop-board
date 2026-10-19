"""执行任务的只读页面状态与轮询标识。"""
from codex_helpers import fake_codex, mode, read_task
from execution_helpers import checkout, execution_board, start_execution, settle
from helpers import BASE, KEY
from test_writable_repos import repo, wait_for
from check_helpers import record_commit


def test_batch_shows_waiting_reason_paths_and_execution_rounds(board_factory, repo, fake_codex):
    """SOP-42-P01/P08/P12/N05/N06：页面展示任务范围、待提交路径和登记轮次，轮询随状态变化。"""
    checkout(repo)
    board = execution_board(board_factory, fake_codex, repo)
    old_etag = board.request('GET', BASE + '/state', auth=False)[1]['etag']
    (repo / 'src/app.py').write_text('uncommitted\n')
    task = start_execution(board)
    wait_for(lambda: read_task(board, task)['status'] == '待提交')
    page = board.request('GET', f'/p/{KEY}/b/demo', auth=False)[1].decode()
    assert '执行任务' in page and '工作区有未提交的产品改动' in page and 'src/app.py' in page
    assert '开发者发起' in page and f"#{task['id']}" in page
    assert board.request('GET', BASE + '/state', auth=False)[1]['etag'] != old_etag
    record_commit(repo)
    done = settle(board, task)
    assert done['status'] == '已完成'
    page = board.request('GET', f'/p/{KEY}/b/demo', auth=False)[1].decode()
    assert '执行任务' in page and '已完成' in page and '第 1 轮' in page
