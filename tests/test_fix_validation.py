"""SOP-54/56：真实 HTTP、SQLite、Git 与隔离执行器验证修复闭环。"""
import json
from pathlib import Path
import yaml
import pytest

from codex_helpers import ADMIN, read_task
from fix_helpers import (fix_repo, fix_codex, fix_board, fail, agent, wait_fix,
                         wait_state, configure, all_invocations as invocations, wait_invocations, git)
from helpers import BASE, IDS, push_body
from test_writable_repos import repo, wait_for
from check_helpers import check_repo, record_commit, preset, declarations, save_declarations


def test_fix_failed_retest_is_candidate_only_and_next_attempt_resumes(board_factory, fix_codex, fix_repo):
    """SOP-54-P01/N01：只复测产品问题，新会话失败不改变基线和批次。"""
    configure(fix_codex, dict(categories={IDS[1]: 'environment'}), dict(delay=10),
              retests=[dict(statuses={IDS[0]: 'fail'}, delay=.5)], invocation_delay=.3)
    board, pushed = fix_board(board_factory, fix_codex, fix_repo, paused=True)
    agent(board, pushed, ['fail', 'fail', 'pass', 'pass'])
    before = board.sql('SELECT * FROM rounds'), board.request('GET', BASE, auth=False)[1]['statistics']['agent_pass_rate']
    baseline = git(fix_repo, 'rev-parse', 'HEAD')
    board.request('POST', '/api/v1/automation/resume', {}, headers=ADMIN)
    task = wait_fix(board, '复测中')
    task = wait_state(board, task, '修复中')
    attempt = task['attempts'][0]
    assert attempt['outcome'] == '未通过' and attempt['validation']['status'] == 'fail'
    assert attempt['validation']['scenarios'] == [dict(batch='demo', scenario=IDS[0])]
    assert task['candidates'][0]['results'][0]['status'] == 'fail'
    assert task['candidates'][0]['commit_id'] == attempt['commit_id']
    assert not task['candidates'][0]['promoted_round']
    calls = wait_invocations(fix_codex, 3)
    assert calls[1]['argv'][0] == 'exec' and 'resume' not in calls[1]['argv']
    retest_directory = Path(calls[1]['argv'][calls[1]['argv'].index('-o') + 1]).parent
    assert calls[1]['cwd'] == str(retest_directory)
    assert json.loads((retest_directory / 'entries.json').read_text())['targets']['自定义入口']['directory'] == task['worktree']
    assert calls[2]['argv'][calls[2]['argv'].index('resume') + 1] == 'TH-1'
    assert git(fix_repo, 'rev-parse', 'HEAD') == baseline
    assert before == (board.sql('SELECT * FROM rounds'), board.request('GET', BASE, auth=False)[1]['statistics']['agent_pass_rate'])


def test_fix_commit_hook_failure_counts_and_returns_output(board_factory, fix_codex, fix_repo):
    """SOP-53-P01/I3：正常执行仓库钩子，失败计次且输出交回同一会话。"""
    hook = fix_repo / '.git/hooks/pre-commit'
    hook.write_text('#!/bin/sh\necho "hook rejected fixture" >&2\nexit 1\n')
    hook.chmod(0o755)
    configure(fix_codex, {}, dict(delay=10), invocation_delay=.3)
    board, pushed = fix_board(board_factory, fix_codex, fix_repo)
    before = git(fix_repo, 'rev-parse', 'HEAD')
    fail(board, pushed)
    task = wait_fix(board, '修复中')
    attempt = task['attempts'][0]
    assert attempt['outcome'] == '未通过'
    assert attempt['reason'] == '仓库提交钩子未通过'
    assert 'hook rejected fixture' in json.dumps(attempt['validation'])
    assert not attempt['commit_id'] and task['attempt'] == 1
    feedback = json.loads((board.data / 'tasks' / str(task['id']) / 'feedback.json').read_text())
    assert 'hook rejected fixture' in json.dumps(feedback['previous'], ensure_ascii=False)
    assert 'resume' in wait_invocations(fix_codex, 2)[1]['argv']
    assert git(fix_repo, 'rev-parse', 'HEAD') == before
    assert git(fix_repo, 'status', '--porcelain') == ''


