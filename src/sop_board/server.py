"""看板守护进程：网页、按检出的环境、浏览器代理与本机无头会话。

访问级别：
- lan：内网可用（查看、人工复测、答复、批注与同意草稿、撤销自己的同意、上传）；写入须同源且为 JSON。
- local：只接受实际回环连接（发起运行、沉淀、守护等）；写入须同源。
- token：回环连接且携带数据目录中的本机令牌（CLI 调用）。
内网响应遮蔽凭据取值、变量名与本机路径。
"""
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
import hmac
import json
import os
from pathlib import Path
import re
import signal
import sys
from urllib.parse import parse_qs, unquote, urlsplit

from . import checkouts, config, daemon
from .errors import APIError, reject
from .network import is_loopback, local_ipv4_addresses
from .redaction import names, response_bytes
from .store import Store

ROUTES = []
ASSETS = {'board.css': 'text/css; charset=utf-8', 'board.js': 'text/javascript; charset=utf-8',
          'vendor/mermaid/mermaid.min.js': 'text/javascript; charset=utf-8'}
MAX_BODY = 64 * 1024 * 1024


def route(method, pattern, access):
    def register(handler):
        ROUTES.append((method, re.compile(pattern + r'\Z'), access, handler))
        return handler
    return register


def same_origin(origin, host):
    try:
        from urllib.parse import urlsplit as split
        source, target = split(origin), split('http://' + host)
        for value in (source, target):
            if (value.scheme != 'http' or not value.hostname or value.username is not None
                    or value.password is not None or value.path or value.query or value.fragment):
                return False
        return (source.hostname, source.port or 80) == (target.hostname, target.port or 80)
    except ValueError:
        return False


class Request:
    def __init__(self, handler, match):
        self.handler, self.match = handler, match
        parts = urlsplit(handler.path)
        self.path, self.query = parts.path, {k: v[-1] for k, v in parse_qs(parts.query).items()}
        self.method = handler.command
        self.local = is_loopback(handler.client_address[0])
        self.headers = handler.headers
        self.body, self.raw = None, b''

    def arg(self, index):
        return unquote(self.match.group(index))

    def json_field(self, name, default=None):
        return self.body.get(name, default) if isinstance(self.body, dict) else default


class Response:
    def __init__(self, status, raw, content_type, headers=(), *, verbatim=False):
        self.status, self.raw, self.content_type, self.headers = status, raw, content_type, list(headers)
        # verbatim：对等读取证据时原样返回（内网遮蔽会改变内容，使 sha256 对不上）；只用于须带对等令牌的 /peer/ 接口。
        self.verbatim = verbatim


def json_response(value, status=200):
    return Response(status, json.dumps(value, ensure_ascii=False, default=str).encode(), 'application/json; charset=utf-8')


def html_response(text, status=200):
    return Response(status, text.encode(), 'text/html; charset=utf-8')


class App:
    """守护进程内共享的项目、数据库与各管理器。"""

    def __init__(self, project, store, *, port, lan):
        from . import envs
        self.project, self.store = project, store
        self.port, self.lan = port, lan
        self.addresses, self.registered = [], False
        # 对等令牌：带 --lan 时登记到业务记录库，别的看板凭它读取本机证据。
        self.peer_token = __import__('secrets').token_urlsafe(24) if lan else None
        self.envs = envs.Manager(project, store)
        self.token = None
        self.config_stamp = None
        self.started_at = datetime.now(timezone.utc).isoformat()
        self.extensions = {}
        # sop-board restart（已定决策 28）：handover 为真时本进程正在交接，停止时保留浏览器与会话；
        # handed_over 是上一个守护进程交接给本进程的记录（保留的浏览器、会话、环境与运行）。
        self.handover = False
        self.handed_over = None
        self.env_restarts = {}   # 交接后重新拉起的环境：检出名 → dict(state, error)
        self.reload()

    def reload(self):
        """config.yaml 变化后重新读取；配置不合格时保留问题供页面显示。"""
        try:
            stamp = self.project.config_path.stat().st_mtime_ns
        except OSError:
            stamp = None
        if stamp != self.config_stamp:
            self.project.reload()
            self.config_stamp = stamp
        return self.project

    def checkout(self, name=None):
        self.reload()
        with self.store.local_tx() as db:
            main = checkouts.ensure_main(db, self.project)
            return checkouts.by_name(db, name) if name else main

    def checkouts(self):
        self.reload()
        with self.store.local_tx() as db:
            checkouts.ensure_main(db, self.project)
            return checkouts.active(db)

    def credential_names(self):
        result = set(names(self.project.entries))
        if self.project.fixtures:
            result.update(self.project.fixtures.get('credential_env', []))
        return result

    def local_roots(self):
        roots = [self.project.data, *self.project.repos.values()]
        for checkout in self.checkouts():
            roots += list(checkout.paths.values())
        return roots


