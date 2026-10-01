"""消防通道临停管控系统 —— 零依赖 HTTP 服务（标准库 http.server + sqlite3）。

启动：python3 server.py [--port 8000] [--host 127.0.0.1]
演示账号：zhang/li（住户）、guard1/guard2/guard3（保安）、sup（值班负责人），密码均 123456
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import secrets
import threading
import time
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from database import (
    UPLOAD_DIR, get_conn, init_db, now, sha256, normalize_plate,
    duration_text, occupancy_seconds, ACTIVE_STATUSES,
)

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

ROLE_RESIDENT, ROLE_GUARD, ROLE_SUP = "resident", "guard", "supervisor"
STAFF = (ROLE_GUARD, ROLE_SUP)

# 可从 accepted/contacted 等状态发起处置动作的“处置中”集合
BLOCKING_STATUSES = ("open", "accepted", "contacted", "tow_requested",
                     "tow_approved", "emergency")

DEMO_PLATES = ["京A12345", "京A66666", "京B88888", "沪C9D8E7", "粤B10086",
               "津A55555", "冀F66688"]


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


# ---------------- 鉴权与查询辅助 ----------------

def current_user(conn, token: str | None):
    if not token:
        return None
    row = conn.execute(
        "SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id "
        "WHERE s.token = ?", (token,)).fetchone()
    return row


def user_plates(conn, user_id: int) -> set[str]:
    return {r["plate_norm"] for r in conn.execute(
        "SELECT plate_norm FROM residents_vehicles WHERE user_id = ?",
        (user_id,)).fetchall()}


def can_see_event(user, plates: set[str], ev) -> bool:
    if user["role"] == ROLE_SUP:
        return True
    if user["role"] == ROLE_GUARD:
        return ev["building_id"] == user["building_id"]
    return ev["plate_norm"] in plates


def require_role(user, roles: tuple[str, ...]):
    if user is None:
        raise ApiError(401, "请先登录")
    if user["role"] not in roles:
        raise ApiError(403, "当前角色无权执行该操作")


def require_staff(user):
    require_role(user, STAFF)


def get_visible_event(conn, event_id: int, user):
    ev = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
    if ev is None:
        raise ApiError(404, "事件不存在")
    if not can_see_event(user, user_plates(conn, user["id"]), ev):
        # 跨楼栋/非本人车辆：统一按不可见处理
        raise ApiError(403, "该事件不在你的可见范围内")
    return ev


def add_timeline(conn, event_id: int, actor, ttype: str, detail: dict | None = None,
                 ts: str | None = None):
    conn.execute(
        "INSERT INTO timeline(event_id,ts,actor_user_id,actor_name,type,detail)"
        " VALUES(?,?,?,?,?,?)",
        (event_id, ts or now(), actor["id"] if actor else None,
         actor["display_name"] if actor else "系统", ttype,
         json.dumps(detail, ensure_ascii=False) if detail is not None else None))


def get_setting(conn, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn, key: str, value: str):
    conn.execute("INSERT INTO settings(key,value) VALUES(?,?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def save_data_url(data_url: str, event_id: int, user_id: int, kind: str) -> str:
    """保存 data URL 图片到本地目录，返回文件名。仅接受 image/png|jpeg。"""
    m = re.match(r"data:image/(png|jpeg|jpg);base64,(.+)", data_url, re.S)
    if not m:
        raise ApiError(400, "照片格式不正确（需要 image/png 或 image/jpeg）")
    ext = "jpg" if m.group(1) in ("jpeg", "jpg") else "png"
    raw = base64.b64decode(m.group(2)[:8_000_000])
    fname = f"ev{event_id}_{kind}_{int(time.time() * 1000)}_{secrets.token_hex(3)}.{ext}"
    with open(os.path.join(UPLOAD_DIR, fname), "wb") as f:
        f.write(raw)
    return fname


def attach_photo(conn, event_id: int, user, data_url: str, kind: str,
                 caption: str = "") -> int | None:
    if not data_url:
        return None
    fname = save_data_url(data_url, event_id, user["id"], kind)
    cur = conn.execute(
        "INSERT INTO photos(event_id,ts,user_id,user_name,kind,filename,caption)"
        " VALUES(?,?,?,?,?,?,?)",
        (event_id, now(), user["id"], user["display_name"], kind, fname, caption))
    return cur.lastrowid


# ---------------- 业务动作 ----------------

def action_report(conn, user, body):
    """上报/巡查上报：同车同通道在案事件自动合并（幂等），不重复开单。"""
    require_role(user, STAFF)
    channel_id = int(body.get("channel_id") or 0)
    ch = conn.execute("SELECT * FROM channels WHERE id=?", (channel_id,)).fetchone()
    if ch is None:
        raise ApiError(400, "请选择消防通道")
    if user["role"] == ROLE_GUARD and ch["building_id"] != user["building_id"]:
        raise ApiError(403, "不能在非所属楼栋开单")

    plate_display = (body.get("plate_display") or "").strip()
    plate_norm = normalize_plate(plate_display)
    if not plate_norm:
        raise ApiError(400, "车牌号不能为空")
    kind = body.get("kind", "violation")
    if kind not in ("violation", "emergency"):
        kind = "violation"
    note = (body.get("note") or "").strip()

    conn.execute("BEGIN IMMEDIATE")
    try:
        existing = conn.execute(
            "SELECT * FROM events WHERE plate_norm=? AND channel_id=? AND status<>'closed'",
            (plate_norm, channel_id)).fetchone()

        if existing is not None:
            # —— 同一占用事件：绝不重复开单 ——
            if existing["status"] == "pending_review":
                # 车辆在待复核期间二次驶入：原占用延续，事件重开
                conn.execute(
                    "UPDATE events SET status='accepted', leave_at=NULL, "
                    "handled_by=NULL, resolution=NULL, reviewed_by=NULL, "
                    "reviewed_at=NULL, review_note=NULL, closed_at=NULL, "
                    "merged_report_count=merged_report_count+1 WHERE id=?",
                    (existing["id"],))
                add_timeline(conn, existing["id"], user, "reentry",
                             {"plate": plate_display,
                              "note": "车辆二次驶入，通道再次被阻断，事件重开"})
                event_id, merged = existing["id"], True
            else:
                conn.execute(
                    "UPDATE events SET merged_report_count=merged_report_count+1 WHERE id=?",
                    (existing["id"],))
                add_timeline(conn, existing["id"], user, "repeat_report",
                             {"plate": plate_display,
                              "note": "重复上报已合并到本事件，未生成新单"})
                event_id, merged = existing["id"], True
            if note:
                add_timeline(conn, event_id, user, "report_note", {"note": note})
            attach_photo(conn, event_id, user, body.get("photo") or "", "scene",
                         "重复上报现场照" if merged else "现场照")
            conn.commit()
            return {"id": event_id, "merged": True,
                    "message": "该车辆在此通道已有在案事件，上报已合并，未重复开单"}

        cur = conn.execute(
            "INSERT INTO events(channel_id,building_id,plate_display,plate_norm,kind,"
            "status,found_at,found_by,note) VALUES(?,?,?,?,?,?,?,?,?)",
            (channel_id, ch["building_id"], plate_display, plate_norm, kind,
             "emergency" if kind == "emergency" else "open", now(), user["id"], note))
        event_id = cur.lastrowid
        add_timeline(conn, event_id, user, "created",
                     {"plate": plate_display, "channel": ch["name"],
                      "kind": kind, "note": note})
        attach_photo(conn, event_id, user, body.get("photo") or "", "scene", "发现现场照")
        conn.commit()
        return {"id": event_id, "merged": False, "message": "事件单已生成"}
    except Exception:
        conn.rollback()
        raise


def _load_event_for_action(conn, event_id, user, staff=True):
    if staff:
        require_staff(user)
    ev = get_visible_event(conn, event_id, user)
    if ev["status"] == "closed":
        raise ApiError(409, "事件已结案封闭，不能再改写")
    return ev


def action_accept(conn, user, body, event_id):
    ev = _load_event_for_action(conn, event_id, user)
    if ev["status"] != "open":
        raise ApiError(409, "仅待接单状态可接单")
    conn.execute("BEGIN IMMEDIATE")
    cur = conn.execute(
        "UPDATE events SET status='accepted', assigned_guard_id=? "
        "WHERE id=? AND status='open'", (user["id"], event_id))
    if cur.rowcount == 0:
        conn.rollback()
        raise ApiError(409, "事件状态已变化，可能已被其他保安接单")
    add_timeline(conn, event_id, user, "accept", {"guard": user["display_name"]})
    conn.commit()
    return {"message": "已接单，请尽快到场核查"}


def action_contact(conn, user, body, event_id):
    ev = _load_event_for_action(conn, event_id, user)
    if ev["status"] not in ("accepted", "contacted", "tow_rejected"):
        raise ApiError(409, "当前状态不能登记联系记录")
    method = body.get("method", "sms")
    if method not in ("sms", "phone", "onsite"):
        raise ApiError(400, "联系方式不合法")
    result = body.get("result", "")
    allowed = {"sms": ("delivered", "failed"),
               "phone": ("delivered", "no_answer", "promised_left", "refused"),
               "onsite": ("promised_left", "refused")}
    if result not in allowed[method]:
        raise ApiError(400, "联系结果不合法")

    # 车主信息（按车牌匹配住户车辆）
    owner = conn.execute(
        "SELECT u.phone,u.display_name FROM residents_vehicles rv "
        "JOIN users u ON u.id=rv.user_id WHERE rv.plate_norm=?",
        (ev["plate_norm"],)).fetchone()
    phone = (owner["phone"] if owner else "") or "未知号码"

    gateway = "skipped"
    content = ""
    if method == "sms":
        ch = conn.execute("SELECT name FROM channels WHERE id=?",
                          (ev["channel_id"],)).fetchone()
        content = (f"【物业消防管控】您的车辆 {ev['plate_display']} 正占用{ch['name']}，"
                   "属消防生命通道，请立即驶离，逾期将依法拖移并追责。")
        mode = get_setting(conn, "sms_mode", "normal")
        gateway = "failed" if mode == "fail" else "delivered"
        if gateway == "failed":
            result = "failed"   # 模拟网关：失败模式下短信一律发送失败
        conn.execute(
            "INSERT INTO sms_outbox(event_id,phone,content,status,ts) VALUES(?,?,?,?,?)",
            (event_id, phone, content, gateway, now()))

    conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        "INSERT INTO contact_logs(event_id,ts,user_id,user_name,method,result,phone,content)"
        " VALUES(?,?,?,?,?,?,?,?)",
        (event_id, now(), user["id"], user["display_name"], method, result, phone, content))
    if ev["status"] == "accepted":
        conn.execute("UPDATE events SET status='contacted' WHERE id=?", (event_id,))
    add_timeline(conn, event_id, user, "contact",
                 {"method": method, "result": result, "phone": phone,
                  "gateway": gateway, "owner": owner["display_name"] if owner else "未匹配到登记车主"})
    conn.commit()
    msg = "联系失败，已留痕，可继续电话/上门或申请拖移" if result == "failed" \
        else "联系记录已登记"
    return {"message": msg, "gateway": gateway}


def action_correct_plate(conn, user, body, event_id):
    """车牌识别错误：更正车牌并留痕（旧牌 -> 新牌）。"""
    ev = _load_event_for_action(conn, event_id, user)
    new_display = (body.get("new_plate") or "").strip()
    new_norm = normalize_plate(new_display)
    if not new_norm:
        raise ApiError(400, "新车牌号不能为空")
    if new_norm == ev["plate_norm"]:
        raise ApiError(400, "新车牌与原车牌一致")
    clash = conn.execute(
        "SELECT id FROM events WHERE plate_norm=? AND channel_id=? AND status<>'closed' AND id<>?",
        (new_norm, ev["channel_id"], event_id)).fetchone()
    if clash:
        raise ApiError(409, "该车牌在本通道已有另一张在案事件单，请人工合并核查")
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("UPDATE events SET plate_display=?, plate_norm=? WHERE id=?",
                 (new_display, new_norm, event_id))
    add_timeline(conn, event_id, user, "plate_corrected",
                 {"old_plate": ev["plate_display"], "new_plate": new_display,
                  "reason": "车牌识别错误，现场人工核对更正"})
    conn.commit()
    return {"message": "车牌已更正，更正记录已留痕"}


def action_emergency(conn, user, body, event_id):
    """紧急救援占道（消防车/救护车等），特殊标记留痕。"""
    ev = _load_event_for_action(conn, event_id, user)
    reason = (body.get("reason") or "").strip() or "紧急救援车辆占道"
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("UPDATE events SET kind='emergency', status='emergency' WHERE id=?",
                 (event_id,))
    add_timeline(conn, event_id, user, "emergency", {"reason": reason})
    conn.commit()
    return {"message": "已标记为紧急救援占道"}


def action_tow_request(conn, user, body, event_id):
    ev = _load_event_for_action(conn, event_id, user)
    if ev["status"] not in ("accepted", "contacted"):
        raise ApiError(409, "仅到场处置中的事件可申请拖移")
    reason = (body.get("reason") or "").strip() or "拒不驶离，持续阻断消防通道"
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        "UPDATE events SET status='tow_requested', tow_status='requested',"
        " tow_requested_by=?, tow_requested_at=? WHERE id=?",
        (user["id"], now(), event_id))
    add_timeline(conn, event_id, user, "tow_requested",
                 {"reason": reason, "awaiting": "值班负责人确认"})
    conn.commit()
    return {"message": "拖移申请已提交，等待值班负责人确认"}


def action_tow_approve(conn, user, body, event_id):
    ev = _load_event_for_action(conn, event_id, user)
    require_role(user, (ROLE_SUP,))
    if ev["status"] != "tow_requested":
        raise ApiError(409, "仅待审批的拖移申请可确认")
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        "UPDATE events SET status='tow_approved', tow_status='approved',"
        " tow_approved_by=?, tow_approved_at=? WHERE id=? AND status='tow_requested'",
        (user["id"], now(), event_id))
    add_timeline(conn, event_id, user, "tow_approved",
                 {"decision": "值班负责人确认升级拖移"})
    conn.commit()
    return {"message": "已确认升级拖移"}


def action_tow_reject(conn, user, body, event_id):
    ev = _load_event_for_action(conn, event_id, user)
    require_role(user, (ROLE_SUP,))
    if ev["status"] != "tow_requested":
        raise ApiError(409, "仅待审批的拖移申请可驳回")
    reason = (body.get("reason") or "").strip() or "不符合拖移条件"
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        "UPDATE events SET status='contacted', tow_status='rejected' WHERE id=?",
        (event_id,))
    add_timeline(conn, event_id, user, "tow_rejected", {"reason": reason})
    conn.commit()
    return {"message": "拖移申请已驳回"}


def action_tow_cancel(conn, user, body, event_id):
    """拖移取消留痕（车主挪车/现场解除等）。"""
    ev = _load_event_for_action(conn, event_id, user)
    if ev["status"] not in ("tow_requested", "tow_approved"):
        raise ApiError(409, "仅申请中/已批准的拖移可取消")
    reason = (body.get("reason") or "").strip() or "拖移取消"
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        "UPDATE events SET status='contacted', tow_status='cancelled' WHERE id=?",
        (event_id,))
    add_timeline(conn, event_id, user, "tow_cancelled", {"reason": reason})
    conn.commit()
    return {"message": "拖移已取消并留痕"}


def action_tow_complete(conn, user, body, event_id):
    ev = _load_event_for_action(conn, event_id, user)
    if ev["status"] != "tow_approved":
        raise ApiError(409, "仅已批准的拖移可登记拖离")
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        "UPDATE events SET status='pending_review', resolution='towed',"
        " handled_by=?, leave_at=?, tow_result='已拖移至指定暂扣点' WHERE id=?",
        (user["id"], now(), event_id))
    add_timeline(conn, event_id, user, "towed",
                 {"handler": user["display_name"], "note": "车辆已拖离，等待清障复核"})
    attach_photo(conn, event_id, user, body.get("photo") or "", "scene", "拖离后现场照")
    conn.commit()
    return {"message": "拖离完成，等待另一名保安复核通道"}


def action_leave(conn, user, body, event_id):
    """登记车主驶离（自行驶离/被劝离），进入待复核；实际处置人记录在案。"""
    ev = _load_event_for_action(conn, event_id, user)
    if ev["status"] not in ("accepted", "contacted", "tow_requested",
                            "tow_approved", "emergency"):
        raise ApiError(409, "当前状态不能登记驶离")
    resolution = "emergency" if ev["kind"] == "emergency" else "self_left"
    conn.execute("BEGIN IMMEDIATE")
    auto_cancel = ev["status"] in ("tow_requested", "tow_approved")
    conn.execute(
        "UPDATE events SET status='pending_review', resolution=?, handled_by=?,"
        " leave_at=?, tow_status=CASE WHEN tow_status IN('requested','approved')"
        " THEN 'cancelled' ELSE tow_status END WHERE id=?",
        (resolution, user["id"], now(), event_id))
    add_timeline(conn, event_id, user, "left",
                 {"handler": user["display_name"], "resolution": resolution,
                  "note": "车辆已驶离，等待清障复核"})
    if auto_cancel:
        add_timeline(conn, event_id, user, "tow_cancelled",
                     {"reason": "车主已自行驶离，拖移自动取消"})
    attach_photo(conn, event_id, user, body.get("photo") or "", "scene", "驶离后现场照")
    conn.commit()
    return {"message": "驶离已登记，等待另一名保安复核通道恢复"}


def action_review(conn, user, body, event_id):
    """清障复核：复核人必须与处置人不同；通过则原子结案，不通过则事件重开。"""
    require_staff(user)
    ev = get_visible_event(conn, event_id, user)
    if ev["status"] != "pending_review":
        raise ApiError(409, "仅待复核事件可复核")
    if ev["handled_by"] == user["id"]:
        raise ApiError(403, "复核人与实际处置人不能为同一人，请由另一名保安复核")
    passed = bool(body.get("passed"))
    note = (body.get("note") or "").strip()

    conn.execute("BEGIN IMMEDIATE")
    if passed:
        cur = conn.execute(
            "UPDATE events SET status='closed', reviewed_by=?, reviewed_at=?,"
            " review_note=?, closed_at=? WHERE id=? AND status='pending_review'",
            (user["id"], now(), note or "通道已恢复畅通", now(), event_id))
        if cur.rowcount == 0:
            conn.rollback()
            raise ApiError(409, "并发冲突：事件已被其他人结案或状态已变化")
        add_timeline(conn, event_id, user, "review_passed",
                     {"reviewer": user["display_name"],
                      "handler_id": ev["handled_by"], "note": note})
        attach_photo(conn, event_id, user, body.get("photo") or "", "clearance",
                     "清障复核照")
        conn.commit()
        return {"message": "复核通过，事件结案"}

    # 复核不通过：通道未恢复/又被占用，事件重开
    conn.execute(
        "UPDATE events SET status='accepted', leave_at=NULL, handled_by=NULL,"
        " resolution=NULL, reviewed_by=NULL, reviewed_at=NULL, review_note=NULL,"
        " closed_at=NULL WHERE id=? AND status='pending_review'", (event_id,))
    add_timeline(conn, event_id, user, "review_failed",
                 {"reviewer": user["display_name"], "note": note or "通道未恢复畅通，事件重开"})
    attach_photo(conn, event_id, user, body.get("photo") or "", "scene", "复核未通过现场照")
    conn.commit()
    return {"message": "复核未通过，事件已重新打开"}


# ---------------- 查询类 ----------------

def event_summary(conn, ev) -> dict:
    ch = conn.execute("SELECT * FROM channels WHERE id=?", (ev["channel_id"],)).fetchone()
    b = conn.execute("SELECT name FROM buildings WHERE id=?", (ev["building_id"],)).fetchone()
    names = {}
    for key, uid in (("found_by", ev["found_by"]), ("assigned_guard", ev["assigned_guard_id"]),
                     ("handled", ev["handled_by"]), ("reviewer", ev["reviewed_by"])):
        if uid:
            r = conn.execute("SELECT display_name FROM users WHERE id=?", (uid,)).fetchone()
            names[key] = r["display_name"] if r else str(uid)
    secs = occupancy_seconds(ev)
    return {
        "id": ev["id"], "plate": ev["plate_display"], "kind": ev["kind"],
        "status": ev["status"], "tow_status": ev["tow_status"],
        "channel": ch["name"], "channel_code": ch["code"], "channel_id": ch["id"],
        "building": b["name"], "building_id": ev["building_id"],
        "found_at": ev["found_at"], "closed_at": ev["closed_at"],
        "leave_at": ev["leave_at"],
        "duration_secs": secs, "duration": duration_text(ev["found_at"],
                                                         ev["leave_at"] or ev["closed_at"]),
        "found_by": names.get("found_by", ""),
        "assigned_guard": names.get("assigned_guard", ""),
        "handled_by": names.get("handled", ""),
        "reviewer": names.get("reviewer", ""),
        "merged_count": ev["merged_report_count"],
        "blocking": ev["status"] in BLOCKING_STATUSES,
    }


def list_events(conn, user, visible_all: bool):
    plates = user_plates(conn, user["id"])
    rows = conn.execute("SELECT * FROM events ORDER BY "
                        "CASE WHEN status='closed' THEN 1 ELSE 0 END, found_at DESC").fetchall()
    out = []
    for ev in rows:
        if can_see_event(user, plates, ev):
            out.append(event_summary(conn, ev))
    return out


def event_detail(conn, user, event_id: int) -> dict:
    ev = get_visible_event(conn, event_id, user)
    s = event_summary(conn, ev)
    s["note"] = ev["note"] or ""
    s["resolution"] = ev["resolution"] or ""
    s["leave_at"] = ev["leave_at"]
    s["contacts"] = [dict(r) for r in conn.execute(
        "SELECT * FROM contact_logs WHERE event_id=? ORDER BY id", (event_id,)).fetchall()]
    s["photos"] = [{"id": r["id"], "kind": r["kind"], "url": f"/uploads/{r['filename']}",
                    "caption": r["caption"], "ts": r["ts"], "by": r["user_name"]}
                   for r in conn.execute(
                       "SELECT * FROM photos WHERE event_id=? ORDER BY id", (event_id,))]
    s["timeline"] = [{"ts": r["ts"], "actor": r["actor_name"], "type": r["type"],
                      "detail": json.loads(r["detail"]) if r["detail"] else None}
                     for r in conn.execute(
                         "SELECT * FROM timeline WHERE event_id=? ORDER BY id", (event_id,))]
    s["can_handle"] = user["role"] in STAFF
    s["is_supervisor"] = user["role"] == ROLE_SUP
    s["i_am_handler"] = ev["handled_by"] == user["id"]
    return s


def meta_payload(conn, user) -> dict:
    plates = user_plates(conn, user["id"])
    channels = []
    for ch in conn.execute("SELECT c.*, b.name building_name FROM channels c "
                           "JOIN buildings b ON b.id=c.building_id ORDER BY c.id").fetchall():
        active = conn.execute(
            "SELECT id,plate_display,kind,status,found_at FROM events "
            "WHERE channel_id=? AND status<>'closed' ORDER BY id", (ch["id"],)).fetchall()
        blocking = [a for a in active if a["status"] in BLOCKING_STATUSES]
        # 保安可见范围限本楼栋；住户可看全小区总平图，但通道上只暴露涉及本人车辆的事件
        in_scope = user["role"] != ROLE_GUARD or ch["building_id"] == user["building_id"]
        entry = {
            "id": ch["id"], "code": ch["code"], "name": ch["name"],
            "building_id": ch["building_id"], "building": ch["building_name"],
            "anchor": ch["anchor_name"], "polygon": ch["polygon"],
            "label_x": ch["label_x"], "label_y": ch["label_y"],
            "exit_x": ch["exit_x"], "exit_y": ch["exit_y"],
            "in_scope": in_scope,
            "active_count": len(active) if in_scope and user["role"] != ROLE_RESIDENT else 0,
            "blocked": len(blocking) > 0,
        }
        if not in_scope:
            entry["active_events"] = []
        elif user["role"] in STAFF:
            entry["active_events"] = [{"id": a["id"], "plate": a["plate_display"],
                                       "kind": a["kind"], "status": a["status"],
                                       "found_at": a["found_at"]} for a in active]
        else:
            # 住户只收到涉及本人车牌的事件；通道级 blocked 仅用于红色阻断提示，不含他人信息
            entry["active_events"] = [{"id": a["id"], "mine": True}
                                      for a in active
                                      if normalize_plate(a["plate_display"]) in plates]
        channels.append(entry)
    return {
        "user": {"id": user["id"], "username": user["username"],
                 "display_name": user["display_name"], "role": user["role"],
                 "building_id": user["building_id"],
                 "plates": sorted(user_plates(conn, user["id"]))},
        "channels": channels,
        "sim": {"sms_mode": get_setting(conn, "sms_mode", "normal"),
                "lpr_next": get_setting(conn, "lpr_next", "")},
    }


def stats_payload(conn, user) -> dict:
    require_staff(user)
    rows = conn.execute("SELECT * FROM events", ).fetchall()
    plates = user_plates(conn, user["id"])
    evs = [e for e in rows if can_see_event(user, plates, e)]
    closed = [e for e in evs if e["status"] == "closed"]
    by_building: dict[str, dict] = {}
    for e in evs:
        bn = conn.execute("SELECT name FROM buildings WHERE id=?",
                          (e["building_id"],)).fetchone()["name"]
        d = by_building.setdefault(bn, {"total": 0, "active": 0, "blocking": 0})
        d["total"] += 1
        if e["status"] != "closed":
            d["active"] += 1
        if e["status"] in BLOCKING_STATUSES:
            d["blocking"] += 1
    durations = [occupancy_seconds(e) for e in closed]
    avg = sum(durations) // len(durations) if durations else 0
    return {
        "total": len(evs), "active": sum(1 for e in evs if e["status"] != "closed"),
        "closed": len(closed),
        "blocking": sum(1 for e in evs if e["status"] in BLOCKING_STATUSES),
        "emergency": sum(1 for e in evs if e["kind"] == "emergency"),
        "tow_requested": sum(1 for e in evs if e["tow_status"] in ("requested", "approved", "rejected", "cancelled")),
        "towed": sum(1 for e in evs if e["resolution"] == "towed"),
        "merged_reports": sum(e["merged_report_count"] for e in evs),
        "avg_closure_secs": avg,
        "avg_closure": duration_text(now_ts_back(avg)),
        "by_building": [{"building": k, **v} for k, v in sorted(by_building.items())],
    }


def now_ts_back(secs: int) -> str:
    from datetime import datetime, timedelta
    return (datetime.now() - timedelta(seconds=secs)).strftime("%Y-%m-%d %H:%M:%S")


# ---------------- HTTP 处理器 ----------------

class Handler(BaseHTTPRequestHandler):
    server_version = "FireLane/1.0"

    def log_message(self, fmt, *args):
        if os.environ.get("FIRELANE_QUIET"):
            return
        super().log_message(fmt, *args)

    # -- 基础收发 --
    def _send_json(self, obj, status=200, extra_headers=None):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra_headers or []):
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _error(self, status, message):
        self._send_json({"error": message}, status)

    def _token(self):
        cookie_header = self.headers.get("Cookie", "")
        if cookie_header:
            try:
                ck = cookies.SimpleCookie()
                ck.load(cookie_header)
                if "fl_token" in ck:
                    return ck["fl_token"].value
            except Exception:
                pass
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            return auth[7:]
        return None

    def _user(self, conn):
        return current_user(conn, self._token())

    def _read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > 12_000_000:
            raise ApiError(413, "上传内容过大（上限约 9MB）")
        raw = self.rfile.read(length) if length else b"{}"
        try:
            return json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            raise ApiError(400, "请求体不是合法 JSON")

    def _static(self, rel: str):
        rel = rel.lstrip("/")
        if not rel:
            rel = "index.html"
        path = os.path.normpath(os.path.join(STATIC_DIR, rel))
        if not path.startswith(STATIC_DIR + os.sep) or not os.path.isfile(path):
            self._error(404, "资源不存在")
            return
        ctype = {"html": "text/html; charset=utf-8", "js": "text/javascript; charset=utf-8",
                 "css": "text/css; charset=utf-8", "png": "image/png",
                 "svg": "image/svg+xml"}.get(path.rsplit(".", 1)[-1], "application/octet-stream")
        with open(path, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _upload(self, name: str, conn, user):
        path = os.path.normpath(os.path.join(UPLOAD_DIR, name))
        if not path.startswith(UPLOAD_DIR + os.sep) or not os.path.isfile(path):
            self._error(404, "照片不存在")
            return
        photo = conn.execute("SELECT * FROM photos WHERE filename=?", (name,)).fetchone()
        if photo is None:
            self._error(404, "照片不存在")
            return
        ev = conn.execute("SELECT * FROM events WHERE id=?", (photo["event_id"],)).fetchone()
        if not can_see_event(user, user_plates(conn, user["id"]), ev):
            self._error(403, "无权查看该照片")
            return
        with open(path, "rb") as f:
            data = f.read()
        ctype = "image/png" if name.endswith(".png") else "image/jpeg"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "private, max-age=60")
        self.end_headers()
        self.wfile.write(data)

    # -- 路由 --
    def do_GET(self):
        parsed = urlparse(self.path)
        path, qs = parsed.path, parse_qs(parsed.query)
        try:
            conn = get_conn()
            user = self._user(conn)
            if path == "/" or path.startswith("/static/"):
                return self._static(path[len("/static/"):] if path.startswith("/static/") else "")
            if path.startswith("/uploads/"):
                if user is None:
                    return self._error(401, "请先登录")
                return self._upload(os.path.basename(path), conn, user)
            if not path.startswith("/api/"):
                return self._error(404, "Not found")

            if path == "/api/login":
                return self._error(405, "请使用 POST 登录")
            if user is None:
                return self._error(401, "请先登录")

            if path == "/api/me":
                return self._send_json(meta_payload(conn, user))
            if path == "/api/events":
                return self._send_json({"events": list_events(conn, user, True)})
            m = re.fullmatch(r"/api/events/(\d+)", path)
            if m:
                return self._send_json(event_detail(conn, user, int(m.group(1))))
            if path == "/api/notifications":
                # 与 /api/events 同数据，语义别名（住户“我的通知”）
                return self._send_json({"events": list_events(conn, user, False)})
            if path == "/api/stats":
                return self._send_json(stats_payload(conn, user))
            if path == "/api/sms-outbox":
                require_staff(user)
                rows = conn.execute(
                    "SELECT o.* FROM sms_outbox o JOIN events e ON e.id=o.event_id "
                    + ("" if user["role"] == ROLE_SUP else "WHERE e.building_id=? ")
                    + "ORDER BY o.id DESC LIMIT 100",
                    (() if user["role"] == ROLE_SUP else (user["building_id"],))).fetchall()
                return self._send_json({"messages": [dict(r) for r in rows]})
            if path == "/api/lpr":
                require_staff(user)
                injected = get_setting(conn, "lpr_next", "")
                if injected:
                    set_setting(conn, "lpr_next", "")
                    plate = injected
                    source = "模拟识别（控制台注入）"
                else:
                    # 普通模拟：从演示车牌池给一个（含少量识别错误概率）
                    plate = secrets.choice(DEMO_PLATES)
                    if secrets.randbelow(10) == 0:
                        plate = plate[:-1] + ("8" if plate[-1] != "8" else "0")
                    source = "摄像头模拟识别"
                return self._send_json({"plate": plate, "source": source})
            self._error(404, "接口不存在")
        except ApiError as e:
            self._error(e.status, e.message)
        except Exception as e:  # noqa: BLE001
            self._error(500, f"服务器错误: {e}")
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            body = self._read_body()
            conn = get_conn()
            user = self._user(conn)

            if path == "/api/login":
                return self._do_login(conn, body)
            if path == "/api/logout":
                if user:
                    conn.execute("DELETE FROM sessions WHERE token=?", (self._token(),))
                return self._send_json({"ok": True},
                                       extra_headers=[("Set-Cookie",
                                                       "fl_token=; Path=/; Max-Age=0")])
            if not path.startswith("/api/"):
                return self._error(404, "Not found")
            if user is None:
                return self._error(401, "请先登录")

            # 模拟控制台（值班负责人/保安均可，便于演示）
            if path == "/api/sim/sms-mode":
                require_staff(user)
                mode = body.get("mode", "normal")
                if mode not in ("normal", "fail"):
                    raise ApiError(400, "短信模式不合法")
                set_setting(conn, "sms_mode", mode)
                return self._send_json({"message": f"短信模拟模式：{'失败' if mode == 'fail' else '正常送达'}"})
            if path == "/api/sim/lpr":
                require_staff(user)
                set_setting(conn, "lpr_next", normalize_plate(body.get("plate", "")))
                return self._send_json({"message": "下一次车牌识别结果已注入"})

            if path == "/api/events":
                return self._send_json(action_report(conn, user, body), 201)

            m = re.fullmatch(r"/api/events/(\d+)/(\w[\w-]*)", path)
            if not m:
                return self._error(404, "接口不存在")
            event_id, act = int(m.group(1)), m.group(2)
            handlers = {
                "accept": action_accept,
                "contact": action_contact,
                "correct-plate": action_correct_plate,
                "emergency": action_emergency,
                "tow-request": action_tow_request,
                "tow-approve": action_tow_approve,
                "tow-reject": action_tow_reject,
                "tow-cancel": action_tow_cancel,
                "tow-complete": action_tow_complete,
                "leave": action_leave,
                "review": action_review,
            }
            fn = handlers.get(act)
            if fn is None:
                return self._error(404, "操作不存在")
            return self._send_json(fn(conn, user, body, event_id))

        except ApiError as e:
            self._error(e.status, e.message)
        except Exception as e:  # noqa: BLE001
            self._error(500, f"服务器错误: {e}")
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def _do_login(self, conn, body):
        username = (body.get("username") or "").strip()
        password = body.get("password") or ""
        row = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        if row is None or row["password_sha"] != sha256(password):
            return self._error(401, "账号或密码错误")
        token = secrets.token_hex(24)
        conn.execute("INSERT INTO sessions(token,user_id,created_at) VALUES(?,?,?)",
                     (token, row["id"], now()))
        return self._send_json(
            {"message": "登录成功", "role": row["role"],
             "display_name": row["display_name"]},
            extra_headers=[("Set-Cookie",
                            f"fl_token={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age=28800")])


def make_server(host="127.0.0.1", port=8000):
    init_db()
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    return httpd


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    httpd = make_server(args.host, args.port)
    print(f"消防通道临停管控系统已启动： http://{args.host}:{args.port}")
    print("演示账号：zhang/li（住户） guard1/guard2/guard3（保安） sup（负责人），密码 123456")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
