import json
import time
import pytest

from fix_helpers import (fix_repo, fix_codex, fix_board, fail, agent, fixes, wait_fix,
                         configure, invocations, wait_state)
from helpers import BASE, KEY, IDS, push_body
from codex_helpers import ADMIN
from test_writable_repos import repo
from check_helpers import check_repo


def test_fix_freezes_inputs_and_commits_with_repository_author(board_factory, fix_codex, fix_repo):
    """SOP-51-P04/P03、SOP-52-P01/P02、SOP-53-P01：固定输入、命令边界及提交。"""
    from fix_helpers import git
    configure(fix_codex, dict(delay=.5))
    board, pushed = fix_board(board_factory, fix_codex, fix_repo, paused=True)
    before = git(fix_repo, 'rev-parse', 'HEAD')
    branch = git(fix_repo, 'branch', '--show-current')
    (fix_repo / 'docs/sop/notes.md').write_text('未提交的场景说明')
    agent(board, pushed, ['pass', 'fail', 'blocked', 'skip'])
    fail(board, pushed)
    board.request('POST', '/api/v1/automation/resume', {}, headers=ADMIN)
    task = wait_fix(board, '复测中')
    assert task['baseline_branch'] == branch and task['baseline_commit'] == before
    assert task['fix_branch'] == f"sop-board/fix/{task['id']}"
    assert task['worktree'] == str(board.data.resolve() / 'worktrees' / f"fix-{task['id']}")
    assert [(p['batch'], p['scenario'], p['first_attempt']) for p in task['problems']] == [('demo', IDS[0], 1), ('demo', IDS[1], 1)]
    assert task['attempt'] == 1 and task['session_id'] == 'TH-1'
    directory = board.data / 'tasks' / str(task['id'])
    problems = json.loads((directory / 'problems.json').read_text())
    assert problems[0]['latest_result']['note'] == '点击结算无响应'
    assert problems[0]['previous_agent']['status'] == 'pass'
    assert problems[0]['scene']['expected'] == ['N1 预期结果']
    assert problems[0]['evidence'][0]['path'].startswith(str(board.data.resolve()))
    assert (directory / 'snapshot.json').is_file() and (directory / 'cases.json').is_file()
    call = invocations(fix_codex)[0]
    args = call['argv']
    assert args[:5] == ['exec', '--json', '-s', 'workspace-write', '-c']
    assert 'sandbox_workspace_write.network_access=true' in args
    assert args[args.index('-C') + 1] == task['worktree'] == call['cwd']
    assert call['stdin_eof'] and call['stdin_null']
    assert not any(n.startswith(('SOP_BOARD_', 'JIRA_')) for n in call['env'])
    assert set(call['env']) <= {'PATH', 'HOME', 'USER', 'LANG', 'TMPDIR', 'CODEX_HOME', 'SHELL'} | {n for n in call['env'] if n.startswith('LC_')}
    prompt = (directory / 'prompt.md').read_text()
    assert str(directory.resolve() / 'problems.json') in prompt
    for phrase in ('product', 'execution', 'environment', 'scenario', 'requirement', 'unfixable', 'AGENTS.md', 'CLAUDE.md', 'TDD', '不加兼容层', '不提交'):
        assert phrase in prompt
    message = git(fix_repo, 'log', '-1', '--format=%an <%ae>%n%B', task['fix_branch'])
    assert message == f"Dev One <dev1@example.com>\nsop-board 修复 #{task['id']} 第 1 次尝试\n\ndemo {IDS[0]} https://github.com/demo-org/demo-app/issues/1\ndemo {IDS[1]} Agent 失败"
    assert git(fix_repo, 'rev-parse', 'HEAD') == before
    assert git(fix_repo, 'branch', '--show-current') == branch
    assert git(fix_repo, 'status', '--porcelain') == '?? docs/sop/notes.md'
    assert task['attempts'][0]['outcome'] == '待验证'
    assert task['attempts'][0]['commit_id'] == git(fix_repo, 'rev-parse', task['fix_branch'])


