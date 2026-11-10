"""检出：一个项目各仓库的一套工作副本（main 或 git worktree），按 git common dir 识别。

main 是 config.yaml 登记的路径，槽位固定为 0。worktree 检出由当前目录所在仓库及同一父目录下
其他已登记仓库的 worktree 组成，缺的仓库回落到 main。场景文件、快照、结果、环境、端口与浏览器都按检出隔离。
"""
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

from . import gitops
from .store import dumps, now


@dataclass
class Checkout:
    id: int
    name: str
    slot: int
    kind: str
    paths: dict = field(default_factory=dict)
    env: dict = field(default_factory=dict)

    def offset(self, project):
        return self.slot * project.adapter.get('port_step', 0)

    def scenarios_dir(self, project):
        if not project.scenario_repo:
            return None
        return self.paths[project.scenario_repo] / project.scenario_path

    def public(self, *, local):
        result = dict(id=self.id, name=self.name, slot=self.slot, kind=self.kind)
        if local:
            result['paths'] = {alias: str(path) for alias, path in self.paths.items()}
        return result


def from_row(row):
    return Checkout(row['id'], row['name'], row['slot'], row['kind'],
                    {alias: Path(path) for alias, path in json.loads(row['paths']).items()}, json.loads(row['env']))


def active(db):
    return [from_row(row) for row in db.execute('SELECT * FROM checkouts WHERE removed_at IS NULL ORDER BY slot')]


def by_name(db, name):
    row = db.execute('SELECT * FROM checkouts WHERE name=? AND removed_at IS NULL', (name,)).fetchone()
    if row is None:
        raise ValueError(f'检出不存在：{name}；用 sop-board checkout list 查看')
    return from_row(row)


def by_id(db, identifier):
    row = db.execute('SELECT * FROM checkouts WHERE id=?', (identifier,)).fetchone()
    if row is None:
        raise ValueError(f'检出不存在：#{identifier}')
    return from_row(row)


def ensure_main(db, project):
    """登记或更新 main 检出，使其路径与 config.yaml 一致。"""
    paths = dumps({alias: str(path) for alias, path in project.repos.items()})
    row = db.execute("SELECT * FROM checkouts WHERE kind='main'").fetchone()
    if row is None:
        db.execute("INSERT INTO checkouts(name,slot,kind,paths,created_at) VALUES ('main',0,'main',?,?)", (paths, now()))
    elif row['paths'] != paths:
        db.execute('UPDATE checkouts SET paths=? WHERE id=?', (paths, row['id']))
    return from_row(db.execute("SELECT * FROM checkouts WHERE kind='main'").fetchone())


def _name(value):
    base = re.sub('-+', '-', re.sub(r'[^A-Za-z0-9._-]+', '-', value)).strip('-.') or 'worktree'
    return base[:48]


def _free_name(db, base):
    name, sequence = base, 2
    while db.execute('SELECT 1 FROM checkouts WHERE name=?', (name,)).fetchone() or name == 'main':
        name, sequence = f'{base}-{sequence}', sequence + 1
    return name


def _free_slot(db):
    used = {row[0] for row in db.execute('SELECT slot FROM checkouts')}
    slot = 1
    while slot in used:
        slot += 1
    return slot


def common_dirs(project):
    return {alias: gitops.common_dir(path) for alias, path in project.repos.items()}


def discover(project, cwd):
    """从当前目录推导检出的仓库路径。返回 (kind, 名称候选, 别名 → 路径)；不在项目仓库内时返回 None。"""
    found = gitops.toplevel(cwd)
    if not found:
        return None
    top, common = found
    commons = common_dirs(project)
    owner = next((alias for alias, value in commons.items() if value == common), None)
    if owner is None:
        return None
    if top == project.repos[owner]:
        return 'main', 'main', dict(project.repos)
    paths = dict(project.repos)
    paths[owner] = top
    siblings = 0
    parent = top.parent
    for alias, value in commons.items():
        if alias == owner or value is None:
            continue
        for child in sorted(parent.iterdir()) if parent.is_dir() else []:
            if child == top or not child.is_dir() or child.is_symlink() or child == project.repos[alias]:
                continue
            located = gitops.toplevel(child) if (child / '.git').exists() else None
            if located and located[0] == child.resolve() and located[1] == value:
                paths[alias] = child.resolve()
                siblings += 1
                break
    base = parent.name if siblings else top.name
    return 'worktree', _name(base), paths