def test_fix_runs_all_guarded_checks_and_returns_failure_evidence(board_factory, fix_codex, check_repo):
    """SOP-54-P01/N02：问题通过仍须所有守护检查通过，跨批次检查只跑一次。"""
    root, presets = check_repo
    value = declarations(root)
    value['entries']['自定义入口']['candidate'] = 'checkout'
    save_declarations(root, value)
    git(root, 'config', 'user.name', 'Dev One')
    git(root, 'config', 'user.email', 'dev1@example.invalid')
    record_commit(root)
    preset(presets, 'junit', report='<testsuite><testcase classname="pkg.Order" name="test_create"><failure/></testcase>'
        '<testcase classname="pkg.Order" name="test_cancel"/><testcase classname="pkg.Order" name="test_refund"/></testsuite>')
    configure(fix_codex, {}, dict(delay=10), retests=[{}])
    board, pushed = fix_board(board_factory, fix_codex, root)
    before = git(root, 'rev-parse', 'HEAD')
    fail(board, pushed, IDS[3])
    task = wait_fix(board, '修复中')
    attempt = task['attempts'][0]
    assert attempt['outcome'] == '未通过'
    assert 'junit' in attempt['reason'] and '失败' in attempt['reason']
    rows = task['candidates'][0]['results']
    assert {r['scenario']: r['status'] for r in rows} == {IDS[3]: 'pass', IDS[0]: 'fail', IDS[1]: 'pass', IDS[2]: 'pass'}
    for name in ('junit', 'go'):
        calls = [json.loads(line) for line in (presets / (name + '-calls.jsonl')).read_text().splitlines()]
        assert len(calls) == 1
        assert calls[0]['cwd'] == task['worktree']
        assert calls[0]['commit'] == attempt['commit_id']
    assert git(root, 'rev-parse', 'HEAD') == before
    feedback = json.loads((board.data / 'tasks' / str(task['id']) / 'feedback.json').read_text())
    assert any(p.endswith('/checks/junit/report.xml') for p in feedback['previous']['validation']['evidence'])
    assert board.sql('SELECT COUNT(*) FROM rounds WHERE kind="agent"') == [(0,)]


def test_fix_fast_forwards_checked_out_baseline_and_records_merge_state(board_factory, fix_codex, fix_repo):
    """SOP-54-P02：真实快进更新分支和文件，待合入可观察，不生成合并提交。"""
    hook = fix_repo / '.git/hooks/post-merge'
    hook.write_text('#!/bin/sh\nsleep 1\n')
    hook.chmod(0o755)
    configure(fix_codex, {}, retests=[dict(delay=.3)])
    board, pushed = fix_board(board_factory, fix_codex, fix_repo)
    before = git(fix_repo, 'rev-parse', 'HEAD')
    fail(board, pushed)
    task = wait_fix(board, '待合入')
    task = wait_state(board, task, '已合入')
    candidate = task['candidates'][-1]['commit_id']
    assert task['attempt'] == 1 and task['attempts'][0]['outcome'] == '通过'
    assert git(fix_repo, 'rev-parse', 'HEAD') == candidate == task['merged_commit']
    assert git(fix_repo, 'rev-list', '--parents', '-1', candidate).split() == [candidate, before]
    assert (fix_repo / 'src/app.py').read_text() == 'print(2)\n'
    assert git(fix_repo, 'status', '--porcelain') == ''
    assert f"merge {task['fix_branch']}: Fast-forward" in git(fix_repo, 'reflog', '-1', '--format=%gs', task['baseline_branch'])


