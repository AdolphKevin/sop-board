"""工作区配置中的真实入口声明；不读取仓库内的旧声明。"""
import hashlib
import os
from pathlib import Path
import re
import stat

import yaml


FIELDS = {'surface', 'operate', 'observe', 'evidence', 'isolation', 'address_env', 'driver', 'credential_env', 'web_evidence'}


def web_evidence(entry):
    """未声明的 Web 录制项沿用原有行为。"""
    return dict(video=True, trace=True) | entry.get('web_evidence', {})


def public_problems(problems, *, local):
    """内网只保留问题类别；异常文字、字段名和路径中的本机信息留在本机。"""
    if local:
        return problems
    labels = {'config': '配置文件不合格', 'repos': '仓库配置不合格',
              'scenarios': '场景目录配置不合格', 'skill': '验证技能不合格',
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
        if 'credential_env' in entry:
            values = entry['credential_env']
            if (not isinstance(values, list) or not all(isinstance(v, str) and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', v) for v in values)
                    or len(set(values)) != len(values)):
                problems.append(dict(field=field + '.credential_env', problem='应为不重复的环境变量名数组'))
        if 'driver' in entry and not skill_name(entry['driver']):
            problems.append(dict(field=field + '.driver', problem='应为技能目录名'))
        if 'web_evidence' in entry:
            policy = entry['web_evidence']
            if (entry.get('surface') != 'web' or not isinstance(policy, dict)
                    or policy.keys() - {'video', 'trace'} or any(type(v) is not bool for v in policy.values())):
                problems.append(dict(field=field + '.web_evidence', problem='仅用于 Web 入口，须为只含 video、trace 布尔开关的对象'))
    return problems


def check_references(entries, documents):
    problems = []
    for document in documents:
        for node in document.get('sops', []):
            for scene in node['scenarios']:
                if scene['verify'] not in entries:
                    problems.append(dict(field=scene['id'] + '.verify', problem='场景引用了未声明的入口 ' + scene['verify']))
    return problems


def skill_name(value):
    return isinstance(value, str) and value not in ('.', '..') and bool(re.fullmatch(r'[A-Za-z0-9_.-]+', value))


def skill_files(directory):
    """固定排序的普通文件清单；拒绝技能内及其入口目录上的符号链接。"""
    if directory.is_symlink() or directory.parent.is_symlink():
        raise ValueError('验证技能目录内不能有符号链接')
    if not (directory / 'SKILL.md').is_file():
        raise ValueError('验证技能不存在：' + str(directory / 'SKILL.md'))
    manifest = []
    def unreadable(error):
        raise error
    for root, directories, names in os.walk(directory, onerror=unreadable, followlinks=False):
        for name in directories + names:
            path = Path(root) / name
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode):
                raise ValueError('验证技能目录内不能有符号链接：' + str(path.relative_to(directory)))
            if stat.S_ISREG(mode):
                manifest.append(dict(path=path.relative_to(directory).as_posix(), sha256=hashlib.sha256(path.read_bytes()).hexdigest()))
    return sorted(manifest, key=lambda row: row['path'])


def verification_skills(data, skill, entries):
    problems, drivers = [], {}
    path = data / 'skills' / skill / 'SKILL.md' if skill_name(skill) else None
    if path is None:
        problems.append(dict(field='skill', problem='验证技能不存在：skill 须为技能目录名'))
    else:
        try:
            skill_files(path.parent)
            text = path.read_text(encoding='utf-8')
            header = re.match(r'\A---[ \t]*\r?\n(.*?)^---[ \t]*\r?$', text, re.M | re.S)
            if not header:
                raise ValueError('验证技能 SKILL.md 缺少以 --- 开头的 YAML 文件头')
            metadata = yaml.safe_load(header[1])
            if not isinstance(metadata, dict) or any(not isinstance(metadata.get(key), str) or not metadata[key].strip() for key in ('name', 'description')):
                raise ValueError('验证技能文件头的 name、description 须为非空文字')
            headings, fence = [], None
            for line in text[header.end():].splitlines():
                marker = re.match(r'^ {0,3}(`{3,}|~{3,})', line)
                if marker:
                    token = marker[1]
                    if fence is None:
                        fence = token
                    elif token[0] == fence[0] and len(token) >= len(fence):
                        fence = None
                elif fence is None and line.startswith('## '):
                    headings.append(line[3:])
            for name in entries:
                if not any(name in heading for heading in headings):
                    problems.append(dict(field='skill', problem='验证技能缺少入口 ' + name + ' 的二级标题说明'))
        except yaml.YAMLError:
            problems.append(dict(field='skill', problem='验证技能 SKILL.md 的 YAML 文件头格式不合格'))
        except (OSError, ValueError) as error:
            problems.append(dict(field='skill', problem=str(error)))
    external = Path(os.environ.get('SOP_BOARD_SKILLS_DIR', str(Path.home() / '.agents/skills'))).expanduser()
    for name, entry in entries.items():
        if not isinstance(entry, dict):
            continue
        driver = entry.get('driver')
        if driver == 'playwright-acceptance' or (driver is None and entry.get('surface') == 'web'):
            # Web 缺省与显式声明都用随包技能；SOP_BOARD_SKILLS_DIR 中的同名技能可覆盖。
            from .playwright import driver_skill
            candidate = driver_skill() / 'SKILL.md'
            if candidate.is_file():
                drivers[name] = str(candidate.resolve())
            else:
                problems.append(dict(field='entries.' + name + '.driver', problem='入口 ' + name + ' 的随包 Web 技能不存在'))
            continue
        if driver is None or not skill_name(driver):
            continue
        for root in (data / 'skills', external):
            candidate = root / entry['driver'] / 'SKILL.md'
            if candidate.is_file():
                drivers[name] = str(candidate.resolve())
                break
        else:
            problems.append(dict(field='entries.' + name + '.driver', problem='入口 ' + name + ' 的辅助技能不存在：' + entry['driver']))
    return path, drivers, problems
