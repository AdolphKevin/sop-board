"""任务的只读判断核对与草稿生成；不写工作区仓库。"""
from copy import deepcopy
from datetime import date
from importlib.resources import files
import json
from pathlib import Path
import re

import yaml

from .errors import APIError
from .cases import load_cases
from .scenario_file import load_scenarios, NEGATIVE_TYPES


SCHEMA = json.loads(files('sop_board').joinpath('prompts/merge.schema.json').read_text(encoding='utf-8'))
GENERATE_SCHEMA = json.loads(files('sop_board').joinpath('prompts/generate.schema.json').read_text(encoding='utf-8'))


def generated_case(item):
    """差异比较与确认上传共用同一种用例正文，避免把草稿专属字段当作修改。"""
    case = {k: v for k, v in item.items() if k not in ('rules', 'basis', 'basis_note') and v is not None}
    basis = {'sop': 'SOP 依据', 'code': '代码推理', 'annotation': '批注确认'}[item['basis']]
    case['description'] = '\n'.join(filter(None, [case.get('description'), '依据：' + '、'.join(item['rules']),
                                                 basis + '：' + item['basis_note']]))
    return case


def generated_cases(snapshot, result, *, confirmed_by=None):
    cases = [generated_case(item) for item in result['cases']]
    title = re.search(r'^#\s+(.+)$', snapshot['sop']['content'], re.M)
    return yaml.safe_dump(dict(metadata=dict(module=snapshot['workspace'], feature=title[1] if title else snapshot['batch'],
        owner=confirmed_by if confirmed_by is not None else snapshot['sop']['confirmed_by'], last_reviewed=date.today().isoformat(), tags=['sop-draft']),
        test_cases=cases), allow_unicode=True, sort_keys=False)