class Handler(BaseHTTPRequestHandler):
    server_version = 'sop-board'

    def setup(self):
        super().setup()
        self.connection.settimeout(30)

    def log_message(self, format, *args):
        pass

    def do_GET(self):
        self.dispatch()

    def do_POST(self):
        self.dispatch()

    def do_PUT(self):
        self.dispatch()

    def do_DELETE(self):
        self.dispatch()

    def send(self, response, request):
        raw, headers = response.raw, response.headers
        if (not request.local and not request.path.startswith('/assets/') and not response.verbatim
                and response.status != 206 and not any(key.lower() == 'content-range' for key, _ in headers)):
            app = self.server.app
            app.reload()
            names_ = app.credential_names()
            roots = app.local_roots()
            raw = response_bytes(raw, response.content_type, names_, local=False, local_roots=roots)
            headers = [(name, response_bytes(value.encode(), 'text/plain', names_, local=False, local_roots=roots)
                        .decode().replace('<本机路径>', '<local-path>')) for name, value in headers]
        self.send_response(response.status)
        self.send_header('Content-Type', response.content_type)
        self.send_header('Content-Length', str(len(raw)))
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Cache-Control', 'no-store')
        for name, value in headers:
            self.send_header(name, value)
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(raw)

    def dispatch(self):
        app = self.server.app
        path = urlsplit(self.path).path
        request = None
        try:
            if self.command == 'GET' and path.startswith('/assets/'):
                name = path[len('/assets/'):]
                if name not in ASSETS:
                    reject(404, 'not-found', 'path', '资源不存在')
                request = Request(self, None)
                self.send(Response(200, files('sop_board').joinpath('assets', name).read_bytes(), ASSETS[name]), request)
                return
            for method, pattern, access, handler in ROUTES:
                match = pattern.fullmatch(path)
                if match and method == self.command:
                    request = Request(self, match)
                    self.authorize(request, access)
                    self.read_body(request)
                    self.send(handler(app, request), request)
                    return
            request = Request(self, None)
            reject(404, 'not-found', 'path', '页面或接口不存在')
        except APIError as error:
            request = request or Request(self, None)
            # 业务记录库的错误：本机写明连的是哪台主机，内网只说明连不上，不出现主机、端口与库名。
            problems = error.problems if request.local else getattr(error, 'lan_problems', error.problems)
            if self.command == 'GET' and path.startswith('/p/'):
                self.send(html_response(error_page(error.code, problems), error.status), request)
            else:
                self.send(json_response(dict(ok=False, error_code=error.code, problems=problems, **error.details), error.status), request)
        except (ValueError, OSError) as error:
            request = request or Request(self, None)
            code = 'env-busy' if type(error).__name__ == 'EnvBusy' else 'invalid'
            status = 409 if code != 'invalid' else 422
            body = dict(ok=False, error_code=code, problems=[dict(field='request', problem=str(error))])
            owner = getattr(error, 'owner', None)
            if owner and request.local:
                body['owner'] = owner
            self.send(json_response(body, status), request)
        except Exception as error:  # noqa: BLE001 - 任何未预期的错误都返回 500，不让连接无响应断开
            import traceback
            from .redaction import redact
            print(redact(traceback.format_exc().encode(), ()).decode(), file=sys.stderr)
            request = request or Request(self, None)
            detail = f'{type(error).__name__}: {error}' if request.local else type(error).__name__
            self.send(json_response(dict(ok=False, error_code='internal-error',
                                         problems=[dict(field='server', problem='看板内部错误：' + detail)]), 500), request)

    def authorize(self, request, access):
        if access == 'lan':
            pass
        elif not request.local:
            reject(403, 'local-only', 'client', '此操作仅限看板所在的本机')
        if access == 'token':
            supplied = self.headers.get('Authorization', '')
            expected = 'Bearer ' + (self.server.app.token or '')
            if not self.server.app.token or not hmac.compare_digest(supplied, expected):
                reject(401, 'token-required', 'Authorization', '需要数据目录中的本机令牌')
        elif request.method in ('POST', 'PUT', 'DELETE'):
            origin = self.headers.get('Origin')
            if origin is not None and not same_origin(origin, self.headers.get('Host', '')):
                reject(403, 'cross-origin', 'Origin', '写入请求必须与看板同源')

    def read_body(self, request):
        if request.method not in ('POST', 'PUT', 'DELETE'):
            return
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if length < 0:
                raise ValueError()
        except ValueError:
            reject(400, 'invalid-request', 'Content-Length', '应为非负字节数')
        if length > MAX_BODY:
            reject(413, 'too-large', 'content', '请求正文不能超过 64 MiB')
        raw = self.rfile.read(length)
        if len(raw) != length:
            reject(400, 'invalid-request', 'content', '请求正文不完整')
        request.raw = raw
        if not raw:
            request.body = {}
            return
        if self.headers.get_content_type() != 'application/json':
            reject(415, 'json-required', 'Content-Type', '请求正文必须为 application/json')
        try:
            request.body = json.loads(raw.decode('utf-8'))
        except (UnicodeError, ValueError):
            reject(400, 'invalid-request', 'body', '应为 UTF-8 JSON')