def test_fix_trigger_is_authorized_cross_batch_latest_failure(board_factory, fix_codex, fix_repo):
    """SOP-51-P01/N01/N02/N03：按当前指纹最新结果归集，暂停时只排队。"""
    board, pushed = fix_board(board_factory, fix_codex, fix_repo, paused=True)
    agent(board, pushed, ['pass', 'fail', 'blocked', 'skip'])
    fail(board, pushed)
    import yaml
    document = yaml.safe_load(pushed['content'])
    document['metadata']['batch'] = 'b2'
    body = push_body(document, path='docs/sop/b2.yaml')
    other = BASE.replace('/demo', '/b2')
    assert board.request('PUT', other + '/scenario-file', body)[0] == 200
    agent(board, body, ['fail', 'pass', 'pass', 'pass'], base=other, key='second')
    task = wait_fix(board, '排队中')
    assert task['kind'] == 'fix' and task['attempt'] == 0
    # 范围到真正开始时才固定。
    assert task['problems'] == []
    assert len(fixes(board)) == 1 and invocations(fix_codex) == []
    board.request('POST', '/api/v1/automation/resume', {}, headers=ADMIN)
    task = wait_state(board, task, '复测中')
    assert [(p['batch'], p['scenario']) for p in task['problems']] == [('demo', IDS[0]), ('demo', IDS[1]), ('b2', IDS[0])]
    board.stop()
    board.command = board.command[:1]
    board.start()
    assert fixes(board) == []
    assert '暂无记录' in board.request('GET', f'/p/{KEY}/fixes', auth=False)[1].decode()


@pytest.mark.parametrize('preset,reason', [
    ({'remove': ['root_cause']}, '模式'),
    ({'raw_final': '{"root_cause": 1}'}, '模式'),
    ({'omit': [IDS[1]]}, '遗漏 N1-N01'),
    ({'no_changes': True}, '修复会话没有改动文件'),
    ({'files': {'src/app.py': 'wrong', 'docs/sop/demo.yaml': 'tampered'}}, '修复改动涉及场景文件或入口声明'),
    ({'files': {'src/app.py': 'wrong', 'docs/sop/entries.yaml': 'tampered'}}, '修复改动涉及场景文件或入口声明'),
])
def test_fix_invalid_attempt_records_reason_and_restores(board_factory, fix_codex, fix_repo, preset, reason):
    """SOP-52-N01/N02、SOP-53-N01/N02/N03：拒绝不合格输出与改动，三次上限。"""
    from fix_helpers import git
    configure(fix_codex, preset)
    board, pushed = fix_board(board_factory, fix_codex, fix_repo, paused=True)
    before = git(fix_repo, 'rev-parse', 'HEAD')
    agent(board, pushed, ['fail', 'fail', 'pass', 'pass'])
    board.request('POST', '/api/v1/automation/resume', {}, headers=ADMIN)
    task = wait_fix(board, '待开发者处理')
    assert task['attempt'] == 3
    assert len(invocations(fix_codex)) == 3
    assert len(task['attempts']) == 3
    for attempt in task['attempts']:
        assert attempt['outcome'] == '未通过' and reason in attempt['reason']
        assert attempt['finished_at'] and not attempt['commit_id']
    assert git(fix_repo, 'rev-parse', task['fix_branch']) == before
    assert git(fix_repo, 'status', '--porcelain') == ''
    assert git(task['worktree'], 'status', '--porcelain') == ''
    assert git(task['worktree'], 'rev-parse', 'HEAD') == before
    assert all('resume' in c['argv'] for c in invocations(fix_codex)[1:])


def test_fix_nonproduct_requires_developer_and_approve_resumes(board_factory, fix_codex, fix_repo):
    """SOP-52-N03/P05/N04/N05：非产品问题、批准、鉴权与同会话续接。"""
    from helpers import assert_error
    categories = dict(zip(IDS, ['execution', 'environment', 'scenario', 'requirement']))
    configure(fix_codex, dict(categories=categories, no_changes=True), {})
    board, pushed = fix_board(board_factory, fix_codex, fix_repo, paused=True)
    agent(board, pushed, ['fail'] * 4)
    board.request('POST', '/api/v1/automation/resume', {}, headers=ADMIN)
    task = wait_fix(board, '待开发者处理')
    assert task['attempt'] == 1 and len(task['attention']) == 4
    assert task['attempts'][0]['outcome'] == '待开发者处理'
    assert all(p['plan'] == '请核实环境与计费规则' and p['reason'] == '独立测试依据' for p in task['attention'])
    path = f"/api/v1/tasks/{task['id']}"
    assert_error(board.request('POST', path + '/approve', {'note': 'x'}), 403, 'admin-required')
    assert_error(board.request('POST', path + '/retry', {}, auth=False), 401, 'unauthorized')
    assert_error(board.request('POST', path + '/cancel', {}), 403, 'admin-required')
    assert_error(board.request('POST', '/api/v1/tasks/999999/approve', {}, headers=ADMIN), 404, 'task-not-found')
    attention = board.request('GET', f'/api/v1/projects/{KEY}/attention', auth=False)[1]
    assert attention['items'][0]['kind'] == 'developer-action'
    assert attention['items'][0]['resume'] == 'codex resume TH-1'
    assert board.request('POST', path + '/approve', {'note': '按方案改用新的计费规则'}, headers=ADMIN)[0] == 200
    task = wait_state(board, task, '复测中')
    assert task['attempt'] == 2 and task['session_id'] == 'TH-1'
    call = invocations(fix_codex)[1]
    assert call['argv'][:2] == ['-s', 'workspace-write']
    assert call['argv'][call['argv'].index('resume') + 1] == 'TH-1'
    assert call['cwd'] == task['worktree'] and 'sandbox_workspace_write.network_access=true' in call['argv']
    assert '按方案改用新的计费规则' in call['argv'][-1]
    assert_error(board.request('POST', path + '/approve', {'note': 'x'}, headers=ADMIN), 409, 'task-state-invalid')


