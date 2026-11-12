"""项目：数据目录、config.yaml 校验、登记与按当前目录查找；以及本机操作者。

一个数据目录对应一个项目。旧格式的数据目录直接拒绝，不读取、不迁移。
"""
import json
import os
from pathlib import Path
import re
import subprocess
import threading
from urllib.parse import urlsplit

import yaml

from . import gitops
from .entries import validate_entries, verification_skills, skill_files

FORMAT = 'sop-board-project-6'
HOME = Path(os.environ.get('SOP_BOARD_HOME', str(Path.home() / '.sop-board'))).expanduser()
TOP_FIELDS = {'repos', 'identities', 'scenarios', 'skill', 'entries', 'adapter', 'snapshot_exclude', 'fixtures', 'agents',
              'shared_database'}
SHARED_FIELDS = {'host', 'port', 'dbname', 'user'}
ADAPTER_FIELDS = {'env', 'worktree', 'cleanup', 'ports', 'port_step', 'ready_timeout', 'services', 'service_timeout'}
SERVICE_NAME = re.compile(r'[a-z][a-z0-9_-]{0,63}\Z')
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
    services = validate_services(adapter.get('services', {}), problem) if 'services' in adapter else {}
    service_timeout = adapter.get('service_timeout', 60)
    if type(service_timeout) is not int or not 1 <= service_timeout <= 3600:
        problem('adapter.service_timeout', '须为 1～3600 的整数秒数')
        service_timeout = 60
    return dict(adapter, ports=ports, port_step=step, ready_timeout=timeout, services=services, service_timeout=service_timeout)


def validate_services(value, problem):
    """adapter.services：服务名 → {about: 业务说明, stoppable: 是否可停（缺省 false）}，按声明顺序保留。"""
    if not isinstance(value, dict) or not value:
        problem('adapter.services', '须为服务名到 {about, stoppable} 的非空对象；没有服务时不写这一项')
        return {}
    result = {}
    for name, item in value.items():
        field = f'adapter.services.{name}'
        if not isinstance(name, str) or not SERVICE_NAME.fullmatch(name):
            problem(field, '服务名须以小写字母开头，只含小写字母、数字、下划线与连字符，最长 64')
            continue
        if not isinstance(item, dict):
            problem(field, '须为含 about 的对象')
            continue
        for extra in item.keys() - {'about', 'stoppable'}:
            problem(f'{field}.{extra}', '格式外字段')
        about = item.get('about')
        if not isinstance(about, str) or not about.strip() or '\n' in about or len(about) > 200:
            problem(field + '.about', '须为一行业务说明（最长 200 字），供执行会话把场景起点对应到服务')
            continue
        stoppable = item.get('stoppable', False)
        if type(stoppable) is not bool:
            problem(field + '.stoppable', '须为布尔值')
            continue
        result[name] = dict(about=about.strip(), stoppable=stoppable)
    return result


def validate_shared(value, problem):
    """shared_database：{host, port（缺省 5432）, dbname, user}。密码只放 ~/.pgpass（或 PGPASSFILE），不写进配置。"""
    if not isinstance(value, dict):
        problem('shared_database', '须为含 host、port、dbname、user 的对象')
        return None
    for extra in value.keys() - SHARED_FIELDS:
        problem('shared_database.' + extra, '密码只放 ~/.pgpass，不写进 config.yaml' if extra in ('password', 'passwd', 'pass')
                else '格式外字段')
    result = {}
    for field in ('host', 'dbname', 'user'):
        item = value.get(field)
        if not isinstance(item, str) or not item.strip() or len(item) > 255 or any(c.isspace() for c in item.strip()):
            problem('shared_database.' + field, '须为不含空白的非空文字')
        else:
            result[field] = item.strip()
    port = value.get('port', 5432)
    if type(port) is not int or not 1 <= port <= 65535:
        problem('shared_database.port', '须为 1–65535 的端口')
    else:
        result['port'] = port
    return result if len(result) == 4 and not (value.keys() - SHARED_FIELDS) else None


_machines = {}


