"""I4：真实看板 HTTP 入口连接本机模拟 Jira，不访问真实站点。"""

import pytest
import time

from helpers import assert_error
from jira_helpers import mock_jira
from sop_board.jira import Jira


def test_jira_summary_requests_exact_path_basic_and_accept(board_factory):
    """SOP-24-P07：读取标题、状态与规范链接，浏览器不需要写入令牌。"""
    with mock_jira() as jira:
        board = board_factory(env={**jira.configured_env(), "JIRA_URL": jira.url + "/"})
        status, body, _ = board.request("GET", "/api/v1/jira/ABC-1", auth=False)
        assert status == 200
        assert body == dict(ok=True, key="ABC-1", summary="登录按钮无响应",
                            status="待确认", url=jira.url + "/browse/ABC-1")
        assert len(jira.requests) == 1
        request = jira.requests[0]
        assert request[0] == "/rest/api/3/issue/ABC-1?fields=summary,status"
        authenticated = jira.authenticated(request)
        assert authenticated, "Jira Basic 认证与隔离测试身份不符"
        assert request[1]["Accept"] == "application/json"


@pytest.mark.parametrize("key", ["abc-1", "ABC", "ABC1", "1ABC-1", "ABC-1%2Fother", "ABC-1%3Ffields=all"])
def test_jira_invalid_key_rejected_without_upstream_request(board_factory, key):
    """SOP-24-N03/N05：先核对完整单号，不把不合规输入传给 Jira。"""
    with mock_jira() as jira:
        board = board_factory(env=jira.configured_env())
        assert_error(board.request("GET", "/api/v1/jira/" + key, auth=False), 422, "jira-key-invalid")
        assert jira.requests == []


@pytest.mark.parametrize("code", [400, 401, 403, 404, 500, 503])
def test_jira_http_errors_have_safe_reason_and_are_not_cached(board_factory, code):
    """SOP-24-N04/N06：4xx、5xx 不透传正文；重试仍请求 Jira。"""
    with mock_jira() as jira:
        jira.replies["ABC-1"] = (code, dict(echo=jira.email + jira.token + jira.basic), 0)
        board = board_factory(env=jira.configured_env())
        for _ in range(2):
            problems = assert_error(board.request("GET", "/api/v1/jira/ABC-1", auth=False),
                                    502, "jira-unavailable")
            assert str(code) in problems[0]["problem"]
        assert len(jira.requests) == 2


def test_jira_disconnected_returns_502_without_affecting_other_pages(board_factory):
    """SOP-24-N03：Jira 停止后接口给出原因，看板首页照常响应。"""
    with mock_jira() as jira:
        env = jira.configured_env()
    board = board_factory(env=env)
    problems = assert_error(board.request("GET", "/api/v1/jira/ABC-1", auth=False), 502, "jira-unavailable")
    assert "连接" in problems[0]["problem"]
    assert board.request("GET", "/", auth=False)[0] == 200


def test_jira_timeout_uses_environment_override_and_does_not_cache_failure(board_factory):
    """SOP-24-N04：只缩短测试等待，超时失败后可以重新读取。"""
    with mock_jira() as jira:
        jira.replies["ABC-777"] = (200, {}, 0.6)
        board = board_factory(env={**jira.configured_env(), "JIRA_TIMEOUT_SECONDS": "0.15"})
        started = time.monotonic()
        problems = assert_error(board.request("GET", "/api/v1/jira/ABC-777", auth=False),
                                502, "jira-unavailable")
        elapsed = time.monotonic() - started
        assert 0.14 <= elapsed < 0.55
        assert "超时" in problems[0]["problem"]
        del jira.replies["ABC-777"]
        assert board.request("GET", "/api/v1/jira/ABC-777", auth=False)[0] == 200
        assert len(jira.requests) == 2


def test_jira_success_cache_expires_at_five_minutes_with_injected_clock():
    """SOP-24-P07：一分钟与到期前复用成功值，恰好五分钟重新读取；每个单号独立。"""
    with mock_jira() as jira:
        instant = [1000.0]
        client = Jira(jira.url, jira.email, jira.token, clock=lambda: instant[0])
        original = client.summary("ABC-1")
        assert original["summary"] == "登录按钮无响应"
        jira.replies["ABC-1"] = (200, dict(fields=dict(summary="已修复", status=dict(name="关闭"))), 0)
        for moment in (1060, 1299.999):
            instant[0] = moment
            assert client.summary("ABC-1") == original
        assert len(jira.requests) == 1
        assert client.summary("QA_TEAM-45")["key"] == "QA_TEAM-45"
        instant[0] = 1300
        refreshed = client.summary("ABC-1")
        assert refreshed["summary"] == "已修复" and refreshed["status"] == "关闭"
        assert len(jira.requests) == 3