def error_page(code, problems):
    from html import escape
    text = ''.join(f'<p>{escape(p.get("problem", ""))}</p>' for p in problems)
    return (f'<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><title>Flow Board</title>'
            f'<link rel="stylesheet" href="/assets/board.css"></head><body class="plain"><main><section class="page">'
            f'<div class="card"><h1>页面暂时无法显示</h1>{text}<p class="muted">{escape(code)}</p></div></section></main></body></html>')


# ---- 对等接口：别的看板读取本机的证据 ----

@route('GET', '/peer/hello', 'lan')
def peer_hello(app, request):
    from . import peers
    if not peers.allowed(app, request):
        reject(403, 'peer-token-required', 'peer', '需要业务记录库中登记的对等令牌')
    return json_response(dict(ok=True, machine=app.store.machine['id']))


@route('GET', r'/peer/evidence/([0-9a-f]{64})', 'lan')
def peer_evidence(app, request):
    from . import peers
    if not peers.allowed(app, request):
        reject(403, 'peer-token-required', 'peer', '需要业务记录库中登记的对等令牌')
    sha = request.arg(1)
    if not app.store.has_evidence(sha):
        reject(404, 'evidence-not-found', 'evidence', '本机没有这份证据')
    return Response(200, app.store.read_evidence(sha), 'application/octet-stream', verbatim=True)


# ---- 本机令牌接口：环境 ----

def _checkout_of(app, request):
    return app.checkout(request.json_field('checkout') or request.query.get('checkout'))


@route('GET', '/local/health', 'token')
def health(app, request):
    result = dict(ok=True, project=app.project.name, pid=os.getpid(), started_at=app.started_at)
    if app.handed_over:
        result['restart'] = handover_report(app)
    return json_response(result)


def handover_report(app):
    """上一个守护进程交接下来的浏览器、会话与环境现在的情况，供 sop-board restart 输出。"""
    handed = app.handed_over
    adopted = {item['launch'] for item in getattr(app.launcher, 'adopted', [])} if hasattr(app, 'launcher') else set()
    return dict(
        from_pid=handed.get('from_pid'),
        browsers=app.browsers.handover_report(handed) if hasattr(app, 'browsers') else [],
        launches=[dict(item, state='adopted' if item['launch'] in adopted else 'gone') for item in handed.get('launches', [])],
        environments=[dict(checkout=name, **app.env_restarts.get(name, dict(state='starting')))
                      for name in handed.get('environments', [])],
        runs=[dict(run=item['id'], checkout=item['checkout']) for item in handed.get('runs', [])])


def _signal_self():
    """先把响应发出去，再让主线程按 SIGTERM 的规则收尾。"""
    import threading
    threading.Timer(.3, os.kill, (os.getpid(), signal.SIGTERM)).start()


