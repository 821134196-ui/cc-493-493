"""端到端测试:真实 HTTP 服务器(临时端口 + 临时 SQLite/照片目录)。

覆盖需求点:
- 重复上报合并(同一占用事件不重复开单)
- 车辆驶离后二次驶入允许开新单
- 跨楼栋可见范围(保安仅本栋详情;地图仅红色提示)
- 住户只看自己车辆;封闭事件不可改写
- 复核人分离(驶离登记人不能复核)
- 并发结案(只有一名复核人成功)
- 联系失败 / 车牌识别错误更正 / 紧急救援占道 / 拖移取消留痕
- 升级拖移必须值班负责人且显式确认
"""
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor

from app.seed import seed
from app.server import create_server

BASE = None


def call(method, path, token=None, body=None):
    url = BASE + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def _token(username, pw):
    """登录并从 Set-Cookie 解析会话 token。"""
    req = urllib.request.Request(
        BASE + "/api/login",
        data=json.dumps({"username": username, "password": pw}).encode(), method="POST")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=10) as r:
        r.read()
        cookie = r.headers["Set-Cookie"]
    return cookie.split("flsid=", 1)[1].split(";", 1)[0]


def login(username, pw):
    """返回 (user_id, session_token)。"""
    _, d = call("POST", "/api/login", body={"username": username, "password": pw})
    return d["user"]["id"], _token(username, pw)


