"""SOP-33/34：真实 HTTP、SQLite、临时工作区和假 codex 的合并行为。"""
import hashlib
import json
from copy import deepcopy
from pathlib import Path
import subprocess
import sys

import pytest
import yaml

from cases_helpers import cases_body, upload_cases
from codex_helpers import ADMIN, fake_codex, mode, task_board, create_task
from helpers import BASE, KEY, demo, push, push_body, assert_error
from merge_helpers import merge_cases, answer, upload_merge, settled, checked, download
from test_writable_repos import repo, git, wait_for


def test_cases_write_exact_fields_and_download_pairs(board_factory, fake_codex, repo):
    """SOP-33-P01/P06、SOP-34-P02：确定字段、元数据、合法文件、配对与不提交。"""
    path = repo / 'docs/sop/demo.yaml'
    path.unlink()
    head = git(repo, 'rev-parse', 'HEAD')
    document = merge_cases()
    document['test_cases'][2]['title'] = '反向：余额不足不能支付'
    mode(fake_codex, final=json.dumps(answer(document), ensure_ascii=False))
    board = task_board(board_factory, fake_codex, repo)
    body = cases_body(document)
    body['content'] = body['content'].replace('- 输入 "admin" 并登录', '- ' + json.dumps('输入 "admin" 并登录', ensure_ascii=False))
    upload_cases(board, body, base=BASE)
    task = settled(board, board.request('GET', '/api/v1/tasks', auth=False)[1]['tasks'][-1])
    assert task['status'] == '已完成', task['error']
    assert path.exists(), '授权仓库应写入场景文件'
    status, raw, _ = download(board, task)
    assert status == 200 and raw == path.read_bytes()
    merged = yaml.safe_load(raw)
    assert [(n['id'], n['title']) for n in merged['sops']] == [('S01', '登录'), ('S02', '订单')]
    scenes = [s for n in merged['sops'] for s in n['scenarios']]
    assert len(scenes) == 3
    for case, scene in zip(document['test_cases'], scenes):
        expected = dict(id=case['id'], title=case['title'], actor='测试岗', priority=case['priority'],
                        polarity='negative' if 'negative' in case['tags'] else 'positive',
                        verify='自定义入口', refs=[case['id']], tags=case['tags'], initial_state=case['preconditions'],
                        steps=case['steps'], expected=case['expected_results'], forbidden_effects=[])
        if 'negative' in case['tags']:
            expected['negative_type'] = 'invalid-input'
        assert scene == expected
    assert merged['metadata'] == dict(requirement='用户与订单', batch='demo', version=1, owner='张三',
        sources=[dict(ref='批次 demo 用例上传 #1', version=1, confirmed_by='张三')])
    assert checked(board, task)['ok'] is True and checked(board, task)['problems'] == []
    assert json.loads(board.request('GET', task['files']['nodes.json'], auth=False)[1]) == {'登录': 'S01', '订单': 'S02'}
    status, pairs, _ = download(board, task, 'pairs.json')
    assert status == 200
    assert pairs == dict(covered=[dict(case=c['id'], scenarios=[c['id']], note='同编号用例场景') for c in document['test_cases']], conflict=[], disposed=[])
    assert git(repo, 'rev-parse', 'HEAD') == head
    assert board.sql('SELECT version,pushed_by FROM versions') == [(1, f"看板合并 #{task['id']}")]
    verify_with_sop(repo, raw, pairs, document)


