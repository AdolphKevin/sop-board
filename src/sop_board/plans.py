"""执行计划与连续运行（D91、D93）。

计划：待执行场景按“身份 → 首个页面 → 前置状态”分组，只读核对在前、改动数据在后，按轮次切分；
身份、页面与是否改动数据取自界面对照绑定（D85），没有绑定时取场景 actor，并按改动数据处理。
连续运行：批次没有进行中的执行时，按计划取下一轮排队；开轮前核对空闲内存、锁屏和待人处理事项，
同一问题挡住多个场景、执行任务连续失败时暂停并登记待人处理。
"""
import hashlib
import json
import os
import re
import subprocess
import time

from .kernel import S, sql_states, EXECUTION_QUEUE
from .service import canonical, now
from .statistics import calculate, pending_scope

MODES = ('pending', 'failed', 'regression')
ORDER = {'failed': 0, 'new': 1, 'changed': 1, 'source-changed': 2, 'rerun': 3}
PRIORITY = {'P0': 0, 'P1': 1, 'P2': 2}
DEFAULTS = dict(round_size=15, stop_when_blocking=5, min_free_memory=0.15)


def bindings(db, batch):
    return {row['scenario']: json.loads(row['body_json']) for row in db.execute(
        'SELECT b.scenario,b.body_json FROM atlas_bindings b JOIN scenarios s ON s.batch=b.batch AND s.scenario=b.scenario '
        'AND s.current=1 AND s.fingerprint=b.fingerprint WHERE b.batch=?', (batch['id'],))}


def build(db, batch, mode='pending', round_size=15, skip=()):
    if mode not in MODES:
        raise ValueError('计划模式应为 ' + '、'.join(MODES))
    if not isinstance(round_size, int) or not 1 <= round_size <= 25:
        raise ValueError('每轮场景数应为 1～25')
    summary = calculate(db, batch)
    details = {scene['scenario']: scene for scene in summary['scenarios']}
    if mode == 'failed':
        scope = [dict(scenario=s['scenario'], reason='failed') for s in summary['scenarios']
                 if not s['held'] and not s.get('waiting_change') and s['status'] in ('fail', 'blocked')]
    else:
        scope = pending_scope(db, batch, summary, batch['product_digest'])
        if mode == 'regression':
            scope = [item for item in scope if item['reason'] == 'source-changed']
    scope = [item for item in scope if item['scenario'] not in set(skip)]
    bound = bindings(db, batch)
    groups = {}
    for item in scope:
        scene = details[item['scenario']]
        binding = bound.get(item['scenario'])
        identity = (binding or {}).get('identity') or scene['body'].get('actor') or ''
        page = next((step['page'] for step in (binding or {}).get('steps', []) if step['page']), '')
        state = hashlib.sha256(canonical(scene['body'].get('initial_state', [])).encode()).hexdigest()[:8]
        entry = dict(scenario=item['scenario'], reason=item['reason'], priority=scene['priority'], identity=identity, page=page,
                     mutates=(binding or {}).get('mutates', True), bound=binding is not None)
        groups.setdefault((identity, page, state), []).append(entry)
    def rank(key):
        members = groups[key]
        return (min(ORDER.get(m['reason'], 3) for m in members), min(PRIORITY.get(m['priority'], 3) for m in members), key)
    rounds, current = [], []
    for key in sorted(groups, key=rank):
        members = sorted(groups[key], key=lambda m: (m['mutates'], ORDER.get(m['reason'], 3), PRIORITY.get(m['priority'], 3), m['scenario']))
        if current and len(current) + len(members) > round_size:
            rounds.append(current)
            current = []
        while len(members) > round_size:
            rounds.append(members[:round_size])
            members = members[round_size:]
        current += members
    if current:
        rounds.append(current)
    return dict(mode=mode, round_size=round_size, total=len(scope), groups=len(groups), rounds=rounds)


def free_memory_ratio():
    """可用内存占比；无法取得时返回 None，不因此阻止运行。"""
    try:
        if os.path.exists('/proc/meminfo'):
            info = dict(re.findall(r'(\w+):\s+(\d+)', open('/proc/meminfo').read()))
            return int(info['MemAvailable']) / int(info['MemTotal'])
        total = int(subprocess.run(['sysctl', '-n', 'hw.memsize'], capture_output=True, text=True, timeout=5).stdout)
        output = subprocess.run(['vm_stat'], capture_output=True, text=True, timeout=5).stdout
        size = int(re.search(r'page size of (\d+)', output)[1])
        pages = {name: int(value) for name, value in re.findall(r'Pages (free|inactive|speculative|purgeable):\s+(\d+)', output)}
        return sum(pages.values()) * size / total
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError):
        return None


def screen_locked():
    try:
        output = subprocess.run(['ioreg', '-n', 'Root', '-d1'], capture_output=True, text=True, timeout=5).stdout
        return bool(re.search(r'"CGSSessionScreenIsLocked"\s*=\s*Yes', output))
    except (OSError, subprocess.SubprocessError):
        return False


