"""运行准备的排队与资源分配（已定决策 28）。

一台机器一套环境，多个运行轮流用、并行用：
- 环境由检出的 prepared 运行占住；port_step 为 0 时只有一套环境，别的检出等它空闲，空闲时直接换过去。
- 身份租约整机独占：同一个身份同一时刻只给一个运行或一位真人。
- 有服务被某个运行停着，或代码变了要重启环境而还有别的运行在用时，新运行等着。

run prepare 先在本机库登记运行（占住范围内的场景），再调 /local/run/acquire 一次取得环境与身份。
条件满足、前面也没有冲突的等待者就分配；否则不带 --wait 立即返回 env-busy / browser-busy，带 --wait 时排队。
队列只在守护进程内存里，按到达先后；等待者按会话编号识别，请求结束后 GRACE 秒内同一会话再来保留原位置。
"""
from datetime import datetime, timezone
import threading
import time

from .errors import APIError

GRACE = 60


class Waiter:
    def __init__(self, session, checkout, run, identities, pid):
        self.session, self.checkout, self.run, self.pid = session, checkout, run, pid
        self.identities = set(identities)
        self.since = datetime.now(timezone.utc)
        self.active, self.ended = True, None

    def public(self, position):
        return dict(position=position, checkout=self.checkout.name, run=self.run, session=self.session,
                    identities=sorted(self.identities), waited=int((datetime.now(timezone.utc) - self.since).total_seconds()),
                    waiting=self.active)


