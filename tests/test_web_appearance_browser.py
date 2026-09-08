"""Opt-in headless UI acceptance against the bundled demo, never real accounts.

PowerShell: $env:CREATORHUB_RUN_LOCAL_CDP='1'; python -m pytest tests/test_web_appearance_browser.py
Optional CREATORHUB_UI_ARTIFACTS saves screenshots to a chosen directory.
"""
import os
import json
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import pytest
from patchright.sync_api import sync_playwright

pytestmark = [pytest.mark.local_cdp, pytest.mark.skipif(
    os.environ.get("CREATORHUB_RUN_LOCAL_CDP") != "1", reason="local UI acceptance is opt-in")]
LEGACY_BRAND = json.loads((Path(__file__).parent / "fixtures/legacy-brand.json").read_text(encoding="utf-8"))


@pytest.fixture
def ui(tmp_path, monkeypatch, request):
    from preview import build_preview
    monkeypatch.setattr(build_preview, "ROOT", tmp_path)
    site = tmp_path / "site"
    build_preview.build(site)
    if request.node.name.startswith("test_workbench"):
        api_fixture = Path(__file__).parent / "fixtures" / "workbench-api.js"
        with (site / "demo-api.js").open("a", encoding="utf-8") as script:
            script.write("\n" + api_fixture.read_text(encoding="utf-8"))

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
            context = browser.new_context(viewport={"width": 1440, "height": 1000}, color_scheme="light", reduced_motion="reduce",
                                          has_touch=request.node.name in ("test_responsive_navigation_and_dialogs", "test_workbench_calendar_touch_and_motion_cleanup") or
                                          (request.node.name.startswith("test_workbench_platform_brand") and request.node.callspec.params["width"] <= 400) or
                                          (request.node.name.startswith("test_workbench_ios_editor_layout") and min(request.node.callspec.params["width"], request.node.callspec.params["height"]) <= 375))
            context.route("**/*", lambda route: route.continue_() if urlparse(route.request.url).netloc == urlparse(origin).netloc else route.abort())
            page = context.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(origin, wait_until="networkidle")
            yield page, errors
            context.close()
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def capture(page, name):
    directory = os.environ.get("CREATORHUB_UI_ARTIFACTS")
    if directory:
        target = Path(directory).resolve()
        target.mkdir(parents=True, exist_ok=True)
        # Full-page capture may resize the viewport and legitimately dismiss a
        # positioned popup. Preserve the actual viewport for transient controls.
        page.screenshot(path=str(target / f"{name}.png"), full_page=not name.startswith("ios-editor-") and name not in (
            "mobile-navigation", "mobile-search", "mobile-confirm", "polish-touch-calendar",
            "polish-mobile-navigation", "polish-monitor-sheet", "polish-account-menu", "polish-failed-submit"))


def navigate(page, name, mobile=False):
    if mobile:
        page.locator("#nav-toggle").click()
    page.locator(f'.navitem[data-tab="{name}"]').click()
    page.wait_for_function("name => document.querySelector('.navitem.active').dataset.tab === name", arg=name)


def assert_no_overflow(page, label):
    result = page.evaluate("""() => ({ width: document.documentElement.clientWidth, scroll: document.documentElement.scrollWidth,
      offenders: [...document.querySelectorAll('main *')].filter(el => {
        const r = el.getBoundingClientRect();
        return r.width && r.right > innerWidth + 2 && !el.closest('.table-wrap,.cs-panel,.dt-panel,.rp-thumbs,.tabbar');
      }).slice(0, 5).map(el => el.id || el.className) })""")
    if result["scroll"] > result["width"] + 2:
        capture(page, "overflow-" + label.replace("/", "-"))
    assert result["scroll"] <= result["width"] + 2, (label, result)


EDITORS = {
    "monitors": ("monitors", "mon-table", "em-alias", "alias", "/api/monitors/11"),
    "comments": ("comments", "watch-table", "ew-alias", "alias", "/api/comment-watches/21"),
    "danmaku": ("danmaku", "danmaku-watch-table", "edw-include", "include_keywords", "/api/danmaku-watches/31"),
    "collections": ("collections", "collection-job-table", "ecol-keywords", "keywords", "/api/collections/1"),
    "rules": ("autocomment", "ac-rule-table", "em-name", "name", "/api/comment-rules/61"),
    "notifications": ("notifications", "n-table", "ec-name", "name", "/api/notifications/91"),
    "publish": ("publish", "pub-table", "ep-title", "title", "/api/publish/51"),
    "proxy": ("accounts", "proxy-table", "ui-inp", "label", "/api/proxies/41"),
    "comment-draft": ("autocomment", "ac-task-table", "ui-inp", "content", "/api/comment-tasks/71"),
}


def open_legacy_editor(page, name):
    if name == "fingerprint":
        navigate(page, "accounts", mobile=page.viewport_size["width"] <= 860)
        page.get_by_role("tab", name="账号列表", exact=True).click()
        page.locator('[data-account-menu] button').first.click()
        page.get_by_role("menuitem", name="设备指纹", exact=True).click()
    else:
        route, table, *_ = EDITORS[name]
        navigate(page, route, mobile=page.viewport_size["width"] <= 860)
        tab = {"publish": "发布记录", "proxy": "代理池", "comment-draft": "任务与审核", "rules": "评论规则"}.get(name)
        if tab:
            page.get_by_role("tab", name=tab, exact=True).click()
        page.locator(f'#{table} button').filter(has_text="编辑").first.click()
    page.locator("#uimodal").wait_for(state="visible")
    page.wait_for_function("!!document.activeElement.closest('#uimodal')")


