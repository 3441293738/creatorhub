"""Chat-first UI acceptance on the synthetic, network-isolated preview only."""
import json
import os
from pathlib import Path

import pytest
from patchright.sync_api import expect

from test_agent_browser import assert_window_fits, capture_agent, main_eval, open_agent, open_options
from test_web_appearance_browser import ui

pytestmark = [pytest.mark.local_cdp, pytest.mark.skipif(
    os.environ.get("CREATORHUB_RUN_LOCAL_CDP") != "1", reason="local UI acceptance is opt-in")]


def send(page, value):
    page.locator("#agent-prompt").fill(value)
    page.get_by_role("button", name="发送", exact=True).click()
    expect(page.locator(".agent-turn").last.get_by_role("heading", name="分析完成", exact=True)).to_be_visible()
    expect(page.locator("#agent-prompt")).to_have_value("")


@pytest.mark.parametrize("width,height,theme", [(1440, 960, "light"), (375, 812, "light"), (375, 812, "dark"), (320, 812, "light"), (812, 375, "light")])
def test_chat_layout_followup_and_docked_composer(ui, width, height, theme):
    page, errors = ui
    page.locator(f'[data-theme-choice="{theme}"]').click()
    open_agent(page, width)
    page.set_viewport_size({"width": width, "height": height})
    assert page.locator(".agent-chat-options").get_attribute("open") is None
    capture_agent(page, f"chat-welcome-{width}-{height}-{theme}")
    send(page, "为什么采集任务失败？")
    before = page.locator("#agent-prompt").bounding_box()
    main_eval(page, "AgentFixture.responseQueue=['## 建议步骤\\n先**检查登录状态**，再验证任务。\\n- 保留错误记录\\n- 用小批量验证']")
    send(page, "具体应该怎样验证修复？")
    expect(page.locator(".agent-turn")).to_have_count(2)
    requests = main_eval(page, "AgentFixture.calls.filter(c=>c.path==='/api/agent/runs' && c.method==='POST').map(c=>c.body)")
    assert requests[1]["parent_run_id"] == requests[0]["request_id"]
    assert "history" not in requests[1] and "messages" not in requests[1]
    expect(page.locator(".agent-answer strong")).to_have_text("检查登录状态")
    expect(page.locator(".agent-answer li")).to_have_count(2)
    after = page.locator("#agent-prompt").bounding_box()
    assert abs(before["y"] - after["y"]) <= 1
    button = page.get_by_role("button", name="发送", exact=True).bounding_box()
    assert after["y"] >= 0 and after["y"] + after["height"] <= height
    assert button["y"] + button["height"] <= height
    assert_window_fits(page)
    capture_agent(page, f"chat-followup-{width}-{height}-{theme}")
    assert errors == []


def test_chat_enter_shift_enter_and_ime_do_not_duplicate_submissions(ui):
    page, errors = ui
    open_agent(page)
    editor = page.locator("#agent-prompt")
    editor.fill("第一行")
    editor.press("End")
    editor.press("Shift+Enter")
    expect(editor).to_have_value("第一行\n")
    editor.fill("中文输入法测试")
    editor.dispatch_event("compositionstart", {"data": "中"})
    editor.dispatch_event("keydown", {"key": "Enter", "code": "Enter", "isComposing": True, "keyCode": 229})
    assert not main_eval(page, "AgentFixture.calls.some(c=>c.path==='/api/agent/runs' && c.method==='POST')")
    editor.dispatch_event("compositionend", {"data": "中文"})
    editor.press("Enter")
    expect(page.locator(".agent-turn")).to_have_count(1)
    expect(page.get_by_role("heading", name="分析完成", exact=True)).to_be_visible()
    assert main_eval(page, "AgentFixture.calls.filter(c=>c.path==='/api/agent/runs' && c.method==='POST').length") == 1
    assert errors == []


def test_chat_history_groups_resumes_and_new_conversation_keeps_draft(ui):
    page, errors = ui
    open_agent(page)
    send(page, "摄影采集诊断")
    send(page, "需要补充哪些证据？")
    page.get_by_role("tab", name="历史记录", exact=True).click()
    expect(page.locator(".agent-history>li")).to_have_count(1)
    page.locator("#agent-history-search").fill("不存在的关键词")
    expect(page.get_by_text("没有匹配的对话", exact=True)).to_be_visible()
    page.locator("#agent-history-search").fill("摄影")
    page.locator(".agent-history button").click()
    expect(page.locator(".agent-turn")).to_have_count(2)
    assert main_eval(page, "AgentFixture.calls.filter(c=>c.path==='/api/agent/runs' && c.method==='POST').length") == 2
    send(page, "给我一个检查清单")
    expect(page.locator(".agent-turn")).to_have_count(3)
    page.locator("#agent-prompt").fill("保留的新草稿")
    page.get_by_role("button", name="新建对话", exact=True).click()
    expect(page.locator(".agent-turn")).to_have_count(0)
    expect(page.locator("#agent-prompt")).to_have_value("保留的新草稿")
    page.locator("#agent-prompt").press("Enter")
    expect(page.get_by_role("heading", name="分析完成", exact=True)).to_be_visible()
    last = main_eval(page, "AgentFixture.calls.filter(c=>c.path==='/api/agent/runs' && c.method==='POST').at(-1).body")
    assert "parent_run_id" not in last
    assert errors == []


