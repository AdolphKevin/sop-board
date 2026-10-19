"""SOP-31 修订 3：工作区发现、版本与独立产品指纹比较。"""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time

import yaml

from conftest import ROOT
from helpers import BASE, KEY, IDS, push_body, round_body, upload
from test_writable_repos import repo, git, writable_board, wait_for


def summary(board):
    return board.request('GET', BASE, auth=False)[1]


def test_discovery_exact_content_ignore_entries_reset_changed_and_deduplicate_push(board_factory, repo):
    """SOP-31-P01/P07：精确首版、入口文件排除、已通过场景回待执行及发现/推送内容去重。"""
    path = repo / 'docs/sop/demo.yaml'
    original = path.read_text()
    before = (git(repo, 'status', '--porcelain'), git(repo, 'rev-parse', 'HEAD'))
    board = writable_board(board_factory, repo)
    initial = summary(board)
    assert board.sql('SELECT name FROM batches') == [('demo',)]
    assert initial['workspace_problems'] == []
    assert board.sql('SELECT content,pushed_by,commit_id,dirty FROM versions') == [(original, '工作区', before[1], 0)]
    pushed = push_body(commit=before[1], product_digest=initial['product_digest'])
    body = round_body(pushed, upload(board), statuses=('pass',) * 4)
    body['product'] = dict(commit=before[1], digest=initial['product_digest'], dirty=False)
    assert board.request('POST', BASE + '/rounds', body)[0] == 200
    assert summary(board)['queues']['pending'] == []
    document = yaml.safe_load(original)
    document['sops'][0]['scenarios'][0]['expected'] = ['新预期']
    raw = yaml.safe_dump(document, allow_unicode=True, sort_keys=False)
    path.write_text(raw)
    wait_for(lambda: summary(board)['version'] == 2)
    assert summary(board)['queues']['pending'] == [IDS[0]]
    assert board.sql('SELECT content,pushed_by,commit_id,dirty FROM versions WHERE version=2') == [(raw, '工作区', before[1], 1)]
    path.write_text(raw)
    time.sleep(5.2)
    assert board.sql('SELECT COUNT(*) FROM versions') == [(2,)]
    document['sops'][0]['scenarios'][0]['expected'] = ['推送版本的预期']
    pushed = push_body(document, commit=before[1], product_digest=initial['product_digest'])
    status, response, _ = board.request('PUT', BASE + '/scenario-file', pushed)
    assert status == 200 and response['version'] == 3
    path.write_text(pushed['content'])
    time.sleep(5.2)
    assert board.sql('SELECT version,pushed_by FROM versions ORDER BY version') == [(1, '工作区'), (2, '工作区'), (3, '开发岗')]
    assert git(repo, 'rev-parse', 'HEAD') == before[1]
    assert git(repo, 'status', '--porcelain') == 'M docs/sop/demo.yaml'


def test_revocation_stops_discovery_and_marks_home_and_batch_readonly(board_factory, repo):
    """SOP-31-N05：去掉参数后无授权输出，两页只读，后续内容变化不登记，不写仓库。"""
    board = writable_board(board_factory, repo)
    assert summary(board)['version'] == 1
    board.stop()
    offset = board.log.stat().st_size
    board.command = [str(ROOT / '.venv/bin/sop-board')]
    board.start()
    assert '可写授权：' not in board.log.read_bytes()[offset:].decode()
    for page in ('/', f'/p/{KEY}/b/demo'):
        assert '只读' in board.request('GET', page, auth=False)[1].decode()
    path = repo / 'docs/sop/demo.yaml'
    path.write_text(path.read_text() + '# 撤销后的新内容\n')
    before = {str(p.relative_to(repo)): p.read_bytes() for p in repo.rglob('*') if p.is_file() and '.git' not in p.relative_to(repo).parts}
    time.sleep(5.2)
    assert summary(board)['version'] == 1 and board.sql('SELECT COUNT(*) FROM versions') == [(1,)]
    assert before == {str(p.relative_to(repo)): p.read_bytes() for p in repo.rglob('*') if p.is_file() and '.git' not in p.relative_to(repo).parts}


def test_invalid_polarity_reports_exact_line_and_preserves_all_version_bytes(board_factory, repo):
    """SOP-31-N06：polarity 错误对应原文精确行号，既有版本逐字段不变。"""
    path = repo / 'docs/sop/demo.yaml'
    raw = path.read_text()
    line = next(i for i, text in enumerate(raw.splitlines(), 1) if 'polarity: positive' in text)
    board = writable_board(board_factory, repo)
    before = board.sql('SELECT * FROM versions')
    path.write_text(raw.replace('polarity: positive', 'polarity: sideways', 1))
    wait_for(lambda: summary(board).get('workspace_problems'))
    problems = summary(board)['workspace_problems']
    assert any(p['field'].endswith('.polarity') and p['line'] == line and 'positive 或 negative' in p['problem'] for p in problems)
    page = board.request('GET', f'/p/{KEY}/b/demo', auth=False)[1].decode()
    assert '工作区场景文件不合格' in page and f'第 {line} 行' in page and 'polarity' in page
    assert summary(board)['version'] == 1 and board.sql('SELECT * FROM versions') == before


def test_product_digest_changes_only_for_product_files_at_all_four_states(board_factory, repo, capsys):
    """SOP-31-P08：F1/F2/F3/F4 各与 sop-verify 对照，场景、入口及忽略文件不改变指纹。"""
    (repo / '.gitignore').write_text('build/\n')
    git(repo, 'add', '.gitignore')
    from check_helpers import record_commit
    record_commit(repo)
    (repo / 'src/app.py').write_text('print(2)\n')
    (repo / 'notes.txt').write_text('untracked\n')
    (repo / 'build').mkdir()
    (repo / 'build/out.log').write_text('ignored one\n')
    board = writable_board(board_factory, repo)
    script = Path.home() / 'go/src/github.com/AdolphKevin/praxis-skills/sop-verify/scripts/sop.py'
    code = "import sys,json;from pathlib import Path;sys.path.insert(0,sys.argv[1]);from sop import product;r=Path(sys.argv[2]);print(json.dumps(product(r,r/'docs/sop/demo.yaml',r/'docs/sop/entries.yaml')))"
    values = []
    path = repo / 'docs/sop/demo.yaml'
    for step in range(4):
        if step:
            if step == 1:
                entry = repo / 'docs/sop/entries.yaml'
                entry.write_text(entry.read_text().replace('调用接口', '新的调用说明'))
                (repo / 'build/out.log').write_text('ignored two\n')
            elif step == 2:
                (repo / 'README.md').unlink()
            else:
                (repo / 'extra.txt').write_text('new product file\n')
            document = yaml.safe_load(path.read_text())
            document['sops'][0]['scenarios'][0]['expected'] = [f'步骤 {step} 的场景预期']
            path.write_text(yaml.safe_dump(document, allow_unicode=True, sort_keys=False))
            wait_for(lambda: summary(board)['version'] == step + 1)
        actual = summary(board)['product_digest']
        assert summary(board)['dirty'] is True
        if script.exists():
            external = json.loads(subprocess.check_output([sys.executable, '-B', '-c', code, str(script.parent), str(repo)], text=True))
            assert actual == external['product_fingerprint']
        values.append(actual)
    assert values[1] == values[0] and values[2] != values[1] and values[3] != values[2]
    if not script.exists():
        with capsys.disabled():
            print('sop-verify 路径不存在；F1–F4 其余断言已执行，仅跳过外部比较')
