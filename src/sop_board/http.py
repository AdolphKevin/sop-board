"""标准库 HTTP 适配层；不承担统计与持久化规则。"""

from http.server import BaseHTTPRequestHandler
from contextlib import nullcontext
from importlib.resources import files
import json
import re
import sqlite3
from urllib.parse import quote, unquote, urlsplit, parse_qs

from .errors import APIError, reject
from .kernel import queue_view
from . import pages
from .network import is_loopback
from .evidence_types import content_type as evidence_content_type, is_text, video_response


def invalid_constant(value):
    raise ValueError("JSON 不接受非有限数字")


def same_origin(origin, host):
    """本服务使用 HTTP；比较规范化主机与端口，不信任转发头。"""
    try:
        source, target = urlsplit(origin), urlsplit('http://' + host)
        for value in (source, target):
            if (value.scheme != 'http' or not value.hostname or value.username is not None
                    or value.password is not None or value.path or value.query or value.fragment):
                return False
        source_port = source.port if source.port is not None else 80
        target_port = target.port if target.port is not None else 80
        return (source.hostname, source_port) == (target.hostname, target_port)
    except ValueError:
        return False


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def respond(self, status, raw, content_type, headers=()):
        if not urlsplit(self.path).path.startswith('/assets/'):
            from .credentials import response_bytes
            workspace = self.server.service.workspace
            local = is_loopback(self.client_address[0])
            roots = [workspace.data, *workspace.repos.values()]
            raw = response_bytes(raw, content_type, workspace.credential_names, local=local, local_roots=roots)
            # 响应头只能用 latin-1；遮蔽后的占位符改为 ASCII。
            headers = [(name, response_bytes(value.encode(), 'text/plain', workspace.credential_names,
                                            local=local, local_roots=roots).decode().replace('<本机路径>', '<local-path>'))
                       for name, value in headers]
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("X-Content-Type-Options", "nosniff")
        for name, value in headers:
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(raw)

    def respond_json(self, status, result):
        self.respond(status, json.dumps(result, ensure_ascii=False).encode(), "application/json; charset=utf-8")

    def handle_atlas(self, service, atlas_page, atlas_api, method, local, body):
        """交互地图：本机与内网只读查看；发起梳理仅限本机。"""
        import json as _json
        from .kernel import S
        match = atlas_page or atlas_api
        key = unquote(match[1])
        if key != service.workspace.name:
            reject(404, 'workspace-not-found', 'workspace', '工作区不存在')
        tasks = self.server.tasks
        if method == 'POST':
            if not (atlas_api and atlas_api[2] and atlas_api[3]):
                reject(404, 'not-found', 'path', '接口不存在')
            if not local:
                reject(403, 'local-only', 'client', '梳理交互只能在本机发起')
            entry = unquote(atlas_api[2])
            if not isinstance(body, dict) or body.keys() - {'mode'} or body.get('mode', 'full') not in ('full', 'stale'):
                reject(422, 'atlas-invalid', 'mode', '只接受 mode：full 或 stale')
            with tasks.lock:
                with tasks.store.connection() as db:
                    active = [row['id'] for row in db.execute(
                        "SELECT id,input_json FROM tasks WHERE kind='atlas' AND workspace=? AND status IN (?,?,?)",
                        (key, S.QUEUED, S.RUNNING, S.SESSION)) if _json.loads(row['input_json']).get('entry') == entry
                        and _json.loads(row['input_json']).get('mode') != 'bind']
                if active:
                    reject(409, 'task-running', 'task', f'该入口已有进行中的梳理任务 #{active[0]}')
                try:
                    inputs = tasks.atlas.inputs(entry, body.get('mode', 'full'), [], False)
                except ValueError as error:
                    reject(409, 'atlas-invalid', 'atlas', str(error))
                task_id = tasks.atlas.enqueue(inputs, '看板梳理交互')
            tasks.kernel.wake(task_id)
            self.respond_json(200, dict(ok=True, task=tasks.read(task_id, local=local)))
            return
        if method != 'GET':
            reject(405, 'method-not-allowed', 'method', '只支持 GET')
        service.workspace.reload()
        summary = tasks.atlas.summary(local=local)
        if atlas_api and not atlas_api[2]:
            self.respond_json(200, dict(ok=True, entries=summary))
            return
        if atlas_api:
            entry = unquote(atlas_api[2])
            item = next((s for s in summary if s['entry'] == entry), None)
            if item is None or item['version'] is None:
                reject(404, 'atlas-not-found', 'entry', f'入口 {entry} 尚无交互地图')
            self.respond_json(200, dict(ok=True, entry=item, atlas=tasks.atlas.load(entry, item['version'])))
            return
        if atlas_page[2] is None:
            documents = {s['entry']: tasks.atlas.load(s['entry'], s['version']) for s in summary if s['version']}
            self.respond(200, pages.atlas(key, summary, documents, local=local).encode(), 'text/html; charset=utf-8')
            return
        entry, page_id = unquote(atlas_page[2]), unquote(atlas_page[3])
        item = next((s for s in summary if s['entry'] == entry), None)
        if item is None or item['version'] is None:
            reject(404, 'atlas-not-found', 'entry', f'入口 {entry} 尚无交互地图')
        document = tasks.atlas.load(entry, item['version'])
        page = next((p for p in document['pages'] if p['id'] == page_id), None)
        if page is None:
            reject(404, 'atlas-page-not-found', 'page', f'地图中没有页面 {page_id}')
        stale = next((s['files'] for s in item['stale'] if s['page'] == page_id), [])
        self.respond(200, pages.atlas_page(key, entry, document, page, stale, local=local).encode(), 'text/html; charset=utf-8')

    def handle_api(self):
        # 只读请求的页面、变化标记和任务列表共享一次排队计算。
        # 写入请求不沿用操作前的队列，返回值仍反映操作后的状态。
        with (queue_view() if self.command == 'GET' else nullcontext()):
            self._handle_api()

    def _handle_api(self):
        try:
            service = self.server.service
            path = urlsplit(self.path).path
            method = self.command
            local = is_loopback(self.client_address[0])
            fixtures_page = re.fullmatch(r'/w/([^/]+)/fixtures', path)
            fixtures_api = re.fullmatch(r'/api/v1/workspaces/([^/]+)/fixtures(?:/([0-9]+)/mark)?', path)
            if (fixtures_page or fixtures_api) and not local:
                reject(403, 'local-only', 'client', '数据配方仅限本机')
            assets = {"/assets/board.css": ("board.css", "text/css; charset=utf-8"),
                      "/assets/board.js": ("board.js", "text/javascript; charset=utf-8"),
                      "/assets/sop.js": ("sop.js", "text/javascript; charset=utf-8"),
                      "/assets/vendor/mermaid/mermaid.min.js": ("vendor/mermaid/mermaid.min.js", "text/javascript; charset=utf-8")}
            if method == "GET" and path in assets:
                name, content_type = assets[path]
                self.respond(200, files("sop_board").joinpath("assets", name).read_bytes(), content_type)
                return
            if method in ("POST", "PUT"):
                developer = bool(re.fullmatch(r"/api/v1/tasks/[^/]+/(retry|cancel|approve|handled|abandon|cleanup)", path) or
                                 re.fullmatch(r"/api/v1/automation/(pause|resume|auto-execute)", path) or
                                 re.fullmatch(r"/api/v1/environment/(check|restart)", path) or
                                 re.fullmatch(r"/api/v1/workspaces/[^/]+/fixes/handoff", path) or
                                 re.fullmatch(r"/api/v1/workspaces/[^/]+/issues/[0-9]+/board-fix", path) or
                                 re.fullmatch(r"/api/v1/workspaces/[^/]+/batches/[^/]+/(merges|executions|distill)", path) or
                                 re.fullmatch(r"/api/v1/workspaces/[^/]+/batches/[^/]+/cases/[^/]+/flow", path))
                if developer and not local:
                    reject(403, "local-only", "client", "此操作仅限本机")
                origin = self.headers.get("Origin")
                if origin is not None and not same_origin(origin, self.headers.get("Host", "")):
                    reject(403, "cross-origin", "Origin", "写入请求必须与看板同源")
                binary_upload = path == "/api/v1/evidence" and method == "POST"
                if not binary_upload and self.headers.get_content_type() != "application/json":
                    reject(415, "json-required", "Content-Type", "请求正文必须为 application/json")
                try:
                    length = int(self.headers.get("Content-Length", "0" if developer else ""))
                    if length < 0:
                        raise ValueError()
                except ValueError:
                    reject(400, "invalid-request", "Content-Length", "应为非负字节数")
                if path == "/api/v1/evidence" and method == "POST" and length > 64 * 1024 * 1024:
                    reject(413, "evidence-too-large", "content", "单个证据不能超过 64 MiB")
                raw = self.rfile.read(length)
                if len(raw) != length:
                    reject(400, "invalid-request", "content", "请求正文不完整")
                if path == "/api/v1/evidence" and method == "POST":
                    result = service.upload(raw, unquote(self.headers.get("X-Evidence-Name", "evidence.bin")))
                    self.respond_json(200, result)
                    return
                try:
                    body = {} if developer and not raw else json.loads(raw.decode("utf-8"), parse_constant=invalid_constant)
                except UnicodeError:
                    if re.fullmatch(r'/api/v1/workspaces/[^/]+/batches/[^/]+/sop', path):
                        reject(422, 'sop-invalid', 'content', 'SOP 原文必须为 UTF-8')
                    if re.fullmatch(r'/api/v1/workspaces/[^/]+/batches/[^/]+/cases', path):
                        reject(422, 'cases-invalid', 'content', '用例文件必须为 UTF-8')
                    reject(400, "invalid-request", "body", "应为 UTF-8 JSON")
                except ValueError:
                    reject(400, "invalid-request", "body", "应为 UTF-8 JSON")
            if method == "GET" and path == "/":
                self.respond(200, pages.home(service.workspaces(local=local), environment=service.environment_status(local=local), local=local).encode(), "text/html; charset=utf-8")
                return
            if fixtures_page or fixtures_api:
                from .fixtures import Fixtures
                key = unquote((fixtures_page or fixtures_api)[1])
                if key != service.workspace.name:
                    reject(404, 'workspace-not-found', 'workspace', '工作区不存在')
                fixtures = Fixtures(service, self.server.tasks)
                try:
                    with self.server.tasks.lock:
                        if method == 'GET' and (fixtures_page or not fixtures_api[2]):
                            result = fixtures.read()
                        elif method == 'POST' and fixtures_api and fixtures_api[2]:
                            if not isinstance(body, dict) or body.keys() - {'promoted', 'dismiss', 'by'} or type(body.get('dismiss', False)) is not bool:
                                raise ValueError('只接受 promoted、dismiss 和标记人 by')
                            result = fixtures.mark(int(fixtures_api[2]), **body)
                        else:
                            reject(404, 'not-found', 'path', '接口不存在')
                except ValueError as error:
                    reject(422, 'fixtures-invalid', 'fixtures', str(error))
                if fixtures_page:
                    self.respond(200, pages.fixtures(key, result).encode(), 'text/html; charset=utf-8')
                else:
                    self.respond_json(200, result)
                return
            if method == 'POST' and (match := re.fullmatch(r'/api/v1/workspaces/([^/]+)/batches/([^/]+)/atlas-checks/([0-9]+)/decision', path)):
                key, name = unquote(match[1]), unquote(match[2])
                if key != service.workspace.name:
                    reject(404, 'workspace-not-found', 'workspace', '工作区不存在')
                if not isinstance(body, dict) or body.keys() - {'decision', 'by', 'note'} or not isinstance(body.get('note', ''), str):
                    reject(422, 'atlas-decision-invalid', 'body', '只接受 decision、by 与 note')
                tasks = self.server.tasks
                with tasks.lock:
                    with tasks.store.connection() as db:
                        batch = service.batch(db, key, name)
                        row = db.execute('SELECT scenario,item FROM atlas_checks WHERE id=? AND batch=?', (int(match[3]), batch['id'])).fetchone()
                    if row is None:
                        reject(404, 'atlas-check-not-found', 'item', '对照条目不存在')
                    try:
                        checks = tasks.atlas.decide(name, f"{row['scenario']}#{row['item']}", body.get('decision'), body.get('by'), body.get('note', ''))
                    except ValueError as error:
                        reject(422, 'atlas-decision-invalid', 'decision', str(error))
                self.respond_json(200, dict(ok=True, atlas_checks=checks))
                return
            if method == 'POST' and (match := re.fullmatch(r'/api/v1/workspaces/([^/]+)/batches/([^/]+)/atlas-bind', path)):
                if not local:
                    reject(403, 'local-only', 'client', '对照界面只能在本机发起')
                key, name = unquote(match[1]), unquote(match[2])
                if key != service.workspace.name:
                    reject(404, 'workspace-not-found', 'workspace', '工作区不存在')
                if not isinstance(body, dict) or body.keys() - {'stale'} or type(body.get('stale', False)) is not bool:
                    reject(422, 'atlas-bind-invalid', 'body', '只接受布尔值 stale')
                tasks = self.server.tasks
                from .kernel import S
                with tasks.lock:
                    with tasks.store.connection() as db:
                        active = db.execute("SELECT id FROM tasks WHERE kind='atlas' AND workspace=? AND batch=? AND status IN (?,?,?)",
                                            (key, name, S.QUEUED, S.RUNNING, S.SESSION)).fetchone()
                    if active:
                        reject(409, 'task-running', 'task', f"该批次已有进行中的对照任务 #{active['id']}")
                    try:
                        task_id = tasks.atlas.enqueue_bind(tasks.atlas.bind_inputs(name, None, body.get('stale', False)), '看板对照界面')
                    except ValueError as error:
                        reject(409, 'atlas-bind-invalid', 'atlas', str(error))
                tasks.kernel.wake(task_id)
                self.respond_json(200, dict(ok=True, task=tasks.read(task_id, local=local)))
                return
            run_route = re.fullmatch(r'/api/v1/workspaces/([^/]+)/batches/([^/]+)/(run|plan-executions)', path)
            resolve_route = re.fullmatch(r'/api/v1/workspaces/([^/]+)/attention/([0-9]+)/resolve', path)
            if method == 'POST' and (run_route or resolve_route):
                if not local:
                    reject(403, 'local-only', 'client', '连续运行、按计划执行与处理事项只能在本机操作')
                key = unquote((run_route or resolve_route)[1])
                if key != service.workspace.name:
                    reject(404, 'workspace-not-found', 'workspace', '工作区不存在')
                tasks = self.server.tasks
                if not isinstance(body, dict):
                    reject(422, 'invalid-request', 'body', '应为 JSON 对象')
                try:
                    with tasks.lock:
                        if resolve_route:
                            from .attention import resolve
                            from .atlas import person
                            with tasks.store.connection() as db:
                                by = person(body.get('by', ''), db, key)
                            resolve(tasks.store, key, int(resolve_route[2]), by, body.get('note', ''))
                            result = dict(ok=True)
                        elif run_route[3] == 'run':
                            from .atlas import person
                            with tasks.store.connection() as db:
                                by = person(body.get('by', ''), db, key)
                            result = dict(ok=True, run=tasks.runs.control(unquote(run_route[2]), body.get('action', ''), mode=body.get('mode', 'pending'),
                                                                          round_size=body.get('round_size'), by=by))
                        else:
                            from .plans import build, DEFAULTS
                            name = unquote(run_route[2])
                            size = body.get('round_size') or (service.workspace.run or {}).get('round_size', DEFAULTS['round_size'])
                            with tasks.store.connection() as db:
                                from .attention import open_scenarios
                                plan = build(db, service.batch(db, key, name), body.get('mode', 'pending'), size, skip=open_scenarios(db, key, name))
                            if not plan['rounds']:
                                reject(409, 'nothing-to-execute', 'scenarios', '计划中没有待执行的场景')
                            result = tasks.executions.enqueue(key, name, trigger='按计划执行', selected=[i['scenario'] for i in plan['rounds'][0]])
                except ValueError as error:
                    reject(422, 'run-invalid', 'run', str(error))
                self.respond_json(200, result)
                return
            atlas_page = re.fullmatch(r'/w/([^/]+)/atlas(?:/([^/]+)/([^/]+))?', path)
            atlas_api = re.fullmatch(r'/api/v1/workspaces/([^/]+)/atlas(?:/([^/]+)(/tasks)?)?', path)
            if atlas_page or atlas_api:
                self.handle_atlas(service, atlas_page, atlas_api, method, local, body if method == 'POST' else None)
                return
            if method == 'GET' and (match := re.fullmatch(r'/w/([^/]+)/tasks/([0-9]+)', path)):
                key, task_id = unquote(match[1]), int(match[2])
                task = self.server.tasks.read(task_id, local=local)
                if task['workspace'] != key:
                    reject(404, 'task-not-found', 'task', '任务不存在')
                case_data = service.read_cases(key, task['batch']) if task['kind'] == 'merge' else None
                if task['kind'] == 'merge':
                    task['progress'] = self.server.tasks.merge_progress(task, local=local)
                actions = self.server.tasks.types[task['kind']].actions(task) if local else set()
                self.respond(200, pages.task_page(key, task, actions=actions, case_data=case_data, local=local).encode(), 'text/html; charset=utf-8')
                return
            if method == 'GET' and (match := re.fullmatch(r'/w/([^/]+)/fixes', path)):
                key = unquote(match[1])
                selected = parse_qs(urlsplit(self.path).query).get('task', [None])[0]
                data = self.server.tasks.fixes.list(key, local=local)
                self.respond(200, pages.fixes(key, data['tasks'], selected, review=data['review'], local=local,
                    session_failures=data['session_failures']).encode(), 'text/html; charset=utf-8')
                return
            if method == "GET" and (match := re.fullmatch(r"/w/([^/]+)/b/([^/]+)", path)):
                key, batch = unquote(match[1]), unquote(match[2])
                self.respond(200, pages.batch(key, service.read_batch(key, batch, local=local), environment=service.environment_status(local=local),
                    local=local, merge=self.server.tasks.latest_merge(key, batch)).encode(), "text/html; charset=utf-8")
                return
            if method == "GET" and (match := re.fullmatch(r"/w/([^/]+)/b/([^/]+)/scenarios", path)):
                key, batch = unquote(match[1]), unquote(match[2])
                self.respond(200, pages.scenarios(key, batch, service.read_batch(key, batch, scenarios=True, local=local),
                    local=local, merge=self.server.tasks.latest_merge(key, batch)).encode(), "text/html; charset=utf-8")
                return
            if method == "GET" and (match := re.fullmatch(r"/w/([^/]+)/b/([^/]+)/cases", path)):
                key, batch = unquote(match[1]), unquote(match[2])
                data = service.read_cases(key, batch, include_state=True)
                self.respond(200, pages.cases(key, batch, data, merge=self.server.tasks.latest_merge(key, batch)).encode(), "text/html; charset=utf-8")
                return
            if method == "GET" and (match := re.fullmatch(r"/w/([^/]+)/b/([^/]+)/draft", path)):
                key, batch = unquote(match[1]), unquote(match[2])
                selected = parse_qs(urlsplit(self.path).query).get('version', [None])[0]
                data = self.server.tasks.generations.read(key, batch, local=local)
                self.respond(200, pages.draft(key, batch, data, selected, local=local).encode(), "text/html; charset=utf-8")
                return
            if method == "GET" and (match := re.fullmatch(r"/w/([^/]+)/b/([^/]+)/merges", path)):
                key, batch = unquote(match[1]), unquote(match[2])
                data = service.read_batch(key, batch, local=local)
                selected = parse_qs(urlsplit(self.path).query).get('task', [None])[0]
                tasks = self.server.tasks.list(key, batch, local=local, kind='merge')['tasks']
                for task in tasks:
                    if task['kind'] == 'merge':
                        task['progress'] = self.server.tasks.merge_progress(task, local=local)
                self.respond(200, pages.merges(key, batch, tasks, service.read_cases(key, batch), selected,
                    local=local, etag=data['etag'], conversation=self.server.tasks.conversation.read(key, batch)).encode(), "text/html; charset=utf-8")
                return
            if method == "GET" and (match := re.fullmatch(r"/w/([^/]+)/b/([^/]+)/sop", path)):
                key, batch = unquote(match[1]), unquote(match[2])
                policy = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self' data:; object-src 'none'; base-uri 'none'; frame-src 'none'"
                query = {key: values[0] for key, values in parse_qs(urlsplit(self.path).query).items()}
                self.respond(200, pages.sop(key, batch, service.read_sop(key, batch), query).encode(), "text/html; charset=utf-8",
                             [('Content-Security-Policy', policy)])
                return
            if method == "GET" and (match := re.fullmatch(r"/w/([^/]+)/b/([^/]+)/progress", path)):
                key, batch = unquote(match[1]), unquote(match[2])
                data = service.read_batch(key, batch, local=local)
                coverage = service.read_cases(key, batch)["coverage"]
                self.respond(200, pages.progress(key, data, coverage).encode(), "text/html; charset=utf-8")
                return
            if method == "GET" and (match := re.fullmatch(r"/w/([^/]+)/b/([^/]+)/s/([^/]+)", path)):
                key, batch, scenario = (unquote(match[i]) for i in (1, 2, 3))
                self.respond(200, pages.scenario(service.read_scenario(key, batch, scenario, local=local), local=local).encode(), "text/html; charset=utf-8")
                return
            if method == 'GET' and path == '/api/v1/environment':
                result = dict(ok=True, **service.environment_status(local=local))
            elif method == 'POST' and path in ('/api/v1/environment/check', '/api/v1/environment/restart'):
                try:
                    if path.endswith('/check'):
                        service.environment.start_check()
                    else:
                        service.environment.restart()
                except ValueError as error:
                    reject(409, 'environment-unavailable', 'environment', str(error))
                result = dict(ok=True, **service.environment_status(local=local))
            elif method == 'GET' and path == '/api/v1/guide':
                self.respond(200, files('sop_board').joinpath('guide.md').read_bytes(), 'text/plain; charset=utf-8')
                return
            elif method == "POST" and (match := re.fullmatch(r"/api/v1/automation/(pause|resume)", path)):
                result = service.set_automation_paused(match[1] == 'pause')
            elif method == 'POST' and path == '/api/v1/automation/auto-execute':
                result = service.set_auto_execute(body)
            elif method == "GET" and path == "/api/v1/tasks":
                query = parse_qs(urlsplit(self.path).query)
                result = self.server.tasks.list(query.get('workspace', [None])[0], query.get('batch', [None])[0], local=local)
            elif method == "GET" and (match := re.fullmatch(r"/api/v1/tasks/([0-9]+)", path)):
                result = dict(ok=True, task=self.server.tasks.read(int(match[1]), local=local))
            elif method == "GET" and (match := re.fullmatch(r"/api/v1/tasks/([0-9]+)/files/(.+)", path)):
                raw = self.server.tasks.file(int(match[1]), unquote(match[2]), local=local)
                self.respond(200, raw, "application/octet-stream", [("Content-Security-Policy", "sandbox; default-src 'none'")])
                return
            elif method == "GET" and (match := re.fullmatch(r"/api/v1/workspaces/([^/]+)/batches/([^/]+)/merges/([0-9]+)/(scenario-file|pairs\.json)", path)):
                raw = self.server.tasks.merge_result(unquote(match[1]), unquote(match[2]), int(match[3]), local=local)
                if match[4] == 'pairs.json':
                    raw = self.server.tasks.file(int(match[3]), 'pairs.json', local=local)
                self.respond(200, raw, 'application/json; charset=utf-8' if match[4] == 'pairs.json' else 'text/yaml; charset=utf-8')
                return
            elif method == 'GET' and (match := re.fullmatch(r'/api/v1/workspaces/([^/]+)/batches/([^/]+)/merge-conversation', path)):
                result = self.server.tasks.conversation.read(unquote(match[1]), unquote(match[2]))
            elif method == 'POST' and (match := re.fullmatch(r'/api/v1/workspaces/([^/]+)/batches/([^/]+)/merge-conversation/(messages|exclusions)', path)):
                operation = self.server.tasks.conversation.submit if match[3] == 'messages' else self.server.tasks.conversation.exclude
                result = operation(unquote(match[1]), unquote(match[2]), body)
            elif method == 'POST' and (match := re.fullmatch(r'/api/v1/workspaces/([^/]+)/batches/([^/]+)/cases/([^/]+)/include', path)):
                result = self.server.tasks.conversation.exclude(unquote(match[1]), unquote(match[2]), body, include=unquote(match[3]))
            elif method == "POST" and (match := re.fullmatch(r"/api/v1/workspaces/([^/]+)/batches/([^/]+)/cases/([^/]+)/flow", path)):
                result = self.server.tasks.enqueue_merge(unquote(match[1]), unquote(match[2]), trigger='开发者指定独立场景', force_case=unquote(match[3]))
            elif method == "POST" and (match := re.fullmatch(r"/api/v1/workspaces/([^/]+)/batches/([^/]+)/merges", path)):
                result = self.server.tasks.enqueue_merge(unquote(match[1]), unquote(match[2]))
            elif method == 'POST' and (match := re.fullmatch(r"/api/v1/workspaces/([^/]+)/batches/([^/]+)/draft/annotations/([0-9]+)/reject", path)):
                result = self.server.tasks.generations.reject_annotation(unquote(match[1]), unquote(match[2]), int(match[3]), body)
            elif match := re.fullmatch(r"/api/v1/workspaces/([^/]+)/batches/([^/]+)/draft(?:/(annotations|revise|confirm))?", path):
                key, batch, action = unquote(match[1]), unquote(match[2]), match[3]
                if method == 'GET' and action is None:
                    result = self.server.tasks.generations.read(key, batch, local=local)
                elif method == 'POST' and action == 'annotations':
                    result = self.server.tasks.generations.annotate(key, batch, body)
                elif method == 'POST' and action == 'revise':
                    result = self.server.tasks.enqueue_generate(key, batch)
                elif method == 'POST' and action == 'confirm':
                    result = self.server.tasks.generations.confirm(key, batch, body)
                else:
                    reject(404, 'not-found', 'path', '接口不存在')
            elif method == "POST" and (match := re.fullmatch(r"/api/v1/workspaces/([^/]+)/batches/([^/]+)/executions", path)):
                if not isinstance(body, dict) or body.keys() - {'scope'}:
                    reject(422, 'invalid-scope', 'body', '执行请求只接受 scope 字段')
                result = self.server.tasks.enqueue_execution(unquote(match[1]), unquote(match[2]), body.get('scope', 'pending'))
            elif method == 'POST' and (match := re.fullmatch(r'/api/v1/workspaces/([^/]+)/batches/([^/]+)/distill', path)):
                result = self.server.tasks.distill.enqueue(unquote(match[1]), unquote(match[2]), body)
            elif method == "POST" and (match := re.fullmatch(r"/api/v1/tasks/([0-9]+)/(retry|cancel)", path)):
                result = self.server.tasks.action(int(match[1]), match[2], body)
            elif method == 'POST' and (match := re.fullmatch(r'/api/v1/tasks/([0-9]+)/(handled|abandon|cleanup)', path)):
                result = self.server.tasks.distill.action(self.server.tasks.read(int(match[1])), match[2], body)
            elif method == "POST" and (match := re.fullmatch(r"/api/v1/tasks/([0-9]+)/approve", path)):
                result = self.server.tasks.fixes.action(self.server.tasks.read(int(match[1])), 'approve', body.get('note', '')) if isinstance(body, dict) else self.server.tasks.fixes.action(self.server.tasks.read(int(match[1])), 'approve', None)
            elif method == "GET" and path == "/api/v1/workspaces":
                result = service.workspaces(local=local)
            elif method == "GET" and (match := re.fullmatch(r"/api/v1/workspaces/([^/]+)/entries", path)):
                result = service.entries(unquote(match[1]), local=local)
            elif method == 'GET' and (match := re.fullmatch(r'/api/v1/workspaces/([^/]+)/batches/([^/]+)/scenarios/([^/]+)', path)):
                result = service.read_scenario(*(unquote(match[i]) for i in (1, 2, 3)), local=local)
            elif method == "GET" and (match := re.fullmatch(r"/api/v1/workspaces/([^/]+)/fixes", path)):
                result = self.server.tasks.fixes.list(unquote(match[1]), local=local)
            elif method == 'POST' and (match := re.fullmatch(r'/api/v1/workspaces/([^/]+)/fixes/handoff', path)):
                result = self.server.tasks.fixes.handoff(unquote(match[1]),body)
            elif method == 'POST' and (match := re.fullmatch(r'/api/v1/workspaces/([^/]+)/issues/([0-9]+)/board-fix', path)):
                result = self.server.tasks.fixes.handoff_issue(unquote(match[1]), int(match[2]))
            elif method == "GET" and (match := re.fullmatch(r"/api/v1/workspaces/([^/]+)/attention", path)):
                result = self.server.tasks.fixes.attention(unquote(match[1]), 'http://' + self.headers.get('Host', 'localhost'), local=local)
            elif method == "GET" and path == "/api/v1/jira/summary":
                result = self.server.jira.summary(parse_qs(urlsplit(self.path).query).get('url', [''])[0])
            elif method == "GET" and (match := re.fullmatch(r"/api/v1/workspaces/([^/]+)/batches/([^/]+)/rounds/([0-9]+)/export", path)):
                key, batch, number = unquote(match[1]), unquote(match[2]), int(match[3])
                self.respond(200, service.export_round(key, batch, number), "application/zip",
                             [("Content-Disposition", f"attachment; filename=round-{number}.zip")])
                return
            elif method == "GET" and (match := re.fullmatch(r"/api/v1/evidence/([^/]+)", path)):
                raw, name = service.evidence(unquote(match[1]))
                media = evidence_content_type(name)
                if is_text(media):
                    media += "; charset=utf-8"
                status, extra = 200, []
                if media.startswith("video/"):
                    status, raw, extra = video_response(raw, self.headers.get("Range"))
                self.respond(status, raw, media,
                             [("Content-Disposition", "inline; filename*=UTF-8''" + quote(name, safe="")),
                              ("Content-Security-Policy", "sandbox; default-src 'none'"), *extra])
                return
            elif match := re.fullmatch(r"/api/v1/workspaces/([^/]+)/batches/([^/]+)(?:/(scenario-file|scenarios|rounds|marks|review-complete|cases|sop|cases\.yaml|state|report\.md|results\.yaml|results)(?:/([0-9]+))?)?", path):
                key, batch, action, number = unquote(match[1]), unquote(match[2]), match[3], match[4]
                if method == "PUT" and action == "scenario-file" and number is None:
                    result = service.push(key, batch, body)
                elif method == "POST" and action == "rounds" and number is None:
                    result = service.round(key, batch, body)
                elif method == "POST" and action == "marks" and number is None:
                    result = service.mark(key, batch, body)
                elif method == "POST" and action == "review-complete" and number is None:
                    result = service.review_complete(key, batch, body)
                elif method == "POST" and action == "cases" and number is None:
                    result = service.upload_cases(key, batch, body)
                elif method == "POST" and action == "sop" and number is None:
                    result = service.upload_sop(key, batch, body)
                elif method == "POST" and action == "results" and number is None:
                    result = service.import_results(key, batch, body)
                elif method == "GET" and action == "cases" and number is None:
                    result = service.read_cases(key, batch)
                elif method == "GET" and action == "report.md" and number is None:
                    self.respond(200, service.report(key, batch, local=local), "text/markdown; charset=utf-8",
                                 [("Content-Disposition", "attachment; filename=report.md")])
                    return
                elif method == "GET" and action == "results.yaml" and number is None:
                    self.respond(200, service.results_yaml(key, batch), "text/yaml; charset=utf-8",
                                 [("Content-Disposition", "attachment; filename=results.yaml")])
                    return
                elif method == "GET" and action == "state" and number is None:
                    result = service.state(key, batch)
                    etag = result["etag"]
                    matches = [value.strip().removeprefix("W/") for value in self.headers.get("If-None-Match", "").split(",")]
                    raw = json.dumps(result, ensure_ascii=False).encode()
                    self.respond(304 if etag in matches or "*" in matches else 200,
                                 b"" if etag in matches or "*" in matches else raw,
                                 "application/json; charset=utf-8", [("ETag", etag), ("Cache-Control", "no-cache")])
                    return
                elif method == "GET" and action in ("cases.yaml", "scenario-file") and number is None:
                    raw = service.cases_yaml(key, batch) if action == "cases.yaml" else service.scenario_file(key, batch)
                    self.respond(200, raw, "text/yaml; charset=utf-8",
                                 [("Content-Disposition", f"attachment; filename={action if action == 'cases.yaml' else 'scenarios.yaml'}")])
                    return
                elif method == "GET" and action in (None, "scenarios") and number is None:
                    result = service.read_batch(key, batch, scenarios=action == "scenarios", local=local)
                elif method == "GET" and action == "rounds" and number is not None:
                    result = service.read_round(key, batch, int(number), local=local)
                else:
                    reject(404, "not-found", "path", "接口不存在")
            else:
                reject(404, "not-found", "path", "接口不存在")
            self.respond_json(200, result)
        except APIError as error:
            self.respond_json(error.status, dict(ok=False, error_code=error.code, problems=error.problems))
        except (sqlite3.Error, OSError):
            self.respond_json(500, dict(ok=False, error_code="storage-error", problems=[dict(field="storage", problem="存储失败，请检查数据目录与数据库")]))

    do_GET = handle_api
    do_PUT = handle_api
    do_POST = handle_api

    def log_message(self, format, *args):
        # Do not log request bodies or request targets.
        pass
