"""项目数据库（格式 6）、跨进程操作锁与按内容寻址的证据存储。"""
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import threading

VERSION = 6

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS checkouts(
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  slot INTEGER NOT NULL UNIQUE,
  kind TEXT NOT NULL CHECK(kind IN ('main','worktree')),
  paths TEXT NOT NULL,
  env TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  removed_at TEXT);

CREATE TABLE IF NOT EXISTS work(
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL CHECK(kind IN ('run','fix','distill','merge','author','draft','atlas')),
  checkout INTEGER NOT NULL REFERENCES checkouts(id),
  status TEXT NOT NULL CHECK(status IN ('prepared','submitted','abandoned')),
  session TEXT,
  operator TEXT NOT NULL,
  implementer TEXT,
  fix INTEGER,
  launch INTEGER,
  dir TEXT NOT NULL,
  scope TEXT NOT NULL DEFAULT '[]',
  snapshot TEXT NOT NULL DEFAULT '{}',
  product_digest TEXT,
  result TEXT,
  reason TEXT,
  created_at TEXT NOT NULL,
  ended_at TEXT);
CREATE UNIQUE INDEX IF NOT EXISTS work_one_prepared ON work(checkout, kind)
  WHERE status='prepared' AND kind IN ('run','distill');

CREATE TABLE IF NOT EXISTS results(
  id INTEGER PRIMARY KEY,
  work INTEGER REFERENCES work(id),
  checkout INTEGER NOT NULL REFERENCES checkouts(id),
  file TEXT NOT NULL,
  scenario TEXT NOT NULL,
  fingerprint TEXT NOT NULL,
  product_digest TEXT,
  product_changed TEXT NOT NULL DEFAULT '[]',
  kind TEXT NOT NULL CHECK(kind IN ('agent','manual')),
  status TEXT NOT NULL CHECK(status IN ('pass','fail','blocked','skip')),
  actual TEXT,
  finding TEXT,
  defect TEXT,
  note TEXT,
  reused_from INTEGER REFERENCES results(id),
  of_result INTEGER REFERENCES results(id),
  executor TEXT,
  implementer TEXT,
  independence TEXT CHECK(independence IN ('independent','self-check','undeclared')),
  marked_by TEXT,
  executed_at TEXT NOT NULL,
  UNIQUE(work, file, scenario));
CREATE INDEX IF NOT EXISTS results_lookup ON results(checkout, file, scenario, fingerprint);

CREATE TABLE IF NOT EXISTS evidence(sha256 TEXT PRIMARY KEY, size INTEGER NOT NULL, name TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS result_evidence(
  result INTEGER NOT NULL REFERENCES results(id),
  ordinal INTEGER NOT NULL,
  name TEXT NOT NULL,
  sha256 TEXT NOT NULL REFERENCES evidence(sha256),
  PRIMARY KEY(result, ordinal));

CREATE TABLE IF NOT EXISTS guard_runs(
  id INTEGER PRIMARY KEY,
  checkout INTEGER NOT NULL REFERENCES checkouts(id),
  file TEXT NOT NULL,
  scenario TEXT NOT NULL,
  fingerprint TEXT NOT NULL,
  product_digest TEXT NOT NULL,
  tests_digest TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('pass','fail')),
  reason TEXT,
  output TEXT NOT NULL DEFAULT '[]',
  operator TEXT NOT NULL,
  ran_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS guard_runs_lookup ON guard_runs(checkout, file, scenario, fingerprint, product_digest);
CREATE TABLE IF NOT EXISTS guard_misses(
  id INTEGER PRIMARY KEY,
  file TEXT NOT NULL,
  scenario TEXT NOT NULL,
  fingerprint TEXT NOT NULL,
  tests_digest TEXT NOT NULL,
  result INTEGER REFERENCES results(id),
  recorded_at TEXT NOT NULL,
  UNIQUE(file, scenario, fingerprint, tests_digest));

CREATE TABLE IF NOT EXISTS fixes(
  id INTEGER PRIMARY KEY,
  checkout INTEGER NOT NULL REFERENCES checkouts(id),
  session TEXT NOT NULL,
  operator TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('open','submitted','verified','closed','abandoned')),
  dir TEXT NOT NULL,
  problems TEXT NOT NULL,
  base_files TEXT NOT NULL,
  answer TEXT,
  changes TEXT,
  verified_work INTEGER REFERENCES work(id),
  verified_files TEXT,
  close TEXT,
  created_at TEXT NOT NULL,
  ended_at TEXT);
CREATE UNIQUE INDEX IF NOT EXISTS fixes_one_open ON fixes(checkout) WHERE status='open';

CREATE TABLE IF NOT EXISTS launches(
  id INTEGER PRIMARY KEY,
  checkout INTEGER NOT NULL REFERENCES checkouts(id),
  kind TEXT NOT NULL,
  agent TEXT NOT NULL,
  scope TEXT NOT NULL DEFAULT '[]',
  session TEXT,
  pid INTEGER,
  log TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('running','exited','stopped','failed')),
  exit_code INTEGER,
  requested_by TEXT NOT NULL,
  started_at TEXT NOT NULL,
  ended_at TEXT);
CREATE UNIQUE INDEX IF NOT EXISTS launches_one_running ON launches(checkout) WHERE status='running';

