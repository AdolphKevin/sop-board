"""持久任务、Codex 进程入口与经过核对后的合并落地。"""

import hashlib
from contextlib import nullcontext
import json
import re
import os
from pathlib import Path
import sqlite3
import threading
from importlib.resources import files
from urllib.parse import quote

# 旧模块路径仍转导出进程入口；实现只在 kernel 中保留。
from .kernel import (ALL_STATES, Kernel, MERGE_DEDUP, MERGE_RESULTS, OUTPUT_ACTIVE, S, sql_states,
                     parse_timeout, codex_environment, codex_failure, start_codex, resume_codex,
                     codex_available, terminate_group, queue_view)


from .errors import reject, APIError
from .drafts import SCHEMA, assemble, section, schema_problems, normalize_judgment

import yaml
from .service import canonical, now


def judgment_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('最终 JSON 消息含重复字段')
        result[key] = value
    return result


def invalid_judgment_constant(value):
    raise ValueError(f'最终 JSON 消息含非有限数字：{value}')


def read_merge_output(raw):
    result = json.loads(raw, object_pairs_hook=judgment_object, parse_constant=invalid_judgment_constant)
    problems = schema_problems(result)
    if problems:
        raise ValueError('输出模式：' + '；'.join(problems))
    return normalize_judgment(result)