@pytest.mark.parametrize('category', ['execution', 'environment', 'scenario', 'requirement', 'unfixable'])
def test_fix_mixed_categories_continue_product_only(board_factory, fix_codex, fix_repo, category):
    """SOP-52-P04/N03：六种判定持久化，非产品转开发者而产品照常提交。"""
    configure(fix_codex, dict(categories={IDS[1]: category}))
    board, pushed = fix_board(board_factory, fix_codex, fix_repo, paused=True)
    agent(board, pushed, ['fail', 'fail', 'pass', 'pass'])
    board.request('POST', '/api/v1/automation/resume', {}, headers=ADMIN)
    task = wait_fix(board, '复测中')
    assert [(p['scenario'], p['category']) for p in task['attention']] == [(IDS[1], category)]
    assert task['attempts'][0]['validation']['scenarios'] == [dict(batch='demo', scenario=IDS[0])]


@pytest.mark.parametrize('first', [{'exit': 2}, {'before_thread': True}, {'no_final': True}])
def test_fix_interruption_retries_once_without_counting(board_factory, fix_codex, fix_repo, first):
    """SOP-53-N04：非零退出或缺最终输出不计尝试，按是否拿到编号选择续接。"""
    configure(fix_codex, first, {})
    board, pushed = fix_board(board_factory, fix_codex, fix_repo)
    fail(board, pushed)
    task = wait_fix(board, '复测中')
    calls = invocations(fix_codex)
    assert len(calls) == 2 and task['attempt'] == 1
    assert ('resume' in calls[1]['argv']) is not bool(first.get('before_thread'))
    assert len(task['attempts'][0]['interruptions']) == 1


def test_fix_timeout_stops_after_one_retry_and_manual_retry_preserves_thread(board_factory, fix_codex, fix_repo):
    """SOP-53-N05：两次超时转开发者，次数为零，手动重试续接原会话。"""
    configure(fix_codex, dict(delay=30))
    board, pushed = fix_board(board_factory, fix_codex, fix_repo, SOP_BOARD_CODEX_TIMEOUT='1')
    fail(board, pushed)
    task = wait_fix(board, '待开发者处理')
    assert '超时' in task['error'] and task['attempt'] == 0
    assert task['session_id'] == 'TH-1' and len(invocations(fix_codex)) == 2
    assert len(task['attempts'][0]['interruptions']) == 2
    configure(fix_codex, {})
    assert board.request('POST', f"/api/v1/tasks/{task['id']}/retry", {}, headers=ADMIN)[0] == 200
    task = wait_state(board, task, '复测中')
    assert task['attempt'] == 1 and task['session_id'] == 'TH-1'
    assert 'resume' in invocations(fix_codex)[2]['argv']


@pytest.mark.parametrize('error', [False, True])
def test_fix_reads_full_jira_or_records_failure_and_continues(board_factory, fix_codex, fix_repo, error):
    """SOP-51-P05/N11/N12：本机 Jira 完整输入、错误降级与非 Jira 不读取。"""
    from jira_helpers import mock_jira
    with mock_jira() as jira:
        fields = dict(summary='结算错误', description={'content': ['说明']}, status={'name': '待确认'},
            comment={'comments': [{'body': '评论一'}, {'body': '评论二'}]}, attachment=[{'filename': '截图.png'}])
        jira.replies['ABC-1'] = (500 if error else 200, {'fields': fields}, 0)
        board, pushed = fix_board(board_factory, fix_codex, fix_repo, paused=True, **jira.configured_env())
        fail(board, pushed, defect='ABC-1')
        fail(board, pushed, IDS[1])
        board.request('POST', '/api/v1/automation/resume', {}, headers=ADMIN)
        task = wait_fix(board, '复测中')
        assert len(jira.requests) == 1 and jira.authenticated(jira.requests[0])
        problem = task['problems'][0]['input']
        assert problem['latest_result']['note'] == '点击结算无响应'
        assert problem['defect_url'] == jira.url + '/browse/ABC-1'
        if error:
            assert 'HTTP 500' in problem['jira_error']
        else:
            assert problem['jira'] == dict(key='ABC-1', summary='结算错误', description={'content': ['说明']},
                status='待确认', comments=[{'body': '评论一'}, {'body': '评论二'}], attachments=['截图.png'])
        assert task['problems'][1]['input']['jira'] is None


