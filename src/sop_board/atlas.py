"""工作区交互地图：梳理任务的准备、输出校验、版本登记、过期计算与执行者反馈（D84）。

地图只记录“在哪、叫什么、怎么定位、点了打开什么、发出什么请求”，不收可见性、校验、默认值和业务结果；
它是执行时的操作知识，不提供预期，也不参与执行作废判断。
"""
import argparse
import hashlib
import json
from importlib.resources import files
from pathlib import Path
import re
import sqlite3
from string import Template

from .drafts import schema_problems
from .errors import APIError, reject
from .kernel import S
from .service import canonical, now


SCHEMA = json.loads(files('sop_board').joinpath('prompts/atlas.schema.json').read_text(encoding='utf-8'))
BIND_SCHEMA = json.loads(files('sop_board').joinpath('prompts/atlas-bind.schema.json').read_text(encoding='utf-8'))
QUOTED = re.compile(r'「([^」]+)」|“([^”]+)”')
DECISIONS = {'sop': '按 SOP', 'change': '改 SOP'}
IDENTIFIER = re.compile(r'[a-z0-9][a-z0-9._-]*\Z')
SOURCE = re.compile(r'([a-z][a-z0-9-]*):([^:\n]+?)(?::([0-9]+))?\Z')
METHODS = {'GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'HEAD', 'OPTIONS'}
FEEDBACK_KINDS = ('missing', 'locator', 'name', 'reach', 'other')
MODES = ('full', 'stale', 'pages')
MAX_OUTPUT = 5 * 1024 * 1024
# 常见的预期类字段名；结构本身已拒收所有未声明字段，这里只给出更明确的说明。
EXPECTATIONS = {'visible_to', 'visible', 'visibility', 'roles', 'role_access', 'permission', 'permissions', 'required',
                'max_length', 'maxlength', 'min_length', 'minlength', 'max', 'min', 'pattern', 'default', 'defaults',
                'expected', 'expectation', 'validation', 'rules', 'rule', 'outcome', 'result', 'effects', 'side_effects',
                'disabled', 'enabled'}


def entries(workspace):
    """声明了 atlas 的 Web 入口。"""
    return {name: entry for name, entry in workspace.entries.items()
            if isinstance(entry, dict) and entry.get('surface') == 'web' and isinstance(entry.get('atlas'), dict)}


def declaration(workspace, name):
    found = entries(workspace).get(name)
    if found is None:
        reject(409, 'atlas-not-declared', 'entry', f'入口 {name} 不是声明了 atlas 的 Web 入口')
    return found['atlas']


def roots(workspace, atlas):
    return {alias: Path(workspace.repos[alias]) for alias in atlas['repos'] if alias in workspace.repos}


def directory(workspace, entry):
    return workspace.data / 'atlas' / entry


def file_digest(root, relative):
    path = (root / relative)
    try:
        resolved = path.resolve()
        if not resolved.is_relative_to(root.resolve()) or not resolved.is_file():
            return None
        return hashlib.sha256(resolved.read_bytes()).hexdigest()
    except OSError:
        return None


def source_files(pages):
    """页面引用到的源码文件（去掉行号），按页面编号分组。"""
    result = {}
    for page in pages:
        found = set()
        for item in walk_sources(page):
            match = SOURCE.fullmatch(item)
            if match:
                found.add(match[1] + ':' + match[2])
        result[page['id']] = sorted(found)
    return result


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


def current_digests(workspace, atlas, names):
    found = {}
    repos = roots(workspace, atlas)
    for name in names:
        alias, _, relative = name.partition(':')
        found[name] = file_digest(repos[alias], relative) if alias in repos else None
    return found


def flatten(value, prefix=''):
    """JSON 多语言文件的“键 → 文案”扁平映射；非字符串叶子不计。"""
    result = {}
    if isinstance(value, dict):
        for key, item in value.items():
            result.update(flatten(item, f'{prefix}.{key}' if prefix else str(key)))
    elif isinstance(value, str):
        result[prefix] = value
    return result


def key_digests(workspace, atlas, names):
    """引用到的 JSON 多语言文件逐键摘要；文件变化时据此只把用到变化键的页面判为过期。"""
    repos = roots(workspace, atlas)
    result = {}
    for name in names:
        alias, _, relative = name.partition(':')
        if not relative.endswith('.json') or alias not in repos:
            continue
        try:
            value = json.loads((repos[alias] / relative).read_text(encoding='utf-8'))
        except (OSError, ValueError, UnicodeDecodeError, RecursionError):
            continue
        flat = flatten(value)
        if flat:
            result[name] = {key: hashlib.sha256(text.encode()).hexdigest() for key, text in flat.items()}
    return result


def page_keys(page):
    keys = set()
    for control in page['controls']:
        keys.add(control['i18n'])
    for dialog in page['dialogs']:
        keys |= {f['i18n'] for f in dialog['fields']} | {a['i18n'] for a in dialog['actions']}
    for table in page['tables']:
        keys |= {c['i18n'] for c in table['columns']} | {a['i18n'] for a in table['row_actions']}
    return keys - {None}


class Atlas:
    def __init__(self, tasks):
        self.tasks, self.service, self.store = tasks, tasks.service, tasks.store

    @property
    def workspace(self):
        return self.service.workspace

    # ---- 版本与过期 ----
    def latest(self, db, entry):
        row = db.execute('SELECT * FROM atlas_versions WHERE workspace=? AND entry=? ORDER BY version DESC LIMIT 1',
                         (self.workspace.name, entry)).fetchone()
        return dict(row) if row else None

    def load(self, entry, version):
        path = directory(self.workspace, entry) / f'v{version}' / 'atlas.json'
        return json.loads(path.read_text(encoding='utf-8'))

    def stale(self, entry, atlas_doc=None, digests=None):
        """按页面比较登记时与当前的源码摘要；文件变化或被删除的页面为过期。"""
        if atlas_doc is None:
            with self.store.connection() as db:
                row = self.latest(db, entry)
            if row is None:
                return []
            atlas_doc, digests = self.load(entry, row['version']), json.loads(row['digests_json'])
        files_by_page = source_files(atlas_doc['pages'])
        names = sorted({name for group in files_by_page.values() for name in group})
        atlas = declaration(self.workspace, entry)
        recorded_files, recorded_keys = digests.get('files', {}), digests.get('keys', {})
        now_digests = current_digests(self.workspace, atlas, names)
        changed_files = {name for name in names if now_digests.get(name) != recorded_files.get(name)}
        changed_keys = {}
        current_keys = key_digests(self.workspace, atlas, sorted(changed_files & set(recorded_keys)))
        for name in changed_files & set(recorded_keys):
            if name in current_keys:
                old, new = recorded_keys[name], current_keys[name]
                changed_keys[name] = {key for key in old.keys() | new.keys() if old.get(key) != new.get(key)}
        result = []
        for page in atlas_doc['pages']:
            files, keys = [], set()
            for name in files_by_page[page['id']]:
                if name not in changed_files:
                    continue
                if name in changed_keys:
                    hit = changed_keys[name] & page_keys(page)
                    if hit:
                        files.append(name)
                        keys |= hit
                else:
                    files.append(name)
            if files:
                result.append(dict(page=page['id'], files=files, keys=sorted(keys)))
        return result

    def feedback(self, db, entry, *, pending=True):
        query = 'SELECT * FROM atlas_feedback WHERE workspace=? AND entry=?' + (' AND handled_version IS NULL' if pending else '') + ' ORDER BY id'
        return [dict(row) for row in db.execute(query, (self.workspace.name, entry))]

    def summary(self, *, local=True):
        """供地图页与接口使用的入口摘要。"""
        result = []
        with self.store.connection() as db:
            for name, entry in entries(self.workspace).items():
                row = self.latest(db, name)
                stale = self.stale(name) if row else []
                result.append(dict(entry=name, repos=entry['atlas']['repos'], languages=entry['atlas'].get('languages'),
                    version=row['version'] if row else None, created_at=row['created_at'] if row else None,
                    task=row['task'] if row else None, **(json.loads(row['summary_json']) if row else dict(pages=0, controls=0)),
                    stale=stale, feedback=[public_feedback(item, local=local) for item in self.feedback(db, name)]))
        return result

    # ---- 任务输入与准备 ----
    def inputs(self, entry, mode, pages, browser):
        atlas = declaration(self.workspace, entry)
        if mode not in MODES:
            raise ValueError('模式应为 full、stale 或 pages')
        with self.store.connection() as db:
            row = self.latest(db, entry)
        if mode != 'full' and row is None:
            raise ValueError('尚无交互地图，请先全量梳理')
        stale = self.stale(entry) if mode == 'stale' else []
        if mode == 'pages':
            known = {page['id'] for page in self.load(entry, row['version'])['pages']}
            if not pages or len(set(pages)) != len(pages) or set(pages) - known:
                raise ValueError('--pages 须为当前地图中不重复的页面编号：' + '、'.join(sorted(set(pages) - known)))
        return dict(entry=entry, mode=mode, pages=list(pages) if mode == 'pages' else [], browser=bool(browser),
                    atlas=dict(repos=list(atlas['repos']), languages=atlas.get('languages')),
                    base_version=row['version'] if row else None, stale=stale, config_digest=self.workspace.config_digest)

    def enqueue(self, inputs, trigger):
        body = canonical(inputs)
        with self.store.transaction() as db:
            return db.execute("INSERT INTO tasks(kind,workspace,batch,status,trigger,input_digest,input_json,created_at) "
                "VALUES ('atlas',?,?,?,?,?,?,?)", (self.workspace.name, '', S.QUEUED, trigger,
                hashlib.sha256(body.encode()).hexdigest(), body, now())).lastrowid

    def prepare(self, task):
        inputs = json.loads(task['input_json'])
        if inputs['mode'] == 'bind':
            return self.prepare_bind(task, inputs)
        folder = self.tasks.directory(task['id'])
        folder.mkdir(parents=True, exist_ok=True)
        repos = roots(self.workspace, inputs['atlas'])
        (folder / 'repos.json').write_text(canonical({alias: str(path) for alias, path in repos.items()}), encoding='utf-8')
        entry = dict(self.workspace.entries.get(inputs['entry'], {}))
        (folder / 'entry.json').write_text(canonical(dict(name=inputs['entry'], declaration=entry,
            languages=inputs['atlas']['languages'])), encoding='utf-8')
        previous = None
        if inputs['base_version']:
            previous = self.load(inputs['entry'], inputs['base_version'])
            (folder / 'previous').mkdir(exist_ok=True)
            (folder / 'previous' / 'atlas.json').write_text(canonical(previous), encoding='utf-8')
        with self.store.connection() as db:
            feedback = [dict(id=item['id'], page=item['page'], control=item['control'], kind=item['kind'], detail=item['detail'],
                             scenario=item['scenario'], task=item['task']) for item in self.feedback(db, inputs['entry'])]
        (folder / 'feedback.json').write_text(canonical(feedback), encoding='utf-8')
        if inputs['mode'] == 'stale':
            (folder / 'stale.json').write_text(canonical(inputs['stale']), encoding='utf-8')
            instruction = ('只更新过期页面：stale.json 列出过期页面及其变化文件。重写这些页面，并检查路由与导航配置有没有新增页面；'
                           'pages 只写重写的页面与新增页面，已不存在的页面写入 removed_pages，其余页面看板原样沿用。')
        elif inputs['mode'] == 'pages':
            (folder / 'pages.json').write_text(canonical(inputs['pages']), encoding='utf-8')
            instruction = ('只更新指定页面：pages.json 列出要重写的页面编号。pages 只写这些页面，'
                           '确实已不存在的写入 removed_pages，其余页面看板原样沿用。')
        else:
            instruction = '全量梳理：pages 写出该入口全部页面；previous/atlas.json 存在时沿用其中的编号。'
        languages = inputs['atlas']['languages']
        browser = (f'允许只读浏览器核对：经入口 {inputs["entry"]} 的真实地址只读打开页面，核对定位器与就绪信号，'
                   '把打码后的页面结构快照写到任务目录 evidence/，并填写 verified_browser 日期。不保存、不提交、不改数据；'
                   '遇到登录、人机验证、浏览器安全提示或未知遮挡立即停止该项，在 notes 说明。'
                   if inputs['browser'] else '本次不用浏览器，verified_browser 一律填 null。')
        prompt = Template(files('sop_board').joinpath('prompts/atlas.md').read_text(encoding='utf-8')).substitute(
            task_directory=folder, entry=inputs['entry'], mode_instruction=instruction, browser_instruction=browser,
            languages='、'.join(languages) if languages else '按前端多语言文件推断，各语言都记录')
        (folder / 'prompt.md').write_text(prompt, encoding='utf-8')
        (folder / 'schema.json').write_text(canonical(SCHEMA), encoding='utf-8')
        (folder / 'input.json').write_text(task['input_json'], encoding='utf-8')
        return prompt

    # ---- 输出校验与登记 ----
    def consume(self, task):
        inputs = json.loads(task['input_json'])
        if inputs['mode'] == 'bind':
            return self.consume_bind(task, inputs)
        folder = self.tasks.directory(task['id'])
        path = folder / 'final.json'
        if not path.is_file() or not path.read_bytes().strip():
            raise FileNotFoundError('没有写出 final.json')
        if path.stat().st_size > MAX_OUTPUT:
            return S.FAILED, dict(ok=False, problems=['final.json 超过 5 MB'])
        try:
            output = json.loads(path.read_text(encoding='utf-8'))
        except (ValueError, RecursionError):
            return S.FAILED, dict(ok=False, problems=['final.json 不是 JSON'])
        problems = [explain(problem) for problem in schema_problems(output, SCHEMA)]
        if problems:
            return S.FAILED, dict(ok=False, problems=problems)
        if self.workspace.entries.get(inputs['entry'], {}).get('atlas') is None:
            return S.FAILED, dict(ok=False, problems=[f'入口 {inputs["entry"]} 已不再声明 atlas'])
        previous = self.load(inputs['entry'], inputs['base_version']) if inputs['base_version'] else None
        feedback_ids = {item['id'] for item in json.loads((folder / 'feedback.json').read_text(encoding='utf-8'))} \
            if (folder / 'feedback.json').is_file() else set()
        merged, problems = merge(inputs, previous, output, feedback_ids)
        if not problems:
            problems = validate(merged, inputs, roots(self.workspace, inputs['atlas']))
        if problems:
            return S.FAILED, dict(ok=False, problems=problems)
        files_by_page = source_files(merged)
        names = sorted({name for group in files_by_page.values() for name in group})
        digests = dict(files=current_digests(self.workspace, inputs['atlas'], names),
                       keys=key_digests(self.workspace, inputs['atlas'], names))
        with self.store.transaction() as db:
            row = self.latest(db, inputs['entry'])
            if (row['version'] if row else None) != inputs['base_version']:
                return S.FAILED, dict(ok=False, problems=['准备后地图已有新版本，请重新准备梳理任务'])
            version = (row['version'] if row else 0) + 1
            document = dict(entry=inputs['entry'], version=version, languages=languages_of(merged), task=task['id'],
                            mode=inputs['mode'], generated_at=now(), notes=output['notes'], pages=merged, glossary=glossary(merged))
            target = directory(self.workspace, inputs['entry']) / f'v{version}'
            target.mkdir(parents=True, exist_ok=True)
            (target / 'atlas.json').write_text(canonical(document), encoding='utf-8')
            (target / 'atlas.md').write_text(render(document), encoding='utf-8')
            evidence = folder / 'evidence'
            if evidence.is_dir():
                import shutil
                shutil.copytree(evidence, target / 'evidence', dirs_exist_ok=True)
            summary = dict(pages=len(merged), controls=count_controls(merged))
            db.execute('INSERT INTO atlas_versions(workspace,entry,version,task,created_at,digests_json,summary_json) VALUES (?,?,?,?,?,?,?)',
                       (self.workspace.name, inputs['entry'], version, task['id'], document['generated_at'], canonical(digests), canonical(summary)))
            for item in output['feedback_handled']:
                db.execute('UPDATE atlas_feedback SET handled_version=?,adopted=?,note=? WHERE id=? AND workspace=? AND handled_version IS NULL',
                           (version, int(item['adopted']), item['note'], item['id'], self.workspace.name))
        return S.COMPLETED, dict(ok=True, problems=[], entry=inputs['entry'], version=version, **summary,
                                 feedback_handled=len(output['feedback_handled']))

    # ---- 场景绑定与界面对照（D85）----
    def bind_inputs(self, batch_name, entry, stale):
        with self.store.connection() as db:
            batch = self.service.batch(db, self.workspace.name, batch_name)
            scenarios = [dict(r) for r in db.execute(
                'SELECT scenario,fingerprint,verify FROM scenarios WHERE batch=? AND current=1 ORDER BY id', (batch['id'],))]
            mapped = set(entries(self.workspace))
            if entry is None:
                used = {s['verify'] for s in scenarios if s['verify'] in mapped}
                if len(used) != 1:
                    raise ValueError('批次用到的地图入口不唯一或没有，请用 --entry 指定')
                entry = used.pop()
            atlas = declaration(self.workspace, entry)
            latest = self.latest(db, entry)
            if latest is None:
                raise ValueError(f'入口 {entry} 尚无交互地图，请先梳理交互')
            selected = [s for s in scenarios if s['verify'] == entry]
            if stale:
                bound = {(r['scenario'], r['fingerprint']): dict(r) for r in db.execute(
                    'SELECT * FROM atlas_bindings WHERE batch=? AND entry=?', (batch['id'], entry))}
                pages_now = {p['id']: p for p in self.load(entry, latest['version'])['pages']}
                keep = []
                for scene in selected:
                    previous = bound.get((scene['scenario'], scene['fingerprint']))
                    if previous is None:
                        keep.append(scene)
                        continue
                    if previous['atlas_version'] != latest['version']:
                        old_pages = {p['id']: p for p in self.load(entry, previous['atlas_version'])['pages']}
                        used_pages = {step['page'] for step in json.loads(previous['body_json'])['steps'] if step['page']}
                        if any(old_pages.get(page) != pages_now.get(page) for page in used_pages):
                            keep.append(scene)
                selected = keep
        if not selected:
            raise ValueError('没有需要对照的场景')
        return dict(entry=entry, mode='bind', batch=batch_name, base_version=latest['version'], stale=bool(stale),
                    scenarios=[dict(id=s['scenario'], fingerprint=s['fingerprint']) for s in selected], pages=[], browser=False,
                    atlas=dict(repos=list(atlas['repos']), languages=atlas.get('languages')), config_digest=self.workspace.config_digest)

    def enqueue_bind(self, inputs, trigger):
        body = canonical(inputs)
        with self.store.transaction() as db:
            return db.execute("INSERT INTO tasks(kind,workspace,batch,status,trigger,input_digest,input_json,created_at) "
                "VALUES ('atlas',?,?,?,?,?,?,?)", (self.workspace.name, inputs['batch'], S.QUEUED, trigger,
                hashlib.sha256(body.encode()).hexdigest(), body, now())).lastrowid

    def scenario_bodies(self, db, batch_id, items):
        bodies = {}
        for item in items:
            row = db.execute('SELECT body FROM scenarios WHERE batch=? AND scenario=? AND fingerprint=? ORDER BY id DESC LIMIT 1',
                             (batch_id, item['id'], item['fingerprint'])).fetchone()
            if row is None:
                raise ValueError(f"场景 {item['id']} 的内容已不存在")
            bodies[item['id']] = json.loads(row['body'])
        return bodies

    def prepare_bind(self, task, inputs):
        import yaml
        folder = self.tasks.directory(task['id'])
        folder.mkdir(parents=True, exist_ok=True)
        with self.store.connection() as db:
            batch = self.service.batch(db, self.workspace.name, inputs['batch'])
            bodies = self.scenario_bodies(db, batch['id'], inputs['scenarios'])
        (folder / 'scenarios.yaml').write_text(yaml.safe_dump(dict(scenarios=[bodies[i['id']] for i in inputs['scenarios']]),
                                               allow_unicode=True, sort_keys=False), encoding='utf-8')
        (folder / 'atlas.json').write_text(canonical(self.load(inputs['entry'], inputs['base_version'])), encoding='utf-8')
        (folder / 'entry.json').write_text(canonical(dict(name=inputs['entry'], languages=inputs['atlas']['languages'])), encoding='utf-8')
        prompt = Template(files('sop_board').joinpath('prompts/atlas-bind.md').read_text(encoding='utf-8')).substitute(
            task_directory=folder, entry=inputs['entry'], batch=inputs['batch'], version=inputs['base_version'])
        (folder / 'prompt.md').write_text(prompt, encoding='utf-8')
        (folder / 'schema.json').write_text(canonical(BIND_SCHEMA), encoding='utf-8')
        (folder / 'input.json').write_text(task['input_json'], encoding='utf-8')
        return prompt

    def consume_bind(self, task, inputs):
        folder = self.tasks.directory(task['id'])
        path = folder / 'final.json'
        if not path.is_file() or not path.read_bytes().strip():
            raise FileNotFoundError('没有写出 final.json')
        try:
            output = json.loads(path.read_text(encoding='utf-8'))
        except (ValueError, RecursionError):
            return S.FAILED, dict(ok=False, problems=['final.json 不是 JSON'])
        problems = schema_problems(output, BIND_SCHEMA)
        if problems:
            return S.FAILED, dict(ok=False, problems=problems)
        document = self.load(inputs['entry'], inputs['base_version'])
        elements = element_texts(document)
        pages = {page['id'] for page in document['pages']}
        with self.store.connection() as db:
            batch = self.service.batch(db, self.workspace.name, inputs['batch'])
            bodies = self.scenario_bodies(db, batch['id'], inputs['scenarios'])
        expected = [item['id'] for item in inputs['scenarios']]
        written = [item['scenario'] for item in output['scenarios']]
        if sorted(written) != sorted(expected):
            problems.append('scenarios 须恰好覆盖本次的场景各一次：缺少 ' + '、'.join(sorted(set(expected) - set(written)) or ['无'])
                            + '；多出或重复 ' + '、'.join(sorted({w for w in written if w not in expected or written.count(w) > 1}) or ['无']))
        for item in output['scenarios']:
            if item['scenario'] in bodies:
                problems += check_binding(item, bodies[item['scenario']], pages, elements)
        if problems:
            return S.FAILED, dict(ok=False, problems=problems)
        fingerprints = {item['id']: item['fingerprint'] for item in inputs['scenarios']}
        counts = dict(mismatch=0, absent=0)
        with self.store.transaction() as db:
            for item in output['scenarios']:
                scenario, fingerprint = item['scenario'], fingerprints[item['scenario']]
                db.execute('INSERT OR REPLACE INTO atlas_bindings(batch,scenario,fingerprint,entry,atlas_version,task,body_json,created_at) '
                           'VALUES (?,?,?,?,?,?,?,?)', (batch['id'], scenario, fingerprint, inputs['entry'], inputs['base_version'],
                           task['id'], canonical(item), now()))
                previous = {(r['text'], r['location']): dict(r) for r in db.execute(
                    'SELECT * FROM atlas_checks WHERE batch=? AND scenario=? AND fingerprint=?', (batch['id'], scenario, fingerprint))}
                db.execute('DELETE FROM atlas_checks WHERE batch=? AND scenario=? AND fingerprint=?', (batch['id'], scenario, fingerprint))
                number = 0
                for text in item['texts']:
                    if text['status'] not in ('mismatch', 'absent'):
                        continue
                    number += 1
                    counts[text['status']] += 1
                    kept = previous.get((text['text'], text['at']), {})
                    db.execute('INSERT INTO atlas_checks(batch,scenario,fingerprint,item,text,location,status,ref,ui_json,note,decision,'
                               'decided_by,decided_at,decision_note,task,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                               (batch['id'], scenario, fingerprint, f'T{number}', text['text'], text['at'], text['status'], text['ref'],
                                canonical(elements.get(text['ref'], [])), text['note'], kept.get('decision'), kept.get('decided_by'),
                                kept.get('decided_at'), kept.get('decision_note'), task['id'], now()))
        return S.COMPLETED, dict(ok=True, problems=[], entry=inputs['entry'], version=inputs['base_version'],
                                 scenarios=len(output['scenarios']), **counts)

    def checks(self, db, batch):
        return read_checks(db, batch)

    def decide(self, batch_name, item_ref, decision, by, note=''):
        if decision not in DECISIONS:
            raise ValueError('--decision 应为 sop（按 SOP 判）或 change（改 SOP）')
        by = person(by) if isinstance(by, str) and by.strip() else None
        if by is None:
            raise ValueError('请填写决定人姓名（命令行用 --by）')
        scenario, separator, item = item_ref.partition('#')
        if not separator:
            raise ValueError('--item 应写成 <场景>#<条目>，如 WEB-N01#T1')
        with self.store.transaction() as db:
            by = person(by, db, self.workspace.name)
            batch = self.service.batch(db, self.workspace.name, batch_name)
            row = db.execute('SELECT c.id,c.text,c.status,c.ui_json FROM atlas_checks c JOIN scenarios s ON s.batch=c.batch AND s.scenario=c.scenario '
                             'AND s.current=1 AND s.fingerprint=c.fingerprint WHERE c.batch=? AND c.scenario=? AND c.item=?',
                             (batch['id'], scenario, item)).fetchone()
            if row is None:
                raise ValueError(f'没有进行中的对照条目 {item_ref}（场景可能已修改，条目已关闭）')
            db.execute('UPDATE atlas_checks SET decision=?,decided_by=?,decided_at=?,decision_note=? WHERE id=?',
                       (decision, by.strip(), now(), note or '', row['id']))
            if decision == 'sop' and row['status'] in ('mismatch', 'absent'):
                # 按 SOP 判且界面不符：产品要改，登记到问题台账（D98）；同一条目重复决定时沿用已有问题。
                from .issues import add as add_issue, link as link_issue
                ui = json.loads(row['ui_json'] or '[]')
                shown = '／'.join(dict.fromkeys(str(item.get('text')) for item in (ui if isinstance(ui, list) else [ui])
                                                if isinstance(item, dict) and item.get('text')))
                title = f"界面与 SOP 不一致：{scenario} 要求“{row['text']}”" + (f"，界面为“{shown}”" if shown else '，界面没有对应文案')
                existing = db.execute("SELECT id FROM issues WHERE workspace=? AND title=? AND status!='closed'",
                                      (self.workspace.name, title)).fetchone()
                if existing:
                    link_issue(db, existing['id'], batch_name, scenario)
                else:
                    add_issue(db, self.workspace.name, title=title, kind='product', by=by.strip(), scenarios=[(batch_name, scenario)],
                              note=f'界面对照条目 {item_ref} 决定按 SOP 判' + (('：' + note) if note else ''), source='atlas')
            return self.checks(db, batch)

    def bindings(self, db, batch, scenarios):
        """执行准备取用：场景当前内容的绑定及对照决定。"""
        result = {}
        for scene in scenarios:
            row = db.execute('SELECT * FROM atlas_bindings WHERE batch=? AND scenario=? AND fingerprint=?',
                             (batch['id'], scene['id'], scene['fingerprint'])).fetchone()
            if row is not None:
                result[scene['id']] = dict(json.loads(row['body_json']), entry=row['entry'], atlas_version=row['atlas_version'])
        return result

    def materialize_execution(self, folder, batch_name, scenes):
        """执行准备：写地图节选、绑定与对照决定；返回所用地图版本，供登记轮次与速查卡使用（D86）。"""
        workspace, service = self.workspace, self.service
        mapped = entries(workspace)
        used = sorted({scene['verify'] for scene in scenes if scene['verify'] in mapped})
        if not used:
            return {}
        excerpts, bindings, versions, unbound = [], {}, {}, []
        with service.store.connection() as db:
            batch = service.batch(db, workspace.name, batch_name)
            for name in used:
                row = db.execute('SELECT * FROM atlas_versions WHERE workspace=? AND entry=? ORDER BY version DESC LIMIT 1',
                                 (workspace.name, name)).fetchone()
                if row is None:
                    continue
                document = json.loads((directory(workspace, name) / f"v{row['version']}" / 'atlas.json').read_text(encoding='utf-8'))
                versions[name] = row['version']
                wanted = set()
                for scene in [s for s in scenes if s['verify'] == name]:
                    found = db.execute('SELECT * FROM atlas_bindings WHERE batch=? AND scenario=? AND fingerprint=?',
                                       (batch['id'], scene['id'], scene['fingerprint'])).fetchone()
                    if found is None:
                        unbound.append(scene['id'])
                        continue
                    body = json.loads(found['body_json'])
                    wanted |= {step['page'] for step in body['steps'] if step['page']}
                    wanted |= {ref.split('/')[0] for step in body['steps'] for ref in step['controls']}
                    checks = [dict(item=c['item'], text=c['text'], location=c['location'], status=c['status'], ref=c['ref'],
                                   ui=json.loads(c['ui_json']), decision=c['decision'] or 'undecided', decided_by=c['decided_by'])
                              for c in db.execute('SELECT * FROM atlas_checks WHERE batch=? AND scenario=? AND fingerprint=? ORDER BY id',
                                                  (batch['id'], scene['id'], scene['fingerprint']))]
                    bindings[scene['id']] = dict(body, entry=name, atlas_version=found['atlas_version'], checks=checks)
                whole = len(canonical(document).encode()) <= EXCERPT_LIMIT
                include = {p['id'] for p in document['pages']} if (whole and any(s in unbound for s in [x['id'] for x in scenes if x['verify'] == name])) \
                    else reach_closure(document, wanted)
                pages = [page for page in document['pages'] if page['id'] in include]
                keys = set().union(*(page_keys(page) for page in pages)) if pages else set()
                excerpts.append(dict(entry=name, version=row['version'], generated_at=document['generated_at'], languages=document['languages'],
                                     index=[dict(id=p['id'], title=p['title'], route=p['route']) for p in document['pages']],
                                     pages=pages, glossary=[g for g in document['glossary'] if g['key'] in keys],
                                     complete=len(pages) == len(document['pages'])))
        if not excerpts:
            return {}
        for item in excerpts:
            item['stale'] = self.stale(item['entry'])
        (Path(folder) / 'atlas.json').write_text(canonical(dict(entries=excerpts)), encoding='utf-8')
        (Path(folder) / 'bindings.json').write_text(canonical(dict(scenarios=bindings, unbound=sorted(set(unbound)))), encoding='utf-8')
        (Path(folder) / 'atlas-versions.json').write_text(canonical(versions), encoding='utf-8')
        return dict(versions=versions, unbound=sorted(set(unbound)), complete={e['entry']: e['complete'] for e in excerpts},
                    checks=sum(len(b['checks']) for b in bindings.values()))

    # ---- 执行者反馈 ----
    def collect_feedback(self, db, task_id, folder, scenarios=None):
        """登记执行结果时收下 atlas-feedback.jsonl；格式不合格的行只提示，不影响结果登记。"""
        path = Path(folder) / 'atlas-feedback.jsonl'
        if not path.is_file():
            return None
        names = list(entries(self.workspace))
        accepted, rejected = 0, []
        try:
            lines = path.read_text(encoding='utf-8').splitlines()
        except (OSError, UnicodeDecodeError) as error:
            return dict(accepted=0, rejected=[dict(line=0, problem='文件无法按 UTF-8 读取：' + str(error))])
        for number, line in enumerate(lines, 1):
            if not line.strip():
                continue
            try:
                item = json.loads(line)
                if not isinstance(item, dict):
                    raise ValueError('每行须为 JSON 对象')
                extra = item.keys() - {'entry', 'page', 'control', 'kind', 'detail', 'evidence', 'scenario'}
                if extra:
                    raise ValueError('格式外字段：' + '、'.join(sorted(extra)))
                entry = item.get('entry') or (names[0] if len(names) == 1 else None)
                if entry not in names:
                    raise ValueError('须写明声明了 atlas 的入口 entry')
                for field in ('page', 'kind', 'detail'):
                    if not isinstance(item.get(field), str) or not item[field].strip():
                        raise ValueError(f'缺少 {field}')
                if item['kind'] not in FEEDBACK_KINDS:
                    raise ValueError('kind 应为 ' + '、'.join(FEEDBACK_KINDS))
                for field in ('control', 'evidence', 'scenario'):
                    if item.get(field) is not None and not isinstance(item[field], str):
                        raise ValueError(f'{field} 须为字符串')
                if scenarios is not None and item.get('scenario') and item['scenario'] not in scenarios:
                    raise ValueError('scenario 不在本任务范围内')
            except (ValueError, RecursionError) as error:
                rejected.append(dict(line=number, problem=str(error)))
                continue
            db.execute('INSERT INTO atlas_feedback(workspace,entry,task,scenario,page,control,kind,detail,evidence,created_at) '
                       'VALUES (?,?,?,?,?,?,?,?,?,?)', (self.workspace.name, entry, task_id, item.get('scenario'), item['page'].strip(),
                       item.get('control'), item['kind'], item['detail'].strip(), item.get('evidence'), now()))
            accepted += 1
        return dict(accepted=accepted, rejected=rejected)


