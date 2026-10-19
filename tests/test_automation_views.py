"""SOP-44：通过真实 HTTP 页面与隔离 Node.js 运行发行的 board.js。"""
from codex_helpers import fake_codex, task_board, mode, read_task
from helpers import BASE, KEY, push
from test_automation import automation
from check_helpers import check_repo, record_commit
from execution_helpers import checkout, execution_board, start_execution, task_directory
from test_client import run_page_client
from test_writable_repos import repo, wait_for
from test_automation import add_batch
from html_helpers import Document
from sop_board.time_format import local_time

import yaml

from codex_helpers import create_task
from execution_helpers import settle
from helpers import push_body
from check_helpers import start_check


def test_execution_list_has_all_columns_triggers_reasons_and_check_rounds(board_factory, check_repo, fake_codex):
    """SOP-44-P07：真实建立各类任务，逐列核对触发原因、失败作废原因与轮次。"""
    repo, _ = check_repo
    checkout(repo)
    board = execution_board(board_factory, fake_codex, repo)
    assert len(settle(board, start_execution(board))['rounds']) == 3
    document = yaml.safe_load((repo / 'docs/sop/demo.yaml').read_text())
    document['sops'][1]['scenarios'][1]['expected'] = ['new scenario version']
    (repo / 'docs/sop/demo.yaml').write_text(yaml.safe_dump(document, allow_unicode=True))
    assert board.request('PUT', BASE + '/scenario-file', push_body(document))[0] == 200
    version = wait_for(lambda: next((t for t in board.request('GET', '/api/v1/tasks', auth=False)[1]['tasks']
                                    if t['trigger'] == '场景版本'), None))
    assert settle(board, version)['status'] == '已完成'
    (repo / 'src/app.py').write_text('new product before merge\n')
    record_commit(repo)
    mode(fake_codex, status='fail')
    assert settle(board, create_task(board))['status'] == '已完成'
    merged = wait_for(lambda: next((t for t in board.request('GET', '/api/v1/tasks', auth=False)[1]['tasks']
                                   if t['kind'] == 'execute' and t['trigger'] == '合并写入'), None))
    assert len(settle(board, merged)['rounds']) == 3
    mode(fake_codex, delay=1)
    voided = start_execution(board)
    wait_for(lambda: (task_directory(board, voided) / 'invocation.json').exists())
    (repo / 'src/app.py').write_text('changed during execution\n')
    assert settle(board, voided)['status'] == '已作废'
    record_commit(repo)
    mode(fake_codex, result_ids=[])
    assert settle(board, start_execution(board))['status'] == '失败'
    assert settle(board, start_check(board))['status'] == '已完成'
    tasks = [t for t in board.request('GET', '/api/v1/tasks', auth=False)[1]['tasks'] if t['kind'] != 'merge']
    page = Document(board.request('GET', f'/p/{KEY}/b/demo', auth=False)[1]).root
    section = next(s for s in page.find_all('section') if any(h.text == '执行任务' for h in s.find_all('h2')))
    rows = section.find_all('tr')
    assert [c.text for c in rows[0].find_all('th')] == ['编号', '种类', '触发原因', '范围条数', '状态', '开始时间', '结束时间', '原因', '轮次']
    assert len(rows) == len(tasks) + 1
    assert {t['trigger'] for t in tasks} == {'场景版本', '合并写入', '开发者发起'}
    for row, task in zip(rows[1:], tasks):
        cells = [c.text for c in row.children if not isinstance(c, str)]
        assert cells == [f"#{task['id']}", {'execute': '执行', 'check': '检查'}[task['kind']], task['trigger'],
                         str(len(task['scope'])), task['status'], local_time(task['started_at']), local_time(task['finished_at']),
                         task['void_reason'] or task['error'], '、'.join(f'第 {n} 轮' for n in task['rounds'])]
    assert '执行待执行场景' in page.text and '运行回归检查' in page.text


