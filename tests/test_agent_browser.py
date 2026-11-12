"""Opt-in real browser on an isolated demo with an entirely local AI fixture."""
import os
import json
from pathlib import Path

import pytest
from patchright.sync_api import expect

from test_web_appearance_browser import ui, navigate, capture, assert_no_overflow

pytestmark = [pytest.mark.local_cdp, pytest.mark.skipif(
    os.environ.get("CREATORHUB_RUN_LOCAL_CDP") != "1", reason="local UI acceptance is opt-in")]


def open_agent(page, width=1440, enabled=None):
    page.add_script_tag(content=(Path(__file__).parent / "fixtures/agent-api.js").read_text(encoding="utf-8"))
    if enabled is not None:
        main_eval(page, "AgentFixture.settings.enabled = " + json.dumps(enabled))
    assert not main_eval(page, "AgentFixture.calls.length")
    page.set_viewport_size({"width": width, "height": 812 if width < 600 else 960})
    page.get_by_role("button", name="打开全局 AI 助手", exact=True).click()
    try:
        expect(page.locator("#agent-account option")).to_have_count(2)
    except AssertionError:
        print(main_eval(page, "({calls:AgentFixture.calls,context:CreatorHubBridge.getContext(),text:document.querySelector('#agent-root').innerText})"))
        capture(page, "agent-load-failure")
        raise


def main_eval(page, expression):
    # Patchright evaluates in an isolated world by default; fixture fetch must
    # live in the same world as the application. Share only synthetic test data.
    script = page.add_script_tag(content=f"document.documentElement.dataset.agentTest = JSON.stringify({expression});")
    result = json.loads(page.locator("html").get_attribute("data-agent-test"))
    script.evaluate("element => element.remove()")
    return result


def capture_agent(page, name):
    directory = os.environ.get("CREATORHUB_UI_ARTIFACTS")
    if directory:
        target = Path(directory).resolve()
        target.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(target / f"{name}.png"), full_page=False)


def assert_window_fits(page):
    window = page.locator("#agent-dialog").bounding_box()
    assert window and window["x"] >= 0 and window["y"] >= 0
    assert window["x"] + window["width"] <= page.viewport_size["width"]
    assert window["y"] + window["height"] <= page.viewport_size["height"]
    assert page.locator(".agent-scroll").evaluate("el => el.scrollWidth <= el.clientWidth + 1")


def open_options(page):
    options = page.locator(".agent-chat-options")
    if options.get_attribute("open") is None:
        options.locator(":scope>summary").click()


def accept_model_picker(page, *, select_all=False):
    picker = page.get_by_role("dialog", name="选择模型", exact=True)
    expect(picker).to_be_visible()
    if select_all:
        picker.get_by_role("button", name="全选", exact=True).click()
    picker.get_by_role("button", name="更新目录", exact=True).click()
    expect(picker).to_have_count(0)


@pytest.mark.parametrize("width,theme", [(1440, "light"), (1440, "dark"), (375, "light"), (375, "dark")])
def test_agent_diagnosis_layout_and_output_escaping(ui, width, theme):
    page, errors = ui
    page.locator(f'[data-theme-choice="{theme}"]').click()
    open_agent(page, width)
    open_options(page)
    page.locator("#agent-mode").select_option("diagnose")
    open_options(page)
    page.locator("#agent-task").select_option("collections:10")
    page.locator("#agent-prompt").fill("为什么这个任务失败，如何验证修复？")
    page.get_by_role("button", name="发送", exact=True).click()
    expect(page.get_by_role("heading", name="分析完成", exact=True)).to_be_visible()
    expect(page.locator(".agent-answer")).to_contain_text("<img src=x")
    expect(page.locator(".agent-answer img")).to_have_count(0)
    expect(page.locator(".agent-workspace .cs")).to_have_count(0)
    page.get_by_text("执行记录与证据（1）", exact=True).click()
    page.get_by_text("e1 · get_task", exact=True).click()
    expect(page.locator(".agent-details pre")).to_contain_text("登录态已失效")
    assert_no_overflow(page, f"agent-{width}-{theme}")
    page.evaluate("window.scrollTo(0, 0)")
    assert_window_fits(page)
    expect(page.locator('.navitem.active')).to_have_attribute("data-tab", "overview")
    page.locator(".agent-result").evaluate("el => el.scrollIntoView({block:'start'})")
    capture_agent(page, f"agent-diagnosis-{width}-{theme}")
    assert errors == []


def test_agent_workflow_requires_explicit_approval(ui):
    page, errors = ui
    open_agent(page)
    open_options(page)
    page.locator("#agent-mode").select_option("workflow")
    page.locator("#agent-prompt").fill("采集摄影关键词，最多20条，并发送日报")
    page.get_by_role("button", name="生成草稿", exact=True).click()
    apply = page.get_by_role("button", name="确认并创建 1 项任务", exact=True)
    expect(apply).to_be_disabled()
    expect(page.get_by_text("自动发送日报尚未包含", exact=True)).to_be_visible()
    assert main_eval(page, "AgentFixture.approvals") == 0
    page.get_by_label("我已核对全部参数，确认创建并交由现有引擎执行").check()
    page.evaluate("window.scrollTo(0, 0)")
    capture_agent(page, "agent-workflow-confirmation")
    apply.click()
    expect(page.get_by_text("已创建的任务（后续执行状态以业务页面为准）", exact=True)).to_be_visible()
    assert main_eval(page, "AgentFixture.approvals") == 1
    expect(page.get_by_role("button", name="确认并创建 1 项任务", exact=True)).to_have_count(0)
    assert errors == []


def test_agent_failed_submission_preserves_input_and_request_id(ui):
    page, errors = ui
    open_agent(page)
    main_eval(page, "AgentFixture.failSubmitOnce = true")
    page.locator("#agent-prompt").fill("总结最近的失败原因")
    page.get_by_role("button", name="发送", exact=True).click()
    expect(page.get_by_role("alert")).to_contain_text("模拟响应丢失")
    expect(page.locator("#agent-prompt")).to_have_value("总结最近的失败原因")
    page.get_by_role("button", name="发送", exact=True).click()
    expect(page.get_by_role("heading", name="分析完成", exact=True)).to_be_visible()
    ids = main_eval(page, "AgentFixture.calls.filter(c=>c.path==='/api/agent/runs' && c.method==='POST').map(c=>c.body.request_id)")
    assert len(ids) == 2 and ids[0] == ids[1]
    assert errors == []


