"""Opt-in UI acceptance on the local demo; no real profiles, accounts or APIs."""
import json
import os

import pytest
from patchright.sync_api import expect

from test_web_appearance_browser import ui, navigate, capture, assert_no_overflow

pytestmark = [pytest.mark.local_cdp, pytest.mark.skipif(
    os.environ.get("CREATORHUB_RUN_LOCAL_CDP") != "1", reason="local UI acceptance is opt-in")]


def settings(page):
    navigate(page, "settings", mobile=page.viewport_size["width"] <= 860)
    page.get_by_role("tab", name="采集与运行", exact=True).click()
    expect(page.locator("#engine-settings-save")).to_be_enabled()


def mode(page, label="API 直连（兼容模式）"):
    page.get_by_role("button", name="小红书作品获取方式", exact=True).click()
    page.get_by_role("option", name=label, exact=True).click()


def reload_settings(page):
    count = int(page.locator("html").get_attribute("data-fixture-engine-load-done") or 0)
    page.evaluate("document.dispatchEvent(new Event('fixture-engine-load'))")
    expect(page.locator("html")).to_have_attribute("data-fixture-engine-load-done", str(count + 1))


def test_workbench_engine_settings_save_draft_and_recommended_values(ui):
    page, errors = ui
    settings(page)
    expect(page.locator("#engine-comment_recent_works")).to_have_value("5")
    assert not page.locator("#engine-settings-advanced").evaluate("el => el.open")
    assert not page.locator("#engine-settings-health").evaluate("el => el.open")
    mode(page)
    page.locator("#engine-comment_recent_works").fill("9")
    expect(page.locator("#engine-settings-status")).to_contain_text("未保存")
    navigate(page, "overview")
    settings(page)
    reload_settings(page)
    expect(page.locator("#engine-comment_recent_works")).to_have_value("9")
    expect(page.locator("#engine-xhs_read_mode")).to_have_value("api")
    page.locator("#engine-settings-health > summary").click()
    expect(page.locator("#engine-work_health_interval_seconds")).to_have_value("60")
    page.locator("#engine-work_health_interval_seconds").fill("90")
    page.locator("#engine-settings-save").click()
    expect(page.locator("#engine-settings-status")).to_contain_text("已保存")
    assert json.loads(page.locator("html").get_attribute("data-fixture-engine-body")) == {
        "xhs_read_mode": "api", "comment_recent_works": 9, "work_health_interval_seconds": 5400,
    }
    page.evaluate("document.dispatchEvent(new Event('fixture-engine-state'))")
    assert page.locator("html").get_attribute("data-fixture-engine-dirty") == "false"
    page.locator("#engine-settings-defaults").click()
    expect(page.locator("#engine-settings-status")).to_contain_text("点击保存后生效")
    expect(page.locator("#engine-xhs_read_mode")).to_have_value("browser")
    assert page.locator("html").get_attribute("data-fixture-engine-count") == "1"
    page.locator("#engine-settings-save").click()
    expect(page.locator("#engine-settings-status")).to_contain_text("已保存")
    reload_settings(page)
    expect(page.locator("#engine-comment_recent_works")).to_have_value("5")
    assert errors == []


def test_workbench_engine_settings_validation_and_failed_save(ui):
    page, errors = ui
    settings(page)
    page.locator("#engine-settings-advanced > summary").click()
    page.locator("#engine-download_timeout_seconds").fill("10")
    page.locator("#engine-settings-advanced > summary").click()
    page.locator("#engine-settings-save").click()
    summary = page.locator("#engine-settings-errors")
    expect(summary).to_be_focused()
    assert page.locator("html").get_attribute("data-fixture-engine-count") is None
    summary.get_by_role("link", name="下载等待上限（秒）", exact=False).click()
    expect(page.locator("#engine-download_timeout_seconds")).to_be_focused()
    expect(page.locator("#engine-download_timeout_seconds")).to_have_attribute("aria-invalid", "true")
    page.locator("#engine-download_timeout_seconds").fill("240")
    page.evaluate("document.documentElement.dataset.fixtureEngineWrite = 'fail'")
    page.locator("#engine-settings-save").click()
    expect(page.locator("#engine-settings-status")).to_contain_text("修改已保留")
    expect(page.locator("#engine-download_timeout_seconds")).to_have_value("240")
    reload_settings(page)
    expect(page.locator("#engine-download_timeout_seconds")).to_have_value("240")
    mode(page)
    page.evaluate("document.documentElement.dataset.fixtureEngineWrite = 'invalid'")
    page.locator("#engine-settings-save").click()
    expect(summary).to_be_focused()
    summary.get_by_role("link", name="小红书作品获取方式", exact=False).click()
    expect(page.get_by_role("button", name="小红书作品获取方式", exact=True)).to_be_focused()
    expect(page.get_by_role("button", name="小红书作品获取方式", exact=True)).to_have_attribute("aria-invalid", "true")
    page.evaluate("document.documentElement.dataset.fixtureEngineWrite = ''; document.documentElement.dataset.fixtureEngineSlow = '1'")
    page.locator("#engine-settings-save").click()
    expect(page.locator("#engine-settings-save")).to_be_disabled()
    expect(page.get_by_role("button", name="小红书作品获取方式", exact=True)).to_be_disabled()
    page.evaluate("document.dispatchEvent(new Event('fixture-engine-save')); document.dispatchEvent(new Event('fixture-engine-load'))")
    expect(page.locator("#engine-settings-status")).to_contain_text("已保存")
    assert page.locator("html").get_attribute("data-fixture-engine-count") == "3"
    assert errors == []


