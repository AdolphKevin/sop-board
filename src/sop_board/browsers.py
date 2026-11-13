"""浏览器代理：sop-board 自带的 Playwright 运行时与 Google Chrome for Testing。

- 运行时是本机级的：<SOP_BOARD_HOME>/runtime，固定版本（随包 lock 文件），浏览器装在同目录 browsers/，
  不读写共享的 ms-playwright 缓存，也不碰用户日常 Chrome。
- 只有看板守护进程启动浏览器：每个 检出×身份 一个 CfT，独立持久 profile 与调试端口，缺省无头；
  执行者通过 CDP 连接。缺省端口 = 9400 + 槽位×20 + 身份序号，被别的进程占用时顺延到下一个空闲端口（已定决策 24），
  实际端口记在浏览器实例里，经 run.env 交给执行者；浏览器运行期间端口不变，下一次启动重新从缺省端口找起。真实账号由人在 `browser open --headed` 打开的窗口里登录、完成验证，之后
  `browser release` 交还：只解除真人租约，窗口保持运行，之后的运行直接沿用它，人做过的验证不作废。
- 浏览器异常退出时记录信号、日志与崩溃报告；租约持有期间不自动重启；30 分钟内两次异常退出进入 crashed。
"""
from datetime import datetime, timedelta, timezone
from importlib.resources import files
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
from urllib.request import ProxyHandler, build_opener

from . import config
from .entries import identities as entry_identities
from .errors import APIError
from .store import dumps, now

BASE_PORT = 9400
SLOT_WIDTH = 20
LAST_PORT = 65535
LAUNCH_ATTEMPTS = 5
READY_SECONDS = 15
CRASH_WINDOW = timedelta(minutes=30)
REPORTS = Path.home() / 'Library' / 'Logs' / 'DiagnosticReports'


def runtime_dir():
    return config.HOME / 'runtime'


def runtime_env():
    env = {k: v for k, v in os.environ.items() if not k.startswith('PLAYWRIGHT_')}
    env['PLAYWRIGHT_BROWSERS_PATH'] = str(runtime_dir() / 'browsers')
    return env


