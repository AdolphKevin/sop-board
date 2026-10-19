"""持久任务、Codex 进程入口与经过核对后的合并落地。"""
import hashlib
import json
import re
import os
from pathlib import Path
import signal
import subprocess
import threading
from importlib.resources import files
from urllib.parse import quote

from .errors import reject, APIError
from .drafts import SCHEMA, assemble, section

import yaml
from .service import canonical, now


def parse_timeout(value):
    if value is None:
        return 1800
    if not re.fullmatch(r'[0-9]+', value.strip()) or int(value) <= 0:
        raise ValueError('SOP_BOARD_CODEX_TIMEOUT 超时取值不合规：应为正整数秒数')
    return int(value)


def codex_environment():
    allowed = {'PATH', 'HOME', 'USER', 'LANG', 'TMPDIR', 'CODEX_HOME', 'SHELL'}
    return {k: v for k, v in os.environ.items() if k in allowed or k.startswith('LC_')}


def judgment_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('最终 JSON 消息含重复字段')
        result[key] = value
    return result


def invalid_judgment_constant(value):
    raise ValueError(f'最终 JSON 消息含非有限数字：{value}')


def start_codex(directory, prompt, sandbox='read-only', environment=None):
    """可替换的启动边界；子进程只得到白名单环境及独立进程组。"""
    directory = Path(directory).resolve()
    events = (directory / 'events.jsonl').open('wb')
    stderr = (directory / 'stderr.txt').open('wb')
    try:
        options = ['-c', 'sandbox_workspace_write.network_access=true'] if sandbox == 'workspace-write' else []
        schema = ['--output-schema', str(directory / 'schema.json')] if (directory / 'schema.json').is_file() else []
        return subprocess.Popen(['codex', 'exec', '--json', '-s', sandbox, *options, '--skip-git-repo-check',
            '-C', str(directory), *schema,
            '-o', str(directory / 'final.json'), prompt], cwd=directory, env=environment if environment is not None else codex_environment(),
            stdin=subprocess.DEVNULL, stdout=events, stderr=stderr, start_new_session=True)
    finally:
        events.close()
        stderr.close()


def resume_codex(directory, thread_id, prompt, sandbox='read-only'):
    """续接指定会话；resume 的沙箱参数位于 Codex 根命令层。"""
    directory = Path(directory).resolve()
    events = (directory / 'events.jsonl').open('wb')
    stderr = (directory / 'stderr.txt').open('wb')
    try:
        return subprocess.Popen(['codex', '-s', sandbox, 'exec', 'resume', thread_id, '--json',
            '--skip-git-repo-check', '-o', str(directory / 'final.json'), prompt], cwd=directory,
            env=codex_environment(), stdin=subprocess.DEVNULL, stdout=events, stderr=stderr, start_new_session=True)
    finally:
        events.close()
        stderr.close()


def codex_available():
    try:
        result = subprocess.run(['codex', '--version'], env=codex_environment(),
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, start_new_session=True, timeout=5)
        if result.returncode:
            return f'codex --version 失败（退出码 {result.returncode}）'
    except FileNotFoundError:
        return '未找到 codex 命令'
    except (OSError, subprocess.SubprocessError):
        return 'codex --version 失败：无法启动或超时'
    return ''


def terminate_group(process):
    # Kill the group even if its leader already exited: grandchildren may still run.
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=.2)
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