def test_fix_dirty_preparation_includes_new_failures_when_committed(board_factory, fix_codex, fix_repo):
    """SOP-51-N09/N13：待提交期间的新问题纳入首次尝试。"""
    from fix_helpers import record_commit, git
    board, pushed = fix_board(board_factory, fix_codex, fix_repo)
    (fix_repo / 'src/app.py').write_text('print(99)\n')
    fail(board, pushed)
    task = wait_fix(board, '待提交')
    assert invocations(fix_codex) == []
    assert not (board.data / 'worktrees' / f"fix-{task['id']}").exists()
    fail(board, pushed, IDS[1])
    before = record_commit(fix_repo)
    task = wait_state(board, task, '复测中')
    assert task['baseline_commit'] == before
    assert [(p['scenario'], p['first_attempt']) for p in task['problems']] == [(IDS[0], 1), (IDS[1], 1)]
    assert git(fix_repo, 'status', '--porcelain') == ''


def test_fix_failed_validation_passes_feedback_new_problems_and_honors_pause(board_factory, fix_codex, fix_repo):
    """SOP-51-N04/N08、SOP-52-P03、SOP-53-P02：真实复测失败驱动下一次尝试。"""
    configure(fix_codex, dict(delay=1), {})
    board, pushed = fix_board(board_factory, fix_codex, fix_repo, validation=[
        dict(status='fail', reason='复测失败', evidence=['evidence/first.txt'], delay=2), dict(status='pass', evidence=['evidence/second.txt'])])
    fail(board, pushed)
    task = wait_fix(board, '分析中')
    fail(board, pushed, IDS[1])
    task = wait_state(board, task, '复测中')
    assert [p['scenario'] for p in task['problems']] == [IDS[0]]
    assert board.request('POST', '/api/v1/automation/pause', {}, headers=ADMIN)[0] == 200
    task = wait_state(board, task, '排队中')
    assert len(invocations(fix_codex)) == 1
    assert task['attempt'] == 1 and task['attempts'][0]['outcome'] == '未通过'
    time.sleep(.2)
    assert len(invocations(fix_codex)) == 1
    board.request('POST', '/api/v1/automation/resume', {}, headers=ADMIN)
    task = wait_state(board, task, '已合入')
    assert task['attempt'] == 2
    assert [(p['scenario'], p['first_attempt']) for p in task['problems']] == [(IDS[0], 1), (IDS[1], 2)]
    assert task['attempts'][1]['added'] == [dict(batch='demo', scenario=IDS[1])]
    directory = board.data / 'tasks' / str(task['id'])
    feedback = json.loads((directory / 'feedback.json').read_text())
    assert any(p.endswith('/evidence/observed.txt') for p in feedback['previous']['validation']['evidence'])
    assert [a['outcome'] for a in task['attempts']] == ['未通过', '通过']
    assert 'resume' in invocations(fix_codex)[1]['argv']


def test_fix_three_failed_validations_preserve_commits_and_evidence(board_factory, fix_codex, fix_repo):
    """SOP-53-N03：三次验证失败保留根因、提交与证据，不合入基线。"""
    from fix_helpers import git
    board, pushed = fix_board(board_factory, fix_codex, fix_repo, validation=[dict(status='fail', reason='未通过', evidence=['observed.txt'])])
    before = git(fix_repo, 'rev-parse', 'HEAD')
    fail(board, pushed)
    task = wait_fix(board, '待开发者处理')
    assert task['attempt'] == 3 and task['session_id'] == 'TH-1'
    assert len(invocations(fix_codex)) == 3
    assert git(fix_repo, 'rev-parse', 'HEAD') == before
    for attempt in task['attempts']:
        assert attempt['commit_id'] and attempt['result']['root_cause'] == '折扣计算共用错误逻辑'
        assert attempt['result']['changed_files'] == ['src/app.py']
        assert any(p.endswith('/evidence/observed.txt') for p in attempt['validation']['evidence'])


