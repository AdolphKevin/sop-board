"""看板守护进程：网页、按检出的环境、浏览器代理与本机无头会话。

访问级别：
- lan：内网可用（查看、人工复测、答复、批注、上传）；写入须同源且为 JSON。
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
import threading
from urllib.parse import parse_qs, unquote, urlsplit

from . import checkouts, config, daemon
from .errors import APIError, reject
from .network import is_loopback, local_ipv4_addresses
from .redaction import names, response_bytes
from .store import Store

ROUTES = []
ASSETS = {'board.css': 'text/css; charset=utf-8', 'board.js': 'text/javascript; charset=utf-8',
          'sop.js': 'text/javascript; charset=utf-8', 'vendor/mermaid/mermaid.min.js': 'text/javascript; charset=utf-8'}
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
    def __init__(self, status, raw, content_type, headers=()):
        self.status, self.raw, self.content_type, self.headers = status, raw, content_type, list(headers)


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
        self.envs = envs.Manager(project, store)
        self.token = None
        self.config_stamp = None
        self.started_at = datetime.now(timezone.utc).isoformat()
        self.extensions = {}
        self.reload()

    def reload(self):
        """config.yaml 变化后重新读取；配置不合格时保留问题供页面显示。"""
        try:
            stamp = self.project.config_path.stat().st_mtime_ns
        except OSError:
            stamp = None
        if stamp != self.config_stamp:
            self.config_stamp = stamp
            self.project.reload()
        return self.project

    def checkout(self, name=None):
        self.reload()
        with self.store.transaction() as db:
            main = checkouts.ensure_main(db, self.project)
            return checkouts.by_name(db, name) if name else main

    def checkouts(self):
        self.reload()
        with self.store.transaction() as db:
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
        if not request.local and not request.path.startswith('/assets/'):
            app = self.server.app
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
            self.send(json_response(dict(ok=False, error_code=error.code, problems=error.problems), error.status), request)
        except (ValueError, OSError) as error:
            request = request or Request(self, None)
            code = 'env-busy' if type(error).__name__ == 'EnvBusy' else 'browser-busy' if type(error).__name__ == 'BrowserBusy' else 'invalid'
            status = 409 if code != 'invalid' else 422
            body = dict(ok=False, error_code=code, problems=[dict(field='request', problem=str(error))])
            owner = getattr(error, 'owner', None)
            if owner and request.local:
                body['owner'] = owner
            self.send(json_response(body, status), request)

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


# ---- 本机令牌接口：环境 ----

def _checkout_of(app, request):
    return app.checkout(request.json_field('checkout') or request.query.get('checkout'))


@route('GET', '/local/health', 'token')
def health(app, request):
    return json_response(dict(ok=True, project=app.project.name, pid=os.getpid(), started_at=app.started_at))


@route('POST', '/local/env/up', 'token')
def env_up(app, request):
    checkout = _checkout_of(app, request)
    return json_response(dict(ok=True, environment=app.envs.up(checkout)))


@route('POST', '/local/env/down', 'token')
def env_down(app, request):
    checkout = _checkout_of(app, request)
    return json_response(dict(ok=True, environment=app.envs.down(checkout)))


@route('POST', '/local/env/status', 'token')
def env_status(app, request):
    if request.json_field('checkout'):
        return json_response(dict(ok=True, environments=[app.envs.status(_checkout_of(app, request))]))
    return json_response(dict(ok=True, environments=[app.envs.status(c) for c in app.checkouts()]))


def serve(project, *, port=8787, lan=False):
    """在前台运行守护进程，直到收到 SIGINT/SIGTERM。"""
    from . import extensions
    state = daemon.describe(project)
    if state['running']:
        raise ValueError(f"项目 {project.name} 的守护进程已在运行：{state['url']}（pid {state['pid']}）")
    store = Store(project.data)
    app = App(project, store, port=port, lan=lan)
    extensions.attach(app)
    host = '0.0.0.0' if lan else '127.0.0.1'
    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    server.app = app
    actual = server.server_address[1]
    addresses = [f'http://{address}:{actual}' for address in local_ipv4_addresses()] if lan else []
    app.envs.recover()
    for recover in extensions.RECOVER:
        recover(app)
    app.token = daemon.write_state(project, os.getpid(), actual, addresses, app.started_at)
    stop = threading.Event()

    def shutdown(*_):
        if not stop.is_set():
            stop.set()
            threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    print(f'Flow Board · 项目 {project.name}', flush=True)
    print(f'本机：http://127.0.0.1:{actual}', flush=True)
    for address in addresses:
        print(f'内网：{address}', flush=True)
    try:
        server.serve_forever(poll_interval=.5)
    finally:
        for close in extensions.CLOSE:
            close(app)
        app.envs.close()
        server.server_close()
        daemon.clear_state(project, os.getpid())
    return 0
