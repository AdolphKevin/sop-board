"""隔离仓库中的 Python 检查；预设在仓库外，变更预设不改变被测产品。"""
import json
import os
import sys

import pytest
import yaml

from codex_helpers import ADMIN, read_task
from helpers import BASE
from test_check_entries import declarations, save_declarations, declared_board
from test_writable_repos import repo, git, wait_for


def record_commit(repo):
    """只给临时夹具写 Git 对象与 HEAD，不调用 git commit 或切换分支。"""
    git(repo, 'add', '.')
    tree = git(repo, 'write-tree')
    parent = git(repo, 'rev-parse', 'HEAD')
    commit = git(repo, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid',
                 'commit-tree', tree, '-p', parent, '-m', 'check fixture')
    git(repo, 'update-ref', 'HEAD', commit)
    return commit


SCRIPT = r'''
import json, os, pathlib, subprocess, sys, time
mode, presets, output = sys.argv[1], pathlib.Path(sys.argv[2]), pathlib.Path(sys.argv[3])
entry_environment = json.loads(sys.argv[4]) if sys.platform == 'darwin' else sorted(os.environ)
config = json.loads((presets / (mode + '.json')).read_text())
with (presets / (mode + '-calls.jsonl')).open('a') as stream:
    stream.write(json.dumps(dict(cwd=str(pathlib.Path.cwd()), env=entry_environment, pid=os.getpid(),
        required_value_present=bool(os.environ.get('CHECK_FIXTURE_VALUE')), output=str(output),
        commit=subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip())) + '\n')
if config.get('child'):
    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(90)'])
    (presets / 'child.pid').write_text(str(child.pid))
time.sleep(config.get('delay', 0))
print('fixture stderr', file=sys.stderr, flush=True)
if mode == 'junit':
    print('fixture stdout', flush=True)
    if config.get('report') is not None:
        report = pathlib.Path('report.xml') if config.get('relative_report') else output / 'report.xml'
        report.write_text(config['report'])
else:
    print(config.get('report', ''), flush=True)
sys.exit(config.get('exit', 0))
'''


@pytest.fixture
def check_repo(repo, tmp_path):
    presets = tmp_path / 'presets'
    presets.mkdir()
    (repo / 'scripts').mkdir()
    for name in ('junit', 'go'):
        (repo / 'scripts' / (name + '.py')).write_text(SCRIPT)
    if sys.platform == 'darwin':
        # 和假 Codex 一样，在 Python 为 macOS 添加变量前观察实际 exec 环境。
        wrapper = repo / 'scripts/check'
        wrapper.write_text('#!/usr/bin/perl\nuse JSON::PP; use FindBin; exec ' + json.dumps(sys.executable, ensure_ascii=False) +
            ', "$FindBin::Bin/$ARGV[0].py", @ARGV, encode_json([sort keys %ENV]);\n')
        wrapper.chmod(0o755)
    value = declarations(repo)
    value['checks'] = {name: dict(argv=[*(['./scripts/check'] if sys.platform == 'darwin' else
                                       [sys.executable, 'scripts/' + name + '.py']), name, str(presets), '{output}'],
                                 format='junit' if name == 'junit' else 'go-json',
                                 **(dict(report='{output}/report.xml') if name == 'junit' else {}))
                       for name in ('junit', 'go')}
    save_declarations(repo, value)
    document = yaml.safe_load((repo / 'docs/sop/demo.yaml').read_text())
    scenes = [s for n in document['sops'] for s in n['scenarios']]
    for scene, check, cases in zip(scenes, ['junit', 'junit', 'go'],
                                  [['pkg.Order::test_create', 'pkg.Order::test_cancel'],
                                   ['pkg.Order::test_refund'],
                                   ['example.com/app::TestCreate', 'example.com/app::TestCancel']]):
        scene['regression'] = dict(check=check, cases=cases)
    (repo / 'docs/sop/demo.yaml').write_text(yaml.safe_dump(document, allow_unicode=True))
    record_commit(repo)
    preset(presets, 'junit', report='<testsuites><testsuite>' + ''.join(
        f'<testcase classname="pkg.Order" name="test_{name}" />' for name in ['create', 'cancel', 'refund']) + '</testsuite></testsuites>')
    preset(presets, 'go', report='\n'.join(json.dumps(dict(Action='pass', Package='example.com/app', **item))
        for item in [dict(Test='TestCreate'), dict(Test='TestCancel'), {}]))
    return repo, presets


def preset(presets, name, **value):
    path = presets / (name + '.json')
    data = json.loads(path.read_text()) if path.exists() else {}
    path.write_text(json.dumps(dict(data, **value)))


def start_check(board):
    status, body, _ = board.request('POST', BASE + '/checks', {}, headers=ADMIN)
    assert status == 200, body
    return body['task']


def settle_check(board, task):
    return wait_for(lambda: (t if (t := read_task(board, task))['status'] in
                            ('已完成', '失败', '已作废', '已放弃', '已中断') else None), timeout=20)


def check_rounds(board, task):
    return [board.request('GET', BASE + '/rounds/' + str(n), auth=False)[1] for n in task['rounds']]
