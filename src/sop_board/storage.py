"""SQLite 事务与内容寻址文件存储。"""

from contextlib import contextmanager
import hashlib
import sqlite3
import threading


SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY, value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY, kind TEXT NOT NULL, workspace TEXT NOT NULL REFERENCES workspaces(key),
    batch TEXT NOT NULL, status TEXT NOT NULL, trigger TEXT NOT NULL, input_digest TEXT NOT NULL,
    input_json TEXT NOT NULL, scenario_file_sha256 TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT,
    attempt INTEGER NOT NULL DEFAULT 1, session_id TEXT, error TEXT NOT NULL DEFAULT '', result_digest TEXT,
    result_json TEXT NOT NULL DEFAULT '{}', scope_json TEXT NOT NULL DEFAULT '[]',
    snapshot_json TEXT NOT NULL DEFAULT '{}', void_reason TEXT NOT NULL DEFAULT '', rounds_json TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS workspaces (
    key TEXT PRIMARY KEY, root TEXT NOT NULL, name TEXT NOT NULL, defect_prefix TEXT
);
CREATE TABLE IF NOT EXISTS fix_tasks (
    task INTEGER PRIMARY KEY REFERENCES tasks(id),
    developer_note TEXT NOT NULL DEFAULT '', retry_reason TEXT NOT NULL DEFAULT ''
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
    commits_json TEXT NOT NULL DEFAULT '{}', outcome TEXT NOT NULL DEFAULT '运行中',
    merged_commits_json TEXT NOT NULL DEFAULT '{}',
    reason TEXT NOT NULL DEFAULT '', result_json TEXT NOT NULL DEFAULT '{}',
    validation_json TEXT NOT NULL DEFAULT '{}', added_json TEXT NOT NULL DEFAULT '[]',
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
