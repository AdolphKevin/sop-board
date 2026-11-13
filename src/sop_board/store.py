"""数据库（格式 7）：本机状态库、业务记录库、跨进程操作锁与按内容寻址的证据存储。

两组表（已定决策 21）：
- 本机状态始终在项目数据目录的 board.sqlite3：检出、运行与修复的本机部分、启动、环境、浏览器、租约、身份计数、配方留痕、服务停启。
- 业务记录在业务记录库：config.yaml 写了 shared_database 时是共享的 PostgreSQL，否则是同一个 board.sqlite3。
  两种库同一套表与 SQL（占位符写 ?，取新行编号用 RETURNING，去重用 ON CONFLICT），只有连接、事务和加锁按库分开写。
  结果、标注、守护运行、修复关闭（以及运行记录）共用 counters 表里的一串递增序号，状态推导只比较序号。
证据文件不在数据库里：按 sha256 存放在数据根目录（项目数据目录的上一级）的 evidence/，不随项目数据目录重建而丢失。
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import threading
import time
import uuid

from .errors import APIError

VERSION = 7
LOCAL_PRODUCT = 'local'

LOCAL_SCHEMA = """
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

CREATE TABLE IF NOT EXISTS local_work(
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
  branch TEXT,
  result TEXT,
  record INTEGER,
  created_at TEXT NOT NULL,
  ended_at TEXT);
CREATE UNIQUE INDEX IF NOT EXISTS local_work_one_prepared ON local_work(checkout, kind)
  WHERE status='prepared' AND kind IN ('run','distill');

CREATE TABLE IF NOT EXISTS local_fixes(
  fix INTEGER PRIMARY KEY,
  product TEXT NOT NULL,
  checkout INTEGER NOT NULL REFERENCES checkouts(id),
  dir TEXT NOT NULL,
  base_files TEXT NOT NULL,
  changes TEXT,
  verified_work INTEGER,
  verified_files TEXT);

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

