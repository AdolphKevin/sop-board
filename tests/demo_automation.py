"""T3 交回演示：真实本机服务、脱敏请求、页面状态与清理；不使用真实账号。"""
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory

import yaml

from codex_helpers import ADMIN, fake_codex, mode, read_task
from conftest import Board, ROOT, free_port
from execution_helpers import checkout, start_execution, task_directory
from helpers import BASE, KEY, push_body
from html_helpers import Document
from test_automation import add_batch
from test_writable_repos import repo as repo_fixture, wait_for
from test_codex_tasks import process_alive


def main():
    with TemporaryDirectory(prefix='sop-board-4-t3-', dir='/private/tmp') as temporary:
        directory = Path(temporary)
        repo = repo_fixture.__wrapped__(directory)
        checkout(repo)
        executable = fake_codex.__wrapped__(directory)
        mode(executable, delay=60)
        board = Board(directory / 'data', free_port(), directory / 'server.log',
                      env={'SOP_BOARD_ADMIN_TOKEN': ADMIN['Authorization'].removeprefix('Bearer '),
                           'PATH': str(executable) + os.pathsep + os.defpath})
        running = None
        try:
            document = yaml.safe_load((repo / 'docs/sop/demo.yaml').read_text())
            assert board.request('PUT', BASE + '/scenario-file', push_body(document, path='docs/sop/demo.yaml'))[0] == 200
            board.stop()
            board.command = [str(ROOT / '.venv/bin/sop-board'), '--writable-repo', f'github.com/demo-org/demo-app={repo}']
            board.start()
            print('启动：<仓库>/.venv/bin/sop-board --data <临时目录>/data --port <空闲端口> --writable-repo github.com/demo-org/demo-app=<临时目录>/demo-repo')
            print(board.log.read_text(), end='')
            running = start_execution(board)
            wait_for(lambda: (task_directory(board, running) / 'invocation.json').exists())
            (repo / 'src/app.py').write_text('uncommitted demo product\n')
            pending = start_execution(board, add_batch(board, repo, 'pending'))
            wait_for(lambda: read_task(board, pending)['status'] == '待提交')

            def show_pages():
                status, raw, _ = board.request('GET', '/', auth=False)
                home = Document(raw).root
                print(f'GET / → {status}')
                notice = home.one(id='automation-state')
                print('首页暂停提示：' + (notice.text if 'hidden' not in notice.attrs else '无'))
                print('首页批次状态：' + '；'.join(row.text for row in home.find_all('li')))
                for name in ('demo', 'pending'):
                    status, raw, _ = board.request('GET', f'/p/{KEY}/b/{name}', auth=False)
                    state = Document(raw).root.one(id='agent-state').text
                    print(f'GET /p/{KEY}/b/{name} → {status}\n批次总览状态：{state}')

            for action, paused in [('pause', True), ('resume', False)]:
                print(f'POST /api/v1/automation/{action}\nAuthorization: Bearer ***\n请求正文：{{}}')
                status, body, _ = board.request('POST', '/api/v1/automation/' + action, {}, headers=ADMIN)
                print(f'HTTP {status}\n' + json.dumps(body, ensure_ascii=False))
                assert status == 200 and body == dict(ok=True, paused=paused)
                print('数据库暂停值：' + repr(board.sql("SELECT value FROM settings WHERE key='automation_paused'")))
                show_pages()
        finally:
            board.stop()
            assert board.process.poll() is not None
            if running and (invocation := task_directory(board, running) / 'invocation.json').exists():
                assert not process_alive(json.loads(invocation.read_text())['pid'])
            assert not list((board.data / 'worktrees').glob('*'))
            print('服务已停止；假 Codex 进程已退出，检出目录已清理。')
    assert not directory.exists()
    print('临时数据目录与演示仓库已删除。')


if __name__ == '__main__':
    main()
