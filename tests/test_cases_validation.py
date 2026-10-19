"""I3：错误字段、准确行号以及失败时没有持久副作用。"""

import pytest

from cases_helpers import CASES_BASE, cases_body, upload_cases
from helpers import BASE, assert_error, push, push_body, snapshot


VALID = """metadata:
  module: 演示
  feature: 登录
  owner: 张三
  last_reviewed: 2026-10-19
test_cases:
  - id: TC_DEMO_LOGIN_001
    title: 登录成功
    priority: P0
    type: functional
    steps: [登录]
    expected_results: [成功]
    tags: ['section:登录']
"""


def bad_upload(board, content, expected):
    before = snapshot(board)
    problems = assert_error(board.request("POST", CASES_BASE + "/cases",
        dict(filename="bad.yaml", uploaded_by="张三", content=content)), 422, "cases-invalid")
    assert {(p["field"], p["line"]) for p in problems} == set(expected), problems
    assert all(set(p) == {"field", "line", "problem"} and p["problem"] for p in problems)
    assert snapshot(board) == before


def test_cases_reports_bad_id_extra_field_duplicate_and_missing_field(board):
    """SOP-23-N01：重复编号指向后出现项，缺字段指向该项。"""
    push(board)
    upload_cases(board)
    raw = VALID.replace("TC_DEMO_LOGIN_001", "TC_1") + """    owner: 多余
  - id: TC_DEMO_LOGIN_002
    title: 第二条
    priority: P1
    type: ui
    steps: [点击]
    expected_results: [成功]
    tags: [smoke]
  - id: TC_DEMO_LOGIN_002
    title: 重复项
    priority: P2
    type: security
    steps: [操作]
    tags: [smoke]
"""
    bad_upload(board, raw, [("test_cases.0.id", 7), ("test_cases.0.owner", 14),
                            ("test_cases.2.id", 22), ("test_cases.2.expected_results", 22)])


def test_cases_reports_each_metadata_and_case_field(board):
    """SOP-23-N05：逐项核对 metadata 与用例字段，不只报首项。"""
    push(board)
    upload_cases(board)
    meta = VALID.replace("  owner: 张三\n", "").replace("2026-10-19", "2026/10/19").replace(
        "test_cases:\n", "  version: 1\n  tags: smoke\ntest_cases:\n")
    bad_upload(board, meta, [("metadata.owner", 1), ("metadata.last_reviewed", 4),
                             ("metadata.version", 5), ("metadata.tags", 6)])
    fields = VALID.replace("priority: P0", "priority: P3").replace("type: functional", "type: api").replace(
        "steps: [登录]", "steps: []").replace("expected_results: [成功]", "expected_results: ['']").replace(
        "tags: ['section:登录']", "tags: []") + """    auto: 'yes'
    preconditions: 已登录
    description: 1
"""
    bad_upload(board, fields, [("test_cases.0.priority", 9), ("test_cases.0.type", 10),
        ("test_cases.0.steps", 11), ("test_cases.0.expected_results.0", 12), ("test_cases.0.tags", 13),
        ("test_cases.0.auto", 14), ("test_cases.0.preconditions", 15), ("test_cases.0.description", 16)])


@pytest.mark.parametrize("raw,expected", [
    ("", [("content", None)]), ("   # empty\n", [("content", None)]),
    ("- list\n", [("content", None)]),
    ("metadata: {}", [("test_cases", None)]), ("test_cases: []", [("metadata", None)]),
    (VALID + "extra: 1\n", [("extra", 14)]),
    (VALID[:VALID.index("  - id:")] + "  []\n", [("test_cases", 6)]),
    (VALID.replace("module: 演示", "module: ' '"), [("metadata.module", 2)]),
    (VALID.replace("feature: 登录", "feature: 1"), [("metadata.feature", 3)]),
    (VALID.replace("owner: 张三", "owner: false"), [("metadata.owner", 4)]),
    (VALID.replace("2026-10-19", "2026-02-30"), [("metadata.last_reviewed", 5)]),
    (VALID.replace("title: 登录成功", "title: ''"), [("test_cases.0.title", 8)]),
    (VALID.replace("steps: [登录]", "steps: [1]"), [("test_cases.0.steps.0", 11)]),
    (VALID.replace("tags: ['section:登录']", "tags: ['']"), [("test_cases.0.tags.0", 13)]),
    (VALID[:VALID.index("  - id:")] + "  - string\n", [("test_cases.0", 7)]),
])
def test_cases_file_shapes_and_remaining_types_are_atomic(board, raw, expected):
    """SOP-23-N05/N06/N07：文件级问题 line=null；拒绝后不建新批次。"""
    push(board)
    bad_upload(board, raw, expected)


@pytest.mark.parametrize("raw", ["x: &a [*a]", "!!python/object/apply:os.system [false]", "1: value", VALID + "test_cases: []\n"])
def test_cases_unsafe_or_ambiguous_yaml_is_rejected_without_writes(board, raw):
    """I3：不让递归别名、特殊对象或重复键绕过 Schema。"""
    push(board)
    before = snapshot(board)
    problems = assert_error(board.request("POST", CASES_BASE + "/cases",
        dict(filename="bad.yaml", content=raw, uploaded_by="张三")), 422, "cases-invalid")
    assert all("line" in p for p in problems)
    assert snapshot(board) == before


@pytest.mark.parametrize("body", [[], {}, {"filename": "", "uploaded_by": 1, "content": 1}])
def test_cases_request_fields_are_validated_without_writes(board, body):
    """I3：正文与上传人、文件名、原文须有正确类型。"""
    push(board)
    before = snapshot(board)
    problems = assert_error(board.request("POST", CASES_BASE + "/cases", body), 422, "cases-invalid")
    assert all(p["line"] is None for p in problems)
    assert snapshot(board) == before


def test_cases_and_scenario_syntax_errors_include_parser_reason_line_and_column(board):
    """SOP-23-N04：两种上传的 YAML 语法错误均保留解析器原因和列号。"""
    push(board)
    before = snapshot(board)
    broken_case = 'metadata:\n  module: demo\n  feature: login\n  owner: tester\n  last_reviewed: "unfinished'
    problems = assert_error(board.request("POST", CASES_BASE + "/cases",
        dict(filename="syntax.yaml", uploaded_by="张三", content=broken_case)), 422, "cases-invalid")
    assert problems[0]["line"] == 5
    assert "found unexpected end of stream" in problems[0]["problem"]
    assert "列 29" in problems[0]["problem"]
    broken_scenario = "metadata:\n  batch: demo\n  version: 1\n  owner: tester\nsops:\n  - id: N1\n   title: broken\n"
    body = push_body(content=broken_scenario)
    problems = assert_error(board.request("PUT", BASE + "/scenario-file", body), 422, "scenario-file-invalid")
    assert problems[0]["line"] == 7
    assert "expected <block end>" in problems[0]["problem"]
    assert "列 4" in problems[0]["problem"]
    assert snapshot(board) == before