def runtime_info():
    """已安装运行时的记录；缺失或不完整时返回 None。"""
    try:
        info = json.loads((runtime_dir() / 'runtime.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    if not isinstance(info, dict) or not Path(info.get('executable', '')).is_file():
        return None
    return info


def require_runtime():
    info = runtime_info()
    if info is None:
        raise APIError(409, 'browser-runtime-missing', [dict(field='browser', problem=
            f'浏览器运行时没有安装：运行 sop-board browser install（安装到 {runtime_dir()}）')])
    return info


def install(log=print):
    """安装固定版本的 Playwright 与 Chrome for Testing（不装 headless shell、WebKit、Firefox）。"""
    directory = runtime_dir()
    directory.mkdir(parents=True, exist_ok=True)
    for name in ('package.json', 'package-lock.json'):
        (directory / name).write_bytes(files('sop_board').joinpath('assets', 'runtime', name).read_bytes())
    npm = shutil.which('npm')
    node = shutil.which('node')
    if not npm or not node:
        raise ValueError('需要 Node.js 与 npm（Node >= 20）')
    env = runtime_env()
    subprocess.run([npm, 'ci', '--no-audit', '--no-fund'], cwd=directory, env=env, check=True)
    cli = directory / 'node_modules' / '@playwright' / 'test' / 'cli.js'
    subprocess.run([node, str(cli), 'install', '--no-shell', 'chromium', 'ffmpeg'], cwd=directory, env=env, check=True)
    script = ("const pw=require('playwright-core');const fs=require('fs');const path=require('path');"
              "const root=path.dirname(require.resolve('playwright-core'));"
              "const c=JSON.parse(fs.readFileSync(path.join(root,'browsers.json'),'utf8')).browsers.find(x=>x.name==='chromium');"
              "const v=JSON.parse(fs.readFileSync(path.join(root,'package.json'),'utf8')).version;"
              "console.log(JSON.stringify({playwright:v,revision:c.revision,browser_version:c.browserVersion,"
              "title:c.title,executable:pw.chromium.executablePath()}))")
    output = subprocess.run([node, '-e', script], cwd=directory, env=env, check=True, capture_output=True, text=True).stdout
    info = json.loads(output.strip().splitlines()[-1])
    if not Path(info['executable']).is_file():
        raise ValueError('Chrome for Testing 安装后找不到可执行文件：' + info['executable'])
    info.update(browsers=str(directory / 'browsers'), node_modules=str(directory / 'node_modules'), installed_at=now())
    (directory / 'runtime.json').write_text(json.dumps(info, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    log(f"已安装 Playwright {info['playwright']} 与 {info['title']} {info['browser_version']}（修订 {info['revision']}）")
    return info


def identity_order(project):
    """全部 Web 入口声明的身份编号（default 为 0），决定端口序号。"""
    order = ['default']
    for entry in project.entries.values():
        if isinstance(entry, dict) and entry.get('surface') == 'web':
            for identity in entry_identities(entry):
                if identity not in order:
                    order.append(identity)
    return order


def port_for(project, checkout, identity):
    """缺省调试端口：9400 + 槽位×20 + 身份序号。实际端口见 Instance.port（被占用时顺延）。"""
    order = identity_order(project)
    if identity not in order:
        raise ValueError(f'没有声明身份 {identity}')
    index = order.index(identity)
    if index >= SLOT_WIDTH:
        raise ValueError(f'身份超过 {SLOT_WIDTH} 个，无法分配调试端口')
    return BASE_PORT + checkout.slot * SLOT_WIDTH + index


def port_in_use(port):
    """127.0.0.1 上的端口已被占用：有进程在监听，或本机地址绑定不上。只探测，不连接后发送任何内容。"""
    with socket.socket() as sock:
        sock.settimeout(.5)
        if sock.connect_ex(('127.0.0.1', port)) == 0:
            return True
    with socket.socket() as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(('127.0.0.1', port))
        except OSError:
            return True
    return False


def free_port(start):
    """从 start 起第一个空闲端口；一直到 65535 都被占用时返回 None。"""
    return next((port for port in range(start, LAST_PORT + 1) if not port_in_use(port)), None)


LISTENING = re.compile(rb'DevTools listening on ws://([^\s/]+)/devtools/browser/([0-9A-Za-z-]+)')


def listener_pids(port, *, timeout=5):
    """在 127.0.0.1:<端口> 上监听的进程号（lsof）；取不到时为空集合。"""
    try:
        result = subprocess.run([shutil.which('lsof') or '/usr/sbin/lsof', '-nP', '-t', f'-iTCP@127.0.0.1:{port}', '-sTCP:LISTEN'],
                                capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return set()
    return {int(item) for item in result.stdout.split() if item.isdigit()}


def listening(log, offset, *, latest=False):
    """本次启动写进 chrome.log 的 DevTools 监听地址：(主机:端口, 浏览器标识)；还没写出时返回 None。"""
    try:
        with open(log, 'rb') as stream:
            stream.seek(offset)
            matches = list(LISTENING.finditer(stream.read()))
            found = (matches[-1] if latest else matches[0]) if matches else None
    except OSError:
        return None
    return (found.group(1).decode(), found.group(2).decode()) if found else None


def debugger_ready(port, *, timeout=1):
    try:
        opener = build_opener(ProxyHandler({}))
        with opener.open(f'http://127.0.0.1:{port}/json/version', timeout=timeout) as response:
            return json.loads(response.read()).get('webSocketDebuggerUrl')
    except (OSError, ValueError):
        return None


def process_snapshot(pid):
    """只查看指定进程；启动时间、命令行一起用于防止进程号被复用。"""
    try:
        result = subprocess.run(['ps', '-ww', '-p', str(pid), '-o', 'lstart=,stat=,command='],
                                capture_output=True, text=True, timeout=5, env=dict(os.environ, LC_ALL='C'))
        parts = result.stdout.strip().split(None, 6)
        if result.returncode == 0 and len(parts) == 7 and not parts[5].startswith('Z'):
            return ' '.join(parts[:5]), parts[6]
    except (OSError, subprocess.SubprocessError):
        pass
    return None


class RecoveredProcess:
    """非子进程没有 waitpid 退出码；以启动时间和命令行监视同一个进程。"""

    def __init__(self, pid, snapshot):
        self.pid, self.snapshot = pid, snapshot
        self.started = datetime.strptime(snapshot[0], '%a %b %d %H:%M:%S %Y').astimezone().timestamp()
        self.verified_at = time.monotonic()
        self.exited = False
        self.guard = threading.Lock()

    def poll(self):
        with self.guard:
            if self.exited:
                return 0
            try:
                os.kill(self.pid, 0)
            except ProcessLookupError:
                self.exited = True
            except PermissionError:
                pass
            if not self.exited and time.monotonic() - self.verified_at >= 5:
                self.verified_at = time.monotonic()
                self.exited = process_snapshot(self.pid) != self.snapshot
            return 0 if self.exited else None

    def wait(self, timeout=None):
        deadline = None if timeout is None else time.monotonic() + timeout
        while self.poll() is None:
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                raise subprocess.TimeoutExpired(str(self.pid), timeout)
            time.sleep(1 if remaining is None else min(1, remaining))
        return None

    def send_signal(self, sig):
        with self.guard:
            # 发信号前总是重新核实身份，不使用五秒缓存。
            self.verified_at = time.monotonic()
            self.exited = self.exited or process_snapshot(self.pid) != self.snapshot
            if not self.exited:
                os.kill(self.pid, sig)


def profile_process(profile, info):
    """锁、运行时可执行文件、完整 profile 参数都符合才返回进程与启动参数。"""
    try:
        host, number = os.readlink(profile / 'SingletonLock').rsplit('-', 1)
        if host != socket.gethostname() or not number.isdigit():
            return None
        pid = int(number)
        snapshot = process_snapshot(pid)
        if not snapshot:
            return None
        executable = str(Path(info['executable']))
        command = snapshot[1]
        if not command.startswith(executable + ' '):
            return None
        # ps 不为含空格的路径加引号；CfT 的参数用下一个 --选项分界。
        options = dict(re.findall(r'--([a-z-]+)(?:=(.*?))?(?= --[a-z-]+|$)', command[len(executable) + 1:]))
        if options.get('user-data-dir') != str(profile):
            return None
        port = options.get('remote-debugging-port', '')
        if not port.isdigit() or not 1 <= int(port) <= LAST_PORT:
            return None
        return RecoveredProcess(pid, snapshot), int(port), 'headless' not in options
    except (OSError, ValueError):
        return None


def crash_report(after):
    """启动之后写出的 Chrome for Testing 崩溃报告（macOS DiagnosticReports）。"""
    if not REPORTS.is_dir():
        return None
    found = [p for p in REPORTS.glob('Google Chrome for Testing*.ips') if p.stat().st_mtime >= after - 1]
    return str(max(found, key=lambda p: p.stat().st_mtime)) if found else None


def signal_name(code):
    if code is None or code >= 0:
        return None
    try:
        return signal.Signals(-code).name
    except ValueError:
        return f'SIG{-code}'


def exit_description(at, code, name, log):
    moment = datetime.fromisoformat(at).astimezone().strftime('%Y-%m-%d %H:%M:%S')
    if code is None and (not name or name == '进程已退出'):
        reason = '已退出（取不到退出信号）'
    else:
        reason = f"因 {name or f'退出码 {code}'} 退出"
    return f'浏览器进程 {moment} {reason}，日志 {log}'


class Instance:
    def __init__(self, manager, checkout, identity):
        self.manager, self.checkout, self.identity = manager, checkout, identity
        self.process, self.watcher = None, None
        self.started, self.stopping = 0.0, False
        self.headed = False
        self.actual = None   # 本次启动实际使用的调试端口
        self.deferred_record = None
        self.detached = threading.Event()
        self.recovered = threading.Event()
        self.recovered.set()
        self.recovery_thread = None
        self.recovery_error = None
        self.keep_holder = None   # restart 交接下来的租约持有者，认回后照旧有效（已定决策 28）

    @property
    def project(self):
        return self.manager.project

    def profile(self):
        return self.project.data / 'browsers' / self.checkout.name / self.identity / 'profile'

    def log(self):
        return self.profile().parent / 'chrome.log'

    def default_port(self):
        return port_for(self.project, self.checkout, self.identity)

    def port(self):
        """实际端口：启动过就是本次（或最近一次）启动所用的端口，否则是缺省端口。"""
        return self.actual or self.default_port()

    def alive(self):
        return self.process is not None and self.process.poll() is None

    def endpoint(self):
        return f'http://127.0.0.1:{self.port()}'

    def launch(self, *, headed, url=None):
        """启动浏览器：从缺省端口找起，被别的进程占用就顺延到下一个空闲端口（不结束、不连接占用者）。

        CfT 在 127.0.0.1 上绑定失败时会改在 [::1] 上监听而不退出，这时 127.0.0.1 上应答的是别的进程；
        所以就绪须确认应答的是本次启动的浏览器：chrome.log 里本次的监听行是 127.0.0.1:<端口> 且浏览器标识与 /json/version 一致，
        或（没有监听行时）lsof 显示在该端口监听的正是本进程。探测之后端口才被别人占去时，结束这次启动，从下一个端口再找。"""
        info = require_runtime()
        default = self.default_port()
        start = default
        for _ in range(LAUNCH_ATTEMPTS):
            port = free_port(start)
            if port is None:
                raise ValueError(f'调试端口从缺省的 {default} 起到 {LAST_PORT} 都被占用，无法启动浏览器')
            outcome = self.start_on(info, port, headed=headed, url=url)
            if outcome != 'taken':
                return outcome
            start = port + 1
        raise ValueError(f'调试端口一再被别的进程抢先占用（从缺省的 {default} 起试了 {LAUNCH_ATTEMPTS} 次），无法启动浏览器')

    def start_on(self, info, port, *, headed, url):
        """在 port 上启动一次。返回 endpoint；启动即退出返回 None；端口被抢先占用返回 'taken'。"""
        profile = self.profile()
        profile.mkdir(parents=True, exist_ok=True)
        argv = [info['executable'], f'--user-data-dir={profile}', f'--remote-debugging-port={port}',
                '--no-first-run', '--no-default-browser-check', '--use-mock-keychain']
        if not headed:
            argv.append('--headless=new')
        argv.append(url or 'about:blank')
        log = self.log().open('ab')
        offset = log.tell()
        self.detached.clear()
        self.stopping, self.headed, self.started, self.actual = False, headed, time.time(), port
        self.process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
        log.close()
        self.manager.record(self, 'starting')
        self.watcher = threading.Thread(target=self.watch, args=(self.process,), daemon=True,
                                        name=f'browser-{self.checkout.name}-{self.identity}')
        self.watcher.start()
        deadline = time.monotonic() + READY_SECONDS
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                break
            heard = listening(self.log(), offset)
            if heard and heard[0] != f'127.0.0.1:{port}':
                # 本机地址上的端口在探测之后被别人占去，CfT 改在别的地址上监听：结束这次启动，换下一个端口。
                self.stop()
                return 'taken'
            ws = debugger_ready(port)
            if ws and (ws.rstrip('/').endswith('/devtools/browser/' + heard[1]) if heard
                       else self.process.pid in listener_pids(port)):
                self.manager.record(self, 'ready')
                return self.endpoint()
            time.sleep(.2)
        if self.process.poll() is None:
            self.stop()
            raise ValueError(f'Chrome for Testing {READY_SECONDS} 秒内没有在调试端口 {port} 上就绪；日志：{self.log()}')
        self.watcher.join(timeout=5)
        return None

    def wait_recovery(self):
        self.recovered.wait()

    def watch(self, process):
        interval = 1 if isinstance(process, RecoveredProcess) else .2
        while process.poll() is None:
            if self.detached.wait(interval):
                return
        if self.detached.is_set():
            return
        code = process.wait()
        if process is not self.process:
            return
        self.manager.exited(self, code, expected=self.stopping)

    def stop(self):
        if self.process is None:
            return
        self.stopping = True
        process = self.process
        if process.poll() is None:
            try:
                if isinstance(process, RecoveredProcess):
                    process.send_signal(signal.SIGTERM)
                else:
                    os.killpg(process.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                try:
                    if isinstance(process, RecoveredProcess):
                        process.send_signal(signal.SIGKILL)
                    else:
                        os.killpg(process.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
                process.wait(timeout=5)
        if self.watcher:
            self.watcher.join(timeout=5)


class Manager:
    """守护进程里的全部浏览器；同一 检出×身份 的操作串行执行。"""

    def __init__(self, project, store):
        self.project, self.store = project, store
        self.instances = {}
        self.pending = {}  # 数据库重建时，按 profile 所在检出名等待检出登记
        self.guard = threading.RLock()

    def instance(self, checkout, identity):
        key = (checkout.id, identity)
        with self.guard:
            current = self.instances.get(key)
            if current is None or current.checkout.slot != checkout.slot:
                current = self.pending.get((checkout.name, identity))
                if current:
                    current.wait_recovery()
                    self.pending.pop((checkout.name, identity))
                    current.checkout = checkout
                    self.instances[key] = current
                    self.record(current, 'starting')
                    state, fields = current.deferred_record or ('ready' if current.alive() else 'exited', {})
                    self.record(current, state, **dict(fields, holder=None))
                    current.deferred_record = None
                else:
                    current = self.instances[key] = Instance(self, checkout, identity)
            current.wait_recovery()
            return current

    def row(self, checkout, identity):
        self.instance(checkout, identity)
        with self.store.local() as db:
            return db.execute('SELECT * FROM browser_instances WHERE checkout=? AND identity=?',
                              (checkout.id, identity)).fetchone()

    def record(self, instance, state, **fields):
        if instance.checkout.id is None:
            instance.deferred_record = (state, fields)
            return
        values = dict(port=instance.port(), pid=instance.process.pid if instance.process else None,
                      profile=str(instance.profile()), headed=int(instance.headed), state=state, log=str(instance.log()))
        values.update(fields)
        with self.store.local_tx() as db:
            db.execute('INSERT OR IGNORE INTO browser_instances(checkout,identity,port,profile,state) VALUES (?,?,?,?,?)',
                       (instance.checkout.id, instance.identity, values['port'], values['profile'], state))
            assignments = ', '.join(f'{name}=?' for name in values)
            if state == 'starting':
                assignments += ', started_at=?, exited_at=NULL, exit_code=NULL, signal=NULL, crash_report=NULL'
                parameters = list(values.values()) + [datetime.fromtimestamp(instance.started, timezone.utc).isoformat()]
            else:
                parameters = list(values.values())
            db.execute(f'UPDATE browser_instances SET {assignments} WHERE checkout=? AND identity=?',
                       (*parameters, instance.checkout.id, instance.identity))

    def exited(self, instance, code, *, expected):
        """浏览器进程退出：主动停止记为 exited；其他退出记录崩溃，并登记待人处理。"""
        name = signal_name(code)
        report = crash_report(instance.started)
        with self.store.local() as db:
            row = db.execute('SELECT * FROM browser_instances WHERE checkout=? AND identity=?',
                             (instance.checkout.id, instance.identity)).fetchone() if instance.checkout.id is not None else None
        crashes = json.loads(row['crashes']) if row else []
        state = 'exited'
        if not expected:
            moment = datetime.now(timezone.utc)
            crashes = [c for c in crashes if datetime.fromisoformat(c['at']) > moment - CRASH_WINDOW]
            crashes.append(dict(at=moment.isoformat(), code=code, signal=name, log=str(instance.log()), report=report,
                                holder=row['holder'] if row else None))
            state = 'crashed' if len(crashes) >= 2 else 'exited'
            text = (f"身份 {instance.identity} 的 Chrome for Testing（检出 {instance.checkout.name}）"
                    + exit_description(moment.isoformat(), code, name, str(instance.log()))
                    + (f'，崩溃报告 {report}' if report else ''))
        self.record(instance, state, pid=None, exited_at=now(), exit_code=code, signal=signal_name(code), crash_report=report,
                    crashes=dumps(crashes))

        if not expected:
            # 待人处理在业务记录库（附产出机器）；连不上共享库时只记本机的崩溃记录，不影响浏览器状态。
            from .ledger import insert_attention
            try:
                with self.store.records_tx() as db:
                    insert_attention(db, self.store, instance.checkout.name, '浏览器崩溃', text, by='sop-board')
            except Exception as error:  # noqa: BLE001
                import sys
                print(f'浏览器崩溃没能登记待人处理：{error}', file=sys.stderr)

    # ---- 租约 ----

    def lease(self, checkout, identities, holder, *, url=None):
        """为一次运行取得身份浏览器；返回 身份 → dict(endpoint 或 problem)。

        真人租约或其他持有者占用时 browser-busy；crashed 时 browser-crashed；启动即崩溃时返回 problem，不抛错。"""
        require_runtime()
        result = {}
        with self.guard:
            for identity in identities:
                self.ensure_recovered(self.instance(checkout, identity))
                row = self.row(checkout, identity)
                if row and row['state'] == 'crashed':
                    raise APIError(409, 'browser-crashed', [dict(field=identity, problem=
                        f'身份 {identity} 的浏览器 30 分钟内两次异常退出；检查后运行 sop-board browser open --identity {identity} --reset-crash')])
                if row and row['holder'] and row['holder'] != holder:
                    raise APIError(409, 'browser-busy', [dict(field=identity, problem=
                        f"身份 {identity} 的浏览器正由 {'真人登录' if row['holder'] == 'human' else row['holder']} 使用"
                        + (f'；真人完成后运行 sop-board browser release --identity {identity} 交还（窗口保持打开，运行直接沿用）'
                           if row['holder'] == 'human' else ''))])
            for identity in identities:
                instance = self.instance(checkout, identity)
                self.set_holder(checkout, identity, holder)
                if not instance.alive():
                    endpoint = instance.launch(headed=False, url=url)
                    if endpoint is None:
                        row = self.row(checkout, identity)
                        result[identity] = dict(problem=f"浏览器进程 {row['exited_at']} 因 {row['signal'] or '退出码 ' + str(row['exit_code'])} "
                                                        f"退出，日志 {row['log']}" + (f"，崩溃报告 {row['crash_report']}" if row['crash_report'] else ''))
                        continue
                result[identity] = dict(endpoint=instance.endpoint())
        return result

    def set_holder(self, checkout, identity, holder):
        with self.store.local_tx() as db:
            db.execute('INSERT OR IGNORE INTO browser_instances(checkout,identity,port,profile,state) VALUES (?,?,?,?,?)',
                       (checkout.id, identity, port_for(self.project, checkout, identity),
                        str(self.instance(checkout, identity).profile()), 'exited'))
            db.execute('UPDATE browser_instances SET holder=? WHERE checkout=? AND identity=?', (holder, checkout.id, identity))

    def release(self, holder):
        with self.store.local_tx() as db:
            db.execute('UPDATE browser_instances SET holder=NULL WHERE holder=?', (holder,))

    def crashes_since(self, checkout, holder, since):
        """该持有者租约期间记录的异常退出：身份 → [crash]。"""
        result = {}
        with self.store.local() as db:
            for row in db.execute('SELECT identity, crashes FROM browser_instances WHERE checkout=?', (checkout.id,)):
                found = [c for c in json.loads(row['crashes']) if c['at'] >= since and c.get('holder') == holder]
                if found:
                    result[row['identity']] = found
        return result

    # ---- 真人操作 ----

    def open(self, checkout, identity, *, headed, reset_crash=False, url=None):
        with self.guard:
            self.ensure_recovered(self.instance(checkout, identity))
            row = self.row(checkout, identity)
            if row and row['state'] == 'crashed' and not reset_crash:
                raise APIError(409, 'browser-crashed', [dict(field=identity, problem='浏览器处于 crashed，加 --reset-crash 重置')])
            if row and row['holder'] and row['holder'] != 'human':
                raise APIError(409, 'browser-busy', [dict(field=identity, problem=f"身份 {identity} 的浏览器正由 {row['holder']} 使用")])
            if reset_crash and row:
                with self.store.local_tx() as db:
                    db.execute("UPDATE browser_instances SET state='exited', crashes='[]' WHERE checkout=? AND identity=?",
                               (checkout.id, identity))
            instance = self.instance(checkout, identity)
            # 只有要窗口而现有进程是无头时才重启；有界面窗口一律沿用，不把人做过的登录与验证关掉。
            if headed and instance.alive() and not instance.headed:
                instance.stop()
            if headed:
                self.set_holder(checkout, identity, 'human')
            endpoint = instance.endpoint() if instance.alive() else instance.launch(headed=headed, url=url)
            if endpoint is None:
                if headed:
                    self.release_identity(checkout, identity)
                raise ValueError(f'Chrome for Testing 启动即退出；日志：{instance.log()}')
            return self.status(checkout, identity)

    def release_identity(self, checkout, identity):
        with self.store.local_tx() as db:
            db.execute('UPDATE browser_instances SET holder=NULL WHERE checkout=? AND identity=?', (checkout.id, identity))

    def handover(self, checkout, identity):
        """真人交还：只解除 human 租约，浏览器进程与窗口保持运行，下一次取得租约的运行直接沿用。"""
        port_for(self.project, checkout, identity)
        with self.guard:
            row = self.row(checkout, identity)
            if row and row['holder'] and row['holder'] != 'human':
                raise APIError(409, 'browser-busy', [dict(field=identity, problem=f"身份 {identity} 的浏览器正由 {row['holder']} 使用")])
            self.release_identity(checkout, identity)
            return self.status(checkout, identity)

    def close(self, checkout, identity):
        with self.guard:
            instance = self.instance(checkout, identity)
            row = self.row(checkout, identity)
            if row and row['holder'] and row['holder'] != 'human':
                raise APIError(409, 'browser-busy', [dict(field=identity, problem=f"身份 {identity} 的浏览器正由 {row['holder']} 使用")])
            instance.stop()
            instance.recovery_error = None
            if not instance.alive():
                self.record(instance, 'exited', pid=None, holder=None)
            self.release_identity(checkout, identity)
            return self.status(checkout, identity)

    def status(self, checkout, identity=None):
        for name, pending_identity in list(self.pending):
            if name == checkout.name:
                self.instance(checkout, pending_identity)
        with self.store.local() as db:
            rows = [dict(row) for row in db.execute('SELECT * FROM browser_instances WHERE checkout=? ORDER BY identity', (checkout.id,))
                    if identity is None or row['identity'] == identity]
        for row in rows:
            instance = self.instances.get((checkout.id, row['identity']))
            row['alive'] = bool(instance and instance.alive())
            row['crashes'] = json.loads(row['crashes'])
            # port 是实际（运行中）或最近一次启动所用的端口；缺省端口被占用时两者不同。
            try:
                row['default_port'] = port_for(self.project, checkout, row['identity'])
            except ValueError:
                row['default_port'] = None
            if instance and instance.recovery_error and row['alive']:
                row['state'] = 'unrecovered'
                row['reason'] = instance.recovery_error
            confirmed = instance and instance.recovered.is_set() and not instance.recovery_error
            row['endpoint'] = f"http://127.0.0.1:{row['port']}" if row['alive'] and confirmed else None
        return dict(checkout=checkout.name, runtime=runtime_info(), browsers=rows)

    def recover(self, handover=None):
        """仅检查本项目 profile 的锁；认回有界面窗口，保留未认回窗口，清理核实过的无头浏览器。

        handover 是 sop-board restart 交接的记录（daemon.read_handover）：其中列出的浏览器（含无头的）也认回，
        租约持有者照旧有效（已定决策 28）。"""
        from . import checkouts
        from types import SimpleNamespace
        kept = {item['profile']: item.get('holder') for item in (handover or {}).get('browsers', [])}
        with self.store.local_tx() as db:
            checkouts.ensure_main(db, self.project)
            registered = {c.name: c for c in checkouts.active(db)}
            db.execute("UPDATE browser_instances SET holder=NULL, pid=NULL, state=CASE WHEN state='crashed' THEN 'crashed' ELSE 'exited' END")
            # 交接下来的租约立即恢复：认回核对期间别的运行也拿不走这个身份。
            for profile, holder in kept.items():
                if holder:
                    db.execute('UPDATE browser_instances SET holder=? WHERE profile=?', (holder, profile))
        info = runtime_info()
        if info is None:
            return
        root = self.project.data / 'browsers'
        for profile in sorted(root.glob('*/*/profile')):
            if any(p.is_symlink() for p in (root, profile.parent.parent, profile.parent, profile)):
                continue
            found = profile_process(profile, info)
            if not found:
                continue
            process, port, headed = found
            name, identity = profile.parent.parent.name, profile.parent.name
            checkout = registered.get(name) or SimpleNamespace(id=None, name=name, slot=0)
            instance = Instance(self, checkout, identity)
            instance.process, instance.actual, instance.headed = process, port, headed
            instance.started = process.started
            handed_over = str(profile) in kept
            if not headed and not handed_over:
                instance.stop()
                continue
            instance.keep_holder = kept.get(str(profile))
            instance.recovered.clear()
            if checkout.id is None:
                self.pending[name, identity] = instance
            else:
                self.instances[checkout.id, identity] = instance
                self.record(instance, 'starting', holder=instance.keep_holder)
            self.start_recovery(instance)

    def handover_report(self, handover):
        """restart 交接下来的各浏览器现在的情况：认回、核对中、未认回或已不在。"""
        found = {str(i.profile()): i for i in list(self.instances.values()) + list(self.pending.values())}
        result = []
        for item in (handover or {}).get('browsers', []):
            instance = found.get(item['profile'])
            if instance is None or not instance.alive():
                state = 'gone'
            elif not instance.recovered.is_set():
                state = 'checking'
            else:
                state = 'unrecovered' if instance.recovery_error else 'adopted'
            result.append(dict(checkout=item.get('checkout'), identity=item.get('identity'), pid=item.get('pid'),
                               port=item.get('port'), headed=item.get('headed'), holder=item.get('holder'), state=state,
                               reason=instance.recovery_error if instance is not None else None))
        return result

    def start_recovery(self, instance):
        instance.recovered.clear()
        instance.recovery_error = None
        instance.recovery_thread = threading.Thread(target=self.confirm_recovery, args=(instance,), daemon=True,
                                                   name=f'browser-recover-{instance.checkout.name}-{instance.identity}')
        instance.recovery_thread.start()

    def ensure_recovered(self, instance):
        instance.wait_recovery()
        if not instance.recovery_error:
            return
        if not instance.alive():
            instance.recovery_error = None
            self.record(instance, 'exited', pid=None, holder=None)
            return
        self.start_recovery(instance)
        instance.wait_recovery()
        if instance.recovery_error:
            raise APIError(409, 'browser-unrecovered', [dict(field=instance.identity, problem=
                f'身份 {instance.identity} 的有界面浏览器未认回（进程 {instance.process.pid}）：{instance.recovery_error}；'
                f'窗口恢复应答后再次请求会重新核对并认回；确实不要这个窗口了，用 '
                f'sop-board browser close --identity {instance.identity} 结束它')])

    def confirm_recovery(self, instance):
        """后台最多核对十五秒；不取管理器锁，等待此结果的请求可以持有该锁。"""
        process, port = instance.process, instance.port()
        deadline = time.monotonic() + READY_SECONDS
        reason = f'调试端口 {port} 没有应答'
        try:
            while not instance.detached.is_set() and process.poll() is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                owners = listener_pids(port, timeout=min(1, remaining))
                reason = (f'调试端口 {port} 被其他进程占用（监听进程：{", ".join(map(str, sorted(owners)))}）'
                          if owners and owners != {process.pid} else f'调试端口 {port} 没有应答或应答来源尚未确认')
                # 明确属于其他进程时，不请求它的调试接口；无输出时核对日志中的浏览器标识。
                if not owners or owners == {process.pid}:
                    remaining = deadline - time.monotonic()
                    heard = listening(instance.log(), 0, latest=True) if not owners else None
                    log_matches_port = heard and heard[0] == f'127.0.0.1:{port}'
                    ws = (debugger_ready(port, timeout=min(1, remaining))
                          if remaining > 0 and (owners or log_matches_port) else None)
                    matched = (owners == {process.pid} or
                               (heard and heard[0] == f'127.0.0.1:{port}' and
                                ws == f'ws://127.0.0.1:{port}/devtools/browser/{heard[1]}'))
                    if ws and matched and process.poll() is None:
                        if instance.detached.is_set():
                            return
                        self.record(instance, 'ready', holder=instance.keep_holder)
                        print(f"已认回{'有界面' if instance.headed else '无头'}浏览器：检出 {instance.checkout.name}，身份 {instance.identity}，"
                              f'进程 {process.pid}，调试端口 {port}', flush=True)
                        instance.recovered.set()
                        instance.watcher = threading.Thread(target=instance.watch, args=(process,), daemon=True,
                                                           name=f'browser-{instance.checkout.name}-{instance.identity}')
                        instance.watcher.start()
                        return
                if instance.detached.wait(min(.2, max(0, deadline - time.monotonic()))):
                    return
            if not instance.detached.is_set():
                if process.poll() is None and not instance.headed:
                    # 交接下来的无头浏览器核对不通过：里面没有真人的登录与验证，结束它；租约留给原持有者，下次取得时重新启动。
                    instance.stop()
                    self.record(instance, 'exited', pid=None, holder=instance.keep_holder)
                    print(f'交接下来的无头浏览器没有认回，已结束：检出 {instance.checkout.name}，身份 {instance.identity}，'
                          f'进程 {process.pid}，原因：{reason}', flush=True)
                elif process.poll() is None:
                    instance.recovery_error = reason
                    self.record(instance, 'starting', holder=instance.keep_holder)
                    print(f'有界面浏览器未认回：检出 {instance.checkout.name}，身份 {instance.identity}，'
                          f'进程 {process.pid}，原因：{reason}', flush=True)
                else:
                    self.record(instance, 'exited', pid=None, holder=instance.keep_holder)
        except Exception as error:  # noqa: BLE001
            # 核对自身失败时不另起浏览器，也不结束窗口；请求明确报错。
            instance.recovery_error = f'核对失败：{error}'
            self.record(instance, 'starting', holder=instance.keep_holder)
        finally:
            if instance.detached.is_set() and not instance.recovered.is_set():
                instance.recovery_error = '看板正在停止，已取消认回核对'
            instance.recovered.set()

    def alive_instances(self):
        """运行中的浏览器：检出、身份、进程号、调试端口、有无界面、profile 与租约持有者。"""
        with self.store.local() as db:
            holders = {(r['checkout'], r['identity']): r['holder'] for r in db.execute('SELECT checkout, identity, holder FROM browser_instances')}
        return [dict(checkout=i.checkout.name, identity=i.identity, pid=i.process.pid, port=i.port(), headed=i.headed,
                     profile=str(i.profile()), holder=holders.get((i.checkout.id, i.identity)))
                for i in list(self.instances.values()) + list(self.pending.values()) if i.alive()]

    def shutdown(self, *, handover=False):
        """正常停止保留有界面的窗口、结束无头浏览器；restart 交接时全部保留（已定决策 28）。返回保留的浏览器。"""
        kept_items = {(item['checkout'], item['identity']): item for item in self.alive_instances()}
        instances = list(self.instances.values()) + list(self.pending.values())
        for instance in instances:
            if instance.recovery_thread:
                instance.detached.set()
        for instance in instances:
            if instance.recovery_thread:
                instance.recovery_thread.join()
        kept = []
        for instance in instances:
            if (instance.headed or handover) and instance.alive():
                instance.stopping = True
                instance.detached.set()
                if instance.watcher:
                    instance.watcher.join(timeout=5)
                kept.append(kept_items.get((instance.checkout.name, instance.identity)) or dict(
                    checkout=instance.checkout.name, identity=instance.identity, pid=instance.process.pid,
                    port=instance.port(), headed=instance.headed, profile=str(instance.profile()), holder=None))
                print(f"已保留{'有界面' if instance.headed else '无头'}浏览器：检出 {instance.checkout.name}，身份 {instance.identity}，"
                      f'进程 {instance.process.pid}，调试端口 {instance.port()}', flush=True)
            else:
                instance.stop()
        return kept


# ---- 守护进程挂载与本机令牌接口 ----

def attach(app):
    from . import extensions
    app.browsers = Manager(app.project, app.store)
    extensions.RECOVER.append(lambda app: app.browsers.recover(app.handed_over))
    extensions.CLOSE.append(lambda app: app.browsers.shutdown(handover=app.handover))


def _entry_url(app, checkout):
    """真人登录窗口打开的地址：第一个 Web 入口在该检出环境中的地址。"""
    addresses = app.envs.status(checkout).get('address_env', {})
    for entry in app.project.entries.values():
        if isinstance(entry, dict) and entry.get('surface') == 'web' and entry.get('address_env') in addresses:
            return addresses[entry['address_env']]
    return None


def _register():
    from .server import json_response, route

    def checkout_of(app, request):
        return app.checkout(request.json_field('checkout'))

    @route('POST', '/local/browser/lease', 'token')
    def lease(app, request):
        checkout = checkout_of(app, request)
        identities = request.json_field('identities') or ['default']
        holder = request.json_field('holder')
        if not isinstance(holder, str) or not holder:
            raise ValueError('须提供租约持有者')
        return json_response(dict(ok=True, browsers=app.browsers.lease(checkout, identities, holder,
                                                                       url=request.json_field('url'))))

    @route('POST', '/local/browser/release', 'token')
    def release(app, request):
        checkout = checkout_of(app, request)
        return json_response(dict(ok=True, browser=app.browsers.handover(checkout, request.json_field('identity') or 'default')))

    @route('POST', '/local/browser/crashes', 'token')
    def crashes(app, request):
        checkout = checkout_of(app, request)
        return json_response(dict(ok=True, crashes=app.browsers.crashes_since(
            checkout, request.json_field('holder'), request.json_field('since') or '')))

    @route('POST', '/local/browser/open', 'token')
    def open_(app, request):
        checkout = checkout_of(app, request)
        identity = request.json_field('identity') or 'default'
        headed = bool(request.json_field('headed'))
        url = request.json_field('url') or (_entry_url(app, checkout) if headed else None)
        return json_response(dict(ok=True, browser=app.browsers.open(
            checkout, identity, headed=headed, reset_crash=bool(request.json_field('reset_crash')), url=url)))

    @route('POST', '/local/browser/close', 'token')
    def close(app, request):
        checkout = checkout_of(app, request)
        return json_response(dict(ok=True, browser=app.browsers.close(checkout, request.json_field('identity') or 'default')))

    @route('POST', '/local/browser/status', 'token')
    def status(app, request):
        checkout = checkout_of(app, request)
        return json_response(dict(ok=True, **app.browsers.status(checkout, request.json_field('identity'))))


_register()
