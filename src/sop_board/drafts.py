"""任务的只读判断核对与草稿生成；不写项目仓库。"""
from copy import deepcopy
from importlib.resources import files
import json
import re

import yaml

from .errors import APIError
from .scenario_file import load_scenarios, NEGATIVE_TYPES


SCHEMA = json.loads(files('sop_board').joinpath('prompts/merge.schema.json').read_text(encoding='utf-8'))


def section(case):
    return next((t[len('section:'):] for t in case['tags'] if t.startswith('section:')), '未分组')


def schema_problems(value, schema=SCHEMA, path='$'):
    """校验随包模式用到的 JSON Schema 子集，服务端不依赖 Codex 自行核对。"""
    problems = []
    kind = schema.get('type')
    valid = {'object': isinstance(value, dict), 'array': isinstance(value, list),
             'string': isinstance(value, str), 'boolean': type(value) is bool}
    if kind in valid and not valid[kind]:
        label = {'object': 'JSON 对象', 'array': '数组', 'string': '字符串', 'boolean': '布尔值'}[kind]
        return [f'{path} 应为 {label}']
    if 'enum' in schema and value not in schema['enum']:
        problems.append(f'{path} 取值不在枚举内：{value}')
    if kind == 'object':
        properties = schema.get('properties', {})
        problems += [f'{path} 缺少 {key}' for key in schema.get('required', []) if key not in value]
        for key, child in value.items():
            subschema = properties.get(key, schema.get('additionalProperties', True))
            if subschema is False:
                problems.append(f'{path}.{key} 格式外字段')
            elif isinstance(subschema, dict):
                problems += schema_problems(child, subschema, f'{path}.{key}')
    elif kind == 'array':
        for i, child in enumerate(value):
            problems += schema_problems(child, schema['items'], f'{path}[{i}]')
    return problems


def judgment_problems(snapshot, result):
    problems = []
    cases = {c['id']: c for c in snapshot['cases']}
    original = yaml.safe_load(snapshot['scenarios']) or {}
    old = {s['id']: s for n in original.get('sops', []) for s in n['scenarios'] if s['id'] not in cases}
    entries = snapshot.get('entries', {}).get('entries', {})
    seen = set()
    for item in result['cases']:
        sid = item['id']
        if sid not in cases or sid in seen:
            problems.append(f'cases 未知或重复用例：{sid}')
        seen.add(sid)
        if not item['actor'].strip():
            problems.append(f'{sid} actor 不能为空')
        if item['verify'] not in entries:
            problems.append(f"{sid} verify {item['verify']} 不在入口声明中")
        if sid in cases and 'negative' in cases[sid]['tags'] and item.get('negative_type') not in NEGATIVE_TYPES:
            problems.append(f'{sid} negative_type 不在枚举内')
    problems += [f'cases 缺少用例 {sid}' for sid in sorted(cases.keys() - seen)]
    seen = set()
    for item in result['scenarios']:
        sid = item['id']
        if sid not in old or sid in seen:
            problems.append(f'scenarios 未知或重复场景：{sid}')
        seen.add(sid)
        if item['action'] != 'kept':
            if item.get('target') not in cases:
                problems.append(f"{sid} target {item.get('target')} 不存在")
            if not item.get('reason', '').strip():
                problems.append(f'{sid} 缺少去向理由')
        if item['action'] == 'duplicate' and sid in old:
            if 'forbidden_effects' not in item or set(item['forbidden_effects']) != set(old[sid]['forbidden_effects']):
                problems.append(f'{sid} forbidden_effects 必须完整且仅包含原有禁止副作用')
            if 'transfer_regression' not in item:
                problems.append(f'{sid} 缺少 transfer_regression 判断')
            original_target = next((s for n in original.get('sops', []) for s in n['scenarios'] if s['id'] == item.get('target')), {})
            if 'regression' in old[sid] and 'regression' not in original_target and not item.get('transfer_regression'):
                problems.append(f'{sid} regression 不可丢失：应转移回归声明')
    problems += [f'{sid} 没有去向' for sid in sorted(old.keys() - seen)]
    for sid in old:
        if sid.startswith('TC_'):
            problems.append(f'原有场景 {sid} 使用了 TC_ 形式的编号')
    for sid, target in result['relocate'].items():
        if sid not in old:
            problems.append(f'relocate 的原有场景 {sid} 不存在')
        if target not in snapshot['nodes'].values():
            problems.append(f'relocate 的节点 {target} 不存在')
    return problems


