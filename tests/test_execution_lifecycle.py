"""SOP-42：待提交、快照作废、重试放弃及执行调度。"""
import json
import time

import pytest
import yaml

from check_helpers import record_commit, declarations, save_declarations
from codex_helpers import ADMIN, fake_codex, mode, read_task
from execution_helpers import checkout, execution_board, start_execution, settle, task_directory
from helpers import BASE, IDS, KEY, assert_error, push_body
from test_writable_repos import repo, git, wait_for
from test_codex_tasks import process_alive


@pytest.mark.parametrize('change,label', [('uncommitted','工作区产品指纹'), ('commit','HEAD 提交'), ('head-only','HEAD 提交'),
    ('scenario','场景版本'), ('entry','入口与检查声明'), ('check','入口与检查声明'), ('invalid','入口与检查声明'), ('cases','用例并集')])
def test_each_snapshot_change_voids_and_preserves_original_snapshot(board_factory, repo, fake_codex, change, label):
    """SOP-42-N07/N08/N09/N10/N11、SOP-41-N05：每个变化维度分别作废且不登记结果。"""
    checkout(repo)
    mode(fake_codex, delay=2)
    board = execution_board(board_factory, fake_codex, repo)
    task = start_execution(board)
    wait_for(lambda: (task_directory(board, task) / 'invocation.json').exists())
    snapshot = json.loads((task_directory(board, task) / 'snapshot.json').read_text())
    if change == 'head-only':
        record_commit(repo)
    elif change in ('uncommitted','commit'):
        (repo / 'src/app.py').write_text('changed product\n')
        if change == 'commit':
            record_commit(repo)
    elif change == 'scenario':
        document = yaml.safe_load((repo / 'docs/sop/demo.yaml').read_text())
        document['sops'][0]['scenarios'][0]['expected'] = ['new expected']
        assert board.request('PUT', BASE + '/scenario-file', push_body(document))[0] == 200
    elif change in ('entry','check','invalid'):
        value = declarations(repo)
        if change == 'check':
            value['checks'] = dict(unit=dict(argv=['true'], format='go-json', timeout=2))
        else:
            value['entries']['自定义入口'].update(**(dict(candidate='docker') if change == 'invalid' else dict(observe=['new observation'])))
        save_declarations(repo, value)
    else:
        from cases_helpers import cases_body
        mode(fake_codex, delay=60)
        assert board.request('POST', BASE + '/cases', cases_body())[0] == 200
    done = settle(board, task)
    assert done['status'] == '已作废' and label in done['void_reason'], done
    assert done['snapshot'] == snapshot and done['rounds'] == []
    if change == 'head-only':
        assert '产品指纹' not in done['void_reason']
    assert board.sql('SELECT COUNT(*) FROM rounds') == [(0,)]
    if change == 'scenario':
        assert IDS[0] in done['void_reason']


@pytest.mark.parametrize('change', ['tracked','staged','untracked','ignored'])
def test_uncommitted_waits_lists_ten_paths_and_can_be_abandoned(board_factory, repo, fake_codex, change):
    """SOP-42-N05/N06/P07/P16：含暂存与未跟踪；被忽略文件不阻止执行。"""
    checkout(repo)
    (repo / '.gitignore').write_text('build/\n')
    record_commit(repo)
    board = execution_board(board_factory, fake_codex, repo)
    if change in ('tracked','staged'):
        (repo / 'src/app.py').write_text('uncommitted\n')
        if change == 'staged':
            git(repo, 'add', '.')
    elif change == 'untracked':
        for n in range(12):
            (repo / f'src/new_{n:02}.py').write_text('new\n')
    else:
        (repo / 'build').mkdir()
        (repo / 'build/out.bin').write_bytes(b'ignored')
    task = start_execution(board)
    if change == 'ignored':
        assert settle(board, task)['status'] == '已完成'
        return
    waiting = wait_for(lambda: t if (t := read_task(board, task))['status'] == '待提交' else None)
    paths = waiting['error'].split('：')[1].split('、')
    assert len(paths) == (10 if change == 'untracked' else 1)
    assert not (task_directory(board, task) / 'invocation.json').exists()
    assert not (board.data / 'worktrees' / str(task['id'])).exists()
    if change == 'untracked':
        assert board.request('POST', f"/api/v1/tasks/{task['id']}/cancel", {}, headers=ADMIN)[0] == 200
        assert read_task(board, task)['status'] == '已放弃'
    else:
        commit = record_commit(repo)
        done = settle(board, task)
        assert done['status'] == '已完成' and done['snapshot']['commit'] == commit


