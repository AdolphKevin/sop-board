"""SOP-42：通过真实 HTTP 发起执行、读取文件与核对持久化轮次。"""
import hashlib
import json

import yaml
import pytest

from codex_helpers import ADMIN, fake_codex, mode
from execution_helpers import checkout, execution_board, start_execution, settle, task_directory, rounds
from helpers import BASE, IDS, independent_fingerprint, push_body, upload, round_body, assert_error
from test_writable_repos import repo, git


def test_developer_execution_snapshot_inputs_session_and_round(board_factory, repo, fake_codex):
    """SOP-42-P08/P09/P10/P12、SOP-43-P01：新会话与已提交快照，证据及轮次可读取。"""
    checkout(repo)
    before = [git(repo, *args) for args in [('status', '--porcelain'), ('branch', '--list'), ('rev-parse', 'HEAD')]]
    board = execution_board(board_factory, fake_codex, repo)
    task = settle(board, start_execution(board))
    assert task['kind'] == 'execute' and task['trigger'] == '开发者发起' and task['status'] == '已完成', task
    assert task['scope'] == [dict(scenario=s, reason='new') for s in IDS]
    directory = task_directory(board, task)
    original = yaml.safe_load((repo / 'docs/sop/demo.yaml').read_text())
    snapshot = task['snapshot']
    assert json.loads((directory / 'snapshot.json').read_text()) == snapshot
    assert snapshot['scenario_file_sha256'] == hashlib.sha256((repo / 'docs/sop/demo.yaml').read_bytes()).hexdigest()
    assert snapshot['scenarios'] == {s['id']: independent_fingerprint(s) for n in original['sops'] for s in n['scenarios']}
    assert snapshot['commit'] == before[-1] and snapshot['cases_sha256'] == ''
    assert len(snapshot['entries_sha256']) == len(snapshot['product_digest']) == 64
    assert yaml.safe_load((directory / 'scenarios.yaml').read_text()) == original
    assert json.loads((directory / 'pending.json').read_text()) == task['scope']
    assert (directory / 'entries.json').is_file() and (directory / 'evidence').is_dir()
    invocation = json.loads((directory / 'invocation.json').read_text())
    args = invocation['argv']
    assert args[:4] == ['exec', '--json', '-s', 'workspace-write']
    assert args[args.index('-c')+1] == 'sandbox_workspace_write.network_access=true'
    assert args[args.index('-C')+1] == str(directory) and '--skip-git-repo-check' in args
    assert invocation['stdin_null'] and invocation['stdin_eof']
    assert set(invocation['env']) <= {'PATH', 'HOME', 'USER', 'LANG', 'TMPDIR', 'CODEX_HOME', 'SHELL'} | {n for n in invocation['env'] if n.startswith('LC_')}
    round_ = rounds(board, task)[0]
    assert len(task['rounds']) == 1 and round_['implementer'] == 'Alice'
    assert round_['product'] == dict(commit=snapshot['commit'], digest=snapshot['product_digest'], dirty=False)
    for row in round_['results']:
        assert row['executor'] == task['session_id'] and row['session'] == str(task['id'])
        assert row['independence'] == 'independent' and row['fingerprint'] == snapshot['scenarios'][row['scenario']]
        e = row['evidence'][0]
        assert board.request('GET', '/api/v1/evidence/' + e['sha256'], auth=False)[1] == ('observed '+row['scenario']).encode()
    assert not list((board.data / 'worktrees').iterdir())
    assert before == [git(repo, *args) for args in [('status', '--porcelain'), ('branch', '--list'), ('rev-parse', 'HEAD')]]


