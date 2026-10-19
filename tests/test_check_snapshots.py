"""SOP-41/45：只运行已提交产品，变化后作废，停止后清理。"""
import json
import time

import pytest
import yaml

from check_helpers import (check_repo, declared_board, preset, start_check, settle_check, record_commit,
                           declarations, save_declarations)
from codex_helpers import ADMIN, read_task
from helpers import BASE, KEY
from test_writable_repos import repo, git, wait_for


@pytest.mark.parametrize('change', ['tracked', 'untracked', 'deleted', 'staged'])
def test_uncommitted_product_waits_then_runs_committed_snapshot(board_factory, check_repo, change):
    """SOP-45-N07 / I2：已跟踪、未跟踪、删除、暂存改动均待提交，提交后自动运行。"""
    repo, presets = check_repo
    path = repo / ('extra.py' if change == 'untracked' else 'src/app.py')
    if change == 'deleted':
        path.unlink()
    else:
        path.write_text('print(2)\n')
    if change == 'staged':
        git(repo, 'add', '.')
    board = declared_board(board_factory, repo)
    task = start_check(board)
    waiting = wait_for(lambda: (t if (t := read_task(board, task))['status'] == '待提交' else None))
    assert '工作区有未提交的产品改动' in waiting['error']
    assert str(path.relative_to(repo)) in waiting['error']
    assert not (presets / 'junit-calls.jsonl').exists()
    commit = record_commit(repo)
    done = settle_check(board, task)
    assert done['status'] == '已完成' and done['snapshot']['commit'] == commit


def test_docs_and_ignored_files_do_not_block_product(board_factory, check_repo):
    """SOP-45-N07 / I2：只改 docs/sop 或忽略文件不阻止检查。"""
    repo, presets = check_repo
    (repo / '.gitignore').write_text('build/\n')
    record_commit(repo)
    (repo / 'build').mkdir()
    (repo / 'build/ignored.py').write_text('ignored')
    (repo / 'docs/sop/notes.md').write_text('not product')
    board = declared_board(board_factory, repo)
    assert settle_check(board, start_check(board))['status'] == '已完成'


@pytest.mark.parametrize('change,reason', [
    ('product', '产品指纹'), ('head', 'HEAD'), ('scenario', '场景'), ('workspace-scenario', '场景'),
    ('entries', '入口与检查声明'), ('invalid-entries', '入口与检查声明'), ('cases', '用例'),
])
def test_running_snapshot_changes_void_without_registering(board_factory, check_repo, change, reason):
    """SOP-41-N05（检查部分）/ SOP-45-N07：结束与登记前核对全部快照维度。"""
    repo, presets = check_repo
    preset(presets, 'junit', delay=2)
    board = declared_board(board_factory, repo)
    task = start_check(board)
    wait_for(lambda: (presets / 'junit-calls.jsonl').exists())
    if change in ('product', 'head'):
        (repo / 'src/app.py').write_text('changed product\n')
        if change == 'head':
            record_commit(repo)
    elif change in ('entries', 'invalid-entries'):
        value = declarations(repo)
        value['entries']['自定义入口']['candidate'] = 'docker' if change == 'invalid-entries' else 'checkout'
        save_declarations(repo, value)
    elif change in ('scenario', 'workspace-scenario'):
        # 场景发现每 5 秒一次；推送接口同时登记新版本，避免依赖扫描时序。
        from helpers import push_body
        document = yaml.safe_load((repo / 'docs/sop/demo.yaml').read_text())
        document['sops'][0]['scenarios'][0]['expected'] = ['新的业务结果']
        if change == 'workspace-scenario':
            (repo / 'docs/sop/demo.yaml').write_text(yaml.safe_dump(document, allow_unicode=True))
        else:
            body = push_body(document)
            assert board.request('PUT', BASE + '/scenario-file', body)[0] == 200
    else:
        from cases_helpers import cases_body
        assert board.request('POST', BASE + '/cases', cases_body())[0] == 200
    done = settle_check(board, task)
    assert done['status'] == '已作废', done
    assert reason in done['void_reason'] and done['rounds'] == []
    if change == 'head':
        assert '产品指纹' in done['void_reason']
    # 新场景版本可另起执行；只核对这个失效快照没有登记或遗留自己的目录。
    assert board.sql('SELECT COUNT(*) FROM results WHERE session=?', (str(task['id']),)) == [(0,)]
    assert not (board.data / 'worktrees' / str(task['id'])).exists()


def test_queued_invalid_entries_wait_and_resume_automatically(board_factory, check_repo):
    """SOP-41-N04（检查部分）：待提交期间声明损坏则不运行，恢复后自动开始。"""
    repo, presets = check_repo
    (repo / 'src/app.py').write_text('temporary\n')
    board = declared_board(board_factory, repo)
    task = start_check(board)
    wait_for(lambda: read_task(board, task)['status'] == '待提交')
    value = declarations(repo)
    value['entries']['自定义入口']['candidate'] = 'docker'
    save_declarations(repo, value)
    (repo / 'src/app.py').write_text('print(1)\n')
    waiting = wait_for(lambda: (t if '入口声明不合格' in (t := read_task(board, task))['error'] else None), timeout=8)
    assert waiting['started_at'] is None and not (presets / 'junit-calls.jsonl').exists()
    value['entries']['自定义入口'].pop('candidate')
    save_declarations(repo, value)
    assert settle_check(board, task)['status'] == '已完成'


@pytest.mark.parametrize('change', ['removed', 'no-regression'])
def test_waiting_check_with_obsolete_scope_fails_before_launch(board_factory, check_repo, change):
    """SOP-45 I5：排队范围已移除或不再声明检查时，不能无结果地报告完成。"""
    from helpers import push_body
    repo, presets = check_repo
    (repo / 'src/app.py').write_text('pending\n')
    board = declared_board(board_factory, repo)
    task = start_check(board)
    wait_for(lambda: read_task(board, task)['status'] == '待提交')
    document = yaml.safe_load((repo / 'docs/sop/demo.yaml').read_text())
    scene = document['sops'][0]['scenarios'][0]
    if change == 'removed':
        scene['id'] = 'NEW-P01'
    else:
        scene.pop('regression')
    assert board.request('PUT', BASE + '/scenario-file', push_body(document))[0] == 200
    (repo / 'src/app.py').write_text('print(1)\n')
    done = settle_check(board, task)
    assert done['status'] == '失败' and '范围' in done['error'], done
    assert done['rounds'] == [] and not (board.data / 'tasks' / str(task['id']) / 'checks').exists()
    calls = presets / 'junit-calls.jsonl'
    if calls.exists():
        assert all(call['output'] != str(board.data / 'tasks' / str(task['id']) / 'checks/junit')
                   for call in map(json.loads, calls.read_text().splitlines()))
