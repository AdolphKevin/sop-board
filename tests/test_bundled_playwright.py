"""内置驱动经真实 CLI / HTTP 入口绑定原批次，不依赖外部流程工具。"""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
import yaml

from conftest import ROOT, TEST_ENV
from codex_helpers import fake_codex
from test_writable_repos import repo
from helpers import BASE, IDS, KEY, independent_fingerprint, push_body


def cli(*args, env=None):
    values = {**TEST_ENV, **(env or {})}
    return subprocess.run([str(ROOT / '.venv/bin/sop-board'), *map(str, args)], env=values,
        capture_output=True, text=True, timeout=30, cwd=ROOT)


def prepare(tmp_path, board):
    body = push_body()
    assert board.request('PUT', BASE + '/scenario-file', body)[0] == 200
    source = tmp_path / 'scenarios.yaml'
    source.write_text(body['content'], encoding='utf-8')
    scope = tmp_path / 'scope.json'
    result = cli('playwright', 'scope', '--project', body['project']['id'], '--scenario-file', source, '--out', scope)
    assert result.returncode == 0, result.stderr
    folder = tmp_path / 'round'
    (folder / 'evidence').mkdir(parents=True)
    (folder / 'evidence/截图.png').write_bytes(b'isolated-artifact')
    rows = [dict(scenario=sid, status='pass', actual='按场景核对通过',
        executed_at=datetime.now(timezone.utc).isoformat(), executor='independent-session',
        evidence=['evidence/截图.png']) for sid in IDS]
    results = folder / 'results.jsonl'
    results.write_text(''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in rows), encoding='utf-8')
    product = tmp_path / 'product.json'
    product.write_text(json.dumps(dict(commit='candidate-commit', product_digest='candidate-digest', dirty=False)))
    args = ['playwright', 'submit', '--scope', scope, '--scenario-file', source, '--results', results,
        '--product', product, '--idempotency-key', 'manual-run-1', '--submitted-by', 'reviewer']
    env = dict(SOP_BOARD_TOKEN='isolated-test', SOP_BOARD_URL=f'http://127.0.0.1:{board.port}')
    return args, env, source, scope, results, rows


def test_guide_and_help_need_no_token_and_resolve_packaged_resources():
    result = cli('guide', env={'SOP_BOARD_TOKEN': ''})
    assert result.returncode == 0 and 'skills/playwright-acceptance/SKILL.md' in result.stdout
    skill = ROOT / 'src/sop_board/skills/playwright-acceptance'
    for name in ['SKILL.md', 'scripts/to_scenarios.py', 'templates/connection.mjs', 'templates/fixtures.mjs', 'references/contracts.md']:
        assert (skill / name).is_file()
    for action in ['convert', 'tags', 'submit']:
        result = cli('playwright', action, '--help')
        assert result.returncode == 0 and 'usage:' in result.stdout


def test_scope_uses_source_fingerprints_and_pending_subset(tmp_path):
    body = push_body()
    source = tmp_path / 'source.yaml'
    source.write_text(body['content'], encoding='utf-8')
    pending = tmp_path / 'pending.json'
    pending.write_text(json.dumps([dict(scenario=IDS[1], reason='new')]))
    # 非默认文件名不能临时覆盖相邻 scope.json。
    (tmp_path / 'scope.json').write_text('keep')
    out = tmp_path / 'selected.json'
    result = cli('playwright', 'scope', '--project', 'repo', '--scenario-file', source,
        '--pending', pending, '--out', out)
    assert result.returncode == 0, result.stderr
    scope = json.loads(out.read_text())
    assert scope['scenarios'] == {IDS[1]: body['fingerprints'][IDS[1]]}
    assert scope['scenario_file_sha256'] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert (tmp_path / 'scope.json').read_text() == 'keep'


def test_manual_submission_registers_evidence_and_is_idempotent_in_original_batch(board, tmp_path):
    args, env, *_ = prepare(tmp_path, board)
    first = cli(*args, env=env)
    assert first.returncode == 0, first.stderr
    assert json.loads(first.stdout)['round'] == 1
    again = cli(*args, env=env)
    assert again.returncode == 0 and json.loads(again.stdout)['round'] == 1
    assert board.sql('SELECT COUNT(*) FROM rounds') == [(1,)]
    assert board.sql('SELECT COUNT(*) FROM results') == [(4,)]
    assert board.sql('SELECT COUNT(*) FROM result_evidence') == [(4,)]
    status, current, _ = board.request('GET', BASE + '/scenarios', auth=False)
    assert status == 200 and all(row['status'] == 'pass' for row in current['scenarios'])


@pytest.mark.parametrize('kind', ['missing', 'duplicate', 'outside-path', 'future-time', 'token', 'scope-version', 'board-version'])
def test_invalid_manual_inputs_create_no_round_or_evidence(board, tmp_path, kind):
    args, env, source, scope, results, rows = prepare(tmp_path, board)
    if kind == 'missing':
        rows.pop()
    elif kind == 'duplicate':
        rows[-1]['scenario'] = rows[0]['scenario']
    elif kind == 'outside-path':
        (tmp_path / 'secret.png').write_bytes(b'do not upload')
        rows[0]['evidence'] = ['../secret.png']
    elif kind == 'future-time':
        rows[0]['executed_at'] = '2999-01-01T00:00:00Z'
    elif kind == 'token':
        env['SOP_BOARD_TOKEN'] = ''
    elif kind == 'scope-version':
        source.write_text(source.read_text() + '\n# changed\n')
    else:
        body = push_body()
        doc = yaml.safe_load(body['content'])
        doc['sops'][0]['scenarios'][0]['expected'] = ['new business expectation']
        assert board.request('PUT', BASE + '/scenario-file', push_body(doc))[0] == 200
    results.write_text(''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in rows), encoding='utf-8')
    result = cli(*args, env=env)
    assert result.returncode == 1 and 'Traceback' not in result.stderr
    assert board.sql('SELECT COUNT(*) FROM rounds') == [(0,)]
    assert board.sql('SELECT COUNT(*) FROM evidence') == [(0,)]


def test_bundled_converter_regressions():
    skill = ROOT / 'src/sop_board/skills/playwright-acceptance'
    result = subprocess.run([sys.executable, '-m', 'unittest', 'discover', '-s', str(skill / 'tests'), '-q'],
        capture_output=True, text=True, env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'}, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


def test_execution_inputs_bind_web_subset_to_original_batch_and_builtin_driver(board_factory, repo, fake_codex):
    from check_helpers import declarations, save_declarations, record_commit
    from codex_helpers import mode
    from execution_helpers import checkout, execution_board, start_execution, settle, task_directory
    checkout(repo)
    value = declarations(repo)
    value['entries']['自定义入口']['surface'] = 'web'
    save_declarations(repo, value)
    record_commit(repo)
    mode(fake_codex)
    board = execution_board(board_factory, fake_codex, repo)
    task = settle(board, start_execution(board))
    assert task['status'] == '已完成'
    directory = task_directory(board, task)
    scope = json.loads((directory / 'scope.json').read_text())
    source = directory / 'scenarios.yaml'
    assert scope['project'] == KEY and scope['batch'] == 'demo'
    assert scope['scenario_file_sha256'] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert scope['scenarios'] == task['snapshot']['scenarios']
    prompt = (directory / 'prompt.md').read_text()
    assert str(ROOT / 'src/sop_board/skills/playwright-acceptance/SKILL.md') in prompt
    assert 'sop-board playwright convert' in prompt and 'tw team' not in prompt