def register(db, project, kind, base, paths):
    """按当前仓库路径复用已登记的 worktree 检出；新的检出分配名称与槽位。"""
    if kind == 'main':
        return ensure_main(db, project)
    encoded = {alias: str(path) for alias, path in paths.items()}
    worktree_paths = {alias: path for alias, path in encoded.items() if Path(path) != project.repos.get(alias)}
    for checkout in active(db):
        if checkout.kind != 'worktree':
            continue
        current = {alias: str(path) for alias, path in checkout.paths.items()}
        if any(current.get(alias) == path for alias, path in worktree_paths.items()):
            if current != encoded:
                db.execute('UPDATE checkouts SET paths=? WHERE id=?', (dumps(encoded), checkout.id))
            return by_id(db, checkout.id)
    name, slot = _free_name(db, base), _free_slot(db)
    cursor = db.execute("INSERT INTO checkouts(name,slot,kind,paths,created_at) VALUES (?,?,'worktree',?,?)",
                        (name, slot, dumps(encoded), now()))
    return by_id(db, cursor.lastrowid)


def current(store, project, *, name=None, cwd=None):
    """命令所在的检出：显式名称优先，其次按当前目录识别，都不在项目仓库内时为 main。"""
    with store.transaction() as db:
        main = ensure_main(db, project)
        if name:
            return by_name(db, name)
        found = discover(project, cwd or Path.cwd())
        if found is None:
            return main
        return register(db, project, *found)


def remove(db, checkout):
    if checkout.kind == 'main':
        raise ValueError('main 检出不能移除')
    db.execute('UPDATE checkouts SET removed_at=?, slot=?, name=? WHERE id=?',
               (now(), -checkout.id, f'{checkout.name}@{checkout.id}', checkout.id))


# ---- 由 adapter.worktree / cleanup 建立与清理 worktree 检出 ----

def validate_path(project, alias, path, branch):
    """返回的路径须为该仓库的 worktree 根目录、共用 git common dir、检出指定分支。"""
    path = Path(path).expanduser().resolve()
    found = gitops.toplevel(path)
    if not found or found[0] != path:
        raise ValueError(f'{alias} 的工作树路径不是 Git 工作树根目录：{path}')
    if found[1] != gitops.common_dir(project.repos[alias]):
        raise ValueError(f'{alias} 的工作树与登记仓库不共用 git common dir')
    if gitops.branch(path) != branch:
        raise ValueError(f'{alias} 的工作树没有检出分支 {branch}')
    listed = os.fsdecode(gitops.git(project.repos[alias], 'worktree', 'list', '--porcelain'))
    if f'worktree {path}' not in listed.splitlines():
        raise ValueError(f'{alias} 的工作树不在 git worktree list 中')
    return path


def _key(alias):
    return alias.upper().replace('-', '_')


def _adapter_env(project, root, branch, aliases, *, base=None, worktrees=None):
    """adapter.worktree / cleanup 的输入变量（沿用既有适配契约的变量名）。"""
    env = {k: v for k, v in os.environ.items() if not k.startswith('SOP_')}
    env.update(SOP_TASK_DIR=str(root), SOP_FIX_BRANCH=branch, SOP_REPOS=','.join(aliases))
    for alias in aliases:
        env['SOP_BASE_' + _key(alias)] = str(project.repos[alias])
    if base:
        env['SOP_BASE_COMMITS'] = json.dumps({alias: base for alias in aliases}, ensure_ascii=False)
    if worktrees is not None:
        env['SOP_WORKTREES'] = json.dumps({a: str(p) for a, p in worktrees.items()}, ensure_ascii=False)
    return env


