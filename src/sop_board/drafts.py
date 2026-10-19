"""任务的只读判断核对与草稿生成；不写项目仓库。"""
from copy import deepcopy
import json

import yaml

from .errors import APIError
from .scenario_file import load_scenarios, NEGATIVE_TYPES


SCHEMA = {
    'type': 'object',
    'properties': {
        'cases': {'type': 'array', 'items': {'type': 'object', 'properties': {
            'id': {'type': 'string'}, 'actor': {'type': 'string'}, 'verify': {'type': 'string'},
            'negative_type': {'type': 'string'}, 'initial_state': {'type': 'array', 'items': {'type': 'string'}}},
            'required': ['id', 'actor', 'verify'], 'additionalProperties': False}},
        'nodes': {'type': 'array', 'items': {'type': 'object', 'properties': {
            'section': {'type': 'string'}, 'actor': {'type': 'string'}, 'entry': {'type': 'string'}, 'merge_into': {'type': 'string'}},
            'required': ['section', 'actor', 'entry'], 'additionalProperties': False}},
        'scenarios': {'type': 'array', 'items': {'type': 'object', 'properties': {
            'id': {'type': 'string'}, 'action': {'type': 'string', 'enum': ['kept', 'duplicate', 'conflict']},
            'target': {'type': 'string'}, 'reason': {'type': 'string'}}, 'required': ['id', 'action'], 'additionalProperties': False}},
        'negative_exempt': {'type': 'array', 'items': {'type': 'object', 'properties': {
            'section': {'type': 'string'}, 'reason': {'type': 'string'}, 'inferred': {'type': 'boolean'}},
            'required': ['section', 'reason'], 'additionalProperties': False}},
        'gaps': {'type': 'array', 'items': {'type': 'string'}},
        'notes': {'type': 'array', 'items': {'type': 'string'}},
        'relocate': {'type': 'object', 'additionalProperties': {'type': 'string'}},
    },
    'required': ['cases', 'nodes', 'scenarios', 'negative_exempt', 'gaps', 'notes', 'relocate'],
    'additionalProperties': False,
}


def section(case):
    return next((t[len('section:'):] for t in case['tags'] if t.startswith('section:')), '未分组')