def assert_editor_layout(page, name):
    state = page.locator("#uimodal").evaluate("""modal => {
      const body = modal.querySelector('#ui-body'), b = body.getBoundingClientRect();
      const box = modal.querySelector('.rp-box').getBoundingClientRect();
      const head = modal.querySelector('.ui-modal-head').getBoundingClientRect();
      const title = modal.querySelector('#ui-title').getBoundingClientRect();
      const actions = modal.querySelector('#ui-actions').getBoundingClientRect();
      const fields = [...body.querySelectorAll('input:not([type=hidden]),textarea,.cs-trg,.dt-trg,label,legend')]
        .filter(el => el.getBoundingClientRect().width > 2);
      return { width:body.clientWidth, scroll:body.scrollWidth,
        boxInside:box.left >= 0 && box.right <= innerWidth && box.top >= 0 && box.bottom <= innerHeight + 1,
        actionsInside:actions.top >= box.top && actions.bottom <= box.bottom,
        headInside:head.top >= box.top && title.left >= head.left && title.right <= head.right && title.bottom <= head.bottom,
        outerScroll:modal.querySelector('.rp-box').scrollTop,
        overflow:fields.filter(el => { const r=el.getBoundingClientRect(); return r.left < b.left-1 || r.right > b.right+1; }).map(el => el.id || el.textContent),
        unnamed:fields.filter(el=>el.matches('input,textarea') && !el.labels?.length && !el.getAttribute('aria-label') && !el.getAttribute('aria-labelledby')).map(el=>el.id),
        buttons:[...modal.querySelectorAll('#ui-actions > button')].map(el => ({ text:el.textContent, height:el.getBoundingClientRect().height, nowrap:getComputedStyle(el).whiteSpace })) };
    }""")
    assert state["scroll"] <= state["width"] + 1, (name, state)
    assert state["boxInside"] and state["actionsInside"], (name, state)
    assert state["headInside"] and state["outerScroll"] == 0, (name, state)
    assert not state["overflow"], (name, state)
    assert not state["unnamed"], (name, state)
    assert all(button["height"] >= 44 and button["nowrap"] == "nowrap" for button in state["buttons"]), (name, state)
    before = page.locator("#ui-actions").bounding_box()
    page.locator("#ui-body").evaluate("el => el.scrollTop = el.scrollHeight")
    after = page.locator("#ui-actions").bounding_box()
    assert abs(before["y"] - after["y"]) <= 1, "Scrolling fields must not move the save action"


@pytest.mark.parametrize("width,height,theme", [(1440, 960, "light"), (1440, 960, "dark"),
    (768, 1024, "light"), (768, 1024, "dark"), (375, 812, "light"), (375, 812, "dark"), (812, 375, "light")])
def test_workbench_ios_editor_layout(ui, width, height, theme):
    page, errors = ui
    page.evaluate("document.documentElement.dataset.fixtureEditors = 'true'")
    page.locator(f'[data-theme-choice="{theme}"]').click()
    page.set_viewport_size({"width": width, "height": height})
    for name in [*EDITORS, "fingerprint"]:
        open_legacy_editor(page, name)
        assert_editor_layout(page, name)
        capture(page, f"ios-editor-{name}-{theme}-{width}-bottom")
        page.locator("#ui-body").evaluate("el => el.scrollTop = 0")
        if width in (375, 1440):
            capture(page, f"ios-editor-{name}-{theme}-{width}")
        page.keyboard.press("Escape")
        page.locator("#uimodal").wait_for(state="hidden")
    assert page.locator("html").get_attribute("data-fixture-edit-count") is None, "Opening/canceling editors must not write"
    assert errors == []


def test_workbench_ios_editor_save_contracts(ui):
    page, errors = ui
    page.evaluate("document.documentElement.dataset.fixtureEditors = 'true'")
    for index, (name, (_, _, field_id, key, path)) in enumerate(EDITORS.items(), 1):
        open_legacy_editor(page, name)
        value = f"已修改的内容{index}"
        page.locator("#" + field_id).fill(value)
        if name == "notifications":
            page.locator("#ec-config").fill('{"key":"********","device_key":"new-fixture-key"}')
        page.locator("#ui-ok").click()
        page.locator("#uimodal").wait_for(state="hidden")
        assert page.locator("html").get_attribute("data-fixture-edit-path") == path
        posted = json.loads(page.locator("html").get_attribute("data-fixture-edit-body"))
        assert posted[key] == ([value] if key in ("keywords", "include_keywords") else value), (name, posted)
        if name == "notifications":
            assert posted["config"] == {"key": "********", "device_key": "new-fixture-key"}
        if name == "rules":
            assert posted["require_review"] is True and posted["account_id"] == 1
        if name == "publish":
            assert posted["scheduled_at"] is None
        assert int(page.locator("html").get_attribute("data-fixture-edit-count")) == index
        # The real list refresh returns the fixture's committed value, not optimistic UI.
        page.wait_for_timeout(140)
        open_legacy_editor(page, name)
        assert page.locator("#" + field_id).input_value() == value
        page.locator('#ui-actions button').filter(has_text="取消").click()
        page.locator("#uimodal").wait_for(state="hidden")
    assert errors == []


def test_workbench_ios_editor_retry_validation_and_fingerprint(ui):
    page, errors = ui
    page.emulate_media(reduced_motion="no-preference")
    page.evaluate("document.documentElement.dataset.fixtureEditors = 'true'")
    open_legacy_editor(page, "monitors")
    page.locator("#em-alias").fill("失败后仍保留的别名")
    page.evaluate("Object.assign(document.documentElement.dataset, {fixtureEditFail:'true',fixtureEditSlow:'true'})")
    width = page.locator("#ui-ok").bounding_box()["width"]
    page.locator("#ui-ok").click()
    page.locator("#ui-ok").evaluate("el=>el.click()")
    assert page.locator("#uimodal").get_attribute("aria-busy") == "true"
    assert page.locator("#ui-body").evaluate("el=>el.inert")
    assert abs(page.locator("#ui-ok").bounding_box()["width"] - width) <= 1
    page.keyboard.press("Escape")
    assert page.locator("#uimodal").is_visible()
    page.wait_for_function("document.querySelector('#ui-feedback').dataset.tone === 'error'")
    assert page.locator("#em-alias").input_value() == "失败后仍保留的别名"
    assert page.locator("html").get_attribute("data-fixture-edit-count") == "1"
    assert not page.locator(".toast.err").count()
    capture(page, "ios-editor-failed-save")
    page.evaluate("delete document.documentElement.dataset.fixtureEditFail; delete document.documentElement.dataset.fixtureEditSlow")
    page.context.set_offline(True)
    page.locator("#ui-ok").click()
    page.wait_for_function("document.querySelector('#ui-feedback').textContent.includes('离线')")
    assert page.locator("html").get_attribute("data-fixture-edit-count") == "1"
    page.context.set_offline(False)
    page.wait_for_timeout(250)
    assert page.locator("html").get_attribute("data-fixture-edit-count") == "1", "Reconnect must not replay an edit"
    page.locator("#ui-ok").click()
    page.locator("#uimodal").wait_for(state="hidden")
    assert page.locator("html").get_attribute("data-fixture-edit-count") == "2"

    open_legacy_editor(page, "notifications")
    page.locator("#ec-config").fill("{invalid")
    page.locator("#ui-ok").click()
    assert page.locator("#uimodal").is_visible()
    assert page.locator("#ec-config").input_value() == "{invalid"
    assert page.locator("#ec-config").get_attribute("aria-invalid") == "true"
    assert page.evaluate("document.activeElement.id") == "ec-config"
    assert page.locator("html").get_attribute("data-fixture-edit-count") == "2"
    page.locator("#ec-config").fill("{}")
    assert page.locator("#ec-config").get_attribute("aria-invalid") is None
    page.keyboard.press("Escape")
    page.locator("#uimodal").wait_for(state="hidden")

    open_legacy_editor(page, "fingerprint")
    page.locator("#fp-tab-basic").focus()
    page.keyboard.press("ArrowRight")
    assert page.locator("#fp-tab-advanced").get_attribute("aria-selected") == "true"
    page.keyboard.press("ArrowLeft")
    assert page.locator("#fp-tab-basic").get_attribute("aria-selected") == "true"
    page.locator('[data-fp-mode="viewport"] button[data-value="custom"]').click()
    page.locator("#fp-edit-vw").fill("1440")
    page.locator("#fp-edit-vh").fill("900")
    page.locator("#ui-ok").click()
    page.locator("#uimodal").wait_for(state="hidden")
    posted = json.loads(page.locator("html").get_attribute("data-fixture-edit-body"))
    assert posted["viewport_w"] == 1440 and posted["viewport_h"] == 900 and posted["viewport_mode"] == "custom"
    assert page.locator("html").get_attribute("data-fixture-edit-count") == "3"
    assert errors == []


