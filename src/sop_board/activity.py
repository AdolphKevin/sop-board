"""守护进程发布的本机活动快照，以及无需数据库的读取。"""
from contextlib import contextmanager
from datetime import datetime
import itertools
import os
import sys
import threading
import time

from . import daemon, progress
from .store import now

FILENAME = 'activity.json'
KINDS = ('launches', 'runs', 'retest', 'distill')
MAX_AGE = 10


def _stamp(value):
    stamp = datetime.fromisoformat(value)
    if stamp.utcoffset() is None:
        raise ValueError('时间须带时区')
    return stamp.timestamp()


def _numbers(value):
    return isinstance(value, list) and all(type(item) is int and item > 0 for item in value) and value == sorted(set(value))


def _valid_progress(value):
    if not isinstance(value, dict):
        return False
    fields = ('total', 'adjudicated', 'registered', 'local')
    if set(value) != {*fields, 'counts', 'in_progress', 'last_activity_at'}:
        return False
    counts = value['counts']
    if not isinstance(counts, dict) or set(counts) != set(progress.STATES):
        return False
    if not all(type(count) is int and count >= 0 for count in [*(value[key] for key in fields), *counts.values()]):
        return False
    if not (sum(counts.values()) == value['adjudicated'] == value['registered'] + value['local'] + counts['voided'] <= value['total']):
        return False
    running = value['in_progress']
    if not isinstance(running, list) or not all(
            isinstance(item, dict) and set(item) == {'scenario', 'started_at'} and isinstance(item['scenario'], str)
            for item in running):
        return False
    for item in running:
        _stamp(item['started_at'])
    if value['last_activity_at'] is not None:
        _stamp(value['last_activity_at'])
    return True


def _valid(value, project):
    if (not isinstance(value, dict) or set(value) != {'version', 'project', 'pid', 'updated_at', 'checkouts'}
            or type(value['version']) is not int or value['version'] != 1 or value['project'] != project
            or type(value['pid']) is not int or not 0 < value['pid'] < 2**31 or not isinstance(value['checkouts'], list)):
        return False
    _stamp(value['updated_at'])
    names = []
    for item in value['checkouts']:
        if not isinstance(item, dict) or set(item) != {'checkout', *KINDS} or not isinstance(item['checkout'], str):
            return False
        names.append(item['checkout'])
        if not all(_numbers(item[k]) for k in ('launches', 'retest', 'distill')) or not isinstance(item['runs'], list):
            return False
        if not all(isinstance(run, dict) and set(run) == {'run', 'progress'} and _valid_progress(run['progress'])
                   for run in item['runs']):
            return False
        if not _numbers([run['run'] for run in item['runs']]):
            return False
    return names == sorted(set(names))


def unknown(project, reason, snapshot=None):
    return 2, dict(ok=False, project=project, state='unknown', reason=reason, snapshot=snapshot)


def read(data):
    project = data.name
    try:
        value = progress.read_json(data / FILENAME)
        if value is None and not (data / FILENAME).exists():
            return unknown(project, '活动文件缺失')
        if not _valid(value, project):
            return unknown(project, '活动文件格式或项目名称无效')
    except OSError:
        return unknown(project, '活动文件无法读取')
    except (ValueError, TypeError, OverflowError):
        return unknown(project, '活动文件格式无效')
    if abs(time.time() - _stamp(value['updated_at'])) > MAX_AGE:
        return unknown(project, '活动快照已过期', value)
    if not daemon.alive(value['pid']):
        return unknown(project, '守护进程已退出', value)
    busy = any(item[key] for item in value['checkouts'] for key in KINDS)
    return int(busy), dict(ok=True, project=project, state='busy' if busy else 'idle', reason=None, snapshot=value)


def text(result):
    if result['state'] == 'unknown':
        print(f"未知：{result['reason']}")
    else:
        print('忙碌' if result['state'] == 'busy' else '空闲')
    if value := result['snapshot']:
        print(f"项目 {value['project']}，快照 {value['updated_at']}，进程 {value['pid']}")
        for item in value['checkouts']:
            parts = [f"{key} " + '、'.join(f"#{row['run'] if key == 'runs' else row}" for row in item[key])
                     for key in KINDS if item[key]]
            if parts:
                print(f"{item['checkout']}：{'，'.join(parts)}")


class Activity:
    def __init__(self, project, store):
        self.project, self.store = project, store
        self.guard = threading.RLock()
        self.stopping = threading.Event()
        self.active = {}
        self.numbers = itertools.count(1)
        self.last, self.updated = None, 0
        self.thread = None

    def snapshot(self):
        from .launcher import finishing, read_attempts
        with self.store.local() as db:
            items = {r['id']: dict(checkout=r['name'], launches=[], runs=[], retest=[], distill=[])
                     for r in db.execute('SELECT id, name FROM checkouts WHERE removed_at IS NULL ORDER BY name')}
            for row in db.execute("SELECT * FROM launches WHERE status IN ('running','exited','failed') ORDER BY id"):
                attempts = read_attempts(row) if row['status'] != 'running' else []
                finishing_now = finishing(attempts) and not attempts[-1]['cleanup'].get('problem')
                if row['checkout'] in items and (row['status'] == 'running' or finishing_now):
                    items[row['checkout']]['launches'].append(row['id'])
            for row in db.execute("SELECT * FROM local_work WHERE kind='run' AND status='prepared' AND dir<>'' ORDER BY id"):
                if row['checkout'] in items:
                    items[row['checkout']]['runs'].append(dict(run=row['id'], progress=progress.summary(dict(row))))
        for number, (checkout, kind) in self.active.items():
            if checkout in items:
                items[checkout][kind].append(number)
        return list(items.values())

    def refresh(self):
        with self.guard:
            if self.stopping.is_set():
                return
            try:
                checkouts = self.snapshot()
                clock = time.monotonic()
                if checkouts != self.last or clock - self.updated >= 1.75:
                    progress.atomic_json(self.project.data / FILENAME, dict(version=1, project=self.project.name,
                                        pid=os.getpid(), updated_at=now(), checkouts=checkouts))
                    self.last, self.updated = checkouts, clock
            except Exception as error:  # 活动文件写失败时，命令仍按正常路径执行。
                if self.last is not None:
                    print(f'无法刷新活动文件（{type(error).__name__}）', file=sys.stderr)
                    self.last = None

    def loop(self):
        while not self.stopping.wait(.25):
            self.refresh()

    def start(self):
        self.refresh()
        self.thread = threading.Thread(target=self.loop, daemon=True, name='activity')
        self.thread.start()

    @contextmanager
    def track(self, checkout, kind):
        with self.guard:
            number = next(self.numbers)
            self.active[number] = (checkout.id, kind)
            self.refresh()
        try:
            yield
        finally:
            with self.guard:
                self.active.pop(number)
                self.refresh()

    def close(self):
        self.stopping.set()
        if self.thread:
            self.thread.join(timeout=2)
        with self.guard:
            try:
                path = self.project.data / FILENAME
                value = progress.read_json(path)
                if isinstance(value, dict) and value.get('pid') == os.getpid():
                    path.unlink(missing_ok=True)
            except (OSError, ValueError):
                pass