class Tasks:
    def __init__(self, service, launcher=start_codex, timeout=None, *, background=True):
        self.service, self.store, self.launcher = service, service.store, launcher
        service.merge_callback = self.enqueue_merge
        self.lock = service.workspace_lock
        self.stopping = threading.Event()
        self.workers = {}
        self.processes = {}
        self.worktree_environments = {}
        self.browser_owner = None
        self.starting = None
        self.timeout = parse_timeout(os.environ.get('SOP_BOARD_CODEX_TIMEOUT')) if timeout is None else timeout
        from .generations import Generations
        self.generations = Generations(self)
        from .author_sop import AuthorSop
        self.author_sop = AuthorSop(self)
        service.sop_callback = self.enqueue_generate
        self.availability_error = codex_available() if background else ''
        from .executions import Executions
        self.executions = Executions(self)
        service.execution_callback = self.executions.enqueue
        from .fixes import Fixes
        self.fixes = Fixes(self)
        from .distill_tasks import DistillTasks
        self.distill = DistillTasks(self)
        from .task_types import MergeType, GenerateType, DistillType, ExecuteType, FixType, FixValidationType, AuthorSopType
        self.types = dict(merge=MergeType(self), generate=GenerateType(self),
                          distill=DistillType(self), execute=ExecuteType(self), fix=FixType(self))
        self.types['author-sop'] = AuthorSopType(self)
        self.validation_type = FixValidationType(self)
        from .merge_conversation import MergeConversation
        self.conversation = MergeConversation(self)
        self.kernel = Kernel(self, self.types)
        self.thread = None
        if background:
            with self.lock:
                self.kernel.recover()
            self.thread = threading.Thread(target=self.run, name='codex-queue', daemon=True)
            self.thread.start()

    def confirmed_draft_cases(self, db, batch):
        # 按并集中每条用例的最新上传追溯确认，不能用最后一次上传的标签代表整个并集。
        return {row['case_id']: row['confirmed_by'] for row in db.execute(
            'SELECT v.case_id,u.metadata,c.confirmed_by FROM case_versions v '
            'JOIN case_uploads u ON u.batch=v.batch AND u.number=v.upload '
            'JOIN draft_confirmations c ON c.upload=u.id '
            'WHERE v.batch=? AND NOT EXISTS '
            '(SELECT 1 FROM case_versions newer WHERE newer.batch=v.batch '
            'AND newer.case_id=v.case_id AND newer.upload>v.upload) ORDER BY v.case_id', (batch['id'],))
            if 'sop-draft' in json.loads(row['metadata']).get('tags', [])}

    def enqueue_merge(self, key, name, trigger="重新合并", force_case=None, *, connection=None, fresh=False):
        workspace = self.service.workspace
        workspace.reload()
        if not workspace.configured:
            if trigger == '重新合并' or force_case is not None:
                reject(409, 'workspace-not-ready', 'workspace', '尚未搭建，请先配置工作区')
            return None
        workspace.scenario_path(name)
        with self.lock, (nullcontext(connection) if connection is not None else self.store.transaction()) as db:
            batch = self.service.batch(db, key, name)
            version = self.service.latest_version(db, batch)
            cases = self.service.case_union(db, batch)
            if not cases and trigger != '重新合并':
                return None
            if not cases:
                reject(409, 'cases-missing', 'cases', '批次没有用例，无法重新合并')
            if force_case is not None:
                if force_case not in {c['id'] for c in cases}:
                    reject(404, 'case-not-found', 'case', '用例不存在')
                known = force_case in self.service.flow_choices(db, batch)
                pair = next((p for p in self.service.case_pairings(db, batch, cases) if p['case'] == force_case), None)
                if not known and (not pair or pair['kind'] != 'detail'):
                    reject(409, 'case-kind-invalid', 'case', '当前合并中此用例不是细节用例')
                db.execute('INSERT OR IGNORE INTO case_flow_choices(batch,case_id,chosen_at) VALUES (?,?,?)', (batch['id'], force_case, now()))
            snapshot, scenario_sha, digest = self.freeze_merge(db, key, name, batch, version, cases)
            # 开发者重新合并只复用排队或运行中的同输入任务；上传与场景变化按 SOP-BOARD-3 只与有效结果去重；
            # 启动或配置扫描是核对而非新事件，任何已有的同输入合并（含已中断、已放弃）都不再新建，由开发者重试或放弃。
            states = OUTPUT_ACTIVE if trigger == '重新合并' or fresh else ALL_STATES if trigger == '配置变化' else MERGE_DEDUP
            placeholders = ','.join('?' for _ in states)
            # 已落地的合并以写入后的输入登记 landed_digest，重启扫描不再以自己的输出为依据重复合并。
            previous = db.execute(f"SELECT id FROM tasks WHERE workspace=? AND batch=? AND kind='merge' "
                                  f"AND (input_digest=? OR json_extract(result_json,'$.landed_digest')=?) AND status IN ({placeholders}) ORDER BY id LIMIT 1",
                                  (key, name, digest, digest, *states)).fetchone()
            if previous:
                task_id = previous['id']
            else:
                task_id = db.execute(f"INSERT INTO tasks(kind,workspace,batch,status,trigger,input_digest,input_json,scenario_file_sha256,created_at) VALUES ('merge',?,?,'{S.QUEUED}',?,?,?,?,?)",
                                     (key, name, trigger, digest, canonical(snapshot), scenario_sha, now())).lastrowid
            self.conversation.dispatched(db, batch['id'])
            return dict(ok=True, task=self.read(task_id, connection=db))

    def freeze_merge(self, db, key, name, batch, version, cases):
        workspace = self.service.workspace
        inputs, sources = self.merge_inputs(db, batch, version, cases)
        forced_flow, upload, entries = sources['forced_flow'], sources['upload'], sources['entries']
        uploaded_sop, sop_sources = sources['uploaded_sop'], sources['sop_sources']
        nodes = {r['section']: r['node'] for r in db.execute('SELECT section,node FROM node_numbers WHERE batch=?', (batch['id'],))}
        for case in cases:
            name_ = section(case)
            if name_ not in nodes:
                nodes[name_] = f'S{len(nodes) + 1:02}'
                db.execute('INSERT INTO node_numbers VALUES (?,?,?)', (batch['id'], name_, nodes[name_]))
        snapshot = dict(cases=cases, scenarios=version['content'] if version else '', entries=entries,
                        batch=name, case_metadata=json.loads(upload['metadata']), upload=upload['number'], nodes=nodes,
                        config_digest=workspace.config_digest, forced_flow=forced_flow,
                        confirmed_draft_cases=sources['confirmed_draft_cases'])
        # 只继承已有记录证明曾经并入、且仍在当前场景中的禁止副作用。
        # 任意外来用例场景上的额外断言不因此成为确定字段的输入。
        transferred = {}
        for row in db.execute(f'SELECT result_json FROM tasks WHERE workspace=? AND batch=? AND status IN ({sql_states(MERGE_RESULTS)}) ORDER BY id',
                              (key, name)):
            for disposition in json.loads(row['result_json']).get('scenarios', []):
                if disposition['action'] == 'duplicate':
                    transferred.setdefault(disposition['target'], set()).update(disposition['forbidden_effects'])
        original = yaml.safe_load(snapshot['scenarios']) or {}
        existing = {scene['id']: scene for node in original.get('sops', []) for scene in node['scenarios']}
        owners = {r['case_id']: r['scenario'] for r in db.execute('SELECT case_id,scenario FROM case_pairings WHERE version=?',
                 (version['id'],))} if version else {}
        snapshot['prior_pairs'] = [dict(case=r['case_id'], case_sha256=r['case_sha256'], scenarios=[r['scenario']],
            kind=r['kind'], kind_reason=r['kind_reason'], note=r['kind_reason'])
            for r in db.execute('SELECT * FROM case_pairings WHERE version=? ORDER BY case_id', (version['id'],))] if version else []
        snapshot['merged_forbidden_effects'] = {
            case: [effect for effect in existing[owners[case]]['forbidden_effects'] if effect in effects]
            for case, effects in transferred.items() if case in owners and owners[case] in existing}
        snapshot['sop_sources'] = sop_sources
        snapshot['clarifications'] = sources['clarifications']
        snapshot['excluded_cases'] = [e['case_id'] for e in sources['clarifications']['exclusions']]
        snapshot['all_cases'] = cases
        snapshot['cases'] = [c for c in cases if c['id'] not in snapshot['excluded_cases']]
        session = db.execute("SELECT id,session_id FROM tasks WHERE workspace=? AND batch=? AND kind='merge' "
            "AND session_id IS NOT NULL AND session_id<>'' ORDER BY id DESC LIMIT 1", (key, name)).fetchone()
        snapshot['resume_session'] = session['session_id'] if session and not self.kernel.claim(session, connection=db) else None
        snapshot['resume_task'] = session['id'] if session else None
        prior = db.execute("SELECT input_json,result_json FROM tasks WHERE workspace=? AND batch=? AND kind='merge' "
            f"AND status IN ({sql_states({S.COMPLETED, S.NEEDS_INPUT})}) ORDER BY id DESC LIMIT 1", (key, name)).fetchone()
        if prior:
            snapshot['entry_gaps'] = dict(entries=json.loads(prior['input_json'])['entries'],
                gaps=[g for g in json.loads(prior['result_json']).get('gaps', []) if isinstance(g, dict) and g['kind'] == 'entry'])
        scenario_sha = version['sha256'] if version else ''
        if version and not (yaml.safe_load(version['content']) or {}).get('sops') and not workspace.scenario_path(name).exists():
            scenario_sha = ''
        if uploaded_sop:
            snapshot['sop_upload_id'] = uploaded_sop['id']
            snapshot['uploaded_sop'] = inputs['uploaded_sop']
        digest = hashlib.sha256(canonical(inputs).encode()).hexdigest()
        return snapshot, scenario_sha, digest

    def refresh_merge(self, task):
        self.service.workspace.reload()
        with self.store.transaction() as db:
            batch = self.service.batch(db, task['workspace'], task['batch'])
            snapshot, sha, digest = self.freeze_merge(db, task['workspace'], task['batch'], batch,
                self.service.latest_version(db, batch), self.service.case_union(db, batch))
            # 会话 prepare 或重试重新冻结的快照包含此刻全部回复，与入队相同计为已派发，本轮结束后不再为它们补派。
            self.conversation.dispatched(db, batch['id'])
        return dict(input_json=canonical(snapshot), input_digest=digest, scenario_file_sha256=sha, snapshot_json='{}')

    def merge_inputs(self, db, batch, version, cases):
        """合并的业务输入及其来源；上传序号和已分配节点不是业务输入，配置改变也必须生成新快照。"""
        from .repositories import read_sop_sources
        workspace = self.service.workspace
        forced_flow = self.service.flow_choices(db, batch)
        upload = db.execute('SELECT number,metadata FROM case_uploads WHERE batch=? ORDER BY number DESC LIMIT 1', (batch['id'],)).fetchone()
        entries = dict(entries=workspace.entries)
        uploaded_sop = self.service.latest_sop_upload(db, batch)
        sop_sources = read_sop_sources(workspace, version['content'] if version else '', uploaded_sop)
        if uploaded_sop:
            sop_sources = [s for s in sop_sources if s['path'] != uploaded_sop['path']]
            sop_sources.append(dict(path=uploaded_sop['path'], ref=uploaded_sop['ref'],
                content=uploaded_sop['content'], commit=uploaded_sop['commit_id'],
                sha256=uploaded_sop['sha256'], confirmed_by=uploaded_sop['confirmed_by']))
            sop_sources.sort(key=lambda s: s['path'])
        confirmed = self.confirmed_draft_cases(db, batch)
        clarifications = self.conversation.snapshot(db, batch)
        inputs = dict(cases=dict(metadata=json.loads(upload['metadata']), test_cases=cases),
                      scenarios=version['sha256'] if version else '', entries=entries, config_digest=workspace.config_digest,
                      forced_flow=forced_flow, sop=[dict(path=s['path'], sha256=s['sha256']) for s in sop_sources])
        # 暂缓展示来自结果，不是新输入；避免自己的落地触发下一轮。
        inputs['clarifications'] = {k: v for k, v in clarifications.items() if k != 'holds'}
        if confirmed:
            inputs['confirmed_draft_cases'] = confirmed
        if uploaded_sop:
            inputs['uploaded_sop'] = dict(ref=uploaded_sop['ref'], version=f"看板上传 #{uploaded_sop['version']}",
                                          confirmed_by=uploaded_sop['confirmed_by'])
        return inputs, dict(forced_flow=forced_flow, upload=upload, entries=entries, uploaded_sop=uploaded_sop,
                            sop_sources=sop_sources, confirmed_draft_cases=confirmed, clarifications=clarifications)

    def enqueue_execution(self, key, name, scope='pending'):
        if isinstance(scope, list):
            if not scope or any(not isinstance(s, str) or not s for s in scope) or len(set(scope)) != len(scope):
                reject(422, 'invalid-scope', 'scope', '指定范围须为不重复的场景编号数组')
            return self.executions.enqueue(key, name, selected=scope)
        if not isinstance(scope, str) or scope not in ('pending', 'all'):
            reject(422, 'invalid-scope', 'scope', '执行范围应为 pending、all 或场景编号数组')
        return self.executions.enqueue(key, name, selection=scope)

    def enqueue_generate(self, key, name, version=None):
        return self.generations.enqueue(key, name, version)

    def list(self, workspace=None, batch=None, *, local=True, kind=None):
        with queue_view():
            with self.store.connection() as db:
                ids = [r[0] for r in db.execute('SELECT id FROM tasks WHERE (? IS NULL OR workspace=?) AND (? IS NULL OR batch=?) AND (? IS NULL OR kind=?) ORDER BY id',
                                               (workspace, workspace, batch, batch, kind, kind))]
            return dict(ok=True, tasks=[self.read(i, local=local) for i in ids])

    def directory(self, task_id):
        return self.store.data.resolve() / 'tasks' / str(task_id)

    def merge_progress(self, task, *, local=False):
        previous = task.get('progress')
        result = (dict(previous) if previous is not None and (not local or 'resume' in previous)
                  else self.kernel.progress(task, local=local))
        result.update(timeout=self.timeout, gap_count=len(task['result'].get('gaps', [])),
                      held_count=len(task['result'].get('held', [])))
        with self.store.connection() as db:
            result['auto_execute'] = self.service.auto_execute_enabled(db)
        if task['status'] == S.NEEDS_SYNC and not result['error']:
            result['error'] = '；'.join(str(problem) for problem in task['result'].get('problems', []))
            if not result['error'] and task['result'].get('workspace_changed'):
                result['error'] = '工作区中的场景文件与看板版本不一致（已被修改，或不是合法的场景文件），请核对后重新合并'
        if not local:
            result = self.types['merge'].visibility.project(dict(task, progress=result, error=result.get('error', ''),
                session_task=result.get('session_task'), ownership=result.get('ownership', '')))['progress']
        return result

    def latest_merge(self, workspace, batch):
        with self.store.connection() as db:
            row = db.execute("SELECT id,kind,status,started_at,finished_at,error,result_json FROM tasks "
                "WHERE workspace=? AND batch=? AND kind='merge' ORDER BY id DESC LIMIT 1", (workspace, batch)).fetchone()
        if row is None:
            return None
        task = dict(row)
        task['result'] = json.loads(task.pop('result_json'))
        return self.merge_progress(task)

    def read(self, task_id, *, connection=None, local=True):
        with (nullcontext(connection) if connection is not None else self.store.connection()) as db:
            row = db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
        if not row:
            reject(404, 'task-not-found', 'task', '任务不存在')
        task = dict(row)
        task['progress'] = self.kernel.progress(task, local=local, connection=connection)
        task['session_task'] = task['progress']['session_task']
        task['ownership'] = task['progress']['ownership']
        if task['kind'] == 'distill':
            from .distill_inputs import pending_validation
            task['validation_scenarios'] = pending_validation(task['status'], json.loads(task['input_json']) if task['status'] == S.WAIT_VALIDATION else {})
        task.pop('input_json')
        task['result'] = json.loads(task.pop('result_json'))
        task['gaps'] = task['result'].get('gaps', [])
        for field in ('scope', 'snapshot', 'rounds'):
            task[field] = json.loads(task.pop(field + '_json'))
        task['reused_count'] = task['rerun_count'] = 0
        if task['kind'] == 'execute' and task['rounds']:
            from .statistics import task_counts
            with (nullcontext(connection) if connection is not None else self.store.connection()) as db:
                task.update(task_counts(db, task['workspace'], task['batch'], task['rounds']))
        directory = self.directory(task_id)
        task['files'] = {}
        for parent, folders, names in (os.walk(directory, followlinks=False) if local else ()):
            if Path(parent) == directory:
                folders[:] = [name for name in folders if name in {'evidence', 'sop', 'sources', 'attempts', 'interruptions', 'validations', 'playwright'} or re.fullmatch(r'attempt-[0-9]+', name)]
            if '.git' in folders or '.git' in names:
                folders.clear()
                continue
            folders.sort()
            for name in sorted(names):
                path = Path(parent) / name
                if self.is_task_file(task_id, path) and (local or self.is_shared_file(directory, path)):
                    relative = str(path.relative_to(directory))
                    task['files'][relative] = f'/api/v1/tasks/{task_id}/files/{quote(relative, safe="/")}'
        if task['kind'] == 'fix':
            self.fixes.details(task)
        elif task['kind'] == 'distill' and local:
            self.distill.details(task)
        return task if local else self.types[task['kind']].visibility.project(task)

    def is_task_file(self, task_id, path, *, archival=False):
        directory = self.directory(task_id)
        if directory in self.service.environment.worktrees.active or (directory / '.git').exists():
            return False
        if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(directory):
            return False
        relative = path.relative_to(directory)
        public = {'task.json', 'retest.json', 'web-pending.json', 'playwright-scopes.json', 'events.jsonl', 'stderr.txt', 'final.json', 'results.jsonl', 'prompt.md', 'schema.json', 'input.json',
                  'cases.yaml', 'cases.json', 'case_choices.json', 'case_confirmations.json', 'scenarios.yaml', 'scenario-file.yaml', 'nodes.json',
                  'entries.yaml', 'entries.json', 'snapshot.json', 'changes.json', 'impact.json', 'pending.json', 'previous-results.json',
                  'regression-links.json', 'credential-blocked.json', 'regression.json', 'pass-evidence.json',
                  'result.json', 'check.json', 'pairs.json', 'problems.json', 'feedback.json', 'clarifications.json', 'resume-fallback.json'}
        public |= {'sop.md', 'repos.json', 'draft.json', 'annotations.json', 'sop-before.md', 'sop.diff',
                   'original-sop.md', 'history.json', 'all-annotations.json', 'revision.json', 'cases-generated.yaml',
                   'sources.json', 'questions.json', 'answers.json', 'sop-guide.md'}
        directories = {'evidence', 'sop', 'sources', 'attempts', 'interruptions', 'validations', 'playwright'}
        if len(relative.parts) == 1:
            if relative.name not in public and not re.fullmatch(r'commit-[0-9]+-[a-z0-9-]+\.txt', relative.name):
                return False
        elif relative.parts[0] not in directories and not re.fullmatch(r'attempt-[0-9]+', relative.parts[0]):
            return False
        if self.kernel.repositories.contains(task_id, path, unknown_is_worktree=not archival):
            return False
        return not any((parent / '.git').exists() for parent in path.parents if parent.is_relative_to(directory))

    def is_shared_file(self, directory, path):
        with self.store.connection() as db:
            row = db.execute('SELECT kind FROM tasks WHERE id=?', (int(directory.name),)).fetchone()
        return bool(row and self.types[row['kind']].visibility.shared(path.relative_to(directory)))

    def file(self, task_id, name, *, local=True):
        task = self.read(task_id, local=local)
        directory = self.directory(task_id)
        path = directory / name
        if not local and not self.types[task['kind']].visibility.shared(Path(name)):
            reject(403, 'local-only', 'file', '该任务材料仅限本机查看')
        if '\0' in name or Path(name).is_absolute() or '..' in Path(name).parts or not self.is_task_file(task_id, path):
            reject(404, 'task-file-not-found', 'file', '任务文件不存在')
        if not local and not self.is_shared_file(directory, path):
            reject(403, 'local-only', 'file', '该任务材料仅限本机查看')
        raw = path.read_bytes()
        if not local:
            from .credentials import hide_names, hide_local_paths
            raw = hide_local_paths(hide_names(raw, self.service.workspace.credential_names),
                                   [self.service.workspace.data, *self.service.workspace.repos.values()])
        return raw

    def merge_result(self, key, name, task_id, *, local=True):
        task = self.read(task_id)
        if task['kind'] != 'merge' or task['workspace'] != key or task['batch'] != name:
            reject(404, 'merge-result-not-found', 'task', '没有合并结果')
        path = self.directory(task_id) / 'scenario-file.yaml'
        if task['status'] not in MERGE_RESULTS or not path.is_file():
            reject(404, 'merge-result-not-found', 'task', '没有合并结果')
        return self.file(task_id, 'scenario-file.yaml', local=local)

    def update(self, task_id, **values):
        if not values:
            return False
        # 等待轮询通常没有新值；先读所需字段，避免为无变化的更新开启写事务。
        with self.store.lock:
            with self.store.connection() as db:
                current = db.execute('SELECT ' + ','.join(values) + ' FROM tasks WHERE id=?', (task_id,)).fetchone()
            if current is None or all(current[key] == value for key, value in values.items()):
                return False
            with self.store.transaction() as db:
                # 其他进程可能在上述读取之后已写入相同值，事务内再按实际值限定更新。
                changed = ' OR '.join(key + ' IS NOT ?' for key in values)
                return bool(db.execute('UPDATE tasks SET ' + ','.join(k+'=?' for k in values) +
                    ' WHERE id=? AND (' + changed + ')',
                    (*values.values(), task_id, *values.values())).rowcount)

    def action(self, task_id, action, body=None):
        with self.lock:
            task = self.read(task_id)
            if task['kind'] == 'execute' and self.kernel.claim(task):
                with self.store.connection() as db:
                    verification = db.execute('SELECT 1 FROM session_fix_executions WHERE execution=?',(task_id,)).fetchone()
                if verification and action != 'cancel':
                    reject(409,'session-verification-owned','task','会话复验请重新 execute prepare --fix，不由看板重试')
                if verification and action == 'cancel' and task['status'] == S.SESSION:
                    from .session_fixes import SessionFixes
                    SessionFixes(self).cancel_validation(task_id)
                    return dict(ok=True,task=self.read(task_id))
            if task['kind'] == 'distill':
                return self.distill.action(task, action, body)
            if task['kind'] == 'fix':
                return self.fixes.action(task, action)
            if action not in self.types[task['kind']].actions(task):
                reject(409, 'task-state-invalid', 'status', '任务当前状态不支持此操作')
            if task['kind'] == 'generate':
                self.generations.action(task, action)
            if action == 'cancel':
                self.update(task_id, status=S.ABANDONED, finished_at=now())
                process = self.processes.get(task_id)
                if process:
                    terminate_group(process)
                if self.browser_owner == task_id:
                    self.browser_owner = None
            else:
                refreshed = self.types[task['kind']].refresh(task)
                self.kernel.archive(task)
                self.kernel.wake(task_id)
                self.update(task_id, status=S.QUEUED, started_at=None, finished_at=None,
                            session_id=None, error='', result_digest=None, result_json='{}',
                            **refreshed,
                            void_reason='', rounds_json='[]', attempt=task['attempt'] + 1)
            if task['kind'] == 'generate':
                self.generations.task_status(task_id, S.ABANDONED if action == 'cancel' else S.QUEUED)
            elif task['kind'] == 'merge':
                self.kernel.wake(0)
        return dict(ok=True, task=self.read(task_id))

    def prepare_merge(self, task):
        directory = self.directory(task['id'])
        directory.mkdir(parents=True, exist_ok=True)
        snapshot = json.loads(task['input_json'])
        prompt = files('sop_board').joinpath('prompts/merge.md').read_text(encoding='utf-8')
        (directory / 'cases.yaml').write_text(yaml.safe_dump(dict(metadata=snapshot['case_metadata'], test_cases=snapshot['cases']), allow_unicode=True), encoding='utf-8')
        (directory / 'scenarios.yaml').write_text(self.conversation.decision_snapshot(snapshot)['scenarios'], encoding='utf-8')
        (directory / 'entries.yaml').write_text(yaml.safe_dump(snapshot['entries'], allow_unicode=True), encoding='utf-8')
        (directory / 'nodes.json').write_text(canonical(snapshot['nodes']), encoding='utf-8')
        for source in snapshot['sop_sources']:
            target = directory / 'sop' / source['path']
            if directory not in target.resolve().parents or '..' in Path(source['path']).parts:
                raise ValueError('SOP 原文路径越界')
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(source['content'], encoding='utf-8')
        (directory / 'case_choices.json').write_text(canonical(dict(forced_flow=snapshot['forced_flow'])), encoding='utf-8')
        (directory / 'case_confirmations.json').write_text(canonical(dict(
            confirmed_draft_cases=snapshot.get('confirmed_draft_cases', {}))), encoding='utf-8')
        (directory / 'clarifications.json').write_text(canonical(snapshot.get('clarifications', {})), encoding='utf-8')
        prompt += f'\n本轮任务目录：{directory}\n请只读取这个目录中的本轮材料，clarifications.json 包含完整历史与本轮待处理回复。\n'
        (directory / 'input.json').write_text(task['input_json'], encoding='utf-8')
        (directory / 'prompt.md').write_text(prompt, encoding='utf-8')
        (directory / 'schema.json').write_text(canonical(SCHEMA), encoding='utf-8')
        return prompt

    def execute(self, task):
        self.kernel.run_output(task)

    def consume_merge_output(self, task):
        directory = self.directory(task['id'])
        final = (directory / 'final.json').read_text(encoding='utf-8')
        if not final.strip():
            raise FileNotFoundError('Codex 没有写出最终输出')
        snapshot = json.loads(task['input_json'])
        result = read_merge_output(final)
        with self.lock:
            if self.stopping.is_set() or self.read(task['id'])['status'] not in (S.RUNNING, S.SESSION):
                return S.INTERRUPTED, dict(ok=False, problems=['合并任务已停止，未写入工作区'])
            with self.store.connection() as db:
                batch = self.service.batch(db, task['workspace'], task['batch'])
                working, plan, problems = self.conversation.validate(snapshot, result, task, db, batch)
            if problems:
                return S.FAILED, dict(ok=False, problems=problems)
            decision = self.conversation.decision_snapshot(working)
            if not decision['cases'] and not (yaml.safe_load(decision['scenarios']) or {}).get('sops'):
                if any(result[key] for key in ('cases', 'nodes', 'scenarios', 'gaps', 'relocate', 'negative_exempt')):
                    return S.FAILED, dict(ok=False, problems=['本批没有待判断用例或场景，输出不得新增分类、缺口或场景'])
                # 用户明确排除全部用例时，撤下旧场景但保留历史；初次无场景不写空文件。
                raw = yaml.safe_dump(dict(metadata=(yaml.safe_load(snapshot['scenarios']) or {}).get('metadata', {}), sops=[]), allow_unicode=True) if snapshot['scenarios'] else None
                check = dict(ok=True, problems=[], gaps=[], held=[], pairs=[], scenarios=[])
            else:
                raw, check = assemble(decision, result)
            if not check['ok']:
                return S.FAILED, check
            check.update(reply=result['reply'], responses=result['responses'], case_updates=result['case_updates'])
            fallback = directory / 'resume-fallback.json'
            if fallback.is_file():
                check['session_note'] = json.loads(fallback.read_text())['note']
            if raw is not None:
                (directory / 'scenario-file.yaml').write_text(raw, encoding='utf-8')
                (directory / 'pairs.json').write_text(canonical(dict(covered=check['pairs'], conflict=[], disposed=[])), encoding='utf-8')
            try:
                return self.land(task, raw, check, working=working, plan=plan), check
            except (OSError, ValueError, sqlite3.Error, APIError) as problem:
                return S.FAILED, dict(check, ok=False, problems=[f'合并结果无法写入工作区：{problem}'])

    def land(self, task, raw, check, *, working=None, plan=None):
        workspace = self.service.workspace
        inputs = self.types['merge'].inputs(task)
        snapshot = inputs.snapshot
        working = working or snapshot
        workspace.reload()
        if not workspace.configured or snapshot['config_digest'] != workspace.config_digest:
            check['problems'] = ['工作区配置变化，请重新合并']
            return S.NEEDS_SYNC
        with self.store.connection() as db:
            batch = self.service.batch(db, task['workspace'], task['batch'])
            cases = self.service.case_union(db, batch)
            metadata = json.loads(db.execute('SELECT metadata FROM case_uploads WHERE batch=? ORDER BY number DESC LIMIT 1', (batch['id'],)).fetchone()[0])
            choices = self.service.flow_choices(db, batch)
            confirmed_draft_cases = self.confirmed_draft_cases(db, batch)
            version = self.service.latest_version(db, batch)
            uploaded_sop = self.service.latest_sop_upload(db, batch)
            exclusions = [r[0] for r in db.execute('SELECT case_id FROM merge_exclusions WHERE batch=? ORDER BY case_id', (batch['id'],))]
            draft_upload = db.execute('SELECT 1 FROM case_uploads u JOIN draft_confirmations c ON c.upload=u.id '
                                     'WHERE u.batch=? AND u.number=?', (batch['id'], snapshot['upload'])).fetchone()
        if (uploaded_sop['id'] if uploaded_sop else None) != snapshot.get('sop_upload_id'):
            check['problems'] = ['业务 SOP 已更新，请重新合并']
            return S.NEEDS_SYNC
        changed = inputs.changed(dict(snapshot, cases=[c for c in cases if c['id'] not in exclusions],
            all_cases=cases, excluded_cases=exclusions, case_metadata=metadata,
            confirmed_draft_cases=confirmed_draft_cases, forced_flow=choices,
            scenarios=version['content'] if version else ''))
        if changed:
            check['problems'] = ['用例、场景或开发者分类选择变化，请重新合并']
            return S.NEEDS_SYNC
        source = workspace.scenario_path(task['batch'])
        current = hashlib.sha256(source.read_bytes()).hexdigest() if source.exists() else ''
        if current != task['scenario_file_sha256']:
            check['workspace_changed'] = True
            return S.NEEDS_SYNC
        status = S.COMPLETED if raw is not None else S.NEEDS_INPUT
        def registered(db, batch_id):
            batch = self.service.batch(db, task['workspace'], task['batch'])
            if plan is not None:
                self.conversation.register(db, batch, task, working, plan)
            landed, _ = self.merge_inputs(db, batch, self.service.latest_version(db, batch), self.service.case_union(db, batch))
            known = {m['id'] for m in snapshot.get('clarifications', {}).get('messages', [])}
            landed['clarifications']['messages'] = [m for m in landed['clarifications']['messages'] if m['id'] in known]
            check['landed_digest'] = hashlib.sha256(canonical(landed).encode()).hexdigest()
            self.kernel.register(task, dict(check=check, status=status), connection=db, phase='merge-land')

        if raw is None:
            with self.store.transaction() as db:
                registered(db, batch['id'])
            return status
        from .service import fingerprint
        document = yaml.safe_load(raw)
        if version and document == yaml.safe_load(version['content']):
            comments = lambda value: [line.strip() for line in value.splitlines() if line.lstrip().startswith('#')]
            # 兼容已有文件的字段顺序；不能把新增/变化的推断注释当作无变化吞掉。
            if comments(raw) == comments(version['content']):
                raw = version['content']
                (self.directory(task['id']) / 'scenario-file.yaml').write_text(raw, encoding='utf-8')
        info = workspace.product(workspace.aliases(document))
        source.parent.mkdir(parents=True, exist_ok=True)
        previous = source.read_bytes() if source.exists() else None
        temporary = source.with_suffix('.yaml.tmp')
        changed_file = False
        if document['sops']:
            if previous != raw.encode('utf-8'):
                temporary.write_text(raw, encoding='utf-8')
                temporary.replace(source)
                changed_file = True
        elif source.exists():
            source.unlink()
            changed_file = True
        case_hashes = {c['id']: hashlib.sha256(canonical(c).encode()).hexdigest() for c in working['cases']}
        try:
            self.service.push(workspace.name, task['batch'], dict(workspace=dict(name=workspace.name),
                path=workspace.display_path(source), content=raw, pushed_by=f"看板合并 #{task['id']}",
                commit=info['commit'], dirty=info['dirty'], product_digest=info['product_digest'],
                fingerprints={s['id']: fingerprint(s) for n in document['sops'] for s in n['scenarios']}),
                pairings=[dict(p, case_sha256=p['case_sha256'] if 'case_sha256' in p else case_hashes[p['case']]) for p in check['pairs']],
                auto_execute=draft_upload is None, on_registered=registered)
        except (OSError, ValueError, sqlite3.Error, APIError):
            # 数据事务未成功时恢复本次写入前的文件；已登记则保留，交由收据恢复。
            if changed_file and not self.kernel.receipt(task, phase='merge-land') and (not source.exists() or source.read_text(encoding='utf-8') == raw):
                if previous is None:
                    source.unlink(missing_ok=True)
                else:
                    source.write_bytes(previous)
            raise
        return status

    def run(self):
        self.kernel.run()

    def close(self):
        self.stopping.set()
        with self.lock:
            for process in list(self.processes.values()):
                terminate_group(process)
        if self.thread:
            self.thread.join()
        for worker in self.workers.values():
            worker.join()
