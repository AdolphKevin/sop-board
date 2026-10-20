"""工作区配置中的真实入口声明；不读取仓库内的旧声明。"""
import re


FIELDS = {'surface', 'operate', 'observe', 'evidence', 'isolation', 'address_env', 'driver'}


def validate_entries(entries):
    problems = []
    if not isinstance(entries, dict) or not entries:
        return [dict(field='entries', problem='至少声明一个验收入口')]
    for name, entry in entries.items():
        field = f'entries.{name}'
        if not isinstance(name, str) or not name.strip() or not isinstance(entry, dict):
            problems.append(dict(field=field, problem='入口名称须为非空文字，声明须为对象'))
            continue
        for extra in entry.keys() - FIELDS:
            problems.append(dict(field=field + '.' + str(extra), problem='格式外字段'))
        for key in ('surface', 'operate', 'isolation'):
            if not isinstance(entry.get(key), str) or not entry[key].strip():
                problems.append(dict(field=field + '.' + key, problem='应为非空文字'))
        for key in ('observe', 'evidence'):
            values = entry.get(key)
            if not isinstance(values, list) or not values or not all(isinstance(v, str) and v.strip() for v in values):
                problems.append(dict(field=field + '.' + key, problem='应为非空文字数组'))
        if 'address_env' in entry and not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', str(entry['address_env'])):
            problems.append(dict(field=field + '.address_env', problem='应为环境变量名'))
        if 'driver' in entry and (not isinstance(entry['driver'], str) or entry['driver'] in ('.', '..') or not re.fullmatch(r'[A-Za-z0-9_.-]+', entry['driver'])):
            problems.append(dict(field=field + '.driver', problem='应为技能目录名'))
    return problems


def check_references(entries, documents):
    problems = []
    for document in documents:
        for node in document.get('sops', []):
            for scene in node['scenarios']:
                if scene['verify'] not in entries:
                    problems.append(dict(field=scene['id'] + '.verify', problem='场景引用了未声明的入口 ' + scene['verify']))
    return problems
