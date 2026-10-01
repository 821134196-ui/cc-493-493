"""HTTP 入口:仅依赖标准库 http.server。

- /api/*   JSON 接口(cookie 会话)
- /photos/* 事件照片(需登录且对该事件有可见权限)
- 其他路径返回 static/ 静态文件
"""
import json
import os
import threading
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from .db import Database
from .services import Service, ApiError

STATIC_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static")

# 无需登录即可访问的接口
PUBLIC_PATHS = {"/api/login", "/api/health"}

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".ico": "image/x-icon",
    ".json": "application/json; charset=utf-8",
    ".webmanifest": "application/manifest+json",
}


def create_server(host, port, db_path, photo_dir):
    db = Database(db_path)
    svc = Service(db, photo_dir)

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            pass  # 静默;演示时可用 --verbose 打开

        def _send_json(self, obj, status=200, extra_headers=None):
            body = json.dumps(obj, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra_headers or []):
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self):
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0:
                return {}
            if n > 12 * 1024 * 1024:
                raise ApiError(413, "请求体过大")
            raw = self.rfile.read(n)
            try:
                data = json.loads(raw.decode())
            except Exception:
                raise ApiError(400, "请求不是合法 JSON")
            return data if isinstance(data, dict) else {}

        def _token(self):
            cookie = SimpleCookie(self.headers.get("Cookie", ""))
            if "flsid" in cookie:
                return cookie["flsid"].value
            auth = self.headers.get("Authorization", "")
            if auth.startswith("Bearer "):
                return auth[7:]
            return None

        def _user(self):
            return svc.current_user(self._token())

        def do_GET(self):
            self._route()

        def do_POST(self):
            self._route()

        def _route(self):
            parsed = urlparse(self.path)
            path = parsed.path
            try:
                if path.startswith("/api/"):
                    self._api(path)
                elif path.startswith("/photos/"):
                    self._photo(path)
                else:
                    self._static(path)
            except ApiError as e:
                self._send_json({"error": e.msg, "code": e.code}, e.code)
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as e:  # noqa: BLE001
                self._send_json({"error": f"服务器内部错误: {e}", "code": 500}, 500)

        # ---------- API ----------
        def _api(self, path):
            user = self._user()
            method = self.command
            if path not in PUBLIC_PATHS and not user:
                raise ApiError(401, "未登录或会话已过期")

            # 认证
            if path == "/api/health":
                return self._send_json({"ok": True})
            if path == "/api/login" and method == "POST":
                d = self._read_json()
                token, udict = svc.login((d.get("username") or "").strip(), d.get("password") or "")
                return self._send_json({"user": udict},
                                       extra_headers=[("Set-Cookie",
                                          f"flsid={token}; HttpOnly; SameSite=Lax; Path=/; Max-Age=86400")])
            if path == "/api/logout" and method == "POST":
                svc.logout(self._token())
                return self._send_json({"ok": True},
                                       extra_headers=[("Set-Cookie",
                                          "flsid=; HttpOnly; Path=/; Max-Age=0")])
            if path == "/api/me" and method == "GET":
                return self._send_json({"user": svc.user_dict(user)})

            if path == "/api/map" and method == "GET":
                return self._send_json(svc.map_data(user))
            if path == "/api/events" and method == "GET":
                q = urlparse(self.path).query
                status_filter = None
                for kv in q.split("&"):
                    if kv.startswith("status="):
                        status_filter = kv.split("=", 1)[1]
                return self._send_json({"events": svc.list_events(user, status_filter)})
            if path == "/api/events" and method == "POST":
                d = self._read_json()
                ev, merged = svc.report_event(
                    user, d.get("plate"), d.get("zone_id"),
                    photo_b64=d.get("photo"), is_emergency=bool(d.get("is_emergency")),
                    lpr_raw=d.get("lpr_raw"), lpr_confidence=d.get("lpr_confidence"))
                return self._send_json({"event": ev, "merged": merged},
                                       200 if merged else 201)

            # /api/events/<id>/<action>
            if path.startswith("/api/events/") and method == "POST":
                parts = path.strip("/").split("/")
                # api/events/<id>/<action>
                if len(parts) == 4:
                    eid = int(parts[2]); action = parts[3]
                    d = self._read_json()
                    return self._event_action(user, eid, action, d)
            if path.startswith("/api/events/") and method == "GET":
                parts = path.strip("/").split("/")
                if len(parts) == 3:
                    return self._send_json(svc.event_detail(user, int(parts[2])))

            if path == "/api/notifications" and method == "GET":
                return self._send_json({"notifications": svc.notifications(user)})
            if path.startswith("/api/notifications/") and method == "POST":
                parts = path.strip("/").split("/")
                if len(parts) == 4 and parts[3] == "read":
                    return self._send_json(svc.mark_read(user, int(parts[2])))

            if path == "/api/stats" and method == "GET":
                q = urlparse(self.path).query
                days = 30
                for kv in q.split("&"):
                    if kv.startswith("days="):
                        days = int(kv.split("=", 1)[1])
                return self._send_json(svc.stats(days))

            if path == "/api/sim/config" and method == "GET":
                if user["role"] != "supervisor":
                    raise ApiError(403, "仅值班负责人可查看模拟配置")
                return self._send_json(svc.sim_config_get())
            if path == "/api/sim/config" and method == "POST":
                if user["role"] != "supervisor":
                    raise ApiError(403, "仅值班负责人可修改模拟配置")
                return self._send_json(svc.sim_config_set(self._read_json()))
            if path == "/api/sim/recognize" and method == "POST":
                d = self._read_json()
                return self._send_json(svc.recognize(d.get("zone_id"), d.get("plate_hint")))

            raise ApiError(404, "接口不存在")

        def _event_action(self, user, eid, action, d):
            if action == "accept":
                return self._send_json({"event": svc.accept(user, eid)})
            if action == "contact":
                return self._send_json({"contact": svc.add_contact(
                    user, eid, d.get("channel"), d.get("result"),
                    d.get("detail", ""), bool(d.get("send_sms")))})
            if action == "correct-plate":
                return self._send_json({"event": svc.correct_plate(
                    user, eid, d.get("plate"), d.get("reason", ""))})
            if action == "tow-request":
                return self._send_json({"event": svc.request_tow(user, eid, d.get("reason", ""))})
            if action == "escalate":
                return self._send_json({"event": svc.escalate(
                    user, eid, bool(d.get("confirm")), d.get("note", ""))})
            if action == "tow-cancel":
                return self._send_json({"event": svc.cancel_tow(user, eid, d.get("reason", ""))})
            if action == "depart":
                return self._send_json({"event": svc.depart(
                    user, eid, d.get("reason"), d.get("note", ""), d.get("photo"))})
            if action == "review":
                return self._send_json({"event": svc.review(
                    user, eid, d.get("note", ""), d.get("photo"))})
            raise ApiError(404, "操作不存在")

        # ---------- 照片 ----------
        def _photo(self, path):
            user = self._user()
            if not user:
                raise ApiError(401, "未登录")
            name = os.path.basename(path)
            if name != path.rsplit("/", 1)[-1] or ".." in path:
                raise ApiError(400, "非法路径")
            row = svc.db.queryone("SELECT * FROM photos WHERE filename=?", (name,))
            if not row:
                raise ApiError(404, "照片不存在")
            e = svc.db.queryone("SELECT * FROM events WHERE id=?", (row["event_id"],))
            z = svc.db.queryone("SELECT * FROM zones WHERE id=?", (e["zone_id"],))
            if not svc._can_see_event(user, e, z):
                raise ApiError(403, "无权查看该照片")
            fpath = os.path.join(svc.photo_dir, name)
            if not os.path.isfile(fpath):
                raise ApiError(404, "照片文件丢失")
            with open(fpath, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", row["content_type"])
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "private, max-age=300")
            self.end_headers()
            self.wfile.write(body)

        # ---------- 静态 ----------
        def _static(self, path):
            if path == "/":
                path = "/index.html"
            rel = path.lstrip("/")
            fpath = os.path.normpath(os.path.join(STATIC_DIR, rel))
            if not fpath.startswith(STATIC_DIR + os.sep) and fpath != STATIC_DIR:
                raise ApiError(403, "非法路径")
            if os.path.isdir(fpath):
                fpath = os.path.join(fpath, "index.html")
            if not os.path.isfile(fpath):
                # SPA 回退
                fpath = os.path.join(STATIC_DIR, "index.html")
            ext = os.path.splitext(fpath)[1].lower()
            with open(fpath, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", CONTENT_TYPES.get(ext, "application/octet-stream"))
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    return httpd, svc
