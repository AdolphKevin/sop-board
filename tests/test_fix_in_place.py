"""SOP-55：经看板 HTTP 入口、真实 Git、SQLite 和开发者运行环境核对原地替换。"""
import json
from pathlib import Path
import pytest

from fix_helpers import (fix_repo, fix_codex, fix_board, configure, fail, wait_fix,
                         all_invocations, wait_invocations, agent, git)
from helpers import BASE, IDS, KEY
from codex_helpers import ADMIN, read_task
from check_helpers import record_commit
from test_check_entries import declarations, save_declarations
import yaml
from in_place_helpers import prepare_in_place, runtime, trace
from jira_helpers import mock_jira
from test_writable_repos import repo, wait_for


def test_in_place_checks_merge_reload_then_http_retest_and_registers(board_factory, fix_codex, fix_repo, tmp_path):
    """SOP-55-P01/P04：真实运行环境读取已替换的代码，身份、顺序、环境与轮次持久化。"""
    control = tmp_path / 'runtime'
    prepare_in_place(fix_repo, control)
    configure(fix_codex, {}, retests=[dict(observe_http=True, expected_content='print(2)\n')])
    with runtime(fix_repo, control) as address, mock_jira() as jira:
        board, pushed = fix_board(board_factory, fix_codex, fix_repo, APP_URL=address, **jira.configured_env())
        before = git(fix_repo, 'rev-parse', 'HEAD')
        fail(board, pushed)
        task = wait_fix(board, '已合入')
        attempt = task['attempts'][0]
        assert task['attempt'] == 1 and attempt['outcome'] == '通过'
        merged = git(fix_repo, 'rev-parse', 'HEAD')
        assert merged == attempt['commit_id'] == task['merged_commit'] != before
        records = trace(control)
        assert [r['event'] for r in records] == ['check-start', 'check-end', 'reload-start', 'reload-end']
        assert all(r['cwd'] == task['worktree'] for r in records[:2])
        assert all(r['cwd'] == str(fix_repo) for r in records[2:])
        assert all(r['commit'] == merged for r in records)
        assert [r['baseline'] for r in records] == [before, before, merged, merged]
        calls = all_invocations(fix_codex)
        assert len(calls) == 2 and 'resume' not in calls[1]['argv']
        assert records[-1]['started'] <= calls[1]['started']
        for observed in [*calls, *records]:
            assert not any(name.startswith('JIRA_') or name in ('SOP_BOARD_TOKEN', 'SOP_BOARD_ADMIN_TOKEN') for name in observed['env'])
        assert 'APP_URL' in calls[1]['env']
        assert json.loads((control / 'requests.jsonl').read_text()) == dict(commit=merged, content='print(2)\n')
        state = board.request('GET', BASE, auth=False)[1]
        number = state['rounds'][-1]['number']
        registered = board.request('GET', BASE + f'/rounds/{number}', auth=False)[1]
        assert registered['product']['commit'] == merged
        assert registered['implementer'] == 'TH-1' and registered['submitted_by'] == 'TH-R1'
        assert [(r['scenario'], r['status'], r['note']) for r in registered['results']] == [
            (IDS[0], 'pass', '运行环境按 HEAD 记录，未经核实')]
        assert task['rounds'] == [dict(batch='demo', round=number)]
        assert board.sql('SELECT status,note FROM results WHERE round_id=(SELECT MAX(id) FROM rounds)') == [
            ('pass', '运行环境按 HEAD 记录，未经核实')]
        board.stop()
        board.start()
        assert len(board.request('GET', BASE, auth=False)[1]['rounds']) == len(state['rounds'])


