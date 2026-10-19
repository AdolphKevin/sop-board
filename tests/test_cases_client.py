"""实际 board.js 的文件上传控件，经 Node.js 隔离 DOM 调用真实服务。"""

import pytest

from cases_helpers import CASES_BASE, CASES_WEB, cases_body, upload_cases
from helpers import push, snapshot
from test_cases_validation import VALID
from test_client import run_page_client
from test_reads import get


@pytest.mark.parametrize("missing", ["marker", "token"])
def test_cases_client_missing_identity_never_reads_file_or_sends_request(board, missing):
    """SOP-23-P05：上传前要求身份，保持记录与并集不变。"""
    push(board)
    upload_cases(board)
    before = snapshot(board)
    run_page_client(board, "cases-missing-" + missing, CASES_WEB + "/cases", network=True)
    assert snapshot(board) == before


def test_cases_client_uploads_file_name_content_marker_and_refreshes_history(board):
    """SOP-23-P05：文件内容来自选择的文件，上传人取身份栏。"""
    push(board)
    upload_cases(board)
    body = cases_body()
    body["content"] = body["content"].replace("登录成功", "修改后的标题")
    run_page_client(board, "cases-success", CASES_WEB + "/cases", network=True, context={"file": body})
    result = get(board, CASES_BASE + "/cases")
    assert result["uploads"][1]["uploaded_by"] == "李四"
    assert result["uploads"][1]["filename"] == "cases-a.yaml"
    assert result["uploads"][1]["updated"] == 1
    assert result["cases"][0]["title"] == "修改后的标题"


def test_cases_client_renders_each_problem_with_field_and_line_without_changing_data(board):
    """SOP-23-N08：显示第 12、41 行的独立问题，上传数据保持不变。"""
    push(board)
    upload_cases(board)
    before = snapshot(board)
    raw = VALID.replace("  - id:", "\n" * 5 + "  - id:").replace("TC_DEMO_LOGIN_001", "TC_ab_1")
    raw += "\n" * 22 + "    owner_note: unknown\n"
    run_page_client(board, "cases-invalid", CASES_WEB + "/cases", network=True,
                    context={"file": dict(filename="bad.yaml", content=raw)})
    assert snapshot(board) == before