def test_retry_renews_snapshot_and_archives_every_attempt_artifact(board_factory, repo, fake_codex):
    """SOP-42-P15/N15：失败重试保留原范围，归档证据，运行与完成不能重试。"""
    checkout(repo)
    mode(fake_codex, result_ids=[IDS[0]])
    board = execution_board(board_factory, fake_codex, repo)
    failed = settle(board, start_execution(board))
    assert failed['status'] == '失败'
    (repo / 'src/app.py').write_text('new commit\n')
    commit = record_commit(repo)
    mode(fake_codex, delay=1)
    assert board.request('POST', f"/api/v1/tasks/{failed['id']}/retry", {}, headers=ADMIN)[0] == 200
    wait_for(lambda: read_task(board, failed)['status'] == '运行中')
    assert_error(board.request('POST', f"/api/v1/tasks/{failed['id']}/retry", {}, headers=ADMIN), 409, 'task-state-invalid')
    done = settle(board, failed)
    assert done['status'] == '已完成' and done['scope'] == failed['scope'] and done['snapshot']['commit'] == commit
    assert len(done['rounds']) == 1 and (task_directory(board, done) / 'attempt-1/evidence').is_dir()
    assert_error(board.request('POST', f"/api/v1/tasks/{failed['id']}/retry", {}, headers=ADMIN), 409, 'task-state-invalid')
    assert_error(board.request('POST', '/api/v1/tasks/999999/retry', {}, headers=ADMIN), 404, 'task-not-found')


def test_cancel_running_kills_session_group_and_removes_worktree(board_factory, repo, fake_codex):
    """SOP-42-N16、SOP-43-N01：放弃运行中的会话，主工作区无变化。"""
    checkout(repo)
    mode(fake_codex, delay=60, child=True)
    board = execution_board(board_factory, fake_codex, repo)
    task = start_execution(board)
    directory = task_directory(board, task)
    wait_for(lambda: (directory / 'child.pid').exists())
    parent = json.loads((directory / 'invocation.json').read_text())['pid']
    child = int((directory / 'child.pid').read_text())
    assert board.request('POST', f"/api/v1/tasks/{task['id']}/cancel", {}, headers=ADMIN)[0] == 200
    wait_for(lambda: not (board.data / 'worktrees' / str(task['id'])).exists())
    assert read_task(board, task)['status'] == '已放弃'
    assert not process_alive(parent) and not process_alive(child)
    assert board.sql('SELECT COUNT(*) FROM rounds') == [(0,)]


def test_browser_waiting_scope_merges_and_cancel_releases_lock(board_factory, repo, fake_codex):
    """SOP-42-P05/P16：等待浏览器时并入范围且可以放弃，其他批次不因它的版本变化作废。"""
    checkout(repo)
    value = declarations(repo)
    value['entries']['自定义入口']['surface'] = 'web'
    save_declarations(repo, value)
    record_commit(repo)
    mode(fake_codex, delay=3)
    board = execution_board(board_factory, fake_codex, repo)
    first = start_execution(board)
    wait_for(lambda: (task_directory(board, first) / 'invocation.json').exists())
    doc = yaml.safe_load((repo / 'docs/sop/demo.yaml').read_text())
    doc['metadata']['batch'] = 'other'
    base = BASE.replace('/demo', '/other')
    assert board.request('PUT', base + '/scenario-file', push_body(doc))[0] == 200
    second = start_execution(board, base)
    wait_for(lambda: read_task(board, second)['status'] == '等待浏览器')
    doc['sops'][0]['scenarios'][0]['id'] = 'OTHER-NEW'
    assert board.request('PUT', base + '/scenario-file', push_body(doc))[0] == 200
    assert start_execution(board, base)['id'] == second['id']
    assert 'OTHER-NEW' in [s['scenario'] for s in read_task(board, second)['scope']]
    assert board.request('POST', f"/api/v1/tasks/{second['id']}/cancel", {}, headers=ADMIN)[0] == 200
    assert settle(board, first)['status'] == '已完成'
    assert read_task(board, second)['status'] == '已放弃'
    assert not (task_directory(board, second) / 'invocation.json').exists()
