"""固定工作区快照，在已就绪的适配环境中执行并登记结果。"""
import hashlib
from importlib.resources import files
import json
from pathlib import Path
from string import Template
import sqlite3
import subprocess
import time

import yaml

from .errors import APIError, reject
from .repositories import git
from .service import canonical, fingerprint, now
from .statistics import calculate
from .tasks import terminate_group, judgment_object, invalid_judgment_constant, codex_failure
from .validation import validate_round


class Executions:
    def __init__(self, tasks):
        self.tasks, self.service, self.store = tasks, tasks.service, tasks.store
        self.next_review = {}

    def enqueue(self, key, name, scope=None, trigger='开发者发起'):
        with self.tasks.lock:
            manual = scope is None
            if manual:
                self.service.require_ready(key)
            with self.store.transaction() as db:
                batch = self.service.batch(db, key, name)
                version = self.service.latest_version(db, batch)
                document = yaml.safe_load(version['content']) if version else {'sops': []}
                if manual:
                    product = self.service.workspace.product(self.service.workspace.aliases(document))
                    db.execute('UPDATE batches SET product_digest=? WHERE id=?', (product['product_digest'], batch['id']))
                    batch = self.service.batch(db, key, name)
                    scope = []
                    for scene in calculate(db, batch)['scenarios']:
                        agent = scene['latest_agent']
                        latest = max((r for r in (agent, scene['latest_manual']) if r), key=lambda r: r['number'], default=None)
                        prior = db.execute('SELECT 1 FROM scenarios WHERE batch=? AND scenario=? AND fingerprint!=? LIMIT 1',
                            (batch['id'], scene['scenario'], scene['fingerprint'])).fetchone()
                        reason = (('changed' if prior else 'new') if latest is None else 'failed' if latest['status'] == 'fail' else
                                  'source-changed' if agent and agent['status'] == 'pass' and latest['status'] == 'pass' and
                                  agent['product_digest'] != batch['product_digest'] else None)
                        if reason:
                            scope.append(dict(scenario=scene['scenario'], reason=reason))
                if not scope:
                    if manual:
                        reject(409, 'nothing-to-execute', 'scenarios', '没有待执行场景')
                    return None
                previous = db.execute("SELECT id,scope_json FROM tasks WHERE kind='execute' AND workspace=? AND batch=? "
                    "AND status IN ('排队中','待提交','等待浏览器') ORDER BY id LIMIT 1", (key, name)).fetchone()
                if previous:
                    combined = {s['scenario']: s for s in json.loads(previous['scope_json'])}
                    combined.update({s['scenario']: s for s in scope})
                    task_id = previous['id']
                    db.execute('UPDATE tasks SET scope_json=? WHERE id=?', (canonical(list(combined.values())), task_id))
                else:
                    task_id = db.execute("INSERT INTO tasks(kind,workspace,batch,status,trigger,input_digest,input_json,scope_json,created_at) "
                        "VALUES ('execute',?,?,'排队中',?,'','{}',?,?)", (key, name, trigger, canonical(scope), now())).lastrowid
        return dict(ok=True, task=self.tasks.read(task_id))

    def freeze(self, task):
        workspace = self.service.workspace
        with self.store.connection() as db:
            batch = self.service.batch(db, task['workspace'], task['batch'])
            version = self.service.latest_version(db, batch)
            if not version:
                raise ValueError('批次尚无场景')
            document = yaml.safe_load(version['content'])
            details = {s['scenario']: s for s in calculate(db, batch)['scenarios']}
            cases = self.service.case_union(db, batch)
        scope = {s['scenario']: s['reason'] for s in json.loads(task['scope_json'])}
        scenes = [s for n in document['sops'] for s in n['scenarios'] if s['id'] in scope]
        if set(scope) != {s['id'] for s in scenes}:
            raise ValueError('执行范围中的场景已移除')
        aliases = workspace.aliases(document)
        product = workspace.product(aliases)
        source = workspace.scenario_path(task['batch'])
        snapshot = dict(scenario_file_sha256=version['sha256'], scenarios={s['id']: fingerprint(s) for s in scenes},
                        config_digest=workspace.config_digest, cases_digest=hashlib.sha256(canonical(cases).encode()).hexdigest(),
                        source_sha256=hashlib.sha256(source.read_bytes()).hexdigest() if source.is_file() else '', **product)
        previous = {sid: details[sid]['latest_agent'] for sid in scope if details[sid]['latest_agent']}
        inputs = dict(document=document, scenes=scenes, entries=workspace.entries, aliases=aliases, previous=previous, cases=cases)
        return snapshot, inputs

    def prepare(self, task):
        if time.monotonic() < self.next_review.get(task['id'], 0):
            return False
        self.next_review[task['id']] = time.monotonic() + 5
        if not self.service.environment.approved():
            self.tasks.update(task['id'], status='排队中', error='尚未搭建或自检未通过，等待工作区就绪')
            return False
        declarations = self.service.entries(task['workspace'])
        if not declarations['ok']:
            self.tasks.update(task['id'], status='排队中', error='入口声明不合格')
            return False
        snapshot, inputs = self.freeze(task)
        if snapshot['dirty']:
            self.tasks.update(task['id'], status='待提交', error='工作区有未提交的产品改动：' + '、'.join(snapshot['changed'][:10]))
            return False
        directory = self.tasks.directory(task['id'])
        directory.mkdir(parents=True, exist_ok=True)
        (directory / 'snapshot.json').write_text(canonical(snapshot), encoding='utf-8')
        task['snapshot_json'], task['input_json'] = canonical(snapshot), canonical(inputs)
        self.tasks.update(task['id'], snapshot_json=task['snapshot_json'], input_json=task['input_json'], error='')
        return True

    def changes(self, task, snapshot):
        self.service.workspace.reload()
        if not self.service.environment.approved() or snapshot['config_digest'] != self.service.workspace.config_digest:
            return '工作区配置或自检状态变化'
        current, _ = self.freeze(task)
        names = dict(scenario_file_sha256='场景版本', scenarios='场景内容', config_digest='工作区配置',
                     cases_digest='用例并集', source_sha256='工作区场景文件', repos='仓库提交或产品内容', product_digest='产品内容')
        changed = [label for key, label in names.items() if current[key] != snapshot[key]]
        return '、'.join(changed) + '变化' if changed else ''

    def inputs(self, task, inputs, environment):
        directory = self.tasks.directory(task['id'])
        selected = {s['id'] for s in inputs['scenes']}
        document = dict(inputs['document'], sops=[dict(n, scenarios=[s for s in n['scenarios'] if s['id'] in selected])
                         for n in inputs['document']['sops'] if any(s['id'] in selected for s in n['scenarios'])])
        (directory / 'scenarios.yaml').write_text(yaml.safe_dump(document, allow_unicode=True, sort_keys=False), encoding='utf-8')
        addresses = {}
        for name, entry in inputs['entries'].items():
            variable = entry.get('address_env')
            if variable:
                if variable not in environment:
                    raise ValueError('入口 ' + name + ' 缺少地址环境变量 ' + variable)
                addresses[name] = environment[variable]
        (directory / 'entries.json').write_text(canonical(dict(entries=inputs['entries'], addresses=addresses)), encoding='utf-8')
        referenced = {ref for scene in inputs['scenes'] for ref in scene.get('refs', [])}
        (directory / 'cases.json').write_text(canonical([case for case in inputs['cases'] if case['id'] in referenced]), encoding='utf-8')
        (directory / 'pending.json').write_text(task['scope_json'], encoding='utf-8')
        (directory / 'previous-results.json').write_text(canonical(inputs['previous']), encoding='utf-8')
        (directory / 'evidence').mkdir(exist_ok=True)
        snapshot = json.loads(task['snapshot_json'])
        changes = {}
        for alias in inputs['aliases']:
            root = self.service.workspace.repos[alias]
            prior = {}
            for row in inputs['previous'].values():
                with self.store.connection() as db:
                    commit = db.execute('SELECT product_commit FROM rounds WHERE id=?', (row['round_id'],)).fetchone()[0]
                for item in commit.split(', '):
                    name, separator, sha = item.partition(':')
                    if separator and name == alias:
                        prior[sha] = True
            changes[alias] = dict(current=snapshot['repos'][alias]['commit'], directory=str(root), previous=[])
            for sha in prior:
                try:
                    diff = git(root, 'diff', '--name-status', sha, snapshot['repos'][alias]['commit']).decode()
                except subprocess.SubprocessError:
                    diff = '旧提交不可读取，不能仅据此沿用结果'
                changes[alias]['previous'].append(dict(commit=sha, files=diff))
        (directory / 'changes.json').write_text(canonical(changes), encoding='utf-8')
        prompt = Template(files('sop_board').joinpath('prompts/execute.md').read_text(encoding='utf-8')).substitute(task_directory=directory)
        (directory / 'prompt.md').write_text(prompt, encoding='utf-8')
        return prompt

    def read_results(self, task, scenes, session, batch=None):
        directory = self.tasks.directory(task['id'])
        path = directory / 'results.jsonl'
        if not path.is_file() or path.is_symlink():
            raise ValueError('results.jsonl 不存在')
        raw = [json.loads(line, object_pairs_hook=judgment_object, parse_constant=invalid_judgment_constant)
               for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]
        selected = {s['id'] for s in scenes}
        snapshot = json.loads(task['snapshot_json'])
        inputs = json.loads(task.get('input_json', '{}'))
        scope = {s['scenario']: s['reason'] for s in json.loads(task.get('scope_json', '[]'))}
        rows, seen = [], set()
        for row in raw:
            if not isinstance(row, dict):
                raise ValueError('结果行应为对象')
            if batch is not None and row.get('batch') != batch:
                continue
            sid = row.get('scenario')
            if not isinstance(sid, str) or sid not in selected or sid in seen:
                raise ValueError('结果重复或不在本轮范围内：' + str(sid))
            seen.add(sid)
            evidence = row.get('evidence')
            if not isinstance(evidence, list):
                raise ValueError(sid + ' evidence 应为路径数组')
            prepared = []
            for name in evidence:
                if not isinstance(name, str):
                    raise ValueError('证据路径应为字符串')
                p = directory / name
                base = directory / 'evidence'
                if Path(name).is_absolute() or '..' in Path(name).parts or not name.startswith('evidence/') or base.is_symlink() or not p.resolve().is_relative_to(base.resolve()) or not base.resolve().is_relative_to(directory):
                    raise ValueError(sid + ' 证据路径不在 evidence/ 下')
                if not p.is_file():
                    raise ValueError(sid + ' 证据文件不存在')
                prepared.append(p)
            note = row.get('note') or ''
            if isinstance(note, str) and note.startswith('沿用：'):
                previous = inputs.get('previous', {}).get(sid)
                if (scope.get(sid) != 'source-changed' or not previous or previous['status'] != 'pass'
                        or row.get('status') != 'pass' or not note[3:].strip()
                        or previous['fingerprint'] != snapshot['scenarios'][sid]):
                    raise ValueError(sid + ' 不符合沿用条件，须重跑；沿用必须保留原通过状态并说明理由')
                prepared += previous['evidence']
            row.update(fingerprint=snapshot['scenarios'][sid], executor=session, session=str(task['id']), evidence=prepared)
            rows.append(row)
        if selected - seen:
            raise ValueError('、'.join(sorted(selected - seen)) + ' 缺少结果行')
        return rows

    def execute(self, task):
        directory = self.tasks.directory(task['id'])
        error = void_reason = session = ''
        rounds = []
        rows = []
        process = None
        snapshot = json.loads(task['snapshot_json'])
        inputs = json.loads(task['input_json'])
        try:
            with self.tasks.lock:
                self.tasks.starting = None
            with self.service.environment.use_baseline() as environment:
                if self.tasks.stopping.is_set() or self.tasks.read(task['id'])['status'] != '运行中':
                    return
                void_reason = self.changes(task, snapshot)
                if void_reason:
                    return
                prompt = self.inputs(task, inputs, environment)
                with self.tasks.lock:
                    process = self.tasks.launcher(directory, prompt, sandbox='workspace-write', environment=environment)
                    self.tasks.processes[task['id']] = process
                try:
                    code = process.wait(timeout=self.tasks.timeout)
                except subprocess.TimeoutExpired:
                    terminate_group(process)
                    raise ValueError(f'Codex 运行超时（{self.tasks.timeout:g} 秒）')
                if code:
                    raise ValueError(codex_failure(directory, code))
                for line in (directory / 'events.jsonl').read_text(encoding='utf-8').splitlines():
                    try:
                        event = json.loads(line)
                        if isinstance(event, dict) and event.get('type') == 'thread.started' and isinstance(event.get('thread_id'), str):
                            session = event['thread_id']
                            break
                    except ValueError:
                        continue
                if not session:
                    raise ValueError('Codex 没有返回会话编号')
                rows = self.read_results(task, inputs['scenes'], session)
                with self.tasks.lock, self.store.transaction() as db:
                    if self.tasks.stopping.is_set() or self.tasks.read(task['id'])['status'] != '运行中':
                        return
                    void_reason = self.changes(task, snapshot)
                    if void_reason:
                        return
                    batch = self.service.batch(db, task['workspace'], task['batch'])
                    version = self.service.latest_version(db, batch)
                    for row in rows:
                        row['evidence'] = [p if isinstance(p, dict) else dict(name=p.name,
                            sha256=self.store.save_evidence(db, p.read_bytes(), p.name)['sha256']) for p in row['evidence']]
                    body = dict(idempotency_key=f"execute-{task['id']}-{task['attempt']}", scenario_file_sha256=snapshot['scenario_file_sha256'],
                        submitted_by=session, product=dict(commit=snapshot['commit'], digest=snapshot['product_digest'], dirty=False), results=rows)
                    validate_round(db, body)
                    rounds = [self.service.insert_round(db, batch, version, body)]
        except APIError as problem:
            error = '；'.join(p['problem'] for p in problem.problems)
        except (OSError, ValueError, KeyError, TypeError, RecursionError, sqlite3.Error, subprocess.SubprocessError) as problem:
            error = str(problem)
        finally:
            if process:
                terminate_group(process)
            with self.tasks.lock:
                self.tasks.processes.pop(task['id'], None)
                if self.tasks.starting == task['id']:
                    self.tasks.starting = None
                if self.tasks.browser_owner == task['id']:
                    self.tasks.browser_owner = None
                if not self.tasks.stopping.is_set() and self.tasks.read(task['id'])['status'] == '运行中':
                    result = dict(ok=not error and not void_reason, problems=[error] if error else [],
                                  reused_count=sum(isinstance(r.get('note'), str) and r['note'].startswith('沿用：') for r in rows))
                    (directory / 'result.json').write_text(canonical(result), encoding='utf-8')
                    self.tasks.update(task['id'], status='已作废' if void_reason else '失败' if error else '已完成',
                        finished_at=now(), error=error, session_id=session or None, void_reason=void_reason,
                        rounds_json=canonical(rounds), result_json=canonical(result))