def test_manual_scope_distinguishes_new_changed_failed_and_source_changed(board_factory, repo, fake_codex):
    """SOP-42-P03/N01：旧指纹有结果为 changed；失败可重跑，blocked/skip 不自动重跑。"""
    from copy import deepcopy
    checkout(repo)
    board = execution_board(board_factory, fake_codex, repo)
    original_product = board.request('GET', BASE, auth=False)[1]['product_digest']
    authorized_command = board.command
    board.stop()
    from conftest import ROOT
    board.command = [str(ROOT / '.venv/bin/sop-board')]
    board.start()
    doc = yaml.safe_load((repo / 'docs/sop/demo.yaml').read_text())
    template = doc['sops'][0]['scenarios'][0]
    names = ['new', 'changed', 'failed', 'source', 'passed', 'blocked', 'skipped', 'guarded']
    doc['sops'] = [dict(doc['sops'][0], negative_exempt='本用例只核对范围计算', scenarios=[dict(deepcopy(template), id=name) for name in names])]
    doc['sops'][0]['scenarios'][-1]['regression'] = dict(check='unit', cases=['pkg::test'])
    from execution_helpers import declarations, save_declarations
    value = declarations(repo)
    value['checks'] = dict(unit=dict(argv=['true'], format='go-json'))
    save_declarations(repo, value)
    body = push_body(doc, product_digest=original_product)
    assert board.request('PUT', BASE + '/scenario-file', body)[0] == 200
    evidence = upload(board)
    product = board.request('GET', BASE, auth=False)[1]['product_digest']
    for name, status in [('changed', 'pass'), ('failed', 'fail'), ('source', 'pass'), ('passed', 'pass'), ('blocked', 'blocked'), ('skipped', 'skip'), ('guarded', 'pass')]:
        from datetime import datetime, timezone
        row = dict(scenario=name, fingerprint=body['fingerprints'][name], status=status, finding='fixture reason',
                   actual='seed result', executor='seed executor', executed_at=datetime.now(timezone.utc).isoformat(), evidence=[evidence])
        payload = dict(idempotency_key=name, scenario_file_sha256=hashlib.sha256(body['content'].encode()).hexdigest(),
                       submitted_by='fixture', results=[row], product=dict(commit='c1', digest=product if name in ('passed', 'blocked', 'skipped', 'failed', 'changed') else 'old-product', dirty=False))
        assert board.request('POST', BASE + '/rounds', payload)[0] == 200
    doc['sops'][0]['scenarios'][1]['expected'] = ['changed expected']
    assert board.request('PUT', BASE + '/scenario-file', push_body(doc, product_digest=original_product))[0] == 200
    board.stop()
    board.command = authorized_command
    board.start()
    mode(fake_codex, delay=1)
    task = start_execution(board)
    assert task['scope'] == [dict(scenario=s, reason=r) for s, r in [('new','new'), ('changed','changed'), ('failed','failed'), ('source','source-changed')]]


@pytest.mark.parametrize('settings,problem', [
    (dict(result_ids=['N1-P01']), '缺少结果行'),
    (dict(result_ids=[*IDS, 'N1-P01']), '重复的结果行'),
    (dict(result_ids=[*IDS, 'OUTSIDE']), '不在本轮范围内'),
    (dict(no_results=True), 'results.jsonl 不存在'),
    (dict(status='blocked', no_finding=True), '缺少 finding'),
    (dict(status='skip', no_finding=True), '缺少 finding'),
    (dict(evidence_path='../../outside'), '不在 evidence/ 下'),
    (dict(evidence_path='/etc/hosts'), '不在 evidence/ 下'),
    (dict(evidence_path='notes/a.txt'), '不在 evidence/ 下'),
    (dict(evidence_path='evidence/missing.png'), '证据文件不存在'),
    (dict(raw_results='not JSON'), 'Expecting value'),
    (dict(exit=3), 'Codex 退出码 3'),
])
def test_invalid_results_fail_without_registering(board_factory, repo, fake_codex, settings, problem):
    """SOP-42-N12/N13/N14、SOP-43-N01：契约失败保持零轮次并清理检出目录。"""
    checkout(repo)
    mode(fake_codex, **settings)
    board = execution_board(board_factory, fake_codex, repo)
    done = settle(board, start_execution(board))
    assert done['status'] == '失败' and problem in done['error'], done
    check = json.loads((task_directory(board, done) / 'check.json').read_text())
    assert check['ok'] is False and problem in str(check['problems'])
    assert done['rounds'] == [] and board.sql('SELECT COUNT(*) FROM rounds') == [(0,)]
    assert not list((board.data / 'worktrees').iterdir())


@pytest.mark.parametrize('authorization,status,code', [(None,401,'unauthorized'), ('write',403,'admin-required')])
def test_execution_requires_developer_token(board_factory, repo, fake_codex, authorization, status, code):
    """SOP-42-N03：缺失令牌与写入令牌不能发起执行。"""
    checkout(repo)
    board = execution_board(board_factory, fake_codex, repo)
    assert_error(board.request('POST', BASE + '/executions', {}, auth=authorization == 'write'), status, code)
    assert board.sql('SELECT COUNT(*) FROM tasks') == [(0,)]


def test_evidence_root_symlink_cannot_upload_files_outside_task(board_factory, repo, fake_codex, tmp_path):
    """SOP-42-N13：evidence 目录本身的软链接也不能绕过证据范围。"""
    checkout(repo)
    outside = tmp_path / 'outside-evidence'
    outside.mkdir()
    mode(fake_codex, evidence_root=str(outside))
    board = execution_board(board_factory, fake_codex, repo)
    done = settle(board, start_execution(board))
    assert done['status'] == '失败' and '不在 evidence/ 下' in done['error'], done
    assert done['rounds'] == [] and board.sql('SELECT COUNT(*) FROM evidence') == [(0,)]
