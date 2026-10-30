"""各类任务的输入、准备、启动、消费、操作与等待约定。"""
import json
import os
import sqlite3
import subprocess

from .kernel import (failures_exhausted, DISTILL_RETRY, DISTILL_TERMINAL, EXECUTION_QUEUE, FIX_NO_CANCEL, FIX_RETRY, Inputs,
                     Launch, WaitCondition, MERGE_WAIT_UPDATE, OUTPUT_CANCEL, OUTPUT_RETRY, S, codex_available)

from . import task_visibility
from .errors import APIError
from .service import canonical, now


class TaskType:
    waiting = None
    def __init__(self, tasks):
        self.tasks = tasks

    def recover(self):
        pass

    def blocked(self, task, other):
        pass

    def recheck(self, task):
        """被其他任务阻挡时核对自身的待提交、待验证条件，返回条件解除后应显示的状态；不作废，不写原因。"""
        return None


class MergeType(TaskType):
    visibility = task_visibility.MERGE
    waiting = WaitCondition('工作区配置', .1)
    invalid_input = '任务输入不合格，请重新合并'

    def inputs(self, task):
        return Inputs(json.loads(task['input_json']), dict(
            cases='用例', case_metadata='用例来源', scenarios='场景', entries='验收入口',
            config_digest='工作区配置', forced_flow='开发者分类选择',
            all_cases='用例并集', excluded_cases='本批不纳入的用例',
            confirmed_draft_cases='草稿确认', sop_sources='业务 SOP', sop_upload_id='业务 SOP 上传版本'))

    def refresh(self, task):
        return self.tasks.refresh_merge(task)

    def prepare(self, task):
        return self.tasks.prepare_merge(task)

    def launch(self, task, *, fresh=False):
        snapshot = self.inputs(task).snapshot
        with self.tasks.store.connection() as db:
            previous = db.execute("SELECT id,session_id FROM tasks WHERE workspace=? AND batch=? AND kind='merge' "
                "AND session_id IS NOT NULL AND session_id<>'' ORDER BY id DESC LIMIT 1",
                (task['workspace'], task['batch'])).fetchone()
        session = snapshot.get('resume_session')
        if previous and previous['id'] >= (snapshot.get('resume_task') or 0):
            # 外部会话可能是其他客户端或替代标签，不能推断 Codex 的续接命令。
            session = None if self.tasks.kernel.claim(previous) else previous['session_id']
        return Launch(schema=self.tasks.directory(task['id']) / 'schema.json',
                      session=None if fresh else session)

    def fallback(self, task, prompt, reason):
        directory = self.tasks.directory(task['id'])
        self.tasks.kernel.archive(task, move=True, checkpoint='resume-failed',
                                  names={'events.jsonl', 'stderr.txt', 'final.json'})
        note = '会话续接失败，已用完整历史新开会话'
        (directory / 'resume-fallback.json').write_text(json.dumps(dict(note=note, reason=reason), ensure_ascii=False), encoding='utf-8')
        return prompt + '\n' + note

    def continue_waiting(self):
        if self.tasks.kernel.check_due(dict(id=0), WaitCondition('合并澄清后续轮次', 1)):
            self.tasks.conversation.continue_waiting()

    def consume(self, task, session):
        receipt = self.tasks.kernel.receipt(task)
        if receipt is not None:
            return json.loads(receipt['result_json'])
        landed = self.tasks.kernel.receipt(task, phase='merge-land')
        status, check = (landed.get('status', S.COMPLETED), landed['check']) if landed else self.tasks.consume_merge_output(task)
        error = '; '.join(str(p) for p in check['problems']) if not check['ok'] else ''
        self.finish(task, self.tasks.kernel.output_values(task, S.FAILED if error else status, check, session, error), check)
        return check

    def finish(self, task, values, check):
        self.tasks.kernel.finish(task, values)
        self.tasks.kernel.wake(0)

    def actions(self, task):
        return ({'retry'} if task['status'] in OUTPUT_RETRY else set()) | (
            {'cancel'} if task['status'] in OUTPUT_CANCEL else set())

    def recover(self):
        from .kernel import session_id
        with self.tasks.store.connection() as db:
            rows = [dict(r) for r in db.execute("SELECT t.* FROM tasks t WHERE t.kind='merge' AND t.status=? "
                "AND NOT EXISTS (SELECT 1 FROM session_tasks s WHERE s.task=t.id)", (S.INTERRUPTED,))]
        for task in rows:
            landed = self.tasks.kernel.receipt(task, phase='merge-land')
            if landed:
                self.tasks.kernel.finish(task, self.tasks.kernel.output_values(task, landed.get('status', S.COMPLETED), landed['check'],
                    session_id(self.tasks.directory(task['id']))))

    def unavailable(self, task, error):
        self.tasks.update(task['id'], status=S.UNAVAILABLE, finished_at=now(), error=error)
        self.tasks.kernel.wake(0)

    def schedule(self, task):
        if not self.tasks.kernel.check_due(task, self.waiting):
            return
        if not self.tasks.service.workspace.configured:
            self.tasks.update(task['id'], error='尚未搭建，等待配置工作区')
            return
        error = self.tasks.kernel.agent_available(task['kind'])
        if error:
            self.unavailable(task, error)
            return
        self.tasks.kernel.start(task, self.tasks.execute, S.RUNNING)


