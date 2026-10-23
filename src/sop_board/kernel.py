"""任务公共机制：状态、会话、调度、进度及启动恢复。

业务类型提供 inputs / prepare / launch / consume / actions；执行与修复暂用适配器。
此模块不依赖 Tasks，也不决定某类任务的业务结果或工作树操作。
"""
from collections import OrderedDict
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import threading
import time

from .credentials import launch, shell_options


class Status:
    QUEUED = '排队中'
    WAIT_COMMIT = '待提交'
    WAIT_VALIDATION = '待验证'
    WAIT_BROWSER = '等待浏览器'
    WAIT_MERGE = '等待合并'
    RUNNING = '运行中'
    ANALYZING = '分析中'
    FIXING = '修复中'
    VERIFYING = '复测中'
    PROMOTING = '待合入'
    DISTILLING = '沉淀中'
    SESSION = '会话处理中'
    COMPLETED = '已完成'
    NEEDS_INPUT = '待补充'
    NEEDS_SYNC = '待同步'
    REVIEW = '待开发者审核'
    DEVELOPER = '待开发者处理'
    DEVELOPER_MERGE = '待开发者合入'
    INTERRUPTED = '已中断'
    VOID = '已作废'
    FAILED = '失败'
    UNAVAILABLE = 'Codex 不可用'
    CLEANUP_FAILED = '清理失败'
    HANDLED = '已处理'
    ABANDONED = '已放弃'
    MERGED = '已合入'


S = Status
WAITING = frozenset((S.WAIT_COMMIT, S.WAIT_VALIDATION, S.WAIT_BROWSER, S.WAIT_MERGE))
QUEUED = WAITING | {S.QUEUED}
RUNNING = frozenset((S.RUNNING, S.ANALYZING, S.FIXING, S.VERIFYING, S.PROMOTING, S.DISTILLING, S.SESSION))
ENDED = frozenset((S.COMPLETED, S.INTERRUPTED, S.VOID, S.FAILED, S.UNAVAILABLE, S.HANDLED, S.ABANDONED, S.MERGED))
DEVELOPER = frozenset((S.NEEDS_INPUT, S.NEEDS_SYNC, S.REVIEW, S.DEVELOPER, S.DEVELOPER_MERGE, S.CLEANUP_FAILED))
ALL_STATES = QUEUED | RUNNING | ENDED | DEVELOPER
OUTPUT_ACTIVE = frozenset((S.QUEUED, S.RUNNING))
EXECUTION_QUEUE = frozenset((S.QUEUED, S.WAIT_COMMIT, S.WAIT_BROWSER, S.WAIT_MERGE))
EXECUTION_ACTIVE = EXECUTION_QUEUE | {S.RUNNING, S.SESSION}
FIX_QUEUE = frozenset((S.QUEUED, S.WAIT_COMMIT, S.WAIT_BROWSER))
FIX_RUNNING = frozenset((S.ANALYZING, S.FIXING, S.VERIFYING, S.PROMOTING))
FIX_ACTIVE = FIX_QUEUE | FIX_RUNNING
FIX_LIVE = frozenset((S.ANALYZING, S.FIXING))
FIX_TERMINAL = frozenset((S.MERGED, S.HANDLED, S.ABANDONED))
FIX_RECOVERY = frozenset((S.INTERRUPTED, S.PROMOTING, S.DEVELOPER_MERGE, S.MERGED))
DISTILL_RETRY = frozenset((S.INTERRUPTED, S.FAILED, S.VOID))
DISTILL_TERMINAL = frozenset((S.HANDLED, S.ABANDONED))
DISTILL_RECOVERY = frozenset((S.INTERRUPTED, S.FAILED, S.DISTILLING))
OUTPUT_RETRY = frozenset((S.FAILED, S.INTERRUPTED, S.UNAVAILABLE))
OUTPUT_CANCEL = frozenset((S.QUEUED, S.RUNNING, S.INTERRUPTED, S.NEEDS_SYNC, S.NEEDS_INPUT))
MERGE_RESULTS = frozenset((S.COMPLETED, S.NEEDS_SYNC))
MERGE_DEDUP = OUTPUT_ACTIVE | OUTPUT_RETRY | MERGE_RESULTS | {S.NEEDS_INPUT, S.ABANDONED, S.SESSION}

FIX_ATTENTION = frozenset((S.DEVELOPER, S.DEVELOPER_MERGE))
FIX_RETRY = frozenset((S.INTERRUPTED, S.UNAVAILABLE))
FIX_NO_CANCEL = frozenset((S.MERGED, S.HANDLED, S.ABANDONED, S.PROMOTING))
MERGE_WAIT_UPDATE = frozenset((S.QUEUED, S.WAIT_MERGE))
MERGE_GAPS = frozenset((S.NEEDS_INPUT, S.COMPLETED))
GENERATE_CANCEL_BUTTON = frozenset((S.QUEUED, S.RUNNING, S.INTERRUPTED))
EXECUTION_RETRY = frozenset((S.FAILED, S.INTERRUPTED, S.UNAVAILABLE, S.VOID))
EXECUTION_CANCEL = frozenset((S.QUEUED, S.RUNNING, S.INTERRUPTED, S.NEEDS_SYNC, S.NEEDS_INPUT, S.WAIT_COMMIT, S.WAIT_BROWSER, S.WAIT_MERGE))
ATTEMPT_RESUMABLE = frozenset((S.RUNNING, S.INTERRUPTED, S.WAIT_COMMIT))


