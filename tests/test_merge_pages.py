"""I4：合并页选择任务、读取详情、下载和 Markdown 报告的合并记录。"""
import json
import re

from codex_helpers import fake_codex, mode, task_board, create_task
from helpers import BASE, KEY, demo, push, push_body
from merge_helpers import answer, merge_cases, upload_merge, settled, checked


WEB = f'/p/{KEY}/b/demo/merges'


def test_merge_page_selects_relationships_dispositions_inferences_and_gaps(board_factory, fake_codex):
    """SOP-33-P12/N06/N07：选定任务显示详情；缺口与失败不混用；本地时间、下载与按钮。"""
    document = merge_cases()
    document['test_cases'][2].pop('preconditions')
    result = answer(document, demo())
    result['scenarios'][0] = dict(id='N1-P01', action='duplicate', target='TC_DEMO_LOGIN_001',
        reason='登录流程完全覆盖 <script>alert(1)</script>', forbidden_effects=[], transfer_regression=False)
    result['relocate'] = {'N1-N01': 'S01'}
    mode(fake_codex, final=json.dumps(result, ensure_ascii=False))
    board = task_board(board_factory, fake_codex)
    push(board)
    first = settled(board, upload_merge(board, document))
    assert first['status'] == '已完成', checked(board, first)
    result['gaps'] = ['退款 没有正向依据']
    mode(fake_codex, final=json.dumps(result, ensure_ascii=False))
    second = settled(board, create_task(board))
    assert second['status'] == '待补充'
    assert second['gaps'] == ['退款 没有正向依据']
    mode(fake_codex, final='invalid JSON')
    failed = settled(board, create_task(board))
    assert failed['status'] == '失败'
    status, raw, _ = board.request('GET', WEB + '?task=' + str(first['id']), auth=False)
    assert status == 200
    text = raw.decode()
    for value in ('用例到场景', '原有场景的去向', '待确认的推断', 'TC_DEMO_LOGIN_001',
                  'N1-P01', '重复', 'N2-P01', '保留', '访客未登录', '重新合并'):
        assert value in text
    assert '<script>alert(1)</script>' not in text and '&lt;script&gt;' in text
    assert re.search(r'\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} [+-]\d{2}:\d{2}', text)
    assert f'?task={second["id"]}' in text and f'?task={failed["id"]}' in text
    assert '/scenario-file' in text and '/pairs.json' in text
    assert '>SOP</a>' in text
    second_page = board.request('GET', WEB + '?task=' + str(second['id']), auth=False)[1].decode()
    assert '退款 没有正向依据' in second_page and '放弃' in second_page
    failed_page = board.request('GET', WEB + '?task=' + str(failed['id']), auth=False)[1].decode()
    assert '重试' in failed_page and 'check.json' in failed_page and 'final.json' in failed_page
    assert 'events.jsonl' in failed_page and 'stderr.txt' in failed_page and 'JSON' in failed_page
    report = board.request('GET', BASE + '/report.md', auth=False)[1].decode().split('## 合并记录\n', 1)[1].split('## 未验证范围', 1)[0]
    assert 'N1-P01' in report and 'TC_DEMO_LOGIN_001' in report and '重复' in report
    assert '访客未登录' in report and 'TC_DEMO_ORDER_001' in report
    assert '退款' not in report and '暂无记录' not in report


def test_no_cases_no_task_no_merge_record(board_factory, fake_codex):
    """SOP-33-N05：无用例不自动合并，手动返回 409，报告暂无记录。"""
    from helpers import assert_error
    board = task_board(board_factory, fake_codex)
    original = push_body()
    push(board, original)
    push(board, dict(original, content=original['content'] + '# 改版\n'))
    assert board.request('GET', '/api/v1/tasks', auth=False)[1]['tasks'] == []
    assert_error(board.request('POST', BASE + '/merges', {}, headers={'Authorization': 'Bearer isolated-admin'}), 409, 'cases-missing')
    assert '## 合并记录\n\n暂无记录' in board.request('GET', BASE + '/report.md', auth=False)[1].decode()
