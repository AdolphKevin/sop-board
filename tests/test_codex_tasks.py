"""SOP-31/32：运行器通过真实 HTTP 入口、假 codex 与落库结果验证。"""
import json

from codex_helpers import ADMIN, fake_codex, task_board, create_task, read_task, finished
from helpers import BASE, KEY, assert_error, push


def test_developer_auth_and_task_files(board_factory, fake_codex):
    """SOP-31-N02、SOP-32-P01：权限拒绝无副作用；精确命令、空 stdin、白名单、原始文件、会话。"""
    board = task_board(board_factory, fake_codex)
    push(board)
    for path in (BASE + '/merges', '/api/v1/tasks/99/retry', '/api/v1/tasks/99/cancel'):
        assert_error(board.request('POST', path, {}, auth=False), 401, 'unauthorized')
        assert_error(board.request('POST', path, {}), 403, 'admin-required')
    assert board.request('GET', '/api/v1/tasks', auth=False)[1]['tasks'] == []
    task = finished(board, create_task(board))
    assert task['kind'] == 'merge' and task['project'] == KEY and task['batch'] == 'demo'
    import hashlib
    assert task['result_digest'] == hashlib.sha256((board.data / 'tasks' / str(task['id']) / 'final.json').read_bytes()).hexdigest()
    assert task['created_at'] <= task['started_at'] <= task['finished_at']
    assert task['session_id'] == 'fixture-thread-' + str(task['id'])
    directory = board.data / 'tasks' / str(task['id'])
    invocation = json.loads((directory / 'invocation.json').read_text())
    assert invocation['argv'] == ['exec', '--json', '-s', 'read-only', '--skip-git-repo-check', '-C', str(directory),
                                   '--output-schema', str(directory / 'schema.json'), '-o', str(directory / 'final.json'),
                                   (directory / 'prompt.md').read_text()]
    assert invocation['stdin_null'] and invocation['cwd'] == str(directory)
    allowed = {'PATH', 'HOME', 'USER', 'LANG', 'TMPDIR', 'CODEX_HOME', 'SHELL'}
    assert all(k in allowed or k.startswith('LC_') for k in invocation['env'])
    assert not {'SOP_BOARD_TOKEN', 'SOP_BOARD_ADMIN_TOKEN', 'JIRA_API_TOKEN'} & set(invocation['env'])
    assert {'input.json', 'prompt.md', 'schema.json', 'events.jsonl', 'stderr.txt', 'final.json', 'check.json'} <= set(task['files'])
    for name, url in task['files'].items():
        status, raw, _ = board.request('GET', url, auth=False)
        assert status == 200
        if isinstance(raw, (dict, list)):
            raw = json.dumps(raw).encode()
            assert json.loads(raw) == json.loads((directory / name).read_bytes())
        else:
            assert raw == (directory / name).read_bytes()
    assert board.sql('SELECT status,session_id FROM tasks') == [('已完成', task['session_id'])]
    assert len(board.request('GET', '/api/v1/tasks?project=other&batch=demo', auth=False)[1]['tasks']) == 0
    assert_error(board.request('GET', f"/api/v1/tasks/{task['id']}/files/%2e%2e%2fboard.sqlite3", auth=False), 404, 'task-file-not-found')


def test_three_slots_fifo_and_duplicate_input(board_factory, fake_codex):
    """SOP-32-P02：五个请求最多三项运行，创建顺序开始，相同输入复用活动任务。"""
    import time
    from codex_helpers import mode
    from helpers import push_body
    mode(fake_codex, delay=1)
    board = task_board(board_factory, fake_codex)
    tasks = []
    for i in range(5):
        body = push_body()
        body['content'] = body['content'].replace('batch: demo', f'batch: batch{i}')
        base = BASE.replace('/demo', f'/batch{i}')
        assert board.request('PUT', base + '/scenario-file', body)[0] == 200
        task = create_task(board, base)
        assert create_task(board, base)['id'] == task['id']
        tasks.append(task)
    counts = []
    until = time.monotonic() + 10
    while time.monotonic() < until:
        rows = board.request('GET', '/api/v1/tasks', auth=False)[1]['tasks']
        count = sum(t['status'] == '运行中' for t in rows)
        counts.append(count)
        assert count <= 3
        if all(t['status'] == '已完成' for t in rows):
            break
        time.sleep(.03)
    assert max(counts) == 3 and all(t['status'] == '已完成' for t in rows)
    assert [t['id'] for t in sorted(rows, key=lambda t: t['started_at'])] == [t['id'] for t in tasks]
    assert board.sql('SELECT COUNT(*) FROM tasks') == [(5,)]
    # 用假程序实际运行的时间区间再次核对，而不只看数据库状态。
    starts = sorted(json.loads((board.data / 'tasks' / str(t['id']) / 'invocation.json').read_text())['started'] for t in rows)
    assert starts[3] - starts[0] >= .9
    invocations = [(json.loads((board.data / 'tasks' / str(t['id']) / 'invocation.json').read_text())['started'], t['id']) for t in rows]
    assert [task_id for _, task_id in sorted(invocations)] == [t['id'] for t in tasks]


