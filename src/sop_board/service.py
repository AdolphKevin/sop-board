"""所有 HTTP 与后续页面共用的业务操作。"""


from datetime import datetime, timezone
import hashlib
import json
from itertools import islice
from io import BytesIO
import re
import threading
from zipfile import ZIP_DEFLATED, ZipFile

import yaml

from .kernel import (EXECUTION_ACTIVE, FIX_ATTENTION, FIX_TERMINAL, OUTPUT_ACTIVE, S, activity_stamp,
                     TaskRepositories, claim_message, queue_positions, queue_view, session_claim, sql_states)

from .task_visibility import project as project_task
from .errors import APIError, reject
from .cases import load_cases
from .entries import public_problems
from .evidence_types import content_type, is_text
from .jira import identify
from .merge_state import read_holds
from .scenario_file import load_scenarios
from .reports import markdown
from .result_files import export_results, load_results
from .regressions import read_links
from .statistics import calculate, pending_scope, ratio
from .validation import Problems, validate_batch, validate_mark, validate_push, validate_round, validate_sop_upload


def now():
    return datetime.now(timezone.utc).isoformat()


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def fingerprint(scenario):
    value = {key: [text.replace("\r\n", "\n").replace("\r", "\n") for text in scenario[key]]
             for key in ("initial_state", "steps", "expected", "forbidden_effects")}
    return hashlib.sha256(canonical(value).encode()).hexdigest()