def test_agent_settings_failure_preserves_fields(ui):
    page, errors = ui
    open_agent(page, 375)
    page.get_by_role("tab", name="模型设置", exact=True).click()
    page.locator("#agent-model").fill("changed-model")
    main_eval(page, "AgentFixture.failSettingsOnce = true")
    page.get_by_role("button", name="保存配置", exact=True).click()
    expect(page.get_by_role("alert")).to_contain_text("模拟保存失败")
    expect(page.locator("#agent-model")).to_have_value("changed-model")
    assert_no_overflow(page, "agent-settings-mobile")
    capture_agent(page, "agent-settings-mobile")
    page.get_by_role("button", name="保存配置", exact=True).click()
    expect(page.get_by_text("配置已保存，后续分析使用新配置。运行中的分析保持原配置。", exact=True)).to_be_visible()
    assert errors == []


def test_agent_first_setup_stays_open_and_requires_saved_configuration(ui):
    page, errors = ui
    open_agent(page, enabled=False)
    page.locator("#agent-prompt").fill("分析数据")
    page.get_by_role("tab", name="模型设置", exact=True).click()
    expect(page.locator("#agent-model")).to_be_visible()
    page.get_by_label("启用 AI 助手", exact=True).check()
    expect(page.locator("#agent-model")).to_be_visible()
    start = page.get_by_role("button", name="发送", exact=True)
    page.get_by_role("tab", name="助手", exact=True).click()
    expect(start).to_be_disabled()
    expect(page.get_by_text("模型配置有未保存更改，请先保存", exact=True)).to_be_visible()
    page.get_by_role("tab", name="模型设置", exact=True).click()
    page.get_by_role("button", name="保存配置", exact=True).click()
    page.get_by_role("tab", name="助手", exact=True).click()
    expect(start).to_be_enabled()
    page.get_by_role("tab", name="模型设置", exact=True).click()
    page.locator("#agent-base").fill("http://127.0.0.1:1234/v1")
    page.get_by_role("tab", name="助手", exact=True).click()
    expect(start).to_be_disabled()
    assert not main_eval(page, "AgentFixture.calls.some(c=>c.path==='/api/agent/runs' && c.method==='POST')")
    assert errors == []


def test_agent_global_launcher_preserves_input_across_pages_and_minimize(ui):
    page, errors = ui
    open_agent(page)
    expect(page.locator('.navitem[data-tab="agent"]')).to_have_count(0)
    capture_agent(page, "agent-global-welcome")
    page.locator("#agent-prompt").fill("保留这条未提交的需求")
    navigate(page, "accounts")
    expect(page.get_by_role("dialog", name="AI 助手", exact=True)).to_be_visible()
    expect(page.locator(".agent-page-context")).to_contain_text("平台账号")
    expect(page.locator("#agent-prompt")).to_have_value("保留这条未提交的需求")
    page.get_by_role("button", name="收起 AI 助手", exact=True).click()
    expect(page.locator("#agent-dialog")).to_be_hidden()
    expect(page.locator("#agent-launcher")).to_be_focused()
    navigate(page, "queue")
    page.get_by_role("button", name="打开全局 AI 助手", exact=True).click()
    expect(page.locator("#agent-prompt")).to_have_value("保留这条未提交的需求")
    page.locator("#agent-prompt").press("Escape")
    expect(page.locator("#agent-dialog")).to_be_hidden()
    expect(page.locator("#agent-launcher")).to_be_focused()
    assert not main_eval(page, "AgentFixture.calls.some(c=>c.method==='POST')")
    assert errors == []


def test_agent_global_scope_and_history_are_shared(ui):
    page, errors = ui
    open_agent(page)
    open_options(page)
    page.locator("#agent-scope").select_option("")
    page.locator("#agent-prompt").fill("汇总所有平台")
    page.get_by_role("button", name="发送", exact=True).click()
    expect(page.get_by_role("heading", name="分析完成", exact=True)).to_be_visible()
    assert main_eval(page, "AgentFixture.calls.find(c=>c.path==='/api/agent/runs' && c.method==='POST').body.platform") == ""
    page.locator('.pswitch [data-pf="xhs"]').click()
    expect(page.locator("#agent-scope")).to_have_value("")
    expect(page.locator(".agent-result")).to_contain_text("本次范围：全平台")
    page.get_by_role("tab", name="历史记录", exact=True).click()
    expect(page.locator(".agent-history")).to_contain_text("汇总所有平台")
    assert all("platform=" not in query for query in main_eval(page, "AgentFixture.calls.filter(c=>c.path==='/api/agent/runs' && c.method==='GET').map(c=>c.query)"))
    page.locator(".agent-history button").first.click()
    expect(page.locator(".agent-answer")).to_contain_text("登录状态异常")
    assert errors == []


def test_agent_pending_run_survives_platform_change_and_minimize(ui):
    page, errors = ui
    open_agent(page)
    main_eval(page, "Object.assign(AgentFixture,{deferSubmit:true,holdRuns:true})")
    page.locator("#agent-prompt").fill("诊断当前平台")
    page.get_by_role("button", name="发送", exact=True).click()
    page.locator('.pswitch [data-pf="xhs"]').click()
    main_eval(page, "(AgentFixture.releaseSubmit(), true)")
    expect(page.get_by_role("heading", name="分析中", exact=True)).to_be_visible()
    expect(page.locator(".agent-result")).to_contain_text("本次范围：抖音")
    page.get_by_role("button", name="收起 AI 助手", exact=True).click()
    expect(page.locator("#agent-launcher")).to_contain_text("正在分析")
    main_eval(page, "AgentFixture.holdRuns = false")
    expect(page.locator("#agent-launcher")).to_have_text("AI 助手")
    page.get_by_role("button", name="打开全局 AI 助手", exact=True).click()
    expect(page.get_by_role("heading", name="分析完成", exact=True)).to_be_visible()
    expect(page.locator(".agent-result")).to_contain_text("本次范围：抖音")
    expect(page.locator("#agent-scope option:checked")).to_contain_text("小红书")
    assert main_eval(page, "AgentFixture.calls.filter(c=>c.path==='/api/agent/runs' && c.method==='POST').length") == 1
    assert errors == []


