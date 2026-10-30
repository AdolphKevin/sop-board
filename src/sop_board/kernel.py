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
import shlex
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
# SOP-BOARD-3：只与已完成、待同步、待补充、排队中或运行中（含会话处理中）的合并输入相同才不建任务；
# 失败、已中断、Codex 不可用与已放弃的输入再次出现时照常建任务。
MERGE_DEDUP = OUTPUT_ACTIVE | MERGE_RESULTS | {S.NEEDS_INPUT, S.SESSION}

FIX_ATTENTION = frozenset((S.DEVELOPER, S.DEVELOPER_MERGE))
FIX_RETRY = frozenset((S.INTERRUPTED, S.UNAVAILABLE))
FIX_NO_CANCEL = frozenset((S.MERGED, S.HANDLED, S.ABANDONED, S.PROMOTING))
MERGE_WAIT_UPDATE = frozenset((S.QUEUED, S.WAIT_MERGE))
MERGE_GAPS = frozenset((S.NEEDS_INPUT, S.COMPLETED))
GENERATE_CANCEL_BUTTON = frozenset((S.QUEUED, S.RUNNING, S.INTERRUPTED))
EXECUTION_RETRY = frozenset((S.FAILED, S.INTERRUPTED, S.UNAVAILABLE, S.VOID))
EXECUTION_CANCEL = frozenset((S.QUEUED, S.RUNNING, S.INTERRUPTED, S.NEEDS_SYNC, S.NEEDS_INPUT, S.WAIT_COMMIT, S.WAIT_BROWSER, S.WAIT_MERGE))
ATTEMPT_RESUMABLE = frozenset((S.RUNNING, S.INTERRUPTED, S.WAIT_COMMIT))


def failures_exhausted(db, task_id):
    """连续三次有效尝试都未通过。作废、中断、环境等不计次的结果跳过，不打断此前的累计（SOP-BOARD-5）。"""
    recent = [row[0] for row in db.execute(
        "SELECT outcome FROM fix_attempts WHERE task=? AND outcome IN ('未通过','通过') ORDER BY number DESC LIMIT 3", (task_id,))]
    return recent == ['未通过'] * 3


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
    # 梳理交互只读前端源码，不占用产品环境，不与执行、修复互斥（D84）。
    'atlas': Policy(2, frozenset((S.QUEUED,)), frozenset((S.RUNNING,)), OUTPUT_ACTIVE),
    'distill': Policy(3, frozenset((S.QUEUED, S.WAIT_COMMIT, S.WAIT_VALIDATION)),
                      frozenset((S.DISTILLING,)), frozenset((S.DISTILLING,))),
}
MAX_RUNNING = 3
# (作用范围，任务类型，与之互斥的运行中类型)。前三组包含同类任务，防止同工作区的两个执行同时占环境。
_ENVIRONMENT = frozenset(('execute', 'fix', 'distill'))
_BATCH_EXECUTE = frozenset(('merge', 'generate', 'execute'))
_BATCH_DISTILL = frozenset(('merge', 'generate', 'distill'))
EXCLUSIONS = (
    ('workspace', _ENVIRONMENT, _ENVIRONMENT),
    ('batch', _BATCH_EXECUTE, _BATCH_EXECUTE),
    ('batch', _BATCH_DISTILL, _BATCH_DISTILL),
    # 合并会改写修复快照所依据的场景：只在修复与合并之间互斥，不同批次的合并仍可并行（D72，2026-10-23 补充）。
    ('workspace', frozenset(('fix',)), frozenset(('merge',))),
    ('workspace', frozenset(('merge',)), frozenset(('fix',))),
)
# 等待已经排队的前置任务；优先级不同于业务依赖，不能仅靠优先级推导。
DEPENDENCIES = {
    'execute': (('workspace', frozenset(('fix',))), ('batch', frozenset(('merge', 'generate')))),
    'distill': (('workspace', frozenset(('fix', 'execute'))), ('batch', frozenset(('merge', 'generate')))),
}
KIND_NAMES = dict(fix='修复', execute='执行', merge='合并', generate='草稿', distill='沉淀')
KIND_NAMES['author-sop'] = '编写 SOP'
KIND_NAMES['atlas'] = '梳理交互'
SCHEDULING_STATES = frozenset((S.SESSION,)).union(*(p.queued | p.running for p in POLICIES.values()))
TASK_COLUMNS = 'id,kind,workspace,batch,status,started_at'
_queue_view = ContextVar('task_queue_view', default=None)