EXCERPT_LIMIT = 60 * 1024


def reach_closure(document, wanted):
    """绑定的页面，加上到达它们实际要经过的页面与控件所在页面。
    能从起点直接打开的页面不再带入其他到达方式；否则只沿第一条到达方式（D86：节选只含到达路径所用的控件与文案）。"""
    pages = {page['id']: page for page in document['pages']}
    result, todo = set(), [p for p in wanted if p in pages]
    while todo:
        page = todo.pop()
        if page in result:
            continue
        result.add(page)
        steps = pages[page]['reach']
        if any(step['from'] == 'start' for step in steps):
            steps = [step for step in steps if step['from'] == 'start'][:1]
        else:
            steps = steps[:1]
        for step in steps:
            for other in (step['from'], (step['via'] or '').split('/')[0]):
                if other in pages and other not in result:
                    todo.append(other)
    return result


def record_round(db, batch_id, number, folder):
    path = Path(folder) / 'atlas-versions.json'
    if not path.is_file():
        return
    row = db.execute('SELECT id FROM rounds WHERE batch=? AND number=?', (batch_id, number)).fetchone()
    if row is not None:
        db.execute('INSERT OR REPLACE INTO round_atlas(round_id,versions_json) VALUES (?,?)', (row['id'], path.read_text(encoding='utf-8')))