def test_fix_cancel_suppresses_same_result_but_new_result_is_eligible(board_factory, fix_codex, fix_repo):
    """SOP-51-N05：放弃只抑制当时的问题，新结果再次触发。"""
    from fix_helpers import git
    configure(fix_codex, dict(no_changes=True, categories={IDS[0]: 'environment'}))
    board, pushed = fix_board(board_factory, fix_codex, fix_repo)
    fail(board, pushed)
    task = wait_fix(board, '待开发者处理')
    assert board.request('POST', f"/api/v1/tasks/{task['id']}/cancel", {}, headers=ADMIN)[0] == 200
    assert git(fix_repo, 'branch', '--list', task['fix_branch']) == ''
    assert not __import__('pathlib').Path(task['worktree']).exists()
    time.sleep(5.2)
    assert len(fixes(board)) == 1
    fail(board, pushed)
    from test_writable_repos import wait_for
    tasks = wait_for(lambda: value if len(value := fixes(board)) == 2 else None)
    assert tasks[0]['status'] == '已放弃'
    assert tasks[1]['id'] != task['id']


def test_fix_restart_preserves_waiting_and_retry_rebuilds_worktree(board_factory, fix_codex, fix_repo):
    """SOP-53-N04、恢复边界：运行任务中断、会话保存、重新建立工作目录。"""
    from test_writable_repos import wait_for
    configure(fix_codex, dict(delay=30))
    board, pushed = fix_board(board_factory, fix_codex, fix_repo)
    fail(board, pushed)
    task = wait_fix(board, '分析中')
    wait_for(lambda: __import__('codex_helpers').read_task(board, task)['session_id'] == 'TH-1')
    board.stop()
    configure(fix_codex, {})
    board.start()
    task = wait_state(board, task, '已中断')
    assert task['session_id'] == 'TH-1' and task['attempt'] == 0
    assert board.request('POST', f"/api/v1/tasks/{task['id']}/retry", {}, headers=ADMIN)[0] == 200
    task = wait_state(board, task, '复测中')
    assert task['session_id'] == 'TH-1' and task['attempt'] == 1
    assert len(invocations(fix_codex)) == 2


def test_fix_waits_for_running_execution_and_queued_execution_waits_for_fix(board_factory, fix_codex, fix_repo):
    """SOP-51-N06/N07：已开始执行完成后修复优先，后续执行等待修复。"""
    from test_writable_repos import wait_for
    from execution_helpers import start_execution, settle
    configure(fix_codex, dict(delay=7), dict(delay=1), {})
    board, pushed = fix_board(board_factory, fix_codex, fix_repo)
    agent(board, pushed, ['pass', 'pass', 'pass'])
    execution = start_execution(board)
    wait_for(lambda: len(invocations(fix_codex)) == 1)
    fail(board, pushed)
    task = wait_fix(board, '排队中')
    assert '执行任务' in task['error']
    assert len(invocations(fix_codex)) == 1
    assert settle(board, execution)['status'] == '已完成'
    task = wait_state(board, task, '分析中')
    # 在分析中登记另一个失败，再发起新的执行。
    fail(board, pushed, IDS[1])
    next_execution = start_execution(board)
    task = wait_state(board, task, '复测中')
    assert __import__('codex_helpers').read_task(board, next_execution)['status'] == '排队中'
    assert len(invocations(fix_codex)) == 2
    board.request('POST', f"/api/v1/tasks/{task['id']}/cancel", {}, headers=ADMIN)
    assert settle(board, next_execution)['status'] == '已完成'


def test_fix_detached_head_requires_retry_and_never_changes_main_checkout(board_factory, fix_codex, fix_repo):
    """SOP-51-N10：分离头指针不建工作目录，切回分支后重试。"""
    from fix_helpers import git
    head = git(fix_repo, 'rev-parse', 'HEAD')
    branch = git(fix_repo, 'branch', '--show-current')
    # 仅改临时夹具的 HEAD 表示，不调用 checkout。
    head_file = fix_repo / '.git/HEAD'
    head_file.write_text(head + '\n')
    board, pushed = fix_board(board_factory, fix_codex, fix_repo)
    fail(board, pushed)
    task = wait_fix(board, '待开发者处理')
    assert task['error'] == '主工作区不在分支上' and invocations(fix_codex) == []
    assert not task['fix_branch'] and not task['worktree']
    head_file.write_text('ref: refs/heads/' + branch + '\n')
    assert board.request('POST', f"/api/v1/tasks/{task['id']}/retry", {}, headers=ADMIN)[0] == 200
    task = wait_state(board, task, '复测中')
    assert task['baseline_branch'] == branch
    assert git(fix_repo, 'rev-parse', 'HEAD') == head