class ApiCase(unittest.TestCase):
    svc = None

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="fl-test-")
        db_path = os.path.join(cls.tmp, "t.db")
        photo_dir = os.path.join(cls.tmp, "photos")
        cls.svc = seed(db_path, photo_dir, verbose=False)
        cls.httpd, _ = create_server("127.0.0.1", 0, db_path, photo_dir)
        cls.port = cls.httpd.server_address[1]
        global BASE
        BASE = f"http://127.0.0.1:{cls.port}"
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        # 账号 token
        cls.T = {}
        for u, p in [("admin", "admin123"), ("guard1", "guard123"), ("guard4", "guard123"),
                     ("guard2", "guard123"), ("guard3", "guard123"),
                     ("resident1", "resident123"), ("resident2", "resident123"),
                     ("resident3", "resident123")]:
            cls.T[u] = _token(u, p)

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    # ---------- 认证 / 权限 ----------
    def test_01_requires_login(self):
        code, _ = call("GET", "/api/events")
        self.assertEqual(code, 401)

    def test_02_bad_password(self):
        code, d = call("POST", "/api/login", body={"username": "guard1", "password": "x"})
        self.assertEqual(code, 401)

    # ---------- 重复上报合并 ----------
    def test_03_duplicate_reports_merge(self):
        # 新事件
        code, d = call("POST", "/api/events", self.T["guard1"],
                       {"plate": "京A12345", "zone_id": 2})
        # 京A12345 已有进行中事件(#2 在 1栋北),同车牌再报必须合并
        self.assertEqual(code, 200)
        self.assertTrue(d["merged"], "同车牌进行中事件应合并而非新开")
        eid = d["event"]["id"]
        code, d2 = call("POST", "/api/events", self.T["guard4"],
                        {"plate": "京A12345", "zone_id": 2})
        self.assertTrue(d2["merged"])
        self.assertEqual(d2["event"]["id"], eid)
        _, detail = call("GET", f"/api/events/{eid}", self.T["guard1"])
        self.assertGreaterEqual(detail["duplicate_count"], 1)
        kinds = [t["kind"] for t in detail["timeline"]]
        self.assertIn("duplicate_report", kinds)

    def test_04_second_entry_after_departure_is_new_case(self):
        # 走完一张单:上报 -> 接单 -> 驶离 -> 另一保安复核
        code, d = call("POST", "/api/events", self.T["guard1"],
                       {"plate": "京B88888", "zone_id": 2})
        # 京B88888 已有 departed 事件(#5),驶离后二次驶入应允许新单
        self.assertFalse(d["merged"], msg=str(d))
        eid = d["event"]["id"]
        self.assertEqual(code, 201)
        call("POST", f"/api/events/{eid}/accept", self.T["guard1"])
        call("POST", f"/api/events/{eid}/depart", self.T["guard1"],
             {"reason": "self_drive", "note": "二次驶入演示,车主驶离"})
        code, _ = call("POST", f"/api/events/{eid}/review", self.T["guard4"],
                       {"note": "通道恢复"})
        self.assertEqual(code, 200)
        # 结案后再次驶入 = 第三张单
        code, d3 = call("POST", "/api/events", self.T["guard1"],
                        {"plate": "京B88888", "zone_id": 2})
        self.assertEqual(code, 201)
        self.assertFalse(d3["merged"])

    # ---------- 跨楼栋可见范围 ----------
    def test_05_cross_building_scope(self):
        # guard1(1栋)不能看 2栋事件 #3 的详情
        code, _ = call("GET", "/api/events/3", self.T["guard1"])
        self.assertEqual(code, 404)
        # guard2(2栋)可以
        code, d = call("GET", "/api/events/3", self.T["guard2"])
        self.assertEqual(code, 200)
        self.assertEqual(d["plate"], "沪A2F68T")
        # 但 guard1 的地图上能看到红色阻断提示(redacted),且拿不到车牌
        _, mp = call("GET", "/api/map", self.T["guard1"])
        redacted = [e for e in mp["events"] if e.get("redacted")]
        self.assertTrue(redacted)
        self.assertNotIn("plate", redacted[0])
        # guard1 不能操作别栋事件
        code, _ = call("POST", "/api/events/3/accept", self.T["guard1"])
        self.assertEqual(code, 404)
        # 负责人全楼可见
        code, _ = call("GET", "/api/events/3", self.T["admin"])
        self.assertEqual(code, 200)

    def test_06_resident_sees_only_own(self):
        _, d = call("GET", "/api/events", self.T["resident1"])
        plates = {e["plate"] for e in d["events"]}
        self.assertEqual(plates, {"京A12345", "京B88888"})
        # 不能看别人的车
        code, _ = call("GET", "/api/events/3", self.T["resident1"])
        self.assertEqual(code, 404)
        # 任何写操作被拒
        code, _ = call("POST", "/api/events/2/accept", self.T["resident1"])
        self.assertEqual(code, 403)
        code, _ = call("POST", "/api/events", self.T["resident1"],
                       {"plate": "京A12345", "zone_id": 1})
        self.assertEqual(code, 403)

    def test_07_closed_event_is_immutable(self):
        # #1 是已结案历史事件
        code, d = call("POST", "/api/events/1/contact", self.T["guard1"],
                       {"channel": "sms", "result": "reached"})
        self.assertEqual(code, 409)
        code, _ = call("POST", "/api/events/1/accept", self.T["guard1"])
        self.assertEqual(code, 409)
        # 住户同样不能改写
        code, _ = call("POST", "/api/events/1/review", self.T["resident1"])
        self.assertIn(code, (403, 409))

    # ---------- 复核人分离 ----------
    def test_08_reviewer_must_differ_from_departure(self):
        _, d = call("POST", "/api/events", self.T["guard1"],
                    {"plate": "京A12345", "zone_id": 2})
        # 京A12345 此刻可能合并到已有单;构造一个干净车牌:用 guard2 的区域与苏牌
        code, d = call("POST", "/api/events", self.T["guard2"],
                       {"plate": "苏A00001", "zone_id": 4})
        self.assertEqual(code, 201, msg=str(d))
        eid = d["event"]["id"]
        call("POST", f"/api/events/{eid}/accept", self.T["guard2"])
        call("POST", f"/api/events/{eid}/depart", self.T["guard2"],
             {"reason": "self_drive"})
        # guard2 自己复核 -> 拒绝
        code, d = call("POST", f"/api/events/{eid}/review", self.T["guard2"],
                       {"note": "self"})
        self.assertEqual(code, 409)
        self.assertIn("另一名", d["error"])
        # 无关住户看不到该事件(存在性隐藏为 404)
        code, _ = call("POST", f"/api/events/{eid}/review", self.T["resident3"])
        self.assertEqual(code, 404)
        # 与事件车辆有关的住户能看到但无权复核 → 403
        # 用 resident2 自己的车牌 #3 走到 departed 再验证
        call("POST", "/api/events/3/depart", self.T["guard2"], {"reason": "self_drive"})
        code, _ = call("POST", "/api/events/3/review", self.T["resident2"], {"note": "x"})
        self.assertEqual(code, 403)
        # 负责人(非驶离登记人)复核成功
        code, _ = call("POST", f"/api/events/{eid}/review", self.T["admin"],
                       {"note": "通道恢复"})
        self.assertEqual(code, 200)

    # ---------- 并发结案 ----------
    def test_09_concurrent_review_single_winner(self):
        _, d = call("POST", "/api/events", self.T["guard2"],
                    {"plate": "苏A00002", "zone_id": 4})
        eid = d["event"]["id"]
        call("POST", f"/api/events/{eid}/accept", self.T["guard2"])
        call("POST", f"/api/events/{eid}/depart", self.T["guard2"],
             {"reason": "self_drive"})

        # 两个独立会话的合格复核人(值班负责人)在同一刻提交,只允许一个赢家
        admin2 = _token("admin", "admin123")
        barrier = threading.Barrier(2)

        def review(tok):
            barrier.wait()
            return call("POST", f"/api/events/{eid}/review", tok, {"note": "并发结案"})

        with ThreadPoolExecutor(max_workers=2) as ex:
            r = list(ex.map(review, [self.T["admin"], admin2]))
        codes = sorted(c for c, _ in r)
        self.assertEqual(codes, [200, 409], msg=str(r))
        _, detail = call("GET", f"/api/events/{eid}", self.T["admin"])
        self.assertEqual(detail["status"], "closed")
        # 结案后任何复核都被拒绝
        code, _ = call("POST", f"/api/events/{eid}/review", admin2, {"note": "再次"})
        self.assertEqual(code, 409)

    # ---------- 升级拖移:负责人确认 ----------
    def test_10_escalation_requires_supervisor_and_confirm(self):
        # #4 已处于 tow_requested
        code, _ = call("POST", "/api/events/4/escalate", self.T["guard3"],
                       {"confirm": True})
        self.assertEqual(code, 403)
        code, _ = call("POST", "/api/events/4/escalate", self.T["admin"],
                       {"confirm": False})
        self.assertEqual(code, 400)
        code, d = call("POST", "/api/events/4/escalate", self.T["admin"],
                       {"confirm": True, "note": "超时且拒驶离"})
        self.assertEqual(code, 200, msg=str(d))
        self.assertEqual(d["event"]["status"], "escalated")
        # 拖移取消留痕,回到巡查中
        code, d = call("POST", "/api/events/4/tow-cancel", self.T["admin"],
                       {"reason": "车主到场承诺驶离"})
        self.assertEqual(code, 200)
        self.assertEqual(d["event"]["status"], "patrol")
        _, detail = call("GET", "/api/events/4", self.T["admin"])
        self.assertTrue(any(t["kind"] == "tow_cancelled" for t in detail["timeline"]))

    # ---------- 车牌识别错误更正 ----------
    def test_11_plate_correction_audited(self):
        _, d = call("POST", "/api/events", self.T["guard2"],
                    {"plate": "苏A00009", "zone_id": 6,
                     "lpr_raw": "苏A00008", "lpr_confidence": 0.61})
        eid = d["event"]["id"]
        call("POST", f"/api/events/{eid}/accept", self.T["guard2"])
        code, d = call("POST", f"/api/events/{eid}/correct-plate", self.T["guard2"],
                       {"plate": "苏A00010", "reason": "现场核对行驶证"})
        self.assertEqual(code, 200)
        self.assertEqual(d["event"]["plate"], "苏A00010")
        self.assertEqual(d["event"]["original_plate"], "苏A00009")
        _, detail = call("GET", f"/api/events/{eid}", self.T["guard2"])
        self.assertTrue(any(t["kind"] == "plate_corrected" for t in detail["timeline"]))
        # 非法车牌被拒
        code, _ = call("POST", f"/api/events/{eid}/correct-plate", self.T["guard2"],
                       {"plate": "BAD"})
        self.assertEqual(code, 400)
        # 清理:驶离+复核,避免污染合并测试
        call("POST", f"/api/events/{eid}/depart", self.T["guard2"],
             {"reason": "self_drive"})
        call("POST", f"/api/events/{eid}/review", self.T["admin"], {"note": "ok"})

    # ---------- 联系失败 / 短信模拟 ----------
    def test_12_contact_failure_audited(self):
        _, d = call("POST", "/api/events", self.T["guard2"],
                    {"plate": "苏A00020", "zone_id": 6})
        eid = d["event"]["id"]
        call("POST", f"/api/events/{eid}/accept", self.T["guard2"])
        # 苏牌车主无登记电话 → 短信失败留痕
        code, d = call("POST", f"/api/events/{eid}/contact", self.T["guard2"],
                       {"channel": "sms", "result": "reached", "send_sms": True})
        self.assertEqual(code, 200)
        self.assertEqual(d["contact"]["sms_status"], "failed")
        _, detail = call("GET", f"/api/events/{eid}", self.T["guard2"])
        self.assertEqual(detail["contacts"][0]["result"], "failed")
        # 网关整体故障模式
        call("POST", "/api/sim/config", self.T["admin"], {"sms_mode": "down"})
        code, d = call("POST", f"/api/events/3/contact", self.T["guard2"],
                       {"channel": "sms", "result": "reached", "send_sms": True})
        self.assertEqual(d["contact"]["sms_status"], "failed")
        call("POST", "/api/sim/config", self.T["admin"], {"sms_mode": "normal"})
        # 恢复后已登记车主短信成功
        code, d = call("POST", "/api/events/3/contact", self.T["guard2"],
                       {"channel": "sms", "result": "promised", "send_sms": True})
        self.assertEqual(d["contact"]["sms_status"], "sent")

    # ---------- 紧急救援占道 ----------
    def test_13_emergency_release(self):
        code, d = call("POST", "/api/events", self.T["guard3"],
                       {"plate": "川A00099", "zone_id": 7, "is_emergency": True})
        self.assertEqual(code, 201)
        eid = d["event"]["id"]
        call("POST", f"/api/events/{eid}/accept", self.T["guard3"])
        # 非紧急事件不能登记为救援放行(反向:用普通事件)
        _, d2 = call("POST", "/api/events", self.T["guard3"],
                     {"plate": "川A00098", "zone_id": 7})
        eid2 = d2["event"]["id"]
        call("POST", f"/api/events/{eid2}/accept", self.T["guard3"])
        code, _ = call("POST", f"/api/events/{eid2}/depart", self.T["guard3"],
                       {"reason": "emergency_release"})
        self.assertEqual(code, 409)
        # 紧急事件正常放行
        code, _ = call("POST", f"/api/events/{eid}/depart", self.T["guard3"],
                       {"reason": "emergency_release", "note": "120 离场"})
        self.assertEqual(code, 200)
        code, _ = call("POST", f"/api/events/{eid}/review", self.T["admin"],
                       {"note": "通道恢复"})
        self.assertEqual(code, 200)
        _, detail = call("GET", f"/api/events/{eid}", self.T["admin"])
        self.assertTrue(any(t["kind"] == "emergency_report" for t in detail["timeline"]))

    # ---------- 拖离必须先经升级 ----------
    def test_14_tow_requires_escalation(self):
        _, d = call("POST", "/api/events", self.T["guard3"],
                    {"plate": "川A00077", "zone_id": 8})
        eid = d["event"]["id"]
        call("POST", f"/api/events/{eid}/accept", self.T["guard3"])
        code, _ = call("POST", f"/api/events/{eid}/depart", self.T["guard3"],
                       {"reason": "towed"})
        self.assertEqual(code, 409)

    # ---------- 统计 ----------
    def test_15_stats(self):
        code, d = call("GET", "/api/stats?days=30", self.T["admin"])
        self.assertEqual(code, 200)
        self.assertGreaterEqual(d["total"], 8)
        self.assertIn("by_building", d)
        self.assertGreaterEqual(d["duplicate_merged"], 1)
        # 保安不能看统计接口?——允许读但前端不展示;这里仅断言接口稳定
        code, _ = call("GET", "/api/stats", self.T["guard1"])
        self.assertEqual(code, 200)

    # ---------- 识别模拟开关 ----------
    def test_16_lpr_sim_error_mode(self):
        call("POST", "/api/sim/config", self.T["admin"], {"lpr_mode": "error"})
        code, d = call("POST", "/api/sim/recognize", self.T["guard2"],
                       {"zone_id": 5, "plate_hint": "沪A2F68T"})
        self.assertEqual(code, 200)
        self.assertEqual(d["sim_mode"], "error")
        self.assertNotEqual(d["plate"], "沪A2F68T")
        call("POST", "/api/sim/config", self.T["admin"], {"lpr_mode": "normal"})
        _, d = call("POST", "/api/sim/recognize", self.T["guard2"],
                    {"zone_id": 5, "plate_hint": "沪A2F68T"})
        self.assertEqual(d["plate"], "沪A2F68T")
        # 非负责人不能改模拟配置
        code, _ = call("POST", "/api/sim/config", self.T["guard1"], {"lpr_mode": "error"})
        self.assertEqual(code, 403)


if __name__ == "__main__":
    unittest.main(verbosity=2)
