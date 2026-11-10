"""按检出运行 adapter.env（守护进程侧）：端口偏移、跨项目端口租约、就绪等待与进程组清理。

适配契约（沿用）：adapter.env 在前台运行，就绪时输出一行 `sop-board: ready`，可在同一行后附 JSON 对象
给出本检出的入口地址等变量（如 `sop-board: ready {"APP_URL": "http://127.0.0.1:8100"}`）。输入变量：
SOP_CHECKOUT、SOP_REPO_<别名>（本检出的仓库）、SOP_BASE_<别名>（登记仓库）、SOP_PORT_OFFSET、SOP_PORTS、
SOP_ENV_LOG_DIR，以及 adapter.worktree 为该检出返回的变量。
"""
import errno
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import subprocess
import threading
import time

from . import daemon, gitops
from .redaction import StreamRedactor, names
from .store import dumps, now

READY = b'sop-board: ready'


class EnvBusy(ValueError):
    def __init__(self, message, owner=None):
        super().__init__(message)
        self.owner = owner or {}


def require_stopped(context, checkout):
    with context.store.connection() as db:
        row = db.execute('SELECT * FROM environments WHERE checkout=?', (checkout.id,)).fetchone()
    if row and row['state'] in ('starting', 'ready') and row['pid'] and daemon.alive(row['pid']):
        raise ValueError(f'检出 {checkout.name} 的环境仍在运行；先运行 sop-board env down --checkout {checkout.name}')


def stop_group(pid, grace=10, process=None):
    """结束以 pid 为组长的进程组：先 SIGTERM，超时再 SIGKILL。

    有子进程对象时直接等待它退出；探测正在退出的进程组可能被宿主环境拒绝（EPERM）。"""
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except PermissionError:
        if process is None or process.poll() is not None:
            return
        raise
    if process is not None:
        try:
            process.wait(timeout=grace)
            return
        except subprocess.TimeoutExpired:
            pass
    else:
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            try:
                os.killpg(pid, 0)
            except (ProcessLookupError, PermissionError):
                return
            time.sleep(.1)
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def port_owners(ports):
    """实际监听这些端口的进程（用 lsof 取得说明）。"""
    occupied = []
    for port in ports:
        busy = False
        candidates = [(socket.AF_INET, ('0.0.0.0', port)), (socket.AF_INET, ('127.0.0.1', port))]
        if socket.has_ipv6:
            candidates += [(socket.AF_INET6, ('::', port)), (socket.AF_INET6, ('::1', port))]
        for family, address in candidates:
            try:
                with socket.socket(family) as sock:
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    if family == socket.AF_INET6:
                        sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
                    sock.bind(address)
            except OSError as error:
                if family == socket.AF_INET6 and error.errno in (errno.EAFNOSUPPORT, errno.EADDRNOTAVAIL):
                    continue
                busy = True
                break
        if busy:
            try:
                executable = shutil.which('lsof') or '/usr/sbin/lsof'
                result = subprocess.run([executable, '-nP', f'-iTCP:{port}', '-sTCP:LISTEN'],
                                        capture_output=True, text=True, timeout=5)
                owner = result.stdout.strip() or '未取得进程信息'
            except (OSError, subprocess.SubprocessError):
                owner = '无法读取进程信息（需要 lsof）'
            occupied.append(dict(port=port, process=owner))
    return occupied


class PortLeases:
    """跨项目、跨检出的端口租约：/tmp 下的文件锁，记录占用者。"""

    def __init__(self, project):
        self.project = project
        self.held = {}

    def path(self, port):
        return Path('/tmp') / f'sop-board-port-{os.getuid()}-{port}.lock'

    def acquire(self, checkout, ports, holder):
        taken = {}
        try:
            for port in ports:
                if port in self.held:
                    continue
                stream = self.path(port).open('a+')
                try:
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    try:
                        stream.seek(0)
                        owner = json.load(stream)
                    except ValueError:
                        owner = {}
                    finally:
                        stream.close()
                    where = f"项目 {owner.get('project', '未知')} 的检出 {owner.get('checkout', '未知')}"
                    raise EnvBusy(f'端口 {port} 正由{where}的环境使用；在那个检出运行 sop-board env down 后再试', owner)
                stream.seek(0)
                stream.truncate()
                json.dump(dict(project=self.project.name, checkout=checkout.name, holder=holder,
                               data=str(self.project.data)), stream, ensure_ascii=False)
                stream.flush()
                taken[port] = stream
        except BaseException:
            for stream in taken.values():
                fcntl.flock(stream, fcntl.LOCK_UN)
                stream.close()
            raise
        self.held.update(taken)
        return list(taken)

    def release(self, ports):
        for port in ports:
            stream = self.held.pop(port, None)
            if stream:
                fcntl.flock(stream, fcntl.LOCK_UN)
                stream.close()


