"""浏览器代理：sop-board 自带的 Playwright 运行时与 Google Chrome for Testing。

- 运行时是本机级的：<SOP_BOARD_HOME>/runtime，固定版本（随包 lock 文件），浏览器装在同目录 browsers/，
  不读写共享的 ms-playwright 缓存，也不碰用户日常 Chrome。
- 只有看板守护进程启动浏览器：每个 检出×身份 一个 CfT，独立持久 profile 与调试端口，缺省无头；
  执行者通过 CDP 连接。真实账号由人在 `browser open --headed` 打开的窗口里登录、完成验证，之后
  `browser release` 交还：只解除真人租约，窗口保持运行，之后的运行直接沿用它，人做过的验证不作废。
- 浏览器异常退出时记录信号、日志与崩溃报告；租约持有期间不自动重启；30 分钟内两次异常退出进入 crashed。
"""
from datetime import datetime, timedelta, timezone
from importlib.resources import files
import json
import os
from pathlib import Path
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
    order = identity_order(project)
    if identity not in order:
        raise ValueError(f'没有声明身份 {identity}')
    index = order.index(identity)
    if index >= SLOT_WIDTH:
        raise ValueError(f'身份超过 {SLOT_WIDTH} 个，无法分配调试端口')
    return BASE_PORT + checkout.slot * SLOT_WIDTH + index


def port_in_use(port):
    with socket.socket() as sock:
        sock.settimeout(.5)
        return sock.connect_ex(('127.0.0.1', port)) == 0


def debugger_ready(port):
    try:
        opener = build_opener(ProxyHandler({}))
        with opener.open(f'http://127.0.0.1:{port}/json/version', timeout=1) as response:
            return json.loads(response.read()).get('webSocketDebuggerUrl')
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


