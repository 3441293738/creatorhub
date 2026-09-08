"""Opt-in local demo acceptance; never opens account profiles or live APIs."""
import os
from urllib.parse import parse_qs

import pytest
from test_web_appearance_browser import ui, navigate, assert_no_overflow, capture

pytestmark = [pytest.mark.local_cdp, pytest.mark.skipif(
    os.environ.get("CREATORHUB_RUN_LOCAL_CDP") != "1", reason="local UI acceptance is opt-in")]


def prepare(page, platform):
    page.evaluate("document.documentElement.dataset.fixtureRecords = 'true'")
    page.locator(f'[data-pf="{platform}"]').first.click()
    page.evaluate("document.dispatchEvent(new Event('fixture-record-refresh'))")
    page.wait_for_function("document.documentElement.dataset.fixtureRecordReady === 'true'")
    navigate(page, "monitors", mobile=page.viewport_size["width"] <= 860)


@pytest.mark.parametrize("platform,width,height,theme", [
    ("xhs",1440,960,"light"), ("xhs",1440,960,"dark"),
    ("xhs",375,812,"light"), ("xhs",375,812,"dark"),
    ("xhs",812,375,"light"), ("douyin",1440,960,"light"), ("douyin",375,812,"dark"),
])
def test_workbench_record_task_scope_and_layout(ui, platform, width, height, theme):
    page, errors = ui
    page.locator(f'[data-theme-choice="{theme}"]').click()
    page.set_viewport_size({"width":width,"height":height})
    prepare(page, platform)
    page.locator('[data-monitor-records="11"]').click()
    page.wait_for_function("document.querySelector('#content-scope-name').textContent.includes('品牌 A')")
    area = page.locator("#content-cards" if platform == "xhs" else "#content-table")
    area.get_by_text("任务 A 本次抓取的作品", exact=True).wait_for()
    assert page.get_by_role("tab",name="作品记录",exact=True).get_attribute("aria-selected") == "true"
    assert "任务 B 独立" not in area.inner_text()
    assert "抓取入库" in area.inner_text() and "发布 ·" in area.inner_text()
    assert "任务 #11" in area.inner_text()
    if platform == "xhs":
        label = area.locator(".ncard-selection").first
        card = area.locator(".ncard").first.bounding_box()
        box = label.bounding_box()
        assert box["width"] >= 44 and box["height"] >= 44
        assert box["x"] >= card["x"] and box["y"] >= card["y"]
        assert box["x"] + box["width"] <= card["x"] + card["width"]
        label.click()
        assert label.locator("input").is_checked()
    query = parse_qs(page.locator("html").get_attribute("data-fixture-records-query").lstrip("?"))
    assert query["target_id"] == ["11"] and query["sort"] == ["captured_desc"]
    assert_no_overflow(page, f"records-{platform}-{theme}-{width}")
    if platform != "xhs" and width <= 760:
        time = area.locator(".content-record-time").first.bounding_box()
        assert time["x"] >= 0 and time["x"] + time["width"] <= width
        label = area.locator(".content-selection").first
        box = label.bounding_box()
        assert box["width"] >= 44 and box["height"] >= 44
        label.click(position={"x": 2, "y": 2})
        assert label.get_by_role("checkbox", name="选择这条作品").is_checked()
    if width <= 760:
        # The stacked layout is specific to monitor records, not download history.
        assert page.locator("#sd-history-table-wrap table").evaluate("el=>getComputedStyle(el).display") == "table"
        assert page.locator("#sd-history-table-wrap thead").evaluate("el=>getComputedStyle(el).display") != "none"
    page.evaluate("window.scrollTo({top:0,behavior:'instant'})")
    page.wait_for_function("window.scrollY === 0")
    capture(page, f"records-{platform}-{theme}-{width}")
    page.get_by_role("button",name="返回任务列表",exact=True).click()
    page.locator('[data-monitor-records="12"]').click()
    area.get_by_text("任务 B 独立记录同一作品",exact=True).wait_for()
    assert "任务 A 本次" not in area.inner_text()
    assert_no_overflow(page, f"records-long-name-{platform}-{width}")
    page.get_by_role("button",name="查看全部任务",exact=True).click()
    area.get_by_text("已删除任务的历史记录",exact=True).wait_for()
    area.get_by_role("button",name="已删除任务 #99",exact=True).click()
    page.wait_for_function("document.querySelector('#content-scope-hint').textContent.includes('原任务已删除')")
    assert "未记录" in area.inner_text()
    assert "任务 A 本次" not in area.inner_text()
    assert errors == []


def test_workbench_record_capture_dates_and_reset(ui):
    page, errors = ui
    prepare(page, "xhs")
    page.locator('[data-monitor-records="11"]').click()
    page.locator("#content-cards").get_by_text("任务 A 本次抓取的作品",exact=True).wait_for()
    for field in ("content-captured-from", "content-captured-to"):
        page.locator("#"+field).fill("2026-09-08")
    page.wait_for_function("document.documentElement.dataset.fixtureRecordsQuery.includes('captured_before')")
    page.wait_for_function("document.querySelectorAll('#content-cards .ncard').length === 1")
    assert "昨天" not in page.locator("#content-cards").inner_text()
    page.get_by_role("button",name="清除筛选",exact=True).click()
    page.wait_for_function("document.querySelectorAll('#content-cards .ncard').length === 2")
    assert page.locator("#content-captured-from").input_value() == ""
    assert page.locator("#content-src").input_value() == "11"
    page.locator("#content-captured-to").fill("2026-09-08")
    page.locator("#content-captured-from").fill("2026-09-09")
    page.locator("#content-src").select_option("12")
    page.wait_for_function("document.querySelector('#content-capture-help').dataset.error === 'true'")
    assert page.locator("#content-captured-from").get_attribute("aria-invalid") == "true"
    assert "任务 A 本次" not in page.locator("#content-cards").inner_text()
    page.get_by_role("button",name="清除筛选",exact=True).click()
    page.locator("#content-cards").get_by_text("任务 B 独立记录同一作品",exact=True).wait_for()
    assert errors == []
