"""修复测试经 HTTP、真实 SQLite 与临时 Git 仓库观察结果。"""
import json
import os
import sys

import pytest
import yaml

from check_helpers import record_commit
from codex_helpers import ADMIN, read_task
from conftest import ROOT
from helpers import BASE, KEY, IDS, push_body, round_body, upload
from test_writable_repos import repo, git, wait_for, IDENTITY

FIXES = f'/api/v1/projects/{KEY}/fixes'


@pytest.fixture
def fix_repo(repo):
    data = yaml.safe_load((repo / 'docs/sop/entries.yaml').read_text())
    data['entries']['自定义入口'].update(surface='cli', candidate='checkout')
    (repo / 'docs/sop/entries.yaml').write_text(yaml.safe_dump(data, allow_unicode=True))
    git(repo, 'config', 'user.name', 'Dev One')
    git(repo, 'config', 'user.email', 'dev1@example.com')
    record_commit(repo)
    return repo


@pytest.fixture
def fix_codex(tmp_path):
    directory = tmp_path / 'fix-bin'
    directory.mkdir()
    driver = directory / 'driver.py'
    driver.write_text((ROOT / 'tests/fix_codex_driver.py').read_text())
    executable = directory / 'codex'
    if sys.platform == 'darwin':
        executable.write_text('#!/usr/bin/perl\nuse JSON::PP; exec ' + json.dumps(sys.executable) +
            ', ' + json.dumps(str(driver)) + ', encode_json([sort keys %ENV]), @ARGV;\n')
    else:
        executable.write_text('#!' + sys.executable + '\n' + driver.read_text())
    executable.chmod(0o755)
    configure(directory, {})
    return directory


def configure(directory, *calls, **settings):
    (directory / 'mode.json').write_text(json.dumps(dict(calls=calls, **settings)))


def invocations(directory):
    path = directory / 'invocations.jsonl'
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def fix_board(factory, fake, repo, *, authorize=True, paused=False, validation=None, **env):
    board = factory(env=dict(SOP_BOARD_ADMIN_TOKEN='isolated-admin',
        PATH=str(fake) + os.pathsep + os.defpath, **env))
    body = push_body(yaml.safe_load((repo / 'docs/sop/demo.yaml').read_text()), path='docs/sop/demo.yaml')
    assert board.request('PUT', BASE + '/scenario-file', body)[0] == 200
    if paused:
        assert board.request('POST', '/api/v1/automation/pause', {}, headers=ADMIN)[0] == 200
    if authorize:
        board.stop()
        if validation is not None:
            preset = fake / 'validation.json'
            preset.write_text(json.dumps(validation))
            board.env['FIX_TEST_VALIDATION'] = str(preset)
        command = [sys.executable, str(ROOT / 'tests/fix_validation_server.py')] if validation is not None else [str(ROOT / '.venv/bin/sop-board')]
        board.command = [*command, '--writable-repo', f'{IDENTITY}={repo}']
        board.start()
    return board, body


def fail(board, pushed, sid=IDS[0], *, defect='https://github.com/demo-org/demo-app/issues/1', status='fail', note='点击结算无响应', base=BASE):
    response = board.request('POST', base + '/marks', dict(scenario=sid,
        fingerprint=pushed['fingerprints'][sid], status=status, marker='Tester', defect_url=defect, note=note))
    assert response[0] == 200, response


def agent(board, pushed, statuses, *, base=BASE, key='failures', executor=None):
    value = round_body(pushed, upload(board), statuses=statuses, key=key)
    value['product']['digest'] = board.request('GET', base, auth=False)[1]['product_digest']
    if executor:
        for row in value['results']:
            row['executor'] = executor
    response = board.request('POST', base + '/rounds', value)
    assert response[0] == 200, response
    return response[1]['round']


def fixes(board):
    status, body, _ = board.request('GET', FIXES, auth=False)
    assert status == 200, body
    return body['tasks']


def wait_fix(board, state=None):
    return wait_for(lambda: next((t for t in fixes(board) if state is None or t['status'] == state), None), timeout=20)


def wait_state(board, task, state):
    return wait_for(lambda: (t if (t := read_task(board, task))['status'] == state else None), timeout=20)
