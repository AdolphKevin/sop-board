"""业务 SOP 草稿、批注、确认与生成任务快照；不写产品仓库。"""

import difflib
import hashlib
from importlib.resources import files
import json

import yaml

from .kernel import (OUTPUT_ACTIVE, S, sql_states)

from .drafts import GENERATE_SCHEMA, generated_cases, generation_problems
from .errors import reject
from .service import canonical, now
from .validation import Problems


class Generations:
    def __init__(self, tasks):
        self.tasks, self.service, self.store = tasks, tasks.service, tasks.store

    def recover(self):
        with self.store.transaction() as db:
            db.execute(f"UPDATE drafts SET status='{S.FAILED}' WHERE status IN ('生成中','修订中','SOP 已更新') "
                       f"AND current_task IN (SELECT id FROM tasks WHERE status='{S.INTERRUPTED}')")

    def latest(self, db, batch):
        return db.execute('SELECT * FROM drafts WHERE batch=? ORDER BY id DESC LIMIT 1', (batch['id'],)).fetchone()

    def versions(self, db, draft):
        versions = [dict(row) for row in db.execute('SELECT v.*,EXISTS(SELECT 1 FROM session_tasks s WHERE s.task=v.task) AS session_owned '
            'FROM draft_versions v WHERE draft=? ORDER BY number', (draft['id'],))]
        from .kernel import resume_command
        for version in versions:
            version['output'] = json.loads(version.pop('output_json'))
            version['resume'] = resume_command(version['thread_id'], self.tasks.directory(version['task']))
        return versions

    def annotations(self, db, draft):
        return [dict(row) for row in db.execute('SELECT * FROM draft_annotations WHERE draft=? ORDER BY number', (draft['id'],))]

    def read(self, key, name, *, local=False):
        with self.tasks.lock, self.store.connection() as db:
            batch = self.service.batch(db, key, name)
            row = self.latest(db, batch)
            etag = self.service.batch_etag(db, batch)
            if not row:
                return dict(ok=True, draft=None, etag=etag)
            draft = dict(row, versions=self.versions(db, row), annotations=self.annotations(db, row))
            # 首版基准固定为生成时的用例并集，确认上传或后续上传不能改写历史差异。
            draft['registered_cases'] = []
            if draft['versions']:
                first_task = db.execute('SELECT input_json FROM tasks WHERE id=?', (draft['versions'][0]['task'],)).fetchone()
                draft['registered_cases'] = json.loads(first_task['input_json'])['cases']
            draft['history'] = [dict(r) for r in db.execute('SELECT * FROM draft_events WHERE draft=? ORDER BY id', (row['id'],))]
            confirmation = db.execute('SELECT c.*,u.number AS upload_number FROM draft_confirmations c '
                                      'JOIN case_uploads u ON u.id=c.upload WHERE c.draft=?', (row['id'],)).fetchone()
            draft['confirmation'] = dict(confirmation) if confirmation else None
            if confirmation:
                for field in ('pending_annotations', 'unanswered_questions'):
                    draft['confirmation'][field] = json.loads(draft['confirmation'].pop(field + '_json'))
            draft['current_task'] = self.tasks.read(row['current_task'], local=local) if row['current_task'] else None
            draft['versions'] = self.tasks.types['generate'].visibility.draft_versions(draft['versions'], local=local)
        return dict(ok=True, draft=draft, etag=etag)

    def annotate(self, key, name, body):
        p = Problems('annotation-invalid')
        if not isinstance(body, dict):
            p.add('body', '应为对象')
            p.finish()
        for field in body.keys() - {'version', 'target', 'kind', 'text', 'author'}:
            p.add(field, '格式外字段')
        for field in ('target', 'kind', 'text', 'author'):
            p.text(body.get(field), field)
        if type(body.get('version')) is not int or body['version'] < 1:
            p.add('version', '应为草稿版本的正整数编号')
        if body.get('kind') not in ('comment', 'remove', 'add', 'answer'):
            p.add('kind', '应为 comment、remove、add 或 answer')
        p.finish()
        with self.tasks.lock, self.store.transaction() as db:
            batch = self.service.batch(db, key, name)
            draft = self.latest(db, batch)
            if not draft or draft['status'] in ('已确认', S.ABANDONED):
                reject(409, 'draft-unavailable', 'draft', '没有可批注的进行中草稿')
            version = db.execute('SELECT * FROM draft_versions WHERE draft=? AND number=?', (draft['id'], body['version'])).fetchone()
            if not version:
                reject(422, 'annotation-invalid', 'version', '草稿版本不存在')
            output = json.loads(version['output_json'])
            targets = {'draft'} | {prefix + ':' + item['id'] for prefix, group in
                (('case', 'cases'), ('rule', 'rules'), ('question', 'questions')) for item in output[group]}
            if body['target'] not in targets:
                p.add('target', '目标必须在所选版本中存在，或为 draft')
            if body['kind'] == 'answer' and not body['target'].startswith('question:'):
                p.add('target', 'answer 只能指向 question: 问题')
            if body['kind'] == 'remove' and not body['target'].startswith('case:'):
                p.add('target', 'remove 只能指向 case: 用例')
            p.finish()
            number = db.execute('SELECT COALESCE(MAX(number),0)+1 FROM draft_annotations WHERE batch=?', (batch['id'],)).fetchone()[0]
            db.execute('INSERT INTO draft_annotations(batch,number,draft,version,target,kind,text,author,created_at) VALUES (?,?,?,?,?,?,?,?,?)',
                       (batch['id'], number, draft['id'], body['version'], body['target'], body['kind'], body['text'].strip(), body['author'].strip(), now()))
        return dict(ok=True, number=number, label=f'#{number}')

    def reject_annotation(self, key, name, number, body):
        p = Problems('annotation-invalid')
        if not isinstance(body, dict):
            p.add('body', '应为对象')
            p.finish()
        for field in body.keys() - {'reason', 'author'}:
            p.add(field, '格式外字段')
        for field in ('reason', 'author'):
            p.text(body.get(field), field)
        p.finish()
        with self.tasks.lock, self.store.transaction() as db:
            batch = self.service.batch(db, key, name)
            draft = self.latest(db, batch)
            if not draft or draft['status'] in ('已确认', S.ABANDONED):
                reject(409, 'draft-unavailable', 'draft', '没有可处理批注的进行中草稿')
            annotation = db.execute('SELECT * FROM draft_annotations WHERE draft=? AND number=?', (draft['id'], number)).fetchone()
            if not annotation:
                reject(404, 'annotation-not-found', 'annotation', '批注不存在')
            if annotation['status'] != '待处理':
                reject(409, 'annotation-processed', 'annotation', '仅待处理批注可标为未采纳')
            version = db.execute('SELECT MAX(number) FROM draft_versions WHERE draft=?', (draft['id'],)).fetchone()[0]
            author, reason = body['author'].strip(), body['reason'].strip()
            note = f'{author} 手动标为未采纳：{reason}'
            db.execute("UPDATE draft_annotations SET status='未采纳',processing_note=?,processed_version=? WHERE draft=? AND number=?",
                       (note, version, draft['id'], number))
            db.execute('INSERT INTO draft_events(draft,version,annotation,author,note,created_at) VALUES (?,?,?,?,?,?)',
                       (draft['id'], version, number, author, reason, now()))
        return dict(ok=True, number=number)

    def confirm(self, key, name, body):
        p = Problems('draft-confirm-invalid')
        if not isinstance(body, dict):
            p.add('body', '应为对象')
            p.finish()
        for field in body.keys() - {'version', 'confirmed_by'}:
            p.add(field, '格式外字段')
        p.text(body.get('confirmed_by'), 'confirmed_by')
        if type(body.get('version')) is not int or body['version'] < 1:
            p.add('version', '应为草稿版本的正整数编号')
        p.finish()
        with self.tasks.lock:
            with self.store.connection() as db:
                batch = self.service.batch(db, key, name)
                draft = self.latest(db, batch)
                if not draft or draft['status'] in ('已确认', S.ABANDONED):
                    reject(409, 'draft-unavailable', 'draft', '没有可确认的进行中草稿')
                versions = self.versions(db, draft)
                if not versions or body['version'] != versions[-1]['number']:
                    reject(409, 'draft-stale', 'version', '草稿已有新版本，请刷新后确认')
                if db.execute(f"SELECT 1 FROM tasks WHERE workspace=? AND batch=? AND kind='generate' AND status IN ({sql_states(OUTPUT_ACTIVE | {S.SESSION})})",
                              (key, name)).fetchone():
                    reject(409, 'draft-busy', 'task', '已有进行中的生成或修订任务')
                version = versions[-1]
                sop = dict(db.execute('SELECT * FROM sop_versions WHERE id=?', (version['sop_version'],)).fetchone())
                pending = [a for a in self.annotations(db, draft) if a['status'] == '待处理']
                confirmed_by = body['confirmed_by'].strip()
                content = generated_cases(dict(workspace=key, batch=name, sop=sop), version['output'], confirmed_by=confirmed_by)

            def record(db, batch, number):
                upload = db.execute('SELECT id FROM case_uploads WHERE batch=? AND number=?', (batch['id'], number)).fetchone()[0]
                db.execute('INSERT INTO draft_confirmations(draft,version,upload,confirmed_by,confirmed_at,pending_annotations_json,unanswered_questions_json) '
                           'VALUES (?,?,?,?,?,?,?)', (draft['id'], version['number'], upload, confirmed_by, now(),
                           canonical(pending), canonical(version['output']['questions'])))
                db.execute("UPDATE draft_annotations SET status='确认时未处理',processing_note=?,processed_version=? WHERE draft=? AND status='待处理'",
                           (f'{confirmed_by} 确认草稿时保留，未作处理', version['number'], draft['id']))
                db.execute("UPDATE drafts SET status='已确认' WHERE id=?", (draft['id'],))

            result = self.service.upload_cases(key, name, dict(filename=f"SOP 草稿 v{version['number']}", content=content,
                                               uploaded_by=confirmed_by), on_upload=record)
        return dict(result, version=version['number'])

    def enqueue(self, key, name, version=None, *, force_new=False):
        automatic = version is not None
        with self.tasks.lock:
            workspace = self.service.workspace
            workspace.reload()
            if not workspace.configured:
                reject(409, 'workspace-not-ready', 'workspace', '尚未搭建，请先配置工作区')
            stopped = []
            with self.store.transaction() as db:
                batch = self.service.batch(db, key, name)
                sop = self.service.latest_sop_upload(db, batch)
                if not sop:
                    reject(409, 'sop-missing', 'sop', '尚未上传业务 SOP')
                draft = self.latest(db, batch)
                if automatic and not force_new and draft and draft['sop_version'] == sop['id']:
                    current = self.tasks.read(draft['current_task'])
                    if current['status'] not in (S.INTERRUPTED,S.ABANDONED,S.VOID,S.FAILED,S.UNAVAILABLE):
                        return dict(ok=True, task=current)
                if not automatic and (not draft or draft['status'] in ('已确认', S.ABANDONED)):
                    reject(409, 'draft-unavailable', 'draft', '没有可修订的进行中草稿')
                if not automatic and db.execute("SELECT 1 FROM tasks WHERE workspace=? AND batch=? AND kind='generate' AND status=?", (key, name, S.SESSION)).fetchone():
                    reject(409, 'draft-busy', 'task', '已有会话正在生成或修订草稿')
                busy = list(db.execute(f"SELECT * FROM tasks WHERE workspace=? AND batch=? AND kind='generate' AND status IN ({sql_states(OUTPUT_ACTIVE)})", (key, name)))
                if busy and not automatic:
                    reject(409, 'draft-busy', 'task', '已有进行中的生成或修订任务')
                for task in busy:
                    stopped.append(task['id'])
                    db.execute(f"UPDATE tasks SET status='{S.ABANDONED}',finished_at=?,error='SOP 已更新，改用最新原文重新生成或修订' WHERE id=?", (now(), task['id']))
                if not draft or draft['status'] in ('已确认', S.ABANDONED):
                    did = db.execute("INSERT INTO drafts(batch,status,sop_version,created_at) VALUES (?,'生成中',?,?)", (batch['id'], sop['id'], now())).lastrowid
                    draft = db.execute('SELECT * FROM drafts WHERE id=?', (did,)).fetchone()
                snapshot = self.freeze(db, key, name, draft, sop)
                if not automatic and not snapshot['annotations']:
                    reject(409, 'annotations-missing', 'annotations', '没有待处理批注，无法按反馈修订')
                previous = snapshot['draft']
                raw = canonical(snapshot)
                task_id = db.execute("INSERT INTO tasks(kind,workspace,batch,status,trigger,input_digest,input_json,created_at,snapshot_json) "
                    f"VALUES ('generate',?,?,'{S.QUEUED}',?,?,?,?,?)", (key, name, '上传 SOP' if automatic else '按反馈修订',
                    hashlib.sha256(raw.encode()).hexdigest(), raw, now(), canonical(snapshot['product']))).lastrowid
                state = 'SOP 已更新' if automatic and sop['id'] != draft['sop_version'] else '修订中' if previous else '生成中'
                db.execute('UPDATE drafts SET status=?,sop_version=?,current_task=? WHERE id=?', (state, sop['id'], task_id, draft['id']))
            from .tasks import terminate_group
            for task_id_ in stopped:
                if task_id_ in self.tasks.processes:
                    terminate_group(self.tasks.processes[task_id_])
            return dict(ok=True, task=self.tasks.read(task_id))

    def freeze(self, db, key, name, draft, sop):
        workspace = self.service.workspace
        batch = self.service.batch(db, key, name)
        history = self.versions(db, draft)
        annotations = self.annotations(db, draft)
        pending = [a for a in annotations if a['status'] == '待处理']
        previous = history[-1] if history else None
        old_sop = db.execute('SELECT * FROM sop_versions WHERE id=?', (previous['sop_version'] if previous else draft['sop_version'],)).fetchone()
        original_sop = db.execute('SELECT content FROM sop_versions WHERE id=?', (history[0]['sop_version'] if history else draft['sop_version'],)).fetchone()[0]
        scenarios = self.service.latest_version(db, batch)
        snapshot = dict(workspace=key, batch=name, draft_id=draft['id'], base_version=previous['number'] if previous else 0,
            draft=previous['output'] if previous else None,
            thread_id=previous['thread_id'] if previous and not previous['session_owned'] else None,
            sop=sop, previous_sop=dict(old_sop), original_sop=original_sop, history=history,
            annotations=pending, all_annotations=annotations, cases=self.service.case_union(db, batch),
            scenarios=scenarios['content'] if scenarios else '', entries=dict(entries=workspace.entries),
            repos={a: str(p) for a, p in workspace.repos.items()}, config_digest=workspace.config_digest,
            product=workspace.product(list(workspace.repos)))
        return snapshot

    def refresh(self, task):
        self.service.workspace.reload()
        with self.store.connection() as db:
            batch = self.service.batch(db, task['workspace'], task['batch'])
            draft = self.latest(db, batch)
            snapshot = self.freeze(db, task['workspace'], task['batch'], draft, self.service.latest_sop_upload(db, batch))
        raw = canonical(snapshot)
        return dict(input_json=raw, input_digest=hashlib.sha256(raw.encode()).hexdigest(), snapshot_json=canonical(snapshot['product']))

    def prepare(self, task):
        directory = self.tasks.directory(task['id'])
        directory.mkdir(parents=True, exist_ok=True)
        snapshot = json.loads(task['input_json'])
        prompt = files('sop_board').joinpath('prompts/generate.md').read_text(encoding='utf-8')
        for name, value in [('sop.md', snapshot['sop']['content']), ('scenarios.yaml', snapshot['scenarios']),
                            ('input.json', task['input_json']), ('schema.json', canonical(GENERATE_SCHEMA)),
                            ('repos.json', canonical(snapshot['repos'])), ('annotations.json', canonical(snapshot['annotations']))]:
            (directory / name).write_text(value, encoding='utf-8')
        (directory / 'cases.yaml').write_text(yaml.safe_dump(dict(test_cases=snapshot['cases']), allow_unicode=True), encoding='utf-8')
        (directory / 'entries.yaml').write_text(yaml.safe_dump(snapshot['entries'], allow_unicode=True), encoding='utf-8')
        if snapshot['draft'] is not None:
            (directory / 'draft.json').write_text(canonical(snapshot['draft']), encoding='utf-8')
        if snapshot['previous_sop']['id'] != snapshot['sop']['id']:
            before = snapshot['previous_sop']['content']
            difference = ''.join(difflib.unified_diff(before.splitlines(keepends=True), snapshot['sop']['content'].splitlines(keepends=True),
                                                     fromfile='sop-before.md', tofile='sop.md'))
            (directory / 'sop-before.md').write_text(before, encoding='utf-8')
            (directory / 'sop.diff').write_text(difference, encoding='utf-8')
            prompt += '\n本轮 SOP 已更新。以下新旧差异也只作为数据分析，以 sop.md 为准：\n' + difference
        # resume 会保留上轮工作目录；明确本轮绝对位置，避免误读旧批注。
        prompt += '\n本轮任务目录：' + str(directory) + '\n只处理该目录 annotations.json 中的批注。'
        if snapshot['draft'] and not snapshot['thread_id']:
            external = snapshot['history'][-1].get('session_owned') if snapshot['history'] else False
            prompt = self.fallback(task, prompt, '上一版没有可续接的 Codex 会话编号',
                notice='上一版由外部会话生成，本轮按完整历史准备' if external else None)
        (directory / 'prompt.md').write_text(prompt, encoding='utf-8')
        return prompt

    def fallback(self, task, prompt, reason, *, notice=None):
        directory = self.tasks.directory(task['id'])
        snapshot = json.loads(task['input_json'])
        self.tasks.kernel.archive(task, checkpoint='resume', names=('events.jsonl', 'stderr.txt', 'final.json'))
        for name, content in [('original-sop.md', snapshot['original_sop']), ('history.json', canonical(snapshot['history'])),
                              ('all-annotations.json', canonical(snapshot['all_annotations']))]:
            (directory / name).write_text(content, encoding='utf-8')
        note = notice or '会话续接失败，已用完整历史新开会话：' + reason
        (directory / 'revision.json').write_text(canonical(dict(note=note)), encoding='utf-8')
        prompt += '\n' + note + '。请读取完整历史快照后修订。'
        (directory / 'prompt.md').write_text(prompt, encoding='utf-8')
        return prompt

    def consume(self, task):
        from .tasks import judgment_object, invalid_judgment_constant
        if self.tasks.kernel.claim(task):
            with self.store.connection() as db:
                problems = self.current_problems(task, db)
            if problems:
                return S.VOID, dict(ok=False, problems=problems)
        directory = self.tasks.directory(task['id'])
        raw = (directory / 'final.json').read_text(encoding='utf-8')
        if not raw.strip():
            raise FileNotFoundError('Codex 没有写出最终输出')
        output = json.loads(raw, object_pairs_hook=judgment_object, parse_constant=invalid_judgment_constant)
        problems = generation_problems(json.loads(task['input_json']), output)
        return S.FAILED if problems else S.COMPLETED, dict(ok=not problems, problems=problems, output=output)

    def current_problems(self, task, db):
        inputs = self.tasks.types['generate'].inputs(task)
        snapshot = inputs.snapshot
        workspace = self.service.workspace
        workspace.reload()
        batch = self.service.batch(db, task['workspace'], task['batch'])
        sop = self.service.latest_sop_upload(db, batch)
        latest = db.execute('SELECT COALESCE(MAX(number),0) FROM draft_versions WHERE draft=?', (snapshot['draft_id'],)).fetchone()[0]
        changed = inputs.changed(dict(snapshot, base_version=latest, sop_version=sop['id'] if sop else None,
            config_digest=workspace.config_digest, cases=self.service.case_union(db, batch)))
        if self.service.sop_sha256(task['batch']) != snapshot['sop']['sha256'] and '业务 SOP' not in changed:
            changed.append('业务 SOP')
        if not workspace.configured and '工作区配置' not in changed:
            changed.append('工作区配置')
        return [name + '已变化，不能保存旧任务的输出' for name in changed]

    def finish(self, task, values, check):
        inputs = self.tasks.types['generate'].inputs(task)
        snapshot = inputs.snapshot
        directory = self.tasks.directory(task['id'])
        output = check.pop('output', None)
        with self.store.transaction() as db:
            receipt = self.tasks.kernel.receipt(task, connection=db)
            if receipt is not None:
                return
            draft = db.execute('SELECT * FROM drafts WHERE id=?', (snapshot['draft_id'],)).fetchone()
            claimed = self.tasks.kernel.claim(task, connection=db)
            if draft['current_task'] != task['id'] or draft['status'] in ('已确认', S.ABANDONED):
                if claimed:
                    reason = '业务 SOP 或草稿已更新，不能保存旧任务的输出'
                    check.update(ok=False, problems=[reason])
                    db.execute('UPDATE tasks SET status=?,void_reason=?,error=?,result_json=?,finished_at=? WHERE id=?',
                        (S.VOID, reason, reason, canonical(check), now(), task['id']))
                return
            if values['status'] == S.COMPLETED:
                latest = db.execute('SELECT COALESCE(MAX(number),0) FROM draft_versions WHERE draft=?', (draft['id'],)).fetchone()[0]
                stale = self.current_problems(task, db)
                check['problems'] += stale
                if not values['session_id']:
                    check['problems'].append('Codex 没有返回会话编号，无法记录后续修订依据')
                if check['problems']:
                    check['ok'] = False
                    values.update(status=S.VOID if claimed and stale else S.FAILED, error='；'.join(check['problems']))
                else:
                    note = json.loads((directory / 'revision.json').read_text(encoding='utf-8'))['note'] if (directory / 'revision.json').exists() else task['trigger']
                    number = latest + 1
                    db.execute('INSERT INTO draft_versions(draft,number,output_json,task,thread_id,revision_note,sop_version,created_at) VALUES (?,?,?,?,?,?,?,?)',
                        (draft['id'], number, canonical(output), task['id'], values['session_id'], note, snapshot['sop']['id'], now()))
                    for response in output['responses']:
                        status = {'adopted': '已采纳', 'rejected': '未采纳', 'question': '已提问'}[response['action']]
                        db.execute("UPDATE draft_annotations SET status=?,processing_note=?,processed_version=? WHERE draft=? AND number=? AND status='待处理'",
                                   (status, response['note'], number, draft['id'], int(response['annotation'][1:])))
                    (directory / 'cases-generated.yaml').write_text(generated_cases(snapshot, output), encoding='utf-8')
                    check['version'] = number
            if claimed and values['status'] == S.FAILED:
                values.update(status=S.SESSION, finished_at=None)
            if claimed and values['status'] == S.VOID:
                values['void_reason'] = '；'.join(str(p) for p in check['problems'])
            state = '待确认' if values['status'] == S.COMPLETED else (
                '修订中' if snapshot['base_version'] else '生成中') if values['status'] == S.SESSION else S.FAILED
            db.execute('UPDATE drafts SET status=? WHERE id=?', (state, draft['id']))
            values['result_json'] = canonical(check)
            db.execute('UPDATE tasks SET ' + ','.join(k + '=?' for k in values) + ' WHERE id=?', (*values.values(), task['id']))
            if values['status'] != S.SESSION:
                self.tasks.kernel.register(task, values, connection=db)

    def action(self, task, action):
        with self.store.connection() as db:
            snapshot = json.loads(db.execute('SELECT input_json FROM tasks WHERE id=?', (task['id'],)).fetchone()[0])
            draft = db.execute('SELECT * FROM drafts WHERE id=?', (snapshot['draft_id'],)).fetchone()
            if draft['current_task'] != task['id'] or draft['status'] in ('已确认', S.ABANDONED):
                reject(409, 'draft-stale', 'task', '草稿已有后续任务或已经结束，不能操作旧任务')
            if action == 'retry':
                batch = self.service.batch(db, task['workspace'], task['batch'])
                sop = self.service.latest_sop_upload(db, batch)
                if not sop or sop['id'] != snapshot['sop']['id']:
                    reject(409, 'draft-stale', 'sop', '业务 SOP 已更新，不能重试旧任务')

    def task_status(self, task_id, status):
        with self.store.transaction() as db:
            task = db.execute('SELECT input_json FROM tasks WHERE id=?', (task_id,)).fetchone()
            snapshot = json.loads(task['input_json'])
            state = ('修订中' if snapshot['base_version'] else '生成中') if status == S.QUEUED else S.ABANDONED if status == S.ABANDONED else S.FAILED
            db.execute(f"UPDATE drafts SET status=? WHERE current_task=? AND status NOT IN ('已确认','{S.ABANDONED}')", (state, task_id))
