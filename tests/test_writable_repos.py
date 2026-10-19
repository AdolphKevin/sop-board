"""SOP-31：通过命令行、HTTP 与 SQLite 核对授权及工作区发现。"""
import subprocess
from pathlib import Path

import pytest
import yaml

from conftest import ROOT, TEST_ENV, free_port
from helpers import BASE, KEY, demo

IDENTITY = 'github.com/demo-org/demo-app'


def git(repo, *args):
    return subprocess.check_output(['git', '-C', str(repo), *args], text=True).strip()


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / 'demo-repo'
    path.mkdir()
    git(path, 'init', '-q')
    (path / 'src').mkdir()
    (path / 'src/app.py').write_text('print(1)\n')
    (path / 'README.md').write_text('demo\n')
    (path / 'docs/sop').mkdir(parents=True)
    (path / 'docs/sop/demo.yaml').write_text(yaml.safe_dump(demo(), allow_unicode=True, sort_keys=False))
    (path / 'docs/sop/entries.yaml').write_text(yaml.safe_dump({'entries': {'自定义入口': dict(surface='http', operate='调用接口', observe=['响应'], evidence=['响应原文'], isolation='临时目录')}}, allow_unicode=True))
    git(path, 'add', '.')
    git(path, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'fixture')
    return path


@pytest.mark.parametrize('kind', ['missing', 'empty-admin', 'same', 'child', 'plain', 'absent', 'same-existing'])
def test_authorization_failure_leaves_no_files(tmp_path, repo, kind):
    """SOP-31-N01：七类启动失败均退出 1，新目录不存在，既有空目录仍为空。"""
    env = {**TEST_ENV, 'SOP_BOARD_ADMIN_TOKEN': 'isolated-admin'}
    path = repo
    reason = 'Git 工作区根目录'
    if kind == 'missing':
        env.pop('SOP_BOARD_ADMIN_TOKEN')
        reason = 'SOP_BOARD_ADMIN_TOKEN'
    elif kind == 'empty-admin':
        env['SOP_BOARD_ADMIN_TOKEN'] = ''
        reason = 'SOP_BOARD_ADMIN_TOKEN'
    elif kind in ('same', 'same-existing'):
        env['SOP_BOARD_ADMIN_TOKEN'] = env['SOP_BOARD_TOKEN']
        reason = '两个令牌相同'
    elif kind == 'child':
        path /= 'src'
    elif kind == 'absent':
        path = tmp_path / 'not-exist'
    elif kind == 'plain':
        path = tmp_path
    data = tmp_path / 'new-data'
    if kind == 'same-existing':
        data.mkdir()
    result = subprocess.run([str(ROOT / '.venv/bin/sop-board'), '--data', str(data), '--port', str(free_port()),
                             '--writable-repo', f'{IDENTITY}={path}'], env=env, capture_output=True, text=True, timeout=5)
    assert result.returncode == 1, result.stderr
    assert reason in result.stderr
    if kind in ('child', 'plain', 'absent'):
        assert str(path) in result.stderr
    assert '看板地址' not in result.stdout and 'Traceback' not in result.stderr
    assert list(data.iterdir()) == [] if kind == 'same-existing' else not data.exists()


def wait_for(check, timeout=12):
    import time
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        result = check()
        if result:
            return result
        time.sleep(.05)
    pytest.fail('等待可观察结果超时')


def writable_board(board_factory, repo):
    return board_factory(command=[str(ROOT / '.venv/bin/sop-board'), '--writable-repo', f'{IDENTITY}={repo}'],
                         env={'SOP_BOARD_ADMIN_TOKEN': 'isolated-admin'})


