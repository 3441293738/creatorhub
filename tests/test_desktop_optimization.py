"""Installer/runtime contracts without external downloads or real accounts."""
import asyncio
import io
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from app.browser import runtime_install
from app.browser.manager import BrowserManager
from app.runtime_tools import find_node
from desktop import platform_support
from desktop.bundle_config import dependency_options


def test_cached_chromium_never_downloads(tmp_path, monkeypatch):
    executable = tmp_path / "browser"
    executable.touch()
    spawn = Mock(side_effect=AssertionError("network preparation not expected"))
    monkeypatch.setattr(runtime_install.subprocess, "Popen", spawn)
    runtime_install.ensure_chromium(executable)
    spawn.assert_not_called()


def test_download_reports_progress_and_reuses_result(tmp_path, monkeypatch):
    monkeypatch.setenv("CREATORHUB_DESKTOP_HOME", str(tmp_path))
    executable = tmp_path / "browser"
    process = Mock(stdout=io.BytesIO(b"download | 45%\rdownload | 100%\n"), returncode=0)
    def poll():
        executable.touch()
        return 0
    process.poll.side_effect = poll
    spawn = Mock(return_value=process)
    monkeypatch.setattr(runtime_install.subprocess, "Popen", spawn)
    runtime_install.ensure_chromium(executable)
    runtime_install.ensure_chromium(executable)
    assert spawn.call_count == 1
    assert spawn.call_args.args[0][-2:] == ["install", "chromium"]
    assert runtime_install.read_status(tmp_path)["status"] == "ready"


@pytest.mark.parametrize("cancel", [True, False])
def test_download_cancel_and_timeout_are_retryable(tmp_path, monkeypatch, cancel):
    monkeypatch.setenv("CREATORHUB_DESKTOP_HOME", str(tmp_path))
    process = Mock(stdout=io.BytesIO(), returncode=None)
    process.poll.return_value = None
    def spawn(*args, **kwargs):
        if cancel:
            (tmp_path / "runtime/browser-component.cancel").touch()
        return process
    monkeypatch.setattr(runtime_install.subprocess, "Popen", spawn)
    terminate = Mock(side_effect=lambda p: setattr(p.poll, "return_value", 1))
    monkeypatch.setattr(runtime_install, "terminate_owned", terminate)
    with pytest.raises(RuntimeError):
        runtime_install.ensure_chromium(tmp_path / "missing", timeout=-1)
    terminate.assert_called_once_with(process)
    assert runtime_install.read_status(tmp_path)["status"] == ("cancelled" if cancel else "error")


def test_no_download_without_desktop_home(tmp_path, monkeypatch):
    monkeypatch.delenv("CREATORHUB_DESKTOP_HOME", raising=False)
    with pytest.raises(RuntimeError, match="Chrome"):
        runtime_install.ensure_chromium(tmp_path / "missing")


def test_browser_start_does_not_install_components(tmp_path, monkeypatch):
    manager = BrowserManager("test", str(tmp_path))
    playwright = SimpleNamespace(start=AsyncMock(return_value=SimpleNamespace()))
    monkeypatch.setattr("app.browser.manager.async_playwright", lambda: playwright)
    monkeypatch.setattr(manager, "_detect_chrome_major", AsyncMock(return_value=None))
    install = Mock(side_effect=AssertionError("startup must stay offline"))
    monkeypatch.setattr(runtime_install, "ensure_chromium", install)
    asyncio.run(manager.start())
    install.assert_not_called()


def test_macos_uses_application_support_and_native_webkit(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(platform_support.platform, "machine", lambda: "arm64")
    assert platform_support.default_home() == Path.home() / "Library/Application Support/CreatorHub/user-data"
    assert platform_support.webview_options() == {}
    assert platform_support.platform_tag() == "macos-arm64"


def test_bundled_node_does_not_need_system_node(tmp_path, monkeypatch):
    node = tmp_path / "node"
    node.touch()
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr("patchright._impl._driver.compute_driver_executable", lambda: (str(node), "cli"))
    monkeypatch.setattr("app.runtime_tools.shutil.which", Mock(side_effect=AssertionError("PATH not needed")))
    assert find_node() == str(node)


@pytest.mark.parametrize("frozen", [True, False])
def test_execjs_uses_absolute_node_and_utf8_even_with_ansi_locale(monkeypatch, frozen):
    import shutil
    from app.platforms.xhs import creator_sign
    node = shutil.which("node")
    assert node, "Build tests require Node (the installer bundles its own runtime)"
    monkeypatch.setattr(sys, "frozen", frozen, raising=False)
    monkeypatch.setattr(creator_sign, "find_node", lambda: node)
    monkeypatch.setattr(creator_sign, "_JS_CACHE", {})
    monkeypatch.setenv("PATH", "")
    # Match English Windows even when the test interpreter itself uses UTF-8.
    monkeypatch.setattr(subprocess, "_text_encoding", lambda: "cp1252")
    context = creator_sign._ctx("xhs_rap.js")
    assert context.eval("1 + 1") == 2
    assert context.eval("'中文输入输出😀'") == "中文输入输出😀"
    import execjs
    with pytest.raises(execjs.ProgramError, match="中文错误"):
        context.exec_("throw new Error('中文错误')")


def test_execjs_pipe_has_a_bounded_runtime(monkeypatch):
    from app.platforms.xhs import creator_sign
    monkeypatch.setattr(creator_sign, "find_node", lambda: "node")
    monkeypatch.setattr(creator_sign, "_JS_CACHE", {})
    run = Mock(side_effect=subprocess.TimeoutExpired("node", 30))
    monkeypatch.setattr(creator_sign.subprocess, "run", run)
    with pytest.raises(subprocess.TimeoutExpired):
        creator_sign._ctx("xhs_rap.js").eval("1 + 1")
    assert run.call_args.kwargs["encoding"] == "utf-8"
    assert run.call_args.kwargs["timeout"] == 30


def test_windows_and_mac_bundles_exclude_unused_heavy_dependencies():
    for target in ("windows", "macos"):
        options = dependency_options(target)
        for name in ("cv2", "numpy", "PyQt5", "PySide6"):
            index = options.index(name)
            assert options[index - 1] == "--exclude-module"
        assert options[options.index("app") - 1] == "--collect-submodules"
        assert options[options.index("pywebview") - 1] == "--copy-metadata"
    mac = dependency_options("macos")
    assert mac[mac.index("pythonnet") - 1] == "--exclude-module"


def test_update_restart_does_not_replay_tasks(tmp_path):
    from desktop.update_helper import restart_app
    with patch("desktop.update_helper.subprocess.Popen") as spawn:
        restart_app({"home": tmp_path, "executable": tmp_path / "aispace.exe"})
    assert spawn.call_args.args[0][-1] == "--no-autostart"


def test_full_upgrade_cleanup_is_limited_to_retired_application_libraries():
    root = Path(__file__).resolve().parents[1]
    installer = (root / "desktop/installer.iss").read_text(encoding="utf-8")
    cleanup = installer.split("[InstallDelete]", 1)[1].split("[Tasks]", 1)[0]
    lines = [line for line in cleanup.splitlines() if line.startswith("Type: filesandordirs;")]
    assert lines == [f'Type: filesandordirs; Name: "{{app}}\\_internal\\{name}"'
                     for name in ("cv2", "numpy", "numpy.libs")]
