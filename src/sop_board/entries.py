"""读取授权仓库的入口与检查声明，返回可展示的问题。"""
import json
import math
from pathlib import Path
import re

import yaml


CHECK_FIELDS = {'argv', 'cwd', 'timeout', 'format', 'report', 'required_env'}


def read_declarations(root):
    config = root / '.tw/config.json'
    path = config if config.exists() else root / 'docs/sop/entries.yaml'
    result = dict(ok=False, source='tw-config' if path == config else 'entries-yaml', entries={}, checks={}, problems=[])

    def add(field, message):
        result['problems'].append(dict(field=field, problem=message))

    def text(value):
        return isinstance(value, str) and bool(value.strip()) and '\0' not in value

    def strings(value, nonempty=True):
        return isinstance(value, list) and (bool(value) or not nonempty) and all(text(v) for v in value)

    def variable(value):
        return isinstance(value, str) and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', value)

    try:
        raw = path.read_text(encoding='utf-8') if path.exists() else '{}'
        for token in yaml.scan(raw):
            if isinstance(token, (yaml.tokens.AnchorToken, yaml.tokens.AliasToken, yaml.tokens.TagToken)):
                raise ValueError('不支持锚点、别名或显式标签')
        node = yaml.compose(raw, Loader=yaml.SafeLoader)

        def inspect(node):
            if isinstance(node, yaml.MappingNode):
                keys = set()
                for key, value in node.value:
                    if key.tag != 'tag:yaml.org,2002:str' or key.value in keys:
                        raise ValueError('字段须为字符串且不能重复')
                    keys.add(key.value)
                    inspect(value)
            elif isinstance(node, yaml.SequenceNode):
                for child in node.value:
                    inspect(child)
        inspect(node)
        document = json.loads(raw) if path == config else yaml.safe_load(raw)
        if not isinstance(document, dict):
            raise ValueError('声明应为对象')
        try:
            checks = document.get('checks', {})
            selected = {name: {k: v for k, v in check.items() if k in CHECK_FIELDS} if isinstance(check, dict) else check
                        for name, check in checks.items()} if isinstance(checks, dict) else checks
            json.dumps(dict(entries=document.get('acceptance_entries' if path == config else 'entries', {}),
                            checks=selected), allow_nan=False)
        except (TypeError, ValueError) as error:
            raise ValueError('声明只支持 JSON 数据类型与有限数字') from error
    except (OSError, ValueError, yaml.YAMLError, RecursionError) as error:
        add(str(path.relative_to(root)), f'入口声明无法读取：{error}')
        return result

    for section, source in [('entries', 'acceptance_entries' if path == config else 'entries'), ('checks', 'checks')]:
        values = document.get(source, {})
        if not isinstance(values, dict):
            add(source, f'{source} 应为对象')
            continue
        for name, value in values.items():
            field = f'{source}.{name}'
            if not text(name) or (section == 'checks' and (name in {'.', '..'} or '/' in name or '\\' in name)):
                add(field, '名称应为非空字符串，检查编号不能含路径分隔符')
                continue
            if not isinstance(value, dict):
                add(field, '声明应为对象')
                continue
            if section == 'entries':
                entry = dict(value, candidate=value.get('candidate', 'in-place'))
                result['entries'][name] = entry
                for key in ('surface', 'operate', 'isolation'):
                    if not text(entry.get(key)):
                        add(field + '.' + key, f'入口 {name} 的 {key} 应为非空字符串')
                for key in ('observe', 'evidence'):
                    if not strings(entry.get(key)):
                        add(field + '.' + key, f'入口 {name} 的 {key} 应为非空字符串数组')
                if entry['candidate'] not in ('checkout', 'start', 'in-place'):
                    add(field + '.candidate', f'入口 {name} 的 candidate 只能为 checkout、start 或 in-place')
                for key in ('start', 'reload'):
                    if (key in entry or (key == 'start' and entry['candidate'] == 'start')) and not strings(entry.get(key)):
                        add(field + '.' + key, f'入口 {name} 的 {key} 应为非空字符串数组')
                if 'address_env' in entry and not variable(entry['address_env']):
                    add(field + '.address_env', f'入口 {name} 的 address_env 应为环境变量名')
            else:
                check = dict(cwd='.', timeout=900, required_env=[])
                check.update({k: v for k, v in value.items() if k in CHECK_FIELDS})
                result['checks'][name] = check
                if not strings(check.get('argv')):
                    add(field + '.argv', f'检查 {name} 的 argv 应为非空字符串数组')
                if check.get('format') not in ('junit', 'go-json'):
                    add(field + '.format', f'检查 {name} 的 format 只能为 junit 或 go-json')
                if check.get('format') == 'junit' and not text(check.get('report')):
                    add(field + '.report', f'检查 {name} 的 format 为 junit 时缺少 report（应为非空字符串）')
                elif 'report' in check and not text(check['report']):
                    add(field + '.report', f'检查 {name} 的 report 应为非空字符串')
                cwd = check['cwd']
                if not text(cwd) or Path(cwd).is_absolute() or '..' in Path(cwd).parts:
                    add(field + '.cwd', f'检查 {name} 的 cwd 应为检出目录内的相对路径')
                timeout = check['timeout']
                if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
                    add(field + '.timeout', f'检查 {name} 的 timeout 应为正数秒数')
                required = check['required_env']
                if not isinstance(required, list) or not all(variable(v) for v in required):
                    add(field + '.required_env', f'检查 {name} 的 required_env 应为环境变量名数组')
    result['ok'] = not result['problems']
    return result


def check_references(declarations, documents):
    for document in documents:
        for node in document.get('sops', []):
            for scene in node['scenarios']:
                for field, name, declared in [('verify', scene['verify'], declarations['entries']),
                        ('regression.check', (scene.get('regression') or {}).get('check'), declarations['checks'])]:
                    if name is not None and name not in declared:
                        declarations['problems'].append(dict(field=f"{scene['id']}.{field}",
                            problem=f"场景 {scene['id']} 的 {field} 引用了未声明的{'入口' if field == 'verify' else '检查'} {name}"))
    declarations['ok'] = not declarations['problems']
    return declarations
