"""SOP-25/26 五场景数据，写入经真实接口，不从实现取得预期数值。"""

from copy import deepcopy
import hashlib

from cases_helpers import cases_body, cases_document, upload_cases
from helpers import KEY, demo, push_body, upload
from review_helpers import seven_mark

P5_IDS = [f"TC_DEMO_FLOW_{i:03}" for i in range(1, 6)]
P5_API = f"/api/v1/projects/{KEY}/batches/p5"
P5_WEB = f"/p/{KEY}/b/p5"


def flow_document(ids=P5_IDS, batch="p5"):
    document = demo()
    document["metadata"]["batch"] = batch
    node = document["sops"][0]
    originals = node["scenarios"]
    node["id"] = "F1"
    node["scenarios"] = []
    for i, sid in enumerate(ids):
        item = deepcopy(originals[0 if i < 3 else 1])
        item.update(id=sid, title="场景 " + sid, expected=[sid + " 的业务预期"])
        node["scenarios"].append(item)
    if len(ids) <= 3:
        node["negative_exempt"] = "本组仅包含成功路径"
    document["sops"] = [node]
    return document


def flow_cases(ids):
    document = cases_document()
    template = document["test_cases"][0]
    document["test_cases"] = [{**deepcopy(template), "id": sid, "title": "用例 " + sid} for sid in ids]
    return cases_body(document)


def prepare_five(board, supplement=False):
    ids = P5_IDS + (["SUP-P01"] if supplement else [])
    pushed = push_body(flow_document(ids), commit="c0ffee1", product_digest="D1", path="p5.yaml")
    status, data, _ = board.request("PUT", P5_API + "/scenario-file", pushed)
    assert status == 200, data
    evidence = upload(board, "五场景原始证据".encode(), "agent.txt")
    results = []
    for sid, status in zip(ids, ["pass", "pass", "pass", "fail", "blocked", "pass"]):
        result = dict(scenario=sid, fingerprint=pushed["fingerprints"][sid], status=status,
                      actual="实际结果 " + sid, executor="Li Si", executed_at="2026-10-18T10:00:00+08:00", evidence=[evidence])
        if status == "blocked":
            result["finding"] = "测试环境不可用"
        results.append(result)
    body = dict(idempotency_key="five-agent", scenario_file_sha256=hashlib.sha256(pushed["content"].encode()).hexdigest(),
                product=dict(commit="c0ffee1", digest="D1", dirty=False), submitted_by="开发岗", results=results)
    status, data, _ = board.request("POST", P5_API + "/rounds", body)
    assert status == 200, data
    for sid, status, extra in [(P5_IDS[0], "pass", {}),
                              (P5_IDS[1], "fail", dict(defect_url="ABC-1", note="复现两次"))]:
        status_code, data, _ = board.request("POST", P5_API + "/marks", seven_mark(pushed, sid, status, **extra))
        assert status_code == 200, data
    upload_cases(board, flow_cases(P5_IDS[:3] + ["TC_DEMO_FLOW_009"]), base=P5_API)
    return pushed
