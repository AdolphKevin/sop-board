"""SOP-35：通过真实 HTTP、工作区发现与 SQLite 核对 SOP 原文契约。"""

from copy import deepcopy
import hashlib

import pytest
import yaml

from helpers import BASE, KEY, assert_error, demo, push, push_body, snapshot
from codex_helpers import fake_codex
from html_helpers import page
from test_writable_repos import git, repo, wait_for, writable_board


def source(content='# 登录流程\n', **overrides):
    return dict(dict(path='docs/sop/sop-login.md', ref='登录 SOP', commit='sop-c1',
                     confirmed_by='owner-a', content=content,
                     sha256=hashlib.sha256(content.encode()).hexdigest()), **overrides)


@pytest.mark.parametrize('invalid,field', [
    ('invalid', 'entries'), (None, 'entries'), ({}, 'entries.entries'),
    ({'entries': []}, 'entries.entries'),
    ({'entries': {'web': {}}}, 'entries.entries.web.surface'),
    ({'entries': {'web': dict(surface='web', operate='打开页面', observe='页面',
                             evidence=['截图'], isolation='临时目录')}}, 'entries.entries.web.observe'),
    ({'entries': push_body()['entries']['entries'], 'checks': []}, 'entries.checks'),
    ({'entries': push_body()['entries']['entries'], 'checks': {'verify': dict(argv=['pytest'], timeout=0, format='junit', report='r.xml')}}, 'entries.checks.verify.timeout'),
])
def test_push_rejects_invalid_entries_without_persistence(board, invalid, field):
    """SOP-35-N03：入口声明不合格时，场景、入口与原文均不变。"""
    body, _ = push(board)
    before = snapshot(board)
    changed = dict(body, content=body['content'] + '# changed\n', entries=invalid,
                   sop_sources=[source()])
    problems = assert_error(board.request('PUT', BASE + '/scenario-file', changed),
                            422, 'scenario-file-invalid')
    assert field in [p['field'] for p in problems]
    assert all(p['line'] == 1 for p in problems)
    assert snapshot(board) == before


def test_push_accepts_complete_sop_verify_entry_declarations(board):
    """SOP-35-P01/N03：沿用 sop-verify 的可选启动/重载说明及检查声明，并逐字段核对。"""
    declaration = push_body()['entries']
    entry = declaration['entries']['自定义入口']
    entry.update(candidate='start', address_env='BOARD_URL', start=['启动测试服务'], reload=['重载测试服务'])
    declaration['checks'] = {'guard': dict(argv=['pytest', '-q'], timeout=30, format='junit', report='results.xml',
                                         cwd='.', required_env=[], purpose='接口检查')}
    push(board, push_body(entries=declaration))
    before = snapshot(board)
    for field, value in [('candidate', 'invalid'), ('start', []), ('reload', 'invalid'), ('address_env', 'not-a-variable')]:
        invalid = deepcopy(declaration)
        invalid['entries']['自定义入口'][field] = value
        problems = assert_error(board.request('PUT', BASE + '/scenario-file', push_body(entries=invalid)),
                                422, 'scenario-file-invalid')
        assert 'entries.entries.自定义入口.' + field in [p['field'] for p in problems]
        assert snapshot(board) == before


@pytest.mark.parametrize('kind,field', [
    ('object', 'sop_sources'), ('item', 'sop_sources[0]'),
    ('missing-sha', 'sop_sources[0].sha256'), ('digest', 'sop_sources[0].sha256'),
    ('confirmer', 'sop_sources[0].confirmed_by'), ('oversize', 'sop_sources[0].content'),
    ('multibyte', 'sop_sources[0].content'), ('absolute', 'sop_sources[0].path'),
    ('parent', 'sop_sources[0].path'), ('duplicate', 'sop_sources[1].path'),
])
def test_push_rejects_invalid_sources_atomically(board, kind, field):
    """SOP-35-N01/N03：摘要、字段、字节上限或路径有误时整份推送不保存。"""
    body, _ = push(board)
    before = snapshot(board)
    sources = [source()]
    if kind == 'object':
        sources = {}
    elif kind == 'item':
        sources = ['text']
    elif kind == 'missing-sha':
        del sources[0]['sha256']
    elif kind == 'digest':
        sources[0]['sha256'] = '0' * 64
    elif kind == 'confirmer':
        del sources[0]['confirmed_by']
    elif kind in ('oversize', 'multibyte'):
        sources = [source('a' * (2 * 1024 * 1024 + 1) if kind == 'oversize' else '中' * 699051)]
    elif kind == 'absolute':
        sources[0]['path'] = '/outside.md'
    elif kind == 'parent':
        sources[0]['path'] = '../outside.md'
    elif kind == 'duplicate':
        sources.append(source('# 别的内容\n'))
    problems = assert_error(board.request('PUT', BASE + '/scenario-file',
                            dict(body, content=body['content'] + '# changed\n', sop_sources=sources)),
                            422, 'scenario-file-invalid')
    assert field in [p['field'] for p in problems]
    assert snapshot(board) == before