def scheduling_tasks(db):
    """历史任务的输入、结果和快照不参与调度读取。"""
    return [dict(row) for row in db.execute(
        f'SELECT {TASK_COLUMNS} FROM tasks WHERE status IN ({sql_states(SCHEDULING_STATES)})')]


def session_scheduling_tasks(db):
    """会话同时核对运行状态与尚未完成环境恢复的看板工作线程。"""
    rows = {row['id']: dict(row) for row in scheduling_tasks(db)}
    for row in db.execute('SELECT t.id,t.kind,t.workspace,t.batch,t.status,t.started_at,w.pid FROM tasks t JOIN task_workers w ON w.task=t.id'):
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


def same_scope(task, other, scope):
    """批次为空的任务（修复、会话独立复验）涉及整个工作区，按批次互斥时与任一批次相交。"""
    if task['workspace'] != other['workspace']:
        return False
    return scope == 'workspace' or not task['batch'] or not other['batch'] or task['batch'] == other['batch']


def excluded(task, other):
    return any(task['kind'] in kinds and other['kind'] in others and same_scope(task, other, scope)
               for scope, kinds, others in EXCLUSIONS)


def concurrent(task, other):
    """会话修复只改当前工作区的产品代码、不占用产品环境，批次执行开始后的产品变化也不使结果作废，两者互不阻挡。
    看板修复会切换环境，修复复验（批次为空的执行）须在固定产品上完成，仍与执行互斥。"""
    fix, execute = (task, other) if task['kind'] == 'fix' else (other, task)
    return (fix['kind'] == 'fix' and fix.get('status') == S.SESSION
            and execute['kind'] == 'execute' and bool(execute['batch']))


def depends(task, other):
    return any(other['kind'] in kinds and same_scope(task, other, scope)
               for scope, kinds in DEPENDENCIES.get(task['kind'], ()))


def find_blocker(task, rows, running):
    """互斥只看正在运行的任务；依赖只看仍在排队的前置任务。rows 按优先级排序。"""
    for other in rows:
        if other['id'] == task['id'] or other['kind'] not in POLICIES:
            continue
        if running(other) and excluded(task, other) and not concurrent(task, other):
            return other
        if other['status'] in POLICIES[other['kind']].queued and depends(task, other):
            return other
    return None


SLOT_REASON = '等待 Codex 名额'
WAIT_REASON = re.compile('等待(?:' + '|'.join(re.escape(name) for name in sorted(set(KIND_NAMES.values()))) + r')任务 #[0-9]+ 结束')


def wait_reason(other):
    return f"等待{KIND_NAMES[other['kind']]}任务 #{other['id']} 结束"


def is_wait_reason(error):
    """内核写入的阻挡原因；阻挡解除后应清除，类型自己的说明（如未提交路径）不在此列。"""
    return bool(error) and (error == SLOT_REASON or bool(WAIT_REASON.fullmatch(error)))


def row_running(row):
    policy = POLICIES.get(row['kind'])
    return bool(row.get('worker')) or row['status'] == S.SESSION or bool(policy and row['status'] in policy.running)


# 自身在等外部条件（提交、在新产品上通过）的任务，在其他任务能开始时不会先开始。
SELF_WAITING = frozenset((S.WAIT_COMMIT, S.WAIT_VALIDATION))


class _ScopeCounts:
    """按工作区、批次与类型计数，使每次阻挡判断不必遍历全部任务。"""

    def __init__(self):
        self.workspace, self.batch = {}, {}

    def add(self, row, delta):
        key = (row['workspace'], row['kind'])
        self.workspace[key] = self.workspace.get(key, 0) + delta
        key = (row['workspace'], row['batch'], row['kind'])
        self.batch[key] = self.batch.get(key, 0) + delta

    def any(self, task, scope, kinds):
        workspace = task['workspace']
        if scope == 'workspace' or not task['batch']:
            return any(self.workspace.get((workspace, kind), 0) > 0 for kind in kinds)
        return any(self.batch.get((workspace, task['batch'], kind), 0) > 0 or
                   self.batch.get((workspace, '', kind), 0) > 0 for kind in kinds)