def test_discovery_versions_validation_and_revocation(board_factory, repo):
    """SOP-31-P01/N03：启动/五秒发现、全历史去重、错误行号、撤销授权、无仓库写入。"""
    original = (repo / 'docs/sop/demo.yaml').read_text()
    before = git(repo, 'status', '--porcelain'), git(repo, 'log', '--oneline'), git(repo, 'worktree', 'list')
    board = writable_board(board_factory, repo)
    assert f'可写授权：{IDENTITY}（{repo}）' in board.log.read_text()
    project = board.request('GET', '/api/v1/projects', auth=False)[1]['projects'][0]
    assert (project['id'], project['name'], project['writable']) == (IDENTITY, repo.name, True)
    assert board.sql('SELECT version,pushed_by,dirty,commit_id FROM versions') == [(1, '工作区', 0, git(repo, 'rev-parse', 'HEAD'))]
    assert '可写' in board.request('GET', '/', auth=False)[1].decode()
    changed = original.replace('N1 预期结果', '新预期结果')
    (repo / 'docs/sop/demo.yaml').write_text(changed)
    wait_for(lambda: board.request('GET', BASE, auth=False)[1].get('version') == 2)
    assert board.sql('SELECT pushed_by,dirty FROM versions WHERE version=2') == [('工作区', 1)]
    assert board.request('GET', BASE, auth=False)[1]['queue_counts']['pending'] == 4
    # 一个曾登记过的版本重新出现，也不重复登记。
    (repo / 'docs/sop/demo.yaml').write_text(original)
    wait_for(lambda: not board.request('GET', BASE, auth=False)[1].get('workspace_problems'))
    board.stop()
    board.start()
    assert board.sql('SELECT COUNT(*) FROM versions') == [(2,)]
    previous_etag = board.request('GET', BASE + '/state', auth=False)[1]['etag']
    (repo / 'docs/sop/demo.yaml').write_text(original.replace('priority: P0', 'priority: [', 1))
    wait_for(lambda: board.request('GET', BASE, auth=False)[1].get('workspace_problems'))
    assert board.request('GET', BASE + '/state', auth=False)[1]['etag'] != previous_etag
    problems = board.request('GET', BASE, auth=False)[1]['workspace_problems']
    assert all(p['line'] > 0 and p['problem'] for p in problems)
    page = board.request('GET', f'/p/{KEY}/b/demo', auth=False)[1].decode()
    assert '工作区场景文件不合格' in page and '行' in page
    assert board.sql('SELECT COUNT(*) FROM versions') == [(2,)]
    (repo / 'docs/sop/demo.yaml').write_text(original)
    assert before == (git(repo, 'status', '--porcelain'), git(repo, 'log', '--oneline'), git(repo, 'worktree', 'list'))
    board.stop()
    board.command = [str(ROOT / '.venv/bin/sop-board')]
    board.start()
    assert board.request('GET', '/api/v1/projects', auth=False)[1]['projects'][0]['writable'] is False
    assert '只读' in board.request('GET', f'/p/{KEY}/b/demo', auth=False)[1].decode()