def test_fix_promotes_only_last_passing_attempt_for_each_batch_and_is_idempotent(board_factory, fix_codex, fix_repo):
    """SOP-56-P01/P02：各批一轮、保留身份与时间、更新产品、重启不重复。"""
    configure(fix_codex, {}, {}, retests=[dict(statuses={IDS[0]: 'fail'}), {}])
    board, pushed = fix_board(board_factory, fix_codex, fix_repo, paused=True)
    agent(board, pushed, ['pass'] * 4)
    fail(board, pushed)
    doc = yaml.safe_load(pushed['content'])
    doc['metadata']['batch'] = 'b2'
    body = push_body(doc, path='docs/sop/b2.yaml')
    other = BASE.replace('/demo', '/b2')
    assert board.request('PUT', other + '/scenario-file', body)[0] == 200
    agent(board, body, ['fail', 'pass', 'pass', 'pass'], base=other, key='b2-fail')
    board.request('POST', '/api/v1/automation/resume', {}, headers=ADMIN)
    task = wait_fix(board, '已合入')
    assert task['attempt'] == 2
    assert [a['outcome'] for a in task['attempts']] == ['未通过', '通过']
    for name, base, number in [('demo', BASE, 3), ('b2', other, 2)]:
        state = board.request('GET', base, auth=False)[1]
        latest = state['rounds'][-1]
        assert latest['number'] == number
        assert latest['implementer'] == 'TH-1' and latest['submitted_by'] == 'TH-R2'
        assert latest['product_commit'] == git(fix_repo, 'rev-parse', 'HEAD')
        assert latest['product_digest'] == state['product_digest']
        assert latest['product_dirty'] == 0
        promoted = board.request('GET', base + '/rounds/' + str(number), auth=False)[1]
        candidate = next(c for c in task['candidates'] if c['attempt'] == 2 and c['batch'] == name)
        assert candidate['promoted_round'] == number
        assert [(r['scenario'], r['executed_at'], r['executor']) for r in promoted['results']] == [
            (IDS[0], candidate['results'][0]['executed_at'], 'TH-R2')]
        assert IDS[0] in state['queues']['retest']
        assert IDS[3] in state['queues']['pending']
    rounds = board.sql('SELECT * FROM rounds')
    before = [board.request('GET', base, auth=False)[1]['statistics'] for base in (BASE, other)]
    board.stop()
    board.start()
    assert board.sql('SELECT * FROM rounds') == rounds
    assert [board.request('GET', base, auth=False)[1]['statistics'] for base in (BASE, other)] == before
    assert read_task(board, task)['status'] == '已合入'


def during_retest(board, fake):
    task = wait_fix(board, '复测中')
    wait_for(lambda: any('/validations/' in call['argv'][call['argv'].index('-o') + 1] for call in invocations(fake)))
    return task


def test_fix_advanced_baseline_merges_and_revalidates_same_attempt(board_factory, fix_codex, fix_repo):
    """SOP-54-P03：无冲突前进后合并并追加一次复测，保留开发者提交。"""
    configure(fix_codex, {}, retests=[dict(delay=2), {}])
    board, pushed = fix_board(board_factory, fix_codex, fix_repo)
    fail(board, pushed)
    task = during_retest(board, fix_codex)
    (fix_repo / 'README.md').write_text('developer commit C1\n')
    advanced = record_commit(fix_repo)
    task = wait_state(board, task, '已合入')
    assert task['attempt'] == 1
    assert len(task['candidates']) == 2
    original, merged = task['candidates']
    assert original['commit_id'] != merged['commit_id']
    assert git(fix_repo, 'rev-list', '--parents', '-1', merged['commit_id']).split() == [merged['commit_id'], original['commit_id'], advanced]
    assert git(fix_repo, 'rev-parse', 'HEAD') == merged['commit_id']
    assert (fix_repo / 'README.md').read_text() == 'developer commit C1\n'
    assert (fix_repo / 'src/app.py').read_text() == 'print(2)\n'
    assert git(fix_repo, 'status', '--porcelain') == ''
    assert [c['session_id'] for c in task['candidates']] == ['TH-R1', 'TH-R2']
    assert not original['promoted_round'] and merged['promoted_round'] == 2
    rounds = board.sql('SELECT * FROM rounds')
    completed = task['finished_at'], task['error'], task['rounds']
    board.stop()
    board.start()
    recovered = read_task(board, task)
    assert board.sql('SELECT * FROM rounds') == rounds
    assert (recovered['finished_at'], recovered['error'], recovered['rounds']) == completed


