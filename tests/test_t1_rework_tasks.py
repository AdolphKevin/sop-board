"""修订 3：通过真实上传、任务及下载入口核对 SOP-31/32。"""
import hashlib
import json
import time

from cases_helpers import cases_body, upload_cases
from codex_helpers import ADMIN, fake_codex, mode, task_board, create_task, read_task, finished
from helpers import BASE, KEY, IDS, assert_error, push, push_body
from test_writable_repos import wait_for, repo


def test_remerge_without_cases_is_rejected_without_task(board_factory, fake_codex):
    """修订 3 I2：无用例不能重新合并，不创建任务或文件。"""
    board = task_board(board_factory, fake_codex)
    push(board)
    assert_error(board.request('POST', BASE + '/merges', {}, headers=ADMIN), 409, 'cases-missing')
    assert board.request('GET', '/api/v1/tasks', auth=False)[1]['tasks'] == []
    assert not (board.data / 'tasks').exists()


def test_failed_and_unavailable_cancel_are_invalid_unknown_task_is_not_found(board_factory, fake_codex):
    """SOP-31-N07：失败不可放弃，错误代码准确，不存在任务返回 404，状态与输入不变。"""
    mode(fake_codex, exit=1)
    board = task_board(board_factory, fake_codex)
    push(board)
    upload_cases(board, base=BASE)
    task = finished(board, create_task(board), '失败')
    original = read_task(board, task)
    assert_error(board.request('POST', f"/api/v1/tasks/{task['id']}/cancel", {}, headers=ADMIN), 409, 'task-state-invalid')
    assert read_task(board, task) == original
    for action in ('retry', 'cancel'):
        assert_error(board.request('POST', '/api/v1/tasks/999/' + action, {}, headers=ADMIN), 404, 'task-not-found')
    mode(fake_codex, version_exit=2)
    other = finished(board, create_task(board), 'Codex 不可用')
    original = read_task(board, other)
    assert_error(board.request('POST', f"/api/v1/tasks/{other['id']}/cancel", {}, headers=ADMIN), 409, 'task-state-invalid')
    assert read_task(board, other) == original


def test_upload_creates_task_remerge_reuses_active_but_repeats_finished_input(board_factory, fake_codex):
    """SOP-31-P04 / SOP-32-P01：上传自动建任务、活动输入去重、完成后重新合并及依据摘要。"""
    mode(fake_codex, delay=1)
    board = task_board(board_factory, fake_codex)
    pushed, _ = push(board)
    upload_cases(board, base=BASE)
    tasks = board.request('GET', f'/api/v1/tasks?project={KEY}&batch=demo', auth=False)[1]['tasks']
    assert len(tasks) == 1
    original = tasks[0]
    assert original['trigger'] == '上传用例' and original['kind'] == 'merge'
    assert original['scenario_file_sha256'] == hashlib.sha256(pushed['content'].encode()).hexdigest()
    assert create_task(board)['id'] == original['id']
    finished(board, original)
    repeated = create_task(board)
    assert repeated['id'] != original['id'] and repeated['input_digest'] == original['input_digest']
    assert repeated['trigger'] == '重新合并'
    assert len(board.request('GET', f'/api/v1/tasks?project={KEY}&batch=demo', auth=False)[1]['tasks']) == 2


def test_scenario_latest_agent_and_manual_results_expose_round(board):
    """修订 3 I3：最新 Agent 与人工结果分别附其真实轮次，不借用最新总轮次。"""
    from helpers import round_body, upload
    from test_marks import mark_body
    pushed, _ = push(board)
    evidence = upload(board)
    assert board.request('POST', BASE + '/rounds', round_body(pushed, evidence))[1]['round'] == 1
    assert board.request('POST', BASE + '/marks', mark_body(pushed))[1]['round'] == 2
    again = round_body(pushed, evidence, key='third-round')
    assert board.request('POST', BASE + '/rounds', again)[1]['round'] == 3
    scenarios = board.request('GET', BASE + '/scenarios', auth=False)[1]['scenarios']
    selected = next(s for s in scenarios if s['scenario'] == IDS[0])
    assert selected['latest_agent']['round'] == 3
    assert selected['latest_manual']['round'] == 2
    assert selected['latest_agent']['round_id'] != selected['latest_manual']['round_id']


