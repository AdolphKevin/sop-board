"""工作区配置中的真实入口声明；不读取仓库内的旧声明。"""
import hashlib
import os
from pathlib import Path
import re
import stat

import yaml


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
        if 'driver' in entry and not skill_name(entry['driver']):
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
        if not isinstance(entry, dict) or 'driver' not in entry or not skill_name(entry['driver']):
            continue
        for root in (data / 'skills', external):
            candidate = root / entry['driver'] / 'SKILL.md'
            if candidate.is_file():
                drivers[name] = str(candidate.resolve())
                break
        else:
            problems.append(dict(field='entries.' + name + '.driver', problem='入口 ' + name + ' 的辅助技能不存在：' + entry['driver']))
    return path, drivers, problems