class Gate:
    def __init__(self, app):
        self.app = app
        self.cond = threading.Condition()
        self.queue = []
        self.granted = {}   # 已分配、运行目录还没写好的运行 → 检出 id

    # ---- 现状 ----

    def shared_environment(self):
        return not self.app.project.adapter.get('port_step', 0)

    def holders(self, exclude=None):
        """占用环境的运行 → 检出 id：已准备好的 prepared 运行，加上刚分配、运行目录还没写好的。"""
        with self.app.store.local() as db:
            rows = db.execute("SELECT id, checkout, dir FROM local_work WHERE kind='run' AND status='prepared'").fetchall()
        prepared = {r['id']: r['checkout'] for r in rows}
        result = {r['id']: r['checkout'] for r in rows if r['dir']}
        for run, checkout in list(self.granted.items()):
            if run in prepared:
                result[run] = checkout
            else:
                self.granted.pop(run, None)
        result.pop(exclude, None)
        return result

    def stopped_services(self, exclude=None):
        """被 prepared 运行停着的服务：[(服务, 运行, 检出 id)]。"""
        with self.app.store.local() as db:
            rows = db.execute("SELECT e.work, e.service, e.action, e.checkout FROM service_events e "
                              "JOIN local_work w ON w.id=e.work WHERE w.status='prepared' ORDER BY e.id").fetchall()
        last = {}
        for row in rows:
            last[row['work'], row['service']] = (row['action'], row['checkout'])
        return [(service, work, checkout) for (work, service), (action, checkout) in last.items()
                if action == 'stop' and work != exclude]

    def names(self):
        with self.app.store.local() as db:
            return {r['id']: r['name'] for r in db.execute('SELECT id, name FROM checkouts')}

    def snapshot(self):
        """看板概况页：环境当前属于哪个检出、哪些运行与身份在用、谁在等。"""
        with self.cond:
            self.expire()
            holders, names = self.holders(), self.names()
            with self.app.store.local() as db:
                leases = {r['identity']: r['holder'] for r in db.execute('SELECT identity, holder FROM browser_instances WHERE holder IS NOT NULL')}
            runs = [dict(run=run, checkout=names.get(checkout), identities=sorted(i for i, h in leases.items() if h == f'run:{run}'))
                    for run, checkout in sorted(holders.items())]
            checkouts = sorted({item['checkout'] for item in runs if item['checkout']})
            return dict(holder=dict(checkouts=checkouts, runs=runs),
                        waiting=[w.public(i + 1) for i, w in enumerate(self.queue)])

    # ---- 排队 ----

    def expire(self):
        moment = time.monotonic()
        self.queue = [w for w in self.queue if w.active or moment - w.ended < GRACE]

    def check(self, checkout, run, identities, digest, waiter):
        """能分配就返回 None；否则返回 (错误码, 说明, 占用者)。调用方持有 self.cond。"""
        holders, names = self.holders(exclude=run), self.names()
        shared = self.shared_environment()
        if shared:
            others = sorted(r for r, c in holders.items() if c != checkout.id)
            if others:
                owner = names.get(holders[others[0]])
                return ('env-busy', f"环境正由检出 {owner} 的运行 {'、'.join(f'#{r}' for r in others)} 使用",
                        dict(kind='environment', checkout=owner, runs=others))
        for service, work, owner in self.stopped_services(exclude=run):
            if shared or owner == checkout.id:
                return ('env-busy', f'服务 {service} 正被运行 #{work} 停着，它提交、放弃或启动服务之后才能在这套环境上准备新的运行',
                        dict(kind='service', service=service, run=work))
        same = sorted(r for r, c in holders.items() if shared or c == checkout.id)
        environment = self.app.envs.environment(checkout)
        if same and digest and environment.alive() and environment.digest and environment.digest != digest:
            return ('env-busy', f"本检出的代码变了，环境要重启，而运行 {'、'.join(f'#{r}' for r in same)} 还在用它",
                    dict(kind='restart', runs=same))
        for identity in sorted(identities):
            problem = self.app.browsers.busy(identity, f'run:{run}')
            if problem:
                return ('browser-busy', problem, dict(kind='identity', identity=identity))
        ahead = self.queue if waiter is None else self.queue[:self.queue.index(waiter)]
        for other in ahead:
            if other.session == (waiter.session if waiter else None):
                continue
            if shared and other.checkout.id != checkout.id:
                return ('env-busy', f'前面有检出 {other.checkout.name} 的会话 {other.session} 在等环境',
                        dict(kind='queue', checkout=other.checkout.name, session=other.session))
            common = other.identities & set(identities)
            if common:
                return ('browser-busy', f"前面有会话 {other.session} 在等身份 {'、'.join(sorted(common))}",
                        dict(kind='queue', checkout=other.checkout.name, session=other.session, identities=sorted(common)))
        return None

    def busy_error(self, verdict, waiter):
        code, problem, holder = verdict
        queue = [w.public(i + 1) for i, w in enumerate(self.queue)]
        position = self.queue.index(waiter) + 1 if waiter in self.queue else None
        return APIError(409, code, [dict(field='environment' if code == 'env-busy' else 'identity', problem=problem)],
                        holder=holder, queue=queue, position=position)

    def acquire(self, checkout, run, identities, *, session, pid, wait, digest, url=None):
        """为运行 run 取得环境与身份；返回 dict(environment, browsers, switched_from)。"""
        from .daemon import alive
        deadline = time.monotonic() + max(0, wait)
        with self.cond:
            self.expire()
            waiter = next((w for w in self.queue if w.session == session), None)
            while True:
                verdict = self.check(checkout, run, identities, digest, waiter)
                if verdict is None:
                    if waiter in self.queue:
                        self.queue.remove(waiter)
                    self.granted[run] = checkout.id
                    break
                if wait > 0 and waiter is None:
                    waiter = Waiter(session, checkout, run, identities, pid)
                    self.queue.append(waiter)
                if waiter is not None:
                    waiter.active, waiter.ended = True, None
                    waiter.run, waiter.identities = run, set(identities)
                remaining = deadline - time.monotonic()
                if wait <= 0 and waiter is not None:
                    # 不带 --wait 再来：这个会话不再排队，放弃保留的位置。
                    self.queue.remove(waiter)
                    waiter = None
                if wait <= 0 or remaining <= 0 or (pid and not alive(pid)):
                    if waiter is not None:
                        waiter.active, waiter.ended = False, time.monotonic()
                    raise self.busy_error(verdict, waiter)
                self.cond.wait(timeout=min(1.0, remaining))
        try:
            switched = self.switch_to(checkout)
            environment = self.app.envs.up(checkout)
            browsers = self.app.browsers.lease(checkout, sorted(identities), f'run:{run}', url=url) if identities else {}
        except BaseException:
            with self.cond:
                self.granted.pop(run, None)
                self.cond.notify_all()
            raise
        return dict(environment=environment, browsers=browsers, switched_from=switched)

    def switch_to(self, checkout):
        """port_step 为 0 时：别的检出的环境没有运行在用，就停掉它，把环境换给 checkout。返回原来的检出名。"""
        if not self.shared_environment():
            return None
        switched = None
        for name in self.app.envs.running():
            if name != checkout.name:
                self.app.envs.down(self.app.checkout(name))
                switched = name
        return switched

    def wake(self):
        with self.cond:
            self.cond.notify_all()

    def recover(self):
        """守护进程启动时：清掉没写好运行目录、又已过了两分钟的 prepared 运行（准备它的命令已经不在了），释放它们的租约。"""
        cutoff = datetime.now(timezone.utc).timestamp() - 120
        with self.app.store.local_tx() as db:
            for row in db.execute("SELECT id, created_at FROM local_work WHERE kind='run' AND status='prepared' AND dir=''").fetchall():
                try:
                    created = datetime.fromisoformat(row['created_at']).timestamp()
                except ValueError:
                    created = 0
                if created < cutoff:
                    db.execute('UPDATE browser_instances SET holder=NULL WHERE holder=?', (f"run:{row['id']}",))
                    db.execute('DELETE FROM local_work WHERE id=?', (row['id'],))


def attach(app):
    from . import extensions
    app.gate = Gate(app)
    extensions.RECOVER.append(lambda app: app.gate.recover())


def _register():
    from .server import json_response, route

    @route('POST', '/local/run/acquire', 'token')
    def acquire(app, request):
        checkout = app.checkout(request.json_field('checkout'))
        run = request.json_field('run')
        if type(run) is not int:
            raise ValueError('须给出运行编号')
        identities = request.json_field('identities') or []
        result = app.gate.acquire(checkout, run, identities, session=request.json_field('session') or f'run:{run}',
                                  pid=request.json_field('pid'), wait=int(request.json_field('wait') or 0),
                                  digest=request.json_field('digest'), url=request.json_field('url'))
        return json_response(dict(ok=True, **result))


_register()