def test_running_state_precedes_pending_commit_and_pause_on_home_and_overview(board_factory, repo, fake_codex):
    """SOP-44-P06：执行中 > 待提交 > 原状态；首页和总览逐字相同，暂停附加文字。"""
    checkout(repo)
    mode(fake_codex, delay=60)
    board = execution_board(board_factory, fake_codex, repo)
    running = start_execution(board)
    wait_for(lambda: (task_directory(board, running) / 'invocation.json').exists())
    started = read_task(board, running)['started_at']
    expected = f"执行中（1 个会话，开始于 {local_time(started)}，任务 #{running['id']}）"
    (repo / 'src/app.py').write_text('uncommitted product\n')
    pending = start_execution(board)
    other_base = add_batch(board, repo, 'pending')
    other = start_execution(board, other_base)
    wait_for(lambda: read_task(board, other)['status'] == '待提交')
    assert read_task(board, pending)['status'] == '待提交'
    for paused in (False, True):
        if paused:
            automation(board, 'pause', True)
        suffix = ' · 已暂停' if paused else ''
        home = board.request('GET', '/', auth=False)[1].decode()
        for name, state in [('demo', expected), ('pending', '待提交：工作区有未提交的产品改动')]:
            page = board.request('GET', f'/p/{KEY}/b/{name}', auth=False)[1].decode()
            assert Document(page).root.one(id='agent-state').text == state + suffix
            assert state + suffix in home
    automation(board, 'resume', False)
    home = board.request('GET', '/', auth=False)[1].decode()
    assert '已暂停' not in home


def test_developer_execution_and_check_buttons_use_saved_identity(board_factory, check_repo, fake_codex):
    """SOP-44-P07/P08/N02：两种按钮要求开发者身份，保存并恢复令牌后分别建任务。"""
    repo, _ = check_repo
    checkout(repo)
    board = execution_board(board_factory, fake_codex, repo)
    automation(board, 'pause', True)
    run_page_client(board, 'automation-tasks', f'/p/{KEY}/b/demo', network=True)
    tasks = board.request('GET', '/api/v1/tasks', auth=False)[1]['tasks']
    assert [(t['kind'], t['trigger'], t['status']) for t in tasks] == [
        ('execute', '开发者发起', '排队中'), ('check', '开发者发起', '排队中')]
    assert board.sql('SELECT kind,started_at FROM tasks ORDER BY id') == [('execute', None), ('check', None)]


def test_home_resume_button_blocks_missing_identity_then_updates_page(board_factory, fake_codex):
    """SOP-44-P04/P08/N02：恢复按钮无令牌不请求，成功立即消除提示并可再次暂停。"""
    board = task_board(board_factory, fake_codex)
    push(board)
    automation(board, 'pause', True)
    run_page_client(board, 'automation-toggle', '/', network=True)
    assert board.sql("SELECT value FROM settings WHERE key='automation_paused'") == [('true',)]


def test_automation_rejection_or_disconnect_preserves_pause_banner(board_factory, fake_codex):
    """SOP-44-N02：服务拒绝或断线不伪装成恢复成功，按钮仍可重试。"""
    board = task_board(board_factory, fake_codex)
    automation(board, 'pause', True)
    run_page_client(board, 'automation-errors', '/')


def test_home_pause_banner_and_resume_control_survive_restart(board_factory, fake_codex):
    """SOP-44-P04/P05/N02：首页顶部、身份栏与恢复按钮，恢复后取消暂停提示。"""
    board = task_board(board_factory, fake_codex)
    push(board)
    automation(board, 'pause', True)
    board.stop()
    board.start()
    page = board.request('GET', '/', auth=False)[1].decode()
    assert '自动任务已暂停' in page and '恢复自动任务' in page
    assert '/api/v1/automation/resume' in page and 'review-identity' in page
    assert page.index('自动任务已暂停') < page.index('项目与批次</h1>')
    overview = board.request('GET', f'/p/{KEY}/b/demo', auth=False)[1].decode()
    assert '有待执行 4 条 · 已暂停' in overview
    automation(board, 'resume', False)
    page = board.request('GET', '/', auth=False)[1].decode()
    assert '自动任务已暂停' not in page and '/api/v1/automation/pause' in page
    assert board.request('GET', BASE, auth=False)[1]['automation_paused'] is False
