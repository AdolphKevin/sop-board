"""按检出运行 adapter.env（守护进程侧）：端口偏移、跨项目端口租约、就绪等待、进程组清理与各服务的状态和停启。

适配契约：adapter.env 在前台运行，就绪时输出一行 `sop-board: ready`，可在同一行后附 JSON 对象
给出本检出的入口地址等变量（如 `sop-board: ready {"APP_URL": "http://127.0.0.1:8100"}`）。输入变量：
SOP_CHECKOUT、SOP_REPO_<别名>（本检出的仓库）、SOP_BASE_<别名>（登记仓库）、SOP_PORT_OFFSET、SOP_PORTS、
SOP_ENV_LOG_DIR，以及 adapter.worktree 为该检出返回的变量。adapter 输出就绪即环境可用，不要求所有服务都在运行。

声明了 adapter.services 时：adapter 把每个服务的状态写成 SOP_ENV_LOG_DIR/services/<名称>.state
（JSON：{"state": "running|stopped|absent|crashed", "log": 异常退出时的日志路径}），状态变化时改写；
守护进程在同一目录写 <名称>.request 请求停止、删除它请求启动，adapter 轮询并执行，完成后改写状态文件。
声明了但没有状态文件的服务为“未上报”。环境启动前与停止后，守护进程清空这个目录。
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
from .errors import APIError
from .redaction import StreamRedactor, names, redact
from .store import dumps, now

READY = b'sop-board: ready'
# 状态文件的取值；unreported（声明了但没有状态文件或内容不合格）与 down（环境没有运行）由守护进程补充。
SERVICE_STATES = {'running': '运行中', 'stopped': '已停止', 'absent': '本机未搭建', 'crashed': '异常退出'}
SERVICE_LABELS = dict(SERVICE_STATES, unreported='未上报', down='环境未运行')
LOG_TAIL_LINES = 20


def services_dir(project, checkout):
    return project.data / 'env' / checkout.name / 'services'


def clear_services(directory):
    """清空状态与请求文件：旧环境留下的状态不代表新环境，遗留的停止请求也不能作用到新环境上。"""
    directory.mkdir(parents=True, exist_ok=True)
    for path in list(directory.glob('*.state')) + list(directory.glob('*.request')):
        path.unlink(missing_ok=True)


def read_state(directory, name):
    """读一个服务的状态文件：{state, log?, problem?}。"""
    path = directory / f'{name}.state'
    try:
        raw = path.read_text(encoding='utf-8')
    except FileNotFoundError:
        return dict(state='unreported')
    except (OSError, UnicodeDecodeError) as error:
        return dict(state='unreported', problem=f'状态文件无法读取：{error}')
    try:
        value = json.loads(raw)
    except ValueError:
        value = None
    if not isinstance(value, dict) or value.get('state') not in SERVICE_STATES:
        return dict(state='unreported', problem='状态文件不合格：须为 {"state": "running|stopped|absent|crashed", "log": …}')
    result = dict(state=value['state'])
    log = value.get('log')
    if isinstance(log, str) and log.strip():
        result['log'] = str((directory.parent / log).resolve()) if not Path(log).is_absolute() else log
    return result


def log_tail(path, secrets=(), lines=LOG_TAIL_LINES):
    try:
        with open(path, 'rb') as stream:
            stream.seek(0, os.SEEK_END)
            size = stream.tell()
            stream.seek(max(0, size - 16384))
            raw = stream.read()
    except OSError:
        return None
    text = redact(raw, secrets).decode('utf-8', 'replace')
    return '\n'.join(text.splitlines()[-lines:])


def not_running(services):
    """没在运行的服务：本机未搭建、已停止、异常退出、未上报或环境未运行。"""
    return [dict(name=s['name'], about=s['about'], state=s['state'], label=s['label'],
                 **({'log': s['log']} if s.get('log') else {})) for s in services if s['state'] != 'running']


def missing_addresses(project, address_env):
    """ready 输出里缺少的入口地址变量：入口名 → 变量名。"""
    return {name: entry['address_env'] for name, entry in project.entries.items()
            if isinstance(entry, dict) and entry.get('address_env') and entry['address_env'] not in address_env}


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
            clear_services(directory / 'services')
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
            clear_services(services_dir(self.project, self.checkout))
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
        alive = bool(environment and environment.alive())
        if not alive and result['state'] in ('starting', 'ready'):
            result['state'] = 'stopped'
        result['address_env'] = environment.address_env() if alive else {}
        # ready 行给出的全部变量（含入口以外的地址，例如被依赖服务的地址），只在本机令牌接口返回。
        result['variables'] = dict(environment.values) if alive else {}
        result['services'] = self.services(checkout, alive=alive and result['state'] == 'ready')
        result['not_running'] = not_running(result['services'])
        result['missing_address_env'] = missing_addresses(self.project, result['address_env']) if alive else {}
        return result

    def services(self, checkout, *, alive):
        """声明的服务及其状态（按声明顺序）；环境没有运行时都是 down。异常退出附日志末尾（按凭据取值脱敏）。"""
        declared = self.project.adapter.get('services') or {}
        directory = services_dir(self.project, checkout)
        secrets = [os.environ[n] for n in names(self.project.entries) if os.environ.get(n)]
        result = []
        for name, spec in declared.items():
            info = read_state(directory, name) if alive else dict(state='down')
            item = dict(name=name, about=spec['about'], stoppable=spec['stoppable'], state=info['state'],
                        label=SERVICE_LABELS[info['state']], stop_requested=(directory / f'{name}.request').exists())
            if info.get('problem'):
                item['problem'] = info['problem']
            if info.get('log'):
                item['log'] = info['log']
                if info['state'] == 'crashed':
                    item['log_tail'] = log_tail(info['log'], secrets)
            result.append(item)
        return result

    # ---- 服务停启（故障模拟）：只在本检出有 prepared 运行时可用，停启记进该运行 ----

    def prepared_run(self, checkout):
        with self.store.connection() as db:
            return db.execute("SELECT id FROM work WHERE checkout=? AND kind='run' AND status='prepared'",
                              (checkout.id,)).fetchone()

    def record_service(self, checkout, work_id, name, action, operator, *, ok=True, note=None):
        with self.store.transaction() as db:
            cursor = db.execute('INSERT INTO service_events(work,checkout,service,action,ok,operator,at,note) VALUES (?,?,?,?,?,?,?,?)',
                                (work_id, checkout.id, name, action, 1 if ok else 0, operator, now(), note))
            return cursor.lastrowid

    def wait_state(self, environment, directory, name, target, timeout):
        deadline = time.monotonic() + timeout
        while True:
            state = read_state(directory, name)['state']
            if state == target:
                return state
            if not environment.alive() or time.monotonic() >= deadline or self.stopping.is_set():
                return state
            time.sleep(.2)

    def service(self, checkout, name, action, *, operator):
        """env stop|start <服务>：写或删请求文件，等状态变为目标值才返回；超时撤回停止请求并返回 service-timeout。"""
        self.project.reload()
        declared = self.project.adapter.get('services') or {}
        if not declared:
            raise APIError(409, 'service-not-declared', [dict(field='service', problem=(
                '项目没有在 config.yaml 的 adapter.services 声明服务，不能停启单个服务；运行之外要恢复环境，用 sop-board env restart'))])
        if name not in declared:
            raise APIError(409, 'service-unknown', [dict(field='service', problem=(
                f'服务 {name} 不在 adapter.services 中；已声明的服务：' + '、'.join(declared)))])
        if not declared[name]['stoppable']:
            raise APIError(409, 'service-not-stoppable', [dict(field='service', problem=(
                f"服务 {name}（{declared[name]['about']}）没有声明为可停（stoppable: true），不能用 env stop|start 停启"))])
        run = self.prepared_run(checkout)
        if run is None:
            raise APIError(409, 'run-required', [dict(field='run', problem=(
                f'检出 {checkout.name} 没有 prepared 的运行：env stop|start 只用于运行中模拟依赖故障，停启记进该运行；'
                '运行之外要恢复环境，用 sop-board env restart'))])
        about = declared[name]['about']
        timeout = self.project.adapter.get('service_timeout', 60)
        with self.lock(checkout):
            environment = self.environment(checkout)
            if not environment.alive():
                raise APIError(409, 'service-unavailable', [dict(field='environment', problem=(
                    f'检出 {checkout.name} 的环境没有运行：先运行 sop-board env up'))])
            directory = services_dir(self.project, checkout)
            request = directory / f'{name}.request'
            current = read_state(directory, name)['state']
            target = 'stopped' if action == 'stop' else 'running'
            base = dict(ok=True, run=run['id'], service=name, about=about, action=action)
            if action == 'stop':
                if current == 'stopped' and request.exists():
                    return dict(base, changed=False, state='stopped', label=SERVICE_LABELS['stopped'], note='已经停止，没有新的停启记录')
                if current != 'running':
                    raise APIError(409, 'service-unavailable', [dict(field='service', problem=(
                        f'服务 {name}（{about}）当前为{SERVICE_LABELS[current]}，不是运行中，不能停止'))])
                directory.mkdir(parents=True, exist_ok=True)
                request.write_text(dumps(dict(action='stop', run=run['id'], by=operator, at=now())) + '\n', encoding='utf-8')
            else:
                if not request.exists():
                    if current == 'running':
                        return dict(base, changed=False, state='running', label=SERVICE_LABELS['running'], note='已经在运行，没有新的停启记录')
                    raise APIError(409, 'service-unavailable', [dict(field='service', problem=(
                        f'服务 {name}（{about}）当前为{SERVICE_LABELS[current]}，不是由 env stop 停下的；'
                        '恢复环境用 sop-board env restart'))])
                request.unlink()
            started = time.monotonic()
            state = self.wait_state(environment, directory, name, target, timeout)
            waited = round(time.monotonic() - started, 1)
            if state != target:
                if action == 'stop':
                    request.unlink(missing_ok=True)
                reason = '环境已经退出' if not environment.alive() else f'{timeout} 秒内'
                raise APIError(409, 'service-timeout', [dict(field='service', problem=(
                    f"服务 {name}（{about}）{reason}没有变为{SERVICE_LABELS[target]}（当前{SERVICE_LABELS[state]}），等待了 {waited} 秒"
                    + ('；已撤回停止请求' if action == 'stop' else '') + '。adapter 须轮询 services 目录的请求文件并改写状态文件'))])
            event = self.record_service(checkout, run['id'], name, action, operator)
        return dict(base, changed=True, state=state, label=SERVICE_LABELS[state], waited=waited, event=event)

    def restore(self, checkout, work_id, *, operator, reason):
        """运行提交或放弃时，恢复本轮停着的服务并逐个记录。返回 [{service, ok, state, note}]。"""
        with self.store.connection() as db:
            events = [dict(r) for r in db.execute('SELECT service, action FROM service_events WHERE work=? ORDER BY id', (work_id,))]
        last = {}
        for event in events:
            last[event['service']] = event['action']
        stopped = [name for name, action in last.items() if action == 'stop']
        result = []
        if not stopped:
            return result
        timeout = self.project.adapter.get('service_timeout', 60)
        with self.lock(checkout):
            environment = self.environment(checkout)
            directory = services_dir(self.project, checkout)
            for name in stopped:
                request = directory / f'{name}.request'
                if not environment.alive():
                    request.unlink(missing_ok=True)
                    ok, state, note = True, 'down', f'{reason}时自动恢复：环境没有运行，已撤回停止请求，下次启动环境时照常启动'
                elif request.exists():
                    request.unlink()
                    state = self.wait_state(environment, directory, name, 'running', timeout)
                    ok = state == 'running'
                    note = f'{reason}时自动恢复' + ('' if ok else f'：{timeout} 秒内没有变为运行中（当前{SERVICE_LABELS[state]}）')
                else:
                    state = read_state(directory, name)['state']
                    ok = state == 'running'
                    note = f'{reason}时自动恢复：停止请求已不在（环境重启过），当前{SERVICE_LABELS[state]}'
                self.record_service(checkout, work_id, name, 'restore', operator, ok=ok, note=note)
                result.append(dict(service=name, ok=ok, state=state, label=SERVICE_LABELS[state], note=note))
        return result

    def recover_services(self):
        for checkout_dir in (self.project.data / 'env').glob('*/services'):
            clear_services(checkout_dir)

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
        self.recover_services()

    def close(self):
        self.stopping.set()
        for environment in list(self.environments.values()):
            if environment.alive():
                environment.stop()


def withdraw(project, store, checkout, work_id, *, operator, reason):
    """守护进程没有运行时（它退出时已停掉全部环境）：撤回本轮留下的停止请求并逐个记录，下次启动环境时服务照常启动。"""
    with store.connection() as db:
        events = [dict(r) for r in db.execute('SELECT service, action FROM service_events WHERE work=? ORDER BY id', (work_id,))]
    last = {}
    for event in events:
        last[event['service']] = event['action']
    result = []
    directory = services_dir(project, checkout)
    for name in [n for n, action in last.items() if action == 'stop']:
        (directory / f'{name}.request').unlink(missing_ok=True)
        note = f'{reason}时自动恢复：看板守护进程没有运行（环境也已停止），已撤回停止请求，下次启动环境时照常启动'
        with store.transaction() as db:
            db.execute('INSERT INTO service_events(work,checkout,service,action,ok,operator,at,note) VALUES (?,?,?,?,?,?,?,?)',
                       (work_id, checkout.id, name, 'restore', 1, operator, now(), note))
        result.append(dict(service=name, ok=True, state='down', label=SERVICE_LABELS['down'], note=note))
    return result


def stopped_during(db, work_ids):
    """结果执行时停着的服务：运行编号 → {场景键: [服务名]}，取自该运行停止事件记下的场景。"""
    result = {}
    ids = sorted({w for w in work_ids if w})
    if not ids:
        return result
    marks = ','.join('?' * len(ids))
    for row in db.execute(f"SELECT work, service, scenarios FROM service_events WHERE action='stop' AND work IN ({marks}) ORDER BY id", ids):
        for key in json.loads(row['scenarios'] or '[]'):
            names_ = result.setdefault(row['work'], {}).setdefault(key, [])
            if row['service'] not in names_:
                names_.append(row['service'])
    return result


def stopped_labels(project, services):
    """['stock'] → [{name, about, text: '执行时 stock（库存查询与扣减）已停止'}]；about 取当前声明。"""
    declared = project.adapter.get('services') or {}
    result = []
    for name in services:
        about = (declared.get(name) or {}).get('about')
        text = f'执行时 {name}（{about}）已停止' if about else f'执行时 {name} 已停止'
        result.append(dict(name=name, about=about, text=text))
    return result
