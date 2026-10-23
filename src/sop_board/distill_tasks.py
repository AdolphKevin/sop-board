"""看板沉淀任务：固定输入、复用工作树适配、登记及开发者处理。"""

from contextlib import nullcontext
import hashlib
from importlib.resources import files
import json
from pathlib import Path
import re
import sqlite3
from string import Template
import subprocess

from .kernel import (DISTILL_RECOVERY, DISTILL_TERMINAL as TERMINAL, Inputs, S,
                     codex_available, codex_failure, session_id, sql_states, terminate_group, wait_codex)

from . import distill_inputs, regressions
from .errors import APIError, reject
from .repair_repositories import head, ancestor
from .repositories import git, product_files
from .service import canonical, now
from .tasks import judgment_object, invalid_judgment_constant
from .worktrees import WorktreeError



def problem_text(error):
    return '；'.join(p['field'] + '：' + p['problem'] for p in error.problems) if isinstance(error, APIError) else str(error)


class DistillTasks:
    def __init__(self, tasks):
        self.tasks, self.service, self.store = tasks, tasks.service, tasks.store
        self.workspace = self.service.workspace

    def recover(self):
        with self.store.transaction() as db:
            # 进程来不及执行 finally 时，也从本次尝试的实际事件找回会话身份。
            for row in db.execute("SELECT id FROM tasks WHERE kind='distill' AND (session_id IS NULL OR session_id='') "
                    "AND NOT EXISTS (SELECT 1 FROM session_tasks s WHERE s.task=tasks.id)").fetchall():
                session = self.session(row)
                if session:
                    db.execute('UPDATE tasks SET session_id=? WHERE id=?', (session, row['id']))
            # 登记收据与关联同事务提交；状态更新前退出也能恢复，无需再运行会话。
            for row in db.execute("SELECT t.id,r.result_json,r.registered_at FROM tasks t "
                "JOIN distill_registrations r ON r.task=t.id AND r.attempt=t.attempt "
                "JOIN distill_tasks d ON d.task=t.id WHERE t.kind='distill' "
                f"AND t.status IN ({sql_states(DISTILL_RECOVERY)}) AND d.decision='' "
                "AND NOT EXISTS (SELECT 1 FROM session_tasks s WHERE s.task=t.id)").fetchall():
                db.execute(f"UPDATE tasks SET status='{S.REVIEW}',result_json=?,finished_at=?,error='' WHERE id=?",
                           (row['result_json'], row['registered_at'], row['id']))

    def repositories(self, task_id, db=None):
        return self.tasks.kernel.repositories.rows(task_id, 'distill', connection=db)

    def busy(self, key):
        with self.store.connection() as db:
            return bool(db.execute(f"SELECT 1 FROM tasks WHERE workspace=? AND kind='distill' AND status='{S.DISTILLING}'", (key,)).fetchone()
                or any(task_id in self.tasks.workers for task_id, in db.execute("SELECT id FROM tasks WHERE workspace=? AND kind='distill'", (key,))))

    def enqueue(self, key, name, body):
        if (not isinstance(body, dict) or body.keys() - {'scenarios', 'needs_environment'}
                or type(body.get('needs_environment', False)) is not bool):
            reject(422, 'distill-invalid', 'body', '仅接受 scenarios 数组及 needs_environment 布尔值')
        selected = body.get('scenarios')
        if (not isinstance(selected, list) or not selected or any(not isinstance(s, str) or not s for s in selected)
                or len(set(selected)) != len(selected)):
            reject(422, 'distill-invalid', 'scenarios', '请选择不重复的场景编号')
        with self.tasks.lock:
            self.service.require_ready(key)
            try:
                snapshot, inputs = distill_inputs.freeze(self.service, key, name, selected)
            except (OSError, ValueError, subprocess.SubprocessError) as error:
                reject(409, 'distill-input-invalid', 'scenarios', str(error))
            with self.store.transaction() as db:
                status = S.WAIT_COMMIT if snapshot['dirty'] else S.QUEUED
                task_id = db.execute("INSERT INTO tasks(kind,workspace,batch,status,trigger,input_digest,input_json,"
                    "scenario_file_sha256,created_at,scope_json,snapshot_json,error) VALUES ('distill',?,?,?,?,?,?,?,?,?,?,?)",
                    (key, name, status, '开发者发起回归沉淀', hashlib.sha256(canonical(snapshot).encode()).hexdigest(), canonical(inputs),
                     snapshot['scenario_file_sha256'], now(), canonical([dict(scenario=s) for s in selected]), canonical(snapshot),
                     self.dirty_reason(snapshot) if snapshot['dirty'] else '')).lastrowid
                db.execute('INSERT INTO distill_tasks(task,needs_environment) VALUES (?,?)', (task_id, body.get('needs_environment', False)))
        return dict(ok=True, task=self.tasks.read(task_id))

    @staticmethod
    def dirty_reason(snapshot):
        return '等待提交产品改动：' + '、'.join(snapshot['changed'])

    def inputs(self, task):
        return Inputs(json.loads(task['snapshot_json']), distill_inputs.INPUT_NAMES)

    def current(self, task):
        selected = [s['scenario'] for s in json.loads(task['scope_json'])]
        return distill_inputs.freeze(self.service, task['workspace'], task['batch'], selected, enforce=False)

    def changes(self, task):
        try:
            current, _ = self.current(task)
            return distill_inputs.changed(json.loads(task['snapshot_json']), current)
        except (OSError, ValueError, APIError, subprocess.SubprocessError) as error:
            return '输入无法重新核对：' + problem_text(error)

    def schedule(self, task):
        if task['id'] in self.tasks.workers or not self.tasks.kernel.check_due(task, self.tasks.types['distill'].waiting):
            return
        try:
            before = self.tasks.types['distill'].inputs(task).snapshot
            try:
                snapshot, inputs = self.current(task)
            except (OSError, ValueError, APIError, subprocess.SubprocessError) as error:
                self.tasks.update(task['id'], status=S.VOID, void_reason='输入无法重新核对：' + problem_text(error), finished_at=now(), error='')
                return
            waiting = before['dirty'] or task['status'] == S.WAIT_VALIDATION
            # 尚未开始时可采用同一产品内容的新提交；通过依据只比较内容摘要。
            reason = distill_inputs.changed(before, snapshot, waiting_commit=True, waiting_validation=waiting)
            if reason:
                self.tasks.update(task['id'], status=S.VOID, void_reason=reason, finished_at=now(), error='')
                return
            if snapshot['dirty']:
                self.tasks.update(task['id'], status=S.WAIT_COMMIT, error=self.dirty_reason(snapshot))
                return
            if waiting or before['repos'] != snapshot['repos']:
                snapshot['queued_snapshot'] = before.get('queued_snapshot', before)
                task['snapshot_json'], task['input_json'] = canonical(snapshot), canonical(inputs)
                self.tasks.update(task['id'], snapshot_json=task['snapshot_json'], input_json=task['input_json'],
                                  input_digest=hashlib.sha256(task['snapshot_json'].encode()).hexdigest())
                pending = [sid + '：' + '；'.join(option['reasons']) for sid, option in inputs['eligibility'].items() if not option['eligible']]
                if pending:
                    self.tasks.update(task['id'], status=S.WAIT_VALIDATION, error='需要在新产品上通过的场景：' + '；'.join(pending))
                    return
                self.tasks.update(task['id'], status=S.QUEUED, error='')
            if not self.service.environment.approved():
                self.tasks.update(task['id'], error='等待工作区自检通过')
                return
            needs_browser = any(inputs['entries'][s['verify']]['surface'] == 'web' for s in inputs['scenes'])
            if not self.tasks.kernel.browser(task, needs_browser):
                return
            error = codex_available()
            if error:
                raise ValueError(error)
            self.tasks.kernel.start(task, self.execute, S.DISTILLING)
        except (OSError, ValueError, KeyError, APIError, sqlite3.Error, subprocess.SubprocessError) as error:
            self.tasks.update(task['id'], status=S.FAILED, error=str(error), finished_at=now())
            if self.tasks.browser_owner == task['id']:
                self.tasks.browser_owner = None

    def ensure_worktrees(self, task):
        snapshot = json.loads(task['snapshot_json'])
        branch = f"sop-board/distill/{task['id']}"
        directory = self.tasks.directory(task['id'])
        runtime = self.service.environment.worktrees
        existing = {r['alias']: r for r in self.repositories(task['id'])}
        if existing.keys() - snapshot['repos'].keys():
            raise ValueError('涉及的仓库减少，保留已有工作树，请放弃后重新发起')
        with self.store.transaction() as db:
            for alias, product in snapshot['repos'].items():
                identity = snapshot['repositories'][alias]
                if alias in existing:
                    row = existing[alias]
                    if row['root'] != identity['root'] or row['identity'] != identity['identity'] or row['branch'] != branch:
                        raise ValueError(alias + ' 的仓库身份变化，原工作树保留；请开发者核对后重新发起')
                else:
                    if git(identity['root'], 'branch', '--list', branch).strip():
                        raise ValueError(alias + ' 已有同名分支，不能接管')
                    self.tasks.kernel.repositories.insert('distill', dict(task=task['id'], alias=alias, root=identity['root'],
                        identity=identity['identity'], base_commit=product['commit'], branch=branch), connection=db)
        rows = self.repositories(task['id'])
        known = {r['alias']: Path(r['worktree']) for r in rows if r['worktree']}
        # 建立后、路径落库前退出：只从已持久登记的任务分支找回，绝不另建分支覆盖现场。
        for row in rows:
            if row['alias'] not in known:
                candidates = [p for p, b in runtime.registered(row['root']).items() if b == branch]
                if len(candidates) == 1:
                    known[row['alias']] = runtime.validate_path(row['alias'], candidates[0], branch, directory)
        # 先推进已有工作树，再让适配按新版本准备环境，不能复用旧提交生成的环境配置。
        for alias, path in known.items():
            previous = existing.get(alias)
            target = snapshot['repos'][alias]['commit']
            if previous and previous['base_commit'] != target:
                runtime.validate_path(alias, path, branch, directory)
                if not ancestor(path, previous['base_commit'], target) or head(path) not in (previous['base_commit'], target):
                    raise ValueError(alias + ' 基线无法安全快进到新快照，保留原工作树与改动，请开发者处理')
                if head(path) != target:
                    # 只推进到已有提交；Git 拒绝覆盖未提交文件时原地停止，不暂存、不造提交。
                    git(path, '-c', 'merge.autoStash=false', '-c', 'core.hooksPath=/dev/null',
                        'merge', '--ff-only', '--no-edit', target)
                with self.store.transaction() as db:
                    self.tasks.kernel.repositories.update('distill', task['id'], alias, base_commit=target, connection=db)
        try:
            paths, extra = runtime.build(directory, branch, sorted(snapshot['repos']),
                self.workspace.data / 'worktree-logs' / f"distill-{task['id']}.log", known,
                commits={a: p['commit'] for a, p in snapshot['repos'].items()})
        except WorktreeError as error:
            with self.store.transaction() as db:
                for alias, path in error.paths.items():
                    self.tasks.kernel.repositories.update('distill', task['id'], alias, worktree=str(path), connection=db)
            raise
        with self.store.transaction() as db:
            for alias, path in paths.items():
                self.tasks.kernel.repositories.update('distill', task['id'], alias, worktree=str(path), connection=db)
        for alias, path in paths.items():
            if head(path) != snapshot['repos'][alias]['commit']:
                raise ValueError(alias + ' 工作树提交与输入快照不一致，现场保留')
        return paths, extra

    def materialize(self, task, paths):
        directory = self.tasks.directory(task['id'])
        inputs, snapshot = json.loads(task['input_json']), json.loads(task['snapshot_json'])
        directory.mkdir(parents=True, exist_ok=True)
        for filename, value in [('input.json', inputs), ('snapshot.json', snapshot), ('repos.json', {a: str(p) for a, p in paths.items()}),
                                ('pass-evidence.json', snapshot['passes'])]:
            (directory / filename).write_text(canonical(value), encoding='utf-8')
        (directory / 'scenarios.yaml').write_text(inputs['raw'], encoding='utf-8')
        evidence_dir = directory / 'evidence'
        evidence_dir.mkdir(exist_ok=True)
        for passed in snapshot['passes'].values():
            for evidence in passed['evidence']:
                (evidence_dir / evidence['sha256']).write_bytes(Path(evidence['path']).read_bytes())
        prompt = Template(files('sop_board').joinpath('prompts/distill.md').read_text(encoding='utf-8')).substitute(
            task_directory=directory, skill_path=inputs['skill_path'], distill_skill=inputs['distill_skill'])
        (directory / 'prompt.md').write_text(prompt, encoding='utf-8')
        return prompt

    def manifest(self, task_id):
        result = {}
        for row in self.repositories(task_id):
            if not row['worktree'] or not Path(row['worktree']).is_dir():
                continue
            root = Path(row['worktree'])
            before = {r['path']: r for r in product_files(root, head=True, revision=row['base_commit'])}
            after = {r['path']: r for r in product_files(root)}
            result[row['alias']] = [after.get(path, dict(path=path, deleted=True)) for path in sorted(before.keys() | after.keys())
                                     if before.get(path) != after.get(path)]
        return result

    def consume(self, task, session):
        receipt = self.tasks.kernel.receipt(task, phase='distill')
        if receipt is not None:
            return receipt
        directory = self.tasks.directory(task['id'])
        path = directory / 'regression.json'
        if not path.is_file() or path.is_symlink():
            raise ValueError('会话没有写出普通文件 regression.json')
        report = json.loads(path.read_text(encoding='utf-8'), object_pairs_hook=judgment_object, parse_constant=invalid_judgment_constant)
        regressions.object_fields(report, 'report', ('schema_version', 'registered_by', 'session', 'scenarios'))
        scenes = regressions.array(report['scenarios'], 'scenarios', True)
        snapshot, inputs = json.loads(task['snapshot_json']), json.loads(task['input_json'])
        if any(not isinstance(s, dict) or not isinstance(s.get('scenario'), str) for s in scenes):
            raise ValueError('报告场景须为带场景编号的对象')
        if len(scenes) != len(snapshot['scenarios']) or {s['scenario'] for s in scenes} != set(snapshot['scenarios']):
            raise ValueError('报告须恰好覆盖本任务全部所选场景')
        roots = {r['alias']: r['worktree'] for r in self.repositories(task['id'])}
        for scene in scenes:
            sid = scene['scenario']
            passed = snapshot['passes'][sid]
            if (scene.get('batch') != task['batch'] or scene.get('scenario_file') != inputs['scenario_file']
                    or scene.get('fingerprint') != snapshot['scenarios'][sid]
                    or scene.get('confirmed_by') != snapshot['confirmations'][sid]['confirmed_by']
                    or scene.get('pass_evidence') != passed['report']
                    or scene.get('independent_acceptance') != passed['independent_acceptance']):
                raise ValueError(sid + ' 报告中的来源、确认或通过依据与固定输入不一致')
            for test in regressions.array(scene.get('tests'), sid + '.tests'):
                if (not isinstance(test, dict) or not isinstance(test.get('repo'), str)
                        or test.get('repo') not in roots or test.get('root') != roots[test['repo']] or test.get('commit') is not None):
                    raise ValueError(sid + ' 测试须来自本任务登记的工作树，commit 须为 null')
        report['session'] = session
        with self.tasks.lock, self.store.transaction() as db:
            if not self.active(task):
                return None
            reason = self.changes(task)
            if reason:
                db.execute(f"UPDATE tasks SET status='{S.VOID}',void_reason=?,finished_at=? WHERE id=?", (reason, now(), task['id']))
                return None
            result = regressions.submit(self.service, report, connection=db, task=dict(task, changes=self.manifest(task['id'])))
            self.tasks.kernel.register(task, result, connection=db, phase='distill')
            return result

    def active(self, task):
        with self.store.connection() as db:
            row = db.execute('SELECT status FROM tasks WHERE id=?', (task['id'],)).fetchone()
        return not self.tasks.stopping.is_set() and row['status'] == S.DISTILLING

    def execute(self, task):
        process = None
        error = reason = session = ''
        result = None
        try:
            with self.tasks.lock:
                self.tasks.starting = None
            if not self.active(task):
                return
            reason = self.changes(task)
            if reason:
                return
            paths, extra = self.ensure_worktrees(task)
            with self.store.connection() as db:
                needs_environment = db.execute('SELECT needs_environment FROM distill_tasks WHERE task=?', (task['id'],)).fetchone()[0]
            directories = dict(self.workspace.repos, **paths)
            environment = self.service.environment
            context = environment.use_repair(directories, extra) if needs_environment else nullcontext(environment.agent_environment(directories, extra))
            with context as variables:
                if not self.active(task):
                    return
                reason = self.changes(task)
                if reason:
                    return
                prompt = self.tasks.types['distill'].prepare(task, paths)
                starts = {alias: head(path) for alias, path in paths.items()}
                with self.tasks.lock:
                    if not self.active(task):
                        return
                    process = self.tasks.kernel.launch(task, self.tasks.directory(task['id']), prompt,
                        self.tasks.types['distill'].launch(task, paths=paths, environment=variables))
                    self.tasks.processes[task['id']] = process
                code = wait_codex(process, self.tasks.timeout)
                if not self.active(task):
                    return
                reason = self.changes(task)
                if reason:
                    return
                if code:
                    failure = codex_failure(self.tasks.directory(task['id']), code, include_stderr=True)
                    raise ValueError(failure)
                environment.worktrees.validate(paths, list(paths), f"sop-board/distill/{task['id']}", self.tasks.directory(task['id']))
                for alias, path in paths.items():
                    if head(path) != starts[alias]:
                        raise ValueError(alias + ' 会话改动了提交，保留现场供开发者核对，不登记')
                session = self.session(task)
                if not session:
                    raise ValueError('Codex 没有返回会话编号')
                changes = self.manifest(task['id'])
                (self.tasks.directory(task['id']) / 'changes.json').write_text(canonical(changes), encoding='utf-8')
                result = self.tasks.types['distill'].consume(task, session)
        except (OSError, ValueError, KeyError, TypeError, RecursionError, APIError, sqlite3.Error, subprocess.SubprocessError) as problem:
            error = problem_text(problem)
        finally:
            if process:
                terminate_group(process)
            with self.tasks.lock:
                self.tasks.processes.pop(task['id'], None)
                if self.tasks.starting == task['id']:
                    self.tasks.starting = None
                if self.tasks.browser_owner == task['id']:
                    self.tasks.browser_owner = None
                session = session or self.session(task)
                values = dict(session_id=session) if session else {}
                if self.active(task):
                    values.update(status=S.VOID if reason else S.FAILED if error else S.REVIEW,
                        error=error, void_reason=reason, finished_at=now(),
                        result_json=canonical(result or dict(ok=False, problems=[error] if error else [])))
                else:
                    with self.store.connection() as db:
                        current = db.execute('SELECT started_at,finished_at FROM tasks WHERE id=?', (task['id'],)).fetchone()
                    if current['started_at'] and not current['finished_at']:
                        values['finished_at'] = now()
                if values:
                    self.tasks.update(task['id'], **values)

    def session(self, task):
        return session_id(self.tasks.directory(task['id']), '')

    def details(self, task):
        with self.store.connection() as db:
            task['distill'] = dict(db.execute('SELECT * FROM distill_tasks WHERE task=?', (task['id'],)).fetchone())
        task['repositories'] = self.repositories(task['id'])
        task['changes'] = {}
        try:
            task['changes'] = self.manifest(task['id'])
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            task['worktree_error'] = str(error)

    def archive(self, task):
        return self.tasks.kernel.archive(task)

    def action(self, task, action, body=None):
        body = {} if body is None else body
        if not isinstance(body, dict):
            reject(422, 'distill-invalid', 'body', '操作正文须为对象')
        with self.tasks.lock:
            task = self.tasks.read(task['id'])
            if task['kind'] != 'distill':
                reject(409, 'task-kind-invalid', 'task', '不是沉淀任务')
            if task['status'] in TERMINAL:
                reject(409, 'task-state-invalid', 'status', '任务已结束')
            try:
                if action == 'retry':
                    if action not in self.tasks.types['distill'].actions(task):
                        raise ValueError('当前任务不能重试，或上次会话尚未退出')
                    snapshot, inputs = distill_inputs.freeze(self.service, task['workspace'], task['batch'], [s['scenario'] for s in task['scope']])
                    self.archive(task)
                    self.tasks.update(task['id'], status=S.WAIT_COMMIT if snapshot['dirty'] else S.QUEUED, attempt=task['attempt'] + 1,
                        input_json=canonical(inputs), snapshot_json=canonical(snapshot), input_digest=hashlib.sha256(canonical(snapshot).encode()).hexdigest(),
                        scenario_file_sha256=snapshot['scenario_file_sha256'], result_json='{}', result_digest=None,
                        started_at=None, finished_at=None, session_id=None, void_reason='', error=self.dirty_reason(snapshot) if snapshot['dirty'] else '')
                    self.tasks.kernel.wake(task['id'])
                elif action in ('cancel', 'abandon'):
                    if task['distill']['decision'] == S.HANDLED:
                        raise ValueError('已按提交核对，等待清理，不能改为放弃')
                    with self.store.transaction() as db:
                        db.execute(f"UPDATE distill_tasks SET decision='{S.ABANDONED}',decision_at=? WHERE task=?", (now(), task['id']))
                        db.execute(f"UPDATE regression_links SET status='{S.ABANDONED}' WHERE json_extract(details_json,'$.task')=?", (task['id'],))
                    if task['status'] == S.DISTILLING:
                        self.tasks.update(task['id'], status=S.INTERRUPTED, error='已停止沉淀，等待环境恢复后确认清理', finished_at=now())
                        process = self.tasks.processes.get(task['id'])
                        if process:
                            terminate_group(process)
                    elif not task['started_at'] and not self.repositories(task['id']) and task['id'] not in self.tasks.workers:
                        self.tasks.update(task['id'], status=S.ABANDONED, error='', finished_at=now())
                    else:
                        self.tasks.update(task['id'], status=S.REVIEW, error='已记录放弃，等待确认清理')
                elif action == 'handled':
                    self.handled(task, body)
                elif action == 'cleanup':
                    return self.cleanup(task, body)
                else:
                    raise ValueError('不支持的沉淀操作')
            except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
                reject(409, 'distill-action-invalid', action, str(error))
        return dict(ok=True, task=self.tasks.read(task['id']))

    def handled(self, task, body):
        if task['status'] != S.REVIEW or task['distill']['decision']:
            raise ValueError('只有尚未处理的待审核任务可以标记已处理')
        if task['id'] in self.tasks.workers:
            raise ValueError('会话尚未退出或环境尚未恢复')
        commits = body.get('commits')
        rows = self.repositories(task['id'])
        if not isinstance(commits, dict) or set(commits) != {r['alias'] for r in rows}:
            raise ValueError('请为每个工作树仓库填写完整提交号')
        with self.store.connection() as db:
            receipt = db.execute('SELECT result_json FROM distill_registrations WHERE task=? AND attempt=?', (task['id'], task['attempt'])).fetchone()
        if not receipt:
            raise ValueError('本次尝试尚未登记，不能标记已处理')
        registered = json.loads(receipt['result_json'])
        changes = registered['changes']
        for row in rows:
            if row['root'] != str(self.workspace.repos.get(row['alias'], '')) or row['identity'] != self.workspace.identities.get(row['alias']):
                raise ValueError(row['alias'] + ' 当前仓库身份或路径变化，不能更新关联')
            commit = commits[row['alias']]
            if not isinstance(commit, str) or not re.fullmatch('[a-f0-9]{40}|[a-f0-9]{64}', commit):
                raise ValueError(row['alias'] + ' 须填写完整提交号')
            if not ancestor(row['root'], row['base_commit'], commit):
                raise ValueError(row['alias'] + ' 提交不包含本任务的基线')
            manifest = {r['path']: r for r in product_files(row['root'], head=True, revision=commit)}
            for change in changes.get(row['alias'], []):
                actual = manifest.get(change['path'])
                if (change.get('deleted') and actual is not None) or (not change.get('deleted') and actual != change):
                    raise ValueError(row['alias'] + '/' + change['path'] + ' 与审核产出的摘要不一致')
        with self.store.transaction() as db:
            links = db.execute("SELECT * FROM regression_links WHERE json_extract(details_json,'$.task')=?", (task['id'],)).fetchall()
            if {r['id'] for r in links} != {r['id'] for r in registered['links']}:
                raise ValueError('关联已被其他会话重新登记，请先核对关联归属')
            for link in links:
                commit = commits[link['repo']]
                raw = regressions.commit_file(self.workspace.repos[link['repo']], commit, link['path'])
                if hashlib.sha256(raw).hexdigest() != link['sha256']:
                    raise ValueError(link['path'] + ' 与登记的测试文件摘要不一致')
            for link in links:
                db.execute("UPDATE regression_links SET status='已提交',commit_id=? WHERE id=?", (commits[link['repo']], link['id']))
            db.execute(f"UPDATE distill_tasks SET decision='{S.HANDLED}',commits_json=?,decision_at=? WHERE task=?", (canonical(commits), now(), task['id']))
            db.execute("UPDATE tasks SET error='提交已核对，等待确认清理' WHERE id=?", (task['id'],))

    def cleanup_preview(self, task):
        if not task['distill']['decision'] or task['id'] in self.tasks.workers or task['status'] == S.DISTILLING:
            raise ValueError('请先标记已处理或放弃，并等待会话退出、环境恢复')
        manifests, changes = {}, {}
        runtime = self.service.environment.worktrees
        for row in self.repositories(task['id']):
            if row['root'] != str(self.workspace.repos.get(row['alias'], '')) or row['identity'] != self.workspace.identities.get(row['alias']):
                raise ValueError(row['alias'] + ' 仓库身份变化，不能清理')
            if not row['worktree']:
                candidates = [p for p, branch in runtime.registered(row['root']).items() if branch == row['branch']]
                if candidates:
                    raise ValueError('任务工作树路径未登记，请先重试找回现场再清理')
                continue
            path = Path(row['worktree'])
            if path.exists():
                runtime.validate_path(row['alias'], path, row['branch'], self.tasks.directory(task['id']))
                changes[row['alias']] = git(path, 'status', '--porcelain', '--untracked-files=all').decode('utf-8', errors='replace').splitlines()
                manifests[row['alias']] = dict(head=head(path), files=product_files(path), changes=changes[row['alias']])
            else:
                manifests[row['alias']] = dict(missing=True)
        return dict(changes=changes, confirmation=hashlib.sha256(canonical(dict(decision=task['distill']['decision'], manifests=manifests)).encode()).hexdigest())

    def cleanup(self, task, body):
        preview = self.cleanup_preview(task)
        if 'confirmation' not in body:
            return dict(ok=True, task=self.tasks.read(task['id']), cleanup=preview)
        if body.get('confirmation') != preview['confirmation']:
            raise ValueError('清理确认后工作树发生变化，请重新查看改动清单再确认')
        rows = self.repositories(task['id'])
        paths = {r['alias']: Path(r['worktree']) for r in rows if r['worktree']}
        try:
            if paths:
                self.service.environment.worktrees.cleanup(self.tasks.directory(task['id']), f"sop-board/distill/{task['id']}", paths,
                    self.workspace.data / 'worktree-logs' / f"distill-{task['id']}-cleanup.log")
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            self.tasks.update(task['id'], status=S.CLEANUP_FAILED, error=str(error))
            return dict(ok=True, task=self.tasks.read(task['id']))
        # 不强删分支：开发者提交可能尚未合入；只清理已经确认的工作树。
        self.tasks.update(task['id'], status=task['distill']['decision'], error='')
        return dict(ok=True, task=self.tasks.read(task['id']))
