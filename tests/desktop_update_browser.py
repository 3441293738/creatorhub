"""Desktop update flow against the real loopback API, with release/network fixtures."""
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from desktop.launcher import prepare_home
from desktop.controller import Controller
from desktop.web_shell import ShellServer
from desktop.updates import release_info
from test_desktop_updates import release
from test_desktop_update_download import verified_release, PACKAGE, Response
import hashlib
from patchright.sync_api import sync_playwright, expect


def main():
    with tempfile.TemporaryDirectory() as temp:
        home = Path(temp); prepare_home(home)
        controller = Controller(home, install_browser=False)
        server = ShellServer(controller)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        errors = []
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page(viewport={"width": 1100, "height": 800}, reduced_motion="reduce")
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(server.origin + "/#/settings")
                check = page.get_by_role("button", name="检查更新", exact=True)
                expect(check).to_be_enabled()
                def fetch(_):
                    time.sleep(.6)
                    return release_info(release(), "0.2.0")
                with patch("desktop.updates.fetch_release", side_effect=fetch), patch("desktop.controller.webbrowser.open", return_value=True) as opened:
                    check.click()
                    expect(page.get_by_role("button", name="正在检查…")).to_be_disabled()
                    page.get_by_role("button", name="查看 0.3.0 更新说明").click()
                    expect(page.get_by_label("更新说明内容")).to_contain_text("<script>alert(1)</script>")
                    assert page.locator(".release-notes script").count() == 0
                    output = Path("build/desktop-qa"); output.mkdir(parents=True, exist_ok=True)
                    page.screenshot(path=str(output / "update-notes.png"))
                    page.get_by_role("button", name="下载新版", exact=True).click()
                    expect(page.get_by_role("heading", name="下载前，请留意")).to_be_visible()
                    opened.assert_not_called()
                    page.get_by_role("button", name="返回更新说明").click()
                    opened.assert_not_called()
                    page.get_by_role("button", name="下载新版", exact=True).click()
                    page.get_by_role("button", name="确认并下载").click()
                    expect(page.get_by_role("status").filter(has_text="已交给浏览器下载")).to_be_visible()
                    opened.assert_called_once()
                    assert controller.process is None
                    page.keyboard.press("Escape")
                    expect(page.get_by_role("button", name="查看 0.3.0 更新说明")).to_be_focused()
                # Reminders never open a modal on their own; dismissal survives navigation/reload.
                page.get_by_role("link", name="工作空间", exact=True).click()
                banner = page.get_by_role("region", name="版本更新提醒")
                expect(banner).to_be_visible()
                expect(page.get_by_role("dialog")).not_to_be_visible()
                page.screenshot(path=str(output / "update-reminder-light.png"))
                banner.get_by_role("button", name="稍后提醒").click()
                expect(banner).not_to_be_visible()
                page.reload()
                expect(banner).not_to_be_visible()
                page.get_by_role("link", name="偏好设置", exact=True).click()
                auto_check = page.get_by_role("switch", name="自动检查新版本")
                expect(auto_check).to_be_checked()
                auto_check.click()
                expect(auto_check).not_to_be_checked()
                page.reload()
                expect(auto_check).not_to_be_checked()
                page.get_by_role("button", name="忽略此版本", exact=True).click()
                assert not controller.updates.state()["notification"]
                for result in (release_info(release(), "0.3.0"), release_info(release(asset=False), "0.2.0"),
                               {"status": "empty", "message": "暂未找到公开的正式版本。"},
                               {"status": "error", "message": "请检查网络后重试。"}):
                    controller.updates.last_attempt -= 6
                    with patch("desktop.updates.fetch_release", return_value=result):
                        check.click()
                        expect(page.locator(".update-status")).to_contain_text(result["message"])
                        if result["status"] == "current":
                            expect(page.locator(".update-plan")).not_to_be_visible()
                controller.updates.last_attempt -= 6
                with patch("desktop.updates.fetch_release", side_effect=fetch):
                    check.click()
                    expect(page.get_by_role("button", name="查看 0.3.0 更新说明")).to_be_visible()
                for size in ({"width": 740, "height": 580}, {"width": 390, "height": 844}):
                    page.set_viewport_size(size)
                    page.get_by_role("button", name="查看 0.3.0 更新说明").click()
                    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                    page.keyboard.press("Escape")
                # One-click flow uses the real API/downloader, but never runs an installer.
                page.set_viewport_size({"width": 1100, "height": 800})
                info = verified_release()
                class SlowPackage(Response):
                    def read(self, size=-1):
                        time.sleep(.4)
                        return super().read(size)
                def asset(url, **kwargs):
                    if url.endswith("SHA256.txt"):
                        return Response(f"{hashlib.sha256(PACKAGE).hexdigest()}  {info['asset_name']}\n".encode())
                    return SlowPackage(PACKAGE, len(PACKAGE))
                original_state = controller.state
                def running_state():
                    return {**original_state(), "can_stop": True}
                controller.updates.last_attempt -= 6
                with patch.object(controller, "update_install_supported", return_value=True), \
                     patch.object(controller, "state", side_effect=running_state), \
                     patch("desktop.updates.fetch_release", return_value=info), \
                     patch("desktop.updates.open_asset", side_effect=asset), \
                     patch.object(controller, "install_update") as install:
                    check.click()
                    page.get_by_role("link", name="工作空间", exact=True).click()
                    page.get_by_role("button", name="一键更新", exact=True).click()
                    expect(page.get_by_role("progressbar", name="安装包下载进度")).to_be_visible()
                    page.screenshot(path=str(output / "update-home-downloading.png"))
                    page.get_by_role("link", name="偏好设置", exact=True).click()
                    expect(check).to_be_disabled()
                    page.screenshot(path=str(output / "update-downloading.png"))
                    page.get_by_role("button", name="取消下载", exact=True).click()
                    expect(page.locator(".update-status")).to_contain_text("下载已取消")
                    install.assert_not_called()
                    page.get_by_role("button", name="重试下载", exact=True).click()
                    page.get_by_role("button", name="安装并重启", exact=True).click()
                    expect(page.get_by_role("heading", name="安装新版并重启？")).to_be_visible()
                    expect(page.locator(".update-warning")).to_contain_text("本地服务正在运行")
                    install.assert_not_called()
                    page.screenshot(path=str(output / "update-install-confirm.png"))
                    page.get_by_role("button", name="暂不安装", exact=True).click()
                    expect(page.get_by_role("button", name="安装并重启", exact=True)).to_be_focused()
                    install.assert_not_called()
                    # Dark/narrow/reduced-motion confirmation retains accessible controls.
                    controller.preferences["theme"] = "dark"
                    expect(page.locator("html")).to_have_attribute("data-theme", "dark")
                    for size in ({"width": 740, "height": 580}, {"width": 375, "height": 812}, {"width": 812, "height": 375}):
                        page.set_viewport_size(size)
                        page.get_by_role("button", name="安装并重启", exact=True).click()
                        expect(page.get_by_role("button", name="确认安装并重启", exact=True)).to_be_enabled()
                        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                        page.screenshot(path=str(output / f"update-confirm-{size['width']}.png"))
                        page.keyboard.press("Escape")
                    page.set_viewport_size({"width": 1100, "height": 800})
                    page.get_by_role("button", name="安装并重启", exact=True).click()
                    page.get_by_role("button", name="确认安装并重启", exact=True).click()
                    expect(page.get_by_role("dialog")).not_to_be_visible()
                    install.assert_called_once_with(info["tag"])
                    assert controller.process is None
                    # Delta plan shows actual transfer size, not the full installer size.
                    controller.updates.result.update(download_kind="delta", download_size=1024 * 1024,
                        size=100 * 1024 * 1024, status="downloaded", progress=100)
                    expect(page.locator(".update-plan")).to_contain_text("文件增量更新")
                    expect(page.locator(".update-plan")).to_contain_text("99.0 MB")
                    for theme in ("light", "dark"):
                        controller.preferences["theme"] = theme
                        expect(page.locator("html")).to_have_attribute("data-theme", theme)
                        page.set_viewport_size({"width": 375, "height": 812})
                        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                        page.screenshot(path=str(output / f"update-delta-{theme}.png"))
                    # Ready-to-install is also reachable on the home page, with the same confirmation.
                    page.get_by_role("link", name="工作空间", exact=True).click()
                    expect(banner).to_be_visible()
                    for theme in ("light", "dark"):
                        controller.preferences["theme"] = theme
                        expect(page.locator("html")).to_have_attribute("data-theme", theme)
                        for width, height in ((1100, 800), (375, 812), (812, 375)):
                            page.set_viewport_size({"width": width, "height": height})
                            page.evaluate("window.scrollTo(0, 0)")
                            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                            page.screenshot(path=str(output / f"update-home-ready-{theme}-{width}.png"))
                    page.set_viewport_size({"width": 740, "height": 580})
                    page.evaluate("document.body.style.zoom = '1.5'")
                    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                    banner.get_by_role("button", name="安装并重启", exact=True).click()
                    expect(page.get_by_role("heading", name="安装新版并重启？")).to_be_visible()
                    page.keyboard.press("Escape")
                    page.evaluate("document.body.style.zoom = ''")
                assert not errors, errors
                browser.close()
                print("PASS manual/one-click updates, home reminders, persistent opt-out/snooze/skip, shared download state, explicit install confirmation, focus, light/dark/narrow/zoom layouts")
        finally:
            server.shutdown(); server.server_close()


if __name__ == "__main__": main()
