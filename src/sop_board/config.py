"""项目：数据目录、config.yaml 校验、登记与按当前目录查找；以及本机操作者。

一个数据目录对应一个项目。旧格式的数据目录直接拒绝，不读取、不迁移。
"""
import json
import os
from pathlib import Path
import re
import subprocess
from urllib.parse import urlsplit

import yaml

from . import gitops
from .entries import validate_entries, verification_skills, skill_files

FORMAT = 'sop-board-project-6'
HOME = Path(os.environ.get('SOP_BOARD_HOME', str(Path.home() / '.sop-board'))).expanduser()
TOP_FIELDS = {'repos', 'scenarios', 'skill', 'entries', 'adapter', 'snapshot_exclude', 'fixtures', 'agents'}
ADAPTER_FIELDS = {'env', 'worktree', 'cleanup', 'ports', 'port_step', 'ready_timeout'}
AGENT_KINDS = ('default', 'run', 'fix', 'distill', 'merge', 'author', 'draft', 'atlas')
EFFORTS = ('low', 'medium', 'high', 'xhigh', 'max')


class ProjectNotFound(ValueError):
    """没有已登记的项目；调用方给出 init 指引。"""


class ProjectFormat(ValueError):
    """数据目录不是当前格式（例如 v9 工作区），不读取、不迁移。"""


def name_problem(name):
    if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}', name):
        return '项目名称须以字母或数字开头，只含字母、数字、点、下划线或连字符，最长 64 字符'
    return None


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


def repository(path, data):
    """核对仓库根目录并返回规范化的 origin 身份。"""
    found = gitops.toplevel(path)
    if not found or found[0] != path:
        raise ValueError('不是 Git 工作树根目录')
    try:
        origin = gitops.text(path, 'remote', 'get-url', 'origin')
    except (OSError, subprocess.SubprocessError) as error:
        raise ValueError('没有可用的 origin') from error
    try:
        identity = origin_identity(origin)
    except ValueError as error:
        raise ValueError('origin 不能规范化：' + str(error)) from error
    if data.is_relative_to(path):
        raise ValueError('数据目录不能位于仓库内')
    return identity


class StrictLoader(yaml.SafeLoader):
    def construct_mapping(self, node, deep=False):
        result = {}
        for key, value in node.value:
            if key.tag != 'tag:yaml.org,2002:str' or key.value in result:
                raise ValueError('配置字段须为不重复的文字')
            result[key.value] = self.construct_object(value, deep=True)
        return result


def strict_yaml(raw):
    for token in yaml.scan(raw):
        if isinstance(token, (yaml.tokens.AnchorToken, yaml.tokens.AliasToken, yaml.tokens.TagToken)):
            raise ValueError('不接受锚点、别名或显式标签')
    return yaml.load(raw, Loader=StrictLoader)


def relative_paths(value, field, repos, problem):
    """别名 → 仓内相对路径数组；返回别名 → Path 列表。"""
    result = {}
    if not isinstance(value, dict):
        problem(field, '须为仓库别名到仓内相对路径数组的对象')
        return result
    for alias, paths in value.items():
        name = f'{field}.{alias}'
        if alias not in repos:
            problem(name, '须为已声明的仓库别名')
            continue
        if not isinstance(paths, list):
            problem(name, '须为仓内相对路径数组')
            continue
        selected = []
        for item in paths:
            if (not isinstance(item, str) or not item.strip() or '\0' in item or Path(item).is_absolute()
                    or '..' in Path(item).parts or not Path(item).parts):
                problem(name, '须为非空仓内相对路径，不能含 .. 或等于仓库根目录')
            elif Path(item) not in selected:
                selected.append(Path(item))
        result[alias] = selected
    return result


def argv_ok(value):
    return isinstance(value, list) and bool(value) and all(isinstance(x, str) and x.strip() and '\0' not in x for x in value)