def verify_with_sop(repo, raw, pairs, document):
    """外部脚本只在临时仓库写本机状态；不改脚本、不读取用户配置或令牌。"""
    import os
    script = Path.home() / 'go/src/github.com/AdolphKevin/praxis-skills/sop-verify/scripts/sop.py'
    if not script.exists():
        from sop_board.scenario_file import load_scenarios
        load_scenarios(raw.decode(), entries={'自定义入口'})
        pytest.skip('sop.py 不存在：已用本仓逻辑核对场景；跳过外部 import-cases/coverage')
    (repo / 'cases.yaml').write_text(yaml.safe_dump(document, allow_unicode=True))
    (repo / 'pairs.json').write_text(json.dumps(pairs, ensure_ascii=False))
    (repo / 'docs/sop/demo.yaml').write_bytes(raw)
    env = {k: v for k, v in os.environ.items() if not k.startswith(('SOP_BOARD_', 'JIRA_', 'GIT_'))}
    env['SOP_VERIFY_STATE'] = str(repo.parent / 'sop-verify-state')
    for args in [('check', 'docs/sop/demo.yaml'),
                 ('import-cases', 'docs/sop/demo.yaml', 'cases.yaml'),
                 ('coverage', 'docs/sop/demo.yaml', '--pairs', 'pairs.json')]:
        run = subprocess.run([sys.executable, '-B', str(script), '--root', str(repo), '--format', 'json', *args],
                             env=env, capture_output=True, text=True, timeout=15)
        assert run.returncode == 0, run.stdout + run.stderr
        value = json.loads(run.stdout)
        assert value['ok'] is True
        print('sop-verify ' + ' '.join(args) + '：' + run.stdout.strip())
        if args[0] == 'coverage':
            assert value['complete'] is True and value['total'] == 3


def test_readonly_draft_downloads_are_accepted_by_sop_verify(board_factory, fake_codex, repo):
    """SOP-34-P02：未授权项目草稿与配对供外部 check/import-cases/coverage 直接消费。"""
    board = task_board(board_factory, fake_codex)
    push(board)
    task = settled(board, upload_merge(board))
    assert task['status'] == '已完成' and checked(board, task)['draft'] is True
    raw = download(board, task)[1]
    status, pairs, _ = download(board, task, 'pairs.json')
    assert status == 200 and len(pairs['covered']) == 3
    assert board.sql('SELECT version,pushed_by FROM versions') == [(1, '开发岗')]
    verify_with_sop(repo, raw, pairs, merge_cases())


@pytest.mark.parametrize('kind,problem', [
    ('json', 'JSON'), ('missing', 'cases'), ('enum', 'negative_type'), ('extra', '格式外字段'),
    ('shape', 'actor'), ('unknown-case', 'TC_DEMO_NONE_001'), ('unknown-scene', 'N9-P01'),
    ('duplicate-json', 'JSON'), ('deep-json', 'JSON')])
def test_invalid_output_fails_with_original_files_and_no_version(board_factory, fake_codex, repo, kind, problem):
    """SOP-33-N07：完整核对输出模式、未知编号和枚举；不写场景并保留原始输出。"""
    result = answer(original=demo())
    if kind == 'missing':
        result.pop('cases')
    elif kind == 'enum':
        result['cases'][1]['negative_type'] = 'sideways'
    elif kind == 'extra':
        result['cases'][0]['title'] = '试图改写标题'
    elif kind == 'shape':
        result['nodes'][0]['actor'] = []
    elif kind == 'unknown-case':
        result['cases'][0]['id'] = 'TC_DEMO_NONE_001'
    elif kind == 'unknown-scene':
        result['scenarios'][0]['id'] = 'N9-P01'
    final = '不是 JSON' if kind == 'json' else json.dumps(result, ensure_ascii=False)
    if kind == 'duplicate-json':
        final = final.replace('"cases":', '"cases": [], "cases":', 1)
    elif kind == 'deep-json':
        final = '[' * 2000 + '0' + ']' * 2000
    mode(fake_codex, final=final)
    board = task_board(board_factory, fake_codex, repo)
    path = repo / 'docs/sop/demo.yaml'
    original = path.read_bytes()
    task = settled(board, upload_merge(board))
    assert task['status'] == '失败', task
    check = checked(board, task)
    assert check['ok'] is False and problem in str(check['problems'])
    for name in ('events.jsonl', 'stderr.txt', 'final.json', 'check.json'):
        status, raw, _ = board.request('GET', task['files'][name], auth=False)
        assert status == 200 and raw == (board.data / 'tasks' / str(task['id']) / name).read_bytes()
    assert (board.data / 'tasks' / str(task['id']) / 'final.json').read_text() == final
    assert path.read_bytes() == original
    assert board.sql('SELECT COUNT(*) FROM versions') == [(1,)]
    assert_error(download(board, task), 404, 'merge-result-not-found')
    assert_error(download(board, task, 'pairs.json'), 404, 'merge-result-not-found')


