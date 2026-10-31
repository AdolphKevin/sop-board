"""会话修复：复用根因与复验契约，当前工作区不执行 Git 写入。"""
import hashlib
import json
import re
import yaml

from .errors import reject
from .fix_validation import FixValidation
from .kernel import FIX_TERMINAL, InputChanged, S, session_scheduling_tasks, sql_states
from .repositories import changed_files, product_files
from .service import canonical, now
from .time_format import local_time
from .workspace import digest, under


def same_session(left, right):
    return ' '.join(left.casefold().split()) == ' '.join(right.casefold().split())


def present(manifests):
    """内容比较时，工作区里“已跟踪但已删除”与提交后“不存在”是同一状态：删除文件的修复提交后不算内容变化。"""
    return {alias: [item for item in items if not item.get('deleted')] for alias, items in manifests.items()}


class SessionFixes:
    def __init__(self, tasks):
        self.tasks, self.fixes = tasks, tasks.fixes
        self.service, self.store = tasks.service, tasks.store
        self.workspace = self.service.workspace

    def info(self, task_id):
        info = self.tasks.read(task_id)
        if info['workspace'] != self.workspace.name or info['kind'] != 'fix' or not self.tasks.kernel.claim(info):
            reject(409, 'session-fix-required', 'task', '须为当前工作区的会话修复任务')
        return info

    def files(self, aliases):
        return {a: product_files(self.workspace.repos[a]) for a in aliases}

    def state(self, task_id, db):
        row = db.execute('SELECT * FROM session_fix_state WHERE task=?', (task_id,)).fetchone()
        return {name: json.loads(row[name + '_json']) for name in ('submitted', 'verified')}

    def check_current(self, info, scope=None, *, protected=True):
        """D101：只核对本修复涉及的场景（scope 缺省为问题场景）与仓库身份；其他场景、批次、无关用例与工作区配置的变化不使修复作废。
        修复会话期间（再次准备、fix submit）另核对受保护的 SOP 与场景文件；修复输出登记后的复验与关闭不再核对，
        之后使用者登记的新 SOP 版本只在改动问题场景时才挡住它们。开始新工作的自检要求由各准备入口负责。"""
        self.workspace.reload()
        stale = self.fixes.scenario_changes(info, scope)
        if stale:
            raise InputChanged(stale)
        for alias, repo in info['snapshot']['repositories'].items():
            if str(self.workspace.repos.get(alias)) != repo['directory'] or self.workspace.identities.get(alias) != repo['identity']:
                raise InputChanged(alias + ' 的仓库身份或路径变化')
        changed = self.fixes.protected_changes(info) if protected else []
        if changed:
            raise ValueError('修复改动涉及受保护的 SOP 或场景文件：' + '、'.join(changed))

    def prepare(self, owner, selected, issues=()):
        """issues：按问题台账编号认领这些问题当前可认领的失败场景（D98），可与 --scenario 同用。"""
        self.service.require_ready(self.workspace.name)
        requested = set()
        for item in selected or []:
            batch, separator, scene = item.partition('#')
            if not separator or not batch or not scene or (batch, scene) in requested:
                raise ValueError('--scenario 须为不重复的 批次#场景')
            requested.add((batch, scene))
        with self.store.connection() as db:
            owned = None
            for row in db.execute("SELECT t.*,s.session_id AS owner FROM tasks t JOIN session_tasks s ON s.task=t.id "
                    f"WHERE t.workspace=? AND t.kind='fix' AND t.status NOT IN ({sql_states(FIX_TERMINAL)}) ORDER BY t.id", (self.workspace.name,)):
                if row['owner'] == owner:
                    owned = dict(row)
                    break
                # 其他会话的未结束修复只阻止认领重叠的场景（D98），重叠由下方 fix-already-owned 判断。
            occupied = {(row['batch'],row['scenario']) for row in db.execute(
                'SELECT p.batch,p.scenario FROM fix_problems p JOIN tasks t ON t.id=p.task WHERE t.workspace=? '
                f'AND t.status NOT IN ({sql_states(FIX_TERMINAL)})', (self.workspace.name,))}
            for batch, scene in requested:
                busy = db.execute("SELECT t.id FROM fix_problems p JOIN tasks t ON t.id=p.task WHERE t.workspace=? "
                    f"AND p.batch=? AND p.scenario=? AND t.status NOT IN ({sql_states(FIX_TERMINAL)}) ORDER BY t.id LIMIT 1",
                    (self.workspace.name, batch, scene)).fetchone()
                if busy and (not owned or busy['id'] != owned['id']):
                    reject(409, 'fix-already-owned', 'scenario', f'{batch}#{scene} 已在修复任务 #{busy["id"]} 中，不能重复认领')
            covered = set()
            if owned:
                covered = {(r['batch'], r['scenario']) for r in db.execute('SELECT batch,scenario FROM fix_problems WHERE task=?', (owned['id'],))}
                if requested and not requested.issubset(covered):
                    reject(409, 'fix-scope-active', 'scenario', f'请先处理已有会话修复 #{owned["id"]}，本次不能扩大问题范围')
            rows = [r for r in session_scheduling_tasks(db) if not owned or r['id'] != owned['id']]
            blocker = self.tasks.kernel.session_blocker(dict(id=-1, kind='fix', workspace=self.workspace.name, batch='', status=S.SESSION), rows)
            if blocker and blocker['kind'] == 'fix' and blocker['status'] == S.SESSION:
                # 会话修复共用当前工作区，同时改代码会让各自的改动混在一起；等对方提交修复输出（进入待验证）后即可准备。
                reject(409, 'task-blocked', 'task', f'会话修复 #{blocker["id"]} 正在修改产品代码，等它提交修复输出后再准备')
            if blocker:
                reject(409, 'task-blocked', 'task', f'等待任务 #{blocker["id"]}（{blocker["status"]}）结束')
            problems = self.fixes.unresolved(db, self.workspace.name, reclaim=True)
            from .issues import scenarios_of
            listed = scenarios_of(db, self.workspace.name, issues) if issues else set()
            claimable = {(p['batch'], p['scenario']) for p in problems} - occupied
            from_issues = listed & claimable
            if issues and not from_issues and not (owned and listed & covered):
                reject(409, 'nothing-to-fix', 'issue', '问题 ' + '、'.join(map(str, issues)) + ' 关联的场景都没有可认领的失败')
            if owned and from_issues - covered:
                reject(409, 'fix-scope-active', 'issue', f'请先处理已有会话修复 #{owned["id"]}，本次不能扩大问题范围')
        if owned and owned['status'] == S.SESSION and (self.tasks.directory(owned['id']) / 'prompt.md').is_file():
            return owned['id']
        if owned:
            task_id = owned['id']
            self.check_current(self.info(task_id))
            self.tasks.kernel.archive(owned)
            self.tasks.update(task_id, status=S.SESSION, finished_at=None, error='', result_json='{}')
            with self.store.transaction() as db:
                db.execute("UPDATE fix_problems SET category='',root_repo=NULL WHERE task=?", (task_id,))
        else:
            wanted = requested | from_issues
            chosen = [p for p in problems if (p['batch'],p['scenario']) not in occupied and
                (not wanted or (p['batch'], p['scenario']) in wanted)]
            missing = requested - {(p['batch'], p['scenario']) for p in chosen}
            if not chosen or missing:
                reject(409, 'nothing-to-fix', 'scenario', '没有可认领的已放行失败场景' + ('：' + '、'.join(b+'#'+s for b,s in sorted(missing)) if missing else ''))
            with self.store.transaction() as db:
                task_id = db.execute("INSERT INTO tasks(kind,workspace,batch,status,trigger,input_digest,input_json,created_at,attempt,session_id) "
                    "VALUES ('fix',?,'',?,'会话认领问题','','{}',?,0,?)", (self.workspace.name, S.QUEUED, now(), owner)).lastrowid
                db.execute('INSERT INTO fix_tasks(task) VALUES (?)', (task_id,))
                db.execute('INSERT INTO session_fix_state(task) VALUES (?)', (task_id,))
                db.executemany('INSERT INTO fix_problems(task,batch,scenario,fingerprint,result_id,first_attempt,input_json) VALUES (?,?,?,?,?,0,?)',
                    [(task_id,p['batch'],p['scenario'],p['fingerprint'],p['result_id'],canonical(p)) for p in chosen])
                self.tasks.kernel.assign(task_id, owner, connection=db)
                from .issues import link_fix
                link_fix(db, self.workspace.name, task_id, {(p['batch'], p['scenario']) for p in chosen}, issues)
        with self.store.connection() as db:
            task = dict(db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone())
        self.tasks.directory(task_id).mkdir(parents=True, exist_ok=True)
        self.tasks.types['fix'].prepare(task)
        self.tasks.types['fix'].prepare(task, materialize=True)
        with self.store.transaction() as db:
            db.execute("UPDATE session_fix_state SET submitted_json='{}',verified_json='{}' WHERE task=?", (task_id,))
        return task_id

    def consume(self, task, answer):
        info = self.info(task['id'])
        self.fixes.check_answer(task, answer)
        products = [p for p in answer['problems'] if p['category'] == 'product']
        # 只核对本次输出判为产品问题的场景：场景写法、环境等问题的场景本来就可能被改写。
        claimed = {(p['batch'], p['scenario']): p['fingerprint'] for p in info['problems']}
        self.check_current(info, [(p['batch'], p['scenario'], claimed[(p['batch'], p['scenario'])]) for p in products])
        manifests = self.files(info['snapshot']['aliases'])
        changes = changed_files(info['snapshot']['session_files'], manifests)
        if products and not any(changes.values()):
            raise ValueError('修复会话没有相对准备时的产品改动')
        if not products and any(changes.values()):
            raise ValueError('全部问题均为非产品问题，不得修改产品代码；请核对本次改动')
        protected = self.fixes.repositories.protected(task['id'], changes)
        if protected:
            raise ValueError('受保护文件不能作为产品改动：' + '、'.join(protected))
        number = self.tasks.kernel.attempt(task)
        owner = self.tasks.kernel.claim(task)['session_id']
        product = self.workspace.product(info['snapshot']['aliases'])
        status = S.WAIT_VALIDATION if products else S.DEVELOPER
        with self.store.transaction() as db:
            # 提交前的状态检查在事务外，放弃可能在其后完成：写事务内复核，已放弃等状态不登记尝试结论。
            current = db.execute('SELECT status FROM tasks WHERE id=?', (task['id'],)).fetchone()[0]
            if current != S.SESSION:
                reject(409, 'task-state-invalid', 'task', '任务当前状态不能提交：' + current + '；本次修复输出未登记')
            receipt = self.tasks.kernel.receipt(task, connection=db, phase='fix-output', number=number)
            if receipt:
                return receipt['products']
            self.fixes.record_problems(db, task, answer)
            for alias, paths in changes.items():
                self.tasks.kernel.repositories.update('fix', task['id'], alias, changed_files_json=canonical(paths), connection=db)
            db.execute('UPDATE fix_attempts SET outcome=?,finished_at=?,result_json=?,validation_json=? WHERE task=? AND number=?',
                (status,now(),canonical(answer),canonical(dict(status='pending' if products else 'not-required',reason='待独立复验' if products else '请使用者处理')),task['id'],number))
            db.execute('UPDATE tasks SET attempt=?,status=?,session_id=?,result_json=?,finished_at=?,error=? WHERE id=?',
                (number,status,owner,canonical(answer),now(), '修复已登记，待新会话独立复验' if products else '非产品问题，交使用者处理，不进入复验',task['id']))
            db.execute("UPDATE session_fix_state SET submitted_json=?,verified_json='{}' WHERE task=?",
                (canonical(dict(product=product,files=manifests,changes=changes)),task['id']))
            self.tasks.kernel.register(task, dict(products=products), connection=db, phase='fix-output', number=number)
        (self.tasks.directory(task['id']) / 'changes.json').write_text(canonical(changes), encoding='utf-8')
        return products

    def prepare_validation(self, fix_id, owner):
        info = self.info(fix_id)
        if same_session(owner,self.tasks.kernel.claim(info)['session_id']):
            reject(409,'independent-session-required','session','复验须由未参与修复的新会话执行，执行者不能与修复会话相同')
        if info['status'] not in (S.WAIT_VALIDATION, S.WAIT_COMMIT):
            reject(409, 'fix-not-ready', 'fix', '修复须先提交产品修复输出，处于待验证或等待开发者提交状态')
        # 复验是一次执行：开始前须自检通过（D101 之前由修复冻结的自检状态间接保证）。
        self.service.require_ready(self.workspace.name)
        self.check_current(info, protected=False)
        environment = self.service.environment
        if environment.external() and not environment.check_external(record=False):
            raise ValueError(environment.state['error'] + '；已登记待人处理，环境恢复后重新准备复验')
        with self.store.connection() as db:
            active = db.execute('SELECT t.*,s.session_id AS owner FROM session_fix_executions v JOIN tasks t ON t.id=v.execution '
                'JOIN session_tasks s ON s.task=t.id WHERE v.fix=? AND t.status=?', (fix_id,S.SESSION)).fetchone()
            if active:
                if active['owner'] != owner:
                    reject(409,'task-owned','session',f'复验任务 #{active["id"]} 已由另一会话认领')
                directory = self.tasks.directory(active['id'])
                if not (directory/'prompt.md').is_file() or not (directory/'input.json').is_file():
                    return self.materialize_validation(active['id'])
                return active['id']
            # D83：排队或等待中的看板修复不阻挡会话复验，实际运行/恢复环境仍互斥。
            rows = [r for r in session_scheduling_tasks(db) if r['id'] != fix_id]
            blocker = self.tasks.kernel.session_blocker(dict(id=-1,kind='execute',workspace=self.workspace.name,batch=''),rows)
            if blocker:
                reject(409,'task-blocked','task',f'等待任务 #{blocker["id"]}（{blocker["status"]}）结束')
        changes = changed_files(info['snapshot']['session_files'], self.files(info['snapshot']['aliases']))
        for repo in info['repositories']:
            repo['changed_files'] = changes[repo['alias']]
            self.fixes.repositories.update(fix_id, repo['alias'], changed_files_json=canonical(repo['changed_files']))
        validator = FixValidation(self.fixes)
        products = [p for p in info['result']['problems'] if p['category']=='product']
        selected, aliases = validator.selection(info, products)
        product = self.workspace.product(aliases)
        manifests = self.files(aliases)
        # 业务配置以复验准备时为准（D101），复验期间变化仍按执行规则作废。
        snapshot = dict(info['snapshot'], aliases=aliases, skill_digest=self.workspace.skill_digest(), session_files=manifests,
                        session_files_stage='复验准备时的产品文件清单（修复输出已登记）', config_digest=self.workspace.config_digest)
        from .fixtures import definition
        snapshot.update(execution_entries=self.workspace.execution_entries(), fixtures=definition(self.workspace))
        with self.store.transaction() as db:
            sequence = db.execute('SELECT COALESCE(MAX(sequence),0)+1 FROM session_fix_executions WHERE fix=? AND attempt=?',(fix_id,info['attempt'])).fetchone()[0]
            execution = db.execute("INSERT INTO tasks(kind,workspace,batch,status,trigger,input_digest,input_json,snapshot_json,scope_json,created_at) "
                "VALUES ('execute',?,'',?,'会话独立复验',?,?,?,?,?)",(self.workspace.name,S.QUEUED,
                hashlib.sha256(canonical(product).encode()).hexdigest(),canonical(dict(fix=fix_id,attempt=info['attempt'],sequence=sequence,selected=selected,aliases=aliases)),
                canonical(dict(business=snapshot,product=product,files=manifests)),canonical([dict(batch=s['batch'],scenario=s['id'],reason='rerun') for s in selected]),now())).lastrowid
            db.execute('INSERT INTO session_fix_executions VALUES (?,?,?,?)',(execution,fix_id,info['attempt'],sequence))
            self.tasks.kernel.assign(execution,owner,connection=db)
            db.execute("UPDATE session_fix_state SET verified_json='{}' WHERE task=?",(fix_id,))
            db.execute('UPDATE tasks SET status=?,error=? WHERE id=?',(S.WAIT_VALIDATION,f'等待独立复验任务 #{execution}',fix_id))
        return self.materialize_validation(execution)

    def cancel_validation(self, execution):
        """取消认领与父修复的等待说明一并保存，不改写其他尝试或新复验。"""
        with self.store.transaction() as db:
            row = db.execute('SELECT v.fix,v.attempt FROM session_fix_executions v JOIN tasks t ON t.id=v.execution '
                'WHERE v.execution=? AND t.status=?',(execution,S.SESSION)).fetchone()
            if row is None:
                return False
            db.execute('UPDATE tasks SET status=?,finished_at=?,error=? WHERE id=?',
                (S.ABANDONED,now(),'使用者取消独立复验任务',execution))
            db.execute('UPDATE tasks SET error=? WHERE id=? AND attempt=? AND status=? AND error=? '
                'AND NOT EXISTS (SELECT 1 FROM session_fix_executions v JOIN tasks child ON child.id=v.execution '
                'WHERE v.fix=? AND v.attempt=? AND child.status=?)',
                (f'复验任务 #{execution} 已取消，等待新的独立复验会话',row['fix'],row['attempt'],S.WAIT_VALIDATION,
                 f'等待独立复验任务 #{execution}',row['fix'],row['attempt'],S.SESSION))
        return True

    def materialize_validation(self, execution):
        with self.store.connection() as db:
            fix_id = db.execute('SELECT fix FROM session_fix_executions WHERE execution=?',(execution,)).fetchone()[0]
        info = self.info(fix_id)
        runtime = self.runtime(execution)
        selected, aliases = runtime['selected'], runtime['aliases']
        directory = runtime['directory'];directory.mkdir(parents=True,exist_ok=True);(directory/'evidence').mkdir(exist_ok=True)
        environment = self.service.environment.agent_environment(self.workspace.repos)
        from .credentials import blocked_results
        runtime['blocked'] = blocked_results(selected,self.workspace.entries,environment)
        blocked = {(s['batch'],s['scenario']) for s in runtime['blocked']}
        runtime['runnable'] = [s for s in selected if (s['batch'],s['id']) not in blocked]
        self.tasks.validation_type.prepare(info,runtime=runtime,environment=environment,validator=FixValidation(self.fixes))
        (directory/'input.json').write_text(canonical(dict(fix=fix_id,selected=selected,aliases=aliases)),encoding='utf-8')
        return execution

    def runtime(self, execution):
        with self.store.connection() as db:
            raw = db.execute('SELECT * FROM tasks WHERE id=?',(execution,)).fetchone()
        inputs,snapshot=json.loads(raw['input_json']),json.loads(raw['snapshot_json'])
        directory=self.tasks.directory(execution)
        blocked=json.loads((directory/'credential-blocked.json').read_text()) if (directory/'credential-blocked.json').is_file() else []
        ids={(s['batch'],s['scenario']) for s in blocked}
        return dict(directory=directory,snapshot=snapshot['business'],info=snapshot['product'],selected=inputs['selected'],
            aliases=inputs['aliases'],directories=self.workspace.repos,number=inputs['attempt'],sequence=inputs['sequence'],
            rows=[],blocked=blocked,runnable=[s for s in inputs['selected'] if (s['batch'],s['id']) not in ids])

    def consume_validation(self, task, owner):
        receipt = self.tasks.kernel.receipt(task, phase='execute')
        if receipt is not None:
            return receipt
        inputs=json.loads(task['input_json']);fix=self.info(inputs['fix'])
        if fix['status'] not in (S.WAIT_VALIDATION,S.WAIT_COMMIT) or fix['attempt'] != inputs['attempt']:
            raise InputChanged('原修复已取消或已有新的修复尝试')
        if same_session(owner,self.tasks.kernel.claim(fix)['session_id']):
            raise ValueError('复验须由未参与修复的新会话执行，执行者不能与修复会话相同')
        self.check_current(fix, protected=False)
        runtime=self.runtime(task['id']);validator=FixValidation(self.fixes)
        if self.workspace.skill_changed(runtime['snapshot']):
            raise InputChanged('验证技能变化，须重新准备独立复验')
        if self.workspace.product(runtime['aliases'])['product_digest'] != runtime['info']['product_digest']:
            raise InputChanged('复验准备后产品内容变化，须重新准备独立复验')
        reason=self.tasks.validation_type.consume(fix,runtime=runtime,session=owner,validator=validator,register=False)
        for row in runtime['rows']:
            row.update(executor=owner,session=owner)
        with self.store.transaction() as db:
            # 取消复验或放弃原修复可能在事务外的检查之后完成：写事务内复核两者，变化时不登记轮次与结论。
            current=db.execute('SELECT status FROM tasks WHERE id=?',(task['id'],)).fetchone()[0]
            if current!=S.SESSION:
                reject(409,'task-state-invalid','task','复验任务当前状态不能提交：'+current+'；本次复验未登记')
            parent=db.execute('SELECT status,attempt FROM tasks WHERE id=?',(fix['id'],)).fetchone()
            if parent['status'] not in (S.WAIT_VALIDATION,S.WAIT_COMMIT) or parent['attempt'] != inputs['attempt']:
                raise InputChanged('原修复已取消或已有新的修复尝试')
            validation=validator.register(fix,runtime,owner,reason,connection=db)
            if validation['status']=='void':
                # 登记事务内发现输入变化：回滚本事务，不登记轮次、不改修复尝试，交由作废处理重新准备。
                raise InputChanged(validation['reason'])
            rounds=[]
            for batch_name in dict.fromkeys(s['batch'] for s in runtime['selected']):
                batch=self.service.batch(db,fix['workspace'],batch_name);version=self.service.latest_version(db,batch)
                aliases=self.workspace.aliases(yaml.safe_load(version['content']))
                repositories={a:runtime['info']['repos'][a] for a in aliases}
                product_digest=digest({a:v['digest'] for a,v in repositories.items()})
                body=dict(idempotency_key=f'session-fix-{fix["id"]}-{task["id"]}-{batch_name}',scenario_file_sha256=version['sha256'],
                    submitted_by=owner,implementer=fix['session_id'],execution_origin='session',
                    product=dict(commit=', '.join(a+':'+v['commit'] for a,v in repositories.items()),digest=product_digest,
                        dirty=any(path.split('/',1)[0] in aliases for path in runtime['info']['changed'])),
                    results=[r for r in runtime['rows'] if r['batch']==batch_name])
                number=self.service.register_round(db,batch,version,body,skill_digest=runtime['snapshot']['skill_digest'])
                from .fixtures import link_results
                link_results(db, task['id'], batch, number, runtime['directory'])
                from .atlas import record_round
                record_round(db, batch['id'], number, runtime['directory'])
                if not rounds:  # 同一复验目录的反馈只收一次
                    self.tasks.atlas.collect_feedback(db, task['id'], runtime['directory'])
                db.execute('UPDATE batches SET product_digest=? WHERE id=?',(product_digest,batch['id']))
                db.execute('UPDATE fix_validations SET promoted_round=? WHERE task=? AND attempt=? AND validation=? AND batch=?',
                    (number,fix['id'],runtime['number'],runtime['sequence'],batch_name))
                rounds.append(dict(batch=batch_name,round=number))
            passed=validation['status']=='pass'
            verified=dict(product=runtime['info'],files=json.loads(task['snapshot_json'])['files'],execution=task['id'],
                skill_digest=runtime['snapshot']['skill_digest'],rounds=rounds,session=owner) if passed else {}
            db.execute('UPDATE session_fix_state SET verified_json=? WHERE task=?',(canonical(verified),fix['id']))
            db.execute('UPDATE fix_attempts SET outcome=?,validation_json=? WHERE task=? AND number=?',
                ('通过' if passed else '未通过',canonical(validation),fix['id'],runtime['number']))
            db.execute('UPDATE tasks SET status=?,rounds_json=?,error=? WHERE id=?',
                (S.WAIT_COMMIT if passed else S.WAIT_VALIDATION,canonical(rounds),'独立复验通过，等待开发者审核提交后 fix land' if passed else '复验未通过，修正后重新准备独立复验：'+reason,fix['id']))
            self.tasks.kernel.register(task,dict(rounds=rounds,validation=validation),connection=db,phase='execute')
        return dict(rounds=rounds,validation=validation)

    RESULT_LABELS = dict(fail='失败', blocked='受阻', skip='跳过')
    RECORD_LIMIT = 100  # 关闭记录最多列出的“其他变化”条数，总数另记

    def verified_by_rounds(self, task_id, info):
        """D98：没有专门复验时，产品问题的场景都在修复登记之后由修复会话以外的执行者通过，
        且那一轮准备时修复改动的文件内容与现在相同，才视为独立复验通过；返回与复验快照同形的记录，
        不满足时返回空记录与逐个场景的原因（说明、变化的修复文件、场景与轮次），供 fix land 列出须补跑的场景。
        比较用执行轮次同样的产品文件清单（排除 snapshot_exclude 与场景目录）。已放弃的修复（D102）同样适用。"""
        if info['status'] not in (S.WAIT_VALIDATION, S.WAIT_COMMIT, S.ABANDONED):
            return {}, []
        with self.store.connection() as db:
            running=db.execute('SELECT t.id FROM session_fix_executions v JOIN tasks t ON t.id=v.execution WHERE v.fix=? AND t.status=?',
                (task_id,S.SESSION)).fetchone()
            if running:
                reject(409,'fix-validation-running','fix',f'复验任务 #{running["id"]} 尚未结束，请先提交或放弃该复验')
            submitted=self.state(task_id,db)['submitted']
            attempt=db.execute('SELECT finished_at FROM fix_attempts WHERE task=? AND number=?',(task_id,info['attempt'])).fetchone()
            problems=[dict(r) for r in db.execute("SELECT DISTINCT batch,scenario FROM fix_problems WHERE task=? AND category='product'",(task_id,))]
            if not submitted or not attempt or not attempt['finished_at'] or not problems:
                return {}, []
            changes=submitted.get('changes',{})
            aliases=list(changes)
            owner=self.tasks.kernel.claim(info,connection=db)['session_id']
            current=self.workspace.product(aliases,files=True)['files']
            index={a:{item['path']:item for item in items} for a,items in present(current).items()}
            skill=self.workspace.skill_digest()
            latest,reasons=None,[]
            for problem in problems:
                label=problem['batch']+'#'+problem['scenario']
                row=db.execute("SELECT r.status,r.executor,r.independence,t.number,t.submitted_at,t.submitted_by,t.skill_digest,t.idempotency_key FROM results r JOIN rounds t ON t.id=r.round_id "
                    "JOIN batches b ON b.id=t.batch WHERE b.workspace=? AND b.name=? AND r.scenario=? AND t.kind='agent' ORDER BY t.number DESC,r.id DESC LIMIT 1",
                    (self.workspace.name,problem['batch'],problem['scenario'])).fetchone()
                if row is None:
                    reasons.append((label+'（没有执行结果）',[],None))
                    continue
                where=f"第 {row['number']} 轮"
                if row['status']!='pass':
                    reasons.append((f"{label}（最近结果为{self.RESULT_LABELS.get(row['status'],row['status'])}，{where}）",[],None))
                    continue
                if row['submitted_at']<=attempt['finished_at']:
                    reasons.append((f'{label}（最近的通过在修复输出登记之前，{where}）',[],None))
                    continue
                if row['independence']=='self-check' or same_session(row['executor'],owner) or same_session(row['submitted_by'],owner):
                    reasons.append((f'{label}（{where}由修复会话执行或为自检）',[],None))
                    continue
                if row['skill_digest']!=skill:
                    reasons.append((f'{label}（{where}之后验证技能有变化）',[],None))
                    continue
                match=re.fullmatch(r'execute-([0-9]+)-[0-9]+',row['idempotency_key'] or '')
                manifest_path=self.tasks.directory(int(match[1]))/'product-files.json' if match else None
                if manifest_path is None or not manifest_path.is_file():
                    reasons.append((f'{label}（{where}没有产品文件清单，无法比对修复改动）',[],None))
                    continue
                manifest=json.loads(manifest_path.read_text(encoding='utf-8'))
                differ=[]
                for alias,paths in changes.items():
                    tested={item['path']:item for item in manifest.get(alias,[]) if not item.get('deleted')}
                    differ+=[alias+'/'+path for path in paths if tested.get(path)!=index.get(alias,{}).get(path)]
                if differ:
                    reasons.append((f'{label}（{where}之后修复改动的 {len(differ)} 个文件又有变化，如 {differ[0]}）',differ,f'{label} {where}'))
                    continue
                if latest is None or row['submitted_at']>latest[0]:
                    latest=(row['submitted_at'],{a:manifest.get(a,[]) for a in aliases},row['submitted_by'],f"{problem['batch']} 第 {row['number']} 轮")
        if reasons:
            return {}, reasons
        return dict(product=dict(repos={a:{} for a in aliases}),files=latest[1],skill_digest=skill,session=latest[2],
                    via='rounds',round=latest[3]), []

    def land(self, task_id):
        info=self.info(task_id)
        if info['status']==S.HANDLED:
            self.fixes.complete(task_id)
            return dict(ok=True,task=self.tasks.read(task_id),duplicate=True)
        abandoned=info['status']==S.ABANDONED
        if abandoned:
            self.check_abandoned(info)
        self.check_current(info, protected=False)
        with self.store.connection() as db:state=self.state(task_id,db)
        verified,reasons=(state['verified'],[]) if state['verified'] else self.verified_by_rounds(task_id,info)
        if not verified and reasons and all(files for _,files,_ in reasons):
            # 唯一的问题是通过之后修复改动的文件又变了：与专门复验的内容核对同样报 fix-content-changed。
            changed=list(dict.fromkeys(path for _,files,_ in reasons for path in files))
            reject(409,'fix-content-changed','product','修复改动的文件与独立通过时的快照不同，须重新独立验证：'+'、'.join(changed)+
                   '（在 '+'、'.join(where for _,_,where in reasons)+'之后变化）')
        if not verified:
            reject(409,'fix-validation-required','fix','没有本次独立复验通过记录，也没有在包含当前修复改动的产品上由独立执行者通过的轮次，不能关闭修复'+
                   ('：'+'；'.join(text for text,_,_ in reasons)+'。由修复会话以外的执行者在当前产品上重新执行这些场景、通过后再关闭' if reasons else ''))
        if self.workspace.skill_digest()!=verified['skill_digest']:
            reject(409,'fix-content-changed','product','验证技能与独立通过时不同，须重新独立验证')
        aliases=list(verified['product']['repos']);product=self.workspace.product(aliases,files=True)
        current=product['files'] if verified.get('via')=='rounds' else self.files(aliases)
        fixed={alias:set(paths) for alias,paths in state['submitted'].get('changes',{}).items()}
        def excluded(alias,path):
            # 场景目录与 snapshot_exclude 不属于产品内容；专门复验的清单未排除它们，这里统一排除。
            roots=list(self.workspace.snapshot_exclude.get(alias,[]))
            if self.workspace.scenarios.is_relative_to(self.workspace.repos[alias]):
                roots.append(self.workspace.scenarios.relative_to(self.workspace.repos[alias]))
            return under(path,roots)
        changed=[(alias,p) for alias,items in changed_files(present(verified['files']),present(current)).items() for p in items if not excluded(alias,p)]
        dirty=[tuple(path.split('/',1)) for path in product['changed']]
        # D100：只核对修复改动的文件——须与独立通过时一致且已提交；其他文件的变化只记入关闭记录，交给产品变化的影响判定。
        paths=[alias+'/'+p for alias,p in changed if p in fixed.get(alias,())]
        if paths:
            reject(409,'fix-content-changed','product','修复改动的文件与独立通过时的快照不同，须重新独立验证：'+'、'.join(paths))
        pending=[alias+'/'+p for alias,p in dirty if p in fixed.get(alias,())]
        if pending:
            reject(409,'fix-commit-required','product','请开发者审核并提交修复改动后再关闭：'+'、'.join(pending))
        before={alias:{r['path']:r for r in items} for alias,items in present(verified['files']).items()}
        after={alias:{r['path']:r for r in items} for alias,items in present(current).items()}
        others=[(alias,p) for alias,p in sorted(set(dirty)|set(changed)) if p not in fixed.get(alias,())]
        def entry(alias,p):
            return dict(path=alias+'/'+p,uncommitted=(alias,p) in dirty,same_as_verified=before.get(alias,{}).get(p)==after.get(alias,{}).get(p))
        local=[entry(alias,p) for alias,p in others if under(p,self.workspace.local_only.get(alias,[]))]
        other=[entry(alias,p) for alias,p in others if not under(p,self.workspace.local_only.get(alias,[]))]
        basis='独立执行（'+verified['round']+'）' if verified.get('via')=='rounds' else '独立复验'
        note=(f'（此前于 {local_time(info["finished_at"])} 放弃，D102）' if abandoned and info.get('finished_at') else '')+'；修复改动已提交，与'+basis+'内容一致'
        note+=f'；带有 {len(local)} 个本机保留文件' if local else ''
        note+=f'；验证后另有 {len(other)} 处修复以外的变化，不计入核对，按产品变化判定影响' if other else ''
        record={**({'local_only':local} if local else {}),**({'other_changes':other[:self.RECORD_LIMIT]} if other else {}),
                **({'other_changes_count':len(other)} if len(other)>self.RECORD_LIMIT else {})}
        with self.store.transaction() as db:
            for repo in info['repositories']:
                commit=product['repos'][repo['alias']]['commit']
                self.tasks.kernel.repositories.update('fix',task_id,repo['alias'],repair_commit=commit,merged_commit=commit,
                    merge_status=S.HANDLED,merge_reason='开发者已提交，内容与'+basis+'一致',connection=db)
            commits={a:r['commit'] for a,r in product['repos'].items()}
            db.execute('UPDATE fix_attempts SET commits_json=?,merged_commits_json=?,outcome=? WHERE task=? AND number=?',
                (canonical(commits),canonical(commits),S.HANDLED,task_id,info['attempt']))
            if verified.get('via') == 'rounds':
                # 关闭依据是普通执行轮次时，把依据写入尝试记录，修复页与场景详情据此显示。
                validation=dict(status='passed',reason='独立执行（'+verified['round']+'）通过，内容一致',round=verified['round'])
            else:
                validation=next((a['validation'] for a in info['attempts'] if a['number']==info['attempt']),{})
            if verified.get('via') == 'rounds' or record:
                # 本机保留文件与验证后的其他变化随关闭依据记入尝试记录，可追溯关闭时与验证快照的差异。
                db.execute('UPDATE fix_attempts SET validation_json=? WHERE task=? AND number=?',
                    (canonical(dict(validation,**record)),task_id,info['attempt']))
            db.execute('UPDATE tasks SET status=?,finished_at=?,error=? WHERE id=?',(S.HANDLED,now(),'会话修复已关闭'+note,task_id))
        self.fixes.complete(task_id)
        return dict(ok=True,task=self.tasks.read(task_id),**record)

    def check_abandoned(self, info):
        """D102：已放弃的会话修复，最后一次尝试已登记产品修复输出、问题没有被其他未放弃的修复认领时，才可按独立通过关闭。"""
        attempt=next((a for a in info['attempts'] if a['number']==info['attempt']),None)
        if attempt is None or attempt['outcome'] in (S.ABANDONED,S.RUNNING,S.INTERRUPTED) or not any(p['category']=='product' for p in info['problems']):
            reject(409,'fix-not-ready','fix','已放弃的修复最后一次尝试没有登记产品修复输出，不能关闭；原失败可重新认领修复')
        with self.store.connection() as db:
            others=db.execute("SELECT DISTINCT p.batch,p.scenario,t.id FROM fix_problems p JOIN fix_problems o ON o.batch=p.batch AND o.scenario=p.scenario "
                "AND o.task!=p.task JOIN tasks t ON t.id=o.task WHERE p.task=? AND p.category='product' AND t.status!=? ORDER BY t.id",
                (info['id'],S.ABANDONED)).fetchall()
        if others:
            reject(409,'fix-problem-reclaimed','fix','问题场景已由其他修复认领，不能再关闭本修复：'+
                   '、'.join(f"{r['batch']}#{r['scenario']}（修复 #{r['id']}）" for r in others))