def process_alive(pid):
    import os
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def test_timeout_terminates_process_group(board_factory, fake_codex):
    """SOP-32-N01：时限届满报失败，整个进程组（包括子进程）终止，保留输出。"""
    import os
    import signal
    from codex_helpers import mode
    from test_writable_repos import wait_for
    mode(fake_codex, delay=4, child=True)
    board = task_board(board_factory, fake_codex, SOP_BOARD_CODEX_TIMEOUT='2')
    push(board)
    task = create_task(board)
    root = board.data / 'tasks' / str(task['id'])
    wait_for(lambda: (root / 'child.pid').exists())
    pid = json.loads((root / 'invocation.json').read_text())['pid']
    child = int((root / 'child.pid').read_text())
    try:
        task = wait_for(lambda: (t if (t := read_task(board, task))['status'] not in ('排队中', '运行中') else None))
        assert task['status'] == '失败' and '超时' in task['error']
        wait_for(lambda: not process_alive(pid) and not process_alive(child))
        assert 'thread.started' in (root / 'events.jsonl').read_text()
        assert json.loads((root / 'check.json').read_text())['ok'] is False
    finally:
        for process in (pid, child):
            try:
                os.kill(process, signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_cancel_running_queued_failed_and_retry_after_restart(board_factory, fake_codex):
    """SOP-32-N02/P03、I2：运行/排队均可取消；重启中断；重试同输入且保留旧文件。"""
    from codex_helpers import mode
    from helpers import push_body
    from test_writable_repos import wait_for
    mode(fake_codex, delay=3)
    board = task_board(board_factory, fake_codex)
    tasks = []
    for i in range(6):
        base = BASE.replace('/demo', f'/b{i}')
        body = push_body()
        body['content'] = body['content'].replace('batch: demo', f'batch: b{i}')
        assert board.request('PUT', base + '/scenario-file', body)[0] == 200
        tasks.append(create_task(board, base))
    wait_for(lambda: len([t for t in board.request('GET', '/api/v1/tasks', auth=False)[1]['tasks'] if t['status'] == '运行中']) == 3)
    directory = board.data / 'tasks' / str(tasks[0]['id'])
    wait_for(lambda: (directory / 'invocation.json').exists())
    pid = json.loads((directory / 'invocation.json').read_text())['pid']
    for task in (tasks[0], tasks[5]):
        status, body, _ = board.request('POST', f"/api/v1/tasks/{task['id']}/cancel", {}, headers=ADMIN)
        assert status == 200 and body['task']['status'] == '已放弃'
    wait_for(lambda: not process_alive(pid))
    board.stop()
    mode(fake_codex)
    board.start()
    interrupted = [read_task(board, task) for task in tasks[1:5]]
    assert all(t['status'] == '已中断' for t in interrupted)
    old_digest = interrupted[0]['input_digest']
    status, response, _ = board.request('POST', f"/api/v1/tasks/{tasks[1]['id']}/retry", {}, headers=ADMIN)
    assert status == 200
    retried = finished(board, response['task'])
    assert retried['input_digest'] == old_digest
    assert read_task(board, tasks[0])['status'] == read_task(board, tasks[5])['status'] == '已放弃'
    assert board.sql('SELECT COUNT(*) FROM tasks WHERE status IN (\'运行中\',\'排队中\')') == [(0,)]


def test_unavailable_version_missing_and_recovery(board_factory, fake_codex):
    """SOP-32-N03：启动及每次取任务均探测；失败不启动任务，恢复后由开发者重试。"""
    from codex_helpers import mode
    mode(fake_codex, version_exit=7)
    board = task_board(board_factory, fake_codex)
    push(board)
    task = finished(board, create_task(board), 'Codex 不可用')
    assert '--version' in task['error']
    assert task['started_at'] is None and 'invocation.json' not in task['files']
    mode(fake_codex)
    status, response, _ = board.request('POST', f"/api/v1/tasks/{task['id']}/retry", {}, headers=ADMIN)
    assert status == 200
    finished(board, response['task'])
    # 服务器不重启，下一次取任务也必须重新探测。
    (fake_codex / 'codex').rename(fake_codex / 'codex.disabled')
    missing = finished(board, create_task(board), 'Codex 不可用')
    assert 'codex' in missing['error'] and missing['started_at'] is None
    (fake_codex / 'codex.disabled').rename(fake_codex / 'codex')
    response = board.request('POST', f"/api/v1/tasks/{missing['id']}/retry", {}, headers=ADMIN)[1]
    finished(board, response['task'])


def test_failed_output_preserved_retry_and_invalid_cancel(board_factory, fake_codex):
    """SOP-32-P03、I2：非零/无效最终消息留证；失败不可放弃，重试仍用旧输入且保留前次文件。"""
    from codex_helpers import mode
    mode(fake_codex, exit=2, final='invalid fixture output')
    board = task_board(board_factory, fake_codex)
    push(board)
    task = finished(board, create_task(board), '失败')
    root = board.data / 'tasks' / str(task['id'])
    original = {name: (root / name).read_bytes() for name in ('events.jsonl', 'stderr.txt', 'final.json')}
    assert '退出码 2' in task['error']
    status, response, _ = board.request('POST', f"/api/v1/tasks/{task['id']}/cancel", {}, headers=ADMIN)
    assert status == 409 and response['error_code'] == 'task-state-invalid'
    assert read_task(board, task)['status'] == '失败'
    for name, raw in original.items():
        assert board.request('GET', task['files'][name], auth=False)[1] == raw
    mode(fake_codex, final='[]')
    failed = finished(board, create_task(board), '失败')
    assert 'JSON 对象' in failed['error']
    before = (board.data / 'tasks' / str(failed['id']) / 'final.json').read_bytes()
    mode(fake_codex)
    retried = board.request('POST', f"/api/v1/tasks/{failed['id']}/retry", {}, headers=ADMIN)[1]['task']
    assert finished(board, retried)['input_digest'] == failed['input_digest']
    assert board.request('GET', f"/api/v1/tasks/{failed['id']}/files/attempt-1/final.json", auth=False)[1] == before
    assert_error(board.request('POST', f"/api/v1/tasks/{failed['id']}/retry", {}, headers=ADMIN), 409, 'task-state-invalid')


def test_resume_launcher_keeps_session_and_isolated_boundary(fake_codex, tmp_path, monkeypatch):
    """I3 补充：按指定编号续接，根命令沙箱、cwd、stdin、环境与会话编号保持约定。"""
    import os
    from sop_board.tasks import resume_codex
    directory = tmp_path / 'resume-task'
    directory.mkdir()
    monkeypatch.setenv('PATH', str(fake_codex) + os.pathsep + os.defpath)
    monkeypatch.setenv('SOP_BOARD_TOKEN', 'isolated-test')
    monkeypatch.setenv('SOP_BOARD_ADMIN_TOKEN', 'isolated-admin')
    monkeypatch.setenv('JIRA_API_TOKEN', 'isolated-jira')
    process = resume_codex(directory, 'target-thread', 'continue fixture', sandbox='workspace-write')
    assert process.wait(timeout=5) == 0
    data = json.loads((directory / 'invocation.json').read_text())
    assert data['argv'] == ['-s', 'workspace-write', 'exec', 'resume', 'target-thread', '--json',
                            '--skip-git-repo-check', '-o', str(directory / 'final.json'), 'continue fixture']
    assert data['cwd'] == str(directory) and data['stdin_null']
    assert all(k in {'PATH', 'HOME', 'USER', 'LANG', 'TMPDIR', 'CODEX_HOME', 'SHELL'} or k.startswith('LC_') for k in data['env'])
    event = json.loads((directory / 'events.jsonl').read_text().splitlines()[0])
    assert event == {'type': 'thread.started', 'thread_id': 'target-thread'}


def test_runner_rejects_final_json_outside_output_schema(board_factory, fake_codex):
    """SOP-32-P03 / I3：JSON 可解析仍须满足本次任务的输出模式。"""
    from codex_helpers import mode
    from test_writable_repos import wait_for
    mode(fake_codex, final='{"ok":"yes"}')
    board = task_board(board_factory, fake_codex)
    push(board)
    task = create_task(board)
    result = wait_for(lambda: (t if (t := read_task(board, task))['status'] not in ('排队中', '运行中') else None))
    assert result['status'] == '失败' and '输出模式' in result['error']


def test_developer_operations_accept_empty_post_body(board_factory, fake_codex):
    """I2：开发者操作没有必填正文，普通空 POST 可发起、重试与放弃任务。"""
    from codex_helpers import mode
    mode(fake_codex, exit=1)
    board = task_board(board_factory, fake_codex)
    push(board)
    finished(board, create_task(board), '失败')
    status, result, _ = board.request('POST', BASE + '/merges', headers=ADMIN)
    assert status == 200, result
    task = finished(board, result['task'], '失败')
    mode(fake_codex, delay=30)
    status, result, _ = board.request('POST', f"/api/v1/tasks/{task['id']}/retry", headers=ADMIN)
    assert status == 200, result
    assert result['task']['status'] in ('排队中', '运行中')
    status, result, _ = board.request('POST', f"/api/v1/tasks/{task['id']}/cancel", headers=ADMIN)
    assert status == 200 and result['task']['status'] == '已放弃'
