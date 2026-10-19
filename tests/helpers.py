"""独立于服务实现的请求样例与内容指纹计算。"""

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json

import yaml


KEY = "github-com-demo-org-demo-app"
BASE = f"/api/v1/projects/{KEY}/batches/demo"
IDS = ["N1-P01", "N1-N01", "N2-P01", "N2-N01"]


def independent_fingerprint(scenario):
    # Build the canonical object's key order explicitly, independently of the
    # server's sorting implementation. Values remain ordered string arrays.
    parts = []
    for name in ("expected", "forbidden_effects", "initial_state", "steps"):
        strings = [item.replace("\r\n", "\n").replace("\r", "\n")
                   for item in scenario[name]]
        parts.append(json.dumps(name) + ":" + json.dumps(
            strings, ensure_ascii=False, separators=(",", ":")))
    return hashlib.sha256(("{" + ",".join(parts) + "}").encode()).hexdigest()


def demo():
    nodes = []
    for node in ("N1", "N2"):
        scenarios = []
        for polarity, suffix in (("positive", "P01"), ("negative", "N01")):
            scenario = dict(id=f"{node}-{suffix}", polarity=polarity,
                            title=f"{node} {polarity}", actor="执行者", priority="P0",
                            verify="自定义入口", initial_state=["初始\r\n状态"],
                            steps=["执行\r操作"], expected=[f"{node} 预期结果"],
                            forbidden_effects=[])
            if polarity == "negative":
                scenario["negative_type"] = "invalid-input"
            scenarios.append(scenario)
        nodes.append(dict(id=node, title=node, actor="执行者", entry="HTTP",
                          scenarios=scenarios))
    return dict(metadata=dict(requirement="演示需求", batch="demo", version=1,
                              owner="演示负责人", sources=[dict(ref="演示 SOP",
                              version="1", confirmed_by="业务负责人")]), sops=nodes)


def push_body(document=None, **overrides):
    document = deepcopy(document if document is not None else demo())
    body = dict(project=dict(id="github.com/demo-org/demo-app", name="演示项目"),
                path="demo.yaml", commit="c1", dirty=False, product_digest="product-1",
                content=yaml.safe_dump(document, allow_unicode=True, sort_keys=False),
                fingerprints={s["id"]: independent_fingerprint(s)
                              for node in document["sops"] for s in node["scenarios"]},
                pushed_by="开发岗", entries={"entries": {"自定义入口": dict(surface="http", operate="调用接口", observe=["响应"], evidence=["响应原文"], isolation="临时目录")}})
    body.update(overrides)
    return body


def push(board, body=None):
    body = body or push_body()
    status, response, _ = board.request("PUT", BASE + "/scenario-file", body)
    assert status == 200, response
    import os
    if board.env.get('PATH') == os.defpath:
        from test_writable_repos import wait_for
        wait_for(lambda: all(t['status'] not in ('排队中', '运行中') for t in
                             board.request('GET', '/api/v1/tasks', auth=False)[1].get('tasks', [])))
    return body, response


def upload(board, raw=b"test evidence", name="evidence.txt"):
    from urllib.parse import quote
    status, response, _ = board.request("POST", "/api/v1/evidence", raw,
                                      headers={"X-Evidence-Name": quote(name)})
    assert status == 200, response
    assert response == dict(ok=True, sha256=hashlib.sha256(raw).hexdigest(), size=len(raw))
    return dict(name=name, sha256=response["sha256"])


def round_body(pushed, evidence, statuses=("pass", "fail", "pass", "blocked"), key="run-1"):
    results = []
    for scenario, status in zip(IDS, statuses):
        results.append(dict(scenario=scenario, fingerprint=pushed["fingerprints"][scenario],
                            status=status, actual="核对实际结果", evidence=[deepcopy(evidence)],
                            executed_at=datetime.now(timezone.utc).isoformat(), executor="Li Si",
                            finding="Q1" if status in ("blocked", "skip") else ""))
    return dict(idempotency_key=key, scenario_file_sha256=hashlib.sha256(
                pushed["content"].encode()).hexdigest(), product=dict(commit="c1",
                digest="product-1", dirty=False), implementer="Zhang San",
                submitted_by="开发岗", results=results)


def assert_error(response, status, code):
    actual_status, body, _ = response
    assert actual_status == status, body
    assert body["ok"] is False and body["error_code"] == code
    assert body["problems"] and all("field" in p and "problem" in p for p in body["problems"])
    return body["problems"]


def snapshot(board):
    tables = [row[0] for row in board.sql(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    rows = {table: board.sql(f'SELECT * FROM "{table}"') for table in tables}
    files = {str(p.relative_to(board.data)): p.read_bytes()
             for p in (board.data / "evidence").rglob("*") if p.is_file()}
    return rows, files
