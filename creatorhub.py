"""CreatorHub 一键安装、启动与自检入口。

这个文件只使用 Python 标准库，因此可以在项目依赖尚未安装时直接运行。
常用命令：

    python creatorhub.py
    python creatorhub.py install
    python creatorhub.py check
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path


ROOT = Path(__file__).resolve().parent
VENV_DIR = ROOT / ".venv"
MARKER_FILE = VENV_DIR / ".creatorhub-install.json"
CONFIG_FILE = ROOT / "config.yaml"
CONFIG_EXAMPLE = ROOT / "config.example.yaml"
MIN_PYTHON = (3, 10)


def log(message: str) -> None:
    print(f"[CreatorHub] {message}", flush=True)


def venv_python() -> Path:
    if os.name == "nt":
        return VENV_DIR / "Scripts" / "python.exe"
    return VENV_DIR / "bin" / "python"


def run(command: list[str | Path], *, env: dict[str, str] | None = None) -> None:
    printable = " ".join(str(part) for part in command)
    log(f"> {printable}")
    subprocess.run(
        [str(part) for part in command],
        cwd=ROOT,
        env=env,
        check=True,
    )


def ensure_supported_python() -> None:
    if sys.version_info < MIN_PYTHON:
        required = ".".join(map(str, MIN_PYTHON))
        current = f"{sys.version_info.major}.{sys.version_info.minor}"
        raise RuntimeError(f"需要 Python {required}+，当前为 {current}")


def ensure_config() -> None:
    if CONFIG_FILE.exists():
        return
    if not CONFIG_EXAMPLE.exists():
        raise RuntimeError("缺少 config.example.yaml")
    shutil.copy2(CONFIG_EXAMPLE, CONFIG_FILE)
    log("已生成 config.yaml（后续可在网页设置中调整常用配置）")


def ensure_venv() -> Path:
    python = venv_python()
    if python.exists():
        return python
    ensure_supported_python()
    log("首次运行：正在创建隔离的 Python 环境 .venv")
    run([sys.executable, "-m", "venv", VENV_DIR])
    if not python.exists():
        raise RuntimeError(f"虚拟环境创建失败：未找到 {python}")
    return python


def npm_executable() -> str | None:
    # PowerShell 默认可能禁止 npm.ps1；Windows 明确优先使用 npm.cmd。
    names = ("npm.cmd", "npm") if os.name == "nt" else ("npm",)
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    return None


def source_fingerprint() -> str:
    digest = hashlib.sha256()
    for relative in ("requirements.txt", "package.json", "package-lock.json"):
        path = ROOT / relative
        digest.update(relative.encode("utf-8"))
        if path.exists():
            digest.update(path.read_bytes())
    digest.update(f"py{sys.version_info.major}.{sys.version_info.minor}".encode())
    digest.update(f"node={bool(npm_executable())}".encode())
    return digest.hexdigest()


def installation_is_current() -> bool:
    if not venv_python().exists() or not MARKER_FILE.exists():
        return False
    try:
        marker = json.loads(MARKER_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return marker.get("fingerprint") == source_fingerprint()


def write_install_marker(*, node_installed: bool, browser_installed: bool) -> None:
    MARKER_FILE.write_text(
        json.dumps(
            {
                "fingerprint": source_fingerprint(),
                "node_installed": node_installed,
                "browser_installed": browser_installed,
                "python": f"{sys.version_info.major}.{sys.version_info.minor}",
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def install(*, skip_browser: bool = False, skip_node: bool = False) -> None:
    ensure_config()
    python = ensure_venv()

    # macOS 上旧版 pip 可能识别不到当前 Python/架构对应的 wheel，继而退回
    # clang/clang++ 本地编译。先更新构建工具，尽量直接安装官方 wheel。
    log("正在更新 Python 安装工具")
    run([python, "-m", "pip", "install", "--upgrade", "pip", "setuptools", "wheel"])

    log("正在安装 Python 依赖")
    run([python, "-m", "pip", "install", "-r", ROOT / "requirements.txt"])

    browser_installed = not skip_browser
    if skip_browser:
        log("已跳过 Chromium 安装；扫码登录和采集前需执行 patchright install chromium")
    else:
        log("正在安装 Patchright Chromium（首次下载耗时取决于网络）")
        run([python, "-m", "patchright", "install", "chromium"])

    node_installed = False
    npm = npm_executable()
    if skip_node:
        log("已跳过 Node.js 依赖（仅影响小红书发布）")
    elif npm:
        log("正在安装小红书发布所需的 Node.js 依赖")
        run([npm, "install", "--no-audit", "--no-fund"])
        node_installed = True
    else:
        log("未检测到 Node.js：核心功能可正常使用，小红书发布功能暂不可用")
        log("需要该功能时安装 Node.js 18+，再重新运行安装命令")

    write_install_marker(
        node_installed=node_installed,
        browser_installed=browser_installed,
    )
    log("安装完成")


def read_server_defaults() -> tuple[str, int]:
    """用轻量解析读取 server 配置，避免启动器依赖 PyYAML。"""
    host = "0.0.0.0"
    port = 8000
    if not CONFIG_FILE.exists():
        return host, port

    in_server = False
    for raw_line in CONFIG_FILE.read_text(encoding="utf-8").splitlines():
        line = raw_line.split("#", 1)[0].rstrip()
        if not line:
            continue
        if not line.startswith((" ", "\t")):
            in_server = line.strip() == "server:"
            continue
        if not in_server or ":" not in line:
            continue
        key, value = (part.strip() for part in line.split(":", 1))
        value = value.strip("'\"")
        if key == "host" and value:
            host = value
        elif key == "port":
            try:
                port = int(value)
            except ValueError:
                pass
    return host, port


def browser_url(host: str, port: int) -> str:
    public_hosts = {"0.0.0.0", "::", "[::]"}
    browser_host = "127.0.0.1" if host in public_hosts else host
    return f"http://{browser_host}:{port}"


def service_is_ready(url: str) -> bool:
    """Return True only when the configured address serves CreatorHub health."""
    try:
        with urllib.request.urlopen(f"{url}/health", timeout=0.8) as response:
            if response.status != 200:
                return False
            payload = json.loads(response.read().decode("utf-8"))
            return isinstance(payload, dict) and payload.get("status") == "ok"
    except Exception:
        return False


def port_is_open(host: str, port: int) -> bool:
    target = "127.0.0.1" if host in {"0.0.0.0", "::", "[::]"} else host
    target = target.strip("[]")
    try:
        with socket.create_connection((target, port), timeout=0.5):
            return True
    except OSError:
        return False


def open_browser_when_ready(url: str) -> None:
    for _ in range(120):
        if service_is_ready(url):
            log(f"服务已就绪：{url}")
            webbrowser.open(url)
            return
        time.sleep(0.5)


def start(
    *,
    host: str | None,
    port: int | None,
    no_open: bool,
    reload: bool,
    skip_install: bool,
    skip_browser: bool,
    skip_node: bool,
) -> None:
    ensure_config()
    if not skip_install and not installation_is_current():
        install(skip_browser=skip_browser, skip_node=skip_node)

    python = venv_python()
    if not python.exists():
        raise RuntimeError("尚未安装运行环境，请先运行：python creatorhub.py install")

    config_host, config_port = read_server_defaults()
    selected_host = host or config_host
    selected_port = port or config_port
    url = browser_url(selected_host, selected_port)

    # 双击启动器时很容易重复开两个控制台。旧逻辑中的浏览器等待线程会先
    # 命中已有服务并打印“已就绪”，随后新 uvicorn 才报 10048，造成服务
    # 已坏的错觉。已运行的是 CreatorHub 时直接复用；其他进程占用则给出
    # 明确错误，不再启动第二个 uvicorn。
    if service_is_ready(url):
        log(f"CreatorHub 已在运行：{url}")
        if not no_open:
            webbrowser.open(url)
        return
    if port_is_open(selected_host, selected_port):
        raise RuntimeError(
            f"端口 {selected_port} 已被其他程序占用；"
            "请关闭占用程序或使用 --port 指定其他端口"
        )

    if not no_open:
        threading.Thread(
            target=open_browser_when_ready,
            args=(url,),
            daemon=True,
        ).start()

    command: list[str | Path] = [
        python,
        "-m",
        "uvicorn",
        "app.main:app",
        "--host",
        selected_host,
        "--port",
        str(selected_port),
    ]
    if reload:
        command.append("--reload")

    log(f"启动中，按 Ctrl+C 停止；访问地址：{url}")
    run(command)


def check() -> None:
    ensure_config()
    python = venv_python()
    if not python.exists():
        raise RuntimeError("尚未安装运行环境，请先运行：python creatorhub.py install")
    run([python, ROOT / "selftest.py"])


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="CreatorHub 一键安装与启动",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "command",
        nargs="?",
        choices=("start", "install", "check"),
        default="start",
        help="操作；不填写时直接启动",
    )
    parser.add_argument("--host", help="监听地址（默认读取 config.yaml）")
    parser.add_argument("--port", type=int, help="监听端口（默认读取 config.yaml）")
    parser.add_argument("--no-open", action="store_true", help="启动后不自动打开浏览器")
    parser.add_argument("--reload", action="store_true", help="开发模式：代码变化时自动重载")
    parser.add_argument(
        "--skip-install",
        action="store_true",
        help="启动时不检查或安装依赖",
    )
    parser.add_argument(
        "--skip-browser",
        action="store_true",
        help="安装时跳过 Patchright Chromium",
    )
    parser.add_argument(
        "--skip-node",
        action="store_true",
        help="安装时跳过 Node.js 依赖（仅影响小红书发布）",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        ensure_supported_python()
        os.chdir(ROOT)
        if args.command == "install":
            install(skip_browser=args.skip_browser, skip_node=args.skip_node)
        elif args.command == "check":
            check()
        else:
            start(
                host=args.host,
                port=args.port,
                no_open=args.no_open,
                reload=args.reload,
                skip_install=args.skip_install,
                skip_browser=args.skip_browser,
                skip_node=args.skip_node,
            )
        return 0
    except KeyboardInterrupt:
        log("已停止")
        return 130
    except subprocess.CalledProcessError as exc:
        log(f"命令执行失败（退出码 {exc.returncode}）")
        return exc.returncode or 1
    except Exception as exc:
        log(f"错误：{exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
