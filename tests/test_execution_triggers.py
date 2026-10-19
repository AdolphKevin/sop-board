"""SOP-42：场景版本与合并写入触发、排队范围并入和同批次串行。"""
from copy import deepcopy
import json

import yaml

from codex_helpers import ADMIN, fake_codex, mode, read_task
from execution_helpers import checkout, execution_board, start_execution, settle, task_directory
from helpers import BASE, IDS, push_body
from test_writable_repos import repo, wait_for
from check_helpers import record_commit


def execution_tasks(board):
    return [t for t in board.request('GET', '/api/v1/tasks', auth=False)[1]['tasks'] if t['kind'] == 'execute']


def test_discovery_and_push_trigger_only_new_changed_and_merge_waiting_scope(board_factory, repo, fake_codex):
    """SOP-42-P02/P04/P07/N05：待提交不占会话，新增与变化并入同批次任务。"""
    checkout(repo)
    board = execution_board(board_factory, fake_codex, repo)
    path = repo / 'docs/sop/demo.yaml'
    document = yaml.safe_load(path.read_text())
    (repo / 'src/app.py').write_text('uncommitted\n')
    document['sops'][0]['scenarios'][0]['expected'] = ['new expectation']
    path.write_text(yaml.safe_dump(document, allow_unicode=True, sort_keys=False))
    first = wait_for(lambda: execution_tasks(board), timeout=8)[0]
    wait_for(lambda: read_task(board, first)['status'] == '待提交')
    assert first['scope'] == [dict(scenario=IDS[0], reason='changed')]
    assert first['trigger'] == '场景版本' and not (task_directory(board, first) / 'invocation.json').exists()
    new = dict(deepcopy(document['sops'][0]['scenarios'][0]), id='NEW-P01')
    document['sops'][0]['scenarios'].append(new)
    path.write_text(yaml.safe_dump(document, allow_unicode=True, sort_keys=False))
    assert board.request('PUT', BASE + '/scenario-file', push_body(document, path='docs/sop/demo.yaml'))[0] == 200
    assert len(execution_tasks(board)) == 1
    assert read_task(board, first)['scope'] == [dict(scenario=IDS[0], reason='changed'), dict(scenario='NEW-P01', reason='new')]
    assert start_execution(board)['id'] == first['id']
    commit = record_commit(repo)
    done = settle(board, first)
    assert done['status'] == '已完成' and done['snapshot']['commit'] == commit


def test_running_scope_is_immutable_and_successor_waits(board_factory, repo, fake_codex):
    """SOP-42-P06/N08：运行中范围不改，新版本使本轮作废，后续任务随后开始。"""
    checkout(repo)
    mode(fake_codex, delay=2)
    board = execution_board(board_factory, fake_codex, repo)
    first = start_execution(board)
    wait_for(lambda: (task_directory(board, first) / 'invocation.json').is_file())
    document = yaml.safe_load((repo / 'docs/sop/demo.yaml').read_text())
    document['sops'][0]['scenarios'][0]['expected'] = ['new expectation']
    assert board.request('PUT', BASE + '/scenario-file', push_body(document))[0] == 200
    tasks = execution_tasks(board)
    assert len(tasks) == 2 and tasks[1]['status'] == '排队中'
    assert tasks[0]['scope'] == first['scope']
    done = settle(board, first)
    assert done['status'] == '已作废' and '场景版本' in done['void_reason']
    successor = settle(board, tasks[1])
    assert successor['status'] == '已完成' and successor['started_at'] >= done['finished_at']


def test_merge_landing_triggers_execution_but_draft_does_not(board_factory, repo, fake_codex):
    """SOP-42-P01/N04：合并写入才触发；第二轮只执行新增与变化。"""
    from merge_helpers import merge_cases, upload_merge, settled
    from cases_helpers import cases_body, upload_cases
    checkout(repo)
    board = execution_board(board_factory, fake_codex, repo)
    first_merge = settled(board, upload_merge(board))
    assert first_merge['status'] == '已完成'
    first = wait_for(lambda: execution_tasks(board))[0]
    assert settle(board, first)['status'] == '已完成'
    document = merge_cases()
    document['test_cases'][0]['expected_results'] = ['changed expected']
    extra = dict(deepcopy(document['test_cases'][0]), id='TC_DEMO_EXTRA_001')
    document['test_cases'].append(extra)
    upload_cases(board, cases_body(document), base=BASE)
    second = wait_for(lambda: ts[-1] if len(ts := execution_tasks(board)) == 2 else None)
    done = settle(board, second)
    assert done['status'] == '已完成' and done['trigger'] == '合并写入'
    assert {s['scenario']:s['reason'] for s in done['scope']} == {document['test_cases'][0]['id']:'changed', extra['id']:'new'}
    assert 'docs/sop/demo.yaml' in __import__('test_writable_repos').git(repo, 'status', '--porcelain')
    readonly = __import__('codex_helpers').task_board(board_factory, fake_codex)
    assert readonly.request('PUT', BASE + '/scenario-file', push_body())[0] == 200
    assert settled(readonly, upload_merge(readonly))['status'] == '已完成'
    assert execution_tasks(readonly) == []
