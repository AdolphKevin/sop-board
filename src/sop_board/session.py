"""本地开发会话入口：认领任务，复用准备、消费与登记，不启动看板队列。"""
import argparse
from contextlib import nullcontext
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
from urllib.parse import urlsplit

from .adapters import Environment
from .errors import APIError, reject
from .kernel import InputChanged, POLICIES, S, session_scheduling_tasks
from .repositories import Discovery
from .service import Service, canonical, now
from .statistics import calculate
from .storage import Store
from .tasks import Tasks
from .workspace import Workspace, WorkspaceNotFound


KINDS = {'draft': 'generate', 'merge': 'merge', 'execute': 'execute'}


def identity(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 200 or '\0' in value or '\n' in value:
        raise ValueError('会话标识须为 1～200 字的非空单行文字；取不到原生编号时使用稳定替代标签')
    return value.strip()


class Session:
    def __init__(self, workspace=None, data=None):
        self.workspace = Workspace(workspace, data)
        if workspace is not None and self.workspace.name != workspace:
            raise ValueError('工作区名称与数据目录登记不一致')
        self.store = Store(self.workspace.data)
        with self.store.transaction() as db:
            db.execute('INSERT INTO workspaces(key,root,name) VALUES (?,?,?) ON CONFLICT(key) DO UPDATE SET root=excluded.root,name=excluded.name',
                (self.workspace.name, str(self.workspace.data), self.workspace.name))
        self.service = Service(self.store, self.workspace, Environment(self.workspace))
        self.tasks = Tasks(self.service, background=False)
        self.key = self.workspace.name
        self.owner = None
        self.replace_queued = False
        self.replaced_tasks = []
        self.service.merge_callback = self.enqueue_merge
        self.service.sop_callback = self.enqueue_generate

    def close(self):
        self.tasks.close()

    def configured(self):
        self.workspace.reload()
        if not self.workspace.configured:
            raise ValueError('工作区配置不合格：' + '；'.join(p['problem'] for p in self.workspace.problems))

    def sync(self):
        self.configured()
        # 同步场景文件，但不替命令隐式安排看板任务。
        merge, execute = self.service.merge_callback, self.service.execution_callback
        try:
            self.service.merge_callback = self.service.execution_callback = None
            Discovery(self.service).scan(reconcile=False)
        finally:
            self.service.merge_callback, self.service.execution_callback = merge, execute

    def person(self, value):
        value = identity(value)
        with self.store.connection() as db:
            sessions = {r[0].casefold() for r in db.execute('SELECT s.session_id FROM session_tasks s JOIN tasks t ON t.id=s.task WHERE t.workspace=?', (self.key,))}
        generic = {'agent', 'assistant', 'codex', 'claude code', 'ai', '助手', '当前会话'}
        if value.casefold() in sessions | generic or value == self.owner or re.fullmatch(r'[a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12}', value):
            raise ValueError('回复人或确认人须为使用者给出的姓名，不能填写会话标识或 agent')
        return value

    def raw(self, task_id):
        with self.store.connection() as db:
            row = db.execute('SELECT * FROM tasks WHERE id=? AND workspace=?', (task_id, self.key)).fetchone()
        if row is None:
            reject(404, 'task-not-found', 'task', '任务不属于当前工作区或不存在')
        return dict(row)

    def preflight(self, kind, batch):
        """操作锁内核对同类认领、看板队列及跨类型互斥，再开始任何业务写入。"""
        with self.store.connection() as db:
            rows = session_scheduling_tasks(db)
            claims = {r['task']: r['session_id'] for r in db.execute('SELECT * FROM session_tasks')}
        own, queued = None, []
        for row in rows:
            if row['workspace'] != self.key or row['batch'] != batch or row['kind'] != kind:
                continue
            claimant = claims.get(row['id'])
            if claimant:
                if claimant != self.owner:
                    reject(409, 'task-owned', 'task', f"任务 #{row['id']} 正由其他会话处理")
                if row['status'] != S.SESSION:
                    reject(409, 'task-state-invalid', 'task', f"任务 #{row['id']} 已不在会话处理中，不能重新认领")
                own = self.raw(row['id'])
            elif row.get('worker') or row['status'] in POLICIES[kind].running:
                reject(409, 'task-running', 'task', f"看板任务 #{row['id']} 正在运行或恢复环境，不能认领")
            else:
                queued.append(row)
        if own:
            return own, False
        remaining = [r for r in rows if r['kind'] != kind or r['workspace'] != self.key or r['batch'] != batch]
        blocker = self.tasks.kernel.session_blocker(dict(id=-1, kind=kind, workspace=self.key, batch=batch), remaining)
        if blocker:
            reject(409, 'task-blocked', 'task', f"等待任务 #{blocker['id']}（{blocker['status']}）结束，不能同时准备")
        if queued and not self.replace_queued:
            reject(409, 'task-queued', 'task', '看板任务 ' + '、'.join('#' + str(r['id']) for r in queued) + ' 已排队；使用者同意取消后可加 --replace-queued')
        for row in queued:
            # 替换排队任务保留草稿版本及批注，不等同于放弃整份草稿。
            draft = None
            if kind == 'generate':
                with self.store.connection() as db:
                    draft = db.execute('SELECT id,status FROM drafts WHERE current_task=?', (row['id'],)).fetchone()
            self.tasks.action(row['id'], 'cancel')
            if draft:
                with self.store.transaction() as db:
                    db.execute('UPDATE drafts SET status=? WHERE id=?', (draft['status'], draft['id']))
            self.replaced_tasks.append(row['id'])
        return None, bool(queued)

    def claim(self, task_id, connection=None):
        with (nullcontext(connection) if connection is not None else self.store.transaction()) as db:
            self.tasks.kernel.assign(task_id, self.owner, connection=db)
            return self.tasks.read(task_id, connection=db)

    def enqueue_generate(self, key, name, version=None):
        if self.owner is None:
            return self.tasks.enqueue_generate(key, name, version)
        existing, _ = self.preflight('generate', name)
        if existing:
            self.tasks.update(existing['id'], status=S.VOID, void_reason='业务 SOP 已更新，原会话任务作废', finished_at=now())
        result = self.tasks.generations.enqueue(key,name,version,force_new=True)
        self.last_generate = result['task']['id']
        result['task'] = self.claim(self.last_generate)
        return result

    def sop_task(self, batch, result):
        """两个 SOP 登记命令共用阶段 2 的认领及重复登记恢复。"""
        if not getattr(self,'last_generate',None):
            with self.store.connection() as db:
                group = self.service.batch(db,self.key,batch)
                draft = self.tasks.generations.latest(db,group)
                sop = self.service.latest_sop_upload(db,group)
            if sop and sop['version'] == result['version'] and sop['sha256'] == result['sha256']:
                if self.replaced_tasks or draft is None or draft['sop_version'] != sop['id']:
                    self.enqueue_generate(self.key,batch,result['version'])
                elif draft['current_task']:
                    current = self.raw(draft['current_task'])
                    claim = self.tasks.kernel.claim(current)
                    if claim and claim['session_id']==self.owner and current['status']==S.SESSION:
                        self.last_generate = current['id']
        if getattr(self,'last_generate',None):
            result['task'] = self.tasks.read(self.last_generate)
        return result

    def upload_sop(self, batch, body, *, on_registered=None):
        from .validation import validate_batch, validate_sop_upload
        validate_batch(batch)
        validate_sop_upload(body)
        self.preflight('generate',batch)
        return self.sop_task(batch,self.service.upload_sop(self.key,batch,body,on_registered=on_registered))

    def author_command(self, args, batch):
        from .author_sop import source_files
        from .validation import validate_batch
        self.configured()
        self.replace_queued = getattr(args,'replace_queued',False)
        if args.action == 'prepare':
            validate_batch(batch)
            sources, contents = source_files(args.source)
            self.owner = identity(args.session)
            self.sync()
            task, _ = self.preflight('author-sop',batch)
            if task is None:
                tid = self.tasks.author_sop.enqueue(batch,sources,contents)
                self.claim(tid)
                task = self.raw(tid)
            elif sources != json.loads(task['input_json'])['sources']:
                self.tasks.update(task['id'],status=S.VOID,void_reason='本次提供的资料已变化，请重新准备',finished_at=now())
                return dict(ok=False,task=self.tasks.read(task['id']),problems=['资料已变化，旧任务作废；再次 prepare 建立新任务'])
            try:
                self.tasks.types['author-sop'].prepare(task)
                if (self.tasks.directory(task['id'])/'final.json').exists():
                    self.tasks.author_sop.review(task)
            except InputChanged as error:
                self.tasks.update(task['id'],status=S.VOID,void_reason=str(error),finished_at=now())
                return dict(ok=False,task=self.tasks.read(task['id']),problems=[str(error)])
            result = self.descriptor(task)
            result['preview'] = str(self.tasks.directory(task['id'])/'sop.md')
            result['answers'] = str(self.tasks.directory(task['id'])/'answers.json')
            return result
        if args.task is None:
            validate_batch(batch)
            with self.store.connection() as db:
                rows = db.execute('SELECT id FROM tasks WHERE workspace=? AND batch=? AND kind=? AND status=?',
                    (self.key,batch,'author-sop',S.SESSION)).fetchall()
            if len(rows) != 1:
                raise ValueError('请用 --task 指定本批次正在编写的 SOP 任务')
            task_id = rows[0]['id']
        else:
            task_id = args.task
        task = self.raw(task_id)
        claim = self.tasks.kernel.claim(task)
        if task['kind'] != 'author-sop' or not claim or batch and task['batch'] != batch:
            reject(409,'sop-task-invalid','task','任务不是本批次的会话 SOP 编写任务')
        if args.session and identity(args.session) != claim['session_id']:
            reject(409,'task-owned','session','会话标识与任务认领不一致')
        self.owner = claim['session_id']
        person = self.person(args.author if args.action=='answer' else args.confirmed_by)
        receipt = self.tasks.author_sop.registered(task)
        if args.action == 'submit' and receipt:
            if receipt.get('confirmed_by',person) != person:
                reject(409,'sop-already-confirmed','confirmed_by','本任务已由原确认人登记；更改确认记录须编写新版本')
            if self.service.sop_sha256(task['batch']) == receipt['sha256']:
                self.preflight('generate',task['batch'])
            result = self.sop_task(task['batch'],dict(receipt))
            return dict(ok=True,duplicate=True,task=self.tasks.read(task_id),draft_task=result.get('task'))
        if task['status'] != S.SESSION:
            reject(409,'task-state-invalid','task','当前状态不能答复或提交：'+task['status'])
        try:
            if args.action == 'answer':
                return dict(self.tasks.author_sop.answer(task,args.question,args.text,person),task=task_id)
            result = self.tasks.types['author-sop'].consume(task,self.owner,confirmed_by=person,upload=self.upload_sop)
            return dict(ok=True,task=self.tasks.read(task_id),draft_task=result.get('task'))
        except InputChanged as error:
            self.tasks.update(task_id,status=S.VOID,void_reason=str(error),error='',finished_at=now())
            return dict(ok=False,task=self.tasks.read(task_id),problems=[str(error)])
        except (OSError,ValueError,KeyError,TypeError,APIError) as error:
            problems = error.problems if isinstance(error,APIError) else [dict(field='output',problem=str(error))]
            if not self.tasks.author_sop.registered(task):
                self.tasks.update(task_id,error='；'.join(str(p) for p in problems),result_json=canonical(dict(ok=False,problems=problems)))
            self.archive_submission(task)
            return dict(ok=False,task=self.tasks.read(task_id),problems=problems)

    def enqueue_merge(self, key, name, trigger='重新合并', force_case=None, *, connection=None):
        if self.owner is None:
            return self.tasks.enqueue_merge(key, name, trigger, force_case, connection=connection)
        existing = None
        if connection is None:
            existing, _ = self.preflight('merge', name)
        if existing:
            return dict(ok=True, task=self.tasks.read(existing['id']))
        result = self.tasks.enqueue_merge(key, name, trigger, force_case, connection=connection, fresh=True)
        if result:
            self.last_merge = result['task']['id']
            if result['task']['status'] == S.QUEUED:
                result['task'] = self.claim(result['task']['id'], connection)
        return result

    def descriptor(self, task):
        directory = self.tasks.directory(task['id'])
        outputs = ['results.jsonl', 'impact.json'] if task['kind'] == 'execute' else ['final.json']
        if task['kind'] == 'execute' and json.loads(task['input_json']).get('fix'):
            outputs = ['results.jsonl']
        return dict(ok=True, task=task['id'], kind=task['kind'], status=task['status'], workspace=self.key,
            url=self.board_origin()+f"/w/{self.key}/tasks/{task['id']}",
            batch=task['batch'], directory=str(directory), prompt=str(directory / 'prompt.md'),
            outputs={name: str(directory / name) for name in outputs},
            session=self.tasks.kernel.claim(task)['session_id'])

    def prepare(self, kind, batch, owner, *, replace_queued=False, scope=('pending',)):
        self.owner, self.replace_queued = identity(owner), replace_queued
        with self.tasks.lock:
            self.sync()
            existing, replaced = self.preflight(kind, batch)
            if existing:
                task = existing
            elif kind == 'generate':
                with self.store.connection() as db:
                    group = self.service.batch(db, self.key, batch)
                    sop = self.service.latest_sop_upload(db, group)
                    draft = self.tasks.generations.latest(db, group)
                    if not sop:
                        reject(409, 'sop-missing', 'sop', '尚未登记业务 SOP，请先在看板上传已确认原文')
                    versions = self.tasks.generations.versions(db, draft) if draft else []
                    current = db.execute('SELECT status FROM tasks WHERE id=?',(draft['current_task'],)).fetchone() if draft else None
                    if draft and draft['status'] == '已确认' and draft['sop_version'] == sop['id']:
                        reject(409, 'draft-confirmed', 'draft', '草稿已确认；新内容请先登记新版业务 SOP')
                fresh = current and current['status'] in (S.INTERRUPTED,S.ABANDONED,S.VOID,S.FAILED,S.UNAVAILABLE)
                automatic = replaced or fresh or not versions or draft['sop_version'] != sop['id']
                result = self.tasks.generations.enqueue(self.key, batch, sop['id'] if automatic else None, force_new=True)
                self.claim(result['task']['id'])
                task = self.raw(result['task']['id'])
            elif kind == 'merge':
                result = self.enqueue_merge(self.key, batch)
                task = self.raw(result['task']['id'])
            else:
                self.service.require_ready(self.key)
                selected = list(scope)
                explicit = selected not in (['pending'], ['all'])
                if not selected or len(set(selected)) != len(selected) or explicit and any(s in ('pending', 'all') for s in selected):
                    raise ValueError('--scope 应为 pending、all 或不重复的场景编号')
                if explicit:
                    with self.store.connection() as db:
                        group = self.service.batch(db, self.key, batch)
                        available = {s['scenario'] for s in calculate(db, group)['scenarios'] if not s['held']}
                    if set(selected) - available:
                        raise ValueError('场景不存在或待补充：' + '、'.join(sorted(set(selected) - available)))
                result = self.tasks.executions.enqueue(self.key, batch, selection='all' if explicit else selected[0], session=True)
                tid = result['task']['id']
                if explicit:
                    self.tasks.update(tid, scope_json=canonical([dict(scenario=s, reason='rerun') for s in selected]))
                self.claim(tid)
                task = self.raw(tid)
            directory = self.tasks.directory(task['id'])
            if (directory / 'prompt.md').is_file() and (directory / 'input.json').is_file():
                self.tasks.conversation.taken(task)
                return self.descriptor(task)
            directory.mkdir(parents=True, exist_ok=True)
            if kind == 'execute':
                self.prepare_execution(task)
            else:
                # cases upload 或答复认领后，在第一次准备前纳入最新上传与此前全部回复（这些回复计为已派发）；准备后保持快照不变。
                if kind == 'merge' and existing:
                    self.tasks.update(task['id'], **self.tasks.refresh_merge(task))
                    task = self.raw(task['id'])
                self.tasks.types[kind].prepare(task)
                self.tasks.conversation.taken(task)
            return self.descriptor(self.raw(task['id']))

    def prepare_execution(self, task):
        from .credentials import blocked_results
        snapshot, inputs = self.tasks.executions.freeze(task)
        task.update(snapshot_json=canonical(snapshot), input_json=canonical(inputs))
        self.tasks.update(task['id'], snapshot_json=task['snapshot_json'], input_json=task['input_json'],
            scenario_file_sha256=snapshot['scenario_file_sha256'], input_digest=hashlib.sha256(canonical(snapshot).encode()).hexdigest())
        directory = self.tasks.directory(task['id'])
        environment = self.service.environment.agent_environment(self.workspace.repos)
        blocked = blocked_results(inputs['scenes'], inputs['entries'], environment)
        (directory / 'credential-blocked.json').write_text(canonical(blocked), encoding='utf-8')
        (directory / 'snapshot.json').write_text(task['snapshot_json'], encoding='utf-8')
        (directory / 'input.json').write_text(task['input_json'], encoding='utf-8')
        ids = {row['scenario'] for row in blocked}
        scoped = dict(task, scope_json=canonical([r for r in json.loads(task['scope_json']) if r['scenario'] not in ids]))
        self.tasks.types['execute'].prepare(scoped, inputs=dict(inputs, scenes=[s for s in inputs['scenes'] if s['id'] not in ids]), environment=environment)

    def submit(self, kind, task_id, *, implementer=None, owner=None):
        with self.tasks.lock:
            task = self.raw(task_id)
            claim = self.tasks.kernel.claim(task)
            if task['kind'] != kind or not claim:
                reject(409, 'task-not-owned', 'task', '任务类型不匹配或不是会话认领的任务')
            if owner is not None and identity(owner) != claim['session_id']:
                retest = kind == 'execute' and json.loads(task['input_json']).get('fix')
                reject(409, 'task-owned', 'session', '会话标识与任务认领不一致' + ('；复验须由未参与修复的新会话准备并提交' if retest else ''))
            if task['status'] in (S.COMPLETED, S.NEEDS_INPUT) or kind=='fix' and task['status'] in (S.WAIT_VALIDATION,S.WAIT_COMMIT,S.DEVELOPER,S.HANDLED):
                return dict(ok=True, task=self.tasks.read(task_id), duplicate=True)
            if task['status'] != S.SESSION:
                reject(409, 'task-state-invalid', 'task', task['void_reason'] or '任务当前状态不能提交：' + task['status'])
            if not (self.tasks.directory(task_id) / 'prompt.md').is_file():
                reject(409, 'task-not-prepared', 'task', '任务已认领，须先运行 prepare 生成材料')
            try:
                if kind == 'execute':
                    inputs = json.loads(task['input_json'])
                    if inputs.get('fix'):
                        from .session_fixes import SessionFixes
                        fixes = SessionFixes(self.tasks)
                        if implementer is not None and identity(implementer) != fixes.info(inputs['fix'])['session_id']:
                            raise ValueError('修复复验的实现者由原修复记录绑定，不能改填其他身份')
                        result = fixes.consume_validation(task, claim['session_id'])
                    else:
                        blocked = json.loads((self.tasks.directory(task_id) / 'credential-blocked.json').read_text())
                        ids = {row['scenario'] for row in blocked}
                        scoped = dict(task, scope_json=canonical([r for r in json.loads(task['scope_json']) if r['scenario'] not in ids]))
                        result = self.tasks.types[kind].consume(task, claim['session_id'], implementer=identity(implementer) if implementer is not None else None,
                            execution_origin='session', blocked=blocked, runnable=[s for s in inputs['scenes'] if s['id'] not in ids], scoped=scoped)
                    counts = self.tasks.executions.counts(result['rows']) if 'rows' in result else {}
                    self.tasks.kernel.finish(task, dict(status=S.COMPLETED, finished_at=now(), error='',
                        rounds_json=canonical(result['rounds']), result_json=canonical(dict(ok=True, problems=[], **counts)), session_id=claim['session_id']))
                else:
                    result = self.tasks.types[kind].consume(task) if kind=='fix' else self.tasks.types[kind].consume(task, claim['session_id'])
                current = self.tasks.read(task_id)
                ok = current['status'] in (S.COMPLETED, S.NEEDS_INPUT) or kind=='fix' and current['status'] in (S.WAIT_VALIDATION,S.DEVELOPER)
                if not ok:
                    self.archive_submission(task)
                return dict(ok=ok, task=current, problems=current['result'].get('problems', []))
            except InputChanged as error:
                self.submission_failed(task_id, status=S.VOID, void_reason=str(error), error='', finished_at=now())
                return dict(ok=False, task=self.tasks.read(task_id), problems=[dict(field='snapshot', problem=str(error))])
            except (OSError, ValueError, KeyError, TypeError, RecursionError, APIError) as error:
                problems = error.problems if isinstance(error, APIError) else [dict(field='output', problem=str(error))]
                self.submission_failed(task_id, error='；'.join(str(p) for p in problems), result_json=canonical(dict(ok=False, problems=problems)))
                self.archive_submission(task)
                return dict(ok=False, task=self.tasks.read(task_id), problems=problems)

    def submission_failed(self, task_id, **values):
        """提交未成功只改写仍在会话处理中的任务；已放弃、已作废或已登记的状态保持原样，不复活旧任务。"""
        with self.store.transaction() as db:
            db.execute('UPDATE tasks SET ' + ','.join(k + '=?' for k in values) + ' WHERE id=? AND status=?',
                       (*values.values(), task_id, S.SESSION))

    def archive_submission(self, task):
        self.tasks.kernel.archive(task, move=False, checkpoint='session-submit', names=('final.json', 'results.jsonl', 'impact.json'))

    def cancel(self, task_id):
        with self.tasks.lock:
            task = self.raw(task_id)
            if self.tasks.kernel.claim(task) and task['status'] == S.SESSION:
                if task['kind'] == 'fix':
                    return self.tasks.action(task_id,'cancel')
                if task['kind'] == 'execute':
                    from .session_fixes import SessionFixes
                    if SessionFixes(self.tasks).cancel_validation(task_id):
                        return dict(ok=True,task=self.tasks.read(task_id))
                if task['kind'] == 'merge':
                    self.tasks.conversation.abandon(task)
                else:
                    self.tasks.update(task_id, status=S.ABANDONED, error='使用者取消会话任务', finished_at=now())
                if task['kind'] == 'generate':
                    self.tasks.generations.task_status(task_id, S.ABANDONED)
                return dict(ok=True, task=self.tasks.read(task_id))
            return self.tasks.action(task_id, 'cancel')

    def status(self):
        with self.tasks.lock:
            with self.store.connection() as db:
                batches = [dict(name=b['name'], **{k: v for k, v in calculate(db, b).items() if k in ('scenario_count','queue_counts','statistics')})
                           for b in db.execute('SELECT * FROM batches WHERE workspace=? ORDER BY name', (self.key,))]
            check = self.service.environment.latest_check()
            attempt = self.service.environment.latest_check_attempt()
            reports = sorted((self.workspace.data/'checks').glob('*.json'))
            return dict(ok=True, workspace=self.workspace.public(), data=str(self.workspace.data), batches=batches,
                        setup_check=dict(ok=self.service.environment.approved(),finished_at=check.get('finished_at') if check else None,
                            report=str(reports[-1]) if reports else None,
                            latest_attempt=dict(ok=attempt.get('ok'),completed=attempt.get('completed',True),
                                finished_at=attempt.get('finished_at'),report=attempt['path'],message=attempt.get('message','')) if attempt else None),
                        attention=self.tasks.fixes.attention(self.key, self.board_origin())['items'])

    def board_origin(self):
        with self.store.connection() as db:
            row = db.execute("SELECT value FROM settings WHERE key='board_url'").fetchone()
        value = row[0].rstrip('/') if row else ''
        try:
            url = urlsplit(value)
            if url.scheme in ('http','https') and url.hostname and not (url.username or url.password or url.path or url.query or url.fragment):
                return value
        except ValueError:
            pass
        return '看板地址'

    def complete_links(self, result):
        origin = self.board_origin()
        def visit(value, key='', files=False):
            if isinstance(value, dict):
                return {k:visit(v,k,files or k=='files') for k,v in value.items()}
            if isinstance(value, list):
                return [visit(v,key,files) for v in value]
            if isinstance(value,str) and (files or key in ('url','href','task_url')) and value.startswith(('/w/','/api/v1/')):
                return origin+value
            return value
        return visit(result)


def parser_for(group):
    parser = argparse.ArgumentParser(prog='sop-board ' + group, description='本机开发会话入口，不启动看板队列')
    if group == 'status':
        choices = {'status': parser}
        parser.set_defaults(action='status')
    else:
        actions = {'task': ('list','show','cancel'), 'draft': ('prepare','submit','annotate','confirm'),
                   'cases': ('upload',), 'merge': ('prepare','submit','reply','exclude','include'),
                   'execute': ('prepare','submit'), 'fix': ('prepare','submit','land'), 'sop': ('upload','prepare','answer','submit')}[group]
        sub = parser.add_subparsers(dest='action', required=True)
        choices = {action: sub.add_parser(action) for action in actions}
    for action, child in choices.items():
        child.add_argument('targets', nargs='*', help='工作区与批次；省略工作区时按 --data 或当前仓库查找')
        child.add_argument('--data', type=Path)
        child.add_argument('--json', action='store_true', help='输出 JSON（操作命令缺省也输出 JSON）')
        if group == 'task':
            child.add_argument('--task', type=int)
            child.add_argument('--batch')
        if action in ('prepare','upload'):
            child.add_argument('--session', required=True)
            if group != 'fix':
                child.add_argument('--replace-queued', action='store_true')
        elif action in ('submit','confirm','reply','exclude','include','answer'):
            child.add_argument('--session', help='提交时核对认领；答复/确认时可认领随后触发的合并')
            child.add_argument('--replace-queued', action='store_true')
        if action in ('submit','land'):
            child.add_argument('--task', type=int, required=True)
        if group == 'fix' and action == 'prepare':
            child.add_argument('--scenario', nargs='+', help='批次#场景；省略则认领当前可处理问题')
        if group == 'execute' and action == 'prepare':
            child.add_argument('--scope', nargs='+', default=['pending'])
            child.add_argument('--fix', type=int, help='独立复验已提交修复输出的会话任务，不再填写批次或 --scope')
        if group == 'execute' and action == 'submit':
            child.add_argument('--implementer', help='实际实现者；省略时登记独立性未声明')
        if group in ('cases','sop') and action == 'upload':
            child.add_argument('--uploaded-by', required=True)
        if group == 'sop' and action in ('upload','submit'):
            child.add_argument('--confirmed-by', required=True)
        if group == 'sop' and action == 'prepare':
            child.add_argument('--source',type=Path,nargs='+',action='extend',required=True)
        if group == 'sop' and action == 'answer':
            child.add_argument('--task',type=int)
            for name in ('question','text','author'):
                child.add_argument('--'+name,required=True)
        if action in ('annotate','confirm'):
            child.add_argument('--version', type=int, required=True)
        if action == 'annotate':
            for name in ('target','kind','text','author'):
                child.add_argument('--' + name, required=True)
        if action == 'confirm':
            child.add_argument('--confirmed-by', required=True)
        if action in ('reply','exclude','include'):
            child.add_argument('--author', required=True)
            if action in ('reply','exclude'):
                child.add_argument('--gap', required=True)
            if action == 'reply':
                child.add_argument('--text', required=True)
            else:
                child.add_argument('--reason', required=True)
            if action == 'include':
                child.add_argument('--case', required=True)
    return parser


def upload_text(path, code, problem):
    """按字节读取上传文件并按 UTF-8 解码，不能解码时返回与看板上传相同的业务错误；换行仍按文本方式读取时统一。"""
    try:
        text = path.read_bytes().decode('utf-8')
    except UnicodeDecodeError:
        reject(422, code, 'content', problem)
    return text.replace('\r\n', '\n').replace('\r', '\n')


def target_args(group, args):
    target = list(args.targets)
    filename = None
    if group in ('cases','sop') and args.action == 'upload':
        if not target:
            raise ValueError('请给出上传文件路径')
        filename = Path(target.pop())
    if group == 'task' and args.action in ('show','cancel') and args.task is None:
        if not target or not target[-1].isdigit():
            raise ValueError('请用 --task <编号> 或末尾的任务编号指定任务')
        args.task = int(target.pop())
    batch_needed = (group in KINDS and args.action != 'submit' or group == 'cases' or
        group == 'sop' and args.action in ('upload','prepare','answer') and not getattr(args,'task',None)) and not getattr(args,'fix',None)
    batch = target.pop() if batch_needed and target else None
    if batch_needed and not batch:
        raise ValueError('请指定批次')
    if len(target) > 1:
        raise ValueError('工作区参数过多')
    return target[0] if target else None, batch, filename


def command(arguments):
    group = arguments[0]
    args = parser_for(group).parse_args(arguments[1:])
    app = None
    try:
        workspace, batch, filename = target_args(group, args)
        app = Session(workspace, args.data)
        with app.tasks.lock:
            if group == 'status':
                result = app.status()
            elif group == 'task':
                if args.action == 'list':
                    result = app.tasks.list(app.key, args.batch)
                elif args.action == 'show':
                    app.raw(args.task)
                    result = dict(ok=True, task=app.tasks.read(args.task))
                else:
                    result = app.cancel(args.task)
            elif group == 'sop' and args.action != 'upload':
                result = app.author_command(args,batch)
            elif group == 'fix' or group == 'execute' and args.action == 'prepare' and args.fix:
                from .session_fixes import SessionFixes
                fixes = SessionFixes(app.tasks)
                if group == 'fix' and args.action in ('prepare','land'):
                    import os
                    from .jira import Jira
                    app.service.jira = Jira(os.environ.get('JIRA_EMAIL',''),os.environ.get('JIRA_API_TOKEN',''),timeout=os.environ.get('JIRA_TIMEOUT_SECONDS','10'))
                if args.action == 'prepare':
                    app.owner = identity(args.session)
                    if group == 'fix':
                        app.sync()
                        tid = fixes.prepare(app.owner,args.scenario)
                    else:
                        if args.scope != ['pending'] or args.replace_queued:
                            raise ValueError('--fix 的范围来自问题与受影响场景，不能同时指定 --scope 或替换排队任务')
                        tid = fixes.prepare_validation(args.fix,app.owner)
                    result = app.descriptor(app.raw(tid))
                elif args.action == 'submit':
                    result = app.submit('fix',args.task,owner=args.session)
                else:
                    result = fixes.land(args.task)
            elif args.action == 'prepare':
                result = app.prepare(KINDS[group], batch, args.session, replace_queued=args.replace_queued,
                                     scope=getattr(args, 'scope', ('pending',)))
            elif args.action == 'submit':
                result = app.submit(KINDS[group], args.task, implementer=getattr(args, 'implementer', None), owner=args.session)
            else:
                app.configured()
                app.owner = identity(args.session) if getattr(args, 'session', None) else None
                app.replace_queued = getattr(args, 'replace_queued', False)
                if group == 'sop':
                    from .validation import validate_batch
                    validate_batch(batch)
                    body = dict(filename=filename.name,content=upload_text(filename,'sop-invalid','SOP 原文必须为 UTF-8'),uploaded_by=app.person(args.uploaded_by),
                        confirmed_by=app.person(args.confirmed_by),base_sha256=app.service.sop_sha256(batch))
                    result = app.upload_sop(batch,body)
                elif group == 'cases':
                    from .cases import load_cases
                    body = dict(filename=filename.name, content=upload_text(filename, 'cases-invalid', '用例文件必须为 UTF-8'), uploaded_by=app.person(args.uploaded_by))
                    load_cases(body)
                    app.preflight('merge', batch)
                    result = app.service.upload_cases(app.key, batch, body)
                    if getattr(app, 'last_merge', None):
                        result['task'] = app.tasks.read(app.last_merge)
                elif group == 'draft' and args.action == 'annotate':
                    result = app.tasks.generations.annotate(app.key, batch, dict(version=args.version, target=args.target,
                        kind=args.kind, text=args.text, author=app.person(args.author)))
                elif group == 'draft':
                    author = app.person(args.confirmed_by)
                    if app.owner:
                        app.preflight('merge', batch)
                    result = app.tasks.generations.confirm(app.key, batch, dict(version=args.version, confirmed_by=author))
                    if getattr(app, 'last_merge', None):
                        result['task'] = app.tasks.read(app.last_merge)
                else:
                    author = app.person(args.author)
                    if app.owner:
                        app.preflight('merge', batch)
                    if args.action == 'reply':
                        result = app.tasks.conversation.submit(app.key, batch, dict(author=author,
                            messages=[dict(gap=None if args.gap=='none' else args.gap, text=args.text)]), source='会话', session=app.owner)
                    else:
                        result = app.tasks.conversation.exclude(app.key, batch,
                            dict(author=author, gap=getattr(args, 'gap', None), reason=args.reason),
                            include=args.case if args.action=='include' else None, source='会话', session=app.owner)
                    if app.owner and result.get('task') and result['task']['status'] == S.QUEUED:
                        result['task'] = app.claim(result['task']['id'])
        if app.replaced_tasks:
            result['replaced_tasks'] = app.replaced_tasks
        result = app.complete_links(result)
        if group == 'status' and not args.json:
            print('工作区：' + result['workspace']['name'])
            for row in result['batches']:
                print(f"批次 {row['name']}：{row['scenario_count']} 条场景；待执行 {row['queue_counts']['pending']}")
            print('待开发者处理：' + str(len(result['attention'])) + ' 项')
            for item in result['attention']:
                print(f"- {item['kind']}：{item['reason']} · {item['url']}" + (f" · {item['resume']}" if item.get('resume') else ''))
        else:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get('ok', True) else 1
    except APIError as error:
        result = dict(ok=False, error_code=error.code, problems=error.problems)
    except WorkspaceNotFound as error:
        result = dict(ok=False, error_code='session-command-invalid', problems=[dict(field='command', problem=str(error))],
                      guidance='先按 sop-board guide 运行 sop-board init 登记工作区，再核对命令参数')
    except (OSError, ValueError, KeyError, TypeError, RecursionError, sqlite3.Error, subprocess.SubprocessError) as error:
        result = dict(ok=False, error_code='session-command-invalid', problems=[dict(field='command', problem=str(error))])
    finally:
        if app:
            app.close()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1