CREATE TABLE IF NOT EXISTS identity_events(
  id INTEGER PRIMARY KEY,
  checkout INTEGER NOT NULL REFERENCES checkouts(id),
  entry TEXT NOT NULL,
  identity TEXT NOT NULL,
  kind TEXT NOT NULL CHECK(kind IN ('login','code')),
  work INTEGER REFERENCES local_work(id),
  recorded_at TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS fixture_runs(
  id INTEGER PRIMARY KEY,
  work INTEGER REFERENCES local_work(id),
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

-- 运行中经 env stop|start 停启的服务与提交或放弃时的自动恢复（restore）；ok=0 表示自动恢复没有完成；
-- scenarios 在提交时写入：服务停着期间执行的场景键。提交时随运行记录写进业务记录库的 work.services。
CREATE TABLE IF NOT EXISTS service_events(
  id INTEGER PRIMARY KEY,
  work INTEGER NOT NULL REFERENCES local_work(id),
  checkout INTEGER NOT NULL REFERENCES checkouts(id),
  service TEXT NOT NULL,
  action TEXT NOT NULL CHECK(action IN ('stop','start','restore')),
  ok INTEGER NOT NULL DEFAULT 1 CHECK(ok IN (0,1)),
  operator TEXT NOT NULL,
  at TEXT NOT NULL,
  note TEXT,
  scenarios TEXT NOT NULL DEFAULT '[]');
CREATE INDEX IF NOT EXISTS service_events_work ON service_events(work, id);
"""

# 业务记录：{id} 是自增主键的写法（SQLite 为 INTEGER PRIMARY KEY，PostgreSQL 为 BIGINT 标识列），其余两种库完全相同。
RECORDS_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS counters(name TEXT PRIMARY KEY, value BIGINT NOT NULL);

CREATE TABLE IF NOT EXISTS products(
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  created_by TEXT,
  machine TEXT,
  created_at TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS machines(
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  addresses TEXT,
  peer_token TEXT,
  updated_at TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS work(
  id {id},
  product TEXT NOT NULL REFERENCES products(id),
  machine TEXT NOT NULL,
  origin TEXT NOT NULL,
  local_id BIGINT NOT NULL,
  seq BIGINT,
  kind TEXT NOT NULL CHECK(kind IN ('run','fix','distill','merge','author','draft','atlas')),
  checkout TEXT NOT NULL,
  branch TEXT,
  session TEXT,
  operator TEXT NOT NULL,
  implementer TEXT,
  implemented_by TEXT,
  independence TEXT,
  fix BIGINT,
  launch BIGINT,
  scope TEXT NOT NULL DEFAULT '[]',
  snapshot TEXT NOT NULL DEFAULT '{}',
  product_digest TEXT,
  result TEXT,
  services TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL,
  ended_at TEXT,
  UNIQUE(machine, origin, local_id));
CREATE INDEX IF NOT EXISTS work_product ON work(product, kind);

CREATE TABLE IF NOT EXISTS results(
  id {id},
  product TEXT NOT NULL REFERENCES products(id),
  seq BIGINT NOT NULL,
  work BIGINT REFERENCES work(id),
  machine TEXT NOT NULL,
  checkout TEXT NOT NULL,
  branch TEXT,
  file TEXT NOT NULL,
  scenario TEXT NOT NULL,
  fingerprint TEXT NOT NULL,
  product_digest TEXT,
  product_changed TEXT NOT NULL DEFAULT '[]',
  kind TEXT NOT NULL CHECK(kind IN ('agent','manual')),
  status TEXT NOT NULL CHECK(status IN ('pass','fail','blocked','skip','changed')),
  actual TEXT,
  finding TEXT,
  defect TEXT,
  note TEXT,
  reused_from BIGINT REFERENCES results(id),
  of_result BIGINT REFERENCES results(id),
  executor TEXT,
  implementer TEXT,
  implemented_by TEXT,
  independence TEXT CHECK(independence IN ('independent','self-check','undeclared')),
  marked_by TEXT,
  executed_at TEXT NOT NULL,
  UNIQUE(work, file, scenario));
CREATE INDEX IF NOT EXISTS results_lookup ON results(product, file, scenario, fingerprint);

CREATE TABLE IF NOT EXISTS evidence(
  product TEXT NOT NULL REFERENCES products(id),
  sha256 TEXT NOT NULL,
  machine TEXT NOT NULL,
  size BIGINT NOT NULL,
  name TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY(product, sha256, machine));
CREATE TABLE IF NOT EXISTS result_evidence(
  result BIGINT NOT NULL REFERENCES results(id),
  ordinal INTEGER NOT NULL,
  name TEXT NOT NULL,
  sha256 TEXT NOT NULL,
  PRIMARY KEY(result, ordinal));

CREATE TABLE IF NOT EXISTS guard_runs(
  id {id},
  product TEXT NOT NULL REFERENCES products(id),
  seq BIGINT NOT NULL,
  machine TEXT NOT NULL,
  checkout TEXT NOT NULL,
  branch TEXT,
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
CREATE INDEX IF NOT EXISTS guard_runs_lookup ON guard_runs(product, file, scenario, fingerprint);
CREATE TABLE IF NOT EXISTS guard_misses(
  id {id},
  product TEXT NOT NULL REFERENCES products(id),
  file TEXT NOT NULL,
  scenario TEXT NOT NULL,
  fingerprint TEXT NOT NULL,
  tests_digest TEXT NOT NULL,
  result BIGINT REFERENCES results(id),
  machine TEXT,
  checkout TEXT,
  recorded_at TEXT NOT NULL,
  UNIQUE(product, file, scenario, fingerprint, tests_digest));
CREATE TABLE IF NOT EXISTS distillations(
  id {id},
  product TEXT NOT NULL REFERENCES products(id),
  work BIGINT REFERENCES work(id),
  file TEXT NOT NULL,
  scenario TEXT NOT NULL,
  fingerprint TEXT NOT NULL,
  tests_digest TEXT NOT NULL,
  distilled_by TEXT NOT NULL,
  machine TEXT NOT NULL,
  checkout TEXT NOT NULL,
  branch TEXT,
  registered_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS distillations_lookup ON distillations(product, file, scenario, fingerprint);

CREATE TABLE IF NOT EXISTS fixes(
  id {id},
  product TEXT NOT NULL REFERENCES products(id),
  machine TEXT NOT NULL,
  checkout TEXT NOT NULL,
  branch TEXT,
  session TEXT NOT NULL,
  operator TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('open','submitted','verified','closed','abandoned')),
  problems TEXT NOT NULL,
  answer TEXT,
  seq BIGINT,
  close TEXT,
  created_at TEXT NOT NULL,
  ended_at TEXT);
CREATE UNIQUE INDEX IF NOT EXISTS fixes_one_open ON fixes(product, machine, checkout) WHERE status='open';

CREATE TABLE IF NOT EXISTS inbox(
  id {id},
  product TEXT NOT NULL REFERENCES products(id),
  kind TEXT NOT NULL CHECK(kind IN ('cases','sop')),
  filename TEXT NOT NULL,
  sha256 TEXT NOT NULL,
  content TEXT NOT NULL,
  uploaded_by TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('pending','merged','rejected')),
  work BIGINT REFERENCES work(id),
  note TEXT,
  created_at TEXT NOT NULL,
  ended_at TEXT);

CREATE TABLE IF NOT EXISTS questions(
  id {id},
  product TEXT NOT NULL REFERENCES products(id),
  machine TEXT,
  work BIGINT,
  about TEXT NOT NULL,
  text TEXT NOT NULL,
  asked_by TEXT NOT NULL,
  asked_at TEXT NOT NULL,
  answer TEXT,
  answered_by TEXT,
  answered_at TEXT);

CREATE TABLE IF NOT EXISTS annotations(
  id {id},
  product TEXT NOT NULL REFERENCES products(id),
  file TEXT NOT NULL,
  target TEXT NOT NULL,
  text TEXT NOT NULL,
  author TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('open','adopted','rejected')),
  note TEXT,
  created_at TEXT NOT NULL,
  resolved_at TEXT);

-- 草稿场景的同意按 产品 + 文件 + 场景 + 指纹 记录，不区分检出与机器；撤销只记时间，记录留在历史中。
CREATE TABLE IF NOT EXISTS draft_approvals(
  id {id},
  product TEXT NOT NULL REFERENCES products(id),
  file TEXT NOT NULL,
  scenario TEXT NOT NULL,
  fingerprint TEXT NOT NULL,
  approved_by TEXT NOT NULL,
  approved_at TEXT NOT NULL,
  revoked_at TEXT);
CREATE UNIQUE INDEX IF NOT EXISTS draft_approvals_active ON draft_approvals(product, file, scenario, fingerprint, approved_by)
  WHERE revoked_at IS NULL;

CREATE TABLE IF NOT EXISTS attention(
  id {id},
  product TEXT NOT NULL REFERENCES products(id),
  machine TEXT,
  checkout TEXT,
  kind TEXT NOT NULL,
  text TEXT NOT NULL,
  scenarios TEXT NOT NULL DEFAULT '[]',
  work BIGINT,
  created_by TEXT NOT NULL,
  created_at TEXT NOT NULL,
  resolved_by TEXT,
  resolved_at TEXT,
  note TEXT)
"""

# 业务记录库中按产品区分的表（导入、核对与行数统计用）。
RECORD_TABLES = ('products', 'machines', 'work', 'results', 'evidence', 'result_evidence', 'guard_runs', 'guard_misses',
                 'distillations', 'fixes', 'inbox', 'questions', 'annotations', 'draft_approvals', 'attention', 'counters', 'meta')
ID_COLUMN = {'sqlite': 'INTEGER PRIMARY KEY', 'postgres': 'BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY'}


def records_ddl(dialect):
    return [part.strip() for part in RECORDS_SCHEMA.replace('{id}', ID_COLUMN[dialect]).split(';')
            if part.strip() and not all(line.strip().startswith('--') for line in part.strip().splitlines())]


def now():
    return datetime.now(timezone.utc).isoformat()


def dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def loads(value, default=None):
    return default if value is None else json.loads(value)


# ---- 错误 ----

class RecordsError(APIError):
    """业务记录库的错误；lan_problems 是内网页面看到的说法（不含主机、端口与库名）。"""

    def __init__(self, status, code, problem, lan_problem=None):
        super().__init__(status, code, [dict(field='shared_database', problem=problem)])
        self.lan_problems = [dict(field='shared_database', problem=lan_problem or problem)]


def where(shared):
    return f"主机 {shared['host']}，端口 {shared['port']}，库 {shared['dbname']}，用户 {shared['user']}"


def unreachable(shared, error=None):
    from .redaction import redact
    reason = str(error).splitlines()[0] if error and str(error).splitlines() else ''
    detail = f'：{reason}' if reason else ''
    problem = redact((f'连不上共享库（{where(shared)}）' + detail
                      + '。业务记录只存在共享库里，没有离线队列与本地缓存；恢复后重新运行这条命令').encode(), ()).decode()
    return RecordsError(503, 'database-unreachable',
                        problem,
                        '连不上共享库，业务记录暂时无法读写；恢复后刷新页面')


def version_error(found):
    return RecordsError(409, 'database-version',
                        f'共享库的表结构版本是 {found}，本机 sop-board 支持的版本是 {VERSION}。'
                        '两种版本不能同时使用：换用与共享库匹配的 sop-board 版本（共享库版本更新时升级本机，更旧时由维护共享库的人确认后统一更换）',
                        f'共享库的表结构版本（{found}）与这台看板的 sop-board（{VERSION}）不一致')


class ProductUnbound(RecordsError):
    def __init__(self, project):
        super().__init__(409, 'product-unbound',
                         f'项目 {project.name} 的 config.yaml 写了 shared_database，但还没有绑定产品：'
                         f'使用设置接口加入或新建产品，或运行 sop-board init {project.name} 列出产品，问使用者后用 --join <ID> 加入或 --new 新建；要回到本机库可在设置接口断开',
                         '这个项目还没有绑定共享库里的产品')


# ---- 行与连接 ----

class Row(dict):
    """两种库统一的行：按列名或位置取值，dict(row) 得到普通字典。"""

    def __getitem__(self, key):
        if isinstance(key, int):
            return list(self.values())[key]
        return dict.__getitem__(self, key)


def _sqlite_row(cursor, values):
    return Row(zip([column[0] for column in cursor.description], values))


def _pg_rows(cursor):
    names = [column.name for column in cursor.description] if cursor.description else []
    return lambda values: Row(zip(names, values))


class DB:
    """一份 SQL 两种库：SQL 一律用 ? 占位，PostgreSQL 时转成 %s。"""

    def __init__(self, connection, dialect, shared=None):
        self.connection, self.dialect, self.shared = connection, dialect, shared

    def execute(self, sql, params=()):
        if self.dialect == 'sqlite':
            return self.connection.execute(sql, params)
        import psycopg
        if params:
            sql = sql.replace('%', '%%').replace('?', '%s')
        try:
            return self.connection.execute(sql, tuple(params) if params else None)
        except psycopg.OperationalError as error:
            raise unreachable(self.shared, error) from error

    def one(self, sql, params=()):
        return self.execute(sql, params).fetchone()

    def all(self, sql, params=()):
        return self.execute(sql, params).fetchall()

    def value(self, sql, params=()):
        row = self.one(sql, params)
        return None if row is None else row[0]

    def insert(self, sql, params=()):
        """INSERT … RETURNING id；冲突忽略时返回 None。"""
        row = self.one(sql + ' RETURNING id', params)
        return None if row is None else row['id']

    def next_seq(self):
        """业务记录库的统一递增序号：结果、标注、守护运行、修复关闭与运行记录共用。行锁使序号顺序与提交顺序一致。"""
        return self.value("UPDATE counters SET value=value+1 WHERE name='seq' RETURNING value")


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


def sqlite_connect(path):
    db = sqlite3.connect(path, timeout=30)
    db.row_factory = _sqlite_row
    db.execute('PRAGMA foreign_keys=ON')
    return db


def pg_connect(shared, *, timeout=5):
    """连接共享库：优先使用配置的 password，否则由 libpq 从 ~/.pgpass 或 PGPASSFILE 读取。"""
    import psycopg
    try:
        return psycopg.connect(host=shared['host'], port=shared['port'], dbname=shared['dbname'], user=shared['user'],
                               **({'password': shared['password']} if 'password' in shared else {}),
                               connect_timeout=timeout, autocommit=True, row_factory=_pg_rows, application_name='sop-board')
    except psycopg.Error as error:
        if getattr(error, 'sqlstate', None) in ('28P01', '28000') or any(text in str(error) for text in ('password authentication failed', 'no password supplied')):
            raise RecordsError(503, 'database-authentication', f'共享库认证失败（{where(shared)}）', '共享库认证失败') from None
        raise unreachable(shared, error) from None


def pg_version(connection):
    """共享库记录的表结构版本；空库（没有 meta 表）返回 None。"""
    # to_regclass 不存在时返回 NULL，不会让所在事务失败。
    if connection.execute("SELECT to_regclass('meta')").fetchone()[0] is None:
        return None
    row = connection.execute("SELECT value FROM meta WHERE key='format'").fetchone()
    return int(row[0]) if row and str(row[0]).isdigit() else (row[0] if row else '（没有版本记录）')


def create_records(db, *, created_by):
    """在空的业务记录库中按当前版本建表；只由 init --new（共享库）与新建本机数据库（SQLite）调用。"""
    for statement in records_ddl(db.dialect):
        db.execute(statement)
    db.execute("INSERT INTO meta(key, value) VALUES ('format', ?) ON CONFLICT DO NOTHING", (str(VERSION),))
    db.execute("INSERT INTO meta(key, value) VALUES ('created_by', ?) ON CONFLICT DO NOTHING", (created_by or '',))
    db.execute("INSERT INTO counters(name, value) VALUES ('seq', 0) ON CONFLICT DO NOTHING")


class Pool:
    """共享库连接的复用：看板每 15 秒轮询一次，CLI 一条命令里多次读写，都不必每次重连。"""

    def __init__(self, shared, size=4):
        self.shared, self.size = shared, size
        self.idle, self.guard = [], threading.Lock()

    def take(self):
        """返回 (连接, 是否须核对)：新连接或闲置超过 5 秒的连接要先核对（同时起到探活作用，共享库重启后旧连接在这里被换掉）。"""
        while True:
            with self.guard:
                item = self.idle.pop() if self.idle else None
            if item is None:
                return pg_connect(self.shared), True
            connection, since = item
            if connection.closed or connection.broken:
                continue
            return connection, time.monotonic() - since > 5

    def discard(self, connection):
        try:
            connection.close()
        except Exception:  # noqa: BLE001
            pass

    def give(self, connection):
        if connection.closed or connection.broken:
            return
        from psycopg import pq
        if connection.info.transaction_status != pq.TransactionStatus.IDLE:
            try:
                connection.rollback()
            except Exception:  # noqa: BLE001
                connection.close()
                return
        with self.guard:
            if len(self.idle) < self.size:
                self.idle.append((connection, time.monotonic()))
                return
        connection.close()

    def close(self):
        with self.guard:
            items, self.idle = self.idle, []
        for connection, _ in items:
            connection.close()


class Store:
    """项目的数据库访问。local()/local_tx() 是本机状态库；records()/records_tx() 是业务记录库。"""

    def __init__(self, project):
        self.project = project
        self.data = Path(project.data)
        self.path = self.data / 'board.sqlite3'
        self.lock = Lock(self.data / '.lock')
        self.created_files = []
        self.pool, self.pool_key = None, None
        self.machine_ready = set()
        with self.local() as db:
            version = db.execute('PRAGMA user_version').fetchone()[0]
            tables = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' LIMIT 1").fetchone()
            if tables and version != VERSION:
                from .config import ProjectFormat
                raise ProjectFormat(f'数据库格式 {version} 不是当前格式 {VERSION}；用 sop-board init 新建项目，不读取或迁移旧库')
            db.execute('PRAGMA journal_mode=WAL')
            if not tables:
                with self.lock:
                    db.execute('BEGIN IMMEDIATE')
                    for statement in [s for s in LOCAL_SCHEMA.split(';') if s.strip()]:
                        db.execute(statement)
                    records = DB(db, 'sqlite')
                    create_records(records, created_by=None)
                    db.execute("INSERT INTO meta(key, value) VALUES ('instance', ?) ON CONFLICT DO NOTHING", (uuid.uuid4().hex,))
                    db.execute('INSERT INTO products(id, name, created_by, created_at) VALUES (?,?,?,?) ON CONFLICT DO NOTHING',
                               (LOCAL_PRODUCT, project.name, None, now()))
                    db.execute(f'PRAGMA user_version={VERSION}')
                    db.commit()
            self.origin = db.execute("SELECT value FROM meta WHERE key='instance'").fetchone()[0]

    # ---- 本机状态库 ----

    @contextmanager
    def local(self):
        db = sqlite_connect(self.path)
        try:
            yield db
        finally:
            db.close()

    @contextmanager
    def local_tx(self):
        with self.lock, self.local() as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise

    # ---- 业务记录库 ----

    @property
    def shared(self):
        return getattr(self.project, 'shared', None)

    @property
    def product(self):
        """业务记录所属的产品：没配共享库时项目自己就是产品（local）；配了共享库时取 project.json 的绑定。"""
        if getattr(self.project, 'shared_invalid', False):
            raise RecordsError(422, 'shared-database-invalid', 'config.yaml 的 shared_database 不合格：' + '；'.join(
                f"{p['field']} {p['problem']}" for p in self.project.problems if p['field'].startswith('shared_database')),
                '这个项目的共享库配置不合格')
        if not self.shared:
            return LOCAL_PRODUCT
        binding = self.project.binding()
        if not binding:
            raise ProductUnbound(self.project)
        return binding['id']

    @property
    def machine(self):
        from .config import machine
        return machine(self.data.parent)

    def product_name(self):
        binding = self.project.binding() if self.shared else None
        return binding['name'] if binding else self.project.name

    def _pool(self):
        key = json.dumps(self.shared, sort_keys=True)
        if self.pool_key != key:
            if self.pool:
                self.pool.close()
            self.pool, self.pool_key = Pool(self.shared), key
        return self.pool

    @contextmanager
    def _shared_connection(self):
        """取一个共享库连接：新连接与闲置过的连接先核对表结构版本与产品（版本不符返回 database-version）。"""
        import psycopg
        product = self.product
        pool = self._pool()
        for attempt in (1, 2):
            connection, check = pool.take()
            if not check:
                break
            try:
                found = pg_version(connection)
                row = connection.execute('SELECT 1 FROM products WHERE id=%s', (product,)).fetchone() if found == VERSION else None
            except psycopg.OperationalError as error:
                pool.discard(connection)
                if attempt == 2:
                    raise unreachable(self.shared, error) from error
                continue
            if found != VERSION and found is not None:
                pool.give(connection)
                raise version_error(found if found is not None else '（空库，没有表结构）')
            if row is None:
                pool.give(connection)
                raise RecordsError(404, 'product-not-found',
                                   f'绑定的产品 {product} 在共享库（{where(self.shared)}）中不存在；'
                                   f'核对 config.yaml 是否指向正确的共享库；使用设置接口选择产品，或运行 sop-board init {self.project.name} 列出产品后用 --join <ID>/--new 重新绑定',
                                   '这个项目绑定的产品在共享库中不存在')
            break
        try:
            yield DB(connection, 'postgres', self.shared)
        except BaseException:
            pool.give(connection)
            raise
        else:
            pool.give(connection)

    @contextmanager
    def records(self):
        if not self.shared:
            db = sqlite_connect(self.path)
            try:
                yield DB(db, 'sqlite')
            finally:
                db.close()
            return
        with self._shared_connection() as db:
            yield db

    @contextmanager
    def records_tx(self):
        """业务记录的一个事务。SQLite 靠本机文件锁串行；PostgreSQL 靠事务、唯一约束与序号行锁，多台机器可以同时写。"""
        if not self.shared:
            with self.lock:
                db = sqlite_connect(self.path)
                try:
                    db.execute('BEGIN IMMEDIATE')
                    wrapped = DB(db, 'sqlite')
                    self._ensure_machine(wrapped)
                    yield wrapped
                    db.commit()
                except BaseException:
                    db.rollback()
                    raise
                finally:
                    db.close()
            return
        import psycopg
        with self._shared_connection() as db:
            try:
                with db.connection.transaction():
                    self._ensure_machine(db)
                    yield db
            except psycopg.OperationalError as error:
                raise unreachable(self.shared, error) from error

    def _ensure_machine(self, db):
        machine = self.machine
        key = (self.pool_key, machine['id'])
        if key in self.machine_ready:
            return
        db.execute('INSERT INTO machines(id, name, addresses, peer_token, updated_at) VALUES (?,?,NULL,NULL,?) ON CONFLICT DO NOTHING',
                   (machine['id'], machine['name'], now()))
        self.machine_ready.add(key)

    def close(self):
        if self.pool:
            self.pool.close()

    # ---- 证据 ----

    def evidence_root(self):
        return self.data.parent / 'evidence'

    def evidence_path(self, sha):
        return self.evidence_root() / sha[:2] / sha

    def put_evidence(self, raw, name=''):
        """把证据文件按内容存进本机证据目录（幂等，原子写入）；返回 sha256。"""
        from .redaction import response_bytes
        from .evidence_types import content_type
        kind = content_type(name) if name else 'application/octet-stream'
        if raw.startswith(b'PK\x03\x04'):
            kind = 'application/zip'
        raw = response_bytes(raw, kind, (), local=True)
        sha = hashlib.sha256(raw).hexdigest()
        path = self.evidence_path(sha)
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_name(f'.{sha}.{os.getpid()}.{threading.get_ident()}')
            temporary.write_bytes(raw)
            os.replace(temporary, path)
        return sha

    def index_evidence(self, db, sha, size, name):
        """证据索引：sha256、大小、名称、产出机器（本机）。"""
        db.execute('INSERT INTO evidence(product, sha256, machine, size, name, created_at) VALUES (?,?,?,?,?,?) ON CONFLICT DO NOTHING',
                   (self.product, sha, self.machine['id'], size, name, now()))

    def save_evidence(self, db, raw, name):
        sha = self.put_evidence(raw, name)
        self.index_evidence(db, sha, self.evidence_path(sha).stat().st_size, name)
        return sha

    def has_evidence(self, sha):
        return self.evidence_path(sha).is_file()

    def read_evidence(self, sha):
        return self.evidence_path(sha).read_bytes()
