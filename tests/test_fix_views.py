"""SOP-58：修复的公开记录、页面标记与报告。"""
import json
import re
import time
import yaml

from codex_helpers import ADMIN, read_task
from fix_helpers import (fix_repo, fix_codex, fix_board, fail, agent, wait_fix,
                         configure, wait_state, fixes, all_invocations)
from helpers import BASE, KEY, IDS, push_body
from test_writable_repos import repo, wait_for
from html_helpers import page


def test_fix_scenario_badges_history_and_polling_follow_task_lifecycle(board_factory, fix_codex, fix_repo):
    """SOP-58-P03/P04/P05：活动问题标记与历史分开，完成后清除标记。"""
    configure(fix_codex, dict(categories={IDS[1]: 'environment'}), {},
              retests=[dict(statuses={IDS[0]: 'fail'}), dict(delay=1)])
    board, pushed = fix_board(board_factory, fix_codex, fix_repo, paused=True)
    agent(board, pushed, ['fail', 'fail', 'blocked', 'pass'])
    before = board.request('GET', BASE + '/state', auth=False)[1]['etag']
    board.request('POST', '/api/v1/automation/resume', {}, headers=ADMIN)
    task = wait_fix(board, '复测中')
    assert before != board.request('GET', BASE + '/state', auth=False)[1]['etag']
    cards = page(board, f'/p/{KEY}/b/demo/scenarios').find_all('article', **{'class': 'scenario-card'})
    assert f"修复中 #{task['id']}" in cards[0].text
    assert f"待开发者处理 #{task['id']}" in cards[1].text
    assert '修复中 #' not in cards[2].text and '待开发者处理 #' not in cards[2].text
    assert cards[0].find_all('a', href=f'/p/{KEY}/fixes?task={task["id"]}')
    detail = board.request('GET', f'/p/{KEY}/b/demo/s/{IDS[0]}', auth=False)[1].decode()
    assert f"修复中 #{task['id']}" in detail
    home = board.request('GET', '/', auth=False)[1].decode()
    assert f"修复任务 #{task['id']}" in home
    task = wait_state(board, task, '已合入')
    cards = board.request('GET', f'/p/{KEY}/b/demo/scenarios', auth=False)[1].decode()
    assert '修复中 #' not in cards and '待开发者处理 #' not in cards
    detail = board.request('GET', f'/p/{KEY}/b/demo/s/{IDS[0]}', auth=False)[1].decode()
    assert f"所属修复任务 #{task['id']}" in detail
    assert fixes(board)[0] == read_task(board, task)


def test_fix_report_records_and_overview_have_exact_independent_totals(board_factory, fix_codex, fix_repo):
    """SOP-58-P07：三个任务 1、2、3 次，独立预期总计 6 次。"""
    configure(fix_codex, {}, retests=[{}])
    board, pushed = fix_board(board_factory, fix_codex, fix_repo)
    tasks = []
    for index, retests in enumerate(([{}], [dict(statuses={IDS[1]: 'fail'}), {}], [dict(statuses={IDS[2]: 'fail'})])):
        previous = sum('/validations/' in c['argv'][c['argv'].index('-o') + 1] for c in all_invocations(fix_codex))
        configure(fix_codex, {}, retests=[{}] * previous + retests)
        fail(board, pushed, IDS[index])
        task = wait_for(lambda: next((t for t in fixes(board) if t['id'] not in {p['id'] for p in tasks}), None))
        tasks.append(wait_state(board, task, '待开发者处理' if index == 2 else '已合入'))
    report = board.request('GET', BASE + '/report.md', auth=False)[1].decode()
    block = report.split('## 修复记录\n', 1)[1].split('## 回归覆盖', 1)[0]
    for task, sid in zip(tasks, IDS):
        assert f"修复任务 #{task['id']}" in block and sid in block
        assert '折扣计算共用错误逻辑' in block
        assert f"尝试次数：{task['attempt']}" in block and task['status'] in block
        if task['merged_commit']:
            assert task['merged_commit'] in block
    expected = dict(tasks=3, problems=3, merged=2, developer=1, first_pass=1, attempts=6)
    assert board.request('GET', BASE, auth=False)[1]['fix_statistics'] == expected
    overview = board.request('GET', f'/p/{KEY}/b/demo', auth=False)[1].decode()
    for label, count in [('任务数', 3), ('问题数', 3), ('已合入数', 2), ('转开发者数', 1), ('首次尝试即通过的任务数', 1), ('总尝试次数', 6)]:
        assert f'{label}：{count}' in block and f'{label}：{count}' in overview