def test_agent_expand_legacy_link_and_landscape_layout(ui):
    page, errors = ui
    open_agent(page)
    original = page.locator("#agent-dialog").bounding_box()["width"]
    page.get_by_role("button", name="展开助手", exact=True).click()
    assert page.locator("#agent-dialog").bounding_box()["width"] > original
    page.get_by_role("button", name="收起 AI 助手", exact=True).click()
    main_eval(page, "location.hash = 'agent'")
    expect(page.locator("#agent-dialog")).to_be_visible()
    expect(page.locator('.navitem.active')).to_have_attribute("data-tab", "overview")
    page.set_viewport_size({"width": 812, "height": 375})
    assert_window_fits(page)
    assert page.locator(".agent-scroll").bounding_box()["height"] >= 90
    capture_agent(page, "agent-landscape")
    page.get_by_role("button", name="收起 AI 助手", exact=True).click()
    expect(page.locator("#agent-launcher")).to_be_focused()
    assert errors == []


def test_agent_context_task_is_explicit_and_preserved_when_changing_scope(ui):
    page, errors = ui
    open_agent(page)
    main_eval(page, "(CreatorHubBridge.getAgentContext = () => ({title:'关键词采集',platform:'douyin',tab:'collections',task:{task_type:'collections',id:99,platform:'douyin',status:'failed'}}), window.dispatchEvent(new Event('creatorhub:agent-context')), true)")
    open_options(page)
    page.locator("#agent-scope").select_option("xhs")
    page.get_by_role("button", name="带入当前任务进行诊断", exact=False).click()
    expect(page.locator("#agent-scope")).to_have_value("follow")
    expect(page.locator("#agent-mode")).to_have_value("diagnose")
    expect(page.locator("#agent-task")).to_have_value("collections:99")
    assert not main_eval(page, "AgentFixture.calls.some(c=>c.method==='POST')")
    assert errors == []


def test_agent_settings_draft_survives_navigation_without_persisting_key(ui):
    page, errors = ui
    open_agent(page)
    page.get_by_role("tab", name="模型设置", exact=True).click()
    page.locator("#agent-model").fill("unsubmitted-model")
    page.locator("#agent-key").fill("fixture-only-unsaved-key")
    page.get_by_role("button", name="收起 AI 助手", exact=True).click()
    navigate(page, "settings")
    page.locator('.pswitch [data-pf="xhs"]').click()
    page.get_by_role("button", name="打开全局 AI 助手", exact=True).click()
    expect(page.locator("#agent-model")).to_have_value("unsubmitted-model")
    expect(page.locator("#agent-key")).to_have_value("fixture-only-unsaved-key")
    assert not main_eval(page, "AgentFixture.calls.some(c=>c.method==='PUT')")
    assert "fixture-only-unsaved-key" not in main_eval(page, "JSON.stringify(localStorage)")
    assert errors == []


@pytest.mark.parametrize("width", [1440, 375, 320])
def test_agent_fetches_models_with_unsaved_key_and_keeps_manual_id(ui, width):
    page, errors = ui
    open_agent(page, width, enabled=False)
    page.get_by_role("tab", name="模型设置", exact=True).click()
    page.locator("#agent-base").fill("https://upstream.example/v1")
    page.locator("#agent-key").fill("fixture-only-new-key")
    page.locator("#agent-model").fill("manual-outside-catalog")
    assert page.get_by_role("tab", name="模型设置", exact=True).bounding_box()["height"] == 44
    page.get_by_role("button", name="获取可用模型", exact=True).click()
    expect(page.locator("#agent-catalog-status")).to_contain_text("本次获取 2 个模型")
    expect(page.locator("#agent-model")).to_have_value("manual-outside-catalog")
    body = main_eval(page, "AgentFixture.calls.find(c=>c.path==='/api/agent/models').body")
    assert body["base_url"] == "https://upstream.example/v1" and body["api_key"] == "fixture-only-new-key"
    assert "model" not in body
    assert not main_eval(page, "AgentFixture.calls.some(c=>c.method==='PUT')")
    accept_model_picker(page)
    page.locator("#agent-model-catalog").select_option("fixture-reasoner")
    expect(page.locator("#agent-model")).to_have_value("fixture-reasoner")
    assert_window_fits(page)
    page.locator(".agent-model-catalog").evaluate("el=>el.scrollIntoView({block:'center'})")
    capture_agent(page, f"agent-model-catalog-{width}")
    page.locator("#agent-model").fill("another-custom-id")
    page.get_by_role("button", name="保存配置", exact=True).click()
    expect(page.get_by_text("配置已保存，后续分析使用新配置。运行中的分析保持原配置。", exact=True)).to_be_visible()
    assert main_eval(page, "AgentFixture.calls.find(c=>c.path==='/api/agent/channels/default' && c.method==='PUT').body.model") == "another-custom-id"
    assert errors == []


