"""持久任务、Codex 进程入口与经过核对后的合并落地。"""
import hashlib
from contextlib import nullcontext
import json
import re
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import threading
from importlib.resources import files
from urllib.parse import quote

from .errors import reject, APIError
from .drafts import SCHEMA, assemble, section, schema_problems, normalize_judgment
from .credentials import launch, shell_options

import yaml
from .service import canonical, now


def parse_timeout(value):
    if value is None:
        return 1800
    if not re.fullmatch(r'[0-9]+', value.strip()) or int(value) <= 0:
        raise ValueError('SOP_BOARD_CODEX_TIMEOUT 超时取值不合规：应为正整数秒数')
    return int(value)


def codex_environment():
    allowed = {'PATH', 'HOME', 'USER', 'LANG', 'TMPDIR', 'CODEX_HOME', 'SHELL'}
    return {k: v for k, v in os.environ.items() if k in allowed or k.startswith('LC_')}


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


def codex_failure(directory, code, label='Codex'):
    """失败事件的最近错误优先；摘要限 300 字，不以日志读取失败覆盖退出码。"""
    messages = []
    try:
        with (Path(directory) / 'events.jsonl').open(encoding='utf-8', errors='replace') as stream:
            for line in stream:
                try:
                    event = json.loads(line)
                except (ValueError, RecursionError):
                    continue
                if not isinstance(event, dict) or event.get('type') not in ('error', 'turn.failed'):
                    continue
                error = event.get('error')
                message = (error.get('message') if isinstance(error, dict) else error) or event.get('message')
                if isinstance(message, str) and message.strip():
                    message = ' '.join(message.split())[:300]
                    if message in messages:
                        messages.remove(message)
                    messages.insert(0, message)
                    messages = messages[:3]
    except OSError:
        pass
    summary = '；'.join(messages)
    if len(summary) > 300:
        summary = summary[:299] + '…'
    return f'{label} 退出码 {code}' + ('：' + summary if summary else '')


def start_codex(directory, prompt, sandbox='read-only', environment=None, work_directory=None, writable_directories=()):
    """可替换的启动边界；子进程只得到白名单环境及独立进程组。"""
    directory = Path(directory).resolve()
    options = ['-c', 'sandbox_workspace_write.network_access=true'] if sandbox == 'workspace-write' else []
    options += shell_options(environment)
    for path in writable_directories:
        options.extend(['--add-dir', str(Path(path).resolve())])
    schema = ['--output-schema', str(directory / 'schema.json')] if (directory / 'schema.json').is_file() else []
    return launch(['codex', 'exec', '--json', '-s', sandbox, *options, '--skip-git-repo-check',
        '-C', str(work_directory or directory), *schema, '-o', str(directory / 'final.json'), prompt],
        directory, environment if environment is not None else codex_environment(), work_directory or directory)


def resume_codex(directory, thread_id, prompt, sandbox='read-only', work_directory=None, environment=None, writable_directories=(), schema=None):
    """续接指定会话；resume 的沙箱参数位于 Codex 根命令层。"""
    directory = Path(directory).resolve()
    options = ['-c', 'sandbox_workspace_write.network_access=true'] if sandbox == 'workspace-write' else []
    options += shell_options(environment)
    for path in writable_directories:
        options.extend(['--add-dir', str(Path(path).resolve())])
    schema_args = ['--output-schema', str(Path(schema).resolve())] if schema is not None else []
    return launch(['codex', '-s', sandbox, *options, 'exec', 'resume', thread_id, '--json',
        '--skip-git-repo-check', *schema_args, '-o', str(directory / 'final.json'), prompt],
        directory, environment if environment is not None else codex_environment(), work_directory or directory)


def codex_available():
    try:
        result = subprocess.run(['codex', '--version'], env=codex_environment(),
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, start_new_session=True, timeout=5)
        if result.returncode:
            return f'codex --version 失败（退出码 {result.returncode}）'
    except FileNotFoundError:
        return '未找到 codex 命令'
    except (OSError, subprocess.SubprocessError):
        return 'codex --version 失败：无法启动或超时'
    return ''