def read_checks(db, batch):
    """批次的界面对照条目；条目对应的场景内容已变化时视为关闭（场景已改）。"""
    rows = [dict(r) for r in db.execute(
        'SELECT c.*, s.fingerprint AS current_fingerprint, s.title AS scenario_title FROM atlas_checks c '
        'LEFT JOIN scenarios s ON s.batch=c.batch AND s.scenario=c.scenario AND s.current=1 '
        'WHERE c.batch=? ORDER BY c.scenario, c.id', (batch['id'],))]
    items = []
    for row in rows:
        current = row['current_fingerprint'] == row['fingerprint']
        items.append(dict(id=row['id'], scenario=row['scenario'], title=row['scenario_title'], item=row['item'], text=row['text'],
                          location=row['location'], status=row['status'], ref=row['ref'], ui=json.loads(row['ui_json']),
                          note=row['note'], decision=row['decision'], decided_by=row['decided_by'], decided_at=row['decided_at'],
                          decision_note=row['decision_note'], open=current, closed_reason=None if current else '场景已改'))
    live = [item for item in items if item['open']]
    counts = dict(undecided=sum(item['decision'] is None for item in live), sop=sum(item['decision'] == 'sop' for item in live),
                  change=sum(item['decision'] == 'change' for item in live), closed=len(items) - len(live))
    return dict(items=items, counts=counts)