def test_unauthorized_upload_produces_downloadable_draft_without_version_or_repo_write(board_factory, fake_codex, repo):
    """SOP-31-N03：上传走完任务到草稿下载，版本与未授权仓库逐项不变。"""
    import yaml
    from test_writable_repos import git
    before = (git(repo, 'status', '--porcelain'), git(repo, 'branch', '--show-current'), git(repo, 'worktree', 'list'))
    board = task_board(board_factory, fake_codex)
    push(board)
    version = board.request('GET', BASE, auth=False)[1]['version']
    upload_cases(board, base=BASE)
    task = finished(board, board.request('GET', '/api/v1/tasks', auth=False)[1]['tasks'][-1])
    status, raw, _ = board.request('GET', BASE + f"/merges/{task['id']}/scenario-file", auth=False)
    assert status == 200, raw
    document = yaml.safe_load(raw)
    assert document['metadata']['batch'] == 'demo'
    assert {'TC_DEMO_LOGIN_001', 'TC_DEMO_LOGIN_002', 'TC_DEMO_ORDER_001', *IDS} == {s['id'] for n in document['sops'] for s in n['scenarios']}
    assert board.request('GET', BASE, auth=False)[1]['version'] == version
    assert '只读' in board.request('GET', f'/p/{KEY}/b/demo', auth=False)[1].decode()
    assert before == (git(repo, 'status', '--porcelain'), git(repo, 'branch', '--show-current'), git(repo, 'worktree', 'list'))


def test_nonzero_and_missing_final_have_exact_errors_keep_output_and_no_result(board_factory, fake_codex, repo):
    """SOP-32-N05：两次失败原因原文、原始输出、无合并结果、仓库及场景版本不变。"""
    from test_writable_repos import git
    (repo / 'docs/sop/demo.yaml').unlink()
    before = git(repo, 'status', '--porcelain')
    mode(fake_codex, exit=1, no_final=True, stderr='boom')
    board = task_board(board_factory, fake_codex, repo=repo)
    upload_cases(board, base=BASE)
    task = finished(board, board.request('GET', '/api/v1/tasks', auth=False)[1]['tasks'][-1], '失败')
    assert task['error'] == 'Codex 退出码 1' and task['scenario_file_sha256'] == ''
    assert b'boom' in board.request('GET', task['files']['stderr.txt'], auth=False)[1]
    assert b'thread.started' in board.request('GET', task['files']['events.jsonl'], auth=False)[1]
    assert_error(board.request('GET', BASE + f"/merges/{task['id']}/scenario-file", auth=False), 404, 'merge-result-not-found')
    mode(fake_codex, no_final=True)
    assert board.request('POST', f"/api/v1/tasks/{task['id']}/retry", {}, headers=ADMIN)[0] == 200
    retried = finished(board, task, '失败')
    assert retried['error'] == 'Codex 没有写出最终输出'
    assert board.request('GET', BASE, auth=False)[1]['version'] is None
    assert not (repo / 'docs/sop/demo.yaml').exists() and git(repo, 'status', '--porcelain') == before


def upload_task(board, name='demo'):
    base = BASE.rsplit('/', 1)[0] + '/' + name
    if name != 'demo':
        body = push_body()
        body['content'] = body['content'].replace('batch: demo', 'batch: ' + name)
        assert board.request('PUT', base + '/scenario-file', body)[0] == 200
    upload_cases(board, base=base)
    return board.request('GET', f'/api/v1/tasks?project={KEY}&batch={name}', auth=False)[1]['tasks'][-1]


def settled(board, task):
    return wait_for(lambda: (t if (t := read_task(board, task))['status'] not in ('排队中', '运行中') else None))