def sql_states(states):
    """仅用于本模块定义的状态常量，不接受外部 SQL 片段。"""
    return ','.join("'" + state.replace("'", "''") + "'" for state in sorted(states))


@dataclass(frozen=True)
class Policy:
    priority: int
    queued: frozenset
    running: frozenset
    interrupted: frozenset


# 重启名单保留旧行为：草稿、合并、执行的排队项也中断；沉淀等待项保持。
POLICIES = {
    'fix': Policy(0, FIX_QUEUE, FIX_RUNNING, FIX_RUNNING),
    'execute': Policy(1, EXECUTION_QUEUE, frozenset((S.RUNNING,)), EXECUTION_ACTIVE),
    'merge': Policy(2, frozenset((S.QUEUED,)), frozenset((S.RUNNING,)), OUTPUT_ACTIVE),
    'generate': Policy(2, frozenset((S.QUEUED,)), frozenset((S.RUNNING,)), OUTPUT_ACTIVE),
    'author-sop': Policy(2, frozenset((S.QUEUED,)), frozenset((S.RUNNING,)), OUTPUT_ACTIVE),
    'distill': Policy(3, frozenset((S.QUEUED, S.WAIT_COMMIT, S.WAIT_VALIDATION)),
                      frozenset((S.DISTILLING,)), frozenset((S.DISTILLING,))),
}
MAX_RUNNING = 3
# (作用范围，互斥类型)。包含同类任务，防止同工作区的两个执行同时占环境。
EXCLUSIONS = (
    ('workspace', frozenset(('execute', 'fix', 'distill'))),
    ('batch', frozenset(('merge', 'generate', 'execute'))),
    ('batch', frozenset(('merge', 'generate', 'distill'))),
)
# 等待已经排队的前置任务；优先级不同于业务依赖，不能仅靠优先级推导。
DEPENDENCIES = {
    'execute': (('workspace', frozenset(('fix',))), ('batch', frozenset(('merge', 'generate')))),
    'distill': (('workspace', frozenset(('fix', 'execute'))), ('batch', frozenset(('merge', 'generate')))),
}
KIND_NAMES = dict(fix='修复', execute='执行', merge='合并', generate='草稿', distill='沉淀')
KIND_NAMES['author-sop'] = '编写 SOP'
SCHEDULING_STATES = frozenset((S.SESSION,)).union(*(p.queued | p.running for p in POLICIES.values()))
TASK_COLUMNS = 'id,kind,workspace,batch,status'
_queue_view = ContextVar('task_queue_view', default=None)


def scheduling_tasks(db):
    """历史任务的输入、结果和快照不参与调度读取。"""
    return [dict(row) for row in db.execute(
        f'SELECT {TASK_COLUMNS} FROM tasks WHERE status IN ({sql_states(SCHEDULING_STATES)})')]


def session_scheduling_tasks(db):
    """会话同时核对运行状态与尚未完成环境恢复的看板工作线程。"""
    rows = {row['id']: dict(row) for row in scheduling_tasks(db)}
    for row in db.execute('SELECT t.id,t.kind,t.workspace,t.batch,t.status,w.pid FROM tasks t JOIN task_workers w ON w.task=t.id'):
        if process_alive(row['pid']):
            rows[row['id']] = dict(row, worker=True)
    return list(rows.values())


def queue(db, *, rows=None, claimed=None):
    if rows is None:
        rows = [dict(row) for row in db.execute(
            f'SELECT {TASK_COLUMNS} FROM tasks WHERE status IN ({sql_states(QUEUED)}) '
            'AND NOT EXISTS (SELECT 1 FROM session_tasks s WHERE s.task=tasks.id)')]
    claimed = claimed or set()
    return sorted((row for row in rows if row['id'] not in claimed and row['kind'] in POLICIES
                   and row['status'] in POLICIES[row['kind']].queued), key=task_order)


def task_order(task):
    return POLICIES.get(task['kind'], POLICIES['distill']).priority, task['id']


@contextmanager
def queue_view():
    """只读请求或一次批量读取共用队列；离开后丢弃，不缓存到下一次轮询。"""
    if _queue_view.get() is not None:
        yield
        return
    token = _queue_view.set({})
    try:
        yield
    finally:
        _queue_view.reset(token)