class Instance:
    def __init__(self, manager, checkout, identity):
        self.manager, self.checkout, self.identity = manager, checkout, identity
        self.process, self.watcher = None, None
        self.started, self.stopping = 0.0, False
        self.headed = False

    @property
    def project(self):
        return self.manager.project

    def profile(self):
        return self.project.data / 'browsers' / self.checkout.name / self.identity / 'profile'

    def log(self):
        return self.profile().parent / 'chrome.log'

    def port(self):
        return port_for(self.project, self.checkout, self.identity)

    def alive(self):
        return self.process is not None and self.process.poll() is None

    def endpoint(self):
        return f'http://127.0.0.1:{self.port()}'

    def launch(self, *, headed, url=None):
        info = require_runtime()
        port = self.port()
        if port_in_use(port):
            raise ValueError(f'调试端口 {port} 已被其他进程占用；不改用随机端口')
        profile = self.profile()
        profile.mkdir(parents=True, exist_ok=True)
        argv = [info['executable'], f'--user-data-dir={profile}', f'--remote-debugging-port={port}',
                '--no-first-run', '--no-default-browser-check', '--use-mock-keychain']
        if not headed:
            argv.append('--headless=new')
        argv.append(url or 'about:blank')
        log = self.log().open('ab')
        self.stopping, self.headed, self.started = False, headed, time.time()
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
            if debugger_ready(port):
                self.manager.record(self, 'ready')
                return self.endpoint()
            time.sleep(.2)
        if self.process.poll() is None:
            self.stop()
            raise ValueError(f'Chrome for Testing {READY_SECONDS} 秒内没有就绪；日志：{self.log()}')
        self.watcher.join(timeout=5)
        return None

    def watch(self, process):
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
                os.killpg(process.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                try:
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
        self.guard = threading.RLock()

    def instance(self, checkout, identity):
        key = (checkout.id, identity)
        with self.guard:
            current = self.instances.get(key)
            if current is None or current.checkout.slot != checkout.slot:
                current = self.instances[key] = Instance(self, checkout, identity)
            return current

    def row(self, checkout, identity):
        with self.store.connection() as db:
            return db.execute('SELECT * FROM browser_instances WHERE checkout=? AND identity=?',
                              (checkout.id, identity)).fetchone()

    def record(self, instance, state, **fields):
        values = dict(port=instance.port(), pid=instance.process.pid if instance.process else None,
                      profile=str(instance.profile()), headed=int(instance.headed), state=state, log=str(instance.log()))
        values.update(fields)
        with self.store.transaction() as db:
            db.execute('INSERT OR IGNORE INTO browser_instances(checkout,identity,port,profile,state) VALUES (?,?,?,?,?)',
                       (instance.checkout.id, instance.identity, values['port'], values['profile'], state))
            assignments = ', '.join(f'{name}=?' for name in values)
            if state == 'starting':
                assignments += ', started_at=?, exited_at=NULL, exit_code=NULL, signal=NULL, crash_report=NULL'
                parameters = list(values.values()) + [now()]
            else:
                parameters = list(values.values())
            db.execute(f'UPDATE browser_instances SET {assignments} WHERE checkout=? AND identity=?',
                       (*parameters, instance.checkout.id, instance.identity))

    def exited(self, instance, code, *, expected):
        """浏览器进程退出：主动停止记为 exited；其他退出记录崩溃，并登记待人处理。"""
        name = signal_name(code)
        report = crash_report(instance.started)
        row = self.row(instance.checkout, instance.identity)
        crashes = json.loads(row['crashes']) if row else []
        state = 'exited'
        if not expected:
            moment = datetime.now(timezone.utc)
            crashes = [c for c in crashes if datetime.fromisoformat(c['at']) > moment - CRASH_WINDOW]
            crashes.append(dict(at=moment.isoformat(), code=code, signal=name, log=str(instance.log()), report=report,
                                holder=row['holder'] if row else None))
            state = 'crashed' if len(crashes) >= 2 else 'exited'
            text = (f"身份 {instance.identity} 的 Chrome for Testing（检出 {instance.checkout.name}）"
                    f"{moment.astimezone().strftime('%Y-%m-%d %H:%M:%S')} 因 {name or f'退出码 {code}'} 退出，日志 {instance.log()}"
                    + (f'，崩溃报告 {report}' if report else ''))
            with self.store.transaction() as db:
                db.execute('INSERT INTO attention(checkout,kind,text,created_by,created_at) VALUES (?,?,?,?,?)',
                           (instance.checkout.id, '浏览器崩溃', text, 'sop-board', now()))
        self.record(instance, state, pid=None, exited_at=now(), exit_code=code, signal=name, crash_report=report,
                    crashes=dumps(crashes))

    # ---- 租约 ----

    def lease(self, checkout, identities, holder, *, url=None):
        """为一次运行取得身份浏览器；返回 身份 → dict(endpoint 或 problem)。

        真人租约或其他持有者占用时 browser-busy；crashed 时 browser-crashed；启动即崩溃时返回 problem，不抛错。"""
        require_runtime()
        result = {}
        with self.guard:
            for identity in identities:
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
        with self.store.transaction() as db:
            db.execute('INSERT OR IGNORE INTO browser_instances(checkout,identity,port,profile,state) VALUES (?,?,?,?,?)',
                       (checkout.id, identity, port_for(self.project, checkout, identity),
                        str(self.instance(checkout, identity).profile()), 'exited'))
            db.execute('UPDATE browser_instances SET holder=? WHERE checkout=? AND identity=?', (holder, checkout.id, identity))

    def release(self, holder):
        with self.store.transaction() as db:
            db.execute('UPDATE browser_instances SET holder=NULL WHERE holder=?', (holder,))

    def crashes_since(self, checkout, holder, since):
        """该持有者租约期间记录的异常退出：身份 → [crash]。"""
        result = {}
        with self.store.connection() as db:
            for row in db.execute('SELECT identity, crashes FROM browser_instances WHERE checkout=?', (checkout.id,)):
                found = [c for c in json.loads(row['crashes']) if c['at'] >= since and c.get('holder') == holder]
                if found:
                    result[row['identity']] = found
        return result

    # ---- 真人操作 ----

    def open(self, checkout, identity, *, headed, reset_crash=False, url=None):
        with self.guard:
            row = self.row(checkout, identity)
            if row and row['state'] == 'crashed' and not reset_crash:
                raise APIError(409, 'browser-crashed', [dict(field=identity, problem='浏览器处于 crashed，加 --reset-crash 重置')])
            if row and row['holder'] and row['holder'] != 'human':
                raise APIError(409, 'browser-busy', [dict(field=identity, problem=f"身份 {identity} 的浏览器正由 {row['holder']} 使用")])
            if reset_crash and row:
                with self.store.transaction() as db:
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
        with self.store.transaction() as db:
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
            self.release_identity(checkout, identity)
            return self.status(checkout, identity)

    def status(self, checkout, identity=None):
        with self.store.connection() as db:
            rows = [dict(row) for row in db.execute('SELECT * FROM browser_instances WHERE checkout=? ORDER BY identity', (checkout.id,))
                    if identity is None or row['identity'] == identity]
        for row in rows:
            instance = self.instances.get((checkout.id, row['identity']))
            row['alive'] = bool(instance and instance.alive())
            row['crashes'] = json.loads(row['crashes'])
            row['endpoint'] = f"http://127.0.0.1:{row['port']}" if row['alive'] else None
        return dict(checkout=checkout.name, runtime=runtime_info(), browsers=rows)

    def recover(self):
        """守护进程启动时结束上次遗留的浏览器（命令行含本项目 profile 路径的进程），释放租约。"""
        root = str(self.project.data / 'browsers')
        try:
            listing = subprocess.run(['ps', '-axo', 'pid=,pgid=,command='], capture_output=True, text=True, timeout=5).stdout
        except (OSError, subprocess.SubprocessError):
            listing = ''
        for line in listing.splitlines():
            parts = line.split(None, 2)
            if len(parts) == 3 and parts[0] == parts[1] and f'--user-data-dir={root}' in parts[2]:
                try:
                    os.killpg(int(parts[0]), signal.SIGTERM)
                except (ProcessLookupError, PermissionError):
                    pass
        with self.store.transaction() as db:
            db.execute("UPDATE browser_instances SET holder=NULL, pid=NULL, state=CASE WHEN state='crashed' THEN 'crashed' ELSE 'exited' END")

    def shutdown(self):
        for instance in list(self.instances.values()):
            instance.stop()


# ---- 守护进程挂载与本机令牌接口 ----

def attach(app):
    from . import extensions
    app.browsers = Manager(app.project, app.store)
    extensions.RECOVER.append(lambda app: app.browsers.recover())
    extensions.CLOSE.append(lambda app: app.browsers.shutdown())


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