def test_waiting_tasks_release_slots_and_all_cancellable_states_stay_cancelled(board_factory, fake_codex, repo):
    """SOP-31-P06、SOP-32-P04：真实流程产生等待状态，等待不占名额，五种放弃均留在已放弃。"""
    from test_codex_tasks import process_alive
    mode(fake_codex, gaps=['需要补充正向依据'])
    board = task_board(board_factory, fake_codex, repo=repo)
    gap1 = settled(board, upload_task(board, 'g1'))
    gap2 = settled(board, upload_task(board, 'g2'))
    assert gap1['status'] == gap2['status'] == '待补充'
    mode(fake_codex, delay=1)
    sync = upload_task(board)
    finished(board, sync, '运行中')
    path = repo / 'docs/sop/demo.yaml'
    path.write_text('metadata: [\n')
    sync = settled(board, sync)
    assert sync['status'] == '待同步'
    workspace_sha = hashlib.sha256(path.read_bytes()).hexdigest()
    mode(fake_codex, delay=30, child=True)
    interrupted = upload_task(board, 'c3')
    finished(board, interrupted, '运行中')
    board.stop()
    board.start()
    assert read_task(board, interrupted)['status'] == '已中断'
    running = [upload_task(board, name) for name in ('c1', 'c6', 'c7')]
    for task in running:
        finished(board, task, '运行中')
    queued = upload_task(board, 'c2')
    assert read_task(board, queued)['status'] == '排队中'
    assert [read_task(board, t)['status'] for t in (gap1, gap2, sync)] == ['待补充', '待补充', '待同步']
    root = board.data / 'tasks' / str(running[0]['id'])
    wait_for(lambda: (root / 'child.pid').exists())
    pid = json.loads((root / 'invocation.json').read_text())['pid']
    child = int((root / 'child.pid').read_text())
    cancelled = (queued, running[0], interrupted, sync, gap1)
    for task in cancelled:
        status, response, _ = board.request('POST', f"/api/v1/tasks/{task['id']}/cancel", {}, headers=ADMIN)
        assert status == 200 and response['task']['status'] == '已放弃'
    wait_for(lambda: not process_alive(pid) and not process_alive(child))
    assert not (board.data / 'tasks' / str(queued['id']) / 'events.jsonl').exists()
    time.sleep(10.1)
    assert all(read_task(board, t)['status'] == '已放弃' for t in cancelled)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == workspace_sha
    assert_error(board.request('POST', f"/api/v1/tasks/{gap2['id']}/retry", {}, headers=ADMIN), 409, 'task-state-invalid')
    for action in ('retry', 'cancel'):
        assert_error(board.request('POST', f"/api/v1/tasks/{gap1['id']}/{action}", {}, headers=ADMIN), 409, 'task-state-invalid')


def test_public_task_reads_filter_fields_nested_files_and_reject_escape(board_factory, fake_codex):
    """SOP-32-P05/N06：公开列表、全部字段与逐字节下载；子目录、归档、越界及不存在的任务。"""
    import http.client
    mode(fake_codex, files={'sop/x.md': '# 任务内原文\n'})
    board = task_board(board_factory, fake_codex)
    push(board)
    first = settled(board, upload_task(board))
    second = finished(board, create_task(board))
    other = settled(board, upload_task(board, 'other'))
    rows = board.request('GET', f'/api/v1/tasks?project={KEY}&batch=demo', auth=False)[1]['tasks']
    assert {t['id'] for t in rows} == {first['id'], second['id']} and other['id'] not in {t['id'] for t in rows}
    required = {'id', 'kind', 'project', 'batch', 'status', 'trigger', 'input_digest', 'scenario_file_sha256',
                'created_at', 'started_at', 'finished_at', 'session_id', 'error'}
    assert all(required <= t.keys() and t['kind'] == 'merge' for t in rows)
    root = board.data / 'tasks' / str(first['id'])
    first = read_task(board, first)
    assert 'sop/x.md' in first['files']
    for name, url in first['files'].items():
        status, raw, _ = board.request('GET', url, auth=False)
        assert status == 200 and raw == (root / name).read_bytes()
    (root / 'leak').symlink_to(board.data / 'board.sqlite3')
    for path in ('..%2F..%2Fboard.sqlite3', '%2Fetc%2Fpasswd', 'not-exist.txt', 'leak', '%00'):
        response = board.request('GET', f"/api/v1/tasks/{first['id']}/files/{path}", auth=False)
        assert_error(response, 404, 'task-file-not-found')
        assert b'SQLite format 3' not in json.dumps(response[1]).encode()
    connection = http.client.HTTPConnection('127.0.0.1', board.port, timeout=5)
    try:
        connection.request('GET', f"/api/v1/tasks/{first['id']}/files/../../board.sqlite3")
        response = connection.getresponse()
        assert response.status == 404 and b'SQLite format 3' not in response.read()
    finally:
        connection.close()
    before = read_task(board, second)
    for action in ('retry', 'cancel'):
        assert_error(board.request('POST', f"/api/v1/tasks/{second['id']}/{action}", {}, headers=ADMIN), 409, 'task-state-invalid')
    assert read_task(board, second) == before
    for suffix in ('', '/files/final.json'):
        assert_error(board.request('GET', '/api/v1/tasks/999' + suffix, auth=False), 404, 'task-not-found')