def queue_positions(db):
    view = _queue_view.get()
    if view is None:
        return {task['id']: index for index, task in enumerate(queue(db))}
    if 'positions' not in view:
        view['positions'] = {task['id']: index for index, task in enumerate(queue(db))}
    return view['positions']


def queued_ahead(db, task_id):
    return queue_positions(db).get(task_id)


def session_claim(db, task_id):
    row = db.execute('SELECT session_id,claimed_at FROM session_tasks WHERE task=?', (task_id,)).fetchone()
    return dict(row) if row else None


def claim_message(claim):
    return '由会话 ' + claim['session_id'] + ' 处理' if claim else ''


@dataclass(frozen=True)
class Launch:
    sandbox: str = 'read-only'
    environment: dict | None = None
    work_directory: Path | None = None
    writable_directories: tuple = ()
    schema: Path | None = None
    session: str | None = None
    output: str = 'final.json'


@dataclass(frozen=True)
class Inputs:
    snapshot: dict
    names: dict

    def changed(self, current):
        return [label for key, label in self.names.items() if self.snapshot.get(key) != current.get(key)]


class InputChanged(ValueError):
    """输入变化导致本轮作废，与输出不合格区分。"""


@dataclass(frozen=True)
class WaitCondition:
    name: str
    interval: float = 5


class TaskRepositories:
    """两种业务表保留原结构，任务工作树记录只经此边界读写。"""
    TABLES = {'fix': 'fix_repositories', 'distill': 'distill_repositories'}

    def __init__(self, store):
        self.store = store

    def rows(self, task_id, kind=None, *, connection=None):
        with (nullcontext(connection) if connection is not None else self.store.connection()) as db:
            kinds = (kind,) if kind else self.TABLES
            return [dict(row) for name in kinds for row in db.execute(
                f'SELECT * FROM {self.TABLES[name]} WHERE task=? ORDER BY alias', (task_id,))]

    def insert(self, kind, values, *, connection=None):
        with (nullcontext(connection) if connection is not None else self.store.transaction()) as db:
            db.execute(f'INSERT INTO {self.TABLES[kind]} (' + ','.join(values) + ') VALUES (' +
                       ','.join('?' for _ in values) + ')', tuple(values.values()))

    def update(self, kind, task_id, alias, *, connection=None, **values):
        with (nullcontext(connection) if connection is not None else self.store.transaction()) as db:
            db.execute(f'UPDATE {self.TABLES[kind]} SET ' + ','.join(k+'=?' for k in values) +
                       ' WHERE task=? AND alias=?', (*values.values(), task_id, alias))

    def contains(self, task_id, path, *, ancestors=False, unknown_is_worktree=True):
        path = Path(path).resolve()
        for row in self.rows(task_id):
            # 路径尚未登记时，不能把未知工作树当成可下载或可搬动的普通材料。
            if not row['worktree']:
                if unknown_is_worktree:
                    return True
                continue
            root = Path(row['worktree']).resolve()
            if path.is_relative_to(root) or ancestors and root.is_relative_to(path):
                return True
        return False


def events(path):
    """读取普通事件文件；半行、损坏记录与非对象不影响其他记录。"""
    if activity_stamp(path) is None:
        return
    try:
        with path.open(encoding='utf-8', errors='replace') as stream:
            for line in stream:
                try:
                    event = json.loads(line)
                except (ValueError, RecursionError):
                    continue
                if isinstance(event, dict):
                    yield event
    except OSError:
        return


def event_session(event):
    value = event.get('thread_id')
    return value if event.get('type') == 'thread.started' and isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,128}', value) else None


def session_id(directory, fallback=None):
    return next((value for event in events(Path(directory) / 'events.jsonl') if (value := event_session(event))), fallback)


def resume_command(session):
    return 'codex resume ' + session if isinstance(session, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,128}', session) else ''


def wait_codex(process, timeout, *, on_activity=None, timeout_message=None):
    deadline = time.monotonic() + timeout
    cancelled = getattr(process, 'task_cancelled', None)
    while True:
        if cancelled and cancelled():
            terminate_group(process)
            return process.wait()
        if on_activity:
            on_activity()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            terminate_group(process)
            raise ValueError(timeout_message or f'Codex 运行超时（{timeout:g} 秒）')
        try:
            return process.wait(timeout=min(.1, remaining) if on_activity or cancelled else remaining)
        except subprocess.TimeoutExpired:
            if (on_activity or cancelled) and time.monotonic() < deadline:
                continue
            terminate_group(process)
            raise ValueError(timeout_message or f'Codex 运行超时（{timeout:g} 秒）') from None


def process_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


