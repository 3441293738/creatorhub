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
                for result in (release_info(release(), "0.3.0"), release_info(release(asset=False), "0.2.0"),
                               {"status": "empty", "message": "暂未找到公开的正式版本。"},
                               {"status": "error", "message": "请检查网络后重试。"}):
                    controller.updates.last_attempt -= 6
                    with patch("desktop.updates.fetch_release", return_value=result):
                        check.click()
                        expect(page.locator(".update-status")).to_contain_text(result["message"])
                controller.updates.last_attempt -= 6
                with patch("desktop.updates.fetch_release", side_effect=fetch):
                    check.click()
                    expect(page.get_by_role("button", name="查看 0.3.0 更新说明")).to_be_visible()
                for size in ({"width": 740, "height": 580}, {"width": 390, "height": 844}):
                    page.set_viewport_size(size)
                    page.get_by_role("button", name="查看 0.3.0 更新说明").click()
                    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                    page.keyboard.press("Escape")
                assert not errors, errors
                browser.close()
                print("PASS update checking, notes, confirmation/cancel, browser handoff, focus return, current/missing/error states, retry and narrow layouts")
        finally:
            server.shutdown(); server.server_close()


if __name__ == "__main__": main()