class GenerateType(MergeType):
    visibility = task_visibility.GENERATE
    invalid_input = '任务输入不合格，请重新生成或修订'

    def inputs(self, task):
        snapshot = json.loads(task['input_json'])
        return Inputs(dict(snapshot, sop_version=snapshot['sop']['id']), dict(
            sop_version='业务 SOP', cases='已登记用例', base_version='草稿版本',
            config_digest='工作区配置', annotations='本轮批注'))

    def refresh(self, task):
        return self.tasks.generations.refresh(task)

    def prepare(self, task):
        return self.tasks.generations.prepare(task)

    def launch(self, task, *, fresh=False):
        return Launch(schema=self.tasks.directory(task['id']) / 'schema.json',
                      session=None if fresh else self.inputs(task).snapshot.get('thread_id'))

    def fallback(self, task, prompt, reason):
        return self.tasks.generations.fallback(task, prompt, reason)

    def consume(self, task, session):
        receipt = self.tasks.kernel.receipt(task)
        if receipt is not None:
            return json.loads(receipt['result_json'])
        status, check = self.tasks.generations.consume(task)
        error = '; '.join(str(p) for p in check['problems']) if not check['ok'] else ''
        self.finish(task, self.tasks.kernel.output_values(task, status, check, session, error), check)
        return check

    def finish(self, task, values, check):
        try:
            self.tasks.generations.finish(task, values, check)
        except (OSError, ValueError, sqlite3.Error) as problem:
            check.update(ok=False, problems=[f'草稿版本无法保存：{problem}'])
            claimed = self.tasks.kernel.claim(task)
            self.tasks.update(task['id'], status=S.SESSION if claimed else S.FAILED, finished_at=None if claimed else now(),
                              error=check['problems'][0], result_json=canonical(check))
            if not claimed:
                self.tasks.generations.task_status(task['id'], S.FAILED)

    def recover(self):
        self.tasks.generations.recover()

    def unavailable(self, task, error):
        super().unavailable(task, error)
        self.tasks.generations.task_status(task['id'], S.UNAVAILABLE)


class AuthorSopType(TaskType):
    visibility = task_visibility.AUTHOR_SOP

    def inputs(self, task):
        return self.tasks.author_sop.inputs(task)

    def prepare(self, task):
        return self.tasks.author_sop.prepare(task)

    def launch(self, task, *, fresh=False):
        return Launch(schema=self.tasks.directory(task['id']) / 'schema.json')

    def consume(self, task, session, *, confirmed_by, upload):
        return self.tasks.author_sop.consume(task,session,confirmed_by=confirmed_by,upload=upload)

    def actions(self, task):
        return {'cancel'} if task['status'] in (S.QUEUED,S.SESSION,S.INTERRUPTED) else set()

    def schedule(self, task):
        # 本段只开放会话编写；预留类型约定，不让看板越过真人答复和确认。
        self.tasks.update(task['id'],error='编写 SOP 须由会话准备、答复并确认')