CREATE TABLE IF NOT EXISTS environments(
  checkout INTEGER PRIMARY KEY REFERENCES checkouts(id),
  state TEXT NOT NULL CHECK(state IN ('starting','ready','failed','stopped')),
  pid INTEGER,
  ports TEXT NOT NULL DEFAULT '[]',
  env TEXT NOT NULL DEFAULT '{}',
  product_digest TEXT,
  log TEXT,
  error TEXT,
  started_at TEXT,
  ended_at TEXT);

CREATE TABLE IF NOT EXISTS browser_instances(
  checkout INTEGER NOT NULL REFERENCES checkouts(id),
  identity TEXT NOT NULL,
  port INTEGER NOT NULL,
  pid INTEGER,
  profile TEXT NOT NULL,
  headed INTEGER NOT NULL DEFAULT 0,
  state TEXT NOT NULL CHECK(state IN ('starting','ready','exited','crashed')),
  holder TEXT,
  started_at TEXT,
  exited_at TEXT,
  exit_code INTEGER,
  signal TEXT,
  log TEXT,
  crash_report TEXT,
  crashes TEXT NOT NULL DEFAULT '[]',
  PRIMARY KEY(checkout, identity));

CREATE TABLE IF NOT EXISTS inbox(
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL CHECK(kind IN ('cases','sop')),
  filename TEXT NOT NULL,
  sha256 TEXT NOT NULL,
  content TEXT NOT NULL,
  uploaded_by TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('pending','merged','rejected')),
  work INTEGER REFERENCES work(id),
  note TEXT,
  created_at TEXT NOT NULL,
  ended_at TEXT);

CREATE TABLE IF NOT EXISTS questions(
  id INTEGER PRIMARY KEY,
  work INTEGER REFERENCES work(id),
  about TEXT NOT NULL,
  text TEXT NOT NULL,
  asked_by TEXT NOT NULL,
  asked_at TEXT NOT NULL,
  answer TEXT,
  answered_by TEXT,
  answered_at TEXT);

CREATE TABLE IF NOT EXISTS annotations(
  id INTEGER PRIMARY KEY,
  file TEXT NOT NULL,
  target TEXT NOT NULL,
  text TEXT NOT NULL,
  author TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('open','adopted','rejected')),
  note TEXT,
  created_at TEXT NOT NULL,
  resolved_at TEXT);

CREATE TABLE IF NOT EXISTS attention(
  id INTEGER PRIMARY KEY,
  checkout INTEGER REFERENCES checkouts(id),
  kind TEXT NOT NULL,
  text TEXT NOT NULL,
  scenarios TEXT NOT NULL DEFAULT '[]',
  work INTEGER REFERENCES work(id),
  created_by TEXT NOT NULL,
  created_at TEXT NOT NULL,
  resolved_by TEXT,
  resolved_at TEXT,
  note TEXT);

CREATE TABLE IF NOT EXISTS identity_events(
  id INTEGER PRIMARY KEY,
  checkout INTEGER NOT NULL REFERENCES checkouts(id),
  entry TEXT NOT NULL,
  identity TEXT NOT NULL,
  kind TEXT NOT NULL CHECK(kind IN ('login','code')),
  work INTEGER REFERENCES work(id),
  recorded_at TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS fixture_runs(
  id INTEGER PRIMARY KEY,
  work INTEGER REFERENCES work(id),
  kind TEXT NOT NULL CHECK(kind IN ('recipe','adhoc')),
  name TEXT,
  scenario TEXT,
  params TEXT NOT NULL DEFAULT '{}',
  command TEXT NOT NULL,
  exit_code INTEGER,
  output TEXT,
  operator TEXT NOT NULL,
  started_at TEXT NOT NULL,
  finished_at TEXT);
"""


def now():
    return datetime.now(timezone.utc).isoformat()


def dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def loads(value, default=None):
    return default if value is None else json.loads(value)


class Lock:
    """看板线程与本机命令共用的可重入跨进程锁。"""

    def __init__(self, path):
        self.path = Path(path)
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
            if self.fd is not None and self.depth == 0:
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
        self.data = Path(data)
        self.path = self.data / 'board.sqlite3'
        self.lock = Lock(self.data / '.lock')
        self.created_files = []
        with self.connection() as db:
            version = db.execute('PRAGMA user_version').fetchone()[0]
            tables = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' LIMIT 1").fetchone()
            if tables and version != VERSION:
                raise ValueError(f'数据库格式 {version} 不是当前格式 {VERSION}；用 sop-board init 新建项目，不读取或迁移旧库')
            db.execute('PRAGMA journal_mode=WAL')
            if not tables:
                with self.lock:
                    db.executescript('BEGIN IMMEDIATE;\n' + SCHEMA + f'\nPRAGMA user_version={VERSION};\nCOMMIT;')

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        try:
            yield db
        finally:
            db.close()

    @contextmanager
    def transaction(self):
        with self.lock, self.connection() as db:
            self.created_files = []
            db.execute('BEGIN IMMEDIATE')
            try:
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                for path in self.created_files:
                    path.unlink(missing_ok=True)
                raise

    def evidence_path(self, sha):
        return self.data / 'evidence' / sha[:2] / sha

    def save_evidence(self, db, raw, name):
        sha = hashlib.sha256(raw).hexdigest()
        path = self.evidence_path(sha)
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            self.created_files.append(path)
            path.write_bytes(raw)
        db.execute('INSERT OR IGNORE INTO evidence VALUES (?,?,?)', (sha, len(raw), name))
        return sha

    def read_evidence(self, sha):
        return self.evidence_path(sha).read_bytes()
