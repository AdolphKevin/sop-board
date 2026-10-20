"""测试岗结果文件的格式转换与完整校验；存储由 Service 的事务负责。"""

from datetime import datetime
import re

import yaml

from .defect_links import ISSUE_KEY


STATUS_OUT = {"pass": "passed", "fail": "failed", "blocked": "blocked", "skip": "skipped"}
STATUS_IN = {value: key for key, value in STATUS_OUT.items()}


def export_results(data, jira_url):
    scenarios = {item["scenario"]: item for item in data["summary"]["scenarios"]}
    results = []
    pairings = {p["case"]: p for p in data["pairings"]}
    for case in data["cases"]:
        pair = pairings.get(case["id"])
        scenario = scenarios.get(pair["scenarios"][0]) if pair else None
        if scenario is None:
            continue
        latest = max((r for r in [scenario["latest_agent"], scenario["latest_manual"]] if r),
                     key=lambda r: r["number"], default=None)
        if not latest:
            continue
        row = dict(case_id=case["id"], status=STATUS_OUT[latest["status"]], actual_result=latest["actual"],
                   executor=latest["executor"], tested_at=latest["executed_at"])
        finding = latest.get("finding") or ""
        jira = re.fullmatch(re.escape(jira_url) + r"/browse/(" + ISSUE_KEY + r")(?:[?#].*)?", finding) if jira_url else None
        notes = (["细节用例并入场景 " + scenario["scenario"] + "；此行引用该流程的结果"] if pair["kind"] == "detail" else [])
        if jira:
            row["jira_issue"] = jira[1]
        elif finding.startswith(("http://", "https://")):
            notes.append("缺陷链接 " + finding)
        if latest.get("note"):
            notes.append(latest["note"])
        if notes:
            row["notes"] = "；".join(notes)
        results.append(row)
    version = data["version"]
    document = dict(run_id=data["batch"]["name"] + "-" + datetime.now().astimezone().strftime("%Y%m%d%H%M%S"),
                    round=max((r["number"] for r in data["summary"]["rounds"]), default=0),
                    environment="sop-board 导出；产品提交 " + (version["commit_id"] if version else "无"), results=results)
    return yaml.safe_dump(document, allow_unicode=True, sort_keys=False).encode()


class ResultsLoader(yaml.SafeLoader):
    """时间保留原文；不接受会隐藏原始输入的重复键。"""

    def construct_mapping(self, node, deep=False):
        seen = set()
        for key, _ in node.value:
            if key.tag != "tag:yaml.org,2002:str" or key.value in seen:
                raise yaml.YAMLError("字段名须为字符串，且不能重复")
            seen.add(key.value)
        return super().construct_mapping(node, deep=deep)


ResultsLoader.add_constructor("tag:yaml.org,2002:timestamp", lambda loader, node: loader.construct_scalar(node))


def load_results(body, current, jira_url, prefix):
    from .defect_links import normalize
    from .errors import APIError
    from .validation import Problems, nonempty

    p = Problems("results-invalid")
    if not isinstance(body, dict):
        p.add("body", "应为对象")
        p.finish()
    for key in ("content", "uploaded_by"):
        p.text(body.get(key), key)
    for key in body.keys() - {"content", "uploaded_by"}:
        p.add(key, "格式外字段")
    p.finish()
    try:
        for token in yaml.scan(body["content"]):
            if isinstance(token, (yaml.tokens.AnchorToken, yaml.tokens.AliasToken, yaml.tokens.TagToken)):
                raise yaml.YAMLError("不支持锚点、别名或显式标签")
        doc = yaml.load(body["content"], Loader=ResultsLoader)
    except (yaml.YAMLError, RecursionError, ValueError) as error:
        mark = getattr(error, "problem_mark", None)
        reason = getattr(error, "problem", None) or str(error)
        location = f"（行 {mark.line + 1}，列 {mark.column + 1}）" if mark else ""
        p.add("content", "YAML 格式不合规：" + reason + location)
        p.finish()

    def shape(value, field, required, optional=()):
        if not isinstance(value, dict):
            p.add(field or "content", "应为对象")
            return False
        for key in sorted(set(required) - value.keys()):
            p.add((field + "." if field else "") + key, "缺少字段：" + key)
        for key in sorted(value.keys() - set(required) - set(optional)):
            p.add((field + "." if field else "") + key, "格式外字段：" + key)
        return True

    if not shape(doc, "", {"run_id", "round", "environment", "results"}):
        p.finish()
    for key in ("run_id", "environment"):
        if key in doc:
            p.text(doc[key], key)
    if "round" in doc and (type(doc["round"]) is not int or doc["round"] < 0):
        p.add("round", "应为非负整数")
    rows = doc.get("results")
    if not isinstance(rows, list):
        if "results" in doc:
            p.add("results", "应为结果数组")
        p.finish()
    prepared, skipped, seen = [], [], set()
    for index, row in enumerate(rows):
        field = f"results[{index}]"
        if not shape(row, field, {"case_id", "status", "actual_result", "executor", "tested_at"},
                     {"duration_ms", "evidence", "jira_issue", "notes"}):
            continue
        for key in ("case_id", "actual_result", "executor"):
            if key in row:
                p.text(row[key], field + "." + key)
        sid = row.get("case_id")
        if nonempty(sid):
            if sid not in current:
                p.add(field + ".case_id", "没有对应的当前场景：" + sid)
            if sid in seen:
                p.add(field + ".case_id", "重复用例编号：" + sid)
            seen.add(sid)
        status = row.get("status")
        if "status" in row and (not isinstance(status, str) or status not in STATUS_IN):
            p.add(field + ".status", "应为 passed、failed、blocked 或 skipped")
        executed_at = None
        if "tested_at" in row:
            try:
                value = row["tested_at"]
                if not isinstance(value, str) or len(value) <= 10 or value[10] not in "Tt ":
                    raise ValueError()
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                executed_at = (parsed.astimezone() if parsed.tzinfo is None else parsed).isoformat()
            except (ValueError, OverflowError, OSError):
                p.add(field + ".tested_at", "应为 ISO 8601 执行时间；未带时区时按看板本地时区解释")
        if "notes" in row and not isinstance(row["notes"], str):
            p.add(field + ".notes", "应为字符串")
        if "duration_ms" in row and (type(row["duration_ms"]) is not int or row["duration_ms"] < 0):
            p.add(field + ".duration_ms", "应为非负整数毫秒数")
        if "evidence" in row and (not isinstance(row["evidence"], list) or not all(nonempty(e) for e in row["evidence"])):
            p.add(field + ".evidence", "应为证据路径字符串数组")
        finding = None
        if status in ("failed", "blocked") and "jira_issue" not in row:
            p.add(field + ".jira_issue", "失败与受阻必须填写 jira_issue")
        if "jira_issue" in row:
            if not nonempty(row["jira_issue"]):
                p.add(field + ".jira_issue", "应为非空字符串")
            else:
                try:
                    finding = normalize(row["jira_issue"], jira_url, prefix)
                except APIError as error:
                    for problem in error.problems:
                        p.add(field + ".jira_issue", problem["problem"])
        if status == "skipped":
            skipped.append(sid)
        elif isinstance(status, str) and status in STATUS_IN and isinstance(sid, str) and sid in current:
            result = dict(scenario=sid, fingerprint=current[sid], status=STATUS_IN[status],
                          actual=row.get("actual_result"), executor=row.get("executor"), executed_at=executed_at, evidence=[])
            if finding:
                result["finding"] = finding
            if "notes" in row:
                result["note"] = row["notes"]
            prepared.append((result, row))
    p.finish()
    return doc, prepared, skipped
