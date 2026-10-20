"""工作树适配契约：建立、核对、删除；不持久化适配返回的环境值。"""
import json
import os
from pathlib import Path
import re
import subprocess

from .repositories import git


INPUT_NAMES = {'SOP_TASK_DIR', 'SOP_FIX_BRANCH', 'SOP_REPOS', 'SOP_WORKTREES', 'SOP_ENV_LOG_DIR'}
ARTIFACT_DIRECTORIES = {'evidence', 'sop', 'attempts', 'interruptions', 'validations'}


def is_board_input(name):
    return name in INPUT_NAMES or name.startswith(('SOP_REPO_', 'SOP_BASE_'))


def unique_object(pairs):
    value = {}
    for name, item in pairs:
        if name in value:
            raise ValueError('适配输出含重复字段')
        value[name] = item
    return value


class WorktreeError(ValueError):
    def __init__(self, message, log, paths=None):
        super().__init__(message + '；日志：' + str(log))
        self.log = str(log)
        self.paths = paths or {}


class Worktrees:
    def __init__(self, runtime):
        self.runtime, self.workspace = runtime, runtime.workspace
        self.cache = {}
        self.active = set()

    def registered(self, root):
        result, record = {}, {}
        for field in git(root, 'worktree', 'list', '--porcelain', '-z').split(b'\0'):
            if not field:
                if 'worktree' in record:
                    result[Path(record['worktree']).resolve()] = record.get('branch', '').removeprefix('refs/heads/')
                record = {}
            else:
                key, _, value = field.partition(b' ')
                record[os.fsdecode(key)] = os.fsdecode(value)
        return result

    def common_dir(self, path):
        raw = os.fsdecode(git(path, 'rev-parse', '--git-common-dir')).strip()
        return (Path(path) / raw).resolve()

    def validate_path(self, alias, value, branch, task_dir):
        if not isinstance(value, (str, Path)) or not str(value).strip() or '\0' in str(value):
            raise ValueError(alias + ' 的工作树路径须为非空文字')
        path = Path(value)
        path = (self.workspace.data / path).resolve()
        # 删除工作树不能连带删除基线、任务证据或整个看板数据目录。
        protected = [self.workspace.data, task_dir, *self.workspace.repos.values()]
        if any(root.is_relative_to(path) for root in protected):
            raise ValueError(alias + ' 工作树不能覆盖基线、任务目录或看板数据目录：' + str(path))
        if any(path.is_relative_to(task_dir / name) for name in ARTIFACT_DIRECTORIES):
            raise ValueError(alias + ' 工作树不能占用任务证据目录：' + str(path))
        try:
            top = Path(os.fsdecode(git(path, 'rev-parse', '--show-toplevel')).strip()).resolve()
            if top != path or git(path, 'rev-parse', '--is-inside-work-tree').strip() != b'true':
                raise ValueError(alias + ' 返回路径不是工作树根目录：' + str(path))
            if self.common_dir(path) != self.common_dir(self.workspace.repos[alias]):
                raise ValueError(alias + ' 返回路径与基线不是同一个 Git 仓库（git common dir 不同）：' + str(path))
            actual = git(path, 'branch', '--show-current').decode().strip()
            if actual != branch:
                raise ValueError(alias + ' 当前分支为 ' + (actual or '分离头指针') + '，要求 ' + branch)
            if self.registered(self.workspace.repos[alias]).get(path) != branch:
                raise ValueError(alias + ' 返回路径未登记为对应仓库的工作树：' + str(path))
        except (OSError, subprocess.SubprocessError) as error:
            raise ValueError(alias + ' 无法读取工作树根目录或 Git 身份：' + str(path)) from error
        return path

    def validate(self, paths, aliases, branch, task_dir, known=None):
        if not isinstance(paths, dict) or set(paths) != set(aliases):
            raise ValueError('adapter.worktree 的 repos 必须恰好包含 SOP_REPOS 中的每个仓库别名')
        result = {a: self.validate_path(a, paths[a], branch, task_dir) for a in aliases}
        if len(set(result.values())) != len(result):
            raise ValueError('不同仓库不能返回同一工作树路径')
        for alias, path in (known or {}).items():
            if alias in result and result[alias] != Path(path).resolve():
                raise ValueError(alias + ' 的已有工作树路径变化，不能替换修复现场')
        return result

    def inputs(self, task_dir, branch, aliases, paths=None):
        env = self.runtime.environment({})
        env.update(SOP_TASK_DIR=str(task_dir), SOP_FIX_BRANCH=branch, SOP_REPOS=','.join(aliases))
        for alias in aliases:
            env['SOP_BASE_' + alias.upper().replace('-', '_')] = str(self.workspace.repos[alias])
        if paths is not None:
            env['SOP_WORKTREES'] = json.dumps({a: str(p) for a, p in paths.items()}, ensure_ascii=False)
        return env

    def build(self, task_dir, branch, aliases, log, known=None):
        task_dir, log = Path(task_dir).resolve(), Path(log)
        aliases = sorted(aliases)
        known = {a: Path(p).resolve() for a, p in (known or {}).items()}
        key = (str(task_dir), branch)
        cached = self.cache.get(key)
        if cached and cached['config_digest'] == self.workspace.config_digest and set(cached['repos']) == set(aliases):
            paths = self.validate(cached['repos'], aliases, branch, task_dir, known)
            return paths, dict(cached['env'])
        task_dir.mkdir(parents=True, exist_ok=True)
        log.parent.mkdir(parents=True, exist_ok=True)
        before = {a: self.registered(self.workspace.repos[a]) for a in aliases}
        self.active.add(task_dir)
        try:
            if 'worktree' in self.workspace.adapter:
                raw = self.runtime.run_command('worktree', self.inputs(task_dir, branch, aliases), log, json_output=True)
                try:
                    output = json.loads(raw, object_pairs_hook=unique_object)
                    if not isinstance(output, dict) or set(output) != {'repos', 'env'} or not isinstance(output['env'], dict):
                        raise ValueError()
                    extra = output['env']
                    for name, value in extra.items():
                        if not re.fullmatch('[A-Za-z_][A-Za-z0-9_]*', name) or not isinstance(value, str) or '\0' in value or is_board_input(name):
                            raise ValueError()
                except (ValueError, TypeError, KeyError, RecursionError):
                    raise ValueError('adapter.worktree 最后一行须为 {"repos": {别名: 路径}, "env": {变量名: 文字值}}，env 不能覆盖看板输入')
                paths = self.validate(output['repos'], aliases, branch, task_dir, known)
            else:
                paths, extra = {}, {}
                with log.open('w', encoding='utf-8') as stream:
                    for alias in aliases:
                        root = self.workspace.repos[alias]
                        path = known.get(alias, task_dir / 'repos' / alias)
                        if path.exists():
                            if alias not in known:
                                raise ValueError(alias + ' 默认工作树目录已存在，不能接管：' + str(path))
                        else:
                            path.parent.mkdir(parents=True, exist_ok=True)
                            if git(root, 'branch', '--list', branch).strip():
                                if alias not in known:
                                    raise ValueError(alias + ' 已存在同名分支，不能接管')
                                git(root, 'worktree', 'add', str(path), branch)
                            else:
                                git(root, 'worktree', 'add', '-b', branch, str(path), 'HEAD')
                            stream.write(alias + '：git worktree add ' + str(path) + '\n')
                        paths[alias] = path
                paths = self.validate(paths, aliases, branch, task_dir, known)
            self.cache[key] = dict(repos=paths, env=extra, config_digest=self.workspace.config_digest)
            return paths, dict(extra)
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            # 只交出本次新建且三项身份核对通过的路径，绝不把错误返回的目录交给删除命令。
            safe = {}
            for alias in aliases:
                try:
                    newly_created = [p for p, ref in self.registered(self.workspace.repos[alias]).items()
                                     if p not in before[alias] and ref == branch]
                    if len(newly_created) == 1:
                        safe[alias] = self.validate_path(alias, newly_created[0], branch, task_dir)
                except (OSError, ValueError, subprocess.SubprocessError):
                    pass
            raise WorktreeError('工作树建立失败：' + str(error) + '；检查目录：' + str(task_dir), log, safe) from error
        finally:
            self.active.discard(task_dir)

    def cleanup(self, task_dir, branch, paths, log):
        task_dir, log = Path(task_dir).resolve(), Path(log)
        paths = {a: Path(p).resolve() for a, p in paths.items()}
        aliases = sorted(paths)
        try:
            for alias, path in paths.items():
                if path.exists():
                    self.validate_path(alias, path, branch, task_dir)
                elif path in self.registered(self.workspace.repos[alias]) and self.registered(self.workspace.repos[alias])[path] != branch:
                    raise ValueError(alias + ' 的残留工作树登记不属于当前分支')
            if 'cleanup' in self.workspace.adapter:
                self.runtime.run_command('cleanup', self.inputs(task_dir, branch, aliases, paths), log)
            else:
                log.parent.mkdir(parents=True, exist_ok=True)
                with log.open('w', encoding='utf-8') as stream:
                    for alias, path in paths.items():
                        if path.exists() or path in self.registered(self.workspace.repos[alias]):
                            git(self.workspace.repos[alias], 'worktree', 'remove', '--force', str(path))
                            stream.write(alias + '：git worktree remove ' + str(path) + '\n')
            remaining = [alias for alias, path in paths.items() if path.exists() or path in self.registered(self.workspace.repos[alias])]
            if remaining:
                raise ValueError('删除命令结束后目录或 Git 工作树登记仍存在：' + '、'.join(remaining))
            self.cache.pop((str(task_dir), branch), None)
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            raise WorktreeError('工作树清理失败：' + str(error), log) from error
