"""项目配置中的真实入口声明。"""
import os
from pathlib import Path
import re


FIELDS = {'surface', 'operate', 'observe', 'evidence', 'isolation', 'address_env', 'credential_env',
          'web_evidence', 'atlas', 'identities', 'spec_library', 'probe', 'probe_timeout'}
PROBE_TIMEOUT = 10


def web_evidence(entry):
    """未声明的 Web 录制项沿用原有行为。"""
    return dict(video=True, trace=True) | entry.get('web_evidence', {})


def argv_ok(value):
    return isinstance(value, list) and bool(value) and all(isinstance(x, str) and x.strip() and '\0' not in x for x in value)


def public_problems(problems, *, local):
    """内网只保留问题类别；异常文字、字段名和路径中的本机信息留在本机。"""
    if local:
        return problems
    labels = {'config': '配置文件不合格', 'repos': '仓库配置不合格',
              'scenarios': '场景目录配置不合格',
              'entries': '验证入口配置不合格', 'metadata': '场景声明不合格',
              'workspace': '工作区配置不合格'}
    result = []
    for problem in problems:
        category = problem.get('field', '').split('.')[0]
        if category not in labels:
            category = 'workspace'
        item = dict(field=category, problem=labels[category] + '，请开发者在本机处理')
        if 'line' in problem:
            item['line'] = problem['line']
        if 'path' in problem:
            item['path'] = '<场景文件>'
        result.append(item)
    return result


