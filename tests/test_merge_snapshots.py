"""SOP-33：输入快照、节点持久编号和自动触发去重。"""
from copy import deepcopy
import json
import hashlib
import time

import yaml

from cases_helpers import cases_body, upload_cases
from codex_helpers import ADMIN, fake_codex, mode, task_board, create_task
from helpers import BASE, KEY, demo, push, push_body, assert_error
from merge_helpers import merge_cases, answer, upload_merge, settled, checked, download
from test_writable_repos import repo, wait_for


def task_list(board):
    return board.request('GET', f'/api/v1/tasks?project={KEY}&batch=demo', auth=False)[1]['tasks']


def test_snapshots_use_config_then_file_and_freeze_sources(board_factory, fake_codex, repo):
    """SOP-33-P10：入口优先级、空场景、用例并集、节点编号、SOP 原文快照。"""
    config = repo / '.tw/config.json'
    config.parent.mkdir()
    declaration = yaml.safe_load((repo / 'docs/sop/entries.yaml').read_text())['entries']
    config.write_text(json.dumps(dict(acceptance_entries={'cfg-web': declaration['自定义入口']})))
    flow = repo / 'docs/flows/order.md'
    flow.parent.mkdir()
    flow.write_text('# 订单流程\n\n独立事实原文。\n')
    original = demo()
    original['metadata']['sources'][0]['ref'] = 'docs/flows/order.md 第 2 节'
    for node in original['sops']:
        for scene in node['scenarios']:
            scene['verify'] = 'cfg-web'
    raw = yaml.safe_dump(original, allow_unicode=True, sort_keys=False)
    (repo / 'docs/sop/demo.yaml').write_text(raw)
    result = answer(original=original, verify='cfg-web')
    mode(fake_codex, delay=.3, final=json.dumps(result, ensure_ascii=False))
    board = task_board(board_factory, fake_codex, repo)
    task = settled(board, upload_merge(board))
    assert task['status'] == '已完成', checked(board, task)
    root = board.data / 'tasks' / str(task['id'])
    assert (root / 'scenarios.yaml').read_text() == raw
    assert yaml.safe_load((root / 'cases.yaml').read_text()) == merge_cases()
    # SOP-BOARD-4 I1：读取后回填 candidate 的明确缺省值。
    assert yaml.safe_load((root / 'entries.yaml').read_text()) == {'entries': {'cfg-web': dict(declaration['自定义入口'], candidate='in-place')}}
    assert (root / 'sop/docs/flows/order.md').read_text() == '# 订单流程\n\n独立事实原文。\n'
    assert 'sop/docs/flows/order.md' in task['files']
    # 新任务建立时即冻结原文；Codex 运行期间的文件变化不改变快照。
    flow.write_text('# 第二版\n')
    mode(fake_codex, delay=.5)
    new_task = create_task(board)
    flow.write_text('# 第三版\n')
    new_task = settled(board, new_task)
    assert (board.data / 'tasks' / str(new_task['id']) / 'sop/docs/flows/order.md').read_text() == '# 第二版\n'
    config.unlink()
    # 原补充场景的入口在新声明中也须有效，改名由使用者显式提交。
    document = yaml.safe_load((repo / 'docs/sop/demo.yaml').read_text())
    for node in document['sops']:
        for scene in node['scenarios']:
            scene['verify'] = '自定义入口'
    push(board, push_body(document))
    task = settled(board, task_list(board)[-1])
    assert yaml.safe_load((board.data / 'tasks' / str(task['id']) / 'entries.yaml').read_text()) == {
        'entries': {'自定义入口': dict(declaration['自定义入口'], candidate='in-place')}}
    new_base = BASE.replace('/demo', '/newb')
    task = settled(board, upload_merge(board, base=new_base))
    assert task['status'] == '已完成'
    assert (board.data / 'tasks' / str(task['id']) / 'scenarios.yaml').read_text() == ''