def test_fix_baseline_conflict_aborts_private_merge_without_touching_baseline(board_factory, fix_codex, fix_repo):
    """SOP-54-N04：冲突仅中止私有工作目录的合并，不改基线。"""
    configure(fix_codex, {}, retests=[dict(delay=2)])
    board, pushed = fix_board(board_factory, fix_codex, fix_repo)
    fail(board, pushed)
    task = during_retest(board, fix_codex)
    (fix_repo / 'src/app.py').write_text('print("developer C2")\n')
    advanced = record_commit(fix_repo)
    task = wait_state(board, task, '待开发者合入')
    assert '合入基线时冲突' in task['error']
    assert git(fix_repo, 'rev-parse', 'HEAD') == advanced
    assert (fix_repo / 'src/app.py').read_text() == 'print("developer C2")\n'
    assert git(task['worktree'], 'status', '--porcelain') == ''
    assert not Path(git(task['worktree'], 'rev-parse', '--git-path', 'MERGE_HEAD')).exists()
    assert git(task['worktree'], 'rev-parse', 'HEAD') == task['attempts'][0]['commit_id']
    assert board.sql('SELECT COUNT(*) FROM rounds WHERE kind="agent"') == [(0,)]


def test_fix_failed_additional_validation_counts_once_and_returns_merge_evidence(board_factory, fix_codex, fix_repo):
    """SOP-54-N05：追加验证失败交回同一修复会话，基线只保留 C1。"""
    configure(fix_codex, {}, dict(delay=10), retests=[dict(delay=2), dict(statuses={IDS[0]: 'fail'})])
    board, pushed = fix_board(board_factory, fix_codex, fix_repo)
    fail(board, pushed)
    task = during_retest(board, fix_codex)
    (fix_repo / 'README.md').write_text('C1\n')
    advanced = record_commit(fix_repo)
    task = wait_state(board, task, '修复中')
    attempt = task['attempts'][0]
    assert attempt['outcome'] == '未通过' and task['attempt'] == 1
    assert '合入基线后的追加验证未通过' in attempt['reason']
    assert '合入基线后的追加验证未通过' in attempt['validation']['reason']
    assert [c['status'] for c in task['candidates']] == ['pass', 'fail']
    assert all(c['promoted_round'] is None for c in task['candidates'])
    assert git(fix_repo, 'rev-parse', 'HEAD') == advanced
    assert git(fix_repo, 'status', '--porcelain') == ''
    calls = wait_invocations(fix_codex, 4)
    assert 'resume' in calls[3]['argv'] and calls[3]['argv'][calls[3]['argv'].index('resume') + 1] == 'TH-1'
    assert board.sql('SELECT COUNT(*) FROM rounds WHERE kind="agent"') == [(0,)]