@pytest.mark.parametrize("body", [b"not json", {}, {"fields": None},
                                 {"fields": {"summary": [], "status": {"name": "待确认"}}},
                                 {"fields": {"summary": "标题", "status": {}}}])
def test_jira_invalid_upstream_response_is_safe_502_and_not_cached(board_factory, body):
    """SOP-24-N04/N06：异常响应同样不泄漏正文，不中断看板、不缓存。"""
    with mock_jira() as jira:
        jira.replies["ABC-1"] = (200, body, 0)
        board = board_factory(env=jira.configured_env())
        problems = assert_error(board.request("GET", "/api/v1/jira/ABC-1", auth=False), 502, "jira-unavailable")
        assert "响应" in problems[0]["problem"]
        del jira.replies["ABC-1"]
        assert board.request("GET", "/api/v1/jira/ABC-1", auth=False)[0] == 200
        assert len(jira.requests) == 2


def test_jira_redacts_echoed_authentication_from_success_and_failure(board_factory):
    """SOP-24-N06：即使上游成功字段或错误正文回显身份，也不外泄。"""
    with mock_jira() as jira:
        echo = " ".join([jira.email, jira.token, jira.basic])
        jira.replies["ABC-1"] = (200, dict(fields=dict(summary=echo, status=dict(name=echo))), 0)
        jira.replies["ABC-500"] = (500, dict(echo=echo), 0)
        board = board_factory(env=jira.configured_env())
        for key, expected_status in [("ABC-1", 200), ("ABC-500", 502)]:
            status, body, headers = board.request("GET", "/api/v1/jira/" + key, auth=False)
            assert status == expected_status
            leaked = any(secret in str(body) + str(headers) for secret in [jira.email, jira.token, jira.basic])
            assert not leaked, "Jira 响应泄漏隔离测试身份"
        leaked = any(secret in board.log.read_text() for secret in [jira.email, jira.token, jira.basic])
        assert not leaked, "看板日志泄漏隔离测试身份"


def test_jira_cache_clears_on_restart_without_changing_saved_data(board_factory):
    """SOP-01-P07（缓存部分）：同目录重启首读访问 Jira，既有数据不变。"""
    from helpers import BASE, push, snapshot
    from cases_helpers import upload_cases
    from test_marks import mark_body

    with mock_jira() as jira:
        board = board_factory(env=jira.configured_env())
        pushed, _ = push(board)
        upload_cases(board, base=BASE)
        assert board.request("POST", BASE + "/marks", mark_body(pushed))[0] == 200
        before = snapshot(board)
        original = board.request("GET", "/api/v1/jira/ABC-1", auth=False)[1]
        assert board.request("GET", "/api/v1/jira/ABC-1", auth=False)[1] == original
        assert len(jira.requests) == 1
        assert snapshot(board) == before
        board.stop()
        board.start()
        assert board.request("GET", "/api/v1/jira/ABC-1", auth=False)[1] == original
        assert len(jira.requests) == 2
        assert snapshot(board) == before


def test_jira_default_timeout_remains_ten_seconds(board_factory):
    """SOP-24-N04：不设置缩短时间的变量，真实等待十秒后返回 502。"""
    from conftest import OPENER
    import json
    from urllib.error import HTTPError
    from urllib.request import Request

    with mock_jira() as jira:
        jira.replies["ABC-777"] = (200, {}, 10.8)
        board = board_factory(env=jira.configured_env())
        request = Request(f"http://127.0.0.1:{board.port}/api/v1/jira/ABC-777")
        started = time.monotonic()
        with pytest.raises(HTTPError) as caught:
            OPENER.open(request, timeout=12)
        elapsed = time.monotonic() - started
        with caught.value as response:
            assert response.code == 502
            body = json.load(response)
        assert body["error_code"] == "jira-unavailable"
        assert "10 秒" in body["problems"][0]["problem"]
        assert 10 <= elapsed < 10.7


def test_jira_redirect_cannot_forward_authentication(board_factory):
    """SOP-24-N06：模拟 Jira 重定向时，不向另一地址转发认证头。"""
    with mock_jira() as jira, mock_jira() as other:
        jira.replies["ABC-1"] = (302, {}, 0, {"Location": other.url + "/rest/api/3/issue/ABC-1"})
        board = board_factory(env=jira.configured_env())
        status, body, _ = board.request("GET", "/api/v1/jira/ABC-1", auth=False)
        assert status == 502
        assert body["error_code"] == "jira-unavailable"
        assert len(other.requests) == 0