def test_chat_scope_change_starts_fresh_and_model_choice_is_per_message(ui):
    page, errors = ui
    open_agent(page)
    page.get_by_role("tab", name="模型设置", exact=True).click()
    page.get_by_role("button", name="获取可用模型", exact=True).click()
    page.get_by_role("dialog", name="选择模型", exact=True).get_by_role("button", name="更新目录", exact=True).click()
    page.get_by_role("button", name="保存配置", exact=True).click()
    expect(page.get_by_role("status").filter(has_text="配置已保存")).to_be_visible()
    page.get_by_role("tab", name="助手", exact=True).click()
    send(page, "第一个问题")
    page.locator("#agent-chat-model").select_option("fixture-reasoner")
    send(page, "换一个模型继续解释")
    request = main_eval(page, "AgentFixture.calls.filter(c=>c.path==='/api/agent/runs' && c.method==='POST').at(-1).body")
    assert request["model"] == "fixture-reasoner" and request["parent_run_id"]
    assert main_eval(page, "AgentFixture.settings.model") == "fixture-model"
    open_options(page)
    page.locator("#agent-scope").select_option("xhs")
    send(page, "现在检查小红书")
    request = main_eval(page, "AgentFixture.calls.filter(c=>c.path==='/api/agent/runs' && c.method==='POST').at(-1).body")
    assert request["platform"] == "xhs" and "parent_run_id" not in request
    expect(page.locator(".agent-turn")).to_have_count(1)
    assert errors == []


def test_chat_markdown_is_text_only_and_copy_does_not_execute_code(ui):
    page, errors = ui
    open_agent(page)
    answer = '## 修复建议\n**先检查** `status`\n```python\nprint("fixture")\n```\n<img src=x onerror=alert(1)>\n[危险链接](javascript:alert(1))'
    main_eval(page, "AgentFixture.responseQueue=" + json.dumps([answer]))
    # A synthetic clipboard keeps the user's actual OS clipboard untouched.
    main_eval(page, "(Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:async value=>{AgentFixture.copied=value;}}}),true)")
    send(page, "格式化说明")
    expect(page.locator(".agent-answer img,.agent-answer script,.agent-answer a")).to_have_count(0)
    expect(page.locator(".agent-code-block pre")).to_have_text('print("fixture")')
    page.get_by_role("button", name="复制代码", exact=True).click()
    assert main_eval(page, "AgentFixture.copied") == 'print("fixture")'
    page.get_by_role("button", name="复制回复", exact=True).click()
    assert main_eval(page, "AgentFixture.copied") == answer
    assert errors == []


def test_chat_stop_retains_next_draft_and_does_not_submit_again(ui):
    page, errors = ui
    open_agent(page)
    main_eval(page, "AgentFixture.holdRuns=true")
    page.locator("#agent-prompt").fill("开始一次分析")
    page.get_by_role("button", name="发送", exact=True).click()
    expect(page.get_by_role("button", name="停止分析", exact=True)).to_be_visible()
    expect(page.get_by_role("button", name="新建对话", exact=True)).to_be_disabled()
    page.locator("#agent-prompt").fill("下一条草稿")
    page.get_by_role("button", name="停止分析", exact=True).click()
    expect(page.get_by_role("heading", name="已停止", exact=True)).to_be_visible()
    expect(page.locator("#agent-prompt")).to_have_value("下一条草稿")
    assert main_eval(page, "AgentFixture.calls.filter(c=>c.path==='/api/agent/runs' && c.method==='POST').length") == 1
    assert errors == []


def test_chat_updates_do_not_pull_reader_away_from_older_messages(ui):
    page, errors = ui
    open_agent(page, 375)
    main_eval(page, "AgentFixture.responseQueue=[" + json.dumps("历史说明\n\n" * 80) + "]")
    send(page, "先生成详细说明")
    main_eval(page, "AgentFixture.holdRuns=true")
    page.locator("#agent-prompt").fill("再分析一次")
    page.get_by_role("button", name="发送", exact=True).click()
    expect(page.get_by_role("heading", name="分析中", exact=True)).to_be_visible()
    page.locator(".agent-scroll").evaluate("el=>{el.scrollTop=0;el.dispatchEvent(new Event('scroll',{bubbles:true}));}")
    main_eval(page, "AgentFixture.holdRuns=false")
    expect(page.locator(".agent-turn").last.get_by_role("heading", name="分析完成", exact=True)).to_be_visible()
    expect(page.get_by_role("button", name="查看最新消息 ↓", exact=True)).to_be_visible()
    assert page.locator(".agent-scroll").evaluate("el=>el.scrollTop") < 100
    page.get_by_role("button", name="查看最新消息 ↓", exact=True).click()
    expect(page.get_by_role("button", name="查看最新消息 ↓", exact=True)).to_be_hidden()
    assert errors == []


def test_chat_restores_persisted_conversation_after_reload_without_resubmission(ui):
    page, errors = ui
    open_agent(page)
    send(page, "保存此对话")
    send(page, "继续讨论")
    saved = main_eval(page, "AgentFixture.runs")
    page.reload(wait_until="networkidle")
    page.add_script_tag(content=(Path(__file__).parent / "fixtures/agent-api.js").read_text(encoding="utf-8"))
    main_eval(page, "AgentFixture.runs=" + json.dumps(saved))
    page.get_by_role("button", name="打开全局 AI 助手", exact=True).click()
    page.get_by_role("tab", name="历史记录", exact=True).click()
    page.locator(".agent-history button").click()
    expect(page.locator(".agent-turn")).to_have_count(2)
    assert not main_eval(page, "AgentFixture.calls.some(c=>c.method==='POST')")
    assert errors == []
