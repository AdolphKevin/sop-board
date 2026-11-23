"""从本机启动记录和运行进度汇总执行用时与结果。"""
from datetime import date, datetime
import re

from . import ledger, progress
from .errors import reject
from .launcher import read_attempts
from .store import loads, now

CATEGORIES = (*ledger.CAUSE_KINDS, 'unclassified')
NUMBERS = ('duration_ms', 'sessions', 'continuations', 'total', 'adjudicated', 'pass', 'fail', 'skip', 'voided',
           'unadjudicated', 'pending_registration', 'unregistered', 'timed_out')


def date_filter(value):
    if value is None:
        return None
    try:
        if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value, re.ASCII):
            raise ValueError()
        return datetime.combine(date.fromisoformat(value), datetime.min.time()).astimezone()
    except ValueError:
        raise ValueError('--since 须为有效的日期（YYYY-MM-DD）') from None


def scenario_items(runs, initial_scope, at, *, store=None):
    if not runs:
        return [dict(run=None, scenario=key, status='unadjudicated', cause=None, registration=None, started_at=None,
                     adjudicated_at=None, duration_ms=None, timed_out=False) for key in sorted(initial_scope)], 0
    items, timeout_count = [], 0
    for work in runs:
        data = progress.observe(store, work)
        entries = data['adjudications']
        prepared = work['status'] == 'prepared'
        overdue = prepared and bool(progress.warning(data, at))
        # 进行中的场景按当前停滞判断是否超时；运行结束时仍在进行的取 unfinished 记下的判断。
        started = ({item['scenario']: dict(item, timed_out=overdue) for item in progress.in_progress(data)} if prepared
                   else {item['scenario']: item for item in data.get('unfinished', [])})
        timeout_keys = set(started) if overdue else set()
        for key in sorted(progress.scope(work) | entries.keys()):
            entry = entries.get(key)
            current = started.get(key) if not entry else None
            row = dict(run=work['id'], scenario=key, status='unadjudicated', cause=None, registration=None,
                       started_at=current['started_at'] if current else None, adjudicated_at=None,
                       duration_ms=None, timed_out=bool(current and current.get('timed_out')))
            if entry:
                row.update(status=entry['status'], cause=entry['cause'], registration=entry['registration'],
                           started_at=entry.get('started_at'), adjudicated_at=entry['at'],
                           duration_ms=entry.get('duration_ms'), timed_out=bool(entry.get('timed_out')))
                if row['registration'] == 'registered' and row['timed_out']:
                    timeout_keys.add(key)
            items.append(row)
        timeout_count += len(timeout_keys)
    return items, timeout_count


def describe(launch, runs, at, *, store=None):
    attempts = read_attempts(launch)
    first = attempts[0] if attempts else {}
    sessions = sum(item.get('pid') is not None for item in attempts) if attempts else int(bool(launch['pid'] or launch['session']))
    started = first.get('started_at') or launch['started_at']
    ended = None if launch['status'] == 'running' else (attempts[-1].get('ended_at') if attempts else None) or launch['ended_at']
    items, timed_out = scenario_items(runs, loads(launch['scope'], []), at, store=store)
    prepared = {run['id'] for run in runs if run['status'] == 'prepared'}
    result = dict.fromkeys(NUMBERS, 0)
    result.update(launch=launch['id'], checkout=launch['checkout_name'], runner=first.get('runner') or launch['agent'],
                  model=first.get('model'), started_at=started, ended_at=ended, duration_ms=progress.elapsed(started, ended or at),
                  sessions=sessions, continuations=max(0, sessions - 1), total=len(items), timed_out=timed_out,
                  scenarios=items, inconclusive=dict.fromkeys(CATEGORIES, 0))
    for item in items:
        registration, state = item['registration'], item['status']
        if registration is None:
            result['unadjudicated'] += 1
            continue
        result['adjudicated'] += 1
        if registration == 'local':
            result['pending_registration' if item['run'] in prepared else 'unregistered'] += 1
        elif state in ('blocked', 'not-executed'):
            cause = item['cause'] if item['cause'] in CATEGORIES else 'unclassified'
            result['inconclusive'][cause] += 1
        else:
            result[state] += 1
    return result


def stats(context, requested, since):
    requested = sorted(set(requested or []))
    with context.store.local() as db:
        launches = {row['id']: dict(row) for row in db.execute(
            "SELECT l.*, c.name AS checkout_name FROM launches l JOIN checkouts c ON c.id=l.checkout "
            "WHERE l.kind='run' ORDER BY l.id")}
        if missing := sorted(set(requested) - launches.keys()):
            reject(404, 'launch-not-found', 'launch', '没有执行启动：' + '、'.join(f'#{number}' for number in missing))
        selected = [launch for number, launch in launches.items()
                    if (number in requested if requested else launch['checkout'] == context.checkout.id)
                    and (since is None or datetime.fromisoformat(launch['started_at']) >= since)]
        result, at = [], now()
        for launch in selected:
            runs = [dict(row) for row in db.execute("SELECT * FROM local_work WHERE launch=? AND kind='run' AND dir<>'' ORDER BY id",
                                                   (launch['id'],))]
            result.append(describe(launch, runs, at, store=context.store))
    totals = {field: sum(row[field] or 0 for row in result) for field in NUMBERS}
    totals.update(launches=len(result), inconclusive={category: sum(row['inconclusive'][category] for row in result)
                                                     for category in CATEGORIES})
    return dict(ok=True, launches=result, totals=totals)