def generation_problems(snapshot, result):
    problems = schema_problems(result, GENERATE_SCHEMA)
    if problems:
        return problems
    try:
        load_cases(dict(filename='SOP 草稿', uploaded_by=snapshot['sop']['confirmed_by'], content=generated_cases(snapshot, result)))
    except APIError as error:
        problems += [p['field'] + '：' + p['problem'] for p in error.problems]
    rules, cases, questions = {}, {c['id']: c for c in result['cases']}, set()
    available = {c['id']: c for c in snapshot['cases']} | cases
    for item in result['rules']:
        rid = item['id']
        if not re.fullmatch(r'R[1-9][0-9]*', rid) or rid in rules:
            problems.append(f'rules 规则编号不合格或重复：{rid}')
        if not item['section'].strip() or not item['excerpt'].strip():
            problems.append(f'{rid} 所属节点和 SOP 摘录不能为空')
        rules[rid] = item
    if not rules:
        problems.append('rules 至少包含一条 SOP 规则')
    links, seen = set(), set()
    for item in result['coverage']:
        rid = item['rule']
        if rid not in rules or rid in seen:
            problems.append(f'coverage 未知或重复规则：{rid}')
        seen.add(rid)
        if not item['positive']:
            problems.append(f'{rid} 缺少正向覆盖')
        if not item['negative'] and not (item['negative_exempt'] or '').strip():
            problems.append(f'{rid} 缺少负向覆盖或豁免理由')
        for polarity in ('positive', 'negative'):
            covered = set()
            for cid in item[polarity]:
                if cid not in available or cid in covered:
                    problems.append(f'{rid} {polarity} 未知或重复用例：{cid}')
                elif ('negative' in available[cid]['tags']) != (polarity == 'negative'):
                    problems.append(f'{rid} {cid} 正负向与用例标签不一致')
                covered.add(cid)
                if cid in cases:
                    links.add((rid, cid))
            if rid in rules and item[polarity] and not any(
                    cid in available and section(available[cid]) == rules[rid]['section'] for cid in item[polarity]):
                label = '正向' if polarity == 'positive' else '负向'
                problems.append(f'{rid} {label}覆盖至少包含一条节点为“{rules[rid]["section"]}”的用例')
    problems += [f'coverage 缺少规则：{rid}' for rid in sorted(rules.keys() - seen)]
    reverse = set()
    annotations = {'#' + str(a['number']): a for a in snapshot['all_annotations']}
    for item in result['cases']:
        cid = item['id']
        sections = [t[8:] for t in item['tags'] if t.startswith('section:')]
        if len(sections) != 1 or not sections[0].strip():
            problems.append(f'{cid} 必须恰好包含一个非空 section: 节点标签')
        if not item['rules'] or len(set(item['rules'])) != len(item['rules']):
            problems.append(f'{cid} rules 必须为非空且不重复的规则编号')
        for rid in item['rules']:
            if rid not in rules:
                problems.append(f'{cid} 引用了不存在的规则：{rid}')
            reverse.add((rid, cid))
        note = item['basis_note']
        if not note.strip():
            problems.append(f'{cid} 缺少依据说明')
        if item['basis'] == 'code':
            locations = re.findall(r'([A-Za-z][A-Za-z0-9_-]*):([^\s:，。；、`<>]+):([0-9]+)(?![0-9A-Za-z_])', note)
            if not locations:
                problems.append(f'{cid} 代码依据须含 <别名>:<路径>:<行>')
            for alias, relative, line in locations:
                root = snapshot['repos'].get(alias)
                if root is None:
                    problems.append(f'{cid} 代码位置别名未声明：{alias}')
                    continue
                try:
                    root = Path(root).resolve()
                    path = (root / relative).resolve()
                    if Path(relative).is_absolute() or '..' in Path(relative).parts or not path.is_relative_to(root) or not path.is_file():
                        problems.append(f'{cid} 代码文件不存在或越出仓库：{alias}:{relative}')
                        continue
                    if not 1 <= int(line) <= len(path.read_bytes().splitlines()):
                        problems.append(f'{cid} 代码行号超出文件范围：{alias}:{relative}:{line}')
                except (OSError, ValueError, RuntimeError):
                    problems.append(f'{cid} 无法读取代码文件：{alias}:{relative}')
        if item['basis'] == 'annotation':
            refs = re.findall(r'#[0-9]+', note)
            if not refs or any(ref not in annotations for ref in refs):
                problems.append(f'{cid} 批注依据须指向已有批注：{note}')
    problems += [f'coverage 与 cases.rules 不一致：{rid} / {cid}' for rid, cid in sorted(links ^ reverse)]
    for item in result['questions']:
        qid = item['id']
        if not re.fullmatch(r'Q[1-9][0-9]*', qid) or qid in questions or not item['text'].strip():
            problems.append(f'questions 编号重复、不合格或问题为空：{qid}')
        if item['rule'] is not None and item['rule'] not in rules:
            problems.append(f'{qid} 引用了不存在的规则：{item["rule"]}')
        questions.add(qid)
    for item in result['assumptions']:
        if not item['text'].strip() or any(cid not in available for cid in item['cases']):
            problems.append('assumptions 假设为空或引用了不存在的用例')
    registered = {c['id'] for c in snapshot['cases']}
    for item in result['obsolete']:
        if item['case'] not in registered or not item['reason'].strip():
            problems.append(f'obsolete 须引用已登记用例并说明原因：{item["case"]}')
    pending = {'#' + str(a['number']): a for a in snapshot['annotations']}
    responses = {}
    for item in result['responses']:
        ref = item['annotation']
        if ref not in pending or ref in responses:
            problems.append(f'responses 未知或重复批注：{ref}')
        if not item['note'].strip():
            problems.append(f'{ref} 缺少批注处理说明')
        responses[ref] = item
    problems += [f'responses 缺少批注：{ref}' for ref in sorted(pending.keys() - responses.keys())]
    previous = {c['id'] for c in (snapshot['draft'] or {}).get('cases', [])}
    removed = previous - cases.keys()
    adopted = {a['target'][5:] for ref, a in pending.items() if a['kind'] == 'remove' and a['target'].startswith('case:')
               and responses.get(ref, {}).get('action') == 'adopted'}
    problems += [f'用例 {cid} 消失，但没有被采纳的 remove 批注' for cid in sorted(removed - adopted)]
    problems += [f'不得删除已登记的用例 {cid}，只能列入 obsolete' for cid in sorted(removed & registered)]
    problems += [f'不得采纳删除已登记用例 {cid} 的批注，只能列入 obsolete' for cid in sorted(adopted & registered)]
    return problems