def test_workbench_ios_editor_repost_flow(ui):
    page, errors = ui
    page.evaluate("document.documentElement.dataset.fixtureEditors = 'true'")
    navigate(page, "monitors")
    page.get_by_role("tab", name="作品记录", exact=True).click()
    for width, theme in [(1440, "light"), (1440, "dark"), (375, "light"), (375, "dark")]:
        page.set_viewport_size({"width": width, "height": 960 if width == 1440 else 812})
        page.locator(f'[data-theme-choice="{theme}"]').click()
        page.locator('[data-panel="monitors"] button').filter(has_text="转发").first.click()
        page.locator("#ui-ok").click()
        page.locator("#repost").wait_for(state="visible")
        page.wait_for_timeout(120)
        assert page.locator("#rp-body").evaluate("el=>el.scrollWidth <= el.clientWidth+1")
        actions = page.locator("#rp-actions").bounding_box()
        assert actions["y"] + actions["height"] <= page.viewport_size["height"]
        page.locator("#rp-body").evaluate("el=>el.scrollTop=el.scrollHeight")
        assert page.locator("#rp-actions").bounding_box() == actions
        page.locator("#rp-body").evaluate("el=>el.scrollTop=0")
        capture(page, f"ios-editor-repost-{theme}-{width}")
        if width == 375 and theme == "dark":
            page.locator("#rp-title").fill("重新整理的标题")
            page.evaluate("document.documentElement.dataset.fixtureEditFail='true'")
            page.locator("#rp-submit").click()
            page.locator("#rp-submit").evaluate("el=>el.click()")
            page.keyboard.press("Escape")
            assert page.locator("#repost").is_visible()
            page.wait_for_function("document.querySelector('#rp-msg').dataset.tone === 'error'")
            assert page.locator("#rp-title").input_value() == "重新整理的标题"
            assert page.locator("html").get_attribute("data-fixture-repost-count") == "1"
            assert not page.locator(".toast.err").count()
            page.evaluate("delete document.documentElement.dataset.fixtureEditFail")
            page.locator("#rp-submit").click()
            page.locator("#repost").wait_for(state="hidden")
            posted = json.loads(page.locator("html").get_attribute("data-fixture-repost-body"))
            assert posted["title"] == "重新整理的标题" and posted["scheduled_at"] is None
            assert page.locator("html").get_attribute("data-fixture-repost-count") == "2"
        else:
            page.keyboard.press("Escape")
            page.locator("#repost").wait_for(state="hidden")
    assert errors == []


def test_workbench_ios_editor_dm_draft(ui):
    page, errors = ui
    page.evaluate("document.documentElement.dataset.fixtureEditors = 'true'")
    page.locator('[data-pf="xhs"]').click()
    navigate(page, "hub")
    page.locator('[data-hubtab="dm"]').click()
    page.locator("#dm-auto-panel > summary").click()
    page.locator('#dm-auto-tasks button').filter(has_text="编辑").click()
    page.locator("#uimodal").wait_for(state="visible")
    page.set_viewport_size({"width": 375, "height": 812})
    # The app's existing system theme listener is exercised rather than editing root styles.
    page.emulate_media(color_scheme="dark")
    assert_editor_layout(page, "dm-draft")
    capture(page, "ios-editor-dm-draft-dark-375")
    page.locator("#ui-inp").fill(" ")
    page.locator("#ui-ok").click()
    assert page.locator("#ui-inp").get_attribute("aria-invalid") == "true"
    assert page.locator("html").get_attribute("data-fixture-edit-count") is None
    page.locator("#ui-inp").fill("更新后的私信草稿")
    page.locator("#ui-ok").click()
    page.locator("#uimodal").wait_for(state="hidden")
    assert page.locator("html").get_attribute("data-fixture-edit-path") == "/api/account-actions/81"
    assert json.loads(page.locator("html").get_attribute("data-fixture-edit-body")) == {"content": "更新后的私信草稿"}
    assert page.locator("html").get_attribute("data-fixture-edit-count") == "1"
    assert errors == []