def element_texts(document):
    """控件、字段、列、操作、弹窗与页面标题的引用到各语言文案。"""
    found = {}
    for page in document['pages']:
        found[page['id']] = page['title']
        for control in page['controls']:
            found[f"{page['id']}/{control['id']}"] = control['name']
        for dialog in page['dialogs']:
            found[f"{page['id']}/{dialog['id']}"] = dialog['title']
            for field in dialog['fields']:
                found[f"{page['id']}/{dialog['id']}/{field['id']}"] = field['label']
            for action in dialog['actions']:
                found[f"{page['id']}/{dialog['id']}/{action['id']}"] = action['name']
        for table in page['tables']:
            for column in table['columns']:
                found[f"{page['id']}/{table['id']}/{column['id']}"] = column['title']
            for action in table['row_actions']:
                found[f"{page['id']}/{table['id']}/{action['id']}"] = action['name']
    return found


def scenario_strings(body):
    values = [body.get('title', '')]
    for key in ('initial_state', 'steps', 'expected', 'forbidden_effects'):
        values += [v for v in body.get(key, []) if isinstance(v, str)]
    return values


def check_binding(item, body, pages, elements):
    problems = []
    where = '场景 ' + item['scenario']
    steps = body.get('steps', [])
    seen = set()
    for step in item['steps']:
        if not 1 <= step['step'] <= len(steps) or step['step'] in seen:
            problems.append(f"{where} 的步骤序号 {step['step']} 超出范围或重复")
        seen.add(step['step'])
        if step['page'] is not None and step['page'] not in pages:
            problems.append(f"{where} 第 {step['step']} 步的页面 {step['page']} 不在地图中")
        for ref in step['controls']:
            if ref not in elements:
                problems.append(f"{where} 第 {step['step']} 步的控件 {ref} 不在地图中")
    strings = scenario_strings(body)
    joined = '\n'.join(strings)
    covered = {text['text'] for text in item['texts']}
    for value in strings:
        for match in QUOTED.finditer(value):
            quoted = match[1] or match[2]
            if quoted not in covered:
                problems.append(f"{where} 中加引号的文字「{quoted}」没有在 texts 中对照")
    for text in item['texts']:
        label = f"{where} 的「{text['text']}」"
        if not text['text'].strip() or text['text'] not in joined:
            problems.append(f'{label} 不在场景原文中')
        names = [t['text'] for t in elements.get(text['ref'], [])] if text['ref'] else []
        if text['status'] in ('match', 'translation', 'mismatch'):
            if text['ref'] is None or text['ref'] not in elements:
                problems.append(f"{label} 判为 {text['status']}，须引用地图中存在的元素")
                continue
        elif text['ref'] is not None:
            problems.append(f"{label} 判为 {text['status']} 时 ref 应为 null")
        if text['status'] == 'match' and text['text'] not in names:
            problems.append(f"{label} 判为一致，但 {text['ref']} 的文案是 " + '／'.join(names))
        if text['status'] == 'mismatch' and text['text'] in names:
            problems.append(f"{label} 与 {text['ref']} 的文案相同，应判为 match")
    return problems