def machine(root):
    """机器：数据根目录下的 machine.json，保存生成一次的随机标识和显示名（缺省“操作者@主机名”）。"""
    root = Path(root).expanduser().resolve()
    cached = _machines.get(root)
    if cached:
        return cached
    path = root / 'machine.json'
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        import socket
        import uuid
        host = socket.gethostname().removesuffix('.local') or 'localhost'
        value = dict(id=uuid.uuid4().hex, name=f'{operator() or os.environ.get("USER") or "unknown"}@{host}')
        root.mkdir(parents=True, exist_ok=True)
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
                stream.write(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
        except FileExistsError:
            value = json.loads(path.read_text(encoding='utf-8'))
    except ValueError as error:
        raise ValueError(f'machine.json 无法解析：{path}') from error
    if not isinstance(value, dict) or not isinstance(value.get('id'), str) or not isinstance(value.get('name'), str):
        raise ValueError(f'machine.json 须含 id 与 name：{path}')
    _machines[root] = dict(id=value['id'], name=value['name'])
    return _machines[root]


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
    (data / 'config.yaml').write_text(yaml.safe_dump(dict(repos=repos, identities=identities), allow_unicode=True, sort_keys=False),
                                      encoding='utf-8')
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
    """按当前目录找到唯一项目的数据目录：位于某个项目的数据目录内（如运行目录）时用该项目，
    否则按所在仓库（含其 worktree）查找。"""
    here = Path(cwd or Path.cwd()).resolve()
    for data in registered_projects(data_root):
        if here == data.resolve() or here.is_relative_to(data.resolve()):
            return data
    found = gitops.toplevel(here)
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
        self.marker = marker
        self.config_path = self.data / 'config.yaml'
        self._publish = threading.Lock()
        self.reload()

    def binding(self):
        """project.json 里绑定的产品 {id, name}；没有绑定时返回 None。每次现读，看板进程也能看到 init 写入的绑定。"""
        try:
            value = json.loads(self.marker.read_text(encoding='utf-8')).get('product')
        except (OSError, ValueError, AttributeError):
            return None
        if isinstance(value, dict) and isinstance(value.get('id'), str) and value['id']:
            return dict(id=value['id'], name=value.get('name') or value['id'])
        return None

    def bind(self, product):
        """把产品绑定写进本机 project.json（改绑时覆盖）；不写产品仓库。"""
        value = json.loads(self.marker.read_text(encoding='utf-8'))
        value['product'] = dict(id=product['id'], name=product['name'])
        temporary = self.marker.with_suffix('.json.tmp')
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        os.replace(temporary, self.marker)

    def reload(self):
        """重新读取 config.yaml：先在局部算出全部字段，再一次换上。

        守护进程的多个请求线程共用这个对象并会同时调用 reload（两个检出同时 run prepare 时各自 env up）。
        边清空边填写会让并发的另一次读取看到半成品：仓库被当成重复声明、场景仓库别名“未声明”、共享库位置暂时为空。
        """
        values = self.read()
        with self._publish:
            self.__dict__.update(values)

    def read(self):
        """读取并校验 config.yaml，返回全部配置字段；不修改本对象。"""
        repos, identities, entries, adapter = {}, {}, {}, {}
        snapshot_exclude, fixtures, agents = {}, None, {}
        shared, shared_invalid = None, False
        scenario_repo, scenario_path = None, None
        problems = []

        def values(skill=None, driver_paths=None):
            return dict(repos=repos, identities=identities, entries=entries, adapter=adapter, snapshot_exclude=snapshot_exclude,
                        fixtures=fixtures, agents=agents, shared=shared, shared_invalid=shared_invalid,
                        scenario_repo=scenario_repo, scenario_path=scenario_path, skill=skill, driver_paths=driver_paths or {},
                        problems=problems)

        def problem(field, message):
            problems.append(dict(field=field, problem=message))
        try:
            raw = self.config_path.read_text(encoding='utf-8')
            config = strict_yaml(raw)
            if not isinstance(config, dict):
                raise ValueError('配置应为对象')
        except FileNotFoundError:
            problem('config.yaml', '尚未搭建：配置文件不存在')
            return values()
        except (OSError, ValueError, TypeError, yaml.YAMLError, RecursionError) as error:
            problem('config.yaml', '配置无法读取或格式不合格：' + str(error))
            return values()
        for field in config.keys() - TOP_FIELDS:
            problem(field, '格式外字段')
        if 'shared_database' in config:
            shared = validate_shared(config['shared_database'], problem)
            shared_invalid = shared is None
        declared = config.get('repos')
        if not isinstance(declared, dict) or not declared:
            problem('repos', '至少声明一个仓库')
            declared = {}
        for alias, value in declared.items():
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
        registered = config.get('identities')
        if not isinstance(registered, dict):
            problem('identities', '须为 别名 → 登记时规范化的 origin 身份（init 写入）')
            registered = {}
        for alias in registered.keys() - declared.keys():
            problem('identities.' + str(alias), '没有对应的仓库')
        for alias, identity in identities.items():
            if alias not in registered:
                problem('identities.' + alias, '缺少登记的 origin 身份')
            elif registered[alias] != identity:
                problem('identities.' + alias, f'仓库的 origin 身份 {identity} 与登记的 {registered[alias]} 不一致')
        location = config.get('scenarios')
        if not isinstance(location, dict):
            problem('scenarios', '须为含 repo、path 的对象')
        else:
            for field in location.keys() - {'repo', 'path'}:
                problem('scenarios.' + field, '格式外字段')
            alias, relative = location.get('repo'), location.get('path')
            if not isinstance(alias, str) or alias not in repos:
                problem('scenarios.repo', '须为已声明的仓库别名')
            elif (not isinstance(relative, str) or not relative.strip() or Path(relative).is_absolute()
                    or '..' in Path(relative).parts or not Path(relative).parts):
                problem('scenarios.path', '须为不含 .. 的仓内相对目录，不能等于仓库根目录')
            else:
                scenario_repo, scenario_path = alias, Path(relative)
        snapshot_exclude = relative_paths(config.get('snapshot_exclude', {}), 'snapshot_exclude', repos, problem)
        raw_entries = config.get('entries', {})
        problems.extend(validate_entries(raw_entries, repos))
        entries = raw_entries if isinstance(raw_entries, dict) else {}
        if 'agents' in config:
            agents = validate_agents(config['agents'], problem)
        if 'fixtures' in config:
            from .ledger import validate_fixtures
            fixtures = validate_fixtures(config['fixtures'], problem)
        adapter = validate_adapter(config.get('adapter', {}), problem) if 'adapter' in config else {}
        if 'adapter' not in config:
            problem('adapter', '须声明 adapter.env：看板守护进程用它按检出启动环境')
        skill, drivers, skill_problems = verification_skills(self.data, config.get('skill'), entries)
        problems.extend(skill_problems)
        return values(skill=skill, driver_paths=drivers)

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
