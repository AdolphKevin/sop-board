"""SQLite 事务与内容寻址文件存储。"""

from contextlib import contextmanager
import hashlib
import sqlite3
import threading


SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY, kind TEXT NOT NULL, project TEXT NOT NULL REFERENCES projects(key),
    batch TEXT NOT NULL, status TEXT NOT NULL, trigger TEXT NOT NULL, input_digest TEXT NOT NULL,
    input_json TEXT NOT NULL, scenario_file_sha256 TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT,
    attempt INTEGER NOT NULL DEFAULT 1, session_id TEXT, error TEXT NOT NULL DEFAULT '', result_digest TEXT
);
CREATE TABLE IF NOT EXISTS projects (
    key TEXT PRIMARY KEY, identity TEXT NOT NULL, name TEXT NOT NULL, defect_prefix TEXT
);
CREATE TABLE IF NOT EXISTS batches (
    id INTEGER PRIMARY KEY, project TEXT NOT NULL REFERENCES projects(key),
    name TEXT NOT NULL, path TEXT NOT NULL, product_digest TEXT NOT NULL,
    UNIQUE(project, name)
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
    verify TEXT NOT NULL, regression TEXT, body TEXT NOT NULL, current INTEGER NOT NULL,
    UNIQUE(batch, scenario, fingerprint)
);
CREATE TABLE IF NOT EXISTS evidence (
    sha256 TEXT PRIMARY KEY, size INTEGER NOT NULL, name TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rounds (
    id INTEGER PRIMARY KEY, batch INTEGER NOT NULL REFERENCES batches(id),
    number INTEGER NOT NULL, kind TEXT NOT NULL, version INTEGER NOT NULL REFERENCES versions(id),
    product_commit TEXT, product_digest TEXT, product_dirty INTEGER,
    implementer TEXT, submitted_by TEXT NOT NULL, submitted_at TEXT NOT NULL,
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
CREATE TABLE IF NOT EXISTS batch_entries (
    batch INTEGER PRIMARY KEY REFERENCES batches(id), content TEXT NOT NULL
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
            db.execute("PRAGMA journal_mode=WAL")
            # executescript 默认逐条提交；补表必须作为一个事务，失败时不留部分升级。
            try:
                db.executescript("BEGIN IMMEDIATE;\n" + SCHEMA)
                columns = {r['name'] for r in db.execute('PRAGMA table_info(tasks)')}
                if 'scenario_file_sha256' not in columns:
                    db.execute("ALTER TABLE tasks ADD COLUMN scenario_file_sha256 TEXT NOT NULL DEFAULT ''")
                    import json
                    for row in db.execute('SELECT id,input_json FROM tasks').fetchall():
                        content = json.loads(row['input_json']).get('scenarios', '')
                        digest = hashlib.sha256(content.encode()).hexdigest() if content else ''
                        db.execute('UPDATE tasks SET scenario_file_sha256=? WHERE id=?', (digest, row['id']))
                db.execute("UPDATE tasks SET trigger='重新合并' WHERE trigger='开发者请求'")
                db.commit()
            except BaseException:
                db.rollback()
                raise

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.data / "board.sqlite3", timeout=30)
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