def test_workbench_motion_menus_sheets_and_nested_keyboard(ui):
    page, errors = ui
    page.emulate_media(reduced_motion="no-preference")
    navigate(page, "accounts")
    page.locator('[data-account-menu] button').first.click()
    menu = page.get_by_role("menu")
    assert menu.evaluate("el => getComputedStyle(el).animationName") == "wb-menu-enter"
    assert menu.locator('[role="menuitem"] svg[aria-hidden="true"]').count() == menu.get_by_role("menuitem").count()
    capture(page, "polish-account-menu")
    page.keyboard.press("End")
    page.keyboard.press("Enter")
    page.locator("#uimodal").wait_for(state="visible")
    page.wait_for_timeout(260)
    assert page.evaluate("!!document.activeElement.closest('#uimodal')"), "Menu exit must not steal dialog focus"
    page.locator('#uimodal button').filter(has_text="取消").click()
    page.locator("#uimodal").wait_for(state="hidden")
    navigate(page, "monitors")
    page.locator("#wb-create").click()
    sheet = page.locator('.wb-sheet[data-state="open"]')
    assert sheet.evaluate("el => getComputedStyle(el).animationName") == "wb-sheet-in"
    page.locator("#t-url").fill("退出动效期间保留的草稿")
    # A portal to body would be outside the Radix focus scope; verify real keys.
    control = sheet.locator(".cs-trg").nth(1)
    control.focus()
    page.keyboard.press("ArrowDown")
    page.locator(".wb-sheet .cs-panel").wait_for()
    page.wait_for_function("document.activeElement.getAttribute('role') === 'option'")
    page.keyboard.press("ArrowDown")
    page.keyboard.press("Enter")
    assert not page.locator(".cs-panel").count()
    assert control.evaluate("el => el === document.activeElement")
    page.keyboard.press("ArrowDown")
    page.wait_for_function("document.activeElement.getAttribute('role') === 'option'")
    page.keyboard.press("Escape")
    assert not page.locator(".cs-panel").count()
    assert sheet.is_visible(), "First Escape closes only the nested popup"
    capture(page, "polish-monitor-sheet")
    page.keyboard.press("Escape")
    closing = page.locator('.wb-sheet[data-state="closed"]')
    assert closing.evaluate("el => getComputedStyle(el).animationName") == "wb-sheet-out"
    assert page.locator("#t-url").input_value() == "退出动效期间保留的草稿"
    assert page.locator("#t-url").is_visible(), "Keep the actual form for the short exit, not an empty sheet"
    closing.wait_for(state="detached")
    page.wait_for_function("document.activeElement.id === 'wb-create'")
    page.locator("#wb-create").click()
    assert page.locator("#t-url").input_value() == "退出动效期间保留的草稿"
    page.keyboard.press("Escape")
    page.locator(".wb-sheet").wait_for(state="detached")
    assert errors == []


def test_workbench_motion_interruptions_markers_and_mobile(ui):
    page, errors = ui
    page.emulate_media(reduced_motion="no-preference")
    navigate(page, "settings")
    # Rapid state changes cancel/rebase their marker; no animation controls state.
    page.evaluate("""async () => {
      for (const theme of ['dark', 'light', 'dark']) {
        document.querySelector(`[data-theme-choice="${theme}"]`).click();
        await new Promise(resolve => setTimeout(resolve, 35));
      }
    }""")
    assert page.locator("html").get_attribute("data-theme") == "dark"
    page.wait_for_timeout(280)
    assert page.locator(".theme-switch").evaluate("""el => {
      const a = el.querySelector('[aria-pressed="true"]').getBoundingClientRect();
      const b = el.querySelector('.wb-selection-marker').getBoundingClientRect();
      return Math.abs(a.left-b.left) < 1 && Math.abs(a.width-b.width) < 1;
    }""")
    page.get_by_role("tab", name="下载设置", exact=True).click()
    page.get_by_role("tab", name="AI 文案", exact=True).click()
    page.get_by_role("tab", name="外观与体验", exact=True).click()
    page.wait_for_timeout(260)
    assert page.locator('[data-panel="settings"] .wb-tablist').evaluate("""el => {
      const a = el.querySelector('[data-state="active"]').getBoundingClientRect();
      const b = el.querySelector('.wb-marker-line').getBoundingClientRect();
      return Math.abs(a.left-b.left) < 1 && Math.abs(a.width-b.width) < 1;
    }""")
    # Change the in-page preference during an actual running animation.
    page.locator('[data-theme-choice="light"]').click()
    page.locator("#appearance-motion").check()
    assert page.evaluate("[...document.querySelectorAll('.wb-selection-marker')].every(el => !el.getAnimations().length)")
    page.locator("#appearance-motion").uncheck()
    page.set_viewport_size({"width": 375, "height": 812})
    page.locator("#nav-toggle").click()
    page.wait_for_timeout(250)
    capture(page, "polish-mobile-navigation")
    page.keyboard.press("Escape")
    assert page.locator("#nav-toggle").get_attribute("aria-expanded") == "false"
    page.set_viewport_size({"width": 1440, "height": 1000})
    page.wait_for_function("!document.body.classList.contains('nav-closing') && !document.getElementById('main-sidebar').inert")
    assert page.locator("#main-content").evaluate("el => !el.inert")
    # The OS preference also cancels an exit and releases all inert nodes.
    page.set_viewport_size({"width": 375, "height": 812})
    page.locator("#nav-toggle").click()
    page.keyboard.press("Escape")
    page.emulate_media(reduced_motion="reduce")
    page.wait_for_function("!document.body.classList.contains('nav-closing')")
    assert_no_overflow(page, "polish-mobile-reduced")
    page.locator('[data-theme-choice="dark"]').click()
    capture(page, "polish-settings-mobile-dark")
    assert errors == []


def test_workbench_busy_feedback_does_not_shift_or_repeat(ui):
    page, errors = ui
    page.emulate_media(reduced_motion="no-preference")
    navigate(page, "accounts")
    page.evaluate("document.documentElement.dataset.fixtureRead = 'slow'")
    refresh = page.get_by_role("button", name="刷新当前页面", exact=True)
    before = refresh.bounding_box()
    refresh.click()
    assert refresh.get_attribute("aria-busy") == "true"
    assert refresh.is_disabled()
    assert refresh.bounding_box()["width"] == before["width"]
    refresh.evaluate("el => el.click()")  # Disabled native buttons do not dispatch.
    page.wait_for_function("document.querySelector('.wb-refresh-feedback').textContent === '已刷新'")
    assert page.locator("html").get_attribute("data-fixture-read-count") == "1"
    assert refresh.get_attribute("data-feedback") == "success"
    page.evaluate("document.documentElement.dataset.fixtureRead = 'fail'")
    refresh.click()
    page.wait_for_function("document.querySelector('.wb-refresh-feedback').textContent === '刷新未完成'")
    assert refresh.get_attribute("data-feedback") is None
    assert page.locator(".wb-connection").is_visible()
    page.evaluate("delete document.documentElement.dataset.fixtureRead")
    navigate(page, "collections")
    page.locator("#wb-create").click()
    page.locator("#col-keywords").fill("交互测试")
    page.evaluate("Object.assign(document.documentElement.dataset, {fixtureSlowWrite: '1', fixtureWrite: 'fail'})")
    submit = page.locator('[data-composer="collections"] button[onclick="createCollection()"]')
    before = submit.bounding_box()
    submit.click()
    assert submit.get_attribute("aria-busy") == "true"
    assert abs(submit.bounding_box()["width"] - before["width"]) <= 1
    submit.evaluate("el => el.click()")
    page.keyboard.press("Escape")
    assert page.locator('.wb-sheet[data-state="open"]').is_visible(), "Submitting sheet stays open"
    page.wait_for_function("!document.querySelector('[data-composer=collections] [aria-busy=true]')")
    assert page.locator("html").get_attribute("data-fixture-write-count") == "1"
    assert page.locator("#col-keywords").input_value() == "交互测试"
    assert page.locator('.wb-sheet[data-state="open"]').is_visible()
    assert page.locator('.wb-sheet-status[role="alert"]').is_visible()
    assert not page.locator('.toast.err').count(), "Inline composer feedback must not cover the retry control"
    capture(page, "polish-failed-submit")
    assert errors == []