def test_jira_credentials_absent_from_existing_pages_apis_exports_and_logs(board_factory):
    """SOP-24-N06、T4：扫描进度、报告、结果导出/导入及已有入口与日志。"""
    from cases_helpers import upload_cases
    from helpers import BASE, IDS, KEY, push, snapshot
    from test_marks import mark_body
    from test_pages import WEB

    with mock_jira() as jira:
        jira.replies["ABC-500"] = (500, dict(headers={"Authorization": "Basic " + jira.basic,
                                                   "email": jira.email, "token": jira.token}), 0)
        board = board_factory(env=jira.configured_env())
        pushed, _ = push(board)
        upload_cases(board, base=BASE)
        for sid, issue in [(IDS[0], "ABC-1"), (IDS[2], "ABC-500")]:
            assert board.request("POST", BASE + "/marks", mark_body(pushed, "fail", scenario=sid,
                fingerprint=pushed["fingerprints"][sid], defect_url=issue))[0] == 200
        import yaml
        document = dict(run_id="scan", round=1, environment="隔离环境", results=[dict(
            case_id=IDS[0], status="passed", actual_result="人工结果", executor="测试者", tested_at="2026-10-18T10:00:00+08:00")])
        status, imported, headers = board.request("POST", BASE + "/results", dict(
            uploaded_by="上传者", content=yaml.safe_dump(document, allow_unicode=True)))
        assert status == 200 and imported["imported"] == 1
        assert not any(secret in str(imported) + str(headers) for secret in [jira.email, jira.token, jira.basic]), "导入响应泄漏隔离测试身份"
        before = snapshot(board)
        paths = ["/", WEB, WEB + "/progress", BASE + "/report.md", BASE + "/results.yaml", BASE + "/state",
                 WEB + "/scenarios", WEB + "/cases", *(WEB + "/s/" + sid for sid in IDS),
                 "/assets/board.js", "/assets/board.css", "/api/v1/projects", BASE, BASE + "/scenarios",
                 BASE + "/cases", BASE + "/cases.yaml", BASE + "/scenario-file", BASE + "/rounds/1",
                 BASE + "/rounds/2", BASE + "/rounds/1/export", BASE + "/rounds/2/export",
                 "/api/v1/jira/ABC-1", "/api/v1/jira/ABC-500", "/api/v1/jira/abc-1"]
        paths += ["/api/v1/evidence/" + sha for sha, in board.sql("SELECT sha256 FROM evidence")]
        secrets = [jira.email, jira.token, jira.basic]
        for path in paths:
            status, body, headers = board.request("GET", path, auth=False)
            expected_status = 502 if path.endswith("/jira/ABC-500") else 422 if path.endswith("/jira/abc-1") else 200
            assert status == expected_status
            raw = body.decode(errors="replace") if isinstance(body, bytes) else str(body)
            leaked = any(secret in raw + str(headers) for secret in secrets)
            assert not leaked, "页面或接口泄漏隔离测试身份，入口：" + path
        assert snapshot(board) == before
        board.stop()
        leaked = any(secret in board.log.read_text() for secret in secrets)
        assert not leaked, "看板日志泄漏隔离测试身份"


def test_jira_expired_success_does_not_hide_or_cache_a_later_failure():
    """SOP-24-P07/N04：过期成功值不能掩盖失败；恢复时立即重新读取。"""
    from sop_board.errors import APIError

    with mock_jira() as jira:
        instant = [0]
        client = Jira(jira.url, jira.email, jira.token, clock=lambda: instant[0])
        assert client.summary("ABC-1")["status"] == "待确认"
        instant[0] = 300
        jira.replies["ABC-1"] = (500, {}, 0)
        for _ in range(2):
            with pytest.raises(APIError) as caught:
                client.summary("ABC-1")
            assert caught.value.status == 502 and caught.value.code == "jira-unavailable"
        jira.replies["ABC-1"] = (200, dict(fields=dict(summary="已恢复", status=dict(name="已解决"))), 0)
        assert client.summary("ABC-1")["summary"] == "已恢复"
        assert len(jira.requests) == 4


@pytest.mark.parametrize("missing", ["JIRA_URL", "JIRA_EMAIL", "JIRA_API_TOKEN", "all"])
def test_jira_incomplete_config_never_requests_upstream(board_factory, missing):
    """SOP-24-N05：三个变量任一个未设置都不请求 Jira。"""
    with mock_jira() as jira:
        env = jira.configured_env()
        env = {} if missing == "all" else {k: v for k, v in env.items() if k != missing}
        board = board_factory(env=env)
        assert_error(board.request("GET", "/api/v1/jira/ABC-1", auth=False), 404, "jira-not-configured")
        assert jira.requests == []
