"""T2 实际交回演示：临时授权仓库、两种候选入口、开发者请求与轮次读取。"""
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory

import yaml

from candidate_helpers import start_entry
from codex_helpers import ADMIN, fake_codex, mode
from conftest import Board, ROOT, free_port
from execution_helpers import checkout, settle, start_execution, rounds
from helpers import BASE, push_body
from check_helpers import record_commit
from test_writable_repos import repo as repo_fixture, git


def main():
    with TemporaryDirectory(prefix='sop-board-4-t2-') as temporary:
        directory = Path(temporary)
        repo = repo_fixture.__wrapped__(directory)
        checkout(repo)
        start_entry(repo, directory)
        path = repo / 'docs/sop/demo.yaml'
        document = yaml.safe_load(path.read_text())
        for scene in document['sops'][1]['scenarios']:
            scene['verify'] = 'api'
        path.write_text(yaml.safe_dump(document, allow_unicode=True, sort_keys=False))
        record_commit(repo)
        executable = fake_codex.__wrapped__(directory)
        mode(executable, address_names=['APP_URL'])
        board = Board(directory / 'data', free_port(), directory / 'server.log',
            env={'SOP_BOARD_ADMIN_TOKEN':'isolated-admin', 'PATH':str(executable)+os.pathsep+os.defpath})
        try:
            # 先准备已登记版本，授权后的这一轮由开发者请求发起。
            assert board.request('PUT', BASE+'/scenario-file', push_body(document, path='docs/sop/demo.yaml'))[0] == 200
            board.stop()
            board.command = [str(ROOT / '.venv/bin/sop-board'), '--writable-repo', f'github.com/demo-org/demo-app={repo}']
            board.start()
            print(board.log.read_text(), end='')
            task = settle(board, start_execution(board))
            assert task['status'] == '已完成' and len(task['rounds']) == 1, task
            print('POST '+BASE+'/executions（开发者令牌已脱敏） → 200')
            print('GET /api/v1/tasks/'+str(task['id'])+' → 200')
            print(json.dumps(dict(ok=True, task=task), ensure_ascii=False, indent=2))
            for saved in rounds(board, task):
                print('GET '+BASE+'/rounds/'+str(saved['round'])+' → 200')
                print(json.dumps(saved, ensure_ascii=False, indent=2))
            assert len(git(repo, 'worktree', 'list').splitlines()) == 1
            assert git(repo, 'status', '--porcelain') == ''
            print('检出目录已清理；授权仓库工作区干净')
        finally:
            board.stop()
            print('服务已停止')
    assert not directory.exists()
    print('临时目录已删除')


if __name__ == '__main__':
    main()