@pytest.mark.parametrize('entry_style', ['file', 'config'])
def test_product_digest_matches_independent_fixture_and_sop_verify(board_factory, repo, entry_style, capsys):
    """SOP-31-P01 / D2：未跟踪、删除、忽略、SOP 排除；独立字面预期与 sop-verify 比较。"""
    import hashlib
    import json
    import sys
    entry = repo / 'docs/sop/entries.yaml'
    if entry_style == 'config':
        declarations = yaml.safe_load(entry.read_text())['entries']
        entry.unlink()
        entry = repo / '.tw/config.json'
        entry.parent.mkdir()
        entry.write_text(json.dumps(dict(acceptance_entries=declarations), ensure_ascii=False))
    (repo / 'README.md').unlink()
    (repo / '.gitignore').write_text('ignored\n')
    (repo / 'ignored').write_text('excluded')
    (repo / 'untracked').write_text('new')
    files = [dict(path='.gitignore', sha256=hashlib.sha256(b'ignored\n').hexdigest()),
             dict(path='README.md', deleted=True),
             dict(path='src/app.py', sha256=hashlib.sha256(b'print(1)\n').hexdigest()),
             dict(path='untracked', sha256=hashlib.sha256(b'new').hexdigest())]
    expected = hashlib.sha256(json.dumps(files, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
    board = writable_board(board_factory, repo)
    actual = board.request('GET', BASE, auth=False)[1]['product_digest']
    assert actual == expected
    script = Path.home() / 'go/src/github.com/AdolphKevin/praxis-skills/sop-verify/scripts/sop.py'
    if not script.exists():
        with capsys.disabled():
            print('sop-verify 路径不存在：仅跳过外部实现比较；其余产品指纹断言已执行')
        pytest.skip('sop-verify 路径不存在：仅跳过外部实现比较；其余产品指纹断言已执行')
    code = "import sys; from pathlib import Path; sys.path.insert(0, sys.argv[1]); from sop import product; import json; print(json.dumps(product(Path(sys.argv[2]), Path(sys.argv[2])/'docs/sop/demo.yaml', Path(sys.argv[3]))))"
    output = subprocess.check_output([sys.executable, '-B', '-c', code, str(script.parent), str(repo), str(entry)], text=True)
    assert actual == json.loads(output)['product_fingerprint']


def test_authorization_rejects_data_inside_repository(tmp_path, repo):
    """T1 只读边界：数据目录不得反过来写入授权仓库。"""
    data = repo / 'board-data'
    before = git(repo, 'status', '--porcelain')
    result = subprocess.run([str(ROOT / '.venv/bin/sop-board'), '--data', str(data), '--port', str(free_port()),
                             '--writable-repo', f'{IDENTITY}={repo}'],
                            env={**TEST_ENV, 'SOP_BOARD_ADMIN_TOKEN': 'isolated-admin'},
                            capture_output=True, text=True, timeout=5)
    assert result.returncode == 1
    assert '数据目录不能位于授权仓库内' in result.stderr
    assert not data.exists() and git(repo, 'status', '--porcelain') == before


def test_invalid_yaml_stays_with_batch_when_filename_differs(board_factory, repo):
    """SOP-31-P01：文件名不同于批次名时，语法错误仍在原批次显示。"""
    path = repo / 'docs/sop/source.yaml'
    (repo / 'docs/sop/demo.yaml').rename(path)
    board = writable_board(board_factory, repo)
    assert board.request('GET', BASE, auth=False)[1]['version'] == 1
    path.write_text('metadata: [\n')
    wait_for(lambda: board.request('GET', BASE, auth=False)[1].get('workspace_problems'), timeout=7)
    assert board.sql('SELECT COUNT(*) FROM versions') == [(1,)]


@pytest.mark.parametrize('kind', ['empty-nodes', 'unknown-entry', 'numeric-source'])
def test_discovery_uses_sop_verify_scenario_rules(board_factory, repo, kind):
    """SOP-31-P01：不接受空节点或未声明入口，接受来源的数值版本。"""
    document = demo()
    if kind == 'empty-nodes':
        document['sops'] = []
    elif kind == 'unknown-entry':
        document['sops'][0]['scenarios'][0]['verify'] = 'undeclared'
    else:
        document['metadata']['sources'][0]['version'] = 2
    (repo / 'docs/sop/demo.yaml').write_text(yaml.safe_dump(document, allow_unicode=True, sort_keys=False))
    board = writable_board(board_factory, repo)
    summary = board.request('GET', BASE, auth=False)[1]
    if kind in ('numeric-source', 'unknown-entry'):
        assert summary.get('version') == 1
        if kind == 'unknown-entry':
            assert any('undeclared' in p['problem'] for p in summary['entry_problems'])
    else:
        assert board.sql('SELECT COUNT(*) FROM versions') == [(0,)]
        assert summary['workspace_problems'] and all(p['line'] > 0 for p in summary['workspace_problems'])