def test_stable_sections_regression_and_board_versions_do_not_loop(board_factory, fake_codex, repo):
    """SOP-33-P05/P07、SOP-34-P03：新分组编号追加、回归按编号保留、写入只增加一个版本。"""
    path = repo / 'docs/sop/demo.yaml'
    path.unlink()
    document = merge_cases()
    # 输入文件顺序不决定编号，编号按用例排序后的 section 首次出现顺序。
    document['test_cases'].reverse()
    board = task_board(board_factory, fake_codex, repo)
    task = settled(board, upload_merge(board, document))
    assert task['status'] == '已完成'
    original = yaml.safe_load(path.read_text())
    regression = dict(check='unit', cases=['guard_login'])
    original['sops'][0]['scenarios'][0]['regression'] = regression
    path.write_text(yaml.safe_dump(original, allow_unicode=True, sort_keys=False))
    wait_for(lambda: len(task_list(board)) == 2, timeout=7)
    task = settled(board, task_list(board)[-1])
    assert task['trigger'] == '场景文件变化' and task['status'] == '已完成'
    unchanged = upload_cases(board, cases_body(document), base=BASE)
    assert unchanged['unchanged_upload'] is True and len(task_list(board)) == 2
    extra = deepcopy(document['test_cases'][0])
    extra.update(id='TC_DEMO_AAA_001', tags=['section:退款'])
    next_document = deepcopy(document)
    next_document['test_cases'].append(deepcopy(extra))
    next(c for c in next_document['test_cases'] if c['id'] == 'TC_DEMO_LOGIN_001')['expected_results'] = ['新的登录预期']
    task = settled(board, upload_merge(board, next_document))
    merged = yaml.safe_load(path.read_text())
    assert [(n['id'], n['title']) for n in merged['sops']] == [('S01', '登录'), ('S02', '订单'), ('S03', '退款')]
    login = merged['sops'][0]['scenarios'][0]
    assert login['regression'] == regression and login['expected'] == ['新的登录预期']
    board.stop()
    board.start()
    assert settled(board, create_task(board))['status'] == '已完成'
    extra.update(id='TC_DEMO_MISC_001', tags=['P1'])
    next_document['test_cases'].append(deepcopy(extra))
    task = settled(board, upload_merge(board, next_document))
    assert [(n['id'], n['title']) for n in yaml.safe_load(path.read_text())['sops']] == [('S01', '登录'), ('S02', '订单'), ('S03', '退款'), ('S04', '未分组')]
    count = len(task_list(board))
    versions = board.sql('SELECT version,pushed_by FROM versions')
    time.sleep(10.2)
    assert len(task_list(board)) == count and board.sql('SELECT version,pushed_by FROM versions') == versions
    assert not any(by == '工作区' for _, by in versions[3:])
    assert board.sql('SELECT section,node FROM node_numbers ORDER BY node') == [('登录','S01'),('订单','S02'),('退款','S03'),('未分组','S04')]


def test_digest_is_input_combination_and_failed_inputs_retry_on_change(board_factory, fake_codex):
    """SOP-33-N04/P11：A/B/C 历史输入去重，失败不去重；入口或 SOP 原文变化参与摘要。"""
    board = task_board(board_factory, fake_codex)
    a = push_body()
    push(board, a)
    first = settled(board, upload_merge(board))
    assert first['status'] == '已完成'
    assert push(board, a)[1]['unchanged'] is True and len(task_list(board)) == 1
    b = dict(a, content=a['content'] + '# B\n')
    push(board, b)
    second = settled(board, task_list(board)[-1])
    assert second['trigger'] == '场景文件变化' and second['input_digest'] != first['input_digest']
    push(board, a)
    assert len(task_list(board)) == 2
    mode(fake_codex, exit=1)
    c = dict(a, content=a['content'] + '# C\n')
    push(board, c)
    third = settled(board, task_list(board)[-1])
    assert third['status'] == '失败'
    mode(fake_codex)
    push(board, b)
    assert len(task_list(board)) == 3
    push(board, c)
    fourth = settled(board, task_list(board)[-1])
    assert fourth['status'] == '已完成' and fourth['input_digest'] == third['input_digest']
    # 原文不同但解析后用例并集相同，不因上传序号或文件排版改变而重复合并。
    old_count = len(task_list(board))
    body = cases_body(merge_cases())
    body['content'] += '# 只有排版变化\n'
    upload_cases(board, body, base=BASE)
    assert len(task_list(board)) == old_count


