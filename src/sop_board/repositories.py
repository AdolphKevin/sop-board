"""Git 内容查询与配置场景目录发现；工作区配置是唯一仓库入口。"""
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import threading

import yaml


def git(root, *args, capture_stderr=False):
    env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
    env['GIT_OPTIONAL_LOCKS'] = '0'
    return subprocess.check_output(['git', '-C', str(root), *args], env=env,
                                   stderr=subprocess.STDOUT if capture_stderr else subprocess.DEVNULL, timeout=30)


class MissingGitIdentity(ValueError):
    """缺少提交身份，保留修复现场并等待开发者设置。"""


def git_identities(repositories):
    identities, problems = {}, []
    for alias, root in repositories.items():
        identity = {}
        for field in ('user.name', 'user.email'):
            try:
                value = git(root, 'config', field).decode().strip()
            except subprocess.CalledProcessError as error:
                if error.returncode != 1:
                    raise
                value = ''
            if not value:
                problems.append(f'{alias} 缺少 {field}；请设置 git -C {shlex.quote(str(root))} config {field} <值>，'
                                f'或 git config --global {field} <值>')
            identity[field] = value
        identities[alias] = identity
    if problems:
        raise MissingGitIdentity('；'.join(problems))
    return identities


def product_files(root, excluded=None, head=False, revision='HEAD'):
    """场景目录之外的已跟踪/未忽略内容；未跟踪文件和删除同样影响产品。"""
    def is_excluded(name):
        return excluded is not None and Path(name).is_relative_to(excluded)
    result = []
    if head:
        for record in git(root, 'ls-tree', '-rz', '--full-tree', revision).split(b'\0'):
            if not record:
                continue
            info, raw = record.split(b'\t', 1)
            name = os.fsdecode(raw)
            if not is_excluded(name):
                mode, kind, object_id = info.split()
                value = object_id if mode == b'160000' else git(root, 'cat-file', 'blob', object_id.decode())
                result.append(dict(path=name, mode=mode.decode(), sha256=hashlib.sha256(value).hexdigest()))
    else:
        for raw in sorted(set(git(root, 'ls-files', '-z', '--cached', '--others', '--exclude-standard').split(b'\0'))):
            if not raw:
                continue
            name = os.fsdecode(raw)
            if is_excluded(name):
                continue
            path = root / name
            if path.is_symlink():
                mode = '120000'
                value = os.fsencode(os.readlink(path))
            elif path.is_dir():
                mode = '160000'
                value = git(path, 'rev-parse', '--verify', 'HEAD').strip()
            elif not path.exists():
                result.append(dict(path=name, deleted=True))
                continue
            else:
                mode = '100755' if path.stat().st_mode & 0o111 else '100644'
                value = path.read_bytes()
            result.append(dict(path=name, mode=mode, sha256=hashlib.sha256(value).hexdigest()))
    return sorted(result, key=lambda r: os.fsencode(r['path']))


def read_sop_sources(workspace, raw):
    document = yaml.safe_load(raw) or {}
    sources = {}
    for source in document.get('metadata', {}).get('sources', []):
        for token in source['ref'].split():
            alias, separator, relative = token.partition(':')
            if not separator:
                alias, relative = workspace.scenario_repo, token
            repository = workspace.repos.get(alias)
            if repository is None or Path(relative).is_absolute() or '..' in Path(relative).parts:
                continue
            path = (repository / relative).resolve()
            if not path.is_relative_to(repository) or not path.is_file():
                continue
            content = path.read_text(encoding='utf-8')
            commit = git(repository, 'rev-parse', '--verify', 'HEAD').decode().strip()
            label = alias + ':' + path.relative_to(repository).as_posix()
            sources[label] = dict(path=label, ref=source['ref'], content=content, commit=commit,
                                  sha256=hashlib.sha256(content.encode()).hexdigest(), confirmed_by=source['confirmed_by'])
            break
    return [sources[name] for name in sorted(sources)]


class Discovery:
    def __init__(self, service):
        self.service = service
        self.stopping = threading.Event()
        self.first_scan = True
        self.thread = threading.Thread(target=self.run, name='workspace-discovery', daemon=True)

    def scan(self):
        from .errors import APIError
        from .scenario_file import load_scenarios
        from .service import fingerprint
        workspace = self.service.workspace
        changed = workspace.reload()
        self.service.environment.reconcile()
        errors = {}
        if not workspace.configured:
            self.service.workspace_problems = errors
            return
        with self.service.workspace_lock:
            for path in sorted(workspace.scenarios.glob('*.yaml')):
                name = path.stem
                try:
                    if workspace.scenario_path(name) != path:
                        raise ValueError('场景路径不合格')
                    raw = path.read_text(encoding='utf-8')
                    document = load_scenarios(raw, entries=set(workspace.entries))
                    if document['metadata']['batch'] != name:
                        raise ValueError('metadata.batch 必须与场景文件名一致')
                    info = workspace.product(workspace.aliases(document))
                    sources = read_sop_sources(workspace, raw)
                    with self.service.store.connection() as db:
                        found = db.execute('SELECT 1 FROM versions v JOIN batches b ON b.id=v.batch '
                            'WHERE b.workspace=? AND b.name=? AND v.content=? '
                            'AND NOT EXISTS (SELECT 1 FROM versions newer WHERE newer.batch=v.batch AND newer.version>v.version)',
                            (workspace.name, name, raw)).fetchone()
                    if found:
                        with self.service.store.transaction() as db:
                            batch = self.service.batch(db, workspace.name, name)
                            self.service.save_sop_sources(db, batch['id'], sources)
                            db.execute('UPDATE batches SET product_digest=? WHERE id=?', (info['product_digest'], batch['id']))
                    else:
                        self.service.push(workspace.name, name, dict(workspace=dict(name=workspace.name),
                            path=workspace.display_path(path), content=raw, pushed_by='工作区', sop_sources=sources,
                            commit=info['commit'], dirty=info['dirty'], product_digest=info['product_digest'],
                            fingerprints={s['id']: fingerprint(s) for n in document['sops'] for s in n['scenarios']}))
                except (APIError, ValueError, OSError, subprocess.SubprocessError) as error:
                    problems = error.problems if isinstance(error, APIError) else [dict(field='content', problem=str(error), line=1)]
                    errors[(workspace.name, name)] = [dict(p, path=workspace.display_path(path)) for p in problems]
            with self.service.store.transaction() as db:
                for (key, name), problems in errors.items():
                    db.execute("INSERT OR IGNORE INTO batches(workspace,name,path,product_digest) VALUES (?,?,?,'')",
                               (key, name, problems[0]['path']))
                waiting = [r[0] for r in db.execute('SELECT name FROM batches WHERE workspace=?', (workspace.name,))] if changed or self.first_scan else []
            self.first_scan = False
            self.service.workspace_problems = errors
            if self.service.merge_callback:
                for name in waiting:
                    self.service.merge_callback(workspace.name, name, trigger='配置变化')

    def run(self):
        while not self.stopping.wait(5):
            try:
                self.scan()
            except (OSError, ValueError, subprocess.SubprocessError) as error:
                self.service.workspace_problems[(self.service.workspace.name, '')] = [dict(field='workspace', problem=str(error), line=1)]

    def close(self):
        self.stopping.set()
        self.thread.join()
