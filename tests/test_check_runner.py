"""SOP-45：真实 HTTP → Git 快照 → 检查子进程 → SQLite 轮次。"""
import json
import pytest

from check_helpers import check_repo, declared_board, start_check, settle_check, check_rounds
from helpers import BASE, IDS
from test_writable_repos import repo, git
from check_helpers import preset
from check_helpers import declarations, save_declarations, record_commit


def test_junit_and_go_checks_register_rounds_once_on_snapshot(board_factory, check_repo):
    """SOP-45-P01/P02/P03/P08：同一检查只跑一次，报告与产品均可追溯。"""
    repo, presets = check_repo
    board = declared_board(board_factory, repo)
    original = git(repo, 'status', '--porcelain')
    task = settle_check(board, start_check(board))
    assert task['status'] == '已完成', task
    assert task['kind'] == 'check' and task['trigger'] == '开发者发起'
    assert {s['scenario'] for s in task['scope']} == set(IDS[:3])
    assert task['void_reason'] == ''
    snapshot = task['snapshot']
    directory = board.data.resolve() / 'tasks' / str(task['id'])
    assert json.loads((directory / 'snapshot.json').read_text()) == snapshot
    assert snapshot['commit'] == git(repo, 'rev-parse', 'HEAD') and snapshot['cases_sha256'] == ''
    assert len(snapshot['entries_sha256']) == 64 and len(snapshot['scenario_file_sha256']) == 64
    assert set(snapshot['scenarios']) == set(IDS[:3])
    rounds = check_rounds(board, task)
    assert len(rounds) == 2
    assert [r['submitted_by'] for r in rounds] == ['回归检查 junit', '回归检查 go']
    for name, round_ in zip(['junit', 'go'], rounds):
        calls = [json.loads(line) for line in (presets / (name + '-calls.jsonl')).read_text().splitlines()]
        assert len(calls) == 1
        assert str(board.data.resolve() / 'worktrees') in calls[0]['cwd']
        assert calls[0]['commit'] == snapshot['commit']
        assert calls[0]['output'] == str(directory / 'checks' / name)
        assert round_['kind'] == 'agent'
        assert round_['product'] == dict(commit=snapshot['commit'], digest=snapshot['product_digest'], dirty=False)
        for result in round_['results']:
            assert result['status'] == 'pass' and result['executor'] == '回归检查 ' + name
            assert result['session'] == str(task['id'])
            count = 1 if result['scenario'] == IDS[1] else 2
            assert result['actual'] == f'回归检查 {name} 通过：声明的 {count} 个用例全部通过'
            names = {e['name'] for e in result['evidence']}
            assert 'output.txt' in names and ('report.xml' in names if name == 'junit' else True)
            for evidence in result['evidence']:
                status, raw, _ = board.request('GET', '/api/v1/evidence/' + evidence['sha256'], auth=False)
                assert status == 200
                expected = (directory / 'checks' / name / evidence['name']).read_bytes()
                assert raw == expected
        output = (directory / 'checks' / name / 'output.txt').read_text()
        assert 'fixture stderr' in output
        if name == 'junit':
            assert 'fixture stdout' in output
    assert board.sql('SELECT COUNT(*) FROM rounds') == [(2,)]
    assert not list((board.data / 'worktrees').iterdir())
    assert git(repo, 'status', '--porcelain') == original
    assert len(git(repo, 'worktree', 'list').splitlines()) == 1
    assert task['session_id'] is None