def validate_agents(value, problem):
    if not isinstance(value, dict):
        problem('agents', '须为工作类型到代理声明的对象')
        return {}
    result = {}
    for kind, item in value.items():
        field = f'agents.{kind}'
        if kind not in AGENT_KINDS:
            problem(field, '工作类型应为 ' + '、'.join(AGENT_KINDS))
            continue
        if not isinstance(item, dict) or item.get('runner') not in ('codex', 'claude'):
            problem(field + '.runner', '应为 codex 或 claude')
            continue
        for extra in item.keys() - {'runner', 'model', 'effort', 'args'}:
            problem(f'{field}.{extra}', '格式外字段')
        if 'model' in item and (not isinstance(item['model'], str) or not item['model'].strip()):
            problem(field + '.model', '须为非空文字')
        if 'effort' in item and item['effort'] not in EFFORTS:
            problem(field + '.effort', '应为 ' + '、'.join(EFFORTS))
        if 'args' in item and (not isinstance(item['args'], list) or not all(isinstance(a, str) and a for a in item['args'])):
            problem(field + '.args', '须为非空文字数组')
        result[kind] = dict(item)
    return result


def validate_adapter(adapter, problem):
    if not isinstance(adapter, dict):
        problem('adapter', '须为对象')
        return {}
    for extra in adapter.keys() - ADAPTER_FIELDS:
        problem('adapter.' + extra, '格式外字段')
    if not argv_ok(adapter.get('env')):
        problem('adapter.env', '须为非空命令数组，不经过 shell')
    for field in ('worktree', 'cleanup'):
        if field in adapter and not argv_ok(adapter[field]):
            problem('adapter.' + field, '须为非空命令数组，不经过 shell')
    if ('worktree' in adapter) != ('cleanup' in adapter):
        problem('adapter.worktree', 'worktree 与 cleanup 须同时声明')
    ports = adapter.get('ports', [])
    if not isinstance(ports, list) or not all(type(x) is int and 1 <= x <= 65535 for x in ports) or len(set(ports)) != len(ports):
        problem('adapter.ports', '须为不重复的端口数组，端口范围 1–65535')
        ports = []
    step = adapter.get('port_step', 0)
    if type(step) is not int or step < 0 or step > 10000:
        problem('adapter.port_step', '须为 0～10000 的整数；0 表示各检出共用端口、轮流使用')
        step = 0
    timeout = adapter.get('ready_timeout', 180)
    if type(timeout) is not int or timeout <= 0:
        problem('adapter.ready_timeout', '须为正整数秒数')
        timeout = 180
    return dict(adapter, ports=ports, port_step=step, ready_timeout=timeout)


def root_of(data_root=None):
    """项目数据目录的上级目录：--data 指定，缺省为 SOP_BOARD_HOME（~/.sop-board）。"""
    return Path(data_root).expanduser().resolve() if data_root else HOME.resolve()


def initialize(name, directories, data_root=None):
    """登记项目：核对全部仓库后才创建 <根目录>/<名称>，已存在的目录不覆盖。"""
    problem = name_problem(name)
    if problem:
        raise ValueError(problem)
    data = root_of(data_root) / name
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
            base = re.sub('-+', '-', re.sub(r'[^a-z0-9-]+', '-', path.name.lower())).strip('-')
            if not re.match('[a-z]', base):
                base = 'r-' + base
            alias, sequence = base, 2
            while alias in repos:
                alias, sequence = f'{base}-{sequence}', sequence + 1
            repos[alias], identities[alias] = str(path), identity
        except (OSError, ValueError) as error:
            problems.append(f'{value}：{error}')
    if not repos and not problems:
        problems.append('至少声明一个仓库')
    if problems:
        raise ValueError('\n'.join(problems))
    data.mkdir(parents=True, exist_ok=False)
    (data / 'project.json').write_text(json.dumps(dict(format=FORMAT, name=name), ensure_ascii=False) + '\n', encoding='utf-8')
    (data / 'config.yaml').write_text(yaml.safe_dump(dict(repos=repos), allow_unicode=True, sort_keys=False), encoding='utf-8')
    return data, identities


