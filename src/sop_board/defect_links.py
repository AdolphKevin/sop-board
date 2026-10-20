"""缺陷文字只检查必填，始终保留提交原文。"""

from .errors import reject


ISSUE_KEY = r"[A-Z][A-Z0-9_]*-[0-9]+"


def normalize(value, field="defect_url"):
    if not isinstance(value, str) or not value.strip():
        reject(422, "defect-link-required", field, "不通过时必须填写非空文字的缺陷链接")
    return value