@pytest.mark.parametrize('name,report,expected', [
    ('junit', '<testsuite><testcase classname="pkg.Order" name="test_create"><failure/></testcase>'
              '<testcase classname="pkg.Order" name="test_cancel"><skipped/></testcase>'
              '<testcase classname="pkg.Order" name="test_refund"><error/></testcase></testsuite>',
     [('fail', ['失败', 'pkg.Order::test_create', '跳过', 'pkg.Order::test_cancel']),
      ('fail', ['失败', 'pkg.Order::test_refund'])]),
    ('go', '\n'.join(json.dumps(e) for e in [
        dict(Action='fail', Package='example.com/app', Test='TestCreate'),
        dict(Action='skip', Package='example.com/app', Test='TestCancel'),
        dict(Action='fail', Package='example.com/app')]),
     [('fail', ['失败', 'example.com/app::TestCreate', '跳过', 'example.com/app::TestCancel'])]),
    ('junit', '<testsuite><testcase classname="other" name="unrelated"/></testsuite>',
     [('fail', ['缺失', 'pkg.Order::test_create', 'pkg.Order::test_cancel']),
      ('fail', ['缺失', 'pkg.Order::test_refund'])]),
    ('go', json.dumps(dict(Action='fail', Package='example.com/app')),
     [('fail', ['缺失', 'example.com/app::TestCreate', 'example.com/app::TestCancel'])]),
])
def test_declared_cases_must_appear_and_pass(board_factory, check_repo, name, report, expected):
    """SOP-45-N01/N02：失败、error、跳过、缺失与包级事件，均不能误判通过。"""
    repo, presets = check_repo
    preset(presets, name, report=report, exit=1)
    board = declared_board(board_factory, repo)
    task = settle_check(board, start_check(board))
    assert task['status'] == '已完成', task
    round_ = next(r for r in check_rounds(board, task) if r['submitted_by'] == '回归检查 ' + name)
    for result, (status, parts) in zip(round_['results'], expected, strict=True):
        assert result['status'] == status
        assert all(p in result['actual'] for p in [*parts, '退出码 1'])


@pytest.mark.parametrize('name', ['junit', 'go'])
def test_passing_cases_survive_nonzero_exit(board_factory, check_repo, name):
    """SOP-45-P09：用例全过时退出码非零不改判，但必须注明。"""
    repo, presets = check_repo
    preset(presets, name, exit=3)
    board = declared_board(board_factory, repo)
    round_ = next(r for r in check_rounds(board, settle_check(board, start_check(board)))
                  if r['submitted_by'] == '回归检查 ' + name)
    assert all(r['status'] == 'pass' and '全部通过' in r['actual'] and '退出码 3' in r['actual'] for r in round_['results'])


@pytest.mark.parametrize('name,report,message', [
    ('junit', None, '报告缺失'), ('junit', 'not xml', '报告无法解析'),
    ('junit', '<unrelated/>', '报告无法解析'),
    ('go', 'not json', '报告无法解析'), ('go', '', '报告缺失'),
    ('go', '[]', '报告无法解析'),
])
def test_bad_reports_fail_scenes_and_preserve_other_checks(board_factory, check_repo, name, report, message):
    """SOP-45-N03：缺失或格式错误不影响其他检查，失败仍登记并附可用证据。"""
    repo, presets = check_repo
    preset(presets, name, report=report)
    board = declared_board(board_factory, repo)
    task = settle_check(board, start_check(board))
    rounds = check_rounds(board, task)
    assert len(rounds) == 2
    failed = next(r for r in rounds if r['submitted_by'] == '回归检查 ' + name)
    assert all(r['status'] == 'fail' and message in r['actual'] for r in failed['results'])
    assert all(r['status'] == 'pass' for r in next(r for r in rounds if r is not failed)['results'])
    assert next(c for c in task['result']['checks'] if c['name'] == name)['status'] == 'fail'
    assert all('output.txt' in {e['name'] for e in r['evidence']} for r in failed['results'])


def test_required_environment_blocks_without_launch_and_only_expands_allowlist(board_factory, check_repo):
    """SOP-45-N04：缺变量不启动；齐全时仅传声明变量与白名单，证据不记录值。"""
    repo, presets = check_repo
    value = declarations(repo)
    value['checks']['junit']['required_env'] = ['CHECK_FIXTURE_VALUE']
    save_declarations(repo, value)
    record_commit(repo)
    board = declared_board(board_factory, repo)
    task = settle_check(board, start_check(board))
    blocked = next(r for r in check_rounds(board, task) if r['submitted_by'] == '回归检查 junit')
    assert all(r['status'] == 'blocked' and r['finding'] == '受阻：缺少环境变量 CHECK_FIXTURE_VALUE' for r in blocked['results'])
    assert not (presets / 'junit-calls.jsonl').exists()
    board.stop()
    board.env.update(CHECK_FIXTURE_VALUE='synthetic-fixture', MY_SECRET='synthetic-unrelated', JIRA_API_TOKEN='synthetic-jira')
    board.start()
    result = settle_check(board, start_check(board))
    assert all(r['status'] == 'pass' for r in check_rounds(board, result)[0]['results'])
    invocation = json.loads((presets / 'junit-calls.jsonl').read_text())
    allowed = {'PATH', 'HOME', 'USER', 'LANG', 'TMPDIR', 'CODEX_HOME', 'SHELL', 'CHECK_FIXTURE_VALUE'}
    assert all(name in allowed or name.startswith('LC_') for name in invocation['env'])
    assert invocation['required_value_present']