def test_agent_model_catalog_401_keeps_previous_choices_and_inputs(ui):
    page, errors = ui
    open_agent(page, 375)
    page.get_by_role("tab", name="模型设置", exact=True).click()
    page.locator("#agent-key").fill("fixture-key-for-error")
    page.get_by_role("button", name="获取可用模型", exact=True).click()
    expect(page.locator("#agent-catalog-status")).to_contain_text("本次获取 2 个模型")
    accept_model_picker(page)
    page.locator("#agent-model-catalog").select_option("fixture-chat")
    main_eval(page, "AgentFixture.catalogStatus = 401")
    page.get_by_role("button", name="获取可用模型", exact=True).click()
    expect(page.locator(".agent-model-catalog [role=alert]")).to_contain_text("401")
    expect(page.locator(".agent-model-catalog [role=alert]")).to_contain_text("上次成功获取的列表已保留")
    expect(page.locator("#agent-model")).to_have_value("fixture-chat")
    expect(page.locator("#agent-key")).to_have_value("fixture-key-for-error")
    expect(page.locator("#agent-model-catalog option")).to_have_count(3)
    assert main_eval(page, "AgentFixture.calls.filter(c=>c.path==='/api/agent/models').length") == 2
    capture_agent(page, "agent-model-catalog-401-mobile")
    # A different address/key is a different catalog, never stale cached choices.
    page.locator("#agent-base").fill("https://another.example/v1")
    expect(page.locator("#agent-model-catalog option")).to_have_count(1)
    expect(page.locator(".agent-model-catalog [role=alert]")).to_have_count(0)
    expect(page.locator("#agent-model")).to_have_value("fixture-chat")
    assert errors == []


def test_agent_empty_model_catalog_still_accepts_manual_model(ui):
    page, errors = ui
    open_agent(page)
    page.get_by_role("tab", name="模型设置", exact=True).click()
    main_eval(page, "AgentFixture.modelCatalog = []")
    page.get_by_role("button", name="获取可用模型", exact=True).click()
    expect(page.locator("#agent-catalog-status")).to_contain_text("上游未返回可选模型")
    expect(page.locator("#agent-model-catalog")).to_be_disabled()
    page.locator("#agent-model").fill("private-deployment-id")
    page.get_by_role("button", name="保存配置", exact=True).click()
    expect(page.get_by_text("配置已保存，后续分析使用新配置。运行中的分析保持原配置。", exact=True)).to_be_visible()
    assert errors == []


def test_agent_missing_local_catalog_route_explains_restart_not_api_key(ui):
    page, errors = ui
    open_agent(page, 375)
    page.get_by_role("tab", name="模型设置", exact=True).click()
    page.locator("#agent-key").fill("fixture-unsaved-key")
    page.locator("#agent-model").fill("fixture-manual-id")
    main_eval(page, "AgentFixture.catalogStatus = 404")
    page.get_by_role("button", name="获取可用模型", exact=True).click()
    error = page.locator(".agent-model-catalog [role=alert]")
    expect(error).to_contain_text("本地工作台的模型目录接口不存在（404）")
    expect(error).to_contain_text("请重启 CreatorHub")
    expect(error).not_to_contain_text("fixture-unsaved-key")
    expect(page.locator("#agent-key")).to_have_value("fixture-unsaved-key")
    expect(page.locator("#agent-model")).to_have_value("fixture-manual-id")
    assert main_eval(page, "AgentFixture.calls.filter(c=>c.path==='/api/agent/models').length") == 1
    error.scroll_into_view_if_needed()
    capture_agent(page, "agent-local-catalog-404")
    assert errors == []


def test_agent_bare_domain_catalog_fallback_does_not_rewrite_generation_config(ui):
    page, errors = ui
    open_agent(page, 375)
    page.get_by_role("tab", name="模型设置", exact=True).click()
    page.locator("#agent-base").fill("https://upstream.example")
    page.locator("#agent-provider").select_option("anthropic")
    main_eval(page, "AgentFixture.catalogPrefixAdded = true")
    page.get_by_role("button", name="获取可用模型", exact=True).click()
    accept_model_picker(page)
    expect(page.locator("#agent-catalog-status")).to_contain_text("目录已使用 /v1/models 路径")
    expect(page.locator("#agent-base")).to_have_value("https://upstream.example")
    expect(page.locator("#agent-provider")).to_have_value("anthropic")
    assert not main_eval(page, "AgentFixture.calls.some(c=>c.method==='PUT' || c.path==='/api/agent/test')")
    assert errors == []


@pytest.mark.parametrize("width,theme", [(1440, "light"), (375, "light"), (375, "dark")])
def test_agent_provider_form_matches_reference_order_and_empty_state(ui, width, theme):
    page, errors = ui
    page.locator(f'[data-theme-choice="{theme}"]').click()
    open_agent(page, width, enabled=False)
    page.get_by_role("tab", name="模型设置", exact=True).click()
    fields = page.locator("#agent-settings-form").evaluate("el => [...el.querySelectorAll('input,select')].slice(0,4).map(field=>field.id)")
    assert fields == ["agent-display-name", "agent-base", "agent-provider", "agent-key"]
    expect(page.locator("#agent-provider option:checked")).to_have_text("OpenAI Chat Completions")
    expect(page.locator(".agent-catalog-empty")).to_contain_text("目录外 ID 仍可直接使用")
    assert not page.locator(".agent-advanced-settings").get_attribute("open")
    expect(page.get_by_role("button", name="取消", exact=True)).to_be_visible()
    expect(page.get_by_role("button", name="保存配置", exact=True)).to_be_visible()
    assert_window_fits(page)
    capture_agent(page, f"agent-provider-form-{width}-{theme}")
    assert errors == []


