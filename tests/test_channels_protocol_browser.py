"""Opt-in headless Chrome UI tests for the local WeChat Channels config tab."""
import os

import pytest
from patchright.sync_api import expect

from test_web_appearance_browser import ui, navigate, assert_no_overflow  # noqa: F401

pytestmark = [pytest.mark.local_cdp, pytest.mark.skipif(
    os.environ.get("CREATORHUB_RUN_LOCAL_CDP") != "1", reason="local UI acceptance is opt-in")]


def test_channels_protocol_tab_workflow_and_jump(ui):
    page, errors = ui
    navigate(page, "settings")
    page.get_by_role("tab", name="采集与运行", exact=True).click()
    page.get_by_role("link", name="去配置微信通道 →", exact=True).click()
    expect(page.locator("#channels-protocol-card")).to_be_visible()
    expect(page.locator("#ch-proto-badge")).to_contain_text("未配置")

    # Switching mode reveals the appropriate input field and saves cleanly.
    page.locator("#ch-proto-mode").select_option("endpoint", force=True)
    expect(page.locator("#ch-proto-endpoint-wrap")).to_be_visible()
    expect(page.locator("#ch-proto-file-wrap")).to_be_hidden()

    page.locator("#ch-proto-endpoint").fill("http://127.0.0.1:9222")
    page.locator("#ch-proto-save-btn").click()
    expect(page.locator("#ch-proto-msg")).to_contain_text("已保存")
    expect(page.locator("#ch-proto-badge")).to_contain_text("已配置")
    expect(page.locator("#ch-proto-caps")).to_contain_text("关键词搜索")

    # Discovery and probe run cleanly without errors.
    page.locator("#ch-proto-scan-btn").click()
    expect(page.locator("#ch-proto-discovered")).to_be_visible()
    page.locator("#ch-proto-probe-btn").click()
    expect(page.locator("#ch-proto-probe-box")).to_be_visible()

    # Reset restores unconfigured defaults.
    page.locator("#ch-proto-reset-btn").click()
    expect(page.locator("#ch-proto-badge")).to_contain_text("未配置")
    assert errors == []


@pytest.mark.parametrize("width,height,theme", [(1440, 1000, "light"), (375, 812, "dark")])
def test_channels_protocol_responsive_and_accessible(ui, width, height, theme):
    page, errors = ui
    page.locator(f'[data-theme-choice="{theme}"]').click()
    page.set_viewport_size({"width": width, "height": height})
    navigate(page, "settings", mobile=width <= 860)
    page.get_by_role("tab", name="视频号微信通道", exact=True).click()
    expect(page.locator("#channels-protocol-card")).to_be_visible()
    assert_no_overflow(page, f"channels-protocol-{theme}-{width}")
    layout = page.locator("#channels-protocol-card").evaluate("""card => ({
      unnamed: [...card.querySelectorAll('input,select')].filter(el => !el.labels?.length).map(el => el.id),
      short: [...card.querySelectorAll('input:not([type=checkbox]),.cs-trg,.switch-row,summary,.channels-proto-actions button,.channels-bridge-actions button')]
        .filter(el => el.getBoundingClientRect().width && el.getBoundingClientRect().height < 44).map(el => el.id || el.className),
      small: [...card.querySelectorAll('input:not([type=checkbox]),.cs-trg')]
        .filter(el => el.getBoundingClientRect().width && parseFloat(getComputedStyle(el).fontSize) < 16).map(el => el.id),
    })""")
    assert not layout["unnamed"] and not layout["short"], layout
    if width <= 600:
        assert not layout["small"], layout
    assert errors == []