@pytest.mark.parametrize('case', ['clean', 'conflicting-dirty', 'staged-dirty', 'other-dirty', 'other-worktree', 'switched'])
def test_fix_merge_preserves_developer_work_and_remote_refs(board_factory, fix_codex, fix_repo, tmp_path, case):
    """SOP-54-P02/P04/P05/N06/N07/N08：逐情形核对文件、索引、分支、reflog 与远端。"""
    baseline = git(fix_repo, 'branch', '--show-current')
    original = git(fix_repo, 'rev-parse', 'HEAD')
    git(fix_repo, 'branch', 'feature-y')
    remote = tmp_path / 'origin.git'
    git(tmp_path, 'init', '--bare', str(remote))
    git(remote, 'fetch', str(fix_repo), f'{baseline}:refs/heads/{baseline}', 'feature-y:refs/heads/feature-y')
    git(fix_repo, 'remote', 'add', 'origin', str(remote))
    refs = git(remote, 'for-each-ref', '--format=%(refname) %(objectname)')
    feature_log = git(fix_repo, 'reflog', 'feature-y')
    configure(fix_codex, {}, retests=[dict(delay=2)])
    board, pushed = fix_board(board_factory, fix_codex, fix_repo)
    fail(board, pushed)
    task = during_retest(board, fix_codex)
    target = fix_repo
    if case in ('conflicting-dirty', 'staged-dirty'):
        (fix_repo / 'src/app.py').write_text('developer unsaved\n')
        if case == 'staged-dirty':
            git(fix_repo, 'add', 'src/app.py')
    elif case == 'other-dirty':
        (fix_repo / 'README.md').write_text('developer unsaved\n')
    elif case in ('other-worktree', 'switched'):
        # 临时夹具两分支原本指向相同提交；只改 HEAD，避免运行分支切换命令。
        (fix_repo / '.git/HEAD').write_text('ref: refs/heads/feature-y\n')
        if case == 'other-worktree':
            target = tmp_path / 'main-wt'
            git(fix_repo, 'worktree', 'add', str(target), baseline)
    state = git(fix_repo, 'status', '--porcelain=v1'), git(fix_repo, 'diff', '--binary'), git(fix_repo, 'diff', '--cached', '--binary')
    content = (fix_repo / 'src/app.py').read_bytes(), (fix_repo / 'README.md').read_bytes()
    expected = '待开发者合入' if case in ('conflicting-dirty', 'staged-dirty', 'switched') else '已合入'
    task = wait_state(board, task, expected)
    assert git(fix_repo, 'stash', 'list') == ''
    assert git(remote, 'for-each-ref', '--format=%(refname) %(objectname)') == refs
    assert git(fix_repo, 'rev-parse', 'feature-y') == original
    assert git(fix_repo, 'reflog', 'feature-y') == feature_log
    reflog = git(fix_repo, 'reflog', '--format=%gs', baseline)
    assert 'reset' not in reflog.lower() and 'forced-update' not in reflog.lower()
    if expected == '待开发者合入':
        assert git(fix_repo, 'rev-parse', baseline) == original
        assert state == (git(fix_repo, 'status', '--porcelain=v1'), git(fix_repo, 'diff', '--binary'), git(fix_repo, 'diff', '--cached', '--binary'))
        assert content == ((fix_repo / 'src/app.py').read_bytes(), (fix_repo / 'README.md').read_bytes())
        assert ('feature-y' if case == 'switched' else 'Git 因未提交修改拒绝快进') in task['error']
        assert not task['rounds']
    else:
        assert git(target, 'rev-parse', 'HEAD') == task['candidates'][-1]['commit_id']
        assert f"merge {task['fix_branch']}: Fast-forward" in reflog
        assert (target / 'src/app.py').read_text() == 'print(2)\n'
        if case == 'other-dirty':
            assert task['error'] == '工作区含未经验收的修改'
            assert (fix_repo / 'README.md').read_bytes() == content[1]
            assert git(fix_repo, 'diff', '--cached') == state[2] == ''
        else:
            assert git(target, 'status', '--porcelain') == ''
        if case == 'other-worktree':
            assert git(fix_repo, 'branch', '--show-current') == 'feature-y'
            assert state == (git(fix_repo, 'status', '--porcelain=v1'), git(fix_repo, 'diff', '--binary'), git(fix_repo, 'diff', '--cached', '--binary'))
            assert content == ((fix_repo / 'src/app.py').read_bytes(), (fix_repo / 'README.md').read_bytes())


def test_fix_start_failure_skips_retest_and_returns_start_log(board_factory, fix_codex, fix_repo, tmp_path):
    """SOP-54-N03：候选启动失败计次，不开复测会话，保留退出码与日志。"""
    from candidate_helpers import start_entry
    mode = start_entry(fix_repo, tmp_path)
    mode.write_text(json.dumps(dict(mode='exit')))
    document = yaml.safe_load((fix_repo / 'docs/sop/demo.yaml').read_text())
    document['sops'][0]['scenarios'][0]['verify'] = 'api'
    (fix_repo / 'docs/sop/demo.yaml').write_text(yaml.safe_dump(document, allow_unicode=True))
    record_commit(fix_repo)
    configure(fix_codex, {}, dict(delay=10), retests=[{}])
    board, pushed = fix_board(board_factory, fix_codex, fix_repo)
    before = git(fix_repo, 'rev-parse', 'HEAD')
    fail(board, pushed)
    task = wait_fix(board, '修复中')
    assert len(task['attempts']) == 2 and task['attempts'][1]['number'] == 2
    first = task['attempts'][0]
    assert first['outcome'] == '未通过'
    assert '候选版本启动失败' in first['reason'] and '退出码 2' in first['reason'] and 'address in use' in first['reason']
    wait_for(lambda: len(invocations(fix_codex)) == 2)
    assert all('/validations/' not in c['argv'][c['argv'].index('-o') + 1] for c in invocations(fix_codex))
    assert any(p.endswith('/start.log') for p in first['validation']['evidence'])
    feedback = json.loads((board.data / 'tasks' / str(task['id']) / 'feedback.json').read_text())
    assert any(p.endswith('/start.log') for p in feedback['previous']['validation']['evidence'])
    assert git(fix_repo, 'rev-parse', 'HEAD') == before
    assert board.sql('SELECT COUNT(*) FROM rounds WHERE kind="agent"') == [(0,)]


