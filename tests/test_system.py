"""端到端自动化测试（标准库 unittest，线程内启动真实 HTTP 服务）。

覆盖：
- 重复上报合并（同车同占用事件不反复开单）
- 跨楼栋可见范围（保安 403、住户只见本人车牌）
- 复核人分离（处置人不能复核自己）
- 并发结案（多线程同时复核，恰一成功）
- 联系失败 / 车牌识别错误更正 / 车辆二次驶入 / 紧急救援占道 / 拖移取消留痕
- 封闭事件不可改写、升级拖移须值班负责人确认、照片鉴权、模拟短信/LPR
"""
import base64
import http.cookiejar
import json
import os
os.environ["FIRELANE_DATA"] = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tmp_testdata")

import sqlite3
import threading
import unittest
import urllib.request
import urllib.error

import database
import server

PNG_1X1 = ("data:image/png;base64," + base64.b64encode(
    bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
        "890000000d49444154789c6360000002000100ffff03000006000557bfabdf00"
        "0000000049454e44ae426082"
    )).decode())


class Client:
    def __init__(self, base):
        self.base = base
        cj = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(cj))

    def call(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with self.opener.open(req, timeout=10) as r:
                return r.status, json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode())

    def login(self, u, p="123456"):
        return self.call("POST", "/api/login", {"username": u, "password": p})