def test_fix_cross_batch_reports_filter_problems_but_keep_whole_task_attempts(board_factory, fix_codex, fix_repo):
    """SOP-58-P08/N07：各批次只计自己的问题，无关批次仍写暂无记录。"""
    # 初始批次在授权前登记，避免新增批次的普通执行任务先消除本用例预置的问题。
    initial_batches = {}
    for batch in ('b2', 'empty'):
        document = yaml.safe_load((fix_repo / 'docs/sop/demo.yaml').read_text())
        document['metadata']['batch'] = batch
        for node in document['sops']:
            for s in node['scenarios']:
                s['id'] = s['id'].replace('N1', 'G1').replace('N2', 'G2')
        body = push_body(document, path=f'docs/sop/{batch}.yaml')
        (fix_repo / body['path']).write_text(body['content'])
        initial_batches[batch] = body
    configure(fix_codex, {}, retests=[dict(statuses={IDS[0]: 'fail'}), {}])
    board, pushed = fix_board(board_factory, fix_codex, fix_repo, paused=True)
    for batch, body in initial_batches.items():
        other = BASE.replace('/demo', '/' + batch)
        if batch == 'b2':
            fail(board, body, 'G1-P01', base=other)
    fail(board, pushed)
    initial_tasks = board.request('GET', '/api/v1/tasks', auth=False)[1]['tasks']
    assert not any(t['kind'] == 'execute' for t in initial_tasks), [
        (t['id'], t['kind'], t['batch'], t['status']) for t in initial_tasks]
    board.request('POST', '/api/v1/automation/resume', {}, headers=ADMIN)
    task = wait_fix(board, '已合入')
    assert {(p['batch'], p['scenario']) for p in task['problems']} == {('demo', 'N1-P01'), ('b2', 'G1-P01')}
    for batch, yes, no in [('demo', 'N1-P01', 'G1-P01'), ('b2', 'G1-P01', 'N1-P01')]:
        base = BASE.replace('/demo', '/' + batch)
        block = board.request('GET', base + '/report.md', auth=False)[1].decode().split('## 修复记录\n', 1)[1].split('## 回归覆盖', 1)[0]
        assert f"修复任务 #{task['id']}" in block and yes in block and no not in block
        assert '问题数：1' in block and '尝试次数：2' in block and '总尝试次数：2' in block
    block = board.request('GET', BASE.replace('/demo', '/empty') + '/report.md', auth=False)[1].decode().split('## 修复记录\n', 1)[1].split('## 回归覆盖', 1)[0]
    assert '暂无记录' in block and f"#{task['id']}" not in block


def test_fix_detail_has_records_evidence_and_all_three_action_buttons(board_factory, fix_codex, fix_repo):
    """SOP-58-P02/P05：列表与详情一致，结束的任务仍展示禁用的操作。"""
    configure(fix_codex, dict(categories={IDS[1]: 'environment'}), {}, retests=[dict(statuses={IDS[0]: 'fail'}), {}])
    board, pushed = fix_board(board_factory, fix_codex, fix_repo, paused=True)
    agent(board, pushed, ['fail', 'fail', 'pass', 'pass'])
    board.request('POST', '/api/v1/automation/resume', {}, headers=ADMIN)
    first = wait_fix(board, '已合入')
    items = board.request('GET', f'/api/v1/projects/{KEY}/attention', auth=False)[1]['items']
    assert len(items) == 1 and items[0]['kind'] == 'developer-action'
    assert '独立测试依据' in items[0]['reason'] and '请核实环境与计费规则' in items[0]['reason']
    configure(fix_codex, dict(no_changes=True, categories={sid: 'environment' for sid in IDS}))
    fail(board, pushed, IDS[2])
    second = wait_fix(board, '待开发者处理')
    document = page(board, f'/p/{KEY}/fixes?task={first["id"]}')
    for text in (f"#{first['id']}", f"#{second['id']}", '折扣计算共用错误逻辑', '问题与处理', '待开发者处理事项', 'Jira 回写结果', first['merged_commit']):
        assert text in document.text
    for label in ('批准方案', '重试', '放弃'):
        button = next((b for b in document.find_all('button') if b.text == label), None)
        assert button is not None and 'disabled' in button.attrs
    evidence = first['attempts'][0]['validation']['evidence']
    name = next(n for n in first['files'] if any(p.endswith('/' + n) for p in evidence) and n.endswith('observed.txt'))
    url = first['files'][name]
    assert document.find_all('a', href=url)
    assert board.request('GET', url, auth=False)[1] == '独立复测已核对'.encode()
    assert fixes(board)[0] == read_task(board, first)


