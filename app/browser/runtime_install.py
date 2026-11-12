"""On-demand Chromium preparation, never a prerequisite for opening the UI.

Only Patchright's pinned driver chooses the download URL/revision. The desktop
status file contains progress, not raw subprocess output or user configuration.
Completed downloads are reused by Patchright; partial downloads may restart.
"""
import json
import os
from pathlib import Path
import re
import subprocess
import threading
import time

from app.processes import child_options, terminate_owned

_LOCK = threading.Lock()


def read_status(home):
    path = Path(home) / "runtime" / "browser-component.json"
    try:
        if path.stat().st_size > 4096:
            return {}
        value = json.loads(path.read_text(encoding="utf-8"))
        if value.get("status") not in {"downloading", "ready", "error", "cancelled"}:
            return {}
        return value
    except (OSError, ValueError, AttributeError):
        return {}


def ensure_chromium(executable, *, timeout=600):
    """Serialize preparations in the service; raise on failure and allow retry."""
    with _LOCK:
        executable = Path(executable)
        if executable.is_file():
            return
        raw_home = os.environ.get("CREATORHUB_DESKTOP_HOME")
        if not raw_home:
            raise RuntimeError("浏览器组件缺失，请安装 Chrome 或运行 patchright install chromium。")
        runtime = Path(raw_home) / "runtime"
        runtime.mkdir(parents=True, exist_ok=True)
        status_file = runtime / "browser-component.json"
        cancel_file = runtime / "browser-component.cancel"
        cancel_file.unlink(missing_ok=True)

        def report(status, message, progress=None):
            value = {"status": status, "message": message, "progress": progress,
                     "updated_at": time.time()}
            temp = status_file.with_suffix(".tmp")
            temp.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
            temp.replace(status_file)

        report("downloading", "正在准备浏览器组件；工作台仍可使用，可在启动中心取消。")
        process = None
        reader = None
        try:
            from patchright._impl._driver import compute_driver_executable, get_driver_env
            node, cli = compute_driver_executable()
            process = subprocess.Popen([str(node), str(cli), "install", "chromium"],
                env=get_driver_env(), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, **child_options())

            def read_progress():
                # Consume the pipe continuously, including output without newlines.
                pending = b""
                while True:
                    chunk = process.stdout.read(1)
                    if not chunk:
                        break
                    pending = (pending + chunk)[-1024:]
                    if chunk in (b"\r", b"\n"):
                        matches = re.findall(rb"\b(\d{1,3})%", pending)
                        if matches:
                            progress = min(100, int(matches[-1]))
                            report("downloading", "正在下载并校验浏览器组件（各组件分别显示进度）。", progress)
                        pending = b""

            reader = threading.Thread(target=read_progress, daemon=True)
            reader.start()
            deadline = time.monotonic() + timeout
            while process.poll() is None:
                if cancel_file.exists():
                    raise InterruptedError("浏览器准备已取消；下次使用时可重试。")
                if time.monotonic() >= deadline:
                    raise TimeoutError("浏览器下载超时，请检查网络后重试。")
                time.sleep(.2)
            reader.join(timeout=5)
            if process.returncode or not executable.is_file():
                raise RuntimeError("浏览器组件准备未完成，请检查网络后重试，或安装系统 Chrome。")
            report("ready", "浏览器组件已就绪，后续启动直接复用。", 100)
        except Exception as exc:
            if process and process.poll() is None:
                terminate_owned(process)
            if reader:
                reader.join(timeout=5)
            message = str(exc) if isinstance(exc, (InterruptedError, TimeoutError, RuntimeError)) else "浏览器组件准备失败，请检查网络后重试。"
            report("cancelled" if isinstance(exc, InterruptedError) else "error", message)
            raise RuntimeError(message) from exc
        finally:
            if process and process.stdout:
                process.stdout.close()
