"""证据下载与详情预览使用相同的文本类型判断。"""

import mimetypes
from pathlib import Path


def content_type(name):
    suffix = Path(name).suffix.lower()
    if suffix in (".webm", ".mp4"):
        return "video/webm" if suffix == ".webm" else "video/mp4"
    if suffix == ".ndjson":
        return "application/x-ndjson"
    if suffix in (".md", ".log"):
        return "text/markdown" if suffix == ".md" else "text/plain"
    if suffix in (".yaml", ".yml"):
        return "application/yaml"
    if suffix == ".jsonl":
        return "application/x-ndjson"
    if suffix in (".har",):
        return "application/json"
    if suffix in (".diff", ".patch", ".toml", ".ini", ".cfg", ".env", ".sql", ".sh", ".py", ".go", ".ts", ".js", ".mjs"):
        return "text/plain"
    return mimetypes.guess_type(name)[0] or "application/octet-stream"


def is_text(value):
    return value.startswith("text/") or value in (
        "application/json", "application/x-ndjson", "application/xml", "application/yaml", "application/x-yaml")


def video_response(raw, range_header=None):
    """视频提供单段字节范围，支持浏览器预加载和拖动播放进度。"""
    headers = [("Accept-Ranges", "bytes")]
    if not range_header:
        return 200, raw, headers
    import re
    match = re.fullmatch(r"bytes=([0-9]*)-([0-9]*)", range_header.strip()) if len(range_header) <= 128 else None
    size = len(raw)
    if match and size and any(match.groups()):
        first, last = match.groups()
        if first:
            start, end = int(first), min(int(last), size - 1) if last else size - 1
        else:
            count = int(last)
            start, end = max(0, size - count), size - 1
        if 0 <= start <= end < size:
            return 206, raw[start:end + 1], headers + [("Content-Range", f"bytes {start}-{end}/{size}")]
    return 416, b"", headers + [("Content-Range", f"bytes */{size}")]