class AtlasType(TaskType):
    """梳理交互：看板与会话共用提示词、输出格式与登记；只读前端源码，不占用产品环境。"""
    visibility = task_visibility.ATLAS
    waiting = WaitCondition('工作区配置', .1)
    invalid_input = '梳理任务输入不合格，请重新发起'

    def inputs(self, task):
        return Inputs(json.loads(task['input_json']), dict(entry='入口', mode='模式', base_version='地图版本'))

    def refresh(self, task):
        # 重试时拿到的是投影后的任务，原始输入从数据库读取。
        with self.tasks.store.connection() as db:
            inputs = json.loads(db.execute('SELECT input_json FROM tasks WHERE id=?', (task['id'],)).fetchone()['input_json'])
        fresh = (self.tasks.atlas.bind_inputs(inputs['batch'], inputs['entry'], inputs['stale']) if inputs['mode'] == 'bind'
                 else self.tasks.atlas.inputs(inputs['entry'], inputs['mode'], inputs['pages'], inputs['browser']))
        body = canonical(fresh)
        import hashlib
        return dict(input_json=body, input_digest=hashlib.sha256(body.encode()).hexdigest())

    def prepare(self, task):
        return self.tasks.atlas.prepare(task)

    def launch(self, task, *, fresh=False):
        return Launch(schema=self.tasks.directory(task['id']) / 'schema.json')

    def fallback(self, task, prompt, reason):
        return prompt

    def consume(self, task, session):
        receipt = self.tasks.kernel.receipt(task)
        if receipt is not None:
            return json.loads(receipt['result_json'])
        status, check = self.tasks.atlas.consume(task)
        error = '; '.join(str(p) for p in check['problems']) if not check['ok'] else ''
        self.finish(task, self.tasks.kernel.output_values(task, S.FAILED if error else status, check, session, error), check)
        return check

    def finish(self, task, values, check):
        self.tasks.kernel.finish(task, values)
        self.tasks.kernel.wake(0)

    def actions(self, task):
        return ({'retry'} if task['status'] in OUTPUT_RETRY else set()) | (
            {'cancel'} if task['status'] in OUTPUT_CANCEL else set())

    def unavailable(self, task, error):
        self.tasks.update(task['id'], status=S.UNAVAILABLE, finished_at=now(), error=error)
        self.tasks.kernel.wake(0)

    def schedule(self, task):
        if not self.tasks.kernel.check_due(task, self.waiting):
            return
        if not self.tasks.service.workspace.configured:
            self.tasks.update(task['id'], error='尚未搭建，等待配置工作区')
            return
        error = self.tasks.kernel.agent_available(task['kind'])
        if error:
            self.unavailable(task, error)
            return
        self.tasks.kernel.start(task, self.tasks.execute, S.RUNNING)


class DistillType(TaskType):
    visibility = task_visibility.DISTILL
    waiting = WaitCondition("产品提交与通过证据", 5)
    def inputs(self, task):
        return self.tasks.distill.inputs(task)

    def prepare(self, task, paths):
        return self.tasks.distill.materialize(task, paths)

    def launch(self, task, *, paths, environment):
        return Launch(sandbox='workspace-write', environment=environment,
                      writable_directories=tuple(paths.values()))

    def consume(self, task, session):
        return self.tasks.distill.consume(task, session)

    def actions(self, task):
        if task['status'] in DISTILL_TERMINAL:
            return set()
        decision = task['distill']['decision']
        if decision:
            return {'cleanup', 'cancel', 'abandon'} if decision != S.HANDLED else {'cleanup'}
        allowed = {'cancel', 'abandon'}
        if task['status'] in DISTILL_RETRY and task['id'] not in self.tasks.workers:
            allowed.add('retry')
        if task['status'] == S.REVIEW:
            allowed.add('handled')
        return allowed

    def schedule(self, task):
        self.tasks.distill.schedule(task)

    def recheck(self, task):
        return self.tasks.distill.recheck(task)

    def recover(self):
        self.tasks.distill.recover()


