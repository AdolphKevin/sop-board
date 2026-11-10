"""编写类工作：收件箱、合并、向测试同学提问、业务 SOP 编写与登记、草稿批注与确认。

场景文件与用例文件都在产品仓库中，由会话直接编辑，开发者审核提交；看板不保存场景版本，也不回写场景内容。
确认人自动取本机操作者；别人确认时用 --as，测试同学的答复以答复人为确认人。
"""
from datetime import datetime
from importlib.resources import files
import hashlib
import json
from pathlib import Path
import re
import shutil
from string import Template

import yaml

from . import config, scenarios
from .errors import APIError, reject
from .store import dumps, now

SOP_HEADINGS = ('业务目标与范围', '角色', '业务规则', '流程图', '禁止的副作用', '未覆盖范围')
CONFIRMATION = '## 确认记录'


# ---- 收件箱 ----

def validate_upload(kind, filename, content):
    if kind not in ('cases', 'sop'):
        reject(422, 'upload-invalid', 'kind', '类型应为 cases 或 sop')
    if not isinstance(filename, str) or not filename.strip() or '/' in filename or '\\' in filename or '\0' in filename:
        reject(422, 'upload-invalid', 'filename', '文件名不能为空或含路径')
    if not isinstance(content, str) or not content.strip():
        reject(422, 'upload-invalid', 'content', '文件内容为空或不是 UTF-8 文本')
    if len(content.encode()) > 2 * 1024 * 1024:
        reject(413, 'upload-too-large', 'content', '文件不能超过 2 MiB')
    if kind == 'cases':
        scenarios.load_cases(content)
    else:
        missing = [h for h in SOP_HEADINGS if not re.search(rf'^#{{1,3}}\s*{re.escape(h)}\s*$', content, re.M)]
        if missing:
            reject(422, 'sop-invalid', 'content', '业务 SOP 缺少标题：' + '、'.join(missing))


def inbox_add(store, kind, filename, content, *, by):
    validate_upload(kind, filename, content)
    with store.transaction() as db:
        cursor = db.execute("INSERT INTO inbox(kind,filename,sha256,content,uploaded_by,status,created_at) VALUES (?,?,?,?,?,'pending',?)",
                            (kind, filename, hashlib.sha256(content.encode()).hexdigest(), content, by, now()))
    return dict(ok=True, inbox=cursor.lastrowid, kind=kind, filename=filename, uploaded_by=by, status='待合并')


def inbox_rows(db, *, status=None):
    query = 'SELECT id, kind, filename, uploaded_by, status, work, note, created_at FROM inbox'
    rows = db.execute(query + (' WHERE status=?' if status else '') + ' ORDER BY id', (status,) if status else ())
    return [dict(row) for row in rows]


def inbox_show(store, item_id):
    with store.connection() as db:
        row = db.execute('SELECT * FROM inbox WHERE id=?', (item_id,)).fetchone()
    if row is None:
        reject(404, 'inbox-not-found', 'inbox', f'收件箱没有 #{item_id}')
    return dict(row)


def inbox_close(store, item_id, status, note=None):
    row = inbox_show(store, item_id)
    if row['status'] != 'pending':
        reject(409, 'inbox-state-invalid', 'inbox', f"#{item_id} 已是 {row['status']}")
    with store.transaction() as db:
        db.execute('UPDATE inbox SET status=?, note=?, ended_at=? WHERE id=?', (status, note, now(), item_id))
    return dict(ok=True, inbox=item_id, status=status)


# ---- 提问与答复 ----

def ask(store, about, text, *, by, work=None):
    if not re.fullmatch(r'inbox:[0-9]+|[A-Za-z0-9][A-Za-z0-9._-]*#[A-Za-z0-9][A-Za-z0-9_.-]*|[A-Za-z0-9][A-Za-z0-9._-]*', about or ''):
        reject(422, 'question-invalid', 'about', '问题须关联 inbox:<编号>、<文件>#<场景编号> 或场景文件名')
    if not isinstance(text, str) or not text.strip():
        reject(422, 'question-invalid', 'text', '问题不能为空')
    with store.transaction() as db:
        cursor = db.execute('INSERT INTO questions(work,about,text,asked_by,asked_at) VALUES (?,?,?,?,?)',
                            (work, about, text.strip(), by, now()))
    return dict(ok=True, question=cursor.lastrowid, about=about)


