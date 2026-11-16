"""网页界面的 HTTP 服务。只用标准库。"""
from __future__ import annotations

import json
import webbrowser
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .. import jenny, ops
from ..core import Config
from . import api, stats
from .auth import Sessions, ensure_server_config, read_server_config

STATIC = Path(__file__).resolve().parent / "static"
STATIC_TYPES = {".html": "text/html", ".css": "text/css", ".js": "text/javascript", ".json": "application/json"}
MAX_BODY = 2 * 1024 * 1024
OPEN_PATHS = {"/api/login", "/api/session"}   # 不需要先登录的接口


class Handler(BaseHTTPRequestHandler):
    home: Path
    sessions: Sessions
    server_version = "lane-ui"

    def log_message(self, fmt, *args):   # 不把每个请求都打到控制台
        pass

    # ---- 基础 ----------------------------------------------------------

    def token(self) -> str | None:
        cookie = SimpleCookie(self.headers.get("Cookie", ""))
        return cookie["lane_session"].value if "lane_session" in cookie else None

    def send_json(self, status: int, payload, cookie: str | None = None) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if cookie is not None:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(body)

    def read_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            raise SystemExit("请求内容太大")
        raw = self.rfile.read(length) if length else b"{}"
        data = json.loads(raw.decode("utf-8") or "{}")
        if not isinstance(data, dict):
            raise SystemExit("请求格式不对")
        return data

    def serve_static(self, path: str) -> None:
        name = "index.html" if path in ("/", "") else path.lstrip("/")
        target = (STATIC / name).resolve()
        if STATIC not in target.parents or not target.is_file():
            self.send_error(404)
            return
        body = target.read_bytes()
        kind = STATIC_TYPES.get(target.suffix)   # 不查系统注册表：Windows 上 .js 可能被登记成 text/plain
        if not kind:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", f"{kind}; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(body)

    # ---- 入口 ----------------------------------------------------------

    def do_GET(self):
        url = urlparse(self.path)
        if not url.path.startswith("/api/"):
            self.serve_static(url.path)
            return
        self.dispatch("GET", url.path, {k: v[0] for k, v in parse_qs(url.query).items()})

    def do_POST(self):
        url = urlparse(self.path)
        if self.headers.get("X-Requested-With") != "lane":   # 浏览器跨站表单带不上这个头
            self.send_json(403, {"error": "请求来源不对"})
            return
        try:
            body = self.read_body()
        except (SystemExit, ValueError) as exc:
            self.send_json(400, {"error": str(exc)})
            return
        self.dispatch("POST", url.path, body)

    def dispatch(self, method: str, path: str, args: dict) -> None:
        if path not in OPEN_PATHS and not self.sessions.valid(self.token()):
            self.send_json(401, {"error": "需要登录"})
            return
        handler = ROUTES.get((method, path))
        if not handler:
            self.send_json(404, {"error": "没有这个接口"})
            return
        try:
            result = handler(self, args)
        except SystemExit as exc:               # 操作不能执行：原因直接给用户
            self.send_json(400, {"error": str(exc)})
        except Exception as exc:                # 配置文件损坏等
            self.send_json(500, {"error": f"{type(exc).__name__}: {exc}"})
        else:
            if result is not None:
                self.send_json(200, result)

    # ---- 登录 ----------------------------------------------------------

    def login(self, args: dict):
        addr = self.client_address[0]
        wait = self.sessions.locked(addr)
        if wait:
            self.send_json(429, {"error": f"密码错误次数过多，请 {wait} 秒后再试", "wait": wait})
            return None
        token = self.sessions.login(addr, str(args.get("username", "")), str(args.get("password", "")))
        if not token:
            self.send_json(401, {"error": "用户名或密码不对"})
            return None
        self.send_json(200, {"ok": True}, cookie=f"lane_session={token}; HttpOnly; SameSite=Strict; Path=/")
        return None

    def logout(self, args: dict):
        self.sessions.logout(self.token())
        self.send_json(200, {"ok": True}, cookie="lane_session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0")
        return None

    def cfg(self) -> Config:
        return Config(self.home)


def message(text: str) -> dict:
    return {"ok": True, "message": text}


ROUTES = {
    ("POST", "/api/login"): Handler.login,
    ("POST", "/api/logout"): Handler.logout,
    ("GET", "/api/session"): lambda h, a: {"logged_in": h.sessions.valid(h.token())},
    ("GET", "/api/board"): lambda h, a: api.board(h.cfg()),
    ("GET", "/api/alerts"): lambda h, a: {"alerts": jenny.alert_history(h.cfg(), a.get("project", ""), 200)},
    ("GET", "/api/lane"):lambda h, a: api.lane_detail(h.cfg(), a.get("id", "")),
    ("GET", "/api/file"): lambda h, a: {"text": api.lane_file(h.cfg(), a.get("id", ""), a.get("name", ""))},
    ("GET", "/api/stats"): lambda h, a: stats.compute(h.cfg(), a.get("project", ""), a.get("from", ""), a.get("to", "")),
    ("GET", "/api/config"): lambda h, a: {"files": api.config_files(h.home)},
    ("GET", "/api/config/file"): lambda h, a: {"text": api.config_read(h.home, a.get("key", ""))},
    ("POST", "/api/config/file"): lambda h, a: message(api.config_save(h.home, a.get("key", ""), str(a.get("text", "")))),
    ("POST", "/api/lane/new"): lambda h, a: message(ops.new(h.cfg(), str(a.get("project", "")), str(a.get("title", "")), str(a.get("text", "")))),
    ("POST", "/api/lane/approve"): lambda h, a: message(ops.approve(h.cfg(), str(a.get("id", "")))),
    ("POST", "/api/lane/reject"): lambda h, a: message(ops.reject(h.cfg(), str(a.get("id", "")), str(a.get("message", "")))),
    ("POST", "/api/lane/resume"): lambda h, a: message(ops.resume(h.cfg(), str(a.get("id", "")), a.get("stage") or None, bool(a.get("reset_rounds")))),
    ("POST", "/api/lane/clean"): lambda h, a: message(ops.clean(h.cfg(), str(a.get("id", "")), bool(a.get("force")))),
    ("POST", "/api/scheduler/start"): lambda h, a: message(api.start_scheduler(h.cfg())),
    ("POST", "/api/scheduler/stop"): lambda h, a: message(api.stop_scheduler(h.cfg())),
}


def serve(cfg: Config, open_browser: bool = True) -> None:
    path, created = ensure_server_config(cfg.home)
    settings = read_server_config(cfg.home)
    host = settings.get("server", {}).get("host", "0.0.0.0")
    port = int(settings.get("server", {}).get("port", 8770))
    Handler.home, Handler.sessions = cfg.home, Sessions(cfg.home)
    httpd = ThreadingHTTPServer((host, port), Handler)
    local = f"http://127.0.0.1:{port}/"
    print(f"网页界面已启动：{local}")
    if host not in ("127.0.0.1", "localhost"):
        print(f"其他机器访问：http://<这台机器的名称或 IP>:{port}/  （连不上时检查 Windows 防火墙是否放行 {port} 端口）")
        print("注意：这是明文 HTTP。要经过公网访问，请放在 HTTPS 反向代理后面。")
    print(f"用户名和密码在 {path}" + ("（刚刚创建，密码是随机生成的）" if created else ""))
    print("Ctrl+C 停止。")
    if open_browser:
        webbrowser.open(local)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        httpd.server_close()