def test_agent_manually_added_models_and_name_persist_without_secret_echo(ui):
    page, errors = ui
    open_agent(page, 375)
    page.get_by_role("tab", name="模型设置", exact=True).click()
    page.get_by_label("显示名称", exact=True).fill("我的模型服务")
    page.locator("#agent-key").fill("fixture-manual-key")
    page.get_by_role("button", name="添加模型", exact=True).click()
    expect(page.locator("#agent-new-model-id")).to_be_focused()
    expect(page.get_by_role("button", name="保存配置", exact=True)).to_be_disabled()
    page.get_by_role("button", name="保存配置", exact=True).hover()
    page.locator("#agent-new-model-id").fill("manual-private-id")
    page.locator("#agent-new-model-name").fill("自定义模型")
    page.locator("#agent-new-model-name").press("Enter")
    expect(page.locator("#agent-model")).to_have_value("manual-private-id")
    expect(page.locator("#agent-model-catalog option:checked")).to_contain_text("自定义模型")
    assert not main_eval(page, "AgentFixture.calls.some(c=>c.method==='PUT' || c.path==='/api/agent/models')")
    page.get_by_role("button", name="获取可用模型", exact=True).click()
    accept_model_picker(page, select_all=True)
    expect(page.locator("#agent-catalog-status")).to_contain_text("目录共 3 个")
    expect(page.locator("#agent-model-catalog option:checked")).to_contain_text("自定义模型")
    page.get_by_role("button", name="保存配置", exact=True).click()
    expect(page.get_by_role("status").filter(has_text="配置已保存")).to_be_visible()
    expect(page.locator("#agent-key")).to_have_value("")
    # Close and reopen the component after a real reload; re-inject only the
    # already-saved, credential-free fixture configuration before opening it.
    saved = main_eval(page, "AgentFixture.settings")
    assert saved["display_name"] == "我的模型服务" and len(saved["models"]) == 3
    assert saved["api_key_set"] and "api_key" not in saved
    page.reload(wait_until="networkidle")
    page.add_script_tag(content=(Path(__file__).parent / "fixtures/agent-api.js").read_text(encoding="utf-8"))
    main_eval(page, "Object.assign(AgentFixture.settings, " + json.dumps(saved) + ")")
    page.get_by_role("button", name="打开全局 AI 助手", exact=True).click()
    page.get_by_role("tab", name="模型设置", exact=True).click()
    expect(page.locator("#agent-display-name")).to_have_value("我的模型服务")
    expect(page.locator("#agent-model-catalog option")).to_have_count(4)
    expect(page.locator("#agent-model")).to_have_value("manual-private-id")
    expect(page.locator("#agent-key")).to_have_value("")
    expect(page.get_by_label("清除已保存密钥", exact=True)).not_to_be_checked()
    assert errors == []


def test_agent_provider_cancel_restores_saved_fields_and_catalog(ui):
    page, errors = ui
    open_agent(page)
    page.get_by_role("tab", name="模型设置", exact=True).click()
    page.locator("#agent-display-name").fill("未保存名称")
    page.locator("#agent-key").fill("fixture-discarded-key")
    page.get_by_role("button", name="获取可用模型", exact=True).click()
    accept_model_picker(page)
    page.locator("#agent-model-catalog").select_option("fixture-chat")
    page.get_by_role("button", name="取消", exact=True).click()
    expect(page.get_by_role("tab", name="助手", exact=True)).to_have_attribute("aria-selected", "true")
    expect(page.get_by_role("tab", name="助手", exact=True)).to_be_focused()
    page.get_by_role("tab", name="模型设置", exact=True).click()
    expect(page.locator("#agent-display-name")).to_have_value("")
    expect(page.locator("#agent-key")).to_have_value("")
    expect(page.locator("#agent-model")).to_have_value("fixture-model")
    expect(page.locator("#agent-model-catalog option")).to_have_count(1)
    expect(page.locator(".agent-unsaved")).to_have_count(0)
    assert not main_eval(page, "AgentFixture.calls.some(c=>c.method==='PUT')")
    assert errors == []


def test_agent_model_add_validates_duplicates_and_remove_keeps_direct_id(ui):
    page, errors = ui
    open_agent(page, 375)
    page.get_by_role("tab", name="模型设置", exact=True).click()
    page.get_by_role("button", name="获取可用模型", exact=True).click()
    accept_model_picker(page)
    page.get_by_role("button", name="添加模型", exact=True).click()
    page.get_by_role("button", name="添加并使用", exact=True).click()
    expect(page.locator("#agent-model-draft-error")).to_contain_text("有效的模型 ID")
    page.locator("#agent-new-model-id").fill("fixture-chat")
    page.get_by_role("button", name="添加并使用", exact=True).click()
    expect(page.locator("#agent-model-draft-error")).to_contain_text("已在目录中")
    capture_agent(page, "agent-provider-add-model-error")
    page.locator("#agent-new-model-id").press("Escape")
    expect(page.locator("#agent-dialog")).to_be_visible()
    expect(page.get_by_role("button", name="添加模型", exact=True)).to_be_focused()
    page.locator("#agent-model-catalog").select_option("fixture-chat")
    page.get_by_role("button", name="移出目录", exact=True).click()
    expect(page.locator("#agent-model-catalog option")).to_have_count(2)
    expect(page.locator("#agent-model")).to_have_value("fixture-chat")
    page.locator("#agent-model-catalog").select_option("fixture-reasoner")
    page.get_by_role("button", name="移出目录", exact=True).click()
    expect(page.locator("#agent-model-catalog option")).to_have_count(1)
    expect(page.locator("#agent-model")).to_have_value("fixture-reasoner")
    assert errors == []


def test_agent_provider_form_landscape_preserves_footer_and_scroll(ui):
    page, errors = ui
    open_agent(page)
    page.get_by_role("tab", name="模型设置", exact=True).click()
    page.set_viewport_size({"width": 812, "height": 375})
    assert_window_fits(page)
    assert page.locator(".agent-scroll").bounding_box()["height"] >= 80
    for name in ["取消", "保存配置", "测试已保存配置"]:
        button = page.get_by_role("button", name=name, exact=True).bounding_box()
        assert button["y"] >= 0 and button["y"] + button["height"] < 375
    page.locator("#agent-display-name").fill("横屏配置")
    page.locator("#agent-model").fill("landscape-model")
    page.get_by_role("button", name="保存配置", exact=True).click()
    expect(page.get_by_role("status").filter(has_text="配置已保存")).to_be_visible()
    capture_agent(page, "agent-provider-landscape")
    assert errors == []