def answer(store, question_id, text, *, by):
    if not isinstance(text, str) or not text.strip():
        reject(422, 'answer-invalid', 'answer', '答复不能为空')
    if not isinstance(by, str) or not by.strip() or len(by) > 64:
        reject(422, 'answer-invalid', 'by', '请填写答复人姓名')
    with store.transaction() as db:
        row = db.execute('SELECT * FROM questions WHERE id=?', (question_id,)).fetchone()
        if row is None:
            reject(404, 'question-not-found', 'question', f'问题不存在：#{question_id}')
        db.execute('UPDATE questions SET answer=?, answered_by=?, answered_at=? WHERE id=?', (text.strip(), by.strip(), now(), question_id))
    return dict(ok=True, question=question_id, answered_by=by.strip())


def answers(store, *, about=None, unanswered=False):
    with store.connection() as db:
        rows = [dict(r) for r in db.execute('SELECT * FROM questions ORDER BY id')]
    return [r for r in rows if (about is None or r['about'] == about) and (not unanswered or r['answer'] is None)]


# ---- 合并 ----

def scene_summary(found):
    return {key: dict(fingerprint=s['fingerprint'], actor=s['actor'], verify=s['verify'], polarity=s['polarity'])
            for key, s in found['scenarios'].items()}


def work_dir(project, kind, work_id):
    return project.data / {'merge': 'merges', 'author': 'authoring', 'draft': 'drafts', 'atlas': 'atlas'}[kind] / str(work_id)


def create_work(context, kind, *, session=None, scope=()):
    operator = config.require_operator()
    with context.store.transaction() as db:
        cursor = db.execute("INSERT INTO work(kind,checkout,status,session,operator,dir,scope,created_at) VALUES (?,?,'prepared',?,?,'',?,?)",
                            (kind, context.checkout.id, session, operator, dumps(list(scope)), now()))
        work_id = cursor.lastrowid
    directory = work_dir(context.project, kind, work_id)
    directory.mkdir(parents=True)
    with context.store.transaction() as db:
        db.execute('UPDATE work SET dir=? WHERE id=?', (str(directory), work_id))
    return work_id, directory, operator


def load_work(context, kind, work_id=None):
    with context.store.connection() as db:
        if work_id is None:
            row = db.execute("SELECT * FROM work WHERE kind=? AND checkout=? AND status='prepared' ORDER BY id DESC LIMIT 1",
                             (kind, context.checkout.id)).fetchone()
        else:
            row = db.execute('SELECT * FROM work WHERE id=? AND kind=?', (work_id, kind)).fetchone()
    if row is None:
        reject(404, 'work-not-found', 'work', '没有进行中的工作；先运行对应的 prepare')
    if row['status'] != 'prepared':
        reject(409, 'work-state-invalid', 'work', f"#{row['id']} 的状态是 {row['status']}")
    return dict(row)


def merge_prepare(context, inbox_ids):
    project = context.project
    with context.store.connection() as db:
        pending = inbox_rows(db, status='pending')
    chosen = [r for r in pending if r['kind'] == 'cases' and (not inbox_ids or r['id'] in inbox_ids)]
    missing = sorted(set(inbox_ids or []) - {r['id'] for r in chosen})
    if missing:
        reject(409, 'inbox-invalid', 'inbox', '这些收件箱条目不是待合并的用例：' + '、'.join(f'#{i}' for i in missing))
    if not chosen:
        reject(409, 'nothing-to-merge', 'inbox', '收件箱没有待合并的用例')
    found = scenarios.scan(context.checkout.scenarios_dir(project), set(project.entries))
    invalid = {name: info['problems'] for name, info in found['files'].items() if info['problems']}
    if invalid:
        reject(409, 'scenario-file-invalid', 'scenarios', '先修正不合格的场景文件：' + '、'.join(invalid))
    work_id, directory, operator = create_work(context, 'merge', scope=[f"inbox:{r['id']}" for r in chosen])
    (directory / 'inbox').mkdir()
    for row in chosen:
        item = inbox_show(context.store, row['id'])
        (directory / 'inbox' / f"{row['id']}-{row['filename']}").write_text(item['content'], encoding='utf-8')
    related = [q for q in answers(context.store) if q['about'] in {f"inbox:{r['id']}" for r in chosen} or '#' in q['about']]
    (directory / 'answers.json').write_text(dumps(related), encoding='utf-8')
    (directory / 'before.json').write_text(dumps(scene_summary(found)), encoding='utf-8')
    prompt = Template(files('sop_board').joinpath('prompts', 'merge.md').read_text(encoding='utf-8')).substitute(
        work=work_id, directory=directory, scenarios=context.checkout.scenarios_dir(project), operator=operator,
        inbox=' '.join(f"inbox:{r['id']}" for r in chosen))
    (directory / 'prompt.md').write_text(prompt, encoding='utf-8')
    return dict(ok=True, work=work_id, directory=str(directory), prompt=str(directory / 'prompt.md'),
                inbox=[dict(id=r['id'], filename=r['filename'], uploaded_by=r['uploaded_by']) for r in chosen])


