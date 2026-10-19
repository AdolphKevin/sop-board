"""浏览器脚本的隔离逻辑测试；真实浏览器操作另行验收。"""

import json
from pathlib import Path
import subprocess

import pytest

from helpers import IDS, push
from html_helpers import page
from test_pages import WEB


def run_client(board, case, saved=None):
    push(board)
    form = page(board, WEB + "/s/" + IDS[0]).one("form", id="mark-form")
    fields = []
    for field in form.find_all():
        if field.tag not in ("input", "textarea", "select"):
            continue
        value = field.attrs.get("value", field.text if field.tag == "textarea" else "")
        if field.tag == "select":
            value = field.find_all("option")[0].attrs["value"]
        fields.append(dict(name=field.attrs["name"], value=value, required="required" in field.attrs))
    status, script, _ = board.request("GET", "/assets/board.js", auth=False)
    assert status == 200
    payload = dict(case=case, fields=fields, action=form.attrs["action"], script=script.decode(), saved=saved or {})
    result = subprocess.run(["node", str(Path(__file__).with_name("client_form_runner.js"))],
                            input=json.dumps(payload), text=True, capture_output=True, timeout=5)
    assert result.returncode == 0, result.stdout + result.stderr


def test_client_restores_identity_and_saves_changes_without_network(board):
    """I1；SOP-04-P01：读取 localStorage，身份可更换且不发出请求。"""
    run_client(board, "identity", {"sop-board.marker": "Saved Tester", "sop-board.token": "local-test-value"})


def test_client_requires_defect_for_fail_and_finding_for_blocked_skip(board):
    """I1、I2；SOP-04-N01：失败必填缺陷，受阻和跳过必填原因。"""
    run_client(board, "constraints")


@pytest.mark.parametrize("submission", ["native", "programmatic"])
def test_client_empty_fail_defect_shows_reason_without_request(board, submission):
    """W1；SOP-04-N01：空缺陷链接在提示区显示原因，保留输入且不发请求。"""
    run_client(board, "empty-defect-" + submission)


def test_client_submits_current_scenario_and_reports_success(board):
    """I1、I2；SOP-04-P01：按人工接口契约发送，令牌只在请求头。"""
    run_client(board, "success")


@pytest.mark.parametrize("reason", ["defect", "unauthorized", "stale", "network"])
def test_client_preserves_input_and_allows_retry_after_rejection(board, reason):
    """I1、I2；SOP-04-N01、SOP-04-N02：拒绝原因、令牌提示、保留内容与可重试。"""
    run_client(board, "reject-" + reason)