@route('POST', '/local/stop', 'token')
def stop_daemon(app, request):
    """sop-board stop：按正常停止的规则收尾；返回将要保留的有界面窗口（已定决策 28）。"""
    kept = [item for item in app.browsers.alive_instances() if item['headed']] if hasattr(app, 'browsers') else []
    _signal_self()
    return json_response(dict(ok=True, pid=os.getpid(), kept=kept))


@route('POST', '/local/restart', 'token')
def restart_daemon(app, request):
    """sop-board restart：写下交接记录，记下受影响运行的中断开始，起一个等本进程退出后启动的新守护进程，然后交接式退出。

    新守护进程由本进程启动，继承本进程的环境变量（测试账号的凭据只在环境变量里）。"""
    import subprocess
    from . import runs
    if app.handover:
        reject(409, 'restarting', 'daemon', '看板已在重启')
    at = datetime.now(timezone.utc).isoformat()
    environments = app.envs.running()
    with app.store.local() as db:
        prepared = [dict(id=r['id'], checkout=r['name'], dir=r['dir']) for r in db.execute(
            "SELECT w.id, w.dir, c.name FROM local_work w JOIN checkouts c ON c.id=w.checkout "
            "WHERE w.kind='run' AND w.status='prepared' ORDER BY w.id")]
    handed = dict(at=at, from_pid=os.getpid(), port=app.port, lan=app.lan,
                  browsers=app.browsers.alive_instances() if hasattr(app, 'browsers') else [],
                  launches=app.launcher.running() if hasattr(app, 'launcher') else [],
                  environments=environments, runs=prepared)
    daemon.write_handover(app.project, handed)
    for item in prepared:
        if item['checkout'] in environments and item['dir']:
            runs.note_interruption(item['dir'], start=at)
    successor = [sys.executable, '-m', 'sop_board.cli', 'serve', app.project.name, '--port', str(app.port),
                 '--data', str(app.project.data.parent), '--after-pid', str(os.getpid())] + (['--lan'] if app.lan else [])
    with (app.project.data / 'serve.log').open('ab') as log:
        subprocess.Popen(successor, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                         start_new_session=True, cwd=str(app.project.data))
    app.handover = True
    _signal_self()
    return json_response(dict(ok=True, pid=os.getpid(), log=str(app.project.data / 'serve.log'),
                              **{key: handed[key] for key in ('browsers', 'launches', 'environments', 'runs')}))


def restore_environments(app):
    """交接后重新拉起原来在运行的环境；就绪（或失败）后补上受影响运行的中断结束时间。"""
    from . import runs
    for name in app.handed_over.get('environments', []):
        error = None
        try:
            app.envs.up(app.checkout(name))
            app.env_restarts[name] = dict(state='ready')
        except Exception as exc:  # noqa: BLE001 - 失败写进中断记录与 restart 输出
            error = f'{type(exc).__name__}: {exc}'
            app.env_restarts[name] = dict(state='failed', error=error)
        end = datetime.now(timezone.utc).isoformat()
        for item in app.handed_over.get('runs', []):
            if item['checkout'] == name and item.get('dir'):
                runs.note_interruption(item['dir'], end=end, error=error)
        print(f"重启后重新拉起环境：检出 {name}，" + ('就绪' if error is None else '失败：' + error), flush=True)


@route('POST', '/local/env/up', 'token')
def env_up(app, request):
    checkout = _checkout_of(app, request)
    return json_response(dict(ok=True, environment=app.envs.up(checkout)))


@route('POST', '/local/env/down', 'token')
def env_down(app, request):
    checkout = _checkout_of(app, request)
    return json_response(dict(ok=True, environment=app.envs.down(checkout)))


@route('POST', '/local/env/restart', 'token')
def env_restart(app, request):
    checkout = _checkout_of(app, request)
    app.envs.down(checkout)
    return json_response(dict(ok=True, environment=app.envs.up(checkout)))


@route('POST', '/local/env/service', 'token')
def env_service(app, request):
    """env stop|start <服务>：只在这套环境上只有这一个 prepared 运行时可用，停启记进该运行（已定决策 28）。"""
    checkout = _checkout_of(app, request)
    action, name = request.json_field('action'), request.json_field('service')
    if action not in ('stop', 'start') or not isinstance(name, str) or not name:
        reject(422, 'invalid', 'service', '须给出 action（stop 或 start）与服务名')
    run = request.json_field('run')
    return json_response(app.envs.service(checkout, name, action, operator=request.json_field('operator') or '本机',
                                          run=run if type(run) is int else None))