class Runs:
    def __init__(self, tasks):
        self.tasks, self.service, self.store = tasks, tasks.service, tasks.store
        self.checked = 0.0

    def settings(self):
        run = (getattr(self.service.workspace, 'run', None) or {})
        return {key: run.get(key, value) for key, value in DEFAULTS.items()}

    def read(self, db, batch):
        row = db.execute('SELECT * FROM batch_runs WHERE batch=?', (batch['id'],)).fetchone()
        return dict(row) if row else None

    def control(self, batch_name, action, *, mode='pending', round_size=None, by=''):
        if action not in ('start', 'pause', 'resume', 'stop'):
            raise ValueError('操作应为 start、pause、resume 或 stop')
        with self.store.transaction() as db:
            batch = self.service.batch(db, self.service.workspace.name, batch_name)
            current = self.read(db, batch)
            if action == 'start':
                size = round_size or self.settings()['round_size']
                build(db, batch, mode, size)
                db.execute('INSERT OR REPLACE INTO batch_runs(batch,mode,round_size,status,started_at,started_by,updated_at,reason,rounds_done,last_task) '
                           'VALUES (?,?,?,?,?,?,?,?,?,?)', (batch['id'], mode, size, 'running', now(), by, now(), '', 0, None))
            elif current is None:
                raise ValueError('该批次没有连续运行')
            else:
                status = dict(pause='paused', resume='running', stop='stopped')[action]
                db.execute('UPDATE batch_runs SET status=?,updated_at=?,reason=? WHERE batch=?',
                           (status, now(), '' if action == 'resume' else f'{by or "使用者"}{dict(pause="暂停", stop="停止")[action]}', batch['id']))
                if action == 'resume':
                    # 人已处理暂停原因：不再按上一轮的失败或受阻重判，从下一轮继续；本批次的“连续运行暂停”事项一并标为已处理。
                    db.execute('UPDATE batch_runs SET last_task=NULL WHERE batch=?', (batch['id'],))
                    db.execute("UPDATE attention_items SET resolved_at=?,resolved_by=?,resolution_note=? WHERE workspace=? AND batch=? "
                               "AND kind='run-paused' AND resolved_at IS NULL", (now(), by or '使用者', '继续连续运行', self.service.workspace.name, batch_name))
            return self.read(db, batch)

    def tick(self):
        """看板主循环调用：每 10 秒为运行中的批次排下一轮。"""
        if time.monotonic() - self.checked < 10:
            return
        self.checked = time.monotonic()
        with self.store.connection() as db:
            runs = [dict(r) for r in db.execute("SELECT r.*,b.name AS batch_name FROM batch_runs r JOIN batches b ON b.id=r.batch "
                                                 "WHERE r.status='running' AND b.workspace=?", (self.service.workspace.name,))]
        for run in runs:
            try:
                self.advance(run)
            except (ValueError, KeyError, OSError) as error:
                self.pause(run, f'连续运行出错：{error}')

    def pause(self, run, reason, *, attention=True):
        with self.store.transaction() as db:
            db.execute("UPDATE batch_runs SET status='paused',updated_at=?,reason=? WHERE batch=?", (now(), reason, run['batch']))
        if attention:
            from .attention import add
            add(self.store, self.service.workspace.name, kind='run-paused', text=reason, batch=run['batch_name'], created_by='连续运行')

    def advance(self, run):
        key, name = self.service.workspace.name, run['batch_name']
        with self.store.connection() as db:
            active = db.execute(f"SELECT id FROM tasks WHERE workspace=? AND batch=? AND kind='execute' AND status IN ({sql_states(EXECUTION_QUEUE | {S.RUNNING, S.SESSION})})",
                                (key, name)).fetchone()
            if active:
                return
            last = db.execute('SELECT * FROM tasks WHERE id=?', (run['last_task'],)).fetchone() if run['last_task'] else None
        settings = self.settings()
        if last is not None and last['status'] not in (S.COMPLETED,):
            if last['status'] in (S.FAILED, S.INTERRUPTED, S.UNAVAILABLE) and last['attempt'] < 2:
                self.tasks.action(last['id'], 'retry')
                return
            self.pause(run, f"执行任务 #{last['id']} 状态为{last['status']}，连续运行暂停")
            return
        if last is not None:
            blocking = self.blocking(last)
            if blocking and blocking[1] >= settings['stop_when_blocking']:
                self.pause(run, f'同一问题挡住 {blocking[1]} 个场景：{blocking[0][:120]}；请整合问题、修复后继续（执行任务 #{last["id"]}）')
                return
        ratio = free_memory_ratio()
        waiting = (f"等待：空闲内存 {ratio:.0%} 低于 {settings['min_free_memory']:.0%}" if ratio is not None and ratio < settings['min_free_memory']
                   else '等待：屏幕已锁定' if screen_locked() else '')
        if waiting or run['reason'].startswith('等待：'):
            with self.store.transaction() as db:
                db.execute('UPDATE batch_runs SET reason=?,updated_at=? WHERE batch=?', (waiting, now(), run['batch']))
        if waiting:
            return
        from .attention import open_scenarios
        with self.store.connection() as db:
            batch = self.service.batch(db, key, name)
            plan = build(db, batch, run['mode'], run['round_size'], skip=open_scenarios(db, key, name))
        if not plan['rounds']:
            with self.store.transaction() as db:
                db.execute("UPDATE batch_runs SET status='completed',updated_at=?,reason=? WHERE batch=?", (now(), '计划内的场景都已执行', run['batch']))
            return
        selected = [item['scenario'] for item in plan['rounds'][0]]
        result = self.tasks.executions.enqueue(key, name, trigger='连续运行', selected=selected)
        task_id = result['task']['id'] if result and result.get('task') else None
        with self.store.transaction() as db:
            db.execute('UPDATE batch_runs SET last_task=?,rounds_done=rounds_done+1,updated_at=? WHERE batch=?', (task_id, now(), run['batch']))

    def blocking(self, task):
        """上一轮受阻结果中最常见的同一问题及其场景数（D93、D98）：
        结果行明确归入问题的按该问题计，其余按受阻说明计；同一说明的场景全都属于同一个未关闭问题时，以该问题标注并合并。
        场景另属别的问题不拆开同一原因的受阻。"""
        rounds = json.loads(task['rounds_json'] or '[]')
        if not rounds:
            return None
        groups = {}
        from .issues import blocking_issues, common_issue
        with self.store.connection() as db:
            batch = self.service.batch(db, task['workspace'], task['batch'])
            for number in rounds:
                blocked = db.execute("SELECT r.id,r.scenario,r.finding FROM results r JOIN rounds t ON t.id=r.round_id "
                                     "WHERE t.batch=? AND t.number=? AND r.status='blocked'", (batch['id'], number)).fetchall()
                owners = blocking_issues(db, [row['id'] for row in blocked])
                for row in blocked:
                    key = ('issue', owners[row['id']]) if row['id'] in owners else ('finding', (row['finding'] or '').strip())
                    if key[1]:
                        groups.setdefault(key, set()).add(row['scenario'])
            merged = {}
            for (kind, text), scenes in groups.items():
                label = text if kind == 'issue' else (common_issue(db, task['workspace'], batch['name'], scenes) or text)
                merged.setdefault(label, set()).update(scenes)
        counts = {label: len(scenes) for label, scenes in merged.items()}
        return max(counts.items(), key=lambda item: item[1]) if counts else None