def test_workbench_engine_settings_read_failure_and_retry(ui):
    page, errors = ui
    page.goto(page.url.split("#", 1)[0] + "?engine-read-fail=1", wait_until="networkidle")
    navigate(page, "settings")
    page.get_by_role("tab", name="采集与运行", exact=True).click()
    expect(page.locator("#engine-settings-status")).to_contain_text("读取失败")
    expect(page.locator("#engine-settings-retry")).to_be_visible()
    expect(page.locator("#engine-settings-save")).to_be_disabled()
    expect(page.get_by_role("button", name="小红书作品获取方式", exact=True)).to_be_disabled()
    page.evaluate("document.documentElement.dataset.fixtureEngineRead = ''")
    page.locator("#engine-settings-retry").click()
    expect(page.locator("#engine-settings-status")).to_contain_text("已读取")
    expect(page.locator("#engine-settings-save")).to_be_enabled()
    expect(page.locator("#engine-settings-retry")).to_be_hidden()
    assert errors == []


@pytest.mark.parametrize("backfill,expected", [("", None), ("0", 0), ("5", 5)])
def test_workbench_engine_settings_monitor_inheritance_choice(ui, backfill, expected):
    page, errors = ui
    navigate(page, "monitors")
    page.locator("#wb-create").click()
    page.locator("#t-url").fill("local-fixture-creator")
    page.locator("#t-acc").select_option("1", force=True)
    expect(page.locator("#t-backfill")).to_have_value("")
    page.locator("#t-backfill").select_option(backfill, force=True)
    page.get_by_role("button", name="开始监控", exact=True).click()
    page.locator(".wb-sheet").wait_for(state="hidden")
    assert json.loads(page.locator("html").get_attribute("data-fixture-monitor-body"))["initial_backfill_count"] == expected
    assert errors == []


@pytest.mark.parametrize("width,height,theme", [(1440, 1000, "light"), (1440, 1000, "dark"),
    (375, 812, "light"), (375, 812, "dark"), (812, 375, "light")])
def test_workbench_engine_settings_responsive(ui, width, height, theme):
    page, errors = ui
    page.locator(f'[data-theme-choice="{theme}"]').click()
    page.set_viewport_size({"width": width, "height": height})
    settings(page)
    capture(page, f"engine-settings-{theme}-{width}-basic")
    for target in ("advanced", "health"):
        page.locator(f"#engine-settings-{target} > summary").click()
    assert_no_overflow(page, f"engine-settings-{theme}-{width}")
    layout = page.locator("#engine-settings-form").evaluate("""form => ({
      unnamed:[...form.querySelectorAll('input,select')].filter(el => !el.labels?.length).map(el => el.id),
      short:[...form.querySelectorAll('input:not([type=checkbox]),.cs-trg,.switch-row,summary,.form-actions button')]
        .filter(el => el.getBoundingClientRect().width && el.getBoundingClientRect().height < 44).map(el => el.id || el.className),
      small:[...form.querySelectorAll('input:not([type=checkbox]),.cs-trg')]
        .filter(el => el.getBoundingClientRect().width && parseFloat(getComputedStyle(el).fontSize) < 16).map(el => el.id),
    })""")
    assert not layout["unnamed"] and not layout["short"], layout
    if width <= 600:
        assert not layout["small"], layout
    page.evaluate("window.scrollTo({top:0, behavior:'instant'})")
    page.wait_for_function("window.scrollY === 0")
    capture(page, f"engine-settings-{theme}-{width}-expanded")
    assert errors == []
