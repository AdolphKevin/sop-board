"""SOP-58：状态、恢复和开发者操作均经 HTTP 核对。"""
from pathlib import Path
import time

from codex_helpers import ADMIN, read_task
from fix_helpers import (fix_repo, fix_codex, fix_board, configure, fail, wait_fix,
                         wait_state, all_invocations, git)
from helpers import KEY, IDS, assert_error
from test_writable_repos import repo, wait_for
from in_place_helpers import prepare_in_place, runtime


def test_fix_observable_states_have_reasons_and_invalid_retry_has_no_effect(board_factory, fix_codex, fix_repo):
    """SOP-58-P01/N02/N06：观察完整状态顺序，无效操作不启动会话。"""
    hook = fix_repo / '.git/hooks/post-merge'
    hook.write_text('#!/bin/sh\nsleep 1\n')
    hook.chmod(0o755)
    configure(fix_codex, dict(delay=.8), retests=[dict(delay=1)])
    board, pushed = fix_board(board_factory, fix_codex, fix_repo)
    fail(board, pushed)
    task = wait_fix(board, '分析中')
    assert task['error']
    task = wait_state(board, task, '复测中')
    assert task['error']
    count = len(all_invocations(fix_codex))
    assert_error(board.request('POST', f"/api/v1/tasks/{task['id']}/retry", {}, headers=ADMIN), 409, 'task-state-invalid')
    assert read_task(board, task)['status'] == '复测中' and len(all_invocations(fix_codex)) == count
    task = wait_state(board, task, '待合入')
    assert task['error']
    task = wait_state(board, task, '已合入')
    assert task['error']
    count = len(all_invocations(fix_codex))
    for action in ('retry', 'cancel'):
        assert_error(board.request('POST', f"/api/v1/tasks/{task['id']}/{action}", {}, headers=ADMIN), 409, 'task-state-invalid')
    assert read_task(board, task)['status'] == '已合入' and len(all_invocations(fix_codex)) == count
    assert_error(board.request('GET', '/api/v1/tasks/999999', auth=False), 404, 'task-not-found')


def test_fix_restart_keeps_waiting_task_and_retry_rebuilds_same_session(board_factory, fix_codex, fix_repo):
    """SOP-58-N01：运行中断不自动续跑，等待状态保持，目录丢失后同会话重建。"""
    configure(fix_codex, {}, dict(delay=30), retests=[dict(statuses={IDS[0]: 'fail'})])
    board, pushed = fix_board(board_factory, fix_codex, fix_repo)
    fail(board, pushed)
    task = wait_fix(board, '修复中')
    wait_for(lambda: len(all_invocations(fix_codex)) == 3)
    board.stop()
    git(fix_repo, 'worktree', 'remove', '--force', task['worktree'])
    configure(fix_codex, {}, retests=[{}])
    board.start()
    interrupted = read_task(board, task)
    assert interrupted['status'] == '已中断' and interrupted['session_id'] == 'TH-1'
    items = board.request('GET', f'/api/v1/projects/{KEY}/attention', auth=False)[1]['items']
    assert items[0]['kind'] == 'interrupted' and items[0]['resume'] == 'codex resume TH-1'
    time.sleep(.3)
    assert len(all_invocations(fix_codex)) == 3
    assert board.request('POST', f"/api/v1/tasks/{task['id']}/retry", {}, headers=ADMIN)[0] == 200
    task = wait_state(board, task, '已合入')
    repair = all_invocations(fix_codex)[3]
    assert repair['argv'][repair['argv'].index('resume') + 1] == 'TH-1'
    assert repair['cwd'] == task['worktree']
    # 另一个待开发者的任务重启后维持原状。
    configure(fix_codex, dict(no_changes=True, categories={sid: 'environment' for sid in IDS}))
    fail(board, pushed, IDS[1])
    waiting = wait_fix(board, '待开发者处理')
    count = len(all_invocations(fix_codex))
    board.stop()
    board.start()
    assert read_task(board, waiting)['status'] == '待开发者处理'
    assert len(all_invocations(fix_codex)) == count


def test_fix_codex_unavailable_can_be_retried_after_recovery(board_factory, fix_codex, fix_repo):
    """SOP-58-N03：版本检查失败保留原因，恢复可用后完成修复。"""
    configure(fix_codex, {}, version_exit=2, retests=[{}])
    board, pushed = fix_board(board_factory, fix_codex, fix_repo)
    fail(board, pushed)
    task = wait_fix(board, 'Codex 不可用')
    assert task['error'] and not all_invocations(fix_codex)
    assert 'Codex 不可用' in board.request('GET', f'/p/{KEY}/fixes', auth=False)[1].decode()
    configure(fix_codex, {}, retests=[{}])
    assert board.request('POST', f"/api/v1/tasks/{task['id']}/retry", {}, headers=ADMIN)[0] == 200
    wait_state(board, task, '已合入')


def test_cancel_in_place_keeps_all_already_merged_commits(board_factory, fix_codex, fix_repo, tmp_path):
    """SOP-58-N04：放弃删除任务分支与目录，三个已合入提交全部保留。"""
    control = tmp_path / 'runtime'
    prepare_in_place(fix_repo, control)
    configure(fix_codex, {}, retests=[dict(observe_http=True, expected_content='print(999)\n')])
    with runtime(fix_repo, control) as address:
        board, pushed = fix_board(board_factory, fix_codex, fix_repo, APP_URL=address)
        fail(board, pushed)
        task = wait_fix(board, '待开发者处理')
        assert task['attempt'] == 3
        before = git(fix_repo, 'rev-list', 'HEAD')
        assert board.request('POST', f"/api/v1/tasks/{task['id']}/cancel", {}, headers=ADMIN)[0] == 200
        assert read_task(board, task)['status'] == '已放弃'
        assert not Path(task['worktree']).exists()
        assert not git(fix_repo, 'branch', '--list', task['fix_branch'])
        assert git(fix_repo, 'rev-list', 'HEAD') == before
        assert board.request('GET', f'/api/v1/projects/{KEY}/attention', auth=False)[1]['items'] == []
