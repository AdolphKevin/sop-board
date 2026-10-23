"""固定工作区快照，在已就绪的适配环境中执行并登记结果。"""

import hashlib
from contextlib import nullcontext
from importlib.resources import files
import json
from pathlib import Path
from string import Template
import sqlite3
import subprocess

import yaml

from .kernel import (InputChanged, EXECUTION_QUEUE, OUTPUT_ACTIVE, S, codex_failure, session_id, sql_states,
                     terminate_group, wait_codex)

from .errors import APIError, reject
from .repositories import git
from .service import canonical, fingerprint, now
from .statistics import calculate, pending_scope
from .merge_state import read_holds, held_scenarios
from .tasks import judgment_object, invalid_judgment_constant
from .validation import validate_round
from .playwright import driver_skill, write_scope
from .credentials import append_blocked, blocked_results, names as credential_names


class Executions:
    def __init__(self, tasks):
        self.tasks, self.service, self.store = tasks, tasks.service, tasks.store

    def waiting_merge(self, db, key, name):
        row = db.execute("SELECT id FROM tasks WHERE kind='merge' AND workspace=? AND batch=? "
                         f'AND status IN ({sql_states(OUTPUT_ACTIVE)}) ORDER BY id LIMIT 1', (key, name)).fetchone()
        return row['id'] if row else None

    def enqueue(self, key, name, scope=None, trigger='开发者发起', selection='pending', *, selected=None, session=False):
        with self.tasks.lock:
            manual = scope is None
            if manual:
                self.service.require_ready(key)
            with self.store.transaction() as db:
                if not manual and not self.service.auto_execute_enabled(db):
                    return None
                batch = self.service.batch(db, key, name)
                merge = None if session else self.waiting_merge(db, key, name)
                version = self.service.latest_version(db, batch)
                document = yaml.safe_load(version['content']) if version else {'sops': []}
                if manual:
                    product = self.service.workspace.product(self.service.workspace.aliases(document))
                    db.execute('UPDATE batches SET product_digest=? WHERE id=?', (product['product_digest'], batch['id']))
                    batch = self.service.batch(db, key, name)
                    summary = calculate(db, batch)
                    if selected is not None:
                        available = {s['scenario'] for s in summary['scenarios'] if not s.get('held')}
                        if set(selected) - available:
                            reject(409, 'scenario-unavailable', 'scope', '场景不存在或待补充：' + '、'.join(sorted(set(selected) - available)))
                        pending = {s['scenario']: s for s in pending_scope(db, batch, summary, product['product_digest'])}
                        scope = [pending.get(sid, dict(scenario=sid, reason='rerun')) for sid in selected]
                        selection = 'explicit'
                    else:
                        scope = ([dict(scenario=s['scenario'], reason='rerun') for s in summary['scenarios'] if not s.get('held')]
                                 if selection == 'all' else pending_scope(db, batch, summary, product['product_digest']))
                if not scope and not (manual and merge):
                    if manual:
                        reject(409, 'nothing-to-execute', 'scenarios', '尚无场景' if selection == 'all' else '没有待执行场景')
                    return None
                previous = db.execute("SELECT id,scope_json,input_json FROM tasks WHERE kind='execute' AND workspace=? AND batch=? "
                    f'AND status IN ({sql_states(EXECUTION_QUEUE)}) ORDER BY id LIMIT 1', (key, name)).fetchone()
                request = dict(selection=selection if manual else 'explicit', refresh_after_merge=bool(merge))
                status, error = (S.WAIT_MERGE, f'等待合并 #{merge}') if merge else (S.QUEUED, '')
                if previous:
                    prior = json.loads(previous['input_json'])
                    if selected is not None and (prior.get('selection') != 'explicit' or
                            {s['scenario'] for s in json.loads(previous['scope_json'])} - set(selected)):
                        reject(409, 'execution-queued', 'scope', f"执行任务 #{previous['id']} 已有其他范围，请先处理该任务后再执行所列场景")
                    request['selection'] = ('all' if 'all' in (prior.get('selection'), request['selection']) else
                                            'pending' if 'pending' in (prior.get('selection'), request['selection']) else 'explicit')
                    request['refresh_after_merge'] |= prior.get('refresh_after_merge', False)
                    combined = {s['scenario']: s for s in json.loads(previous['scope_json'])}
                    for scene in scope:
                        if combined.get(scene['scenario'], {}).get('reason') != 'rerun':
                            combined[scene['scenario']] = scene
                    task_id = previous['id']
                    db.execute('UPDATE tasks SET scope_json=?,input_json=? WHERE id=?',
                               (canonical(list(combined.values())), canonical(dict(prior, **request)), task_id))
                    if merge:
                        db.execute("UPDATE tasks SET status=?,error=?,snapshot_json='{}' WHERE id=?", (status, error, task_id))
                else:
                    task_id = db.execute("INSERT INTO tasks(kind,workspace,batch,status,trigger,input_digest,input_json,scope_json,created_at) "
                        "VALUES ('execute',?,?,?,?,?,?,?,?)",
                        (key, name, status, trigger, '', canonical(request), canonical(scope), now())).lastrowid
                    if error:
                        db.execute('UPDATE tasks SET error=? WHERE id=?', (error, task_id))
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
            held = {h['case'] for h in read_holds(db, batch)['held']}
        scope = {s['scenario']: s['reason'] for s in json.loads(task['scope_json'])}
        scenes = [s for n in document['sops'] for s in n['scenarios'] if s['id'] in scope]
        if set(scope) != {s['id'] for s in scenes}:
            raise ValueError('执行范围中的场景已移除')
        aliases = workspace.aliases(document)
        product = workspace.product(aliases)
        source = workspace.scenario_path(task['batch'])
        snapshot = dict(scenario_file_sha256=version['sha256'], scenarios={s['id']: fingerprint(s) for s in scenes},
                        config_digest=workspace.config_digest, skill_digest=workspace.skill_digest(), cases_digest=hashlib.sha256(canonical(cases).encode()).hexdigest(),
                        source_sha256=hashlib.sha256(source.read_bytes()).hexdigest() if source.is_file() else '', **product)
        previous = {sid: details[sid]['latest_agent'] for sid in scope if details[sid]['latest_agent']}
        request = json.loads(task['input_json'])
        inputs = dict(document=document, scenes=scenes, entries=workspace.execution_entries(), aliases=aliases, previous=previous,
                      cases=[c for c in cases if c['id'] not in held],
                      execution_origin='session' if self.tasks.kernel.claim(task) else 'board',
                      selection=request.get('selection', 'explicit'), refresh_after_merge=False)
        return snapshot, inputs

    @staticmethod
    def counts(rows):
        """沿用与重跑分开计数：重跑是实际执行并得出通过或失败的场景，受阻、跳过不计入两者。"""
        reused = [isinstance(r.get('note'), str) and r['note'].startswith('沿用：') for r in rows]
        return dict(reused_count=sum(reused),
                    rerun_count=sum(not used and r.get('status') in ('pass', 'fail') for used, r in zip(reused, rows)))

    def committed(self, task):
        """被阻挡期间只核对产品是否已无未提交改动；开始前仍按 freeze 完整冻结并核对输入。"""
        workspace = self.service.workspace
        try:
            with self.store.connection() as db:
                version = self.service.latest_version(db, self.service.batch(db, task['workspace'], task['batch']))
            if not version:
                return False
            return not workspace.product(workspace.aliases(yaml.safe_load(version['content'])))['dirty']
        except (OSError, ValueError, KeyError, yaml.YAMLError, APIError, subprocess.SubprocessError):
            return False

    def prepare(self, task):
        with self.store.connection() as db:
            merge = self.waiting_merge(db, task['workspace'], task['batch'])
        if merge:
            request = json.loads(task['input_json'])
            if (task['status'] != S.WAIT_MERGE or task['error'] != f'等待合并 #{merge}'
                    or task['snapshot_json'] != '{}' or not request.get('refresh_after_merge')):
                request['refresh_after_merge'] = True
                self.tasks.update(task['id'], status=S.WAIT_MERGE, error=f'等待合并 #{merge}',
                                  input_json=canonical(request), snapshot_json='{}')
            (self.tasks.directory(task['id']) / 'snapshot.json').unlink(missing_ok=True)
            if self.tasks.browser_owner == task['id']:
                self.tasks.browser_owner = None
            self.tasks.kernel.wake(task['id'])
            return False
        if not self.tasks.kernel.check_due(task, self.tasks.types['execute'].waiting):
            return False
        # 已排队任务可能尚未观察到一次很快结束的合并，准备前仍须排除最新暂缓项。
        with self.store.transaction() as db:
            batch = self.service.batch(db, task['workspace'], task['batch'])
            held = held_scenarios(read_holds(db, batch))
            original_scope = json.loads(task['scope_json'])
            available = [s for s in original_scope if s['scenario'] not in held]
            if available != original_scope:
                task['scope_json'] = canonical(available)
                db.execute('UPDATE tasks SET scope_json=? WHERE id=?', (task['scope_json'], task['id']))
        if original_scope and not available and not json.loads(task['input_json']).get('refresh_after_merge'):
            self.tasks.update(task['id'], status=S.COMPLETED, finished_at=now(), error='执行范围的用例全部暂缓，等待补充')
            return False
        if not self.service.environment.approved():
            self.tasks.update(task['id'], status=S.QUEUED, error='尚未搭建或自检未通过，等待工作区就绪')
            return False
        declarations = self.service.entries(task['workspace'])
        if not declarations['ok']:
            self.tasks.update(task['id'], status=S.QUEUED, error='入口声明不合格')
            return False
        if self.service.environment.state.get('waiting_port'):
            self.tasks.update(task['id'], status=S.QUEUED, error=self.service.environment.state['status'])
            return False
        request = json.loads(task['input_json'])
        if request.get('refresh_after_merge'):
            with self.store.transaction() as db:
                batch = self.service.batch(db, task['workspace'], task['batch'])
                version = self.service.latest_version(db, batch)
                document = yaml.safe_load(version['content']) if version else {'sops': []}
                product = self.service.workspace.product(self.service.workspace.aliases(document))
                db.execute('UPDATE batches SET product_digest=? WHERE id=?', (product['product_digest'], batch['id']))
                batch = self.service.batch(db, task['workspace'], task['batch'])
                summary = calculate(db, batch)
                if request.get('selection') == 'all':
                    scope = [dict(scenario=s['scenario'], reason='rerun') for s in summary['scenarios'] if not s.get('held')]
                else:
                    current = {s['scenario'] for s in summary['scenarios'] if not s.get('held')}
                    scope = ({s['scenario']: s for s in pending_scope(db, batch, summary, product['product_digest'])}
                             if request.get('selection') == 'pending' else {})
                    for scene in json.loads(task['scope_json']):
                        if scene['scenario'] in current and (scene['reason'] == 'rerun' or scene['scenario'] not in scope):
                            scope[scene['scenario']] = scene
                    scope = list(scope.values())
                request['refresh_after_merge'] = False
                task['scope_json'], task['input_json'] = canonical(scope), canonical(request)
                db.execute(f"UPDATE tasks SET scope_json=?,input_json=?,status='{S.QUEUED}',error='' WHERE id=?",
                           (task['scope_json'], task['input_json'], task['id']))
            if not scope:
                self.tasks.update(task['id'], status=S.COMPLETED, finished_at=now(), error='合并结束，当前版本没有待执行场景')
                return False
        snapshot, inputs = self.freeze(task)
        if snapshot['dirty']:
            self.tasks.update(task['id'], status=S.WAIT_COMMIT, error='工作区有未提交的产品改动：' + '、'.join(snapshot['changed'][:10]))
            return False
        directory = self.tasks.directory(task['id'])
        directory.mkdir(parents=True, exist_ok=True)
        (directory / 'snapshot.json').write_text(canonical(snapshot), encoding='utf-8')
        task['snapshot_json'], task['input_json'] = canonical(snapshot), canonical(inputs)
        self.tasks.update(task['id'], snapshot_json=task['snapshot_json'], input_json=task['input_json'], error='')
        return True

    def changes(self, task, snapshot):
        self.service.workspace.reload()
        if self.service.workspace.skill_changed(snapshot):
            return '验证技能变化'
        if not self.service.environment.approved() or snapshot['config_digest'] != self.service.workspace.config_digest:
            return '工作区配置或自检状态变化'
        current, _ = self.freeze(task)
        changed = self.tasks.types['execute'].inputs(task).changed(current)
        return '、'.join(changed) + '变化' if changed else ''

    def materialize(self, task, inputs, environment):
        directory = self.tasks.directory(task['id'])
        selected = {s['id'] for s in inputs['scenes']}
        document = dict(inputs['document'], sops=[dict(n, scenarios=[s for s in n['scenarios'] if s['id'] in selected])
                         for n in inputs['document']['sops'] if any(s['id'] in selected for s in n['scenarios'])])
        (directory / 'scenarios.yaml').write_text(yaml.safe_dump(document, allow_unicode=True, sort_keys=False), encoding='utf-8')
        web_scenes = [s for s in inputs['scenes'] if inputs['entries'][s['verify']]['surface'] == 'web' and inputs['entries'][s['verify']].get('driver') in (None, 'playwright-acceptance')]
        write_scope(directory, task['workspace'], task['batch'], directory / 'scenarios.yaml', web_scenes)
        (directory / 'web-pending.json').write_text(canonical([dict(scenario=s['id']) for s in web_scenes]), encoding='utf-8')
        addresses = {}
        for name, entry in inputs['entries'].items():
            variable = entry.get('address_env')
            if variable and variable not in credential_names(inputs['entries']):
                if not environment.get(variable, '').strip():
                    raise ValueError('入口 ' + name + ' 缺少地址环境变量 ' + variable)
                addresses[name] = environment[variable]
        (directory / 'entries.json').write_text(canonical(dict(entries=inputs['entries'], addresses=addresses)), encoding='utf-8')
        referenced = {ref for scene in inputs['scenes'] for ref in scene.get('refs', [])}
        (directory / 'cases.json').write_text(canonical([case for case in inputs['cases'] if case['id'] in referenced]), encoding='utf-8')
        (directory / 'pending.json').write_text(task['scope_json'], encoding='utf-8')
        (directory / 'previous-results.json').write_text(canonical(inputs['previous']), encoding='utf-8')
        with self.store.connection() as db:
            from .regressions import read_links
            batch = self.service.batch(db, task['workspace'], task['batch'])
            (directory / 'regression-links.json').write_text(canonical(read_links(db, batch, local=True)), encoding='utf-8')
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
            if snapshot.get('dirty'):
                changes[alias]['uncommitted'] = [p[len(alias) + 1:] for p in snapshot['changed'] if p.startswith(alias + '/')]
            for sha in prior:
                try:
                    target = [] if snapshot.get('dirty') else [snapshot['repos'][alias]['commit']]
                    diff = git(root, 'diff', '--name-status', sha, *target, '--').decode()
                except subprocess.SubprocessError:
                    diff = '旧提交不可读取，不能仅据此沿用结果'
                changes[alias]['previous'].append(dict(commit=sha, files=diff))
        (directory / 'changes.json').write_text(canonical(changes), encoding='utf-8')
        origin = inputs.get('execution_origin','session' if self.tasks.kernel.claim(task) else 'board')
        (directory/'origin.json').write_text(canonical(dict(execution_origin=origin)),encoding='utf-8')
        (directory/'repos.json').write_text(canonical({alias:str(path) for alias,path in self.service.workspace.repos.items()}),encoding='utf-8')
        external = origin == 'session'
        prompt = Template(files('sop_board').joinpath('prompts/execute.md').read_text(encoding='utf-8')).substitute(task_directory=directory, skill_path=self.service.workspace.skill,
            environment_preparation=('由执行会话按项目验证技能与手册准备入口与隔离数据；按 entries.json 的 operate、observe 和 evidence 操作。'
                '结束前按手册停止本轮由自己启动的服务进程并核对已退出，只清理本轮获授权的资源，不停止使用者已有的服务或浏览器。'
                '在结果 note 中列出已停止的服务名称、PID 或进程组及停止结果；没有启动服务时写“本轮未启动服务”，停止失败时如实列出仍运行的进程与原因。' if external else
                '看板已经用工作区配置启动了实际环境；按 entries.json 的 operate、observe 和 evidence 操作。'),
            repository_instruction=('地址从 entries.json 的 addresses 取得；各仓库目录从任务目录的 repos.json 按仓库别名读取。' if external else
                '地址从 entries.json 的 addresses 取得；各仓库目录由 SOP_REPO_<别名> 环境变量提供。'),
            environment_origin='执行会话自身' if external else '启动看板',
            environment_restriction='' if external else '不启动或替换环境，',
            playwright_skill=driver_skill() / 'SKILL.md')
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
        changed = {sid for sid in selected if scope.get(sid) == 'source-changed'}
        impact = {}
        if changed:
            label = '、'.join(sorted(changed))
            impact_path = directory / 'impact.json'
            if not impact_path.is_file() or impact_path.is_symlink():
                raise ValueError(label + ' 缺少 impact.json 影响判定')
            try:
                decisions = json.loads(impact_path.read_text(encoding='utf-8'), object_pairs_hook=judgment_object,
                                       parse_constant=invalid_judgment_constant)
            except (OSError, ValueError) as error:
                raise ValueError(label + ' impact.json 不合格：' + str(error)) from error
            if not isinstance(decisions, list):
                raise ValueError(label + ' impact.json 应为数组')
            for decision in decisions:
                if not isinstance(decision, dict):
                    raise ValueError(label + ' impact.json 每项应为对象')
                sid = decision.get('scenario')
                if not isinstance(sid, str) or sid not in changed or sid in impact:
                    raise ValueError('impact.json 场景重复或不在待判定范围内：' + str(sid))
                if type(decision.get('affected')) is not bool or not isinstance(decision.get('reason'), str) or not decision['reason'].strip():
                    raise ValueError(sid + ' impact.json 必须有布尔值 affected 和非空 reason')
                impact[sid] = decision['affected']
            if changed - impact.keys():
                raise ValueError('、'.join(sorted(changed - impact.keys())) + ' 缺少 impact.json 影响判定')
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
            note = row.get('note') or ''
            reused = isinstance(note, str) and note.startswith('沿用：')
            if sid in impact and impact[sid] == reused:
                raise ValueError(sid + ' impact.json 判定与结果不一致：' +
                                 ('判定受影响，必须实际执行' if impact[sid] else '判定不受影响，必须沿用'))
            if scope.get(sid) == 'rerun' and isinstance(note, str) and note.startswith('沿用：'):
                raise ValueError(sid + ' 须实际执行并保存新证据，不得沿用')
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
            if scope.get(sid) == 'rerun' and row.get('status') in ('pass','fail') and not prepared:
                raise ValueError(sid + ' 须实际执行并保存新证据')
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

    def blocked_impact(self, task, blocked):
        """保留完整影响判定清单；凭据缺失项不能以原通过结果沿用。"""
        if not blocked:
            return
        path = self.tasks.directory(task['id']) / 'impact.json'
        if path.is_symlink():
            raise ValueError('impact.json 不能是符号链接')
        changed = {row['scenario'] for row in json.loads(task['scope_json']) if row['reason'] == 'source-changed'}
        generated = [dict(scenario=row['scenario'], affected=True,
                         reason='缺少凭据，尚无法证明不受影响，待环境就绪后经真实入口执行；' + row['finding'])
                     for row in blocked if row['scenario'] in changed]
        if generated:
            previous = json.loads(path.read_text(encoding='utf-8'), object_pairs_hook=judgment_object,
                                  parse_constant=invalid_judgment_constant) if path.is_file() else []
            expected = changed - {row['scenario'] for row in generated}
            if (not isinstance(previous, list) or any(not isinstance(row, dict) or not isinstance(row.get('scenario'), str) for row in previous)
                    or len(previous) != len(expected) or {row['scenario'] for row in previous} != expected):
                raise ValueError('impact.json 与本次可执行场景的影响判定范围不一致')
            path.write_text(canonical(previous + generated), encoding='utf-8')
        elif not path.exists():
            path.write_text('[]', encoding='utf-8')

    def consume(self, task, session, *, runnable=None, blocked=None, scoped=None, implementer=None, execution_origin='board'):
        snapshot = json.loads(task['snapshot_json'])
        inputs = json.loads(task['input_json'])
        blocked = blocked or []
        runnable = inputs['scenes'] if runnable is None else runnable
        scoped = task if scoped is None else scoped
        with self.tasks.lock:
            receipt = self.tasks.kernel.receipt(task, phase='execute')
            if receipt is not None:
                return dict(rounds=receipt['rounds'], rows=receipt['rows'])
            reason = self.changes(task, snapshot)
            if reason:
                raise InputChanged(reason)
            rows = self.read_results(scoped, runnable, session) if runnable else []
            self.blocked_impact(task, blocked)
            append_blocked(self.tasks.directory(task['id']), blocked)
            rows.extend(dict(row, fingerprint=snapshot['scenarios'][row['scenario']], session=str(task['id'])) for row in blocked)
            if execution_origin == 'session':
                for row in rows:
                    row.update(executor=session, session=session)
            with self.store.transaction() as db:
                if self.tasks.stopping.is_set() or self.tasks.read(task['id'])['status'] not in (S.RUNNING, S.SESSION):
                    return dict(rounds=[], rows=[])
                reason = self.changes(task, snapshot)
                if reason:
                    raise InputChanged(reason)
                batch = self.service.batch(db, task['workspace'], task['batch'])
                version = self.service.latest_version(db, batch)
                key = f"execute-{task['id']}-{task['attempt']}"
                existing = db.execute('SELECT number FROM rounds WHERE batch=? AND idempotency_key=?', (batch['id'], key)).fetchone()
                for row in rows:
                    row['evidence'] = [p if isinstance(p, dict) else dict(name=p.name,
                        sha256=self.store.save_evidence(db, p.read_bytes(), p.name)['sha256']) for p in row['evidence']]
                body = dict(idempotency_key=key, scenario_file_sha256=snapshot['scenario_file_sha256'],
                    submitted_by=session, execution_origin=execution_origin,
                    product=dict(commit=snapshot['commit'], digest=snapshot['product_digest'],
                                 dirty=snapshot['dirty'] if execution_origin == 'session' else False), results=rows)
                if implementer is not None:
                    body['implementer'] = implementer
                if existing:
                    rounds = [existing['number']]
                else:
                    rounds = [self.service.register_round(db, batch, version, body, skill_digest=snapshot['skill_digest'])]
                result = dict(rounds=rounds, rows=rows, session=session)
                self.tasks.kernel.register(task, result, connection=db, phase='execute')
                return result

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
            preflight = self.service.environment.agent_environment(self.service.workspace.repos)
            blocked = blocked_results(inputs['scenes'], inputs['entries'], preflight)
            blocked_ids = {row['scenario'] for row in blocked}
            runnable = [scene for scene in inputs['scenes'] if scene['id'] not in blocked_ids]
            scoped = dict(task, scope_json=canonical([row for row in json.loads(task['scope_json']) if row['scenario'] not in blocked_ids]))
            context = self.service.environment.use_baseline() if runnable else nullcontext(preflight)
            with context as environment:
                if self.tasks.stopping.is_set() or self.tasks.read(task['id'])['status'] != S.RUNNING:
                    return
                void_reason = self.changes(task, snapshot)
                if void_reason:
                    return
                (directory / 'credential-blocked.json').write_text(canonical(blocked), encoding='utf-8')
                if runnable:
                    prompt = self.tasks.types['execute'].prepare(scoped, inputs=dict(inputs, scenes=runnable), environment=environment)
                    with self.tasks.lock:
                        process = self.tasks.kernel.launch(task, directory, prompt, self.tasks.types['execute'].launch(task, environment=environment))
                        self.tasks.processes[task['id']] = process
                    code = wait_codex(process, self.tasks.timeout)
                    if code:
                        raise ValueError(codex_failure(directory, code))
                    session = session_id(directory, '')
                    if not session:
                        raise ValueError('Codex 没有返回会话编号')
                else:
                    session = 'Flow Board 入口检查'
                consumed = self.tasks.types['execute'].consume(task, session, runnable=runnable, blocked=blocked, scoped=scoped)
                rounds, rows = consumed['rounds'], consumed['rows']
        except InputChanged as problem:
            void_reason = str(problem)
        except APIError as problem:
            error = '；'.join(p['problem'] for p in problem.problems)
        except (OSError, ValueError, KeyError, TypeError, RecursionError, sqlite3.Error, subprocess.SubprocessError) as problem:
            error = str(problem)
        finally:
            if process:
                terminate_group(process)
                self.service.workspace.reload()
                if not rounds and self.service.workspace.skill_changed(snapshot):
                    void_reason = '验证技能变化'
                session = session_id(directory, session)
            with self.tasks.lock:
                self.tasks.processes.pop(task['id'], None)
                if self.tasks.starting == task['id']:
                    self.tasks.starting = None
                if self.tasks.browser_owner == task['id']:
                    self.tasks.browser_owner = None
                if not self.tasks.stopping.is_set() and self.tasks.read(task['id'])['status'] == S.RUNNING:
                    result = dict(ok=not error and not void_reason, problems=[error] if error else [], **self.counts(rows))
                    (directory / 'result.json').write_text(canonical(result), encoding='utf-8')
                    self.tasks.update(task['id'], status=S.VOID if void_reason else S.FAILED if error else S.COMPLETED,
                        finished_at=now(), error=error, session_id=session or None, void_reason=void_reason,
                        rounds_json=canonical(rounds), result_json=canonical(result))
