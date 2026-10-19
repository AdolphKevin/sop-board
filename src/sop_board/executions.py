"""执行任务的输入、结果核对与登记；产品与场景一并绑定到固定快照。"""
import json
from importlib.resources import files
from pathlib import Path
from string import Template
import os
import subprocess
import sqlite3

import yaml

from .errors import APIError, reject
from .repositories import git, product
from .service import canonical, now
from .statistics import calculate
from .tasks import codex_environment, codex_available, terminate_group, judgment_object, invalid_judgment_constant
from .validation import validate_round


class Executions:
    def __init__(self, tasks):
        self.tasks, self.service, self.store = tasks, tasks.service, tasks.store
        from .candidates import Candidates
        self.candidates = Candidates(tasks)

    def enqueue(self, key, name, scope=None, trigger='开发者发起'):
        with self.tasks.lock:
            manual = scope is None
            if manual:
                declarations = self.service.entries(key)
                if not declarations['ok']:
                    raise APIError(409, 'entries-invalid', declarations['problems'])
            with self.store.transaction() as db:
                batch = self.service.batch(db, key, name)
                if manual:
                    root = self.service.writable_repos[key]
                    entry = root / ('.tw/config.json' if declarations['source'] == 'tw-config' else 'docs/sop/entries.yaml')
                    current = product(root, root / batch['path'], entry)['product_digest']
                    db.execute('UPDATE batches SET product_digest=? WHERE id=?', (current, batch['id']))
                    batch = self.service.batch(db, key, name)
                    scope = []
                    details = {s['scenario']: s for s in calculate(db, batch)['scenarios']}
                    version = self.service.latest_version(db, batch)
                    document = yaml.safe_load(version['content']) if version else {'sops': []}
                    for sid in [s['id'] for n in document['sops'] for s in n['scenarios']]:
                        scene = details[sid]
                        result = scene['latest_agent']
                        latest = max((r for r in (result, scene['latest_manual']) if r), key=lambda r:r['number'], default=None)
                        prior = db.execute('SELECT 1 FROM scenarios WHERE batch=? AND scenario=? AND fingerprint!=? LIMIT 1',
                            (batch['id'], sid, scene['fingerprint'])).fetchone()
                        reason = (('changed' if prior else 'new') if latest is None else 'failed' if latest['status'] == 'fail' else
                                  'source-changed' if latest is result and result['status'] == 'pass' and result['product_digest'] != batch['product_digest']
                                  and not (scene['regression'] or {}).get('check') else None)
                        if reason:
                            scope.append(dict(scenario=sid, reason=reason))
                if not scope:
                    if manual:
                        reject(409, 'nothing-to-execute', 'scenarios', '没有待执行场景')
                    return None
                previous = db.execute("SELECT id,scope_json FROM tasks WHERE kind='execute' AND project=? AND batch=? "
                    "AND status IN ('排队中','待提交','等待浏览器') ORDER BY id LIMIT 1", (key, name)).fetchone()
                if previous:
                    combined = {s['scenario']: s for s in json.loads(previous['scope_json'])}
                    combined.update({s['scenario']: s for s in scope})
                    task_id = previous['id']
                    db.execute('UPDATE tasks SET scope_json=? WHERE id=?', (canonical(list(combined.values())), task_id))
                else:
                    task_id = db.execute("INSERT INTO tasks(kind,project,batch,status,trigger,input_digest,input_json,scope_json,created_at) "
                        "VALUES ('execute',?,?,'排队中',?,'','{}',?,?)", (key, name, trigger, canonical(scope), now())).lastrowid
        return dict(ok=True, task=self.tasks.read(task_id))

    def inputs(self, task, scenes, declarations, worktree):
        directory = self.tasks.directory(task['id'])
        document = json.loads(task['input_json'])['document']
        selected = {s['id'] for s in scenes}
        document['sops'] = [dict(n, scenarios=[s for s in n['scenarios'] if s['id'] in selected])
                            for n in document['sops'] if any(s['id'] in selected for s in n['scenarios'])]
        (directory / 'scenarios.yaml').write_text(yaml.safe_dump(document, allow_unicode=True, sort_keys=False), encoding='utf-8')
        (directory / 'entries.json').write_text(canonical(declarations), encoding='utf-8')
        (directory / 'pending.json').write_text(canonical([s for s in json.loads(task['scope_json']) if s['scenario'] in selected]), encoding='utf-8')
        (directory / 'evidence').mkdir(exist_ok=True)
        prompt = Template(files('sop_board').joinpath('prompts/execute.md').read_text(encoding='utf-8')).substitute(
            task_directory=directory, repository=self.service.writable_repos[task['project']], candidate_directory=worktree or '开发者运行中的环境',
            scenario_file=directory / 'scenarios.yaml', entries_file=directory / 'entries.json', pending_file=directory / 'pending.json',
            snapshot_file=directory / 'snapshot.json', skills_directory=os.environ.get('SOP_BOARD_SKILLS_DIR', str(Path.home() / '.agents/skills')))
        (directory / 'prompt.md').write_text(prompt, encoding='utf-8')
        return prompt

    def read_results(self, task, scenes, session, batch=None):
        directory = self.tasks.directory(task['id'])
        path = directory / 'results.jsonl'
        if not path.is_file() or path.is_symlink():
            raise ValueError('results.jsonl 不存在')
        rows = [json.loads(line, object_pairs_hook=judgment_object, parse_constant=invalid_judgment_constant)
                for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]
        selected = {s['id'] for s in scenes}
        seen = set()
        snapshot = json.loads(task['snapshot_json'])
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError('结果行应为对象')
            if batch is not None and row.get('batch') != batch:
                continue
            sid = row.get('scenario')
            if not isinstance(sid, str) or sid not in selected:
                raise ValueError(f'{sid} 不在本轮范围内')
            if sid in seen:
                raise ValueError(f'{sid} 有重复的结果行')
            seen.add(sid)
            if row.get('status') in ('blocked', 'skip') and not row.get('finding'):
                raise ValueError(f"{sid} 为 {row['status']} 而缺少 finding")
            evidence = row.get('evidence')
            if not isinstance(evidence, list):
                raise ValueError(f'{sid} evidence 应为路径数组')
            prepared = []
            for name in evidence:
                if not isinstance(name, str):
                    raise ValueError('证据路径应为字符串')
                p = directory / name
                evidence_root = directory / 'evidence'
                if Path(name).is_absolute() or '..' in Path(name).parts or not name.startswith('evidence/') or evidence_root.is_symlink() or directory not in evidence_root.resolve().parents or evidence_root.resolve() not in p.resolve().parents:
                    raise ValueError(f'{sid} 证据路径不在 evidence/ 下')
                if not p.is_file():
                    raise ValueError(f'{sid} 证据文件不存在：{name}')
                prepared.append(p)
            row.update(fingerprint=snapshot['scenarios'][sid], executor=session, session=str(task['id']), evidence=prepared)
        missing = selected - seen
        if missing:
            raise ValueError('、'.join(sorted(missing)) + ' 缺少结果行')
        return rows if batch is None else [row for row in rows if row.get('batch') == batch]

    def execute(self, task):
        directory = self.tasks.directory(task['id'])
        root = self.service.writable_repos[task['project']]
        worktree = self.store.data.resolve() / 'worktrees' / str(task['id'])
        error = void_reason = session = ''
        rounds = []
        process = None
        unavailable = False
        check_worktree = None
        collected = []
        try:
            snapshot = json.loads(task['snapshot_json'])
            inputs = json.loads(task['input_json'])
            scenes, declarations = inputs['scenes'], inputs['declarations']
            with self.tasks.lock:
                self.tasks.starting = None
            guarded = [s for s in scenes if (s.get('regression') or {}).get('check')]
            agent_scenes = [s for s in scenes if not (s.get('regression') or {}).get('check')]
            needs_tree = any(declarations['entries'][s['verify']]['candidate'] != 'in-place' for s in agent_scenes)
            if needs_tree:
                worktree.parent.mkdir(parents=True, exist_ok=True)
                git(root, 'worktree', 'add', '--detach', str(worktree), snapshot['commit'])
            if guarded:
                if needs_tree:
                    check_worktree = worktree
                else:
                    check_worktree = worktree.with_name(worktree.name + '-check')
                    check_worktree.parent.mkdir(parents=True, exist_ok=True)
                    git(root, 'worktree', 'add', '--detach', str(check_worktree), snapshot['commit'])
                collected = self.tasks.checks.collect(task, check_worktree, guarded, declarations)
                if collected is None:
                    collected = []
                    return
            prepared = self.candidates.prepare(task, agent_scenes, declarations, worktree if needs_tree else None)
            if prepared is None:
                return
            selected, rows, environment, targets = prepared
            prompt = self.inputs(task, selected, dict(declarations, targets=targets), worktree if needs_tree else None)
            if selected:
                error = codex_available()
                if error:
                    unavailable = True
                    return
                with self.tasks.lock:
                    if self.tasks.stopping.is_set() or self.tasks.read(task['id'])['status'] != '运行中':
                        return
                    process = self.tasks.launcher(directory, prompt, sandbox='workspace-write', environment=environment)
                    self.tasks.processes[task['id']] = process
                try:
                    code = process.wait(timeout=self.tasks.timeout)
                except subprocess.TimeoutExpired:
                    raise ValueError(f'Codex 运行超时（{self.tasks.timeout:g} 秒）')
            else:
                code = 0
            void_reason = self.tasks.checks.changes(task, snapshot)
            if void_reason:
                return
            if code:
                raise ValueError(f'Codex 退出码 {code}')
            if selected:
                for line in (directory / 'events.jsonl').read_text(encoding='utf-8').splitlines():
                    try:
                        event = json.loads(line)
                        if isinstance(event, dict) and isinstance(event.get('thread_id'), str) and event.get('type') == 'thread.started':
                            session = event['thread_id']
                            break
                    except ValueError:
                        continue
                if not session:
                    raise ValueError('Codex 没有返回会话编号')
                rows.extend(self.read_results(task, selected, session))
            for row in rows:
                row['fingerprint'] = snapshot['scenarios'][row['scenario']]
                scene = next(s for s in scenes if s['id'] == row['scenario'])
                if declarations['entries'][scene['verify']]['candidate'] == 'in-place':
                    row['note'] = '运行环境按 HEAD 记录，未经核实'
            with self.tasks.lock, self.store.transaction() as db:
                if self.tasks.stopping.is_set() or self.tasks.read(task['id'])['status'] != '运行中':
                    return
                void_reason = self.tasks.checks.changes(task, snapshot)
                if void_reason:
                    return
                batch = self.service.batch(db, task['project'], task['batch'])
                version = self.service.latest_version(db, batch)
                try:
                    implementer = git(root, 'config', 'user.name').decode().strip()
                except subprocess.CalledProcessError:
                    implementer = ''
                for check_name, check_rows, evidence in collected:
                    for row in check_rows:
                        row['evidence'] = evidence
                groups = [(None, rows)] if rows else []
                groups += [(name, results) for name, results, _ in collected]
                bodies = []
                for name, results in groups:
                    for row in results:
                        row['evidence'] = [dict(name=p.name, sha256=self.store.save_evidence(db, p.read_bytes(), p.name)['sha256']) for p in row['evidence']]
                    body = dict(idempotency_key=f"execute-{task['id']}-{task['attempt']}-{name or 'agent'}", scenario_file_sha256=snapshot['scenario_file_sha256'],
                        submitted_by='回归检查 ' + name if name else session or '入口准备',
                        product=dict(commit=snapshot['commit'], digest=snapshot['product_digest'], dirty=False), results=results)
                    if implementer:
                        body['implementer'] = implementer
                    validate_round(db, body)
                    bodies.append((name, body))
                void_reason = self.tasks.checks.changes(task, snapshot)
                if void_reason:
                    return
                registered = [self.service.insert_round(db, batch, version, body, regression_check=name) for name, body in bodies]
            rounds = registered
        except FileNotFoundError as problem:
            error = f'Codex 启动失败：{problem}'
            unavailable = process is None
        except APIError as problem:
            error = '；'.join(p['problem'] for p in problem.problems)
        except (OSError, ValueError, KeyError, TypeError, RecursionError, sqlite3.Error, subprocess.SubprocessError) as problem:
            error = str(problem)
        finally:
            if process:
                terminate_group(process)
            self.candidates.close(task['id'])
            with self.tasks.lock:
                if not rounds and not self.tasks.stopping.is_set() and self.tasks.read(task['id'])['status'] == '运行中' and not void_reason:
                    try:
                        void_reason = self.tasks.checks.changes(task, json.loads(task['snapshot_json']))
                    except (OSError, ValueError, KeyError, APIError, subprocess.SubprocessError) as problem:
                        void_reason = f'无法复核快照：{problem}'
            for path in {worktree, check_worktree} - {None}:
                if path.exists():
                    try:
                        git(root, 'worktree', 'remove', '--force', str(path))
                    except (OSError, subprocess.SubprocessError) as problem:
                        error = error or f'无法清理检出目录：{problem}'
            with self.tasks.lock:
                self.tasks.processes.pop(task['id'], None)
                if self.tasks.starting == task['id']:
                    self.tasks.starting = None
                if self.tasks.browser_owner == task['id']:
                    self.tasks.browser_owner = None
                if not self.tasks.stopping.is_set() and self.tasks.read(task['id'])['status'] == '运行中':
                    check = dict(ok=not error and not void_reason, problems=[error] if error else [], checks=[
                        dict(name=name, status='fail' if any(r['status'] == 'fail' for r in results) else results[0]['status'])
                        for name, results, _ in collected])
                    (directory / 'check.json').write_text(canonical(check), encoding='utf-8')
                    self.tasks.update(task['id'], status='已作废' if void_reason else 'Codex 不可用' if unavailable else '失败' if error else '已完成',
                        finished_at=now(), error=error, session_id=session or None, void_reason=void_reason, rounds_json=canonical(rounds), result_json=canonical(check))