GENERIC_NAMES = {'agent', 'assistant', 'codex', 'claude', 'claude code', 'ai', '助手', '当前会话'}


def person(value, db=None, workspace=None):
    """决定人须为使用者给出的姓名，不接受会话标识或 agent；给出 db 时另拒绝本工作区登记过的会话标识。"""
    if not isinstance(value, str) or not value.strip() or len(value) > 100 or '\n' in value or '\0' in value:
        raise ValueError('请填写决定人姓名（1～100 字的单行文字）')
    value = value.strip()
    sessions = set() if db is None else {row[0].casefold() for row in db.execute(
        'SELECT s.session_id FROM session_tasks s JOIN tasks t ON t.id=s.task WHERE t.workspace=?', (workspace,))}
    if (value.casefold() in GENERIC_NAMES | sessions
            or re.fullmatch(r'[a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12}', value)):
        raise ValueError('决定人须为使用者给出的姓名，不能填写会话标识或 agent')
    return value


def public_feedback(item, *, local):
    keys = ('id', 'entry', 'task', 'scenario', 'page', 'control', 'kind', 'detail', 'evidence', 'created_at')
    return {key: item[key] for key in keys if key in item}


def explain(problem):
    match = re.match(r'(.+)\.([A-Za-z_]+) 格式外字段\Z', problem)
    if match and match[2].lower() in EXPECTATIONS:
        return f'{match[1]}.{match[2]} 格式外字段：地图不记录可见性、权限、校验规则、默认值或业务结果这类预期，请删除'
    return problem