def test_retry_three_allowed_states_keeps_id_input_and_finishes(board_factory, fake_codex):
    """SOP-31-P05：失败、重启中断与 Codex 不可用均按相同编号和输入重试。"""
    mode(fake_codex, exit=1)
    board = task_board(board_factory, fake_codex)
    push(board)
    failed = settled(board, upload_task(board))
    assert failed['status'] == '失败'
    mode(fake_codex, delay=30)
    interrupted = upload_task(board, 'r2')
    finished(board, interrupted, '运行中')
    board.stop()
    board.start()
    interrupted = read_task(board, interrupted)
    assert interrupted['status'] == '已中断'
    mode(fake_codex, version_exit=2)
    unavailable = settled(board, upload_task(board, 'r3'))
    assert unavailable['status'] == 'Codex 不可用'
    # A later upload changes the current input; retry must still use the original snapshot.
    newer = cases_body()
    newer['content'] = newer['content'].replace('执行操作', '新的输入步骤')
    upload_cases(board, newer, base=BASE)
    wait_for(lambda: board.request('GET', '/api/v1/tasks', auth=False)[1]['tasks'][-1]['status'] == 'Codex 不可用')
    original_ids = {t['id'] for t in board.request('GET', '/api/v1/tasks', auth=False)[1]['tasks']}
    mode(fake_codex, delay=1)
    tasks = [failed, interrupted, unavailable]
    for task in tasks:
        status, response, _ = board.request('POST', f"/api/v1/tasks/{task['id']}/retry", {}, headers=ADMIN)
        assert status == 200
        assert response['task']['status'] in ('排队中', '运行中')
        assert (response['task']['id'], response['task']['input_digest']) == (task['id'], task['input_digest'])
    for task in tasks:
        assert finished(board, task)['input_digest'] == task['input_digest']
    assert {t['id'] for t in board.request('GET', '/api/v1/tasks', auth=False)[1]['tasks']} == original_ids
    original_input = json.loads((board.data / 'tasks' / str(failed['id']) / 'input.json').read_text())
    assert original_input['cases'][0]['steps'] == ['执行操作']


def test_permissions_on_real_failed_and_queued_tasks_have_no_side_effects(board_factory, fake_codex):
    """SOP-31-N02：对真实失败与排队任务核对三种错误身份和所有操作，无状态或数量变化。"""
    mode(fake_codex, exit=1)
    board = task_board(board_factory, fake_codex)
    push(board)
    failed = settled(board, upload_task(board))
    mode(fake_codex, delay=30)
    active = [upload_task(board, name) for name in ('busy1', 'busy2', 'busy3')]
    for task in active:
        finished(board, task, '运行中')
    queued = create_task(board)
    assert read_task(board, queued)['status'] == '排队中'
    before = [(t['id'], t['status'], t['input_digest']) for t in board.request('GET', '/api/v1/tasks', auth=False)[1]['tasks']]
    for headers, status, code in [({}, 401, 'unauthorized'), ({'Authorization': 'Bearer isolated-test'}, 403, 'admin-required'),
                                   ({'Authorization': 'Bearer invalid-isolated-value'}, 401, 'unauthorized')]:
        for path in (BASE + '/merges', f"/api/v1/tasks/{failed['id']}/retry", f"/api/v1/tasks/{queued['id']}/cancel"):
            assert_error(board.request('POST', path, {}, auth=False, headers=headers), status, code)
    after = [(t['id'], t['status'], t['input_digest']) for t in board.request('GET', '/api/v1/tasks', auth=False)[1]['tasks']]
    assert after == before


