"""场景文件与测试用例文件：严格读取、逐字段校验、指纹，以及从检出目录现读场景。

场景文件在产品仓库中是唯一真相；看板只读取，从不回写。
"""
from datetime import date
import hashlib
import json
import math
from pathlib import Path
import re
import threading

import yaml

from .errors import APIError

NEGATIVE_TYPES = tuple('empty-input over-length not-found permission cross-tenant duplicate concurrency disconnect '
                       'timeout archived invalid-input incomplete-input interrupted stale-input tampered-input other'.split())
CONTENT_FIELDS = ('initial_state', 'steps', 'expected', 'forbidden_effects')
GUARD_INDEX = 'regression.yaml'
STATUSES = ('draft', 'confirmed')


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'))


def fingerprint(scene):
    value = {key: [text.replace('\r\n', '\n').replace('\r', '\n') for text in scene[key]] for key in CONTENT_FIELDS}
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def key(file, scenario):
    return f'{file}#{scenario}'


def split_key(value):
    file, separator, scenario = value.partition('#')
    if not separator or not file or not scenario:
        raise ValueError(f'场景须写成 <文件名>#<场景编号>：{value}')
    return file, scenario


def syntax_problem(error):
    mark = getattr(error, 'problem_mark', None)
    reason = getattr(error, 'problem', None) or str(error)
    return dict(field='content', line=mark.line + 1 if mark else None,
                problem=f"YAML 语法错误：{reason}（列 {mark.column + 1 if mark else '未知'}）")


class _Checker:
    """记录字段路径到原文行号，问题带行号返回。"""

    def __init__(self, code):
        self.code, self.lines, self.problems = code, {(): 1}, []

    def add(self, path, message, line=None):
        parent = path
        while parent not in self.lines:
            parent = parent[:-1]
        self.problems.append(dict(field='.'.join(map(str, path)) or 'content', problem=message,
                                  line=self.lines[parent] if line is None else line))

    def finish(self):
        if self.problems:
            self.problems.sort(key=lambda item: (item['line'] or 0, item['field']))
            raise APIError(422, self.code, self.problems)

    def compose(self, raw, loader, scalar_tags):
        try:
            for token in yaml.scan(raw):
                if isinstance(token, (yaml.tokens.AnchorToken, yaml.tokens.AliasToken, yaml.tokens.TagToken)):
                    self.add((), '只接受 JSON 数据类型的 YAML，不支持锚点、别名或显式标签', token.start_mark.line + 1)
            self.finish()
            root = yaml.compose(raw, Loader=loader)
        except yaml.YAMLError as error:
            raise APIError(422, self.code, [syntax_problem(error)]) from error
        if root is None or not isinstance(root, yaml.MappingNode):
            self.add((), '内容不能为空，顶层应为对象')
            self.finish()

        def inspect(node, path=(), depth=0):
            self.lines[path] = node.start_mark.line + 1
            if depth > 100:
                self.add(path, '嵌套过深')
                return
            if isinstance(node, yaml.MappingNode):
                seen = set()
                for name, child in node.value:
                    if name.tag != 'tag:yaml.org,2002:str':
                        self.add(path, '字段名应为字符串', name.start_mark.line + 1)
                        continue
                    child_path = path + (name.value,)
                    self.lines[child_path] = name.start_mark.line + 1
                    if name.value in seen:
                        self.add(child_path, '重复字段：' + name.value)
                    seen.add(name.value)
                    inspect(child, child_path, depth + 1)
            elif isinstance(node, yaml.SequenceNode):
                for index, child in enumerate(node.value):
                    inspect(child, path + (index,), depth + 1)
            elif node.tag not in {'tag:yaml.org,2002:' + kind for kind in scalar_tags}:
                self.add(path, '不支持的数据类型')

        inspect(root)
        self.finish()
        try:
            return yaml.load(raw, Loader=loader)
        except yaml.YAMLError as error:
            raise APIError(422, self.code, [syntax_problem(error)]) from error

    def shape(self, item, path, required, optional=()):
        if not isinstance(item, dict):
            self.add(path, '应为对象')
            return False
        for name in sorted(set(required) - item.keys()):
            self.add(path + (name,), '缺少字段：' + name)
        for name in sorted(item.keys() - set(required) - set(optional)):
            self.add(path + (name,), '格式外字段：' + name)
        return True

    def text(self, item, path):
        if not isinstance(item, str) or not item.strip():
            self.add(path, '应为非空字符串')

    def array(self, item, path, nonempty=False):
        if not isinstance(item, list) or (nonempty and not item):
            self.add(path, '应为非空数组' if nonempty else '应为数组')
            return []
        return item

    def texts(self, item, path, nonempty=False):
        for index, child in enumerate(self.array(item, path, nonempty)):
            self.text(child, path + (index,))


