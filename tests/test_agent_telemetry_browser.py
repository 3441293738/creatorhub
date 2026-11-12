"""Token and public reasoning UI on the network-isolated synthetic preview."""
import json
import os

import pytest
from patchright.sync_api import expect

from test_agent_browser import assert_window_fits, capture_agent, main_eval, open_agent
from test_agent_chat_browser import send
from test_web_appearance_browser import ui

pytestmark = [pytest.mark.local_cdp, pytest.mark.skipif(
    os.environ.get("CREATORHUB_RUN_LOCAL_CDP") != "1", reason="local UI acceptance is opt-in")]


def telemetry(**values):
    return {"usage": {"input_tokens": 1200, "output_tokens": 300, "total_tokens": 1500, "reasoning_tokens": 200,
        "cached_input_tokens": 800, "cache_write_input_tokens": None, "status": "reported", "requests": 2, "reported_requests": 2,
        "costs": [{"amount": "0.0032", "currency": "USD"}], "cost_complete": True},
        "reasoning": [{"round": 1, "kind": "summary", "text": "先核对任务状态和证据，再区分登录失效与网络异常。<img src=x onerror=alert(1)>", "truncated": False}],
        "model_calls": [{"round": 1, "status": "completed", "usage": {"input_tokens": 500, "output_tokens": 100, "total_tokens": 600}},
                        {"round": 2, "status": "completed", "usage": {"input_tokens": 700, "output_tokens": 200, "total_tokens": 900}}], **values}


@pytest.mark.parametrize("width,theme", [(1440, "light"), (375, "light"), (375, "dark"), (320, "light")])
def test_telemetry_is_visible_reasoning_is_separate_and_layout_fits(ui, width, theme):
    page, errors = ui
    page.locator(f'[data-theme-choice="{theme}"]').click()
    open_agent(page, width)
    main_eval(page, "AgentFixture.telemetryQueue=" + json.dumps([telemetry()]))
    send(page, "分析具体原因")
    usage = page.locator(".agent-token-usage")
    expect(usage).to_be_visible()
    assert usage.locator("dd").all_text_contents() == ["1,200", "300", "1,500"]
    expect(usage).to_contain_text("推理 200（已含在输出中）")
    expect(usage).to_contain_text("缓存命中 800（已含在输入中）")
    expect(usage).to_contain_text("USD 0.0032")
    assert page.locator(".agent-message-evidence").get_attribute("open") is None
    expect(page.locator(".agent-conversation-usage")).to_contain_text("1,500 token")
    capture_agent(page, f"chat-usage-{width}-{theme}")
    page.locator(".agent-reasoning>summary").click()
    expect(page.locator(".agent-reasoning-body")).to_contain_text("先核对任务状态和证据")
    expect(page.locator(".agent-reasoning img,.agent-reasoning script")).to_have_count(0)
    page.locator(".agent-reasoning").scroll_into_view_if_needed()
    capture_agent(page, f"chat-reasoning-{width}-{theme}")
    assert_window_fits(page)
    page.locator(".agent-message-evidence>summary").click()
    expect(page.locator(".agent-call-usage")).to_contain_text("合计 600 token")
    assert errors == []


def test_missing_usage_is_not_zero_and_a_real_reported_zero_is_preserved(ui):
    page, errors = ui
    open_agent(page)
    main_eval(page, "AgentFixture.telemetryQueue=" + json.dumps([
        {"usage": {"status": "unavailable", "costs": []}},
        {"usage": {"status": "reported", "input_tokens": 0, "output_tokens": 0, "total_tokens": 0,
                   "requests": 1, "reported_requests": 1, "costs": [{"currency": "USD", "amount": "0"}], "cost_complete": True}},
    ]))
    send(page, "缺少统计")
    assert page.locator(".agent-token-usage dd").all_text_contents() == ["—", "—", "—"]
    expect(page.locator(".agent-usage-hint")).to_contain_text("不代表消耗为 0")
    expect(page.locator(".agent-usage-cost")).to_contain_text("上游未返回可核对金额")
    send(page, "真实零用量")
    assert page.locator(".agent-turn").last.locator(".agent-token-usage dd").all_text_contents() == ["0", "0", "0"]
    expect(page.locator(".agent-turn").last.locator(".agent-usage-cost")).to_contain_text("USD 0")
    expect(page.locator(".agent-conversation-usage")).to_contain_text("用量缺失 / 不完整")
    assert errors == []