def registered_projects(data_root=None):
    """根目录下全部当前格式的项目数据目录。"""
    result = []
    for marker in sorted(root_of(data_root).glob('*/project.json')):
        try:
            value = json.loads(marker.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        if isinstance(value, dict) and value.get('format') == FORMAT:
            result.append(marker.parent)
    return result


def configured_repos(data):
    """不做完整校验，只读出 repos 的绝对路径，用于按当前目录查找项目。"""
    try:
        values = strict_yaml((data / 'config.yaml').read_text(encoding='utf-8')).get('repos')
    except (OSError, ValueError, AttributeError, yaml.YAMLError, RecursionError):
        return {}
    if not isinstance(values, dict):
        return {}
    return {alias: Path(value).expanduser().resolve() for alias, value in values.items()
            if isinstance(alias, str) and isinstance(value, str) and (Path(value).is_absolute() or value.startswith('~/'))}


def locate(cwd=None, data_root=None):
    """按当前目录所在仓库（含其 worktree）找到唯一项目的数据目录。"""
    found = gitops.toplevel(cwd or Path.cwd())
    if not found:
        raise ProjectNotFound('当前目录不在 Git 仓库中；用 --project <名称> 或 --data <目录> 指定项目')
    top, common = found
    matches = []
    for data in registered_projects(data_root):
        for path in configured_repos(data).values():
            if path == top or gitops.common_dir(path) == common:
                matches.append(data)
                break
    if not matches:
        raise ProjectNotFound('当前仓库没有登记的项目；用 sop-board init <名称> <仓库目录>... 登记')
    if len(matches) > 1:
        raise ValueError('当前仓库属于多个项目：' + '、'.join(p.name for p in matches) + '；用项目名称指定')
    return matches[0]


def resolve_data(project=None, data_root=None, cwd=None):
    """项目数据目录：<根目录>/<名称>；未给名称时按当前目录所在仓库查找。"""
    if project:
        problem = name_problem(project)
        if problem:
            raise ValueError(problem)
        return root_of(data_root) / project
    return locate(cwd, data_root)


class Project:
    """项目配置。problems 为空表示配置合格；不合格时仍可读取已能确定的部分。"""

    def __init__(self, data):
        self.data = Path(data).expanduser().resolve()
        marker = self.data / 'project.json'
        try:
            registered = json.loads(marker.read_text(encoding='utf-8'))
        except FileNotFoundError as error:
            if (self.data / 'workspace.json').exists() or (self.data / 'workspace.sqlite3').exists():
                raise ProjectFormat('这是 v9 工作区数据目录，不读取也不迁移；用 sop-board init 新建项目') from error
            raise ProjectNotFound('项目不存在：' + str(self.data)) from error
        except ValueError as error:
            raise ProjectFormat('project.json 无法解析：' + str(marker)) from error
        if not isinstance(registered, dict) or registered.get('format') != FORMAT or name_problem(registered.get('name')):
            raise ProjectFormat('数据目录不是当前格式（' + FORMAT + '）；用 sop-board init 新建项目')
        self.name = registered['name']
        self.config_path = self.data / 'config.yaml'
        self.reload()

    def reload(self):
        self.repos, self.identities, self.entries, self.adapter = {}, {}, {}, {}
        self.snapshot_exclude, self.fixtures, self.agents = {}, None, {}
        self.scenario_repo, self.scenario_path = None, None
        self.skill, self.driver_paths = None, {}
        problems = []

        def problem(field, message):
            problems.append(dict(field=field, problem=message))
        try:
            raw = self.config_path.read_text(encoding='utf-8')
            config = strict_yaml(raw)
            if not isinstance(config, dict):
                raise ValueError('配置应为对象')
        except FileNotFoundError:
            self.problems = [dict(field='config.yaml', problem='尚未搭建：配置文件不存在')]
            return
        except (OSError, ValueError, TypeError, yaml.YAMLError, RecursionError) as error:
            self.problems = [dict(field='config.yaml', problem='配置无法读取或格式不合格：' + str(error))]
            return
        for field in config.keys() - TOP_FIELDS:
            problem(field, '格式外字段')
        values = config.get('repos')
        if not isinstance(values, dict) or not values:
            problem('repos', '至少声明一个仓库')
            values = {}
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
                if path in self.repos.values():
                    raise ValueError('同一仓库不能重复声明')
                self.repos[alias], self.identities[alias] = path, identity
            except (OSError, ValueError) as error:
                problem('repos.' + alias, str(error))
        location = config.get('scenarios')
        if not isinstance(location, dict):
            problem('scenarios', '须为含 repo、path 的对象')
        else:
            for field in location.keys() - {'repo', 'path'}:
                problem('scenarios.' + field, '格式外字段')
            alias, relative = location.get('repo'), location.get('path')
            if not isinstance(alias, str) or alias not in self.repos:
                problem('scenarios.repo', '须为已声明的仓库别名')
            elif (not isinstance(relative, str) or not relative.strip() or Path(relative).is_absolute()
                    or '..' in Path(relative).parts or not Path(relative).parts):
                problem('scenarios.path', '须为不含 .. 的仓内相对目录，不能等于仓库根目录')
            else:
                self.scenario_repo, self.scenario_path = alias, Path(relative)
        self.snapshot_exclude = relative_paths(config.get('snapshot_exclude', {}), 'snapshot_exclude', self.repos, problem)
        entries = config.get('entries', {})
        problems.extend(validate_entries(entries, self.repos))
        self.entries = entries if isinstance(entries, dict) else {}
        if 'agents' in config:
            self.agents = validate_agents(config['agents'], problem)
        if 'fixtures' in config:
            from .ledger import validate_fixtures
            self.fixtures = validate_fixtures(config['fixtures'], problem)
        self.adapter = validate_adapter(config.get('adapter', {}), problem) if 'adapter' in config else {}
        if 'adapter' not in config:
            problem('adapter', '须声明 adapter.env：看板守护进程用它按检出启动环境')
        skill, drivers, skill_problems = verification_skills(self.data, config.get('skill'), self.entries)
        self.skill, self.driver_paths = skill, drivers
        problems.extend(skill_problems)
        self.problems = problems

    @property
    def configured(self):
        return not self.problems

    def require(self):
        if self.problems:
            raise ValueError('项目配置不合格：' + '；'.join(f"{p['field']}：{p['problem']}" for p in self.problems))
        return self

    def skill_digest(self):
        if self.skill is None:
            raise ValueError('验证技能不存在')
        return gitops.digest(skill_files(self.skill.parent))

    def exclusions(self, extra=None):
        """快照排除：声明的排除项与场景目录；extra 为额外的 别名 → 路径。"""
        result = {alias: list(paths) for alias, paths in self.snapshot_exclude.items()}
        if self.scenario_repo:
            result.setdefault(self.scenario_repo, []).append(self.scenario_path)
        for alias, paths in (extra or {}).items():
            result.setdefault(alias, []).extend(Path(p) for p in paths)
        return result

    def serve_command(self):
        command = f'sop-board serve {self.name}'
        if self.data.parent != HOME.resolve():
            command += f' --data {self.data.parent}'
        return command

    def public(self):
        return dict(name=self.name, configured=self.configured,
                    repositories=[dict(alias=a, identity=self.identities[a]) for a in self.repos])


def operator_path():
    return HOME / 'operator'


def operator():
    """本机操作者：设置过的姓名，其次 git config user.name。"""
    path = operator_path()
    if path.is_file():
        value = path.read_text(encoding='utf-8').strip()
        if value:
            return value
    return gitops.user_name()


def set_operator(name):
    if not isinstance(name, str) or not name.strip() or len(name) > 64 or '\n' in name:
        raise ValueError('操作者姓名须为非空单行文字，最长 64 字符')
    HOME.mkdir(parents=True, exist_ok=True)
    operator_path().write_text(name.strip() + '\n', encoding='utf-8')
    return name.strip()


def require_operator():
    name = operator()
    if not name:
        raise ValueError('本机还没有操作者姓名：运行 sop-board operator set <姓名>，或设置 git config --global user.name')
    return name
