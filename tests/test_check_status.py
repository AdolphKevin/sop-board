"""SOP-45 I5：场景的检查状态取真实检查结果与当前产品。"""
import pytest

from check_helpers import check_repo, declared_board, start_check, settle_check, preset, record_commit
from helpers import BASE, IDS, push_body, push, assert_error
from codex_helpers import ADMIN
from test_writable_repos import repo, wait_for


def statuses(board):
    return {s['scenario']: s['regression_status'] for s in board.request('GET', BASE + '/scenarios', auth=False)[1]['scenarios']}


def test_regression_status_uses_check_provenance_and_current_product(board_factory, check_repo):
    """SOP-45 I5：未运行 pending、通过 passed、普通 Agent 结果不冒充检查，产品变化后 pending。"""
    from helpers import round_body, upload
    repo, presets = check_repo
    board = declared_board(board_factory, repo)
    assert statuses(board) == {**dict.fromkeys(IDS[:3], 'pending'), IDS[3]: None}
    settle_check(board, start_check(board))
    assert statuses(board) == {**dict.fromkeys(IDS[:3], 'passed'), IDS[3]: None}
    page = board.request('GET', BASE.replace('/api/v1/projects/', '/p/').replace('/batches/', '/b/') + '/scenarios', auth=False)[1].decode()
    assert '检查通过' in page
    # 后来的普通 Agent fail 不应覆盖真实检查通过。
    summary = board.request('GET', BASE, auth=False)[1]
    body = round_body(push_body(), upload(board))
    body['scenario_file_sha256'] = summary['scenario_file_sha256']
    body['product']['digest'] = summary['product_digest']
    body['results'] = body['results'][:1]
    body['results'][0].update(status='fail', actual='普通场景检查失败', executor='回归检查 junit')
    assert board.request('POST', BASE + '/rounds', body)[0] == 200
    assert statuses(board)[IDS[0]] == 'passed'
    (repo / 'src/app.py').write_text('print(9)\n')
    record_commit(repo)
    wait_for(lambda: statuses(board)[IDS[0]] == 'pending', timeout=8)
    page = board.request('GET', f'/p/github-com-demo-org-demo-app/b/demo/scenarios', auth=False)[1].decode()
    assert '检查待运行' in page


@pytest.mark.parametrize('blocked', [False, True])
def test_regression_failed_includes_blocked(board_factory, check_repo, blocked):
    """SOP-45 I5 / N01/N04：失败或受阻均映射 failed。"""
    from check_helpers import declarations, save_declarations
    repo, presets = check_repo
    if blocked:
        value = declarations(repo)
        value['checks']['junit']['required_env'] = ['MISSING_FIXTURE_VALUE']
        save_declarations(repo, value)
        record_commit(repo)
    else:
        preset(presets, 'junit', report='<testsuite/>')
    board = declared_board(board_factory, repo)
    settle_check(board, start_check(board))
    assert statuses(board) == {IDS[0]: 'failed', IDS[1]: 'failed', IDS[2]: 'passed', IDS[3]: None}
    assert '检查失败' in board.request('GET', f'/p/github-com-demo-org-demo-app/b/demo/scenarios', auth=False)[1].decode()


def test_checks_developer_auth_and_empty_scope_boundaries(board_factory, repo):
    """SOP-45-N06 / SOP-41-N06：令牌隔离、缺项目/批次、无回归和未授权都不建任务。"""
    board = declared_board(board_factory, repo)
    assert_error(board.request('POST', BASE + '/checks', {}, auth=False), 401, 'unauthorized')
    assert_error(board.request('POST', BASE + '/checks', {}), 403, 'admin-required')
    assert_error(board.request('POST', BASE + '/checks', {}, headers=ADMIN), 409, 'nothing-to-check')
    assert_error(board.request('POST', '/api/v1/projects/absent/batches/demo/checks', {}, headers=ADMIN), 404, 'project-not-found')
    assert_error(board.request('POST', BASE.replace('/demo', '/absent') + '/checks', {}, headers=ADMIN), 404, 'batch-not-found')
    assert board.sql('SELECT COUNT(*) FROM tasks') == [(0,)]
    readonly = board_factory(env={'SOP_BOARD_ADMIN_TOKEN': 'isolated-admin'})
    push(readonly)
    assert_error(readonly.request('POST', BASE + '/checks', {}, headers=ADMIN), 409, 'repo-not-writable')
