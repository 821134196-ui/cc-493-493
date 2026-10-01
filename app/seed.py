"""演示数据初始化:3 个楼栋、消防锚点、各角色账号,以及覆盖
各留痕分支(重复合并、识别更正、联系失败、二次驶入、紧急救援、
拖移申请/批准/取消、待复核、已结案)的事件。

仅在数据库文件不存在或显式 --reseed 时运行。
"""
import os
import struct
import zlib

from .db import Database, now_ms
from .services import Service, hash_pw

BUILDINGS = [
    (1, "1栋", 180, 300),
    (2, "2栋", 500, 300),
    (3, "3栋", 820, 300),
]

# (id, building, name, kind, x, y, r)
ZONES = [
    (1, 1, "1栋北消防出入口", "exit", 180, 140, 30),
    (2, 1, "1栋南消防出入口", "exit", 180, 470, 30),
    (3, 1, "1栋东侧消防车道", "lane", 320, 300, 22),
    (4, 2, "2栋北消防出入口", "exit", 500, 140, 30),
    (5, 2, "2栋南消防出入口", "exit", 500, 470, 30),
    (6, 2, "2栋东侧消防车道", "lane", 640, 300, 22),
    (7, 3, "3栋北消防出入口", "exit", 820, 140, 30),
    (8, 3, "3栋南消防出入口", "exit", 820, 470, 30),
    (9, 2, "中央消防回车场", "lane", 500, 300, 26),
]

# username, pw, name, role, building, phone, plates
USERS = [
    ("admin", "admin123", "周敏(值班负责人)", "supervisor", None, "13900000000", []),
    ("guard1", "guard123", "王强(1栋保安)", "guard", 1, "13800000001", []),
    ("guard4", "guard123", "陈磊(1栋巡逻岗)", "guard", 1, "13800000004", []),
    ("guard2", "guard123", "李娜(2栋保安)", "guard", 2, "13800000002", []),
    ("guard3", "guard123", "赵磊(3栋保安)", "guard", 3, "13800000003", []),
    ("resident1", "resident123", "陈晨(1栋住户)", "resident", 1, "13700000001",
     ["京A12345", "京B88888"]),
    ("resident2", "resident123", "刘洋(2栋住户)", "resident", 2, "13700000002",
     ["沪A2F68T"]),
    ("resident3", "resident123", "孙莉(3栋住户)", "resident", 3, "13700000003",
     ["粤B33333"]),
]

DAY = 86400_000


def _png_tile(path, rgb, w=240, h=160):
    """生成带斜纹的占位照片,避免引入 Pillow 依赖。"""
    r0, g0, b0 = rgb
    rows = []
    for y in range(h):
        row = bytearray([0])
        for x in range(w):
            stripe = 18 if ((x + y) // 16) % 2 == 0 else 0
            edge = 26 if (x < 8 or y < 8 or x >= w - 8 or y >= h - 8) else 0
            row += bytes((max(0, r0 - stripe - edge),
                          max(0, g0 - stripe - edge),
                          max(0, b0 - stripe - edge)))
        rows.append(bytes(row))
    raw = b"".join(rows)

    def chunk(tag, data):
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(raw, 6))
           + chunk(b"IEND", b""))
    with open(path, "wb") as f:
        f.write(png)