def validate_entries(entries, repos=None):
    """repos 为 None 时（如手工 playwright scope 读取任务 entries.json）不核对 atlas 的仓库别名是否登记。"""
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
        if 'probe' in entry and not argv_ok(entry['probe']):
            problems.append(dict(field=field + '.probe', problem='应为非空命令数组，每项为非空文字'))
        if 'probe_timeout' in entry:
            timeout = entry['probe_timeout']
            if 'probe' not in entry or type(timeout) is not int or not 1 <= timeout <= 3600:
                problems.append(dict(field=field + '.probe_timeout', problem='须同时声明 probe，并填写 1～3600 的整数秒'))
        if 'credential_env' in entry:
            values = entry['credential_env']
            if (not isinstance(values, list) or not all(isinstance(v, str) and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', v) for v in values)
                    or len(set(values)) != len(values)):
                problems.append(dict(field=field + '.credential_env', problem='应为不重复的环境变量名数组'))
        if 'web_evidence' in entry:
            policy = entry['web_evidence']
            if (entry.get('surface') != 'web' or not isinstance(policy, dict)
                    or policy.keys() - {'video', 'trace'} or any(type(v) is not bool for v in policy.values())):
                problems.append(dict(field=field + '.web_evidence', problem='仅用于 Web 入口，须为只含 video、trace 布尔开关的对象'))
        if 'spec_library' in entry:
            value = entry['spec_library']
            alias, separator, relative = value.partition(':') if isinstance(value, str) else ('', '', '')
            if (entry.get('surface') != 'web' or not separator or not alias or not relative.strip()
                    or Path(relative).is_absolute() or '..' in Path(relative).parts or not Path(relative).parts
                    or '\0' in relative or repos is not None and alias not in repos):
                problems.append(dict(field=field + '.spec_library', problem='仅用于 Web，须为已登记仓库别名:仓内目录，不能是仓库根目录'))
            elif repos is not None:
                path = repos[alias] / relative
                if path.is_symlink() or not path.resolve().is_relative_to(repos[alias]) or path.resolve() == repos[alias] or path.exists() and not path.is_dir():
                    problems.append(dict(field=field + '.spec_library', problem='规范库须为仓库内普通目录，不能越界或是符号链接'))
        if 'identities' in entry:
            if entry.get('surface') != 'web':
                problems.append(dict(field=field + '.identities', problem='identities 只用于 Web 入口'))
            else:
                validate_identities(entry['identities'], field + '.identities', problems)
        if 'atlas' in entry:
            atlas = entry['atlas']
            def problem(key, message):
                problems.append(dict(field=field + '.atlas' + key, problem=message))
            if entry.get('surface') != 'web':
                problem('', 'atlas 只用于 Web 入口')
            if not isinstance(atlas, dict):
                problem('', '应为含 repos 的对象')
                continue
            for extra in atlas.keys() - {'repos', 'languages'}:
                problem('.' + str(extra), '格式外字段')
            aliases = atlas.get('repos')
            if not isinstance(aliases, list) or not aliases or not all(isinstance(v, str) and v.strip() for v in aliases):
                problem('.repos', '应为非空仓库别名数组')
            elif repos is not None and any(alias not in repos for alias in aliases):
                problem('.repos', '仓库别名未登记')
            if 'languages' in atlas:
                languages = atlas['languages']
                if (not isinstance(languages, list) or not languages
                        or not all(isinstance(v, str) and v.strip() for v in languages)
                        or len(set(languages)) != len(languages)):
                    problem('.languages', '应为不重复的非空文字数组')
    return problems


IDENTIFIER = re.compile(r'[a-z0-9][a-z0-9_-]*\Z')
ENV_NAME = re.compile(r'[A-Za-z_][A-Za-z0-9_]*\Z')
CODES = ('mailbox', 'backend', 'manual')
LIMITS = ('logins_per_day', 'codes_per_hour', 'codes_per_day')


def validate_identities(value, field, problems):
    """Web 入口的身份：每个身份对应看板浏览器代理的一份持久 profile。

    real 为真表示真实账号，只由人在有界面的 Chrome for Testing 窗口登录。"""
    if not isinstance(value, list) or not value:
        problems.append(dict(field=field, problem='须为非空身份数组'))
        return
    seen = set()
    for index, item in enumerate(value):
        where = f'{field}[{index}]'
        if not isinstance(item, dict):
            problems.append(dict(field=where, problem='身份须为对象'))
            continue
        for extra in item.keys() - {'id', 'label', 'account_env', 'code', 'limits', 'real'}:
            problems.append(dict(field=f'{where}.{extra}', problem='格式外字段'))
        if not isinstance(item.get('id'), str) or not IDENTIFIER.fullmatch(item['id']) or item['id'] in seen or item['id'] == 'default':
            problems.append(dict(field=where + '.id', problem='须为不重复的小写编号，不能是 default'))
        seen.add(item.get('id'))
        if not isinstance(item.get('label'), str) or not item['label'].strip():
            problems.append(dict(field=where + '.label', problem='须为非空文字，对应场景的 actor'))
        if 'account_env' in item and (not isinstance(item['account_env'], str) or not ENV_NAME.fullmatch(item['account_env'])):
            problems.append(dict(field=where + '.account_env', problem='须为环境变量名'))
        if item.get('code', 'manual') not in CODES:
            problems.append(dict(field=where + '.code', problem='应为 ' + '、'.join(CODES)))
        limits = item.get('limits', {})
        if not isinstance(limits, dict) or limits.keys() - set(LIMITS) or any(type(v) is not int or v <= 0 for v in limits.values()):
            problems.append(dict(field=where + '.limits', problem='只接受 ' + '、'.join(LIMITS) + '，取值为正整数'))
        if 'real' in item and type(item['real']) is not bool:
            problems.append(dict(field=where + '.real', problem='须为布尔值'))


def identities(entry):
    """Web 入口的身份编号；未声明身份时只有 default。"""
    values = entry.get('identities') if isinstance(entry, dict) else None
    return [item['id'] for item in values] if isinstance(values, list) and values else ['default']


def web_skill():
    directory = os.environ.get('SOP_BOARD_SKILLS_DIR')
    custom = Path(directory).expanduser() / 'playwright-acceptance' if directory else None
    return custom if custom and (custom / 'SKILL.md').is_file() else Path(__file__).parent / 'skills' / 'playwright-acceptance'


def check_references(entries, documents):
    problems = []
    for document in documents:
        for node in document.get('sops', []):
            for scene in node['scenarios']:
                if scene['verify'] not in entries:
                    problems.append(dict(field=scene['id'] + '.verify', problem='场景引用了未声明的入口 ' + scene['verify']))
    return problems