class Tasks:
    def __init__(self, service, launcher=start_codex, timeout=None):
        self.service, self.store, self.launcher = service, service.store, launcher
        service.merge_callback = self.enqueue_merge
        self.lock = service.workspace_lock
        self.stopping = threading.Event()
        self.workers = {}
        self.check_workers = set()
        self.processes = {}
        self.candidate_processes = {}
        self.browser_owner = None
        self.starting = None
        self.timeout = parse_timeout(os.environ.get('SOP_BOARD_CODEX_TIMEOUT')) if timeout is None else timeout
        with self.store.transaction() as db:
            db.execute("UPDATE tasks SET status='已中断',finished_at=?,error='看板重启，任务已中断' WHERE status IN ('排队中','待提交','等待浏览器','运行中')", (now(),))
        self.availability_error = codex_available()
        from .checks import Checks
        self.checks = Checks(self)
        self.checks.cleanup_worktrees()
        from .executions import Executions
        self.executions = Executions(self)
        service.execution_callback = self.executions.enqueue
        self.thread = threading.Thread(target=self.run, name='codex-queue', daemon=True)
        self.thread.start()

    def enqueue_merge(self, key, name, trigger="重新合并"):
        with self.lock, self.store.transaction() as db:
            batch = self.service.batch(db, key, name)
            version = self.service.latest_version(db, batch)
            cases = self.service.case_union(db, batch)
            if not cases and trigger != '重新合并':
                return None
            if not cases:
                reject(409, 'cases-missing', 'cases', '批次没有用例，无法重新合并')
            upload = db.execute('SELECT number,metadata FROM case_uploads WHERE batch=? ORDER BY number DESC LIMIT 1', (batch['id'],)).fetchone()
            source = db.execute('SELECT content FROM batch_entries WHERE batch=?', (batch['id'],)).fetchone()
            entries = json.loads(source['content']) if source else {}
            sop_sources = [dict(row) for row in db.execute(
                'SELECT path,ref,commit_id AS "commit",sha256,confirmed_by,content FROM sop_versions v WHERE batch=? '
                'AND NOT EXISTS (SELECT 1 FROM sop_versions newer WHERE newer.batch=v.batch AND newer.path=v.path AND newer.version>v.version) ORDER BY path',
                (batch['id'],))]
            if key in self.service.writable_repos:
                from .repositories import read_entries, read_sop_sources
                declared, _ = read_entries(self.service.writable_repos[key])
                entries = dict(entries=declared)
                sop_sources = read_sop_sources(self.service.writable_repos[key], version['content'] if version else '')
            nodes = {r['section']: r['node'] for r in db.execute('SELECT section,node FROM node_numbers WHERE batch=?', (batch['id'],))}
            for case in cases:
                name_ = section(case)
                if name_ not in nodes:
                    nodes[name_] = f'S{len(nodes) + 1:02}'
                    db.execute('INSERT INTO node_numbers VALUES (?,?,?)', (batch['id'], name_, nodes[name_]))
            snapshot = dict(cases=cases, scenarios=version['content'] if version else '', entries=entries,
                            batch=name, case_metadata=json.loads(upload['metadata']), upload=upload['number'], nodes=nodes,
                            writable=key in self.service.writable_repos)
            # 只继承已有记录证明曾经并入、且仍在当前场景中的禁止副作用。
            # 任意外来用例场景上的额外断言不因此成为确定字段的输入。
            transferred = {}
            for row in db.execute("SELECT result_json FROM tasks WHERE project=? AND batch=? AND status IN ('已完成','待同步') ORDER BY id",
                                  (key, name)):
                for disposition in json.loads(row['result_json']).get('scenarios', []):
                    if disposition['action'] == 'duplicate':
                        transferred.setdefault(disposition['target'], set()).update(disposition['forbidden_effects'])
            original = yaml.safe_load(snapshot['scenarios']) or {}
            snapshot['merged_forbidden_effects'] = {
                scene['id']: [effect for effect in scene['forbidden_effects'] if effect in transferred[scene['id']]]
                for node in original.get('sops', []) for scene in node['scenarios'] if scene['id'] in transferred}
            snapshot['sop_sources'] = sop_sources
            scenario_sha = version['sha256'] if version else ''
            # 排版、上传序号和已分配节点不是业务输入；按规定的四种内容组成摘要。
            inputs = dict(cases=dict(metadata=snapshot['case_metadata'], test_cases=cases),
                          scenarios=scenario_sha, entries=entries,
                          sop=[dict(path=s['path'], sha256=s['sha256']) for s in sop_sources])
            digest = hashlib.sha256(canonical(inputs).encode()).hexdigest()
            states = ('排队中', '运行中') if trigger == '重新合并' else ('已完成', '待同步', '待补充', '排队中', '运行中')
            placeholders = ','.join('?' for _ in states)
            previous = db.execute(f"SELECT id FROM tasks WHERE project=? AND batch=? AND input_digest=? AND status IN ({placeholders}) ORDER BY id LIMIT 1",
                                  (key, name, digest, *states)).fetchone()
            if previous:
                task_id = previous['id']
            else:
                task_id = db.execute("INSERT INTO tasks(kind,project,batch,status,trigger,input_digest,input_json,scenario_file_sha256,created_at) VALUES ('merge',?,?,'排队中',?,?,?,?,?)",
                                     (key, name, trigger, digest, canonical(snapshot), scenario_sha, now())).lastrowid
        return dict(ok=True, task=self.read(task_id))

    def enqueue_check(self, key, name):
        with self.lock:
            declarations = self.service.entries(key)
            if not declarations['ok']:
                raise APIError(409, 'entries-invalid', declarations['problems'])
            with self.store.transaction() as db:
                batch = self.service.batch(db, key, name)
                scope = [dict(scenario=r['scenario'], reason='regression') for r in db.execute(
                    'SELECT scenario,regression FROM scenarios WHERE batch=? AND current=1 ORDER BY id', (batch['id'],))
                    if (json.loads(r['regression']) or {}).get('check')]
                if not scope:
                    reject(409, 'nothing-to-check', 'scenarios', '批次没有声明回归的场景')
                task_id = db.execute("INSERT INTO tasks(kind,project,batch,status,trigger,input_digest,input_json,scope_json,created_at) "
                    "VALUES ('check',?,?,'排队中','开发者发起',?,'{}',?,?)", (key, name,
                    hashlib.sha256(canonical(scope).encode()).hexdigest(), canonical(scope), now())).lastrowid
        return dict(ok=True, task=self.read(task_id))

    def enqueue_execution(self, key, name):
        return self.executions.enqueue(key, name)

    def list(self, project=None, batch=None):
        with self.store.connection() as db:
            ids = [r[0] for r in db.execute('SELECT id FROM tasks WHERE (? IS NULL OR project=?) AND (? IS NULL OR batch=?) ORDER BY id',
                                           (project, project, batch, batch))]
        return dict(ok=True, tasks=[self.read(i) for i in ids])

    def directory(self, task_id):
        return self.store.data.resolve() / 'tasks' / str(task_id)

    def read(self, task_id):
        with self.store.connection() as db:
            row = db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
        if not row:
            reject(404, 'task-not-found', 'task', '任务不存在')
        task = dict(row)
        task.pop('input_json')
        task['result'] = json.loads(task.pop('result_json'))
        task['gaps'] = task['result'].get('gaps', [])
        for field in ('scope', 'snapshot', 'rounds'):
            task[field] = json.loads(task.pop(field + '_json'))
        directory = self.directory(task_id)
        task['files'] = {str(p.relative_to(directory)): f'/api/v1/tasks/{task_id}/files/{quote(str(p.relative_to(directory)), safe="/")}'
                         for p in sorted(directory.rglob('*')) if p.is_file() and not p.is_symlink() and directory in p.resolve().parents}
        return task

    def file(self, task_id, name):
        self.read(task_id)
        directory = self.directory(task_id)
        path = directory / name
        if '\0' in name or Path(name).is_absolute() or '..' in Path(name).parts or directory not in path.resolve().parents or path.is_symlink() or not path.is_file():
            reject(404, 'task-file-not-found', 'file', '任务文件不存在')
        return path.read_bytes()

    def merge_result(self, key, name, task_id):
        task = self.read(task_id)
        if task['project'] != key or task['batch'] != name:
            reject(404, 'merge-result-not-found', 'task', '没有合并结果')
        path = self.directory(task_id) / 'scenario-file.yaml'
        if task['status'] not in ('已完成', '待同步') or not path.is_file():
            reject(404, 'merge-result-not-found', 'task', '没有合并结果')
        return path.read_bytes()

    def update(self, task_id, **values):
        with self.store.transaction() as db:
            db.execute('UPDATE tasks SET ' + ','.join(k+'=?' for k in values) + ' WHERE id=?', (*values.values(), task_id))

    def action(self, task_id, action):
        with self.lock:
            task = self.read(task_id)
            allowed = ('失败', '已中断', 'Codex 不可用') if action == 'retry' else ('排队中', '运行中', '已中断', '待同步', '待补充')
            if task['kind'] in ('check', 'execute'):
                allowed += ('已作废',) if action == 'retry' else ('待提交', '等待浏览器')
            if task['status'] not in allowed:
                reject(409, 'task-state-invalid', 'status', '任务当前状态不支持此操作')
            if action == 'cancel':
                self.update(task_id, status='已放弃', finished_at=now())
                process = self.processes.get(task_id)
                if process:
                    terminate_group(process)
                self.executions.candidates.close(task_id)
                if self.browser_owner == task_id:
                    self.browser_owner = None
            else:
                directory = self.directory(task_id)
                if directory.exists():
                    archive = directory / ('attempt-' + str(task['attempt']))
                    archive.mkdir()
                    for path in directory.iterdir():
                        if path.is_file() or (task['kind'] in ('check', 'execute') and path != archive and not path.name.startswith('attempt-')):
                            path.rename(archive / path.name)
                self.checks.next_review.pop(task_id, None)
                self.update(task_id, status='排队中', started_at=None, finished_at=None,
                            session_id=None, error='', result_digest=None, result_json='{}', snapshot_json='{}',
                            void_reason='', rounds_json='[]', attempt=task['attempt'] + 1)
        return dict(ok=True, task=self.read(task_id))

    def execute(self, task):
        directory = self.directory(task['id'])
        process = None
        error = ''
        session = None
        unavailable = False
        check = None
        outcome = "已完成"
        try:
            directory.mkdir(parents=True, exist_ok=True)
            snapshot = json.loads(task['input_json'])
            prompt = files('sop_board').joinpath('prompts/merge.md').read_text(encoding='utf-8')
            (directory / 'cases.yaml').write_text(yaml.safe_dump(dict(metadata=snapshot['case_metadata'], test_cases=snapshot['cases']), allow_unicode=True), encoding='utf-8')
            (directory / 'scenarios.yaml').write_text(snapshot['scenarios'], encoding='utf-8')
            (directory / 'entries.yaml').write_text(yaml.safe_dump(snapshot['entries'], allow_unicode=True), encoding='utf-8')
            (directory / 'nodes.json').write_text(canonical(snapshot['nodes']), encoding='utf-8')
            for source in snapshot['sop_sources']:
                target = directory / 'sop' / source['path']
                if directory not in target.resolve().parents or '..' in Path(source['path']).parts:
                    raise ValueError('SOP 原文路径越界')
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(source['content'], encoding='utf-8')
            (directory / 'input.json').write_text(task['input_json'], encoding='utf-8')
            (directory / 'prompt.md').write_text(prompt, encoding='utf-8')
            (directory / 'schema.json').write_text(canonical(SCHEMA), encoding='utf-8')
            with self.lock:
                if self.stopping.is_set() or self.read(task['id'])['status'] != '运行中':
                    return
                process = self.launcher(directory, prompt)
                self.processes[task['id']] = process
                self.starting = None
            try:
                code = process.wait(timeout=self.timeout)
            except subprocess.TimeoutExpired:
                terminate_group(process)
                code = process.returncode
                error = f'Codex 运行超时（{self.timeout:g} 秒）'
            if code:
                error = error or f'Codex 退出码 {code}'
            try:
                final = (directory / 'final.json').read_text(encoding='utf-8')
                if not final.strip():
                    raise FileNotFoundError()
                result = json.loads(final, object_pairs_hook=judgment_object,
                                    parse_constant=invalid_judgment_constant)
                if not error:
                    raw, check = assemble(snapshot, result)
                    if check['ok']:
                        check['pairs'] = [dict(case=c['id'], scenarios=[c['id']], note='同编号用例场景') for c in snapshot['cases']]
                        check['scenarios'] = result['scenarios']
                    if not check['ok']:
                        error = '; '.join(str(p) for p in check['problems'])
                    elif check.get('gaps'):
                        outcome = '待补充'
                    elif raw is not None:
                        (directory / 'scenario-file.yaml').write_text(raw, encoding='utf-8')
                        pairs = dict(covered=[dict(case=c['id'], scenarios=[c['id']], note='同编号用例场景') for c in snapshot['cases']], conflict=[], disposed=[])
                        (directory / 'pairs.json').write_text(canonical(pairs), encoding='utf-8')
                        with self.lock:
                            if not self.stopping.is_set() and self.read(task['id'])['status'] == '运行中':
                                try:
                                    outcome = self.land(task, raw, check)
                                except (OSError, ValueError, APIError) as problem:
                                    error = f'合并结果无法写入工作区：{problem}'
            except FileNotFoundError:
                error = error or 'Codex 没有写出最终输出'
            except (OSError, ValueError, RecursionError):
                error = error or '最终消息不是 JSON 对象'
            except (KeyError, TypeError):
                error = error or '最终消息不符合输出模式'
        except FileNotFoundError:
            error = 'Codex 启动失败：命令或任务文件不存在'
            unavailable = process is None
        except (OSError, subprocess.SubprocessError):
            error = 'Codex 运行失败：无法启动进程或读取任务文件'
        except (KeyError, TypeError, ValueError):
            error = '任务输入不合格，请重新合并'
        finally:
            if process:
                terminate_group(process)
            with self.lock:
                self.processes.pop(task['id'], None)
                if self.starting == task['id']:
                    self.starting = None
                events = directory / 'events.jsonl'
                if events.exists():
                    for line in events.read_text(encoding='utf-8', errors='replace').splitlines():
                        try:
                            event = json.loads(line)
                            if event.get('type') == 'thread.started' and isinstance(event.get('thread_id'), str):
                                session = event['thread_id']
                                break
                        except (ValueError, AttributeError):
                            continue
                if not self.stopping.is_set() and self.read(task['id'])['status'] == '运行中':
                    if check is None:
                        check = dict(ok=False, problems=[error])
                    elif error and check['ok']:
                        check.update(ok=False, problems=[error])
                    (directory / 'check.json').write_text(canonical(check), encoding='utf-8')
                    self.update(task['id'], status='Codex 不可用' if unavailable else '失败' if error else outcome,
                                finished_at=now(), session_id=session, error=error,
                                result_json=canonical(check),
                                result_digest=hashlib.sha256((directory / 'final.json').read_bytes()).hexdigest() if (directory / 'final.json').is_file() else None)

    def land(self, task, raw, check):
        root = self.service.writable_repos.get(task['project'])
        if not root:
            return '已完成'
        source = root / 'docs/sop' / (task['batch'] + '.yaml')
        if source.parent != root / 'docs/sop' or source.is_symlink() or root not in source.resolve().parents:
            raise ValueError('场景文件路径必须位于授权仓库的 docs/sop 目录内')
        if source.exists() and hashlib.sha256(source.read_bytes()).hexdigest() != task['scenario_file_sha256']:
            check['workspace_changed'] = True
            return '待同步'
        from .repositories import product, read_entries
        from .service import fingerprint
        if check.get('inferred_entries'):
            entry_file = root / 'docs/sop/entries.yaml'
            if root not in entry_file.resolve().parents or entry_file.exists():
                check['workspace_changed'] = True
                return '待同步'
            entry_file.parent.mkdir(parents=True, exist_ok=True)
            with entry_file.open('x', encoding='utf-8') as stream:
                stream.write('# 待确认：以下入口由 Codex 根据本次输入推断，请开发者核实。\n')
                stream.write(yaml.safe_dump(check['inferred_entries'], allow_unicode=True, sort_keys=False))
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(raw, encoding='utf-8')
        _, entry_path = read_entries(root)
        document = yaml.safe_load(raw)
        with self.store.connection() as db:
            project = dict(db.execute('SELECT * FROM projects WHERE key=?', (task['project'],)).fetchone())
        self.service.push(task['project'], task['batch'], dict(
            project=dict(id=project['identity'], name=project['name']),
            path=str(source.relative_to(root)), content=raw, pushed_by=f"看板合并 #{task['id']}",
            **product(root, source, entry_path),
            fingerprints={s['id']: fingerprint(s) for n in document['sops'] for s in n['scenarios']}),
            preserve_project_name=True)
        check['draft'] = False
        return '已完成'

    def run(self):
        while not self.stopping.wait(.05):
            with self.lock:
                self.workers = {i: w for i, w in self.workers.items() if w.is_alive()}
                self.check_workers.intersection_update(self.workers)
                with self.store.connection() as db:
                    checks = [dict(r) for r in db.execute("SELECT * FROM tasks WHERE kind='check' AND status IN ('排队中','待提交') ORDER BY id")]
                for task in checks:
                    try:
                        prepared = self.checks.prepare(task)
                    except (OSError, ValueError, KeyError, APIError, subprocess.SubprocessError) as error:
                        self.update(task['id'], status='失败', finished_at=now(), error=f'无法准备检查快照：{error}')
                        continue
                    if prepared:
                        self.update(task['id'], status='运行中', started_at=now())
                        worker = threading.Thread(target=self.checks.execute, args=(task,), daemon=True)
                        self.workers[task['id']] = worker
                        self.check_workers.add(task['id'])
                        worker.start()
                with self.store.connection() as db:
                    queued = [dict(r) for r in db.execute("SELECT * FROM tasks WHERE kind IN ('merge','execute') "
                        "AND status IN ('排队中','待提交','等待浏览器') ORDER BY id")]
                    running_batches = {(r['project'], r['batch']) for r in db.execute("SELECT project,batch FROM tasks WHERE kind='execute' AND status='运行中'")}
                for task in queued:
                    if self.stopping.is_set():
                        break
                    checks_only = False
                    if task['kind'] == 'execute':
                        if (task['project'], task['batch']) in running_batches:
                            continue
                        try:
                            if not self.checks.prepare(task):
                                continue
                        except (OSError, ValueError, KeyError, APIError, subprocess.SubprocessError) as error:
                            self.update(task['id'], status='失败', finished_at=now(), error=f'无法准备执行快照：{error}')
                            continue
                        scenes = json.loads(task['input_json'])['scenes']
                        checks_only = all((s.get('regression') or {}).get('check') for s in scenes)
                        declarations = json.loads(task['input_json'])['declarations']
                        web = any(not (s.get('regression') or {}).get('check') and declarations['entries'][s['verify']]['surface'] == 'web' for s in scenes)
                        if web and self.browser_owner not in (None, task['id']):
                            self.update(task['id'], status='等待浏览器')
                            continue
                        if web:
                            self.browser_owner = task['id']
                    if not checks_only and (self.starting is not None or len(self.workers) - len(self.check_workers) >= 3):
                        self.update(task['id'], status='排队中')
                        continue
                    if task['kind'] == 'merge':
                        self.availability_error = codex_available()
                        if self.availability_error:
                            self.update(task['id'], status='Codex 不可用', finished_at=now(), error=self.availability_error)
                            continue
                    self.update(task['id'], status='运行中', started_at=now())
                    if not checks_only:
                        self.starting = task['id']
                    else:
                        self.check_workers.add(task['id'])
                    worker = threading.Thread(target=self.executions.execute if task['kind'] == 'execute' else self.execute,
                                              args=(task,), daemon=True)
                    self.workers[task['id']] = worker
                    if task['kind'] == 'execute':
                        running_batches.add((task['project'], task['batch']))
                    worker.start()

    def close(self):
        self.stopping.set()
        with self.lock:
            for process in list(self.processes.values()):
                terminate_group(process)
            for task_id in list(self.candidate_processes):
                self.executions.candidates.close(task_id)
        self.thread.join()
        for worker in self.workers.values():
            worker.join()
