"""本机计时、裁决进度和业务提交后的恢复。"""
from datetime import datetime
import json
import os
from pathlib import Path
import tempfile

from .store import Lock, dumps, loads, now
from . import works

STATES = ('pass', 'fail', 'blocked', 'skip', 'not-executed', 'voided')
LIMIT = 900
FILE = 'progress.json'
PENDING = 'progress.pending.json'


def atomic_bytes(path, payload):
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.' + path.name + '-', delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(payload)
            stream.flush()
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def atomic_json(path, value):
    atomic_bytes(path, (dumps(value) + '\n').encode())


def read_json(path, default=None):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return default


def scope(work):
    return {item['scenario'] for item in loads(work['scope'], [])}


def item(key, fingerprint, state, at, *, session=None, registration='local', **fields):
    return dict(scenario=key, fingerprint=fingerprint, status=state, cause=None, suspect_defect=False,
                finding=None, at=at, session=session, result=None, registration=registration) | fields


def initial(limit, at, adjudications=()):
    return dict(version=1, limit_seconds=limit, last_activity_at=at, active=None,
                adjudications={row['scenario']: row for row in adjudications})


def save(directory, data):
    atomic_json(Path(directory) / FILE, data)


def path_of(work):
    return Path(work['dir']) if work.get('dir') else None


def legacy(work):
    result = loads(work.get('result'), {})
    entries = []
    for row in result.get('not_executed', []):
        entries.append(item(row['scenario'], row.get('fingerprint'), 'not-executed', row.get('at') or work.get('ended_at'),
                            registration='registered', source=row.get('source'), cause=row.get('cause'),
                            finding=row.get('finding'), suspect_defect=bool(row.get('suspect_defect'))))
    for row in result.get('recorded', []):
        entry = item(row['scenario'], None, row['status'], None, session=None, registration='registered')
        entries.append(entry | row)
    for key in result.get('voided', []):
        entries.append(item(key, None, 'voided', None, registration='voided'))
    return initial(LIMIT, work.get('ended_at') or work.get('created_at'), entries)


def read(work):
    directory = path_of(work)
    if directory is not None:
        try:
            value = read_json(directory / FILE)
        except (ValueError, OSError):
            value = None
        if (isinstance(value, dict) and value.get('version') == 1 and isinstance(value.get('adjudications'), dict)
                and type(value.get('limit_seconds')) is int and 1 <= value['limit_seconds'] <= 86400):
            return value
    return legacy(work)


def business(db, store, run):
    record = works.existing(db, store, run)
    return db.one('SELECT id, result, ended_at, implementer, implemented_by FROM work WHERE id=?', (record,)) if record else None


def stage(directory, data, expected, *, ended_at=None):
    atomic_json(directory / PENDING, dict(progress=data, expected=expected, ended_at=ended_at))


def publish(directory, data):
    save(directory, data)
    (directory / PENDING).unlink(missing_ok=True)


def recover(store, work):
    """有未确认的文件时先核对业务事务。失败时保留上次可读进度。调用方持有本轮锁。"""
    directory = path_of(work)
    data = read(work)
    if not directory:
        return data
    pending = read_json(directory / PENDING)
    if pending:
        with store.records() as db:
            record = business(db, store, work['id'])
        registered = loads(record['result'], {}).get('adjudications', {}) if record else {}
        matches = all(registered.get(key, {}).get('seq') == seq for key, seq in pending['expected'].items())
        ended_at = pending['ended_at']
        if record and matches and (ended_at is None or record['ended_at'] == ended_at):
            recovered = pending['progress']
            if work['status'] == 'abandoned':
                active = recovered['active']
                if active:
                    recovered['last_active'] = dict(active, timed_out=bool(warning(recovered, active, work['ended_at'])))
                recovered.update(active=None, ended_at=work['ended_at'], last_activity_at=work['ended_at'])
            publish(directory, recovered)
            return recovered
        (directory / PENDING).unlink(missing_ok=True)
    return data


def lock(work):
    return Lock(Path(work['dir']) / '.progress.lock')


def observe(store, work):
    """只在原子替换未完成时核对业务记录，读取失败时仍提供最近本机快照。"""
    from .errors import APIError
    directory = path_of(work)
    if store and directory and (directory / PENDING).exists():
        try:
            with lock(work):
                return recover(store, work)
        except (APIError, OSError, ValueError):
            pass
    return read(work)


def public_item(row):
    fields = ('scenario', 'fingerprint', 'status', 'cause', 'suspect_defect', 'finding', 'at', 'session', 'result', 'registration', 'note')
    return {key: row[key] for key in fields if key in row}


def unadjudicated(work, data):
    return sorted(scope(work) - data['adjudications'].keys())


def local_verdicts(data):
    return [{k: item[k] for k in ('scenario', 'status', 'cause', 'finding', 'suspect_defect')}
            for _, item in sorted(data['adjudications'].items()) if item['registration'] == 'local']


def elapsed(started, at):
    if not started or not at:
        return None
    return max(0, int((datetime.fromisoformat(at) - datetime.fromisoformat(started)).total_seconds() * 1000))


def warning(data, active, at):
    if not active:
        return []
    duration = elapsed(active['started_at'], at)
    if duration is None or duration <= data['limit_seconds'] * 1000:
        return []
    return [dict(code='scenario-timeout', scenario=active['scenario'], started_at=active['started_at'],
                 limit_seconds=data['limit_seconds'], elapsed_seconds=duration / 1000)]


def summary(work, data=None):
    data = read(work) if data is None else data
    entries = data['adjudications']
    counts = dict.fromkeys(STATES, 0)
    for row in entries.values():
        counts[row['status']] += 1
    active = (data['active'] or {}) if work['status'] == 'prepared' else {}
    return dict(total=len(scope(work) | entries.keys()), adjudicated=len(entries), counts=counts,
                registered=sum(r['registration'] == 'registered' for r in entries.values()),
                local=sum(r['registration'] == 'local' for r in entries.values()),
                current=active.get('scenario'), started_at=active.get('started_at'), last_activity_at=data['last_activity_at'])


def view(work, *, local=True, data=None):
    if work['kind'] != 'run':
        return {}
    data = read(work) if data is None else data
    result = dict(progress=summary(work, data))
    if local:
        result['local_verdicts'] = local_verdicts(data)
    return result


def run_number(explicit):
    if explicit is not None:
        return explicit
    configured = os.environ.get('SOP_BOARD_RUN', '')
    return int(configured) if configured.isascii() and configured.isdecimal() else None