def test_reasoning_and_usage_restore_from_history_without_extra_model_call(ui):
    page, errors = ui
    open_agent(page)
    main_eval(page, "AgentFixture.telemetryQueue=" + json.dumps([telemetry()]))
    send(page, "保存用量和摘要")
    page.get_by_role("button", name="新建对话", exact=True).click()
    page.get_by_role("tab", name="历史记录", exact=True).click()
    page.locator(".agent-history button").click()
    expect(page.locator(".agent-token-usage")).to_contain_text("1,500")
    page.locator(".agent-reasoning>summary").click()
    expect(page.locator(".agent-reasoning-body")).to_contain_text("登录失效")
    assert main_eval(page, "AgentFixture.calls.filter(c=>c.method==='POST' && c.path==='/api/agent/runs').length") == 1
    assert errors == []


def test_in_progress_usage_is_pending_or_partial_not_estimated(ui):
    page, errors = ui
    open_agent(page)
    main_eval(page, "AgentFixture.holdRuns=true")
    page.locator("#agent-prompt").fill("持续分析")
    page.get_by_role("button", name="发送", exact=True).click()
    expect(page.locator(".agent-usage-hint")).to_contain_text("等待上游 usage")
    assert page.locator(".agent-token-usage dd").all_text_contents() == ["—", "—", "—"]
    item = telemetry()
    item["usage"].update(status="partial", reported_requests=1, costs=[], cost_complete=False)
    main_eval(page, "AgentFixture.liveTelemetry=" + json.dumps(item))
    expect(page.locator(".agent-usage-hint")).to_contain_text("1/2 次调用用量完整")
    expect(page.locator(".agent-reasoning>summary")).to_contain_text("正在接收")
    assert errors == []


def test_failed_response_keeps_reported_tokens_and_protected_reasoning_is_not_fake_text(ui):
    page, errors = ui
    open_agent(page)
    item = telemetry(status="failed", error="模型输出被长度限制截断", answer="", reasoning=[{"round": 1, "text": "", "protected": True}])
    main_eval(page, "AgentFixture.telemetryQueue=" + json.dumps([item]))
    page.locator("#agent-prompt").fill("失败仍有消耗")
    page.get_by_role("button", name="发送", exact=True).click()
    expect(page.get_by_role("heading", name="未完成", exact=True)).to_be_visible()
    expect(page.locator(".agent-token-usage")).to_contain_text("1,500")
    page.locator(".agent-reasoning>summary").click()
    expect(page.locator(".agent-reasoning-body")).to_contain_text("没有可展示的文本")
    expect(page.get_by_role("button", name="编辑后重试", exact=True)).to_be_visible()
    assert errors == []


def test_reasoning_settings_are_opt_in_saved_and_reset_when_protocol_changes(ui):
    page, errors = ui
    open_agent(page)
    page.get_by_role("tab", name="模型设置", exact=True).click()
    page.locator(".agent-advanced-settings>summary").click()
    expect(page.locator("#agent-reasoning-mode")).to_have_value("default")
    page.locator("#agent-provider").select_option("openai_responses")
    page.locator("#agent-reasoning-mode").select_option("summary")
    page.get_by_role("button", name="保存配置", exact=True).click()
    expect(page.get_by_role("status").filter(has_text="配置已保存")).to_be_visible()
    assert main_eval(page, "AgentFixture.settings.reasoning_mode") == "summary"
    assert not main_eval(page, "AgentFixture.calls.some(c=>c.path.endsWith('/test') || c.path==='/api/agent/runs' && c.method==='POST')")
    page.locator("#agent-provider").select_option("anthropic")
    expect(page.locator("#agent-reasoning-mode")).to_have_value("default")
    page.locator("#agent-reasoning-mode").select_option("enabled")
    expect(page.locator("#agent-thinking-budget")).to_have_value("1024")
    page.locator("#agent-provider").select_option("openai_compatible")
    expect(page.locator("#agent-thinking-budget")).to_have_count(0)
    expect(page.locator("#agent-reasoning-mode")).to_have_value("default")
    assert errors == []