def load_scenarios(raw, entries=None):
    """校验场景文件原文并返回文档。entries 为入口名称集合时核对 verify 引用。"""
    check = _Checker('scenario-file-invalid')
    value = check.compose(raw, yaml.SafeLoader, ('str', 'int', 'float', 'bool', 'null'))

    def finite(item, path=()):
        if isinstance(item, float) and not math.isfinite(item):
            check.add(path, '数字必须为有限值')
        elif isinstance(item, dict):
            for name, child in item.items():
                finite(child, path + (name,))
        elif isinstance(item, list):
            for index, child in enumerate(item):
                finite(child, path + (index,))
    finite(value)
    if check.shape(value, (), {'metadata', 'sops'}):
        meta = value.get('metadata')
        if check.shape(meta, ('metadata',), {'requirement', 'version', 'owner', 'sources'}, {'status', 'tags'}):
            for name in ('requirement', 'owner'):
                check.text(meta.get(name), ('metadata', name))
            if type(meta.get('version')) not in (int, float):
                check.text(meta.get('version'), ('metadata', 'version'))
            if 'status' in meta and meta['status'] not in STATUSES:
                check.add(('metadata', 'status'), '应为 draft 或 confirmed')
            for index, source in enumerate(check.array(meta.get('sources'), ('metadata', 'sources'), True)):
                path = ('metadata', 'sources', index)
                if check.shape(source, path, {'ref', 'version', 'confirmed_by'}):
                    for name in ('ref', 'version', 'confirmed_by'):
                        if name != 'version' or type(source.get(name)) not in (int, float):
                            check.text(source.get(name), path + (name,))
            if 'tags' in meta:
                check.texts(meta['tags'], ('metadata', 'tags'))
        nodes, scenarios = set(), set()
        for i, node in enumerate(check.array(value.get('sops'), ('sops',), True)):
            path = ('sops', i)
            if not check.shape(node, path, {'id', 'title', 'actor', 'entry', 'scenarios'}, {'must', 'negative_exempt'}):
                continue
            for name in ('id', 'title', 'actor', 'entry'):
                check.text(node.get(name), path + (name,))
            identifier = node.get('id')
            if isinstance(identifier, str):
                if identifier in nodes:
                    check.add(path + ('id',), '重复节点编号：' + identifier)
                nodes.add(identifier)
            if 'must' in node:
                check.texts(node['must'], path + ('must',), True)
            if 'negative_exempt' in node:
                check.text(node['negative_exempt'], path + ('negative_exempt',))
            polarities = set()
            for j, row in enumerate(check.array(node.get('scenarios'), path + ('scenarios',), True)):
                p = path + ('scenarios', j)
                if not check.shape(row, p, {'id', 'polarity', 'title', 'actor', 'priority', 'verify', *CONTENT_FIELDS},
                                   {'negative_type', 'refs', 'evidence_hints', 'tags'}):
                    continue
                for name in ('id', 'title', 'actor', 'priority', 'verify'):
                    check.text(row.get(name), p + (name,))
                if isinstance(row.get('id'), str):
                    if '#' in row['id'] or not row['id'].strip():
                        check.add(p + ('id',), '场景编号不能含 #')
                    if row['id'] in scenarios:
                        check.add(p + ('id',), '重复场景编号：' + row['id'])
                    scenarios.add(row['id'])
                if entries is not None and isinstance(row.get('verify'), str) and row['verify'] not in entries:
                    check.add(p + ('verify',), '未声明的入口：' + row['verify'])
                polarity = row.get('polarity')
                if polarity not in ('positive', 'negative'):
                    check.add(p + ('polarity',), '应为 positive 或 negative')
                else:
                    polarities.add(polarity)
                if polarity == 'negative' or 'negative_type' in row:
                    if row.get('negative_type') not in NEGATIVE_TYPES:
                        check.add(p + ('negative_type',), '应为已定义的反向类型')
                for name in (*CONTENT_FIELDS, 'refs', 'evidence_hints', 'tags'):
                    if name in CONTENT_FIELDS or name in row:
                        check.texts(row.get(name), p + (name,), name in CONTENT_FIELDS[:3])
            if 'positive' not in polarities:
                check.add(path, f'节点 {identifier} 缺少正向场景')
            if 'negative' not in polarities and not (isinstance(node.get('negative_exempt'), str) and node['negative_exempt'].strip()):
                check.add(path, f'节点 {identifier} 缺少反向场景或 negative_exempt 理由')
    check.finish()
    return value


class _CasesLoader(yaml.SafeLoader):
    pass


# 日期保留原文字符串，导出时仍是 YYYY-MM-DD。
_CasesLoader.add_constructor('tag:yaml.org,2002:timestamp', lambda loader, node: loader.construct_scalar(node))


