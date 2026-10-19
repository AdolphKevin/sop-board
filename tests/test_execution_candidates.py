"""SOP-43：候选服务就绪、启动失败和运行环境地址传递。"""
import json
import socket
import time

import pytest
import yaml

from candidate_helpers import start_entry
from codex_helpers import ADMIN, fake_codex, mode, read_task
from execution_helpers import checkout, execution_board, start_execution, settle, task_directory, rounds
from check_helpers import record_commit
from test_writable_repos import repo, git, wait_for
from test_codex_tasks import process_alive


def select_entries(repo, names):
    path = repo / 'docs/sop/demo.yaml'
    document = yaml.safe_load(path.read_text())
    for scene, name in zip([s for n in document['sops'] for s in n['scenarios']], names):
        scene['verify'] = name
    path.write_text(yaml.safe_dump(document, allow_unicode=True, sort_keys=False))
    record_commit(repo)


def listening(port):
    with socket.socket() as sock:
        return sock.connect_ex(('127.0.0.1', int(port))) == 0


@pytest.mark.parametrize('outcome', ['success', 'exit', 'timeout'])
def test_start_candidate_response_failure_timeout_and_cleanup(board_factory, repo, fake_codex, tmp_path, outcome):
    """SOP-43-P02/N02/N03：500 也就绪；失败与 120 秒超时登记 fail 并清理整个进程组。"""
    checkout(repo)
    preset = start_entry(repo, tmp_path)
    preset.write_text(json.dumps(dict(mode=outcome)))
    select_entries(repo, ['api'] * 4)
    mode(fake_codex, delay=.5, address_names=['APP_URL'])
    board = execution_board(board_factory, fake_codex, repo)
    started = time.monotonic()
    done = settle(board, start_execution(board), timeout=135)
    elapsed = time.monotonic() - started
    assert done['status'] == '已完成' and len(done['rounds']) == 1, done
    call = json.loads(preset.with_suffix('.call').read_text())
    assert call['cwd'] == str(board.data / 'worktrees' / str(done['id']))
    assert call['address'] == 'http://127.0.0.1:' + call['port']
    assert not {'SOP_BOARD_TOKEN','SOP_BOARD_ADMIN_TOKEN','JIRA_API_TOKEN','MY_SECRET'} & set(call['env'])
    assert not process_alive(call['pid']) and not listening(call['port'])
    if 'child' in call:
        assert not process_alive(call['child'])
    rows = rounds(board, done)[0]['results']
    if outcome == 'success':
        invocation = json.loads((task_directory(board, done) / 'invocation.json').read_text())
        assert invocation['addresses'] == dict(APP_URL=call['address'])
        assert all(r['status'] == 'pass' for r in rows)
    else:
        assert not (task_directory(board, done) / 'invocation.json').exists()
        assert all(r['status'] == 'fail' and r['executor'] == '候选启动 api' for r in rows)
        assert all(r['actual'].startswith('候选版本启动失败：') and r['evidence'][0]['name'] == 'start.log' for r in rows)
        if outcome == 'timeout':
            assert 119 <= elapsed < 133 and all('就绪超时' in r['actual'] for r in rows)
        else:
            assert all('退出码 2' in r['actual'] and 'listen failed: address in use' in r['actual'] for r in rows)
    assert not list((board.data / 'worktrees').iterdir())


@pytest.mark.parametrize('live', [None, 'http://127.0.0.1:9999'])
def test_inplace_and_failed_start_share_one_round_with_other_entries(board_factory, repo, fake_codex, tmp_path, live):
    """SOP-42-P14、SOP-43-N04/N05/P03：入口失败隔离，原地地址缺失受阻并附说明。"""
    checkout(repo)
    preset = start_entry(repo, tmp_path)
    preset.write_text('{"mode":"exit"}')
    select_entries(repo, ['api', 'live', '自定义入口', '自定义入口'])
    mode(fake_codex, address_names=['LIVE_URL'])
    board = execution_board(board_factory, fake_codex, repo, **({'LIVE_URL':live} if live else {}))
    done = settle(board, start_execution(board))
    assert done['status'] == '已完成' and len(done['rounds']) == 1, done
    rows = {r['scenario']:r for r in rounds(board, done)[0]['results']}
    assert rows['N1-P01']['status'] == 'fail' and rows['N1-P01']['executor'] == '候选启动 api'
    assert rows['N1-N01']['note'] == '运行环境按 HEAD 记录，未经核实'
    if live:
        assert rows['N1-N01']['status'] == 'pass'
    else:
        assert rows['N1-N01']['status'] == 'blocked' and rows['N1-N01']['finding'] == '看板环境缺少 LIVE_URL'
    assert rows['N2-P01']['status'] == 'pass' and not rows['N2-P01'].get('note')
    invocation = json.loads((task_directory(board, done) / 'invocation.json').read_text())
    assert invocation['addresses'] == (dict(LIVE_URL=live) if live else {})


def test_board_exit_kills_start_process_group_and_restart_interrupts(board_factory, repo, fake_codex, tmp_path):
    """SOP-43-N06、SOP-42-N17：服务退出与重启时无遗留候选服务和工作树。"""
    checkout(repo)
    preset = start_entry(repo, tmp_path)
    select_entries(repo, ['api'] * 4)
    mode(fake_codex, delay=60)
    board = execution_board(board_factory, fake_codex, repo)
    task = start_execution(board)
    wait_for(lambda: (task_directory(board, task) / 'invocation.json').exists())
    call = json.loads(preset.with_suffix('.call').read_text())
    board.stop()
    assert not process_alive(call['pid']) and not process_alive(call['child']) and not listening(call['port'])
    orphan = board.data.resolve() / 'worktrees/leftover'
    git(repo, 'worktree', 'add', '--detach', str(orphan), 'HEAD')
    board.start()
    assert read_task(board, task)['status'] == '已中断' and not list(orphan.parent.iterdir())


def test_inplace_preserves_existing_service_process(board_factory, repo, fake_codex, tmp_path):
    """SOP-43-P03：已有 HTTP 环境在任务结束和看板退出后仍运行。"""
    import os
    import subprocess
    import sys
    from conftest import free_port
    from sop_board.tasks import terminate_group
    checkout(repo)
    start_entry(repo, tmp_path)
    select_entries(repo, ['live'] * 4)
    port = free_port()
    external = subprocess.Popen([sys.executable, '-m', 'http.server', str(port), '--bind', '127.0.0.1'],
        cwd=tmp_path, env={'PATH':os.defpath}, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        wait_for(lambda: listening(port))
        board = execution_board(board_factory, fake_codex, repo, LIVE_URL=f'http://127.0.0.1:{port}')
        done = settle(board, start_execution(board))
        assert done['status'] == '已完成' and external.poll() is None and listening(port)
        assert not (board.data / 'worktrees' / str(done['id'])).exists()
        board.stop()
        assert external.poll() is None and listening(port)
    finally:
        terminate_group(external)