@pytest.mark.parametrize('failure', ['reload', 'retest'])
def test_in_place_failed_attempt_keeps_code_and_restarts_from_latest_baseline(board_factory, fix_codex, fix_repo, tmp_path, failure):
    """SOP-55-N02/N03：替换后失败保留代码；下一次同名修复分支以最新基线为起点。"""
    control = tmp_path / 'runtime'
    prepare_in_place(fix_repo, control)
    (control / 'mode.json').write_text(json.dumps(dict(reload_delay=1, reload_fail=failure == 'reload')))
    configure(fix_codex, {}, {}, retests=[dict(observe_http=True, expected_content='print(999)\n', delay=1),
                                        dict(observe_http=True, expected_content='print(4)\n')]
              if failure == 'retest' else [dict(observe_http=True, expected_content='print(3)\n')])
    with runtime(fix_repo, control) as address:
        board, pushed = fix_board(board_factory, fix_codex, fix_repo, APP_URL=address)
        fail(board, pushed)
        task = wait_fix(board, '复测中')
        board.request('POST', '/api/v1/automation/pause', {}, headers=ADMIN)
        task = wait_for(lambda: (t if (t := read_task(board, task))['status'] == '排队中' and t['attempt'] == 1 else None))
        first = task['attempts'][0]
        assert first['outcome'] == '未通过'
        assert git(fix_repo, 'rev-parse', 'HEAD') == first['commit_id']
        if failure == 'reload':
            assert 'reload failed' in first['reason'] and '退出码 3' in first['reason']
            assert len(all_invocations(fix_codex)) == 1
            assert board.sql('SELECT COUNT(*) FROM rounds WHERE kind="agent"') == [(0,)]
        else:
            assert board.sql('SELECT status,note FROM results WHERE round_id=(SELECT MAX(id) FROM rounds)') == [
                ('fail', '运行环境按 HEAD 记录，未经核实')]
        (fix_repo / 'README.md').write_text('developer advanced baseline\n')
        latest = record_commit(fix_repo)
        (control / 'mode.json').write_text('{}')
        board.request('POST', '/api/v1/automation/resume', {}, headers=ADMIN)
        task = wait_fix(board, '已合入')
        second = task['attempts'][1]
        assert second['start_commit'] == latest
        assert task['fix_branch'] == f"sop-board/fix/{task['id']}"
        calls = all_invocations(fix_codex)
        repairs = [c for c in calls if '/validations/' not in c['argv'][c['argv'].index('-o') + 1]]
        assert repairs[1]['start_commit'] == latest and repairs[1]['cwd'] == task['worktree']
        assert repairs[1]['argv'][repairs[1]['argv'].index('resume') + 1] == 'TH-1'
        assert git(fix_repo, 'merge-base', '--is-ancestor', first['commit_id'], 'HEAD') == ''
        assert (fix_repo / 'README.md').read_text() == 'developer advanced baseline\n'
        assert len(task['rounds']) == (2 if failure == 'retest' else 1)


def test_in_place_three_failed_retests_keep_all_merged_commits_and_evidence(board_factory, fix_codex, fix_repo, tmp_path):
    """SOP-55-N04：三轮失败也写入批次，保留三个合入提交与可读取的证据。"""
    control = tmp_path / 'runtime'
    prepare_in_place(fix_repo, control)
    configure(fix_codex, {}, retests=[dict(observe_http=True, expected_content='print(999)\n')])
    with runtime(fix_repo, control) as address:
        board, pushed = fix_board(board_factory, fix_codex, fix_repo, APP_URL=address)
        before = git(fix_repo, 'rev-parse', 'HEAD')
        fail(board, pushed)
        task = wait_fix(board, '待开发者处理')
        commits = [a['commit_id'] for a in task['attempts']]
        assert task['attempt'] == 3 and [a['outcome'] for a in task['attempts']] == ['未通过'] * 3
        assert git(fix_repo, 'rev-list', '--reverse', before + '..HEAD').splitlines() == commits
        assert task['merged_commits'] == commits
        assert [a['merged_commit'] for a in task['attempts']] == commits
        assert task['merged_commit'] == commits[-1]
        assert [a['start_commit'] for a in task['attempts']] == [before, *commits[:2]]
        assert len(all_invocations(fix_codex)) == 6
        assert board.sql('SELECT status,note FROM results WHERE round_id IN (SELECT id FROM rounds WHERE kind="agent")') == [
            ('fail', '运行环境按 HEAD 记录，未经核实')] * 3
        page = board.request('GET', f'/p/{KEY}/fixes', auth=False)[1].decode()
        for attempt in task['attempts']:
            assert f"已合入 {task['baseline_branch']}：{attempt['merged_commit']}" in page
            assert attempt['validation']['evidence']
            for name in attempt['validation']['evidence']:
                relative = str(Path(name).relative_to(board.data / 'tasks' / str(task['id'])))
                url = task['files'][relative]
                assert url in page
                assert board.request('GET', url, auth=False)[0] == 200
        rounds = board.sql('SELECT * FROM rounds')
        board.stop()
        board.start()
        assert board.sql('SELECT * FROM rounds') == rounds
        assert git(fix_repo, 'rev-parse', 'HEAD') == commits[-1]


def test_in_place_without_reload_retests_running_http_service(board_factory, fix_codex, fix_repo, tmp_path):
    """SOP-55-P02：热更新入口没有 reload 时照常替换和直接登记。"""
    control = tmp_path / 'runtime'
    prepare_in_place(fix_repo, control, reload=False)
    configure(fix_codex, {}, retests=[dict(observe_http=True, expected_content='print(2)\n')])
    with runtime(fix_repo, control) as address:
        board, pushed = fix_board(board_factory, fix_codex, fix_repo, APP_URL=address)
        fail(board, pushed)
        task = wait_fix(board, '已合入')
        assert [r['event'] for r in trace(control)] == ['check-start', 'check-end']
        assert len(all_invocations(fix_codex)) == 2
        assert len(task['rounds']) == 1
        assert json.loads((control / 'requests.jsonl').read_text())['commit'] == task['merged_commit']


