"""一个数据目录对应一个工作区；配置、仓库身份与多仓产品快照。"""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import shlex
from urllib.parse import urlsplit

import yaml

from .entries import validate_entries, verification_skills, skill_files
from .repositories import git, product_files


class WorkspaceNotFound(ValueError):
    """没有已登记工作区，调用入口可提供初始化指引。"""


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def origin_identity(raw):
    if '://' in raw:
        parsed = urlsplit(raw)
        if parsed.scheme not in ('https', 'http', 'ssh', 'git') or not parsed.hostname:
            raise ValueError('origin 必须是带仓库身份的远程地址')
        host, path = parsed.hostname, parsed.path.lstrip('/')
    else:
        match = re.fullmatch(r'(?:[^/@:]+@)?([^/:]+):(.+)', raw)
        if not match:
            raise ValueError('origin 必须是带仓库身份的远程地址')
        host, path = match.groups()
    path = path.rstrip('/').removesuffix('.git')
    if not path or any(part in ('', '.', '..') for part in path.split('/')):
        raise ValueError('origin 的仓库路径不合格')
    return host.lower() + '/' + path


class ConfigLoader(yaml.SafeLoader):
    def construct_mapping(self, node, deep=False):
        result = {}
        for key, value in node.value:
            if key.tag != 'tag:yaml.org,2002:str' or key.value in result:
                raise ValueError('配置字段须为不重复的文字')
            result[key.value] = self.construct_object(value, deep=True)
        return result


def workspace_name(name):
    if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}', name):
        raise ValueError('工作区名称须以字母或数字开头，只含字母、数字、点、下划线或连字符，最长 64 字符')
    return name


def repository(path, data):
    try:
        top = Path(os.fsdecode(git(path, 'rev-parse', '--show-toplevel')).strip()).resolve()
        if top != path or git(path, 'rev-parse', '--is-inside-work-tree').strip() != b'true':
            raise ValueError('不是 Git 工作树根目录')
    except (OSError, subprocess.SubprocessError) as error:
        raise ValueError('不是 Git 工作树根目录') from error
    try:
        origin = git(path, 'remote', 'get-url', 'origin').decode().strip()
    except (OSError, subprocess.SubprocessError) as error:
        raise ValueError('没有可用的 origin') from error
    try:
        identity = origin_identity(origin)
    except ValueError as error:
        raise ValueError('origin 不能规范化：' + str(error)) from error
    if data.is_relative_to(path):
        raise ValueError('看板数据目录不能位于仓库内')
    return identity


def initialize(name, directories, data=None):
    name = workspace_name(name)
    data = (Path(data) if data is not None else Path.home() / '.sop-board' / name).expanduser().resolve()
    problems, repos, identities, seen = [], {}, {}, set()
    if data.exists():
        problems.append('数据目录已存在：' + str(data))
    for value in directories:
        try:
            path = Path(value).expanduser().resolve()
            identity = repository(path, data)
            if path in seen:
                raise ValueError('同一仓库不能重复声明')
            seen.add(path)
            base = re.sub(r'[^a-z0-9-]+', '-', path.name.lower())
            base = re.sub('-+', '-', base)
            if not re.match('[a-z]', base):
                base = 'r-' + base
            alias, sequence = base, 2
            while alias in repos:
                alias, sequence = base + '-' + str(sequence), sequence + 1
            repos[alias], identities[alias] = str(path), identity
        except (OSError, ValueError) as error:
            problems.append(str(value) + '：' + str(error))
    if not repos and not problems:
        problems.append('至少声明一个仓库')
    if problems:
        raise ValueError('\n'.join(problems))
    # 所有输入核对完毕才创建目录；已存在的目录不会被覆盖。
    data.mkdir(parents=True, exist_ok=False)
    (data / 'workspace.json').write_text(json.dumps(dict(format='sop-board-workspace-v10', name=name), ensure_ascii=False) + '\n', encoding='utf-8')
    (data / 'config.yaml').write_text(yaml.safe_dump(dict(repos=repos), allow_unicode=True, sort_keys=False), encoding='utf-8')
    return data, identities


def locate_workspace():
    missing = '当前仓库没有登记的工作区；用 sop-board init <名称> <仓库目录>... 登记，或用 sop-board <名称> 指定已有工作区'
    try:
        root = Path(os.fsdecode(git(Path.cwd(), 'rev-parse', '--show-toplevel')).strip()).resolve()
    except subprocess.CalledProcessError as error:
        raise ValueError('无法识别当前产品仓库；请指定工作区名称或 --data <目录>') from error
    matches = []
    for config in sorted((Path.home() / '.sop-board').glob('*/config.yaml')):
        try:
            values = yaml.load(config.read_text(encoding='utf-8'), Loader=ConfigLoader)['repos']
            if not isinstance(values, dict):
                continue
            paths = [Path(value).expanduser().resolve() for value in values.values()
                     if isinstance(value, str) and (Path(value).is_absolute() or value.startswith('~/'))]
            if root in paths:
                matches.append(config.parent)
        except (OSError, ValueError, TypeError, KeyError, yaml.YAMLError, RecursionError):
            continue
    if not matches:
        raise WorkspaceNotFound(missing)
    if len(matches) != 1:
        raise ValueError('当前仓库属于多个工作区：' + '、'.join(p.name for p in matches) + '；请用 sop-board <名称> 或 sop-board check <名称> 指定')
    return matches[0]


