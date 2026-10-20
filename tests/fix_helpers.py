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
from sop_board.repositories import product

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
        executable.write_text('#!/usr/bin/perl\nuse JSON::PP; exec ' + json.dumps(sys.executable, ensure_ascii=False) +
            ', ' + json.dumps(str(driver), ensure_ascii=False) + ', encode_json([sort keys %ENV]), @ARGV;\n')
    else:
        executable.write_text('#!' + sys.executable + '\n' + driver.read_text())
    executable.chmod(0o755)
    configure(directory, {})
    return directory


def configure(directory, *calls, **settings):
    settings.setdefault('retests', [dict(delay=30)])
    (directory / 'mode.json').write_text(json.dumps(dict(calls=calls, **settings)))


def all_invocations(directory):
    path = directory / 'invocations.jsonl'
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def invocations(directory):
    return [call for call in all_invocations(directory) if '/validations/' not in call['argv'][call['argv'].index('-o') + 1]]


def wait_invocations(directory, count):
    calls = wait_for(lambda: value if len(value := all_invocations(directory)) >= count else None)
    assert len(calls) == count, calls
    return calls


def fix_board(factory, fake, repo, *, authorize=True, paused=False, validation=None, **env):
    board = factory(env=dict(SOP_BOARD_ADMIN_TOKEN='isolated-admin',
        PATH=str(fake) + os.pathsep + os.defpath, **env))
    # 先登记全部初始批次；否则授权后的工作区发现会给未登记的批次创建普通执行任务。
    entry = repo / ('.tw/config.json' if (repo / '.tw/config.json').exists() else 'docs/sop/entries.yaml')
    for path in sorted((repo / 'docs/sop').glob('*.yaml')):
        if path.name == 'entries.yaml':
            continue
        document = yaml.safe_load(path.read_text())
        initial = push_body(document, path=str(path.relative_to(repo)), content=path.read_text(),
                            **product(repo, path, entry))
        base = BASE.replace('/demo', '/' + document['metadata']['batch'])
        assert board.request('PUT', base + '/scenario-file', initial)[0] == 200
        if document['metadata']['batch'] == 'demo':
            body = initial
    if paused:
        assert board.request('POST', '/api/v1/automation/pause', {}, headers=ADMIN)[0] == 200
    if authorize:
        board.stop()
        if validation is not None:
            path = fake / 'mode.json'
            value = json.loads(path.read_text())
            value['retests'] = [dict(delay=p.get('delay', 0), statuses={sid: 'fail' for sid in IDS} if p.get('status') == 'fail' else {}) for p in validation]
            path.write_text(json.dumps(value))
        command = [str(ROOT / '.venv/bin/sop-board')]
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
