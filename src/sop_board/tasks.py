"""持久任务与 Codex 进程入口。合并内容的业务核对由后续处理器承担。"""
import hashlib
import json
import re
import os
from pathlib import Path
import signal
import subprocess
import threading
from urllib.parse import quote

from .errors import reject, APIError
from .drafts import SCHEMA, assemble

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


def start_codex(directory, prompt, sandbox='read-only'):
    """可替换的启动边界；子进程只得到白名单环境及独立进程组。"""
    directory = Path(directory).resolve()
    events = (directory / 'events.jsonl').open('wb')
    stderr = (directory / 'stderr.txt').open('wb')
    try:
        return subprocess.Popen(['codex', 'exec', '--json', '-s', sandbox, '--skip-git-repo-check',
            '-C', str(directory), '--output-schema', str(directory / 'schema.json'),
            '-o', str(directory / 'final.json'), prompt], cwd=directory, env=codex_environment(),
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
        self.lock = threading.RLock()
        self.stopping = threading.Event()
        self.workers = {}
        self.processes = {}
        self.starting = None
        self.timeout = parse_timeout(os.environ.get('SOP_BOARD_CODEX_TIMEOUT')) if timeout is None else timeout
        with self.store.transaction() as db:
            db.execute("UPDATE tasks SET status='已中断',finished_at=?,error='看板重启，任务已中断' WHERE status IN ('排队中','运行中')", (now(),))
        self.availability_error = codex_available()
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
            if key in self.service.writable_repos:
                from .repositories import read_entries
                declared, _ = read_entries(self.service.writable_repos[key])
                entries = dict(entries=declared)
            snapshot = dict(cases=cases, scenarios=version['content'] if version else '', entries=entries,
                            batch=name, case_metadata=json.loads(upload['metadata']), upload=upload['number'])
            scenario_sha = version['sha256'] if version else ''
            digest = hashlib.sha256(canonical(snapshot).encode()).hexdigest()
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
            if task['status'] not in allowed:
                reject(409, 'task-state-invalid', 'status', '任务当前状态不支持此操作')
            if action == 'cancel':
                self.update(task_id, status='已放弃', finished_at=now())
                process = self.processes.get(task_id)
                if process:
                    terminate_group(process)
            else:
                directory = self.directory(task_id)
                if directory.exists():
                    archive = directory / ('attempt-' + str(task['attempt']))
                    archive.mkdir()
                    for path in directory.iterdir():
                        if path.is_file():
                            path.rename(archive / path.name)
                self.update(task_id, status='排队中', started_at=None, finished_at=None,
                            session_id=None, error='', result_digest=None, attempt=task['attempt'] + 1)
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
            prompt = '读取 cases.yaml、scenarios.yaml、entries.yaml。按 schema.json 返回用例角色和入口、节点、原有场景去向及缺口。只读取快照，不运行 Git 写操作，不访问网络。'
            (directory / 'cases.yaml').write_text(yaml.safe_dump(dict(metadata=snapshot['case_metadata'], test_cases=snapshot['cases']), allow_unicode=True), encoding='utf-8')
            (directory / 'scenarios.yaml').write_text(snapshot['scenarios'], encoding='utf-8')
            (directory / 'entries.yaml').write_text(yaml.safe_dump(snapshot['entries'], allow_unicode=True), encoding='utf-8')
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
                result = json.loads(final)
                if not error:
                    raw, check = assemble(snapshot, result)
                    if not check['ok']:
                        error = '; '.join(str(p) for p in check['problems'])
                    elif check.get('gaps'):
                        outcome = '待补充'
                    elif raw is not None:
                        (directory / 'scenario-file.yaml').write_text(raw, encoding='utf-8')
                        root = self.service.writable_repos.get(task['project'])
                        if root:
                            source = root / 'docs/sop' / (task['batch'] + '.yaml')
                            if root not in source.resolve().parents:
                                error = '场景文件路径越出授权仓库'
                            elif source.exists() and hashlib.sha256(source.read_bytes()).hexdigest() != task['scenario_file_sha256']:
                                outcome = '待同步'
                                check['workspace_changed'] = True
            except FileNotFoundError:
                error = error or 'Codex 没有写出最终输出'
            except (OSError, ValueError):
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
                    (directory / 'check.json').write_text(canonical(check if check and not error else dict(ok=False, problems=[error])), encoding='utf-8')
                    self.update(task['id'], status='Codex 不可用' if unavailable else '失败' if error else outcome,
                                finished_at=now(), session_id=session, error=error,
                                result_digest=hashlib.sha256((directory / 'final.json').read_bytes()).hexdigest() if (directory / 'final.json').is_file() else None)

    def run(self):
        while not self.stopping.wait(.05):
            with self.lock:
                self.workers = {i: w for i, w in self.workers.items() if w.is_alive()}
                if self.starting is not None or len(self.workers) >= 3:
                    continue
                with self.store.connection() as db:
                    row = db.execute("SELECT * FROM tasks WHERE status='排队中' ORDER BY id LIMIT 1").fetchone()
                if not row or self.stopping.is_set():
                    continue
                task = dict(row)
                self.availability_error = codex_available()
                if self.availability_error:
                    self.update(task['id'], status='Codex 不可用', finished_at=now(), error=self.availability_error)
                    continue
                self.update(task['id'], status='运行中', started_at=now())
                self.starting = task['id']
                worker = threading.Thread(target=self.execute, args=(task,), daemon=True)
                self.workers[task['id']] = worker
                worker.start()

    def close(self):
        self.stopping.set()
        with self.lock:
            for process in list(self.processes.values()):
                terminate_group(process)
        self.thread.join()
        for worker in self.workers.values():
            worker.join()
