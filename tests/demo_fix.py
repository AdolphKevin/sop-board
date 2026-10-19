"""T1 交回演示：本机 Jira、真实 HTTP、临时授权仓库与修复提交。"""
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from codex_helpers import ADMIN
from conftest import Board, free_port
from fix_helpers import fix_repo, fix_codex, fix_board, agent, fail, wait_fix, git
from helpers import BASE
from jira_helpers import mock_jira
from test_writable_repos import repo as repo_fixture, wait_for


def main():
    with TemporaryDirectory(prefix='sop-board-5-t1-') as temporary, mock_jira() as jira:
        directory = Path(temporary)
        root = fix_repo.__wrapped__(repo_fixture.__wrapped__(directory))
        executable = fix_codex.__wrapped__(directory)
        jira.replies['ABC-1'] = (200, dict(fields=dict(summary='结算按钮无响应', description='结算计算存在错误',
            status={'name': '待确认'}, comment={'comments': [{'body': '首次报告'}, {'body': '补充复现'}]},
            attachment=[{'filename': 'checkout.png'}])), 0)
        board = None

        def factory(**kwargs):
            return Board(directory / 'data', free_port(), directory / 'server.log', **kwargs)

        try:
            board, pushed = fix_board(factory, executable, root, paused=True, **jira.configured_env())
            before = git(root, 'rev-parse', 'HEAD')
            agent(board, pushed, ['pass', 'fail', 'pass', 'pass'])
            fail(board, pushed, defect='ABC-1')
            board.request('POST', '/api/v1/automation/resume', {}, headers=ADMIN)
            task = wait_fix(board, '复测中')
            path = f"/api/v1/tasks/{task['id']}"
            response = wait_for(lambda: value if (value := board.request('GET', path, auth=False)[1])['task']['attempts'][0]['validation'] else None)
            task = response['task']
            assert task['attempt'] == 1 and len(task['problems']) == 2
            assert task['attempts'][0]['validation']['status'] == 'pending'
            assert task['session_id'] == 'TH-1'
            assert git(root, 'rev-parse', 'HEAD') == before
            assert git(root, 'status', '--porcelain') == ''
            assert len(jira.requests) == 1
            print(board.log.read_text(), end='')
            print(f'GET {path} → 200（无令牌读取）')
            print(json.dumps(response, ensure_ascii=False, indent=2))
            print("git log -1 --format='%an <%ae>%n%B' " + task['fix_branch'])
            print(git(root, 'log', '-1', '--format=%an <%ae>%n%B', task['fix_branch']))
            print(json.dumps(dict(id=task['id'], kind=task['kind'], status=task['status'], attempt=task['attempt'],
                session_id=task['session_id'], problems=[dict(batch=p['batch'], scenario=p['scenario'], category=p['category']) for p in task['problems']],
                commit=task['attempts'][0]['commit_id'], validation=task['attempts'][0]['validation']), ensure_ascii=False))
            print('基线提交与主工作区核对通过；验证占位未合入')
        finally:
            if board:
                board.stop()
                assert board.process.poll() is not None
                print('服务已停止')
    assert not directory.exists()
    print('临时目录已删除；模拟 Jira 已停止')


if __name__ == '__main__':
    main()