class ExecuteType(TaskType):
    visibility = task_visibility.EXECUTE
    waiting = WaitCondition("合并完成与执行条件", 5)
    def inputs(self, task, *, product=True):
        """product=False 用于开始执行之后：产品内容与提交号的变化不再使本轮作废。"""
        names = dict(scenario_file_sha256='场景版本',
            scenarios='场景内容', config_digest='工作区配置', cases_digest='用例并集', source_sha256='工作区场景文件')
        if product:
            if not self.tasks.kernel.claim(task):
                names['repos'] = '仓库提交或产品内容'  # 会话可在未提交内容上执行；提交本身不使结果失效。
            names['product_digest'] = '产品内容'
        return Inputs(json.loads(task['snapshot_json']), names)

    def recover(self):
        with self.tasks.store.transaction() as db:
            rows = db.execute("SELECT t.id,r.result_json,r.registered_at FROM tasks t JOIN task_receipts r "
                "ON r.task=t.id AND r.attempt=t.attempt AND r.phase='execute' WHERE t.kind='execute' AND t.status=? "
                "AND NOT EXISTS (SELECT 1 FROM session_tasks s WHERE s.task=t.id)", (S.INTERRUPTED,)).fetchall()
            for row in rows:
                receipt = json.loads(row['result_json'])
                result = dict(ok=True, problems=[], **self.tasks.executions.counts(receipt['rows']),
                              **self.tasks.executions.moved(receipt))
                db.execute("UPDATE tasks SET status=?,finished_at=?,rounds_json=?,result_json=?,session_id=?,error='' WHERE id=?",
                    (S.COMPLETED, row['registered_at'], canonical(receipt['rounds']), canonical(result), receipt.get('session'), row['id']))

    def refresh(self, task):
        with self.tasks.store.connection() as db:
            request = json.loads(db.execute('SELECT input_json FROM tasks WHERE id=?', (task['id'],)).fetchone()[0])
        return dict(input_json=canonical(dict(selection=request.get('selection', 'explicit'),
            refresh_after_merge=request.get('refresh_after_merge', False))), snapshot_json='{}')

    def prepare(self, task, *, inputs=None, environment=None):
        if environment is None:
            return self.tasks.executions.prepare(task)
        return self.tasks.executions.materialize(task, inputs, environment)

    def launch(self, task, *, environment):
        return Launch(sandbox='workspace-write', environment=environment)

    def consume(self, task, session, **inputs):
        return self.tasks.executions.consume(task, session, **inputs)

    def actions(self, task):
        return ({'retry'} if task['status'] in OUTPUT_RETRY | {S.VOID} else set()) | (
            {'cancel'} if task['status'] in OUTPUT_CANCEL | EXECUTION_QUEUE else set())

    def recheck(self, task):
        if task['status'] == S.WAIT_COMMIT and self.tasks.executions.committed(task):
            return S.QUEUED
        return None

    def blocked(self, task, other):
        if other['kind'] not in ('merge', 'generate') or other['batch'] != task['batch']:
            # “等待合并”只表示正在等同一批次的合并或草稿；改等其他任务时回到排队中，原因由内核写明。
            # 合并后刷新范围的标记保留在输入中，不因此丢失。
            if task['status'] == S.WAIT_MERGE:
                self.tasks.update(task['id'], status=S.QUEUED)
            return
        with self.tasks.store.connection() as db:
            inputs = db.execute('SELECT input_json,snapshot_json FROM tasks WHERE id=?', (task['id'],)).fetchone()
        request = json.loads(inputs['input_json'])
        if request.get('refresh_after_merge') and inputs['snapshot_json'] == '{}' and task['status'] != S.QUEUED:
            return
        request['refresh_after_merge'] = True
        values = dict(input_json=canonical(request), snapshot_json='{}')
        if task['status'] in MERGE_WAIT_UPDATE:
            values['status'] = S.WAIT_MERGE
        if self.tasks.update(task['id'], **values):
            (self.tasks.directory(task['id']) / 'snapshot.json').unlink(missing_ok=True)
            self.tasks.kernel.wake(task['id'])

    def schedule(self, task):
        tasks = self.tasks
        if task['status'] == S.WAIT_MERGE:
            with tasks.store.connection() as db:
                merge = tasks.executions.waiting_merge(db, task['workspace'], task['batch'])
            if not merge:
                # 合并已结束：先回到排队中，再判断环境等其他条件；合并后刷新范围的标记仍在输入中。
                values = dict(status=S.QUEUED)
                if (task.get('error') or '').startswith('等待合并'):
                    values['error'] = ''
                tasks.update(task['id'], **values)
                task.update(values)
        try:
            if not tasks.service.environment.approved():
                tasks.update(task['id'], error='尚未搭建或自检未通过，等待工作区就绪')
                return
            if tasks.service.environment.state.get('waiting_port'):
                tasks.update(task['id'], error=tasks.service.environment.state['status'])
                return
            if not self.prepare(task):
                return
            inputs = json.loads(task['input_json'])
            from .credentials import blocked_results
            blocked = {row['scenario'] for row in blocked_results(inputs['scenes'], inputs['entries'], os.environ)}
            runnable = [scene for scene in inputs['scenes'] if scene['id'] not in blocked]
            needs_browser = any(inputs['entries'][scene['verify']]['surface'] == 'web' for scene in runnable)
            if not tasks.kernel.browser(task, needs_browser, waiting_status=S.WAIT_BROWSER):
                return
            error = self.tasks.kernel.agent_available('execute') if runnable else ''
            if error:
                tasks.update(task['id'], status=S.UNAVAILABLE, finished_at=now(), error=error)
                if tasks.browser_owner == task['id']:
                    tasks.browser_owner = None
                return
            tasks.kernel.start(task, tasks.executions.execute, S.RUNNING)
        except (OSError, ValueError, KeyError, APIError, subprocess.SubprocessError) as error:
            tasks.update(task['id'], status=S.FAILED, finished_at=now(), error=f'无法准备执行快照：{error}')
            if tasks.browser_owner == task['id']:
                tasks.browser_owner = None