def test_readonly_uses_latest_pushed_entries_and_hashes_their_content(board_factory, fake_codex):
    """SOP-33-P10、I4 摘要：未授权项目读取推送声明，入口改变独立影响输入摘要。"""
    original = demo()
    for node in original['sops']:
        for scene in node['scenarios']:
            scene['verify'] = 'push-web'
    declaration = dict(surface='web', operate='操作推送入口', observe=['页面'], evidence=['截图'], isolation='临时目录')
    body = push_body(original, entries={'entries': {'push-web': declaration}})
    board = task_board(board_factory, fake_codex)
    push(board, body)
    first = settled(board, upload_merge(board))
    assert first['status'] == '已完成', checked(board, first)
    root = board.data / 'tasks' / str(first['id'])
    assert yaml.safe_load((root / 'entries.yaml').read_text()) == body['entries']
    assert not (root / 'sop').exists()
    same = settled(board, create_task(board))
    assert same['input_digest'] == first['input_digest']
    changed = deepcopy(body)
    changed['entries']['entries']['push-web']['operate'] = '修改后的入口操作说明'
    assert push(board, changed)[1]['unchanged'] is True
    new = settled(board, create_task(board))
    assert new['scenario_file_sha256'] == first['scenario_file_sha256']
    assert new['input_digest'] != first['input_digest']
    assert yaml.safe_load((board.data / 'tasks' / str(new['id']) / 'entries.yaml').read_text()) == changed['entries']


def test_sop_source_content_alone_changes_input_digest(board_factory, fake_codex, repo):
    """SOP-33-P10、I4 摘要：场景和用例相同时，SOP 原文改变仍得到不同摘要。"""
    path = repo / 'docs/flow.md'
    path.write_text('# 第一版事实\n')
    original = demo()
    original['metadata']['sources'][0]['ref'] = 'docs/flow.md 第 1 节'
    (repo / 'docs/sop/demo.yaml').write_text(yaml.safe_dump(original, allow_unicode=True))
    board = task_board(board_factory, fake_codex, repo)
    settled(board, upload_merge(board))
    first = settled(board, create_task(board))
    same = settled(board, create_task(board))
    assert first['input_digest'] == same['input_digest']
    path.write_text('# 第二版事实\n')
    changed = settled(board, create_task(board))
    assert changed['scenario_file_sha256'] == same['scenario_file_sha256']
    assert changed['input_digest'] != same['input_digest']


def test_readonly_snapshots_include_pushed_sop_originals(board_factory, fake_codex):
    """SOP-33-P10：未授权项目已有推送原文时也冻结原文，来源变化参与组合摘要。"""
    content = '# 远端 SOP\r\n\r\n已确认的业务事实。\r\n'
    source = dict(path='docs/remote.md', ref='远端业务流程', commit='source-1', confirmed_by='业务负责人',
                  content=content, sha256=hashlib.sha256(content.encode()).hexdigest())
    body = push_body(sop_sources=[source])
    board = task_board(board_factory, fake_codex)
    push(board, body)
    first = settled(board, upload_merge(board))
    assert first['status'] == '已完成'
    path = board.data / 'tasks' / str(first['id']) / 'sop/docs/remote.md'
    assert path.read_bytes() == content.encode()
    same = settled(board, create_task(board))
    assert same['input_digest'] == first['input_digest']
    new_source = dict(source, content='# 第二版 SOP\n',
                      sha256=hashlib.sha256('# 第二版 SOP\n'.encode()).hexdigest())
    assert push(board, dict(body, sop_sources=[new_source]))[1]['unchanged'] is True
    changed = settled(board, create_task(board))
    assert changed['scenario_file_sha256'] == same['scenario_file_sha256']
    assert changed['input_digest'] != same['input_digest']
    assert path.read_bytes() == content.encode()
    assert (board.data / 'tasks' / str(changed['id']) / 'sop/docs/remote.md').read_bytes() == new_source['content'].encode()
