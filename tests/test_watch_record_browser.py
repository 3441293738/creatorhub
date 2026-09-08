"""Local preview acceptance; fixture records only, external traffic blocked."""
import os
from pathlib import Path
from urllib.parse import parse_qs

import pytest
from test_web_appearance_browser import ui, navigate, assert_no_overflow, capture

pytestmark = [pytest.mark.local_cdp, pytest.mark.skipif(
    os.environ.get("CREATORHUB_RUN_LOCAL_CDP") != "1", reason="local UI acceptance is opt-in")]


def prepare(page, kind):
    page.add_script_tag(path=str(Path(__file__).parent / "fixtures/watch-records-api.js"))
    page.evaluate("document.documentElement.dataset.fixtureWatchRecords='true'")
    page.locator('[data-pf="douyin"]').first.click()
    page.evaluate("document.dispatchEvent(new Event('fixture-watch-record-refresh'))")
    page.wait_for_function("document.documentElement.dataset.fixtureWatchReady==='true'")
    navigate(page, "comments" if kind == "comment" else "danmaku", mobile=page.viewport_size["width"] <= 860)


@pytest.mark.parametrize("kind", ["comment", "danmaku"])
@pytest.mark.parametrize("width,height,theme", [(1440,960,"light"),(1440,960,"dark"),
    (375,812,"light"),(375,812,"dark"),(812,375,"light")])
def test_workbench_watch_record_scope_and_layout(ui, kind, width, height, theme):
    page, errors = ui
    page.locator(f'[data-theme-choice="{theme}"]').click()
    page.set_viewport_size({"width":width,"height":height})
    prepare(page,kind)
    page.locator(f'[data-{kind}-records="21"]').click()
    area=page.locator(f"#{kind}-table")
    area.get_by_text("任务 A 本次抓取的内容",exact=True).wait_for()
    assert "任务 B 独立" not in area.inner_text()
    assert "抓取入库" in area.inner_text() and "任务 #21" in area.inner_text()
    assert ("评论发布" if kind == "comment" else "弹幕发送") in area.inner_text()
    if kind == "danmaku":
        assert "创作中心" in area.inner_text() and "0:12.340" in area.inner_text()
    query=parse_qs(page.locator("html").get_attribute("data-fixture-watch-query").lstrip("?"))
    assert query["watch_id"] == ["21"] and query["sort"] == ["captured_desc"]
    if width <= 1100:
        box=area.locator(".watch-record-time").first.bounding_box()
        assert box["x"] >= 0 and box["x"]+box["width"] <= width
        if kind == "danmaku":
            assert area.locator(".watch-record-point code").first.evaluate("el=>el.getClientRects().length") == 1
        if kind == "comment":
            label=area.locator(".watch-record-selection").first
            box=label.bounding_box()
            assert box["width"] >= 44 and box["height"] >= 44
            label.click(position={"x":2,"y":2})
            assert label.locator("input").is_checked()
    assert_no_overflow(page,f"{kind}-records-{width}-{theme}")
    page.evaluate("window.scrollTo({top:0,behavior:'instant'})")
    page.wait_for_function("window.scrollY===0")
    capture(page,f"{kind}-records-{theme}-{width}")
    page.get_by_role("button",name="返回任务列表",exact=True).click()
    page.locator(f'[data-{kind}-records="22"]').click()
    area.get_by_text("任务 B 独立记录同一内容",exact=True).wait_for()
    assert "任务 A 本次" not in area.inner_text()
    assert_no_overflow(page,f"{kind}-long-source-{width}")
    page.get_by_role("button",name="查看全部任务",exact=True).click()
    area.get_by_text("已删除任务的历史内容",exact=True).wait_for()
    label="评论" if kind=="comment" else "弹幕"
    area.get_by_role("button",name=f"已删除{label}任务 #99",exact=True).click()
    page.wait_for_function("kind=>document.querySelector('#'+kind+'-scope-hint').textContent.includes('原任务已删除')",arg=kind)
    assert "未记录" in area.inner_text() and "任务 A 本次" not in area.inner_text()
    page.locator(f"#{kind}-src").select_option("0")
    area.get_by_text("未关联监控的旧记录",exact=True).wait_for()
    assert area.locator("tr").count()==2
    assert errors==[]


@pytest.mark.parametrize("kind", ["comment", "danmaku"])
def test_workbench_watch_record_dates_errors_and_task_deletion(ui, kind):
    page, errors=ui
    prepare(page,kind)
    page.locator(f'[data-{kind}-records="21"]').click()
    area=page.locator(f"#{kind}-table")
    area.get_by_text("任务 A 本次抓取的内容",exact=True).wait_for()
    for end in ("from","to"):
        page.locator(f"#{kind}-captured-{end}").fill("2026-09-08")
    page.wait_for_function("kind=>document.querySelectorAll('#'+kind+'-table tr').length===1",arg=kind)
    assert "昨天" not in area.inner_text()
    page.locator(f"#{kind}-captured-from").fill("2026-09-09")
    page.locator(f"#{kind}-src").select_option("22")
    page.wait_for_function("kind=>document.querySelector('#'+kind+'-capture-help').dataset.error==='true'",arg=kind)
    assert "任务 A 本次" not in area.inner_text()
    assert page.locator(f"#{kind}-captured-from").get_attribute("aria-invalid")=="true"
    page.get_by_role("button",name="清除筛选",exact=True).click()
    area.get_by_text("任务 B 独立记录同一内容",exact=True).wait_for()
    assert page.locator(f"#{kind}-src").input_value()=="22"
    page.evaluate("document.documentElement.dataset.fixtureWatchFail='true'")
    page.locator(f"#{kind}-src").select_option("21")
    area.get_by_text("记录加载失败，请重新加载",exact=True).wait_for()
    assert "任务 B 独立" not in area.inner_text()
    page.evaluate("delete document.documentElement.dataset.fixtureWatchFail")
    page.get_by_role("button",name="清除筛选",exact=True).click()
    area.get_by_text("任务 A 本次抓取的内容",exact=True).wait_for()
    page.get_by_role("button",name="返回任务列表",exact=True).click()
    row=page.locator(f'[data-{kind}-records="21"]').locator("xpath=ancestor::tr")
    row.get_by_role("button",name="删除",exact=True).click()
    page.locator("#uimodal").wait_for(state="visible")
    assert "记录会保留" in page.locator("#ui-hint").inner_text()
    page.locator("#ui-ok").click()
    row.wait_for(state="detached")
    page.get_by_role("tab",name="评论记录" if kind=="comment" else "弹幕记录",exact=True).click()
    area.get_by_text("任务 A 本次抓取的内容",exact=True).wait_for()
    assert "原任务已删除" in area.inner_text()
    delete_flag = "with_comments=false" if kind == "comment" else "with_records=false"
    assert delete_flag in page.locator("html").get_attribute("data-fixture-watch-delete")
    assert errors==[]