def start_order(rows, claimed=frozenset()):
    """按当前互斥、依赖、优先级与运行名额推算排队任务的开始先后（估算）。

    每次在空闲名额内按优先级开始所有不被阻挡的任务；没有可开始的任务时，假定最早开始的
    运行中任务先结束，再重新挑选。待提交、待验证的任务要等外部条件，只在没有任何任务
    运行、其他任务也不能开始时才排进去。会话认领的任务占用环境但不占看板名额。
    """
    pending = queue(None, rows=rows, claimed=claimed)
    running, queued, fixes = _ScopeCounts(), _ScopeCounts(), _ScopeCounts()
    active = []

    def counts(row):
        # 会话修复与批次执行互不阻挡（见 concurrent），单独计数；核对其他任务时仍计入。
        return fixes if row['kind'] == 'fix' and row['status'] == S.SESSION else running

    # 已运行的任务按实际开始时间排列（先开始先结束）；没有开始时间的视为最早开始，同时刻按编号。
    for row in sorted((row for row in rows if row_running(row) and row['kind'] in POLICIES),
                      key=lambda row: (row.get('started_at') or '', row['id'])):
        active.append((row, row['status'] != S.SESSION))
        counts(row).add(row, 1)
    for task in pending:
        queued.add(task, 1)
    used = sum(1 for _, slot in active if slot)

    def blocked(task):
        queued.add(task, -1)
        try:
            occupied = (running,) if task['kind'] == 'execute' and task['batch'] else (running, fixes)
            return (any(c.any(task, scope, others) for c in occupied
                        for scope, kinds, others in EXCLUSIONS if task['kind'] in kinds) or
                    any(queued.any(task, scope, kinds) for scope, kinds in DEPENDENCIES.get(task['kind'], ())))
        finally:
            queued.add(task, 1)

    def begin(task):
        nonlocal used
        pending.remove(task)
        queued.add(task, -1)
        running.add(task, 1)
        active.append((task, True))
        used += 1
        order.append(task)

    def fill(include_waiting):
        started = False
        for task in list(pending):
            if used >= MAX_RUNNING:
                break
            if task['status'] in SELF_WAITING and not include_waiting:
                continue
            if not blocked(task):
                begin(task)
                started = True
        return started

    order = []
    while pending:
        fill(False)
        if not pending:
            break
        if active:
            # 名额已满或其余任务被运行中的任务挡住：最早开始的任务先结束后再挑选。
            row, slot = active.pop(0)
            counts(row).add(row, -1)
            if slot:
                used -= 1
            continue
        if fill(True):
            continue
        # 剩余任务互相等待时按优先级推进，避免推算停滞。
        begin(pending[0])
    return order


def queue_positions(db):
    view = _queue_view.get()
    if view is not None and 'positions' in view:
        return view['positions']
    rows = session_scheduling_tasks(db)
    claimed = {row[0] for row in db.execute('SELECT task FROM session_tasks')}
    positions = {task['id']: index for index, task in enumerate(start_order(rows, claimed))}
    if view is not None:
        view['positions'] = positions
    return positions


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


# 被阻挡期间核对具体等待条件的节奏；与类型自己的核对分开计时，阻挡解除后的开始不因此推迟。
BLOCKED_RECHECK = WaitCondition('阻挡期间的具体等待条件', 5)


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


