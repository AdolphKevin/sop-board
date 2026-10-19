"""写入正文的字段校验，不产生持久副作用。"""

from datetime import datetime, timedelta, timezone
import re

from .errors import APIError


def nonempty(value):
    return isinstance(value, str) and bool(value.strip())


class Problems:
    def __init__(self, code):
        self.code = code
        self.items = []

    def add(self, field, message):
        self.items.append(dict(field=field, problem=message))

    def text(self, value, field):
        if not nonempty(value):
            self.add(field, "应为非空字符串")

    def finish(self):
        if self.items:
            raise APIError(422, self.code, self.items)


def validate_push(body):
    p = Problems("scenario-file-invalid")
    if not isinstance(body, dict):
        p.add("body", "应为对象")
    else:
        for key in ("path", "commit", "product_digest", "content", "pushed_by"):
            p.text(body.get(key), key)
        if type(body.get("dirty")) is not bool:
            p.add("dirty", "应为布尔值")
        project = body.get("project")
        if not isinstance(project, dict):
            p.add("project", "应含 id 与 name 的对象")
        else:
            for key in ("id", "name"):
                p.text(project.get(key), "project." + key)
        fingerprints = body.get("fingerprints")
        if not isinstance(fingerprints, dict) or not all(isinstance(v, str) for v in fingerprints.values()):
            p.add("fingerprints", "应为场景编号到指纹字符串的对象")
    for item in p.items:
        item["line"] = 1
    p.finish()


def validate_evidence(db, evidence, p, field):
    if not isinstance(evidence, list):
        p.add(field, "应为证据数组")
        return
    for index, item in enumerate(evidence):
        prefix = f"{field}[{index}]"
        if not isinstance(item, dict):
            p.add(prefix, "应含 name 与 sha256 的对象")
            continue
        p.text(item.get("name"), prefix + ".name")
        sha = item.get("sha256")
        if not isinstance(sha, str) or not re.fullmatch("[a-f0-9]{64}", sha) or not db.execute(
                "SELECT 1 FROM evidence WHERE sha256=?", (sha,)).fetchone():
            p.add(prefix + ".sha256", "evidence-missing：证据尚未上传")


def validate_round(db, body):
    p = Problems("round-invalid")
    if not isinstance(body, dict):
        p.add("body", "应为对象")
        p.finish()
    for key in ("idempotency_key", "scenario_file_sha256", "submitted_by"):
        p.text(body.get(key), key)
    if "implementer" in body:
        p.text(body["implementer"], "implementer")
    product = body.get("product")
    if not isinstance(product, dict):
        p.add("product", "应为对象")
    else:
        for key in ("commit", "digest"):
            p.text(product.get(key), "product." + key)
        if type(product.get("dirty")) is not bool:
            p.add("product.dirty", "应为布尔值")
    results = body.get("results")
    if not isinstance(results, list) or not results:
        p.add("results", "应为非空结果数组")
        p.finish()
    seen = set()
    for index, row in enumerate(results):
        field = f"results[{index}]"
        if not isinstance(row, dict):
            p.add(field, "应为结果对象")
            continue
        for key in ("scenario", "fingerprint", "actual", "executor"):
            p.text(row.get(key), field + "." + key)
        if isinstance(row.get("scenario"), str):
            if row["scenario"] in seen:
                p.add(field + ".scenario", "同轮场景重复：" + row["scenario"])
            seen.add(row["scenario"])
        status = row.get("status")
        if status not in ("pass", "fail", "blocked", "skip"):
            p.add(field + ".status", "应为 pass、fail、blocked 或 skip")
        evidence = row.get("evidence")
        if status in ("pass", "fail") and isinstance(evidence, list) and not evidence:
            p.add(field + ".evidence", "pass 与 fail 至少需要一份证据")
        validate_evidence(db, evidence, p, field + ".evidence")
        if status in ("blocked", "skip"):
            p.text(row.get("finding"), field + ".finding")
        try:
            executed_at = row.get("executed_at")
            if not isinstance(executed_at, str):
                raise ValueError()
            parsed = datetime.fromisoformat(executed_at.replace("Z", "+00:00"))
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                raise ValueError()
            if parsed > datetime.now(timezone.utc) + timedelta(minutes=5):
                raise ValueError()
        except (ValueError, OverflowError):
            p.add(field + ".executed_at", "应为带时区的时间，不能晚于当前时间五分钟")
        for key in ("session", "finding", "note"):
            if key in row and not isinstance(row[key], str):
                p.add(field + "." + key, "应为字符串")
    p.finish()


def validate_mark(db, body):
    p = Problems("mark-invalid")
    if not isinstance(body, dict):
        p.add("body", "应为对象")
        p.finish()
    for key in ("scenario", "fingerprint", "actual", "marker", "environment"):
        p.text(body.get(key), key)
    if body.get("status") not in ("pass", "fail", "blocked", "skip"):
        p.add("status", "应为 pass、fail、blocked 或 skip")
    if body.get("status") in ("blocked", "skip"):
        p.text(body.get("finding"), "finding")
    if "evidence" in body:
        validate_evidence(db, body["evidence"], p, "evidence")
    for key in ("defect_url", "finding", "note"):
        if key in body and not isinstance(body[key], str):
            p.add(key, "应为字符串")
    p.finish()