def test_fix_restart_recovers_promotion_after_actual_fast_forward(board_factory, fix_codex, fix_repo, tmp_path):
    """SOP-56-N01/P02：真实快进后、提升前杀进程，重启只补一轮。"""
    marker, release = tmp_path / 'merged', tmp_path / 'release'
    hook = fix_repo / '.git/hooks/post-merge'
    hook.write_text(f'#!/bin/sh\necho merged > "{marker}"\nwhile [ ! -e "{release}" ]; do sleep 0.05; done\n')
    hook.chmod(0o755)
    configure(fix_codex, {}, retests=[{}])
    board, pushed = fix_board(board_factory, fix_codex, fix_repo)
    fail(board, pushed)
    try:
        wait_for(marker.exists)
        task = wait_fix(board, '待合入')
        candidate = task['candidates'][0]['commit_id']
        assert git(fix_repo, 'rev-parse', 'HEAD') == candidate
        assert board.sql('SELECT COUNT(*) FROM rounds WHERE kind="agent"') == [(0,)]
        board.process.kill()
        board.process.wait(timeout=5)
    finally:
        release.touch()
        board.stop()
    board.start()
    task = read_task(board, task)
    assert task['status'] == '已合入'
    assert task['candidates'][0]['promoted_round'] == 2
    assert board.sql('SELECT product_commit,implementer,submitted_by FROM rounds WHERE kind="agent"') == [(candidate, 'TH-1', 'TH-R1')]
    rows = board.sql('SELECT * FROM rounds')
    board.stop()
    board.start()
    assert board.sql('SELECT * FROM rounds') == rows
    assert len(invocations(fix_codex)) == 2


def test_fix_nonzero_check_command_uses_declared_cases_and_records_exit(board_factory, fix_codex, check_repo):
    """SOP-54-P01、SOP-45-P09/I5：声明用例全通过时仍通过，另记非零退出码。"""
    root, presets = check_repo
    value = declarations(root)
    value['entries']['自定义入口']['candidate'] = 'checkout'
    save_declarations(root, value)
    git(root, 'config', 'user.name', 'Dev One')
    git(root, 'config', 'user.email', 'dev1@example.invalid')
    record_commit(root)
    preset(presets, 'junit', exit=2)
    configure(fix_codex, {}, dict(delay=10), retests=[{}])
    board, pushed = fix_board(board_factory, fix_codex, root)
    before = git(root, 'rev-parse', 'HEAD')
    fail(board, pushed, IDS[3])
    from fix_helpers import fixes
    task = wait_for(lambda: next((t for t in fixes(board) if t['status'] in ('已合入', '修复中', '待开发者处理')), None))
    assert task['status'] == '已合入' and task['attempts'][0]['outcome'] == '通过'
    checked = [r for r in task['candidates'][0]['results'] if r.get('regression_check') == 'junit']
    assert checked and all(r['status'] == 'pass' and '退出码 2' in r['actual'] for r in checked)
    assert git(root, 'rev-parse', 'HEAD') != before