def test_in_place_check_failure_never_replaces_or_reloads_or_retests(board_factory, fix_codex, fix_repo, tmp_path):
    """SOP-55-N01：检查失败返回证据并续接，不改变开发者代码和批次结果。"""
    control = tmp_path / 'runtime'
    prepare_in_place(fix_repo, control)
    (control / 'mode.json').write_text('{"check_fail":true}')
    configure(fix_codex, {}, dict(delay=30), retests=[dict(observe_http=True, expected_content='print(2)\n')])
    with runtime(fix_repo, control) as address:
        board, pushed = fix_board(board_factory, fix_codex, fix_repo, APP_URL=address)
        before = git(fix_repo, 'rev-parse', 'HEAD')
        fail(board, pushed)
        task = wait_fix(board, '修复中')
        first = task['attempts'][0]
        assert first['outcome'] == '未通过' and 'guard' in first['reason'] and '失败' in first['reason']
        assert git(fix_repo, 'rev-parse', 'HEAD') == before
        assert (fix_repo / 'src/app.py').read_text() == 'print(1)\n'
        assert task['attempts'][1]['start_commit'] == before
        assert [r['event'] for r in trace(control)] == ['check-start', 'check-end']
        assert not (control / 'requests.jsonl').exists()
        assert board.sql('SELECT COUNT(*) FROM rounds WHERE kind="agent"') == [(0,)]
        calls = wait_invocations(fix_codex, 2)
        assert calls[1]['argv'][calls[1]['argv'].index('resume') + 1] == 'TH-1'
        assert any(name.endswith('/checks/guard/report.xml') for name in first['validation']['evidence'])


@pytest.mark.parametrize('other', ['checkout', 'start'])
def test_in_place_mixed_entries_all_retest_after_replacement(board_factory, fix_codex, fix_repo, tmp_path, other):
    """SOP-55-P03：混合入口在替换后统一复测，不启动独立候选。"""
    control = tmp_path / 'runtime'
    prepare_in_place(fix_repo, control)
    value = declarations(fix_repo)
    value['entries']['other'] = dict(value['entries']['自定义入口'], candidate=other,
        start=['should-never-run-a-candidate'])
    save_declarations(fix_repo, value)
    path = fix_repo / 'docs/sop/demo.yaml'
    document = yaml.safe_load(path.read_text())
    next(s for n in document['sops'] for s in n['scenarios'] if s['id'] == IDS[2])['verify'] = 'other'
    path.write_text(yaml.safe_dump(document, allow_unicode=True))
    record_commit(fix_repo)
    configure(fix_codex, {}, retests=[dict(observe_http=True, expected_content='print(2)\n')])
    with runtime(fix_repo, control) as address:
        board, pushed = fix_board(board_factory, fix_codex, fix_repo, paused=True, APP_URL=address)
        agent(board, pushed, ['pass', 'pass', 'fail', 'pass'])
        fail(board, pushed)
        board.request('POST', '/api/v1/automation/resume', {}, headers=ADMIN)
        task = wait_fix(board, '已合入')
        assert [r['event'] for r in trace(control)] == ['check-start', 'check-end', 'reload-start', 'reload-end']
        requests = [json.loads(line) for line in (control / 'requests.jsonl').read_text().splitlines()]
        assert requests == [dict(content='print(2)\n', commit=task['merged_commit'])] * 2
        assert len(all_invocations(fix_codex)) == 2
        result = board.request('GET', BASE + f"/rounds/{task['rounds'][0]['round']}", auth=False)[1]
        assert {r['scenario'] for r in result['results']} == {IDS[0], IDS[2]}
        assert all(r['status'] == 'pass' and r['note'] == '运行环境按 HEAD 记录，未经核实' for r in result['results'])


def test_in_place_handoff_does_not_change_whole_task_replacement_mode(board_factory, fix_codex, fix_repo, tmp_path):
    """I5、SOP-55-P03：原地入口的问题转开发者后，任务仍按原地替换处理产品问题。"""
    control = tmp_path / 'runtime'
    prepare_in_place(fix_repo, control)
    value = declarations(fix_repo)
    value['entries']['other'] = dict(value['entries']['自定义入口'], candidate='checkout')
    save_declarations(fix_repo, value)
    path = fix_repo / 'docs/sop/demo.yaml'
    document = yaml.safe_load(path.read_text())
    next(s for n in document['sops'] for s in n['scenarios'] if s['id'] == IDS[2])['verify'] = 'other'
    path.write_text(yaml.safe_dump(document, allow_unicode=True))
    record_commit(fix_repo)
    configure(fix_codex, dict(categories={IDS[0]: 'environment'}),
        retests=[dict(observe_http=True, expected_content='print(2)\n')])
    with runtime(fix_repo, control) as address:
        board, pushed = fix_board(board_factory, fix_codex, fix_repo, paused=True, APP_URL=address)
        agent(board, pushed, ['fail', 'pass', 'fail', 'pass'])
        board.request('POST', '/api/v1/automation/resume', {}, headers=ADMIN)
        wait_invocations(fix_codex, 2)
        task = wait_fix(board)
        assert git(fix_repo, 'rev-parse', 'HEAD') == task['attempts'][0]['commit_id']
        task = wait_for(lambda: (t if (t := read_task(board, task))['status'] == '已合入' else None))
        assert [r['event'] for r in trace(control)] == ['check-start', 'check-end', 'reload-start', 'reload-end']
        registered = board.request('GET', BASE + f"/rounds/{task['rounds'][0]['round']}", auth=False)[1]
        assert [r['scenario'] for r in registered['results']] == [IDS[2]]
        assert task['attention'][0]['scenario'] == IDS[0]