def merge_submit(context, work_id=None):
    project = context.project
    work = load_work(context, 'merge', work_id)
    found = scenarios.scan(context.checkout.scenarios_dir(project), set(project.entries))
    problems = [dict(p, file=name) for name, info in found['files'].items() for p in info['problems']]
    if problems:
        raise APIError(422, 'scenario-file-invalid', problems)
    before = json.loads((Path(work['dir']) / 'before.json').read_text(encoding='utf-8'))
    after = scene_summary(found)
    added = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))
    changed = sorted(k for k in set(before) & set(after) if before[k]['fingerprint'] != after[k]['fingerprint'])
    attributes = []
    for key in sorted(set(before) & set(after)):
        for field in ('actor', 'verify', 'polarity'):
            if before[key][field] != after[key][field]:
                attributes.append(dict(scenario=key, field=field, before=before[key][field], after=after[key][field]))
    inbox_ids = [int(item.split(':', 1)[1]) for item in json.loads(work['scope'])]
    diff = dict(added=added, removed=removed, changed=changed, attribute_changes=attributes,
                total_before=len(before), total_after=len(after))
    with context.store.transaction() as db:
        for item_id in inbox_ids:
            db.execute("UPDATE inbox SET status='merged', work=?, ended_at=? WHERE id=? AND status='pending'", (work['id'], now(), item_id))
        db.execute("UPDATE work SET status='submitted', ended_at=?, result=? WHERE id=?", (now(), dumps(diff), work['id']))
    return dict(ok=True, work=work['id'], inbox=inbox_ids, scenario_diff=diff,
                next='场景与用例文件的改动留在仓库中，请审核后提交')


# ---- 业务 SOP ----

def sop_prepare(context, sources):
    if not sources:
        reject(422, 'source-required', 'source', '至少给出一份资料')
    if len(sources) > 32:
        reject(422, 'source-invalid', 'source', '资料最多 32 份')
    paths = []
    for value in sources:
        path = Path(value).expanduser().resolve()
        if not path.is_file() or path.stat().st_size > 20 * 1024 * 1024:
            reject(422, 'source-invalid', 'source', f'资料不存在或超过 20 MiB：{value}')
        paths.append(path)
    work_id, directory, operator = create_work(context, 'author', scope=[str(p) for p in paths])
    listing = []
    for index, path in enumerate(paths, 1):
        target = directory / 'sources' / f'S{index}' / path.name
        target.parent.mkdir(parents=True)
        shutil.copyfile(path, target)
        listing.append(dict(id=f'S{index}', name=path.name, path=str(target), sha256=hashlib.sha256(path.read_bytes()).hexdigest()))
    (directory / 'sources.json').write_text(dumps(listing), encoding='utf-8')
    target_dir = context.checkout.scenarios_dir(context.project)
    prompt = Template(files('sop_board').joinpath('prompts', 'author-sop.md').read_text(encoding='utf-8')).substitute(
        work=work_id, directory=directory, target=target_dir, headings='、'.join(SOP_HEADINGS))
    (directory / 'prompt.md').write_text(prompt, encoding='utf-8')
    return dict(ok=True, work=work_id, directory=str(directory), prompt=str(directory / 'prompt.md'), sources=listing)


def sop_check(text):
    missing = [h for h in SOP_HEADINGS if not re.search(rf'^##\s*{re.escape(h)}\s*$', text, re.M)]
    problems = [f'缺少标题“{h}”' for h in missing]
    if '## 业务规则' in text and not re.search(r'^#{3,4}\s*R[0-9]+', text, re.M):
        problems.append('业务规则下没有 R 编号的规则小节')
    if CONFIRMATION in text:
        problems.append('正文已有确认记录；确认信息由命令追加')
    return problems


def sop_submit(context, file, *, by, work_id=None):
    work = load_work(context, 'author', work_id)
    path = Path(file).expanduser().resolve()
    target_dir = context.checkout.scenarios_dir(context.project).resolve()
    if not path.is_file() or not path.is_relative_to(target_dir) or path.suffix != '.md':
        reject(422, 'sop-invalid', 'file', f'SOP 须是场景目录 {target_dir} 下的 .md 文件')
    text = path.read_text(encoding='utf-8')
    problems = sop_check(text)
    if problems:
        reject(422, 'sop-invalid', 'content', '；'.join(problems))
    listing = json.loads((Path(work['dir']) / 'sources.json').read_text(encoding='utf-8'))
    stamp = datetime.now().astimezone().strftime('%Y-%m-%d %H:%M')
    record = [CONFIRMATION, '', f'- 确认人：{by}（{stamp}，sop-board sop submit）'] + \
             [f"- 资料 {s['id']}：{s['name']}（sha256 {s['sha256'][:16]}）" for s in listing]
    path.write_text(text.rstrip() + '\n\n' + '\n'.join(record) + '\n', encoding='utf-8')
    with context.store.transaction() as db:
        db.execute("UPDATE work SET status='submitted', ended_at=?, result=? WHERE id=?",
                   (now(), dumps(dict(file=str(path), confirmed_by=by)), work['id']))
    return dict(ok=True, work=work['id'], file=str(path), confirmed_by=by)