@pytest.mark.parametrize("width,theme", [(1440, "light"), (375, "light"), (375, "dark"), (320, "light")])
def test_agent_model_picker_selects_subset_and_configures_selected_model(ui, width, theme):
    page, errors = ui
    page.locator(f'[data-theme-choice="{theme}"]').click()
    open_agent(page, width)
    page.get_by_role("tab", name="模型设置", exact=True).click()
    page.get_by_role("button", name="获取可用模型", exact=True).click()
    picker = page.get_by_role("dialog", name="选择模型", exact=True)
    expect(picker).to_be_visible()
    expect(page.locator("#agent-model-search")).to_be_focused()
    expect(page.locator("#agent-model-catalog option")).to_have_count(1)
    assert not main_eval(page, "AgentFixture.calls.some(c=>c.method==='PUT')")
    picker.get_by_role("button", name="取消全选", exact=True).click()
    page.locator("#agent-model-search").fill("reasoner")
    expect(picker.get_by_role("checkbox")).to_have_count(1)
    picker.get_by_role("checkbox", name="选择模型 fixture-reasoner", exact=True).check()
    expect(picker.get_by_role("status")).to_contain_text("已选 1 个")
    box = picker.bounding_box()
    assert box["x"] >= 0 and box["x"] + box["width"] <= width
    assert box["y"] >= 0 and box["y"] + box["height"] <= page.viewport_size["height"]
    assert picker.evaluate("el=>el.scrollWidth <= el.clientWidth + 1")
    capture_agent(page, f"agent-model-picker-{width}-{theme}")
    picker.get_by_role("button", name="更新目录", exact=True).click()
    expect(page.locator(".agent-directory-list>li")).to_have_count(1)
    page.locator("#agent-model-catalog").select_option("fixture-reasoner")
    page.get_by_label("模型 fixture-reasoner 的显示名称", exact=True).fill("问题诊断模型")
    page.get_by_role("button", name="配置模型 fixture-reasoner", exact=True).click()
    expect(page.locator("#agent-new-model-name")).to_have_value("问题诊断模型")
    page.locator("#agent-new-model-id").fill("fixture-custom-id")
    page.get_by_role("button", name="保存模型", exact=True).click()
    expect(page.locator("#agent-model")).to_have_value("fixture-custom-id")
    expect(page.locator("#agent-model-catalog option:checked")).to_contain_text("问题诊断模型")
    page.locator(".agent-directory").scroll_into_view_if_needed()
    assert_window_fits(page)
    capture_agent(page, f"agent-configured-model-{width}-{theme}")
    page.get_by_role("button", name="保存配置", exact=True).click()
    expect(page.get_by_role("status").filter(has_text="配置已保存")).to_be_visible()
    saved = main_eval(page, "AgentFixture.settings")
    assert saved["models"] == [{"id": "fixture-custom-id", "name": "问题诊断模型"}]
    assert saved["model"] == "fixture-custom-id"
    assert errors == []


def test_agent_model_picker_cancel_escape_and_clear_do_not_overwrite_direct_id(ui):
    page, errors = ui
    open_agent(page)
    page.get_by_role("tab", name="模型设置", exact=True).click()
    page.get_by_role("button", name="获取可用模型", exact=True).click()
    picker = page.get_by_role("dialog", name="选择模型", exact=True)
    picker.get_by_role("button", name="取消全选", exact=True).click()
    page.locator("#agent-model-search").fill("no-match")
    expect(picker).to_contain_text("未找到匹配模型")
    page.locator("#agent-model-search").press("Escape")
    expect(picker).to_have_count(0)
    expect(page.locator("#agent-dialog")).to_be_visible()
    expect(page.get_by_role("button", name="获取可用模型", exact=True)).to_be_focused()
    expect(page.locator(".agent-unsaved")).to_have_count(0)
    expect(page.locator("#agent-model-catalog option")).to_have_count(1)
    page.get_by_role("button", name="重新选择模型", exact=True).click()
    accept_model_picker(page)
    expect(page.locator("#agent-model-catalog option")).to_have_count(3)
    page.locator("#agent-model-catalog").select_option("fixture-chat")
    page.get_by_role("button", name="重新选择模型", exact=True).click()
    picker.get_by_role("button", name="取消全选", exact=True).click()
    picker.get_by_role("button", name="取消", exact=True).click()
    expect(page.locator("#agent-model-catalog option")).to_have_count(3)
    page.get_by_role("button", name="重新选择模型", exact=True).click()
    picker.get_by_role("button", name="取消全选", exact=True).click()
    picker.get_by_role("button", name="更新目录", exact=True).click()
    expect(page.locator("#agent-model-catalog option")).to_have_count(1)
    expect(page.locator("#agent-model")).to_have_value("fixture-chat")
    assert not main_eval(page, "AgentFixture.calls.some(c=>c.method==='PUT')")
    assert errors == []


def test_agent_model_picker_large_catalog_is_paged_and_selection_is_bounded(ui):
    page, errors = ui
    open_agent(page)
    page.get_by_role("tab", name="模型设置", exact=True).click()
    page.get_by_role("button", name="添加模型", exact=True).click()
    page.locator("#agent-new-model-id").fill("manual-only")
    page.get_by_role("button", name="添加并使用", exact=True).click()
    main_eval(page, "AgentFixture.modelCatalog = Array.from({length:1000},(_,i)=>({id:'remote-'+String(i).padStart(4,'0'),name:'Remote '+i}))")
    page.get_by_role("button", name="获取可用模型", exact=True).click()
    picker = page.get_by_role("dialog", name="选择模型", exact=True)
    expect(picker.get_by_role("status")).to_contain_text("找到 1001 个模型 · 已选 1 个")
    expect(picker.get_by_role("checkbox")).to_have_count(100)
    picker.get_by_role("button", name="全选", exact=True).click()
    expect(picker.get_by_role("alert")).to_contain_text("最多配置 1000 个模型")
    picker.get_by_role("button", name="取消全选", exact=True).click()
    page.locator("#agent-model-search").fill("remote-")
    picker.get_by_role("button", name="全选搜索结果", exact=True).click()
    expect(picker.get_by_role("status")).to_contain_text("已选 1000 个")
    picker.get_by_role("button", name="下一页可选模型", exact=True).click()
    expect(picker.get_by_role("checkbox", name="选择模型 remote-0100", exact=True)).to_be_checked()
    picker.get_by_role("button", name="更新目录", exact=True).click()
    expect(page.locator("#agent-model-catalog option")).to_have_count(1001)
    expect(page.locator(".agent-directory-list>li")).to_have_count(20)
    expect(page.locator("#agent-model")).to_have_value("manual-only")
    page.get_by_role("button", name="下一页已配置模型", exact=True).click()
    expect(page.get_by_label("模型 remote-0020 的 ID", exact=True)).to_be_visible()
    assert errors == []


