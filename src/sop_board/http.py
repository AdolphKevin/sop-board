"""标准库 HTTP 适配层；不承担统计与持久化规则。"""

from http.server import BaseHTTPRequestHandler
from importlib.resources import files
import json
import re
import sqlite3
from urllib.parse import quote, unquote, urlsplit, parse_qs

from .errors import APIError, reject
from . import pages
from .network import is_loopback
from .evidence_types import content_type as evidence_content_type, is_text


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

    def handle_api(self):
        try:
            service = self.server.service
            path = urlsplit(self.path).path
            method = self.command
            local = is_loopback(self.client_address[0])
            assets = {"/assets/board.css": ("board.css", "text/css; charset=utf-8"),
                      "/assets/board.js": ("board.js", "text/javascript; charset=utf-8"),
                      "/assets/sop.js": ("sop.js", "text/javascript; charset=utf-8"),
                      "/assets/vendor/mermaid/mermaid.min.js": ("vendor/mermaid/mermaid.min.js", "text/javascript; charset=utf-8")}
            if method == "GET" and path in assets:
                name, content_type = assets[path]
                self.respond(200, files("sop_board").joinpath("assets", name).read_bytes(), content_type)
                return
            if method in ("POST", "PUT"):
                developer = bool(re.fullmatch(r"/api/v1/tasks/[^/]+/(retry|cancel|approve)", path) or
                                 re.fullmatch(r"/api/v1/automation/(pause|resume)", path) or
                                 re.fullmatch(r"/api/v1/environment/(check|restart)", path) or
                                 re.fullmatch(r"/api/v1/workspaces/[^/]+/batches/[^/]+/(merges|executions)", path) or
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
                except (ValueError, UnicodeError):
                    reject(400, "invalid-request", "body", "应为 UTF-8 JSON")
            if method == "GET" and path == "/":
                self.respond(200, pages.home(service.workspaces(), environment=service.environment.public(local), local=local).encode(), "text/html; charset=utf-8")
                return
            if method == 'GET' and (match := re.fullmatch(r'/w/([^/]+)/fixes', path)):
                key = unquote(match[1])
                selected = parse_qs(urlsplit(self.path).query).get('task', [None])[0]
                self.respond(200, pages.fixes(key, self.server.tasks.fixes.list(key)['tasks'], selected, local=local).encode(), 'text/html; charset=utf-8')
                return
            if method == "GET" and (match := re.fullmatch(r"/w/([^/]+)/b/([^/]+)", path)):
                key, batch = unquote(match[1]), unquote(match[2])
                self.respond(200, pages.batch(key, service.read_batch(key, batch), environment=service.environment.public(local), local=local).encode(), "text/html; charset=utf-8")
                return
            if method == "GET" and (match := re.fullmatch(r"/w/([^/]+)/b/([^/]+)/scenarios", path)):
                key, batch = unquote(match[1]), unquote(match[2])
                self.respond(200, pages.scenarios(key, batch, service.read_batch(key, batch, scenarios=True), service.jira_url).encode(), "text/html; charset=utf-8")
                return
            if method == "GET" and (match := re.fullmatch(r"/w/([^/]+)/b/([^/]+)/cases", path)):
                key, batch = unquote(match[1]), unquote(match[2])
                data = service.read_cases(key, batch, include_state=True)
                self.respond(200, pages.cases(key, batch, data).encode(), "text/html; charset=utf-8")
                return
            if method == "GET" and (match := re.fullmatch(r"/w/([^/]+)/b/([^/]+)/merges", path)):
                key, batch = unquote(match[1]), unquote(match[2])
                service.read_batch(key, batch)
                selected = parse_qs(urlsplit(self.path).query).get('task', [None])[0]
                self.respond(200, pages.merges(key, batch, self.server.tasks.list(key, batch)['tasks'], service.read_cases(key, batch), selected, local=local).encode(), "text/html; charset=utf-8")
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
                data = service.read_batch(key, batch)
                coverage = service.read_cases(key, batch)["coverage"]
                self.respond(200, pages.progress(key, data, coverage).encode(), "text/html; charset=utf-8")
                return
            if method == "GET" and (match := re.fullmatch(r"/w/([^/]+)/b/([^/]+)/s/([^/]+)", path)):
                key, batch, scenario = (unquote(match[i]) for i in (1, 2, 3))
                self.respond(200, pages.scenario(service.read_scenario(key, batch, scenario), service.jira_url).encode(), "text/html; charset=utf-8")
                return
            if method == 'GET' and path == '/api/v1/environment':
                result = dict(ok=True, **service.environment.public(local))
            elif method == 'POST' and path in ('/api/v1/environment/check', '/api/v1/environment/restart'):
                try:
                    if path.endswith('/check'):
                        service.environment.start_check()
                    else:
                        service.environment.restart()
                except ValueError as error:
                    reject(409, 'environment-unavailable', 'environment', str(error))
                result = dict(ok=True, **service.environment.public(local))
            elif method == 'GET' and path == '/api/v1/guide':
                self.respond(200, files('sop_board').joinpath('guide.md').read_bytes(), 'text/plain; charset=utf-8')
                return
            elif method == "POST" and (match := re.fullmatch(r"/api/v1/automation/(pause|resume)", path)):
                result = service.set_automation_paused(match[1] == 'pause')
            elif method == "GET" and path == "/api/v1/tasks":
                query = parse_qs(urlsplit(self.path).query)
                result = self.server.tasks.list(query.get('workspace', [None])[0], query.get('batch', [None])[0])
            elif method == "GET" and (match := re.fullmatch(r"/api/v1/tasks/([0-9]+)", path)):
                result = dict(ok=True, task=self.server.tasks.read(int(match[1])))
            elif method == "GET" and (match := re.fullmatch(r"/api/v1/tasks/([0-9]+)/files/(.+)", path)):
                raw = self.server.tasks.file(int(match[1]), unquote(match[2]))
                self.respond(200, raw, "application/octet-stream", [("Content-Security-Policy", "sandbox; default-src 'none'")])
                return
            elif method == "GET" and (match := re.fullmatch(r"/api/v1/workspaces/([^/]+)/batches/([^/]+)/merges/([0-9]+)/(scenario-file|pairs\.json)", path)):
                raw = self.server.tasks.merge_result(unquote(match[1]), unquote(match[2]), int(match[3]))
                if match[4] == 'pairs.json':
                    raw = self.server.tasks.file(int(match[3]), 'pairs.json')
                self.respond(200, raw, 'application/json; charset=utf-8' if match[4] == 'pairs.json' else 'text/yaml; charset=utf-8')
                return
            elif method == "POST" and (match := re.fullmatch(r"/api/v1/workspaces/([^/]+)/batches/([^/]+)/cases/([^/]+)/flow", path)):
                result = self.server.tasks.enqueue_merge(unquote(match[1]), unquote(match[2]), trigger='开发者指定独立场景', force_case=unquote(match[3]))
            elif method == "POST" and (match := re.fullmatch(r"/api/v1/workspaces/([^/]+)/batches/([^/]+)/merges", path)):
                result = self.server.tasks.enqueue_merge(unquote(match[1]), unquote(match[2]))
            elif method == "POST" and (match := re.fullmatch(r"/api/v1/workspaces/([^/]+)/batches/([^/]+)/executions", path)):
                result = self.server.tasks.enqueue_execution(unquote(match[1]), unquote(match[2]))
            elif method == "POST" and (match := re.fullmatch(r"/api/v1/tasks/([0-9]+)/(retry|cancel)", path)):
                result = self.server.tasks.action(int(match[1]), match[2])
            elif method == "POST" and (match := re.fullmatch(r"/api/v1/tasks/([0-9]+)/approve", path)):
                result = self.server.tasks.fixes.action(self.server.tasks.read(int(match[1])), 'approve', body.get('note', '')) if isinstance(body, dict) else self.server.tasks.fixes.action(self.server.tasks.read(int(match[1])), 'approve', None)
            elif method == "GET" and path == "/api/v1/workspaces":
                result = service.workspaces()
            elif method == "GET" and (match := re.fullmatch(r"/api/v1/workspaces/([^/]+)/entries", path)):
                result = service.entries(unquote(match[1]))
            elif method == "GET" and (match := re.fullmatch(r"/api/v1/workspaces/([^/]+)/fixes", path)):
                result = self.server.tasks.fixes.list(unquote(match[1]))
            elif method == "GET" and (match := re.fullmatch(r"/api/v1/workspaces/([^/]+)/attention", path)):
                result = self.server.tasks.fixes.attention(unquote(match[1]), 'http://' + self.headers.get('Host', 'localhost'))
            elif method == "GET" and (match := re.fullmatch(r"/api/v1/jira/([^/]+)", path)):
                result = self.server.jira.summary(unquote(match[1]))
            elif method == "GET" and (match := re.fullmatch(r"/api/v1/workspaces/([^/]+)/batches/([^/]+)/rounds/([0-9]+)/export", path)):
                key, batch, number = unquote(match[1]), unquote(match[2]), int(match[3])
                self.respond(200, service.export_round(key, batch, number), "application/zip",
                             [("Content-Disposition", f"attachment; filename=round-{number}.zip")])
                return
            elif method == "PUT" and (match := re.fullmatch(r"/api/v1/workspaces/([^/]+)", path)):
                result = service.settings(unquote(match[1]), body)
            elif method == "GET" and (match := re.fullmatch(r"/api/v1/evidence/([^/]+)", path)):
                raw, name = service.evidence(unquote(match[1]))
                media = evidence_content_type(name)
                if is_text(media):
                    media += "; charset=utf-8"
                self.respond(200, raw, media,
                             [("Content-Disposition", "inline; filename*=UTF-8''" + quote(name, safe="")),
                              ("Content-Security-Policy", "sandbox; default-src 'none'")])
                return
            elif match := re.fullmatch(r"/api/v1/workspaces/([^/]+)/batches/([^/]+)(?:/(scenario-file|scenarios|rounds|marks|cases|cases\.yaml|state|report\.md|results\.yaml|results)(?:/([0-9]+))?)?", path):
                key, batch, action, number = unquote(match[1]), unquote(match[2]), match[3], match[4]
                if method == "PUT" and action == "scenario-file" and number is None:
                    result = service.push(key, batch, body)
                elif method == "POST" and action == "rounds" and number is None:
                    result = service.round(key, batch, body)
                elif method == "POST" and action == "marks" and number is None:
                    result = service.mark(key, batch, body)
                elif method == "POST" and action == "cases" and number is None:
                    result = service.upload_cases(key, batch, body)
                elif method == "POST" and action == "results" and number is None:
                    result = service.import_results(key, batch, body)
                elif method == "GET" and action == "cases" and number is None:
                    result = service.read_cases(key, batch)
                elif method == "GET" and action == "report.md" and number is None:
                    self.respond(200, service.report(key, batch), "text/markdown; charset=utf-8",
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
                    result = service.read_batch(key, batch, scenarios=action == "scenarios")
                elif method == "GET" and action == "rounds" and number is not None:
                    result = service.read_round(key, batch, int(number))
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