def test_fix_approve_without_admin_does_not_send_request(board_factory, fix_codex, fix_repo):
    """SOP-58-N05：实际页面脚本阻止未填令牌的批准操作。"""
    from test_client import run_page_client
    configure(fix_codex, dict(no_changes=True, categories={sid: 'environment' for sid in IDS}))
    board, pushed = fix_board(board_factory, fix_codex, fix_repo)
    fail(board, pushed)
    task = wait_fix(board, '待开发者处理')
    run_page_client(board, 'fix-approve-no-token', f'/p/{KEY}/fixes?task={task["id"]}')
    assert read_task(board, task)['status'] == '待开发者处理'


def test_attention_covers_seven_kinds_all_task_types_host_and_resume(board_factory, fix_codex, fix_repo):
    """SOP-58-P06：读取既有事项，七种类型、三种任务、会话缺省与 Host。"""
    import sqlite3
    board, _ = fix_board(board_factory, fix_codex, fix_repo, paused=True)
    prepared = [('fix', '待开发者处理', 'developer-action', ''), ('fix', '待开发者合入', 'developer-merge', 'TH-2'),
        ('execute', '待提交', 'commit-needed', 'TH-3'), ('merge', '待补充', 'cases-needed', 'TH-4'),
        ('merge', '待同步', 'sync-needed', 'TH-5'), ('fix', '已中断', 'interrupted', 'TH-6'),
        ('fix', '已合入', 'jira-manual', 'TH-7')]
    expected = []
    with sqlite3.connect(board.data / 'board.sqlite3') as db:
        for kind, status, attention, session in prepared:
            task_id = db.execute('INSERT INTO tasks(kind,project,batch,status,trigger,input_digest,input_json,created_at,session_id,error) '
                'VALUES (?,?,?,?,?,?,?,?,?,?)', (kind, KEY, '' if kind == 'fix' else 'demo', status, '前置记录', '', '{}', '2026-10-20', session, '明确的原因')).lastrowid
            if kind == 'fix':
                db.execute('INSERT INTO fix_tasks(task,repository) VALUES (?,?)', (task_id, str(fix_repo)))
            if attention == 'jira-manual':
                db.execute('INSERT INTO fix_jira VALUES (?,?,?,?,?)', (task_id, 'ABC-1', '评论成功', 'Jira 状态待人工更新', '没有可用流转'))
            expected.append((task_id, attention, kind, session))
    response = board.request('GET', f'/api/v1/projects/{KEY}/attention', auth=False, headers={'Host': 'board.local:7890'})
    assert response[0] == 200 and response[1]['ok'] is True
    items = response[1]['items']
    assert [(i['task'], i['kind']) for i in items] == [(i, k) for i, k, _, _ in expected]
    for item, (_, _, kind, session) in zip(items, expected):
        assert set(item) == {'kind', 'task', 'batch', 'reason', 'url', 'resume'} and item['reason']
        path = f'/p/{KEY}/fixes' if kind == 'fix' else f'/p/{KEY}/b/demo' + ('/merges' if kind == 'merge' else '')
        assert item['url'] == 'http://board.local:7890' + path
        assert item['resume'] == ('codex resume ' + session if session else '')
