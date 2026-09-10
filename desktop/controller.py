"""Thread-safe desktop service lifecycle, independent of any GUI toolkit."""
from __future__ import annotations
from collections import deque
import json
import os
from pathlib import Path
import platform
import subprocess
import threading
import time
import urllib.request
import uuid
import webbrowser

from desktop.launcher import child_command, resources, snapshot, version
from desktop.updates import UpdateChecker, RELEASES_URL


class Controller:
    def __init__(self, home: Path, *, install_browser=True):
        self.home = home
        self.install_browser = install_browser
        self.lock = threading.RLock()
        self.process = None
        self.worker = None
        self.cancel = threading.Event()
        self.phase = "stopped"
        self.detail = "启动本地服务后，即可登录账号、管理内容。"
        self.url = None
        self.started_at = None
        self.events = deque(maxlen=200)
        self.close_requested = False
        self.preferences = {"theme": "system", "open_on_ready": True}
        self.pref_file = home / "runtime" / "desktop-preferences.json"
        try:
            saved = json.loads(self.pref_file.read_text(encoding="utf-8"))
            if saved.get("theme") in {"light", "dark", "system"}:
                self.preferences["theme"] = saved["theme"]
            if isinstance(saved.get("open_on_ready"), bool):
                self.preferences["open_on_ready"] = saved["open_on_ready"]
        except (OSError, ValueError, AttributeError):
            pass
        self.hide_window = None
        self.exit_window = None
        self.export_file = None
        self.exiting = False
        self.updates = UpdateChecker(version())

    def event(self, title, detail="", level="info"):
        with self.lock:
            self.events.appendleft({"id": uuid.uuid4().hex, "time": time.time(),
                                    "title": title, "detail": detail, "level": level})

    def state(self):
        with self.lock:
            return {"phase": self.phase, "detail": self.detail, "url": self.url,
                    "version": version(), "home": str(self.home), "events": list(self.events),
                    "updates": self.updates.state(),
                    "can_stop": bool((self.process and self.process.poll() is None) or (self.worker and self.worker.is_alive())),
                    "uptime": int(time.monotonic() - self.started_at) if self.started_at else 0,
                    "preferences": dict(self.preferences), "close_requested": self.close_requested,
                    "native": self.hide_window is not None}

    def change(self, phase, detail):
        with self.lock:
            self.phase, self.detail = phase, detail

    def start(self):
        with self.lock:
            if self.phase not in {"stopped", "error"} or (self.worker and self.worker.is_alive()):
                return
            if self.process and self.process.poll() is None:
                raise ValueError("上一次服务尚未退出，请先停止服务再重试。")
            self.cancel.clear()
            self.url = None
            self.phase, self.detail = "starting", "正在检查环境并启动本地服务。首次使用可能需要下载浏览器。"
            self.event("开始启动", "检查环境、准备浏览器并连接本地服务。")
            self.worker = threading.Thread(target=self.run, daemon=True)
            self.worker.start()

    def run(self):
        session = uuid.uuid4().hex
        process = None
        try:
            marker = self.home / "runtime" / "last-version.txt"
            if marker.exists() and marker.read_text(encoding="utf-8") != version():
                snapshot(self.home)
                self.event("升级备份已保存", "包含配置与数据库；原有媒体和账号资料保留。")
            if self.cancel.is_set():
                return
            flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            args = ["--serve", "--session", session, "--parent-pid", str(os.getpid())]
            if not self.install_browser:
                args.append("--skip-browser-install")
            with (self.home / "logs" / f"desktop-{session[:8]}.log").open("w", encoding="utf-8") as log:
                process = subprocess.Popen(child_command(*args), cwd=self.home,
                    env={**os.environ, "CREATORHUB_DESKTOP_HOME": str(self.home), "PYTHONUTF8": "1", "PYTHONUNBUFFERED": "1"},
                    stdout=log, stderr=subprocess.STDOUT, creationflags=flags)
            with self.lock:
                self.process = process
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            info = self.home / "runtime" / f"{session}.json"
            ready = False
            failed_checks = 0
            last_check = 0
            while process.poll() is None and not self.cancel.wait(.35):
                if not info.exists() or time.monotonic() - last_check < (3 if ready else .3):
                    continue
                last_check = time.monotonic()
                try:
                    port = json.loads(info.read_text(encoding="utf-8"))["port"]
                    url = f"http://127.0.0.1:{int(port)}"
                    with opener.open(url + "/_desktop/ready", timeout=2) as response:
                        if json.load(response).get("session") != session:
                            raise ValueError("Session mismatch")
                    failed_checks = 0
                    with self.lock:
                        if self.cancel.is_set():
                            break
                        self.url = url
                        if not ready:
                            self.started_at = time.monotonic()
                            marker.write_text(version(), encoding="utf-8")
                            self.event("本地服务已就绪", "现在可以打开工作台。", "success")
                        elif self.phase == "unreachable":
                            self.event("连接已恢复", "本地服务重新响应。", "success")
                        self.phase = "ready"
                        self.detail = "账号、监控与发布，都在你的本机工作台。"
                        auto_open = not ready and self.preferences["open_on_ready"]
                    ready = True
                    if auto_open:
                        webbrowser.open(url)
                except (OSError, ValueError, KeyError):
                    failed_checks += 1
                    if ready and failed_checks == 3:
                        self.change("unreachable", "服务进程仍在运行，但暂时没有响应。正在重新检查连接。")
                        self.event("本地服务暂未响应", "保留当前状态，等待连接恢复或停止后重试。", "warning")
            if self.cancel.is_set():
                self.change("stopping", "正在结束本地任务并释放浏览器，请稍候。")
                (self.home / "runtime" / f"{session}.stop").touch()
                try:
                    process.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    self.event("服务退出超时", "正在结束本次启动的进程。", "warning")
                    if os.name == "nt":
                        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                                       capture_output=True, creationflags=flags, check=True)
                    else:
                        process.terminate()
                    process.wait(timeout=15)
            else:
                raise RuntimeError(f"Service exited: {process.returncode}")
        except Exception:
            # Raw logs remain local. Never put arbitrary exceptions or credentials in UI/export.
            import traceback
            traceback.print_exc()
            self.change("error", "服务启动或运行未完成。请检查网络，查看本地日志后重试。")
            self.event("服务需要处理", "可打开日志目录定位原因，或导出不含账号信息的诊断摘要。", "error")
        finally:
            with self.lock:
                self.started_at, self.url = None, None
                if not process or process.poll() is not None:
                    self.process = None
                if self.cancel.is_set() and self.process is None:
                    self.phase = "stopped"
                    self.detail = "本地服务已停止，数据已保留。"
                    self.event("本地服务已停止", "再次启动可继续使用。")
                elif self.process is not None:
                    self.phase = "error"
                    self.detail = "服务尚未完全退出，请再次尝试停止。"
            if self.exiting and self.exit_window and self.process is None:
                self.exit_window()

    def stop(self, exiting=False):
        with self.lock:
            self.close_requested = False
            self.exiting = exiting
            alive = self.worker and self.worker.is_alive()
            if alive:
                self.phase = "stopping"
                self.cancel.set()
            elif self.process and self.process.poll() is None:
                self.phase = "stopping"
                self.worker = threading.Thread(target=self.retry_stop, daemon=True)
                self.worker.start()
            elif exiting and self.exit_window:
                self.exit_window()

    def retry_stop(self):
        """Recover a failed shutdown, touching only the child owned by this controller."""
        process = self.process
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW, timeout=20)
            else:
                process.terminate()
            process.wait(timeout=15)
            with self.lock:
                self.process = None
                self.phase, self.detail = "stopped", "本地服务已停止，数据已保留。"
                self.event("本地服务已停止", "再次启动可继续使用。")
            if self.exiting and self.exit_window:
                self.exit_window()
        except Exception:
            self.change("error", "服务尚未完全退出，请再次尝试停止或查看本地日志。")

    def diagnostic(self):
        with self.lock:
            return {"app_version": version(), "os": platform.system(), "os_release": platform.release(),
                    "python": platform.python_version(), "service_state": self.phase,
                    "exit_code": self.process.poll() if self.process else None}

    def action(self, name, data):
        if name == "check_updates":
            self.updates.check()
        elif name == "open_releases":
            if not webbrowser.open(RELEASES_URL):
                raise ValueError("浏览器未能打开，请手动访问 GitHub 项目发布页。")
        elif name == "download_update":
            if data.get("confirmed") is not True:
                raise ValueError("请先阅读安装提醒并确认下载。")
            url = self.updates.download_url(data.get("tag"))
            if not webbrowser.open(url):
                raise ValueError("浏览器未能打开，请从项目发布页下载安装包。")
            return {"ok": True, "message": "已交给浏览器下载；下载完成后，请先停止并退出旧版再安装。"}
        elif name == "start":
            self.start()
        elif name in {"stop", "exit"}:
            if data.get("confirmed") is not True:
                raise ValueError("请先确认停止服务。")
            self.stop(exiting=name == "exit")
        elif name == "dismiss_close":
            self.close_requested = False
        elif name == "open_panel":
            with self.lock:
                url = self.url if self.phase == "ready" else None
            if not url:
                raise ValueError("本地服务尚未就绪，请先启动或等待连接恢复。")
            webbrowser.open(url)
        elif name in {"open_data", "open_logs"}:
            os.startfile(str(self.home if name == "open_data" else self.home / "logs"))
        elif name == "open_guide":
            slug = data.get("platform", "")
            if slug not in {"", "douyin", "xhs", "kuaishou", "shipinhao"}:
                raise ValueError("请选择有效的平台。")
            root = self.url + "/guide/" if self.url and (resources() / "desktop-guide").exists() else "https://3441293738.github.io/creatorhub/guide/"
            webbrowser.open(root + (slug + "/" if slug else ""))
        elif name == "hide":
            if not self.hide_window:
                raise ValueError("请在桌面应用中使用托盘功能。")
            self.hide_window()
        elif name == "preferences":
            with self.lock:
                updates = {}
                if "theme" in data:
                    if data["theme"] not in {"light", "dark", "system"}:
                        raise ValueError("无效主题。")
                    updates["theme"] = data["theme"]
                if "open_on_ready" in data:
                    if not isinstance(data["open_on_ready"], bool):
                        raise ValueError("无效设置。")
                    updates["open_on_ready"] = data["open_on_ready"]
                next_preferences = {**self.preferences, **updates}
                temp = self.pref_file.with_suffix(".tmp")
                temp.write_text(json.dumps(next_preferences), encoding="utf-8")
                temp.replace(self.pref_file)
                self.preferences = next_preferences
        elif name == "export":
            summary = self.diagnostic()
            if self.export_file:
                saved = self.export_file(summary)
                return {"ok": True, "message": "诊断摘要已保存" if saved else "已取消导出"}
            return {"ok": True, "download": summary}
        else:
            raise ValueError("未支持的操作。")
        return {"ok": True}
