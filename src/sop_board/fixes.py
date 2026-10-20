"""每工作区一个跨仓修复任务；尝试、独立复测与整组合入独立记录。"""
import json
import time
from importlib.resources import files
from pathlib import Path
import shutil
from string import Template
import sqlite3
import subprocess
import os

import yaml

from .errors import reject, APIError
from .jira import identify
from .service import canonical, now
from .repositories import git
from .repair_repositories import RepairRepositories, head
from .drafts import schema_problems
from .tasks import codex_available, resume_codex, terminate_group, judgment_object, invalid_judgment_constant, codex_failure




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
                "AND status IN ('排队中','待提交','分析中','修复中','复测中','待合入')", (key,)).fetchone())

    def schedule(self, task):
        with self.store.connection() as db:
            running = [r[0] for r in db.execute("SELECT workspace FROM tasks WHERE kind='execute' AND status='运行中'")]
        if task['id'] in self.tasks.workers:
            return
        if task['workspace'] in running:
            self.tasks.update(task['id'], status='排队中', error='等待同一工作区的执行任务结束')
            return
        if self.tasks.starting is not None or len(self.tasks.workers) >= 3:
            self.tasks.update(task['id'], status='排队中', error='等待 Codex 名额')
            return
        error = codex_available()
        if error:
            self.tasks.update(task['id'], status='Codex 不可用', error=error)
            return
        try:
            if not self.prepare(task):
                return
        except (OSError, ValueError, KeyError, sqlite3.Error, subprocess.SubprocessError, APIError) as error:
            with self.store.transaction() as db:
                db.execute("UPDATE fix_tasks SET retry_reason='preparation' WHERE task=?", (task['id'],))
            self.tasks.update(task['id'], status='待开发者处理', error=f'无法准备修复：{error}')
            return
        self.tasks.update(task['id'], status='分析中' if task['attempt'] == 0 else '修复中', started_at=now(), error='修复会话运行中')
        import threading
        self.tasks.starting = task['id']
        worker = threading.Thread(target=self.execute, args=(task,), daemon=True)
        self.tasks.workers[task['id']] = worker
        worker.start()

    def prepare(self, task):
        declarations = self.service.require_ready(task['workspace'])
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
            self.tasks.update(task['id'], status='待开发者处理', error='没有需要修复的仓库')
            return False
        product = workspace.product(aliases)
        if product['dirty']:
            self.tasks.update(task['id'], status='待提交', error='工作区有未提交的产品改动：' + '、'.join(product['changed']))
            return False
        self.tasks.worktree_environments[task['id']] = self.repositories.ensure(task['id'], aliases)
        number = task['attempt'] + 1
        protected_files = self.repositories.protected_files(task['id'])
        previous = json.loads(task['snapshot_json'])
        for alias, manifest in previous.get('protected_files', {}).items():
            protected_files[alias] = manifest
        with self.store.transaction() as db:
            pending_attempt = db.execute("SELECT 1 FROM fix_attempts WHERE task=? AND number=? AND outcome IN ('运行中','已中断')",
                (task['id'], number)).fetchone()
            db.execute("UPDATE fix_tasks SET retry_reason='' WHERE task=?", (task['id'],))
            if pending_attempt and task['input_json'] != '{}':
                db.execute("UPDATE fix_attempts SET outcome='运行中',finished_at=NULL WHERE task=? AND number=?", (task['id'], number))
                return True
        starts = self.repositories.heads(task['id'])
        repositories = {r['alias']: dict(identity=r['identity'], directory=r['worktree'], baseline_directory=r['root'],
            baseline_branch=r['baseline_branch'], baseline_commit=r['baseline_commit'], commit=starts[r['alias']])
            for r in self.repositories.rows(task['id'])}
        snapshot = dict(entries=declarations, config_digest=workspace.config_digest, batches={}, cases={},
                        aliases=aliases, repositories=repositories, protected_files=protected_files)
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

            db.execute('INSERT INTO fix_attempts(task,number,started_at,start_commits_json,added_json) VALUES (?,?,?,?,?)',
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
        path = directory / 'events.jsonl'
        if path.is_file():
            for line in path.read_text(encoding='utf-8', errors='replace').splitlines():
                try:
                    event = json.loads(line)
                    if isinstance(event, dict) and event.get('type') == 'thread.started' and isinstance(event.get('thread_id'), str):
                        self.tasks.update(task['id'], session_id=event['thread_id'])
                        return event['thread_id']
                except ValueError:
                    continue
        return self.tasks.read(task['id'])['session_id']

    def execute(self, task):
        directory = self.tasks.directory(task['id'])
        info = self.tasks.read(task['id'])
        number = task['attempt'] + 1
        prompt = Template(files('sop_board').joinpath('prompts/fix.md').read_text(encoding='utf-8')).substitute(
            problems_file=directory / 'problems.json', entries_file=directory / 'entries.json', snapshot_file=directory / 'snapshot.json',
            cases_file=directory / 'cases.json', feedback_file=directory / 'feedback.json', developer_note=info['developer_note'],
            repositories='\n'.join(r['alias'] + '：' + r['worktree'] + '；身份 ' + r['identity'] for r in info['repositories']))
        (directory / 'prompt.md').write_text(prompt, encoding='utf-8')
        (directory / 'feedback.json').write_text(canonical(dict(added=json.loads(task['input_json'])['added'],
            previous=info['attempts'][-2] if len(info['attempts']) > 1 else None)), encoding='utf-8')
        try:
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
                            db.execute("UPDATE fix_attempts SET outcome='已中断',reason=?,finished_at=? WHERE task=? AND number=?",
                                (interruption, now(), task['id'], number))
                        self.tasks.update(task['id'], status='待开发者处理', error='修复会话中断，重试后仍中断：' + interruption)
                        return
            with self.tasks.lock:
                if not self.live(task):
                    return
                try:
                    answer = json.loads((directory / 'final.json').read_text(encoding='utf-8'), object_pairs_hook=judgment_object, parse_constant=invalid_judgment_constant)
                    products = self.finish(task, answer)
                except (ValueError, TypeError, KeyError, RecursionError) as error:
                    self.failed(task, '输出不符合模式：' + str(error))
                    products = None
            if products:
                self.verify(task, products)
        except (OSError, ValueError, KeyError, TypeError, sqlite3.Error, subprocess.SubprocessError) as error:
            with self.tasks.lock:
                if not self.tasks.stopping.is_set():
                    self.tasks.update(task['id'], status='待开发者处理', error=str(error))
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
                process = self.tasks.launcher(directory, prompt, sandbox='workspace-write', environment=environment, work_directory=directory, writable_directories=self.repositories.repair_directories(task['id']).values()) if not session else resume_codex(
                    directory, session, prompt, sandbox='workspace-write', environment=environment, work_directory=directory, writable_directories=self.repositories.repair_directories(task['id']).values())
                self.tasks.processes[task['id']] = process
                self.tasks.starting = None
            deadline = time.monotonic() + self.tasks.timeout
            while process.poll() is None:
                self.session(task, directory)
                if time.monotonic() >= deadline:
                    return f'Codex 运行超时（{self.tasks.timeout:g} 秒）'
                time.sleep(.05)
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
        directory = self.tasks.directory(task['id'])
        target = directory / 'interruptions' / f"{task['attempt'] + 1}-{time.time_ns()}-{retry}"
        target.mkdir(parents=True)
        for name in ('events.jsonl', 'stderr.txt', 'final.json'):
            source = directory / name
            if source.is_file() and not source.is_symlink():
                shutil.copyfile(source, target / name)
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
        changes = self.repositories.record_changes(task['id'])
        def forbidden_files():
            protected = self.repositories.protected(task['id'], self.repositories.changes(task['id']))
            current = self.repositories.protected_files(task['id'])
            for alias, before in info['snapshot']['protected_files'].items():
                after = current[alias]
                protected.extend(alias + '/' + name for name in before.keys() | after.keys() if before.get(name) != after.get(name))
            return sorted(set(protected))
        protected = forbidden_files()
        if protected:
            self.failed(task, '修复改动涉及禁止修改的 SOP 或场景文件：' + '、'.join(protected), answer)
            return
        product = [p for p in answer['problems'] if p['category'] == 'product']
        with self.store.transaction() as db:
            for problem in answer['problems']:
                db.execute('UPDATE fix_problems SET category=?,root_repo=?,handling=?,reason=?,plan=? WHERE task=? AND batch=? AND scenario=?',
                    (problem['category'], problem['root_repo'], problem['handling'], problem['reason'], answer['plan'], task['id'], problem['batch'], problem['scenario']))
        if not product:
            with self.store.transaction() as db:
                db.execute("UPDATE fix_attempts SET outcome='待开发者处理',finished_at=?,reason=?,result_json=? WHERE task=? AND number=?",
                    (now(), '全部问题需要开发者处理；不产生产品提交', canonical(answer), task['id'], number))
            self.tasks.update(task['id'], attempt=number, status='待开发者处理', error='全部问题需要开发者处理；保留工作树，不提交代码', result_json=canonical(answer))
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
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            self.failed(task, str(error), answer)
            return
        protected = forbidden_files()
        if protected:
            self.failed(task, '提交期间 SOP 或场景文件被改动：' + '、'.join(protected), answer)
            return
        with self.store.transaction() as db:
            pending = dict(status='pending', reason='等待验证', evidence=[], scenarios=[dict(batch=p['batch'], scenario=p['scenario']) for p in product])
            db.execute("UPDATE fix_attempts SET outcome='待验证',result_json=?,validation_json=? WHERE task=? AND number=?",
                (canonical(answer), canonical(pending), task['id'], number))
        self.tasks.update(task['id'], attempt=number, status='复测中', error='使用各仓修复工作树的适配环境复测', result_json=canonical(answer))
        return product

    def verify(self, task, problems, additional=False):
        info = self.tasks.read(task['id'])
        validation = self.validate(info, problems)
        if validation is None:
            return
        with self.tasks.lock:
            if self.tasks.stopping.is_set() or self.tasks.read(task['id'])['status'] != '复测中':
                return
            number = task['attempt'] + 1
            with self.store.transaction() as db:
                db.execute('UPDATE fix_attempts SET validation_json=? WHERE task=? AND number=?', (canonical(validation), task['id'], number))
            if validation['status'] == 'void':
                with self.store.transaction() as db:
                    db.execute("UPDATE fix_attempts SET outcome='已作废',reason=?,finished_at=? WHERE task=? AND number=?",
                        (validation['reason'], now(), task['id'], number))
                    db.execute("UPDATE fix_tasks SET retry_reason='preparation' WHERE task=?", (task['id'],))
                self.tasks.update(task['id'], status='待开发者处理', error=validation['reason'], void_reason=validation['reason'])
            elif validation['status'] == 'fail':
                if additional:
                    validation['reason'] = '合入基线后的追加验证未通过：' + validation.get('reason', '验证未通过')
                answer = self.tasks.read(task['id'])['result']
                self.failed(task, validation.get('reason', '验证未通过'), answer, validation)
            elif validation['status'] == 'pass':
                with self.store.transaction() as db:
                    db.execute("UPDATE fix_attempts SET outcome='通过',finished_at=? WHERE task=? AND number=?", (now(), task['id'], number))
                self.tasks.update(task['id'], status='待合入', error='验证通过，核对基线与工作区后快进合入')
            self.archive(task)
        if validation['status'] == 'pass':
            self.merge(task, problems)

    def snapshot_changes(self, task):
        snapshot = task['snapshot']
        workspace = self.service.workspace
        workspace.reload()
        if not self.service.environment.approved() or workspace.config_digest != snapshot['config_digest']:
            return '工作区配置或自检状态变化'
        with self.store.connection() as db:
            current = {}
            for batch in db.execute('SELECT * FROM batches WHERE workspace=?', (task['workspace'],)):
                version = self.service.latest_version(db, batch)
                if version:
                    current[batch['name']] = version['sha256']
                    if canonical(self.service.case_union(db, batch)) != canonical(snapshot['cases'].get(batch['name'])):
                        return '用例并集变化'
            if current != {name: row['sha256'] for name, row in snapshot['batches'].items()}:
                return '场景版本变化'
        return ''

    def defer_merge(self, task, plans, reason=''):
        self.repositories.record_blocked(task['id'], plans)
        message = reason or '；'.join(p['alias'] + '：' + '；'.join(p['reasons']) for p in plans if p['reasons'])
        self.tasks.update(task['id'], status='待开发者合入', error=message)

    def merge(self, task, problems):
        environment = self.service.environment
        revalidate = False
        with environment.operation, self.tasks.lock:
            info = self.tasks.read(task['id'])
            if info['status'] != '待合入' or self.tasks.stopping.is_set():
                return
            validation = info['attempts'][-1]['validation']
            records = [r for r in info['validations'] if r['attempt'] == info['attempt']]
            last = max((r['validation'] for r in records), default=0)
            records = [r for r in records if r['validation'] == last]
            if validation.get('status') != 'pass' or not records or any(r['status'] != 'pass' for r in records):
                self.tasks.update(task['id'], status='待开发者处理', error='缺少本次独立复测通过记录，不能合入')
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
                    db.execute("UPDATE fix_attempts SET outcome='待验证',finished_at=NULL WHERE task=? AND number=?", (task['id'], info['attempt']))
                self.tasks.update(task['id'], status='复测中', error='基线已前进，各仓工作树更新后重新复测')
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
                        if repo['merge_status'] not in ('已合入', '无改动', '合入失败', '合入中止'):
                            self.repositories.update(task['id'], repo['alias'], merge_status='合入中止', merge_reason='整组操作中止：' + str(error))
                    self.tasks.update(task['id'], status='待开发者合入', error=str(error))
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
            db.execute("UPDATE tasks SET status='已合入',finished_at=?,error=?,rounds_json=? WHERE id=?",
                (now(), reason, canonical(rounds), task['id']))
        self.complete(task['id'])

    def complete(self, task_id):
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
            ids = [r[0] for r in db.execute("SELECT id FROM tasks WHERE kind='fix' AND status IN ('已中断','待合入','待开发者合入','已合入')")]
        for task_id in ids:
            task = self.tasks.read(task_id)
            if task['status'] == '已合入':
                try:
                    self.complete(task_id)
                except (OSError, ValueError, subprocess.SubprocessError) as error:
                    self.tasks.update(task_id, status='待开发者合入', error='合入已完成，但清理尚未完成：' + str(error))
                continue
            if not task['attempts'] or task['attempts'][-1]['validation'].get('status') != 'pass':
                continue
            validated = task['attempts'][-1]['validation']['repositories']
            missing = set(validated) - set(self.service.workspace.repos)
            if missing:
                self.tasks.update(task_id, status='待开发者合入', error='配置已移除仓库，不能自动恢复清理：' + '、'.join(sorted(missing)))
                continue
            try:
                if any(self.service.workspace.product([a])['repos'][a] != value for a, value in validated.items()):
                    continue
                if any(git(r['root'], 'branch', '--show-current').decode().strip() != r['baseline_branch'] for r in task['repositories']):
                    continue
                for repo in task['repositories']:
                    merged = validated[repo['alias']]['commit']
                    self.repositories.update(task_id, repo['alias'], merged_commit=merged if merged != repo['baseline_commit'] else '',
                        merge_status='已合入' if merged != repo['baseline_commit'] else '无改动', merge_reason='重启后逐仓核对通过')
                self.service.environment.ensure_baseline()
                self.promote(self.tasks.read(task_id), '重启后确认所有仓库与已通过复测的提交一致，补做结果登记')
            except (OSError, ValueError, subprocess.SubprocessError) as error:
                self.tasks.update(task_id, status='待开发者合入', error=str(error))

    def live(self, task):
        return not self.tasks.stopping.is_set() and self.tasks.read(task['id'])['status'] in ('分析中', '修复中')

    def archive(self, task):
        directory = self.tasks.directory(task['id'])
        target = directory / 'attempts' / str(task['attempt'] + 1)
        target.mkdir(parents=True, exist_ok=True)
        for path in directory.iterdir():
            if self.tasks.is_task_file(task['id'], path):
                shutil.copyfile(path, target / path.name)

    def failed(self, task, reason, answer=None, validation=None):
        number = task['attempt'] + 1
        with self.store.transaction() as db:
            db.execute("UPDATE fix_attempts SET outcome='未通过',finished_at=?,reason=?,result_json=?,validation_json=? WHERE task=? AND number=?",
                (now(), reason, canonical(answer or {}), canonical(validation or {}), task['id'], number))
            recent = [r[0] for r in db.execute('SELECT outcome FROM fix_attempts WHERE task=? ORDER BY number DESC LIMIT 3', (task['id'],))]
        exhausted = recent == ['未通过'] * 3
        self.tasks.update(task['id'], attempt=number, status='待开发者处理' if exhausted else '排队中',
            error='连续三次未通过：' + reason if exhausted else reason, result_json=canonical(answer or {}))
        self.archive(task)

    def validate(self, task, problems):
        from .fix_validation import FixValidation
        return FixValidation(self).run(task, problems)

    def action(self, task, action, note=''):
        with self.tasks.lock:
            return self._action(self.tasks.read(task['id']), action, note)

    def _action(self, task, action, note):
        if action in ('approve', 'retry'):
            self.service.require_ready(task['workspace'])
        if task['kind'] != 'fix':
            reject(409, 'task-state-invalid', 'kind', '此任务不支持批准修复方案')
        if action == 'approve':
            if task['status'] != '待开发者处理':
                reject(409, 'task-state-invalid', 'status', '任务当前状态不支持此操作')
            if not isinstance(note, str):
                reject(422, 'note-invalid', 'note', '开发者说明应为文字')
            with self.store.transaction() as db:
                db.execute('UPDATE fix_tasks SET developer_note=? WHERE task=?', (note, task['id']))
                db.execute("UPDATE fix_problems SET category='',root_repo=NULL WHERE task=?", (task['id'],))
            self.tasks.update(task['id'], status='排队中', error='开发者已批准方案')
        elif action == 'retry':
            if task['status'] not in ('已中断', 'Codex 不可用') and not (task['status'] == '待开发者处理' and task['retry_reason'] in ('preparation', 'interrupted')):
                reject(409, 'task-state-invalid', 'status', '任务当前状态不支持重试')
            self.tasks.update(task['id'], status='排队中', error='开发者重试，重新准备工作目录', finished_at=None)
        elif action == 'cancel':
            if task['status'] in ('已合入', '已放弃', '待合入'):
                reject(409, 'task-state-invalid', 'status', '任务当前状态不支持放弃')
            self.tasks.update(task['id'], status='已放弃', finished_at=now(), error='开发者已放弃修复；保留各仓工作树与分支')
            process = self.tasks.processes.get(task['id'])
            if process:
                terminate_group(process)
            with self.store.transaction() as db:
                if not task['problems']:
                    for problem in self.unresolved(db, task['workspace']):
                        db.execute('INSERT OR IGNORE INTO fix_problems(task,batch,scenario,fingerprint,result_id,first_attempt,input_json) VALUES (?,?,?,?,?,0,?)',
                            (task['id'], problem['batch'], problem['scenario'], problem['fingerprint'], problem['result_id'], canonical(problem)))
            self.tasks.worktree_environments.pop(task['id'], None)

        else:
            reject(409, 'task-state-invalid', 'status', '任务当前状态不支持此操作')
        return dict(ok=True, task=self.tasks.read(task['id']))

    def attention(self, key, origin):
        mapping = {'待开发者处理': 'developer-action', '待开发者合入': 'developer-merge', '待提交': 'commit-needed',
                   '待补充': 'cases-needed', '待同步': 'sync-needed', '已中断': 'interrupted'}
        items = []
        for task in self.tasks.list(key)['tasks']:
            if task['status'] == '已放弃':
                continue
            url = f"/w/{key}/fixes" if task['kind'] == 'fix' else f"/w/{key}/b/{task['batch']}/merges" if task['kind'] == 'merge' else f"/w/{key}/b/{task['batch']}"
            base = dict(task=task['id'], batch=task['batch'], url=origin + url,
                resume='codex resume ' + task['session_id'] if task['session_id'] else '')
            if task['status'] in mapping or task.get('attention'):
                reasons = [task['error']] + [f"{p['batch']} {p['scenario']}：{p['reason']} · {p['plan']}" for p in task.get('attention', [])]
                items.append(dict(base, kind=mapping.get(task['status'], 'developer-action'), reason='；'.join(filter(None, reasons))))
            manual = [r for r in task.get('jira', []) if r['comment'] != '评论成功' or r['transition'] != '流转成功']
            if manual:
                items.append(dict(base, kind='jira-manual', reason='；'.join(
                    f"{r['issue']}：{r['comment']} · {r['transition']} · {r['reason']}" for r in manual)))
        return dict(ok=True, items=items)

    def unresolved(self, db, key):
        return [p for p in self.service.pending_fixes(db, key) if p['review_ready']]

    def discover(self):
        if time.monotonic() < self.next_scan or not self.service.environment.approved():
            return
        self.next_scan = time.monotonic() + 5
        workspace = self.service.workspace
        with self.service.workspace_lock, self.store.transaction() as db:
            if self.service.fix_start_reason(db, workspace.name):
                return
            problems = self.unresolved(db, workspace.name)
            if not problems:
                return
            task_id = db.execute("INSERT INTO tasks(kind,workspace,batch,status,trigger,input_digest,input_json,created_at,attempt,error) "
                "VALUES ('fix',?,'','排队中','未解决问题','','{}',?,0,'等待开始修复')", (workspace.name, now())).lastrowid
            db.execute('INSERT INTO fix_tasks(task) VALUES (?)', (task_id,))
            db.executemany('INSERT INTO fix_problems(task,batch,scenario,fingerprint,result_id,first_attempt,input_json) '
                           'VALUES (?,?,?,?,?,0,?)',
                [(task_id, p['batch'], p['scenario'], p['fingerprint'], p['result_id'], canonical(p)) for p in problems])

    def details(self, task):
        with self.store.connection() as db:
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

    def list(self, key):
        with self.store.connection() as db:
            if not db.execute('SELECT 1 FROM workspaces WHERE key=?', (key,)).fetchone():
                reject(404, 'workspace-not-found', 'workspace', '工作区不存在')
            ids = [r[0] for r in db.execute("SELECT id FROM tasks WHERE kind='fix' AND workspace=? ORDER BY id", (key,))]
            review = self.service.fix_review_state(db, key)
        return dict(ok=True, tasks=[self.tasks.read(i) for i in ids], review=review)
