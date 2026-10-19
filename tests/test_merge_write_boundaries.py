"""SOP-34：落地前核对工作区，保留正在发生的开发者修改。"""
import hashlib
import json
import time

import yaml

from codex_helpers import ADMIN, fake_codex, mode, task_board, create_task
from helpers import BASE, KEY, push, push_body, assert_error
from merge_helpers import upload_merge, settled, checked, download
from test_writable_repos import repo, wait_for


def test_changed_workspace_waits_for_sync_and_discovers_a_second_task(board_factory, fake_codex, repo):
    """SOP-34-N01：旧任务不覆盖修改；五秒发现新版本并排新任务，合并页同时展示。"""
    mode(fake_codex, delay=6)
    board = task_board(board_factory, fake_codex, repo)
    path = repo / 'docs/sop/demo.yaml'
    task = upload_merge(board)
    wait_for(lambda: (board.data / 'tasks' / str(task['id']) / 'invocation.json').exists())
    edited = path.read_text() + '# 开发者的新版本\n'
    path.write_text(edited)
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    task = settled(board, task)
    assert task['status'] == '待同步'
    assert checked(board, task)['workspace_changed'] is True
    assert hashlib.sha256(path.read_bytes()).hexdigest() == sha
    wait_for(lambda: board.sql('SELECT COUNT(*) FROM tasks') == [(2,)], timeout=7)
    tasks = board.request('GET', '/api/v1/tasks', auth=False)[1]['tasks']
    assert tasks[1]['trigger'] == '场景文件变化' and tasks[1]['scenario_file_sha256'] == sha
    assert board.sql('SELECT pushed_by FROM versions') == [('工作区',), ('工作区',)]
    page = board.request('GET', f'/p/{KEY}/b/demo/merges', auth=False)[1].decode()
    assert f"#{task['id']}" in page and f"#{tasks[1]['id']}" in page and '待同步' in page
    assert download(board, task)[0] == 200 and download(board, task, 'pairs.json')[0] == 200
    # 及时停止，避免第二个任务的合法写入遮盖旧任务没有覆盖修改这一观察点。
    board.stop()


def test_readonly_missing_entries_fails_and_inference_cannot_expand_authority(board_factory, fake_codex):
    """I4 输入边界：未授权项目没有推送入口时失败，不允许模型推断扩大范围。"""
    from merge_helpers import answer
    result = answer()
    result['entries'] = {'entries': {'自定义入口': dict(surface='http', operate='调用',
        observe=['响应'], evidence=['原文'], isolation='临时目录')}}
    mode(fake_codex, final=json.dumps(result, ensure_ascii=False))
    board = task_board(board_factory, fake_codex)
    body = push_body()
    body.pop('entries')
    push(board, body)
    task = settled(board, upload_merge(board))
    assert task['status'] == '失败'
    assert task['error'] == '缺少入口声明：请随场景文件推送 entries'
    assert_error(download(board, task), 404, 'merge-result-not-found')


def test_same_version_overwrite_increments_once_and_cannot_read_cross_batch(board_factory, fake_codex, repo):
    """SOP-34-P03/P02：已有版本相同才能覆盖，版本只增一，下载不可跨批次。"""
    head = (repo / '.git/HEAD').read_text()
    board = task_board(board_factory, fake_codex, repo)
    task = settled(board, upload_merge(board))
    assert task['status'] == '已完成'
    assert board.sql('SELECT version,pushed_by FROM versions') == [(1, '工作区'), (2, f"看板合并 #{task['id']}")]
    assert (repo / 'docs/sop/demo.yaml').read_bytes() == download(board, task)[1]
    assert (repo / '.git/HEAD').read_text() == head
    for suffix in ('scenario-file', 'pairs.json'):
        assert_error(board.request('GET', BASE.replace('/demo', '/other') + f"/merges/{task['id']}/" + suffix, auth=False), 404, 'merge-result-not-found')


def test_workspace_crlf_is_compared_as_original_bytes(board_factory, fake_codex, repo):
    """SOP-34-P03：合并依据的摘要取原始字节；CRLF 文件相同也应写入而非误报待同步。"""
    path = repo / 'docs/sop/demo.yaml'
    path.write_bytes(path.read_bytes().replace(b'\n', b'\r\n'))
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    board = task_board(board_factory, fake_codex, repo)
    task = settled(board, upload_merge(board))
    assert task['scenario_file_sha256'] == sha
    assert task['status'] == '已完成'


def test_batch_path_cannot_write_outside_scenario_directory(board_factory, fake_codex, repo):
    """I4 落地边界：批次名不能经 .. 绕出 docs/sop，失败时不写任何工作区文件。"""
    board = task_board(board_factory, fake_codex, repo)
    before = {str(p.relative_to(repo)): p.read_bytes() for p in repo.rglob('*') if p.is_file()}
    task = settled(board, upload_merge(board, base=BASE.replace('/demo', '/..%2Fescape')))
    assert task['status'] == '失败'
    assert '场景文件路径' in str(checked(board, task)['problems'])
    assert {str(p.relative_to(repo)): p.read_bytes() for p in repo.rglob('*') if p.is_file()} == before