class FixType(TaskType):
    visibility = task_visibility.FIX
    waiting = WaitCondition('修复准备', .1)
    def inputs(self, task):
        snapshot = task.get('snapshot') or json.loads(task['snapshot_json'])
        return Inputs(dict(config_digest=snapshot['config_digest'], cases=snapshot['cases'],
            batches={name: row['sha256'] for name, row in snapshot['batches'].items()}),
            dict(config_digest='工作区配置或自检状态', cases='用例并集', batches='场景版本'))

    def prepare(self, task, *, materialize=False):
        return self.tasks.fixes.materialize(task) if materialize else self.tasks.fixes.prepare(task)

    def launch(self, task, *, environment, session=None):
        return Launch(sandbox='workspace-write', environment=environment, session=session,
            schema=self.tasks.directory(task['id']) / 'schema.json',
            work_directory=self.tasks.directory(task['id']),
            writable_directories=tuple(self.tasks.fixes.repositories.repair_directories(task['id']).values()))

    def consume(self, task, answer=None):
        from .tasks import judgment_object, invalid_judgment_constant
        if self.tasks.kernel.claim(task):
            from .session_fixes import SessionFixes
            if answer is None:
                answer = json.loads((self.tasks.directory(task['id']) / 'final.json').read_text(encoding='utf-8'),
                    object_pairs_hook=judgment_object, parse_constant=invalid_judgment_constant)
            return SessionFixes(self.tasks).consume(task, answer)
        receipt = self.tasks.kernel.receipt(task, phase='fix-output')
        if receipt is not None:
            return receipt['products']
        number = self.tasks.kernel.attempt(task)
        with self.tasks.store.connection() as db:
            previous = db.execute('SELECT outcome,result_json,reason FROM fix_attempts WHERE task=? AND number=?',
                                  (task['id'], number)).fetchone()
        if previous is None:
            raise ValueError('修复尝试尚未准备，不能登记输出')
        # 旧数据的业务尝试本身就是登记凭据，不能因没有新内核收据再提交一次。
        if previous and previous['outcome'] not in (S.RUNNING, S.INTERRUPTED, S.WAIT_COMMIT) and previous['result_json'] != '{}':
            old = json.loads(previous['result_json'])
            products = [p for p in old.get('problems', []) if p['category'] == 'product'] if previous['outcome'] in (S.WAIT_VALIDATION, '通过') else None
            if self.tasks.read(task['id'])['status'] in (S.ANALYZING, S.FIXING):
                status = S.VERIFYING if products else S.DEVELOPER
                if previous['outcome'] == '未通过':
                    with self.tasks.store.connection() as db:
                        exhausted = failures_exhausted(db, task['id'])
                    status = S.DEVELOPER if exhausted else S.QUEUED
                self.tasks.update(task['id'], attempt=number, status=status,
                    result_json=previous['result_json'], error=previous['reason'])
            with self.tasks.store.transaction() as db:
                self.tasks.kernel.register(task, dict(products=products), connection=db, phase='fix-output', number=number)
            return products
        task = dict(task, attempt=number - 1)
        if answer is None:
            answer = json.loads((self.tasks.directory(task['id']) / 'final.json').read_text(encoding='utf-8'),
                object_pairs_hook=judgment_object, parse_constant=invalid_judgment_constant)
        products = self.tasks.fixes.finish(task, answer)
        current = self.tasks.read(task['id'])
        if current.get('retry_reason') != 'git-identity':
            with self.tasks.store.transaction() as db:
                self.tasks.kernel.register(task, dict(products=products), connection=db, phase='fix-output')
        return products

    def schedule(self, task):
        if not self.tasks.kernel.check_due(task, self.waiting):
            return
        if self.tasks.service.environment.approved():
            self.tasks.fixes.schedule(task)
        else:
            self.tasks.update(task['id'], error='等待工作区自检通过')

    def recover(self):
        if self.tasks.service.workspace.configured:
            self.tasks.fixes.recover_promotions()

    def recheck(self, task):
        if task['status'] == S.WAIT_COMMIT and self.tasks.fixes.committed(task):
            return S.QUEUED
        return None

    def actions(self, task):
        allowed = set()
        if self.tasks.kernel.claim(task):
            return {'cancel'} if task['status'] not in FIX_NO_CANCEL else set()
        if task['status'] == S.DEVELOPER:
            allowed.add('approve')
        if task['status'] in FIX_RETRY or (
                task['status'] == S.DEVELOPER and task.get('retry_reason') in ('preparation', 'interrupted', 'git-identity')):
            allowed.add('retry')
        if task['status'] not in FIX_NO_CANCEL:
            allowed.add('cancel')
        return allowed


class FixValidationType(TaskType):
    visibility = task_visibility.FIX
    waiting = WaitCondition('复验浏览器', .1)
    """复验借用修复任务的工作线程，但准备、启动和登记按独立类型处理。"""
    def inputs(self, task, *, product):
        if self.tasks.kernel.claim(task):
            return Inputs(product, dict(product_digest='复测产品内容'))
        return Inputs(product, {key: '复测产品' for key in product})

    def prepare(self, task, *, runtime, environment, validator):
        return validator.prepare(task, runtime, environment)

    def launch(self, task, *, environment):
        return Launch(sandbox='workspace-write', environment=environment)

    def consume(self, task, *, runtime, session, validator, reason='', register=True):
        # 环境恢复失败仍须计作复验失败，故保留读取和恢复后登记两个阶段。
        return validator.register(task, runtime, session, reason) if register else validator.read_output(task, runtime, session)

    def actions(self, task):
        return self.tasks.types['fix'].actions(task)
