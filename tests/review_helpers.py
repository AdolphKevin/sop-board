"""SOP-BOARD-2 v2 七场景前置数据；全部写入经真实 HTTP 入口。"""

from copy import deepcopy
import hashlib

from helpers import KEY, demo, push_body, upload


F7_API = f"/api/v1/projects/{KEY}/batches/f7"
F7_WEB = f"/p/{KEY}/b/f7"
F7_IDS = ["N1-P01", "N1-P02", "N1-N01", "N1-N02", "N2-P01", "N2-P02", "N2-N01"]


def seven_document():
    document = demo()
    document["metadata"]["batch"] = "f7"
    for node in document["sops"]:
        originals = node["scenarios"]
        node["title"] = "节点 " + node["id"]
        node["entry"] = "节点入口 " + node["id"]
        node["scenarios"] = []
        for sid in F7_IDS:
            if not sid.startswith(node["id"] + "-"):
                continue
            positive = "-P" in sid
            row = deepcopy(originals[0 if positive else 1])
            row.update(id=sid, title="说明 " + sid, actor="场景执行者 " + sid,
                       initial_state=[sid + " 已准备"], steps=[sid + " 执行核对"],
                       expected=[sid + " 独立业务预期"], forbidden_effects=[sid + " 不得产生额外数据"],
                       refs=["R1"] if sid == "N1-P02" else ["依据 " + sid])
            if sid == "N2-P01":
                row["regression"] = {"check": "unit", "cases": ["tests.test_demo::test_n2"]}
            node["scenarios"].append(row)
    return document


def seven_mark(pushed, sid, status="pass", marker="张三", **extra):
    return dict(scenario=sid, fingerprint=pushed["fingerprints"][sid], status=status, marker=marker, **extra)


def prepare_seven(board):
    document = seven_document()
    pushed = push_body(document, path="f7.yaml")
    code, response, _ = board.request("PUT", F7_API + "/scenario-file", pushed)
    assert code == 200, response
    evidence = upload(board, "七场景 Agent 原始证据".encode(), "agent.txt")
    results = []
    for sid, status in [("N1-P02", "pass"), ("N1-N01", "fail"), ("N1-N02", "blocked"),
                        ("N2-P01", "pass"), ("N2-P02", "pass"), ("N2-N01", "skip")]:
        result = dict(scenario=sid, fingerprint=pushed["fingerprints"][sid], status=status,
                      actual=sid + " Agent 实际结果", executor="Li Si", evidence=[evidence],
                      executed_at="2020-01-15T00:09:10+00:00")
        if status in ("blocked", "skip"):
            result["finding"] = "环境未就绪" if status == "blocked" else "依赖未部署"
        results.append(result)
    body = dict(idempotency_key="seven-agent", scenario_file_sha256=hashlib.sha256(pushed["content"].encode()).hexdigest(),
                product=dict(commit="c1", digest="product-1", dirty=False), submitted_by="开发岗",
                implementer="开发实现者", results=results)
    code, response, _ = board.request("POST", F7_API + "/rounds", body)
    assert code == 200 and response["round"] == 1, response
    for sid, status, extra in [("N2-P01", "pass", {}), ("N2-P02", "fail", {"defect_url": "https://github.com/demo-org/demo-app/issues/1"})]:
        code, response, _ = board.request("POST", F7_API + "/marks", seven_mark(pushed, sid, status, marker="Tester A", **extra))
        assert code == 200, response
    return pushed