class Environment:
    """一个检出的 adapter.env 进程。"""

    def __init__(self, manager, checkout):
        self.manager, self.checkout = manager, checkout
        self.process, self.reader, self.log = None, None, None
        self.ports, self.values, self.digest = [], {}, None
        self.error = ''

    @property
    def project(self):
        return self.manager.project

    def actual_ports(self):
        offset = self.checkout.offset(self.project)
        return [port + offset for port in self.project.adapter.get('ports', [])]

    def inputs(self, log_dir):
        project, checkout = self.project, self.checkout
        env = {k: v for k, v in os.environ.items() if not k.startswith('SOP_')}
        env.update(checkout.env)
        for name in names(project.entries):
            env.pop(name, None)
            if os.environ.get(name):
                env[name] = os.environ[name]
        for alias, path in checkout.paths.items():
            key = alias.upper().replace('-', '_')
            env['SOP_REPO_' + key] = str(path)
            env['SOP_BASE_' + key] = str(project.repos[alias])
        env.update(SOP_CHECKOUT=checkout.name, SOP_PORT_OFFSET=str(checkout.offset(project)),
                   SOP_PORTS=json.dumps(self.actual_ports()), SOP_ENV_LOG_DIR=str(log_dir))
        return env

    def command(self):
        argv = list(self.project.adapter['env'])
        executable = self.project.data / argv[0]
        if executable.is_file():
            argv[0] = str(executable)
        return argv

    def alive(self):
        return self.process is not None and self.process.poll() is None

    def start(self, digest):
        project, checkout = self.project, self.checkout
        ports = self.actual_ports()
        leases = self.manager.leases.acquire(checkout, ports, f'env:{checkout.name}')
        try:
            occupied = port_owners(leases)
            if occupied:
                raise EnvBusy('端口已被其他进程监听：' + '；'.join(f"{o['port']}：{o['process']}" for o in occupied))
            directory = project.data / 'env' / checkout.name
            directory.mkdir(parents=True, exist_ok=True)
            sequence = max((int(p.stem) for p in directory.glob('*.log') if p.stem.isdigit()), default=0) + 1
            self.log = directory / f'{sequence}.log'
            self.ports, self.values, self.digest, self.error = ports, {}, digest, ''
            self.manager.record(checkout, 'starting', pid=None, ports=ports, log=self.log, digest=digest)
            process = subprocess.Popen(self.command(), cwd=project.data, env=self.inputs(directory),
                                       stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                       start_new_session=True)
            self.process = process
            self.manager.record(checkout, 'starting', pid=process.pid, ports=ports, log=self.log, digest=digest)
            ready = threading.Event()
            secrets = [os.environ[n] for n in names(project.entries) if os.environ.get(n)]

            def read():
                redactor = StreamRedactor(secrets)
                with self.log.open('wb') as stream:
                    for line in iter(process.stdout.readline, b''):
                        stream.write(redactor.feed(line))
                        stream.flush()
                        stripped = line.rstrip(b'\r\n')
                        if stripped.startswith(READY):
                            rest = stripped[len(READY):].strip()
                            if rest:
                                try:
                                    values = json.loads(rest)
                                    if isinstance(values, dict):
                                        self.values = {k: str(v) for k, v in values.items()
                                                       if re.fullmatch('[A-Za-z_][A-Za-z0-9_]*', str(k)) and not str(k).startswith('SOP_')}
                                except ValueError:
                                    pass
                            ready.set()
                    stream.write(redactor.feed(b'', final=True))
            self.reader = threading.Thread(target=read, name=f'env-{checkout.name}', daemon=True)
            self.reader.start()
            deadline = time.monotonic() + project.adapter.get('ready_timeout', 180)
            while not ready.wait(.1):
                if process.poll() is not None:
                    self.reader.join(timeout=2)
                    raise ValueError(f'adapter.env 就绪前退出（退出码 {process.returncode}）；日志：{self.log}')
                if time.monotonic() >= deadline or self.manager.stopping.is_set():
                    raise ValueError(f'adapter.env 等待就绪超时；日志：{self.log}')
            self.manager.record(checkout, 'ready', pid=process.pid, ports=ports, log=self.log, digest=digest, env=self.values)
        except BaseException as error:
            self.error = str(error)
            self.stop(state='failed', error=str(error))
            raise

    def stop(self, *, state='stopped', error=None):
        if self.process is not None:
            if self.process.poll() is None:
                stop_group(self.process.pid, process=self.process)
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            if self.reader:
                self.reader.join(timeout=5)
            self.process.stdout.close()
            self.process = None
        self.manager.leases.release(self.ports or self.actual_ports())
        self.manager.record(self.checkout, state, pid=None, ports=self.ports, log=self.log, digest=self.digest,
                            error=error, ended=True)

    def address_env(self):
        """入口地址变量：ready 行给出的值优先，其次检出的适配变量与守护进程环境。"""
        result = {}
        for entry in self.project.entries.values():
            name = entry.get('address_env') if isinstance(entry, dict) else None
            if not name:
                continue
            value = self.values.get(name) or self.checkout.env.get(name) or os.environ.get(name)
            if value:
                result[name] = value
        return result