def terminate_group(process):
    from .adapters import stop_process
    stop_process(process, grace=.2)


class Tasks:
    def __init__(self, service, launcher=start_codex, timeout=None):
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
        with self.store.transaction() as db:
            db.execute("UPDATE tasks SET status='已中断',finished_at=?,error='看板重启，任务已中断' WHERE "
                "(kind!='fix' AND status IN ('排队中','待提交','等待浏览器','等待合并','运行中')) OR "
                "(kind='fix' AND status IN ('分析中','修复中','复测中','待合入'))", (now(),))
        from .generations import Generations
        self.generations = Generations(self)
        service.sop_callback = self.enqueue_generate
        self.availability_error = codex_available()
        from .executions import Executions
        self.executions = Executions(self)
        service.execution_callback = self.executions.enqueue
        from .fixes import Fixes
        self.fixes = Fixes(self)
        if self.service.workspace.configured:
            self.fixes.recover_promotions()
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

    def enqueue_merge(self, key, name, trigger="重新合并", force_case=None, *, connection=None):
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
            for row in db.execute("SELECT result_json FROM tasks WHERE workspace=? AND batch=? AND status IN ('已完成','待同步') ORDER BY id",
                                  (key, name)):
                for disposition in json.loads(row['result_json']).get('scenarios', []):
                    if disposition['action'] == 'duplicate':
                        transferred.setdefault(disposition['target'], set()).update(disposition['forbidden_effects'])
            original = yaml.safe_load(snapshot['scenarios']) or {}
            existing = {scene['id']: scene for node in original.get('sops', []) for scene in node['scenarios']}
            owners = {r['case_id']: r['scenario'] for r in db.execute('SELECT case_id,scenario FROM case_pairings WHERE version=?',
                     (version['id'],))} if version else {}
            snapshot['merged_forbidden_effects'] = {
                case: [effect for effect in existing[owners[case]]['forbidden_effects'] if effect in effects]
                for case, effects in transferred.items() if case in owners and owners[case] in existing}
            snapshot['sop_sources'] = sop_sources
            scenario_sha = version['sha256'] if version else ''
            if uploaded_sop:
                snapshot['sop_upload_id'] = uploaded_sop['id']
                snapshot['uploaded_sop'] = inputs['uploaded_sop']
            digest = hashlib.sha256(canonical(inputs).encode()).hexdigest()
            states = ('排队中', '运行中') if trigger == '重新合并' else (
                '已完成', '待同步', '待补充', '排队中', '运行中', '失败', 'Codex 不可用', '已中断', '已放弃')
            placeholders = ','.join('?' for _ in states)
            # 已落地的合并以写入后的输入登记 landed_digest，重启扫描不再以自己的输出为依据重复合并。
            previous = db.execute(f"SELECT id FROM tasks WHERE workspace=? AND batch=? AND kind='merge' "
                                  f"AND (input_digest=? OR json_extract(result_json,'$.landed_digest')=?) AND status IN ({placeholders}) ORDER BY id LIMIT 1",
                                  (key, name, digest, digest, *states)).fetchone()
            if previous:
                task_id = previous['id']
            else:
                task_id = db.execute("INSERT INTO tasks(kind,workspace,batch,status,trigger,input_digest,input_json,scenario_file_sha256,created_at) VALUES ('merge',?,?,'排队中',?,?,?,?,?)",
                                     (key, name, trigger, digest, canonical(snapshot), scenario_sha, now())).lastrowid
            return dict(ok=True, task=self.read(task_id, connection=db))

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
        inputs = dict(cases=dict(metadata=json.loads(upload['metadata']), test_cases=cases),
                      scenarios=version['sha256'] if version else '', entries=entries, config_digest=workspace.config_digest,
                      forced_flow=forced_flow, sop=[dict(path=s['path'], sha256=s['sha256']) for s in sop_sources])
        if confirmed:
            inputs['confirmed_draft_cases'] = confirmed
        if uploaded_sop:
            inputs['uploaded_sop'] = dict(ref=uploaded_sop['ref'], version=f"看板上传 #{uploaded_sop['version']}",
                                          confirmed_by=uploaded_sop['confirmed_by'])
        return inputs, dict(forced_flow=forced_flow, upload=upload, entries=entries, uploaded_sop=uploaded_sop,
                            sop_sources=sop_sources, confirmed_draft_cases=confirmed)

    def enqueue_execution(self, key, name, scope='pending'):
        if not isinstance(scope, str) or scope not in ('pending', 'all'):
            reject(422, 'invalid-scope', 'scope', '执行范围应为 pending 或 all')
        return self.executions.enqueue(key, name, selection=scope)

    def enqueue_generate(self, key, name, version=None):
        return self.generations.enqueue(key, name, version)

    def list(self, workspace=None, batch=None, *, local=True):
        with self.store.connection() as db:
            ids = [r[0] for r in db.execute('SELECT id FROM tasks WHERE (? IS NULL OR workspace=?) AND (? IS NULL OR batch=?) ORDER BY id',
                                           (workspace, workspace, batch, batch))]
        return dict(ok=True, tasks=[self.read(i, local=local) for i in ids])

    def directory(self, task_id):
        return self.store.data.resolve() / 'tasks' / str(task_id)

    def read(self, task_id, *, connection=None, local=True):
        with (nullcontext(connection) if connection is not None else self.store.connection()) as db:
            row = db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
        if not row:
            reject(404, 'task-not-found', 'task', '任务不存在')
        task = dict(row)
        task.pop('input_json')
        task['result'] = json.loads(task.pop('result_json'))
        task['gaps'] = task['result'].get('gaps', [])
        task['reused_count'] = task['result'].get('reused_count', 0)
        for field in ('scope', 'snapshot', 'rounds'):
            task[field] = json.loads(task.pop(field + '_json'))
        directory = self.directory(task_id)
        task['files'] = {}
        for parent, folders, names in os.walk(directory, followlinks=False):
            if Path(parent) == directory:
                folders[:] = [name for name in folders if name in {'evidence', 'sop', 'attempts', 'interruptions', 'validations'}]
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
        return task

    def is_task_file(self, task_id, path):
        directory = self.directory(task_id)
        if directory in self.service.environment.worktrees.active or (directory / '.git').exists():
            return False
        if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(directory):
            return False
        relative = path.relative_to(directory)
        public = {'events.jsonl', 'stderr.txt', 'final.json', 'results.jsonl', 'prompt.md', 'schema.json', 'input.json',
                  'cases.yaml', 'cases.json', 'case_choices.json', 'case_confirmations.json', 'scenarios.yaml', 'scenario-file.yaml', 'nodes.json',
                  'entries.yaml', 'entries.json', 'snapshot.json', 'changes.json', 'impact.json', 'pending.json', 'previous-results.json',
                  'regression-links.json', 'credential-blocked.json',
                  'result.json', 'check.json', 'pairs.json', 'problems.json', 'feedback.json'}
        public |= {'sop.md', 'repos.json', 'draft.json', 'annotations.json', 'sop-before.md', 'sop.diff',
                   'original-sop.md', 'history.json', 'all-annotations.json', 'revision.json', 'cases-generated.yaml'}
        directories = {'evidence', 'sop', 'attempts', 'interruptions', 'validations'}
        if len(relative.parts) == 1:
            if relative.name not in public and not re.fullmatch(r'commit-[0-9]+-[a-z0-9-]+\.txt', relative.name):
                return False
        elif relative.parts[0] not in directories:
            return False
        with self.store.connection() as db:
            roots = [r[0] for r in db.execute('SELECT worktree FROM fix_repositories WHERE task=?', (task_id,))]
        if any(not root or path.resolve().is_relative_to(Path(root)) for root in roots):
            return False
        return not any((parent / '.git').exists() for parent in path.parents if parent.is_relative_to(directory))

    # 内网只开放测试同事协作所需的材料；提示词、会话记录、快照与证据等代理材料仅限本机（D45、D62）。
    SHARED_FILES = {'sop.md', 'original-sop.md', 'sop-before.md', 'sop.diff', 'draft.json', 'annotations.json',
                    'all-annotations.json', 'revision.json', 'history.json', 'cases.yaml', 'cases.json', 'cases-generated.yaml',
                    'case_choices.json', 'case_confirmations.json', 'scenarios.yaml', 'scenario-file.yaml', 'nodes.json',
                    'results.jsonl', 'pairs.json', 'problems.json', 'feedback.json'}

    def is_shared_file(self, directory, path):
        relative = path.relative_to(directory)
        # 合并任务 sop/ 下是 SOP 原文，属于协作材料。
        return (len(relative.parts) > 1 and relative.parts[0] == 'sop') or (len(relative.parts) == 1 and relative.name in self.SHARED_FILES)

    def file(self, task_id, name, *, local=True):
        self.read(task_id)
        directory = self.directory(task_id)
        path = directory / name
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

    def merge_result(self, key, name, task_id):
        task = self.read(task_id)
        if task['workspace'] != key or task['batch'] != name:
            reject(404, 'merge-result-not-found', 'task', '没有合并结果')
        path = self.directory(task_id) / 'scenario-file.yaml'
        if task['status'] not in ('已完成', '待同步') or not path.is_file():
            reject(404, 'merge-result-not-found', 'task', '没有合并结果')
        return path.read_bytes()

    def update(self, task_id, **values):
        with self.store.transaction() as db:
            db.execute('UPDATE tasks SET ' + ','.join(k+'=?' for k in values) + ' WHERE id=?', (*values.values(), task_id))

    def action(self, task_id, action):
        with self.lock:
            task = self.read(task_id)
            if task['kind'] == 'fix':
                return self.fixes.action(task, action)
            allowed = ('失败', '已中断', 'Codex 不可用') if action == 'retry' else ('排队中', '运行中', '已中断', '待同步', '待补充')
            if task['kind'] == 'execute':
                allowed += ('已作废',) if action == 'retry' else ('待提交', '等待浏览器', '等待合并')
            if task['status'] not in allowed:
                reject(409, 'task-state-invalid', 'status', '任务当前状态不支持此操作')
            if task['kind'] == 'generate':
                self.generations.action(task, action)
            if action == 'cancel':
                self.update(task_id, status='已放弃', finished_at=now())
                process = self.processes.get(task_id)
                if process:
                    terminate_group(process)
                if self.browser_owner == task_id:
                    self.browser_owner = None
            else:
                directory = self.directory(task_id)
                if directory.exists():
                    archive = directory / ('attempt-' + str(task['attempt']))
                    archive.mkdir()
                    for path in directory.iterdir():
                        if path.is_file() or (task['kind'] in ('execute', 'generate') and path != archive and not path.name.startswith('attempt-')):
                            path.rename(archive / path.name)
                self.executions.next_review.pop(task_id, None)
                self.update(task_id, status='排队中', started_at=None, finished_at=None,
                            session_id=None, error='', result_digest=None, result_json='{}',
                            snapshot_json=canonical(task['snapshot']) if task['kind'] == 'generate' else '{}',
                            void_reason='', rounds_json='[]', attempt=task['attempt'] + 1)
            if task['kind'] == 'generate':
                self.generations.task_status(task_id, '已放弃' if action == 'cancel' else '排队中')
        return dict(ok=True, task=self.read(task_id))

    def prepare_merge(self, task):
        directory = self.directory(task['id'])
        directory.mkdir(parents=True, exist_ok=True)
        snapshot = json.loads(task['input_json'])
        prompt = files('sop_board').joinpath('prompts/merge.md').read_text(encoding='utf-8')
        (directory / 'cases.yaml').write_text(yaml.safe_dump(dict(metadata=snapshot['case_metadata'], test_cases=snapshot['cases']), allow_unicode=True), encoding='utf-8')
        (directory / 'scenarios.yaml').write_text(snapshot['scenarios'], encoding='utf-8')
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
        (directory / 'input.json').write_text(task['input_json'], encoding='utf-8')
        (directory / 'prompt.md').write_text(prompt, encoding='utf-8')
        (directory / 'schema.json').write_text(canonical(SCHEMA), encoding='utf-8')
        return prompt

    def execute(self, task):
        directory = self.directory(task['id'])
        process = None
        error = ''
        session = None
        unavailable = False
        check = None
        outcome = "已完成"
        timed_out = False
        generating = task['kind'] == 'generate'
        snapshot = json.loads(task['input_json']) if generating else {}
        resumed = generating and bool(snapshot.get('thread_id'))
        try:
            directory.mkdir(parents=True, exist_ok=True)
            prompt = self.generations.prepare(task) if generating else self.prepare_merge(task)
            with self.lock:
                if self.stopping.is_set() or self.read(task['id'])['status'] != '运行中':
                    return
                process = resume_codex(directory, snapshot['thread_id'], prompt, schema=directory / 'schema.json') if resumed else self.launcher(directory, prompt)
                self.processes[task['id']] = process
                self.starting = None
            try:
                code = process.wait(timeout=self.timeout)
            except subprocess.TimeoutExpired:
                terminate_group(process)
                code = process.returncode
                timed_out = True
                error = f'Codex 运行超时（{self.timeout:g} 秒）'
            if code:
                error = error or codex_failure(directory, code)
            if resumed and code and not self.stopping.is_set() and not timed_out:
                with self.lock:
                    if self.read(task['id'])['status'] != '运行中':
                        return
                    terminate_group(process)
                    prompt = self.generations.fallback(task, prompt, error)
                    process = self.launcher(directory, prompt)
                    self.processes[task['id']] = process
                    resumed = False
                    error = ''
                try:
                    code = process.wait(timeout=self.timeout)
                except subprocess.TimeoutExpired:
                    terminate_group(process)
                    code = process.returncode
                    error = f'Codex 运行超时（{self.timeout:g} 秒）'
                if code:
                    error = error or codex_failure(directory, code)
            try:
                if not error:
                    outcome, check = self.generations.consume(task) if generating else self.consume_merge_output(task)
                    if not check['ok']:
                        error = '; '.join(str(p) for p in check['problems'])
            except FileNotFoundError:
                error = error or 'Codex 没有写出最终输出'
            except (OSError, json.JSONDecodeError, RecursionError):
                error = error or '最终消息不是 JSON 对象'
            except ValueError as problem:
                error = error or str(problem)
            except (KeyError, TypeError):
                error = error or '最终消息不符合输出模式'
        except FileNotFoundError:
            error = 'Codex 启动失败：命令或任务文件不存在'
            unavailable = process is None
        except sqlite3.Error as problem:
            error = '任务记录无法保存：' + str(problem)
        except (OSError, subprocess.SubprocessError):
            error = 'Codex 运行失败：无法启动进程或读取任务文件'
        except (KeyError, TypeError, ValueError):
            error = '任务输入不合格，请重新生成或修订' if generating else '任务输入不合格，请重新合并'
        finally:
            if process:
                terminate_group(process)
            with self.lock:
                self.processes.pop(task['id'], None)
                if self.starting == task['id']:
                    self.starting = None
                events = directory / 'events.jsonl'
                if resumed:
                    session = snapshot['thread_id']
                if events.exists():
                    for line in events.read_text(encoding='utf-8', errors='replace').splitlines():
                        try:
                            event = json.loads(line)
                            if event.get('type') == 'thread.started' and isinstance(event.get('thread_id'), str):
                                session = event['thread_id']
                                break
                        except (ValueError, AttributeError):
                            continue
                if not self.stopping.is_set() and self.read(task['id'])['status'] == '运行中':
                    if check is None:
                        check = dict(ok=False, problems=[error])
                    elif error and check['ok']:
                        check.update(ok=False, problems=[error])
                    values = dict(status='Codex 不可用' if unavailable else '失败' if error else outcome,
                                finished_at=now(), session_id=session, error=error,
                                result_json=canonical(check),
                                result_digest=hashlib.sha256((directory / 'final.json').read_bytes()).hexdigest() if (directory / 'final.json').is_file() else None)
                    if generating:
                        try:
                            self.generations.finish(task, values, check)
                        except (OSError, ValueError, sqlite3.Error) as problem:
                            check = dict(ok=False, problems=[f'草稿版本无法保存：{problem}'])
                            self.update(task['id'], status='失败', finished_at=now(), error=check['problems'][0], result_json=canonical(check))
                            self.generations.task_status(task['id'], '失败')
                    else:
                        self.update(task['id'], **values)
                    (directory / 'check.json').write_text(canonical(check), encoding='utf-8')

    def consume_merge_output(self, task):
        directory = self.directory(task['id'])
        final = (directory / 'final.json').read_text(encoding='utf-8')
        if not final.strip():
            raise FileNotFoundError('Codex 没有写出最终输出')
        snapshot = json.loads(task['input_json'])
        result = read_merge_output(final)
        raw, check = assemble(snapshot, result)
        if not check['ok']:
            return '失败', check
        if check.get('gaps'):
            return '待补充', check
        (directory / 'scenario-file.yaml').write_text(raw, encoding='utf-8')
        (directory / 'pairs.json').write_text(canonical(dict(covered=check['pairs'], conflict=[], disposed=[])), encoding='utf-8')
        with self.lock:
            if self.stopping.is_set() or self.read(task['id'])['status'] != '运行中':
                return '已中断', dict(check, ok=False, problems=['合并任务已停止，未写入工作区'])
            try:
                return self.land(task, raw, check), check
            except (OSError, ValueError, sqlite3.Error, APIError) as problem:
                return '失败', dict(check, ok=False, problems=[f'合并结果无法写入工作区：{problem}'])

    def land(self, task, raw, check):
        workspace = self.service.workspace
        snapshot = json.loads(task['input_json'])
        workspace.reload()
        if not workspace.configured or snapshot['config_digest'] != workspace.config_digest:
            check['problems'] = ['工作区配置变化，请重新合并']
            return '待同步'
        with self.store.connection() as db:
            batch = self.service.batch(db, task['workspace'], task['batch'])
            cases = self.service.case_union(db, batch)
            metadata = json.loads(db.execute('SELECT metadata FROM case_uploads WHERE batch=? ORDER BY number DESC LIMIT 1', (batch['id'],)).fetchone()[0])
            choices = self.service.flow_choices(db, batch)
            confirmed_draft_cases = self.confirmed_draft_cases(db, batch)
            version = self.service.latest_version(db, batch)
            uploaded_sop = self.service.latest_sop_upload(db, batch)
            # 只看本次快照依据的那次上传，批次中其他草稿确认不影响普通上传的自动执行。
            draft_upload = db.execute('SELECT 1 FROM case_uploads u JOIN draft_confirmations c ON c.upload=u.id '
                                     'WHERE u.batch=? AND u.number=?', (batch['id'], snapshot['upload'])).fetchone()
        if (uploaded_sop['id'] if uploaded_sop else None) != snapshot.get('sop_upload_id'):
            check['problems'] = ['业务 SOP 已更新，请重新合并']
            return '待同步'
        if (canonical(cases) != canonical(snapshot['cases']) or metadata != snapshot['case_metadata']
                or confirmed_draft_cases != snapshot.get('confirmed_draft_cases', {})
                or choices != snapshot['forced_flow'] or (version['content'] if version else '') != snapshot['scenarios']):
            check['problems'] = ['用例、场景或开发者分类选择变化，请重新合并']
            return '待同步'
        source = workspace.scenario_path(task['batch'])
        current = hashlib.sha256(source.read_bytes()).hexdigest() if source.exists() else ''
        if current != task['scenario_file_sha256']:
            check['workspace_changed'] = True
            return '待同步'
        from .service import fingerprint
        document = yaml.safe_load(raw)
        info = workspace.product(workspace.aliases(document))
        source.parent.mkdir(parents=True, exist_ok=True)
        temporary = source.with_suffix('.yaml.tmp')
        temporary.write_text(raw, encoding='utf-8')
        temporary.replace(source)
        case_hashes = {c['id']: hashlib.sha256(canonical(c).encode()).hexdigest() for c in snapshot['cases']}
        self.service.push(workspace.name, task['batch'], dict(workspace=dict(name=workspace.name),
            path=workspace.display_path(source), content=raw, pushed_by=f"看板合并 #{task['id']}",
            commit=info['commit'], dirty=info['dirty'], product_digest=info['product_digest'],
            fingerprints={s['id']: fingerprint(s) for n in document['sops'] for s in n['scenarios']}),
            pairings=[dict(p, case_sha256=case_hashes[p['case']]) for p in check['pairs']],
            auto_execute=draft_upload is None)
        with self.store.connection() as db:
            batch = self.service.batch(db, task['workspace'], task['batch'])
            landed, _ = self.merge_inputs(db, batch, self.service.latest_version(db, batch), self.service.case_union(db, batch))
        check['landed_digest'] = hashlib.sha256(canonical(landed).encode()).hexdigest()
        return '已完成'

    def run(self):
        while not self.stopping.wait(.1):
            with self.lock:
                self.workers = {i: w for i, w in self.workers.items() if w.is_alive()}
                checking = self.service.environment.check_thread
                if checking and checking.is_alive():
                    continue
                if self.service.environment.approved():
                    self.fixes.discover()
                with self.store.connection() as db:
                    if self.service.automation_paused(db):
                        continue
                    queued = [dict(r) for r in db.execute("SELECT * FROM tasks WHERE kind IN ('merge','generate','execute','fix') "
                        "AND status IN ('排队中','待提交','等待浏览器','等待合并') ORDER BY id")]
                    running_batches = {(r['workspace'], r['batch']) for r in db.execute("SELECT workspace,batch FROM tasks WHERE kind='execute' AND status='运行中'")}
                for task in queued:
                    needs_codex = True
                    if self.stopping.is_set():
                        break
                    if task['kind'] == 'fix':
                        if self.service.environment.approved():
                            self.fixes.schedule(task)
                        continue
                    if task['kind'] in ('merge', 'generate') and (task['workspace'], task['batch']) in running_batches:
                        continue
                    if task['kind'] == 'execute':
                        if self.fixes.blocks_execution(task['workspace']):
                            self.update(task['id'], status='排队中', error='等待同一工作区的修复任务')
                            continue
                        try:
                            if not self.executions.prepare(task):
                                continue
                        except (OSError, ValueError, KeyError, APIError, subprocess.SubprocessError) as error:
                            self.update(task['id'], status='失败', finished_at=now(), error=f'无法准备执行快照：{error}')
                            if self.browser_owner == task['id']:
                                self.browser_owner = None
                            continue
                        if (task['workspace'], task['batch']) in running_batches:
                            continue
                        inputs = json.loads(task['input_json'])
                        from .credentials import blocked_results
                        blocked = {row['scenario'] for row in blocked_results(inputs['scenes'], inputs['entries'], os.environ)}
                        runnable = [scene for scene in inputs['scenes'] if scene['id'] not in blocked]
                        needs_codex = bool(runnable)
                        if any(inputs['entries'][s['verify']]['surface'] == 'web' for s in runnable):
                            if self.browser_owner not in (None, task['id']):
                                self.update(task['id'], status='等待浏览器')
                                continue
                            self.browser_owner = task['id']
                    if self.starting is not None or len(self.workers) >= 3:
                        continue
                    if task['kind'] in ('merge', 'generate') and not self.service.workspace.configured:
                        self.update(task['id'], error='尚未搭建，等待配置工作区')
                        continue
                    error = codex_available() if needs_codex else ''
                    if error:
                        self.update(task['id'], status='Codex 不可用', finished_at=now(), error=error)
                        if task['kind'] == 'generate':
                            self.generations.task_status(task['id'], 'Codex 不可用')
                        if self.browser_owner == task['id']:
                            self.browser_owner = None
                        continue
                    self.update(task['id'], status='运行中', started_at=now(), error='')
                    self.starting = task['id']
                    worker = threading.Thread(target=self.executions.execute if task['kind'] == 'execute' else self.execute,
                                              args=(task,), daemon=True)
                    self.workers[task['id']] = worker
                    if task['kind'] == 'execute':
                        running_batches.add((task['workspace'], task['batch']))
                    worker.start()

    def close(self):
        self.stopping.set()
        with self.lock:
            for process in list(self.processes.values()):
                terminate_group(process)
        self.thread.join()
        for worker in self.workers.values():
            worker.join()
