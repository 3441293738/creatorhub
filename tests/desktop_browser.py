"""Headless renderer acceptance test with an isolated real control server.

No access to user profiles; error/offline fixtures exist only inside this test.
"""
import json
from pathlib import Path
import tempfile
import threading
import sys
from unittest.mock import Mock, patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from desktop.launcher import prepare_home
from desktop.controller import Controller
from desktop.web_shell import ShellServer
from patchright.sync_api import sync_playwright, expect


def main():
    screenshots = Path("build/desktop-qa")
    screenshots.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        prepare_home(home)
        controller = Controller(home, install_browser=False)
        controller.preferences["open_on_ready"] = False
        server = ShellServer(controller)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        errors = []
        checks = []
        def done(name):
            checks.append(name)
            print("PASS:", name, flush=True)
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                context = browser.new_context(viewport={"width": 1100, "height": 800}, reduced_motion="reduce")
                page = context.new_page()
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(server.origin)
                expect(page.get_by_role("button", name="启动本地服务", exact=True)).to_be_enabled()
                page.wait_for_timeout(1500)
                assert controller.process is None and controller.worker is None
                assert not (home / "data" / "creatorhub.db").exists()
                done("opening the desktop waits for manual startup")
                page.get_by_role("button", name="启动本地服务", exact=True).click()
                expect(page.get_by_role("button", name="打开工作台", exact=True)).to_be_enabled(timeout=45000)
                page.screenshot(path=str(screenshots / "workspace-light.png"), full_page=True)
                done("real service startup and light workspace")
                page.get_by_role("button", name="切换外观").click()
                page.get_by_role("menuitemradio", name="深色").click()
                expect(page.locator("html")).to_have_attribute("data-theme", "dark")
                page.screenshot(path=str(screenshots / "workspace-dark.png"), full_page=True)
                page.reload()
                expect(page.locator("html")).to_have_attribute("data-theme", "dark")
                done("dark appearance persists across reload")
                page.keyboard.press("Control+k")
                search = page.get_by_role("combobox", name="搜索入口与指南")
                search.fill("小红书")
                page.keyboard.press("ArrowDown")
                page.keyboard.press("Enter")
                expect(page.get_by_role("heading", name="小红书上手指南", exact=True)).to_be_visible()
                expect(page.get_by_role("heading", name="先完成这两步")).to_be_visible()
                page.get_by_role("button", name="返回平台列表").click()
                expect(page.get_by_role("heading", name="你想从哪个平台开始？")).to_be_visible()
                done("keyboard command search, platform detail and return")
                page.get_by_role("link", name="运行记录", exact=True).click()
                page.get_by_role("textbox", name="搜索运行记录").fill("不存在的记录")
                expect(page.get_by_role("heading", name="没有找到匹配记录")).to_be_visible()
                page.get_by_role("button", name="查看全部记录").click()
                page.locator(".activity-row").first.click()
                expect(page.get_by_role("dialog")).to_be_visible()
                page.keyboard.press("Escape")
                expect(page.get_by_role("dialog")).to_have_count(0)
                expect(page.locator(".activity-row").first).to_be_focused()
                done("activity filtering, empty results, detail and Escape")
                page.get_by_role("link", name="工作空间", exact=True).click()
                page.get_by_role("button", name="文件与数据", exact=False).click()
                expect(page.get_by_role("heading", name="用户数据目录")).to_be_visible()
                page.keyboard.press("Escape")
                page.get_by_role("button", name="诊断与帮助", exact=False).click()
                with page.expect_download() as download:
                    page.get_by_role("button", name="导出诊断摘要", exact=True).click()
                data = json.loads(Path(download.value.path()).read_text(encoding="utf-8"))
                assert "home" not in data and "events" not in data
                page.keyboard.press("Escape")
                done("data detail and privacy-preserving diagnostic download")
                page.get_by_role("button", name="停止并退出", exact=True).click()
                page.get_by_role("button", name="继续使用", exact=True).click()
                expect(page.get_by_role("button", name="打开工作台", exact=True)).to_be_enabled()
                page.get_by_role("button", name="服务操作").click()
                page.get_by_role("menuitem", name="停止本地服务", exact=True).click()
                page.get_by_role("button", name="停止服务", exact=True).click()
                expect(page.get_by_role("button", name="启动本地服务", exact=True)).to_be_enabled(timeout=40000)
                done("cancel exit and confirmed graceful stop")
                controller.change("error", "测试场景：网络检查未通过，请重试。")
                leftover = Mock(); leftover.poll.return_value = None
                controller.process = leftover
                def recover():
                    controller.process = None
                    controller.change("stopped", "测试服务已停止。")
                with patch.object(controller, "retry_stop", side_effect=recover) as retry:
                    page.get_by_role("button", name="服务操作").click()
                    expect(page.get_by_role("menuitem", name="停止本地服务", exact=True)).to_be_enabled()
                    page.get_by_role("menuitem", name="停止本地服务", exact=True).click()
                    page.get_by_role("button", name="停止服务", exact=True).click()
                    expect(page.get_by_role("button", name="启动本地服务", exact=True)).to_be_enabled()
                    retry.assert_called_once()
                done("error state with owned child allows confirmed stop retry")
                controller.change("error", "测试场景：网络检查未通过，请重试。")
                controller.event("测试故障", "只在测试目录注入的错误状态。", "error")
                expect(page.get_by_role("button", name="重新启动", exact=True)).to_be_enabled()
                page.screenshot(path=str(screenshots / "workspace-error.png"), full_page=True)
                page.route("**/api/state", lambda route: route.abort())
                expect(page.get_by_role("alert").filter(has_text="启动中心连接中断")).to_be_visible(timeout=10000)
                expect(page.get_by_role("button", name="重新启动", exact=True)).to_be_disabled()
                page.unroute("**/api/state")
                page.get_by_role("button", name="重新检查", exact=True).click()
                expect(page.get_by_role("button", name="重新启动", exact=True)).to_be_enabled(timeout=10000)
                done("error, offline disabled actions and recovery")
                for size in ({"width": 740, "height": 580}, {"width": 390, "height": 844}):
                    page.set_viewport_size(size)
                    for route in ("workspace", "activity", "guides", "guides/xhs", "settings"):
                        page.goto(server.origin + "/#/" + route)
                        expect(page.locator("h1")).to_be_visible()
                        page.wait_for_timeout(150)
                        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), (size, route)
                    page.screenshot(path=str(screenshots / f"settings-{size['width']}.png"), full_page=True)
                assert page.evaluate("getComputedStyle(document.querySelector('.theme-choices button')).transitionDuration") == "0s"
                done("responsive pages and reduced motion")
                assert not errors, errors
                browser.close()
        finally:
            controller.stop()
            if controller.worker:
                controller.worker.join(timeout=50)
            server.shutdown(); server.server_close()
        (screenshots / "results.json").write_text(json.dumps({"checks": checks, "javascript_errors": errors}, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