class Service:
    def __init__(self, store, workspace, environment):
        self.store = store
        self.task_repositories = TaskRepositories(store)
        self.workspace, self.environment = workspace, environment
        self.workspace_problems = {}
        self.merge_callback = None
        self.sop_callback = None
        self.execution_callback = None
        # 发现与合并落地共享锁，防止看板写入在登记版本前被自己的扫描发现。
        from .storage import WorkspaceLock
        self.workspace_lock = WorkspaceLock(store.data)
        from .credentials import declared_in
        with self.store.connection() as db:
            for row in db.execute('SELECT input_json,snapshot_json FROM tasks'):
                for value in row:
                    workspace.credential_names.update(declared_in(json.loads(value)))

    def automation_paused(self, db):
        row = db.execute("SELECT value FROM settings WHERE key='automation_paused'").fetchone()
        return bool(row and row['value'] == 'true')

    def set_automation_paused(self, paused):
        # 与调度共用锁：响应完成后，尚未开始的任务不会越过暂停边界。
        with self.workspace_lock, self.store.transaction() as db:
            db.execute("INSERT INTO settings(key,value) VALUES ('automation_paused',?) "
                       "ON CONFLICT(key) DO UPDATE SET value=excluded.value", ('true' if paused else 'false',))
        return dict(ok=True, paused=paused)

    def auto_execute_enabled(self, db):
        row = db.execute("SELECT value FROM settings WHERE key='auto_execute'").fetchone()
        return row is None or row['value'] != 'false'

    def set_auto_execute(self, body):
        if not isinstance(body, dict) or body.keys() != {'enabled'} or type(body['enabled']) is not bool:
            reject(422, 'invalid-auto-execute', 'enabled', '只接受 enabled 字段，值应为 true 或 false')
        with self.workspace_lock, self.store.transaction() as db:
            db.execute("INSERT INTO settings(key,value) VALUES ('auto_execute',?) "
                       "ON CONFLICT(key) DO UPDATE SET value=excluded.value", ('true' if body['enabled'] else 'false',))
        return dict(ok=True, enabled=body['enabled'])

    def pending_fixes(self, db, key, name=None):
        """只取当前未进入修复的问题；人工结果以登记时间判定复核是否完成。"""
        problems = []
        for batch in db.execute('SELECT * FROM batches WHERE workspace=? AND (? IS NULL OR name=?) ORDER BY id', (key, name, name)):
            for scene in calculate(db, batch)['scenarios']:
                latest = max((r for r in (scene['latest_manual'], scene['latest_agent']) if r),
                             key=lambda r: r['number'], default=None)
                if not latest or not (
                    latest['kind'] == 'manual' and latest['status'] in ('fail', 'blocked') and latest['finding']
                    or latest['kind'] == 'agent' and latest['status'] == 'fail'
                ):
                    continue
                # 已放弃、已分类为非产品问题及已纳入任务的同一结果均不重复建任务。
                if db.execute('SELECT 1 FROM fix_problems p JOIN tasks t ON t.id=p.task WHERE t.workspace=? '
                    f'AND p.batch=? AND p.scenario=? AND p.fingerprint=? AND (p.result_id=? OR (t.status NOT IN ({sql_states(FIX_TERMINAL)}) '
                    'AND EXISTS (SELECT 1 FROM session_tasks s WHERE s.task=t.id))) LIMIT 1',
                    (key, batch['name'], scene['scenario'], scene['fingerprint'], latest['id'])).fetchone():
                    continue
                manual = latest['kind'] == 'manual'
                reviewed = not manual or bool(db.execute(
                    'SELECT 1 FROM review_completions WHERE batch=? AND completed_at>=? LIMIT 1',
                    (batch['id'], latest['submitted_at'])).fetchone())
                problems.append(dict(batch=batch['name'], scenario=scene['scenario'], fingerprint=scene['fingerprint'],
                    result_id=latest['id'], source=latest['kind'], review_ready=reviewed,
                    execution_origin=latest.get('execution_origin','unknown'),
                    board_requested=bool(db.execute('SELECT 1 FROM fix_handoffs WHERE result_id=?',(latest['id'],)).fetchone()),
                    scene=scene['body'], latest_result=latest, previous_agent=scene['latest_agent'],
                    defect_url=latest['finding'] if manual else '',
                    evidence=[dict(e, path=str(self.store.data.resolve() / 'evidence' / e['sha256'][:2] / e['sha256']))
                              for e in latest['evidence']]))
        return problems

    def fix_start_reason(self, db, key):
        if self.automation_paused(db):
            return '自动任务已暂停'
        if db.execute("SELECT 1 FROM tasks WHERE workspace=? AND kind='fix' "
                      f'AND status NOT IN ({sql_states(FIX_TERMINAL)}) LIMIT 1', (key,)).fetchone():
            return '等待当前修复结束'
        if db.execute("SELECT 1 FROM tasks WHERE workspace=? AND kind='execute' "
                      f'AND status IN ({sql_states(EXECUTION_ACTIVE)}) LIMIT 1', (key,)).fetchone():
            return '等待验证运行完'
        return ''

    def fix_review_state(self, db, key, name=None):
        problems = self.pending_fixes(db, key, name)
        waiting = []
        if problems:
            reason = self.fix_start_reason(db, key)
            if reason:
                waiting.append(reason)
            if any(not p['review_ready'] for p in problems):
                waiting.append('等待测试同事复核完成')
        return dict(manual_count=sum(p['source'] == 'manual' for p in problems), waiting=waiting)

    def review_complete(self, key, name, body):
        p = Problems('review-complete-invalid')
        if not isinstance(body, dict):
            p.add('body', '应为只含 marker 的对象')
            p.finish()
        for field in body.keys() - {'marker'}:
            p.add(field, '复核完成只接受 marker 字段')
        p.text(body.get('marker'), 'marker')
        p.finish()
        with self.workspace_lock, self.store.transaction() as db:
            batch = self.batch(db, key, name)
            problems = [p for p in self.pending_fixes(db, key, name) if p['source'] == 'manual']
            if not problems:
                reject(409, 'nothing-to-fix', 'batch', '没有待修复的人工不通过')
            timestamp = now()
            db.execute('INSERT INTO review_completions(batch,marker,completed_at) VALUES (?,?,?)',
                       (batch['id'], body['marker'].strip(), timestamp))
            return dict(ok=True, scenarios=[p['scenario'] for p in problems], completed_at=timestamp,
                        start=self.fix_start_reason(db, key) or '立即',
                        review=self.fix_review_state(db, key, name))

    def upload(self, raw, name):
        with self.store.transaction() as db:
            return self.store.save_evidence(db, raw, name)

    def submit_regressions(self, report):
        from .regressions import submit
        return submit(self, report)

    def evidence(self, sha):
        with self.store.connection() as db:
            row = db.execute("SELECT * FROM evidence WHERE sha256=?", (sha,)).fetchone()
            if not row:
                reject(404, "evidence-not-found", "sha256", "证据不存在")
            return (self.store.data / "evidence" / sha[:2] / sha).read_bytes(), row["name"]

    def batch(self, db, key, name):
        if not db.execute("SELECT 1 FROM workspaces WHERE key=?", (key,)).fetchone():
            reject(404, "workspace-not-found", "workspace", "工作区不存在")
        batch = db.execute("SELECT * FROM batches WHERE workspace=? AND name=?", (key, name)).fetchone()
        if not batch:
            reject(404, "batch-not-found", "batch", "批次不存在")
        return batch

    def environment_status(self, *, local=False):
        result = self.environment.public(local)
        if local:
            jira = getattr(self, 'jira', None)
            result['jira_configured'] = bool(jira and jira.email and jira.token)
        return result

    def entries(self, key, *, local=True):
        if key != self.workspace.name:
            reject(404, 'workspace-not-found', 'workspace', '工作区不存在')
        from .entries import check_references
        with self.store.connection() as db:
            documents = [yaml.safe_load(row[0]) for row in db.execute(
                'SELECT v.content FROM versions v JOIN batches b ON b.id=v.batch WHERE b.workspace=? '
                'AND NOT EXISTS (SELECT 1 FROM versions newer WHERE newer.batch=v.batch AND newer.version>v.version)', (key,))]
        problems = self.workspace.problems + check_references(self.workspace.entries, documents)
        if self.workspace.configured:
            for document in documents:
                try:
                    self.workspace.aliases(document)
                except ValueError as error:
                    problems.append(dict(field='metadata.repos', problem=str(error)))
        entries = self.workspace.entries if local else {
            name: {k: v for k, v in entry.items() if k != 'credential_env'} if isinstance(entry, dict) else {}
            for name, entry in self.workspace.entries.items()}
        return dict(ok=not problems, entries=entries, problems=public_problems(problems, local=local))

    def require_ready(self, key):
        if key != self.workspace.name:
            reject(404, 'workspace-not-found', 'workspace', '工作区不存在')
        self.workspace.reload()
        if not self.environment.approved():
            reject(409, 'workspace-not-ready', 'workspace', '尚未搭建或自检未通过，执行与修复停用')
        declarations = self.entries(key)
        if not declarations['ok']:
            raise APIError(409, 'entries-invalid', declarations['problems'])
        return declarations

    def latest_version(self, db, batch):
        return db.execute("SELECT * FROM versions WHERE batch=? ORDER BY version DESC LIMIT 1", (batch["id"],)).fetchone()

    def batch_etag(self, db, batch):
        numbers = [db.execute(f"SELECT COALESCE(MAX({column}),0) FROM {table} WHERE batch=?",
                              (batch["id"],)).fetchone()[0]
                   for table, column in [("versions", "version"), ("rounds", "number"), ("case_uploads", "number")]]
        etag = 'v%d-r%d-u%d' % tuple(numbers)
        conversation = [dict(r) for r in db.execute('SELECT id,status,task FROM merge_messages WHERE batch=? ORDER BY id', (batch['id'],))]
        if conversation:
            etag += '-clarify' + hashlib.sha256(canonical(conversation).encode()).hexdigest()[:12]
        if self.workspace.configured:
            etag += '-p' + batch['product_digest'][:12]
        sop_version = db.execute('SELECT COALESCE(MAX(id),0) FROM sop_versions WHERE batch=?', (batch['id'],)).fetchone()[0]
        if sop_version:
            etag += f'-s{sop_version}'
        problems = self.workspace_problems.get((batch['workspace'], batch['name']), [])
        if problems:
            etag += '-w' + hashlib.sha256(canonical(problems).encode()).hexdigest()[:12]
        if self.workspace.configured:
            etag += '-e' + hashlib.sha256(canonical(self.entries(batch['workspace'])).encode()).hexdigest()[:12]
        tasks = [dict(r) for r in db.execute("SELECT id,status,scope_json,started_at,finished_at,error,void_reason,rounds_json FROM tasks "
            "WHERE workspace=? AND batch=? AND kind IN ('execute','generate','merge','distill') ORDER BY id", (batch['workspace'], batch['name']))]
        for task in tasks:
            task['session_task'] = session_claim(db, task['id'])
        if tasks:
            etag += '-t' + hashlib.sha256(canonical(tasks).encode()).hexdigest()[:12]
        merges = [dict(r) for r in db.execute("SELECT id,status FROM tasks WHERE workspace=? AND batch=? AND kind='merge' "
            f'AND status IN ({sql_states(OUTPUT_ACTIVE)}) ORDER BY id', (batch['workspace'], batch['name']))]
        positions = queue_positions(db) if any(task['status'] == S.QUEUED for task in merges) else {}
        for task in merges:
            if task['status'] == S.QUEUED:
                task['ahead'] = positions.get(task['id'])
            else:
                task['activity'] = activity_stamp(self.store.data / 'tasks' / str(task['id']) / 'events.jsonl')
        if merges:
            etag += '-merge' + hashlib.sha256(canonical(merges).encode()).hexdigest()[:12]
        drafts = [dict(r) for r in db.execute('SELECT id,status,current_task FROM drafts WHERE batch=? ORDER BY id', (batch['id'],))]
        annotations = [dict(r) for r in db.execute('SELECT * FROM draft_annotations WHERE batch=? ORDER BY number', (batch['id'],))]
        if drafts:
            etag += '-draft' + hashlib.sha256(canonical([drafts, annotations]).encode()).hexdigest()[:12]
        fixes = [dict(r) for r in db.execute('SELECT t.id,t.status,t.error,t.attempt,t.result_json,p.category,p.fingerprint '
            'FROM tasks t JOIN fix_problems p ON p.task=t.id WHERE t.workspace=? AND p.batch=? ORDER BY t.id,p.id',
            (batch['workspace'], batch['name']))]
        if fixes:
            etag += '-f' + hashlib.sha256(canonical(fixes).encode()).hexdigest()[:12]
        regressions = [dict(r) for r in db.execute('SELECT * FROM regression_links WHERE batch=? ORDER BY id', (batch['id'],))]
        if regressions:
            etag += '-reg' + hashlib.sha256(canonical(regressions).encode()).hexdigest()[:12]
        etag += '-pairs' + hashlib.sha256(canonical([self.case_pairings(db, batch), self.flow_choices(db, batch)]).encode()).hexdigest()[:12]
        etag += '-env' + hashlib.sha256(canonical(self.environment.public()).encode()).hexdigest()[:12]
        if self.automation_paused(db):
            etag += '-a1'
        if not self.auto_execute_enabled(db):
            etag += '-auto0'
        review = self.fix_review_state(db, batch['workspace'], batch['name'])
        completion = db.execute('SELECT COALESCE(MAX(id),0) FROM review_completions WHERE batch=?', (batch['id'],)).fetchone()[0]
        etag += '-review' + hashlib.sha256(canonical([review, completion]).encode()).hexdigest()[:12]
        return '"' + etag + '"'

    def state(self, key, name):
        with self.store.connection() as db:
            db.execute("BEGIN")
            batch = self.batch(db, key, name)
            return dict(ok=True, etag=self.batch_etag(db, batch))

    def report_data(self, db, batch, *, local=False):
        summary = calculate(db, batch)
        cases = self.case_union(db, batch)
        pairings = self.case_pairings(db, batch, cases)
        merge = db.execute(f"SELECT id,result_json FROM tasks WHERE workspace=? AND batch=? AND kind='merge' AND status='{S.COMPLETED}' ORDER BY id DESC LIMIT 1",
                           (batch['workspace'], batch['name'])).fetchone()
        fixes, fix_statistics = self.batch_fixes(db, batch)
        return dict(batch=dict(batch), workspace=dict(db.execute("SELECT key,root,name FROM workspaces WHERE key=?", (batch["workspace"],)).fetchone(), repositories=self.workspace.public()["repositories"]),
                    fixes=fixes, fix_statistics=fix_statistics, regression_links=read_links(db, batch, local=local),
                    merge=dict(id=merge['id'], **json.loads(merge['result_json'])) if merge else None,
                    version=self.latest_version(db, batch), summary=summary, cases=cases,
                    pairings=pairings, holds=read_holds(db, batch), exclusions={r['case_id']: dict(r) for r in db.execute(
                        'SELECT e.case_id,m.author,m.created_at,m.text FROM merge_exclusions e JOIN merge_messages m ON m.id=e.message WHERE e.batch=?', (batch['id'],))},
                    coverage=ratio(len(pairings), len(cases)),
                    latest_upload=db.execute("SELECT COALESCE(MAX(number),0) FROM case_uploads WHERE batch=?", (batch["id"],)).fetchone()[0])

    def report(self, key, name, *, local=False):
        with self.store.connection() as db:
            db.execute("BEGIN")
            data = self.report_data(db, self.batch(db, key, name), local=local)
            return markdown(data, datetime.now().astimezone().isoformat()).encode()

    def results_yaml(self, key, name):
        with self.store.connection() as db:
            db.execute("BEGIN")
            return export_results(self.report_data(db, self.batch(db, key, name)))

    def import_results(self, key, name, body):
        with self.store.transaction() as db:
            batch = self.batch(db, key, name)
            current = {row["scenario"]: row["fingerprint"] for row in db.execute(
                "SELECT scenario,fingerprint FROM scenarios WHERE batch=? AND current=1", (batch["id"],))}
            document, prepared, skipped = load_results(body, current)
            if not prepared:
                return dict(ok=True, round=None, imported=0, skipped=skipped)
            version = self.latest_version(db, batch)
            number = db.execute("SELECT COALESCE(MAX(number),0)+1 FROM rounds WHERE batch=?", (batch["id"],)).fetchone()[0]
            latest_upload = db.execute("SELECT COALESCE(MAX(number),0) FROM case_uploads WHERE batch=?", (batch["id"],)).fetchone()[0]
            filename = f"import-{number}.json"
            for result, original in prepared:
                record = dict(uploaded_by=body["uploaded_by"], run_id=document["run_id"], round=document["round"],
                              environment=document["environment"], latest_upload=latest_upload, result=original)
                saved = self.store.save_evidence(db, canonical(record).encode(), filename)
                result["evidence"] = [dict(name=filename, sha256=saved["sha256"])]
            round_body = dict(product=dict(commit=version["commit_id"], digest=batch["product_digest"], dirty=bool(version["dirty"])),
                              submitted_by=body["uploaded_by"], results=[result for result, _ in prepared])
            number = self.insert_round(db, batch, version, round_body, kind="manual")
            return dict(ok=True, round=number, imported=len(prepared), skipped=skipped)

    def upload_cases(self, key, name, body, *, on_upload=None):
        validate_batch(name)
        document = load_cases(body)
        with self.workspace_lock, self.store.transaction() as db:
            if not db.execute("SELECT 1 FROM workspaces WHERE key=?", (key,)).fetchone():
                reject(404, "workspace-not-found", "workspace", "工作区不存在")
            db.execute("INSERT OR IGNORE INTO batches(workspace,name,path,product_digest) VALUES (?,?,'','')", (key, name))
            batch = self.batch(db, key, name)
            previous = db.execute("SELECT * FROM case_uploads WHERE batch=? ORDER BY number DESC LIMIT 1", (batch["id"],)).fetchone()
            sha = hashlib.sha256(body["content"].encode()).hexdigest()
            current = {item["id"]: item for item in self.case_union(db, batch)}
            count = len(document["test_cases"])
            # 草稿确认各登记一次上传，并在同一事务中保存确认记录。
            if previous and previous["sha256"] == sha and on_upload is None:
                return dict(ok=True, upload=previous["number"], added=0, updated=0, unchanged=count,
                            union_count=len(current), unchanged_upload=True)
            number = previous["number"] + 1 if previous else 1
            added = sum(item["id"] not in current for item in document["test_cases"])
            updated = sum(item["id"] in current and item != current[item["id"]] for item in document["test_cases"])
            db.execute("INSERT INTO case_uploads(batch,number,filename,uploaded_by,uploaded_at,sha256,metadata,case_count,added,updated) "
                       "VALUES (?,?,?,?,?,?,?,?,?,?)", (batch["id"], number, body["filename"], body["uploaded_by"], now(),
                       sha, canonical(document["metadata"]), count, added, updated))
            for item in document["test_cases"]:
                db.execute("INSERT INTO case_versions(batch,case_id,upload,body) VALUES (?,?,?,?)",
                           (batch["id"], item["id"], number, canonical(item)))
            result = dict(ok=True, upload=number, added=added, updated=updated,
                          unchanged=count - added - updated, union_count=len(current) + added)
            if on_upload:
                on_upload(db, batch, number)
                if self.merge_callback and self.workspace.configured:
                    self.merge_callback(key, name, trigger="上传用例", connection=db)
        if on_upload is None and self.merge_callback and self.workspace.configured:
            self.merge_callback(key, name, trigger="上传用例")
        return result

    def sop_path(self, name):
        validate_batch(name)
        path = self.workspace.scenarios / (name + '.md')
        if path.is_symlink() or path.resolve().parent != self.workspace.scenarios or path.exists() and not path.is_file():
            reject(422, 'sop-invalid', 'batch', 'SOP 路径须为配置场景目录中的普通文件')
        return path

    def sop_hashes(self):
        if not self.workspace.configured:
            return {}
        return {path.stem: hashlib.sha256(path.read_bytes()).hexdigest()
                for path in sorted(self.workspace.scenarios.glob('*.md'))
                if path.is_file() and not path.is_symlink()}

    def sop_sha256(self, name):
        if not self.workspace.configured:
            return ''
        path = self.workspace.scenarios / (name + '.md')
        return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() and not path.is_symlink() else ''

    def latest_sop_upload(self, db, batch):
        row = db.execute('SELECT v.* FROM sop_versions v JOIN sop_uploads u ON u.version=v.id '
                         'WHERE v.batch=? ORDER BY v.id DESC LIMIT 1', (batch['id'],)).fetchone()
        return dict(row, ref=row['path'].partition(':')[2]) if row else None

    def upload_sop(self, key, name, body, *, on_registered=None):
        validate_batch(name)
        validate_sop_upload(body)
        with self.workspace_lock, self.store.transaction() as db:
            if key != self.workspace.name:
                reject(404, 'workspace-not-found', 'workspace', '工作区不存在')
            self.workspace.reload()
            if not self.workspace.configured:
                reject(422, 'sop-invalid', 'workspace', '尚未搭建，请先配置工作区')
            path = self.sop_path(name)
            current_content = path.read_bytes() if path.exists() else None
            current = hashlib.sha256(current_content).hexdigest() if current_content is not None else ''
            if current != body['base_sha256']:
                reject(409, 'sop-changed', 'base_sha256', '场景目录中的 SOP 原文已在别处修改，请刷新后重试')
            db.execute("INSERT OR IGNORE INTO batches(workspace,name,path,product_digest) VALUES (?,?,'','')", (key, name))
            batch = self.batch(db, key, name)
            label = self.workspace.display_path(path)
            previous = db.execute('SELECT * FROM sop_versions WHERE batch=? AND path=? ORDER BY version DESC LIMIT 1',
                                  (batch['id'], label)).fetchone()
            content = body['content'].encode('utf-8')
            sha = hashlib.sha256(content).hexdigest()
            if current_content == content and previous and previous['content'] == body['content']:
                db.execute('INSERT OR IGNORE INTO sop_uploads(version,filename,uploaded_by) VALUES (?,?,?)',
                           (previous['id'], body['filename'], body['uploaded_by'].strip()))
                if on_registered:
                    on_registered(db, previous)
                return dict(ok=True, version=previous['version'], sha256=current, unchanged=True)
            from .repositories import git
            repository = self.workspace.repos[self.workspace.scenario_repo]
            source = dict(path=label, ref=path.relative_to(repository).as_posix(), content=body['content'],
                          commit=git(repository, 'rev-parse', '--verify', 'HEAD').decode().strip(),
                          sha256=sha, confirmed_by=body['confirmed_by'].strip())
            self.save_sop_sources(db, batch['id'], [source])
            version = db.execute('SELECT id,version FROM sop_versions WHERE batch=? AND path=? ORDER BY version DESC LIMIT 1',
                                 (batch['id'], label)).fetchone()
            # 恢复磁盘原文可能沿用已有版本；只补登记，不覆写该版本已有的上传记录。
            db.execute('INSERT OR IGNORE INTO sop_uploads(version,filename,uploaded_by) VALUES (?,?,?)',
                       (version['id'], body['filename'], body['uploaded_by'].strip()))
            if on_registered:
                on_registered(db, version)
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix('.md.tmp')
            try:
                temporary.write_bytes(content)
                current = hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else ''
                if current != body['base_sha256']:
                    reject(409, 'sop-changed', 'base_sha256', '场景目录中的 SOP 原文已在别处修改，请刷新后重试')
                temporary.replace(path)
            finally:
                temporary.unlink(missing_ok=True)
            result = dict(ok=True, version=version['version'], sha256=sha, unchanged=False)
        if self.sop_callback and (previous is None or version['id'] != previous['id']):
            self.sop_callback(key, name, version=result['version'])
        return result

    def case_union(self, db, batch):
        return [json.loads(row["body"]) for row in db.execute(
            "SELECT v.body FROM case_versions v WHERE v.batch=? AND NOT EXISTS "
            "(SELECT 1 FROM case_versions newer WHERE newer.batch=v.batch AND newer.case_id=v.case_id AND newer.upload>v.upload) "
            "ORDER BY v.case_id", (batch["id"],))]

    def flow_choices(self, db, batch):
        return [r[0] for r in db.execute('SELECT case_id FROM case_flow_choices WHERE batch=? ORDER BY case_id', (batch['id'],))]

    def case_pairings(self, db, batch, cases=None):
        cases = self.case_union(db, batch) if cases is None else cases
        hashes = {c['id']: hashlib.sha256(canonical(c).encode()).hexdigest() for c in cases}
        version = self.latest_version(db, batch)
        if not version:
            return []
        current = {r[0] for r in db.execute('SELECT scenario FROM scenarios WHERE batch=? AND current=1', (batch['id'],))}
        retained = {(h['case'], sid) for h in read_holds(db, batch)['held'] for sid in h.get('scenarios', [])}
        return [dict(case=r['case_id'], scenarios=[r['scenario']], kind=r['kind'], kind_reason=r['kind_reason'], note=r['kind_reason'])
                for r in db.execute('SELECT * FROM case_pairings WHERE version=? ORDER BY case_id', (version['id'],))
                if r['scenario'] in current and (hashes.get(r['case_id']) == r['case_sha256'] or (r['case_id'], r['scenario']) in retained)]

    def save_case_pairings(self, db, version, pairs):
        db.execute('DELETE FROM case_pairings WHERE version=?', (version,))
        db.executemany('INSERT INTO case_pairings(version,case_id,case_sha256,scenario,kind,kind_reason) VALUES (?,?,?,?,?,?)',
            [(version, p['case'], p['case_sha256'], p['scenarios'][0], p['kind'], p['kind_reason']) for p in pairs])

    def read_cases(self, key, name, include_state=False):
        with self.store.connection() as db:
            db.execute("BEGIN")
            batch = self.batch(db, key, name)
            uploads = [dict(upload=row["number"], filename=row["filename"], uploaded_by=row["uploaded_by"],
                            uploaded_at=row["uploaded_at"], case_count=row["case_count"], added=row["added"], updated=row["updated"])
                       for row in db.execute("SELECT * FROM case_uploads WHERE batch=? ORDER BY number", (batch["id"],))]
            cases = self.case_union(db, batch)
            pairings = self.case_pairings(db, batch, cases)
            exclusions = {r['case_id']: dict(r) for r in db.execute('SELECT e.case_id,m.id,m.author,m.created_at,m.text FROM merge_exclusions e JOIN merge_messages m ON m.id=e.message WHERE e.batch=?', (batch['id'],))}
            updates = []
            for row in db.execute('SELECT * FROM merge_case_updates WHERE batch=? ORDER BY task,case_id', (batch['id'],)):
                item = dict(row)
                for key in ('messages', 'before', 'after'):
                    item[key] = json.loads(item.pop(key + '_json'))
                updates.append(item)
            return dict(uploads=uploads, cases=cases, pairings=pairings, holds=read_holds(db, batch), exclusions=exclusions, clarification_updates=updates,
                        flow_choices=self.flow_choices(db, batch), coverage=ratio(len(pairings), len(cases)),
                        **(dict(etag=self.batch_etag(db, batch)) if include_state else {}))

    def cases_yaml(self, key, name):
        with self.store.connection() as db:
            db.execute("BEGIN")
            batch = self.batch(db, key, name)
            latest = db.execute("SELECT metadata FROM case_uploads WHERE batch=? ORDER BY number DESC LIMIT 1", (batch["id"],)).fetchone()
            if not latest:
                reject(404, "cases-not-found", "cases", "尚无用例上传")
            document = dict(metadata=json.loads(latest["metadata"]), test_cases=self.case_union(db, batch))
            return yaml.safe_dump(document, allow_unicode=True, sort_keys=False).encode()

    def scenario_file(self, key, name):
        with self.store.connection() as db:
            batch = self.batch(db, key, name)
            version = self.latest_version(db, batch)
            if not version:
                reject(404, "scenario-file-not-found", "scenario-file", "尚无场景文件")
            return version["content"].encode()

    def read_sop(self, key, name):
        with self.store.connection() as db:
            db.execute('BEGIN')
            batch = self.batch(db, key, name)
            sources = {}
            for row in db.execute('SELECT * FROM sop_versions WHERE batch=? ORDER BY path,version DESC', (batch['id'],)):
                version = dict(row)
                version['authorship'] = [dict(sources=json.loads(r['sources_json']),answers=json.loads(r['answers_json']),
                    confirmed_by=r['confirmed_by'],registered_at=r['registered_at']) for r in db.execute(
                        'SELECT * FROM author_sop_registrations WHERE version=? ORDER BY task',(row['id'],))]
                sources.setdefault(row['path'], []).append(version)
            return sources

    def save_sop_sources(self, db, batch_id, sources):
        """由推送与发现共用；同一路径只与最新原文比较，历史不覆写。"""
        for source in sources:
            last = db.execute('SELECT version,content FROM sop_versions WHERE batch=? AND path=? ORDER BY version DESC LIMIT 1',
                              (batch_id, source['path'])).fetchone()
            if not last or last['content'] != source['content']:
                db.execute('INSERT INTO sop_versions(batch,path,version,ref,commit_id,sha256,confirmed_by,content,recorded_at) VALUES (?,?,?,?,?,?,?,?,?)',
                           (batch_id, source['path'], last['version'] + 1 if last else 1, source['ref'], source['commit'],
                            source['sha256'], source['confirmed_by'], source['content'], now()))

    def insert_round(self, db, batch, version, body, kind="agent", skill_digest=None, *, execution_origin='board'):
        number = db.execute("SELECT COALESCE(MAX(number),0)+1 FROM rounds WHERE batch=?", (batch["id"],)).fetchone()[0]
        product = body["product"]
        cursor = db.execute("INSERT INTO rounds(batch,number,kind,version,product_commit,product_digest,product_dirty,"
                            "implementer,submitted_by,submitted_at,idempotency_key,body_sha256,skill_digest) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (batch["id"], number, kind, version["id"], product["commit"], product["digest"], product["dirty"],
                             body.get("implementer"), body["submitted_by"], now(), body.get("idempotency_key"),
                             hashlib.sha256(canonical(body).encode()).hexdigest(), skill_digest))
        round_id = cursor.lastrowid
        db.execute('INSERT INTO round_origins(round_id,origin) VALUES (?,?)', (round_id, execution_origin))
        for row in body["results"]:
            implementer = body.get("implementer")
            fold = lambda name: " ".join(name.casefold().split())
            independence = ("undeclared" if implementer is None else
                            "self-check" if fold(implementer) == fold(row["executor"]) else "independent")
            cursor = db.execute("INSERT INTO results(round_id,scenario,fingerprint,status,actual,executed_at,executor,session,"
                                "finding,note,independence) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                                (round_id, row["scenario"], row["fingerprint"], row["status"], row["actual"], row["executed_at"],
                                 row["executor"], row.get("session"), row.get("finding"), row.get("note"), independence))
            for ordinal, evidence in enumerate(row["evidence"], 1):
                db.execute("INSERT INTO result_evidence VALUES (?,?,?,?)", (cursor.lastrowid, ordinal, evidence["name"], evidence["sha256"]))
        return number

    def register_round(self, db, batch, version, body, *, skill_digest=None):
        """看板、会话与手工 Web 结果共用的轮次核对和登记入口。"""
        if isinstance(body, dict) and isinstance(body.get('idempotency_key'), str):
            existing = db.execute('SELECT * FROM rounds WHERE batch=? AND idempotency_key=?', (batch['id'], body['idempotency_key'])).fetchone()
            if existing:
                if hashlib.sha256(canonical(body).encode()).hexdigest() != existing['body_sha256']:
                    reject(409, 'idempotency-conflict', 'idempotency_key', '同一幂等键的正文不同')
                return existing['number']
        validate_round(db, body)
        if not version or body['scenario_file_sha256'] != version['sha256']:
            reject(409, 'scenario-file-stale', 'scenario_file_sha256', '场景文件不是当前版本')
        self.check_scenarios(db, batch, body['results'])
        return self.insert_round(db, batch, version, body, skill_digest=skill_digest,
                                 execution_origin=body.get('execution_origin', 'board'))

    def round(self, key, name, body):
        with self.store.transaction() as db:
            batch = self.batch(db, key, name)
            number = self.register_round(db, batch, self.latest_version(db, batch), body)
            summary = calculate(db, batch)
            return dict(ok=True, round=number, statistics=summary['statistics'], queues=summary['queues'])

    def check_scenarios(self, db, batch, results):
        current = {row["scenario"]: row["fingerprint"] for row in db.execute(
            "SELECT scenario,fingerprint FROM scenarios WHERE batch=? AND current=1", (batch["id"],))}
        problems = [dict(field=f"results[{index}].fingerprint", problem=f"场景 {row['scenario']} 不在当前版本或内容指纹已变更")
                    for index, row in enumerate(results) if current.get(row["scenario"]) != row["fingerprint"]]
        if problems:
            raise APIError(409, "scenario-stale", problems)

    def execution_options(self, db, batch, summary, *, local=True):
        # 产品指纹取发现线程每 5 秒更新的批次记录，页面与轮询不重读仓库文件；点击入队时再按当时内容精确计算。
        problems = self.entries(batch['workspace'], local=local)['problems']
        reason = '尚未搭建：' + problems[0]['problem'] if problems else ''
        count = len(pending_scope(db, batch, summary, batch['product_digest']))
        if not reason and not self.environment.approved():
            reason = '自检未通过'
        if not reason and not summary['execution_count']:
            reason = '没有可执行场景（用例待补充）' if summary['scenario_count'] else '尚无场景'
        return dict(pending_count=count, total_count=summary['execution_count'],
                    pending_reason=reason or ('没有待执行场景' if not count else ''), all_reason=reason)

    def task_links(self, db, batch, scenario=None):
        from .distill_inputs import pending_validation
        result = []
        for row in db.execute("SELECT id,kind,status,CASE WHEN status=? THEN input_json ELSE '{}' END AS input_json,scope_json "
                "FROM tasks WHERE workspace=? AND batch=? AND kind='distill' ORDER BY id", (S.WAIT_VALIDATION, batch['workspace'], batch['name'])):
            if scenario is not None and scenario not in {s['scenario'] for s in json.loads(row['scope_json'])}:
                continue
            result.append(dict(id=row['id'], kind=row['kind'], status=row['status'],
                validation_scenarios=pending_validation(row['status'], json.loads(row['input_json']))))
        return result

    def summary(self, db, batch, *, local=True):
        result = calculate(db, batch)
        execution = self.execution_options(db, batch, result, local=local)
        result['scenario_titles'] = {scene['scenario']: scene['title'] for scene in result.pop('scenarios')}
        version = self.latest_version(db, batch)
        _, fix_statistics = self.batch_fixes(db, batch)
        tasks = []
        by_number = {r['number']: r for r in result['rounds']}
        for row in db.execute("SELECT id,kind,trigger,status,scope_json,started_at,finished_at,error,void_reason,rounds_json "
                              "FROM tasks WHERE workspace=? AND batch=? AND kind='execute' ORDER BY id", (batch['workspace'], batch['name'])):
            task = dict(row)
            task['session_task'] = session_claim(db, task['id'])
            task['ownership'] = claim_message(task['session_task'])
            task['scope'] = json.loads(task.pop('scope_json'))
            task['rounds'] = json.loads(task.pop('rounds_json'))
            # 沿用与重跑按任务登记的轮次计算，看板、会话与旧任务一致。
            for key in ('reused_count', 'rerun_count'):
                task[key] = sum(by_number[n][key] for n in task['rounds'] if not isinstance(n, dict) and n in by_number)
            tasks.append(project_task(task, local=local))
        return dict(ok=True, etag=self.batch_etag(db, batch), name=batch["name"], version=version["version"] if version else None,
                    review=self.fix_review_state(db, batch['workspace'], batch['name']),
                    execution=execution, execution_tasks=tasks, task_links=self.task_links(db, batch), fix_statistics=fix_statistics, automation_paused=self.automation_paused(db),
                    auto_execute=self.auto_execute_enabled(db), holds=read_holds(db, batch),
                    regression_links=read_links(db, batch, local=local),
                    pushed_at=version["pushed_at"] if version else None,
                    pushed_by=version["pushed_by"] if version else None,
                    dirty=bool(version["dirty"]) if version else None,
                    commit=version["commit_id"] if version else None,
                    configured=self.workspace.configured, enabled=self.environment.approved(),
                    sop_sha256=self.sop_sha256(batch['name']),
                    entry_problems=self.entries(batch['workspace'], local=local)['problems'] if self.workspace.configured else [],
                    workspace_problems=public_problems(self.workspace_problems.get((batch["workspace"], batch["name"]), []), local=local),
                    scenario_file_sha256=version["sha256"] if version else None, product_digest=batch["product_digest"], **result)

    def read_batch(self, key, name, scenarios=False, *, local=True):
        with self.store.connection() as db:
            db.execute("BEGIN")
            batch = self.batch(db, key, name)
            if scenarios:
                summary = calculate(db, batch)
                version = self.latest_version(db, batch)
                document = yaml.safe_load(version['content']) if version else {'sops': []}
                choices = {}
                if local and version:
                    from .distill_inputs import eligibility
                    product = self.workspace.product(self.workspace.aliases(document)) if self.workspace.configured else dict(
                        product_digest=batch['product_digest'], dirty=False)
                    choices = eligibility(self, db, batch, document,
                        [s for n in document['sops'] for s in n['scenarios']], product['product_digest'], allow_stale_product=product['dirty'])
                for scene in summary['scenarios']:
                    scene['fix_tasks'] = self.scenario_fixes(db, batch, scene['scenario'], scene['fingerprint'])
                    scene['regression_links'] = read_links(db, batch, scene['scenario'], local=local)
                    if local:
                        option = choices.get(scene['scenario'], dict(eligible=False, reasons=['场景尚未同步']))
                        scene['distill'] = {k: option[k] for k in ('eligible', 'reasons')}
                        problems = self.workspace_problems.get((key, name), [])
                        if problems:
                            scene['distill'] = dict(eligible=False, reasons=['当前场景文件不合格：' + p['problem'] for p in problems])
                nodes = yaml.safe_load(version["content"])["sops"] if version else []
                return dict(ok=True, scenarios=summary["scenarios"], filters=summary["filters"], nodes=nodes, task_links=self.task_links(db, batch),
                            holds=read_holds(db, batch), auto_execute=self.auto_execute_enabled(db),
                            review=self.fix_review_state(db, key, name),
                            execution=self.execution_options(db, batch, summary, local=local),
                            has_version=version is not None, etag=self.batch_etag(db, batch))
            return self.summary(db, batch, local=local)

    def workspaces(self, *, local=True):
        with queue_view(), self.store.connection() as db:
            db.execute("BEGIN")
            workspace = dict(key=self.workspace.name, name=self.workspace.name, **{
                'sop_hashes': self.sop_hashes(),
                'repositories': self.workspace.public()['repositories'], 'configured': self.workspace.configured,
                'enabled': self.environment.approved(), 'auto_execute': self.auto_execute_enabled(db),
                'batches': [self.summary(db, batch, local=local) for batch in db.execute('SELECT * FROM batches WHERE workspace=? ORDER BY name', (self.workspace.name,))],
                'fix_tasks': [dict(r) for r in db.execute(f"SELECT id,status,error FROM tasks WHERE workspace=? AND kind='fix' AND status NOT IN ({sql_states(FIX_TERMINAL)}) ORDER BY id", (self.workspace.name,))]})
            for task in workspace['fix_tasks']:
                task['session_task'] = session_claim(db, task['id'])
                task['ownership'] = claim_message(task['session_task'])
                task['kind'] = 'fix'
            workspace['fix_tasks'] = [project_task(task, local=local) for task in workspace['fix_tasks']]
            return dict(ok=True, workspaces=[workspace], automation_paused=self.automation_paused(db))

    def read_round(self, key, name, number, *, local=True):
        with self.store.connection() as db:
            db.execute("BEGIN")
            batch = self.batch(db, key, name)
            row = db.execute("SELECT r.*,COALESCE(o.origin,'unknown') AS execution_origin FROM rounds r LEFT JOIN round_origins o ON o.round_id=r.id WHERE r.batch=? AND r.number=?", (batch["id"], number)).fetchone()
            if not row:
                reject(404, "round-not-found", "round", "轮次不存在")
            results = []
            for stored in db.execute("SELECT * FROM results WHERE round_id=? ORDER BY id", (row["id"],)):
                item = dict(stored)
                item["evidence"] = [dict(e) for e in db.execute(
                    "SELECT name,sha256 FROM result_evidence WHERE result_id=? ORDER BY ordinal", (stored["id"],))]
                for key_ in ("id", "round_id"):
                    item.pop(key_)
                for key_ in ("session", "finding", "note"):
                    if item[key_] is None:
                        item.pop(key_)
                if not local:
                    item.pop('session', None)
                results.append(item)
            from .statistics import round_identity
            version = db.execute("SELECT version FROM versions WHERE id=?", (row["version"],)).fetchone()[0]
            return dict(ok=True, round=row["number"], kind=row["kind"], version=version, execution_origin=row["execution_origin"], **round_identity(results),
                        product=dict(commit=row["product_commit"], digest=row["product_digest"], dirty=bool(row["product_dirty"])),
                        implementer=row["implementer"], submitted_by=row["submitted_by"], submitted_at=row["submitted_at"],
                        results=results)

    def read_scenario(self, key, name, scenario, *, local=True):
        """详情保留当前与历史内容，不受 calculate 的当前版本过滤影响。"""
        with self.store.connection() as db:
            db.execute("BEGIN")
            batch = self.batch(db, key, name)
            records = [dict(row) for row in db.execute(
                "SELECT * FROM scenarios WHERE batch=? AND scenario=? ORDER BY current DESC,id DESC",
                (batch["id"], scenario))]
            if not records:
                reject(404, "scenario-not-found", "scenario", "场景不存在")
            results = [dict(row) for row in db.execute(
                "SELECT r.*,t.number,t.kind,t.submitted_by,t.submitted_at,t.implementer,COALESCE(o.origin,'unknown') AS execution_origin FROM results r JOIN rounds t ON t.id=r.round_id LEFT JOIN round_origins o ON o.round_id=t.id "
                "WHERE t.batch=? AND r.scenario=? ORDER BY t.number,r.id", (batch["id"], scenario))]
            for row in results:
                if not local:
                    row.pop("session", None)
                row["evidence"] = [dict(item) for item in db.execute(
                    "SELECT r.name,r.sha256,e.name AS stored_name FROM result_evidence r JOIN evidence e ON e.sha256=r.sha256 "
                    "WHERE r.result_id=? ORDER BY r.ordinal", (row["id"],))]
                for evidence in row["evidence"]:
                    evidence["content_type"] = content_type(evidence.pop("stored_name"))
                    if is_text(evidence["content_type"]):
                        sha = evidence["sha256"]
                        with (self.store.data / "evidence" / sha[:2] / sha).open(encoding="utf-8", errors="replace") as source:
                            lines = list(islice(source, 201))
                            evidence["preview"] = "".join(lines[:200])
                            evidence["preview_truncated"] = len(lines) > 200
            groups = []
            for record in records:
                groups.append(dict(fingerprint=record["fingerprint"], current=bool(record["current"]),
                                   fix_tasks=self.scenario_fixes(db, batch, scenario, record['fingerprint']),
                                   body=json.loads(record["body"]), results=[r for r in results if r["fingerprint"] == record["fingerprint"]]))
            latest = records[0]
            sop_sources = [dict(row) for row in db.execute(
                'SELECT path,content FROM sop_versions v WHERE batch=? AND NOT EXISTS '
                '(SELECT 1 FROM sop_versions newer WHERE newer.batch=v.batch AND newer.path=v.path AND newer.version>v.version) ORDER BY path',
                (batch['id'],))]
            return dict(workspace=key, batch=name, etag=self.batch_etag(db, batch), scenario=scenario, node=latest["node"], title=latest["title"],
                        holds=read_holds(db, batch),
                        task_links=self.task_links(db, batch, scenario),
                        regression_links=read_links(db, batch, scenario, local=local),
                        review=self.fix_review_state(db, key, name),
                        polarity=latest["polarity"], priority=latest["priority"], verify=latest["verify"],
                        fingerprint=latest["fingerprint"], current=bool(latest["current"]),
                        pushed_at=self.latest_version(db, batch)["pushed_at"], groups=groups, sop_sources=sop_sources,
                        summary=next((item for item in calculate(db, batch)["scenarios"] if item["scenario"] == scenario), None))

    def scenario_fixes(self, db, batch, scenario, fingerprint):
        records = []
        for row in db.execute('SELECT t.id,t.status,t.result_json,t.rounds_json,p.category FROM tasks t JOIN fix_problems p ON p.task=t.id '
            'WHERE t.workspace=? AND p.batch=? AND p.scenario=? AND p.fingerprint=? ORDER BY t.id',
            (batch['workspace'], batch['name'], scenario, fingerprint)):
            item = dict(id=row['id'],status=row['status'],category=row['category'])
            if session_claim(db,row['id']):
                item.update(repair_origin='session',root_cause=json.loads(row['result_json']).get('root_cause',''),rounds=json.loads(row['rounds_json']),
                    changed_files=[r['alias']+'/'+path for r in self.task_repositories.rows(row['id'],'fix',connection=db)
                        for path in json.loads(r['changed_files_json'])])
            records.append(item)
        return records

    def batch_fixes(self, db, batch):
        records = []
        for row in db.execute("SELECT t.id,t.status,t.error,t.attempt,t.result_json FROM tasks t "
            "JOIN fix_tasks f ON f.task=t.id WHERE t.workspace=? AND EXISTS "
            "(SELECT 1 FROM fix_problems p WHERE p.task=t.id AND p.batch=?) ORDER BY t.id", (batch['workspace'], batch['name'])):
            record = dict(row)
            record['root_cause'] = json.loads(record.pop('result_json')).get('root_cause', '')
            record['scenarios'] = [p[0] for p in db.execute('SELECT scenario FROM fix_problems WHERE task=? AND batch=? ORDER BY id', (row['id'], batch['name']))]
            record['problems'] = len(record['scenarios'])
            record['root_repositories'] = [dict(r) for r in db.execute('SELECT batch,scenario,root_repo FROM fix_problems WHERE task=? ORDER BY id', (row['id'],))]
            record['repositories'] = [{key: repo[key] for key in ('alias','identity','baseline_branch','repair_commit','merged_commit',
                'changed_files_json','merge_status','merge_reason')} for repo in self.task_repositories.rows(row['id'], 'fix', connection=db)]
            for repo in record['repositories']:
                repo['changed_files'] = json.loads(repo.pop('changed_files_json'))

            record['jira'] = [dict(r) for r in db.execute('SELECT issue,comment,transition,reason FROM fix_jira WHERE task=? ORDER BY issue', (row['id'],))]
            records.append(record)
        stats = dict(tasks=len(records), problems=sum(t['problems'] for t in records),
            merged=sum(t['status'] == S.MERGED for t in records),
            session_closed=sum(t['status'] == S.HANDLED for t in records),
            developer=sum(t['status'] in FIX_ATTENTION for t in records),
            first_pass=sum(t['status'] in (S.MERGED,S.HANDLED) and t['attempt'] == 1 for t in records),
            attempts=sum(t['attempt'] for t in records))
        return records, stats

    def export_round(self, key, name, number):
        round_data = self.read_round(key, name, number)
        output = BytesIO()
        rows, ordinal = [], 0
        with ZipFile(output, "w", compression=ZIP_DEFLATED) as archive:
            for result in round_data["results"]:
                row = {field: result[field] for field in ("scenario", "status", "actual", "executed_at", "executor")}
                row["evidence"] = []
                for evidence in result["evidence"]:
                    ordinal += 1
                    filename = evidence["name"].replace("\\", "/").rsplit("/", 1)[-1]
                    filename = "".join(char for char in filename if ord(char) >= 32 and ord(char) != 127)
                    if filename in ("", ".", ".."):
                        filename = "evidence.bin"
                    path = f"evidence/{ordinal}-{filename}"
                    raw, _ = self.evidence(evidence["sha256"])
                    archive.writestr(path, raw)
                    row["evidence"].append(path)
                for field in ("session", "finding", "note"):
                    if field in result:
                        row[field] = result[field]
                rows.append(canonical(row))
            archive.writestr("results.jsonl", "\n".join(rows) + "\n")
        return output.getvalue()

    def mark(self, key, name, body):
        with self.store.transaction() as db:
            batch = self.batch(db, key, name)
            validate_mark(body)
            current = db.execute("SELECT fingerprint FROM scenarios WHERE batch=? AND scenario=? AND current=1",
                                 (batch["id"], body["scenario"])).fetchone()
            if not current:
                reject(409, "scenario-stale", "scenario", f"场景 {body['scenario']} 不在当前版本")
            if current["fingerprint"] != body["fingerprint"]:
                reject(409, "scenario-stale", "fingerprint", "场景内容指纹已变更")
            version = self.latest_version(db, batch)
            number = db.execute("SELECT COALESCE(MAX(number),0)+1 FROM rounds WHERE batch=?", (batch["id"],)).fetchone()[0]
            timestamp = now()
            observation = dict(marker=body["marker"], status=body["status"], recorded_at=timestamp)
            for field in ("defect_url", "note"):
                if body.get(field):
                    observation[field] = body[field]
            filename = f"observation-{number}.json"
            saved = self.store.save_evidence(db, canonical(observation).encode(), filename)
            result = dict(scenario=body["scenario"], fingerprint=body["fingerprint"], status=body["status"],
                          actual="人工复核通过", executor=body["marker"], executed_at=timestamp,
                          evidence=[dict(name=filename, sha256=saved["sha256"])])
            if body["status"] in ("fail", "blocked"):
                label = S.FAILED if body["status"] == "fail" else "受阻"
                result["finding"] = body["defect_url"]
                result["actual"] = f"人工复核不通过（{label}），缺陷 {body['defect_url']}"
            if "note" in body:
                result["note"] = body["note"]
            round_body = dict(product=dict(commit=version["commit_id"], digest=batch["product_digest"], dirty=bool(version["dirty"])),
                              submitted_by=body["marker"], results=[result])
            number = self.insert_round(db, batch, version, round_body, kind="manual")
            summary = calculate(db, batch)
            scenario = next(item for item in summary["scenarios"] if item["scenario"] == body["scenario"])
            scenario["latest_manual"]["jira_link"] = bool(identify(scenario["latest_manual"]["finding"]))
            return dict(ok=True, round=number, scenario=scenario, filters=summary["filters"],
                        review=self.fix_review_state(db, key, name))

    def push(self, key, batch, body, *, pairings=None, auto_execute=True, on_registered=None):
        validate_push(body)
        document = load_scenarios(body["content"])
        if document["metadata"]["batch"] != batch:
            reject(422, "workspace-mismatch", "metadata.batch", "批次与地址不一致")
        if body["workspace"]["name"] != key or key != self.workspace.name:
            reject(422, "workspace-mismatch", "workspace.name", "工作区名称与地址不一致")
        if self.workspace.configured:
            try:
                self.workspace.aliases(document)
                body = dict(body, path=self.workspace.display_path(self.workspace.scenario_path(batch)))
            except ValueError as error:
                reject(422, 'scenario-file-invalid', 'metadata.repos', str(error))
        scenarios = [(node["id"], scenario) for node in document["sops"]
                     for scenario in node["scenarios"]]
        computed = {s["id"]: fingerprint(s) for _, s in scenarios}
        supplied = body["fingerprints"]
        mismatches = [dict(field=f"fingerprints.{s}", problem=f"场景 {s} 的内容指纹不一致")
                      for s in sorted(computed.keys() | supplied.keys()) if computed.get(s) != supplied.get(s)]
        if mismatches:
            raise APIError(409, "fingerprint-mismatch", mismatches)
        with self.workspace_lock, self.store.transaction() as db:
            db.execute("INSERT INTO batches(workspace,name,path,product_digest) VALUES (?,?,?,?) "
                       "ON CONFLICT(workspace,name) DO UPDATE SET path=excluded.path, product_digest=excluded.product_digest",
                       (key, batch, body["path"], body["product_digest"]))
            batch_id = db.execute("SELECT id FROM batches WHERE workspace=? AND name=?", (key, batch)).fetchone()[0]
            self.save_sop_sources(db, batch_id, body.get('sop_sources', []))
            previous = db.execute("SELECT * FROM versions WHERE batch=? ORDER BY version DESC LIMIT 1", (batch_id,)).fetchone()
            if previous and previous["content"] == body["content"]:
                if pairings is not None:
                    self.save_case_pairings(db, previous["id"], pairings)
                if on_registered:
                    on_registered(db, batch_id)
                return dict(ok=True, version=previous["version"], added=[], changed=[], removed=[], unchanged=True)
            version = previous["version"] + 1 if previous else 1
            old = {row["scenario"]: row["fingerprint"] for row in db.execute(
                "SELECT scenario,fingerprint FROM scenarios WHERE batch=? AND current=1", (batch_id,))}
            added = [s for s in computed if s not in old]
            changed = [s for s in computed if s in old and computed[s] != old[s]]
            removed = [s for s in old if s not in computed]
            db.execute("INSERT INTO versions(batch,version,content,sha256,path,commit_id,dirty,pushed_by,pushed_at) "
                       "VALUES (?,?,?,?,?,?,?,?,?)", (batch_id, version, body["content"],
                       hashlib.sha256(body["content"].encode()).hexdigest(), body["path"],
                       body["commit"], body["dirty"], body["pushed_by"], now()))
            if pairings is not None:
                version_id = db.execute('SELECT id FROM versions WHERE batch=? AND version=?', (batch_id, version)).fetchone()[0]
                self.save_case_pairings(db, version_id, pairings)
            db.execute("UPDATE scenarios SET current=0 WHERE batch=?", (batch_id,))
            for node, scenario in scenarios:
                db.execute("INSERT INTO scenarios(batch,scenario,fingerprint,node,title,polarity,priority,verify,body,current) "
                           "VALUES (?,?,?,?,?,?,?,?,?,1) ON CONFLICT(batch,scenario,fingerprint) DO UPDATE SET "
                           "node=excluded.node,title=excluded.title,polarity=excluded.polarity,priority=excluded.priority,"
                           "verify=excluded.verify,body=excluded.body,current=1",
                           (batch_id, scenario["id"], fingerprint(scenario), node,
                           scenario["title"], scenario["polarity"], scenario["priority"], scenario["verify"],
                           canonical(scenario)))
            if on_registered:
                on_registered(db, batch_id)
        landed = pairings is not None
        with self.store.connection() as db:
            has_cases = bool(self.case_union(db, self.batch(db, key, batch)))
            auto_execute = auto_execute and self.auto_execute_enabled(db)
        if auto_execute and self.execution_callback and self.workspace.configured and (landed or not has_cases):
            self.execution_callback(key, batch, scope=[dict(scenario=s, reason='new' if s in added else 'changed')
                for s in computed if s in added or s in changed], trigger='合并写入' if landed else '场景版本')
        if self.merge_callback and not landed and self.workspace.configured:
            self.merge_callback(key, batch, trigger='场景文件变化')
        return dict(ok=True, version=version, added=added, changed=changed, removed=removed, unchanged=False)