def test_fix_does_not_trigger_for_blocked_skip_pass_or_stale_fingerprint(board_factory, fix_codex, fix_repo):
    """SOP-51-N01/N02：阻塞、跳过、通过覆盖与旧指纹都不触发。"""
    import yaml
    board, pushed = fix_board(board_factory, fix_codex, fix_repo, paused=True, authorize=False)
    agent(board, pushed, ['fail', 'fail', 'blocked', 'skip'])
    agent(board, pushed, ['fail', 'pass', 'blocked', 'skip'], key='newer')
    document = yaml.safe_load(pushed['content'])
    document['sops'][0]['scenarios'][0]['expected'] = ['新规则']
    body = push_body(document, path='docs/sop/demo.yaml')
    assert board.request('PUT', BASE + '/scenario-file', body)[0] == 200
    from conftest import ROOT
    from test_writable_repos import IDENTITY
    board.stop()
    board.command = [str(ROOT / '.venv/bin/sop-board'), '--writable-repo', f'{IDENTITY}={fix_repo}']
    board.start()
    time.sleep(5.2)
    assert fixes(board) == [] and invocations(fix_codex) == []


def test_fix_read_pages_show_status_problems_attempts_and_actions(board_factory, fix_codex, fix_repo):
    """SOP-51-P01/N03、SOP-52-N03：任务读取与修复页能核对转开发者结果。"""
    configure(fix_codex, dict(categories={IDS[0]: 'unfixable'}, no_changes=True))
    board, pushed = fix_board(board_factory, fix_codex, fix_repo)
    fail(board, pushed)
    task = wait_fix(board, '待开发者处理')
    status, raw, _ = board.request('GET', f'/p/{KEY}/fixes', auth=False)
    assert status == 200
    page = raw.decode()
    for word in ('待开发者处理', '折扣计算共用错误逻辑', IDS[0], 'TH-1', '独立测试依据', '请核实环境与计费规则', '批准方案', '重试', '放弃'):
        assert word in page
    home = board.request('GET', '/', auth=False)[1].decode()
    assert f'修复任务 #{task["id"]}' in home and '待开发者处理' in home
    assert fixes(board)[0] == __import__('codex_helpers').read_task(board, task)


def test_fix_distinguishes_registered_check_failure_from_executor_label(board_factory, fix_codex, check_repo):
    """SOP-51-P01、SOP-53-P01：检查来源由登记关系识别，不由执行者名称识别。"""
    from check_helpers import preset, start_check, settle_check
    from fix_helpers import git
    root, presets = check_repo
    git(root, 'config', 'user.name', 'Dev One')
    git(root, 'config', 'user.email', 'dev1@example.com')
    preset(presets, 'junit', delay=2, report='<testsuite><testcase classname="pkg.Order" name="test_create"><failure/></testcase>'
        '<testcase classname="pkg.Order" name="test_cancel"/><testcase classname="pkg.Order" name="test_refund"/></testsuite>')
    board, pushed = fix_board(board_factory, fix_codex, root)
    check = start_check(board)
    wait_state(board, check, '运行中')
    board.request('POST', '/api/v1/automation/pause', {}, headers=ADMIN)
    assert settle_check(board, check)['status'] == '已完成'
    from helpers import round_body, upload
    result = round_body(pushed, upload(board), statuses=['pass', 'pass', 'pass', 'fail'], key='executor-label')
    result['results'] = result['results'][3:]
    result['results'][0]['executor'] = '回归检查 junit'
    result['results'][0]['regression_check'] = 'junit'
    assert board.request('POST', BASE + '/rounds', result)[0] == 200
    board.request('POST', '/api/v1/automation/resume', {}, headers=ADMIN)
    task = wait_fix(board, '复测中')
    assert [(p['scenario'], p['input']['source']) for p in task['problems']] == [(IDS[0], 'check'), (IDS[3], 'agent')]
    message = git(root, 'log', '-1', '--format=%B', task['fix_branch'])
    assert f'demo {IDS[0]} 回归检查失败' in message
    assert f'demo {IDS[3]} Agent 失败' in message


def test_fix_restores_modified_tw_entry_declaration(board_factory, fix_codex, fix_repo):
    """SOP-53-N01 情形乙：入口来自 .tw/config.json 时同样禁止修改。"""
    from fix_helpers import record_commit, git
    import yaml
    entry = yaml.safe_load((fix_repo / 'docs/sop/entries.yaml').read_text())
    config = fix_repo / '.tw/config.json'
    config.parent.mkdir()
    config.write_text(json.dumps(dict(acceptance_entries=entry['entries'])))
    before = record_commit(fix_repo)
    configure(fix_codex, dict(files={'src/app.py': 'wrong', '.tw/config.json': '{}'}))
    board, pushed = fix_board(board_factory, fix_codex, fix_repo)
    fail(board, pushed)
    task = wait_fix(board, '待开发者处理')
    assert task['attempt'] == 3
    assert all(a['reason'] == '修复改动涉及场景文件或入口声明' for a in task['attempts'])
    assert git(task['worktree'], 'rev-parse', 'HEAD') == before
    assert git(task['worktree'], 'status', '--porcelain') == ''