class Kernel:
    def __init__(self, tasks, handlers):
        self.tasks, self.handlers = tasks, handlers
        self.activity_directories = {}
        self.worker_rows = {}
        self.repositories = TaskRepositories(tasks.store)
        self.next_checks = {}

    def check_due(self, task, condition):
        key = (task['id'], condition.name)
        current = time.monotonic()
        if current < self.next_checks.get(key, 0):
            return False
        self.next_checks[key] = current + condition.interval
        return True

    def wake(self, task_id):
        for key in list(self.next_checks):
            if key[0] == task_id:
                self.next_checks.pop(key)

    def wait_for(self, task, condition, predicate, active):
        while not self.tasks.stopping.is_set() and active():
            with self.tasks.lock:
                if self.check_due(task, condition) and predicate():
                    return True
            self.tasks.stopping.wait(min(condition.interval, .1))
        return False

    def attempt(self, task):
        if task['kind'] == 'fix':
            with self.tasks.store.connection() as db:
                number = db.execute('SELECT MAX(number) FROM fix_attempts WHERE task=?', (task['id'],)).fetchone()[0]
            return number or max(1, task['attempt'] + 1)
        return task['attempt']

    def archive(self, task, *, number=None, move=True, checkpoint=None, names=None):
        """归档保留旧版本，不覆盖已留证内容；工作树从不移动或复制。"""
        directory = self.tasks.directory(task['id'])
        if ((directory / '.git').exists() or directory in self.tasks.service.environment.worktrees.active
                or not directory.resolve().is_relative_to(self.tasks.store.data.resolve())):
            raise ValueError('任务材料目录不合格，保留现场，不能归档工作树或目录外文件')
        target = directory / 'attempts' / str(number or self.attempt(task))
        if checkpoint:
            if not re.fullmatch(r'[A-Za-z0-9_-]+', checkpoint):
                raise ValueError('归档阶段名称不合格')
            target = target / 'checkpoints' / checkpoint
        if target.exists() and any(target.iterdir()):
            base = target / 'checkpoints'
            sequence = 1
            while (base / str(sequence)).exists():
                sequence += 1
            target = base / str(sequence)
        target.mkdir(parents=True, exist_ok=True)
        for parent, folders, filenames in os.walk(directory, followlinks=False):
            parent = Path(parent)
            folders[:] = [name for name in folders if name != '.git'
                and not (parent == directory and (name in ('attempts', 'repos', 'interruptions') or re.fullmatch(r'attempt-[0-9]+', name)))
                and not (parent / name).is_symlink()
                and not self.repositories.contains(task['id'], parent / name, ancestors=True, unknown_is_worktree=False)]
            for name in filenames:
                path = parent / name
                if names is not None and (parent != directory or name not in names):
                    continue
                if not self.tasks.is_task_file(task['id'], path, archival=True):
                    continue
                destination = target / path.relative_to(directory)
                destination.parent.mkdir(parents=True, exist_ok=True)
                if move:
                    path.rename(destination)
                else:
                    shutil.copyfile(path, destination)
        from .service import canonical
        with self.tasks.store.connection() as db:
            record = db.execute('SELECT * FROM tasks WHERE id=?', (task['id'],)).fetchone()
        (target / 'task.json').write_text(canonical(dict(record) if record else task), encoding='utf-8')
        return target

    def receipt(self, task, *, connection=None, phase='result', number=None):
        with (nullcontext(connection) if connection is not None else self.tasks.store.connection()) as db:
            row = db.execute('SELECT result_json FROM task_receipts WHERE task=? AND attempt=? AND phase=?',
                (task['id'], number or self.attempt(task), phase)).fetchone()
        return json.loads(row[0]) if row else None

    def claim(self, task, *, connection=None):
        with (nullcontext(connection) if connection is not None else self.tasks.store.connection()) as db:
            return session_claim(db, task['id'])

    def assign(self, task_id, session, *, connection):
        from .service import now
        row = connection.execute('SELECT status FROM tasks WHERE id=?', (task_id,)).fetchone()
        if row is None:
            raise ValueError('任务不存在')
        previous = session_claim(connection, task_id)
        if previous:
            if previous['session_id'] != session:
                raise ValueError('任务已被其他会话认领')
            if row['status'] == S.SESSION:
                return
            raise ValueError(f'任务 #{task_id} 的认领已结束，不能复活旧任务')
        if row['status'] != S.QUEUED:
            raise ValueError(f'只可认领排队中的任务；#{task_id} 当前为{row["status"]}')
        connection.execute('INSERT OR IGNORE INTO session_tasks(task,session_id,claimed_at) VALUES (?,?,?)', (task_id, session, now()))
        connection.execute('UPDATE tasks SET status=?,started_at=COALESCE(started_at,?),finished_at=NULL,error=? WHERE id=?',
                           (S.SESSION, now(), '', task_id))

    def register(self, task, value, *, connection, phase='result', number=None):
        from .service import canonical, now
        connection.execute('INSERT OR IGNORE INTO task_receipts(task,attempt,phase,result_json,registered_at) VALUES (?,?,?,?,?)',
            (task['id'], number or self.attempt(task), phase, canonical(value), now()))

    def finish(self, task, values):
        with self.tasks.store.transaction() as db:
            claimed = session_claim(db, task['id'])
            if claimed and values['status'] in (S.FAILED, S.UNAVAILABLE):
                values = dict(values, status=S.SESSION, finished_at=None)
            elif claimed and values['status'] == S.NEEDS_SYNC:
                values = dict(values, status=S.VOID, void_reason=values.get('error') or
                    '；'.join(str(p) for p in json.loads(values.get('result_json', '{}')).get('problems', [])) or '任务输入已变化')
            db.execute('UPDATE tasks SET '+','.join(k+'=?' for k in values)+' WHERE id=?', (*values.values(), task['id']))
            if values['status'] != S.SESSION:
                self.register(task, values, connection=db)

    def launch(self, task, directory, prompt, options):
        self.activity_directories[task['id']] = Path(directory)
        if options.session:
            process = resume_codex(directory, options.session, prompt, sandbox=options.sandbox,
                environment=options.environment, work_directory=options.work_directory,
                writable_directories=options.writable_directories, schema=options.schema, output=options.output)
            return self.watch_process(task, process)
        arguments = {}
        if options.sandbox != 'read-only':
            arguments['sandbox'] = options.sandbox
        if options.environment is not None:
            arguments['environment'] = options.environment
        if options.work_directory is not None:
            arguments['work_directory'] = options.work_directory
        if options.writable_directories:
            arguments['writable_directories'] = options.writable_directories
        if options.schema is not None and options.schema != Path(directory) / 'schema.json':
            arguments['schema'] = options.schema
        if options.output != 'final.json':
            arguments['output'] = options.output
        return self.watch_process(task, self.tasks.launcher(directory, prompt, **arguments))

    def watch_process(self, task, process):
        def cancelled():
            with self.tasks.store.connection() as db:
                row = db.execute('SELECT status FROM tasks WHERE id=?', (task['id'],)).fetchone()
            return self.tasks.stopping.is_set() or row is None or row['status'] not in POLICIES[task['kind']].running
        process.task_cancelled = cancelled
        return process

    def recover(self):
        from .service import now
        with self.tasks.store.transaction() as db:
            for worker in db.execute('SELECT * FROM task_workers').fetchall():
                if not process_alive(worker['pid']):
                    db.execute('DELETE FROM task_workers WHERE task=?', (worker['task'],))
            for kind, policy in POLICIES.items():
                finished = 'COALESCE(finished_at,?)' if kind == 'distill' else '?'
                db.execute(f'UPDATE tasks SET status=?,finished_at={finished},error=? WHERE kind=? '
                    f'AND status IN ({sql_states(policy.interrupted)}) '
                    'AND NOT EXISTS (SELECT 1 FROM session_tasks s WHERE s.task=tasks.id)',
                    (S.INTERRUPTED, now(), '看板重启，沉淀已中断' if kind == 'distill' else '看板重启，任务已中断', kind))
        for handler in self.handlers.values():
            handler.recover()

    @staticmethod
    def same_scope(task, other, scope):
        return task['workspace'] == other['workspace'] and (scope == 'workspace' or task['batch'] == other['batch'])

    def blocker(self, task, rows):
        for other in rows:
            if other['id'] == task['id'] or other['kind'] not in POLICIES:
                continue
            policy = POLICIES[other['kind']]
            running = other.get('worker', False) or other['id'] in self.tasks.workers or other['status'] in policy.running or other['status'] == S.SESSION
            for scope, kinds in EXCLUSIONS:
                if running and task['kind'] in kinds and other['kind'] in kinds and self.same_scope(task, other, scope):
                    return other
            if other['status'] in policy.queued:
                for scope, kinds in DEPENDENCIES.get(task['kind'], ()):
                    if other['kind'] in kinds and self.same_scope(task, other, scope):
                        return other
        return None

    def session_blocker(self, task, rows):
        """会话不继承看板队列依赖，只等待实际占用或恢复环境的任务。"""
        for other in rows:
            if other['id'] == task['id'] or other['workspace'] != task['workspace'] or other['kind'] not in ('execute','fix','distill'):
                continue
            if other.get('worker') or other['id'] in self.tasks.workers or other['status'] == S.SESSION or other['status'] in POLICIES[other['kind']].running:
                return other
        return None

    def block(self, task, other):
        reason = f"等待{KIND_NAMES[other['kind']]}任务 #{other['id']} 结束"
        # 阻挡只更新原因；保留待提交、等待浏览器、待验证等更具体的状态。
        self.tasks.update(task['id'], error=reason)
        self.handlers[task['kind']].blocked(task, other)

    def browser(self, task, needed, *, waiting_status=None, reason='等待浏览器'):
        if not needed:
            return True
        if self.tasks.browser_owner not in (None, task['id']):
            values = dict(error=reason)
            if waiting_status and task['status'] == S.QUEUED:
                values['status'] = waiting_status
            self.tasks.update(task['id'], **values)
            return False
        self.tasks.browser_owner = task['id']
        return True

    def start(self, task, target, status, *, error=''):
        from .service import now
        self.tasks.update(task['id'], status=status, started_at=now(), finished_at=None, error=error)
        self.worker_rows[task['id']] = {key: task[key] for key in TASK_COLUMNS.split(',')} | {'status': status}
        self.tasks.starting = task['id']
        with self.tasks.store.transaction() as db:
            db.execute('INSERT OR REPLACE INTO task_workers(task,pid) VALUES (?,?)', (task['id'], os.getpid()))
        def run():
            try:
                target(task)
            finally:
                with self.tasks.lock, self.tasks.store.transaction() as db:
                    db.execute('DELETE FROM task_workers WHERE task=? AND pid=?', (task['id'], os.getpid()))
        worker = threading.Thread(target=run, daemon=True)
        self.tasks.workers[task['id']] = worker
        worker.start()

    def run(self):
        tasks = self.tasks
        while not tasks.stopping.wait(.1):
            with tasks.lock:
                tasks.workers = {i: w for i, w in tasks.workers.items() if w.is_alive()}
                self.worker_rows = {i: row for i, row in self.worker_rows.items() if i in tasks.workers}
                checking = tasks.service.environment.check_thread
                if checking and checking.is_alive():
                    continue
                if tasks.service.environment.approved():
                    tasks.fixes.discover()
                with tasks.store.connection() as db:
                    if tasks.service.automation_paused(db):
                        continue
                self.handlers['merge'].continue_waiting()
                with tasks.store.connection() as db:
                    rows = {row['id']: row for row in scheduling_tasks(db)}
                    # 已取消或已写入结束状态的工作线程可能仍在恢复环境，释放前继续互斥。
                    for task_id, row in self.worker_rows.items():
                        rows.setdefault(task_id, dict(row))
                    claimed = {row[0] for row in db.execute('SELECT task FROM session_tasks')}
                    candidates = queue(db, rows=rows.values(), claimed=claimed)
                    ordered = sorted(rows.values(), key=task_order)
                for task in candidates:
                    if tasks.stopping.is_set():
                        break
                    with tasks.store.connection() as db:
                        # 认领可以来自另一个会话，在每次准备前重新读取。
                        current = db.execute(f'SELECT {TASK_COLUMNS} FROM tasks WHERE id=?', (task['id'],)).fetchone()
                        if current is None:
                            continue
                        task.update(dict(current))
                        if session_claim(db, task['id']) or task['status'] not in POLICIES[task['kind']].queued:
                            continue
                    if task['id'] in tasks.workers:
                        continue
                    try:
                        blocker = self.blocker(task, ordered)
                        if blocker:
                            self.block(task, blocker)
                            continue
                        if tasks.starting is not None or len(tasks.workers) >= MAX_RUNNING:
                            tasks.update(task['id'], error='等待 Codex 名额')
                            continue
                        # 只有进入类型准备时才加载该任务的完整输入。
                        with tasks.store.connection() as db:
                            full = db.execute('SELECT * FROM tasks WHERE id=?', (task['id'],)).fetchone()
                        self.handlers[task['kind']].schedule(dict(full))
                    finally:
                        # 类型准备也可能直接写数据库；本轮后续任务必须看到新的状态。
                        with tasks.store.connection() as db:
                            fresh = db.execute(f'SELECT {TASK_COLUMNS} FROM tasks WHERE id=?', (task['id'],)).fetchone()
                        if fresh:
                            task.update(dict(fresh))

    def progress(self, task, *, local=False, connection=None):
        from contextlib import nullcontext
        directory = self.tasks.directory(task['id'])
        path = self.activity_directories.get(task['id'], directory) / 'events.jsonl'
        if task['kind'] == 'fix' and task['id'] not in self.activity_directories:
            # 复验借用修复任务的名额，但事件在单独目录；重启后也能找回最近活动。
            candidates = [path, *(directory / 'validations').glob('*/*/events.jsonl')]
            candidates = [p for p in candidates if self.tasks.is_task_file(task['id'], p)]
            path = max(candidates, key=lambda p: activity_stamp(p) or 0, default=path)
        result = {key: task.get(key) for key in ('id', 'status', 'started_at', 'finished_at', 'error')}
        result.update(timeout=self.tasks.timeout, elapsed=0, ahead=None, max_running=MAX_RUNNING)
        if task.get('started_at'):
            try:
                start = datetime.fromisoformat(task['started_at'].replace('Z', '+00:00'))
                end = datetime.fromisoformat(task['finished_at'].replace('Z', '+00:00')) if task.get('finished_at') else datetime.now(timezone.utc)
                result['elapsed'] = max(0, (end - start).total_seconds())
            except (ValueError, TypeError):
                pass
        with (nullcontext(connection) if connection is not None else self.tasks.store.connection()) as db:
            result['ahead'] = queued_ahead(db, task['id'])
            claim = session_claim(db, task['id'])
        result.update(session_task=claim, ownership=claim_message(claim))
        result.update(activity(path, local=local and self.tasks.is_task_file(task['id'], path)))
        if local:
            # 活动可来自复验；“继续会话”始终继续修复，不接管独立验收会话。
            if task['kind'] == 'fix':
                result['session_id'] = (session_id(directory, task.get('session_id'))
                    if self.tasks.is_task_file(task['id'], directory / 'events.jsonl') else task.get('session_id'))
            else:
                result['session_id'] = result.get('session_id') or task.get('session_id')
            result['resume'] = resume_command(result['session_id'])
            if claim:
                # 会话可能来自不同客户端或使用替代标签，不能猜成 Codex 的续接命令。
                result['resume'] = ''
        return result

    def output_values(self, task, status, check, session, error=''):
        import hashlib
        from .service import canonical, now
        path = self.tasks.directory(task['id']) / 'final.json'
        return dict(status=status, finished_at=now(), session_id=session, error=error,
                    result_json=canonical(check),
                    result_digest=hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None)

    def run_output(self, task):
        """草稿和合并的公共运行过程；consume 拥有校验、快照复核和登记。"""
        import sqlite3
        from .service import canonical
        tasks, handler = self.tasks, self.handlers[task['kind']]
        directory = tasks.directory(task['id'])
        process, check, options = None, None, None
        error, unavailable = '', False
        try:
            directory.mkdir(parents=True, exist_ok=True)
            prompt = handler.prepare(task)
            options = handler.launch(task)
            with tasks.lock:
                if tasks.stopping.is_set() or tasks.read(task['id'])['status'] != S.RUNNING:
                    return
                process = self.launch(task, directory, prompt, options)
                tasks.processes[task['id']] = process
                tasks.starting = None
            code = wait_codex(process, tasks.timeout)
            if code and options.session and not tasks.stopping.is_set():
                with tasks.lock:
                    if tasks.read(task['id'])['status'] != S.RUNNING:
                        return
                    terminate_group(process)
                    prompt = handler.fallback(task, prompt, codex_failure(directory, code))
                    options = handler.launch(task, fresh=True)
                    process = self.launch(task, directory, prompt, options)
                    tasks.processes[task['id']] = process
                code = wait_codex(process, tasks.timeout)
            if code:
                error = codex_failure(directory, code)
        except FileNotFoundError:
            error = 'Codex 启动失败：命令或任务文件不存在'
            unavailable = process is None
        except (OSError, subprocess.SubprocessError):
            error = 'Codex 运行失败：无法启动进程或读取任务文件'
        except ValueError as problem:
            error = str(problem)
        except (KeyError, TypeError):
            error = handler.invalid_input
        finally:
            if process:
                terminate_group(process)
            with tasks.lock:
                tasks.processes.pop(task['id'], None)
                if tasks.starting == task['id']:
                    tasks.starting = None
                session = session_id(directory, options.session if options else None)
                if not tasks.stopping.is_set() and tasks.read(task['id'])['status'] == S.RUNNING:
                    try:
                        if not error:
                            check = handler.consume(task, session)
                    except FileNotFoundError:
                        error = 'Codex 没有写出最终输出'
                    except (OSError, json.JSONDecodeError, RecursionError):
                        error = '最终消息不是 JSON 对象'
                    except ValueError as problem:
                        error = str(problem)
                    except (KeyError, TypeError):
                        error = '最终消息不符合输出模式'
                    except sqlite3.Error as problem:
                        error = '任务记录无法保存：' + str(problem)
                    if error:
                        check = dict(ok=False, problems=[error])
                        handler.finish(task, self.output_values(task,
                            S.UNAVAILABLE if unavailable else S.FAILED, check, session, error), check)
                    (directory / 'check.json').write_text(canonical(check), encoding='utf-8')