def test_agent_model_picker_landscape_keeps_confirmation_visible(ui):
    page, errors = ui
    open_agent(page)
    page.get_by_role("tab", name="模型设置", exact=True).click()
    page.set_viewport_size({"width": 812, "height": 375})
    page.get_by_role("button", name="获取可用模型", exact=True).click()
    picker = page.get_by_role("dialog", name="选择模型", exact=True)
    expect(picker).to_be_visible()
    box = picker.bounding_box()
    assert box["y"] >= 0 and box["y"] + box["height"] <= 375
    button = picker.get_by_role("button", name="更新目录", exact=True).bounding_box()
    assert button["y"] >= box["y"] and button["y"] + button["height"] <= box["y"] + box["height"]
    capture_agent(page, "agent-model-picker-landscape")
    picker.get_by_role("button", name="更新目录", exact=True).click()
    expect(page.locator("#agent-dialog")).to_be_visible()
    assert errors == []


def test_agent_model_picker_preserves_aliases_and_manual_entries_on_refresh(ui):
    page, errors = ui
    open_agent(page)
    page.get_by_role("tab", name="模型设置", exact=True).click()
    page.get_by_role("button", name="获取可用模型", exact=True).click()
    accept_model_picker(page)
    page.get_by_label("模型 fixture-chat 的显示名称", exact=True).fill("我的问答模型")
    page.get_by_role("button", name="添加模型", exact=True).click()
    page.locator("#agent-new-model-id").fill("manual-private")
    page.get_by_role("button", name="添加并使用", exact=True).click()
    main_eval(page, "AgentFixture.modelCatalog = [{id:'fixture-chat',name:'New upstream name'},{id:'new-model',name:'New model'}]")
    page.get_by_role("button", name="获取可用模型", exact=True).click()
    picker = page.get_by_role("dialog", name="选择模型", exact=True)
    expect(picker.get_by_role("status")).to_contain_text("找到 4 个模型 · 已选 3 个")
    expect(picker.get_by_role("checkbox", name="选择模型 manual-private", exact=True)).to_be_checked()
    expect(picker.get_by_role("checkbox", name="选择模型 fixture-reasoner", exact=True)).to_be_checked()
    expect(picker.get_by_role("checkbox", name="选择模型 new-model", exact=True)).not_to_be_checked()
    expect(picker).to_contain_text("我的问答模型")
    picker.get_by_role("checkbox", name="选择模型 new-model", exact=True).check()
    picker.get_by_role("button", name="更新目录", exact=True).click()
    expect(page.get_by_label("模型 fixture-chat 的显示名称", exact=True)).to_have_value("我的问答模型")
    page.get_by_role("button", name="配置模型 manual-private", exact=True).click()
    expect(page.get_by_role("button", name="移出目录", exact=True)).to_be_disabled()
    page.get_by_role("button", name="取消编辑", exact=True).click()
    page.get_by_role("button", name="移除模型 manual-private", exact=True).click()
    expect(page.locator("#agent-model")).to_have_value("manual-private")
    expect(page.locator(".agent-directory-list>li")).to_have_count(3)
    page.get_by_role("button", name="保存配置", exact=True).click()
    expect(page.get_by_role("status").filter(has_text="配置已保存")).to_be_visible()
    saved = main_eval(page, "AgentFixture.settings")
    assert saved["model"] == "manual-private"
    assert saved["models"][0] == {"id": "fixture-chat", "name": "我的问答模型"}
    assert "manual-private" not in [item["id"] for item in saved["models"]]
    assert errors == []


@pytest.mark.parametrize("width,theme", [(1440, "light"), (375, "light"), (375, "dark")])
def test_agent_multiple_channels_create_select_edit_and_delete(ui, width, theme):
    page, errors = ui
    page.locator(f'[data-theme-choice="{theme}"]').click()
    open_agent(page, width)
    page.get_by_role("tab", name="模型设置", exact=True).click()
    page.get_by_role("button", name="添加模型渠道", exact=True).click()
    expect(page.locator("#agent-key")).to_have_value("")
    expect(page.locator("#agent-base")).to_have_value("")
    expect(page.locator("#agent-model")).to_have_value("")
    page.locator("#agent-display-name").fill("备用渠道")
    page.locator("#agent-base").fill("https://second.example")
    page.locator("#agent-provider").select_option("anthropic")
    page.locator("#agent-key").fill("SECOND-FIXTURE-SECRET")
    page.get_by_role("button", name="获取可用模型", exact=True).click()
    accept_model_picker(page)
    assert main_eval(page, "AgentFixture.calls.find(c=>c.path==='/api/agent/models').body.channel_id") == ""
    page.get_by_label("启用 AI 助手", exact=True).check()
    page.get_by_role("button", name="保存配置", exact=True).click()
    expect(page.get_by_role("status").filter(has_text="配置已保存")).to_be_visible()
    assert main_eval(page, "AgentFixture.activeChannelId") == "default"
    assert main_eval(page, "AgentFixture.settings.model") == "fixture-model"
    expect(page.locator("#agent-key")).to_have_value("")
    page.get_by_role("button", name="测试已保存配置", exact=True).click()
    assert main_eval(page, "AgentFixture.calls.filter(c=>c.path.endsWith('/test')).at(-1).path") == "/api/agent/channels/fixture-channel-1/test"
    page.get_by_role("button", name="使用渠道 备用渠道", exact=True).click()
    expect(page.get_by_role("status").filter(has_text="已切换当前渠道")).to_be_visible()
    assert main_eval(page, "AgentFixture.settings.provider") == "anthropic"
    page.get_by_role("button", name="编辑渠道 默认渠道", exact=True).click()
    expect(page.locator("#agent-model")).to_have_value("fixture-model")
    page.locator("#agent-display-name").fill("原渠道")
    page.get_by_role("button", name="使用渠道 默认渠道", exact=True).wait_for(state="visible")
    expect(page.get_by_role("button", name="使用渠道 默认渠道", exact=True)).to_be_disabled()
    page.get_by_role("button", name="保存配置", exact=True).click()
    expect(page.get_by_role("button", name="删除渠道 原渠道", exact=True)).to_be_enabled()
    assert main_eval(page, "AgentFixture.activeChannelId") == "fixture-channel-1"
    page.locator(".agent-channels").scroll_into_view_if_needed()
    capture_agent(page, f"agent-multiple-channels-{width}-{theme}")
    page.get_by_role("button", name="删除渠道 原渠道", exact=True).click()
    confirm = page.get_by_role("dialog", name="删除模型渠道？", exact=True)
    expect(confirm).to_be_visible()
    capture_agent(page, f"agent-channel-delete-{width}-{theme}")
    confirm.get_by_role("button", name="保留渠道", exact=True).click()
    assert main_eval(page, "AgentFixture.channels.length") == 2
    page.get_by_role("button", name="删除渠道 原渠道", exact=True).click()
    if theme == "dark":
        main_eval(page, "AgentFixture.failDeleteOnce = true")
        confirm.get_by_role("button", name="确认删除渠道", exact=True).click()
        expect(confirm.get_by_role("alert")).to_contain_text("删除未完成")
        assert main_eval(page, "AgentFixture.channels.length") == 2
    confirm.get_by_role("button", name="确认删除渠道", exact=True).click()
    expect(confirm).to_have_count(0)
    expect(page.locator(".agent-channel-list>li")).to_have_count(1)
    expect(page.locator("#agent-provider")).to_have_value("anthropic")
    page.get_by_role("tab", name="助手", exact=True).click()
    expect(page.locator("#agent-active-channel")).to_have_value("fixture-channel-1")
    assert_window_fits(page)
    assert errors == []