def merge(inputs, previous, output, feedback_ids):
    """按模式与上一版合并；增量模式只接受指定或过期页面及新增页面。"""
    problems = []
    written = output['pages']
    ids = [page['id'] for page in written]
    duplicated = sorted({i for i in ids if ids.count(i) > 1})
    if duplicated:
        problems.append('页面编号重复：' + '、'.join(duplicated))
    handled = [item['id'] for item in output['feedback_handled']]
    unknown = sorted(set(handled) - feedback_ids)
    if unknown or len(set(handled)) != len(handled):
        problems.append('feedback_handled 只能列出本任务 feedback.json 中的反馈且不重复：' + '、'.join(map(str, unknown)))
    if inputs['mode'] == 'full' or previous is None:
        return written, problems
    old = {page['id']: page for page in previous['pages']}
    removed = output['removed_pages']
    if set(removed) - set(old):
        problems.append('removed_pages 只能列出上一版已有的页面：' + '、'.join(sorted(set(removed) - set(old))))
    if inputs['mode'] == 'pages':
        allowed = set(inputs['pages'])
        outside = sorted((set(ids) | set(removed)) - allowed)
        if outside:
            problems.append('只更新指定页面时不能写出或删除其他页面：' + '、'.join(outside))
    else:
        allowed = {item['page'] for item in inputs['stale']}
        outside = sorted({i for i in ids if i in old and i not in allowed} | (set(removed) - allowed))
        if outside:
            problems.append('只更新过期页面时不能改写或删除未过期的页面：' + '、'.join(outside))
    replaced = {page['id']: page for page in written}
    merged = [replaced.get(page['id'], page) for page in previous['pages'] if page['id'] not in removed]
    merged += [page for page in written if page['id'] not in old]
    return merged, problems


def validate(pages, inputs, repos):
    problems = []
    languages = inputs['atlas']['languages']
    page_ids = {page['id'] for page in pages}
    dialogs = {}
    refs = set()
    for page in pages:
        if not IDENTIFIER.fullmatch(page['id']):
            problems.append(f"页面编号 {page['id']} 只能用小写字母、数字、点、下划线和连字符")
        for dialog in page['dialogs']:
            if dialog['id'] in dialogs:
                problems.append(f"弹窗编号 {dialog['id']} 在地图内重复")
            dialogs[dialog['id']] = page['id']
        local_ids = [c['id'] for c in page['controls']] + [d['id'] for d in page['dialogs']] + [t['id'] for t in page['tables']]
        for item in sorted({i for i in local_ids if local_ids.count(i) > 1}):
            problems.append(f"页面 {page['id']} 内编号 {item} 重复")
        refs |= {f"{page['id']}/{c['id']}" for c in page['controls']}
        for group, actions in [(d['id'], d['actions']) for d in page['dialogs']] + [(t['id'], t['row_actions']) for t in page['tables']]:
            refs |= {f"{page['id']}/{group}/{a['id']}" for a in actions}
    def check_texts(value, where, *, required=True):
        if required and not value:
            problems.append(f'{where} 至少要有一种语言的文案')
        seen = set()
        for text in value:
            if not text['text'].strip() or not text['lang'].strip():
                problems.append(f'{where} 有空的语言或文案')
            if languages and text['lang'] not in languages:
                problems.append(f"{where} 的语言 {text['lang']} 不在声明的 {'、'.join(languages)} 中")
            if text['lang'] in seen:
                problems.append(f"{where} 的语言 {text['lang']} 重复")
            seen.add(text['lang'])
    def check_sources(values, where):
        for value in values:
            match = SOURCE.fullmatch(value)
            if not match:
                problems.append(f'{where} 的源码位置 {value} 应写成 <别名>:<路径>[:<行>]')
                continue
            alias, relative, line = match[1], match[2], match[3]
            root = repos.get(alias)
            if root is None:
                problems.append(f'{where} 的源码位置 {value} 使用了未在 atlas.repos 中声明的仓库')
                continue
            if Path(relative).is_absolute() or '..' in Path(relative).parts:
                problems.append(f'{where} 的源码位置 {value} 必须是仓内相对路径')
                continue
            path = root / relative
            try:
                if not path.resolve().is_relative_to(root.resolve()) or not path.is_file():
                    problems.append(f'{where} 的源码位置 {value} 指向的文件不存在')
                    continue
                if line is not None:
                    total = len(path.read_bytes().splitlines())
                    if not 1 <= int(line) <= max(total, 1):
                        problems.append(f'{where} 的源码位置 {value} 行号超出文件（共 {total} 行）')
            except OSError:
                problems.append(f'{where} 的源码位置 {value} 无法读取')
    def check_control(control, where):
        check_texts(control['name'], where + ' 的名称')
        if not control['locator'].strip():
            problems.append(f'{where} 缺少定位器')
        if not IDENTIFIER.fullmatch(control['id']):
            problems.append(f"{where} 的编号只能用小写字母、数字、点、下划线和连字符")
        opens = control['opens']
        if opens is not None and opens != 'menu':
            kind, _, target = opens.partition(':')
            if kind == 'page' and target not in page_ids or kind == 'dialog' and target not in dialogs or kind not in ('page', 'dialog'):
                problems.append(f'{where} 的 opens 指向不存在的对象：{opens}')
        for request in control['requests']:
            if request['method'].upper() not in METHODS or not (request['path'].startswith('/') or request['path'].startswith('http')):
                problems.append(f"{where} 的请求 {request['method']} {request['path']} 应为 HTTP 方法加路径")
        check_sources(control['sources'], where)
    for page in pages:
        where = '页面 ' + page['id']
        check_texts(page['title'], where + ' 的标题')
        check_sources(page['sources'], where)
        if not page['sources']:
            problems.append(f'{where} 缺少源码位置')
        for step in page['reach']:
            if step['from'] != 'start' and step['from'] not in page_ids:
                problems.append(f"{where} 的到达路径起点 {step['from']} 不存在")
            if step['via'] is not None and step['via'] not in refs:
                problems.append(f"{where} 的到达路径控件 {step['via']} 不存在")
        for control in page['controls']:
            check_control(control, f"{where} 控件 {control['id']}")
        for dialog in page['dialogs']:
            label = f"{where} 弹窗 {dialog['id']}"
            check_texts(dialog['title'], label + ' 的标题', required=False)  # 弹窗可以没有标题，不逼梳理者编造
            check_sources(dialog['sources'], label)
            for field in dialog['fields']:
                check_texts(field['label'], f"{label} 字段 {field['id']} 的标签")
            for action in dialog['actions']:
                check_control(action, f"{label} 操作 {action['id']}")
        for table in page['tables']:
            label = f"{where} 表格 {table['id']}"
            check_sources(table['sources'], label)
            for column in table['columns']:
                check_texts(column['title'], f"{label} 列 {column['id']} 的标题")
            check_texts(table['empty'], label + ' 的空状态', required=False)
            for action in table['row_actions']:
                check_control(action, f"{label} 行操作 {action['id']}")
    return problems