def test_all_semantic_problems_are_listed_together(board_factory, fake_codex, repo):
    """SOP-33-N01：入口、角色、遗漏、目标和移动编号的问题须逐条列出。"""
    original = demo()
    result = answer(original=original)
    result['cases'][0]['verify'] = 'desktop'
    result['cases'][2]['actor'] = ''
    result['scenarios'] = [dict(id='N1-N01', action='duplicate', target='TC_DEMO_NONE_001', reason='重复',
                                forbidden_effects=[], transfer_regression=False),
                           dict(id='N2-P01', action='kept'), dict(id='N2-N01', action='kept')]
    result['relocate'] = {'N2-N01': 'S09'}
    mode(fake_codex, final=json.dumps(result, ensure_ascii=False))
    board = task_board(board_factory, fake_codex, repo)
    before = (repo / 'docs/sop/demo.yaml').read_bytes()
    task = settled(board, upload_merge(board))
    assert task['status'] == '失败'
    problems = str(checked(board, task)['problems'])
    for value in ('desktop', 'TC_DEMO_ORDER_001', 'actor', 'N1-P01', 'TC_DEMO_NONE_001', 'S09'):
        assert value in problems
    assert (repo / 'docs/sop/demo.yaml').read_bytes() == before


def test_duplicate_conflict_and_kept_preserve_guarantees(board_factory, fake_codex):
    """SOP-33-P02/P03/P08：重复合入禁止副作用及回归，冲突移除，其他角色/条件保留。"""
    original = demo()
    regression = dict(check='unit', cases=['test_payment_guard'])
    original['sops'][0]['scenarios'][0].update(forbidden_effects=['不重复扣款'], regression=regression)
    original['sops'][1]['scenarios'][0]['actor'] = '管理员'
    original['sops'][1]['scenarios'][0]['regression'] = dict(check='unit', cases=['guard_admin'])
    original['sops'][1]['scenarios'][1]['initial_state'] = ['账号已锁定']
    result = answer(original=original)
    result['scenarios'][0] = dict(id='N1-P01', action='duplicate', target='TC_DEMO_LOGIN_001',
                                  reason='同一扣款流程', forbidden_effects=['不重复扣款'], transfer_regression=True)
    result['scenarios'][1] = dict(id='N1-N01', action='conflict', target='TC_DEMO_LOGIN_002',
                                  reason='余额不足时用例要求拒绝')
    mode(fake_codex, final=json.dumps(result, ensure_ascii=False))
    board = task_board(board_factory, fake_codex)
    push(board, push_body(original))
    task = settled(board, upload_merge(board))
    assert task['status'] == '已完成', checked(board, task)
    status, raw, _ = download(board, task)
    assert status == 200
    document = yaml.safe_load(raw)
    scenes = {s['id']: s for n in document['sops'] for s in n['scenarios']}
    assert set(scenes) == {'TC_DEMO_LOGIN_001', 'TC_DEMO_LOGIN_002', 'TC_DEMO_ORDER_001', 'N2-P01', 'N2-N01'}
    assert scenes['TC_DEMO_LOGIN_001']['forbidden_effects'] == ['不重复扣款']
    assert scenes['TC_DEMO_LOGIN_001']['regression'] == regression
    assert scenes['TC_DEMO_LOGIN_002']['expected'] == ['拒绝登录']
    for old in original['sops'][1]['scenarios']:
        assert scenes[old['id']] == old
    assert document['sops'][-1] == original['sops'][1]
    assert document['metadata']['sources'][-1] == original['metadata']['sources'][0]
    page = board.request('GET', f'/p/{KEY}/b/demo/merges?task={task["id"]}', auth=False)[1].decode()
    report = board.request('GET', BASE + '/report.md', auth=False)[1].decode().split('## 合并记录', 1)[1]
    for content in (page, report):
        for value in ('N1-P01', 'TC_DEMO_LOGIN_001', '重复', '同一扣款流程',
                      'N1-N01', 'TC_DEMO_LOGIN_002', '冲突', '余额不足时用例要求拒绝'):
            assert value in content
    # 用例更新后的合并仍保留补充场景的回归声明和目标用例已转移的回归声明。
    updated = merge_cases()
    updated['test_cases'][0]['expected_results'] = ['新的预期结果']
    mode(fake_codex, final=json.dumps(answer(merge_cases(), document), ensure_ascii=False))
    push(board, push_body(document))
    intermediate = settled(board, board.request('GET', '/api/v1/tasks', auth=False)[1]['tasks'][-1])
    assert intermediate['status'] == '已完成', checked(board, intermediate)
    mode(fake_codex, final=json.dumps(answer(updated, document), ensure_ascii=False))
    next_task = settled(board, upload_merge(board, updated))
    assert next_task['status'] == '已完成', checked(board, next_task)
    next_scenes = {s['id']: s for n in yaml.safe_load(download(board, next_task)[1])['sops'] for s in n['scenarios']}
    assert next_scenes['TC_DEMO_LOGIN_001']['regression'] == regression
    assert next_scenes['N2-P01']['regression'] == dict(check='unit', cases=['guard_admin'])
    assert next_scenes['TC_DEMO_LOGIN_001']['expected'] == ['新的预期结果']
    assert next_scenes['TC_DEMO_LOGIN_001']['forbidden_effects'] == ['不重复扣款']