def sop_files(context):
    directory = context.checkout.scenarios_dir(context.project)
    return sorted(directory.glob('*.md')) if directory and directory.is_dir() else []


# ---- 草稿 ----

def annotate(context, file, target, text, *, by):
    found = scenarios.scan(context.checkout.scenarios_dir(context.project))
    info = found['files'].get(file)
    if info is None or info.get('status') != 'draft':
        reject(409, 'draft-not-found', 'file', f'{file} 不是草稿场景文件')
    if target != 'file' and target not in {s for n in info['nodes'] for s in n['scenarios']}:
        reject(422, 'annotation-invalid', 'target', f'{file} 中没有场景 {target}')
    if not isinstance(text, str) or not text.strip() or len(text) > 4000:
        reject(422, 'annotation-invalid', 'text', '批注不能为空，最长 4000 字')
    if not isinstance(by, str) or not by.strip() or len(by) > 64:
        reject(422, 'annotation-invalid', 'by', '请填写批注人姓名')
    with context.store.transaction() as db:
        cursor = db.execute("INSERT INTO annotations(file,target,text,author,status,created_at) VALUES (?,?,?,?,'open',?)",
                            (file, target, text.strip(), by.strip(), now()))
    return dict(ok=True, annotation=cursor.lastrowid)


def annotations(store, *, file=None, include_resolved=False):
    with store.connection() as db:
        rows = [dict(r) for r in db.execute('SELECT * FROM annotations ORDER BY id')]
    return [r for r in rows if (file is None or r['file'] == file) and (include_resolved or r['status'] == 'open')]


def resolve_annotation(store, annotation_id, *, reject_it=False, note=None):
    with store.transaction() as db:
        row = db.execute('SELECT * FROM annotations WHERE id=?', (annotation_id,)).fetchone()
        if row is None:
            reject(404, 'annotation-not-found', 'annotation', f'批注不存在：#{annotation_id}')
        db.execute('UPDATE annotations SET status=?, note=?, resolved_at=? WHERE id=?',
                   ('rejected' if reject_it else 'adopted', note, now(), annotation_id))
    return dict(ok=True, annotation=annotation_id, status='rejected' if reject_it else 'adopted')


def draft_confirm(context, file, *, by):
    directory = context.checkout.scenarios_dir(context.project)
    path = directory / f'{file}.yaml'
    if not path.is_file():
        reject(404, 'draft-not-found', 'file', f'场景文件不存在：{file}')
    raw = path.read_text(encoding='utf-8')
    document = scenarios.load_scenarios(raw, set(context.project.entries))
    if document['metadata'].get('status') != 'draft':
        reject(409, 'draft-not-found', 'file', f'{file} 不是草稿')
    open_items = annotations(context.store, file=file)
    if open_items:
        reject(409, 'draft-annotations-open', 'annotations', '还有未处理的批注：' + '、'.join(f"#{a['id']}" for a in open_items))
    lines = raw.splitlines(keepends=True)
    status_line = next((i for i, line in enumerate(lines) if re.match(r'^\s+status:\s*["\']?draft["\']?\s*(#.*)?$', line)), None)
    sources_line = next((i for i, line in enumerate(lines) if re.match(r'^\s+sources:\s*$', line)), None)
    if status_line is None or sources_line is None:
        reject(422, 'draft-format', 'file', 'metadata 中须有单独一行的 status: draft 与块格式的 sources:')
    indent = re.match(r'^(\s*)', lines[status_line])[1]
    lines[status_line] = f'{indent}status: confirmed\n'
    item_indent = indent + '  '
    stamp = datetime.now().astimezone().strftime('%Y-%m-%d')
    entry = (f'{item_indent}- ref: "sop-board draft confirm"\n{item_indent}  version: "{stamp}"\n'
             f'{item_indent}  confirmed_by: {json.dumps(by, ensure_ascii=False)}\n')
    lines.insert(sources_line + 1, entry)
    updated = ''.join(lines)
    scenarios.load_scenarios(updated, set(context.project.entries))
    path.write_text(updated, encoding='utf-8')
    return dict(ok=True, file=file, status='confirmed', confirmed_by=by)
