"""Loopback-only, CSRF-protected shell host and WebView2 desktop wrapper."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import hmac
import json
import mimetypes
import os
import secrets
import threading

from desktop.controller import Controller
from desktop.launcher import InstanceLock, resources


class ShellServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, controller, port=0):
        self.controller = controller
        self.token = secrets.token_urlsafe(32)
        self.assets = resources() / "desktop" / "web"
        super().__init__(("127.0.0.1", port), ShellHandler)
        self.origin = f"http://127.0.0.1:{self.server_port}"


class ShellHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send(self, status, body, kind="application/json; charset=utf-8"):
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
        self.end_headers()
        self.wfile.write(body)

    def json(self, status, value):
        self.send(status, json.dumps(value, ensure_ascii=False).encode("utf-8"))

    def valid(self, api=False):
        if self.headers.get("Host") != f"127.0.0.1:{self.server.server_port}":
            return False
        if self.headers.get("Origin", self.server.origin) != self.server.origin:
            return False
        return not api or hmac.compare_digest(self.headers.get("X-Desktop-Token", ""), self.server.token)

    def do_GET(self):
        if not self.valid(self.path.startswith("/api/")):
            self.json(403, {"error": "访问校验失败，请重新打开启动中心。"})
            return
        if self.path == "/api/state":
            self.json(200, self.server.controller.state())
            return
        filename = "index.html" if self.path == "/" else self.path.lstrip("/")
        if filename not in {"index.html", "app.js", "app.css", "icons.svg", "app.js.LEGAL.txt"}:
            self.json(404, {"error": "页面不存在。"})
            return
        path = self.server.assets / filename
        if not path.is_file():
            self.json(503, {"error": "界面资源尚未构建，请运行 npm run build:desktop。"})
            return
        body = path.read_bytes()
        if filename == "index.html":
            body = body.replace(b"__DESKTOP_TOKEN__", self.server.token.encode())
        mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        self.send(200, body, mime + "; charset=utf-8")

    def do_POST(self):
        if self.path != "/api/action" or not self.valid(True):
            self.json(403, {"error": "访问校验失败，请重新打开启动中心。"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 8192:
                raise ValueError("请求大小无效。")
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict) or not isinstance(payload.get("data", {}), dict):
                raise ValueError("请求格式无效。")
            result = self.server.controller.action(payload.get("name"), payload.get("data", {}))
            self.json(200, result)
        except (ValueError, TypeError) as exc:
            self.json(400, {"error": str(exc)})
        except Exception:
            self.json(500, {"error": "操作未完成，请检查本机权限或稍后重试。"})


def run_desktop(home, *, autostart=False, install_browser=True):
    import webview
    import pystray
    from desktop.ui import brand_image
    lock = InstanceLock(home)
    controller = Controller(home, install_browser=install_browser)
    server = ShellServer(controller)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    window = webview.create_window("CreatorHub", server.origin, width=1100, height=800,
                                  min_size=(740, 580), background_color="#f6f5f3", text_select=True)
    tray = None
    allow_close = threading.Event()
    def finish():
        allow_close.set()
        if tray:
            tray.stop()
        window.destroy()
    def closing():
        if allow_close.is_set():
            return True
        controller.close_requested = True
        window.show()
        return False
    def restore():
        window.show()
        window.restore()
    def hide():
        nonlocal tray
        if not tray:
            tray = pystray.Icon("CreatorHub", brand_image(), "CreatorHub 启动中心", menu=pystray.Menu(
                pystray.MenuItem("打开启动中心", restore, default=True),
                pystray.MenuItem("停止并退出", closing)))
            tray.run_detached()
        window.hide()
    def export(summary):
        selected = window.create_file_dialog(webview.FileDialog.SAVE, save_filename="creatorhub-diagnostics.json", file_types=("JSON (*.json)",))
        if not selected:
            return False
        path = selected if isinstance(selected, str) else selected[0]
        Path(path).write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        return True
    controller.hide_window, controller.exit_window, controller.export_file = hide, finish, export
    window.events.closing += closing
    window.events.loaded += lambda: controller.start() if autostart else None
    try:
        webview.start(gui="edgechromium", private_mode=False, storage_path=str(home / "runtime" / "shell-browser"))
    finally:
        controller.stop()
        if controller.worker:
            controller.worker.join(timeout=50)
        server.shutdown()
        server.server_close()
        lock.close()
