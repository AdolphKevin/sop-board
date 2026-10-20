"""T3 交回自检：临时授权仓库、reload 和按工作区文件响应的 HTTP 服务。"""
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from conftest import Board, free_port
from fix_helpers import fix_repo, fix_codex, fix_board, configure, fail, wait_fix, git
from helpers import BASE
from in_place_helpers import prepare_in_place, runtime, trace
from test_writable_repos import repo as repo_fixture


def main():
    with TemporaryDirectory(prefix='sop-board-5-t3-') as temporary:
        directory = Path(temporary)
        root = fix_repo.__wrapped__(repo_fixture.__wrapped__(directory))
        control = directory / 'runtime'
        prepare_in_place(root, control)
        executable = fix_codex.__wrapped__(directory)
        configure(executable, {}, retests=[dict(observe_http=True, expected_content='print(2)\n')])

        def factory(**kwargs):
            return Board(directory / 'data', free_port(), directory / 'server.log', **kwargs)

        with runtime(root, control) as address:
            board = None
            try:
                board, pushed = fix_board(factory, executable, root, APP_URL=address)
                before = git(root, 'rev-parse', 'HEAD')
                fail(board, pushed)
                task = wait_fix(board, '已合入')
                path = f"/api/v1/tasks/{task['id']}"
                status, response, _ = board.request('GET', path, auth=False)
                assert status == 200
                task = response['task']
                commit = git(root, 'rev-parse', 'HEAD')
                assert commit == task['merged_commit'] == task['attempts'][0]['merged_commit']
                assert task['attempt'] == 1 and task['attempts'][0]['outcome'] == '通过'
                records = trace(control)
                assert [r['event'] for r in records] == ['check-start', 'check-end', 'reload-start', 'reload-end']
                assert [r['baseline'] for r in records] == [before, before, commit, commit]
                assert json.loads((control / 'requests.jsonl').read_text()) == dict(content='print(2)\n', commit=commit)
                number = task['rounds'][0]['round']
                status, registered, _ = board.request('GET', BASE + f'/rounds/{number}', auth=False)
                assert status == 200 and registered['product']['commit'] == commit
                assert registered['submitted_by'] == 'TH-R1' and registered['implementer'] == 'TH-1'
                assert [(r['status'], r['note']) for r in registered['results']] == [
                    ('pass', '运行环境按 HEAD 记录，未经核实')]
                print('Codex 使用隔离预设；复测经 HTTP 实际读取开发者工作区，预期内容 print(2)。')
                print(f'GET {path} → 200（无令牌读取）')
                print(json.dumps(response, ensure_ascii=False, indent=2))
                print('git log -2 ' + task['baseline_branch'])
                print(git(root, 'log', '-2', '--format=%h %s', task['baseline_branch']))
                print(f'GET {BASE}/rounds/{number} → 200')
                print(json.dumps(registered, ensure_ascii=False, indent=2))
                print('自检核对：' + json.dumps(dict(status=task['status'], attempt=task['attempt'],
                    baseline=task['baseline_branch'], commit=commit, round=number,
                    check_then_merge_then_reload_then_http_retest=True), ensure_ascii=False))
            finally:
                if board:
                    board.stop()
                    assert board.process.poll() is not None
                    print('看板服务已停止')
        print('运行环境 HTTP 服务已停止')
    assert not directory.exists()
    print('临时目录已删除')


if __name__ == '__main__':
    main()