def load_cases(raw):
    """校验测试同学的用例文件原文（metadata + test_cases）。"""
    check = _Checker('cases-invalid')
    value = check.compose(raw, _CasesLoader, ('str', 'int', 'float', 'bool', 'null', 'timestamp'))
    check.shape(value, (), {'metadata', 'test_cases'})
    if 'metadata' not in value or 'test_cases' not in value:
        check.finish()
    meta = value['metadata']
    if check.shape(meta, ('metadata',), {'module', 'feature', 'owner', 'last_reviewed'}, {'tags'}):
        for name in ('module', 'feature', 'owner'):
            if name in meta:
                check.text(meta[name], ('metadata', name))
        reviewed = meta.get('last_reviewed')
        try:
            if not isinstance(reviewed, str) or not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', reviewed):
                raise ValueError()
            date.fromisoformat(reviewed)
        except ValueError:
            check.add(('metadata', 'last_reviewed'), '应为有效日期 YYYY-MM-DD')
        if 'tags' in meta:
            check.texts(meta['tags'], ('metadata', 'tags'))
    seen = set()
    for index, item in enumerate(check.array(value['test_cases'], ('test_cases',), True)):
        p = ('test_cases', index)
        if not check.shape(item, p, {'id', 'title', 'priority', 'type', 'steps', 'expected_results', 'tags'},
                           {'description', 'preconditions', 'auto'}):
            continue
        identifier = item.get('id')
        if not isinstance(identifier, str) or not re.fullmatch(r'TC_[A-Z0-9_]{3,}_[0-9]{3}', identifier):
            check.add(p + ('id',), '编号应匹配 ^TC_[A-Z0-9_]{3,}_[0-9]{3}$')
        elif identifier in seen:
            check.add(p + ('id',), '重复用例编号：' + identifier)
        seen.add(identifier)
        check.text(item.get('title'), p + ('title',))
        for name, choices in (('priority', ('P0', 'P1', 'P2')),
                              ('type', ('functional', 'ui', 'security', 'performance', 'accessibility'))):
            if item.get(name) not in choices:
                check.add(p + (name,), '应为 ' + '、'.join(choices))
        for name in ('steps', 'expected_results', 'tags'):
            check.texts(item.get(name), p + (name,), True)
        if 'description' in item and not isinstance(item['description'], str):
            check.add(p + ('description',), '应为字符串')
        if 'preconditions' in item:
            check.texts(item['preconditions'], p + ('preconditions',))
        if 'auto' in item and type(item['auto']) is not bool:
            check.add(p + ('auto',), '应为布尔值')
    check.finish()
    return value


_cache = {}
_cache_lock = threading.Lock()


def read_file(path, entries=None):
    """读取一个场景文件；按路径、修改时间与大小缓存。返回 (文档, 问题列表, 原文摘要)。"""
    path = Path(path)
    stat = path.stat()
    marker = (str(path), stat.st_mtime_ns, stat.st_size, tuple(sorted(entries or ())))
    with _cache_lock:
        cached = _cache.get(str(path))
        if cached and cached[0] == marker:
            return cached[1]
    raw = path.read_bytes()
    sha = hashlib.sha256(raw).hexdigest()
    try:
        document = load_scenarios(raw.decode('utf-8'), entries)
        result = (document, [], sha)
    except UnicodeDecodeError:
        result = (None, [dict(field='content', problem='场景文件不是 UTF-8 文本', line=None)], sha)
    except APIError as error:
        result = (None, error.problems, sha)
    with _cache_lock:
        _cache[str(path)] = (marker, result)
    return result


def scan(directory, entries=None):
    """读取检出中的全部场景文件。

    返回 dict(files={文件名: {...}}, scenarios={场景键: scene})；
    scene 带 file、node、node_title、fingerprint、status（文件的 draft/confirmed）。"""
    directory = Path(directory)
    files, scenes = {}, {}
    if not directory.is_dir():
        return dict(files=files, scenarios=scenes)
    for path in sorted(directory.glob('*.yaml')):
        if path.name == GUARD_INDEX or path.is_symlink() or not path.is_file():
            continue
        stem = path.stem
        document, problems, sha = read_file(path, entries)
        info = dict(name=stem, path=path, sha256=sha, problems=problems)
        if document is not None:
            meta = document['metadata']
            status = meta.get('status', 'confirmed')
            info.update(requirement=meta['requirement'], version=meta['version'], status=status,
                        sources=meta['sources'], nodes=[])
            for node in document['sops']:
                ids = []
                for scene in node['scenarios']:
                    item = dict(scene, file=stem, node=node['id'], node_title=node['title'],
                                fingerprint=fingerprint(scene), status=status)
                    scenes[key(stem, scene['id'])] = item
                    ids.append(scene['id'])
                info['nodes'].append(dict(id=node['id'], title=node['title'], actor=node['actor'],
                                          entry=node['entry'], scenarios=ids))
        files[stem] = info
    return dict(files=files, scenarios=scenes)


def resolve(scope, scenes):
    """把用户给出的场景（场景键，或在全部文件中唯一的场景编号）解析为场景键。"""
    by_id = {}
    for item in scenes.values():
        by_id.setdefault(item['id'], []).append(key(item['file'], item['id']))
    result, problems = [], []
    for value in scope:
        if '#' in value:
            if value in scenes:
                result.append(value)
            else:
                problems.append(f'场景不存在：{value}')
        elif len(by_id.get(value, [])) == 1:
            result.append(by_id[value][0])
        elif value in by_id:
            problems.append(f'场景编号 {value} 在多个文件中出现，请写成 <文件名>#{value}')
        else:
            problems.append(f'场景不存在：{value}')
    if problems:
        raise ValueError('；'.join(problems))
    return list(dict.fromkeys(result))
