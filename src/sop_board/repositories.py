"""Git 内容查询与配置场景目录发现；工作区配置是唯一仓库入口。"""
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import threading
import time

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


def _head_blob_hashes(root, object_ids):
    """每仓只开一个进程，按对象长度分块读取，不在内存中保留文件内容。"""
    if not object_ids:
        return []
    env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
    env['GIT_OPTIONAL_LOCKS'] = '0'
    command = ['git', '-C', str(root), 'cat-file', '--batch']
    hashes = []
    with subprocess.Popen(command, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL) as process:
        expired, finished = threading.Event(), threading.Event()
        progress = [time.monotonic()]

        def watch():
            while not finished.wait(1):
                if time.monotonic() - progress[0] > 30:
                    expired.set()
                    process.kill()
                    return

        # 与逐个读取时相同：单个对象 30 秒无进展才终止；阻塞的管道读取也受保护。
        timer = threading.Thread(target=watch, daemon=True)
        try:
            timer.start()
            for object_id in object_ids:
                progress[0] = time.monotonic()
                # 读完一个对象再发送下一个，避免输入、输出管道同时写满。
                process.stdin.write(object_id + b'\n')
                process.stdin.flush()
                header = process.stdout.readline(1024)
                fields = header.split()
                if (not header.endswith(b'\n') or len(fields) != 3 or fields[0] != object_id
                        or fields[1] != b'blob' or not fields[2].isdigit()):
                    raise ValueError(f'Git 对象缺失或批量响应不合格：{root} {object_id.decode()}')
                remaining = int(fields[2])
                sha256 = hashlib.sha256()
                while remaining:
                    chunk = process.stdout.read(min(remaining, 64 * 1024))
                    if not chunk:
                        raise ValueError(f'Git 对象内容不完整：{root} {object_id.decode()}')
                    sha256.update(chunk)
                    remaining -= len(chunk)
                if process.stdout.read(1) != b'\n':
                    raise ValueError(f'Git 对象结束符不完整：{root} {object_id.decode()}')
                hashes.append(sha256.hexdigest())
            process.stdin.close()
            if process.stdout.read(1):
                raise ValueError(f'Git 批量响应有多余内容：{root}')
            returncode = process.wait()
            if returncode:
                raise subprocess.CalledProcessError(returncode, command)
        finally:
            finished.set()
            if timer.ident is not None:
                timer.join()
            process.kill()
            process.wait()
            if expired.is_set():
                raise subprocess.TimeoutExpired(command, 30)
    return hashes


def product_files(root, excluded=None, head=False, revision='HEAD'):
    """排除声明路径后的已跟踪/未忽略内容；保留旧的单路径调用。"""
    exclusions = [] if excluded is None else [excluded] if isinstance(excluded, (str, Path)) else excluded
    def is_excluded(name):
        return any(Path(name).is_relative_to(path) for path in exclusions)
    result = []
    if head:
        entries = []
        for record in git(root, 'ls-tree', '-rz', '--full-tree', revision).split(b'\0'):
            if not record:
                continue
            info, raw = record.split(b'\t', 1)
            name = os.fsdecode(raw)
            if not is_excluded(name):
                mode, kind, object_id = info.split()
                entries.append((name, mode, object_id))
        hashes = iter(_head_blob_hashes(root, [oid for _, mode, oid in entries if mode != b'160000']))
        for name, mode, object_id in entries:
            sha256 = hashlib.sha256(object_id).hexdigest() if mode == b'160000' else next(hashes)
            result.append(dict(path=name, mode=mode.decode(), sha256=sha256))
    else:
        submodules = {}
        for record in git(root, 'ls-files', '-sz').split(b'\0'):
            if record:
                info, raw = record.split(b'\t', 1)
                mode, object_id, stage = info.split()
                if mode == b'160000' and stage == b'0':
                    submodules[os.fsdecode(raw)] = object_id
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
                # 未检出的目录会向上找到外层 Git 仓库，不能从中读取 HEAD。
                value = (submodules[name] if name in submodules and not (path / '.git').exists()
                         else git(path, 'rev-parse', '--verify', 'HEAD').strip())
            elif name in submodules and not path.exists():
                mode, value = '160000', submodules[name]
            elif not path.exists():
                result.append(dict(path=name, deleted=True))
                continue
            else:
                try:
                    mode = '100755' if path.stat().st_mode & 0o111 else '100644'
                    value = path.read_bytes()
                except FileNotFoundError:
                    # 执行期间允许并行修改：读取前刚被删除的文件按已删除记录，不使登记失败。
                    result.append(dict(path=name, deleted=True))
                    continue
            result.append(dict(path=name, mode=mode, sha256=hashlib.sha256(value).hexdigest()))
    return sorted(result, key=lambda r: os.fsencode(r['path']))


def changed_files(before, after):
    """按仓库别名比较两份 product_files 清单，返回内容、模式或存在性不同的相对路径。"""
    result = {}
    for alias in before.keys() | after.keys():
        old = {r['path']: r for r in before.get(alias, [])}
        new = {r['path']: r for r in after.get(alias, [])}
        result[alias] = [p for p in sorted(old.keys() | new.keys()) if old.get(p) != new.get(p)]
    return result


def read_sop_sources(workspace, raw, uploaded_sop=None):
    document = yaml.safe_load(raw) or {}
    sources = {}
    for source in document.get('metadata', {}).get('sources', []):
        # 上传来源是完整仓内路径，允许批次名含空格、冒号，并保留原文换行。
        uploaded = re.fullmatch(r'看板上传 #[0-9]+', str(source.get('version', '')))
        for token in [source['ref']] if uploaded else source['ref'].split():
            alias, separator, relative = token.partition(':')
            if uploaded or not separator:
                alias, relative = workspace.scenario_repo, token
            repository = workspace.repos.get(alias)
            if repository is None or Path(relative).is_absolute() or '..' in Path(relative).parts:
                continue
            path = (repository / relative).resolve()
            if not path.is_relative_to(repository) or not path.is_file():
                continue
            label = alias + ':' + path.relative_to(repository).as_posix()
            preserve = uploaded or uploaded_sop and uploaded_sop['path'] == label
            content = path.read_bytes().decode('utf-8') if preserve else path.read_text(encoding='utf-8')
            commit = git(repository, 'rev-parse', '--verify', 'HEAD').decode().strip()
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

    def scan(self, *, reconcile=True):
        from .errors import APIError
        from .scenario_file import load_scenarios
        from .service import fingerprint
        workspace = self.service.workspace
        changed = workspace.reload()
        if reconcile:
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
                    with self.service.store.connection() as db:
                        batch = db.execute('SELECT * FROM batches WHERE workspace=? AND name=?', (workspace.name, name)).fetchone()
                        uploaded_sop = self.service.latest_sop_upload(db, batch) if batch else None
                        found = db.execute('SELECT 1 FROM versions v JOIN batches b ON b.id=v.batch '
                            'WHERE b.workspace=? AND b.name=? AND v.content=? '
                            'AND NOT EXISTS (SELECT 1 FROM versions newer WHERE newer.batch=v.batch AND newer.version>v.version)',
                            (workspace.name, name, raw)).fetchone()
                    sources = read_sop_sources(workspace, raw, uploaded_sop)
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

