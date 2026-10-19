"""标准库 HTTP 适配层；不承担统计与持久化规则。"""

import hmac
from http.server import BaseHTTPRequestHandler
from importlib.resources import files
import json
import re
import sqlite3
from urllib.parse import quote, unquote, urlsplit, parse_qs

from .errors import APIError, reject
from . import pages
from .evidence_types import content_type as evidence_content_type, is_text


def invalid_constant(value):
    raise ValueError("JSON 不接受非有限数字")


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
            assets = {"/assets/board.css": ("board.css", "text/css; charset=utf-8"),
                      "/assets/board.js": ("board.js", "text/javascript; charset=utf-8"),
                      "/assets/sop.js": ("sop.js", "text/javascript; charset=utf-8"),
                      "/assets/vendor/mermaid/mermaid.min.js": ("vendor/mermaid/mermaid.min.js", "text/javascript; charset=utf-8")}
            if method == "GET" and path in assets:
                name, content_type = assets[path]
                self.respond(200, files("sop_board").joinpath("assets", name).read_bytes(), content_type)
                return
            if method in ("POST", "PUT"):
                authorization = self.headers.get("Authorization", "")
                developer = bool(re.fullmatch(r"/api/v1/tasks/[^/]+/(retry|cancel)", path) or
                                 re.fullmatch(r"/api/v1/projects/[^/]+/batches/[^/]+/(merges|checks|executions)", path))
                if developer and hmac.compare_digest(authorization.encode(), ("Bearer " + self.server.token).encode()):
                    reject(403, "admin-required", "Authorization", "此操作需要开发者令牌")
                expected = self.server.admin_token if developer else self.server.token
                if not expected or not hmac.compare_digest(authorization.encode(), ("Bearer " + expected).encode()):
                    reject(401, "unauthorized", "Authorization", "令牌缺失或不符")
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
                self.respond(200, pages.home(service.projects()).encode(), "text/html; charset=utf-8")
                return
            if method == "GET" and (match := re.fullmatch(r"/p/([^/]+)/b/([^/]+)", path)):
                key, batch = unquote(match[1]), unquote(match[2])
                self.respond(200, pages.batch(key, service.read_batch(key, batch)).encode(), "text/html; charset=utf-8")
                return
            if method == "GET" and (match := re.fullmatch(r"/p/([^/]+)/b/([^/]+)/scenarios", path)):
                key, batch = unquote(match[1]), unquote(match[2])
                self.respond(200, pages.scenarios(key, batch, service.read_batch(key, batch, scenarios=True), service.jira_url).encode(), "text/html; charset=utf-8")
                return
            if method == "GET" and (match := re.fullmatch(r"/p/([^/]+)/b/([^/]+)/cases", path)):
                key, batch = unquote(match[1]), unquote(match[2])
                data = service.read_cases(key, batch, include_state=True)
                scenarios = service.read_batch(key, batch, scenarios=True)["scenarios"]
                self.respond(200, pages.cases(key, batch, data, scenarios).encode(), "text/html; charset=utf-8")
                return
            if method == "GET" and (match := re.fullmatch(r"/p/([^/]+)/b/([^/]+)/merges", path)):
                key, batch = unquote(match[1]), unquote(match[2])
                service.read_batch(key, batch)
                selected = parse_qs(urlsplit(self.path).query).get('task', [None])[0]
                self.respond(200, pages.merges(key, batch, self.server.tasks.list(key, batch)['tasks'], selected).encode(), "text/html; charset=utf-8")
                return
            if method == "GET" and (match := re.fullmatch(r"/p/([^/]+)/b/([^/]+)/sop", path)):
                key, batch = unquote(match[1]), unquote(match[2])
                policy = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self' data:; object-src 'none'; base-uri 'none'; frame-src 'none'"
                query = {key: values[0] for key, values in parse_qs(urlsplit(self.path).query).items()}
                self.respond(200, pages.sop(key, batch, service.read_sop(key, batch), query).encode(), "text/html; charset=utf-8",
                             [('Content-Security-Policy', policy)])
                return
            if method == "GET" and (match := re.fullmatch(r"/p/([^/]+)/b/([^/]+)/progress", path)):
                key, batch = unquote(match[1]), unquote(match[2])
                data = service.read_batch(key, batch)
                coverage = service.read_cases(key, batch)["coverage"]
                self.respond(200, pages.progress(key, data, coverage).encode(), "text/html; charset=utf-8")
                return
            if method == "GET" and (match := re.fullmatch(r"/p/([^/]+)/b/([^/]+)/s/([^/]+)", path)):
                key, batch, scenario = (unquote(match[i]) for i in (1, 2, 3))
                self.respond(200, pages.scenario(service.read_scenario(key, batch, scenario), service.jira_url).encode(), "text/html; charset=utf-8")
                return
            if method == "GET" and path == "/api/v1/tasks":
                query = parse_qs(urlsplit(self.path).query)
                result = self.server.tasks.list(query.get('project', [None])[0], query.get('batch', [None])[0])
            elif method == "GET" and (match := re.fullmatch(r"/api/v1/tasks/([0-9]+)", path)):
                result = dict(ok=True, task=self.server.tasks.read(int(match[1])))
            elif method == "GET" and (match := re.fullmatch(r"/api/v1/tasks/([0-9]+)/files/(.+)", path)):
                raw = self.server.tasks.file(int(match[1]), unquote(match[2]))
                self.respond(200, raw, "application/octet-stream", [("Content-Security-Policy", "sandbox; default-src 'none'")])
                return
            elif method == "GET" and (match := re.fullmatch(r"/api/v1/projects/([^/]+)/batches/([^/]+)/merges/([0-9]+)/(scenario-file|pairs\.json)", path)):
                raw = self.server.tasks.merge_result(unquote(match[1]), unquote(match[2]), int(match[3]))
                if match[4] == 'pairs.json':
                    raw = self.server.tasks.file(int(match[3]), 'pairs.json')
                self.respond(200, raw, 'application/json; charset=utf-8' if match[4] == 'pairs.json' else 'text/yaml; charset=utf-8')
                return
            elif method == "POST" and (match := re.fullmatch(r"/api/v1/projects/([^/]+)/batches/([^/]+)/merges", path)):
                result = self.server.tasks.enqueue_merge(unquote(match[1]), unquote(match[2]))
            elif method == "POST" and (match := re.fullmatch(r"/api/v1/projects/([^/]+)/batches/([^/]+)/checks", path)):
                result = self.server.tasks.enqueue_check(unquote(match[1]), unquote(match[2]))
            elif method == "POST" and (match := re.fullmatch(r"/api/v1/projects/([^/]+)/batches/([^/]+)/executions", path)):
                result = self.server.tasks.enqueue_execution(unquote(match[1]), unquote(match[2]))
            elif method == "POST" and (match := re.fullmatch(r"/api/v1/tasks/([0-9]+)/(retry|cancel)", path)):
                result = self.server.tasks.action(int(match[1]), match[2])
            elif method == "GET" and path == "/api/v1/projects":
                result = service.projects()
            elif method == "GET" and (match := re.fullmatch(r"/api/v1/projects/([^/]+)/entries", path)):
                result = service.entries(unquote(match[1]))
            elif method == "GET" and (match := re.fullmatch(r"/api/v1/jira/([^/]+)", path)):
                result = self.server.jira.summary(unquote(match[1]))
            elif method == "GET" and (match := re.fullmatch(r"/api/v1/projects/([^/]+)/batches/([^/]+)/rounds/([0-9]+)/export", path)):
                key, batch, number = unquote(match[1]), unquote(match[2]), int(match[3])
                self.respond(200, service.export_round(key, batch, number), "application/zip",
                             [("Content-Disposition", f"attachment; filename=round-{number}.zip")])
                return
            elif method == "PUT" and (match := re.fullmatch(r"/api/v1/projects/([^/]+)", path)):
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
            elif match := re.fullmatch(r"/api/v1/projects/([^/]+)/batches/([^/]+)(?:/(scenario-file|scenarios|rounds|marks|cases|cases\.yaml|state|report\.md|results\.yaml|results)(?:/([0-9]+))?)?", path):
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
        # Do not log request bodies, authorization headers, or request targets.
        pass