def section(case):
    return next((t[len('section:'):] for t in case['tags'] if t.startswith('section:')), '未分组')


def schema_problems(value, schema=SCHEMA, path='$'):
    """校验随包模式用到的 JSON Schema 子集，服务端不依赖 Codex 自行核对。"""
    problems = []
    kinds = schema.get('type')
    kinds = kinds if isinstance(kinds, list) else [kinds]
    valid = {'object': isinstance(value, dict), 'array': isinstance(value, list),
             'string': isinstance(value, str), 'boolean': type(value) is bool, 'null': value is None}
    if not any(valid.get(kind, False) for kind in kinds):
        labels = {'object': 'JSON 对象', 'array': '数组', 'string': '字符串', 'boolean': '布尔值', 'null': 'null'}
        label = '或'.join(labels[kind] for kind in kinds)
        return [f'{path} 应为 {label}']
    if 'enum' in schema and value not in schema['enum']:
        problems.append(f'{path} 取值不在枚举内：{value}')
    if isinstance(value, dict):
        properties = schema.get('properties', {})
        problems += [f'{path} 缺少 {key}' for key in schema.get('required', []) if key not in value]
        for key, child in value.items():
            subschema = properties.get(key, schema.get('additionalProperties', True))
            if subschema is False:
                problems.append(f'{path}.{key} 格式外字段')
            elif isinstance(subschema, dict):
                problems += schema_problems(child, subschema, f'{path}.{key}')
    elif isinstance(value, list):
        for i, child in enumerate(value):
            problems += schema_problems(child, schema['items'], f'{path}[{i}]')
    return problems


def normalize_judgment(result):
    """严格输出已核对后，将可选 null 和固定形状的移动记录转为业务输入。"""
    result = deepcopy(result)
    for group in ('cases', 'nodes', 'scenarios', 'negative_exempt'):
        result[group] = [{key: value for key, value in item.items() if value is not None}
                         for item in result[group]]
    moves = {}
    for move in result['relocate']:
        if move['scenario'] in moves:
            raise ValueError('relocate 重复场景：' + move['scenario'])
        moves[move['scenario']] = move['node']
    result['relocate'] = moves
    return result