def languages_of(pages):
    found = set()
    for page in pages:
        for text in page['title']:
            found.add(text['lang'])
    return sorted(found)


def count_controls(pages):
    return sum(len(p['controls']) + sum(len(d['actions']) + len(d['fields']) for d in p['dialogs'])
               + sum(len(t['row_actions']) for t in p['tables']) for p in pages)


def glossary(pages):
    """由页面上的多语言键汇总出的文案索引，供对照场景文案使用。"""
    found = {}
    def add(key, texts, ref):
        if not key:
            return
        item = found.setdefault(key, dict(key=key, texts={}, used_by=[]))
        for text in texts:
            item['texts'].setdefault(text['lang'], text['text'])
        if ref not in item['used_by']:
            item['used_by'].append(ref)
    for page in pages:
        for control in page['controls']:
            add(control['i18n'], control['name'], f"{page['id']}/{control['id']}")
        for dialog in page['dialogs']:
            for field in dialog['fields']:
                add(field['i18n'], field['label'], f"{page['id']}/{dialog['id']}/{field['id']}")
            for action in dialog['actions']:
                add(action['i18n'], action['name'], f"{page['id']}/{dialog['id']}/{action['id']}")
        for table in page['tables']:
            for column in table['columns']:
                add(column['i18n'], column['title'], f"{page['id']}/{table['id']}/{column['id']}")
            for action in table['row_actions']:
                add(action['i18n'], action['name'], f"{page['id']}/{table['id']}/{action['id']}")
    return [dict(item, texts=[dict(lang=k, text=v) for k, v in sorted(item['texts'].items())]) for _, item in sorted(found.items())]


def texts(value):
    return ' / '.join(f"{t['text']}（{t['lang']}）" for t in value) or '—'


def render(document):
    """人读版本；与 atlas.json 同时生成，不作为输入。"""
    lines = [f"# 交互地图：{document['entry']} · 版本 {document['version']}", '',
             f"生成时间：{document['generated_at']} · 语言：{'、'.join(document['languages']) or '—'} · 页面 {len(document['pages'])}", '']
    if document.get('notes'):
        lines += ['说明：' + document['notes'], '']
    for page in document['pages']:
        lines += [f"## {page['id']} · {texts(page['title'])}", '',
                  f"- 路由：{page['route'] or '—'}；容器：{page['container']}",
                  '- 到达：' + ('；'.join(f"{s['from']} → {s['via'] or '直接打开'}" + (f"（{s['note']}）" if s['note'] else '') for s in page['reach']) or '—'),
                  '- 就绪信号：' + ('；'.join(page['ready']) or '—'),
                  '- 源码：' + ('、'.join(page['sources']) or '—'),
                  f"- 浏览器核对：{page['verified_browser'] or '未核对'}", '']
        if page['controls']:
            lines += ['| 控件 | 角色 | 名称 | 多语言键 | 定位器 | 打开 | 请求 |', '| --- | --- | --- | --- | --- | --- | --- |']
            lines += [f"| {c['id']} | {c['role']} | {texts(c['name'])} | {c['i18n'] or '—'} | `{c['locator']}` | {c['opens'] or '—'} | "
                      f"{'；'.join(r['method'] + ' ' + r['path'] for r in c['requests']) or '—'} |" for c in page['controls']]
            lines.append('')
        for dialog in page['dialogs']:
            lines += [f"### 弹窗 {dialog['id']} · {texts(dialog['title'])}", '', f"定位：`{dialog['locator']}`", '']
            lines += [f"- 字段 {f['id']}（{f['role']}）：{texts(f['label'])} · `{f['locator']}`" for f in dialog['fields']]
            lines += [f"- 操作 {a['id']}（{a['role']}）：{texts(a['name'])} · `{a['locator']}`" for a in dialog['actions']]
            lines.append('')
        for table in page['tables']:
            lines += [f"### 表格 {table['id']}", '', f"定位：`{table['locator']}`；列：" + ('、'.join(texts(c['title']) for c in table['columns']) or '—')]
            lines += [f"- 行操作 {a['id']}：{texts(a['name'])} · `{a['locator']}`" for a in table['row_actions']]
            lines.append('')
    return '\n'.join(lines) + '\n'