def test_source_versions_are_independent_deduplicated_and_survive_restart(board):
    """SOP-35-P01/P02：原文变化独立登记，缺省字段与相同原文保留既有版本。"""
    first = source()
    body, _ = push(board, push_body(sop_sources=[first]))
    recorded = board.sql('SELECT recorded_at FROM sop_versions')[0][0]
    assert board.sql('SELECT path,version,ref,commit_id,sha256,confirmed_by,content FROM sop_versions') == [
        (first['path'], 1, first['ref'], 'sop-c1', first['sha256'], 'owner-a', first['content'])]
    # 只有原文变化，场景版本与场景行保持原值。
    scenarios = board.sql('SELECT * FROM scenarios')
    second = source('# 登录流程\n\n第二版。\n', confirmed_by='owner-b', commit='sop-c2')
    assert push(board, dict(body, sop_sources=[second]))[1]['unchanged'] is True
    assert board.sql('SELECT version FROM versions') == [(1,)]
    assert board.sql('SELECT * FROM scenarios') == scenarios
    assert board.sql('SELECT version,commit_id,confirmed_by,content FROM sop_versions ORDER BY version') == [
        (1, 'sop-c1', 'owner-a', first['content']), (2, 'sop-c2', 'owner-b', second['content'])]
    # 相同内容、不同确认人/提交号不能新增或覆盖历史。
    push(board, dict(body, content=body['content'] + '# 场景排版变化\n',
                     sop_sources=[dict(second, commit='sop-c3', confirmed_by='owner-c')]))
    omitted = deepcopy(body)
    del omitted['entries']
    del omitted['sop_sources']
    omitted['content'] += '# 未带原文\n'
    push(board, omitted)
    assert board.sql('SELECT version FROM versions ORDER BY version') == [(1,), (2,), (3,)]
    assert board.sql('SELECT version,confirmed_by FROM sop_versions ORDER BY version') == [(1, 'owner-a'), (2, 'owner-b')]
    assert board.sql('SELECT recorded_at FROM sop_versions WHERE version=1') == [(recorded,)]
    assert len(board.sql('SELECT * FROM batch_entries')) == 1
    board.stop()
    board.start()
    assert board.sql('SELECT version,content FROM sop_versions ORDER BY version') == [(1, first['content']), (2, second['content'])]


def test_source_exactly_two_mib_is_accepted(board):
    """SOP-35-N01 边界：上限按 UTF-8 字节数计，恰好 2 MiB 合格。"""
    original = source('中' * 699050 + 'ab')
    push(board, push_body(sop_sources=[original]))
    assert board.sql('SELECT length(CAST(content AS BLOB)),version FROM sop_versions') == [(2097152, 1)]


def test_discovery_registers_sources_even_when_scenarios_are_unchanged(board_factory, repo):
    """SOP-35-P03：按 ref 中首个仓库内文件读取，HEAD/确认人正确，五秒发现原文单独变化。"""
    login = repo / 'docs/sop/sop-login.md'
    login.write_bytes('# 登录流程\r\n\r\n已确认。\r\n'.encode())
    order = repo / 'docs/flows/order.md'
    order.parent.mkdir()
    order.write_text('# 订单流程\n')
    document = demo()
    document['metadata']['sources'] = [
        dict(ref='docs/sop/sop-login.md 第 2 节', version='1', confirmed_by='owner-a'),
        dict(ref='docs/flows/none.md docs/flows/order.md', version='1', confirmed_by='owner-order')]
    (repo / 'docs/sop/demo.yaml').write_text(yaml.safe_dump(document, allow_unicode=True))
    head = git(repo, 'rev-parse', 'HEAD')
    board = writable_board(board_factory, repo)
    assert board.sql('SELECT path,version,ref,commit_id,confirmed_by,content FROM sop_versions ORDER BY path') == [
        ('docs/flows/order.md', 1, 'docs/flows/none.md docs/flows/order.md', head, 'owner-order', '# 订单流程\n'),
        ('docs/sop/sop-login.md', 1, 'docs/sop/sop-login.md 第 2 节', head, 'owner-a', '# 登录流程\r\n\r\n已确认。\r\n')]
    login.write_text('# 登录流程\n\n第二版。\n')
    wait_for(lambda: board.sql('SELECT COUNT(*) FROM sop_versions') == [(3,)], timeout=7)
    assert board.sql('SELECT version,commit_id,confirmed_by,content FROM sop_versions WHERE path=? ORDER BY version',
                     ('docs/sop/sop-login.md',))[-1] == (2, head, 'owner-a', '# 登录流程\n\n第二版。\n')
    assert board.sql('SELECT version FROM versions') == [(1,)]
    assert '第二版。' in page(board, f'/p/{KEY}/b/demo/sop').text
    board.stop()
    board.start()
    assert board.sql('SELECT COUNT(*) FROM sop_versions') == [(3,)]


def test_discovery_skips_outside_paths_urls_and_symlinks_in_page_and_task(board_factory, repo, fake_codex):
    """SOP-35-N02：越界路径、网址和越界符号链接不读取；原文页与合并快照均为空。"""
    from codex_helpers import task_board
    from merge_helpers import settled, upload_merge
    secret = repo.parent / 'outside.md'
    secret.write_text('outside-fixture-only')
    (repo / 'docs/link.md').symlink_to(secret)
    document = demo()
    document['metadata']['sources'] = [dict(ref=ref, version='1', confirmed_by='owner') for ref in (
        '../outside.md 第 1 节', str(secret), 'https://example.invalid/order 第 2 节', 'docs/link.md')]
    (repo / 'docs/sop/demo.yaml').write_text(yaml.safe_dump(document, allow_unicode=True))
    board = task_board(board_factory, fake_codex, repo)
    assert board.sql('SELECT COUNT(*) FROM sop_versions') == [(0,)]
    doc = page(board, f'/p/{KEY}/b/demo/sop')
    assert '尚无 SOP 原文' in doc.text and 'outside-fixture-only' not in doc.text
    task = settled(board, upload_merge(board))
    assert task['status'] == '已完成'
    assert not any(name.startswith('sop/') for name in task['files'])
    assert not (board.data / 'tasks' / str(task['id']) / 'sop').exists()