def parse_timeout(value):
    if value is None:
        return 1800
    if not re.fullmatch(r'[0-9]+', value.strip()) or int(value) <= 0:
        raise ValueError('SOP_BOARD_CODEX_TIMEOUT 超时取值不合规：应为正整数秒数')
    return int(value)


def codex_environment():
    allowed = {'PATH', 'HOME', 'USER', 'LANG', 'TMPDIR', 'CODEX_HOME', 'SHELL'}
    return {k: v for k, v in os.environ.items() if k in allowed or k.startswith('LC_')}


def codex_failure(directory, code, label='Codex', *, include_stderr=False):
    """失败事件的最近错误优先；摘要限 300 字，不以日志读取失败覆盖退出码。"""
    messages = []
    try:
        with (Path(directory) / 'events.jsonl').open(encoding='utf-8', errors='replace') as stream:
            for line in stream:
                try:
                    event = json.loads(line)
                except (ValueError, RecursionError):
                    continue
                if not isinstance(event, dict) or event.get('type') not in ('error', 'turn.failed'):
                    continue
                error = event.get('error')
                message = (error.get('message') if isinstance(error, dict) else error) or event.get('message')
                if isinstance(message, str) and message.strip():
                    message = ' '.join(message.split())[:300]
                    if message in messages:
                        messages.remove(message)
                    messages.insert(0, message)
                    messages = messages[:3]
    except OSError:
        pass
    summary = '；'.join(messages)
    if len(summary) > 300:
        summary = summary[:299] + '…'
    result = f'{label} 退出码 {code}' + ('：' + summary if summary else '')
    if include_stderr:
        try:
            lines = (Path(directory) / 'stderr.txt').read_text(encoding='utf-8', errors='replace').splitlines()
            detail = next((line for line in reversed(lines) if line.startswith('Error:')), '')
            if detail:
                result += '：' + detail[:300]
        except OSError:
            pass
    return result