def command(arguments):
    parser = argparse.ArgumentParser(prog='sop-board atlas', description='梳理 Web 入口的交互地图（只记录怎么操作，不提供预期）')
    sub = parser.add_subparsers(dest='action', required=True)
    for action in ('prepare', 'submit', 'show', 'bind', 'decide'):
        child = sub.add_parser(action)
        child.add_argument('targets', nargs='*', help='[工作区]')
        child.add_argument('--data', type=Path)
        if action == 'prepare':
            child.add_argument('--entry', required=True)
            child.add_argument('--session', required=True)
            mode = child.add_mutually_exclusive_group()
            mode.add_argument('--stale', action='store_true', help='只更新过期页面')
            mode.add_argument('--pages', help='只更新指定页面，逗号分隔')
            child.add_argument('--browser', action='store_true', help='允许只读浏览器核对')
            child.add_argument('--replace-queued', action='store_true')
        elif action == 'submit':
            child.add_argument('--task', type=int, required=True)
            child.add_argument('--session')
        elif action == 'bind':
            child.add_argument('batch', help='批次名称')
            child.add_argument('--entry')
            child.add_argument('--session', required=True)
            child.add_argument('--stale', action='store_true', help='只对照内容有变化或绑定页面有变化的场景')
            child.add_argument('--replace-queued', action='store_true')
        elif action == 'decide':
            child.add_argument('batch', help='批次名称')
            child.add_argument('--item', required=True, help='<场景>#<条目>，如 WEB-N01#T1')
            child.add_argument('--decision', required=True, choices=('sop', 'change'))
            child.add_argument('--by', help='决定人姓名')
            child.add_argument('--note', default='')
        else:
            child.add_argument('--entry')
            child.add_argument('--page')
            child.add_argument('--json', action='store_true')
    args = parser.parse_args(arguments)
    app = None
    try:
        if len(args.targets) > 1:
            raise ValueError('工作区参数过多')
        from .session import Session, identity
        app = Session(args.targets[0] if args.targets else None, args.data)
        tasks = app.tasks
        if args.action == 'decide':
            with tasks.lock:
                app.configured()
                if args.by is None:
                    raise ValueError('请用 --by 填写决定人姓名')
                result = dict(ok=True, atlas_checks=tasks.atlas.decide(args.batch, args.item, args.decision, app.person(args.by), args.note))
        elif args.action == 'bind':
            with tasks.lock:
                app.configured()
                result = prepare_bind_session(app, identity(args.session), args.batch, args.entry, args.stale, args.replace_queued)
        elif args.action == 'show':
            app.workspace.reload()
            result = show(tasks.atlas, args.entry, args.page)
            if not args.json:
                print(show_text(result))
                return 0
        elif args.action == 'submit':
            result = app.submit('atlas', args.task, owner=args.session)
        else:
            with tasks.lock:
                app.configured()
                owner = identity(args.session)
                pages = [p.strip() for p in args.pages.split(',')] if args.pages else []
                mode = 'stale' if args.stale else 'pages' if args.pages else 'full'
                declaration(app.workspace, args.entry)
                result = prepare_session(app, owner, args.entry, mode, pages, args.browser, args.replace_queued)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get('ok') else 1
    except APIError as error:
        result = dict(ok=False, error_code=error.code, problems=error.problems)
    except (OSError, ValueError, TypeError, KeyError, RecursionError, sqlite3.Error) as error:
        result = dict(ok=False, problems=[dict(field='atlas', problem=str(error))])
    finally:
        if app:
            app.close()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1


def prepare_session(app, owner, entry, mode, pages, browser, replace_queued):
    """会话认领梳理任务：同一入口只允许一个进行中的梳理；用浏览器时与执行、修复互斥。"""
    from .kernel import session_scheduling_tasks
    tasks = app.tasks
    with app.store.connection() as db:
        rows = session_scheduling_tasks(db)
        claims = {r['task']: r['session_id'] for r in db.execute('SELECT * FROM session_tasks')}
        inputs = {r['id']: json.loads(r['input_json']) for r in db.execute("SELECT id,input_json FROM tasks WHERE kind='atlas' AND workspace=?", (app.key,))}
    own, queued = None, []
    for row in rows:
        if row['workspace'] != app.key or row['kind'] != 'atlas' or inputs.get(row['id'], {}).get('entry') != entry:
            continue
        if inputs[row['id']].get('mode') == 'bind':
            continue
        claimant = claims.get(row['id'])
        if claimant and claimant != owner:
            reject(409, 'task-owned', 'task', f"梳理任务 #{row['id']} 正由其他会话处理")
        if claimant:
            own = row
        elif row['status'] == S.RUNNING or row.get('worker'):
            reject(409, 'task-running', 'task', f"看板梳理任务 #{row['id']} 正在运行")
        else:
            queued.append(row)
    if own:
        return app.descriptor(app.raw(own['id']))
    if browser:
        busy = [r for r in rows if r['workspace'] == app.key and r['kind'] in ('execute', 'fix') and
                (r['status'] in (S.RUNNING, S.SESSION, S.VERIFYING) or r.get('worker'))]
        if busy:
            reject(409, 'task-blocked', 'task', f"任务 #{busy[0]['id']}（{busy[0]['status']}）可能正在使用浏览器，用浏览器核对须等它结束")
    if queued and not replace_queued:
        reject(409, 'task-queued', 'task', '看板梳理任务 ' + '、'.join('#' + str(r['id']) for r in queued) + ' 已排队；使用者同意取消后可加 --replace-queued')
    for row in queued:
        tasks.action(row['id'], 'cancel')
    task_id = tasks.atlas.enqueue(tasks.atlas.inputs(entry, mode, pages, browser), '会话梳理交互')
    app.owner = owner
    app.claim(task_id)
    task = app.raw(task_id)
    tasks.types['atlas'].prepare(task)
    return app.descriptor(app.raw(task_id))


def prepare_bind_session(app, owner, batch, entry, stale, replace_queued):
    """会话认领对照任务：同一批次只允许一个进行中的对照。"""
    from .kernel import session_scheduling_tasks
    tasks = app.tasks
    app.sync()
    with app.store.connection() as db:
        rows = [r for r in session_scheduling_tasks(db) if r['workspace'] == app.key and r['kind'] == 'atlas' and r['batch'] == batch]
        claims = {r['task']: r['session_id'] for r in db.execute('SELECT * FROM session_tasks')}
    queued = []
    for row in rows:
        claimant = claims.get(row['id'])
        if claimant and claimant != owner:
            reject(409, 'task-owned', 'task', f"对照任务 #{row['id']} 正由其他会话处理")
        if claimant:
            return app.descriptor(app.raw(row['id']))
        if row['status'] == S.RUNNING or row.get('worker'):
            reject(409, 'task-running', 'task', f"看板对照任务 #{row['id']} 正在运行")
        queued.append(row)
    if queued and not replace_queued:
        reject(409, 'task-queued', 'task', '看板对照任务 ' + '、'.join('#' + str(r['id']) for r in queued) + ' 已排队；使用者同意取消后可加 --replace-queued')
    for row in queued:
        tasks.action(row['id'], 'cancel')
    task_id = tasks.atlas.enqueue_bind(tasks.atlas.bind_inputs(batch, entry, stale), '会话对照界面')
    app.owner = owner
    app.claim(task_id)
    tasks.types['atlas'].prepare(app.raw(task_id))
    return app.descriptor(app.raw(task_id))


def show(atlas, entry=None, page=None):
    summary = atlas.summary()
    if entry is not None:
        summary = [item for item in summary if item['entry'] == entry]
        if not summary:
            reject(404, 'atlas-not-declared', 'entry', f'入口 {entry} 没有声明 atlas')
    result = dict(ok=True, entries=summary)
    if page is not None:
        if entry is None or summary[0]['version'] is None:
            raise ValueError('查看页面须指定已有地图的 --entry')
        document = atlas.load(entry, summary[0]['version'])
        found = next((p for p in document['pages'] if p['id'] == page), None)
        if found is None:
            reject(404, 'atlas-page-not-found', 'page', f'地图中没有页面 {page}')
        result['page'] = found
    return result


def show_text(result):
    lines = []
    for item in result['entries']:
        if item['version'] is None:
            lines.append(f"入口 {item['entry']}：尚无交互地图（前端仓库 {'、'.join(item['repos'])}）")
            continue
        lines.append(f"入口 {item['entry']}：版本 {item['version']} · {item['created_at']} · 页面 {item['pages']} · 控件 {item['controls']}")
        if item['stale']:
            lines.append('  过期页面：' + '；'.join(f"{s['page']}（{'、'.join(s['files'])}）" for s in item['stale']))
        if item['feedback']:
            lines.append(f"  待处理反馈 {len(item['feedback'])} 条：" + '；'.join(f"#{f['id']} {f['page']} {f['kind']}" for f in item['feedback']))
    if 'page' in result:
        lines.append(json.dumps(result['page'], ensure_ascii=False, indent=2))
    return '\n'.join(lines)
