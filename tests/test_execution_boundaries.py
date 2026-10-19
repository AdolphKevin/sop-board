"""SOP-41/42：错误声明、空范围、只读项目、会话与提示词边界。"""
import json

import pytest
import yaml

from check_helpers import record_commit, declarations, save_declarations
from codex_helpers import ADMIN, fake_codex, mode, read_task, task_board
from execution_helpers import checkout, execution_board, start_execution, settle, task_directory, rounds
from helpers import BASE, KEY, assert_error, push_body
from test_writable_repos import repo, wait_for, git


def test_readonly_execution_and_empty_scope_have_no_side_effects(board_factory, repo, fake_codex):
    """SOP-42-N01/N02/N03：未授权与无待执行不建任务，完成的通过场景不会再次执行。"""
    checkout(repo)
    readonly = task_board(board_factory, fake_codex)
    assert readonly.request('PUT', BASE + '/scenario-file', push_body())[0] == 200
    assert_error(readonly.request('POST', BASE + '/executions', {}, headers=ADMIN), 409, 'repo-not-writable')
    assert readonly.sql('SELECT COUNT(*) FROM tasks') == [(0,)]
    board = execution_board(board_factory, fake_codex, repo)
    assert settle(board, start_execution(board))['status'] == '已完成'
    assert_error(board.request('POST', BASE + '/executions', {}, headers=ADMIN), 409, 'nothing-to-execute')
    assert board.sql('SELECT COUNT(*) FROM tasks') == [(1,)]


def test_manual_latest_failure_is_pending_but_latest_blocked_is_not(board_factory, repo, fake_codex):
    """SOP-42-P03/N01：最新结果包含人工复核轨道，人工失败可执行，人工受阻不重跑。"""
    checkout(repo)
    board = execution_board(board_factory, fake_codex, repo)
    first = settle(board, start_execution(board))
    for sid, status in [('N1-P01','fail'), ('N1-N01','blocked')]:
        body = dict(scenario=sid, fingerprint=first['snapshot']['scenarios'][sid], status=status,
                    marker='tester', defect_url='https://github.com/demo-org/demo-app/issues/1')
        assert board.request('POST', BASE + '/marks', body)[0] == 200
    task = start_execution(board)
    assert task['scope'] == [dict(scenario='N1-P01', reason='failed')]


@pytest.mark.parametrize('bad', ['candidate','reference'])
def test_invalid_entries_reject_new_execution_and_queue_resumes_after_repair(board_factory, repo, fake_codex, bad):
    """SOP-41-N01/N03/N04：待提交任务等声明恢复，开发者新请求返回 entries-invalid。"""
    checkout(repo)
    board = execution_board(board_factory, fake_codex, repo)
    (repo / 'src/app.py').write_text('pending\n')
    task = start_execution(board)
    wait_for(lambda: read_task(board, task)['status'] == '待提交')
    value = declarations(repo)
    if bad == 'candidate':
        value['entries']['自定义入口']['candidate'] = 'docker'
    else:
        value['entries'] = {}
    save_declarations(repo, value)
    waiting = wait_for(lambda: t if '入口声明不合格' in (t := read_task(board, task))['error'] else None)
    assert waiting['started_at'] is None
    assert_error(board.request('POST', BASE + '/executions', {}, headers=ADMIN), 409, 'entries-invalid')
    assert not (task_directory(board, task) / 'invocation.json').exists()
    value['entries'] = {'自定义入口':dict(surface='cli', candidate='checkout', operate='run', observe=['response'], evidence=['output'], isolation='temp')}
    save_declarations(repo, value)
    (repo / 'src/app.py').write_text('print(1)\n')
    assert settle(board, task)['status'] == '已完成'


def test_codex_unavailable_and_voided_executions_retry_original_scope(board_factory, repo, fake_codex):
    """SOP-42-P15/N14/N17：不可用与已作废均能重试，快照重新固定。"""
    checkout(repo)
    mode(fake_codex, version_exit=3)
    board = execution_board(board_factory, fake_codex, repo)
    failed = settle(board, start_execution(board))
    assert failed['status'] == 'Codex 不可用' and failed['rounds'] == []
    mode(fake_codex, delay=1)
    assert board.request('POST', f"/api/v1/tasks/{failed['id']}/retry", {}, headers=ADMIN)[0] == 200
    wait_for(lambda: (task_directory(board, failed) / 'invocation.json').exists())
    (repo / 'src/app.py').write_text('new product\n')
    voided = settle(board, failed)
    assert voided['status'] == '已作废'
    commit = record_commit(repo)
    mode(fake_codex)
    assert board.request('POST', f"/api/v1/tasks/{failed['id']}/retry", {}, headers=ADMIN)[0] == 200
    done = settle(board, failed)
    assert done['status'] == '已完成' and done['scope'] == failed['scope'] and done['snapshot']['commit'] == commit


def test_inplace_no_worktree_missing_implementer_and_driver_prompt(board_factory, repo, fake_codex):
    """SOP-42-P11/P13/P14、SOP-43-P03：无实现者省略，默认及自定义技能目录、原地入口不建工作树。"""
    checkout(repo)
    value = declarations(repo)
    value['entries']['自定义入口'].update(candidate='in-place', address_env='FIXTURE_URL')
    save_declarations(repo, value)
    git(repo, 'config', 'user.name', '')
    record_commit(repo)
    mode(fake_codex, address_names=['FIXTURE_URL'])
    board = execution_board(board_factory, fake_codex, repo, FIXTURE_URL='http://127.0.0.1:9999')
    first = settle(board, start_execution(board))
    assert first['status'] == '已完成' and not (board.data / 'worktrees' / str(first['id'])).exists()
    saved = rounds(board, first)[0]
    assert not saved.get('implementer') and all(r['independence'] == 'undeclared' for r in saved['results'])
    prompt = (task_directory(board, first) / 'prompt.md').read_text()
    for term in ('playwright-acceptance','verify-backend-sop-db','build-scenario-validation-loop','临时 HOME','operate','results.jsonl','evidence/','.agents/skills'):
        assert term in prompt
    assert all(r['note'] == '运行环境按 HEAD 记录，未经核实' for r in saved['results'])
    board.stop()
    board.env['SOP_BOARD_SKILLS_DIR'] = '/tmp/skills-x'
    board.start()
    doc = yaml.safe_load((repo / 'docs/sop/demo.yaml').read_text())
    doc['sops'][0]['scenarios'][0]['expected'] = ['changed expected']
    # 产品指纹排除 docs/sop；此版本自动建立新执行任务。
    assert board.request('PUT', BASE + '/scenario-file', push_body(doc))[0] == 200
    tasks = board.request('GET', '/api/v1/tasks', auth=False)[1]['tasks']
    second = settle(board, tasks[-1])
    assert second['status'] == '已完成' and second['session_id'] != first['session_id']
    assert '/tmp/skills-x' in (task_directory(board, second) / 'prompt.md').read_text()
