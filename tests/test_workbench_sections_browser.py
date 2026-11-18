"""Opt-in tab/content regressions on the isolated demo, never real accounts."""
import os

import pytest
from patchright.sync_api import expect

from test_web_appearance_browser import ui, navigate, capture, assert_no_overflow


pytestmark = [pytest.mark.local_cdp, pytest.mark.skipif(
    os.environ.get("CREATORHUB_RUN_LOCAL_CDP") != "1", reason="local UI acceptance is opt-in")]

ACCOUNT_TABS = (
    ("账号列表", "acc-table"),
    ("浏览器内核", "runtime-table"),
    ("代理池", "proxy-table"),
)


def reorder_cards_before_boot(page):
    """Card order must not determine tab identity, even after new cards appear."""
    def bundle(route):
        response = route.fetch()
        prefix = """document.querySelectorAll('main > [data-panel]').forEach(panel => {
          const cards = [...panel.children].filter(node => node.classList.contains('card'));
          panel.append(...cards.reverse());
        });\n"""
        route.fulfill(response=response, body=prefix + response.text())

    page.route("**/workbench.js", bundle)
    page.reload(wait_until="networkidle")


@pytest.mark.parametrize("width", [1440, 375])
@pytest.mark.parametrize("reordered", [False, True], ids=["original-order", "reordered"])
def test_workbench_account_tabs_match_content(ui, width, reordered):
    page, errors = ui
    if reordered:
        reorder_cards_before_boot(page)
    page.set_viewport_size({"width": width, "height": 1000})
    navigate(page, "accounts", mobile=width <= 860)
    accounts = page.locator('[data-panel="accounts"]')

    for platform in ("douyin", "xhs", "kuaishou", "shipinhao"):
        page.locator(f'.pswitch [data-pf="{platform}"]').click()
        expect(accounts.get_by_role("tab")).to_have_text([label for label, _ in ACCOUNT_TABS])
        expect(accounts.get_by_role("tab", name="聚合私信", exact=True, include_hidden=True)).to_have_count(0)
        expect(accounts.locator(":scope > .card")).to_have_count(0)
        # Keep the feature's DOM available to existing refresh handlers, but do
        # not expose it visually or as a keyboard-accessible tab.
        expect(page.locator("#dm-inbox-table")).to_have_count(1)
        expect(page.locator("#dm-inbox-table")).to_be_hidden()

        for label, table_id in ACCOUNT_TABS:
            tab = accounts.get_by_role("tab", name=label, exact=True)
            tab.click()
            expect(tab).to_have_attribute("aria-selected", "true")
            active = accounts.get_by_role("tabpanel")
            expect(active).to_have_count(1)
            expect(active.locator(f"#{table_id}")).to_be_visible()
            for _, other_id in ACCOUNT_TABS:
                expect(page.locator(f"#{other_id}")).to_have_count(1)
                if other_id != table_id:
                    expect(page.locator(f"#{other_id}")).to_be_hidden()
            expect(page.locator("#dm-inbox-table")).to_be_hidden()
            assert_no_overflow(page, f"account-tabs-{platform}-{table_id}-{width}")
            if platform == "douyin" and table_id == "runtime-table" and not reordered:
                capture(page, f"account-runtime-{width}")

        # Keyboard activation must select the same content as clicking.
        accounts.get_by_role("tab", name="浏览器内核", exact=True).focus()
        page.keyboard.press("ArrowRight")
        expect(accounts.get_by_role("tab", name="代理池", exact=True)).to_have_attribute("aria-selected", "true")
        expect(page.locator("#proxy-table")).to_be_visible()

    # Refreshing account data must not reveal the temporarily hidden feature.
    page.get_by_role("button", name="刷新当前页面", exact=True).click()
    expect(accounts.get_by_role("tab", name="聚合私信", exact=True, include_hidden=True)).to_have_count(0)
    expect(page.locator("#dm-inbox-table")).to_be_hidden()
    assert errors == []


def test_workbench_section_bindings_survive_card_reordering(ui):
    page, errors = ui
    reorder_cards_before_boot(page)
    for section, tabs in (
        ("settings", (("外观与体验", "#appearance-motion"), ("下载设置", "#dl-dir"),
                      ("AI 文案", "#ai-key"), ("采集与运行", "#engine-settings-card"),
                      ("视频号微信通道", "#channels-protocol-card"))),
        ("monitors", (("监控目标", "#mon-table"), ("作品记录", "#content-table"))),
        ("comments", (("监控目标", "#watch-table"), ("评论记录", "#comment-table"))),
        ("danmaku", (("监控目标", "#danmaku-watch-table"), ("弹幕记录", "#danmaku-table"))),
        ("autocomment", (("评论规则", "#ac-rule-table"), ("任务与审核", "#ac-task-table"))),
        ("publish", (("撰写内容", "#pub-title"), ("发布记录", "#pub-table"))),
        ("risk-control", (("账号状态", "#risk-account-search"), ("风控规则", "#risk-enabled"))),
    ):
        navigate(page, section)
        panel = page.locator(f'[data-panel="{section}"]')
        for label, selector in tabs:
            panel.get_by_role("tab", name=label, exact=True).click()
            active = panel.get_by_role("tabpanel")
            expect(active).to_have_count(1)
            expect(active.locator(selector)).to_be_visible()
    page.locator('.pswitch [data-pf="xhs"]').click()
    navigate(page, "publish")
    page.get_by_role("tab", name="已发布作品", exact=True).click()
    expect(page.locator("#published-msg")).to_be_visible()
    page.locator('.pswitch [data-pf="douyin"]').click()
    expect(page.get_by_role("tab", name="已发布作品", exact=True)).to_have_count(0)
    expect(page.locator("#pub-title")).to_be_visible()
    assert errors == []