def command(arguments):
    """sop-board plan [工作区] <批次> [--plan 模式] [--round-size N] [--json]
    sop-board run [工作区] <批次> start|pause|resume|stop|status [--plan 模式] [--round-size N] --by <姓名>"""
    import argparse
    import sqlite3
    from pathlib import Path
    from .errors import APIError
    group, rest = arguments[0], arguments[1:]
    parser = argparse.ArgumentParser(prog='sop-board ' + group,
        description='查看执行计划' if group == 'plan' else '连续运行（由运行中的看板推进，看板未启动时不前进）')
    parser.add_argument('targets', nargs='+', help='[工作区] 批次' + ('' if group == 'plan' else ' 操作'))
    parser.add_argument('--data', type=Path)
    parser.add_argument('--plan', default='pending', choices=MODES)
    parser.add_argument('--round-size', type=int)
    parser.add_argument('--json', action='store_true')
    if group == 'run':
        parser.add_argument('--by', help='操作人姓名')
    args = parser.parse_args(rest)
    targets = list(args.targets)
    action = targets.pop() if group == 'run' else None
    if not targets or len(targets) > 2:
        parser.error('参数应为 [工作区] 批次' + ('' if group == 'plan' else ' 操作'))
    batch_name = targets.pop()
    app = None
    try:
        from .session import Session
        app = Session(targets[0] if targets else None, args.data)
        app.sync()
        runs = Runs(app.tasks)
        size = args.round_size or runs.settings()['round_size']
        if group == 'plan':
            with app.store.connection() as db:
                from .attention import open_scenarios
                result = dict(ok=True, **build(db, app.service.batch(db, app.key, batch_name), args.plan, size,
                                               skip=open_scenarios(db, app.key, batch_name)))
            if not args.json:
                lines = [f"{batch_name} · {args.plan} · 共 {result['total']} 个场景，{result['groups']} 组，{len(result['rounds'])} 轮（每轮至多 {size} 个）"]
                for number, items in enumerate(result['rounds'], 1):
                    lines.append(f'第 {number} 轮：' + '、'.join(f"{i['scenario']}（{i['identity'] or '—'}·{i['page'] or '—'}{'·改数据' if i['mutates'] else ''}）" for i in items))
                print('\n'.join(lines))
                return 0
        elif action == 'status':
            with app.store.connection() as db:
                result = dict(ok=True, run=runs.read(db, app.service.batch(db, app.key, batch_name)))
        else:
            if not args.by:
                raise ValueError('请用 --by 填写操作人姓名')
            result = dict(ok=True, run=runs.control(batch_name, action, mode=args.plan, round_size=args.round_size, by=app.person(args.by)))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except APIError as error:
        result = dict(ok=False, error_code=error.code, problems=error.problems)
    except (OSError, ValueError, sqlite3.Error) as error:
        result = dict(ok=False, problems=[dict(field=group, problem=str(error))])
    finally:
        if app:
            app.close()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1
