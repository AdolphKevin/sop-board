"""标准库 HTTP 适配层；不承担统计与持久化规则。"""

import hmac
from http.server import BaseHTTPRequestHandler
import json
import mimetypes
import re
import sqlite3
from urllib.parse import quote, unquote, urlsplit

from .errors import APIError, reject


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
            if method in ("POST", "PUT"):
                authorization = self.headers.get("Authorization", "")
                if not hmac.compare_digest(authorization.encode(), ("Bearer " + self.server.token).encode()):
                    reject(401, "unauthorized", "Authorization", "令牌缺失或不符")
                try:
                    length = int(self.headers.get("Content-Length", ""))
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
                    body = json.loads(raw.decode("utf-8"), parse_constant=invalid_constant)
                except (ValueError, UnicodeError):
                    reject(400, "invalid-request", "body", "应为 UTF-8 JSON")
            if method == "GET" and path == "/":
                self.respond(200, "<!doctype html><html lang='zh'><meta charset='utf-8'><title>sop-board</title><h1>sop-board</h1></html>".encode(), "text/html; charset=utf-8")
                return
            if method == "GET" and path == "/api/v1/projects":
                result = service.projects()
            elif method == "PUT" and (match := re.fullmatch(r"/api/v1/projects/([^/]+)", path)):
                result = service.settings(unquote(match[1]), body)
            elif method == "GET" and (match := re.fullmatch(r"/api/v1/evidence/([^/]+)", path)):
                raw, name = service.evidence(unquote(match[1]))
                self.respond(200, raw, mimetypes.guess_type(name)[0] or "application/octet-stream",
                             [("Content-Disposition", "inline; filename*=UTF-8''" + quote(name, safe=""))])
                return
            elif match := re.fullmatch(r"/api/v1/projects/([^/]+)/batches/([^/]+)(?:/(scenario-file|scenarios|rounds|marks)(?:/([0-9]+))?)?", path):
                key, batch, action, number = unquote(match[1]), unquote(match[2]), match[3], match[4]
                if method == "PUT" and action == "scenario-file" and number is None:
                    result = service.push(key, batch, body)
                elif method == "POST" and action == "rounds" and number is None:
                    result = service.round(key, batch, body)
                elif method == "POST" and action == "marks" and number is None:
                    result = service.mark(key, batch, body)
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