@route('POST', '/local/env/restore', 'token')
def env_restore(app, request):
    """运行提交或放弃时恢复本轮停着的服务。"""
    checkout = _checkout_of(app, request)
    work = request.json_field('run')
    if type(work) is not int:
        reject(422, 'invalid', 'run', '须给出运行编号')
    return json_response(dict(ok=True, restored=app.envs.restore(
        checkout, work, operator=request.json_field('operator') or '本机', reason=request.json_field('reason') or '运行结束')))


@route('POST', '/local/env/status', 'token')
def env_status(app, request):
    if request.json_field('checkout'):
        return json_response(dict(ok=True, environments=[app.envs.status(_checkout_of(app, request))]))
    return json_response(dict(ok=True, environments=[app.envs.status(c) for c in app.checkouts()]))


def serve(project, *, port=8787, lan=False, after_pid=None):
    """在前台运行守护进程，直到收到 SIGINT/SIGTERM。

    after_pid：sop-board restart 由旧守护进程启动本进程时给出，先等旧进程退出，再按它的交接记录认回（已定决策 28）。"""
    if 'CODEX_SANDBOX' in os.environ:
        print('提示：在 Codex 沙箱里启动的看板无法启动浏览器，启动的会话读不到客户端的登录凭据；请在终端启动看板。', flush=True)
    from . import extensions
    if after_pid is not None and not daemon.wait_exit(after_pid, 600):
        raise ValueError(f'等了 600 秒，原来的看板守护进程（pid {after_pid}）仍未退出；没有启动新的守护进程')
    state = daemon.describe(project)
    if state['running']:
        raise ValueError(f"项目 {project.name} 的守护进程已在运行：{state['url']}（pid {state['pid']}）")
    store = Store(project)
    app = App(project, store, port=port, lan=lan)
    app.handed_over = daemon.read_handover(project, from_pid=after_pid)
    if app.handed_over:
        app.env_restarts = {name: dict(state='starting') for name in app.handed_over.get('environments', [])}
    extensions.attach(app)
    host = '0.0.0.0' if lan else '127.0.0.1'
    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    server.app = app
    actual = server.server_address[1]
    app.port = actual
    addresses = [f'http://{address}:{actual}' for address in local_ipv4_addresses()] if lan else []
    app.addresses = addresses
    app.envs.recover()
    for recover in extensions.RECOVER:
        recover(app)
    # 登记本机（带 --lan 时含内网地址与对等令牌）；业务记录库此刻不可用时，之后的页面请求再登记。
    from . import peers
    peers.ensure_registered(app)
    app.token = daemon.write_state(project, os.getpid(), actual, addresses, app.started_at)
    if app.handed_over:
        # 状态文件写好之后再删交接记录：等待重启的 CLI 看到的总是旧进程、正在重启或新进程之一。
        daemon.clear_handover(project)
        print(f"已从守护进程 {app.handed_over.get('from_pid')} 交接：重新拉起环境 "
              + ('、'.join(app.handed_over.get('environments', [])) or '（无）'), flush=True)
        import threading
        threading.Thread(target=restore_environments, args=(app,), daemon=True, name='restart-environments').start()
    def shutdown(*_):
        # serve_forever 在主线程；信号处理只抛出中断，由 finally 统一清理，避免在信号处理中等待其他线程。
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    print(f'Flow Board · 项目 {project.name}', flush=True)
    print(f'本机：http://127.0.0.1:{actual}', flush=True)
    for address in addresses:
        print(f'内网：{address}', flush=True)
    try:
        server.serve_forever(poll_interval=.5)
    except KeyboardInterrupt:
        pass
    finally:
        # 清理期间不再响应重复的停止信号，保证环境、浏览器与状态文件都被收尾。
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        for close in extensions.CLOSE:
            close(app)
        app.envs.close()
        from . import peers
        peers.withdraw(app)
        store.close()
        server.server_close()
        daemon.clear_state(project, os.getpid())
    return 0