def test_workbench_calendar_touch_and_motion_cleanup(ui):
    page, errors = ui
    page.emulate_media(reduced_motion="no-preference")
    page.set_viewport_size({"width": 375, "height": 812})
    navigate(page, "publish", mobile=True)
    control = page.locator("#pub-when + .dt-trg")
    control.click()
    calendar = page.locator(".dt-panel")
    calendar.wait_for()
    assert calendar.evaluate("""el => {
      const r = el.getBoundingClientRect();
      return r.left >= 0 && r.right <= innerWidth && r.top >= 0 && r.bottom <= innerHeight;
    }""")
    assert calendar.locator("button.dt-day").first.bounding_box()["height"] >= 44
    assert calendar.locator("button.dt-day").first.bounding_box()["width"] >= 44
    page.keyboard.press("ArrowRight")
    assert page.evaluate("document.activeElement.classList.contains('dt-day')"), page.evaluate("({active: document.activeElement.outerHTML.slice(0,500), calendar: !!document.querySelector('.dt-panel')})")
    calendar.evaluate("el => Promise.allSettled(el.getAnimations().map(animation => animation.finished))")
    capture(page, "polish-touch-calendar")
    page.keyboard.press("Escape")
    assert not page.locator(".dt-panel").count()
    assert control.evaluate("el => el === document.activeElement")
    assert not page.locator("#pub-when").input_value(), "Browsing a calendar does not commit a schedule"
    control.click()
    page.locator('.dt-panel [data-act="ok"]').click()
    assert "T" in page.locator("#pub-when").input_value()
    assert control.evaluate("el => el === document.activeElement")
    control.click()
    page.locator('.dt-panel [data-act="clear"]').click()
    assert not page.locator("#pub-when").input_value()
    assert control.evaluate("el => el === document.activeElement")
    navigate(page, "monitors", mobile=True)
    page.locator("#wb-create").click()
    page.locator("#t-url").fill("减少动态效果仍保留草稿")
    page.keyboard.press("Escape")
    page.emulate_media(reduced_motion="reduce")
    page.locator(".wb-sheet").wait_for(state="detached")
    page.wait_for_function("document.activeElement.id === 'wb-create'")
    assert not page.locator("body").get_attribute("data-scroll-locked")
    page.locator("#wb-create").click()
    assert page.locator("#t-url").input_value() == "减少动态效果仍保留草稿"
    page.keyboard.press("Escape")
    assert errors == []


def assert_reference_logo(page, platform):
    reference = LEGACY_BRAND["platforms"][platform]["logo"]
    mark = page.locator(".brand-mark")
    assert mark.is_visible(), "The original mark must not disappear on touch screens"
    state = mark.evaluate("""el => {
      const s=getComputedStyle(el);
      return {background:s.backgroundImage, radius:s.borderRadius, shadow:s.boxShadow,
        iconWidth:getComputedStyle(el.querySelector('svg')).width,
        symbol:el.querySelector('use').getAttribute('href'), width:s.width, height:s.height};
    }""")
    for key in ("background", "radius", "shadow", "iconWidth"):
        assert state[key] == reference[key], (platform, key, state[key], reference[key])
    assert state["symbol"] == "#i-brand"
    assert state["width"] == state["height"] == ("32px" if page.viewport_size["width"] <= 400 else "34px")


@pytest.mark.parametrize("width", [1440, 375, 320])
def test_workbench_platform_brand_colors_and_theme_independence(ui, width):
    page, errors = ui
    page.set_viewport_size({"width": width, "height": 960 if width > 860 else 812})
    navigate(page, "accounts", mobile=width <= 860)
    colors = {
        "douyin": {"light": "rgb(199, 31, 70)", "dark": "rgb(255, 102, 135)",
                   "solid": "rgb(217, 28, 69)", "hover": "rgb(195, 22, 62)"},
        "xhs": {"light": "rgb(197, 27, 50)", "dark": "rgb(255, 117, 138)",
                "solid": "rgb(219, 25, 57)", "hover": "rgb(191, 21, 50)"},
    }
    for theme in ("light", "dark"):
        page.locator(f'[data-theme-choice="{theme}"]').click()
        states = {}
        # Return to Douyin to catch inherited/stale soft colors after switching.
        for platform in ("douyin", "xhs", "douyin"):
            expected = colors[platform]
            page.locator(f'.pswitch [data-pf="{platform}"]').click()
            page.wait_for_function("""expected => {
              const style = selector => getComputedStyle(document.querySelector(selector));
              return style('.pswitch .active').color === expected.accent &&
                style('.navitem.active .nav-label').color === expected.accent &&
                style('.wb-add-account').backgroundColor === expected.solid;
            }""", arg={"accent": expected[theme], "solid": expected["solid"]})
            assert page.locator("html").get_attribute("data-theme-mode") == theme
            assert page.locator("html").get_attribute("data-theme") == theme
            state = page.evaluate("""() => {
              const s = getComputedStyle(document.body);
              return { semantic: ['--info','--success','--warn','--danger']
                .map(token => s.getPropertyValue(token).trim()),
                background: s.getPropertyValue('--bg').trim(),
                selected: getComputedStyle(document.querySelector('.navitem.active')).backgroundColor,
                primaryText: getComputedStyle(document.querySelector('.wb-add-account')).color };
            }""")
            assert state["primaryText"] == "rgb(255, 255, 255)"
            assert_reference_logo(page, platform)
            assert page.locator('meta[name="theme-color"]').get_attribute("content") == state["background"]
            if width <= 400:
                controls = page.locator('.header-actions button, #nav-toggle').evaluate_all("""nodes => nodes.map(el => {
                  const r=el.getBoundingClientRect(); return {width:r.width,height:r.height,left:r.left,right:r.right};
                })""")
                assert all(c["width"] >= 44 and c["height"] >= 44 and c["left"] >= 0 and c["right"] <= width for c in controls), controls
            if platform in states:
                assert state == states[platform], "Returning to a platform must restore all derived colors"
            states[platform] = state
            assert_no_overflow(page, f"brand/{platform}/{theme}/{width}")
            page.locator(f'#acc-table [data-account-id="{1 if platform == "douyin" else 2}"]').wait_for(state="visible")
            page.locator("#busy-spinner.on").wait_for(state="hidden")
            capture(page, f"brand-{platform}-{theme}-{width}")
            if width > 860:
                page.locator(".wb-add-account").hover()
                page.wait_for_function("color => getComputedStyle(document.querySelector('.wb-add-account')).backgroundColor === color", arg=expected["hover"])
                assert page.locator(".wb-add-account").evaluate("el => getComputedStyle(el).color") == "rgb(255, 255, 255)"
                page.mouse.move(0, 0)
        assert states["douyin"]["semantic"] == states["xhs"]["semantic"]
        assert states["douyin"]["background"] != states["xhs"]["background"]
        assert states["douyin"]["selected"] != states["xhs"]["selected"]
        # A reload keeps the user's theme and the platform-specific accent.
        page.reload(wait_until="networkidle")
        assert page.locator("body").evaluate("el => getComputedStyle(el).getPropertyValue('--acc').trim()") == (
            "#c71f46" if theme == "light" else "#ff6687")
        assert page.locator("html").get_attribute("data-theme-mode") == theme
    assert errors == []


