"""一个数据目录对应一个工作区；配置、仓库身份与多仓产品快照。"""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from urllib.parse import urlsplit

import yaml

from .entries import validate_entries
from .repositories import git, product_files


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


class Workspace:
    def __init__(self, root, data=None):
        self.root = Path(root).expanduser().resolve()
        if not self.root.is_dir():
            raise ValueError('工作区目录不存在')
        self.name = self.root.name or 'workspace'
        if data is None:
            data = Path.home() / '.sop-board' / self.name
            marker = data / 'workspace.json'
            if marker.is_file():
                registered = json.loads(marker.read_text(encoding='utf-8'))
                if registered.get('root') != str(self.root):
                    self.name += '-' + hashlib.sha256(os.fsencode(self.root)).hexdigest()[:8]
                    data = data.with_name(self.name)
        self.data = Path(data).expanduser().resolve()
        self.config_path = self.data / 'config.yaml'
        marker = self.data / 'workspace.json'
        if (self.data / 'board.sqlite3').exists():
            raise ValueError('不读取旧数据目录，请为工作区选择新的数据目录')
        if marker.exists():
            registered = json.loads(marker.read_text(encoding='utf-8'))
            if registered.get('format') != 'sop-board-workspace-v9' or registered.get('root') != str(self.root):
                raise ValueError('数据目录已属于其他工作区或数据格式不合格')
            self.name = registered['name']
        elif (self.data / 'workspace.sqlite3').exists():
            raise ValueError('数据目录缺少工作区登记，不能读取其中数据库')
        self.data.mkdir(parents=True, exist_ok=True)
        if not marker.exists():
            with marker.open('x', encoding='utf-8') as stream:
                json.dump(dict(format='sop-board-workspace-v9', root=str(self.root), name=self.name), stream, ensure_ascii=False)
        self.repos, self.identities, self.entries = {}, {}, {}
        self.adapter, self.problems, self.config_digest = {}, [], ''
        self.scenarios = None
        self.reload()

    @property
    def configured(self):
        return not self.problems

    def reload(self):
        repos, identities, entries, adapter, problems = {}, {}, {}, {}, []
        scenarios = None
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
            for field in config.keys() - {'repos', 'scenarios', 'entries', 'adapter'}:
                problem(field, '格式外字段')
            values = config.get('repos')
            if not isinstance(values, dict) or not values:
                problem('repos', '至少声明一个仓库')
            else:
                for alias, relative in values.items():
                    if not isinstance(alias, str) or not re.fullmatch('[a-z][a-z0-9-]*', alias):
                        problem('repos', '仓库别名只能使用小写字母、数字与连字符，且以字母开头')
                        continue
                    if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
                        problem('repos.' + alias, '仓库路径须为相对工作区的路径')
                        continue
                    path = (self.root / relative).resolve()
                    if not path.is_relative_to(self.root):
                        problem('repos.' + alias, '仓库路径不能越出工作区')
                        continue
                    try:
                        top = Path(os.fsdecode(git(path, 'rev-parse', '--show-toplevel')).strip()).resolve()
                        if top != path or git(path, 'rev-parse', '--is-inside-work-tree').strip() != b'true':
                            raise ValueError('不是 Git 工作区根目录')
                        identity = origin_identity(git(path, 'remote', 'get-url', 'origin').decode().strip())
                        if self.data.is_relative_to(path):
                            raise ValueError('看板数据目录不能位于仓库内')
                        if path in repos.values():
                            raise ValueError('同一仓库不能重复声明')
                        repos[alias], identities[alias] = path, identity
                    except (OSError, subprocess.SubprocessError, ValueError) as error:
                        problem('repos.' + alias, str(error) if isinstance(error, ValueError) else '无法读取 Git 仓库或 origin')
            relative = config.get('scenarios')
            if not isinstance(relative, str) or not relative.strip() or Path(relative).is_absolute():
                problem('scenarios', '须为相对工作区的场景目录')
            else:
                scenarios = (self.root / relative).resolve()
                if not any(scenarios != path and scenarios.is_relative_to(path) for path in repos.values()):
                    problem('scenarios', '场景目录须位于一个已声明仓库内，不能等于仓库根目录')
                elif scenarios.exists() and not scenarios.is_dir():
                    problem('scenarios', '场景路径不是目录')
            entries = config.get('entries', {})
            problems.extend(validate_entries(entries))
            adapter = config.get('adapter', {})
            if not isinstance(adapter, dict):
                problem('adapter', '适配声明应为对象')
                adapter = {}
            for extra in adapter.keys() - {'prepare', 'env', 'ports', 'ready_timeout'}:
                problem('adapter.' + extra, '格式外字段')
            for field in ('prepare', 'env'):
                if field == 'prepare' and field not in adapter:
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
            for field in ('prepare', 'env'):
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
        changed = fingerprint != self.config_digest
        self.repos, self.identities, self.entries, self.adapter = repos, identities, entries if isinstance(entries, dict) else {}, adapter
        self.scenarios, self.problems, self.config_digest = scenarios, problems, fingerprint
        return changed

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
