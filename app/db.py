"""SQLite layer: one connection per thread, explicit IMMEDIATE transactions.

Writes rely on SQL-level constraints (partial unique index on the active plate)
plus BEGIN IMMEDIATE so that concurrent reports merge and concurrent case
closure cannot both succeed, even without an application lock.
"""
import sqlite3
import threading
import time

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS buildings(
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  px REAL NOT NULL DEFAULT 0,
  py REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS zones(
  id INTEGER PRIMARY KEY,
  building_id INTEGER NOT NULL REFERENCES buildings(id),
  name TEXT NOT NULL,
  kind TEXT NOT NULL,                 -- exit(消防出入口) | lane(消防通道)
  px REAL NOT NULL, py REAL NOT NULL, -- 地图锚点(viewBox 1000x640)
  rx REAL NOT NULL DEFAULT 26,        -- 出入口半径
  blocked_label TEXT
);

CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY,
  username TEXT UNIQUE NOT NULL,
  pw_salt TEXT NOT NULL,
  pw_hash TEXT NOT NULL,
  display_name TEXT NOT NULL,
  role TEXT NOT NULL CHECK(role IN ('resident','guard','supervisor')),
  building_id INTEGER REFERENCES buildings(id),
  phone TEXT
);

-- 住户名下车辆(用于"与自己车辆有关的通知")
CREATE TABLE IF NOT EXISTS resident_vehicles(
  resident_id INTEGER NOT NULL REFERENCES users(id),
  plate TEXT NOT NULL,
  PRIMARY KEY(resident_id, plate)
);

-- status: open(待接单) patrol(巡查中) tow_requested(申请拖移)
--         escalated(已升级/拖移批准) departed(已驶离,待复核) closed(已结案)
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY,
  plate TEXT NOT NULL,
  original_plate TEXT NOT NULL,
  zone_id INTEGER NOT NULL REFERENCES zones(id),
  status TEXT NOT NULL,
  is_emergency INTEGER NOT NULL DEFAULT 0,
  duplicate_count INTEGER NOT NULL DEFAULT 0,
  found_at INTEGER NOT NULL,
  reported_by INTEGER NOT NULL REFERENCES users(id),
  accepted_by INTEGER REFERENCES users(id),
  accepted_at INTEGER,
  tow_requested_by INTEGER REFERENCES users(id),
  tow_requested_at INTEGER,
  tow_reason TEXT,
  escalated_by INTEGER REFERENCES users(id),
  escalated_at INTEGER,
  escalation_note TEXT,
  tow_cancelled_by INTEGER REFERENCES users(id),
  tow_cancelled_at INTEGER,
  tow_cancel_reason TEXT,
  departed_by INTEGER REFERENCES users(id),
  departed_at INTEGER,
  departure_reason TEXT,             -- self_drive | towed | emergency_release
  departure_note TEXT,
  reviewed_by INTEGER REFERENCES users(id),
  reviewed_at INTEGER,
  review_note TEXT,
  closed_at INTEGER,
  lpr_raw TEXT,
  lpr_confidence REAL,
  created_at INTEGER NOT NULL
);

-- 同一车辆在占用期间(未驶离)只能有一张单:重复上报走合并,而不是插新单
CREATE UNIQUE INDEX IF NOT EXISTS ux_event_active_plate
  ON events(plate)
  WHERE status IN ('open','patrol','tow_requested','escalated');
CREATE INDEX IF NOT EXISTS ix_event_zone ON events(zone_id);
CREATE INDEX IF NOT EXISTS ix_event_status ON events(status);

CREATE TABLE IF NOT EXISTS timeline(
  id INTEGER PRIMARY KEY,
  event_id INTEGER NOT NULL REFERENCES events(id),
  kind TEXT NOT NULL,
  actor_id INTEGER REFERENCES users(id),
  detail TEXT,
  created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_timeline_event ON timeline(event_id);

CREATE TABLE IF NOT EXISTS contact_logs(
  id INTEGER PRIMARY KEY,
  event_id INTEGER NOT NULL REFERENCES events(id),
  channel TEXT NOT NULL,              -- sms | phone | onsite | notice
  result TEXT NOT NULL,              -- reached | promised | no_answer | wrong_number | refused | failed
  detail TEXT,
  actor_id INTEGER REFERENCES users(id),
  created_at INTEGER
);

CREATE TABLE IF NOT EXISTS photos(
  id INTEGER PRIMARY KEY,
  event_id INTEGER NOT NULL REFERENCES events(id),
  kind TEXT NOT NULL,                 -- report | departure | review
  filename TEXT NOT NULL,
  content_type TEXT NOT NULL,
  uploaded_by INTEGER REFERENCES users(id),
  created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS notifications(
  id INTEGER PRIMARY KEY,
  resident_id INTEGER NOT NULL REFERENCES users(id),
  event_id INTEGER NOT NULL REFERENCES events(id),
  text TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  read_at INTEGER
);
CREATE INDEX IF NOT EXISTS ix_notif_resident ON notifications(resident_id);

CREATE TABLE IF NOT EXISTS sms_outbox(
  id INTEGER PRIMARY KEY,
  phone TEXT NOT NULL,
  plate TEXT,
  event_id INTEGER,
  text TEXT NOT NULL,
  status TEXT NOT NULL,              -- sent | failed
  fail_reason TEXT,
  created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions(
  token TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id),
  created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS sim_kv(k TEXT PRIMARY KEY, v TEXT NOT NULL);
"""

BLOCKING_STATUSES = ("open", "patrol", "tow_requested", "escalated")


def now_ms():
    return int(time.time() * 1000)


class Database:
    def __init__(self, path):
        self.path = path
        self._tls = threading.local()
        self._init()

    def _conn(self):
        conn = getattr(self._tls, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=10000")
            self._tls.conn = conn
        return conn

    def _init(self):
        conn = self._conn()
        conn.executescript(SCHEMA)

    def query(self, sql, args=()):
        return self._conn().execute(sql, args).fetchall()

    def queryone(self, sql, args=()):
        return self._conn().execute(sql, args).fetchone()

    def begin(self):
        self._conn().execute("BEGIN IMMEDIATE")

    def commit(self):
        if self._conn().in_transaction:
            self._conn().execute("COMMIT")

    def rollback(self):
        if self._conn().in_transaction:
            self._conn().execute("ROLLBACK")

    def execute(self, sql, args=()):
        return self._conn().execute(sql, args)

    def get_kv(self, k, default=None):
        row = self.queryone("SELECT v FROM sim_kv WHERE k=?", (k,))
        return row["v"] if row else default

    def set_kv(self, k, v):
        self.execute(
            "INSERT INTO sim_kv(k,v) VALUES(?,?) "
            "ON CONFLICT(k) DO UPDATE SET v=excluded.v",
            (k, str(v)),
        )
