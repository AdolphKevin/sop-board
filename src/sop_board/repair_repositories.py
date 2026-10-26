"""修复任务拥有的多仓工作树、提交与合入前的整组核对；不还原基线或工作树。"""
import hashlib
import json
import os
from pathlib import Path
import subprocess

from .repositories import git, git_identities
from .service import canonical
from .worktrees import WorktreeError


def head(root):
    return git(root, 'rev-parse', '--verify', 'HEAD').decode().strip()


def ancestor(root, before, after):
    try:
        git(root, 'merge-base', '--is-ancestor', before, after)
        return True
    except subprocess.CalledProcessError as error:
        if error.returncode != 1:
            raise
        return False


class RepairRepositories:
    def __init__(self, tasks):
        self.tasks, self.store = tasks, tasks.store
        self.workspace = tasks.service.workspace

    def rows(self, task_id):
        rows = self.tasks.kernel.repositories.rows(task_id, 'fix')
        for row in rows:
            row['changed_files'] = json.loads(row.pop('changed_files_json'))
        return rows

    def update(self, task_id, alias, **values):
        self.tasks.kernel.repositories.update('fix', task_id, alias, **values)

    def ensure(self, task_id, aliases):
        existing = {r['alias']: r for r in self.rows(task_id)}
        planned = []
        branch = f'sop-board/fix/{task_id}'
        for alias in aliases:
            root = self.workspace.repos[alias]
            if alias in existing:
                if existing[alias]['root'] != str(root) or existing[alias]['identity'] != self.workspace.identities[alias]:
                    raise ValueError(alias + ' 的仓库路径或身份变化，须由开发者处理')
                continue
            baseline = git(root, 'branch', '--show-current').decode().strip()
            if not baseline:
                raise ValueError(alias + ' 基线处于分离头指针，不能开始修复')
            if git(root, 'branch', '--list', branch).strip():
                raise ValueError(alias + ' 已存在同名修复分支，不能接管')
            planned.append((task_id, alias, str(root), self.workspace.identities[alias], baseline, head(root), branch, ''))
        with self.store.transaction() as db:
            for values in planned:
                self.tasks.kernel.repositories.insert('fix', dict(zip(
                    ('task','alias','root','identity','baseline_branch','baseline_commit','fix_branch','worktree'), values)), connection=db)
        directory = self.tasks.directory(task_id)
        known = {r['alias']: Path(r['worktree']) for r in self.rows(task_id) if r['worktree']}
        if 'worktree' not in self.workspace.adapter:
            known.update({a: directory / 'repos' / a for a in aliases if a not in known})
        try:
            paths, extra = self.tasks.service.environment.worktrees.build(directory, branch, aliases,
                self.workspace.data / 'worktree-logs' / f'fix-{task_id}.log', known)
        except WorktreeError as error:
            for alias, path in error.paths.items():
                self.update(task_id, alias, worktree=str(path))
            raise
        with self.store.transaction() as db:
            for alias, path in paths.items():
                self.tasks.kernel.repositories.update('fix', task_id, alias, worktree=str(path), connection=db)
        return extra

    def repair_directories(self, task_id):
        return {r['alias']: Path(r['worktree']) for r in self.rows(task_id) if r['worktree']}

    def use_workspace(self, task_id, aliases):
        """会话入口只登记当前目录，不建立、切换或清理 Git 工作树。"""
        existing = {r['alias']: r for r in self.rows(task_id)}
        with self.store.transaction() as db:
            for alias in aliases:
                root = self.workspace.repos[alias]
                if alias in existing:
                    if existing[alias]['root'] != str(root) or existing[alias]['identity'] != self.workspace.identities[alias]:
                        raise ValueError(alias + ' 的仓库路径或身份已变化')
                    continue
                branch = git(root, 'branch', '--show-current').decode().strip()
                self.tasks.kernel.repositories.insert('fix', dict(task=task_id, alias=alias, root=str(root),
                    identity=self.workspace.identities[alias], baseline_branch=branch, baseline_commit=head(root),
                    fix_branch=branch, worktree=str(root)), connection=db)

    def directories(self, task_id):
        return dict(self.workspace.repos, **self.repair_directories(task_id))

    def heads(self, task_id):
        return {r['alias']: head(r['worktree']) for r in self.rows(task_id)}

    def changes(self, task_id, starts=None):
        result = {}
        for repo in self.rows(task_id):
            start = starts[repo['alias']] if starts is not None else repo['baseline_commit']
            raw = git(repo['worktree'], 'diff', '--no-renames', '--name-only', '-z', start, '--')
            raw += git(repo['worktree'], 'ls-files', '--others', '--exclude-standard', '-z')
            result[repo['alias']] = sorted({os.fsdecode(p) for p in raw.split(b'\0') if p})
        return result

    def protected_files(self, task_id, *, workspace=False):
        manifests = {}
        rows = self.rows(task_id)
        if workspace:
            included = {row['alias'] for row in rows}
            rows += [dict(alias=alias,root=str(root),worktree=str(root)) for alias,root in self.workspace.repos.items() if alias not in included]
        for repo in rows:
            root, worktree = Path(repo['root']), Path(repo['worktree'])
            directories = [worktree / 'docs/sop']
            if self.workspace.scenarios.is_relative_to(root):
                directories.append(worktree / self.workspace.scenarios.relative_to(root))
            manifest = {}
            for directory in directories:
                if not directory.is_symlink() and not directory.resolve().is_relative_to(worktree):
                    raise ValueError(repo['alias'] + ' 的保护目录越出修复工作树')
                paths = [directory] if directory.is_symlink() else directory.rglob('*')
                for path in paths:
                    if path.is_symlink():
                        raw = os.fsencode(os.readlink(path))
                    elif path.is_file():
                        raw = path.read_bytes()
                    else:
                        continue
                    manifest[str(path.relative_to(worktree))] = hashlib.sha256(raw).hexdigest()
            manifests[repo['alias']] = manifest
        return manifests

    def protected(self, task_id, changes):
        bad = []
        for repo in self.rows(task_id):
            root = Path(repo['root'])
            excluded = [Path('docs/sop')]
            if self.workspace.scenarios.is_relative_to(root):
                excluded.append(self.workspace.scenarios.relative_to(root))
            for name in changes[repo['alias']]:
                if any(Path(name).is_relative_to(path) for path in excluded):
                    bad.append(repo['alias'] + '/' + name)
        return bad

    def record_changes(self, task_id):
        changes = self.changes(task_id)
        for repo in self.rows(task_id):
            self.update(task_id, repo['alias'], changed_files_json=canonical(changes[repo['alias']]), repair_commit=head(repo['worktree']))
        return changes

    def commit(self, task_id, number, message):
        paths = self.repair_directories(task_id)
        self.tasks.service.environment.worktrees.validate(paths, list(paths), f'sop-board/fix/{task_id}', self.tasks.directory(task_id))
        # 整组先核对 Git 实际生效的身份，缺失时不能暂存或开始任何仓库的提交。
        identities = git_identities({r['alias']: r['root'] for r in self.rows(task_id)})
        committed = {}
        for repo in self.rows(task_id):
            worktree = repo['worktree']
            if not git(worktree, 'status', '--porcelain', '--untracked-files=all').strip():
                continue
            if git(worktree, 'ls-files', '--unmerged'):
                raise ValueError(repo['alias'] + ' 修复工作树存在未解决冲突')
            # 只暂存任务工作树中的修复文件；从不暂存或保存基线目录的修改。
            git(worktree, 'add', '--all')
            if not git(worktree, 'diff', '--cached', '--name-only').strip():
                continue
            author = identities[repo['alias']]['user.name']
            email = identities[repo['alias']]['user.email']
            try:
                git(worktree, '-c', 'commit.gpgsign=false', '-c', 'user.name=' + author, '-c', 'user.email=' + email,
                    'commit', '-m', message, capture_stderr=True)
            except subprocess.CalledProcessError as error:
                log = self.tasks.directory(task_id) / f"commit-{number}-{repo['alias']}.txt"
                log.write_bytes(error.output)
                raise ValueError(repo['alias'] + ' 修复提交失败，工作树与已提交仓库保留；日志：' + str(log)) from error
            committed[repo['alias']] = head(worktree)
            with self.store.transaction() as db:
                stored = json.loads(db.execute('SELECT commits_json FROM fix_attempts WHERE task=? AND number=?', (task_id, number)).fetchone()[0])
                stored.update(committed)
                db.execute('UPDATE fix_attempts SET commits_json=? WHERE task=? AND number=?', (canonical(stored), task_id, number))
        self.record_changes(task_id)
        return committed

    def preflight(self, task_id, validated):
        plans = []
        for repo in self.rows(task_id):
            alias, root, worktree = repo['alias'], repo['root'], repo['worktree']
            reasons = []
            baseline = repair = ''
            advanced = False
            try:
                branch = git(root, 'branch', '--show-current').decode().strip()
                baseline = git(root, 'rev-parse', 'refs/heads/' + repo['baseline_branch']).decode().strip()
                repair = head(worktree)
                if branch != repo['baseline_branch']:
                    reasons.append('基线分支未在工作区检出，当前为 ' + (branch or '分离头指针'))
                product = self.workspace.product([alias])
                if product['dirty']:
                    reasons.append('基线有未提交的产品改动：' + '、'.join(product['changed']))
                if git(root, 'ls-files', '--unmerged'):
                    reasons.append('基线有未解决的合并冲突')
                if git(worktree, 'branch', '--show-current').decode().strip() != repo['fix_branch']:
                    reasons.append('修复工作树分支变化')
                if git(worktree, 'status', '--porcelain', '--untracked-files=all').strip():
                    reasons.append('修复工作树有未提交改动或冲突')
                if alias not in validated or repair != validated[alias]['commit']:
                    reasons.append('修复提交与通过复测的提交不一致')
                advanced = baseline != repo['baseline_commit']
                if advanced and not ancestor(root, repo['baseline_commit'], baseline):
                    reasons.append('基线历史被改写，不能自动合入')
                if not advanced and not ancestor(root, baseline, repair):
                    reasons.append('基线不能快进到修复提交')
            except (OSError, KeyError, ValueError, subprocess.SubprocessError) as error:
                reasons.append('无法核对仓库或分支：' + str(error))
            plans.append(dict(repo, current_baseline=baseline, target=repair, advanced=advanced,
                              changed=baseline != repair, reasons=reasons))
        return plans

    def record_blocked(self, task_id, plans):
        blocked = [p['alias'] for p in plans if p['reasons']]
        for plan in plans:
            self.update(task_id, plan['alias'], merge_status='不满足' if plan['reasons'] else '等待其他仓库',
                        merge_reason='；'.join(plan['reasons']) if plan['reasons'] else '自身条件满足，整组等待：' + '、'.join(blocked))

    def integrate_baselines(self, task_id, number, plans):
        for plan in plans:
            if not plan['advanced']:
                continue
            try:
                git(plan['worktree'], '-c', 'commit.gpgsign=false', 'merge', '--no-edit', plan['current_baseline'], capture_stderr=True)
            except subprocess.CalledProcessError as error:
                plan['reasons'].append('在修复工作树合入前进的基线失败，保留现场：' + error.output.decode('utf-8', errors='replace'))
                self.record_blocked(task_id, plans)
                return False
            self.update(task_id, plan['alias'], baseline_commit=plan['current_baseline'], repair_commit=head(plan['worktree']),
                        merge_status='待复测', merge_reason='基线已前进，工作树更新后须重新复测')
        self.record_changes(task_id)
        return True

    def fast_forward(self, task_id, number, plans):
        merged = {}
        for plan in plans:
            alias = plan['alias']
            # 所有仓库已整组核对；每次落地前仍核对分支和提交，绝不强制移动。
            if (head(plan['root']) != plan['current_baseline'] or git(plan['root'], 'branch', '--show-current').decode().strip() != plan['baseline_branch']
                    or self.workspace.product([alias])['dirty']):
                self.update(task_id, alias, merge_status='合入中止', merge_reason='合入期间基线变化；不还原已经完成的 Git 操作')
                raise ValueError(alias + ' 合入期间基线变化，停止余下合入，请逐仓核对')
            if plan['changed']:
                try:
                    git(plan['root'], 'merge', '--ff-only', plan['target'], capture_stderr=True)
                except subprocess.CalledProcessError as error:
                    reason = error.output.decode('utf-8', errors='replace')
                    self.update(task_id, alias, merge_status='合入失败', merge_reason=reason)
                    raise ValueError(alias + ' 快进合入失败，停止余下合入：' + reason) from error
                merged[alias] = head(plan['root'])
                self.update(task_id, alias, merged_commit=merged[alias], merge_status='已合入', merge_reason='已快进合入 ' + plan['baseline_branch'])
                with self.store.transaction() as db:
                    db.execute('UPDATE fix_attempts SET merged_commits_json=? WHERE task=? AND number=?', (canonical(merged), task_id, number))
            else:
                self.update(task_id, alias, merge_status='无改动', merge_reason='基线与修复目录提交相同，不产生合入提交')
        return merged

    def cleanup(self, task_id):
        repositories = self.rows(task_id)
        branch = f'sop-board/fix/{task_id}'
        paths = {}
        remaining = False
        for repo in repositories:
            if not repo['worktree'] or repo['fix_branch'] != branch:
                raise ValueError('修复工作树记录不属于此任务')
            path = Path(repo['worktree'])
            if (path.exists() or path in self.tasks.service.environment.worktrees.registered(repo['root'])
                    or git(repo['root'], 'branch', '--list', branch).strip()):
                remaining = True
        if not remaining:
            self.tasks.worktree_environments.pop(task_id, None)
            return
        # 整组核对通过后才交给删除适配，避免先删掉一仓再发现另一仓仍有修复。
        for repo in repositories:
            if not repo['worktree'] or repo['fix_branch'] != branch:
                raise ValueError('修复工作树记录不属于此任务')
            root, path = Path(repo['root']), Path(repo['worktree'])
            if root != self.workspace.repos.get(repo['alias']):
                raise ValueError(repo['alias'] + ' 当前配置的基线目录变化，不能清理')
            baseline = git(root, 'rev-parse', 'refs/heads/' + repo['baseline_branch']).decode().strip()
            if not ancestor(root, repo['repair_commit'], baseline):
                raise ValueError(repo['alias'] + ' 修复提交尚未合入，保留工作树与分支')
            if path.exists() and (head(path) != repo['repair_commit'] or git(path, 'status', '--porcelain', '--untracked-files=all').strip()):
                raise ValueError(repo['alias'] + ' 工作树被改动，保留现场')
            paths[repo['alias']] = path
        self.tasks.service.environment.worktrees.cleanup(self.tasks.directory(task_id), branch, paths,
            self.workspace.data / 'worktree-logs' / f'fix-{task_id}-cleanup.log')
        self.tasks.worktree_environments.pop(task_id, None)
        # 删除适配成功并核对目录及 Git 登记均已消失之后，才删除已合入分支。
        for repo in repositories:
            if git(repo['root'], 'branch', '--list', branch).strip():
                git(repo['root'], 'branch', '-d', branch)
