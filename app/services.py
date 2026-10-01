"""Domain rules: auth, event lifecycle, contacts, simulation, stats.

所有写操作在 BEGIN IMMEDIATE 中完成,关键状态迁移用
UPDATE ... WHERE status IN (...) 判定影响行数,保证并发下幂等/单赢。
"""
import hashlib
import hmac
import os
import re
import secrets
import statistics as _stats

from .db import Database, now_ms, BLOCKING_STATUSES

PLATE_RE = re.compile(r"^[京津沪渝冀豫云辽黑湘皖鲁新苏浙赣鄂桂甘晋蒙陕吉闽贵粤川青藏琼宁使][A-Z][0-9A-Z]{5,6}$")

ROLE_STAFF = ("guard", "supervisor")

# 状态迁移表
NEXT_STATUS = {
    "accept":  ("open", "patrol"),
    "request_tow": ("patrol", "tow_requested"),
    "escalate": ("tow_requested", "escalated"),
    "cancel_tow": ("tow_requested", "escalated"),  # 取消后回到巡查中
    "depart": None,  # 单独判定(多种在控状态 -> departed)
    "review":  ("departed", "closed"),
}


class ApiError(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code = code
        self.msg = msg


# ---------- 密码 / 会话 ----------

def hash_pw(password, salt=None):
    salt = salt or secrets.token_hex(8)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 120_000).hex()
    return salt, digest


def check_pw(password, salt, digest):
    return hmac.compare_digest(hashlib.pbkdf2_hmac(
        "sha256", password.encode(), salt.encode(), 120_000).hex(), digest)


# ---------- 服务 ----------

