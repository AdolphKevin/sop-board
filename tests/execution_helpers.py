"""执行入口的隔离夹具；先登记已有版本，授权后由开发者发起任务。"""
import json
import os

import yaml

from check_helpers import record_commit, declarations, save_declarations
from codex_helpers import ADMIN, task_board, read_task
from conftest import ROOT
from helpers import BASE, KEY, push_body
from test_writable_repos import IDENTITY, git, wait_for


def execution_board(factory, fake, repo, **env):
    board = task_board(factory, fake, **env)
    document = yaml.safe_load((repo / 'docs/sop/demo.yaml').read_text())
    body = push_body(document, path='docs/sop/demo.yaml')
    assert board.request('PUT', BASE + '/scenario-file', body)[0] == 200
    board.stop()
    board.command = [str(ROOT / '.venv/bin/sop-board'), '--writable-repo', f'{IDENTITY}={repo}']
    board.start()
    return board


def checkout(repo):
    value = declarations(repo)
    value['entries']['自定义入口'].update(surface='cli', candidate='checkout')
    save_declarations(repo, value)
    git(repo, 'config', 'user.name', 'Alice')
    record_commit(repo)


def start_execution(board, base=BASE):
    status, body, _ = board.request('POST', base + '/executions', {}, headers=ADMIN)
    assert status == 200, body
    return body['task']


def settle(board, task, timeout=20):
    return wait_for(lambda: (t if (t := read_task(board, task))['status'] in
        ('已完成', '失败', '已作废', '已放弃', '已中断', 'Codex 不可用') else None), timeout=timeout)


def task_directory(board, task):
    return board.data / 'tasks' / str(task['id'])


def rounds(board, task):
    return [board.request('GET', BASE + '/rounds/' + str(n), auth=False)[1] for n in task['rounds']]
