"""T4 交回自检：真实修复入口、Git、候选命令、Jira 接收端、页面与报告。"""
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from codex_helpers import ADMIN
from conftest import Board, free_port
from fix_helpers import fix_repo, fix_codex, fix_board, agent, fail, wait_fix, git, configure, all_invocations
from check_helpers import check_repo
from helpers import BASE, IDS, KEY
from html_helpers import Document
from jira_helpers import mock_jira
from test_writable_repos import repo as repo_fixture, wait_for


def main():
    with TemporaryDirectory(prefix='sop-board-5-t4-') as temporary, mock_jira() as jira:
        directory = Path(temporary)
        root = fix_repo.__wrapped__(repo_fixture.__wrapped__(directory))
        root, presets = check_repo.__wrapped__(root, directory)
        executable = fix_codex.__wrapped__(directory)
        configure(executable, {}, retests=[dict(observe_product=True, expected_stdout='2\n')])
        jira.replies['ABC-1'] = (200, dict(fields=dict(summary='结算按钮无响应', description='结算计算存在错误',
            status={'name': '待确认'}, comment={'comments': [{'body': '首次报告'}, {'body': '补充复现'}]},
            attachment=[{'filename': 'checkout.png'}])), 0)
        board = None

        def factory(**kwargs):
            return Board(directory / 'data', free_port(), directory / 'server.log', **kwargs)

        try:
            board, pushed = fix_board(factory, executable, root, paused=True, JIRA_FIXED_STATUS='已修复', **jira.configured_env())
            before = git(root, 'rev-parse', 'HEAD')
            agent(board, pushed, ['pass'] * 4)
            fail(board, pushed, IDS[3], defect='ABC-1')
            board.request('POST', '/api/v1/automation/resume', {}, headers=ADMIN)
            task = wait_fix(board, '已合入')
            wait_for(lambda: len(jira.writes) == 3)
            wait_for(lambda: board.request('GET', f"/api/v1/tasks/{task['id']}", auth=False)[1]['task']['jira'][0]['transition'] == '流转成功')
            path = f"/api/v1/tasks/{task['id']}"
            status, response, _ = board.request('GET', path, auth=False)
            assert status == 200
            task = response['task']
            assert task['attempt'] == 1 and len(task['problems']) == 1
            assert task['attempts'][0]['validation']['status'] == 'pass'
            assert task['session_id'] == 'TH-1'
            commit = task['candidates'][0]['commit_id']
            assert git(root, 'rev-parse', 'HEAD') == commit == task['merged_commit']
            assert git(root, 'rev-list', '--parents', '-1', commit).split() == [commit, before]
            assert git(root, 'status', '--porcelain') == ''
            assert len(jira.requests) == 4
            assert all(jira.authenticated(r) for r in jira.requests)
            assert jira.writes[-1]['body'] == dict(transition=dict(id='31'))
            assert not Path(task['worktree']).exists()
            assert not git(root, 'branch', '--list', task['fix_branch'])
            for name in ('junit', 'go'):
                calls = [json.loads(line) for line in (presets / (name + '-calls.jsonl')).read_text().splitlines()]
                assert len(calls) == 1 and calls[0]['cwd'] == task['worktree'] and calls[0]['commit'] == commit
            calls = all_invocations(executable)
            assert len(calls) == 2 and calls[0]['cwd'] == task['worktree']
            execution = Path(calls[1]['argv'][calls[1]['argv'].index('-o') + 1]).parent
            assert calls[1]['cwd'] == str(execution)
            observed = json.loads((execution / 'evidence/observed.txt').read_text())
            assert observed['code'] == 0 and observed['stdout'] == '2\n'
            state = board.request('GET', BASE, auth=False)[1]
            number = task['candidates'][0]['promoted_round']
            assert number == 3
            promoted = board.request('GET', BASE + f'/rounds/{number}', auth=False)[1]
            assert promoted['product']['commit'] == commit and promoted['product']['digest'] == state['product_digest']
            assert promoted['submitted_by'] == 'TH-R1' and promoted['implementer'] == 'TH-1'
            assert len(promoted['results']) == 4 and all(row['status'] == 'pass' for row in promoted['results'])
            assert board.sql('SELECT COUNT(*) FROM check_results') == [(3,)]
            print('Codex 为隔离预设；复测实际运行候选的 python src/app.py，预期输出为 2。')
            print(f'GET {path} → 200（无令牌读取）')
            print(json.dumps({k: task[k] for k in ('id', 'status', 'error', 'session_id', 'attempt', 'merged_commit', 'jira')}, ensure_ascii=False, indent=2))
            print('git log -1 ' + task['baseline_branch'])
            print(git(root, 'log', '-1', '--format=%h %s%n%an <%ae>', task['baseline_branch']))
            print(f'GET {BASE}/rounds/{number} → 200')
            print(json.dumps(dict(round=number, executor=promoted['submitted_by'], results=[dict(scenario=r['scenario'], status=r['status']) for r in promoted['results']]), ensure_ascii=False))
            print('演示核对：' + json.dumps(dict(task=task['id'], status=task['status'], attempt=task['attempt'],
                baseline=task['baseline_branch'], commit=commit, executor=promoted['submitted_by'], implementer=promoted['implementer'],
                promoted_round=number, result_count=len(promoted['results']), checks=task['attempts'][0]['validation']['checks'],
                candidate_stdout=observed['stdout'], clean=True), ensure_ascii=False))
            print('模拟 Jira 接收记录（认证头仅核对，不输出值）：')
            for request in jira.writes:
                print(json.dumps(dict(method=request['method'], path=request['path'], authenticated=True,
                    body=request['body']), ensure_ascii=False))
            raw = board.request('GET', f'/p/{KEY}/fixes', auth=False)[1]
            page_text = Document(raw).root.one('main').text
            assert '评论成功' in page_text and '流转成功' in page_text
            print('修复页文本片段：')
            print(page_text.split('尝试记录', 1)[0])
            print('Jira 回写结果' + page_text.split('Jira 回写结果', 1)[1].split('开发者说明', 1)[0])
            attention = board.request('GET', f'/api/v1/projects/{KEY}/attention', auth=False, headers={'Host': 'board.local:7890'})
            assert attention[0] == 200 and attention[1] == dict(ok=True, items=[])
            print('attention 接口响应：' + json.dumps(attention[1], ensure_ascii=False))
            report = board.request('GET', BASE + '/report.md', auth=False)[1].decode()
            print('报告的修复记录区块：\n## 修复记录\n' + report.split('## 修复记录\n', 1)[1].split('## 回归覆盖', 1)[0])
        finally:
            if board:
                board.stop()
                assert board.process.poll() is not None
                print('服务已停止')
    assert not directory.exists()
    print('临时目录已删除；模拟 Jira 已停止')


if __name__ == '__main__':
    main()
