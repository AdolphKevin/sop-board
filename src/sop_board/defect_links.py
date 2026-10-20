"""按 Jira 配置和工作区的既有前缀核对新登记的缺陷链接。"""

import re

from .errors import reject


ISSUE_KEY = r"[A-Z][A-Z0-9_]*-[0-9]+"


def normalize(value, jira_url, prefix):
    value = value.strip()
    if jira_url:
        if re.fullmatch(ISSUE_KEY, value):
            return f"{jira_url}/browse/{value}"
        match = re.fullmatch(re.escape(jira_url) + r"/browse/(" + ISSUE_KEY + r")(?:[?#].*)?", value)
        if match:
            return f"{jira_url}/browse/{match[1]}"
    if prefix and value.casefold().startswith(prefix.casefold()):
        return value
    if not jira_url and not prefix:
        message = "请先设置工作区的缺陷链接前缀或配置 JIRA_URL；缺陷单号需填写完整链接"
    else:
        accepted = []
        if jira_url:
            accepted.append(f"Jira 单号（如 ABC-1）或 {jira_url}/browse/<单号>")
        if prefix:
            accepted.append(f"以 {prefix} 开头的完整链接（不区分大小写）")
        message = "接受的写法：" + "；".join(accepted)
        if not jira_url:
            message = "未配置 JIRA_URL，请填写完整链接。" + message
    reject(422, "defect-link-invalid", "defect_url", message)