def test_fix_three_failed_candidates_only_change_fix_statistics(board_factory, fix_codex, fix_repo):
    """SOP-56-N02、SOP-54-N01：三次失败只计修复统计，不写批次轮次。"""
    configure(fix_codex, {}, retests=[dict(statuses={IDS[0]: 'fail'})])
    board, pushed = fix_board(board_factory, fix_codex, fix_repo, paused=True)
    agent(board, pushed, ['pass'] * 4)
    fail(board, pushed)
    before = board.sql('SELECT * FROM rounds'), board.request('GET', BASE, auth=False)[1]['statistics']
    baseline = git(fix_repo, 'rev-parse', 'HEAD')
    board.request('POST', '/api/v1/automation/resume', {}, headers=ADMIN)
    task = wait_fix(board, '待开发者处理')
    assert task['attempt'] == 3 and len(task['candidates']) == 3
    assert all(c['status'] == 'fail' and c['promoted_round'] is None for c in task['candidates'])
    assert before == (board.sql('SELECT * FROM rounds'), board.request('GET', BASE, auth=False)[1]['statistics'])
    assert git(fix_repo, 'rev-parse', 'HEAD') == baseline
    report = board.request('GET', BASE + '/report.md', auth=False)[1].decode()
    assert '自动修复统计' in report
    assert '总尝试次数：3' in report and '转开发者数：1' in report


def test_fix_promotes_guarded_results_across_batches_in_one_round_each(board_factory, fix_codex, check_repo):
    """SOP-54-P01、SOP-56-P01：全部批次共享检查运行，提升后保留守护来源。"""
    root, presets = check_repo
    value = declarations(root)
    value['entries']['自定义入口']['candidate'] = 'checkout'
    save_declarations(root, value)
    doc = yaml.safe_load((root / 'docs/sop/demo.yaml').read_text())
    doc['metadata']['batch'] = 'b2'
    (root / 'docs/sop/b2.yaml').write_text(yaml.safe_dump(doc, allow_unicode=True))
    git(root, 'config', 'user.name', 'Dev One')
    git(root, 'config', 'user.email', 'dev1@example.invalid')
    record_commit(root)
    configure(fix_codex, {}, retests=[dict(delay=1)])
    board, pushed = fix_board(board_factory, fix_codex, root, paused=True)
    assert board.sql("SELECT id FROM tasks WHERE kind='execute'") == []
    other = BASE.replace('/demo', '/b2')
    other_body = push_body(doc, path='docs/sop/b2.yaml')
    agent(board, other_body, ['pass'] * 4, base=other, key='b2-initial')
    fail(board, pushed, IDS[3])
    board.request('POST', '/api/v1/automation/resume', {}, headers=ADMIN)
    during_retest(board, fix_codex)
    board.request('POST', '/api/v1/automation/pause', {}, headers=ADMIN)
    task = wait_fix(board, '已合入')
    assert len(task['candidates']) == 2
    for name in ('junit', 'go'):
        calls = [json.loads(line) for line in (presets / (name + '-calls.jsonl')).read_text().splitlines()]
        assert len(calls) == 1 and calls[0]['cwd'] == task['worktree'], dict(calls=calls,
            candidates=[(c['batch'], c['attempt'], c['validation'], c['commit_id'], c['status']) for c in task['candidates']],
            tasks=board.sql('SELECT id,kind,status,batch,attempt FROM tasks'))
        assert calls[0]['commit'] == task['attempts'][0]['commit_id']
    for base, number in [(BASE, 2), (other, 2)]:
        state = board.request('GET', base, auth=False)[1]
        assert len([r for r in state['rounds'] if r['kind'] == 'agent']) == (1 if base == BASE else 2)
        assert state['rounds'][-1]['number'] == number
        scenes = board.request('GET', base + '/scenarios', auth=False)[1]['scenarios']
        assert all(s['regression_status'] == 'passed' for s in scenes[:3])


def test_fix_retest_writes_evidence_in_its_own_execution_workspace(board_factory, fix_codex, fix_repo):
    """SOP-54-P01：沿用执行运行器目录边界，证据可写且候选目录明确。"""
    configure(fix_codex, {}, retests=[{}])
    board, pushed = fix_board(board_factory, fix_codex, fix_repo)
    fail(board, pushed)
    task = wait_fix(board, '已合入')
    call = invocations(fix_codex)[1]
    directory = Path(call['argv'][call['argv'].index('-o') + 1]).parent
    assert call['cwd'] == str(directory)
    assert call['argv'][call['argv'].index('-C') + 1] == str(directory)
    assert json.loads((directory / 'entries.json').read_text())['targets']['自定义入口']['directory'] == task['worktree']