def test_agent_channels_reload_and_quick_select_keep_saved_configuration(ui):
    page, errors = ui
    open_agent(page)
    page.get_by_role("tab", name="模型设置", exact=True).click()
    page.get_by_role("button", name="添加模型渠道", exact=True).click()
    page.locator("#agent-display-name").fill("第二渠道")
    page.locator("#agent-base").fill("https://second.example/v1")
    page.locator("#agent-model").fill("second-model")
    page.locator("#agent-key").fill("SYNTHETIC-SECRET")
    page.get_by_label("启用 AI 助手", exact=True).check()
    page.get_by_role("button", name="保存配置", exact=True).click()
    expect(page.get_by_role("status").filter(has_text="配置已保存")).to_be_visible()
    page.get_by_role("tab", name="助手", exact=True).click()
    page.locator("#agent-active-channel").select_option("fixture-channel-1")
    expect(page.get_by_role("status").filter(has_text="已切换当前渠道")).to_be_visible()
    saved = main_eval(page, "({channels:AgentFixture.channels,activeChannelId:AgentFixture.activeChannelId,settings:AgentFixture.settings})")
    assert "SYNTHETIC-SECRET" not in json.dumps(saved)
    page.reload(wait_until="networkidle")
    page.add_script_tag(content=(Path(__file__).parent / "fixtures/agent-api.js").read_text(encoding="utf-8"))
    main_eval(page, "(Object.assign(AgentFixture.settings," + json.dumps(saved["settings"]) + "),Object.assign(AgentFixture," + json.dumps({"channels": saved["channels"], "activeChannelId": saved["activeChannelId"]}) + "),true)")
    page.get_by_role("button", name="打开全局 AI 助手", exact=True).click()
    expect(page.locator("#agent-active-channel")).to_have_value("fixture-channel-1")
    page.get_by_role("tab", name="模型设置", exact=True).click()
    expect(page.locator("#agent-model")).to_have_value("second-model")
    expect(page.locator("#agent-key")).to_have_value("")
    expect(page.get_by_label("清除已保存密钥", exact=True)).not_to_be_checked()
    page.get_by_role("tab", name="助手", exact=True).click()
    page.locator("#agent-active-channel").select_option("default")
    expect(page.get_by_role("status").filter(has_text="已切换当前渠道")).to_be_visible()
    assert main_eval(page, "AgentFixture.settings.model") == "fixture-model"
    assert errors == []


def test_agent_new_channel_draft_cancel_and_chat_switch_preserve_active_configuration(ui):
    page, errors = ui
    open_agent(page)
    page.get_by_role("tab", name="模型设置", exact=True).click()
    page.get_by_role("button", name="添加模型渠道", exact=True).click()
    page.locator("#agent-display-name").fill("放弃的新渠道")
    page.locator("#agent-key").fill("DISCARDED-FIXTURE-KEY")
    expect(page.get_by_role("button", name="编辑渠道 默认渠道", exact=True)).to_be_disabled()
    page.get_by_role("tab", name="助手", exact=True).click()
    expect(page.locator("#agent-active-channel")).to_be_disabled()
    page.get_by_role("tab", name="模型设置", exact=True).click()
    page.get_by_role("button", name="取消", exact=True).click()
    expect(page.locator("#agent-active-channel")).to_be_enabled()
    assert main_eval(page, "AgentFixture.channels.length") == 1
    assert not main_eval(page, "AgentFixture.calls.some(c=>c.method!=='GET')")
    page.get_by_role("tab", name="模型设置", exact=True).click()
    expect(page.locator("#agent-model")).to_have_value("fixture-model")
    expect(page.locator("#agent-key")).to_have_value("")
    page.get_by_role("button", name="添加模型", exact=True).click()
    page.locator("#agent-new-model-id").fill("uncommitted-model-draft")
    page.get_by_role("tab", name="助手", exact=True).click()
    expect(page.locator("#agent-active-channel")).to_be_disabled()
    page.get_by_role("tab", name="模型设置", exact=True).click()
    expect(page.locator("#agent-new-model-id")).to_have_value("uncommitted-model-draft")
    page.get_by_role("button", name="取消添加", exact=True).click()
    assert errors == []
