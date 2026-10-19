"""只在本机运行的 Jira 替代服务；认证值仅在内存中生成与核对。"""

import base64
from contextlib import contextmanager
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import time
from uuid import uuid4


@contextmanager
def mock_jira():
    class Jira:
        requests = []
        replies = {}
        email = uuid4().hex + "@example.invalid"
        token = uuid4().hex

        def configured_env(self):
            return dict(JIRA_URL=self.url, JIRA_EMAIL=self.email, JIRA_API_TOKEN=self.token)

        @property
        def basic(self):
            return base64.b64encode(f"{self.email}:{self.token}".encode()).decode()

        def authenticated(self, request):
            return hmac.compare_digest(request[1].get("Authorization", ""), "Basic " + self.basic)

    jira = Jira()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            jira.requests.append((self.path, dict(self.headers)))
            key = self.path.split("/issue/")[-1].split("?")[0]
            status, body, delay, *extra = jira.replies.get(key, (200, dict(
                fields=dict(summary="登录按钮无响应", status=dict(name="待确认"))), 0))
            time.sleep(delay)
            raw = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            for name, value in (extra[0] if extra else {}).items():
                self.send_header(name, value)
            self.end_headers()
            try:
                self.wfile.write(raw)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = False
    jira.url = f"http://127.0.0.1:{server.server_port}"
    thread = threading.Thread(target=server.serve_forever, kwargs=dict(poll_interval=0.02))
    thread.start()
    try:
        yield jira
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()