def test_restart_interrupts_all_five_active_tasks_and_preserves_completed(board_factory, fake_codex):
    """SOP-32-N02：完成项不变，三个运行及两个排队项重启中断，可显式重试完成。"""
    board = task_board(board_factory, fake_codex)
    push(board)
    completed = settled(board, upload_task(board))
    assert completed['status'] == '已完成'
    mode(fake_codex, delay=30)
    tasks = [upload_task(board, f'restart{i}') for i in range(5)]
    wait_for(lambda: [read_task(board, t)['status'] for t in tasks] == ['运行中'] * 3 + ['排队中'] * 2)
    board.stop()
    board.start()
    assert all(read_task(board, t)['status'] == '已中断' for t in tasks)
    assert read_task(board, completed) == completed
    mode(fake_codex)
    assert board.request('POST', f"/api/v1/tasks/{tasks[0]['id']}/retry", {}, headers=ADMIN)[0] == 200
    assert finished(board, tasks[0])['id'] == tasks[0]['id']


def test_missing_codex_never_auto_retries_and_runtime_version_failure_never_executes(board_factory, fake_codex):
    """SOP-32-N03/N04：没有命令和版本探测失败留在不可用；恢复后须重试，版本失败不 exec。"""
    executable = fake_codex / 'codex'
    executable.rename(fake_codex / 'disabled')
    board = task_board(board_factory, fake_codex)
    push(board)
    task = settled(board, upload_task(board))
    assert task['status'] == 'Codex 不可用' and task['error']
    page = board.request('GET', f'/p/{KEY}/b/demo/merges', auth=False)[1].decode()
    assert task['error'] in page
    (fake_codex / 'disabled').rename(executable)
    time.sleep(10.1)
    assert read_task(board, task)['status'] == 'Codex 不可用'
    assert board.request('POST', f"/api/v1/tasks/{task['id']}/retry", {}, headers=ADMIN)[0] == 200
    finished(board, task)
    calls_before = len((fake_codex / 'calls.jsonl').read_text().splitlines())
    mode(fake_codex, version_exit=2)
    failed_probe = settled(board, upload_task(board, 'version-fail'))
    assert failed_probe['status'] == 'Codex 不可用' and 'codex --version 失败' in failed_probe['error']
    page = board.request('GET', f'/p/{KEY}/b/version-fail/merges', auth=False)[1].decode()
    assert failed_probe['error'] in page
    calls = [json.loads(line)['argv'] for line in (fake_codex / 'calls.jsonl').read_text().splitlines()[calls_before:]]
    assert calls and all(argv == ['--version'] for argv in calls)


def test_scene_change_trigger_uses_new_sha_and_skips_seen_or_board_inputs(board_factory, fake_codex):
    """修订 3 I3、SOP-31-P07：场景变化记录原因与新摘要，相同/看板输入不重复触发。"""
    board = task_board(board_factory, fake_codex)
    original, _ = push(board)
    settled(board, upload_task(board))
    mode(fake_codex, delay=1)
    changed = dict(original, content=original['content'] + '# 第二版本\n')
    assert board.request('PUT', BASE + '/scenario-file', changed)[0] == 200
    tasks = board.request('GET', '/api/v1/tasks', auth=False)[1]['tasks']
    assert len(tasks) == 2
    latest = tasks[-1]
    assert latest['trigger'] == '场景文件变化'
    assert latest['scenario_file_sha256'] == hashlib.sha256(changed['content'].encode()).hexdigest()
    finished(board, latest)
    assert board.request('PUT', BASE + '/scenario-file', original)[0] == 200
    assert len(board.request('GET', '/api/v1/tasks', auth=False)[1]['tasks']) == 2
    own = dict(original, content=original['content'] + '# 看板版本\n', pushed_by='看板合并 #1')
    assert board.request('PUT', BASE + '/scenario-file', own)[0] == 200
    assert len(board.request('GET', '/api/v1/tasks', auth=False)[1]['tasks']) == 2