def destinations(snapshot, result, problems):
    sections = {section(c) for c in snapshot['cases']}
    positive = {section(c) for c in snapshot['cases'] if 'negative' not in c['tags']}
    nodes = {}
    for item in result['nodes']:
        name = item['section']
        if name not in sections or name in nodes:
            problems.append(f'nodes 未知或重复 section：{name}')
        nodes[name] = item
        for key in ('actor', 'entry'):
            if not item[key].strip():
                problems.append(f'节点 {name} 的 {key} 不能为空')
        if 'merge_into' in item and name in positive:
            problems.append(f'节点 {name} 有正向用例，不可 merge_into')
    problems += [f'nodes 缺少 section：{name}' for name in sorted(sections - nodes.keys())]
    routes = {}
    for name in sections:
        trail, target = set(), name
        while target in nodes and 'merge_into' in nodes[target]:
            if target in trail:
                problems.append(f'merge_into 循环引用：{name}')
                break
            trail.add(target)
            target = nodes[target]['merge_into']
        if target not in sections:
            problems.append(f'merge_into 目标 {target} 不存在')
        routes[name] = target
    return nodes, routes, positive


def assemble(snapshot, result):
    """只用快照和判断组装；不让模型改写用例，所有核对通过后才返回原文。"""
    problems = schema_problems(result)
    if problems:
        return None, dict(ok=False, problems=['输出模式：' + p for p in problems])
    inferred_entries = None
    if not snapshot.get('entries', {}).get('entries'):
        if not snapshot.get('writable'):
            return None, dict(ok=False, problems=['缺少入口声明：请随场景文件推送 entries'])
        inferred_entries = result.get('entries')
        entries = (inferred_entries or {}).get('entries', {})
        if not entries:
            problems.append('授权仓库缺少入口声明，entries 未提供推断的入口')
        for name, entry in entries.items():
            if not name.strip() or any(not entry[k].strip() for k in ('surface', 'operate', 'isolation')):
                problems.append(f'推断入口 {name} 的名称或文字不能为空')
            if any(not entry[k] or any(not v.strip() for v in entry[k]) for k in ('observe', 'evidence')):
                problems.append(f'推断入口 {name} 的 observe 和 evidence 应为非空字符串数组')
            if 'address_env' in entry and not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', entry['address_env']):
                problems.append(f'推断入口 {name} 的 address_env 应为环境变量名')
        if problems:
            return None, dict(ok=False, problems=problems)
        snapshot = dict(snapshot, entries=inferred_entries)
    elif 'entries' in result:
        return None, dict(ok=False, problems=['已有入口声明时应省略 entries 推断字段'])
    problems = judgment_problems(snapshot, result)
    nodes, routes, positive = destinations(snapshot, result, problems)
    original = yaml.safe_load(snapshot['scenarios']) or {}
    original_nodes = {n['id'] for n in original.get('sops', [])}
    exemptions = {}
    for item in result['negative_exempt']:
        name = item['section']
        if name in exemptions or name not in snapshot['nodes'] and name not in original_nodes:
            problems.append(f'negative_exempt 未知或重复 section：{name}')
        if not item['reason'].strip():
            problems.append(f'节点 {name} 的 negative_exempt 理由不能为空')
        exemptions[name] = item
    for key in ('gaps', 'notes'):
        if any(not value.strip() for value in result[key]):
            problems.append(f'{key} 不可包含空字符串')
    if problems:
        return None, dict(ok=False, problems=problems)
    gaps = list(result['gaps'])
    for name, target in routes.items():
        if target not in positive and not any(name in gap for gap in gaps):
            gaps.append(f'{name} 没有正向依据')
    if gaps:
        return None, dict(ok=True, problems=[], gaps=gaps)

    entries = snapshot.get('entries', {}).get('entries', {})
    if not entries:
        return None, dict(ok=False, problems=['缺少入口声明：请随场景文件推送 entries'])
    original_scenarios = {s['id']: s for n in original.get('sops', []) for s in n['scenarios']}
    judgments = {item['id']: item for item in result['cases']}
    dispositions = {item['id']: item for item in result['scenarios']}
    groups = {name: dict(id=snapshot['nodes'][name], title=name, actor=nodes[name]['actor'],
                         entry=nodes[name]['entry'], scenarios=[])
              for name in sorted(nodes, key=lambda n: int(snapshot['nodes'][n][1:])) if routes[name] == name}
    generated, inferences, notes = {}, [], list(result['notes'])
    for case in sorted(snapshot['cases'], key=lambda c: c['id']):
        decision = judgments[case['id']]
        initial = case.get('preconditions') or decision.get('initial_state')
        if not isinstance(initial, list) or not initial or any(not v.strip() for v in initial):
            problems.append(f"{case['id']} 缺少 initial_state")
            initial = []
        elif not case.get('preconditions'):
            inferences.append(dict(id=case['id'], field='initial_state', content='；'.join(initial)))
        elif 'initial_state' in decision:
            notes.append(f"{case['id']} 已有 preconditions，Codex 给出的 initial_state 未采用")
        scene = dict(id=case['id'], title=case['title'], actor=decision['actor'], priority=case['priority'],
                     polarity='negative' if 'negative' in case['tags'] else 'positive', verify=decision['verify'],
                     refs=[case['id']], tags=deepcopy(case['tags']), initial_state=deepcopy(initial),
                     steps=deepcopy(case['steps']), expected=deepcopy(case['expected_results']),
                     forbidden_effects=deepcopy(snapshot['merged_forbidden_effects'].get(case['id'], [])))
        if scene['polarity'] == 'negative':
            scene['negative_type'] = decision['negative_type']
        if 'regression' in original_scenarios.get(case['id'], {}):
            scene['regression'] = deepcopy(original_scenarios[case['id']]['regression'])
        generated[case['id']] = scene
        groups[routes[section(case)]]['scenarios'].append(scene)
    for sid, item in dispositions.items():
        if item['action'] == 'duplicate':
            target = generated[item['target']]
            target['forbidden_effects'] = list(dict.fromkeys(target['forbidden_effects'] + item['forbidden_effects']))
            if item['transfer_regression'] and 'regression' not in target and 'regression' in original_scenarios[sid]:
                target['regression'] = deepcopy(original_scenarios[sid]['regression'])

    by_node = {n['id']: n for n in groups.values()}
    used_moves = set()
    kept_nodes = []
    for node in original.get('sops', []):
        kept = deepcopy(node)
        kept['scenarios'] = [s for s in kept['scenarios'] if dispositions.get(s['id'], {}).get('action') == 'kept']
        if not kept['scenarios']:
            continue
        if any(s['polarity'] == 'positive' for s in kept['scenarios']):
            if not any(s['polarity'] == 'negative' for s in kept['scenarios']) and node['id'] in exemptions:
                exemption = exemptions[node['id']]
                kept['negative_exempt'] = exemption['reason']
                if exemption.get('inferred'):
                    inferences.append(dict(id=node['id'], field='negative_exempt', content=exemption['reason']))
            kept_nodes.append(kept)
        else:
            for scene in kept['scenarios']:
                target = result['relocate'].get(scene['id'])
                if target not in by_node:
                    problems.append(f"{scene['id']} 缺少指向有效用例节点的 relocate：{target}")
                else:
                    by_node[target]['scenarios'].append(scene)
                    used_moves.add(scene['id'])
    for sid in result['relocate'].keys() - used_moves:
        problems.append(f'relocate {sid} 不适用于保留正向场景的节点或被移除的场景')
    for name, node in groups.items():
        if not any(s['polarity'] == 'negative' for s in node['scenarios']):
            if name not in exemptions:
                problems.append(f'节点 {name} 缺少反向豁免理由')
            else:
                node['negative_exempt'] = exemptions[name]['reason']
                if exemptions[name].get('inferred'):
                    inferences.append(dict(id=node['id'], field='negative_exempt', content=node['negative_exempt']))
    if problems:
        return None, dict(ok=False, problems=problems)
    meta = snapshot['case_metadata']
    source = dict(ref=f"批次 {snapshot['batch']} 用例上传 #{snapshot['upload']}", version=snapshot['upload'], confirmed_by=meta['owner'])
    # 只带有保留补充场景的文件才需要延续旧来源，避免重复合并累加看板生成的来源。
    old_sources = deepcopy(original.get('metadata', {}).get('sources', [])) if dispositions or any(snapshot['merged_forbidden_effects'].values()) else []
    document = dict(metadata=dict(requirement=original.get('metadata', {}).get('requirement', meta['feature']),
                                 batch=snapshot['batch'], version=snapshot['upload'], owner=meta['owner'],
                                 sources=[source] + [s for s in old_sources if s != source]),
                    sops=list(groups.values()) + kept_nodes)
    raw = annotated_yaml(document, inferences)
    try:
        load_scenarios(raw, entries=set(entries))
    except APIError as error:
        return None, dict(ok=False, problems=error.problems)
    if inferred_entries:
        inferences.append(dict(id='entries', field='entries', content='入口声明由 Codex 推断：' + '、'.join(entries)))
    return raw, dict(ok=True, problems=[], gaps=[], draft=True, notes=notes, inferences=inferences,
                     inferred_entries=inferred_entries)


def annotated_yaml(document, inferences):
    raw = yaml.safe_dump(document, allow_unicode=True, sort_keys=False)
    comments = {(item['id'], item['field']): item['content'] for item in inferences}
    inserts = {}

    def visit(node):
        if isinstance(node, yaml.MappingNode):
            fields = {k.value: v for k, v in node.value}
            identifier = fields.get('id')
            if identifier is not None:
                for key, _ in node.value:
                    content = comments.get((identifier.value, key.value))
                    if content is not None:
                        inserts[key.start_mark.line] = [
                            ' ' * key.start_mark.column + f'# 待确认 {identifier.value}：{line}'
                            for line in content.splitlines()]
            for _, child in node.value:
                visit(child)
        elif isinstance(node, yaml.SequenceNode):
            for child in node.value:
                visit(child)

    visit(yaml.compose(raw))
    lines = []
    for index, line in enumerate(raw.splitlines()):
        lines.extend(inserts.get(index, []))
        lines.append(line)
    return '\n'.join(lines) + '\n'