class Workspace:
    def __init__(self, name=None, data=None):
        if data is None:
            data = Path.home() / '.sop-board' / workspace_name(name) if name is not None else locate_workspace()
        self.data = Path(data).expanduser().resolve()
        marker = self.data / 'workspace.json'
        try:
            registered = json.loads(marker.read_text(encoding='utf-8'))
            if not isinstance(registered, dict) or registered.get('format') != 'sop-board-workspace-v10':
                raise ValueError('不是 v10 工作区')
            self.name = workspace_name(registered.get('name'))
            if (self.data / 'board.sqlite3').exists():
                raise ValueError('旧数据库')
        except FileNotFoundError as error:
            raise WorkspaceNotFound('工作区登记不存在：' + str(marker)) from error
        except ValueError as error:
            raise ValueError('工作区登记不是当前格式；请指定当前格式的工作区数据目录，不读取或迁移旧数据') from error
        self.config_path = self.data / 'config.yaml'
        self.repos, self.identities, self.entries = {}, {}, {}
        self.credential_names = set()
        self.adapter, self.problems, self.config_digest = {}, [], ''
        self.skill, self.driver_paths, self.skill_problems = None, {}, []
        self.scenarios, self.scenario_repo = None, None
        self.reload()

    def setup_instructions(self):
        directory = shlex.quote(str(self.data))
        prompt = '按 sop-board guide 继续为工作区 ' + self.name + ' 搭建'
        return [f'Codex 新开会话：codex -C {directory} "{prompt}"',
                f'Claude Code：在当前会话执行 /add-dir {directory} 后，按 sop-board guide 继续搭建。']

    @property
    def configured(self):
        return not self.problems

    def reload(self):
        repos, identities, entries, adapter, problems = {}, {}, {}, {}, []
        scenarios, scenario_repo, skill = None, None, None
        def problem(field, text):
            problems.append(dict(field=field, problem=text))
        try:
            raw = self.config_path.read_text(encoding='utf-8')
            for token in yaml.scan(raw):
                if isinstance(token, (yaml.tokens.AnchorToken, yaml.tokens.AliasToken, yaml.tokens.TagToken)):
                    raise ValueError('配置不接受锚点、别名或显式标签')
            config = yaml.load(raw, Loader=ConfigLoader)
            if not isinstance(config, dict):
                raise ValueError('配置应为对象')
            for field in config.keys() - {'repos', 'scenarios', 'skill', 'entries', 'adapter'}:
                problem(field, '格式外字段')
            values = config.get('repos')
            if not isinstance(values, dict) or not values:
                problem('repos', '至少声明一个仓库')
            else:
                for alias, value in values.items():
                    if not isinstance(alias, str) or not re.fullmatch('[a-z][a-z0-9-]*', alias):
                        problem('repos', '仓库别名只能使用小写字母、数字与连字符，且以字母开头')
                        continue
                    if not isinstance(value, str) or not (Path(value).is_absolute() or value.startswith('~/')):
                        problem('repos.' + alias, '仓库路径须为绝对路径（可用 ~/ 开头）')
                        continue
                    try:
                        path = Path(value).expanduser().resolve()
                        identity = repository(path, self.data)
                        if path in repos.values():
                            raise ValueError('同一仓库不能重复声明')
                        repos[alias], identities[alias] = path, identity
                    except (OSError, ValueError) as error:
                        problem('repos.' + alias, str(error))
            location = config.get('scenarios')
            if not isinstance(location, dict):
                problem('scenarios', '须为含 repo、path 的对象')
            else:
                for field in location.keys() - {'repo', 'path'}:
                    problem('scenarios.' + field, '格式外字段')
                alias, relative = location.get('repo'), location.get('path')
                if not isinstance(alias, str) or alias not in repos:
                    problem('scenarios.repo', '须为已声明的仓库别名')
                elif not isinstance(relative, str) or not relative.strip() or Path(relative).is_absolute() or '..' in Path(relative).parts:
                    problem('scenarios.path', '须为不含 .. 的仓内相对目录')
                else:
                    path = (repos[alias] / relative).resolve()
                    if path == repos[alias] or not path.is_relative_to(repos[alias]):
                        problem('scenarios.path', '场景目录须位于声明仓库内，不能等于仓库根目录')
                    elif path.exists() and not path.is_dir():
                        problem('scenarios.path', '场景路径不是目录')
                    else:
                        scenarios, scenario_repo = path, alias
            skill = config.get('skill')
            entries = config.get('entries', {})
            problems.extend(validate_entries(entries))
            adapter = config.get('adapter', {})
            if not isinstance(adapter, dict):
                problem('adapter', '适配声明应为对象')
                adapter = {}
            for extra in adapter.keys() - {'worktree', 'cleanup', 'env', 'ports', 'ready_timeout'}:
                problem('adapter.' + extra, '格式外字段')
            for field in ('worktree', 'cleanup', 'env'):
                if field in ('worktree', 'cleanup') and field not in adapter:
                    continue
                argv = adapter.get(field)
                if not isinstance(argv, list) or not argv or not all(isinstance(x, str) and x.strip() and '\0' not in x for x in argv):
                    problem('adapter.' + field, '须为非空命令数组，不经过 shell')
            ports = adapter.get('ports')
            if not isinstance(ports, list) or not all(type(x) is int and 1 <= x <= 65535 for x in ports) or len(set(ports)) != len(ports):
                problem('adapter.ports', '须为不重复的端口数组，端口范围 1–65535')
            timeout = adapter.get('ready_timeout', 180)
            if type(timeout) is not int or timeout <= 0:
                problem('adapter.ready_timeout', '须为正整数秒数')
            adapter = dict(adapter, ready_timeout=timeout)
            tracked = {'config': raw}
            for field in ('worktree', 'cleanup', 'env'):
                argv = adapter.get(field, [])
                if isinstance(argv, list):
                    for arg in argv:
                        if isinstance(arg, str):
                            path = self.data / arg
                            if path.is_file() and path.resolve().is_relative_to(self.data):
                                tracked[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
            fingerprint = digest(tracked)
        except FileNotFoundError:
            problem('config.yaml', '尚未搭建：配置文件不存在')
            fingerprint = ''
        except (OSError, ValueError, TypeError, yaml.YAMLError, RecursionError):
            problem('config.yaml', '配置无法读取或格式不合格')
            fingerprint = ''
        entry_values = entries if isinstance(entries, dict) else {}
        skill, drivers, skill_problems = verification_skills(self.data, skill, entry_values)
        driver_problems = [p for p in problems if p['field'].startswith('entries.') and p['field'].endswith('.driver')]
        self.skill, self.driver_paths, self.skill_problems = skill, drivers, skill_problems + driver_problems
        problems.extend(skill_problems)
        changed = fingerprint != self.config_digest
        self.repos, self.identities, self.entries, self.adapter = repos, identities, entries if isinstance(entries, dict) else {}, adapter
        from .credentials import names
        self.credential_names.update(names(self.entries))
        self.scenarios, self.scenario_repo, self.problems, self.config_digest = scenarios, scenario_repo, problems, fingerprint
        return changed

    def skill_digest(self):
        if self.skill is None:
            raise ValueError('验证技能不存在')
        return digest(skill_files(self.skill.parent))

    def skill_changed(self, snapshot):
        try:
            return self.skill_digest() != snapshot['skill_digest']
        except (OSError, ValueError):
            return True

    def execution_entries(self):
        return {name: dict(entry, **({'driver_path': self.driver_paths[name]} if name in self.driver_paths else {}))
                for name, entry in self.entries.items()}

    def display_path(self, path):
        return self.scenario_repo + ':' + path.relative_to(self.repos[self.scenario_repo]).as_posix()

    def aliases(self, document):
        aliases = document.get('metadata', {}).get('repos', list(self.repos))
        if not isinstance(aliases, list) or not aliases or not all(isinstance(a, str) and a in self.repos for a in aliases) or len(set(aliases)) != len(aliases):
            raise ValueError('metadata.repos 须为不重复的已声明仓库别名数组')
        return aliases

    def scenario_path(self, batch):
        if not self.configured:
            raise ValueError('尚未搭建')
        if not isinstance(batch, str) or not batch or batch in ('.', '..') or '/' in batch or '\\' in batch or '\0' in batch:
            raise ValueError('批次名称不能含路径分隔符')
        path = self.scenarios / (batch + '.yaml')
        if path.is_symlink() or path.resolve().parent != self.scenarios:
            raise ValueError('场景路径越出配置目录')
        return path

    def product(self, aliases, directories=None):
        directories = directories or self.repos
        repos = {}
        dirty = []
        for alias in aliases:
            root = directories[alias]
            exclusion = self.scenarios.relative_to(self.repos[alias]) if self.scenarios.is_relative_to(self.repos[alias]) else None
            current = product_files(root, exclusion)
            committed = product_files(root, exclusion, head=True)
            commit = git(root, 'rev-parse', '--verify', 'HEAD').decode().strip()
            before = {v['path']: v for v in committed}
            after = {v['path']: v for v in current}
            changed = [p for p in sorted(before.keys() | after.keys()) if before.get(p) != after.get(p)]
            repos[alias] = dict(commit=commit, digest=digest(current))
            dirty += [alias + '/' + p for p in changed]
        return dict(repos=repos, product_digest=digest({a: r['digest'] for a, r in repos.items()}),
                    commit=', '.join(a + ':' + r['commit'] for a, r in repos.items()), dirty=bool(dirty), changed=dirty)

    def public(self):
        return dict(name=self.name, configured=self.configured,
                    repositories=[dict(alias=a, identity=self.identities[a]) for a in self.repos])
