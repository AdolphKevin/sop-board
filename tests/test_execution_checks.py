"""SOP-45：执行开始时同快照运行回归检查，回归场景不交给 Codex。"""
import json
import time

import pytest
import yaml

from check_helpers import check_repo, preset, record_commit, declarations, save_declarations
from codex_helpers import ADMIN, fake_codex, mode, read_task
from execution_helpers import execution_board, start_execution, settle, task_directory, rounds
from helpers import BASE, IDS, upload, round_body, assert_error
from test_writable_repos import repo, wait_for, git


@pytest.mark.parametrize('candidate', ['checkout', 'in-place'])
def test_execution_runs_regressions_once_in_shared_snapshot(board_factory, check_repo, fake_codex, candidate):
    """SOP-45-P04/P07：范围中的回归只运行检查，与执行共享快照，原地入口用 -check 目录。"""
    repo, presets = check_repo
    value = declarations(repo)
    value['entries']['自定义入口']['candidate'] = candidate
    save_declarations(repo, value)
    record_commit(repo)
    mode(fake_codex, delay=.2)
    board = execution_board(board_factory, fake_codex, repo)
    done = settle(board, start_execution(board))
    assert done['status'] == '已完成' and len(done['rounds']) == 3, done
    directory = task_directory(board, done)
    doc = yaml.safe_load((directory / 'scenarios.yaml').read_text())
    assert [s['id'] for n in doc['sops'] for s in n['scenarios']] == ['N2-N01']
    for name in ('junit','go'):
        calls = [json.loads(line) for line in (presets / (name+'-calls.jsonl')).read_text().splitlines()]
        assert len(calls) == 1
        assert calls[0]['cwd'] == str(board.data / 'worktrees' / (str(done['id']) + ('-check' if candidate == 'in-place' else '')))
        assert calls[0]['commit'] == done['snapshot']['commit']
    saved = rounds(board, done)
    assert {r['results'][0]['executor'].split()[0] for r in saved} >= {'回归检查'}
    assert all(r['product'] == dict(commit=done['snapshot']['commit'], digest=done['snapshot']['product_digest'], dirty=False) for r in saved)
    assert not list((board.data / 'worktrees').iterdir())


def test_regression_only_scope_does_not_start_codex(board_factory, check_repo, fake_codex):
    """SOP-45-P05：范围只有回归时不启动会话，无待执行时为 nothing-to-execute。"""
    repo, presets = check_repo
    path = repo / 'docs/sop/demo.yaml'
    document = yaml.safe_load(path.read_text())
    document['sops'][1]['scenarios'][1]['regression'] = dict(check='go', cases=['example.com/app::TestCreate'])
    path.write_text(yaml.safe_dump(document, allow_unicode=True))
    record_commit(repo)
    board = execution_board(board_factory, fake_codex, repo)
    done = settle(board, start_execution(board))
    assert done['status'] == '已完成' and len(done['rounds']) == 2, done
    assert not (task_directory(board, done) / 'invocation.json').exists()
    assert done['session_id'] is None
    assert_error(board.request('POST', BASE + '/executions', {}, headers=ADMIN), 409, 'nothing-to-execute')


def test_product_changed_guard_runs_even_outside_automatic_scope(board_factory, check_repo, fake_codex):
    """SOP-45-P04：自动范围不扩大，但因产品变化待确认的守护检查仍运行。"""
    from test_execution_triggers import execution_tasks
    repo, presets = check_repo
    board = execution_board(board_factory, fake_codex, repo)
    body = __import__('helpers').push_body(yaml.safe_load((repo / 'docs/sop/demo.yaml').read_text()))
    evidence = upload(board)
    seeded = round_body(body, evidence, statuses=['pass'] * 4)
    seeded['product']['digest'] = 'old-product'
    assert board.request('POST', BASE + '/rounds', seeded)[0] == 200
    document = yaml.safe_load((repo / 'docs/sop/demo.yaml').read_text())
    document['sops'][1]['scenarios'][1]['expected'] = ['changed nonregression expected']
    path = repo / 'docs/sop/demo.yaml'
    path.write_text(yaml.safe_dump(document, allow_unicode=True))
    assert board.request('PUT', BASE + '/scenario-file', __import__('helpers').push_body(document))[0] == 200
    task = wait_for(lambda: ts[-1] if (ts := execution_tasks(board)) else None)
    done = settle(board, task)
    assert done['scope'] == [dict(scenario='N2-N01', reason='changed')]
    assert done['status'] == '已完成' and len(done['rounds']) == 3, done


def test_checks_complete_while_three_execution_sessions_are_running(board_factory, check_repo, fake_codex):
    """SOP-45-P06：三个真实执行任务占满会话名额时，独立检查仍能完成。"""
    from check_helpers import start_check, settle_check
    from helpers import demo, push_body
    from codex_helpers import task_board
    from conftest import ROOT
    from test_writable_repos import IDENTITY
    repo, presets = check_repo
    mode(fake_codex, delay=60)
    board = task_board(board_factory, fake_codex)
    raw = (repo / 'docs/sop/demo.yaml').read_text()
    assert board.request('PUT', BASE+'/scenario-file', push_body(yaml.safe_load(raw), content=raw,
                        path='docs/sop/demo.yaml'))[0] == 200
    bases = []
    for index in range(3):
        document = demo()
        document['metadata']['batch'] = f'busy{index}'
        base = BASE.replace('/demo', '/'+document['metadata']['batch'])
        assert board.request('PUT', base+'/scenario-file', push_body(document))[0] == 200
        bases.append(base)
    # 版本已登记后才授权，避免准备时的自动执行和手工执行同时进入队列。
    board.stop()
    board.command = [str(ROOT / '.venv/bin/sop-board'), '--writable-repo', f'{IDENTITY}={repo}']
    board.start()
    tasks = [start_execution(board, base) for base in bases]
    assert len(board.request('GET', '/api/v1/tasks', auth=False)[1]['tasks']) == 3
    wait_for(lambda: all(read_task(board, task)['status'] == '运行中' for task in tasks))
    done = settle_check(board, start_check(board))
    assert done['status'] == '已完成' and all(read_task(board, task)['status'] == '运行中' for task in tasks)