class Manager:
    """守护进程里全部检出的环境。同一检出的启停串行执行。"""

    def __init__(self, project, store):
        self.project, self.store = project, store
        self.leases = PortLeases(project)
        self.environments, self.locks = {}, {}
        self.guard = threading.Lock()
        self.stopping = threading.Event()

    def record(self, checkout, state, *, pid, ports, log, digest, env=None, error=None, ended=False):
        with self.store.transaction() as db:
            db.execute('''INSERT INTO environments(checkout,state,pid,ports,env,product_digest,log,error,started_at,ended_at)
                          VALUES (?,?,?,?,?,?,?,?,?,?)
                          ON CONFLICT(checkout) DO UPDATE SET state=excluded.state, pid=excluded.pid, ports=excluded.ports,
                            env=CASE WHEN excluded.env='{}' AND excluded.state='ready' THEN environments.env ELSE excluded.env END,
                            product_digest=excluded.product_digest, log=excluded.log, error=excluded.error,
                            started_at=CASE WHEN excluded.state='starting' THEN excluded.started_at ELSE environments.started_at END,
                            ended_at=excluded.ended_at''',
                       (checkout.id, state, pid, dumps(ports or []), dumps(env or {}), digest,
                        str(log) if log else None, error, now(), now() if ended else None))

    def lock(self, checkout):
        with self.guard:
            return self.locks.setdefault(checkout.id, threading.RLock())

    def environment(self, checkout):
        with self.guard:
            current = self.environments.get(checkout.id)
            if current is None or current.checkout.paths != checkout.paths or current.checkout.slot != checkout.slot:
                current = self.environments[checkout.id] = Environment(self, checkout)
            return current

    def snapshot_digest(self, checkout):
        from .guards import test_exclusions_for
        return gitops.snapshot(checkout.paths, self.project.exclusions(test_exclusions_for(self.project, checkout)))['product_digest']

    def busy_owner(self, checkout):
        """port_step 为 0 时其他检出的环境占用同一组端口。"""
        if self.project.adapter.get('port_step', 0):
            return None
        with self.guard:
            for other in self.environments.values():
                if other.checkout.id != checkout.id and other.alive():
                    return other.checkout
        return None

    def up(self, checkout, *, restart_on_change=True):
        """确保检出的环境就绪；产品快照与启动时不同则重启。返回状态。"""
        self.project.reload()
        self.project.require()
        if not self.project.adapter.get('env'):
            raise ValueError('项目没有声明 adapter.env')
        with self.lock(checkout):
            owner = self.busy_owner(checkout)
            if owner is not None:
                with self.store.connection() as db:
                    run = db.execute("SELECT id FROM work WHERE checkout=? AND kind='run' AND status='prepared'", (owner.id,)).fetchone()
                using = f'（运行 #{run["id"]} 正在使用）' if run else ''
                raise EnvBusy(f'各检出共用端口（port_step 为 0），检出 {owner.name} 的环境正在运行{using}；'
                              f'先运行 sop-board env down --checkout {owner.name}',
                              dict(checkout=owner.name, run=run['id'] if run else None))
            environment = self.environment(checkout)
            digest = self.snapshot_digest(checkout)
            if environment.alive() and (environment.digest == digest or not restart_on_change):
                return self.status(checkout)
            if environment.alive():
                environment.stop()
            environment.start(digest)
            return self.status(checkout)

    def down(self, checkout):
        with self.lock(checkout):
            environment = self.environment(checkout)
            environment.stop()
            return self.status(checkout)

    def status(self, checkout):
        environment = self.environments.get(checkout.id)
        with self.store.connection() as db:
            row = db.execute('SELECT * FROM environments WHERE checkout=?', (checkout.id,)).fetchone()
        result = dict(checkout=checkout.name, state='stopped', ports=[], log=None, error=None)
        if row:
            result.update(state=row['state'], ports=json.loads(row['ports']), log=row['log'], error=row['error'],
                          product_digest=row['product_digest'], started_at=row['started_at'])
        if environment and not environment.alive() and result['state'] in ('starting', 'ready'):
            result['state'] = 'stopped'
        result['address_env'] = environment.address_env() if environment and environment.alive() else {}
        return result

    def recover(self):
        """守护进程启动时清理上次遗留的环境进程组（只处理记录在案、组长仍是适配命令的进程）。"""
        with self.store.connection() as db:
            rows = list(db.execute("SELECT * FROM environments WHERE state IN ('starting','ready') AND pid IS NOT NULL"))
        script = Path(self.project.adapter.get('env', [''])[-1]).name if self.project.adapter.get('env') else ''
        for row in rows:
            pid = row['pid']
            try:
                found = subprocess.run(['ps', '-o', 'pgid=,command=', '-p', str(pid)], capture_output=True, text=True, timeout=5).stdout.strip()
            except (OSError, subprocess.SubprocessError):
                found = ''
            if found and found.split()[0] == str(pid) and (not script or script in found):
                stop_group(pid)
            with self.store.transaction() as db:
                db.execute("UPDATE environments SET state='stopped', pid=NULL, ended_at=? WHERE checkout=?", (now(), row['checkout']))

    def close(self):
        self.stopping.set()
        for environment in list(self.environments.values()):
            if environment.alive():
                environment.stop()
