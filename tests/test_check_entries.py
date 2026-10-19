"""SOP-41：经真实 HTTP 读取入口与检查声明。"""
import json

import pytest
import yaml

from conftest import ROOT
from helpers import KEY, BASE, assert_error, push
from test_writable_repos import repo, IDENTITY
from codex_helpers import ADMIN


def declared_board(factory, repo, **env):
    from prepared_board_helpers import prepared_board
    return prepared_board(factory, [str(ROOT / '.venv/bin/sop-board'), '--writable-repo', f'{IDENTITY}={repo}'],
                          repo, {'SOP_BOARD_ADMIN_TOKEN': 'isolated-admin', **env})


def declarations(repo):
    return yaml.safe_load((repo / 'docs/sop/entries.yaml').read_text())


def save_declarations(repo, value):
    (repo / 'docs/sop/entries.yaml').write_text(yaml.safe_dump(json.loads(json.dumps(value)), allow_unicode=True))


def test_entries_sources_defaults_and_precedence(board_factory, repo):
    """SOP-41-P01/P02：无令牌可读，来源互斥、回填缺省、忽略其他检查字段。"""
    value = declarations(repo)
    value['checks'] = {'unit': dict(argv=['python3', 'check.py', '{output}'], format='junit',
                                 report='{output}/report.xml', owner='qa')}
    value['entries']['api'] = dict(value['entries']['自定义入口'], candidate='start',
                                   start=['./serve.sh'], reload=['./reload.sh'], address_env='APP_URL')
    save_declarations(repo, value)
    # I1 规定其他检查字段忽略，YAML 自动识别日期也不应改变这一点。
    path = repo / 'docs/sop/entries.yaml'
    path.write_text(path.read_text().replace('owner: qa', 'owner: 2026-10-20'))
    board = declared_board(board_factory, repo)
    status, data, _ = board.request('GET', f'/api/v1/projects/{KEY}/entries', auth=False)
    assert status == 200 and data['ok'] is True and data['problems'] == []
    assert data['source'] == 'entries-yaml'
    assert data['entries']['自定义入口']['candidate'] == 'in-place'
    assert data['entries']['api']['start'] == ['./serve.sh']
    assert data['entries']['api']['reload'] == ['./reload.sh']
    assert data['entries']['api']['address_env'] == 'APP_URL'
    assert data['checks']['unit'] == dict(argv=['python3', 'check.py', '{output}'], format='junit',
                                        report='{output}/report.xml', cwd='.', timeout=900, required_env=[])
    config = repo / '.tw/config.json'
    config.parent.mkdir()
    config.write_text(json.dumps(dict(acceptance_entries={'web-ui': value['entries']['api']},
                                     checks={'go-unit': dict(argv=['python3', 'go.py'], format='go-json')})))
    data = board.request('GET', f'/api/v1/projects/{KEY}/entries', auth=False)[1]
    assert data['source'] == 'tw-config' and set(data['entries']) == {'web-ui'}
    assert set(data['checks']) == {'go-unit'}
    config.write_text(json.dumps(dict(acceptance_entries=value['entries'])))
    data = board.request('GET', f'/api/v1/projects/{KEY}/entries', auth=False)[1]
    assert data['source'] == 'tw-config' and data['checks'] == {}


def test_entries_project_boundaries(board_factory, repo):
    """SOP-41-N06：项目不存在 404，未授权 409。"""
    board = board_factory()
    push(board)
    assert_error(board.request('GET', '/api/v1/projects/no-such/entries', auth=False), 404, 'project-not-found')
    assert_error(board.request('GET', f'/api/v1/projects/{KEY}/entries', auth=False), 409, 'repo-not-writable')