def assemble(snapshot, result):
    """返回 (草稿原文或 None, 核对记录)。只处理本轮所需的保留原场景流程。"""
    problems = []
    if not isinstance(result, dict):
        return None, dict(ok=False, problems=['最终消息不是 JSON 对象'])
    for name in SCHEMA['required']:
        if name not in result:
            problems.append(f'输出模式缺少 {name} 字段')
        elif name == 'relocate':
            if not isinstance(result[name], dict):
                problems.append('relocate 应为对象')
        elif not isinstance(result[name], list):
            problems.append(f'{name} 应为数组')
    if problems:
        return None, dict(ok=False, problems=problems)
    for name in ('gaps', 'notes'):
        if not all(isinstance(v, str) and v.strip() for v in result[name]):
            problems.append(f'{name} 应为非空字符串组成的数组')
    entries = snapshot.get('entries', {}).get('entries', {})
    if not entries:
        problems.append('缺少入口声明：请随场景文件推送 entries')
    cases = snapshot['cases']
    original = yaml.safe_load(snapshot['scenarios']) if snapshot['scenarios'] else None
    original_scenarios = {s['id']: s for n in (original or {}).get('sops', []) for s in n['scenarios']}
    expected_ids = {c['id'] for c in cases}
    judgments = {}
    for item in result['cases']:
        if not isinstance(item, dict) or not isinstance(item.get('id'), str) or item.get('id') not in expected_ids or item.get('id') in judgments:
            problems.append('cases 中存在未知、重复或不合格的用例')
            continue
        judgments[item['id']] = item
        if not isinstance(item.get('actor'), str) or not item['actor'].strip():
            problems.append(f"{item['id']} actor 不能为空")
        if not isinstance(item.get('verify'), str) or item['verify'] not in entries:
            problems.append(f"{item['id']} verify 引用了未声明的入口")
    if set(judgments) != expected_ids:
        problems.append('cases 没有覆盖全部用例')
    if problems:
        return None, dict(ok=False, problems=problems)
    if result['gaps']:
        return None, dict(ok=True, problems=[], gaps=result['gaps'])
    groups, nodes, exemptions = {}, {}, {}
    for item in result['nodes']:
        if not isinstance(item, dict) or not isinstance(item.get('section'), str) or item['section'] in nodes:
            problems.append('nodes 中存在重复或不合格的节点')
        else:
            nodes[item['section']] = item
    for item in result['negative_exempt']:
        if isinstance(item, dict) and isinstance(item.get('section'), str):
            exemptions[item['section']] = item.get('reason')
    for case in sorted(cases, key=lambda c: c['id']):
        name = section(case)
        decision = judgments[case['id']]
        if name not in groups:
            node = nodes.get(name, {})
            if not all(isinstance(node.get(k), str) and node[k].strip() for k in ('actor', 'entry')):
                problems.append(f'节点 {name} 缺少 actor 或 entry')
            groups[name] = dict(id=f'S{len(groups)+1:02}', title=name, actor=node.get('actor', ''), entry=node.get('entry', ''), scenarios=[])
        initial = case.get('preconditions') or decision.get('initial_state')
        if not isinstance(initial, list) or not initial or not all(isinstance(v, str) and v.strip() for v in initial):
            problems.append(f"{case['id']} 缺少 initial_state")
            initial = []
        scenario = dict(id=case['id'], title=case['title'], actor=decision['actor'], priority=case['priority'],
                        polarity='negative' if 'negative' in case['tags'] else 'positive', verify=decision['verify'],
                        refs=[case['id']], tags=case['tags'], initial_state=initial, steps=case['steps'],
                        expected=case['expected_results'], forbidden_effects=[])
        if scenario['polarity'] == 'negative':
            if decision.get('negative_type') not in NEGATIVE_TYPES:
                problems.append(f"{case['id']} negative_type 不合格")
            scenario['negative_type'] = decision.get('negative_type', 'other')
        if 'regression' in original_scenarios.get(case['id'], {}):
            scenario['regression'] = deepcopy(original_scenarios[case['id']]['regression'])
        groups[name]['scenarios'].append(scenario)
    for name, node in groups.items():
        if not any(s['polarity'] == 'negative' for s in node['scenarios']):
            reason = exemptions.get(name)
            if not isinstance(reason, str) or not reason.strip():
                problems.append(f'节点 {name} 缺少反向豁免理由')
            else:
                node['negative_exempt'] = reason
    old_ids = set(original_scenarios) - expected_ids
    dispositions = {}
    for item in result['scenarios']:
        if not isinstance(item, dict) or not isinstance(item.get('id'), str) or item.get('id') not in old_ids or item.get('id') in dispositions:
            problems.append('scenarios 中存在未知、重复或不合格的场景')
            continue
        dispositions[item['id']] = item.get('action')
    if set(dispositions) != old_ids:
        problems.append('scenarios 没有覆盖全部原有场景')
    if any(action != 'kept' for action in dispositions.values()) or result['relocate']:
        problems.append('非保留场景的组装与节点移动尚未启用')
    if problems:
        return None, dict(ok=False, problems=problems)
    metadata = snapshot['case_metadata']
    source = dict(ref=f"批次 {snapshot['batch']} 用例上传 #{snapshot['upload']}", version=snapshot['upload'], confirmed_by=metadata['owner'])
    kept_nodes = []
    for node in (original or {}).get('sops', []):
        kept = deepcopy(node)
        kept['scenarios'] = [s for s in kept['scenarios'] if s['id'] not in expected_ids]
        if kept['scenarios']:
            kept_nodes.append(kept)
    document = dict(metadata=dict(requirement=(original or {}).get('metadata', {}).get('requirement', metadata['feature']),
                                 batch=snapshot['batch'], version=snapshot['upload'], owner=metadata['owner'],
                                 sources=[source] + deepcopy((original or {}).get('metadata', {}).get('sources', []))),
                    sops=list(groups.values()) + kept_nodes)
    raw = yaml.safe_dump(document, allow_unicode=True, sort_keys=False)
    try:
        load_scenarios(raw, entries=set(entries))
    except APIError as error:
        return None, dict(ok=False, problems=error.problems)
    return raw, dict(ok=True, problems=[], gaps=[], draft=True)