def test_runner_exact_arguments_empty_stdin_environment_values_and_thread(board_factory, fake_codex, tmp_path):
    """SOP-32-P01：精确参数、EOF、白名单值保持与所有敏感变量排除，th-001 会话。"""
    import yaml
    from cases_helpers import cases_document
    codex_home = tmp_path / 'empty-codex-home'
    codex_home.mkdir()
    mode(fake_codex, thread_id='th-001')
    board = task_board(board_factory, fake_codex, LC_ALL='zh_CN.UTF-8', CODEX_HOME=str(codex_home),
                       MY_SECRET='isolated-x', JIRA_URL='https://isolated.invalid', JIRA_EMAIL='fixture@example.invalid', JIRA_OTHER='isolated-other')
    push(board)
    task = settled(board, upload_task(board))
    assert task['status'] == '已完成' and task['session_id'] == 'th-001' and task['trigger'] == '上传用例'
    root = board.data / 'tasks' / str(task['id'])
    invocation = json.loads((root / 'invocation.json').read_text())
    assert invocation['argv'] == ['exec', '--json', '-s', 'read-only', '--skip-git-repo-check', '-C', str(root),
                                  '--output-schema', str(root / 'schema.json'), '-o', str(root / 'final.json'), (root / 'prompt.md').read_text()]
    assert invocation['stdin_null'] and invocation['stdin_eof'] and invocation['cwd'] == str(root)
    assert invocation['values'] == {'LC_ALL': 'zh_CN.UTF-8', 'CODEX_HOME': str(codex_home)}
    assert all(k in {'PATH', 'HOME', 'USER', 'LANG', 'TMPDIR', 'CODEX_HOME', 'SHELL'} or k.startswith('LC_') for k in invocation['env'])
    assert not any(k.startswith('JIRA_') or k in ('SOP_BOARD_TOKEN', 'SOP_BOARD_ADMIN_TOKEN', 'MY_SECRET') for k in invocation['env'])
    assert {'input.json', 'cases.yaml', 'scenarios.yaml', 'entries.yaml', 'prompt.md', 'schema.json', 'events.jsonl', 'stderr.txt', 'final.json', 'check.json'} <= task['files'].keys()
    assert yaml.safe_load((root / 'cases.yaml').read_text()) == cases_document()


def test_five_uploads_limit_processes_to_three_and_start_in_creation_order(board_factory, fake_codex):
    """SOP-32-P02：三个进程与两个排队，实际 PID/时间区间及开始顺序同时核对。"""
    from test_codex_tasks import process_alive
    mode(fake_codex, delay=2)
    board = task_board(board_factory, fake_codex)
    push(board)
    tasks = [upload_task(board, f'fifo{i}') for i in range(5)]
    wait_for(lambda: [read_task(board, t)['status'] for t in tasks] == ['运行中'] * 3 + ['排队中'] * 2)
    roots = [board.data / 'tasks' / str(t['id']) for t in tasks]
    wait_for(lambda: all((root / 'invocation.json').exists() for root in roots[:3]))
    assert all(process_alive(json.loads((root / 'invocation.json').read_text())['pid']) for root in roots[:3])
    assert all(not (root / 'events.jsonl').exists() for root in roots[3:])
    until = time.monotonic() + 10
    while time.monotonic() < until:
        rows = [read_task(board, t) for t in tasks]
        assert sum(t['status'] == '运行中' for t in rows) <= 3
        records = [json.loads((root / 'invocation.json').read_text()) for root in roots if (root / 'invocation.json').exists()]
        assert sum(process_alive(r['pid']) for r in records) <= 3
        if all(t['status'] == '已完成' for t in rows):
            break
        time.sleep(.03)
    assert all(t['status'] == '已完成' for t in rows)
    records = [json.loads((root / 'invocation.json').read_text()) for root in roots]
    assert [r['started'] for r in records] == sorted(r['started'] for r in records)
    overlap, peak = 0, 0
    for _, delta in sorted([(r['started'], 1) for r in records] + [(r['finished'], -1) for r in records]):
        overlap += delta
        peak = max(peak, overlap)
    assert peak == 3 and overlap == 0