def test_fix_new_result_reintroduces_transferred_problem_next_attempt(board_factory, fix_codex, fix_repo):
    """SOP-51-N05：转开发者的问题收到新结果后可重新纳入。"""
    configure(fix_codex, dict(categories={IDS[1]: 'environment'}), {})
    board, pushed = fix_board(board_factory, fix_codex, fix_repo, paused=True,
        validation=[dict(status='fail', reason='产品问题未通过', evidence=['first'], delay=2), dict(status='pass')])
    agent(board, pushed, ['fail', 'fail', 'pass', 'pass'])
    board.request('POST', '/api/v1/automation/resume', {}, headers=ADMIN)
    task = wait_fix(board, '复测中')
    agent(board, pushed, ['fail', 'fail', 'pass', 'pass'], key='new-result')
    task = wait_state(board, task, '已合入')
    assert task['attempt'] == 2
    assert task['attempts'][1]['validation']['scenarios'] == [dict(batch='demo', scenario=IDS[0]), dict(batch='demo', scenario=IDS[1])]
    assert task['attempts'][1]['added'] == [dict(batch='demo', scenario=IDS[1])]


def test_fix_blocked_check_never_triggers_repair(board_factory, fix_codex, check_repo):
    """SOP-51-N01：缺少检查环境时问题保留，不能触发修复。"""
    from check_helpers import declarations, save_declarations, record_commit, start_check, settle_check
    root, _ = check_repo
    value = declarations(root)
    for check in value['checks'].values():
        check['required_env'] = ['FIX_CHECK_NOT_CONFIGURED_5']
    save_declarations(root, value)
    record_commit(root)
    board, _ = fix_board(board_factory, fix_codex, root)
    assert settle_check(board, start_check(board))['status'] == '已完成'
    time.sleep(5.2)
    assert fixes(board) == [] and invocations(fix_codex) == []
    assert board.request('GET', BASE, auth=False)[1]['queue_counts']['issues'] == 3


def test_fix_changed_content_failure_replaces_problem_snapshot_next_attempt(board_factory, fix_codex, fix_repo):
    """SOP-51-N04/N05：同一场景的新内容失败纳入下一次，输出仍按批次与编号唯一。"""
    import yaml
    board, pushed = fix_board(board_factory, fix_codex, fix_repo, validation=[dict(status='fail', delay=2), dict(status='pass')])
    fail(board, pushed)
    task = wait_fix(board, '复测中')
    document = yaml.safe_load(pushed['content'])
    document['sops'][0]['scenarios'][0]['expected'] = ['已确认的新规则']
    updated = push_body(document, path='docs/sop/demo.yaml')
    (fix_repo / 'docs/sop/demo.yaml').write_text(updated['content'])
    assert board.request('PUT', BASE + '/scenario-file', updated)[0] == 200
    agent(board, updated, ['fail', 'pass', 'pass', 'pass'], key='new-content')
    task = wait_state(board, task, '已合入')
    assert task['attempt'] == 2 and len(task['problems']) == 1
    assert task['problems'][0]['fingerprint'] == updated['fingerprints'][IDS[0]]
    assert task['problems'][0]['input']['scene']['expected'] == ['已确认的新规则']
    assert task['attempts'][1]['added'] == [dict(batch='demo', scenario=IDS[0])]


def test_fix_manual_interruption_retry_keeps_original_attempt_scope(board_factory, fix_codex, fix_repo):
    """SOP-51-N04、SOP-53-N04/N05：中断续接沿用未完成尝试，不扩大范围。"""
    configure(fix_codex, dict(exit=2))
    board, pushed = fix_board(board_factory, fix_codex, fix_repo)
    fail(board, pushed)
    task = wait_fix(board, '待开发者处理')
    fail(board, pushed, IDS[1])
    configure(fix_codex, {})
    assert board.request('POST', f"/api/v1/tasks/{task['id']}/retry", {}, headers=ADMIN)[0] == 200
    task = wait_state(board, task, '复测中')
    assert task['attempt'] == 1
    assert [p['scenario'] for p in task['problems']] == [IDS[0]]
    assert task['attempts'][0]['validation']['scenarios'] == [dict(batch='demo', scenario=IDS[0])]


def test_fix_unconfigured_jira_skips_read_but_keeps_issue_key_in_commit(board_factory, fix_codex, fix_repo):
    """SOP-51-N12、SOP-53-P01：配置不完整不读取 Jira，提交仍识别单号。"""
    from jira_helpers import mock_jira
    from fix_helpers import git
    with mock_jira() as jira:
        board, pushed = fix_board(board_factory, fix_codex, fix_repo, JIRA_URL=jira.url)
        fail(board, pushed, defect='ABC-1')
        task = wait_fix(board, '复测中')
        assert jira.requests == [] and task['problems'][0]['input']['jira'] is None
        message = git(fix_repo, 'log', '-1', '--format=%B', task['fix_branch'])
        assert message.splitlines()[-1] == f'demo {IDS[0]} ABC-1'


