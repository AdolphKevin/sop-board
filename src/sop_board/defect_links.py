"""缺陷链接可选；填写时保留提交原文，只去掉首尾空白并限制长度。"""

from .errors import reject


def normalize(value, field="defect"):
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 2000:
        reject(422, "defect-invalid", field, "缺陷链接须为文字，最长 2000 字")
    return value.strip() or None
