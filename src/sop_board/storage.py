"""SQLite 事务与内容寻址文件存储。"""
from contextlib import contextmanager
import hashlib
import fcntl
import os
import sqlite3
import threading

from .kernel import S


SCHEMA = f"""
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY, value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY, kind TEXT NOT NULL, workspace TEXT NOT NULL REFERENCES workspaces(key),
    batch TEXT NOT NULL, status TEXT NOT NULL, trigger TEXT NOT NULL, input_digest TEXT NOT NULL,
    input_json TEXT NOT NULL, scenario_file_sha256 TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT,
    attempt INTEGER NOT NULL DEFAULT 1, session_id TEXT, error TEXT NOT NULL DEFAULT '', result_digest TEXT,
    result_json TEXT NOT NULL DEFAULT '{{}}', scope_json TEXT NOT NULL DEFAULT '[]',
    snapshot_json TEXT NOT NULL DEFAULT '{{}}', void_reason TEXT NOT NULL DEFAULT '', rounds_json TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS session_tasks (
    task INTEGER PRIMARY KEY REFERENCES tasks(id), session_id TEXT NOT NULL,
    claimed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS author_sop_questions (
    task INTEGER NOT NULL REFERENCES tasks(id), number TEXT NOT NULL,
    body_json TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(task,number)
);
CREATE TABLE IF NOT EXISTS author_sop_answers (
    id INTEGER PRIMARY KEY, task INTEGER NOT NULL, question TEXT NOT NULL,
    text TEXT NOT NULL, author TEXT NOT NULL, answered_at TEXT NOT NULL,
    FOREIGN KEY(task,question) REFERENCES author_sop_questions(task,number)
);
CREATE TABLE IF NOT EXISTS author_sop_registrations (
    task INTEGER PRIMARY KEY REFERENCES tasks(id), version INTEGER NOT NULL REFERENCES sop_versions(id),
    sources_json TEXT NOT NULL, answers_json TEXT NOT NULL, confirmed_by TEXT NOT NULL,
    registered_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS task_workers (
    task INTEGER PRIMARY KEY REFERENCES tasks(id), pid INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS task_receipts (
    task INTEGER NOT NULL REFERENCES tasks(id), attempt INTEGER NOT NULL,
    phase TEXT NOT NULL DEFAULT 'result', result_json TEXT NOT NULL, registered_at TEXT NOT NULL,
    PRIMARY KEY(task,attempt,phase)
);
CREATE TABLE IF NOT EXISTS workspaces (
    key TEXT PRIMARY KEY, root TEXT NOT NULL, name TEXT NOT NULL, defect_prefix TEXT
);
CREATE TABLE IF NOT EXISTS distill_tasks (
    task INTEGER PRIMARY KEY REFERENCES tasks(id), needs_environment INTEGER NOT NULL,
    decision TEXT NOT NULL DEFAULT '', commits_json TEXT NOT NULL DEFAULT '{{}}', decision_at TEXT
);
CREATE TABLE IF NOT EXISTS distill_repositories (
    task INTEGER NOT NULL REFERENCES tasks(id), alias TEXT NOT NULL,
    root TEXT NOT NULL, identity TEXT NOT NULL, base_commit TEXT NOT NULL,
    branch TEXT NOT NULL, worktree TEXT NOT NULL DEFAULT '', PRIMARY KEY(task,alias)
);
CREATE TABLE IF NOT EXISTS distill_registrations (
    task INTEGER NOT NULL REFERENCES tasks(id), attempt INTEGER NOT NULL,
    report_digest TEXT NOT NULL, report_json TEXT NOT NULL, result_json TEXT NOT NULL,
    registered_at TEXT NOT NULL, PRIMARY KEY(task,attempt)
);
CREATE TABLE IF NOT EXISTS fix_tasks (
    task INTEGER PRIMARY KEY REFERENCES tasks(id),
    developer_note TEXT NOT NULL DEFAULT '', retry_reason TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS fix_handoffs (
    result_id INTEGER PRIMARY KEY REFERENCES results(id), requested_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS session_fix_state (
    task INTEGER PRIMARY KEY REFERENCES tasks(id), submitted_json TEXT NOT NULL DEFAULT '{{}}',
    verified_json TEXT NOT NULL DEFAULT '{{}}'
);
CREATE TABLE IF NOT EXISTS session_fix_executions (
    execution INTEGER PRIMARY KEY REFERENCES tasks(id), fix INTEGER NOT NULL REFERENCES tasks(id),
    attempt INTEGER NOT NULL, sequence INTEGER NOT NULL, UNIQUE(fix,attempt,sequence)
);
CREATE TABLE IF NOT EXISTS fix_repositories (
    task INTEGER NOT NULL REFERENCES tasks(id), alias TEXT NOT NULL,
    root TEXT NOT NULL, identity TEXT NOT NULL, baseline_branch TEXT NOT NULL,
    baseline_commit TEXT NOT NULL, fix_branch TEXT NOT NULL, worktree TEXT NOT NULL,
    repair_commit TEXT NOT NULL DEFAULT '', merged_commit TEXT NOT NULL DEFAULT '',
    changed_files_json TEXT NOT NULL DEFAULT '[]', merge_status TEXT NOT NULL DEFAULT '未核对',
    merge_reason TEXT NOT NULL DEFAULT '', PRIMARY KEY(task,alias)
);
CREATE TABLE IF NOT EXISTS fix_problems (
    id INTEGER PRIMARY KEY, task INTEGER NOT NULL REFERENCES tasks(id),
    batch TEXT NOT NULL, scenario TEXT NOT NULL, fingerprint TEXT NOT NULL,
    result_id INTEGER NOT NULL REFERENCES results(id), first_attempt INTEGER NOT NULL,
    category TEXT NOT NULL DEFAULT '', root_repo TEXT, handling TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT '', plan TEXT NOT NULL DEFAULT '', input_json TEXT NOT NULL,
    UNIQUE(task,batch,scenario,fingerprint)
);
CREATE TABLE IF NOT EXISTS fix_attempts (
    task INTEGER NOT NULL REFERENCES tasks(id), number INTEGER NOT NULL,
    started_at TEXT NOT NULL, finished_at TEXT, start_commits_json TEXT NOT NULL,
    commits_json TEXT NOT NULL DEFAULT '{{}}', outcome TEXT NOT NULL DEFAULT '{S.RUNNING}',
    merged_commits_json TEXT NOT NULL DEFAULT '{{}}',
    reason TEXT NOT NULL DEFAULT '', result_json TEXT NOT NULL DEFAULT '{{}}',
    validation_json TEXT NOT NULL DEFAULT '{{}}', added_json TEXT NOT NULL DEFAULT '[]',
    interruptions_json TEXT NOT NULL DEFAULT '[]', PRIMARY KEY(task,number)
);
CREATE TABLE IF NOT EXISTS fix_validations (
    task INTEGER NOT NULL, attempt INTEGER NOT NULL, validation INTEGER NOT NULL,
    batch TEXT NOT NULL, version INTEGER NOT NULL REFERENCES versions(id),
    repositories_json TEXT NOT NULL, product_digest TEXT NOT NULL, session_id TEXT NOT NULL,
    status TEXT NOT NULL, reason TEXT NOT NULL, results_json TEXT NOT NULL,
    evidence_json TEXT NOT NULL, promoted_round INTEGER, skill_digest TEXT,
    PRIMARY KEY(task,attempt,validation,batch),
    FOREIGN KEY(task,attempt) REFERENCES fix_attempts(task,number)
);
CREATE TABLE IF NOT EXISTS fix_jira (
    task INTEGER NOT NULL REFERENCES tasks(id), issue TEXT NOT NULL,
    comment TEXT NOT NULL, transition TEXT NOT NULL, reason TEXT NOT NULL,
    PRIMARY KEY(task,issue)
);
CREATE TABLE IF NOT EXISTS batches (
    id INTEGER PRIMARY KEY, workspace TEXT NOT NULL REFERENCES workspaces(key),
    name TEXT NOT NULL, path TEXT NOT NULL, product_digest TEXT NOT NULL,
    UNIQUE(workspace, name)
);
CREATE TABLE IF NOT EXISTS merge_messages (
    id INTEGER PRIMARY KEY, batch INTEGER NOT NULL REFERENCES batches(id),
    author TEXT NOT NULL, created_at TEXT NOT NULL, gap TEXT, gap_json TEXT NOT NULL,
    text TEXT NOT NULL, status TEXT NOT NULL DEFAULT '待处理', task INTEGER REFERENCES tasks(id),
    source TEXT NOT NULL CHECK(source IN ('看板','会话')), operation TEXT NOT NULL DEFAULT 'reply',
    cases_json TEXT NOT NULL DEFAULT '[]', note TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS merge_messages_batch ON merge_messages(batch,id);
CREATE TABLE IF NOT EXISTS merge_exclusions (
    batch INTEGER NOT NULL REFERENCES batches(id), case_id TEXT NOT NULL,
    message INTEGER NOT NULL REFERENCES merge_messages(id), PRIMARY KEY(batch,case_id)
);
CREATE TABLE IF NOT EXISTS merge_followups (
    batch INTEGER PRIMARY KEY REFERENCES batches(id), requested INTEGER NOT NULL DEFAULT 0,
    dispatched INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS session_merge_followups (
    batch INTEGER PRIMARY KEY REFERENCES batches(id), session_id TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS merge_case_updates (
    task INTEGER NOT NULL REFERENCES tasks(id), batch INTEGER NOT NULL REFERENCES batches(id),
    case_id TEXT NOT NULL, upload INTEGER NOT NULL, messages_json TEXT NOT NULL,
    before_json TEXT NOT NULL, after_json TEXT NOT NULL, reason TEXT NOT NULL,
    PRIMARY KEY(task,case_id)
);
CREATE TABLE IF NOT EXISTS review_completions (
    id INTEGER PRIMARY KEY, batch INTEGER NOT NULL REFERENCES batches(id),
    marker TEXT NOT NULL, completed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS versions (
    id INTEGER PRIMARY KEY, batch INTEGER NOT NULL REFERENCES batches(id),
    version INTEGER NOT NULL, content TEXT NOT NULL, sha256 TEXT NOT NULL,
    path TEXT NOT NULL, commit_id TEXT NOT NULL, dirty INTEGER NOT NULL,
    pushed_by TEXT NOT NULL, pushed_at TEXT NOT NULL, UNIQUE(batch, version)
);
CREATE TABLE IF NOT EXISTS scenarios (
    id INTEGER PRIMARY KEY, batch INTEGER NOT NULL REFERENCES batches(id),
    scenario TEXT NOT NULL, fingerprint TEXT NOT NULL, node TEXT NOT NULL,
    title TEXT NOT NULL, polarity TEXT NOT NULL, priority TEXT NOT NULL,
    verify TEXT NOT NULL, body TEXT NOT NULL, current INTEGER NOT NULL,
    UNIQUE(batch, scenario, fingerprint)
);
CREATE TABLE IF NOT EXISTS regression_links (
    id INTEGER PRIMARY KEY, batch INTEGER NOT NULL REFERENCES batches(id),
    scenario TEXT NOT NULL, fingerprint TEXT NOT NULL, scenario_file TEXT NOT NULL,
    session TEXT NOT NULL, repo TEXT NOT NULL, identity TEXT NOT NULL, root TEXT NOT NULL,
    path TEXT NOT NULL, sha256 TEXT NOT NULL, test_id TEXT NOT NULL, test_name TEXT NOT NULL,
    layer TEXT NOT NULL, command TEXT NOT NULL, commit_id TEXT,
    status TEXT NOT NULL CHECK(status IN ('{S.WAIT_COMMIT}','已提交','{S.ABANDONED}')),
    coverage_json TEXT NOT NULL, pass_evidence_json TEXT NOT NULL, local_run_json TEXT NOT NULL,
    ci_json TEXT NOT NULL, details_json TEXT NOT NULL,
    registered_at TEXT NOT NULL, registered_by TEXT NOT NULL,
    UNIQUE(batch,scenario,fingerprint,identity,path,test_name)
);
CREATE TABLE IF NOT EXISTS evidence (
    sha256 TEXT PRIMARY KEY, size INTEGER NOT NULL, name TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rounds (
    id INTEGER PRIMARY KEY, batch INTEGER NOT NULL REFERENCES batches(id),
    number INTEGER NOT NULL, kind TEXT NOT NULL, version INTEGER NOT NULL REFERENCES versions(id),
    product_commit TEXT, product_digest TEXT, product_dirty INTEGER,
    skill_digest TEXT, implementer TEXT, submitted_by TEXT NOT NULL, submitted_at TEXT NOT NULL,
    idempotency_key TEXT, body_sha256 TEXT, UNIQUE(batch,number), UNIQUE(batch,idempotency_key)
);
CREATE TABLE IF NOT EXISTS round_origins (
    round_id INTEGER PRIMARY KEY REFERENCES rounds(id),
    origin TEXT NOT NULL CHECK(origin IN ('board','session'))
);
CREATE TABLE IF NOT EXISTS round_product_changes (
    round_id INTEGER PRIMARY KEY REFERENCES rounds(id),
    changed_json TEXT NOT NULL DEFAULT '[]', uncommitted_json TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS results (
    id INTEGER PRIMARY KEY, round_id INTEGER NOT NULL REFERENCES rounds(id),
    scenario TEXT NOT NULL, fingerprint TEXT NOT NULL, status TEXT NOT NULL,
    actual TEXT NOT NULL, executed_at TEXT NOT NULL, executor TEXT NOT NULL,
    session TEXT, finding TEXT, note TEXT, independence TEXT NOT NULL,
    UNIQUE(round_id,scenario)
);
CREATE TABLE IF NOT EXISTS result_evidence (
    result_id INTEGER NOT NULL REFERENCES results(id), ordinal INTEGER NOT NULL,
    name TEXT NOT NULL, sha256 TEXT NOT NULL REFERENCES evidence(sha256),
    PRIMARY KEY(result_id,ordinal)
);
CREATE TABLE IF NOT EXISTS fixture_runs (
    id INTEGER PRIMARY KEY, workspace TEXT NOT NULL REFERENCES workspaces(key),
    batch INTEGER NOT NULL REFERENCES batches(id), task INTEGER REFERENCES tasks(id),
    scenario TEXT, directory TEXT NOT NULL, record_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS fixture_runs_task ON fixture_runs(task,batch,scenario);
CREATE TABLE IF NOT EXISTS fixture_run_results (
    run INTEGER NOT NULL REFERENCES fixture_runs(id), result_id INTEGER NOT NULL REFERENCES results(id),
    PRIMARY KEY(run,result_id)
);
CREATE TABLE IF NOT EXISTS atlas_versions (
    workspace TEXT NOT NULL REFERENCES workspaces(key), entry TEXT NOT NULL, version INTEGER NOT NULL,
    task INTEGER REFERENCES tasks(id), created_at TEXT NOT NULL, digests_json TEXT NOT NULL, summary_json TEXT NOT NULL,
    PRIMARY KEY(workspace, entry, version)
);
CREATE TABLE IF NOT EXISTS atlas_feedback (
    id INTEGER PRIMARY KEY, workspace TEXT NOT NULL REFERENCES workspaces(key), entry TEXT NOT NULL,
    task INTEGER REFERENCES tasks(id), scenario TEXT, page TEXT NOT NULL, control TEXT, kind TEXT NOT NULL,
    detail TEXT NOT NULL, evidence TEXT, created_at TEXT NOT NULL, handled_version INTEGER, adopted INTEGER, note TEXT
);
CREATE TABLE IF NOT EXISTS atlas_bindings (
    batch INTEGER NOT NULL REFERENCES batches(id), scenario TEXT NOT NULL, fingerprint TEXT NOT NULL,
    entry TEXT NOT NULL, atlas_version INTEGER NOT NULL, task INTEGER REFERENCES tasks(id), body_json TEXT NOT NULL,
    created_at TEXT NOT NULL, PRIMARY KEY(batch, scenario, fingerprint)
);
CREATE TABLE IF NOT EXISTS atlas_checks (
    id INTEGER PRIMARY KEY, batch INTEGER NOT NULL REFERENCES batches(id), scenario TEXT NOT NULL, fingerprint TEXT NOT NULL,
    item TEXT NOT NULL, text TEXT NOT NULL, location TEXT NOT NULL, status TEXT NOT NULL, ref TEXT, ui_json TEXT NOT NULL,
    note TEXT NOT NULL, decision TEXT, decided_by TEXT, decided_at TEXT, decision_note TEXT, task INTEGER REFERENCES tasks(id),
    created_at TEXT NOT NULL, UNIQUE(batch, scenario, fingerprint, item)
);
CREATE TABLE IF NOT EXISTS round_atlas (
    round_id INTEGER PRIMARY KEY REFERENCES rounds(id), versions_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS batch_runs (
    batch INTEGER PRIMARY KEY REFERENCES batches(id), mode TEXT NOT NULL, round_size INTEGER NOT NULL, status TEXT NOT NULL,
    started_at TEXT NOT NULL, started_by TEXT NOT NULL, updated_at TEXT NOT NULL, reason TEXT NOT NULL, rounds_done INTEGER NOT NULL,
    last_task INTEGER REFERENCES tasks(id)
);
CREATE TABLE IF NOT EXISTS attention_items (
    id INTEGER PRIMARY KEY, workspace TEXT NOT NULL REFERENCES workspaces(key), batch TEXT, kind TEXT NOT NULL,
    scenarios_json TEXT NOT NULL, text TEXT NOT NULL, task INTEGER REFERENCES tasks(id), created_at TEXT NOT NULL,
    created_by TEXT NOT NULL, resolved_at TEXT, resolved_by TEXT, resolution_note TEXT
);
CREATE TABLE IF NOT EXISTS identity_events (
    id INTEGER PRIMARY KEY, workspace TEXT NOT NULL REFERENCES workspaces(key), entry TEXT NOT NULL, identity TEXT NOT NULL,
    kind TEXT NOT NULL, at TEXT NOT NULL, task INTEGER REFERENCES tasks(id)
);
CREATE TABLE IF NOT EXISTS issues (
    id INTEGER PRIMARY KEY, workspace TEXT NOT NULL REFERENCES workspaces(key), number INTEGER NOT NULL, title TEXT NOT NULL,
    kind TEXT NOT NULL, root_cause TEXT NOT NULL, status TEXT NOT NULL, jira TEXT NOT NULL, created_at TEXT NOT NULL,
    created_by TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(workspace, number)
);
CREATE TABLE IF NOT EXISTS issue_scenarios (
    issue INTEGER NOT NULL REFERENCES issues(id), batch TEXT NOT NULL, scenario TEXT NOT NULL,
    result_id INTEGER REFERENCES results(id), linked_at TEXT NOT NULL, PRIMARY KEY(issue, batch, scenario)
);
CREATE TABLE IF NOT EXISTS issue_fixes (
    issue INTEGER NOT NULL REFERENCES issues(id), task INTEGER NOT NULL REFERENCES tasks(id), PRIMARY KEY(issue, task)
);
CREATE TABLE IF NOT EXISTS case_upload_sources (
    batch INTEGER NOT NULL REFERENCES batches(id), upload INTEGER NOT NULL, path TEXT NOT NULL,
    written_sha256 TEXT, PRIMARY KEY(batch, upload)
);
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY, workspace TEXT NOT NULL REFERENCES workspaces(key), batch TEXT, issue INTEGER REFERENCES issues(id),
    text TEXT NOT NULL, author TEXT NOT NULL, at TEXT NOT NULL, source TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS node_numbers (
    batch INTEGER NOT NULL REFERENCES batches(id), section TEXT NOT NULL, node TEXT NOT NULL,
    PRIMARY KEY(batch, section), UNIQUE(batch, node)
);
CREATE TABLE IF NOT EXISTS sop_versions (
    id INTEGER PRIMARY KEY, batch INTEGER NOT NULL REFERENCES batches(id),
    path TEXT NOT NULL, version INTEGER NOT NULL, ref TEXT NOT NULL, commit_id TEXT NOT NULL,
    sha256 TEXT NOT NULL, confirmed_by TEXT NOT NULL, content TEXT NOT NULL, recorded_at TEXT NOT NULL,
    UNIQUE(batch, path, version)
);
CREATE TABLE IF NOT EXISTS sop_uploads (
    version INTEGER PRIMARY KEY REFERENCES sop_versions(id),
    filename TEXT NOT NULL, uploaded_by TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS drafts (
    id INTEGER PRIMARY KEY, batch INTEGER NOT NULL REFERENCES batches(id),
    status TEXT NOT NULL CHECK(status IN ('生成中','待确认','修订中','SOP 已更新','已确认','失败','已放弃')),
    sop_version INTEGER NOT NULL REFERENCES sop_versions(id),
    current_task INTEGER REFERENCES tasks(id), created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS active_draft ON drafts(batch) WHERE status NOT IN ('已确认','已放弃');
CREATE TABLE IF NOT EXISTS draft_versions (
    id INTEGER PRIMARY KEY, draft INTEGER NOT NULL REFERENCES drafts(id), number INTEGER NOT NULL,
    output_json TEXT NOT NULL, task INTEGER NOT NULL UNIQUE REFERENCES tasks(id), thread_id TEXT,
    revision_note TEXT NOT NULL, sop_version INTEGER NOT NULL REFERENCES sop_versions(id),
    created_at TEXT NOT NULL, UNIQUE(draft,number)
);
CREATE TABLE IF NOT EXISTS draft_annotations (
    batch INTEGER NOT NULL REFERENCES batches(id), number INTEGER NOT NULL,
    draft INTEGER NOT NULL REFERENCES drafts(id), version INTEGER NOT NULL,
    target TEXT NOT NULL, kind TEXT NOT NULL, text TEXT NOT NULL, author TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT '待处理' CHECK(status IN ('待处理','已采纳','未采纳','已提问','确认时未处理')),
    processing_note TEXT NOT NULL DEFAULT '', processed_version INTEGER, created_at TEXT NOT NULL,
    PRIMARY KEY(batch,number), FOREIGN KEY(draft,version) REFERENCES draft_versions(draft,number),
    FOREIGN KEY(draft,processed_version) REFERENCES draft_versions(draft,number)
);
CREATE TABLE IF NOT EXISTS draft_events (
    id INTEGER PRIMARY KEY, draft INTEGER NOT NULL REFERENCES drafts(id), version INTEGER NOT NULL,
    annotation INTEGER NOT NULL, author TEXT NOT NULL, note TEXT NOT NULL, created_at TEXT NOT NULL,
    FOREIGN KEY(draft,version) REFERENCES draft_versions(draft,number)
);
CREATE TABLE IF NOT EXISTS draft_confirmations (
    draft INTEGER PRIMARY KEY REFERENCES drafts(id), version INTEGER NOT NULL,
    upload INTEGER NOT NULL UNIQUE REFERENCES case_uploads(id), confirmed_by TEXT NOT NULL, confirmed_at TEXT NOT NULL,
    pending_annotations_json TEXT NOT NULL, unanswered_questions_json TEXT NOT NULL,
    FOREIGN KEY(draft,version) REFERENCES draft_versions(draft,number)
);
CREATE TABLE IF NOT EXISTS case_flow_choices (
    batch INTEGER NOT NULL REFERENCES batches(id), case_id TEXT NOT NULL,
    chosen_at TEXT NOT NULL, PRIMARY KEY(batch,case_id)
);
CREATE TABLE IF NOT EXISTS case_pairings (
    version INTEGER NOT NULL REFERENCES versions(id), case_id TEXT NOT NULL,
    case_sha256 TEXT NOT NULL, scenario TEXT NOT NULL, kind TEXT NOT NULL,
    kind_reason TEXT NOT NULL, PRIMARY KEY(version,case_id)
);
CREATE TABLE IF NOT EXISTS case_uploads (
    id INTEGER PRIMARY KEY, batch INTEGER NOT NULL REFERENCES batches(id),
    number INTEGER NOT NULL, filename TEXT NOT NULL, uploaded_by TEXT NOT NULL,
    uploaded_at TEXT NOT NULL, sha256 TEXT NOT NULL, metadata TEXT NOT NULL,
    case_count INTEGER NOT NULL, added INTEGER NOT NULL, updated INTEGER NOT NULL,
    UNIQUE(batch, number)
);
CREATE TABLE IF NOT EXISTS case_versions (
    id INTEGER PRIMARY KEY, batch INTEGER NOT NULL REFERENCES batches(id),
    case_id TEXT NOT NULL, upload INTEGER NOT NULL, body TEXT NOT NULL,
    UNIQUE(batch, case_id, upload),
    FOREIGN KEY(batch, upload) REFERENCES case_uploads(batch, number)
);
"""