def start_codex(directory, prompt, sandbox='read-only', environment=None, work_directory=None, writable_directories=(), schema=None, output="final.json"):
    """可替换的启动边界；子进程只得到白名单环境及独立进程组。"""
    directory = Path(directory).resolve()
    options = ['-c', 'sandbox_workspace_write.network_access=true'] if sandbox == 'workspace-write' else []
    options += shell_options(environment)
    for path in writable_directories:
        options.extend(['--add-dir', str(Path(path).resolve())])
    schema_path = Path(schema) if schema is not None else directory / 'schema.json'
    schema = ['--output-schema', str(schema_path)] if schema_path.is_file() else []
    return launch(['codex', 'exec', '--json', '-s', sandbox, *options, '--skip-git-repo-check',
        '-C', str(work_directory or directory), *schema, '-o', str(directory / output), prompt],
        directory, environment if environment is not None else codex_environment(), work_directory or directory)


def resume_codex(directory, thread_id, prompt, sandbox='read-only', work_directory=None, environment=None, writable_directories=(), schema=None, output="final.json"):
    """续接指定会话；resume 的沙箱参数位于 Codex 根命令层。"""
    directory = Path(directory).resolve()
    options = ['-c', 'sandbox_workspace_write.network_access=true'] if sandbox == 'workspace-write' else []
    options += shell_options(environment)
    for path in writable_directories:
        options.extend(['--add-dir', str(Path(path).resolve())])
    schema_path = Path(schema) if schema is not None else directory / 'schema.json'
    schema_args = ['--output-schema', str(schema_path)] if schema_path.is_file() else []
    return launch(['codex', '-s', sandbox, *options, 'exec', 'resume', thread_id, '--json',
        '--skip-git-repo-check', *schema_args, '-o', str(directory / output), prompt],
        directory, environment if environment is not None else codex_environment(), work_directory or directory)


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
    from .adapters import stop_process
    stop_process(process, grace=.2)



