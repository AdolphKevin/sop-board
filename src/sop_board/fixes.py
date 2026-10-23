"""每工作区一个跨仓修复任务；尝试、独立复测与整组合入独立记录。"""

import json
import time
from importlib.resources import files
from string import Template
import sqlite3
import subprocess
import os

import yaml

from .kernel import (ATTEMPT_RESUMABLE, failures_exhausted, FIX_ACTIVE, FIX_LIVE, FIX_NO_CANCEL, FIX_RECOVERY, FIX_RETRY,
                     S, codex_available, codex_failure, session_id, sql_states,
                     terminate_group, wait_codex, queue_view)

from .errors import reject, APIError
from .jira import identify
from .service import canonical, now
from .repositories import git, MissingGitIdentity
from .repair_repositories import RepairRepositories, head
from .drafts import schema_problems




class Fixes:
    def __init__(self, tasks):
        self.tasks, self.service, self.store = tasks, tasks.service, tasks.store
        self.next_scan = 0
        self.repositories = RepairRepositories(tasks)

    def directories(self, task):
        return self.repositories.directories(task['id'])

    def blocks_execution(self, key):
        with self.store.connection() as db:
            return bool(db.execute("SELECT 1 FROM tasks WHERE workspace=? AND kind='fix' "
                f'AND status IN ({sql_states(FIX_ACTIVE)})', (key,)).fetchone())

    def schedule(self, task):
        error = codex_available()
        if error:
            self.tasks.update(task['id'], status=S.UNAVAILABLE, error=error)
            return
        try:
            if not self.tasks.types['fix'].prepare(task):
                return
        except (OSError, ValueError, KeyError, sqlite3.Error, subprocess.SubprocessError, APIError) as error:
            with self.store.transaction() as db:
                db.execute("UPDATE fix_tasks SET retry_reason='preparation' WHERE task=? AND retry_reason!='git-identity'", (task['id'],))
            self.tasks.update(task['id'], status=S.DEVELOPER, error=f'无法准备修复：{error}')
            return
        if self.verification_attempt(task):
            self.tasks.kernel.start(task, self.verify_again, S.VERIFYING, error='修复已完成，重新进行独立复验')
        else:
            self.tasks.kernel.start(task, self.execute, S.ANALYZING if task['attempt'] == 0 else S.FIXING,
                                    error='修复会话运行中')

    def verification_attempt(self, task):
        """待验证是已完成修复的阶段收据，不能按 attempt+1 再启动修复。"""
        with self.store.connection() as db:
            row = db.execute('SELECT * FROM fix_attempts WHERE task=? ORDER BY number DESC LIMIT 1', (task['id'],)).fetchone()
        if row and row['outcome'] == S.WAIT_VALIDATION and task['attempt'] in (row['number'] - 1, row['number']):
            return dict(row)
        return None

    def prepare_verification(self, task, attempt):
        info = self.tasks.read(task['id'])
        stale = self.snapshot_changes(info)
        if stale:
            raise ValueError(stale)
        snapshot = info['snapshot']
        self.tasks.worktree_environments[task['id']] = self.repositories.ensure(task['id'], snapshot['aliases'])
        commits = json.loads(attempt['commits_json'])
        starts = json.loads(attempt['start_commits_json'])
        for repo in self.repositories.rows(task['id']):
            expected = repo['repair_commit'] or commits.get(repo['alias'], starts[repo['alias']])
            if head(repo['worktree']) != expected:
                raise ValueError(repo['alias'] + ' 的修复提交已变化，不能续接原复验')
        if self.service.workspace.product(snapshot['aliases'], self.directories(task))['dirty']:
            raise ValueError('复验目录有未提交的产品改动，保留原尝试与提交')
        protected = self.repositories.protected_files(task['id'])
        for alias, before in snapshot.get('protected_files', {}).items():
            if protected.get(alias) != before:
                raise ValueError(alias + ' 的 SOP 或场景文件变化，不能续接原复验')
        products = [p for p in json.loads(attempt['result_json']).get('problems', []) if p['category'] == 'product']
        if not products:
            raise ValueError('原修复尝试没有待复验的产品问题')
        # 兼容提交后、任务次数更新前中断的窄窗口；仍然使用已登记的同一尝试。
        task['attempt'] = attempt['number']
        self.tasks.update(task['id'], attempt=attempt['number'], result_json=attempt['result_json'])
        return True

    def verify_again(self, task):
        try:
            with self.tasks.lock:
                self.tasks.starting = None
                if self.tasks.stopping.is_set() or self.tasks.read(task['id'])['status'] != S.VERIFYING:
                    return
                attempt = self.verification_attempt(task)
                if attempt is None:
                    raise ValueError('原修复尝试已不在待验证阶段')
                products = [p for p in json.loads(attempt['result_json'])['problems'] if p['category'] == 'product']
            # verify 的调用约定使用修复开始前的次数，持久任务计数保持 number。
            self.verify(dict(task, attempt=attempt['number'] - 1), products)
        except (OSError, ValueError, KeyError, TypeError, sqlite3.Error, subprocess.SubprocessError) as error:
            with self.tasks.lock:
                if not self.tasks.stopping.is_set():
                    with self.store.transaction() as db:
                        db.execute("UPDATE fix_tasks SET retry_reason='preparation' WHERE task=?", (task['id'],))
                    self.tasks.update(task['id'], status=S.DEVELOPER, error='无法恢复复验：' + str(error))
        finally:
            with self.tasks.lock:
                if self.tasks.starting == task['id']:
                    self.tasks.starting = None

    def prepare(self, task):
        declarations = self.service.require_ready(task['workspace'])
        external = bool(self.tasks.kernel.claim(task))
        attempt = None if external else self.verification_attempt(task)
        if attempt:
            return self.prepare_verification(task, attempt)
        workspace = self.service.workspace
        with self.store.connection() as db:
            pending = [json.loads(r[0]) for r in db.execute(
                'SELECT input_json FROM fix_problems WHERE task=? ORDER BY id', (task['id'],))]
            aliases = [r['alias'] for r in self.repositories.rows(task['id'])]
            for problem in pending:
                batch = self.service.batch(db, task['workspace'], problem['batch'])
                document = yaml.safe_load(self.service.latest_version(db, batch)['content'])
                for alias in workspace.aliases(document):
                    if alias not in aliases:
                        aliases.append(alias)
        if not aliases:
            self.tasks.update(task['id'], status=S.DEVELOPER, error='没有需要修复的仓库')
            return False
        product = workspace.product(aliases)
        if product['dirty'] and not external:
            self.tasks.update(task['id'], status=S.WAIT_COMMIT, error='工作区有未提交的产品改动：' + '、'.join(product['changed']))
            return False
        if external:
            self.repositories.use_workspace(task['id'], aliases)
        else:
            self.tasks.worktree_environments[task['id']] = self.repositories.ensure(task['id'], aliases)
        number = task['attempt'] + 1
        protected_files = self.repositories.protected_files(task['id'], workspace=external)
        previous = json.loads(task['snapshot_json'])
        for alias, manifest in previous.get('protected_files', {}).items():
            protected_files[alias] = manifest
        with self.store.transaction() as db:
            pending_attempt = db.execute(f"SELECT * FROM fix_attempts WHERE task=? AND number=? AND outcome IN ({sql_states(ATTEMPT_RESUMABLE)})",
                (task['id'], number)).fetchone()
            if pending_attempt and task['input_json'] != '{}':
                db.execute(f"UPDATE fix_attempts SET outcome='{S.RUNNING}',finished_at=NULL WHERE task=? AND number=?", (task['id'], number))
                reason = db.execute('SELECT retry_reason FROM fix_tasks WHERE task=?', (task['id'],)).fetchone()[0]
                if reason == 'git-identity':
                    stale = self.snapshot_changes(self.tasks.read(task['id']))
                    if stale:
                        raise ValueError(stale)
                    return True
            db.execute("UPDATE fix_tasks SET retry_reason='' WHERE task=?", (task['id'],))
        starts = json.loads(pending_attempt['start_commits_json']) if pending_attempt else self.repositories.heads(task['id'])
        repositories = {r['alias']: dict(identity=r['identity'], directory=r['worktree'], baseline_directory=r['root'],
            baseline_branch=r['baseline_branch'], baseline_commit=r['baseline_commit'], commit=starts[r['alias']])
            for r in self.repositories.rows(task['id'])}
        snapshot = dict(entries=declarations, config_digest=workspace.config_digest, batches={}, cases={},
                        aliases=aliases, repositories=repositories, protected_files=protected_files)
        if external:
            from .repositories import product_files
            snapshot.update(session_entry=True, session_files=previous.get('session_files') or
                {a: product_files(workspace.repos[a]) for a in aliases})
        with self.store.transaction() as db:
            fresh = [dict(r) for r in db.execute('SELECT * FROM fix_problems WHERE task=? ORDER BY id', (task['id'],))]
            added = []
            for previous in fresh:
                if previous['first_attempt']:
                    continue
                problem = json.loads(previous['input_json'])
                problem.update(jira=None, jira_error='')
                jira = getattr(self.service, 'jira', None)
                issue = identify(problem['defect_url'])
                if issue:
                    problem['jira_site'], problem['jira_key'] = issue
                if not issue:
                    problem['jira_error'] = '未识别为 Jira Cloud 单据链接，未读取、不会回写'
                elif not jira:
                    problem['jira_error'] = 'Jira 未配置，未能读取'
                else:
                    try:
                        problem['jira'] = jira.issue(problem['defect_url'])
                    except APIError as error:
                        problem['jira_error'] = '；'.join(p['problem'] for p in error.problems)
                db.execute('UPDATE fix_problems SET first_attempt=?,input_json=? WHERE id=?',
                           (number, canonical(problem), previous['id']))
                added.append(dict(batch=problem['batch'], scenario=problem['scenario']))
            problems = [json.loads(r[0]) for r in db.execute("SELECT input_json FROM fix_problems WHERE task=? AND category IN ('','product') ORDER BY id", (task['id'],))]
            for batch in db.execute('SELECT * FROM batches WHERE workspace=? ORDER BY id', (task['workspace'],)):
                version = self.service.latest_version(db, batch)
                if version:
                    snapshot['batches'][batch['name']] = dict(content=version['content'], sha256=version['sha256'], path=batch['path'])
                    snapshot['cases'][batch['name']] = self.service.case_union(db, batch)

            db.execute('INSERT OR IGNORE INTO fix_attempts(task,number,started_at,start_commits_json,added_json) VALUES (?,?,?,?,?)',
                (task['id'], number, now(), canonical(starts), canonical(added)))
        directory = self.tasks.directory(task['id'])
        task['input_json'] = canonical(dict(problems=problems, added=added))
        task['snapshot_json'] = canonical(snapshot)
        self.tasks.update(task['id'], input_json=task['input_json'], snapshot_json=task['snapshot_json'])
        for name, value in [('problems', problems), ('entries', declarations), ('snapshot', snapshot), ('cases', snapshot['cases'])]:
            (directory / (name + '.json')).write_text(canonical(value), encoding='utf-8')
        (directory / 'schema.json').write_text(files('sop_board').joinpath('prompts/fix.schema.json').read_text(encoding='utf-8'), encoding='utf-8')
        return True

    def session(self, task, directory):
        session = session_id(directory)
        if session:
            self.tasks.update(task['id'], session_id=session)
        return session or self.tasks.read(task['id'])['session_id']

    def materialize(self, task):
        directory = self.tasks.directory(task['id'])
        info = self.tasks.read(task['id'])
        external = bool(self.tasks.kernel.claim(task))
        prompt = Template(files('sop_board').joinpath('prompts/fix.md').read_text(encoding='utf-8')).substitute(
            workspace_instruction=('你在开发者当前工作区的各仓库根目录中修复产品，不创建或切换工作树。' if external else
                '你在同一任务目录的多个隔离 worktree 中修复产品。'),
            change_instruction=('只在上面列出的当前仓库根目录内修改产品代码，保留已有未提交改动；不创建工作树、不提交、不推送。' if external else
                '只在上面逐仓列出的工作树路径内改产品代码，不修改基线目录或其它仓库。改动留在 worktree，不提交，不推送。'),
            environment_origin='本会话' if external else '启动看板',
            problems_file=directory / 'problems.json', entries_file=directory / 'entries.json', snapshot_file=directory / 'snapshot.json',
            cases_file=directory / 'cases.json', feedback_file=directory / 'feedback.json', developer_note=info['developer_note'],
            repositories='\n'.join(r['alias'] + '：' + r['worktree'] + '；身份 ' + r['identity'] for r in info['repositories']))
        (directory / 'prompt.md').write_text(prompt, encoding='utf-8')
        (directory / 'feedback.json').write_text(canonical(dict(added=json.loads(task['input_json'])['added'],
            previous=info['attempts'][-2] if len(info['attempts']) > 1 else None)), encoding='utf-8')
        return prompt

    def execute(self, task):
        directory = self.tasks.directory(task['id'])
        info = self.tasks.read(task['id'])
        number = task['attempt'] + 1
        prompt = self.tasks.types['fix'].prepare(task, materialize=True)
        try:
            if info['retry_reason'] == 'git-identity':
                with self.tasks.lock:
                    self.tasks.starting = None
                    if not self.live(task):
                        return
                    products = self.tasks.types['fix'].consume(task, info['attempts'][-1]['result'])
                if products:
                    self.verify(task, products)
                return
            for retry in range(2):
                interruption = self.run_session(task, directory, prompt)
                with self.tasks.lock:
                    if not self.live(task):
                        return
                    if not interruption:
                        break
                    self.record_interruption(task, interruption, retry)
                    if retry == 1:
                        with self.store.transaction() as db:
                            db.execute("UPDATE fix_tasks SET retry_reason='interrupted' WHERE task=?", (task['id'],))
                            db.execute(f"UPDATE fix_attempts SET outcome='{S.INTERRUPTED}',reason=?,finished_at=? WHERE task=? AND number=?",
                                (interruption, now(), task['id'], number))
                        self.tasks.update(task['id'], status=S.DEVELOPER, error='修复会话中断，重试后仍中断：' + interruption)
                        return
            with self.tasks.lock:
                if not self.live(task):
                    return
                try:
                    products = self.tasks.types['fix'].consume(task)
                except (ValueError, TypeError, KeyError, RecursionError) as error:
                    self.failed(task, '输出不符合模式：' + str(error))
                    products = None
            if products:
                self.verify(task, products)
        except (OSError, ValueError, KeyError, TypeError, sqlite3.Error, subprocess.SubprocessError) as error:
            with self.tasks.lock:
                if not self.tasks.stopping.is_set():
                    self.tasks.update(task['id'], status=S.DEVELOPER, error=str(error))
        finally:
            process = self.tasks.processes.get(task['id'])
            if process:
                terminate_group(process)
            with self.tasks.lock:
                self.tasks.processes.pop(task['id'], None)
                if self.tasks.starting == task['id']:
                    self.tasks.starting = None

    def run_session(self, task, directory, prompt):
        process = None
        try:
            (directory / 'final.json').unlink(missing_ok=True)
            with self.tasks.lock:
                if not self.live(task):
                    return ''
                session = self.tasks.read(task['id'])['session_id']
                if task['id'] not in self.tasks.worktree_environments:
                    self.tasks.worktree_environments[task['id']] = self.repositories.ensure(task['id'], [r['alias'] for r in self.repositories.rows(task['id'])])
                environment = self.service.environment.agent_environment(self.repositories.repair_directories(task['id']), self.tasks.worktree_environments[task['id']])
                process = self.tasks.kernel.launch(task, directory, prompt,
                    self.tasks.types['fix'].launch(task, environment=environment, session=session))
                self.tasks.processes[task['id']] = process
                self.tasks.starting = None
            try:
                wait_codex(process, self.tasks.timeout, on_activity=lambda: self.session(task, directory))
            except ValueError as error:
                return str(error)
            session = self.session(task, directory)
            if process.returncode:
                return codex_failure(directory, process.returncode)
            path = directory / 'final.json'
            if not path.is_file() or path.is_symlink() or not path.read_text(encoding='utf-8').strip():
                return 'Codex 没有写出最终输出'
            if not session:
                return 'Codex 没有返回会话编号'
            return ''
        except (OSError, subprocess.SubprocessError) as error:
            return f'Codex 无法启动或读取输出：{type(error).__name__}'
        finally:
            if process:
                terminate_group(process)
            with self.tasks.lock:
                self.tasks.processes.pop(task['id'], None)

    def record_interruption(self, task, reason, retry):
        target = self.tasks.kernel.archive(task, move=False,
            checkpoint=f'interruption-{time.time_ns()}-{retry}', names=('events.jsonl', 'stderr.txt', 'final.json'))
        with self.store.transaction() as db:
            row = db.execute('SELECT interruptions_json FROM fix_attempts WHERE task=? AND number=?', (task['id'], task['attempt'] + 1)).fetchone()
            records = json.loads(row[0])
            records.append(dict(reason=reason, recorded_at=now(), directory=str(target)))
            db.execute('UPDATE fix_attempts SET interruptions_json=? WHERE task=? AND number=?',
                (canonical(records), task['id'], task['attempt'] + 1))

    def finish(self, task, answer):
        info = self.tasks.read(task['id'])
        number = task['attempt'] + 1
        selected = json.loads(task['input_json'])['problems']
        self.check_answer(task, answer)
        changes = self.repositories.record_changes(task['id'])
        def forbidden_files():
            return self.protected_changes(info)
        protected = forbidden_files()
        if protected:
            self.failed(task, '修复改动涉及禁止修改的 SOP 或场景文件：' + '、'.join(protected), answer)
            return
        product = [p for p in answer['problems'] if p['category'] == 'product']
        with self.store.transaction() as db:
            self.record_problems(db, task, answer)
        if not product:
            with self.store.transaction() as db:
                db.execute(f"UPDATE fix_attempts SET outcome='{S.DEVELOPER}',finished_at=?,reason=?,result_json=? WHERE task=? AND number=?",
                    (now(), '全部问题需要开发者处理；不产生产品提交', canonical(answer), task['id'], number))
            self.tasks.update(task['id'], attempt=number, status=S.DEVELOPER, error='全部问题需要开发者处理；保留工作树，不提交代码', result_json=canonical(answer))
            self.archive(task)
            return
        if not any(changes.values()):
            self.failed(task, '修复会话没有产品改动', answer)
            return
        lines = []
        for problem in selected:
            if (problem['batch'], problem['scenario']) not in {(r['batch'], r['scenario']) for r in product}:
                continue
            label = (problem.get('jira_key') or problem['defect_url']) if problem['source'] == 'manual' else 'Agent 失败'
            lines.append(f"{problem['batch']} {problem['scenario']} {label}")
        message = f"sop-board 修复 #{task['id']} 第 {number} 次尝试\n\n" + '\n'.join(lines)
        attempt = info['attempts'][-1]
        for repo in info['repositories']:
            expected_head = attempt['commits'].get(repo['alias'], attempt['start_commits'][repo['alias']])
            if head(repo['worktree']) != expected_head:
                self.failed(task, repo['alias'] + ' 的提交被修复会话或外部进程改变；保留现场，禁止自动改写', answer)
                return
        try:
            self.repositories.commit(task['id'], number, message)
        except MissingGitIdentity as error:
            with self.store.transaction() as db:
                db.execute("UPDATE fix_tasks SET retry_reason='git-identity' WHERE task=?", (task['id'],))
                db.execute(f"UPDATE fix_attempts SET outcome='{S.WAIT_COMMIT}',reason=?,result_json=? WHERE task=? AND number=?",
                    (str(error), canonical(answer), task['id'], number))
            self.tasks.update(task['id'], status=S.DEVELOPER, error=str(error), result_json=canonical(answer))
            self.archive(task)
            return
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            self.failed(task, str(error), answer)
            return
        protected = forbidden_files()
        if protected:
            self.failed(task, '提交期间 SOP 或场景文件被改动：' + '、'.join(protected), answer)
            return
        with self.store.transaction() as db:
            db.execute("UPDATE fix_tasks SET retry_reason='' WHERE task=?", (task['id'],))
            pending = dict(status='pending', reason='等待验证', evidence=[], scenarios=[dict(batch=p['batch'], scenario=p['scenario']) for p in product])
            db.execute(f"UPDATE fix_attempts SET outcome='{S.WAIT_VALIDATION}',reason='',result_json=?,validation_json=? WHERE task=? AND number=?",
                (canonical(answer), canonical(pending), task['id'], number))
        self.tasks.update(task['id'], attempt=number, status=S.VERIFYING, error='使用各仓修复工作树的适配环境复测', result_json=canonical(answer))
        return product

    def check_answer(self, task, answer):
        info = self.tasks.read(task['id'])
        selected = json.loads(task['input_json'])['problems']
        schema = json.loads(files('sop_board').joinpath('prompts/fix.schema.json').read_text(encoding='utf-8'))
        issues = schema_problems(answer, schema)
        if issues:
            raise ValueError('输出不符合模式：' + '；'.join(issues))
        expected = {(p['batch'], p['scenario']) for p in selected}
        returned = [(p['batch'], p['scenario']) for p in answer['problems']]
        if len(returned) != len(set(returned)) or set(returned) != expected:
            raise ValueError('问题重复、遗漏或不在本次范围内')
        aliases = {r['alias'] for r in info['repositories']}
        for problem in answer['problems']:
            root_repo = problem['root_repo']
            if root_repo is not None and root_repo not in aliases or problem['category'] == 'product' and root_repo is None:
                raise ValueError(problem['scenario'] + ' 产品根因必须指向本任务仓库；其他根因未知时填 null')

    def protected_changes(self, info):
        # 会话入口允许预先存在的未提交 SOP，只拒绝准备之后的变化。
        external = bool(self.tasks.kernel.claim(info))
        protected = [] if external else self.repositories.protected(info['id'], self.repositories.changes(info['id']))
        current = self.repositories.protected_files(info['id'], workspace=external)
        for alias, before in info['snapshot']['protected_files'].items():
            after = current[alias]
            protected.extend(alias + '/' + name for name in before.keys() | after.keys() if before.get(name) != after.get(name))
        return sorted(set(protected))

    @staticmethod
    def record_problems(db, task, answer):
        for problem in answer['problems']:
            db.execute('UPDATE fix_problems SET category=?,root_repo=?,handling=?,reason=?,plan=? WHERE task=? AND batch=? AND scenario=?',
                (problem['category'], problem['root_repo'], problem['handling'], problem['reason'], answer['plan'], task['id'], problem['batch'], problem['scenario']))

    def verify(self, task, problems, additional=False):
        info = self.tasks.read(task['id'])
        validation = self.validate(info, problems)
        if validation is None:
            return
        with self.tasks.lock:
            if self.tasks.stopping.is_set() or self.tasks.read(task['id'])['status'] != S.VERIFYING:
                return
            number = task['attempt'] + 1
            with self.store.transaction() as db:
                db.execute('UPDATE fix_attempts SET validation_json=? WHERE task=? AND number=?', (canonical(validation), task['id'], number))
            if validation['status'] == 'void':
                with self.store.transaction() as db:
                    db.execute(f"UPDATE fix_attempts SET outcome='{S.VOID}',reason=?,finished_at=? WHERE task=? AND number=?",
                        (validation['reason'], now(), task['id'], number))
                    db.execute("UPDATE fix_tasks SET retry_reason='preparation' WHERE task=?", (task['id'],))
                self.tasks.update(task['id'], status=S.DEVELOPER, error=validation['reason'], void_reason=validation['reason'])
            elif validation['status'] == 'fail':
                if additional:
                    validation['reason'] = '合入基线后的追加验证未通过：' + validation.get('reason', '验证未通过')
                answer = self.tasks.read(task['id'])['result']
                self.failed(task, validation.get('reason', '验证未通过'), answer, validation)
            elif validation['status'] == 'pass':
                with self.store.transaction() as db:
                    db.execute("UPDATE fix_attempts SET outcome='通过',finished_at=? WHERE task=? AND number=?", (now(), task['id'], number))
                self.tasks.update(task['id'], status=S.PROMOTING, error='验证通过，核对基线与工作区后快进合入')
            self.archive(task)
        if validation['status'] == 'pass':
            self.merge(task, problems)

    def snapshot_changes(self, task):
        snapshot = task['snapshot']
        workspace = self.service.workspace
        workspace.reload()
        frozen = self.tasks.types['fix'].inputs(task)
        if not self.service.environment.approved() or frozen.changed(dict(frozen.snapshot, config_digest=workspace.config_digest)):
            return '工作区配置或自检状态变化'
        with self.store.connection() as db:
            current, cases = {}, {}
            for batch in db.execute('SELECT * FROM batches WHERE workspace=?', (task['workspace'],)):
                version = self.service.latest_version(db, batch)
                if version:
                    current[batch['name']] = version['sha256']
                    cases[batch['name']] = self.service.case_union(db, batch)
        changed = self.tasks.types['fix'].inputs(task).changed(dict(
            config_digest=workspace.config_digest, cases=cases, batches=current))
        if changed:
            return '、'.join(changed) + '变化'
        return ''

    def defer_merge(self, task, plans, reason=''):
        self.repositories.record_blocked(task['id'], plans)
        message = reason or '；'.join(p['alias'] + '：' + '；'.join(p['reasons']) for p in plans if p['reasons'])
        self.tasks.update(task['id'], status=S.DEVELOPER_MERGE, error=message)

    def merge(self, task, problems):
        environment = self.service.environment
        revalidate = False
        with environment.operation, self.tasks.lock:
            info = self.tasks.read(task['id'])
            if info['status'] != S.PROMOTING or self.tasks.stopping.is_set():
                return
            validation = info['attempts'][-1]['validation']
            records = [r for r in info['validations'] if r['attempt'] == info['attempt']]
            last = max((r['validation'] for r in records), default=0)
            records = [r for r in records if r['validation'] == last]
            if validation.get('status') != 'pass' or not records or any(r['status'] != 'pass' for r in records):
                self.tasks.update(task['id'], status=S.DEVELOPER, error='缺少本次独立复测通过记录，不能合入')
                return
            validated = validation['repositories']
            plans = self.repositories.preflight(task['id'], validated)
            stale = self.snapshot_changes(info)
            # 同批次其它仓库虽不需修复，也不得在复测后偷偷改变产品内容。
            dependencies = set(validated) - {p['alias'] for p in plans}
            for alias in dependencies:
                current = self.service.workspace.product([alias])
                if current['repos'][alias] != validated[alias] or current['dirty']:
                    stale = alias + ' 的基线在复测后变化'
            if stale:
                for plan in plans:
                    plan['reasons'].append(stale)
            if any(p['reasons'] for p in plans):
                self.defer_merge(info, plans)
                return
            if any(p['advanced'] for p in plans):
                if not self.repositories.integrate_baselines(task['id'], info['attempt'], plans):
                    self.defer_merge(info, plans)
                    return
                with self.store.transaction() as db:
                    db.execute(f"UPDATE fix_attempts SET outcome='{S.WAIT_VALIDATION}',finished_at=NULL WHERE task=? AND number=?", (task['id'], info['attempt']))
                self.tasks.update(task['id'], status=S.VERIFYING, error='基线已前进，各仓工作树更新后重新复测')
                revalidate = True
            else:
                # 在第一仓落地前再次完成整组核对，不边核对边合入。
                plans = self.repositories.preflight(task['id'], validated)
                if any(p['reasons'] or p['advanced'] for p in plans):
                    for plan in plans:
                        if plan['advanced']:
                            plan['reasons'].append('整组核对期间基线前进，须重新复测')
                    self.defer_merge(info, plans)
                    return
                try:
                    environment._stop()
                    self.repositories.fast_forward(task['id'], info['attempt'], plans)
                    environment.ensure_baseline()
                    self.promote(self.tasks.read(task['id']), '各仓复测通过，已整组快进并重启基线环境')
                except (OSError, ValueError, subprocess.SubprocessError) as error:
                    for repo in self.repositories.rows(task['id']):
                        if repo['merge_status'] not in (S.MERGED, '无改动', '合入失败', '合入中止'):
                            self.repositories.update(task['id'], repo['alias'], merge_status='合入中止', merge_reason='整组操作中止：' + str(error))
                    self.tasks.update(task['id'], status=S.DEVELOPER_MERGE, error=str(error))
                    return
        if revalidate:
            self.verify(task, problems, additional=True)

    def promote(self, task, reason):
        workspace = self.service.workspace
        stale = self.snapshot_changes(task)
        if stale:
            raise ValueError('合入后不能提升旧复测结果：' + stale)
        with self.store.transaction() as db:
            records = list(db.execute('SELECT * FROM fix_validations WHERE task=? AND attempt=? AND validation='
                '(SELECT MAX(validation) FROM fix_validations WHERE task=? AND attempt=?) ORDER BY batch',
                (task['id'], task['attempt'], task['id'], task['attempt'])))
            if not records or any(r['status'] != 'pass' for r in records):
                raise ValueError('没有可提升的通过的修复复测记录')
            rounds = []
            for record in records:
                batch = self.service.batch(db, task['workspace'], record['batch'])
                version = db.execute('SELECT * FROM versions WHERE id=?', (record['version'],)).fetchone()
                aliases = workspace.aliases(yaml.safe_load(version['content']))
                product = workspace.product(aliases)
                validated = json.loads(record['repositories_json'])
                if product['dirty'] or product['repos'] != {a: validated[a] for a in aliases}:
                    raise ValueError(record['batch'] + ' 合入后的各仓提交或产品内容与复测不一致')
                key = f"fix-{task['id']}-{task['attempt']}-{record['batch']}"
                existing = db.execute('SELECT number FROM rounds WHERE batch=? AND idempotency_key=?', (batch['id'], key)).fetchone()
                if existing:
                    number = existing[0]
                else:
                    rows = json.loads(record['results_json'])
                    body = dict(idempotency_key=key, scenario_file_sha256=version['sha256'], submitted_by=record['session_id'],
                        implementer=task['session_id'], product=dict(commit=product['commit'], digest=product['product_digest'], dirty=False), results=rows)
                    from .validation import validate_round
                    validate_round(db, body)
                    number = self.service.insert_round(db, batch, version, body, skill_digest=record['skill_digest'])
                    db.execute('UPDATE batches SET product_digest=? WHERE id=?', (product['product_digest'], batch['id']))
                db.execute('UPDATE fix_validations SET promoted_round=? WHERE task=? AND attempt=? AND validation=? AND batch=?',
                    (number, task['id'], task['attempt'], record['validation'], record['batch']))
                rounds.append(dict(batch=record['batch'], round=number))
            db.execute(f"UPDATE tasks SET status='{S.MERGED}',finished_at=?,error=?,rounds_json=? WHERE id=?",
                (now(), reason, canonical(rounds), task['id']))
        self.complete(task['id'])

    def complete(self, task_id):
        if not self.tasks.kernel.claim(dict(id=task_id)):
            self.repositories.cleanup(task_id)
        self.tasks.worktree_environments.pop(task_id, None)
        task = self.tasks.read(task_id)
        jira = self.service.jira
        # 同号可能属于不同站点；持久记录用完整站点和单号，保留既有表结构。
        links = dict.fromkeys(
            site + '/browse/' + key for p in task['problems']
            if (issue := identify(p['input'].get('defect_url'))) for site, key in [issue])
        paragraphs = ['根因摘要：' + task['result'].get('root_cause', ''),
            '根因仓库：' + '；'.join(p['scenario'] + '：' + (p['root_repo'] or '未定位') for p in task['problems']),
            '仓库与提交：' + '；'.join(r['alias'] + ' ' + r['identity'] + ' ' + r['baseline_branch'] + ' ' +
                r['merged_commit'] + ' ' + r['merge_status'] for r in task['repositories']),
            '改动文件：' + '；'.join(r['alias'] + '：' + '、'.join(r['changed_files']) for r in task['repositories']),
            '复测场景与结果：' + '；'.join(f"{c['batch']} {r['scenario']} {r['status']}" for c in task['validations']
                if c['attempt'] == task['attempt'] for r in c['results']), '尚未部署到测试环境']
        for link in links:
            with self.store.transaction() as db:
                # 旧版只支持单一 Jira 站点，已用裸单号登记过回写；重启不能重复评论。
                key = identify(link)[1]
                if db.execute('SELECT 1 FROM fix_jira WHERE task=? AND issue=?', (task_id, key)).fetchone():
                    continue
                inserted = db.execute('INSERT OR IGNORE INTO fix_jira VALUES (?,?,?,?,?)',
                    (task_id, link, '评论结果待人工确认', 'Jira 状态待人工更新', '回写中断时请人工核对，不自动重试')).rowcount
            if not inserted:
                continue
            result = jira.write_fix(link, paragraphs, os.environ.get('JIRA_FIXED_STATUS', '').strip())
            with self.store.transaction() as db:
                db.execute('UPDATE fix_jira SET comment=?,transition=?,reason=? WHERE task=? AND issue=?',
                    (result['comment'], result['transition'], result['reason'], task_id, link))

    def recover_promotions(self):
        with self.store.connection() as db:
            ids = [r[0] for r in db.execute(f"SELECT id FROM tasks WHERE kind='fix' AND status IN ({sql_states(FIX_RECOVERY)}) "
                "AND NOT EXISTS (SELECT 1 FROM session_tasks s WHERE s.task=tasks.id)")]
        for task_id in ids:
            task = self.tasks.read(task_id)
            if task['status'] == S.MERGED:
                try:
                    self.complete(task_id)
                except (OSError, ValueError, subprocess.SubprocessError) as error:
                    self.tasks.update(task_id, status=S.DEVELOPER_MERGE, error='合入已完成，但清理尚未完成：' + str(error))
                continue
            if not task['attempts'] or task['attempts'][-1]['validation'].get('status') != 'pass':
                continue
            validated = task['attempts'][-1]['validation']['repositories']
            missing = set(validated) - set(self.service.workspace.repos)
            if missing:
                self.tasks.update(task_id, status=S.DEVELOPER_MERGE, error='配置已移除仓库，不能自动恢复清理：' + '、'.join(sorted(missing)))
                continue
            try:
                if any(self.service.workspace.product([a])['repos'][a] != value for a, value in validated.items()):
                    continue
                if any(git(r['root'], 'branch', '--show-current').decode().strip() != r['baseline_branch'] for r in task['repositories']):
                    continue
                for repo in task['repositories']:
                    merged = validated[repo['alias']]['commit']
                    self.repositories.update(task_id, repo['alias'], merged_commit=merged if merged != repo['baseline_commit'] else '',
                        merge_status=S.MERGED if merged != repo['baseline_commit'] else '无改动', merge_reason='重启后逐仓核对通过')
                self.service.environment.ensure_baseline()
                self.promote(self.tasks.read(task_id), '重启后确认所有仓库与已通过复测的提交一致，补做结果登记')
            except (OSError, ValueError, subprocess.SubprocessError) as error:
                self.tasks.update(task_id, status=S.DEVELOPER_MERGE, error=str(error))

    def live(self, task):
        return not self.tasks.stopping.is_set() and self.tasks.read(task['id'])['status'] in FIX_LIVE

    def archive(self, task):
        return self.tasks.kernel.archive(task, move=False)

    def failed(self, task, reason, answer=None, validation=None):
        number = task['attempt'] + 1
        with self.store.transaction() as db:
            db.execute("UPDATE fix_attempts SET outcome='未通过',finished_at=?,reason=?,result_json=?,validation_json=? WHERE task=? AND number=?",
                (now(), reason, canonical(answer or {}), canonical(validation or {}), task['id'], number))
            exhausted = failures_exhausted(db, task['id'])
        self.tasks.update(task['id'], attempt=number, status=S.DEVELOPER if exhausted else S.QUEUED,
            error='连续三次未通过：' + reason if exhausted else reason, result_json=canonical(answer or {}))
        self.archive(task)

    def validate(self, task, problems):
        from .fix_validation import FixValidation
        return FixValidation(self).run(task, problems)

    def action(self, task, action, note=''):
        with self.tasks.lock:
            return self._action(self.tasks.read(task['id']), action, note)

    def _action(self, task, action, note):
        if self.tasks.kernel.claim(task) and action != 'cancel':
            reject(409, 'session-fix-owned', 'task', '会话修复请使用 fix prepare、execute prepare --fix 或 fix land 继续，不能由看板自动提交或合入')
        if action == 'approve' and task['retry_reason'] == 'git-identity':
            action = 'retry'
        if action in ('approve', 'retry'):
            self.service.require_ready(task['workspace'])
        if task['kind'] != 'fix':
            reject(409, 'task-state-invalid', 'kind', '此任务不支持批准修复方案')
        if action == 'approve':
            if task['status'] != S.DEVELOPER:
                reject(409, 'task-state-invalid', 'status', '任务当前状态不支持此操作')
            if not isinstance(note, str):
                reject(422, 'note-invalid', 'note', '开发者说明应为文字')
            with self.store.transaction() as db:
                db.execute('UPDATE fix_tasks SET developer_note=? WHERE task=?', (note, task['id']))
                db.execute("UPDATE fix_problems SET category='',root_repo=NULL WHERE task=?", (task['id'],))
            self.tasks.update(task['id'], status=S.QUEUED, error='开发者已批准方案')
        elif action == 'retry':
            if task['status'] not in FIX_RETRY and not (task['status'] == S.DEVELOPER and task['retry_reason'] in ('preparation', 'interrupted', 'git-identity')):
                reject(409, 'task-state-invalid', 'status', '任务当前状态不支持重试')
            self.tasks.kernel.archive(task, move=False)
            self.tasks.kernel.wake(task['id'])
            self.tasks.update(task['id'], status=S.QUEUED,
                error=('开发者重试，沿用原尝试与提交重新复验' if self.verification_attempt(task) else
                       '开发者重试，从提交步骤继续' if task['retry_reason'] == 'git-identity' else '开发者重试，重新准备工作目录'), finished_at=None)
        elif action == 'cancel':
            if task['status'] in FIX_NO_CANCEL:
                reject(409, 'task-state-invalid', 'status', '任务当前状态不支持放弃')
            self.tasks.update(task['id'], status=S.ABANDONED, finished_at=now(),
                error='开发者已放弃会话修复；保留当前工作区的改动' if self.tasks.kernel.claim(task) else '开发者已放弃修复；保留各仓工作树与分支')
            process = self.tasks.processes.get(task['id'])
            if process:
                terminate_group(process)
            with self.store.transaction() as db:
                if not task['problems']:
                    for problem in self.unresolved(db, task['workspace'], automatic=True):
                        db.execute('INSERT OR IGNORE INTO fix_problems(task,batch,scenario,fingerprint,result_id,first_attempt,input_json) VALUES (?,?,?,?,?,0,?)',
                            (task['id'], problem['batch'], problem['scenario'], problem['fingerprint'], problem['result_id'], canonical(problem)))
            self.tasks.worktree_environments.pop(task['id'], None)

        else:
            reject(409, 'task-state-invalid', 'status', '任务当前状态不支持此操作')
        return dict(ok=True, task=self.tasks.read(task['id']))

    def attention(self, key, origin, *, local=True):
        mapping = {S.DEVELOPER: 'developer-action', S.DEVELOPER_MERGE: 'developer-merge', S.WAIT_COMMIT: 'commit-needed',
                   S.WAIT_VALIDATION: 'validation-needed',
                   S.NEEDS_INPUT: 'cases-needed', S.NEEDS_SYNC: 'sync-needed', S.INTERRUPTED: 'interrupted'}
        items = []
        with self.store.connection() as db:
            for problem in self.session_failures(db,key):
                reason = ('已交给看板修复，等待现有调度条件' if problem['board_requested'] else
                    '在会话中修复（fix prepare），或在本机修复页交给看板修复')
                items.append(dict(kind='board-fix-requested' if problem['board_requested'] else 'session-failure',
                    batch=problem['batch'],scenario=problem['scenario'],result_id=problem['result_id'],
                    url=origin+f'/w/{key}/fixes',reason=problem['batch']+'#'+problem['scenario']+'：'+reason))
        for task in self.tasks.list(key, local=local)['tasks']:
            if task['status'] == S.ABANDONED:
                continue
            url = f"/w/{key}/tasks/{task['id']}"
            base = dict(task=task['id'], batch=task['batch'], url=origin + url,
                **({'resume': task['progress']['resume']} if task['progress'].get('resume') else {}))
            if task['status'] in mapping or task.get('attention'):
                reasons = [task['error']] + [f"{p['batch']} {p['scenario']}：{p['reason']} · {p['plan']}" for p in task.get('attention', [])]
                items.append(dict(base, kind=mapping.get(task['status'], 'developer-action'), reason='；'.join(filter(None, reasons))))
            manual = [r for r in task.get('jira', []) if r['comment'] != '评论成功' or r['transition'] != '流转成功']
            if manual:
                items.append(dict(base, kind='jira-manual', reason='；'.join(
                    f"{r['issue']}：{r['comment']} · {r['transition']} · {r.get('reason', '请开发者在本机查看回写详情')}" for r in manual)))
        return dict(ok=True, items=items)

    def unresolved(self, db, key, *, automatic=False):
        return [p for p in self.service.pending_fixes(db, key) if p['review_ready'] and
            (not automatic or p['source'] != 'agent' or p['execution_origin'] != 'session' or p['board_requested'])]

    def session_failures(self, db, key):
        return [dict(batch=p['batch'],scenario=p['scenario'],title=p['scene']['title'],result_id=p['result_id'],board_requested=p['board_requested'])
            for p in self.unresolved(db,key) if p['source']=='agent' and p['execution_origin']=='session']

    def enqueue_problems(self, db, key, problems, trigger='未解决问题'):
        task_id = db.execute("INSERT INTO tasks(kind,workspace,batch,status,trigger,input_digest,input_json,created_at,attempt,error) "
            "VALUES ('fix',?,'',?,?,'','{}',?,0,'等待开始修复')", (key,S.QUEUED,trigger,now())).lastrowid
        db.execute('INSERT INTO fix_tasks(task) VALUES (?)', (task_id,))
        db.executemany('INSERT INTO fix_problems(task,batch,scenario,fingerprint,result_id,first_attempt,input_json) VALUES (?,?,?,?,?,0,?)',
            [(task_id,p['batch'],p['scenario'],p['fingerprint'],p['result_id'],canonical(p)) for p in problems])
        return task_id

    def handoff(self, key, body):
        if key != self.service.workspace.name:
            reject(404,'workspace-not-found','workspace','工作区不存在')
        if not isinstance(body,dict) or body.keys() != {'result_id'} or type(body['result_id']) is not int or body['result_id'] <= 0:
            reject(422,'fix-handoff-invalid','result_id','请选择一条会话失败记录')
        task_id = None
        with self.tasks.lock, self.store.transaction() as db:
            previous = db.execute('SELECT t.id FROM fix_problems p JOIN tasks t ON t.id=p.task WHERE t.workspace=? AND p.result_id=?',
                (key,body['result_id'])).fetchone()
            if previous:
                if self.tasks.kernel.claim(dict(id=previous['id']),connection=db):
                    reject(409,'fix-already-owned','result_id',f'该失败已由会话修复 #{previous["id"]} 处理，不能交给看板')
                return dict(ok=True,task=self.tasks.read(previous['id'],connection=db),duplicate=True,notice='该失败记录已进入修复任务')
            problem = next((p for p in self.session_failures(db,key) if p['result_id']==body['result_id']),None)
            if not problem:
                reject(409,'session-failure-stale','result_id','该记录已不是当前可交接的会话失败，请刷新后核对')
            db.execute('INSERT OR IGNORE INTO fix_handoffs(result_id,requested_at) VALUES (?,?)',(body['result_id'],now()))
            reason = self.service.fix_start_reason(db,key)
            if not self.service.environment.approved():
                reason = '等待工作区自检通过'
            if not reason:
                task_id = self.enqueue_problems(db,key,self.unresolved(db,key,automatic=True),'开发者将会话失败交给看板修复')
        return dict(ok=True,task=self.tasks.read(task_id) if task_id else None,
            notice='已交给看板修复' + ('；'+reason if reason else ''))

    def discover(self):
        if time.monotonic() < self.next_scan or not self.service.environment.approved():
            return
        self.next_scan = time.monotonic() + 5
        workspace = self.service.workspace
        with self.service.workspace_lock, self.store.transaction() as db:
            if self.service.fix_start_reason(db, workspace.name):
                return
            problems = self.unresolved(db, workspace.name, automatic=True)
            if not problems:
                return
            self.enqueue_problems(db,workspace.name,problems)

    def details(self, task):
        with self.store.connection() as db:
            task['repair_origin'] = 'session' if self.tasks.kernel.claim(task, connection=db) else 'board'
            task.update(dict(db.execute('SELECT * FROM fix_tasks WHERE task=?', (task['id'],)).fetchone()))
            task['problems'] = []
            for row in db.execute('SELECT * FROM fix_problems WHERE task=? ORDER BY id', (task['id'],)):
                problem = dict(row)
                problem['input'] = json.loads(problem.pop('input_json'))
                task['problems'].append(problem)
            task['attempts'] = []
            for row in db.execute('SELECT * FROM fix_attempts WHERE task=? ORDER BY number', (task['id'],)):
                attempt = dict(row)
                for field in ('result', 'validation', 'added', 'interruptions', 'start_commits', 'commits', 'merged_commits'):
                    attempt[field] = json.loads(attempt.pop(field + '_json'))
                task['attempts'].append(attempt)
            task['repositories'] = self.repositories.rows(task['id'])
            task['validations'] = []
            for row in db.execute('SELECT * FROM fix_validations WHERE task=? ORDER BY attempt,validation,batch', (task['id'],)):
                record = dict(row)
                for field in ('results', 'evidence', 'repositories'):
                    record[field] = json.loads(record.pop(field + '_json'))
                task['validations'].append(record)
            task['jira'] = [dict(r) for r in db.execute('SELECT issue,comment,transition,reason FROM fix_jira WHERE task=? ORDER BY issue', (task['id'],))]
        task['attention'] = [p for p in task['problems'] if p['category'] not in ('', 'product')]
        return task

    def list(self, key, *, local=True):
        with queue_view():
            with self.store.connection() as db:
                if not db.execute('SELECT 1 FROM workspaces WHERE key=?', (key,)).fetchone():
                    reject(404, 'workspace-not-found', 'workspace', '工作区不存在')
                ids = [r[0] for r in db.execute("SELECT id FROM tasks WHERE kind='fix' AND workspace=? ORDER BY id", (key,))]
                review = self.service.fix_review_state(db, key)
                session_failures = self.session_failures(db,key)
            return dict(ok=True, tasks=[self.tasks.read(i, local=local) for i in ids], review=review, session_failures=session_failures)
