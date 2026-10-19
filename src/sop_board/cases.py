"""测试岗用例格式与 YAML 原文位置；核对阶段不产生持久副作用。"""

from datetime import date
import re

import yaml

from .errors import APIError


class CasesLoader(yaml.SafeLoader):
    pass


# 日期保留原文字符串，下载时仍满足测试岗的 YYYY-MM-DD 格式。
CasesLoader.add_constructor("tag:yaml.org,2002:timestamp", lambda loader, node: loader.construct_scalar(node))


def syntax_problem(error):
    mark = getattr(error, "problem_mark", None)
    reason = getattr(error, "problem", None) or str(error)
    return dict(field="content", line=mark.line + 1 if mark else None,
                problem=f"YAML 语法错误：{reason}（列 {mark.column + 1 if mark else '未知'}）")


def load_cases(body):
    problems, lines = [], {(): None}

    def add(path, message, line=None):
        parent = path
        while parent not in lines:
            parent = parent[:-1]
        problems.append(dict(field=".".join(map(str, path)) or "content",
                             line=lines[parent] if line is None else line, problem=message))

    def finish():
        if problems:
            problems.sort(key=lambda item: (item["line"] or 0, item["field"]))
            raise APIError(422, "cases-invalid", problems)

    if not isinstance(body, dict):
        add(("body",), "应为对象")
        finish()
    for key in ("filename", "uploaded_by"):
        if not isinstance(body.get(key), str) or not body[key].strip():
            add((key,), "应为非空字符串")
    if not isinstance(body.get("content"), str):
        add(("content",), "应为 YAML 原文字符串")
    finish()
    raw = body["content"]
    try:
        for token in yaml.scan(raw):
            if isinstance(token, (yaml.tokens.AnchorToken, yaml.tokens.AliasToken, yaml.tokens.TagToken)):
                add((), "不支持锚点、别名或显式标签", token.start_mark.line + 1)
        finish()
        root = yaml.compose(raw, Loader=CasesLoader)
        if root is None or not isinstance(root, yaml.MappingNode):
            add((), "内容不能为空，顶层应为对象")
            finish()

        def inspect(node, path=(), depth=0):
            if depth > 100:
                add(path, "嵌套过深")
                return
            if isinstance(node, yaml.MappingNode):
                seen = set()
                for key, child in node.value:
                    if key.tag != "tag:yaml.org,2002:str":
                        add(path, "字段名应为字符串", key.start_mark.line + 1)
                        continue
                    p = path + (key.value,)
                    lines[p] = key.start_mark.line + 1
                    if key.value in seen:
                        add(p, "重复字段：" + key.value)
                    seen.add(key.value)
                    inspect(child, p, depth + 1)
            elif isinstance(node, yaml.SequenceNode):
                for index, child in enumerate(node.value):
                    p = path + (index,)
                    lines[p] = child.start_mark.line + 1
                    inspect(child, p, depth + 1)
            elif node.tag not in {"tag:yaml.org,2002:" + kind for kind in ("str", "int", "float", "bool", "null", "timestamp")}:
                add(path, "不支持的数据类型")

        inspect(root)
        finish()
        value = yaml.load(raw, Loader=CasesLoader)
    except yaml.YAMLError as error:
        raise APIError(422, "cases-invalid", [syntax_problem(error)]) from error

    def shape(item, path, required, optional=()):
        if not isinstance(item, dict):
            add(path, "应为对象")
            return False
        for key in sorted(set(required) - item.keys()):
            add(path + (key,), "缺少字段：" + key)
        for key in sorted(item.keys() - set(required) - set(optional)):
            add(path + (key,), "格式外字段：" + key)
        return True

    def string(item, path, nonempty=False):
        if not isinstance(item, str) or (nonempty and not item.strip()):
            add(path, "应为非空字符串" if nonempty else "应为字符串")

    def strings(item, path, nonempty=False, nonempty_items=False):
        if not isinstance(item, list) or (nonempty and not item):
            add(path, "应为非空字符串数组" if nonempty else "应为字符串数组")
            return
        for index, child in enumerate(item):
            string(child, path + (index,), nonempty_items)

    # 缺少任一顶层字段时为文件级错误，无可对应的行号。
    shape(value, (), {"metadata", "test_cases"})
    if "metadata" not in value or "test_cases" not in value:
        finish()
    meta = value["metadata"]
    if shape(meta, ("metadata",), {"module", "feature", "owner", "last_reviewed"}, {"tags"}):
        for key in ("module", "feature", "owner"):
            if key in meta:
                string(meta[key], ("metadata", key), True)
        if "last_reviewed" in meta:
            reviewed = meta["last_reviewed"]
            try:
                if not isinstance(reviewed, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", reviewed):
                    raise ValueError()
                date.fromisoformat(reviewed)
            except ValueError:
                add(("metadata", "last_reviewed"), "应为有效日期 YYYY-MM-DD")
        if "tags" in meta:
            strings(meta["tags"], ("metadata", "tags"))
    cases = value["test_cases"]
    if not isinstance(cases, list) or not cases:
        add(("test_cases",), "应为非空用例数组")
    else:
        seen = set()
        for index, item in enumerate(cases):
            p = ("test_cases", index)
            if not shape(item, p, {"id", "title", "priority", "type", "steps", "expected_results", "tags"},
                         {"description", "preconditions", "auto"}):
                continue
            if "id" in item:
                identifier = item["id"]
                if not isinstance(identifier, str) or not re.fullmatch(r"TC_[A-Z0-9_]{3,}_[0-9]{3}", identifier):
                    add(p + ("id",), "编号应匹配 ^TC_[A-Z0-9_]{3,}_[0-9]{3}$")
                if isinstance(identifier, str):
                    if identifier in seen:
                        add(p + ("id",), "重复用例编号：" + identifier)
                    seen.add(identifier)
            if "title" in item:
                string(item["title"], p + ("title",), True)
            for key, choices in [("priority", ("P0", "P1", "P2")),
                                 ("type", ("functional", "ui", "security", "performance", "accessibility"))]:
                if key in item and item[key] not in choices:
                    add(p + (key,), "应为 " + "、".join(choices))
            for key in ("steps", "expected_results", "tags"):
                if key in item:
                    strings(item[key], p + (key,), True, True)
            if "description" in item:
                string(item["description"], p + ("description",))
            if "preconditions" in item:
                strings(item["preconditions"], p + ("preconditions",))
            if "auto" in item and type(item["auto"]) is not bool:
                add(p + ("auto",), "应为布尔值")
    finish()
    return value
