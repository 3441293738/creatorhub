"""Stream-only UI acceptance using incremental, isolated ReadableStream fixtures."""
import json
import os

import pytest
from patchright.sync_api import expect

from test_agent_browser import assert_window_fits, capture_agent, main_eval, open_agent
from test_web_appearance_browser import ui

pytestmark = [pytest.mark.local_cdp, pytest.mark.skipif(
    os.environ.get("CREATORHUB_RUN_LOCAL_CDP") != "1", reason="local UI acceptance is opt-in")]


def begin(page):
    main_eval(page, "AgentFixture.holdRuns=true")
    page.locator("#agent-prompt").fill("请流式分析原因")
    page.get_by_role("button", name="发送", exact=True).click()
    expect(page.get_by_role("heading", name="分析中", exact=True)).to_be_visible()


def live(page, **value):
    main_eval(page, "AgentFixture.liveTelemetry=" + json.dumps(value))


def submissions(page):
    return main_eval(page, "AgentFixture.calls.filter(c=>c.method==='POST' && c.path==='/api/agent/runs').length")


@pytest.mark.parametrize("width,height,theme", [(1440, 960, "light"), (375, 812, "light"),
    (375, 812, "dark"), (320, 812, "light"), (812, 375, "light")])
def test_live_answer_reasoning_usage_and_composer_layout(ui, width, height, theme):
    page, errors = ui
    page.locator(f'[data-theme-choice="{theme}"]').click()
    open_agent(page, width)
    page.set_viewport_size({"width": width, "height": height})
    main_eval(page, "AgentFixture.fragmentStreamBytes=7")
    begin(page)
    before = page.locator(".agent-composer-dock").bounding_box()
    live(page, partial_answer="中文片段：先核对登录状态。", reasoning=[{"round": 1, "kind": "summary", "text": "正在核对证据。"}])
    preview = page.locator(".agent-stream-preview")
    expect(preview).to_contain_text("中文片段：先核对登录状态。")
    expect(page.locator(".agent-reasoning-body")).to_be_visible()
    expect(page.locator(".agent-working")).to_contain_text("正在流式输出")
    expect(page.get_by_role("heading", name="分析完成", exact=True)).to_have_count(0)
    assert page.locator(".agent-token-usage dd").all_text_contents() == ["—", "—", "—"]
    text = "中文片段：先核对登录状态。\n接着检查网络。<img src=x onerror=alert(1)>"
    usage = {"input_tokens": 20, "output_tokens": 5, "total_tokens": 25, "status": "partial",
             "requests": 1, "reported_requests": 0, "costs": []}
    live(page, partial_answer=text, reasoning=[{"round": 1, "kind": "summary", "text": "正在核对证据。已找到登录异常。"}], usage=usage)
    expect(preview).to_contain_text("接着检查网络")
    expect(preview.locator("img,script")).to_have_count(0)
    expect(page.locator(".agent-reasoning-body")).to_contain_text("已找到登录异常")
    expect(page.locator(".agent-token-usage dd").last).to_have_text("25")
    assert page.locator(".agent-composer-dock").bounding_box()["y"] == before["y"]
    assert_window_fits(page)
    preview.scroll_into_view_if_needed()
    capture_agent(page, f"stream-live-{width}-{height}-{theme}")
    assert main_eval(page, "AgentFixture.calls.filter(c=>/\\/runs\\/[^/]+$/.test(c.path) && c.method==='GET').length") == 0
    assert submissions(page) == 1
    # Final snapshot replaces, rather than duplicates, the partial text and usage.
    usage.update(output_tokens=10, total_tokens=30, status="reported", reported_requests=1)
    main_eval(page, "(Object.assign(Object.values(AgentFixture.runs)[0],AgentFixture.liveTelemetry," +
              json.dumps({"partial_answer": "", "answer": text, "status": "completed", "usage": usage}) + "),AgentFixture.holdRuns=false)")
    expect(page.get_by_role("heading", name="分析完成", exact=True)).to_be_visible()
    expect(preview).to_have_count(0)
    expect(page.locator(".agent-answer")).to_have_count(1)
    expect(page.locator(".agent-token-usage dd").last).to_have_text("30")
    expect(page.locator(".agent-conversation-usage")).to_contain_text("30 token")
    assert errors == []


def test_dropped_sse_subscription_reconnects_without_model_resubmit_or_poll(ui):
    page, errors = ui
    open_agent(page)
    main_eval(page, "AgentFixture.disconnectStreamOnce=true")
    begin(page)
    expect(page.locator(".agent-stream-issue")).to_contain_text("不会重新调用模型")
    live(page, partial_answer="重连后继续接收同一条消息")
    expect(page.locator(".agent-stream-preview")).to_contain_text("重连后继续接收同一条消息")
    expect(page.locator(".agent-stream-issue")).to_have_count(0)
    assert main_eval(page, "AgentFixture.calls.filter(c=>c.path.endsWith('/events')).length") == 2
    assert main_eval(page, "AgentFixture.calls.filter(c=>/\\/runs\\/[^/]+$/.test(c.path) && c.method==='GET').length") == 0
    assert submissions(page) == 1 and errors == []


def test_stop_preserves_stream_preview_and_next_draft(ui):
    page, errors = ui
    open_agent(page)
    begin(page)
    live(page, partial_answer="停止前已经收到的内容")
    expect(page.locator(".agent-stream-preview")).to_contain_text("停止前已经收到的内容")
    page.locator("#agent-prompt").fill("保留下一条草稿")
    page.get_by_role("button", name="停止分析", exact=True).click()
    expect(page.get_by_role("heading", name="已停止", exact=True)).to_be_visible()
    expect(page.locator(".agent-stream-preview")).to_contain_text("停止前已经收到的内容")
    expect(page.locator(".agent-partial-label")).to_have_text("已接收的部分内容 · 本次未完成")
    expect(page.locator("#agent-prompt")).to_have_value("保留下一条草稿")
    assert submissions(page) == 1 and errors == []


def test_minimized_chat_keeps_one_stream_and_latest_partial(ui):
    page, errors = ui
    open_agent(page)
    begin(page)
    page.get_by_role("button", name="收起 AI 助手", exact=True).click()
    live(page, partial_answer="收起时仍然接收事件")
    page.get_by_role("button", name="打开全局 AI 助手", exact=True).click()
    expect(page.locator(".agent-stream-preview")).to_contain_text("收起时仍然接收事件")
    assert main_eval(page, "AgentFixture.calls.filter(c=>c.path.endsWith('/events')).length") == 1
    assert submissions(page) == 1 and errors == []


@pytest.mark.parametrize("setting,message", [("streamHttpStatus=404", "重启 CreatorHub"),
                                             ("streamContentType='application/json'", "未返回 SSE 流")])
def test_wrong_local_stream_endpoint_never_falls_back_to_json_polling(ui, setting, message):
    page, errors = ui
    open_agent(page)
    main_eval(page, "AgentFixture." + setting)
    main_eval(page, "AgentFixture.holdRuns=true")
    page.locator("#agent-prompt").fill("检查流式连接")
    page.get_by_role("button", name="发送", exact=True).click()
    expect(page.locator(".agent-stream-issue")).to_contain_text(message)
    page.wait_for_timeout(1500)  # Beyond first reconnect delay: fatal local errors must not loop.
    assert main_eval(page, "AgentFixture.calls.filter(c=>c.path.endsWith('/events')).length") == 1
    assert main_eval(page, "AgentFixture.calls.filter(c=>/\\/runs\\/[^/]+$/.test(c.path) && c.method==='GET').length") == 0
    assert submissions(page) == 1 and errors == []