def test_all_platform_pages_in_both_themes(ui):
    page, errors = ui
    visited = 0
    for theme in ("light", "dark"):
        page.locator(f'[data-theme-choice="{theme}"]').click()
        for platform in ("douyin", "xhs", "kuaishou", "shipinhao"):
            page.locator(f'.pswitch [data-pf="{platform}"]').click()
            assert page.locator("html").get_attribute("data-theme") == theme
            assert_reference_logo(page, platform)
            pages = page.locator('.navitem:not(.hidden)').evaluate_all("nodes => nodes.map(n => n.dataset.tab)")
            for name in pages:
                navigate(page, name)
                assert_no_overflow(page, f"{theme}/{platform}/{name}")
                if name == "accounts":
                    assert page.locator(".wb-add-account").evaluate("el => getComputedStyle(el).color") == "rgb(255, 255, 255)", (theme, platform)
                if name == "overview":
                    page.locator("#overview-chart .bar").first.wait_for(state="visible")
                    series = page.evaluate("""() => ({
                      bars: [...document.querySelectorAll('#overview-chart .bar')].slice(0, 2).map(el => getComputedStyle(el).fill),
                      legend: ['.lg-a', '.lg-b'].map(selector => getComputedStyle(document.querySelector(selector)).backgroundColor)
                    })""")
                    assert series["bars"] == series["legend"], (theme, platform, series)
                    assert series["bars"][0] != series["bars"][1], (theme, platform, series)
                background = page.locator("body").evaluate("el => getComputedStyle(el).backgroundColor")
                expected_background = ("rgb(251, 243, 241)" if theme == "light" else "rgb(23, 18, 20)") if platform == "xhs" else (
                    "rgb(245, 245, 247)" if theme == "light" else "rgb(9, 11, 16)")
                assert background == expected_background
                visited += 1
            if platform == "douyin":
                navigate(page, "settings")
                capture(page, f"desktop-settings-{theme}")
                navigate(page, "overview")
                capture(page, f"desktop-overview-{theme}")
    assert visited >= 80
    assert errors == []


def test_preference_persistence_system_and_keyboard(ui):
    page, errors = ui
    page.locator('[data-theme-choice="dark"]').click()
    page.reload(wait_until="networkidle")
    assert page.locator("html").get_attribute("data-theme") == "dark"
    page.emulate_media(color_scheme="light")
    assert page.locator("html").get_attribute("data-theme") == "dark"
    page.locator('[data-theme-choice="system"]').click()
    page.wait_for_function("document.documentElement.dataset.theme === 'light'")
    page.emulate_media(color_scheme="dark")
    page.wait_for_function("document.documentElement.dataset.theme === 'dark'")
    page.keyboard.press("Control+k")
    assert page.locator("#command-dialog").evaluate("el => el.open")
    page.locator("#command-search").fill("设置")
    page.keyboard.press("Enter")
    assert page.locator(".navitem.active").get_attribute("data-tab") == "settings"
    page.locator('input[name="appearance-density"][value="compact"]').check()
    page.locator("#appearance-motion").check()
    page.locator('input[name="appearance-theme"][value="light"]').check()
    assert page.locator('[data-theme-choice="light"]').get_attribute("aria-pressed") == "true"
    page.reload(wait_until="networkidle")
    assert page.locator("html").get_attribute("data-density") == "compact"
    assert page.locator("html").get_attribute("data-motion") == "reduced"
    assert page.locator("html").get_attribute("data-theme") == "light"
    # Validation is associated with its field and clears without losing helper IDs.
    page.get_by_role("tab", name="AI 文案", exact=True).click()
    page.locator("#ai-base").fill("invalid-url")
    page.locator("#ai-model").focus()
    assert page.locator("#ai-base").get_attribute("aria-describedby") == "ai-base-error"
    assert page.locator("#ai-base-error").is_visible()
    page.locator("#ai-base").fill("https://example.invalid/v1")
    page.locator("#ai-model").focus()
    assert page.locator("#ai-base").get_attribute("aria-invalid") is None
    assert page.locator("#ai-base-error").count() == 0
    page.locator("#command-trigger").click()
    page.locator("#command-search").fill("not-a-feature")
    assert page.locator(".command-empty").is_visible()
    page.keyboard.press("Escape")
    page.wait_for_function("document.activeElement === document.getElementById('command-trigger')")
    # OS changes are synchronized across tabs only when auto is selected.
    second = page.context.new_page()
    second.goto(page.url, wait_until="networkidle")
    second.locator('[data-theme-choice="dark"]').click()
    page.wait_for_function("document.documentElement.dataset.theme === 'dark'")
    second.close()
    assert errors == []


