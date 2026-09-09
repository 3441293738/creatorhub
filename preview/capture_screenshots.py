"""Capture README images from the current UI and the isolated static demo.

Run from the repository root: python -m preview.capture_screenshots
No backend, real account, persisted browser profile, or external requests are used.
"""
from __future__ import annotations

import json
import shutil
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from preview.build_preview import ROOT, build


VIEWPORT = {"width": 1600, "height": 1000}
CAPTURE_TIME = datetime(2026, 9, 9, 2, 0, tzinfo=timezone.utc)


@dataclass(frozen=True)
class Scene:
    filename: str
    tab: str
    ready: str
    theme: str = "light"
    platform: str = "douyin"
    subtab: str = ""
    action: str = ""


SCENES = (
    Scene("overview-douyin.png", "overview", "#stat-acc", theme="dark"),
    Scene("overview-xiaohongshu.png", "overview", "#stat-acc", platform="xhs"),
    Scene("accounts-list.png", "accounts", "#acc-table [data-account-detail]"),
    Scene("accounts-proxy.png", "accounts", "#proxy-table tbody tr", subtab="代理池"),
    Scene("monitor-posts.png", "monitors", "#content-table input[data-id]", subtab="作品记录"),
    Scene("monitor-create.png", "monitors", "#mon-table button", action="monitor"),
    Scene("monitor-comments.png", "comments", "#comment-table input[data-id]", subtab="评论记录"),
    Scene("publish-workflow.png", "publish", "#pub-title", action="publish"),
    Scene("share-download.png", "share-download", "#sd-history-body input[data-id]", action="download"),
    Scene("autocomment-rules.png", "autocomment", "#ac-rule-table tr"),
    Scene("account-hub-dm.png", "hub", "#hub-acc", theme="dark", action="dm"),
)


def prepare(page, scene: Scene) -> None:
    """Use the real controls; never replace the app's HTML or styles for a shot."""
    page.locator(f'.navitem[data-tab="{scene.tab}"]').click()
    if scene.subtab:
        page.get_by_role("tab", name=scene.subtab, exact=True).click()
    page.locator(scene.ready).first.wait_for(state="visible")
    if scene.action == "monitor":
        page.locator("#wb-create").click()
        page.get_by_role("dialog", name="新建作品监控").wait_for()
        page.locator("#t-url").fill("https://www.douyin.com/user/DEMO_CREATOR")
    elif scene.action == "publish":
        page.locator("#pub-files").set_input_files(str(ROOT / "preview" / "fixtures" / "publish-cover.svg"))
        page.locator('.wb-preview-media img[alt="publish-cover.svg"]').wait_for()
        page.wait_for_function("document.querySelector('.wb-preview-media img')?.naturalWidth > 0")
        page.locator("#pub-title").fill("把日常剪成一段小电影")
        page.locator("#pub-desc").fill("从清晨的第一束光，到傍晚的城市街角。\n记录日常，也记录每一次创作的灵感。")
        page.locator("#pub-topics").fill("日常记录,城市漫游,创作灵感")
        page.locator(".wb-preview-copy").get_by_text("把日常剪成一段小电影", exact=True).wait_for()
    elif scene.action == "download":
        page.locator("#sd-text").fill("示例作品 · 城市漫游 https://v.douyin.com/DEMO/")
    elif scene.action == "dm":
        page.locator('[data-hubtab="dm"]').click()
        page.locator('#dm-convs [data-conv="DEMO_CONVERSATION"]').click()
        page.locator("#dm-thread .dm-bubble").first.wait_for()
    # Move the pointer away from controls and wait for fonts/layout, not a timer.
    page.mouse.move(1599, 999)
    page.evaluate("""async () => {
      await document.fonts.ready;
      window.scrollTo({ top: 0, behavior: 'instant' });
      await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
    }""")


def capture() -> None:
    from patchright.sync_api import sync_playwright

    staging_root = ROOT / "_site"
    staging_root.mkdir(exist_ok=True)
    # The builder and cleanup only touch this freshly allocated local directory.
    with tempfile.TemporaryDirectory(prefix="screenshots-", dir=staging_root) as directory:
        staging = Path(directory).resolve()
        site = staging / "site"
        build(site)

        class QuietHandler(SimpleHTTPRequestHandler):
            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), partial(QuietHandler, directory=str(site)))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        origin = f"http://127.0.0.1:{server.server_port}"
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(channel="chrome", headless=True)
                try:
                    for scene in SCENES:
                        context = browser.new_context(
                            viewport=VIEWPORT, device_scale_factor=1, color_scheme=scene.theme,
                            reduced_motion="reduce", locale="zh-CN", timezone_id="Asia/Shanghai",
                            service_workers="block",
                        )
                        try:
                            errors, blocked = [], []

                            def route_request(route):
                                url = urlparse(route.request.url)
                                if url.netloc == urlparse(origin).netloc and not url.path.startswith("/api/"):
                                    route.continue_()
                                else:
                                    blocked.append(route.request.url)
                                    route.abort()

                            context.route("**/*", route_request)
                            context.add_init_script(
                                "localStorage.setItem('dym-pf', " + json.dumps(scene.platform) + ");"
                                "localStorage.setItem('creatorhub-appearance', " + json.dumps(json.dumps({
                                    "theme": scene.theme, "density": "comfortable", "motion": "reduced",
                                })) + ");"
                            )
                            page = context.new_page()
                            page.on("pageerror", lambda error: errors.append(str(error)))
                            page.on("response", lambda response: errors.append(f"HTTP {response.status}: {response.url}")
                                    if response.status >= 400 else None)
                            page.clock.set_fixed_time(CAPTURE_TIME)
                            page.goto(origin, wait_until="networkidle")
                            page.locator(".wb-attention").wait_for()
                            page.wait_for_function("document.querySelector('#stat-acc').textContent === '1'")
                            page.wait_for_function("document.querySelector('[data-overview-state=active]').textContent === '1'")
                            prepare(page, scene)
                            assert page.locator("html").get_attribute("data-theme") == scene.theme
                            assert page.locator("body").evaluate("(body, pf) => body.classList.contains('pf-' + pf)", scene.platform)
                            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), scene.filename
                            assert not page.locator(".wb-connection").is_visible(), scene.filename
                            assert not errors and not blocked, (scene.filename, errors, blocked)
                            page.screenshot(path=str(staging / scene.filename), full_page=scene.action != "monitor", animations="disabled")
                            print(f"Captured {scene.filename}")
                        finally:
                            context.close()
                finally:
                    browser.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

        # Publish only after every scene passed, so failed runs leave old images intact.
        destination = ROOT / "assets" / "screenshots"
        destination.mkdir(parents=True, exist_ok=True)
        for scene in SCENES:
            shutil.copy2(staging / scene.filename, destination / scene.filename)
        print(f"Updated {len(SCENES)} screenshots in {destination}")


if __name__ == "__main__":
    capture()
