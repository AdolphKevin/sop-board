"""SOP-57：经真实修复入口核对清理和 Jira 实际收到的请求。"""
from pathlib import Path
import json
import time
import os
import shutil
import sys
import pytest

from codex_helpers import ADMIN, read_task
from jira_helpers import mock_jira
from helpers import KEY

from fix_helpers import fix_repo, fix_codex, fix_board, fail, wait_fix, wait_state, configure, git
from helpers import BASE, IDS
from test_writable_repos import repo, wait_for


def test_merged_fix_cleans_worktree_branch_and_keeps_retest_queue(board_factory, fix_codex, fix_repo):
    """SOP-57-P01：合入的提交保留，只移除任务目录与分支。"""
    # 在外部 Git 边界延迟分支删除，让“目录已删、分支仍在”的中间阶段稳定可见。
    real_git = shutil.which('git', path=os.defpath)
    deleting = fix_codex / 'deleting-branch'
    wrapper = fix_codex / 'git'
    wrapper.write_text('#!' + sys.executable + '\n' +
        'import os,sys,time\nfrom pathlib import Path\n' +
        "if sys.argv[-3:-1] == ['branch', '-d']:\n" +
        f'    Path({str(deleting)!r}).touch()\n    time.sleep(2)\n' +
        f'os.execv({real_git!r}, [{real_git!r}, *sys.argv[1:]])\n')
    wrapper.chmod(0o755)
    configure(fix_codex, {}, retests=[{}])
    board, pushed = fix_board(board_factory, fix_codex, fix_repo)
    fail(board, pushed)
    task = wait_fix(board, '已合入')
    wait_for(deleting.exists)
    wait_for(lambda: not Path(task['worktree']).exists() and
        not git(fix_repo, 'branch', '--list', task['fix_branch']))
    assert not git(fix_repo, 'branch', '--list', task['fix_branch'])
    assert task['worktree'] not in git(fix_repo, 'worktree', 'list')
    assert git(fix_repo, 'rev-parse', 'HEAD') == task['merged_commit']
    assert board.request('GET', BASE, auth=False)[1]['queues']['retest'] == [IDS[0]]
    board.stop()
    board.start()
    assert not Path(task['worktree']).exists()


def test_cleanup_uses_the_worktree_where_baseline_was_merged(board_factory, fix_codex, fix_repo, tmp_path):
    """SOP-57-P01、I7/I4：基线在另一检出目录时也安全删除修复分支。"""
    configure(fix_codex, {}, retests=[dict(delay=2)])
    board, pushed = fix_board(board_factory, fix_codex, fix_repo)
    fail(board, pushed)
    task = wait_fix(board, '复测中')
    git(fix_repo, 'switch', '-c', 'developer')
    target = tmp_path / 'baseline'
    git(fix_repo, 'worktree', 'add', str(target), task['baseline_branch'])
    task = wait_state(board, task, '已合入')
    wait_for(lambda: not git(fix_repo, 'branch', '--list', task['fix_branch']))
    assert not Path(task['worktree']).exists()
    assert git(target, 'rev-parse', 'HEAD') == task['merged_commit']


def test_fix_jira_posts_document_and_selects_destination_name_once(board_factory, fix_codex, fix_repo):
    """SOP-57-P02：逐单写 ADF 评论，选择目标状态名而非操作名；重启不重复。"""
    with mock_jira() as jira:
        configure(fix_codex, {}, retests=[{}])
        board, pushed = fix_board(board_factory, fix_codex, fix_repo, paused=True,
            JIRA_FIXED_STATUS='已修复', **jira.configured_env())
        for sid, key in zip(IDS[:3], ['ABC-1', 'ABC-2', 'ABC-1']):
            fail(board, pushed, sid, defect=key)
        board.request('POST', '/api/v1/automation/resume', {}, headers=ADMIN)
        task = wait_fix(board, '已合入')
        task = wait_for(lambda: t if len((t := read_task(board, task)).get('jira', [])) == 2 and
            all(r['comment'] != '评论结果待人工确认' for r in t['jira']) else None)
        for key in ('ABC-1', 'ABC-2'):
            calls = [r for r in jira.writes if f'/issue/{key}/' in r['path']]
            assert [(r['method'], r['path'].split('/')[-1]) for r in calls] == [
                ('POST', 'comment'), ('GET', 'transitions'), ('POST', 'transitions')]
            assert all(jira.authenticated((r['path'], r['headers'])) for r in calls)
            assert calls[2]['body'] == {'transition': {'id': '31'}}
            doc = calls[0]['body']['body']
            assert doc['version'] == 1 and doc['type'] == 'doc'
            assert all(p['type'] == 'paragraph' for p in doc['content'])
            comment = '\n'.join(c['text'] for p in doc['content'] for c in p['content'])
            for value in ('折扣计算共用错误逻辑', 'src/app.py', task['attempts'][0]['commit_id'],
                          task['baseline_branch'], IDS[0], 'pass', '尚未部署到测试环境'):
                assert value in comment
        assert [(r['comment'], r['transition']) for r in task['jira']] == [('评论成功', '流转成功')] * 2
        page = board.request('GET', f'/p/{KEY}/fixes', auth=False)[1].decode()
        assert 'ABC-1' in page and 'ABC-2' in page and '评论成功' in page and '流转成功' in page
        assert board.sql('SELECT COUNT(*) FROM fix_jira') == [(2,)]
        assert board.request('GET', f'/api/v1/projects/{KEY}/attention', auth=False)[1]['items'] == []
        count = len(jira.writes)
        board.stop()
        board.start()
        assert len(jira.writes) == count