def judgment_problems(snapshot, result):
    problems = []
    cases = {c['id']: c for c in snapshot['cases']}
    original = yaml.safe_load(snapshot['scenarios']) or {}
    old = {s['id']: s for n in original.get('sops', []) for s in n['scenarios'] if s['id'] not in cases}
    entries = snapshot.get('entries', {}).get('entries', {})
    seen = set()
    judgments = {item['id']: item for item in result['cases']}
    flows = {item['id'] for item in result['cases'] if item['kind'] == 'flow'}
    for item in result['cases']:
        sid = item['id']
        if sid not in cases or sid in seen:
            problems.append(f'cases 未知或重复用例：{sid}')
        seen.add(sid)
        if not item['actor'].strip():
            problems.append(f'{sid} actor 不能为空')
        if item['verify'] not in entries:
            problems.append(f"{sid} verify {item['verify']} 不在入口声明中")
        if not item['kind_reason'].strip():
            problems.append(f'{sid} 缺少分类理由')
        if item['kind'] == 'flow':
            if 'merged_into' in item:
                problems.append(f'{sid} flow 的 merged_into 必须为 null')
            if sid in cases and 'negative' in cases[sid]['tags'] and item.get('negative_type') not in NEGATIVE_TYPES:
                problems.append(f'{sid} negative_type 不在枚举内')
        else:
            target = item.get('merged_into')
            if target not in flows or target not in cases:
                problems.append(f'{sid} merged_into 必须指向本次生成的流程场景')
            elif (item['actor'], item['verify']) != (judgments[target]['actor'], judgments[target]['verify']):
                problems.append(f'{sid} 与所属流程的角色或验收入口不一致，不能并入')
        if sid in snapshot['forced_flow'] and item['kind'] != 'flow':
            problems.append(f'{sid} 已被开发者指定为独立流程场景')
    problems += [f'cases 缺少用例 {sid}' for sid in sorted(cases.keys() - seen)]
    seen = set()
    for item in result['scenarios']:
        sid = item['id']
        if sid not in old or sid in seen:
            problems.append(f'scenarios 未知或重复场景：{sid}')
        seen.add(sid)
        if item['action'] != 'kept':
            if item.get('target') not in flows:
                problems.append(f"{sid} target {item.get('target')} 不是本次流程场景")
            if not item.get('reason', '').strip():
                problems.append(f'{sid} 缺少去向理由')
        if item['action'] == 'duplicate' and sid in old:
            if 'forbidden_effects' not in item or set(item['forbidden_effects']) != set(old[sid]['forbidden_effects']):
                problems.append(f'{sid} forbidden_effects 必须完整且仅包含原有禁止副作用')
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
    flows = {item['id'] for item in result['cases'] if item['kind'] == 'flow'}
    sections = {section(c) for c in snapshot['cases'] if c['id'] in flows}
    positive = {section(c) for c in snapshot['cases'] if c['id'] in flows and 'negative' not in c['tags']}
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
    """使用入口已核对并规范化的判断；不让模型改写用例。"""
    entries = snapshot['entries']['entries']
    if not entries:
        return None, dict(ok=False, problems=['工作区缺少入口声明，请先搭建适配'])
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
        if decision['kind'] != 'flow':
            continue
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
        generated[case['id']] = scene
        groups[routes[section(case)]]['scenarios'].append(scene)
    for case in sorted(snapshot['cases'], key=lambda c: c['id']):
        decision = judgments[case['id']]
        if decision['kind'] == 'detail':
            target = generated[decision['merged_into']]
            target['expected'].extend('[用例 ' + case['id'] + '] ' + value for value in case['expected_results'])
            target['refs'].append(case['id'])
            target['forbidden_effects'] = list(dict.fromkeys(target['forbidden_effects'] + snapshot['merged_forbidden_effects'].get(case['id'], [])))
    for sid, item in dispositions.items():
        if item['action'] == 'duplicate':
            target = generated[item['target']]
            target['forbidden_effects'] = list(dict.fromkeys(target['forbidden_effects'] + item['forbidden_effects']))

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
    sources = [source] + [s for s in old_sources if s != source]
    uploaded_sop = snapshot.get('uploaded_sop')
    if uploaded_sop:
        sources = [s for s in sources if s['ref'] != uploaded_sop['ref']]
        sources.append(deepcopy(uploaded_sop))
    document = dict(metadata=dict(requirement=original.get('metadata', {}).get('requirement', meta['feature']),
                                 batch=snapshot['batch'], version=snapshot['upload'], owner=meta['owner'],
                                 sources=sources),
                    sops=list(groups.values()) + kept_nodes)
    if 'repos' in original.get('metadata', {}):
        document['metadata']['repos'] = original['metadata']['repos']
    raw = annotated_yaml(document, inferences)
    try:
        load_scenarios(raw, entries=set(entries))
    except APIError as error:
        return None, dict(ok=False, problems=error.problems)
    pairs = [dict(case=item['id'], scenarios=[item['id'] if item['kind'] == 'flow' else item['merged_into']],
                  kind=item['kind'], kind_reason=item['kind_reason'], note=item['kind_reason']) for item in result['cases']]
    return raw, dict(ok=True, problems=[], gaps=[], notes=notes, inferences=inferences, pairs=pairs, scenarios=result['scenarios'])


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
