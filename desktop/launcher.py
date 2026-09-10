"""Windows desktop entry point. Source mode uses the same isolated user data.

The frozen executable also hosts --serve and --smoke-test child modes; it never
attempts to run `sys.executable -m ...` as if a frozen bootloader were Python.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import queue
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
import urllib.request
import webbrowser
import zipfile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def resources() -> Path:
    return Path(getattr(sys, "_MEIPASS", ROOT))


def user_directory() -> Path:
    override = os.environ.get("CREATORHUB_DESKTOP_HOME")
    if override:
        return Path(override).expanduser().resolve()
    base = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / ".local" / "share")))
    return base / "CreatorHub" / "user-data"


def version() -> str:
    path = resources() / "desktop-version.txt"
    return path.read_text(encoding="utf-8").strip() if path.exists() else "source"


def prepare_home(home: Path) -> None:
    home.mkdir(parents=True, exist_ok=True)
    for name in ("logs", "backups", "runtime"):
        (home / name).mkdir(exist_ok=True)
    config = home / "config.yaml"
    if not config.exists():
        shutil.copy2(resources() / "config.example.yaml", config)


def snapshot(home: Path) -> Path:
    """Offline pre-upgrade config/database snapshot; media/Profile stay in place."""
    import yaml
    config = home / "config.yaml"
    raw = yaml.safe_load(config.read_text(encoding="utf-8")) or {}
    db = Path((raw.get("storage") or {}).get("db_path", "data/creatorhub.db"))
    if not db.is_absolute():
        db = home / db
    filename = home / "backups" / f"settings-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}.zip"
    with zipfile.ZipFile(filename, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.write(config, "config.yaml")
        if db.is_file():
            temp = home / "runtime" / f"snapshot-{uuid.uuid4().hex}.db"
            try:
                with closing(sqlite3.connect(db)) as source, closing(sqlite3.connect(temp)) as destination:
                    source.backup(destination)
                archive.write(temp, "database.db")
            finally:
                temp.unlink(missing_ok=True)
        archive.writestr("README.txt", "配置与数据库快照，不包含媒体和账号 Profile。\n恢复时停止服务，将 database.db 放回 config.yaml 中 storage.db_path 对应位置。\n完整迁移请另行备份整个 user-data 及自定义目录。\n")
    return filename


class InstanceLock:
    def __init__(self, home: Path, name="desktop.lock"):
        self.file = (home / "runtime" / name).open("a+b")
        self.file.seek(0)
        if os.name == "nt":
            import msvcrt
            try:
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                self.file.close()
                raise RuntimeError("本地服务仍在使用此数据目录，请等待旧服务退出后重试。" if name == "service.lock" else "启动管理器已经打开，请检查任务栏或系统托盘。")
        else:
            import fcntl
            fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def close(self):
        self.file.close()


def bind_local_port(preferred: int = 8000) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if os.name == "nt":
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    try:
        sock.bind(("127.0.0.1", preferred))
    except OSError:
        sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    return sock


def child_command(*args: str) -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, *args]
    return [sys.executable, str(Path(__file__).resolve()), *args]


def serve(home: Path, session: str, install_browser: bool, parent_pid=0) -> int:
    from desktop.lifecycle import watch_parent
    service_lock = InstanceLock(home, "service.lock")
    close_watcher = watch_parent(parent_pid, home / "runtime" / f"{session}.stop")
    try:
        return _serve(home, session, install_browser)
    finally:
        close_watcher()
        service_lock.close()


def _serve(home: Path, session: str, install_browser: bool) -> int:
    os.chdir(home)
    os.environ["CREATORHUB_CONFIG_PATH"] = str(home / "config.yaml")
    os.environ.pop("DY_CONFIG_PATH", None)
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(home / "browsers")
    docs = resources() / "desktop-guide"
    if docs.is_dir():
        os.environ["CREATORHUB_DESKTOP"] = "1"
    else:
        os.environ.pop("CREATORHUB_DESKTOP", None)
    if install_browser:
        from patchright.sync_api import sync_playwright
        with sync_playwright() as playwright:
            available = Path(playwright.chromium.executable_path).is_file()
        if not available:
            print("首次启动：下载浏览器组件，请保持联网。完成后会自动打开面板。", flush=True)
            from patchright._impl._driver import compute_driver_executable, get_driver_env
            node, cli = compute_driver_executable()
            subprocess.run([node, cli, "install", "chromium"], env=get_driver_env(), check=True)
    from fastapi.staticfiles import StaticFiles
    from app.main import app
    import uvicorn

    docs = resources() / "desktop-guide"
    if docs.exists():
        app.mount("/guide", StaticFiles(directory=str(docs), html=True), name="desktop-guide")

    @app.get("/_desktop/ready", include_in_schema=False)
    async def desktop_ready():
        return {"session": session}

    sock = bind_local_port()
    port = sock.getsockname()[1]
    state = home / "runtime" / f"{session}.json"
    temp = state.with_suffix(".tmp")
    temp.write_text(json.dumps({"port": port}), encoding="utf-8")
    temp.replace(state)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, workers=1, log_config=None))
    stop = home / "runtime" / f"{session}.stop"
    def watch_stop():
        while not server.should_exit:
            if stop.exists():
                server.should_exit = True
                return
            time.sleep(.25)
    threading.Thread(target=watch_stop, daemon=True).start()
    try:
        server.run(sockets=[sock])
    finally:
        server.should_exit = True
        sock.close()
        state.unlink(missing_ok=True)
        stop.unlink(missing_ok=True)
    return 0


def smoke_test() -> int:
    # Import all application dependencies and resolve bundled assets in an
    # isolated directory, without starting jobs, signing in or making requests.
    import tempfile
    previous = Path.cwd()
    with tempfile.TemporaryDirectory() as temp:
        try:
            os.chdir(temp)
            os.environ["CREATORHUB_CONFIG_PATH"] = str(Path(temp) / "absent.yaml")
            from app.main import app, WEB_DIR
            from patchright._impl._driver import compute_driver_executable
            from imageio_ffmpeg import get_ffmpeg_exe
            from patchright.sync_api import sync_playwright
            import tkinter as tk
            import pystray
            assert app and (WEB_DIR / "workbench.js").is_file()
            assert (resources() / "config.example.yaml").is_file()
            assert (resources() / "desktop-guide" / "xhs" / "index.html").is_file()
            assert all(Path(path).is_file() for path in compute_driver_executable())
            assert Path(get_ffmpeg_exe()).is_file()
            with sync_playwright() as playwright:
                assert playwright.chromium.executable_path
            ui = tk.Tk()
            ui.withdraw()
            from types import SimpleNamespace
            from desktop.ui import LauncherView
            owner = SimpleNamespace(root=ui, **{name: lambda: None for name in (
                "start", "open_panel", "open_guide", "open_data", "diagnostics", "hide", "close")})
            view = LauncherView(owner, version())
            view.phase("ready", "http://127.0.0.1:8000")
            ui.update_idletasks()
            ui.destroy()
            assert pystray.Icon
        finally:
            os.chdir(previous)
    return 0


class Launcher:
    def __init__(self, home: Path):
        import tkinter as tk
        self.home, self.events = home, queue.Queue()
        self.process = None
        self.session = None
        self.url = None
        self.busy = False
        self.closing = False
        self.tray = None
        self.launch_done = threading.Event()
        self.launch_done.set()
        self.lock = InstanceLock(home)
        self.root = tk.Tk()
        from desktop.ui import LauncherView
        self.view = LauncherView(self, version())
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.after(100, self.poll)

    def start(self):
        if self.closing or self.busy or (self.process and self.process.poll() is None):
            return
        self.busy = True
        self.launch_done.clear()
        self.start_button.configure(state="disabled")
        self.view.phase("starting")
        self.progress.start()
        self.status.set("正在准备运行环境…")
        self.events.put(("detail", "首次下载可能需要数分钟；失败后可点“启动 / 重试”。"))
        threading.Thread(target=self.launch_worker, daemon=True).start()

    def launch_worker(self):
        try:
            marker = self.home / "runtime" / "last-version.txt"
            current = version()
            if marker.exists() and marker.read_text(encoding="utf-8") != current:
                self.events.put(("status", "升级前备份配置与数据库…"))
                snapshot(self.home)
            self.session = uuid.uuid4().hex
            if self.closing:
                return
            log = self.home / "logs" / f"desktop-{time.strftime('%Y%m%d-%H%M%S')}.log"
            self.events.put(("status", "正在启动；如缺少浏览器组件，将自动下载…"))
            with log.open("w", encoding="utf-8") as output:
                env = {**os.environ, "PYTHONUTF8": "1", "PYTHONUNBUFFERED": "1"}
                self.process = subprocess.Popen(child_command("--serve", "--session", self.session, "--parent-pid", str(os.getpid())),
                    cwd=self.home, env=env, stdout=output, stderr=subprocess.STDOUT,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            started = time.monotonic()
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            while self.process.poll() is None:
                if self.closing:
                    return
                state = self.home / "runtime" / f"{self.session}.json"
                if state.exists():
                    port = json.loads(state.read_text(encoding="utf-8"))["port"]
                    url = f"http://127.0.0.1:{port}"
                    try:
                        with opener.open(url + "/_desktop/ready", timeout=1) as response:
                            ready = json.load(response).get("session") == self.session
                        if ready:
                            marker.write_text(current, encoding="utf-8")
                            self.events.put(("ready", url))
                            return
                    except (OSError, ValueError):
                        pass
                if time.monotonic() - started > 120:
                    self.events.put(("detail", "仍在准备。下载受网络影响，可查看数据目录下 logs；停止并退出后可重试。"))
                time.sleep(.5)
            self.events.put(("error", f"启动未完成（退出码 {self.process.returncode}）。请检查网络，并查看 logs 中本次 desktop / process 日志；日志可能含私人数据，不要直接公开。"))
        except Exception as exc:
            self.events.put(("error", f"启动失败：{exc}"))
        finally:
            self.launch_done.set()

    def poll(self):
        try:
            while True:
                event, value = self.events.get_nowait()
                if event == "status":
                    self.status.set(value)
                elif event == "detail":
                    self.detail.set(value)
                elif event == "ready":
                    if self.closing:
                        continue
                    self.url, self.busy = value, False
                    self.progress.stop()
                    self.view.phase("ready", value)
                    self.status.set("工作台已就绪")
                    self.detail.set("关闭网页不会停止任务。退出时点击“停止并退出”。")
                    self.open_button.configure(state="normal")
                    self.open_panel()
                elif event == "error":
                    self.busy = False
                    self.progress.stop()
                    self.view.phase("error")
                    self.status.set("启动遇到了问题")
                    self.detail.set(value)
                    self.start_button.configure(state="normal")
                elif event == "show":
                    self.root.deiconify()
                    self.root.lift()
                elif event == "exit":
                    self.close()
                elif event == "stopped":
                    if self.tray:
                        self.tray.stop()
                    self.lock.close()
                    self.root.destroy()
                    return
        except queue.Empty:
            pass
        if self.url and self.process and self.process.poll() is not None and not self.closing:
            self.url = None
            self.open_button.configure(state="disabled")
            self.events.put(("error", "本地服务已停止。查看 logs 中的错误后，可点击启动 / 重试。"))
        self.root.after(150, self.poll)

    def open_data(self):
        from tkinter import messagebox
        try:
            os.startfile(str(self.home))
        except OSError as exc:
            messagebox.showerror("打开目录失败", str(exc))

    def open_panel(self):
        if self.url:
            webbrowser.open(self.url)

    def open_guide(self):
        if self.url and (resources() / "desktop-guide").exists():
            webbrowser.open(self.url + "/guide/")
        else:
            webbrowser.open("https://3441293738.github.io/creatorhub/guide/")

    def hide(self):
        from tkinter import messagebox
        try:
            if not self.tray:
                import pystray
                from desktop.ui import brand_image
                icon = brand_image()
                self.tray = pystray.Icon("CreatorHub", icon, "CreatorHub 正在本地运行", menu=pystray.Menu(
                    pystray.MenuItem("打开管理器", lambda: self.events.put(("show", None)), default=True),
                    pystray.MenuItem("停止并退出", lambda: self.events.put(("exit", None)))))
                self.tray.run_detached()
            self.root.withdraw()
        except Exception as exc:
            messagebox.showinfo("托盘未就绪", f"请使用任务栏最小化；启动窗口仍保留。\n{exc}")

    def diagnostics(self):
        from tkinter import filedialog, messagebox
        import platform
        path = filedialog.asksaveasfilename(title="导出不含账号数据的诊断摘要", defaultextension=".json", initialfile="creatorhub-diagnostics.json")
        if path:
            # Allowlist only: no raw logs, paths, config, tokens, accounts or DB.
            data = {"app_version": version(), "os": platform.system(), "os_release": platform.release(),
                    "python": platform.python_version(), "frozen": bool(getattr(sys, "frozen", False)),
                    "service_running": bool(self.process and self.process.poll() is None),
                    "exit_code": self.process.poll() if self.process else None}
            try:
                Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
                messagebox.showinfo("已导出", "摘要不包含配置、账号、日志或本机目录。")
            except OSError as exc:
                messagebox.showerror("保存失败", str(exc))

    def close(self):
        from tkinter import messagebox
        if self.closing:
            return
        if not messagebox.askyesno("停止并退出", "退出会停止本地任务。确定停止服务并退出？"):
            return
        self.closing = True
        self.view.phase("stopping")
        self.status.set("正在停止本地服务…")
        self.progress.start()
        threading.Thread(target=self.stop_worker, daemon=True).start()

    def stop_worker(self):
        # Wait for a concurrent pre-start backup/spawn to finish before closing.
        self.launch_done.wait()
        if self.process and self.process.poll() is None:
            (self.home / "runtime" / f"{self.session}.stop").touch()
            try:
                self.process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                # Only terminate our owned child tree, never all Python/Chrome.
                if os.name == "nt":
                    subprocess.run(["taskkill", "/PID", str(self.process.pid), "/T", "/F"],
                                   creationflags=subprocess.CREATE_NO_WINDOW, capture_output=True)
                else:
                    self.process.terminate()
                self.process.wait()
        self.events.put(("stopped", None))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--session", default="")
    parser.add_argument("--parent-pid", type=int, default=0)
    parser.add_argument("--smoke-test", action="store_true")
    parser.add_argument("--shell-smoke-test", action="store_true")
    parser.add_argument("--skip-browser-install", action="store_true")
    parser.add_argument("--legacy-ui", action="store_true", help="Use the legacy emergency window")
    parser.add_argument("--no-autostart", action="store_true")
    args = parser.parse_args()
    if args.shell_smoke_test:
        from desktop.webview_smoke import smoke
        return smoke()
    if args.smoke_test:
        return smoke_test()
    home = user_directory()
    prepare_home(home)
    if args.serve:
        if not args.session or any(c not in "0123456789abcdef" for c in args.session):
            raise ValueError("Invalid desktop session")
        return serve(home, args.session, not args.skip_browser_install, args.parent_pid)
    try:
        if args.legacy_ui:
            ui = Launcher(home)
            ui.root.mainloop()
        else:
            from desktop.web_shell import run_desktop
            run_desktop(home, install_browser=not args.skip_browser_install)
    except Exception as exc:
        from tkinter import messagebox
        messagebox.showerror("CreatorHub 启动提示", str(exc))
        return 1
    return 0


if __name__ == "__main__":
    import multiprocessing
    multiprocessing.freeze_support()
    # Windowed frozen executables may not initialize Python's standard streams.
    if sys.stdout is None or sys.stderr is None:
        home = user_directory()
        (home / "logs").mkdir(parents=True, exist_ok=True)
        stream = (home / "logs" / f"process-{os.getpid()}.log").open("a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stderr = stream
    try:
        result = main()
    except Exception:
        import traceback
        traceback.print_exc()
        result = 1
    raise SystemExit(result)