def test_fix_malformed_retest_is_counted_as_failed_validation(board_factory, fix_codex, fix_repo):
    """SOP-54-N01/I3：格式不合格的复测不能提升，按未通过交回原修复会话。"""
    configure(fix_codex, {}, dict(delay=10), retests=[dict(result_fields=dict(executed_at='invalid'))])
    board, pushed = fix_board(board_factory, fix_codex, fix_repo)
    baseline = git(fix_repo, 'rev-parse', 'HEAD')
    fail(board, pushed)
    from fix_helpers import fixes
    task = wait_for(lambda: next((t for t in fixes(board) if t['status'] in ('修复中', '待开发者处理', '已合入')), None))
    assert task['attempts'][0]['outcome'] == '未通过'
    assert '时间' in task['attempts'][0]['reason']
    assert task['candidates'][0]['status'] == 'fail'
    assert not task['candidates'][0]['promoted_round']
    assert git(fix_repo, 'rev-parse', 'HEAD') == baseline
    assert board.sql('SELECT COUNT(*) FROM rounds WHERE kind="agent"') == [(0,)]
    assert 'resume' in wait_invocations(fix_codex, 3)[2]['argv']


def test_fix_web_retest_shares_browser_with_execution_tasks(board_factory, fix_codex, fix_repo, tmp_path):
    """SOP-54-P01、SOP-44-P01：另一仓库占用浏览器时复测等待，释放后继续。"""
    from helpers import KEY
    from test_writable_repos import repo as repo_fixture
    from execution_helpers import start_execution
    identity = 'github.com/demo-org/second-app'
    key = 'github-com-demo-org-second-app'
    second_parent = tmp_path / 'second'
    second_parent.mkdir()
    second = repo_fixture.__wrapped__(second_parent)
    for root in (fix_repo, second):
        value = declarations(root)
        value['entries']['自定义入口'].update(surface='web', candidate='checkout')
        save_declarations(root, value)
        git(root, 'config', 'user.name', 'Dev One')
        git(root, 'config', 'user.email', 'dev1@example.invalid')
        record_commit(root)
    configure(fix_codex, dict(delay=20), {}, retests=[{}])
    board, _ = fix_board(board_factory, fix_codex, fix_repo, paused=True)
    content = (second / 'docs/sop/demo.yaml').read_text()
    body = push_body(yaml.safe_load(content), project=dict(id=identity, name='第二项目'),
                     content=content, path='docs/sop/demo.yaml')
    base = BASE.replace(KEY, key)
    assert board.request('PUT', base + '/scenario-file', body)[0] == 200
    board.stop()
    board.command.extend(['--writable-repo', f'{identity}={second}'])
    board.start()
    owner = start_execution(board)
    board.request('POST', '/api/v1/automation/resume', {}, headers=ADMIN)
    wait_invocations(fix_codex, 1)
    fail(board, body, base=base, defect='https://github.com/demo-org/second-app/issues/1')
    task = wait_for(lambda: next((t for t in board.request('GET', f'/api/v1/projects/{key}/fixes', auth=False)[1]['tasks']
        if t['status'] == '已合入' or t['error'] == '等待测试浏览器'), None))
    assert task['status'] == '复测中' and task['error'] == '等待测试浏览器'
    assert read_task(board, owner)['status'] == '运行中'
    assert len(invocations(fix_codex)) == 2
    assert not any('/validations/' in c['argv'][c['argv'].index('-o') + 1] for c in invocations(fix_codex))
    assert board.request('POST', f"/api/v1/tasks/{owner['id']}/cancel", {}, headers=ADMIN)[0] == 200
    task = wait_state(board, task, '已合入')
    assert len(wait_invocations(fix_codex, 3)) == 3
    assert task['candidates'][0]['session_id'] == 'TH-R1'
