"""SOP-31-P02、SOP-32-P03：页面身份、开发者按钮与原始输出。"""
from codex_helpers import fake_codex, task_board, create_task, finished, mode
from helpers import BASE, KEY, push
from test_client import run_page_client


def test_admin_token_page_storage_and_operation(board_factory, fake_codex):
    from cases_helpers import upload_cases
    mode(fake_codex, delay=10)
    board = task_board(board_factory, fake_codex)
    push(board)
    upload_cases(board, base=BASE)
    run_page_client(board, 'admin-token', f'/p/{KEY}/b/demo/merges', network=True)
    assert len(board.request('GET', '/api/v1/tasks', auth=False)[1]['tasks']) == 1


def test_failed_task_page_displays_reason_files_and_buttons(board_factory, fake_codex):
    mode(fake_codex, exit=3, final='bad output')
    board = task_board(board_factory, fake_codex)
    push(board)
    task = finished(board, create_task(board), '失败')
    status, raw, _ = board.request('GET', f'/p/{KEY}/b/demo/merges', auth=False)
    assert status == 200
    page = raw.decode()
    assert 'Codex 退出码 3' in page and '开发者令牌' in page
    assert '重试' in page and f'/api/v1/tasks/{task["id"]}/cancel' not in page
    for name in ('events.jsonl', 'stderr.txt', 'final.json', 'check.json'):
        assert task['files'][name] in page


def test_missing_admin_blocks_both_buttons_then_retry_and_merge_keep_writer_identity(board_factory, fake_codex):
    """SOP-31-P02、SOP-32-P03：精确提示、两次不请求、开发者操作与人工标记分别用各自令牌。"""
    from cases_helpers import upload_cases
    from helpers import IDS
    mode(fake_codex, final='{}')
    board = task_board(board_factory, fake_codex)
    push(board)
    upload_cases(board, base=BASE)
    task = board.request('GET', '/api/v1/tasks', auth=False)[1]['tasks'][-1]
    failed = finished(board, task, '失败')
    assert 'cases' in failed['error']
    status, page, _ = board.request('GET', f'/p/{KEY}/b/demo/merges', auth=False)
    assert status == 200 and 'cases' in page.decode()
    root = board.data / 'tasks' / str(task['id'])
    for name in ('check.json', 'events.jsonl', 'stderr.txt', 'final.json'):
        assert failed['files'][name] in page.decode()
        assert board.request('GET', failed['files'][name], auth=False)[1] == (root / name).read_bytes()
    mode(fake_codex, delay=10)
    saved = {'sop-board.marker': 'Tester A', 'sop-board.token': 'isolated-test'}
    run_page_client(board, 'admin-rework', f'/p/{KEY}/b/demo/merges', saved=saved, network=True)
    result = board.request('GET', f"/api/v1/tasks/{task['id']}", auth=False)[1]['task']
    assert result['status'] in ('排队中', '运行中') and result['input_digest'] == failed['input_digest']
    assert len(board.request('GET', '/api/v1/tasks', auth=False)[1]['tasks']) == 1
    run_page_client(board, 'admin-write-token', f'/p/{KEY}/b/demo/scenarios',
                    saved={**saved, 'sop-board.admin-token': 'isolated-admin'}, network=True, context={'sid': IDS[0]})
    result = board.request('GET', BASE + '/scenarios', auth=False)[1]['scenarios'][0]['latest_manual']
    assert result['status'] == 'pass' and result['executor'] == 'Tester A' and result['round'] == 1
    mode(fake_codex)
    # Let this attempt finish after the test has observed the active-state response.
    finished(board, task)
