"""SOP-44：真实队列与假 Codex 进程的浏览器互斥、名额及异常释放。"""
import json
import time

import pytest
import yaml

from check_helpers import declarations, save_declarations, record_commit
from codex_helpers import ADMIN, fake_codex, mode, read_task, task_board
from conftest import ROOT
from execution_helpers import checkout, start_execution, settle, task_directory
from helpers import BASE, push_body
from test_automation import add_batch, automation
from test_codex_tasks import process_alive
from test_writable_repos import IDENTITY, repo, wait_for


def browser_board(factory, repo, fake):
    checkout(repo)
    value = declarations(repo)
    value['entries']['web'] = dict(value['entries']['自定义入口'], surface='web')
    save_declarations(repo, value)
    record_commit(repo)
    # SOP-44 的批次先存在，再由开发者发起执行。授权后才推送新批次会自动
    # 运行，紧接着的手工请求可能合法地另建同批次后续任务，污染占满名额的前提。
    board = task_board(factory, fake)
    document = yaml.safe_load((repo / 'docs/sop/demo.yaml').read_text())
    assert board.request('PUT', BASE + '/scenario-file', push_body(document, path='docs/sop/demo.yaml'))[0] == 200
    for name in ('w1', 'w2', 'w3', 'w4', 'owner', 'waiter', 'c0', 'c1', 'c2', 'c3', 'c4', 'c5'):
        add_batch(board, repo, name, web=name.startswith('w') or name in ('owner', 'waiter'))
    board.stop()
    board.command = [str(ROOT / '.venv/bin/sop-board'), '--writable-repo', f'{IDENTITY}={repo}']
    board.start()
    assert board.request('GET', '/api/v1/tasks', auth=False)[1]['tasks'] == []
    return board


def launched(board, task):
    return (task_directory(board, task) / 'invocation.json').exists()


def cancel(board, task):
    assert board.request('POST', f"/api/v1/tasks/{task['id']}/cancel", headers=ADMIN)[0] == 200


def test_browser_lock_precedes_slots_and_waiter_does_not_consume_slot(board_factory, repo, fake_codex):
    """SOP-44-P01/P02/P03：一个 web 加两个 cli 可并行；满额时 web 一排队一等待。"""
    mode(fake_codex, delay=60)
    board = browser_board(board_factory, repo, fake_codex)
    first = start_execution(board, add_batch(board, repo, 'w1', web=True))
    wait_for(lambda: launched(board, first))
    waiting = start_execution(board, add_batch(board, repo, 'w2', web=True))
    wait_for(lambda: read_task(board, waiting)['status'] == '等待浏览器')
    cli = [start_execution(board, add_batch(board, repo, f'c{i}')) for i in (1, 2)]
    wait_for(lambda: all(launched(board, t) for t in cli))
    assert not launched(board, waiting)
    active = [t for t in board.request('GET', '/api/v1/tasks', auth=False)[1]['tasks'] if t['status'] == '运行中']
    assert {t['id'] for t in active} == {t['id'] for t in [first, *cli]}
    assert sum(process_alive(json.loads((task_directory(board, t) / 'invocation.json').read_text())['pid']) for t in active) == 3
    for task in (first, waiting, *cli):
        cancel(board, task)
    cli = [start_execution(board, add_batch(board, repo, f'c{i}')) for i in (3, 4, 5)]
    wait_for(lambda: all(launched(board, t) for t in cli))
    owner = start_execution(board, add_batch(board, repo, 'w3', web=True))
    waiting = start_execution(board, add_batch(board, repo, 'w4', web=True))
    wait_for(lambda: read_task(board, waiting)['status'] == '等待浏览器')
    assert read_task(board, owner)['status'] == '排队中' and not launched(board, owner)
    cancel(board, cli[0])
    wait_for(lambda: launched(board, owner))
    assert not launched(board, waiting)
    cancel(board, owner)
    wait_for(lambda: launched(board, waiting))
    assert read_task(board, owner)['status'] == '已放弃'
    assert not process_alive(json.loads((task_directory(board, owner) / 'invocation.json').read_text())['pid'])


@pytest.mark.parametrize('exit_code', [0, 1])
def test_browser_released_after_completion_or_failure_and_pause_holds_waiter(board_factory, repo, fake_codex, exit_code):
    """SOP-44-P01/P04/N01：结束或失败后释放；暂停时等待浏览器的任务仍不开始。"""
    mode(fake_codex, delay=2, exit=exit_code)
    board = browser_board(board_factory, repo, fake_codex)
    first = start_execution(board, add_batch(board, repo, 'w1', web=True))
    wait_for(lambda: launched(board, first))
    second = start_execution(board, add_batch(board, repo, 'w2', web=True))
    wait_for(lambda: read_task(board, second)['status'] == '等待浏览器')
    automation(board, 'pause', True)
    assert settle(board, first)['status'] == ('失败' if exit_code else '已完成')
    time.sleep(6)
    assert read_task(board, second)['status'] == '等待浏览器' and not launched(board, second)
    mode(fake_codex, delay=.2)
    automation(board, 'resume', False)
    assert settle(board, second)['status'] == '已完成'


def test_queued_browser_owner_releases_lock_when_snapshot_preparation_fails(board_factory, repo, fake_codex):
    """SOP-44-P03/N01：已取浏览器但仍排队的任务异常时，也必须释放锁。"""
    mode(fake_codex, delay=60)
    board = browser_board(board_factory, repo, fake_codex)
    cli = [start_execution(board, add_batch(board, repo, f'c{i}')) for i in range(3)]
    wait_for(lambda: all(launched(board, t) for t in cli))
    base = add_batch(board, repo, 'owner', web=True)
    owner = start_execution(board, base)
    waiting = start_execution(board, add_batch(board, repo, 'waiter', web=True))
    wait_for(lambda: read_task(board, waiting)['status'] == '等待浏览器')
    document = yaml.safe_load((repo / 'docs/sop/demo.yaml').read_text())
    document['metadata']['batch'] = 'owner'
    document['sops'][0]['scenarios'][0]['id'] = 'REPLACEMENT-P01'
    response = board.request('PUT', base + '/scenario-file', push_body(document))
    assert response[0] == 200, response[1]
    assert settle(board, owner)['status'] == '失败'
    wait_for(lambda: read_task(board, waiting)['status'] == '排队中')
    assert not launched(board, waiting)
    cancel(board, cli[0])
    wait_for(lambda: launched(board, waiting))
