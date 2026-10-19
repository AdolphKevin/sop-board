"""可重复的 T2 命令行交回自检：真实看板入口，隔离仓库，固定的 Codex 判断。"""
import json
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory

import yaml

from conftest import Board, ROOT, free_port
from codex_helpers import fake_codex, mode
from helpers import BASE
from merge_helpers import answer, merge_cases, upload_merge, settled, download


def main():
    with TemporaryDirectory(prefix='sop-board-3-t2-') as temporary:
        directory = Path(temporary)
        repo = directory / 'demo-repo'
        repo.mkdir()
        subprocess.run(['git', 'init', '-q', str(repo)], check=True)
        (repo / 'docs/sop').mkdir(parents=True)
        entries = dict(entries={'web': dict(surface='web', operate='打开浏览器登录页',
            observe=['页面文字'], evidence=['截图'], isolation='一次性演示仓库'),
            'api': dict(surface='http', operate='调用订单接口', observe=['响应'],
                        evidence=['响应原文'], isolation='一次性演示仓库')})
        (repo / 'docs/sop/entries.yaml').write_text(yaml.safe_dump(entries, allow_unicode=True), encoding='utf-8')
        executable = fake_codex.__wrapped__(directory)
        cases = merge_cases()
        judgment = answer(cases, verify='web')
        judgment['cases'][2]['verify'] = 'api'
        judgment['cases'][2]['actor'] = '买家'
        judgment['nodes'][1].update(actor='买家', entry='订单接口')
        mode(executable, final=json.dumps(judgment, ensure_ascii=False))
        command = [str(ROOT / '.venv/bin/sop-board'), '--writable-repo',
                   f'github.com/demo-org/demo-app={repo}']
        board = Board(directory / 'data', free_port(), directory / 'server.log', command=command,
                      env={'SOP_BOARD_ADMIN_TOKEN': 'isolated-admin',
                           'PATH': str(executable) + os.pathsep + os.defpath})
        try:
            task = settled(board, upload_merge(board, cases))
            assert task['status'] == '已完成', task
            status, response, _ = board.request('GET', f"/api/v1/tasks/{task['id']}", auth=False)
            assert status == 200
            raw = (repo / 'docs/sop/demo.yaml').read_bytes()
            assert raw == download(board, task)[1]
            status, pairs, _ = download(board, task, 'pairs.json')
            assert status == 200
            assert len(pairs['covered']) == 3
            print('GET /api/v1/tasks/' + str(task['id']) + ' → 200')
            print(json.dumps(response, ensure_ascii=False, indent=2))
            print('docs/sop/demo.yaml（全文）')
            print(raw.decode(), end='')
            print('pairs.json（全文）')
            print(json.dumps(pairs, ensure_ascii=False, indent=2))
            assert subprocess.run(['git', '-C', str(repo), 'rev-parse', '--verify', 'HEAD'],
                                  capture_output=True).returncode != 0
            print('Git 提交：仍无 HEAD，看板没有创建提交')
        finally:
            board.stop()
            assert board.process.poll() is not None
            print('服务已停止')
    assert not directory.exists()
    print('临时目录已删除')


if __name__ == '__main__':
    main()
