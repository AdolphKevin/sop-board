"""交互地图：Web 入口的页面、控件、文案与定位方式，帮执行者操作；不提供预期。

地图放在产品仓库：<入口 spec_library 目录>/atlas/<入口>.json（结构见 prompts/atlas.schema.json），
同目录 <入口>.digests.json 记录登记时引用到的前端源码摘要；源码摘要变化的页面即过期。
"""
from importlib.resources import files
import hashlib
import json
from pathlib import Path
import re
from string import Template

from .errors import reject


SCHEMA = json.loads(files('sop_board').joinpath('prompts', 'atlas.schema.json').read_text(encoding='utf-8'))
SOURCE = re.compile(r'([a-z][a-z0-9-]*):([^:\n]+?)(?::([0-9]+))?\Z')


def schema_problems(value, schema=SCHEMA, path='$'):
    """校验随包模式用到的 JSON Schema 子集。"""
    problems = []
    kinds = schema.get('type')
    kinds = kinds if isinstance(kinds, list) else [kinds]
    valid = {'object': isinstance(value, dict), 'array': isinstance(value, list), 'string': isinstance(value, str),
             'integer': type(value) is int, 'boolean': type(value) is bool, 'null': value is None}
    if kinds != [None] and not any(valid.get(kind, False) for kind in kinds):
        return [f'{path} 类型应为 {"或".join(map(str, kinds))}']
    if 'enum' in schema and value not in schema['enum']:
        problems.append(f'{path} 取值不在枚举内：{value}')
    if isinstance(value, dict):
        properties = schema.get('properties', {})
        problems += [f'{path} 缺少 {key}' for key in schema.get('required', []) if key not in value]
        for key, child in value.items():
            subschema = properties.get(key, schema.get('additionalProperties', True))
            if subschema is False:
                problems.append(f'{path}.{key} 格式外字段')
            elif isinstance(subschema, dict):
                problems += schema_problems(child, subschema, f'{path}.{key}')
    elif isinstance(value, list) and isinstance(schema.get('items'), dict):
        for index, child in enumerate(value):
            problems += schema_problems(child, schema['items'], f'{path}[{index}]')
    return problems


def web_entries(project):
    return {name: entry for name, entry in project.entries.items()
            if isinstance(entry, dict) and entry.get('surface') == 'web' and isinstance(entry.get('atlas'), dict)}


def declaration(project, entry):
    found = web_entries(project).get(entry)
    if found is None:
        reject(409, 'atlas-not-declared', 'entry', f'入口 {entry} 不是声明了 atlas 的 Web 入口')
    if not found.get('spec_library'):
        reject(409, 'atlas-not-declared', 'entry', f'入口 {entry} 须声明 spec_library（<仓库别名>:<目录>），地图放在其下的 atlas/')
    return found


def paths(project, checkout, entry):
    declared = declaration(project, entry)
    alias, _, relative = declared['spec_library'].partition(':')
    directory = checkout.paths[alias] / relative / 'atlas'
    return directory / f'{entry}.json', directory / f'{entry}.digests.json'


def walk_sources(page):
    yield from page.get('sources', [])
    for control in page.get('controls', []):
        yield from control.get('sources', [])
    for dialog in page.get('dialogs', []):
        yield from dialog.get('sources', [])
        for action in dialog.get('actions', []):
            yield from action.get('sources', [])
    for table in page.get('tables', []):
        yield from table.get('sources', [])
        for action in table.get('row_actions', []):
            yield from action.get('sources', [])


def source_files(page):
    found = set()
    for value in walk_sources(page):
        match = SOURCE.fullmatch(value)
        if match:
            found.add(match[1] + ':' + match[2])
    return sorted(found)


