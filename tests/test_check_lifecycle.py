"""SOP-45 / D1：任务生命周期、独立调度与失败后的清理。"""
import json
import os
import time

import pytest

from check_helpers import (check_repo, declared_board, preset, start_check, settle_check, record_commit,
                           declarations, save_declarations)
from codex_helpers import ADMIN, fake_codex, task_board, create_task, read_task, mode
from helpers import BASE, push_body
from test_writable_repos import repo, git, wait_for


def test_check_runs_while_all_three_codex_slots_are_busy(board_factory, check_repo, fake_codex):
    """SOP-45-P06：T1 用三项合并占满同一 Codex 队列；检查仍独立完成。"""
    repo, presets = check_repo
    mode(fake_codex, delay=6)
    board = task_board(board_factory, fake_codex, repo=repo)
    merges = []
    for number in range(3):
        name = 'busy' + str(number)
        body = push_body()
        body['content'] = body['content'].replace('batch: demo', 'batch: ' + name)
        base = BASE.replace('/demo', '/' + name)
        assert board.request('PUT', base + '/scenario-file', body)[0] == 200
        merges.append(create_task(board, base))
    wait_for(lambda: all(read_task(board, t)['status'] == '运行中' for t in merges))
    done = settle_check(board, start_check(board))
    assert done['status'] == '已完成'
    assert all(read_task(board, t)['status'] == '运行中' for t in merges)
    assert len((presets / 'junit-calls.jsonl').read_text().splitlines()) == 1


def test_cancel_waiting_and_running_checks_and_retry_voided_task(board_factory, check_repo):
    """SOP-45-N07 / I2：待提交可放弃，作废可重试，重试固定新快照并保存旧证据。"""
    repo, presets = check_repo
    (repo / 'src/app.py').write_text('uncommitted\n')
    board = declared_board(board_factory, repo)
    task = start_check(board)
    wait_for(lambda: read_task(board, task)['status'] == '待提交')
    assert board.request('POST', f"/api/v1/tasks/{task['id']}/cancel", {}, headers=ADMIN)[0] == 200
    assert read_task(board, task)['status'] == '已放弃'
    record_commit(repo)
    preset(presets, 'junit', delay=2)
    task = start_check(board)
    wait_for(lambda: (presets / 'junit-calls.jsonl').exists())
    (repo / 'src/app.py').write_text('changed during run\n')
    done = settle_check(board, task)
    assert done['status'] == '已作废'
    commit = record_commit(repo)
    preset(presets, 'junit', delay=0, report=None)
    status, body, _ = board.request('POST', f"/api/v1/tasks/{task['id']}/retry", {}, headers=ADMIN)
    assert status == 200, body
    done = settle_check(board, task)
    assert done['attempt'] == 2 and done['snapshot']['commit'] == commit and done['void_reason'] == ''
    assert done['result']['checks'][0]['status'] == 'fail'  # 不能复用上次遗留的通过报告。
    directory = board.data / 'tasks' / str(task['id'])
    assert (directory / 'attempt-1/checks/junit/report.xml').exists()


def test_stop_interrupts_checks_and_restart_removes_orphan_worktrees(board_factory, check_repo):
    """SOP-45 / D1：停止时杀进程组，重启登记已中断并清理残留 worktree。"""
    from test_codex_tasks import process_alive
    repo, presets = check_repo
    preset(presets, 'junit', delay=60, child=True)
    board = declared_board(board_factory, repo)
    task = start_check(board)
    wait_for(lambda: (presets / 'child.pid').exists())
    parent = json.loads((presets / 'junit-calls.jsonl').read_text())['pid']
    child = int((presets / 'child.pid').read_text())
    board.stop()
    assert not process_alive(parent) and not process_alive(child)
    orphan = board.data.resolve() / 'worktrees/orphan'
    orphan.parent.mkdir(exist_ok=True)
    git(repo, 'worktree', 'add', '--detach', str(orphan), 'HEAD')
    board.start()
    assert read_task(board, task)['status'] == '已中断'
    assert not list((board.data / 'worktrees').iterdir())
    assert len(git(repo, 'worktree', 'list').splitlines()) == 1


def test_merge_reads_extended_entry_declarations(board_factory, check_repo, fake_codex):
    """SOP-41-P01：新增入口字段在原有合并读取入口处同样有效。"""
    repo, presets = check_repo
    value = declarations(repo)
    value['entries']['自定义入口'].update(candidate='start', start=['./serve.sh'], reload=['./reload.sh'])
    save_declarations(repo, value)
    board = task_board(board_factory, fake_codex, repo=repo)
    task = create_task(board)
    done = settle_check(board, task)
    assert done['status'] == '已完成', done
    snapshot = json.loads((board.data / 'tasks' / str(task['id']) / 'input.json').read_text())
    assert snapshot['entries']['entries']['自定义入口']['start'] == ['./serve.sh']