def test_fix_merged_task_allows_new_task_and_transfers_stay_suppressed(board_factory, fix_codex, fix_repo):
    """SOP-51-P02/N05：真实合入后经调度核对新任务与抑制范围。"""
    configure(fix_codex, dict(categories={IDS[1]: 'environment'}), {}, retests=[{}, dict(delay=30)])
    board, pushed = fix_board(board_factory, fix_codex, fix_repo, paused=True)
    agent(board, pushed, ['fail', 'fail', 'pass', 'pass'])
    board.request('POST', '/api/v1/automation/resume', {}, headers=ADMIN)
    first = wait_fix(board, '已合入')
    time.sleep(5.2)
    assert len(fixes(board)) == 1 and fixes(board)[0]['status'] == '已合入'
    fail(board, pushed, IDS[2])
    second = wait_fix(board, '复测中')
    assert second['id'] != first['id']
    assert [p['scenario'] for p in second['problems']] == [IDS[2]]
    assert __import__('codex_helpers').read_task(board, first)['status'] == '已合入'


def test_fix_readonly_project_failures_never_start_or_change_repository(board_factory, fix_codex, fix_repo):
    """SOP-51-N03：未授权项目的问题保留，接口与页面为空，Git 与调用记录不变。"""
    from fix_helpers import git
    before = [git(fix_repo, *args) for args in [('status', '--porcelain'), ('branch',), ('worktree', 'list')]]
    board, pushed = fix_board(board_factory, fix_codex, fix_repo, authorize=False)
    agent(board, pushed, ['pass', 'fail', 'pass', 'pass'])
    fail(board, pushed)
    time.sleep(5.2)
    assert fixes(board) == [] and invocations(fix_codex) == []
    assert board.sql("SELECT COUNT(*) FROM tasks WHERE kind='fix'") == [(0,)]
    assert '暂无记录' in board.request('GET', f'/p/{KEY}/fixes', auth=False)[1].decode()
    assert set(board.request('GET', BASE, auth=False)[1]['queues']['issues']) == {IDS[0], IDS[1]}
    assert [git(fix_repo, *args) for args in [('status', '--porcelain'), ('branch',), ('worktree', 'list')]] == before


def test_fix_preserves_codex_queue_order_and_freezes_scope_after_waiting(board_factory, fix_codex, fix_repo):
    """SOP-51 排队与范围、SOP-BOARD-3 I3：名额满时等待，不越过更早的合并任务。"""
    from cases_helpers import cases_document, cases_body, upload_cases
    from test_writable_repos import wait_for
    configure(fix_codex, dict(delay=30, no_final=True))
    board, pushed = fix_board(board_factory, fix_codex, fix_repo)
    for index in range(4):
        document = cases_document()
        document['test_cases'][0]['expected_results'] = [f'已确认的规则 {index}']
        upload_cases(board, cases_body(document), base=BASE)
    wait_for(lambda: len(invocations(fix_codex)) == 3)
    merges = [t for t in board.request('GET', '/api/v1/tasks', auth=False)[1]['tasks'] if t['kind'] == 'merge']
    assert len(merges) == 4
    assert [t['status'] for t in merges] == ['运行中', '运行中', '运行中', '排队中']
    fail(board, pushed)
    task = wait_fix(board, '排队中')
    from codex_helpers import read_task
    task = wait_for(lambda: current if (current := read_task(board, task))['error'] == '等待 Codex 名额' else None)
    assert task['error'] == '等待 Codex 名额' and task['problems'] == []
    fail(board, pushed, IDS[1])
    configure(fix_codex, dict(delay=10, no_final=True))
    for merge in merges[:3]:
        assert board.request('POST', f"/api/v1/tasks/{merge['id']}/cancel", {}, headers=ADMIN)[0] == 200
    wait_for(lambda: len(invocations(fix_codex)) >= 4)
    assert invocations(fix_codex)[3]['cwd'] == str(board.data.resolve() / 'tasks' / str(merges[3]['id']))
    task = wait_state(board, task, '分析中')
    assert [(p['scenario'], p['first_attempt']) for p in task['problems']] == [(IDS[0], 1), (IDS[1], 1)]
    for pending in (merges[3], task):
        assert board.request('POST', f"/api/v1/tasks/{pending['id']}/cancel", {}, headers=ADMIN)[0] == 200