def resume_command(session, directory=None):
    """续接命令：任务由 Claude 执行代理运行时（事件里 thread.started 带 runner），给出在原工作目录 claude --resume 的写法。"""
    if not isinstance(session, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', session):
        return ''
    started = next((event for event in events(Path(directory) / 'events.jsonl')
                    if event.get('type') == 'thread.started' and event.get('runner')), None) if directory else None
    if started and started.get('runner') == 'claude':
        cwd = started.get('cwd')
        return (f"cd {shlex.quote(cwd)} && " if isinstance(cwd, str) and cwd else '') + 'claude --resume ' + session
    return 'codex resume ' + session


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

    def agent(self, kind):
        """按任务类型取执行代理声明；缺省为 codex（D92）。"""
        agents = getattr(self.tasks.service.workspace, 'agents', None) or {}
        return agents.get(kind) or agents.get('default') or {'runner': 'codex'}

    def agent_available(self, kind):
        return claude_available() if self.agent(kind)['runner'] == 'claude' else codex_available()

    def launch(self, task, directory, prompt, options):
        self.activity_directories[task['id']] = Path(directory)
        agent = self.agent(task['kind'])
        if agent['runner'] == 'claude':
            return self.watch_process(task, start_claude(directory, prompt, agent, options))
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

    def running(self, row):
        return row_running(row) or row['id'] in self.tasks.workers

    def blocker(self, task, rows):
        return find_blocker(task, rows, self.running)

    def session_blocker(self, task, rows):
        """会话不继承看板队列依赖，只等待实际占用或恢复环境的任务，以及与之互斥的运行中任务。"""
        for other in sorted(rows, key=task_order):
            if other['id'] == task['id'] or other['workspace'] != task['workspace'] or other['kind'] not in POLICIES:
                continue
            if not self.running(other) or concurrent(task, other):
                continue
            if other['kind'] in _ENVIRONMENT or excluded(task, other):
                return other
        return None

    def block(self, task, other):
        handler = self.handlers[task['kind']]
        values = dict(error=wait_reason(other))
        # 阻挡只更新原因，保留待提交、等待浏览器、待验证等更具体的状态；这些条件在阻挡期间解除时
        # 改为解除后的状态，使显示的状态与原因一致。作废与开始前的输入核对仍留给类型准备。
        if task['status'] == S.WAIT_BROWSER and self.tasks.browser_owner in (None, task['id']):
            values['status'] = S.QUEUED
        elif task['status'] in SELF_WAITING and self.check_due(task, BLOCKED_RECHECK):
            with self.tasks.store.connection() as db:
                full = db.execute('SELECT * FROM tasks WHERE id=?', (task['id'],)).fetchone()
            status = handler.recheck(dict(full)) if full else None
            if status and status != task['status']:
                values['status'] = status
        if 'status' in values and handler.waiting:
            # 状态已随条件改变：清掉类型自身的核对期限，阻挡解除后同一轮即可准备，低优先级任务不能借此先开始。
            self.next_checks.pop((task['id'], handler.waiting.name), None)
        self.tasks.update(task['id'], **values)
        task.update(values)
        handler.blocked(task, other)

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
        started = now()
        self.tasks.update(task['id'], status=status, started_at=started, finished_at=None, error=error)
        self.worker_rows[task['id']] = {key: task.get(key) for key in TASK_COLUMNS.split(',')} | {'status': status, 'started_at': started}
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
                tasks.runs.tick()
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
                            tasks.update(task['id'], error=SLOT_REASON)
                            continue
                        # 只有进入类型准备时才加载该任务的完整输入。
                        with tasks.store.connection() as db:
                            full = dict(db.execute('SELECT * FROM tasks WHERE id=?', (task['id'],)).fetchone())
                        if is_wait_reason(full['error']):
                            # 阻挡已解除：清掉过时的等待原因，具体状态由类型准备重新判断。
                            tasks.update(task['id'], error='')
                            full['error'] = ''
                        self.handlers[task['kind']].schedule(full)
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
            result['resume'] = resume_command(result['session_id'], directory)
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


CLAUDE_READ_ONLY = ['--allowedTools', 'Read', 'Glob', 'Grep']
CLAUDE_WRITE = ['--permission-mode', 'acceptEdits', '--allowedTools', 'Bash', 'Read', 'Write', 'Edit', 'Glob', 'Grep']


def claude_environment(environment):
    base = dict(environment if environment is not None else codex_environment())
    for key, value in os.environ.items():
        if key == 'CLAUDE_CONFIG_DIR' or key.startswith('ANTHROPIC_'):
            base.setdefault(key, value)
    return base


def start_claude(directory, prompt, agent, options):
    """经 claude_runner 启动或续接 Claude Code；权限参数缺省按沙箱选只读或可写一组，配置的 args 优先。"""
    import sys
    from .credentials import launch
    directory = Path(directory).resolve()
    schema = Path(options.schema) if options.schema is not None else directory / 'schema.json'
    argv = [sys.executable, '-m', 'sop_board.claude_runner', '--directory', str(directory), '-o', str(directory / options.output)]
    if schema.is_file():
        argv += ['--schema', str(schema)]
    if agent.get('model'):
        argv += ['--model', agent['model']]
    if agent.get('effort'):
        argv += ['--effort', agent['effort']]
    if options.session:
        argv += ['--session', options.session, '--resume']
    work = Path(options.work_directory or directory).resolve()
    for path in {str(directory), str(work), *(str(Path(p).resolve()) for p in options.writable_directories)}:
        argv += ['--add-dir', path]
    argv += ['--work-directory', str(work), prompt, '--']
    argv += agent.get('args') or (CLAUDE_READ_ONLY if options.sandbox == 'read-only' else CLAUDE_WRITE)
    return launch(argv, directory, claude_environment(options.environment), work)


def claude_available():
    try:
        result = subprocess.run(['claude', '--version'], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, start_new_session=True, timeout=10)
        if result.returncode:
            return f'claude --version 失败（退出码 {result.returncode}）'
    except FileNotFoundError:
        return '未找到 claude 命令'
    except (OSError, subprocess.SubprocessError):
        return 'claude --version 失败：无法启动或超时'
    return ''


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
