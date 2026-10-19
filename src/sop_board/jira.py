"""读取 Jira 单据摘要；认证信息只保留在看板进程中。"""

import base64
from http.client import HTTPException
import json
import re
import time
from threading import Lock
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .defect_links import ISSUE_KEY
from .errors import reject


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        # A redirect must never forward Basic authentication to another address.
        return None


class Jira:
    def __init__(self, url="", email="", token="", timeout=10, clock=time.monotonic):
        self.url = url.rstrip("/")
        self.email = email
        self.token = token
        try:
            timeout = float(timeout)
        except (ValueError, TypeError):
            timeout = 10
        self.timeout = timeout if 0 < timeout <= 10 else 10
        self.clock = clock
        self.cache = {}
        self.lock = Lock()
        self.opener = build_opener(NoRedirect())

    def issue(self, key):
        """修复会话需要的单据内容；不使用页面摘要缓存。"""
        if not re.fullmatch(ISSUE_KEY, key):
            reject(422, 'jira-key-invalid', 'key', 'Jira 单号格式不符')
        if not all((self.url, self.email, self.token)):
            reject(404, 'jira-not-configured', 'jira', 'Jira 未配置完整')
        authorization = base64.b64encode(f'{self.email}:{self.token}'.encode()).decode()
        request = Request(f'{self.url}/rest/api/3/issue/{key}?fields=summary,description,status,comment,attachment',
            headers={'Authorization': 'Basic ' + authorization, 'Accept': 'application/json'})
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                fields = json.load(response)['fields']
            result = dict(key=key, summary=fields['summary'], description=fields.get('description'),
                status=fields['status']['name'], comments=fields.get('comment', {}).get('comments', []),
                attachments=[a['filename'] for a in fields.get('attachment', [])])
            if not isinstance(result['summary'], str) or not isinstance(result['status'], str) or not isinstance(result['comments'], list):
                raise ValueError()
        except HTTPError as error:
            error.close()
            reject(502, 'jira-unavailable', 'jira', f'Jira 返回 HTTP {error.code}')
        except TimeoutError:
            reject(502, 'jira-unavailable', 'jira', f'Jira 读取超时（{self.timeout:g} 秒）')
        except OSError:
            reject(502, 'jira-unavailable', 'jira', 'Jira 连接失败')
        except (ValueError, KeyError, TypeError, HTTPException):
            reject(502, 'jira-unavailable', 'jira', 'Jira 响应格式无效或不完整')

        def redact(value):
            if isinstance(value, str):
                for secret in sorted((self.email, self.token, authorization), key=len, reverse=True):
                    value = value.replace(secret, '***')
                return value
            if isinstance(value, list):
                return [redact(item) for item in value]
            if isinstance(value, dict):
                return {redact(k): redact(v) for k, v in value.items()}
            return value
        return redact(result)

    def summary(self, key):
        if not re.fullmatch(ISSUE_KEY, key):
            reject(422, "jira-key-invalid", "key", "Jira 单号格式不符，请填写如 ABC-1 的大写单号")
        if not all((self.url, self.email, self.token)):
            reject(404, "jira-not-configured", "jira", "需同时配置 JIRA_URL、JIRA_EMAIL 与 JIRA_API_TOKEN")
        with self.lock:
            cached = self.cache.get(key)
            if cached and self.clock() < cached[0]:
                return dict(cached[1])
        authorization = base64.b64encode(f"{self.email}:{self.token}".encode()).decode()
        request = Request(f"{self.url}/rest/api/3/issue/{key}?fields=summary,status",
                          headers={"Authorization": "Basic " + authorization,
                                   "Accept": "application/json"})
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                fields = json.load(response)["fields"]
            summary, status = fields["summary"], fields["status"]["name"]
            if not isinstance(summary, str) or not isinstance(status, str):
                raise ValueError()
        except HTTPError as error:
            error.close()
            reject(502, "jira-unavailable", "jira", f"Jira 返回 HTTP {error.code}")
        except TimeoutError:
            reject(502, "jira-unavailable", "jira", f"Jira 读取超时（{self.timeout:g} 秒）")
        except OSError:
            reject(502, "jira-unavailable", "jira", "Jira 连接失败，请检查网络或站点配置")
        except (ValueError, KeyError, TypeError, HTTPException):
            reject(502, "jira-unavailable", "jira", "Jira 响应格式无效或不完整")

        def redact(value):
            for secret in sorted((self.email, self.token, authorization), key=len, reverse=True):
                value = value.replace(secret, "***")
            return value

        result = dict(ok=True, key=key, summary=redact(summary),
                      status=redact(status), url=f"{self.url}/browse/{key}")
        with self.lock:
            current = self.clock()
            self.cache = {key: value for key, value in self.cache.items() if current < value[0]}
            self.cache[key] = (current + 300, result)
        return dict(result)
