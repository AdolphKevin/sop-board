"""SOP-44：开发者暂停所有自动任务，状态保存在本次隔离数据目录。"""
from codex_helpers import ADMIN, fake_codex, task_board
from helpers import BASE, assert_error, push

import time

import yaml

from check_helpers import check_repo, record_commit, start_check
from codex_helpers import mode, read_task, create_task
from execution_helpers import checkout, execution_board, start_execution, settle, task_directory
from helpers import push_body
from test_writable_repos import repo, wait_for


def add_batch(board, repo, name, *, web=False):
    document = yaml.safe_load((repo / 'docs/sop/demo.yaml').read_text())
    document['metadata']['batch'] = name
    for node in document['sops']:
        for scene in node['scenarios']:
            scene.pop('regression', None)
            if web:
                scene['verify'] = 'web'
    base = BASE.replace('/demo', '/' + name)
    assert board.request('PUT', base + '/scenario-file', push_body(document))[0] == 200
    return base


def automation(board, action, paused):
    status, body, _ = board.request('POST', '/api/v1/automation/' + action, headers=ADMIN)
    assert status == 200 and body == dict(ok=True, paused=paused), body


def test_pause_resume_require_developer_and_persist_after_restart(board_factory, fake_codex):
    """SOP-44-P04/P05/N02：只允许开发者，重复操作幂等，重启保留暂停。"""
    board = task_board(board_factory, fake_codex)
    push(board)
    original = board.request('GET', BASE + '/state', auth=False)[1]['etag']
    for action in ('pause', 'resume'):
        path = '/api/v1/automation/' + action
        assert_error(board.request('POST', path, auth=False), 401, 'unauthorized')
        assert_error(board.request('POST', path), 403, 'admin-required')
    automation(board, 'pause', True)
    automation(board, 'pause', True)
    paused_etag = board.request('GET', BASE + '/state', auth=False)[1]['etag']
    assert paused_etag != original
    board.stop()
    board.start()
    assert board.sql("SELECT value FROM settings WHERE key='automation_paused'") == [('true',)]
    assert board.request('GET', BASE + '/state', auth=False)[1]['etag'] == paused_etag
    automation(board, 'resume', False)
    automation(board, 'resume', False)
    assert board.sql("SELECT value FROM settings WHERE key='automation_paused'") == [('false',)]
    assert board.request('GET', BASE + '/state', auth=False)[1]['etag'] == original


def test_pause_stops_manual_execution_merges_and_checks_but_running_finishes(board_factory, check_repo, fake_codex):
    """SOP-44-P04：暂停覆盖三种任务，运行中仍登记结果，恢复后全部继续。"""
    repo, presets = check_repo
    checkout(repo)
    document = yaml.safe_load((repo / 'docs/sop/demo.yaml').read_text())
    document['sops'][0]['scenarios'][0].pop('regression')
    (repo / 'docs/sop/demo.yaml').write_text(yaml.safe_dump(document, allow_unicode=True))
    record_commit(repo)
    mode(fake_codex, delay=3)
    board = execution_board(board_factory, fake_codex, repo)
    running = start_execution(board)
    wait_for(lambda: (task_directory(board, running) / 'invocation.json').exists())
    automation(board, 'pause', True)
    pending = start_execution(board, add_batch(board, repo, 'pending'))
    merge = create_task(board, add_batch(board, repo, 'merge'))
    check = start_check(board)
    calls = {p.name: p.read_text() for p in presets.glob('*-calls.jsonl')}
    done = settle(board, running)
    assert done['status'] == '已完成' and done['rounds']
    time.sleep(15)
    for task in (pending, merge, check):
        current = read_task(board, task)
        assert current['status'] == '排队中' and current['started_at'] is None
        assert not (task_directory(board, task) / 'invocation.json').exists()
    assert {p.name: p.read_text() for p in presets.glob('*-calls.jsonl')} == calls
    mode(fake_codex, delay=.2)
    automation(board, 'resume', False)
    for task in (pending, merge, check):
        assert settle(board, task)['status'] == '已完成'


def test_paused_waiting_for_commit_stays_idle_and_new_work_waits_after_restart(board_factory, repo, fake_codex):
    """SOP-44-P04/P05：待提交在暂停期间提交也不开始，重启后的手工任务仍等待。"""
    checkout(repo)
    board = execution_board(board_factory, fake_codex, repo)
    (repo / 'src/app.py').write_text('pending product\n')
    waiting = start_execution(board)
    wait_for(lambda: read_task(board, waiting)['status'] == '待提交')
    automation(board, 'pause', True)
    record_commit(repo)
    time.sleep(6)
    assert read_task(board, waiting)['status'] == '待提交'
    assert not (task_directory(board, waiting) / 'invocation.json').exists()
    automation(board, 'resume', False)
    assert settle(board, waiting)['status'] == '已完成'
    automation(board, 'pause', True)
    board.stop()
    board.start()
    task = start_execution(board, add_batch(board, repo, 'after-restart'))
    time.sleep(15)
    assert read_task(board, task)['started_at'] is None
    assert not (task_directory(board, task) / 'invocation.json').exists()
    automation(board, 'resume', False)
    assert settle(board, task)['status'] == '已完成'