def _run_adapter(project, field, env, log):
    result = subprocess.run(project.adapter[field], cwd=project.data, env=env, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            timeout=project.adapter.get('ready_timeout', 180))
    if log:
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(result.stdout + result.stderr, encoding='utf-8')
    if result.returncode:
        raise ValueError(f'adapter.{field} 失败（退出码 {result.returncode}）：{result.stderr.strip()[-400:]}')
    return result.stdout


def build(project, store, name, *, aliases=None, base=None, log=None):
    """建立新的 worktree 检出：有 adapter.worktree 时由它建立，否则 git worktree add 新分支。"""
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,47}', name) or name == 'main':
        raise ValueError('检出名称须以字母或数字开头，只含字母、数字、点、下划线或连字符，最长 48 字符，不能是 main')
    aliases = list(aliases or project.repos)
    unknown = [alias for alias in aliases if alias not in project.repos]
    if unknown:
        raise ValueError('未登记的仓库别名：' + '、'.join(unknown))
    with store.connection() as db:
        if db.execute('SELECT 1 FROM checkouts WHERE name=? AND removed_at IS NULL', (name,)).fetchone():
            raise ValueError(f'检出 {name} 已存在')
    branch = 'sop-board/' + name
    root = project.data / 'checkouts' / name
    extra = {}
    if project.adapter.get('worktree'):
        root.mkdir(parents=True, exist_ok=True)
        raw = _run_adapter(project, 'worktree', _adapter_env(project, root, branch, aliases, base=base), log)
        try:
            output = json.loads(raw.strip().splitlines()[-1])
            returned, extra = output['repos'], output.get('env', {})
            if (not isinstance(returned, dict) or set(returned) != set(aliases) or not isinstance(extra, dict)
                    or not all(re.fullmatch('[A-Za-z_][A-Za-z0-9_]*', k) and isinstance(v, str) and not k.startswith('SOP_')
                               for k, v in extra.items())):
                raise ValueError()
        except (ValueError, KeyError, IndexError, TypeError) as error:
            raise ValueError('adapter.worktree 的最后一行须为 {"repos": {别名: 路径}, "env": {变量名: 文字值}}，'
                             '覆盖全部请求的仓库，env 不能使用 SOP_ 开头的变量') from error
        paths = {alias: validate_path(project, alias, path, branch) for alias, path in returned.items()}
    else:
        root.mkdir(parents=True, exist_ok=False)
        paths = {}
        for alias in aliases:
            target = root / alias
            gitops.git(project.repos[alias], 'worktree', 'add', '-b', branch, str(target), base or 'HEAD', timeout=300)
            paths[alias] = validate_path(project, alias, target, branch)
    full = dict(project.repos)
    full.update(paths)
    with store.transaction() as db:
        ensure_main(db, project)
        slot = _free_slot(db)
        cursor = db.execute("INSERT INTO checkouts(name,slot,kind,paths,env,created_at) VALUES (?,?,'worktree',?,?,?)",
                            (name, slot, dumps({a: str(p) for a, p in full.items()}), dumps(extra), now()))
        return by_id(db, cursor.lastrowid)


def destroy(project, store, checkout, *, log=None):
    """清理由 build 建立的 worktree 检出；只处理已核对的路径，失败保留现场。"""
    if checkout.kind != 'worktree':
        raise ValueError('只能移除 worktree 检出')
    owned = {alias: path for alias, path in checkout.paths.items() if path != project.repos.get(alias)}
    if project.adapter.get('cleanup'):
        root = project.data / 'checkouts' / checkout.name
        _run_adapter(project, 'cleanup', _adapter_env(project, root, 'sop-board/' + checkout.name, sorted(owned),
                                                      worktrees=owned), log)
    else:
        for alias, path in owned.items():
            gitops.git(project.repos[alias], 'worktree', 'remove', str(path), timeout=120)
        root = project.data / 'checkouts' / checkout.name
        if root.is_dir() and not any(root.iterdir()):
            shutil.rmtree(root)
    leftovers = [str(path) for path in owned.values() if path.exists()]
    if leftovers:
        raise ValueError('清理后工作树目录仍存在：' + '、'.join(leftovers))
    with store.transaction() as db:
        remove(db, checkout)
