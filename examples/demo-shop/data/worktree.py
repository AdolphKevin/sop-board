"""演示项目的 adapter.worktree / cleanup。

create：为 SOP_REPOS 中的每个仓库在 SOP_TASK_DIR/<别名> 建 worktree，检出新分支 SOP_FIX_BRANCH
（起点取 SOP_BASE_COMMITS，缺省 HEAD）；最后一行输出 {"repos": {别名: 路径}, "env": {}}。
cleanup：移除 SOP_WORKTREES 中的 worktree 与分支；有未提交改动或未合并提交时 git 拒绝，现场保留。
"""
import json
import os
from pathlib import Path
import subprocess
import sys


def git(repo, *args):
    subprocess.run(['git', '-C', str(repo), *args], check=True, stdout=sys.stderr)


def base(alias):
    return os.environ['SOP_BASE_' + alias.upper().replace('-', '_')]


def create():
    root, branch = Path(os.environ['SOP_TASK_DIR']), os.environ['SOP_FIX_BRANCH']
    starts = json.loads(os.environ.get('SOP_BASE_COMMITS') or '{}')
    repos = {}
    for alias in os.environ['SOP_REPOS'].split(','):
        target = root / alias
        git(base(alias), 'worktree', 'add', '-b', branch, str(target), starts.get(alias, 'HEAD'))
        repos[alias] = str(target)
    print(json.dumps({'repos': repos, 'env': {}}, ensure_ascii=False))


def cleanup():
    branch = os.environ['SOP_FIX_BRANCH']
    for alias, path in json.loads(os.environ['SOP_WORKTREES']).items():
        git(base(alias), 'worktree', 'remove', path)
        git(base(alias), 'branch', '-d', branch)
    root = Path(os.environ['SOP_TASK_DIR'])
    if root.is_dir() and not any(root.iterdir()):
        root.rmdir()


if __name__ == '__main__':
    {'create': create, 'cleanup': cleanup}[sys.argv[1]]()