def test_workbench_lists_drafts_menus_and_back(ui):
    page, errors = ui
    navigate(page, "monitors")
    page.locator("#mon-table tr").first.wait_for()
    assert page.locator("#mon-table").bounding_box()["y"] < 480
    assert not page.locator("#t-url").is_visible()
    page.locator("#wb-create").click()
    page.locator("#t-url").fill("尚未提交的目标")
    assert page.get_by_role("dialog").filter(has=page.locator("#t-url")).is_visible()
    capture(page, "workbench-monitor-sheet")
    page.keyboard.press("Escape")
    page.wait_for_function("document.activeElement.id === 'wb-create'")
    page.locator("#wb-create").click()
    assert page.locator("#t-url").input_value() == "尚未提交的目标"
    page.get_by_role("button", name="返回列表", exact=True).click()
    page.locator('#mon-search').fill("not-a-match")
    assert page.locator("#mon-table .empty").is_visible()
    navigate(page, "accounts")
    page.locator('#acc-table tr[data-account-id]').first.wait_for()
    assert page.locator("#acc-table").evaluate("el => el.scrollWidth <= el.clientWidth + 2")
    page.locator("[data-account-detail]").first.click()
    assert page.locator(".wb-account-inspector").is_visible()
    assert page.get_by_role("button", name="上一个账号").is_disabled()
    page.keyboard.press("Escape")
    page.wait_for_function("document.activeElement.hasAttribute('data-account-detail')")
    page.locator("[data-account-menu] button").first.click()
    page.keyboard.press("End")
    assert page.get_by_role("menuitem", name="删除", exact=True).evaluate("el => el === document.activeElement")
    capture(page, "workbench-account-menu")
    page.keyboard.press("Enter")
    page.locator("#uimodal").wait_for(state="visible")
    page.keyboard.press("Escape")
    assert not page.locator("#uimodal").is_visible()
    page.locator("#wb-account-search").fill("nothing-matches")
    assert page.get_by_role("button", name="清除搜索", exact=True).is_visible()
    page.get_by_role("button", name="清除搜索", exact=True).click()
    page.go_back()
    assert page.locator('.navitem.active').get_attribute('data-tab') == "monitors"
    assert page.locator('#mon-search').input_value() == "not-a-match"
    assert errors == []


def test_workbench_real_response_states_and_create_loop(ui):
    page, errors = ui
    page.locator('[data-overview-state="failed"]').wait_for()
    page.wait_for_function("document.querySelector('[data-overview-state=failed]').textContent === '2'")
    page.locator('.wb-attention-row[data-tone="danger"]').click()
    assert page.locator('#queue-state').input_value() == "failed"
    assert page.locator('#queue-platform').input_value() == "current"
    page.locator('#queue-table').get_by_text('测试失败任务', exact=True).wait_for()
    navigate(page, "collections")
    page.locator('#wb-create').click()
    page.locator('#col-keywords').fill("本地测试关键词")
    page.locator('#col-account').select_option('1', force=True)
    page.evaluate("document.documentElement.dataset.fixtureWrite = 'fail'")
    page.get_by_role('button', name='开始批量采集', exact=True).click()
    page.locator('#col-create-msg').get_by_text('创建失败：测试：暂时忙碌，请稍后重试', exact=True).wait_for()
    assert page.locator('#col-keywords').input_value() == "本地测试关键词"
    assert page.locator('.wb-sheet').is_visible()
    page.evaluate("document.documentElement.dataset.fixtureWrite = 'ok'")
    page.get_by_role('button', name='开始批量采集', exact=True).click()
    page.locator('.wb-sheet').wait_for(state='hidden')
    page.locator('#collection-job-table').get_by_text('本地测试关键词', exact=True).wait_for()
    assert page.locator('html').get_attribute('data-fixture-write-count') == '2'
    page.locator('#collection-job-table button', has_text='查看结果').first.click()
    assert page.locator('#collection-results-card').is_visible()
    assert not page.locator('#collection-job-table').is_visible()
    page.get_by_role('button', name='返回任务列表', exact=True).click()
    assert page.locator('#collection-job-table').is_visible()
    navigate(page, "accounts")
    page.locator('#acc-table [data-account-detail]').first.wait_for()
    page.evaluate("document.documentElement.dataset.fixtureRead = 'fail'")
    page.get_by_role('button', name='刷新当前页面', exact=True).click()
    page.locator('.wb-connection').wait_for(state='visible')
    assert page.locator('#acc-table [data-account-detail]').first.is_visible()
    capture(page, 'workbench-read-error')
    page.evaluate("document.documentElement.dataset.fixtureRead = 'ok'")
    page.get_by_role('button', name='重新加载', exact=True).click()
    page.locator('.wb-connection').wait_for(state='hidden')
    page.context.set_offline(True)
    page.locator('.wb-connection').wait_for(state='visible')
    assert page.get_by_role('button', name='重新加载', exact=True).is_disabled()
    page.context.set_offline(False)
    page.locator('.wb-connection').wait_for(state='hidden')
    assert page.locator('html').get_attribute('data-fixture-write-count') == '2'
    assert errors == []