def test_timeout_kills_three_groups_and_gives_slot_to_fourth_task(board_factory, fake_codex):
    """SOP-32-N01：三个任务超时后父子进程消失，第四个排队任务取得空闲名额。"""
    from test_codex_tasks import process_alive
    mode(fake_codex, delay=30, child=True)
    board = task_board(board_factory, fake_codex, SOP_BOARD_CODEX_TIMEOUT='2')
    push(board)
    tasks = [upload_task(board, f'timeout{i}') for i in range(4)]
    assert read_task(board, tasks[3])['status'] == '排队中'
    roots = [board.data / 'tasks' / str(t['id']) for t in tasks[:3]]
    wait_for(lambda: all((root / 'child.pid').exists() for root in roots))
    pids = [(json.loads((root / 'invocation.json').read_text())['pid'], int((root / 'child.pid').read_text())) for root in roots]
    wait_for(lambda: all(read_task(board, t)['status'] == '失败' for t in tasks[:3]) and read_task(board, tasks[3])['status'] == '运行中')
    assert all('超时' in read_task(board, t)['error'] for t in tasks[:3])
    wait_for(lambda: all(not process_alive(pid) and not process_alive(child) for pid, child in pids))


def test_current_task_database_restart_preserves_digest_trigger_and_files(board_factory, fake_codex):
    """T2 清理：当前任务重启后依据摘要、原因和文件保持。"""
    board = task_board(board_factory, fake_codex)
    pushed, _ = push(board)
    task = settled(board, upload_task(board))
    before = read_task(board, task)
    files = {name: (board.data / 'tasks' / str(task['id']) / name).read_bytes() for name in before['files']}
    board.stop()
    board.start()
    after = read_task(board, task)
    assert after == before
    assert after['scenario_file_sha256'] == hashlib.sha256(pushed['content'].encode()).hexdigest()
    assert after['trigger'] == '上传用例'
    assert files == {name: (board.data / 'tasks' / str(task['id']) / name).read_bytes() for name in after['files']}


def test_unhashable_judgment_id_is_failed_not_completed_or_thread_crash(board_factory, fake_codex):
    """SOP-32-P03：不合格判断字段必须失败并留核对结果，不能因异常被记为完成。"""
    invalid = dict(cases=[dict(id=[], actor='测试岗', verify='自定义入口')], nodes=[], scenarios=[],
                   negative_exempt=[], gaps=[], notes=[], relocate={})
    mode(fake_codex, final=json.dumps(invalid))
    board = task_board(board_factory, fake_codex)
    push(board)
    task = settled(board, upload_task(board))
    assert task['status'] == '失败' and task['error']
    check = json.loads(board.request('GET', task['files']['check.json'], auth=False)[1])
    assert check['ok'] is False and check['problems']
    assert 'Traceback' not in board.log.read_text()
    assert_error(board.request('GET', BASE + f"/merges/{task['id']}/scenario-file", auth=False), 404, 'merge-result-not-found')


def test_existing_case_numbered_scenarios_complete_without_duplicate_and_keep_regression(board_factory, fake_codex):
    """SOP-31-P04、SOP-32-P02 前置：已有同编号用例场景时仍能完成，且不重复编号。"""
    import yaml
    from cases_helpers import cases_document
    from helpers import demo
    document = demo()
    original = document['sops'][0]['scenarios'][0]
    node = document['sops'][0]
    from copy import deepcopy
    node['scenarios'] = [dict(deepcopy(original), id=c['id'], title=c['title'], tags=c['tags'], initial_state=c['preconditions'],
                              steps=c['steps'], expected=c['expected_results']) for c in cases_document()['test_cases']]
    node['negative_exempt'] = '原场景只含正向用例'
    regression = dict(check='unit', cases=['test_existing_guard'])
    node['scenarios'][0]['regression'] = regression
    document['sops'] = [node]
    board = task_board(board_factory, fake_codex)
    push(board, push_body(document))
    task = settled(board, upload_task(board))
    assert task['status'] == '已完成', task['error']
    status, raw, _ = board.request('GET', BASE + f"/merges/{task['id']}/scenario-file", auth=False)
    assert status == 200
    scenes = [s for n in yaml.safe_load(raw)['sops'] for s in n['scenarios']]
    assert len(scenes) == 3 and len({s['id'] for s in scenes}) == 3
    assert next(s for s in scenes if s['id'] == 'TC_DEMO_LOGIN_001')['regression'] == regression
