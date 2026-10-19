"""每个授权仓库一个修复任务；范围、判定与尝试独立保存。"""
import json
import time
from importlib.resources import files
from pathlib import Path
import shutil
from string import Template
import subprocess
import re
import hashlib

from .errors import reject, APIError
from .defect_links import ISSUE_KEY
from .service import canonical, now
from .statistics import calculate
from .repositories import git, product_files
from .tasks import codex_available, resume_codex, terminate_group, judgment_object, invalid_judgment_constant


ACTIVE = ('排队中', '分析中', '修复中', '复测中', '待合入')


def validate_output(value, schema, path='最终输出'):
    """只解释本产品随附模式使用的对象、数组、字符串与枚举。"""
    kind = schema['type']
    valid = isinstance(value, {'object': dict, 'array': list, 'string': str}[kind])
    if not valid or ('enum' in schema and value not in schema['enum']):
        raise ValueError(f'输出不符合模式：{path}')
    if kind == 'object':
        if set(value) != set(schema['required']):
            raise ValueError(f'输出不符合模式：{path} 字段缺失或多余')
        for name, item in value.items():
            validate_output(item, schema['properties'][name], path + '.' + name)
    if kind == 'array':
        for index, item in enumerate(value):
            validate_output(item, schema['items'], f'{path}[{index}]')