def test_timeout_kills_parent_and_child_and_registers_failure(board_factory, check_repo):
    """SOP-45-N05：时限到达终止整个进程组并登记准确失败原因。"""
    from test_codex_tasks import process_alive
    import time
    repo, presets = check_repo
    value = declarations(repo)
    value['checks']['junit']['timeout'] = 2
    save_declarations(repo, value)
    record_commit(repo)
    preset(presets, 'junit', delay=60, child=True)
    board = declared_board(board_factory, repo)
    start = time.monotonic()
    task = settle_check(board, start_check(board))
    assert time.monotonic() - start < 8
    results = next(r for r in check_rounds(board, task) if r['submitted_by'] == '回归检查 junit')['results']
    assert all(r['status'] == 'fail' and r['actual'] == '回归检查 junit 超时（2 秒）' for r in results)
    assert not process_alive(int((presets / 'child.pid').read_text()))
    assert not process_alive(json.loads((presets / 'junit-calls.jsonl').read_text())['pid'])
    assert not list((board.data / 'worktrees').iterdir())


@pytest.mark.parametrize('name,report', [
    ('junit', '<testsuite><testcase classname="pkg.Order" name="test_create"><failure/></testcase>'
              '<testcase classname="pkg.Order" name="test_create"/><testcase classname="pkg.Order" name="test_cancel"/>'
              '<testcase classname="pkg.Order" name="test_refund"/></testsuite>'),
    ('go', '\n'.join(json.dumps(e) for e in [dict(Action=a, Package='example.com/app', Test=t)
            for a, t in [('fail', 'TestCreate'), ('pass', 'TestCreate'), ('pass', 'TestCancel')]])),
])
def test_duplicate_case_does_not_hide_failure(board_factory, check_repo, name, report):
    """SOP-45 I5：重复同名用例有失败时，后面的 pass 不能掩盖失败。"""
    repo, presets = check_repo
    preset(presets, name, report=report)
    board = declared_board(board_factory, repo)
    round_ = next(r for r in check_rounds(board, settle_check(board, start_check(board)))
                  if r['submitted_by'] == '回归检查 ' + name)
    assert round_['results'][0]['status'] == 'fail'


def test_relative_cwd_and_report_are_read_before_worktree_cleanup(board_factory, check_repo):
    """SOP-45 I5：显式 cwd 相对检出目录，report 相对命令目录，清理后证据仍可读取。"""
    repo, presets = check_repo
    value = declarations(repo)
    check = value['checks']['junit']
    check.update(cwd='src', report='report.xml')
    check['argv'] = [('../' + arg if arg.startswith('scripts/') else
                      arg.replace('./scripts/check', '../scripts/check')) for arg in check['argv']]
    save_declarations(repo, value)
    record_commit(repo)
    preset(presets, 'junit', relative_report=True)
    board = declared_board(board_factory, repo)
    task = settle_check(board, start_check(board))
    round_ = next(r for r in check_rounds(board, task) if r['submitted_by'] == '回归检查 junit')
    assert all(r['status'] == 'pass' for r in round_['results'])
    report = next(e for e in round_['results'][0]['evidence'] if e['name'] == 'report.xml')
    assert board.request('GET', '/api/v1/evidence/' + report['sha256'], auth=False)[1].decode() == json.loads((presets / 'junit.json').read_text())['report']
    cwd = json.loads((presets / 'junit-calls.jsonl').read_text())['cwd']
    assert cwd == str(board.data.resolve() / 'worktrees' / str(task['id']) / 'src')
    assert not list((board.data / 'worktrees').iterdir())
