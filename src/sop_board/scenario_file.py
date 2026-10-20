"""场景文件的安全读取、逐字段检查及原文行号。"""

import math

import yaml

from .errors import APIError
from .cases import syntax_problem


NEGATIVE_TYPES = set("empty-input over-length not-found permission cross-tenant duplicate concurrency disconnect timeout archived invalid-input incomplete-input interrupted stale-input tampered-input other".split())
CONTENT_FIELDS = ("initial_state", "steps", "expected", "forbidden_effects")


def load_scenarios(raw, entries=None, check_entry_references=True):
    lines = {(): 1}
    problems = []

    def add(path, message):
        parent = path
        while parent not in lines:
            parent = parent[:-1]
        problems.append(dict(field=".".join(map(str, path)) or "content",
                             problem=message, line=lines[parent]))

    try:
        for token in yaml.scan(raw):
            if isinstance(token, (yaml.tokens.AnchorToken, yaml.tokens.AliasToken, yaml.tokens.TagToken)):
                raise APIError(422, "scenario-file-invalid", [dict(field="content",
                    problem="仅接受 JSON 数据类型的 YAML，不支持锚点、别名或显式标签",
                    line=token.start_mark.line + 1)])
        root = yaml.compose(raw, Loader=yaml.SafeLoader)

        def inspect(node, path=(), depth=0):
            if node is None:
                return
            lines[path] = node.start_mark.line + 1
            if depth > 100:
                add(path, "嵌套过深")
                return
            if isinstance(node, yaml.MappingNode):
                keys = set()
                for key, value in node.value:
                    if key.tag != "tag:yaml.org,2002:str":
                        add(path, "对象键须为字符串")
                        continue
                    if key.value in keys:
                        add(path, "重复字段：" + key.value)
                    keys.add(key.value)
                    inspect(value, path + (key.value,), depth + 1)
            elif isinstance(node, yaml.SequenceNode):
                for index, value in enumerate(node.value):
                    inspect(value, path + (index,), depth + 1)
            elif node.tag not in {"tag:yaml.org,2002:" + t for t in ("str", "int", "float", "bool", "null")}:
                add(path, "仅接受 JSON 数据类型")

        inspect(root)
        if problems:
            raise APIError(422, "scenario-file-invalid", problems)
        value = yaml.safe_load(raw)
        # Reject non-finite JSON numbers, which PyYAML otherwise accepts.
        def finite(item):
            if isinstance(item, float) and not math.isfinite(item):
                add((), "数字必须为有限值")
            elif isinstance(item, dict):
                for child in item.values():
                    finite(child)
            elif isinstance(item, list):
                for child in item:
                    finite(child)
        finite(value)
    except yaml.YAMLError as error:
        raise APIError(422, "scenario-file-invalid", [syntax_problem(error)]) from error

    def shape(item, path, required, optional=()):
        if not isinstance(item, dict):
            add(path, "应为对象")
            return False
        for key in sorted(set(required) - item.keys()):
            add(path + (key,), "缺少字段：" + key)
        for key in sorted(item.keys() - set(required) - set(optional)):
            add(path + (key,), "格式外字段：" + key)
        return True

    def text(item, path):
        if not isinstance(item, str) or not item.strip():
            add(path, "应为非空字符串")

    def array(item, path, nonempty=False):
        if not isinstance(item, list) or (nonempty and not item):
            add(path, "应为非空数组" if nonempty else "应为数组")
            return []
        return item

    def texts(item, path, nonempty=False):
        for index, child in enumerate(array(item, path, nonempty)):
            text(child, path + (index,))

    if shape(value, (), {"metadata", "sops"}):
        meta = value.get("metadata")
        if shape(meta, ("metadata",), {"requirement", "batch", "version", "owner", "sources"}, {"tags", "repos"}):
            for key in ("requirement", "batch", "owner"):
                text(meta.get(key), ("metadata", key))
            if type(meta.get("version")) not in (int, float):
                text(meta.get("version"), ("metadata", "version"))
            for index, source in enumerate(array(meta.get("sources"), ("metadata", "sources"), True)):
                path = ("metadata", "sources", index)
                if shape(source, path, {"ref", "version", "confirmed_by"}):
                    for key in ("ref", "version", "confirmed_by"):
                        if key != "version" or type(source.get(key)) not in (int, float):
                            text(source.get(key), path + (key,))
            if "repos" in meta:
                texts(meta["repos"], ("metadata", "repos"), True)
            if "tags" in meta:
                texts(meta["tags"], ("metadata", "tags"))
        nodes, scenarios = set(), set()
        for i, node in enumerate(array(value.get("sops"), ("sops",), entries is not None)):
            path = ("sops", i)
            if not shape(node, path, {"id", "title", "actor", "entry", "scenarios"}, {"must", "negative_exempt"}):
                continue
            for key in ("id", "title", "actor", "entry"):
                text(node.get(key), path + (key,))
            identifier = node.get("id")
            if isinstance(identifier, str):
                if identifier in nodes:
                    add(path + ("id",), "重复节点编号：" + identifier)
                nodes.add(identifier)
            if "must" in node:
                texts(node["must"], path + ("must",), entries is not None)
            if "negative_exempt" in node:
                text(node["negative_exempt"], path + ("negative_exempt",))
            polarities = set()
            for j, row in enumerate(array(node.get("scenarios"), path + ("scenarios",))):
                p = path + ("scenarios", j)
                if not shape(row, p, {"id", "polarity", "title", "actor", "priority", "verify", *CONTENT_FIELDS},
                             {"negative_type", "refs", "evidence_hints", "tags"}):
                    continue
                for key in ("id", "title", "actor", "priority", "verify"):
                    text(row.get(key), p + (key,))
                if check_entry_references and entries is not None and isinstance(row.get("verify"), str) and row["verify"] not in entries:
                    add(p + ("verify",), f"未声明的入口：{row['verify']}")
                if isinstance(row.get("id"), str):
                    if row["id"] in scenarios:
                        add(p + ("id",), "重复场景编号：" + row["id"])
                    scenarios.add(row["id"])
                polarity = row.get("polarity")
                if polarity not in ("positive", "negative"):
                    add(p + ("polarity",), "应为 positive 或 negative")
                else:
                    polarities.add(polarity)
                if polarity == "negative" or "negative_type" in row:
                    if not isinstance(row.get("negative_type"), str) or row["negative_type"] not in NEGATIVE_TYPES:
                        add(p + ("negative_type",), "应为已定义的反向类型")
                for key in (*CONTENT_FIELDS, "refs", "evidence_hints", "tags"):
                    if key in CONTENT_FIELDS or key in row:
                        texts(row.get(key), p + (key,), key in CONTENT_FIELDS[:3])
            if "positive" not in polarities:
                add(path, f"节点 {identifier} 缺少正向场景")
            if "negative" not in polarities and not (isinstance(node.get("negative_exempt"), str) and node["negative_exempt"].strip()):
                add(path, f"节点 {identifier} 缺少反向场景或 negative_exempt 理由")
    if problems:
        raise APIError(422, "scenario-file-invalid", problems)
    return value