class Service:
    def __init__(self, db: Database, photo_dir):
        self.db = db
        self.photo_dir = photo_dir
        os.makedirs(photo_dir, exist_ok=True)

    # ===== 认证 =====
    def login(self, username, password):
        u = self.db.queryone("SELECT * FROM users WHERE username=?", (username,))
        if not u or not check_pw(password, u["pw_salt"], u["pw_hash"]):
            raise ApiError(401, "用户名或密码错误")
        token = secrets.token_urlsafe(24)
        self.db.execute(
            "INSERT INTO sessions(token,user_id,created_at) VALUES(?,?,?)",
            (token, u["id"], now_ms()),
        )
        self.db.commit()
        return token, self.user_dict(u)

    def logout(self, token):
        self.db.execute("DELETE FROM sessions WHERE token=?", (token,))
        self.db.commit()

    def current_user(self, token):
        if not token:
            return None
        row = self.db.queryone(
            "SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token=?",
            (token,),
        )
        return row

    @staticmethod
    def user_dict(u):
        return {
            "id": u["id"], "username": u["username"], "display_name": u["display_name"],
            "role": u["role"], "building_id": u["building_id"], "phone": u["phone"],
        }

    def require_user(self, token):
        u = self.current_user(token)
        if not u:
            raise ApiError(401, "未登录")
        return u

    def require_staff(self, token):
        u = self.require_user(token)
        if u["role"] not in ROLE_STAFF:
            raise ApiError(403, "仅物业人员可操作")
        return u

    def require_supervisor(self, token):
        u = self.require_user(token)
        if u["role"] != "supervisor":
            raise ApiError(403, "需要值班负责人权限")
        return u

    # ===== 地图 / 可见范围 =====
    def map_data(self, user):
        buildings = [dict(b) for b in self.db.query("SELECT * FROM buildings ORDER BY id")]
        zones = [dict(z) for z in self.db.query(
            "SELECT z.*, (SELECT COUNT(*) FROM events e WHERE e.zone_id=z.id "
            "AND e.status IN ('open','patrol','tow_requested','escalated')) AS blocking "
            "FROM zones z ORDER BY z.id")]
        plates = {r["plate"] for r in self.db.query(
            "SELECT plate FROM resident_vehicles WHERE resident_id=?", (user["id"],))}
        active = self.db.query(
            "SELECT e.* FROM events e WHERE e.status IN "
            "('open','patrol','tow_requested','escalated','departed') ORDER BY e.found_at")
        events = []
        for e in active:
            z = self.db.queryone("SELECT * FROM zones WHERE id=?", (e["zone_id"],))
            if not self._can_see_event(user, e, z, plates):
                # 跨范围:仅对物业人员提示"某通道正在阻断"(红色,无车牌等详情);
                # 住户与待复核事件均不出现在他人地图上
                if user["role"] in ROLE_STAFF and e["status"] in BLOCKING_STATUSES:
                    events.append({"id": e["id"], "zone_id": e["zone_id"], "status": e["status"],
                                   "is_emergency": e["is_emergency"], "redacted": True})
                continue
            events.append(self.event_dict(e))
        return {"buildings": buildings, "zones": zones, "events": events}

    def _can_see_event(self, user, e, z, own_plates=None):
        if user["role"] == "supervisor":
            return True
        if user["role"] == "guard":
            return z is not None and z["building_id"] == user["building_id"]
        if own_plates is None:
            own_plates = {r["plate"] for r in self.db.query(
                "SELECT plate FROM resident_vehicles WHERE resident_id=?", (user["id"],))}
        return e["plate"] in own_plates

    def _get_visible_event(self, user, eid):
        e = self.db.queryone("SELECT * FROM events WHERE id=?", (eid,))
        if not e:
            raise ApiError(404, "事件不存在")
        z = self.db.queryone("SELECT * FROM zones WHERE id=?", (e["zone_id"],))
        if not self._can_see_event(user, e, z):
            raise ApiError(404, "事件不存在")  # 不泄漏跨范围存在性
        return e, z

    # ===== 事件查询 =====
    def list_events(self, user, status_filter=None):
        if user["role"] == "resident":
            rows = self.db.query(
                "SELECT e.* FROM events e JOIN resident_vehicles v ON v.plate=e.plate "
                "WHERE v.resident_id=? ORDER BY e.found_at DESC", (user["id"],))
        elif user["role"] == "guard":
            rows = self.db.query(
                "SELECT e.* FROM events e JOIN zones z ON z.id=e.zone_id "
                "WHERE z.building_id=? ORDER BY e.found_at DESC", (user["building_id"],))
        else:
            rows = self.db.query("SELECT * FROM events ORDER BY found_at DESC")
        out = []
        for e in rows:
            if status_filter and e["status"] != status_filter:
                continue
            out.append(self.event_dict(e))
        return out

    def event_detail(self, user, eid):
        e, z = self._get_visible_event(user, eid)
        d = self.event_dict(e)
        d["zone"] = dict(z)
        tl = self.db.query("SELECT t.*, u.display_name AS actor_name FROM timeline t "
                           "LEFT JOIN users u ON u.id=t.actor_id WHERE event_id=? ORDER BY t.id", (eid,))
        d["timeline"] = [dict(r) for r in tl]
        cl = self.db.query("SELECT c.*, u.display_name AS actor_name FROM contact_logs c "
                           "LEFT JOIN users u ON u.id=c.actor_id WHERE event_id=? ORDER BY c.id", (eid,))
        d["contacts"] = [dict(r) for r in cl]
        ph = self.db.query("SELECT * FROM photos WHERE event_id=? ORDER BY id", (eid,))
        d["photos"] = [dict(r) for r in ph]
        return d

    def event_dict(self, e):
        z = self.db.queryone("SELECT name, building_id FROM zones WHERE id=?", (e["zone_id"],))
        bname = None
        if z:
            b = self.db.queryone("SELECT name FROM buildings WHERE id=?", (z["building_id"],))
            bname = b["name"] if b else None
        actor_ids = {e[k] for k in ("reported_by", "accepted_by", "tow_requested_by",
                    "escalated_by", "tow_cancelled_by", "departed_by", "reviewed_by") if e[k]}
        names = {}
        if actor_ids:
            q = ",".join("?" * len(actor_ids))
            for r in self.db.query(f"SELECT id,display_name FROM users WHERE id IN ({q})", tuple(actor_ids)):
                names[r["id"]] = r["display_name"]
        return {
            "id": e["id"], "plate": e["plate"], "original_plate": e["original_plate"],
            "zone_id": e["zone_id"], "zone_name": z["name"] if z else None,
            "building_name": bname,
            "status": e["status"], "is_emergency": bool(e["is_emergency"]),
            "duplicate_count": e["duplicate_count"],
            "found_at": e["found_at"], "created_at": e["created_at"],
            "accepted_by": e["accepted_by"], "accepted_at": e["accepted_at"],
            "tow_requested_by": e["tow_requested_by"], "tow_requested_at": e["tow_requested_at"],
            "tow_reason": e["tow_reason"],
            "escalated_by": e["escalated_by"], "escalated_at": e["escalated_at"],
            "escalation_note": e["escalation_note"],
            "tow_cancelled_by": e["tow_cancelled_by"], "tow_cancelled_at": e["tow_cancelled_at"],
            "tow_cancel_reason": e["tow_cancel_reason"],
            "departed_by": e["departed_by"], "departed_at": e["departed_at"],
            "departure_reason": e["departure_reason"], "departure_note": e["departure_note"],
            "reviewed_by": e["reviewed_by"], "reviewed_at": e["reviewed_at"],
            "review_note": e["review_note"], "closed_at": e["closed_at"],
            "lpr_raw": e["lpr_raw"], "lpr_confidence": e["lpr_confidence"],
            "reported_by_name": names.get(e["reported_by"]),
            "accepted_by_name": names.get(e["accepted_by"]),
            "tow_requester_name": names.get(e["tow_requested_by"]),
            "escalated_by_name": names.get(e["escalated_by"]),
            "tow_cancelled_by_name": names.get(e["tow_cancelled_by"]),
            "departed_by_name": names.get(e["departed_by"]),
            "reviewed_by_name": names.get(e["reviewed_by"]),
        }

    # ===== 内部工具 =====
    def _log(self, eid, kind, actor_id, detail=None, ts=None):
        self.db.execute(
            "INSERT INTO timeline(event_id,kind,actor_id,detail,created_at) VALUES(?,?,?,?,?)",
            (eid, kind, actor_id, detail, ts or now_ms()),
        )

    def _staff_event_for_write(self, user, eid):
        """取事件并校验:物业人员 + 所属范围 + 事件未结案(封闭事件任何人不可改写)。"""
        if user["role"] not in ROLE_STAFF:
            raise ApiError(403, "仅物业人员可操作")
        e, _z = self._get_visible_event(user, eid)
        if e["status"] == "closed":
            raise ApiError(409, "事件已结案封闭,不能再修改")
        return e

    def _transition(self, eid, action, extra_set="", extra_args=()):
        """按状态迁移表执行条件 UPDATE;状态不符时抛 409。"""
        if action == "depart":
            allowed, target = BLOCKING_STATUSES, "departed"
        else:
            src, target = NEXT_STATUS[action]
            allowed = src if isinstance(src, tuple) else (src,)
        sets = "status=? " + extra_set
        args = (target, *extra_args, eid)
        placeholders = ",".join("?" * len(allowed))
        cur = self.db.execute(
            f"UPDATE events SET {sets} WHERE id=? AND status IN ({placeholders})",
            (*args, *allowed),
        )
        if cur.rowcount == 0:
            cur_e = self.db.queryone("SELECT status FROM events WHERE id=?", (eid,))
            got = cur_e["status"] if cur_e else "?"
            raise ApiError(409, f"当前状态「{STATUS_CN.get(got, got)}」不允许该操作")

    def _notify_plate_owners(self, plate, eid, text):
        owners = self.db.query(
            "SELECT id FROM resident_vehicles v JOIN users u ON u.id=v.resident_id "
            "WHERE v.plate=?", (plate,))
        for r in owners:  # 跨楼栋同样送达
            self.db.execute(
                "INSERT INTO notifications(resident_id,event_id,text,created_at) VALUES(?,?,?,?)",
                (r["id"], eid, text, now_ms()))

    # ===== 上报 / 合并 =====
    def report_event(self, user, plate, zone_id, photo_b64=None, is_emergency=False,
                     lpr_raw=None, lpr_confidence=None, found_at=None):
        if user["role"] not in ROLE_STAFF:
            raise ApiError(403, "仅物业人员可上报")
        plate = (plate or "").strip().upper().replace(" ", "")
        if not PLATE_RE.match(plate):
            raise ApiError(400, "车牌格式不正确")
        zone = self.db.queryone("SELECT * FROM zones WHERE id=?", (zone_id,))
        if not zone:
            raise ApiError(404, "占用区域不存在")
        if user["role"] == "guard" and zone["building_id"] != user["building_id"]:
            raise ApiError(403, "不能上报其他楼栋的区域")

        ts = found_at or now_ms()
        self.db.begin()
        try:
            existing = self.db.queryone(
                "SELECT * FROM events WHERE plate=? AND status IN "
                "('open','patrol','tow_requested','escalated')", (plate,))
            if existing:
                # 同一车辆同一占用事件:合并,绝不重复开单
                self.db.execute(
                    "UPDATE events SET duplicate_count=duplicate_count+1 WHERE id=?",
                    (existing["id"],))
                photo_id = None
                if photo_b64:
                    photo_id = self._save_photo(existing["id"], "report", photo_b64, user["id"])
                detail = f"重复上报自动合并(#{existing['duplicate_count'] + 1} 次发现)"
                if photo_id:
                    detail += f",照片#{photo_id}"
                self._log(existing["id"], "duplicate_report", user["id"], detail)
                self.db.commit()
                e = self.db.queryone("SELECT * FROM events WHERE id=?", (existing["id"],))
                return self.event_dict(e), True

            cur = self.db.execute(
                "INSERT INTO events(plate,original_plate,zone_id,status,is_emergency,"
                "found_at,reported_by,lpr_raw,lpr_confidence,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (plate, plate, zone_id, "open", 1 if is_emergency else 0,
                 ts, user["id"], lpr_raw, lpr_confidence, now_ms()),
            )
            eid = cur.lastrowid
            if photo_b64:
                self._save_photo(eid, "report", photo_b64, user["id"])
            kind = "emergency_report" if is_emergency else "reported"
            self._log(eid, kind, user["id"],
                      f"在 {zone['name']} 发现占道车辆 {plate}"
                      + (f";识别原始值 {lpr_raw}({lpr_confidence:.0%})" if lpr_raw and lpr_raw != plate else ""),
                      ts=ts)
            self._notify_plate_owners(
                plate, eid,
                f"【消防通道占道通知】您的车辆 {plate} 被发现停放在{zone['name']},请立即驶离。")
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        e = self.db.queryone("SELECT * FROM events WHERE id=?", (eid,))
        return self.event_dict(e), False

    # ===== 接单巡查 =====
    def accept(self, user, eid):
        e = self._staff_event_for_write(user, eid)
        self.db.begin()
        try:
            self._transition(eid, "accept", ",accepted_by=?,accepted_at=?",
                             (user["id"], now_ms()))
            self._log(eid, "accepted", user["id"], "保安接单并前往巡查")
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        return self.event_dict(self.db.queryone("SELECT * FROM events WHERE id=?", (eid,)))

    # ===== 联系记录(短信可控模拟,失败留痕) =====
    def add_contact(self, user, eid, channel, result, detail="", send_sms=False):
        e = self._staff_event_for_write(user, eid)
        if channel not in ("sms", "phone", "onsite", "notice"):
            raise ApiError(400, "联系渠道非法")
        if result not in ("reached", "promised", "no_answer", "wrong_number", "refused", "failed"):
            raise ApiError(400, "联系结果非法")
        sms_status = None
        sms_fail_reason = None
        if channel == "sms" or send_sms:
            phone, why = self._owner_phone(e["plate"])
            if not phone:
                sms_status, sms_fail_reason = "failed", why or "车主无登记联系方式"
                # 记录本身就是短信尝试时,结果记为发送失败;附带发送不覆盖电话/现场结果
                if channel == "sms":
                    result = "failed"
            else:
                mode = self.db.get_kv("sms_mode", "normal")
                if mode == "down" or self.db.get_kv(f"sms_fail:{phone}", "0") == "1":
                    sms_status, sms_fail_reason = "failed", \
                        "短信网关投递失败(模拟)" if mode == "down" else "该号码投递失败(模拟)"
                    if channel == "sms" and result == "reached":
                        result = "failed"
                else:
                    sms_status = "sent"
            text = f"【消防通道提醒】您的车辆 {e['plate']} 正占用消防通道,请立即挪车。事件#{eid}"
            self.db.execute(
                "INSERT INTO sms_outbox(phone,plate,event_id,text,status,fail_reason,created_at)"
                " VALUES(?,?,?,?,?,?,?)",
                (phone or "-", e["plate"], eid, text, sms_status, sms_fail_reason, now_ms()))
        self.db.begin()
        try:
            cur = self.db.execute(
                "INSERT INTO contact_logs(event_id,channel,result,detail,actor_id,created_at)"
                " VALUES(?,?,?,?,?,?)",
                (eid, channel, result, detail, user["id"], now_ms()))
            cid = cur.lastrowid
            self._log(eid, "contact", user["id"],
                      f"联系记录#{cid}:{CHANNEL_CN.get(channel, channel)}/{RESULT_CN.get(result, result)}"
                      + (f" — {detail}" if detail else "")
                      + (f"[短信{sms_fail_reason or '已发送'}]" if sms_status else ""))
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        return {"id": cid, "sms_status": sms_status, "sms_fail_reason": sms_fail_reason}

    def _owner_phone(self, plate):
        r = self.db.queryone(
            "SELECT u.phone FROM resident_vehicles v JOIN users u ON u.id=v.resident_id "
            "WHERE v.plate=? AND u.phone IS NOT NULL AND u.phone!=''", (plate,))
        if r:
            return r["phone"], None
        return None, "车主无登记联系方式"

    # ===== 车牌识别错误更正(留痕) =====
    def correct_plate(self, user, eid, new_plate, reason=""):
        e = self._staff_event_for_write(user, eid)
        new_plate = (new_plate or "").strip().upper().replace(" ", "")
        if not PLATE_RE.match(new_plate):
            raise ApiError(400, "车牌格式不正确")
        if new_plate == e["plate"]:
            raise ApiError(400, "新车牌与当前车牌相同")
        self.db.begin()
        try:
            clash = self.db.queryone(
                "SELECT id FROM events WHERE plate=? AND status IN "
                "('open','patrol','tow_requested','escalated') AND id<>?",
                (new_plate, eid))
            if clash:
                raise ApiError(409, f"车牌 {new_plate} 已存在进行中的事件 # {clash['id']}".replace("# ", "#"))
            old = e["plate"]
            try:
                self.db.execute("UPDATE events SET plate=? WHERE id=?", (new_plate, eid))
            except Exception as exc:
                raise ApiError(409, f"车牌 {new_plate} 已有进行中的占用事件") from exc
            cur = self.db.execute(
                "INSERT INTO contact_logs(event_id,channel,result,detail,actor_id,created_at)"
                " VALUES(?,?,?,?,?,?)",
                (eid, "notice", "wrong_number",
                 f"车牌识别更正:{old} → {new_plate}。原因:{reason or '现场核对'}", user["id"], now_ms()))
            self._log(eid, "plate_corrected", user["id"],
                      f"车牌识别错误更正:{old} → {new_plate}({reason or '现场核对'})")
            self._notify_plate_owners(
                new_plate, eid,
                f"【消防通道占道通知】经现场核对,占道车辆车牌更正为 {new_plate},请立即驶离。")
            self.db.commit()
        except ApiError:
            self.db.rollback()
            raise
        except Exception:
            self.db.rollback()
            raise ApiError(409, f"车牌 {new_plate} 已有进行中的占用事件")
        return self.event_dict(self.db.queryone("SELECT * FROM events WHERE id=?", (eid,)))

    # ===== 拖移升级:值班负责人确认 =====
    def request_tow(self, user, eid, reason):
        self._staff_event_for_write(user, eid)
        if not reason or not reason.strip():
            raise ApiError(400, "请填写申请拖移理由")
        self.db.begin()
        try:
            self._transition(eid, "request_tow",
                             ",tow_requested_by=?,tow_requested_at=?,tow_reason=?",
                             (user["id"], now_ms(), reason.strip()))
            self._log(eid, "tow_requested", user["id"], f"申请升级拖移:{reason.strip()}")
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        return self.event_dict(self.db.queryone("SELECT * FROM events WHERE id=?", (eid,)))

    def escalate(self, user, eid, confirm, note=""):
        # 升级操作必须由值班负责人执行,且必须显式确认
        if user["role"] != "supervisor":
            raise ApiError(403, "升级拖移须由值班负责人决定")
        if not confirm:
            raise ApiError(400, "升级操作需要值班负责人显式确认(confirm=true)")
        self._staff_event_for_write(user, eid)
        self.db.begin()
        try:
            self._transition(eid, "escalate",
                             ",escalated_by=?,escalated_at=?,escalation_note=?",
                             (user["id"], now_ms(), note))
            self._log(eid, "escalated", user["id"],
                      f"值班负责人批准升级拖移" + (f":{note}" if note else ""))
            ee = self.db.queryone("SELECT * FROM events WHERE id=?", (eid,))
            self._notify_plate_owners(
                ee["plate"], eid,
                f"【紧急通知】车辆 {ee['plate']} 占用消防通道,已批准拖移,请立即到场处理。")
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        return self.event_dict(self.db.queryone("SELECT * FROM events WHERE id=?", (eid,)))

    def cancel_tow(self, user, eid, reason):
        if user["role"] != "supervisor":
            raise ApiError(403, "取消拖移须由值班负责人决定")
        if not reason or not reason.strip():
            raise ApiError(400, "请填写取消拖移原因")
        self._staff_event_for_write(user, eid)
        self.db.begin()
        try:
            # tow_requested / escalated -> patrol(继续巡查)
            cur = self.db.execute(
                "UPDATE events SET status='patrol',tow_cancelled_by=?,tow_cancelled_at=?,"
                "tow_cancel_reason=? WHERE id=? AND status IN ('tow_requested','escalated')",
                (user["id"], now_ms(), reason.strip(), eid))
            if cur.rowcount == 0:
                raise ApiError(409, "当前状态不允许取消拖移")
            self._log(eid, "tow_cancelled", user["id"], f"拖移取消:{reason.strip()}")
            self.db.commit()
        except ApiError:
            self.db.rollback()
            raise
        except Exception:
            self.db.rollback()
            raise
        return self.event_dict(self.db.queryone("SELECT * FROM events WHERE id=?", (eid,)))

    # ===== 驶离(车主自行驶离 / 拖离 / 紧急救援放行) =====
    def depart(self, user, eid, reason, note="", photo_b64=None):
        if reason not in ("self_drive", "towed", "emergency_release"):
            raise ApiError(400, "驶离原因非法")
        e = self._staff_event_for_write(user, eid)
        if reason == "towed" and e["status"] != "escalated":
            raise ApiError(409, "未经值班负责人批准升级,不能登记为拖离")
        if reason == "emergency_release" and not e["is_emergency"]:
            raise ApiError(409, "仅紧急救援占道事件可登记为救援放行")
        self.db.begin()
        try:
            cur = self.db.execute(
                "UPDATE events SET status='departed',departed_by=?,departed_at=?,"
                "departure_reason=?,departure_note=? WHERE id=? AND status IN "
                "('open','patrol','tow_requested','escalated')",
                (user["id"], now_ms(), reason, note, eid))
            if cur.rowcount == 0:
                raise ApiError(409, "当前状态不允许登记驶离")
            if photo_b64:
                self._save_photo(eid, "departure", photo_b64, user["id"])
            self._log(eid, "departed", user["id"],
                      f"车辆离开:{DEPART_CN[reason]}" + (f" — {note}" if note else ""))
            self.db.commit()
        except ApiError:
            self.db.rollback()
            raise
        except Exception:
            self.db.rollback()
            raise
        return self.event_dict(self.db.queryone("SELECT * FROM events WHERE id=?", (eid,)))

    # ===== 复核结案:必须由另一名保安/负责人完成 =====
    def review(self, user, eid, note="", photo_b64=None):
        e, _z = self._get_visible_event(user, eid)
        if user["role"] not in ROLE_STAFF:
            raise ApiError(403, "仅物业人员可复核")
        if e["status"] == "closed":
            raise ApiError(409, "事件已结案,请勿重复复核")
        if e["status"] != "departed":
            raise ApiError(409, "车辆尚未登记驶离,不能复核结案")
        if e["departed_by"] == user["id"]:
            raise ApiError(409, "驶离登记人与复核人不能为同一人,请由另一名保安复核")
        self.db.begin()
        try:
            # 条件更新是并发结案的唯一赢家判定
            cur = self.db.execute(
                "UPDATE events SET status='closed',reviewed_by=?,reviewed_at=?,"
                "review_note=?,closed_at=? WHERE id=? AND status='departed' AND departed_by<>?",
                (user["id"], now_ms(), note, now_ms(), eid, user["id"]))
            if cur.rowcount == 0:
                # 区分:被并发结案 / 驶离人变更
                fresh = self.db.queryone("SELECT * FROM events WHERE id=?", (eid,))
                if fresh["status"] == "closed":
                    raise ApiError(409, "事件已被其他保安复核结案(并发冲突)")
                raise ApiError(409, "复核条件已变化,请刷新后重试")
            if photo_b64:
                self._save_photo(eid, "review", photo_b64, user["id"])
            self._log(eid, "reviewed", user["id"],
                      f"通道恢复复核通过并结案" + (f":{note}" if note else ""))
            self._notify_plate_owners(
                e["plate"], eid,
                f"【结案通知】占用消防通道的车辆 {e['plate']} 已驶离,通道恢复通行。")
            self.db.commit()
        except ApiError:
            self.db.rollback()
            raise
        except Exception:
            self.db.rollback()
            raise
        return self.event_dict(self.db.queryone("SELECT * FROM events WHERE id=?", (eid,)))

    # ===== 照片 =====
    def _save_photo(self, eid, kind, b64, user_id):
        import base64
        if "," in b64 and b64.startswith("data:"):
            header, b64 = b64.split(",", 1)
            declared = header.split(";")[0]
        else:
            declared = ""
        raw = base64.b64decode(b64)
        if len(raw) > 8 * 1024 * 1024:
            raise ApiError(413, "照片不能超过 8MB")
        # 以魔数判定真实类型,不信客户端声明
        sig = raw[:4]
        if raw[:3] == b"\xff\xd8\xff":
            ext, ctype = "jpg", "image/jpeg"
        elif raw[:8] == b"\x89PNG\r\n\x1a\n":
            ext, ctype = "png", "image/png"
        elif raw[:6] in (b"GIF87a", b"GIF89a"):
            ext, ctype = "gif", "image/gif"
        else:
            raise ApiError(400, "仅支持 JPEG/PNG/GIF 照片")
        fname = f"e{eid}_{kind}_{secrets.token_hex(6)}.{ext}"
        with open(os.path.join(self.photo_dir, fname), "wb") as f:
            f.write(raw)
        cur = self.db.execute(
            "INSERT INTO photos(event_id,kind,filename,content_type,uploaded_by,created_at)"
            " VALUES(?,?,?,?,?,?)",
            (eid, kind, fname, ctype, user_id, now_ms()))
        return cur.lastrowid

    # ===== 通知 =====
    def notifications(self, user):
        rows = self.db.query(
            "SELECT n.*, e.plate FROM notifications n JOIN events e ON e.id=n.event_id "
            "WHERE n.resident_id=? ORDER BY n.id DESC", (user["id"],))
        return [dict(r) for r in rows]

    def mark_read(self, user, nid):
        cur = self.db.execute(
            "UPDATE notifications SET read_at=? WHERE id=? AND resident_id=? AND read_at IS NULL",
            (now_ms(), nid, user["id"]))
        self.db.commit()
        return {"updated": cur.rowcount}

    # ===== 可控模拟:车牌识别 / 短信 =====
    def sim_config_get(self):
        return {
            "lpr_mode": self.db.get_kv("lpr_mode", "normal"),  # normal | error
            "sms_mode": self.db.get_kv("sms_mode", "normal"),  # normal | down
            "sms_fail_phones": [r["k"].split(":", 1)[1] for r in self.db.query(
                "SELECT k FROM sim_kv WHERE k LIKE 'sms_fail:%' AND v='1'")],
        }

    def sim_config_set(self, cfg):
        allowed = {"lpr_mode": ("normal", "error"), "sms_mode": ("normal", "down")}
        for k, vals in allowed.items():
            if k in cfg:
                if cfg[k] not in vals:
                    raise ApiError(400, f"{k} 取值非法")
                self.db.set_kv(k, cfg[k])
        if "sms_fail_phones" in cfg:
            phones = set(cfg["sms_fail_phones"])
            self.db.execute("DELETE FROM sim_kv WHERE k LIKE 'sms_fail:%'")
            for p in phones:
                self.db.set_kv(f"sms_fail:{p}", "1")
        self.db.commit()
        return self.sim_config_get()

    def recognize(self, zone_id, hint_plate=None):
        """模拟车牌识别:正常返回登记车辆车牌;error 模式故意认错一位用于演示更正留痕。"""
        zone = self.db.queryone("SELECT * FROM zones WHERE id=?", (zone_id,))
        if not zone:
            raise ApiError(404, "区域不存在")
        if hint_plate:
            target = hint_plate.strip().upper()
        else:
            r = self.db.queryone(
                "SELECT v.plate FROM resident_vehicles v JOIN users u ON u.id=v.resident_id "
                "JOIN zones z ON z.building_id=u.building_id WHERE z.id=? ORDER BY v.plate",
                (zone_id,))
            if not r:
                r = self.db.queryone("SELECT plate FROM resident_vehicles ORDER BY plate")
            if not r:
                raise ApiError(404, "模拟识别器没有可识别的车辆样本")
            target = r["plate"]
        mode = self.db.get_kv("lpr_mode", "normal")
        if mode == "error":
            chars = "0123456789ABCDEFGHJKLMNPQRSTUVWXYZ"
            idx = 2 + (zone_id * 3) % max(1, len(target) - 2)
            bad = chars[(chars.find(target[idx]) + 7) % len(chars)]
            raw = target[:idx] + bad + target[idx + 1:]
            return {"plate": raw, "raw": raw, "confidence": 0.62,
                    "sim_mode": "error", "hint_true_plate": target}
        return {"plate": target, "raw": target, "confidence": 0.96, "sim_mode": "normal"}

    # ===== 统计 =====
    def stats(self, days=30):
        days = max(1, min(int(days or 30), 366))
        cutoff = now_ms() - days * 86400_000
        total = self.db.queryone(
            "SELECT COUNT(*) c FROM events WHERE found_at>=?", (cutoff,))["c"]
        by_status = {r["status"]: r["c"] for r in self.db.query(
            "SELECT status,COUNT(*) c FROM events WHERE found_at>=? GROUP BY status", (cutoff,))}
        by_building = [dict(r) for r in self.db.query(
            "SELECT b.name building, COUNT(e.id) c FROM buildings b "
            "LEFT JOIN zones z ON z.building_id=b.id "
            "LEFT JOIN events e ON e.zone_id=z.id AND e.found_at>=? "
            "GROUP BY b.id ORDER BY b.id", (cutoff,))]
        durations = [r["d"] for r in self.db.query(
            "SELECT closed_at-found_at d FROM events WHERE status='closed' AND found_at>=?",
            (cutoff,))]
        contact_total = self.db.queryone(
            "SELECT COUNT(*) c FROM contact_logs WHERE created_at>=?", (cutoff,))["c"]
        contact_fail = self.db.queryone(
            "SELECT COUNT(*) c FROM contact_logs WHERE created_at>=? AND result IN ('failed','no_answer','wrong_number')",
            (cutoff,))["c"]
        towed = self.db.queryone(
            "SELECT COUNT(*) c FROM events WHERE departure_reason='towed' AND found_at>=?", (cutoff,))["c"]
        tow_cancel = self.db.queryone(
            "SELECT COUNT(*) c FROM events WHERE tow_cancelled_at IS NOT NULL AND found_at>=?", (cutoff,))["c"]
        corrected = self.db.queryone(
            "SELECT COUNT(*) c FROM timeline WHERE kind='plate_corrected' AND created_at>=?", (cutoff,))["c"]
        dup_merged = self.db.queryone(
            "SELECT COALESCE(SUM(duplicate_count),0) c FROM events WHERE found_at>=?", (cutoff,))["c"]
        daily = [dict(r) for r in self.db.query(
            "SELECT CAST((found_at-?)/86400000 AS INTEGER) AS day_offset, COUNT(*) c "
            "FROM events WHERE found_at>=? GROUP BY day_offset ORDER BY day_offset",
            (cutoff, cutoff))]
        return {
            "days": days, "total": total,
            "by_status": by_status,
            "by_building": by_building,
            "avg_duration_min": round((_stats.fmean(durations) / 60000), 1) if durations else None,
            "median_duration_min": round((_stats.median(durations) / 60000), 1) if durations else None,
            "contact_total": contact_total,
            "contact_fail": contact_fail,
            "contact_fail_rate": round(contact_fail / contact_total, 3) if contact_total else None,
            "towed": towed, "tow_cancelled": tow_cancel,
            "plate_corrected": corrected, "duplicate_merged": dup_merged,
            "daily": daily, "cutoff": cutoff,
        }


STATUS_CN = {
    "open": "待接单", "patrol": "巡查中", "tow_requested": "待升级审批",
    "escalated": "已批准拖移", "departed": "已驶离待复核", "closed": "已结案",
}
CHANNEL_CN = {"sms": "短信", "phone": "电话", "onsite": "现场", "notice": "书面/更正"}
RESULT_CN = {"reached": "已联系上", "promised": "承诺驶离", "no_answer": "无人接听",
             "wrong_number": "号码有误/识别错误", "refused": "拒绝驶离", "failed": "发送失败"}
DEPART_CN = {"self_drive": "车主自行驶离", "towed": "依法拖离", "emergency_release": "紧急救援结束放行"}