class FireLaneTests(unittest.TestCase):
    httpd = None
    base = ""

    @classmethod
    def setUpClass(cls):
        cls.httpd = server.make_server("127.0.0.1", 0)
        port = cls.httpd.server_address[1]
        cls.base = f"http://127.0.0.1:{port}"
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    def setUp(self):
        # 每个用例前清空业务数据（保留账号/通道/楼栋种子）
        conn = sqlite3.connect(database.DB_PATH)
        for t in ("timeline", "contact_logs", "photos", "sms_outbox", "events"):
            conn.execute(f"DELETE FROM {t}")
        conn.execute("UPDATE settings SET value='normal' WHERE key='sms_mode'")
        conn.execute("UPDATE settings SET value='' WHERE key='lpr_next'")
        conn.commit()
        conn.close()
        self.g1 = Client(self.base); self.g1.login("guard1")   # 1号楼
        self.g2 = Client(self.base); self.g2.login("guard2")   # 1号楼（另一人）
        self.g3 = Client(self.base); self.g3.login("guard3")   # 2号楼
        self.sup = Client(self.base); self.sup.login("sup")
        self.zhang = Client(self.base); self.zhang.login("zhang")
        self.li = Client(self.base); self.li.login("li")

    def client(self):
        c = Client(self.base)
        return c

    def report(self, cl, channel=1, plate="京A12345", **kw):
        body = {"channel_id": channel, "plate_display": plate}
        body.update(kw)
        return cl.call("POST", "/api/events", body)

    def drive_to_review(self, cl, eid, plate="京A12345"):
        """接单 -> 驶离，使事件进入待复核。"""
        self.assertEqual(cl.call("POST", f"/api/events/{eid}/accept", {})[0], 200)
        st, _ = cl.call("POST", f"/api/events/{eid}/leave", {})
        self.assertEqual(st, 200)

    # ---------- 1. 重复上报合并 ----------
    def test_01_duplicate_reports_merge(self):
        st, r1 = self.report(self.g1, plate="京A·12345")
        self.assertEqual(st, 201)
        self.assertFalse(r1["merged"])
        eid = r1["id"]

        st, r2 = self.report(self.g1, plate="京A12345")  # 归一化后相同车牌
        self.assertEqual(st, 201)
        self.assertTrue(r2["merged"])
        self.assertEqual(r2["id"], eid)

        # 另一名保安重复上报也合并
        st, r3 = self.report(self.g2, plate="京A12345")
        self.assertTrue(r3["merged"])
        self.assertEqual(r3["id"], eid)

        st, det = self.g1.call("GET", f"/api/events/{eid}")
        self.assertEqual(st, 200)
        self.assertEqual(det["merged_count"], 2)
        types = [t["type"] for t in det["timeline"]]
        self.assertEqual(types.count("repeat_report"), 2)
        # 数据库中该车牌在该通道只有一张单
        conn = sqlite3.connect(database.DB_PATH)
        n = conn.execute(
            "SELECT COUNT(*) FROM events WHERE plate_norm='京A12345' AND channel_id=1").fetchone()[0]
        conn.close()
        self.assertEqual(n, 1)

    def test_01b_same_plate_different_channel_separate(self):
        _, r1 = self.report(self.g1, channel=1, plate="沪C9D8E7")
        _, r2 = self.report(self.g1, channel=2, plate="沪C9D8E7")
        self.assertNotEqual(r1["id"], r2["id"])

    # ---------- 2. 跨楼栋可见范围 ----------
    def test_02_cross_building_invisible(self):
        _, r = self.report(self.g1, plate="京A66666")
        eid = r["id"]
        # 2号楼保安看不到 1号楼事件
        st, _ = self.g3.call("GET", f"/api/events/{eid}")
        self.assertEqual(st, 403)
        # 不能接单/处置
        st, _ = self.g3.call("POST", f"/api/events/{eid}/accept", {})
        self.assertEqual(st, 403)
        # 不能在 1号楼通道开单
        st, _ = self.report(self.g3, channel=1, plate="京A66666")
        self.assertEqual(st, 403)
        # 2号楼住户看不到 1号楼住户的车牌事件
        st, data = self.li.call("GET", "/api/events")
        self.assertEqual(st, 200)
        self.assertNotIn(eid, [e["id"] for e in data["events"]])
        # 负责人全小区可见
        st, det = self.sup.call("GET", f"/api/events/{eid}")
        self.assertEqual(st, 200)

    def test_02b_resident_sees_own_plate_any_building(self):
        # zhang 的车在 2号楼通道（channel 3）被 2号楼保安上报
        _, r = self.report(self.g3, channel=3, plate="京A12345")
        st, data = self.zhang.call("GET", "/api/events")
        self.assertEqual(st, 200)
        self.assertIn(r["id"], [e["id"] for e in data["events"]])

    # ---------- 3. 复核人分离 ----------
    def test_03_reviewer_must_differ_from_handler(self):
        _, r = self.report(self.g1, plate="京A66666")
        eid = r["id"]
        self.drive_to_review(self.g1, eid, "京A66666")
        # 处置人 guard1 自己复核 -> 403
        st, body = self.g1.call("POST", f"/api/events/{eid}/review",
                                {"passed": True})
        self.assertEqual(st, 403)
        self.assertIn("不能为同一人", body["error"])
        # 事件仍是待复核
        st, det = self.g2.call("GET", f"/api/events/{eid}")
        self.assertEqual(det["status"], "pending_review")
        # 另一名保安 guard2 复核通过
        st, _ = self.g2.call("POST", f"/api/events/{eid}/review",
                             {"passed": True, "note": "通道恢复"})
        self.assertEqual(st, 200)
        st, det = self.g1.call("GET", f"/api/events/{eid}")
        self.assertEqual(det["status"], "closed")
        self.assertEqual(det["reviewer"], "陈静（1号楼保安）")
        self.assertEqual(det["handled_by"], "王强（1号楼保安）")

    # ---------- 4. 并发结案 ----------
    def test_04_concurrent_review_exactly_one_wins(self):
        _, r = self.report(self.g1, plate="津A55555")
        eid = r["id"]
        self.drive_to_review(self.g1, eid)

        results = []
        barrier = threading.Barrier(8)

        def worker():
            cl = Client(self.base)
            cl.login("guard2")  # 与处置人 guard1 不同，全部具备复核资格
            barrier.wait()
            st, body = cl.call("POST", f"/api/events/{eid}/review",
                               {"passed": True})
            results.append((st, body))

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        ok = [x for x in results if x[0] == 200]
        conflict = [x for x in results if x[0] == 409]
        self.assertEqual(len(ok), 1, f"应恰有一次结案成功，实际：{results}")
        self.assertEqual(len(conflict), 7)
        st, det = self.g1.call("GET", f"/api/events/{eid}")
        self.assertEqual(det["status"], "closed")

    # ---------- 封闭事件不可改写 ----------
    def test_05_closed_event_locked(self):
        _, r = self.report(self.g1, plate="津A55555")
        eid = r["id"]
        self.drive_to_review(self.g1, eid)
        self.g2.call("POST", f"/api/events/{eid}/review", {"passed": True})
        # 任何处置动作均被拒
        for action, body in [("accept", {}),
                             ("contact", {"method": "sms", "result": "delivered"}),
                             ("leave", {}),
                             ("review", {"passed": True})]:
            st, payload = self.g1.call("POST", f"/api/events/{eid}/{action}", body)
            self.assertEqual(st, 409, f"{action} 应被拒绝: {payload}")

    # ---------- 升级拖移须负责人确认 ----------
    def test_06_tow_escalation_requires_supervisor(self):
        _, r = self.report(self.g1, plate="冀F66688")
        eid = r["id"]
        self.g1.call("POST", f"/api/events/{eid}/accept", {})
        self.g1.call("POST", f"/api/events/{eid}/contact",
                     {"method": "phone", "result": "refused"})
        st, _ = self.g1.call("POST", f"/api/events/{eid}/tow-request",
                             {"reason": "拒不移车"})
        self.assertEqual(st, 200)
        # 保安不能自行批准
        st, _ = self.g1.call("POST", f"/api/events/{eid}/tow-approve", {})
        self.assertEqual(st, 403)
        st, _ = self.g2.call("POST", f"/api/events/{eid}/tow-approve", {})
        self.assertEqual(st, 403)
        # 住户更不行
        st, _ = self.zhang.call("POST", f"/api/events/{eid}/tow-approve", {})
        self.assertEqual(st, 403)
        # 负责人确认
        st, _ = self.sup.call("POST", f"/api/events/{eid}/tow-approve", {})
        self.assertEqual(st, 200)
        st, det = self.g1.call("GET", f"/api/events/{eid}")
        self.assertEqual(det["status"], "tow_approved")
        self.assertTrue(any(t["type"] == "tow_approved" for t in det["timeline"]))

    # ---------- 拖移取消留痕 ----------
    def test_07_tow_cancel_audited(self):
        _, r = self.report(self.g1, plate="冀F66688")
        eid = r["id"]
        self.g1.call("POST", f"/api/events/{eid}/accept", {})
        self.g1.call("POST", f"/api/events/{eid}/tow-request", {"reason": "x"})
        st, _ = self.g1.call("POST", f"/api/events/{eid}/tow-cancel",
                             {"reason": "车主赶到现场挪车"})
        self.assertEqual(st, 200)
        st, det = self.g1.call("GET", f"/api/events/{eid}")
        self.assertEqual(det["tow_status"], "cancelled")
        rec = [t for t in det["timeline"] if t["type"] == "tow_cancelled"]
        self.assertEqual(len(rec), 1)
        self.assertEqual(rec[0]["detail"]["reason"], "车主赶到现场挪车")

    # ---------- 联系失败留痕（模拟短信网关失败模式） ----------
    def test_08_contact_failure_audited(self):
        _, r = self.report(self.g1, plate="京A12345")
        eid = r["id"]
        self.g1.call("POST", f"/api/events/{eid}/accept", {})
        st, _ = self.sup.call("POST", "/api/sim/sms-mode", {"mode": "fail"})
        self.assertEqual(st, 200)
        st, body = self.g1.call("POST", f"/api/events/{eid}/contact",
                                {"method": "sms", "result": "delivered"})
        self.assertEqual(st, 200)
        self.assertEqual(body["gateway"], "failed")
        st, det = self.g1.call("GET", f"/api/events/{eid}")
        self.assertEqual(det["contacts"][-1]["result"], "failed")
        st, box = self.sup.call("GET", "/api/sms-outbox")
        self.assertEqual(st, 200)
        self.assertEqual(box["messages"][0]["status"], "failed")

    # ---------- 车牌识别错误更正 ----------
    def test_09_plate_correction_audited(self):
        _, r = self.report(self.g1, plate="京A1234O")  # 模拟把 5 识别成 O
        eid = r["id"]
        st, _ = self.g1.call("POST", f"/api/events/{eid}/correct-plate",
                             {"new_plate": "京A12345"})
        self.assertEqual(st, 200)
        st, det = self.g1.call("GET", f"/api/events/{eid}")
        self.assertEqual(det["plate"], "京A12345")
        rec = [t for t in det["timeline"] if t["type"] == "plate_corrected"]
        self.assertEqual(len(rec), 1)
        self.assertEqual(rec[0]["detail"]["old_plate"], "京A1234O")
        self.assertEqual(rec[0]["detail"]["new_plate"], "京A12345")

    # ---------- 车辆二次驶入 ----------
    def test_10_reentry_reopens_event(self):
        _, r = self.report(self.g1, plate="京A66666")
        eid = r["id"]
        self.drive_to_review(self.g1, eid)
        # 待复核期间车辆再次驶入同一通道：不新单，原事件重开
        st, r2 = self.report(self.g2, plate="京A66666")
        self.assertEqual(st, 201)
        self.assertTrue(r2["merged"])
        self.assertEqual(r2["id"], eid)
        st, det = self.g1.call("GET", f"/api/events/{eid}")
        self.assertEqual(det["status"], "accepted")
        self.assertIsNone(det["leave_at"])
        self.assertTrue(any(t["type"] == "reentry" for t in det["timeline"]))

    def test_10b_new_entry_after_close_creates_new_event(self):
        _, r = self.report(self.g1, plate="京A66666")
        eid = r["id"]
        self.drive_to_review(self.g1, eid)
        self.g2.call("POST", f"/api/events/{eid}/review", {"passed": True})
        # 结案后再次驶入 -> 新的占用事件、新单
        st, r2 = self.report(self.g1, plate="京A66666")
        self.assertEqual(st, 201)
        self.assertFalse(r2["merged"])
        self.assertNotEqual(r2["id"], eid)

    # ---------- 紧急救援占道 ----------
    def test_11_emergency_occupancy(self):
        _, r = self.report(self.g1, plate="粤B10086")
        eid = r["id"]
        self.g1.call("POST", f"/api/events/{eid}/accept", {})
        st, _ = self.g1.call("POST", f"/api/events/{eid}/emergency",
                             {"reason": "120 救护车临时停靠"})
        self.assertEqual(st, 200)
        st, det = self.g1.call("GET", f"/api/events/{eid}")
        self.assertEqual(det["kind"], "emergency")
        self.assertEqual(det["status"], "emergency")
        st, _ = self.g1.call("POST", f"/api/events/{eid}/leave", {})
        self.assertEqual(st, 200)
        st, det = self.g1.call("GET", f"/api/events/{eid}")
        self.assertEqual(det["resolution"], "emergency")

    def test_11b_emergency_at_report_time(self):
        st, r = self.report(self.g1, plate="粤B10086", kind="emergency",
                            note="消防车作业")
        self.assertEqual(st, 201)
        st, det = self.g1.call("GET", f"/api/events/{r['id']}")
        self.assertEqual(det["status"], "emergency")

    # ---------- 复核不通过：事件重开 ----------
    def test_12_review_failed_reopens(self):
        _, r = self.report(self.g1, plate="津A55555")
        eid = r["id"]
        self.drive_to_review(self.g1, eid)
        st, _ = self.g2.call("POST", f"/api/events/{eid}/review",
                             {"passed": False, "note": "车辆未完全驶离通道"})
        self.assertEqual(st, 200)
        st, det = self.g1.call("GET", f"/api/events/{eid}")
        self.assertEqual(det["status"], "accepted")
        self.assertEqual(det["handled_by"], "")

    # ---------- 照片：本地存储 + 按关系鉴权 ----------
    def test_13_photo_storage_and_auth(self):
        st, r = self.report(self.g1, plate="京A66666", photo=PNG_1X1)
        self.assertEqual(st, 201)
        eid = r["id"]
        st, det = self.g1.call("GET", f"/api/events/{eid}")
        self.assertEqual(st, 200)
        self.assertTrue(det["photos"])
        url = det["photos"][0]["url"]
        # 有权限者可取
        with self.g1.opener.open(self.base + url, timeout=10) as resp:
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.read()[:8], b"\x89PNG\r\n\x1a\n")
        # 跨楼栋保安 403
        try:
            self.g3.opener.open(self.base + url, timeout=10)
            self.fail("应拒绝跨楼栋访问照片")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 403)
        # 文件确实落在本地目录
        fname = url.rsplit("/", 1)[-1]
        self.assertTrue(os.path.isfile(os.path.join(database.UPLOAD_DIR, fname)))

    # ---------- 模拟 LPR 注入 ----------
    def test_14_lpr_injection(self):
        st, _ = self.sup.call("POST", "/api/sim/lpr", {"plate": "测A00001"})
        self.assertEqual(st, 200)
        st, r = self.g1.call("GET", "/api/lpr")
        self.assertEqual(st, 200)
        self.assertEqual(r["plate"], "测A00001")
        # 注入一次性消费，之后恢复随机模拟
        st, r2 = self.g1.call("GET", "/api/lpr")
        self.assertNotEqual(r2["plate"], "测A00001")

    # ---------- 权限与认证 ----------
    def test_15_auth_and_role_gates(self):
        c = Client(self.base)
        st, _ = c.call("GET", "/api/events")
        self.assertEqual(st, 401)
        self.assertEqual(c.login("nope", "x")[0], 401)
        # 住户不能处置
        st, _ = self.zhang.call("POST", "/api/events",
                                {"channel_id": 1, "plate_display": "京A12345"})
        self.assertEqual(st, 403)
        # 保安不能打开统计之外的审批；负责人统计可用
        st, s = self.sup.call("GET", "/api/stats")
        self.assertEqual(st, 200)
        self.assertIn("by_building", s)
        st, _ = self.zhang.call("GET", "/api/stats")
        self.assertEqual(st, 403)

    # ---------- 完整拖移结案链路 ----------
    def test_16_full_tow_flow(self):
        _, r = self.report(self.g1, plate="冀F66688", photo=PNG_1X1)
        eid = r["id"]
        self.g1.call("POST", f"/api/events/{eid}/accept", {})
        self.g1.call("POST", f"/api/events/{eid}/contact",
                     {"method": "sms", "result": "failed"})
        self.g1.call("POST", f"/api/events/{eid}/tow-request", {"reason": "联系失败"})
        self.sup.call("POST", f"/api/events/{eid}/tow-approve", {})
        st, _ = self.g1.call("POST", f"/api/events/{eid}/tow-complete",
                             {"photo": PNG_1X1})
        self.assertEqual(st, 200)
        st, det = self.g2.call("GET", f"/api/events/{eid}")
        self.assertEqual(det["status"], "pending_review")
        self.assertEqual(det["resolution"], "towed")
        st, _ = self.g2.call("POST", f"/api/events/{eid}/review",
                             {"passed": True, "photo": PNG_1X1})
        self.assertEqual(st, 200)
        st, det = self.g1.call("GET", f"/api/events/{eid}")
        self.assertEqual(det["status"], "closed")
        # 留痕完整、顺序正确
        seq = [t["type"] for t in det["timeline"]]
        self.assertEqual(seq, [
            "created", "accept", "contact", "tow_requested",
            "tow_approved", "towed", "review_passed"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