@pytest.mark.parametrize('kind', ['missing-effects', 'missing-transfer'])
def test_duplicate_cannot_drop_original_guarantees(board_factory, fake_codex, kind):
    """SOP-33-P03/P08：重复处理不能丢掉已有的禁止副作用或回归声明。"""
    original = demo()
    original['sops'][0]['scenarios'][0].update(forbidden_effects=['不通知其他租户'],
                                            regression=dict(check='unit', cases=['guard']))
    result = answer(original=original)
    result['scenarios'][0] = dict(id='N1-P01', action='duplicate', target='TC_DEMO_LOGIN_001',
        reason='同一流程', forbidden_effects=[] if kind == 'missing-effects' else ['不通知其他租户'],
        transfer_regression=kind != 'missing-transfer')
    result['relocate'] = {'N1-N01': 'S01'}
    mode(fake_codex, final=json.dumps(result, ensure_ascii=False))
    board = task_board(board_factory, fake_codex)
    push(board, push_body(original))
    task = settled(board, upload_merge(board))
    assert task['status'] == '失败'
    assert ('forbidden_effects' if kind == 'missing-effects' else 'regression') in str(checked(board, task)['problems'])


def test_original_tc_id_is_rejected(board_factory, fake_codex):
    """SOP-33-N02：非用例场景占用 TC_ 编号须明确报错且不生成结果。"""
    original = demo()
    original['sops'][0]['scenarios'][0]['id'] = 'TC_DEMO_OLD_001'
    mode(fake_codex, final=json.dumps(answer(original=original), ensure_ascii=False))
    board = task_board(board_factory, fake_codex)
    push(board, push_body(original))
    task = settled(board, upload_merge(board))
    assert task['status'] == '失败'
    assert 'TC_DEMO_OLD_001 使用了 TC_ 形式' in str(checked(board, task)['problems'])


