"""增量只补省略的判断；业务核对与场景组装仍由原合并链路负责。"""
from copy import deepcopy
import json

import yaml

from .drafts import section


def baseline_inputs(snapshot):
    return dict(cases=snapshot.get('all_cases', snapshot['cases']),
                confirmed_draft_cases=snapshot.get('confirmed_draft_cases', {}),
                config_digest=snapshot['config_digest'], entries=snapshot['entries'],
                forced_flow=snapshot['forced_flow'], excluded_cases=snapshot.get('excluded_cases', []),
                sop=[dict(path=s['path'], sha256=s['sha256']) for s in snapshot['sop_sources']],
                sop_upload_id=snapshot.get('sop_upload_id'))


def summary(snapshot, *, carried_count=None):
    return dict(mode=snapshot['mode'], mode_reason=snapshot['mode_reason'], baseline=snapshot['baseline'],
                changed_count=len(snapshot['changed']),
                carried_count=len(snapshot['carried'].get('cases', [])) if carried_count is None else carried_count)


def freeze(tasks, db, batch, snapshot, *, full_reason=None):
    """只使用有落地收据且仍对应当前场景内容的最近任务，不回退到更早基线。"""
    result = dict(mode='全量', mode_reason='', baseline=None, changed=[], carried={})

    def full(reason):
        return dict(result, mode_reason=reason)

    if full_reason:
        return full(full_reason)
    row = db.execute("SELECT t.* FROM tasks t WHERE t.workspace=? AND t.batch=? AND t.kind='merge' "
        "AND EXISTS (SELECT 1 FROM task_receipts r WHERE r.task=t.id AND r.attempt=t.attempt AND r.phase='merge-land') "
        'ORDER BY t.id DESC LIMIT 1', (batch['workspace'], batch['name'])).fetchone()
    if row is None:
        return full('没有可沿用的上一轮合并')
    baseline = dict(row)
    result['baseline'] = baseline['id']
    directory = tasks.directory(baseline['id'])
    try:
        old_snapshot = json.loads(baseline['input_json'])
        path = directory / 'judgment.json'
        if path.is_file():
            judgment = json.loads(path.read_text(encoding='utf-8'))
            before = judgment.pop('baseline_inputs')
        else:
            if old_snapshot.get('mode') == '增量':
                return full('上一轮增量合并的完整判断无法读取')
            # 格式 5 的旧版任务没有 judgment.json，但 final.json 是严格的全量判断。
            from .tasks import read_merge_output
            judgment = read_merge_output((directory / 'final.json').read_text(encoding='utf-8'))
            before = baseline_inputs(old_snapshot)
            cases = {c['id']: c for c in before['cases']}
            for update in db.execute('SELECT case_id,after_json FROM merge_case_updates WHERE task=?', (baseline['id'],)):
                cases[update['case_id']] = json.loads(update['after_json'])
                before['confirmed_draft_cases'].pop(update['case_id'], None)
            before['cases'] = list(cases.values())
        expected = {c['id'] for c in before['cases']} - set(before['excluded_cases'])
        if {c['id'] for c in judgment['cases']} != expected:
            return full('上一轮合并的完整判断无法读取')
        for key in ('nodes', 'negative_exempt', 'scenarios', 'gaps'):
            if not isinstance(judgment[key], list):
                return full('上一轮合并的完整判断无法读取')
    except (OSError, ValueError, KeyError, TypeError):
        return full('上一轮合并的完整判断无法读取')
    try:
        landed = (directory / 'scenario-file.yaml').read_text(encoding='utf-8')
        source = tasks.service.workspace.scenario_path(batch['name'])
        actual = source.read_text(encoding='utf-8') if source.exists() else ''
        if snapshot['scenarios'] != landed or actual != landed:
            return full('场景文件在上一轮合并之后有变化')
    except OSError:
        return full('上一轮合并的场景结果无法读取')
    if any(m['pending'] for m in snapshot['clarifications']['messages']):
        return full('有待处理的合并回复')
    current = baseline_inputs(snapshot)
    if json.dumps(before['entries'], sort_keys=True) != json.dumps(current['entries'], sort_keys=True):
        return full('入口声明有变化')
    if before['sop'] != current['sop'] or before['sop_upload_id'] != current['sop_upload_id']:
        return full('业务 SOP 原文有变化')
    if before['forced_flow'] != current['forced_flow']:
        return full('开发者的独立场景选择有变化')
    if before['excluded_cases'] != current['excluded_cases']:
        return full('本批不纳入的用例有变化')
    old = {c['id']: c for c in before['cases']}
    if old.keys() - {c['id'] for c in current['cases']}:
        return full('用例并集中有用例被移除')
    changed = [dict(case=c['id'], action='updated' if c['id'] in old else 'added', previous=old.get(c['id']))
               for c in snapshot['cases'] if old.get(c['id']) != c or
               before['confirmed_draft_cases'].get(c['id']) != current['confirmed_draft_cases'].get(c['id'])]
    if not changed:
        return full('没有新增或变化的用例')
    changed_ids = {c['case'] for c in changed}
    unchanged = {c['id'] for c in snapshot['cases']} - changed_ids
    original = yaml.safe_load(snapshot['scenarios']) or {}
    scenes = {s['id'] for n in original.get('sops', []) for s in n['scenarios']} - {c['id'] for c in snapshot['cases']}
    receipt = tasks.kernel.receipt(baseline, connection=db, phase='merge-land')
    carried = dict(cases=[c for c in judgment['cases'] if c['id'] in unchanged],
                   nodes=judgment['nodes'], negative_exempt=judgment['negative_exempt'],
                   scenarios=[s for s in judgment['scenarios'] if s['action'] == 'kept' and s['id'] in scenes],
                   gaps=[g for g in receipt['check'].get('gaps', []) if g['cases'] and set(g['cases']) <= unchanged])
    return dict(result, mode='增量', mode_reason='只有用例新增或变化，其余合并输入未变化', changed=changed, carried=carried)


def complete(snapshot, output):
    changed = {c['case'] for c in snapshot['changed']}
    missing = changed - {c['id'] for c in output['cases']}
    if missing:
        return None, 0, ['缺少变化用例的判断：' + '、'.join(sorted(missing))]
    result = deepcopy(output)
    carried = snapshot['carried']

    def fill(key, identity, applicable=lambda item: True):
        given = {item[identity] for item in result[key]}
        additions = [deepcopy(item) for item in carried[key] if item[identity] not in given and applicable(item)]
        result[key].extend(additions)
        return len(additions)

    count = fill('cases', 'id')
    flows = {c['id'] for c in result['cases'] if c['kind'] == 'flow'}
    sections = {section(c) for c in snapshot['cases'] if c['id'] in flows}
    fill('nodes', 'section', lambda item: item['section'] in sections)
    original = yaml.safe_load(snapshot['scenarios']) or {}
    original_nodes = {n['id'] for n in original.get('sops', [])}
    fill('negative_exempt', 'section', lambda item: item['section'] in sections | original_nodes)
    fill('scenarios', 'id')
    # 输出缺口编号保留；沿用缺口分配本轮未占用的编号，避免碰撞。
    used = {g['id'] for g in result['gaps']}
    for gap in carried['gaps']:
        number = 1
        while f'G{number}' in used:
            number += 1
        gid = f'G{number}'
        used.add(gid)
        result['gaps'].append(dict(deepcopy(gap), id=gid))
    # responses、case_updates、notes 与 relocate 全部使用本轮输出。
    return result, count, []
