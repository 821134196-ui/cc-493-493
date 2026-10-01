"""SQLite 数据层：建表、演示数据种子、查询辅助。

时间一律使用本地时间字符串（%Y-%m-%d %H:%M:%S），便于演示与比对。
"""
from __future__ import annotations

import hashlib
import os
import re
import sqlite3
from datetime import datetime, timedelta

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("FIRELANE_DATA", os.path.join(BASE_DIR, "data"))
DB_PATH = os.path.join(DATA_DIR, "firelane.db")
UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")

# 仍在占用/处置中的事件状态（结案状态只有 closed）
ACTIVE_STATUSES = ("open", "accepted", "contacted", "tow_requested",
                   "tow_approved", "pending_review", "emergency")

SCHEMA = """
CREATE TABLE IF NOT EXISTS buildings (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT UNIQUE NOT NULL,
    password_sha TEXT NOT NULL,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('resident','guard','supervisor')),
    building_id INTEGER REFERENCES buildings(id),   -- 住户/保安的所属楼栋；负责人为 NULL=全小区
    phone TEXT
);

CREATE TABLE IF NOT EXISTS residents_vehicles (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    plate_norm TEXT NOT NULL,           -- 归一化车牌，用于匹配
    plate_display TEXT NOT NULL,        -- 展示车牌
    UNIQUE(user_id, plate_norm)
);

CREATE TABLE IF NOT EXISTS channels (
    id INTEGER PRIMARY KEY,
    code TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    building_id INTEGER NOT NULL REFERENCES buildings(id),
    anchor_name TEXT NOT NULL,          -- 视觉锚点：消防出入口名称
    -- SVG viewBox=0 0 1000 640 中的路面多边形与文字位置
    polygon TEXT NOT NULL,              -- "x1,y1;x2,y2;..."
    label_x INTEGER NOT NULL,
    label_y INTEGER NOT NULL,
    exit_x INTEGER NOT NULL,            -- 消防出入口锚点坐标
    exit_y INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    channel_id INTEGER NOT NULL REFERENCES channels(id),
    building_id INTEGER NOT NULL,
    plate_display TEXT NOT NULL,
    plate_norm TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'violation' CHECK(kind IN ('violation','emergency')),
    status TEXT NOT NULL,
    found_at TEXT NOT NULL,
    found_by INTEGER NOT NULL REFERENCES users(id),
    assigned_guard_id INTEGER REFERENCES users(id),   -- 接单巡查保安
    handled_by INTEGER REFERENCES users(id),          -- 实际处置（记驶离/拖移）人
    leave_at TEXT,
    resolution TEXT,                                 -- self_left / towed / emergency
    tow_status TEXT NOT NULL DEFAULT 'none',         -- none/requested/approved/rejected/cancelled
    tow_requested_by INTEGER,
    tow_requested_at TEXT,
    tow_approved_by INTEGER,
    tow_approved_at TEXT,
    tow_result TEXT,
    reviewed_by INTEGER REFERENCES users(id),        -- 清障复核人（须与处置人不同）
    reviewed_at TEXT,
    review_note TEXT,
    closed_at TEXT,
    note TEXT,
    merged_report_count INTEGER NOT NULL DEFAULT 0
);

-- 关键约束：同一车辆在同一占用事件（未结案）中只能有一张单
CREATE UNIQUE INDEX IF NOT EXISTS ux_events_active_plate_channel
    ON events(plate_norm, channel_id)
    WHERE status <> 'closed';

CREATE INDEX IF NOT EXISTS ix_events_plate ON events(plate_norm);
CREATE INDEX IF NOT EXISTS ix_events_building ON events(building_id);

CREATE TABLE IF NOT EXISTS timeline (
    id INTEGER PRIMARY KEY,
    event_id INTEGER NOT NULL REFERENCES events(id),
    ts TEXT NOT NULL,
    actor_user_id INTEGER REFERENCES users(id),
    actor_name TEXT NOT NULL,
    type TEXT NOT NULL,
    detail TEXT                       -- JSON 文本，存结构化留痕（旧车牌、取消原因等）
);
CREATE INDEX IF NOT EXISTS ix_timeline_event ON timeline(event_id);

CREATE TABLE IF NOT EXISTS contact_logs (
    id INTEGER PRIMARY KEY,
    event_id INTEGER NOT NULL REFERENCES events(id),
    ts TEXT NOT NULL,
    user_id INTEGER NOT NULL REFERENCES users(id),
    user_name TEXT NOT NULL,
    method TEXT NOT NULL CHECK(method IN ('sms','phone','onsite')),
    result TEXT NOT NULL CHECK(result IN ('delivered','failed','no_answer','promised_left','refused')),
    phone TEXT,
    content TEXT
);
CREATE INDEX IF NOT EXISTS ix_contact_event ON contact_logs(event_id);

CREATE TABLE IF NOT EXISTS photos (
    id INTEGER PRIMARY KEY,
    event_id INTEGER REFERENCES events(id),   -- 上报瞬间可能先为 NULL，随后绑定
    ts TEXT NOT NULL,
    user_id INTEGER NOT NULL REFERENCES users(id),
    user_name TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'scene' CHECK(kind IN ('scene','clearance')),
    filename TEXT NOT NULL,
    caption TEXT
);

CREATE TABLE IF NOT EXISTS sms_outbox (
    id INTEGER PRIMARY KEY,
    event_id INTEGER REFERENCES events(id),
    phone TEXT NOT NULL,
    content TEXT NOT NULL,
    status TEXT NOT NULL,               -- queued/delivered/failed
    ts TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def normalize_plate(raw: str) -> str:
    """车牌归一化：大写并去掉所有非字母数字/省份汉字字符（京A·12345 -> 京A12345）。"""
    if raw is None:
        return ""
    upper = raw.upper()
    return re.sub(r"[^0-9A-Z一-鿿]", "", upper)


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def get_conn() -> sqlite3.Connection:
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def init_db(force_seed: bool = False) -> None:
    conn = get_conn()
    try:
        conn.executescript(SCHEMA)
        count = conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"]
        if count == 0 or force_seed:
            _seed(conn)
    finally:
        conn.close()


def _seed(conn: sqlite3.Connection) -> None:
    """演示种子数据：3 栋楼、5 条消防通道、住户/保安/负责人账号。"""
    conn.execute("DELETE FROM sessions")
    buildings = [(1, "梧桐苑1号楼"), (2, "梧桐苑2号楼"), (3, "梧桐苑3号楼")]
    conn.executemany("INSERT OR REPLACE INTO buildings(id,name) VALUES(?,?)", buildings)

    # id, username, 密码, 显示名, 角色, 楼栋, 手机
    users = [
        (1, "zhang",  "123456", "张磊（1号楼住户）", "resident", 1, "13800001111"),
        (2, "li",     "123456", "李敏（2号楼住户）", "resident", 2, "13800002222"),
        (3, "guard1", "123456", "王强（1号楼保安）", "guard", 1, "13900000001"),
        (4, "guard2", "123456", "陈静（1号楼保安）", "guard", 1, "13900000002"),
        (5, "guard3", "123456", "赵虎（2号楼保安）", "guard", 2, "13900000003"),
        (6, "sup",    "123456", "周值班（值班负责人）", "supervisor", None, "13700000000"),
    ]
    conn.executemany(
        "INSERT OR REPLACE INTO users(id,username,password_sha,display_name,role,building_id,phone)"
        " VALUES(?,?,?,?,?,?,?)",
        [(i, u, sha256(p), n, r, b, ph) for i, u, p, n, r, b, ph in users],
    )

    # 住户登记车辆
    conn.executemany(
        "INSERT OR IGNORE INTO residents_vehicles(user_id,plate_norm,plate_display)"
        " VALUES(?,?,?)",
        [(1, "京A12345", "京A·12345"),
         (1, "京A66666", "京A·66666"),
         (2, "京B88888", "京B·88888")],
    )

    # 通道：polygon 为 SVG 路面多边形；exit 为消防出入口（绿色锚点）
    channels = [
        (1, "C1-S", "1号楼南门消防通道", 1, "南门消防出入口",
         "70,430;560,430;560,478;70,478", 250, 460, 95, 452),
        (2, "C1-E", "1号楼东侧消防通道", 1, "东侧消防出入口",
         "560,120;608,120;608,430;560,430", 556, 270, 584, 140),
        (3, "C2-N", "2号楼北侧消防通道", 2, "2号楼北消防出入口",
         "660,205;950,205;950,253;660,253", 790, 235, 920, 229),
        (4, "C2-W", "2号楼西侧消防通道", 2, "西侧消防出入口",
         "660,300;708,300;708,560;660,560", 656, 430, 684, 540),
        (5, "C3-M", "3号楼主消防通道", 3, "3号楼消防出入口",
         "180,120;420,120;420,168;180,168", 300, 150, 200, 144),
    ]
    conn.executemany(
        "INSERT OR REPLACE INTO channels(id,code,name,building_id,anchor_name,polygon,label_x,label_y,exit_x,exit_y)"
        " VALUES(?,?,?,?,?,?,?,?,?,?)", channels)

    conn.execute(
        "INSERT OR IGNORE INTO settings(key,value) VALUES('sms_mode','normal')")
    conn.execute(
        "INSERT OR IGNORE INTO settings(key,value) VALUES('lpr_next','')")
    conn.execute(
        "INSERT OR IGNORE INTO settings(key,value) VALUES('sms_fail_until','')")


# ---------- 小工具 ----------

def duration_text(start: str, end: str | None = None) -> str:
    """占用时长，如 “1小时23分”“45分”。"""
    fmt = "%Y-%m-%d %H:%M:%S"
    t0 = datetime.strptime(start, fmt)
    t1 = datetime.strptime(end, fmt) if end else datetime.now()
    secs = max(0, int((t1 - t0).total_seconds()))
    if secs < 60:
        return f"{secs}秒"
    mins = secs // 60
    if mins < 60:
        return f"{mins}分"
    hours, mins = divmod(mins, 60)
    if hours < 48:
        return f"{hours}小时{mins}分"
    days, hours = divmod(hours, 24)
    return f"{days}天{hours}小时"


def occupancy_seconds(event: sqlite3.Row) -> int:
    fmt = "%Y-%m-%d %H:%M:%S"
    end = event["closed_at"] or event["leave_at"] or now()
    try:
        return max(0, int((datetime.strptime(end, fmt)
                           - datetime.strptime(event["found_at"], fmt)).total_seconds()))
    except (TypeError, ValueError):
        return 0