def activity_stamp(path):
    try:
        info = path.lstat()
        return info.st_mtime_ns if stat.S_ISREG(info.st_mode) else None
    except OSError:
        return None


def event_summary(item):
    kind = item.get('type')
    if kind == 'command_execution':
        label, value = '执行命令', item.get('command')
    elif kind == 'file_read':
        label, value = '读取文件', item.get('path')
    elif kind == 'file_change':
        changes = item.get('changes')
        if not isinstance(changes, list):
            return ''
        label, value = '修改文件', '、'.join(change['path'] for change in changes
            if isinstance(change, dict) and isinstance(change.get('path'), str))
    elif kind == 'mcp_tool_call':
        label, value = '调用工具', ' / '.join(item[key] for key in ('server', 'tool') if isinstance(item.get(key), str))
    elif kind == 'web_search':
        label, value = '搜索', item.get('query')
    elif kind in ('reasoning', 'agent_message', 'error'):
        label = {'reasoning': '推理摘要', 'agent_message': '会话消息', 'error': '错误'}[kind]
        value = item.get('text') or item.get('message')
    else:
        return ''
    if not isinstance(value, str) or not value.strip():
        return ''
    line = value.strip().splitlines()[0]
    return label + '：' + (line[:299] + '…' if len(line) > 300 else line)


def activity(path, *, local=False):
    stamp = activity_stamp(path)
    result = dict(last_activity=datetime.fromtimestamp(stamp / 1e9, timezone.utc).isoformat() if stamp is not None else None)
    if not local:
        return result
    result.update(events=[], session_id=None)
    if stamp is None:
        return result
    summaries = OrderedDict()
    for number, event in enumerate(events(path)):
        session = event_session(event)
        if session and result['session_id'] is None:
            result['session_id'] = session
        item = event.get('item')
        if event.get('type') not in ('item.started', 'item.updated', 'item.completed') or not isinstance(item, dict):
            continue
        summary = event_summary(item)
        if not summary:
            continue
        key = item.get('id') if isinstance(item.get('id'), str) else number
        summaries[key] = summary
        summaries.move_to_end(key)
        if len(summaries) > 20:
            summaries.popitem(last=False)
    result['events'] = list(summaries.values())
    return result
