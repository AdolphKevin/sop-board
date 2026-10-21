"""写入正文的字段校验，不产生持久副作用。"""

from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import PurePosixPath
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


def validate_batch(name):
    if not name or name in ('.', '..') or any(c in name for c in ('/', '\\', '\0')):
        raise APIError(422, 'batch-invalid', [dict(field='batch', problem='批次名称不能含路径分隔符')])


def validate_sop_upload(body):
    p = Problems('sop-invalid')
    if not isinstance(body, dict):
        p.add('body', '应为对象')
        p.finish()
    for key in ('filename', 'content', 'uploaded_by', 'confirmed_by'):
        p.text(body.get(key), key)
    filename = body.get('filename')
    if isinstance(filename, str) and not filename.lower().endswith(('.md', '.txt')):
        p.add('filename', '只接受 Markdown（.md）或纯文本（.txt）文件')
    content = body.get('content')
    if isinstance(content, str):
        try:
            if len(content.encode('utf-8')) > 1024 * 1024:
                p.add('content', 'SOP 原文不能超过 1 MiB')
        except UnicodeError:
            p.add('content', 'SOP 原文必须为 UTF-8')
    base = body.get('base_sha256')
    if not isinstance(base, str) or base and not re.fullmatch('[a-f0-9]{64}', base):
        p.add('base_sha256', '应为当前文件的 SHA-256 摘要，文件不存在时为空串')
    p.finish()


def validate_sop_sources(sources, p):
    if not isinstance(sources, list):
        p.add('sop_sources', '应为 SOP 原文数组')
    else:
        seen = set()
        required = {'ref', 'path', 'commit', 'sha256', 'confirmed_by', 'content'}
        for index, source in enumerate(sources):
            field = f'sop_sources[{index}]'
            if not isinstance(source, dict):
                p.add(field, '应为 SOP 原文对象')
                continue
            for key in required - {'content'}:
                p.text(source.get(key), field + '.' + key)
            for key in source.keys() - required:
                p.add(field + '.' + key, '格式外字段')
            path = source.get('path')
            if isinstance(path, str):
                if '\0' in path or not PurePosixPath(path).parts or PurePosixPath(path).is_absolute() or '..' in PurePosixPath(path).parts or path in seen:
                    p.add(field + '.path', '应为不重复且不含 .. 的仓库内相对路径')
                seen.add(path)
            content = source.get('content')
            if not isinstance(content, str):
                p.add(field + '.content', '应为 Markdown 原文字符串')
            elif len(content.encode()) > 2 * 1024 * 1024:
                p.add(field + '.content', 'SOP 原文不能超过 2 MiB')
            elif hashlib.sha256(content.encode()).hexdigest() != source.get('sha256'):
                p.add(field + '.sha256', '与 SOP 原文实际摘要不一致')


def validate_push(body):
    p = Problems("scenario-file-invalid")
    if not isinstance(body, dict):
        p.add("body", "应为对象")
    else:
        for key in ("path", "commit", "product_digest", "content", "pushed_by"):
            p.text(body.get(key), key)
        if type(body.get("dirty")) is not bool:
            p.add("dirty", "应为布尔值")
        workspace = body.get("workspace")
        if not isinstance(workspace, dict):
            p.add("workspace", "应含 name 的对象")
        else:
            for extra in workspace.keys() - {"name"}:
                p.add("workspace." + str(extra), "格式外字段")
            for key in ("name",):
                p.text(workspace.get(key), "workspace." + key)
        fingerprints = body.get("fingerprints")
        if not isinstance(fingerprints, dict) or not all(isinstance(v, str) for v in fingerprints.values()):
            p.add("fingerprints", "应为场景编号到指纹字符串的对象")
        if "entries" in body:
            p.add("entries", "入口只从工作区 config.yaml 读取")
        if 'sop_sources' in body:
            validate_sop_sources(body['sop_sources'], p)
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


def validate_mark(body):
    p = Problems("mark-invalid")
    if not isinstance(body, dict):
        p.add("body", "应为对象")
        p.finish()
    allowed = {"scenario", "fingerprint", "status", "marker", "defect_url", "note"}
    for field in sorted(body.keys() - allowed):
        p.add(field, "人工标记不接受此字段")
    for key in ("scenario", "fingerprint", "marker"):
        p.text(body.get(key), key)
    status = body.get("status")
    if status not in ("pass", "fail", "blocked"):
        p.add("status", "应为 pass、fail 或 blocked")
    if status == "pass" and "defect_url" in body:
        p.add("defect_url", "通过标记不接受缺陷链接")
    if status in ("fail", "blocked"):
        from .defect_links import normalize
        normalize(body.get("defect_url"))
    if "note" in body and not isinstance(body["note"], str):
        p.add("note", "应为字符串")
    p.finish()
