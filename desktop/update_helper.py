"""Standalone, stdlib-only updater, frozen separately and copied outside the app.

An explicitly confirmed request is prepared by Controller. The helper pins the
parent process handle, acknowledges readiness, waits for exit, then exclusively
locks the desktop and service before running the verified Inno Setup package.
No network, arbitrary commands, user-data replacement or automatic task replay.
"""
from contextlib import ExitStack, contextmanager
import argparse
import ctypes
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from desktop.update_manifest import MAX_MANIFEST_SIZE, delta_name, manifest_name, valid_digest
from desktop.update_delta import apply_delta, load_delta, read_journal, rollback


def atomic_json(path, data):
    path = Path(path)
    temp = path.with_suffix(".tmp")
    with temp.open("w", encoding="utf-8") as stream:
        json.dump(data, stream, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())
    temp.replace(path)


def clean_environment():
    # A restarted frozen process must unpack/use the NEW application's resources.
    env = {key: value for key, value in os.environ.items() if not key.startswith("_PYI_")}
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    return env


def numeric_version(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{1,8}\.\d{1,8}\.\d{1,8}(?:\.\d{1,8})?", value):
        raise ValueError("Invalid update version")
    parts = tuple(map(int, value.split(".")))
    return parts + (0,) * (4 - len(parts))


def read_request(filename):
    request = Path(filename).resolve(strict=True)
    if request.name != "request.json" or request.stat().st_size > 8192:
        raise ValueError("Invalid update request")
    data = json.loads(request.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schema") != 1:
        raise ValueError("Invalid update request")
    for key in ("home", "install_dir", "installer"):
        if not isinstance(data.get(key), str) or not Path(data[key]).is_absolute():
            raise ValueError("Expected absolute update paths")
        data[key] = Path(data[key]).resolve(strict=True)
    stage = request.parent
    if (stage.parent != (data["home"] / "runtime" / "updates").resolve()
            or not re.fullmatch(r"update-[a-f0-9]{32}", stage.name)
            or data["home"].is_relative_to(data["install_dir"])
            or stage.is_relative_to(data["install_dir"])):
        raise ValueError("Update staging must be outside the application")
    target = numeric_version(data.get("version"))
    kind = data.get("kind", "full")
    if kind not in {"full", "delta"}:
        raise ValueError("Unsupported update kind")
    expected_name = (delta_name(data.get("from_version"), data["version"]) if kind == "delta"
                     else f"CreatorHub-Setup-{data['version']}-windows-x64.exe")
    if (data["installer"].parent != stage or data["installer"].name != expected_name
            or not data["installer"].is_file()):
        raise ValueError("Invalid staged installer")
    if (type(data.get("parent_pid")) is not int or data["parent_pid"] <= 0
            or type(data.get("size")) is not int or not 0 < data["size"] <= 2 * 1024 ** 3
            or not isinstance(data.get("sha256"), str)
            or not re.fullmatch(r"[a-f0-9]{64}", data["sha256"])):
        raise ValueError("Invalid update identity or digest")
    executable = data["install_dir"] / "CreatorHub.exe"
    current = (data["install_dir"] / "_internal" / "desktop-version.txt").read_text(encoding="utf-8").strip()
    if not executable.is_file() or target <= numeric_version(current):
        raise ValueError("Only a newer version may be installed")
    if kind == "delta":
        if (data["from_version"] != current or not isinstance(data.get("manifest"), str)
                or not Path(data["manifest"]).is_absolute() or not valid_digest(data.get("manifest_sha256"))):
            raise ValueError("Invalid delta manifest identity")
        manifest = Path(data["manifest"]).resolve(strict=True)
        if (manifest.parent != stage or manifest.name != manifest_name(data["version"])
                or manifest.stat().st_size > MAX_MANIFEST_SIZE):
            raise ValueError("Invalid staged manifest")
        data["manifest"] = manifest
        load_delta(data)
    data["kind"] = kind
    data.update(stage=stage, executable=executable, attempt=stage.name)
    return data


def verify_installer(data, stream=None):
    if stream is None:
        with Path(data["installer"]).open("rb") as source:
            return verify_installer(data, source)
    digest = hashlib.sha256()
    size = 0
    while chunk := stream.read(1024 * 1024):
        size += len(chunk)
        if size > data["size"]:
            raise ValueError("Installer size mismatch")
        digest.update(chunk)
    if size != data["size"] or not hmac.compare_digest(digest.hexdigest(), data["sha256"]):
        raise ValueError("Installer checksum mismatch")
    if data.get("kind") == "delta":
        from desktop.update_manifest import verify_tree
        manifest = load_delta(data)
        verify_tree(data["install_dir"], manifest["delta"]["base_files"])


@contextmanager
def locked_installer(path):
    """Windows denies writes/deletes while the verified package is executed."""
    if os.name != "nt":
        with Path(path).open("rb") as stream:
            yield stream
        return
    import msvcrt
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    handle = kernel.CreateFileW(str(path), 0x80000000, 1, None, 3, 0x80, None)
    if handle == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    with os.fdopen(fd, "rb") as stream:
        yield stream


class ParentProcess:
    def __init__(self, pid, executable):
        from ctypes import wintypes
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.kernel.OpenProcess.restype = wintypes.HANDLE
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self.kernel.WaitForSingleObject.restype = wintypes.DWORD
        self.kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                                          wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
        self.handle = self.kernel.OpenProcess(0x100000 | 0x1000, False, pid)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            name = ctypes.create_unicode_buffer(32768)
            size = wintypes.DWORD(len(name))
            if not self.kernel.QueryFullProcessImageNameW(self.handle, 0, name, ctypes.byref(size)):
                raise ctypes.WinError(ctypes.get_last_error())
            if Path(name.value).resolve() != Path(executable).resolve():
                raise ValueError("Parent executable mismatch")
        except Exception:
            self.close()
            raise

    def wait(self, cancel_file, timeout=120):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if cancel_file.exists():
                raise RuntimeError("Update handoff cancelled")
            result = self.kernel.WaitForSingleObject(self.handle, 250)
            if result == 0:
                if cancel_file.exists():
                    raise RuntimeError("Update handoff cancelled")
                return
            if result != 258:
                raise OSError("Parent wait failed")
        raise TimeoutError("Parent is still running")

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


@contextmanager
def runtime_lock(home, name):
    # Same byte-range contract as desktop.launcher.InstanceLock.
    stream = (home / "runtime" / name).open("a+b")
    try:
        stream.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        stream.close()


def installer_command(data):
    # https://jrsoftware.org/ishelp/topic_setupcmdline.htm
    return [str(data["installer"]), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/SP-",
            "/NORESTART", "/RESTARTEXITCODE=3010", "/NOCLOSEAPPLICATIONS",
            "/NORESTARTAPPLICATIONS", f"/DIR={data['install_dir']}",
            f"/LOG={data['home'] / 'logs' / (data['attempt'] + '-setup.log')}"]


def run_installer(data):
    # Avoid inheriting a PyInstaller DLL search directory into Inno Setup.
    if os.name == "nt":
        ctypes.windll.kernel32.SetDllDirectoryW(None)
    return subprocess.run(installer_command(data), cwd=data["stage"],
                          env=clean_environment(), check=False,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).returncode


def restart_app(data):
    env = clean_environment()
    env["CREATORHUB_DESKTOP_HOME"] = str(data["home"])
    # Reopen the launcher only. Running tasks are never replayed automatically.
    subprocess.Popen([str(data["executable"])], cwd=data["home"], env=env,
                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def notify_user(message):
    if os.name == "nt":
        ctypes.windll.user32.MessageBoxW(None, message, "CreatorHub 更新", 0x30)


def apply_update(filename):
    data = read_request(filename)
    parent = None
    exited = False
    result = {"status": "install_error", "version": data["version"], "reason": "handoff"}
    try:
        parent = ParentProcess(data["parent_pid"], data["executable"])
        atomic_json(data["stage"] / "ready.json", {"attempt": data["attempt"]})
        parent.wait(data["stage"] / "cancel")
        exited = True
        with ExitStack() as stack:
            # Another launcher or orphaned service => abort; never force-close it.
            stack.enter_context(runtime_lock(data["home"], "desktop.lock"))
            stack.enter_context(runtime_lock(data["home"], "service.lock"))
            package = stack.enter_context(locked_installer(data["installer"]))
            result["reason"] = "checksum"
            verify_installer(data, package)
            result["reason"] = "delta" if data["kind"] == "delta" else "installer"
            if data["kind"] == "delta":
                if os.name == "nt":
                    ctypes.windll.kernel32.SetDllDirectoryW(None)
                apply_delta(data, clean_environment())
                code = 0
            else:
                code = run_installer(data)
            result["exit_code"] = code
            if code == 3010:
                result.update(status="restart_required", reason="restart_required")
            elif code == 0:
                installed = (data["install_dir"] / "_internal" / "desktop-version.txt").read_text(encoding="utf-8").strip()
                if numeric_version(installed) != numeric_version(data["version"]):
                    raise ValueError("Installed version mismatch")
                result.update(status="installed", reason="complete")
            atomic_json(data["home"] / "runtime" / "update-result.json", result)
    except Exception:
        if data["kind"] == "delta":
            result["prefer_full"] = True
        import traceback
        try:
            with (data["home"] / "logs" / (data["attempt"] + "-helper.log")).open("a", encoding="utf-8") as log:
                traceback.print_exc(file=log)
        except OSError:
            pass
        if exited:
            atomic_json(data["home"] / "runtime" / "update-result.json", result)
        else:
            atomic_json(data["stage"] / "failed.json", {"reason": "handoff"})
    finally:
        if parent:
            parent.close()
    if exited and result["status"] == "restart_required":
        notify_user("新版已安装，需要重启 Windows 才能完成。请先保存其他工作，再手动重启电脑。账号与配置保留。")
    elif exited:
        try:
            restart_app(data)
        except OSError:
            # Persist the result even if the GUI is damaged or a reboot is needed.
            result.update(status="install_error", reason="restart")
            atomic_json(data["home"] / "runtime" / "update-result.json", result)
            notify_user("更新后的启动中心尚未打开。请重新启动 CreatorHub；如仍未打开，使用安装包修复安装。账号与备份保留在用户目录。")
    return 0 if result["status"] in {"installed", "restart_required"} else 1


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--request")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--recover-home")
    parser.add_argument("--parent-pid", type=int, default=0)
    args = parser.parse_args()
    if args.self_test:
        assert numeric_version("1.10.0") > numeric_version("1.9.0")
        return 0
    if os.name == "nt" and args.recover_home:
        return recover_update(Path(args.recover_home), args.parent_pid)
    if os.name != "nt" or not args.request:
        return 2
    try:
        return apply_update(args.request)
    except Exception:
        return 1


def recover_update(home, parent_pid=0):
    """External repair entry, also usable if power loss left the install path absent."""
    journal = read_journal(home)
    if not journal or journal.get("phase") in {"committed", "rolled_back"}:
        return 0
    parent = None
    try:
        if parent_pid:
            parent = ParentProcess(parent_pid, journal["install_dir"] / "CreatorHub.exe")
            atomic_json(home / "runtime/update-recovery-ready.json", {"pid": parent_pid})
            parent.wait(home / "runtime/update-recovery-cancel")
        with ExitStack() as stack:
            stack.enter_context(runtime_lock(home, "desktop.lock"))
            stack.enter_context(runtime_lock(home, "service.lock"))
            rollback(home, journal)
            atomic_json(home / "runtime/update-result.json", {"status": "install_error",
                        "version": journal["version"], "reason": "recovered", "prefer_full": True})
        restart_app({"home": home, "executable": journal["install_dir"] / "CreatorHub.exe"})
        return 0
    finally:
        if parent:
            parent.close()


if __name__ == "__main__":
    sys.exit(main())