class Fixes:
    def __init__(self, tasks):
        self.tasks, self.service, self.store = tasks, tasks.service, tasks.store
        self.next_scan = 0

    def blocks_execution(self, key):
        root = self.service.writable_repos[key]
        with self.store.connection() as db:
            return bool(db.execute("SELECT 1 FROM fix_tasks f JOIN tasks t ON t.id=f.task WHERE repository=? "
                "AND status IN ('排队中','待提交','分析中','修复中','复测中','待合入')", (str(root),)).fetchone())

    def schedule(self, task):
        with self.store.connection() as db:
            running = [self.service.writable_repos[r[0]] for r in db.execute("SELECT project FROM tasks WHERE kind='execute' AND status='运行中'")]
        if task['id'] in self.tasks.workers:
            return
        root = self.service.writable_repos[task['project']]
        if root in running:
            self.tasks.update(task['id'], status='排队中', error='等待同一仓库的执行任务结束')
            return
        if self.tasks.starting is not None or len(self.tasks.workers) - len(self.tasks.check_workers) >= 3:
            self.tasks.update(task['id'], status='排队中', error='等待 Codex 名额')
            return
        error = codex_available()
        if error:
            self.tasks.update(task['id'], status='Codex 不可用', error=error)
            return
        try:
            if not self.prepare(task):
                return
        except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
            self.tasks.update(task['id'], status='待开发者处理', error=f'无法准备修复：{error}')
            return
        self.tasks.update(task['id'], status='分析中' if task['attempt'] == 0 else '修复中', started_at=now(), error='修复会话运行中')
        import threading
        self.tasks.starting = task['id']
        worker = threading.Thread(target=self.execute, args=(task,), daemon=True)
        self.tasks.workers[task['id']] = worker
        worker.start()

    def prepare(self, task):
        root = self.service.writable_repos[task['project']]
        declarations = self.service.entries(task['project'])
        if not declarations['ok']:
            self.tasks.update(task['id'], status='排队中', error='入口声明不合格')
            return False
        entry = '.tw/config.json' if declarations['source'] == 'tw-config' else 'docs/sop/entries.yaml'
        current = product_files(root, root / 'docs/sop', root / entry)
        committed = product_files(root, root / 'docs/sop', root / entry, head=True)
        if current != committed:
            self.tasks.update(task['id'], status='待提交', error='工作区有未提交的产品改动')
            return False
        info = self.tasks.read(task['id'])
        if not info['baseline_branch']:
            branch = git(root, 'branch', '--show-current').decode().strip()
            if not branch:
                self.tasks.update(task['id'], status='待开发者处理', error='主工作区不在分支上')
                with self.store.transaction() as db:
                    db.execute("UPDATE fix_tasks SET retry_reason='detached' WHERE task=?", (task['id'],))
                return False
            commit = git(root, 'rev-parse', 'HEAD').decode().strip()
            worktree = self.store.data.resolve() / 'worktrees' / f"fix-{task['id']}"
            fix_branch = f"sop-board/fix/{task['id']}"
            worktree.parent.mkdir(parents=True, exist_ok=True)
            git(root, 'worktree', 'add', '-b', fix_branch, str(worktree), commit)
            with self.store.transaction() as db:
                db.execute('UPDATE fix_tasks SET baseline_branch=?,baseline_commit=?,fix_branch=?,worktree=?,retry_reason=\'\' WHERE task=?',
                    (branch, commit, fix_branch, str(worktree), task['id']))
        else:
            worktree = Path(info['worktree'])
            if not worktree.exists():
                git(root, 'worktree', 'prune')
                git(root, 'worktree', 'add', str(worktree), info['fix_branch'])
        number = task['attempt'] + 1
        with self.store.transaction() as db:
            pending = db.execute("SELECT 1 FROM fix_attempts WHERE task=? AND number=? AND outcome IN ('运行中','已中断')",
                (task['id'], number)).fetchone()
            db.execute("UPDATE fix_tasks SET retry_reason='' WHERE task=?", (task['id'],))
            if pending and task['input_json'] != '{}':
                db.execute("UPDATE fix_attempts SET outcome='运行中',finished_at=NULL WHERE task=? AND number=?", (task['id'], number))
                return True
        snapshot = dict(entries=declarations, entry_path=entry, batches={}, cases={})
        with self.store.transaction() as db:
            fresh = self.unresolved(db, task['project'])
            added = []
            for problem in fresh:
                previous = db.execute('SELECT * FROM fix_problems WHERE task=? AND batch=? AND scenario=?',
                    (task['id'], problem['batch'], problem['scenario'])).fetchone()
                content_changed = previous and previous['fingerprint'] != problem['fingerprint']
                if previous and previous['result_id'] == problem['result_id'] and not content_changed:
                    continue
                problem.update(jira=None, jira_error='')
                jira = getattr(self.service, 'jira', None)
                match = re.fullmatch(re.escape(self.service.jira_url) + r'/browse/(' + ISSUE_KEY + ')', problem['defect_url']) if self.service.jira_url else None
                if match:
                    problem['jira_key'] = match[1]
                if match and jira and all((jira.url, jira.email, jira.token)):
                    try:
                        problem['jira'] = jira.issue(match[1])
                    except APIError as error:
                        problem['jira_error'] = '；'.join(p['problem'] for p in error.problems)
                if previous:
                    db.execute("UPDATE fix_problems SET result_id=?,fingerprint=?,first_attempt=?,input_json=?,category='',handling='',reason='',plan='' WHERE id=?",
                        (problem['result_id'], problem['fingerprint'], number if content_changed else previous['first_attempt'], canonical(problem), previous['id']))
                else:
                    db.execute('INSERT INTO fix_problems(task,batch,scenario,fingerprint,result_id,first_attempt,input_json) VALUES (?,?,?,?,?,?,?)',
                        (task['id'], problem['batch'], problem['scenario'], problem['fingerprint'], problem['result_id'], number, canonical(problem)))
                if not previous or content_changed or previous['category'] not in ('', 'product'):
                    added.append(dict(batch=problem['batch'], scenario=problem['scenario']))
            problems = [json.loads(r[0]) for r in db.execute("SELECT input_json FROM fix_problems WHERE task=? AND category IN ('','product') ORDER BY id", (task['id'],))]
            for batch in db.execute('SELECT * FROM batches WHERE project=? ORDER BY id', (task['project'],)):
                version = self.service.latest_version(db, batch)
                if version:
                    snapshot['batches'][batch['name']] = dict(content=version['content'], sha256=version['sha256'], path=batch['path'])
                    snapshot['cases'][batch['name']] = self.service.case_union(db, batch)
            start = git(worktree, 'rev-parse', 'HEAD').decode().strip()
            # 中断重试沿用尚未完成的同一次尝试。
            db.execute('INSERT OR IGNORE INTO fix_attempts(task,number,started_at,start_commit,added_json) VALUES (?,?,?,?,?)',
                (task['id'], number, now(), start, canonical(added)))
        directory = self.tasks.directory(task['id'])
        directory.mkdir(parents=True, exist_ok=True)
        snapshot['commit'] = start
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
        worktree = Path(info['worktree'])
        prompt = Template(files('sop_board').joinpath('prompts/fix.md').read_text(encoding='utf-8')).substitute(
            problems_file=directory / 'problems.json', entries_file=directory / 'entries.json', snapshot_file=directory / 'snapshot.json',
            cases_file=directory / 'cases.json', feedback_file=directory / 'feedback.json', developer_note=info['developer_note'])
        (directory / 'prompt.md').write_text(prompt, encoding='utf-8')
        (directory / 'feedback.json').write_text(canonical(dict(added=json.loads(task['input_json'])['added'],
            previous=info['attempts'][-2] if len(info['attempts']) > 1 else None)), encoding='utf-8')
        try:
            for retry in range(2):
                interruption = self.run_session(task, directory, worktree, prompt)
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
                    self.restore(task)
                    self.failed(task, '输出不符合模式：' + str(error))
                    products = None
            if products:
                self.verify(task, products)
        except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
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

    def run_session(self, task, directory, worktree, prompt):
        process = None
        try:
            (directory / 'final.json').unlink(missing_ok=True)
            with self.tasks.lock:
                if not self.live(task):
                    return ''
                session = self.tasks.read(task['id'])['session_id']
                process = self.tasks.launcher(directory, prompt, sandbox='workspace-write', work_directory=worktree) if not session else resume_codex(
                    directory, session, prompt, sandbox='workspace-write', work_directory=worktree)
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
                return f'Codex 退出码 {process.returncode}'
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
        worktree = Path(info['worktree'])
        number = task['attempt'] + 1
        selected = json.loads(task['input_json'])['problems']
        schema = json.loads(files('sop_board').joinpath('prompts/fix.schema.json').read_text(encoding='utf-8'))
        validate_output(answer, schema)
        expected = {(p['batch'], p['scenario']) for p in selected}
        returned = [(p['batch'], p['scenario']) for p in answer['problems']]
        missing = expected - set(returned)
        if missing:
            raise ValueError('遗漏 ' + '、'.join(s for _, s in sorted(missing)))
        if len(returned) != len(set(returned)) or set(returned) != expected:
            raise ValueError('问题重复或不在本次范围内')
        changed = self.changed(task)
        entry = json.loads(task['snapshot_json'])['entry_path']
        if any(path.startswith('docs/sop/') or path == entry for path in changed):
            self.restore(task)
            self.failed(task, '修复改动涉及场景文件或入口声明', answer)
            return
        product = [p for p in answer['problems'] if p['category'] == 'product']
        with self.store.transaction() as db:
            for p in answer['problems']:
                db.execute('UPDATE fix_problems SET category=?,handling=?,reason=?,plan=? WHERE task=? AND batch=? AND scenario=?',
                    (p['category'], p['handling'], p['reason'], answer['plan'], task['id'], p['batch'], p['scenario']))
        if not product:
            self.restore(task)
            with self.store.transaction() as db:
                db.execute("UPDATE fix_attempts SET outcome='待开发者处理',finished_at=?,reason=?,result_json=? WHERE task=? AND number=?",
                    (now(), '全部问题需要开发者处理', canonical(answer), task['id'], number))
            self.tasks.update(task['id'], attempt=number, status='待开发者处理', error='全部问题需要开发者处理', result_json=canonical(answer))
            self.archive(task)
            return
        if not changed:
            self.failed(task, '修复会话没有改动文件', answer)
            return
        git(worktree, 'add', '--all')
        lines = []
        for p in selected:
            if (p['batch'], p['scenario']) not in {(r['batch'], r['scenario']) for r in product}:
                continue
            label = p.get('jira_key') or p['defect_url'] if p['source'] == 'manual' else '回归检查失败' if p['source'] == 'check' else 'Agent 失败'
            lines.append(f"{p['batch']} {p['scenario']} {label}")
        message = f"sop-board 修复 #{task['id']} 第 {number} 次尝试\n\n" + '\n'.join(lines)
        root = self.service.writable_repos[task['project']]
        author = git(root, 'config', 'user.name').decode().strip()
        email = git(root, 'config', 'user.email').decode().strip()
        try:
            git(worktree, '-c', 'commit.gpgsign=false', '-c', 'user.name=' + author,
                '-c', 'user.email=' + email, 'commit', '-m', message, capture_stderr=True)
        except subprocess.CalledProcessError as error:
            output = error.output.decode('utf-8', errors='replace')
            log = self.tasks.directory(task['id']) / 'commit-hook.txt'
            log.write_text(output, encoding='utf-8')
            self.failed(task, '仓库提交钩子未通过', answer,
                dict(status='fail', reason='仓库提交钩子未通过', output=output, evidence=[str(log)]))
            return
        commit = git(worktree, 'rev-parse', 'HEAD').decode().strip()
        with self.store.transaction() as db:
            pending = dict(status='pending', reason='等待验证', evidence=[], scenarios=[dict(batch=p['batch'], scenario=p['scenario']) for p in product])
            db.execute("UPDATE fix_attempts SET commit_id=?,outcome='待验证',result_json=?,validation_json=? WHERE task=? AND number=?",
                (commit, canonical(answer), canonical(pending), task['id'], number))
        self.tasks.update(task['id'], attempt=number, status='复测中', error='复测产品问题并运行回归检查', result_json=canonical(answer))
        return product

    def verify(self, task, problems, additional=False):
        validation = self.validate(self.tasks.read(task['id']), problems)
        if validation is None:
            return
        with self.tasks.lock:
            if self.tasks.stopping.is_set() or self.tasks.read(task['id'])['status'] != '复测中':
                return
            number = task['attempt'] + 1
            with self.store.transaction() as db:
                db.execute('UPDATE fix_attempts SET validation_json=? WHERE task=? AND number=?', (canonical(validation), task['id'], number))
            if validation['status'] == 'fail':
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

    def baseline_directory(self, task):
        root = self.service.writable_repos[task['project']]
        directory = None
        for field in git(root, 'worktree', 'list', '--porcelain', '-z').decode().split('\0'):
            if field.startswith('worktree '):
                directory = Path(field[9:])
            elif field == 'branch refs/heads/' + task['baseline_branch']:
                return directory

    def merge(self, task, problems):
        info = self.tasks.read(task['id'])
        root = self.service.writable_repos[task['project']]
        worktree = Path(info['worktree'])
        baseline = git(root, 'rev-parse', 'refs/heads/' + info['baseline_branch']).decode().strip()
        candidate = git(worktree, 'rev-parse', 'HEAD').decode().strip()
        try:
            git(root, 'merge-base', '--is-ancestor', baseline, candidate)
        except subprocess.CalledProcessError as error:
            if error.returncode != 1:
                raise
            try:
                git(worktree, '-c', 'commit.gpgsign=false', 'merge', '--no-edit', baseline, capture_stderr=True)
            except subprocess.CalledProcessError as conflict:
                if git(worktree, 'ls-files', '--unmerged'):
                    git(worktree, 'merge', '--abort')
                    reason = '合入基线时冲突'
                else:
                    reason = '无法合入基线'
                self.tasks.update(task['id'], status='待开发者合入', error=reason + '：' + conflict.output.decode('utf-8', errors='replace'))
                return
            with self.store.transaction() as db:
                db.execute("UPDATE fix_attempts SET outcome='待验证',finished_at=NULL WHERE task=? AND number=?", (task['id'], info['attempt']))
            self.tasks.update(task['id'], status='复测中', error='基线已前进，验证合入基线后的候选提交')
            self.verify(task, problems, additional=True)
            return
        directory = self.baseline_directory(info)
        if directory is None:
            branch = git(root, 'branch', '--show-current').decode().strip() or '分离头指针'
            self.tasks.update(task['id'], status='待开发者合入', error='基线未在任何工作目录检出，主工作区当前分支为 ' + branch)
            return
        branch = git(directory, 'branch', '--show-current').decode().strip()
        if branch != info['baseline_branch']:
            self.tasks.update(task['id'], status='待开发者合入', error='基线工作目录当前分支为 ' + branch)
            return
        try:
            git(directory, 'merge', '--ff-only', info['fix_branch'], capture_stderr=True)
        except subprocess.CalledProcessError as error:
            dirty = bool(git(directory, 'status', '--porcelain', '--untracked-files=all'))
            reason = 'Git 因未提交修改拒绝快进' if dirty else 'Git 拒绝快进合入'
            self.tasks.update(task['id'], status='待开发者合入', error=reason + '：' + error.output.decode('utf-8', errors='replace'))
            return
        merged = git(directory, 'rev-parse', 'HEAD').decode().strip()
        dirty = bool(git(directory, 'status', '--porcelain', '--untracked-files=all'))
        with self.store.transaction() as db:
            db.execute('UPDATE fix_tasks SET merged_commit=? WHERE task=?', (merged, task['id']))
        if not self.tasks.stopping.is_set():
            self.promote(info, merged, '工作区含未经验收的修改' if dirty else '验证通过，已快进合入基线')

    def promote(self, task, merged, reason):
        root = self.service.writable_repos[task['project']]
        entry = task['snapshot']['entry_path']
        manifest = product_files(root, root / 'docs/sop', root / entry, head=True, revision=merged)
        product_digest = hashlib.sha256(canonical(manifest).encode()).hexdigest()
        with self.store.transaction() as db:
            records = list(db.execute('SELECT * FROM fix_candidates WHERE task=? AND attempt=? AND validation='
                '(SELECT MAX(validation) FROM fix_candidates WHERE task=? AND attempt=?) ORDER BY batch',
                (task['id'], task['attempt'], task['id'], task['attempt'])))
            if not records or any(r['status'] != 'pass' for r in records):
                raise ValueError('没有可提升的通过候选记录')
            rounds = []
            for record in records:
                batch = self.service.batch(db, task['project'], record['batch'])
                key = f"fix-{task['id']}-{task['attempt']}-{record['batch']}"
                existing = db.execute('SELECT number FROM rounds WHERE batch=? AND idempotency_key=?', (batch['id'], key)).fetchone()
                if existing:
                    number = existing[0]
                else:
                    rows = json.loads(record['results_json'])
                    for row in rows:
                        row['executor'] = record['session_id']
                    version = db.execute('SELECT * FROM versions WHERE id=?', (record['version'],)).fetchone()
                    body = dict(idempotency_key=key, scenario_file_sha256=version['sha256'], submitted_by=record['session_id'],
                        implementer=task['session_id'], product=dict(commit=merged, digest=product_digest, dirty=False), results=rows)
                    from .validation import validate_round
                    validate_round(db, body)
                    number = self.service.insert_round(db, batch, version, body,
                        result_checks={r['scenario']: r['regression_check'] for r in rows if r.get('regression_check')})
                    db.execute('UPDATE batches SET product_digest=? WHERE id=?', (product_digest, batch['id']))
                db.execute('UPDATE fix_candidates SET promoted_round=? WHERE task=? AND attempt=? AND validation=? AND batch=?',
                    (number, task['id'], task['attempt'], record['validation'], record['batch']))
                rounds.append(dict(batch=record['batch'], round=number))
            db.execute('UPDATE fix_tasks SET merged_commit=? WHERE task=?', (merged, task['id']))
            db.execute("UPDATE tasks SET status='已合入',finished_at=?,error=?,rounds_json=? WHERE id=?",
                (now(), reason, canonical(rounds), task['id']))

    def recover_promotions(self):
        with self.store.connection() as db:
            ids = [r[0] for r in db.execute("SELECT DISTINCT t.id FROM tasks t JOIN fix_candidates c ON c.task=t.id "
                "JOIN fix_attempts a ON a.task=t.id AND a.number=t.attempt "
                "WHERE t.kind='fix' AND t.status IN ('已中断','待合入','待开发者合入','已合入') "
                "AND a.outcome='通过' AND c.attempt=t.attempt AND c.promoted_round IS NULL "
                "AND c.validation=(SELECT MAX(latest.validation) FROM fix_candidates latest WHERE latest.task=t.id AND latest.attempt=t.attempt)")]
        for task_id in ids:
            task = self.tasks.read(task_id)
            if task['project'] not in self.service.writable_repos:
                continue
            latest = max(c['validation'] for c in task['candidates'] if c['attempt'] == task['attempt'])
            records = [c for c in task['candidates'] if c['attempt'] == task['attempt'] and c['validation'] == latest]
            if any(c['status'] != 'pass' for c in records):
                continue
            root = self.service.writable_repos[task['project']]
            try:
                merged = git(root, 'rev-parse', 'refs/heads/' + task['baseline_branch']).decode().strip()
                git(root, 'merge-base', '--is-ancestor', records[0]['commit_id'], merged)
            except subprocess.CalledProcessError:
                continue
            self.promote(task, merged, '重启后确认候选已在基线中，已补做结果提升')

    def live(self, task):
        return not self.tasks.stopping.is_set() and self.tasks.read(task['id'])['status'] in ('分析中', '修复中')

    def changed(self, task):
        info = self.tasks.read(task['id'])
        worktree = Path(info['worktree'])
        start = info['attempts'][-1]['start_commit']
        raw = git(worktree, 'diff', '--name-only', '-z', start, '--') + git(worktree, 'ls-files', '--others', '--exclude-standard', '-z')
        return sorted({p.decode() for p in raw.split(b'\0') if p})

    def restore(self, task):
        info = self.tasks.read(task['id'])
        worktree = Path(info['worktree'])
        expected = self.store.data.resolve() / 'worktrees' / f"fix-{task['id']}"
        if worktree != expected or info['fix_branch'] != f"sop-board/fix/{task['id']}":
            raise ValueError('修复工作目录不属于此任务')
        start = info['attempts'][-1]['start_commit']
        head = git(worktree, 'rev-parse', 'HEAD').decode().strip()
        if head != start:
            git(worktree, 'update-ref', 'refs/heads/' + info['fix_branch'], start, head)
        git(worktree, 'restore', '--source', start, '--staged', '--worktree', '--', '.')
        git(worktree, 'clean', '-fd')

    def archive(self, task):
        directory = self.tasks.directory(task['id'])
        target = directory / 'attempts' / str(task['attempt'] + 1)
        target.mkdir(parents=True, exist_ok=True)
        for path in directory.iterdir():
            if path.is_file() and not path.is_symlink():
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
        if task['kind'] != 'fix':
            reject(409, 'task-state-invalid', 'kind', '此任务不支持批准修复方案')
        if action == 'approve':
            if task['status'] != '待开发者处理':
                reject(409, 'task-state-invalid', 'status', '任务当前状态不支持此操作')
            if not isinstance(note, str):
                reject(422, 'note-invalid', 'note', '开发者说明应为文字')
            with self.store.transaction() as db:
                db.execute('UPDATE fix_tasks SET developer_note=? WHERE task=?', (note, task['id']))
                db.execute("UPDATE fix_problems SET category='' WHERE task=?", (task['id'],))
            self.tasks.update(task['id'], status='排队中', error='开发者已批准方案')
        elif action == 'retry':
            if task['status'] not in ('已中断', 'Codex 不可用') and not (task['status'] == '待开发者处理' and task['retry_reason'] in ('detached', 'interrupted')):
                reject(409, 'task-state-invalid', 'status', '任务当前状态不支持重试')
            self.tasks.update(task['id'], status='排队中', error='开发者重试，重新准备工作目录', finished_at=None)
        elif action == 'cancel':
            if task['status'] in ('已合入', '已放弃', '待合入'):
                reject(409, 'task-state-invalid', 'status', '任务当前状态不支持放弃')
            self.tasks.update(task['id'], status='已放弃', finished_at=now(), error='开发者已放弃修复')
            process = self.tasks.processes.get(task['id'])
            if process:
                terminate_group(process)
            root = self.service.writable_repos[task['project']]
            with self.store.transaction() as db:
                if not task['problems']:
                    for problem in self.unresolved(db, task['project']):
                        db.execute('INSERT OR IGNORE INTO fix_problems(task,batch,scenario,fingerprint,result_id,first_attempt,input_json) VALUES (?,?,?,?,?,0,?)',
                            (task['id'], problem['batch'], problem['scenario'], problem['fingerprint'], problem['result_id'], canonical(problem)))
            if task['worktree'] and Path(task['worktree']).exists():
                git(root, 'worktree', 'remove', '--force', task['worktree'])
            if task['fix_branch']:
                git(root, 'branch', '-D', task['fix_branch'])
        else:
            reject(409, 'task-state-invalid', 'status', '任务当前状态不支持此操作')
        return dict(ok=True, task=self.tasks.read(task['id']))

    def attention(self, key, origin):
        mapping = {'待开发者处理': 'developer-action', '待开发者合入': 'developer-merge', '待提交': 'commit-needed',
                   '待补充': 'cases-needed', '待同步': 'sync-needed', '已中断': 'interrupted'}
        items = []
        for task in self.tasks.list(key)['tasks']:
            if task['status'] not in mapping and not task.get('attention'):
                continue
            url = f"/p/{key}/fixes" if task['kind'] == 'fix' else f"/p/{key}/b/{task['batch']}/merges" if task['kind'] == 'merge' else f"/p/{key}/b/{task['batch']}"
            items.append(dict(kind=mapping.get(task['status'], 'developer-action'), task=task['id'], batch=task['batch'],
                reason=task['error'], url=origin + url, resume='codex resume ' + task['session_id'] if task['session_id'] else ''))
        return dict(ok=True, items=items)

    def unresolved(self, db, key):
        problems = []
        for batch in db.execute('SELECT * FROM batches WHERE project=? ORDER BY id', (key,)):
            for scene in calculate(db, batch)['scenarios']:
                rows = [r for r in (scene['latest_manual'], scene['latest_agent']) if r]
                latest = max(rows, key=lambda r: r['number'], default=None)
                if not latest or not (latest['kind'] == 'manual' and latest['status'] in ('fail', 'blocked') and latest['finding'] or
                                      latest['kind'] == 'agent' and latest['status'] == 'fail'):
                    continue
                suppressed = db.execute("SELECT 1 FROM fix_problems p JOIN tasks t ON t.id=p.task WHERE t.project=? "
                    "AND p.batch=? AND p.scenario=? AND p.fingerprint=? AND p.result_id=? "
                    "AND (t.status='已放弃' OR p.category NOT IN ('','product')) LIMIT 1",
                    (key, batch['name'], scene['scenario'], scene['fingerprint'], latest['id'])).fetchone()
                if suppressed:
                    continue
                source = 'manual' if latest['kind'] == 'manual' else 'check' if latest['regression_check'] else 'agent'
                problems.append(dict(batch=batch['name'], scenario=scene['scenario'], fingerprint=scene['fingerprint'],
                    result_id=latest['id'], source=source, scene=scene['body'], latest_result=latest,
                    previous_agent=scene['latest_agent'], defect_url=latest['finding'] if source == 'manual' else '',
                    evidence=[dict(e, path=str(self.store.data.resolve() / 'evidence' / e['sha256'][:2] / e['sha256'])) for e in latest['evidence']]))
        return problems

    def discover(self):
        if time.monotonic() < self.next_scan:
            return
        self.next_scan = time.monotonic() + 5
        with self.store.transaction() as db:
            for key, root in self.service.writable_repos.items():
                if db.execute("SELECT 1 FROM fix_tasks f JOIN tasks t ON t.id=f.task WHERE repository=? "
                              "AND status NOT IN ('已合入','已放弃')", (str(root),)).fetchone():
                    continue
                if not self.unresolved(db, key):
                    continue
                task_id = db.execute("INSERT INTO tasks(kind,project,batch,status,trigger,input_digest,input_json,created_at,attempt,error) "
                    "VALUES ('fix',?,'','排队中','未解决问题','','{}',?,0,'等待开始修复')", (key, now())).lastrowid
                db.execute('INSERT INTO fix_tasks(task,repository) VALUES (?,?)', (task_id, str(root)))
                if any(self.service.writable_repos[r[0]] == root for r in db.execute("SELECT project FROM tasks WHERE kind='execute' AND status='运行中'")):
                    db.execute('UPDATE tasks SET error=? WHERE id=?', ('等待同一仓库的执行任务结束', task_id))

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
                for field in ('result', 'validation', 'added', 'interruptions'):
                    attempt[field] = json.loads(attempt.pop(field + '_json'))
                task['attempts'].append(attempt)
            task['candidates'] = []
            for row in db.execute('SELECT * FROM fix_candidates WHERE task=? ORDER BY attempt,validation,batch', (task['id'],)):
                candidate = dict(row)
                for field in ('results', 'evidence'):
                    candidate[field] = json.loads(candidate.pop(field + '_json'))
                task['candidates'].append(candidate)
        task['attention'] = [p for p in task['problems'] if p['category'] not in ('', 'product')]
        return task

    def list(self, key):
        with self.store.connection() as db:
            if not db.execute('SELECT 1 FROM projects WHERE key=?', (key,)).fetchone():
                reject(404, 'project-not-found', 'project', '项目不存在')
            ids = [r[0] for r in db.execute("SELECT id FROM tasks WHERE kind='fix' AND project=? ORDER BY id", (key,))] if key in self.service.writable_repos else []
        return dict(ok=True, tasks=[self.tasks.read(i) for i in ids])