def test_workbench_preview_tabs_mobile_and_reduced_motion(ui):
    page, errors = ui
    navigate(page, 'publish')
    page.locator('#pub-title').fill('一次本地内容预览')
    page.locator('#pub-desc').fill('正文与素材仅在本地预览，不会自动提交。')
    page.locator('.wb-preview-copy').get_by_text('一次本地内容预览', exact=True).wait_for()
    # Tiny locally generated SVG files; no upload is performed by the preview.
    page.locator('#pub-files').set_input_files([
        {"name": "first.svg", "mimeType": "image/svg+xml", "buffer": b'<svg xmlns="http://www.w3.org/2000/svg" width="80" height="80"><rect width="80" height="80" fill="red"/></svg>'},
        {"name": "second.svg", "mimeType": "image/svg+xml", "buffer": b'<svg xmlns="http://www.w3.org/2000/svg" width="80" height="80"><rect width="80" height="80" fill="blue"/></svg>'},
    ])
    page.locator('.wb-preview-media img[alt="first.svg"]').wait_for()
    page.get_by_role('button', name='下一张素材', exact=True).click()
    assert page.locator('.wb-preview-media img').get_attribute('alt') == 'second.svg'
    page.get_by_role('tab', name='发布记录', exact=True).click()
    assert not page.locator('#pub-title').is_visible()
    page.get_by_role('tab', name='撰写内容', exact=True).click()
    assert page.locator('#pub-title').input_value() == '一次本地内容预览'
    capture(page, 'workbench-publish-preview')
    for width in (768, 375):
        page.set_viewport_size({"width": width, "height": 900})
        for theme in ('light', 'dark'):
            page.locator(f'[data-theme-choice="{theme}"]').click()
            navigate(page, 'monitors', mobile=True)
            page.locator('#wb-create').click()
            assert page.locator('.wb-sheet').bounding_box()['width'] <= width
            assert page.locator('.wb-sheet').evaluate('el => el.scrollWidth <= el.clientWidth + 2')
            assert page.locator('.wb-sheet').evaluate('el => parseFloat(getComputedStyle(el).animationDuration) * 1000') <= .001
            page.get_by_role('button', name='返回列表', exact=True).click()
            if not page.locator('.wb-filter-details').first.get_attribute('open') == "":
                page.locator('.wb-filter-details > summary').first.click()
            assert page.locator('#mon-group').locator('..').is_visible()
            assert_no_overflow(page, f'workbench-{width}-{theme}')
            navigate(page, 'accounts', mobile=True)
            capture(page, f'workbench-{width}-accounts-{theme}')
            account_bounds = page.locator('#acc-table').evaluate('''el => ({width:el.clientWidth, scroll:el.scrollWidth,
              wide:[...el.querySelectorAll('*')].filter(n=>n.getBoundingClientRect().right > el.getBoundingClientRect().right+2).map(n=>({tag:n.tagName,cls:n.className,text:n.textContent.slice(0,30),w:n.getBoundingClientRect().width}))})''')
            assert account_bounds['scroll'] <= account_bounds['width'] + 2, account_bounds
    assert page.locator('html').get_attribute('data-fixture-write-count') is None
    assert errors == []


def test_workbench_other_composers_and_settings_draft(ui):
    page, errors = ui
    for tab, field, account, action in (
        ('monitors', 't-url', 't-acc', '开始监控'),
        ('comments', 'w-url', 'w-acc', '开始监控评论'),
        ('danmaku', 'd-w-url', 'd-w-acc', '开始监控弹幕'),
        ('autocomment', 'ac-templates', 'ac-acc', '创建规则(默认关闭)'),
        ('notifications', 'n-name', None, '添加通知渠道'),
    ):
        navigate(page, tab)
        page.locator('#wb-create').click()
        page.keyboard.press('Control+k')
        assert not page.locator('#command-dialog').evaluate('el => el.open')
        page.keyboard.press('Shift+Tab')
        assert page.locator('.wb-sheet').evaluate('el => el.contains(document.activeElement)')
        page.locator('#' + field).fill('本地交互测试')
        if account:
            page.locator('#' + account).select_option('1', force=True)
        if tab == 'notifications':
            page.locator('#n-config').fill('{')
            page.get_by_role('button', name=action, exact=True).click()
            assert page.locator('#n-config').get_attribute('aria-invalid') == 'true'
            page.locator('#n-config').fill('{"key":"fixture"}')
        page.get_by_role('button', name=action, exact=True).click()
        page.locator('.wb-sheet').wait_for(state='hidden')
        page.wait_for_function("document.activeElement.id === 'wb-create'")
    # A reconnect/read retry must not replace a user's unsaved settings.
    navigate(page, 'settings')
    page.get_by_role('tab', name='外观与体验', exact=True).focus()
    page.keyboard.press('ArrowRight')
    assert page.get_by_role('tab', name='下载设置', exact=True).get_attribute('aria-selected') == 'true'
    page.locator('#dl-dir').fill('D:/fixture/unsaved')
    page.context.set_offline(True)
    page.locator('.wb-connection').wait_for(state='visible')
    page.context.set_offline(False)
    page.locator('.wb-connection').wait_for(state='hidden')
    assert page.locator('#dl-dir').input_value() == 'D:/fixture/unsaved'
    assert errors == []


def test_responsive_navigation_and_dialogs(ui):
    page, errors = ui
    for width in (768, 375):
        page.set_viewport_size({"width": width, "height": 900})
        for theme in ("light", "dark"):
            page.locator(f'[data-theme-choice="{theme}"]').click()
            for name in ("overview", "accounts", "monitors", "collections", "comments", "danmaku", "hub", "queue", "publish", "autocomment", "share-download", "notifications", "settings", "risk-control"):
                navigate(page, name, mobile=True)
                assert page.locator("#nav-toggle").get_attribute("aria-expanded") == "false"
                assert not page.locator("#main-content").evaluate("el => el.inert")
                assert_no_overflow(page, f"{width}/{theme}/{name}")
            navigate(page, "settings", mobile=True)
            capture(page, f"{width}-settings-{theme}")
    page.locator("#nav-toggle").click()
    assert page.locator("#main-content").evaluate("el => el.inert")
    page.keyboard.press("Shift+Tab")
    assert page.locator('.navitem[data-tab="risk-control"]').evaluate("el => el === document.activeElement")
    page.keyboard.press("Tab")
    assert page.locator("#nav-close").evaluate("el => el === document.activeElement")
    capture(page, "mobile-navigation")
    page.keyboard.press("Escape")
    assert page.locator("#nav-toggle").evaluate("el => el === document.activeElement")
    page.locator("#command-trigger").click()
    page.keyboard.press("ArrowDown")
    assert page.locator(".command-result").first.evaluate("el => el === document.activeElement")
    capture(page, "mobile-search")
    page.keyboard.press("Escape")
    # Open a demo record's confirmation, then cancel: no task/API write.
    navigate(page, "monitors", mobile=True)
    page.locator('button[onclick^="delMon("]').first.click()
    page.wait_for_function("document.activeElement === document.querySelector('#ui-actions .ghost')")
    assert page.locator("#uimodal").is_visible()
    capture(page, "mobile-confirm")
    page.keyboard.press("Escape")
    assert not page.locator("#uimodal").is_visible()
    # Landscape and desktop resizing must release the drawer's focus/inert state.
    page.locator("#nav-toggle").click()
    page.set_viewport_size({"width": 1024, "height": 600})
    page.wait_for_function("!document.body.classList.contains('nav-open')")
    assert not page.locator("#main-content").evaluate("el => el.inert")
    assert_no_overflow(page, "landscape")
    assert errors == []