def digest(checkout, name):
    alias, _, relative = name.partition(':')
    root = checkout.paths.get(alias)
    if root is None:
        return None
    path = root / relative
    try:
        if not path.resolve().is_relative_to(root.resolve()) or not path.is_file():
            return None
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def validate(document, project, checkout, entry):
    problems = schema_problems(document)
    if problems:
        return problems
    allowed = set(declaration(project, entry)['atlas'].get('repos', []))
    seen = set()
    for page in document['pages']:
        if page['id'] in seen:
            problems.append(f"页面编号 {page['id']} 重复")
        seen.add(page['id'])
        for value in walk_sources(page):
            match = SOURCE.fullmatch(value)
            if not match:
                problems.append(f"页面 {page['id']} 的源码位置 {value} 应写成 <别名>:<路径>[:<行>]")
            elif match[1] not in allowed:
                problems.append(f"页面 {page['id']} 的源码位置 {value} 使用了 atlas.repos 以外的仓库")
            elif digest(checkout, match[1] + ':' + match[2]) is None:
                problems.append(f"页面 {page['id']} 的源码位置 {value} 指向的文件不存在")
    return problems


def prepare(context, entry, *, mode='full'):
    from .authoring import create_work
    project, checkout = context.project, context.checkout
    target, sidecar = paths(project, checkout, entry)
    declared = declaration(project, entry)
    work_id, directory, _ = create_work(context, 'atlas', scope=[entry])
    stale_pages = stale(context, entry) if mode == 'stale' and target.exists() else None
    repos = {alias: str(checkout.paths[alias]) for alias in declared['atlas'].get('repos', []) if alias in checkout.paths}
    prompt = Template(files('sop_board').joinpath('prompts', 'atlas.md').read_text(encoding='utf-8')).substitute(
        work=work_id, entry=entry, target=target, schema=Path(files('sop_board').joinpath('prompts', 'atlas.schema.json')),
        repos=json.dumps(repos, ensure_ascii=False), languages='、'.join(declared['atlas'].get('languages', [])) or '页面实际使用的语言',
        mode='只更新下列过期页面：' + json.dumps(stale_pages, ensure_ascii=False) if stale_pages else '梳理全部页面')
    (directory / 'prompt.md').write_text(prompt, encoding='utf-8')
    return dict(ok=True, work=work_id, entry=entry, prompt=str(directory / 'prompt.md'), target=str(target))


def submit(context, entry, *, work_id=None):
    from .authoring import load_work
    project, checkout = context.project, context.checkout
    target, sidecar = paths(project, checkout, entry)
    work = load_work(context, 'atlas', work_id)
    try:
        document = json.loads(target.read_text(encoding='utf-8'))
    except (OSError, ValueError) as error:
        reject(422, 'atlas-invalid', 'file', f'地图文件无法读取：{target}：{error}')
    problems = validate(document, project, checkout, entry)
    if problems:
        reject(422, 'atlas-invalid', 'file', '；'.join(problems[:20]))
    names = sorted({name for page in document['pages'] for name in source_files(page)})
    sidecar.write_text(json.dumps(dict(files={name: digest(checkout, name) for name in names}), ensure_ascii=False, indent=2) + '\n',
                       encoding='utf-8')
    from . import works
    from .checkouts import branch_of
    works.finish(context.store, work, checkout=checkout.name, branch=branch_of(project, checkout),
                 result=dict(entry=entry, pages=len(document['pages'])))
    return dict(ok=True, entry=entry, file=str(target), digests=str(sidecar), pages=len(document['pages']), sources=len(names))


def stale(context, entry):
    """源码摘要变化的页面：[{page, files}]。地图不存在时返回 None。"""
    target, sidecar = paths(context.project, context.checkout, entry)
    if not target.exists() or not sidecar.exists():
        return None
    document = json.loads(target.read_text(encoding='utf-8'))
    recorded = json.loads(sidecar.read_text(encoding='utf-8')).get('files', {})
    result = []
    for page in document['pages']:
        changed = [name for name in source_files(page) if digest(context.checkout, name) != recorded.get(name)]
        if changed:
            result.append(dict(page=page['id'], files=changed))
    return result


def summary(context):
    result = []
    for entry in web_entries(context.project):
        try:
            target, _ = paths(context.project, context.checkout, entry)
        except Exception as error:  # noqa: BLE001 - 声明不完整时如实显示原因
            result.append(dict(entry=entry, exists=False, problem=str(getattr(error, 'problems', [{}])[0].get('problem', error))))
            continue
        pages = stale(context, entry)
        result.append(dict(entry=entry, exists=target.exists(), stale=pages,
                           document=json.loads(target.read_text(encoding='utf-8')) if target.exists() else None))
    return result
