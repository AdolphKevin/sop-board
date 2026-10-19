"""修订 3：SOP-31 的启动失败与数据目录不变保证。"""
import subprocess

import pytest

from conftest import ROOT, TEST_ENV, free_port
from test_writable_repos import IDENTITY, repo


def launch(data, arguments, env):
    return subprocess.run([str(ROOT / '.venv/bin/sop-board'), '--data', str(data), '--port', str(free_port()), *arguments],
                          env={**TEST_ENV, 'SOP_BOARD_ADMIN_TOKEN': 'isolated-admin', **env},
                          capture_output=True, text=True, timeout=5)


@pytest.mark.parametrize('argument', ['github.com/demo-org/demo-app', '=somewhere', 'github.com/demo-org/demo-app=', ' =somewhere', 'github.com/demo-org/demo-app=   '])
def test_malformed_authorization_has_exact_reason_and_no_files(tmp_path, argument):
    """SOP-31-N04：错误参数报约定原文，退出 1，既有与新目录不变。"""
    for existing in (False, True):
        data = tmp_path / ('existing' if existing else 'new')
        if existing:
            data.mkdir()
        result = launch(data, ['--writable-repo', argument], {})
        assert result.returncode == 1 and '看板地址' not in result.stdout
        assert '参数应写成 <项目身份>=<本机仓库路径>' in result.stderr
        assert 'Traceback' not in result.stderr
        assert list(data.iterdir()) == [] if existing else not data.exists()


@pytest.mark.parametrize('value', ['0', '-5', 'abc', '1.5', '', ' ', 'nan', 'inf'])
@pytest.mark.parametrize('authorized', [False, True])
def test_timeout_must_be_positive_integer_before_any_data_files(tmp_path, repo, value, authorized):
    """SOP-31-N04 / 修订 3 I1：无授权时也拒绝无效超时，且不留新文件。"""
    for existing in (False, True):
        data = tmp_path / ('existing' if existing else 'new')
        if existing:
            data.mkdir()
        args = ['--writable-repo', f'{IDENTITY}={repo}'] if authorized else []
        result = launch(data, args, {'SOP_BOARD_CODEX_TIMEOUT': value})
        assert result.returncode == 1 and '看板地址' not in result.stdout
        assert 'SOP_BOARD_CODEX_TIMEOUT' in result.stderr and '正整数' in result.stderr
        assert 'Traceback' not in result.stderr
        assert list(data.iterdir()) == [] if existing else not data.exists()


def test_repeated_authorization_keeps_registered_name_and_readonly_project(board_factory, repo, tmp_path):
    """SOP-31-P03：两行授权、新项目名称、已有名称及未授权页面状态。"""
    import shutil
    from helpers import push_body
    from html_helpers import page
    board = board_factory()
    for suffix, name, batch in [('app-a', '甲应用', 'demo'), ('other', '其他应用', 'b1')]:
        body = push_body()
        body['project'] = dict(id=f'github.com/demo-org/{suffix}', name=name)
        body['content'] = body['content'].replace('batch: demo', f'batch: {batch}')
        assert board.request('PUT', f'/api/v1/projects/github-com-demo-org-{suffix}/batches/{batch}/scenario-file', body)[0] == 200
    board.stop()
    first, second = tmp_path / 'app-a', tmp_path / 'app-b'
    shutil.copytree(repo, first)
    shutil.copytree(repo, second)
    with (first / 'docs/sop/demo.yaml').open('a') as target:
        target.write('# 新发现版本\n')
    board.command = [str(ROOT / '.venv/bin/sop-board'), '--writable-repo', f'github.com/demo-org/app-a={first}',
                     '--writable-repo', f'github.com/demo-org/app-b={second}']
    board.env['SOP_BOARD_ADMIN_TOKEN'] = 'isolated-admin'
    board.start()
    projects = {p['id']: p for p in board.request('GET', '/api/v1/projects', auth=False)[1]['projects']}
    assert projects['github.com/demo-org/app-a']['name'] == '甲应用'
    assert projects['github.com/demo-org/app-b']['name'] == 'app-b'
    current_log = board.log.read_text().splitlines()[-3:]
    assert current_log[:2] == [f'可写授权：github.com/demo-org/app-a（{first}）', f'可写授权：github.com/demo-org/app-b（{second}）']
    sections = page(board, '/').find_all('section')
    for ident, label in [('app-a', '可写'), ('app-b', '可写'), ('other', '只读')]:
        section = next(s for s in sections if f'github.com/demo-org/{ident}' in s.text)
        assert label in section.text
    assert '只读' in page(board, '/p/github-com-demo-org-other/b/b1').text