@pytest.mark.parametrize('target,field,value,problem', [
    ('entry', 'candidate', 'docker', 'candidate'),
    ('entry', 'candidate', 7, 'candidate'),
    ('entry', 'start', 'serve.sh', 'start'),
    ('entry', 'reload', [1], 'reload'),
    ('entry', 'surface', [], 'surface'),
    ('entry', 'observe', 'response', 'observe'),
    ('entry', 'evidence', [], 'evidence'),
    ('entry', 'operate', '', 'operate'),
    ('entry', 'isolation', None, 'isolation'),
    ('entry', 'address_env', 'BAD-NAME', 'address_env'),
    ('check', 'argv', None, 'argv'),
    ('check', 'argv', 'pytest -q', 'argv'),
    ('check', 'argv', [], 'argv'),
    ('check', 'format', 'tap', 'format'),
    ('check', 'report', None, 'report'),
    ('check', 'cwd', '../outside', 'cwd'),
    ('check', 'timeout', True, 'timeout'),
    ('check', 'timeout', 0, 'timeout'),
    ('check', 'required_env', 'DB_URL', 'required_env'),
    ('check', 'required_env', ['bad-name'], 'required_env'),
    ('entry', 'start', None, 'start'),
])
def test_invalid_declarations_block_request_and_show_problem(board_factory, repo, target, field, value, problem):
    """SOP-41-N01/N02：错误声明可读可展示，但不能建立检查任务。"""
    board = declared_board(board_factory, repo)
    data = declarations(repo)
    data['checks'] = {'unit': dict(argv=['python3', 'check.py'], format='junit', report='{output}/report.xml')}
    obj = data['entries']['自定义入口'] if target == 'entry' else data['checks']['unit']
    if field == 'start':
        obj['candidate'] = 'start'
    if value is None:
        obj.pop(field, None)
    else:
        obj[field] = value
    save_declarations(repo, data)
    status, result, _ = board.request('GET', f'/api/v1/projects/{KEY}/entries', auth=False)
    assert status == 200 and result['ok'] is False
    assert any(problem in p['problem'] or problem in p['field'] for p in result['problems'])
    page = board.request('GET', f'/p/{KEY}/b/demo', auth=False)[1].decode()
    assert '入口声明不合格' in page and problem in page
    assert_error(board.request('POST', BASE + '/checks', {}, headers=ADMIN), 409, 'entries-invalid')
    assert board.sql('SELECT COUNT(*) FROM tasks') == [(0,)]


def test_unknown_references_are_registered_and_reported(board_factory, repo):
    """SOP-41-N03：分别指出场景的未知入口和检查，页面与检查请求一致。"""
    document = yaml.safe_load((repo / 'docs/sop/demo.yaml').read_text())
    document['sops'][0]['scenarios'][0]['verify'] = 'ghost-entry'
    document['sops'][0]['scenarios'][1]['regression'] = dict(check='ghost-check', cases=['pkg::missing'])
    (repo / 'docs/sop/demo.yaml').write_text(yaml.safe_dump(document, allow_unicode=True))
    board = declared_board(board_factory, repo)
    assert board.sql('SELECT COUNT(*) FROM versions') == [(1,)]
    result = board.request('GET', f'/api/v1/projects/{KEY}/entries', auth=False)[1]
    assert result['ok'] is False
    problems = str(result['problems'])
    assert all(s in problems for s in ('N1-P01', 'ghost-entry', 'N1-N01', 'ghost-check'))
    assert_error(board.request('POST', BASE + '/checks', {}, headers=ADMIN), 409, 'entries-invalid')


@pytest.mark.parametrize('raw', ['[1]', 'entries: []', 'entries: {cli: 2}', 'entries: {}\nchecks: []',
                                 'entries: &e {cli: *e}', 'entries: [', 'entries: {}\nentries: {}',
                                 'entries: {cli: {surface: 2026-10-20}}',
                                 'entries: {cli: {candidate: .nan}}',
                                 '{"entries": {"cli": {"surface": NaN}}}'])
def test_malformed_declarations_are_reported(board_factory, repo, raw):
    """SOP-41 I1：结构、语法、别名和重复字段错误均作为声明问题返回。"""
    board = declared_board(board_factory, repo)
    (repo / 'docs/sop/entries.yaml').write_text(raw)
    status, result, _ = board.request('GET', f'/api/v1/projects/{KEY}/entries', auth=False)
    assert status == 200 and result['ok'] is False and result['problems']
    json.dumps(result, allow_nan=False)  # 浏览器必须能解析返回的标准 JSON。


def test_declaration_changes_invalidate_page_poll_state(board_factory, repo):
    """SOP-41 I1：声明问题恢复与出现均让已打开页面的轮询发现变化。"""
    board = declared_board(board_factory, repo)
    original = board.request('GET', BASE + '/state', auth=False)[1]['etag']
    value = declarations(repo)
    value['entries']['自定义入口']['candidate'] = 'docker'
    save_declarations(repo, value)
    invalid = board.request('GET', BASE + '/state', auth=False)[1]['etag']
    assert invalid != original
    value['entries']['自定义入口'].pop('candidate')
    save_declarations(repo, value)
    assert board.request('GET', BASE + '/state', auth=False)[1]['etag'] == original