def _placeholder_photo(svc, event_id, kind, rgb):
    import base64
    fname = f"seed_e{event_id}_{kind}.png"
    fpath = os.path.join(svc.photo_dir, fname)
    _png_tile(fpath, rgb)
    with open(fpath, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    return svc._save_photo(event_id, kind, b64, 1)  # noqa: SLF001


def _backdate(db, eid, found_delta_ms, closed_delta_ms=None):
    """把某事件的全部时间戳整体平移,使 found_at = now-found_delta。
    可选把结案点钉到 now-closed_delta。"""
    e = db.queryone("SELECT * FROM events WHERE id=?", (eid,))
    t0 = now_ms()
    shift = (t0 - found_delta_ms) - e["found_at"]
    cols = ["found_at", "created_at", "accepted_at", "tow_requested_at",
            "escalated_at", "tow_cancelled_at", "departed_at",
            "reviewed_at", "closed_at"]
    for c in cols:
        db.execute(f"UPDATE events SET {c}={c}+? WHERE id=? AND {c} IS NOT NULL",
                   (shift, eid))
    db.execute("UPDATE timeline SET created_at=created_at+? WHERE event_id=?", (shift, eid))
    db.execute("UPDATE contact_logs SET created_at=created_at+? WHERE event_id=?", (shift, eid))
    db.execute("UPDATE photos SET created_at=created_at+? WHERE event_id=?", (shift, eid))
    db.execute("UPDATE notifications SET created_at=created_at+? WHERE event_id=?", (shift, eid))
    db.execute("UPDATE sms_outbox SET created_at=created_at+? WHERE event_id=?", (shift, eid))
    db.commit()
    if closed_delta_ms is not None:
        t = now_ms() - closed_delta_ms
        db.execute("UPDATE events SET closed_at=?, reviewed_at=? WHERE id=?",
                   (t, t, eid))
        db.commit()


def seed(db_path, photo_dir, verbose=True):
    if os.path.exists(db_path):
        os.remove(db_path)
    for suffix in ("-wal", "-shm"):
        p = db_path + suffix
        if os.path.exists(p):
            os.remove(p)
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    # 清空旧照片
    if os.path.isdir(photo_dir):
        for f in os.listdir(photo_dir):
            if f.startswith(("seed_", "e")):
                os.remove(os.path.join(photo_dir, f))

    db = Database(db_path)
    svc = Service(db, photo_dir)
    db.begin()
    for bid, name, px, py in BUILDINGS:
        db.execute("INSERT INTO buildings(id,name,px,py) VALUES(?,?,?,?)", (bid, name, px, py))
    for zid, bid, name, kind, px, py, rx in ZONES:
        db.execute("INSERT INTO zones(id,building_id,name,kind,px,py,rx) VALUES(?,?,?,?,?,?,?)",
                   (zid, bid, name, kind, px, py, rx))
    uid = {}
    for username, pw, dname, role, bid, phone, plates in USERS:
        salt, digest = hash_pw(pw)
        cur = db.execute(
            "INSERT INTO users(username,pw_salt,pw_hash,display_name,role,building_id,phone)"
            " VALUES(?,?,?,?,?,?,?)",
            (username, salt, digest, dname, role, bid, phone))
        uid[username] = cur.lastrowid
        for p in plates:
            db.execute("INSERT INTO resident_vehicles(resident_id,plate) VALUES(?,?)",
                       (cur.lastrowid, p))
    db.commit()

    g1, g2, g3, g4 = (db.queryone("SELECT id FROM users WHERE username=?", (n,))["id"]
                      for n in ("guard1", "guard2", "guard3", "guard4"))
    admin = db.queryone("SELECT id FROM users WHERE username='admin'")["id"]

    def U(name):
        return db.queryone("SELECT * FROM users WHERE username=?", (name,))

    # —— 历史:京A12345 五天前二次驶入的前序事件(已结案,中性色)——
    ev, _ = svc.report_event(U("guard1"), "京A12345", 2,
                             is_emergency=False, found_at=now_ms() - 5 * DAY - 3 * 3600_000)
    e_prev = ev["id"]
    svc.accept(U("guard1"), e_prev)
    svc.add_contact(U("guard1"), e_prev, "sms", "promised", "通知后 10 分钟驶离", send_sms=True)
    svc.depart(U("guard1"), e_prev, "self_drive", "车主接通知后驶离")
    svc.review(U("guard4"), e_prev, "通道已恢复")
    _backdate(db, e_prev, 5 * DAY + 3 * 3600_000, 5 * DAY + 2 * 3600_000)
    _placeholder_photo(svc, e_prev, "review", (214, 220, 214))

    # —— 进行中 e1:京A12345 再次占道(二次驶入,新单;红色阻断)——
    ev, _ = svc.report_event(U("guard1"), "京A12345", 1, found_at=now_ms() - 25 * 60_000)
    e1 = ev["id"]
    _placeholder_photo(svc, e1, "report", (210, 90, 90))
    # 再上报一次 → 自动合并
    svc.report_event(U("guard4"), "京A12345", 1, found_at=now_ms() - 8 * 60_000)
    _backdate(db, e1, 25 * 60_000)
    # _backdate 把整条事件平移;合并记录也要早于"现在"
    db.execute("UPDATE timeline SET created_at=? WHERE event_id=? AND kind='duplicate_report'",
               (now_ms() - 8 * 60_000, e1))
    db.commit()

    # —— 进行中 e2:巡查中,已短信联系,承诺驶离 ——
    ev, _ = svc.report_event(U("guard2"), "沪A2F68T", 5, found_at=now_ms() - 47 * 60_000)
    e2 = ev["id"]
    _placeholder_photo(svc, e2, "report", (210, 90, 90))
    svc.accept(U("guard2"), e2)
    svc.add_contact(U("guard2"), e2, "sms", "promised", "车主称 15 分钟内到场", send_sms=True)
    _backdate(db, e2, 47 * 60_000)

    # —— 进行中 e3:电话拒绝驶离,已申请拖移,待值班负责人升级 ——
    ev, _ = svc.report_event(U("guard3"), "粤B33333", 8, found_at=now_ms() - 70 * 60_000)
    e3 = ev["id"]
    _placeholder_photo(svc, e3, "report", (210, 90, 90))
    svc.accept(U("guard3"), e3)
    svc.add_contact(U("guard3"), e3, "sms", "reached", "短信已送达", send_sms=True)
    svc.add_contact(U("guard3"), e3, "phone", "refused", "车主拒绝挪车,称临时停车")
    svc.request_tow(U("guard3"), e3, "阻断南消防出入口超 1 小时,车主明确拒绝驶离")
    _backdate(db, e3, 70 * 60_000)

    # —— 待复核 e4:车主已驶离,必须由另一名保安复核 ——
    ev, _ = svc.report_event(U("guard1"), "京B88888", 3, found_at=now_ms() - 95 * 60_000)
    e4 = ev["id"]
    _placeholder_photo(svc, e4, "report", (210, 90, 90))
    svc.accept(U("guard1"), e4)
    svc.add_contact(U("guard1"), e4, "sms", "reached", send_sms=True)
    svc.depart(U("guard1"), e4, "self_drive", "车主返回后自行离开",
               photo_b64=None)
    _placeholder_photo(svc, e4, "departure", (206, 214, 224))
    _backdate(db, e4, 95 * 60_000)

    # —— 历史 e5:识别错误 + 联系失败 + 升级 + 拖移取消(完整留痕)——
    ev, _ = svc.report_event(U("guard2"), "苏E77177", 6,
                             lpr_raw="苏E77177", lpr_confidence=0.62,
                             found_at=now_ms() - 3 * DAY)
    e5 = ev["id"]
    _placeholder_photo(svc, e5, "report", (190, 190, 175))
    svc.accept(U("guard2"), e5)
    svc.correct_plate(U("guard2"), e5, "苏E77777", "现场核对行驶证,识别器把末位 7 认成 1")
    svc.add_contact(U("guard2"), e5, "sms", "failed", "号码有误/联系失败留痕", send_sms=True)
    svc.request_tow(U("guard2"), e5, "无法联系车主,阻断东侧消防车道")
    svc.escalate(U("admin"), e5, True, "同意拖移,拖车途中车主到场")
    svc.cancel_tow(U("admin"), e5, "车主赶到现场并承诺立即自行驶离,撤销拖移")
    svc.depart(U("guard2"), e5, "self_drive", "车主自行离开")
    svc.review(U("admin"), e5, "通道恢复,拖移已取消")
    _backdate(db, e5, 3 * DAY, 3 * DAY - 40 * 60_000)

    # —— 历史 e6:紧急救援占道,救援结束放行 ——
    ev, _ = svc.report_event(U("guard3"), "川A9J120", 7, is_emergency=True,
                             found_at=now_ms() - 2 * DAY)
    e6 = ev["id"]
    _placeholder_photo(svc, e6, "report", (235, 200, 130))
    svc.accept(U("guard3"), e6)
    svc.add_contact(U("guard3"), e6, "onsite", "reached", "120 急救临时占道,已现场疏导")
    svc.depart(U("guard3"), e6, "emergency_release", "急救完成,车辆离场")
    svc.review(U("admin"), e6, "救援结束,通道恢复")
    _backdate(db, e6, 2 * DAY, 2 * DAY - 35 * 60_000)

    # —— 历史 e7:普通已结案 ——
    ev, _ = svc.report_event(U("guard2"), "津C56789", 4, found_at=now_ms() - 26 * 3600_000)
    e7 = ev["id"]
    _placeholder_photo(svc, e7, "report", (190, 190, 175))
    svc.accept(U("guard2"), e7)
    svc.add_contact(U("guard2"), e7, "sms", "promised", send_sms=True)
    svc.depart(U("guard2"), e7, "self_drive")
    svc.review(U("admin"), e7, "复核通过")
    _backdate(db, e7, 26 * 3600_000, 25 * 3600_000)

    # 模拟开关默认正常
    db.set_kv("lpr_mode", "normal")
    db.set_kv("sms_mode", "normal")
    db.commit()

    if verbose:
        print(f"演示数据已写入 {db_path}(事件 {len(db.query('SELECT id FROM events'))} 条)")
    return svc
