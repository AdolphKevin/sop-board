"""编写类工作：收件箱、合并、向测试同学提问、业务 SOP 编写与登记、草稿审阅（批注、同意）与确认。

场景文件与用例文件都在产品仓库中，由会话直接编辑，开发者审核提交；看板不保存场景版本，也不回写场景内容。
确认人自动取本机操作者；别人确认时用 --as，测试同学的答复以答复人为确认人。
草稿例外：确认人是在看板上同意草稿场景的人，draft confirm 没有 --as。
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

from . import config, scenarios, status
from .errors import APIError, reject
from .sop_markdown import FENCE, HEADING, inline, render
from .store import dumps, now
from .time_format import local_time

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
    with store.records_tx() as db:
        number = db.insert("INSERT INTO inbox(product,kind,filename,sha256,content,uploaded_by,status,created_at) VALUES (?,?,?,?,?,?,'pending',?)",
                           (store.product, kind, filename, hashlib.sha256(content.encode()).hexdigest(), content, by, now()))
    return dict(ok=True, inbox=number, kind=kind, filename=filename, uploaded_by=by, status='待合并')


def inbox_rows(db, product, *, status=None):
    """收件箱（业务记录库，同一产品的各台机器共用一份）。"""
    query = 'SELECT id, kind, filename, uploaded_by, status, work, note, created_at FROM inbox WHERE product=?'
    rows = db.execute(query + (' AND status=?' if status else '') + ' ORDER BY id', (product, status) if status else (product,))
    return [dict(row) for row in rows]


def inbox_show(store, item_id):
    with store.records() as db:
        row = db.one('SELECT * FROM inbox WHERE id=? AND product=?', (item_id, store.product))
    if row is None:
        reject(404, 'inbox-not-found', 'inbox', f'收件箱没有 #{item_id}')
    return dict(row)


def inbox_close(store, item_id, status, note=None):
    row = inbox_show(store, item_id)
    if row['status'] != 'pending':
        reject(409, 'inbox-state-invalid', 'inbox', f"#{item_id} 已是 {row['status']}")
    with store.records_tx() as db:
        # 条件更新：别的机器同时处理同一条时只有一次生效。
        if db.execute("UPDATE inbox SET status=?, note=?, ended_at=? WHERE id=? AND status='pending'",
                      (status, note, now(), item_id)).rowcount == 0:
            reject(409, 'inbox-state-invalid', 'inbox', f'#{item_id} 已被处理')
    return dict(ok=True, inbox=item_id, status=status)


# ---- 提问与答复 ----

def ask(store, about, text, *, by, work=None):
    """向测试同学提问：挂在业务记录库，同一产品的各台机器的看板“待答复”都能看到。work 是本机的工作编号。"""
    if not re.fullmatch(r'inbox:[0-9]+|[A-Za-z0-9][A-Za-z0-9._-]*#[A-Za-z0-9][A-Za-z0-9_.-]*|[A-Za-z0-9][A-Za-z0-9._-]*', about or ''):
        reject(422, 'question-invalid', 'about', '问题须关联 inbox:<编号>、<文件>#<场景编号> 或场景文件名')
    if not isinstance(text, str) or not text.strip():
        reject(422, 'question-invalid', 'text', '问题不能为空')
    with store.records_tx() as db:
        number = db.insert('INSERT INTO questions(product,machine,work,about,text,asked_by,asked_at) VALUES (?,?,?,?,?,?,?)',
                           (store.product, store.machine['id'], work, about, text.strip(), by, now()))
    return dict(ok=True, question=number, about=about)


def answer(store, question_id, text, *, by):
    if not isinstance(text, str) or not text.strip():
        reject(422, 'answer-invalid', 'answer', '答复不能为空')
    if not isinstance(by, str) or not by.strip() or len(by) > 64:
        reject(422, 'answer-invalid', 'by', '请填写答复人姓名')
    with store.records_tx() as db:
        row = db.one('SELECT * FROM questions WHERE id=? AND product=?', (question_id, store.product))
        if row is None:
            reject(404, 'question-not-found', 'question', f'问题不存在：#{question_id}')
        db.execute('UPDATE questions SET answer=?, answered_by=?, answered_at=? WHERE id=?', (text.strip(), by.strip(), now(), question_id))
    return dict(ok=True, question=question_id, answered_by=by.strip())


def answers(store, *, about=None, unanswered=False):
    with store.records() as db:
        rows = [dict(r) for r in db.execute('SELECT * FROM questions WHERE product=? ORDER BY id', (store.product,))]
    return [r for r in rows if (about is None or r['about'] == about) and (not unanswered or r['answer'] is None)]


# ---- 合并 ----

def scene_summary(found):
    return {key: dict(fingerprint=s['fingerprint'], actor=s['actor'], verify=s['verify'], polarity=s['polarity'])
            for key, s in found['scenarios'].items()}


def work_dir(project, kind, work_id):
    return project.data / {'merge': 'merges', 'author': 'authoring', 'draft': 'drafts', 'atlas': 'atlas'}[kind] / str(work_id)


def create_work(context, kind, *, session=None, scope=()):
    """编写类工作的本机部分；提交时由 works.finish 把业务部分登记进业务记录库。"""
    operator = config.require_operator()
    with context.store.local_tx() as db:
        cursor = db.execute("INSERT INTO local_work(kind,checkout,status,session,operator,dir,scope,created_at) VALUES (?,?,'prepared',?,?,'',?,?)",
                            (kind, context.checkout.id, session, operator, dumps(list(scope)), now()))
        work_id = cursor.lastrowid
    from . import workdirs
    directory = workdirs.create(work_dir(context.project, kind, work_id).parent, work_id)
    with context.store.local_tx() as db:
        db.execute('UPDATE local_work SET dir=? WHERE id=?', (str(directory), work_id))
    return work_id, directory, operator


def load_work(context, kind, work_id=None):
    with context.store.local() as db:
        if work_id is None:
            state = '' if kind == 'author' else " AND status='prepared'"
            row = db.execute('SELECT * FROM local_work WHERE kind=? AND checkout=?' + state + ' ORDER BY id DESC LIMIT 1',
                             (kind, context.checkout.id)).fetchone()
        else:
            row = db.execute('SELECT * FROM local_work WHERE id=? AND kind=?', (work_id, kind)).fetchone()
    if row is None:
        reject(404, 'work-not-found', 'work', '没有进行中的工作；先运行对应的 prepare')
    allowed = ('prepared', 'submitted') if kind == 'author' else ('prepared',)
    if row['status'] not in allowed:
        reject(409, 'work-state-invalid', 'work', f"#{row['id']} 的状态是 {row['status']}")
    return dict(row)


def merge_prepare(context, inbox_ids):
    project = context.project
    with context.store.records() as db:
        pending = inbox_rows(db, context.store.product, status='pending')
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
    from . import works
    from .checkouts import branch_of
    store = context.store
    with store.records_tx() as db:
        record, _ = works.publish(db, store, work, checkout=context.checkout.name, branch=branch_of(project, context.checkout), result=diff)
        for item_id in inbox_ids:
            db.execute("UPDATE inbox SET status='merged', work=?, ended_at=? WHERE id=? AND product=? AND status='pending'",
                       (record, now(), item_id, store.product))
    with store.local_tx() as db:
        db.execute("UPDATE local_work SET status='submitted', ended_at=?, result=?, record=? WHERE id=?",
                   (now(), dumps(diff), record, work['id']))
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
    """一次编写工作可分别提交多份 SOP（新 SOP 与随之改正的已有 SOP），结果累计为 files 清单。"""
    work = load_work(context, 'author', work_id)
    path = Path(file).expanduser().resolve()
    target_dir = context.checkout.scenarios_dir(context.project).resolve()
    if not path.is_file() or not path.is_relative_to(target_dir) or path.suffix != '.md':
        reject(422, 'sop-invalid', 'file', f'SOP 须是场景目录 {target_dir} 下的 .md 文件')
    text = path.read_text(encoding='utf-8')
    problems = sop_check(text)
    if problems:
        reject(422, 'sop-invalid', 'content', '；'.join(problems))
    earlier = json.loads(work['result']).get('files', []) if work['result'] else []
    submitted = [item for item in earlier if item['file'] != path.name]
    listing = json.loads((Path(work['dir']) / 'sources.json').read_text(encoding='utf-8'))
    confirmed_at = now()
    stamp = datetime.fromisoformat(confirmed_at).astimezone().strftime('%Y-%m-%d %H:%M')
    record = [CONFIRMATION, '', f'- 确认人：{by}（{stamp}，sop-board sop submit）'] + \
             [f"- 资料 {s['id']}：{s['name']}（sha256 {s['sha256'][:16]}）" for s in listing]
    path.write_text(text.rstrip() + '\n\n' + '\n'.join(record) + '\n', encoding='utf-8')
    from . import works
    from .checkouts import branch_of
    submitted.append(dict(file=path.name, confirmed_by=by, confirmed_at=confirmed_at))
    works.finish(context.store, work, checkout=context.checkout.name, branch=branch_of(context.project, context.checkout),
                 result=dict(files=submitted))
    return dict(ok=True, work=work['id'], file=str(path), confirmed_by=by, files=submitted)


def sop_files(context):
    directory = context.checkout.scenarios_dir(context.project)
    return sorted(directory.glob('*.md')) if directory and directory.is_dir() else []


# ---- 草稿 ----
# 测试同学在工作台对照检查清单与 SOP 规则原文审阅草稿：批注，或逐条、按节点同意。
# 审阅状态见 status.draft_reviews；同意按 文件 + 场景 + 指纹 记在 draft_approvals，不写场景文件、不区分检出。

CHECKLIST = ('场景是否忠实于 SOP 规则', '预期能否直接观察并带具体值', '禁止副作用是否写全', '起点数据是否具体',
             '是否漏了反向、边界或与已有业务交叉的场景')


def signer_name(by, field='by'):
    if not isinstance(by, str) or not by.strip() or len(by.strip()) > 64:
        reject(422, 'signer-required', field, '请先填写姓名（顶栏“署名”，最长 64 字）')
    return by.strip()


def draft_scan(context):
    return scenarios.scan(context.checkout.scenarios_dir(context.project), set(context.project.entries))


def draft_info(found, file):
    info = found['files'].get(file)
    if info is None or info.get('status') != 'draft' or info['problems']:
        reject(404, 'draft-not-found', 'file', f'{file} 不是草稿场景文件（或文件不合格）')
    return info


def review_files(context, file=None):
    """草稿文件的审阅进度：每条场景的审阅状态与同意人、未处理的批注、能否确认。"""
    found = draft_scan(context)
    names = [name for name, info in sorted(found['files'].items())
             if info.get('status') == 'draft' and not info['problems'] and file in (None, name)]
    scenes = {k: s for k, s in found['scenarios'].items() if s['file'] in names}
    with context.store.records() as db:
        reviews = status.draft_reviews(db, context.store.product, scenes)
        notes = [dict(r) for r in db.execute("SELECT * FROM annotations WHERE product=? AND status='open' ORDER BY id",
                                             (context.store.product,))]
    result = []
    for name in names:
        items = []
        for node in found['files'][name]['nodes']:
            for sid in node['scenarios']:
                review = reviews[scenarios.key(name, sid)]
                text = review['label'] + (f"（{'、'.join(review['approvers'])}）" if review['state'] == 'approved' else '')
                items.append(dict(scenario=sid, node=node['id'], review=review['state'], label=review['label'],
                                  approvers=review['approvers'], text=text,
                                  approvals=[dict(id=a['id'], by=a['approved_by'], at=a['approved_at']) for a in review['approvals']]))
        progress = {state: sum(i['review'] == state for i in items) for state in status.DRAFT_REVIEW}
        open_notes = [dict(id=n['id'], target=n['target'], text=n['text'], author=n['author']) for n in notes if n['file'] == name]
        ordered = sorted((a for i in items for a in i['approvals']), key=lambda a: a['id'])
        result.append(dict(file=name, total=len(items), progress=progress, scenarios=items, open_annotations=open_notes,
                           approvers=list(dict.fromkeys(a['by'] for a in ordered)),
                           ready=bool(items) and progress['approved'] == len(items) and not open_notes))
    return result


def approve(context, file, ids, *, by, fingerprints=None, node=None):
    """同意草稿场景。单条（node 为空）有未处理批注或内容已变化时拒绝；按节点同意时只同意组内待审场景，
    有未处理批注或内容已变化的跳过并写明原因。同一人在同一指纹上重复同意不重复登记。"""
    by = signer_name(by)
    found = draft_scan(context)
    info = draft_info(found, file)
    if node is not None:
        group = next((n for n in info['nodes'] if n['id'] == node), None)
        if group is None:
            reject(404, 'draft-node-not-found', 'node', f'{file} 中没有节点 {node}')
        ids = group['scenarios']
    elif not ids:
        reject(422, 'approval-invalid', 'scenario', '须给出要同意的场景')
    fingerprints = fingerprints if isinstance(fingerprints, dict) else {}
    approved, skipped, already = [], [], []
    store = context.store
    with store.records_tx() as db:
        reviews = status.draft_reviews(db, store.product, {scenarios.key(file, sid): found['scenarios'].get(scenarios.key(file, sid))
                                            for sid in ids if scenarios.key(file, sid) in found['scenarios']})
        for sid in ids:
            scene = found['scenarios'].get(scenarios.key(file, sid))
            if scene is None:
                reject(404, 'scenario-not-found', 'scenario', f'{file} 中没有场景 {sid}')
            review = reviews[scenarios.key(file, sid)]
            seen = fingerprints.get(sid)
            if seen is not None and seen != scene['fingerprint']:
                reason = '场景内容已变化，刷新后重新审阅'
                if node is None:
                    reject(409, 'draft-changed', 'fingerprint', f'{sid}：{reason}')
                skipped.append(dict(scenario=sid, reason=reason))
                continue
            if review['state'] == 'annotated':
                reason = '有未处理的批注，处理后才能同意'
                if node is None:
                    reject(409, 'draft-annotations-open', 'scenario', f'{sid}：{reason}')
                skipped.append(dict(scenario=sid, reason=reason))
                continue
            if node is not None and review['state'] == 'approved':
                continue
            if by in review['approvers']:
                already.append(sid)
                continue
            # 唯一约束兜底：别的机器上同一人同时同意同一条，只登记一次。
            if db.insert('INSERT INTO draft_approvals(product,file,scenario,fingerprint,approved_by,approved_at) VALUES (?,?,?,?,?,?) '
                         'ON CONFLICT DO NOTHING', (store.product, file, sid, scene['fingerprint'], by, now())) is None:
                already.append(sid)
                continue
            approved.append(sid)
    return dict(ok=True, file=file, node=node, approved=approved, already=already, skipped=skipped, approved_by=by)


def revoke_approval(context, *, by, approval=None, file=None, scenario=None):
    """撤销自己的同意：按同意编号，或按 文件 + 场景（当前指纹上自己的同意）。只能撤销自己的。"""
    by = signer_name(by)
    store = context.store
    with store.records_tx() as db:
        if approval is not None:
            if type(approval) is not int:
                reject(422, 'approval-invalid', 'approval', '同意编号应为整数')
            row = db.one('SELECT * FROM draft_approvals WHERE id=? AND product=?', (approval, store.product))
        else:
            found = draft_scan(context)
            scene = found['scenarios'].get(scenarios.key(file or '', scenario or ''))
            if scene is None or scene.get('status') != 'draft':
                reject(404, 'scenario-not-found', 'scenario', f'草稿场景不存在：{file}#{scenario}')
            row = db.one('SELECT * FROM draft_approvals WHERE product=? AND file=? AND scenario=? AND fingerprint=? AND approved_by=? '
                         'AND revoked_at IS NULL', (store.product, file, scenario, scene['fingerprint'], by))
        if row is None:
            reject(404, 'approval-not-found', 'approval', '没有可撤销的同意')
        if row['approved_by'] != by:
            reject(403, 'approval-not-own', 'approval', f"只能撤销自己的同意：这条同意属于 {row['approved_by']}")
        if row['revoked_at']:
            reject(409, 'approval-revoked', 'approval', '这条同意已撤销')
        db.execute('UPDATE draft_approvals SET revoked_at=? WHERE id=? AND revoked_at IS NULL', (now(), row['id']))
    return dict(ok=True, approval=row['id'], file=row['file'], scenario=row['scenario'], revoked_by=by)


def approval_history(store, file, scenario, fingerprint):
    """某场景的全部同意记录（含已撤销与旧内容上的），新到旧。"""
    with store.records() as db:
        rows = db.all('SELECT * FROM draft_approvals WHERE product=? AND file=? AND scenario=? ORDER BY id DESC',
                      (store.product, file, scenario))
    return [dict(id=r['id'], by=r['approved_by'], at=local_time(r['approved_at']), current=r['fingerprint'] == fingerprint,
                 active=r['revoked_at'] is None and r['fingerprint'] == fingerprint,
                 revoked_at=local_time(r['revoked_at']) if r['revoked_at'] else None) for r in rows]


def rule_texts(context, refs):
    """refs 指向的 SOP 规则原文：`<SOP 文件>#R<n>` 在场景目录的 SOP 中按标题找到该规则，渲染其整节。"""
    documents = {path.name: path for path in sop_files(context)}
    result = []
    for ref in refs or []:
        name, separator, anchor = ref.partition('#')
        base = Path(name.strip()).name
        path = documents.get(base) or documents.get(base + '.md')
        if path is None or not separator or not anchor.strip():
            result.append(dict(ref=ref, problem='不是场景目录中某份 SOP 的规则'))
            continue
        try:
            raw = path.read_text(encoding='utf-8')
        except (OSError, UnicodeDecodeError) as error:
            result.append(dict(ref=ref, document=path.name, problem=f'无法读取：{error}'))
            continue
        found = rule_section(raw, anchor.strip(), path.name)
        if found is None:
            result.append(dict(ref=ref, document=path.name, problem=f'{path.name} 中没有标题以 {anchor.strip()} 开头的规则'))
        else:
            result.append(dict(ref=ref, document=path.name, **found))
    return result


def rule_section(raw, anchor, name):
    """标题以 anchor 开头（其后不是字母数字）的那一节，到下一个同级或更高级标题为止。"""
    lines = raw.splitlines()
    start = level = title = None
    fence = None
    for index, line in enumerate(lines):
        opening = FENCE.match(line)
        if fence:
            if re.fullmatch(r' {0,3}' + re.escape(fence[0]) + '{' + str(len(fence)) + r',}\s*', line):
                fence = None
            continue
        if opening:
            fence = opening[1]
            continue
        heading = HEADING.match(line)
        if not heading:
            continue
        if start is not None and len(heading[1]) <= level:
            return dict(title=title, html=render('\n'.join(lines[start:index]), name)[0])
        text = inline(re.sub(r'[ \t]+#+[ \t]*$', '', heading[2]))[1].strip()
        if start is None and text.startswith(anchor) and not re.match(r'[0-9A-Za-z_]', text[len(anchor):len(anchor) + 1]):
            start, level, title = index, len(heading[1]), text
    if start is None:
        return None
    return dict(title=title, html=render('\n'.join(lines[start:]), name)[0])


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
    store = context.store
    with store.records_tx() as db:
        number = db.insert("INSERT INTO annotations(product,file,target,text,author,status,created_at) VALUES (?,?,?,?,?,'open',?)",
                           (store.product, file, target, text.strip(), by.strip(), now()))
    return dict(ok=True, annotation=number)


def annotations(store, *, file=None, include_resolved=False):
    with store.records() as db:
        rows = [dict(r) for r in db.execute('SELECT * FROM annotations WHERE product=? ORDER BY id', (store.product,))]
    return [r for r in rows if (file is None or r['file'] == file) and (include_resolved or r['status'] == 'open')]


def resolve_annotation(store, annotation_id, *, reject_it=False, note=None):
    with store.records_tx() as db:
        row = db.one('SELECT * FROM annotations WHERE id=? AND product=?', (annotation_id, store.product))
        if row is None:
            reject(404, 'annotation-not-found', 'annotation', f'批注不存在：#{annotation_id}')
        db.execute('UPDATE annotations SET status=?, note=?, resolved_at=? WHERE id=?',
                   ('rejected' if reject_it else 'adopted', note, now(), annotation_id))
    return dict(ok=True, annotation=annotation_id, status='rejected' if reject_it else 'adopted')


def draft_confirm(context, file):
    """每条场景在当前指纹上已同意、且文件没有未处理的批注时，把草稿改为 confirmed；确认人是同意人
    （去重，按首次同意的先后，用“、”连接）。"""
    directory = context.checkout.scenarios_dir(context.project)
    path = directory / f'{file}.yaml'
    if not path.is_file():
        reject(404, 'draft-not-found', 'file', f'场景文件不存在：{file}')
    with context.store.lock:
        return _confirm(context, file, path)


def _confirm(context, file, path):
    raw = path.read_text(encoding='utf-8')
    document = scenarios.load_scenarios(raw, set(context.project.entries))
    if document['metadata'].get('status') != 'draft':
        reject(409, 'draft-not-found', 'file', f'{file} 不是草稿')
    review = review_files(context, file)
    if not review:
        reject(409, 'draft-not-found', 'file', f'{file} 不是草稿')
    review = review[0]
    if not review['ready']:
        unapproved = [s for s in review['scenarios'] if s['review'] != 'approved']
        problems = [dict(field='scenarios', scenario=s['scenario'], problem=f"{s['scenario']} 未同意：{s['label']}")
                    for s in unapproved]
        problems += [dict(field='annotations', annotation=a['id'],
                          problem=f"批注 #{a['id']}（{'整个文件' if a['target'] == 'file' else a['target']}，{a['author']}）未处理：{a['text'][:60]}")
                     for a in review['open_annotations']]
        raise APIError(409, 'draft-not-approved' if unapproved else 'draft-annotations-open', problems)
    by = '、'.join(review['approvers'])
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
    return dict(ok=True, file=file, status='confirmed', confirmed_by=by, approvers=review['approvers'])
