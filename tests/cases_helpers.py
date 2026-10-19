"""方案 I3 的独立用例样本。"""

from copy import deepcopy

import yaml

from helpers import KEY


CASES_BASE = f"/api/v1/projects/{KEY}/batches/cases-only"
CASES_WEB = f"/p/{KEY}/b/cases-only"
CASE_IDS = ["TC_DEMO_LOGIN_001", "TC_DEMO_LOGIN_002", "TC_DEMO_ORDER_001"]


def cases_document():
    return dict(metadata=dict(module="演示应用", feature="用户与订单", owner="张三",
                              last_reviewed="2026-10-19", tags=["smoke"]),
                test_cases=[dict(id=sid, title=title, priority="P0", type="functional",
                                preconditions=["已有测试账号"], steps=["执行操作"],
                                expected_results=["操作成功"], tags=["section:" + section], auto=False)
                            for sid, title, section in zip(CASE_IDS, ["登录成功", "登录失败", "建立订单"],
                                                           ["登录", "登录", "订单"])])


def cases_body(document=None, **overrides):
    document = deepcopy(document if document is not None else cases_document())
    return dict(filename="cases-a.yaml", uploaded_by="张三",
                content=yaml.safe_dump(document, allow_unicode=True, sort_keys=False), **overrides)


def upload_cases(board, body=None, base=CASES_BASE):
    status, data, _ = board.request("POST", base + "/cases", body or cases_body())
    assert status == 200, data
    return data