def test_merge_into_and_relocate_keep_bodies_sources_and_reserved_numbers(board_factory, fake_codex):
    """SOP-33-P04/P09：负向分组合并、原节点失去正向依据时移动，编号与内容保留。"""
    document = merge_cases()
    extra = deepcopy(document['test_cases'][1])
    extra.update(id='TC_DEMO_LOGIN_004', tags=['section:登录异常', 'negative'])
    document['test_cases'].append(extra)
    original = demo()
    result = answer(document, original)
    next(n for n in result['nodes'] if n['section'] == '订单').update(actor='买家', entry='下单接口')
    next(n for n in result['nodes'] if n['section'] == '登录异常')['merge_into'] = '登录'
    result['scenarios'][2] = dict(id='N2-P01', action='duplicate', target='TC_DEMO_ORDER_001',
                                  reason='订单正向已覆盖', forbidden_effects=[], transfer_regression=False)
    result['relocate'] = {'N2-N01': 'S01'}
    mode(fake_codex, final=json.dumps(result, ensure_ascii=False))
    board = task_board(board_factory, fake_codex)
    push(board, push_body(original))
    task = settled(board, upload_merge(board, document))
    assert task['status'] == '已完成', checked(board, task)
    merged = yaml.safe_load(download(board, task)[1])
    assert [(n['id'], n['title']) for n in merged['sops']] == [('S01', '登录'), ('S03', '订单'), ('N1', 'N1')]
    assert merged['sops'][1]['actor'] == '买家' and merged['sops'][1]['entry'] == '下单接口'
    scenes = {s['id']: (n['id'], s) for n in merged['sops'] for s in n['scenarios']}
    assert scenes['TC_DEMO_LOGIN_004'][0] == 'S01'
    assert scenes['N2-N01'] == ('S01', original['sops'][1]['scenarios'][1])
    assert merged['sops'][-1] == original['sops'][0]
    assert json.loads(board.request('GET', task['files']['nodes.json'], auth=False)[1]) == {'登录': 'S01', '登录异常': 'S02', '订单': 'S03'}


def test_gaps_wait_without_draft_and_new_cases_trigger_again(board_factory, fake_codex, repo):
    """SOP-33-P04/N06：没有正向依据时待补充、无两个下载、原文件和版本不变。"""
    document = merge_cases()
    extra = deepcopy(document['test_cases'][1])
    extra.update(id='TC_DEMO_REFUND_001', tags=['section:退款', 'negative'])
    document['test_cases'].append(extra)
    result = answer(document, demo())
    result['gaps'] = ['退款 没有正向依据']
    mode(fake_codex, final=json.dumps(result, ensure_ascii=False))
    board = task_board(board_factory, fake_codex, repo)
    original = (repo / 'docs/sop/demo.yaml').read_bytes()
    task = settled(board, upload_merge(board, document))
    assert task['status'] == '待补充'
    assert checked(board, task)['gaps'] == ['退款 没有正向依据']
    assert_error(download(board, task), 404, 'merge-result-not-found')
    assert_error(download(board, task, 'pairs.json'), 404, 'merge-result-not-found')
    assert 'scenario-file.yaml' not in task['files'] and 'pairs.json' not in task['files']
    assert (repo / 'docs/sop/demo.yaml').read_bytes() == original
    assert board.sql('SELECT COUNT(*) FROM versions') == [(1,)]
    new = deepcopy(document)
    extra.update(id='TC_DEMO_REFUND_002', tags=['section:退款'])
    new['test_cases'].append(extra)
    mode(fake_codex, final=json.dumps(answer(new, demo()), ensure_ascii=False))
    next_task = settled(board, upload_merge(board, new))
    assert next_task['id'] != task['id'] and next_task['status'] == '已完成'


