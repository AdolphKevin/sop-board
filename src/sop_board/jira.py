"""读取 Jira 单据摘要；认证信息只保留在看板进程中。"""

import base64
from http.client import HTTPException
import json
import re
import time
from threading import Lock
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .defect_links import ISSUE_KEY
from .errors import reject


def identify(value):
    """只从 Jira Cloud 的 HTTPS 单据链接取得站点和单号。"""
    if not isinstance(value, str):
        return None
    try:
        link = urlsplit(value)
        hostname = (link.hostname or '').lower()
        match = re.fullmatch(r'/browse/(' + ISSUE_KEY + r')/?', link.path)
        if link.scheme == 'https' and hostname.endswith('.atlassian.net') and match:
            return 'https://' + hostname, match[1]
    except ValueError:
        pass
    return None


def require_issue(value):
    issue = identify(value)
    if issue is None:
        reject(422, 'jira-link-invalid', 'url', '未识别为 Jira Cloud 单据链接')
    return issue


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        # A redirect must never forward Basic authentication to another address.
        return None


class Jira:
    def __init__(self, email="", token="", timeout=10, clock=time.monotonic):
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

    def write_fix(self, url, paragraphs, fixed_status):
        """逐项记录结果；不保存或转发上游正文、异常详情中的认证内容。"""
        issue = identify(url)
        if issue is None:
            return dict(comment='未识别为 Jira，未回写', transition='未识别为 Jira，未回写', reason='缺陷链接不是 Jira Cloud 单据链接')
        site, key = issue
        if not all((self.email, self.token)):
            return dict(comment='Jira 未配置，未回写', transition='Jira 未配置，未回写', reason='Jira 未配置，未回写')
        authorization = base64.b64encode(f'{self.email}:{self.token}'.encode()).decode()

        def request(suffix, body=None):
            req = Request(f'{site}/rest/api/3/issue/{key}/{suffix}',
                data=json.dumps(body, ensure_ascii=False).encode() if body is not None else None,
                headers={'Authorization': 'Basic ' + authorization, 'Accept': 'application/json',
                         'Content-Type': 'application/json'})
            try:
                with self.opener.open(req, timeout=self.timeout) as response:
                    return json.load(response) if body is None else None
            except HTTPError as error:
                error.close()
                raise ValueError(f'Jira 返回 HTTP {error.code}') from None
            except TimeoutError:
                raise ValueError('Jira 请求超时') from None
            except OSError:
                raise ValueError('Jira 连接失败') from None
            except (ValueError, KeyError, TypeError, HTTPException):
                raise ValueError('Jira 响应格式无效或连接中断') from None

        reasons = []
        try:
            request('comment', dict(body=dict(type='doc', version=1, content=[
                dict(type='paragraph', content=[dict(type='text', text=p)]) for p in paragraphs if p])))
            comment = '评论成功'
        except ValueError as error:
            comment = '评论失败'
            reasons.append('评论失败：' + str(error))
        transition = 'Jira 状态待人工更新'
        if fixed_status:
            try:
                choices = request('transitions')['transitions']
                choice = next((t for t in choices if t['to']['name'] == fixed_status), None)
                if choice is None:
                    reasons.append('没有目标状态的可用流转')
                else:
                    request('transitions', dict(transition=dict(id=choice['id'])))
                    transition = '流转成功'
            except (ValueError, KeyError, TypeError):
                reasons.append('流转失败：Jira 请求失败或响应无效')
        else:
            reasons.append('未设置 Jira 修复目标状态')
        return dict(comment=comment, transition=transition, reason='；'.join(reasons))

    def issue(self, url):
        """修复会话需要的单据内容；不使用页面摘要缓存。"""
        site, key = require_issue(url)
        if not all((self.email, self.token)):
            reject(404, 'jira-not-configured', 'jira', 'Jira 未配置完整')
        authorization = base64.b64encode(f'{self.email}:{self.token}'.encode()).decode()
        request = Request(f'{site}/rest/api/3/issue/{key}?fields=summary,description,status,comment,attachment',
            headers={'Authorization': 'Basic ' + authorization, 'Accept': 'application/json'})
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                fields = json.load(response)['fields']
            result = dict(site=site, key=key, summary=fields['summary'], description=fields.get('description'),
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

    def summary(self, url):
        site, key = require_issue(url)
        if not all((self.email, self.token)):
            reject(404, "jira-not-configured", "jira", "Jira 未配置：需同时配置 JIRA_EMAIL 与 JIRA_API_TOKEN")
        with self.lock:
            cached = self.cache.get((site, key))
            if cached and self.clock() < cached[0]:
                return dict(cached[1])
        authorization = base64.b64encode(f"{self.email}:{self.token}".encode()).decode()
        request = Request(f"{site}/rest/api/3/issue/{key}?fields=summary,status",
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
                      status=redact(status), url=f"{site}/browse/{key}")
        with self.lock:
            current = self.clock()
            self.cache = {key: value for key, value in self.cache.items() if current < value[0]}
            self.cache[(site, key)] = (current + 300, result)
        return dict(result)