@pytest.mark.parametrize('check_fails', [False, True])
def test_in_place_advanced_baseline_reruns_checks_before_any_runtime_retest(board_factory, fix_codex, fix_repo, tmp_path, check_fails):
    """I5/I4、SOP-55-P01/N01：基线前进后只追加检查，通过后才替换、重载和复测。"""
    control = tmp_path / 'runtime'
    prepare_in_place(fix_repo, control)
    (control / 'mode.json').write_text('{"check_delay":1.5}')
    configure(fix_codex, {}, dict(delay=30), retests=[dict(observe_http=True, expected_content='print(2)\n')])
    with runtime(fix_repo, control) as address:
        board, pushed = fix_board(board_factory, fix_codex, fix_repo, APP_URL=address)
        fail(board, pushed)
        wait_for(lambda: trace(control))
        (fix_repo / 'README.md').write_text('new developer commit\n')
        advanced = record_commit(fix_repo)
        (control / 'mode.json').write_text(json.dumps(dict(check_fail=check_fails)))
        task = wait_fix(board, '修复中' if check_fails else '已合入')
        records = trace(control)
        assert [r['event'] for r in records[:4]] == ['check-start', 'check-end'] * 2
        assert records[0]['commit'] != records[2]['commit']
        parents = git(task['worktree'], 'rev-list', '--parents', '-1', records[2]['commit']).split()
        assert parents[1:] == [task['attempts'][0]['commit_id'], advanced]
        calls = all_invocations(fix_codex)
        if check_fails:
            assert '追加验证未通过' in task['attempts'][0]['reason']
            assert git(fix_repo, 'rev-parse', 'HEAD') == advanced
            assert len(records) == 4 and not (control / 'requests.jsonl').exists()
            assert not any('/validations/' in c['argv'][c['argv'].index('-o') + 1] for c in calls)
        else:
            assert task['attempt'] == 1 and task['merged_commit'] == records[2]['commit']
            assert len(calls) == 2 and calls[1]['started'] >= records[-1]['started']
            assert json.loads((control / 'requests.jsonl').read_text())['commit'] == records[2]['commit']


@pytest.mark.parametrize('dirty', ['product', 'unrelated'])
def test_in_place_merge_preserves_uncommitted_work_and_records_head_product(board_factory, fix_codex, fix_repo, tmp_path, dirty):
    """I5/I4、SOP-55-P01：替换前保护工作区，运行环境按提交记录产品。"""
    control = tmp_path / 'runtime'
    prepare_in_place(fix_repo, control)
    (control / 'mode.json').write_text('{"check_delay":1}')
    configure(fix_codex, {}, retests=[dict(observe_http=True, expected_content='print(2)\n')])
    with runtime(fix_repo, control) as address:
        board, pushed = fix_board(board_factory, fix_codex, fix_repo, APP_URL=address)
        before = git(fix_repo, 'rev-parse', 'HEAD')
        fail(board, pushed)
        wait_for(lambda: trace(control))
        path = fix_repo / ('src/app.py' if dirty == 'product' else 'README.md')
        path.write_text('developer unsaved\n')
        task = wait_fix(board, '待开发者合入' if dirty == 'product' else '已合入')
        assert path.read_text() == 'developer unsaved\n'
        if dirty == 'product':
            assert git(fix_repo, 'rev-parse', 'HEAD') == before
            assert '未提交修改拒绝快进' in task['error']
            assert len(trace(control)) == 2 and not (control / 'requests.jsonl').exists()
            assert len(all_invocations(fix_codex)) == 1
        else:
            assert task['error'] == '工作区含未经验收的修改'
            registered = board.request('GET', BASE + f"/rounds/{task['rounds'][0]['round']}", auth=False)[1]
            assert registered['product']['commit'] == task['merged_commit']
            assert registered['product']['digest'] == task['candidates'][0]['product_digest']