class WorkspaceLock:
    """看板线程与本机会话命令共享的可重入操作锁。"""
    def __init__(self, data):
        self.path = data / '.operation.lock'
        self.mutex = threading.RLock()
        self.depth = 0
        self.fd = None

    def __enter__(self):
        self.mutex.acquire()
        try:
            if self.depth == 0:
                self.fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
                fcntl.flock(self.fd, fcntl.LOCK_EX)
            self.depth += 1
            return self
        except BaseException:
            if self.fd is not None:
                os.close(self.fd)
                self.fd = None
            self.mutex.release()
            raise

    def __exit__(self, *error):
        self.depth -= 1
        if self.depth == 0:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
            os.close(self.fd)
            self.fd = None
        self.mutex.release()


class Store:
    def __init__(self, data):
        self.data = data
        self.lock = threading.RLock()
        with self.connection() as db:
            version = db.execute('PRAGMA user_version').fetchone()[0]
            tables = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' LIMIT 1").fetchone()
            if tables and version != 5:
                raise ValueError('数据格式不是当前验证技能版本，请用 sop-board init 新建数据目录；不读取或迁移旧库')
            db.execute("PRAGMA journal_mode=WAL")
            # 新格式数据库的建表保持事务完整；不读取或升级旧数据库。
            try:
                db.executescript("BEGIN IMMEDIATE;\n" + SCHEMA + "\nPRAGMA user_version=5;")
                db.commit()
            except BaseException:
                db.rollback()
                raise

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.data / "workspace.sqlite3", timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            yield db
        finally:
            db.close()

    @contextmanager
    def transaction(self):
        with self.lock, self.connection() as db:
            self.created_files = []
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                for path in self.created_files:
                    path.unlink(missing_ok=True)
                raise

    def save_evidence(self, db, raw, name):
        sha = hashlib.sha256(raw).hexdigest()
        path = self.data / "evidence" / sha[:2] / sha
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            # Track before writing so even an incomplete write is removed.
            self.created_files.append(path)
            path.write_bytes(raw)
        db.execute("INSERT OR IGNORE INTO evidence VALUES (?,?,?)", (sha, len(raw), name))
        return dict(ok=True, sha256=sha, size=len(raw))