def test_inferred_entries_initial_state_and_exemption_are_marked(board_factory, fake_codex, repo):
    """SOP-34-P01：缺失入口和前置状态时推断；注释、两个文件、版本、不再触发。"""
    (repo / 'docs/sop/demo.yaml').unlink()
    (repo / 'docs/sop/entries.yaml').unlink()
    document = merge_cases()
    document['test_cases'][2].pop('preconditions')
    result = answer(document, verify='web')
    result['entries'] = dict(entries={'web': dict(surface='web', operate='打开浏览器登录页',
        observe=['页面文字'], evidence=['截图'], isolation='临时目录')})
    result['negative_exempt'] = [dict(section='订单', reason='只读查询，无写入', inferred=True)]
    mode(fake_codex, final=json.dumps(result, ensure_ascii=False))
    head = git(repo, 'rev-parse', 'HEAD')
    board = task_board(board_factory, fake_codex, repo)
    task = settled(board, upload_merge(board, document))
    assert task['status'] == '已完成', checked(board, task)
    raw = (repo / 'docs/sop/demo.yaml').read_text()
    assert '# 待确认 TC_DEMO_ORDER_001：访客未登录' in raw
    assert '# 待确认 S02：只读查询，无写入' in raw
    assert (repo / 'docs/sop/entries.yaml').read_text().startswith('# 待确认')
    assert yaml.safe_load((repo / 'docs/sop/entries.yaml').read_text()) == result['entries']
    check = checked(board, task)
    assert [i['id'] for i in check['inferences']] == ['TC_DEMO_ORDER_001', 'S02', 'entries']
    page = board.request('GET', f'/p/{KEY}/b/demo/merges?task={task["id"]}', auth=False)[1].decode()
    report = board.request('GET', BASE + '/report.md', auth=False)[1].decode().split('## 合并记录', 1)[1]
    for content in (page, report):
        for value in ('TC_DEMO_ORDER_001', '访客未登录', 'S02', '只读查询，无写入', '入口声明由 Codex 推断'):
            assert value in content
    assert raw.encode() == download(board, task)[1]
    assert git(repo, 'rev-parse', 'HEAD') == head
    before = board.sql('SELECT version,pushed_by FROM versions')
    assert before == [(1, f"看板合并 #{task['id']}")]
    # 五秒扫描至少执行两次，写入内容不成为工作区版本或新任务。
    import time
    time.sleep(10.2)
    assert board.sql('SELECT version,pushed_by FROM versions') == before
    assert board.sql('SELECT COUNT(*) FROM tasks') == [(1,)]


def test_preconditions_override_model_initial_state_with_note(board_factory, fake_codex):
    """SOP-33-N03：原前置条件逐字保留，不写推断注释，检查记录说明未采用模型输入。"""
    document = merge_cases()
    result = answer(document, demo())
    result['cases'][0]['initial_state'] = ['未登录的访客']
    mode(fake_codex, final=json.dumps(result, ensure_ascii=False))
    board = task_board(board_factory, fake_codex)
    push(board)
    task = settled(board, upload_merge(board, document))
    assert task['status'] == '已完成'
    raw = download(board, task)[1].decode()
    scene = yaml.safe_load(raw)['sops'][0]['scenarios'][0]
    assert scene['initial_state'] == ['已注册账号 alice']
    assert '未登录的访客' not in raw and '# 待确认 TC_DEMO_LOGIN_001' not in raw
    notes = str(checked(board, task)['notes'])
    assert 'TC_DEMO_LOGIN_001' in notes and 'preconditions' in notes and '未采用' in notes


def test_remaining_original_positive_node_can_receive_exemption(board_factory, fake_codex):
    """SOP-33-P02：正反重复场景移除后，额外正向场景仍留原节点并满足合法格式。"""
    original = demo()
    extra = deepcopy(original['sops'][0]['scenarios'][0])
    extra.update(id='N1-P02', title='登录次数限制', expected=['超限时拒绝登录'])
    original['sops'][0]['scenarios'].append(extra)
    result = answer(original=original)
    result['scenarios'][0] = dict(id='N1-P01', action='duplicate', target='TC_DEMO_LOGIN_001',
        reason='同一登录流程', forbidden_effects=[], transfer_regression=False)
    result['scenarios'][1] = dict(id='N1-N01', action='duplicate', target='TC_DEMO_LOGIN_002',
        reason='同一登录失败流程', forbidden_effects=[], transfer_regression=False)
    result['negative_exempt'].append(dict(section='N1', reason='保留的次数限制只有正向依据', inferred=False))
    mode(fake_codex, final=json.dumps(result, ensure_ascii=False))
    board = task_board(board_factory, fake_codex)
    push(board, push_body(original))
    task = settled(board, upload_merge(board))
    assert task['status'] == '已完成', checked(board, task)
    nodes = yaml.safe_load(download(board, task)[1])['sops']
    node = next(n for n in nodes if n['id'] == 'N1')
    assert node['scenarios'] == [extra]
    assert node['negative_exempt'] == '保留的次数限制只有正向依据'
