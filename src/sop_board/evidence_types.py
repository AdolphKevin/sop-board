"""证据下载与详情预览使用相同的文本类型判断。"""

import mimetypes
from pathlib import Path


def content_type(name):
    suffix = Path(name).suffix.lower()
    if suffix == ".ndjson":
        return "application/x-ndjson"
    if suffix in (".md", ".log"):
        return "text/markdown" if suffix == ".md" else "text/plain"
    return mimetypes.guess_type(name)[0] or "application/octet-stream"


def is_text(value):
    return value.startswith("text/") or value in (
        "application/json", "application/x-ndjson", "application/xml", "application/yaml", "application/x-yaml")