@pytest.mark.parametrize('mode', ['unreachable', 'unset', 'http-error', 'disconnect', 'unconfigured', 'other-links'])
def test_fix_jira_manual_results_never_undo_merge_or_retry(board_factory, fix_codex, fix_repo, mode):
    """SOP-57-N01/N02/N03/N04/N05：失败不回退，回显认证内容也不得存储。"""
    with mock_jira() as jira:
        env = jira.configured_env()
        if mode == 'unconfigured':
            env.pop('JIRA_API_TOKEN')
        if mode != 'unset':
            env['JIRA_FIXED_STATUS'] = '已修复'
        if mode == 'unreachable':
            jira.transitions['ABC-1'] = [dict(id='41', name='已修复', to=dict(name='已关闭'))]
        if mode in ('http-error', 'disconnect'):
            jira.write_replies['/rest/api/3/issue/ABC-1/comment'] = 500 if mode == 'http-error' else 'disconnect'
        configure(fix_codex, {}, retests=[{}])
        board, pushed = fix_board(board_factory, fix_codex, fix_repo, **env)
        fail(board, pushed, defect='https://github.com/demo-org/demo-app/issues/1' if mode == 'other-links' else 'ABC-1')
        task = wait_fix(board, '已合入')
        if mode == 'other-links':
            assert task.get('jira') == [] and jira.writes == []
            return
        task = wait_for(lambda: t if (t := read_task(board, task)).get('jira') and
            t['jira'][0]['comment'] != '评论结果待人工确认' else None)
        row = task['jira'][0]
        if mode == 'unconfigured':
            assert row['comment'] == row['transition'] == 'Jira 未配置，未回写'
            assert jira.requests == []
        elif mode in ('unset', 'unreachable'):
            assert row['comment'] == '评论成功' and row['transition'] == 'Jira 状态待人工更新'
            assert [r['method'] for r in jira.writes] == (['POST'] if mode == 'unset' else ['POST', 'GET'])
        else:
            assert row['comment'] == '评论失败' and row['reason'] and row['transition'] == '流转成功'
            assert len(jira.writes) == 3
        items = board.request('GET', f'/api/v1/projects/{KEY}/attention', auth=False)[1]['items']
        assert len(items) == 1 and items[0]['kind'] == 'jira-manual' and items[0]['task'] == task['id']
        assert board.request('GET', BASE, auth=False)[1]['queues']['retest'] == [IDS[0]]
        for path in (f'/p/{KEY}/fixes', f'/api/v1/projects/{KEY}/fixes', f"/api/v1/tasks/{task['id']}"):
            value = board.request('GET', path, auth=False)[1]
            raw = value if isinstance(value, bytes) else json.dumps(value).encode()
            assert jira.token.encode() not in raw and jira.basic.encode() not in raw
        assert row['comment'] in board.request('GET', f'/p/{KEY}/fixes', auth=False)[1].decode()
        count = len(jira.writes)
        time.sleep(5.2)
        assert read_task(board, task)['status'] == '已合入' and len(jira.writes) == count
        board.stop()
        for path in [*board.data.rglob('*'), board.log]:
            if path.is_file():
                raw = path.read_bytes()
                assert jira.token.encode() not in raw and jira.basic.encode() not in raw, path
