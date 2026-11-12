import React, { useEffect, useRef, useState } from "react";
import { Icon, Tabs } from "./ui/primitives";
import { selectionMarker } from "./ui/motion";
import { ModelDirectory, ModelPicker } from "./agent-models";
import { ChannelList, channelName } from "./agent-channels";
import { ChatMessages, ConversationUsage } from "./agent-chat";
import { watchRun } from "./agent-stream";

const statusText = { queued: "等待分析", running: "分析中", completed: "分析完成", failed: "未完成", canceled: "已停止", interrupted: "服务中断", draft: "待确认", applying: "创建中", applied: "已创建", partial: "部分完成" };
const platformText = { douyin: "抖音", xhs: "小红书", kuaishou: "快手", tiktok: "TikTok", shipinhao: "视频号" };
const taskText = { collections: "采集", publishes: "发布", comments: "评论", actions: "账号操作", monitors: "监控" };
const running = run => run && ["queued", "running"].includes(run.status);
const editableSettings = data => { const { id, ...values } = data; return { display_name: "", models: [], api_path_mode: "auto", reasoning_mode: "default", thinking_budget: 1024, ...values, api_key: "", clear_api_key: false }; };
const newChannel = () => ({ display_name: "", models: [], base_url: "", model: "", provider: "openai_compatible", enabled: false, mcp_enabled: false,
  api_path_mode: "auto", reasoning_mode: "default", thinking_budget: 1024, tool_calling: true, output_token_parameter: "max_tokens", timeout_seconds: 45, max_rounds: 4, max_output_tokens: 2048, api_key_set: false });

async function request(path, options = {}) {
  const response = await fetch("/api/agent" + path, { credentials: "same-origin", ...options,
    headers: { "Content-Type": "application/json", ...options.headers } });
  const result = await response.json().catch(() => ({}));
  if (!response.ok) {
    if (path === "/channels" && response.status === 404) throw new Error("本地后端尚未加载多渠道接口，请重启 CreatorHub 后刷新页面。");
    if (path === "/models" && response.status === 404) {
      throw new Error("本地工作台的模型目录接口不存在（404）。请重启 CreatorHub 后刷新页面；若仍出现，请检查后端地址或反向代理路由。这不是上游密钥校验结果。");
    }
    const detail = result.detail;
    throw new Error(Array.isArray(detail) ? detail.map(item => `${(item.loc || []).join(".")}: ${item.msg}`).join("；")
      : typeof detail === "string" ? detail : `请求失败（${response.status}）`);
  }
  return result;
}
const post = (path, body = {}) => request(path, { method: "POST", body: JSON.stringify(body) });

function WorkflowCard({ workflow, busy, onApply }) {
  const [confirmed, setConfirmed] = useState(false);
  useEffect(() => setConfirmed(false), [workflow.id, workflow.status]);
  return <section className="agent-plan" data-status={workflow.status} aria-label="工作流确认">
    <div className="agent-section-head"><h3>{workflow.plan.title}</h3><span className="agent-status-pill">{statusText[workflow.status] || workflow.status}</span></div>
    <p>{workflow.effect}</p>
    <ol className="agent-plan-steps">{workflow.plan.steps.map((step, index) => <li key={index}>
      <b>{step.kind === "create_collection" ? "创建关键词采集" : "创建持续作品监控"}</b>
      <dl><dt>执行账号</dt><dd>{platformText[step.platform]} · 账号 #{step.account_id}</dd>
        <dt>{step.kind === "create_collection" ? "关键词" : "目标"}</dt><dd>{step.kind === "create_collection" ? step.keywords.join("、") : step.target}</dd>
        {step.kind === "create_monitor" && <><dt>检查间隔</dt><dd>{step.interval_seconds} 秒 · 首次不回填历史</dd></>}
        <dt>作品上限</dt><dd>{step.max_contents} 条 / {step.kind === "create_collection" ? "关键词" : "轮"}</dd>
        <dt>下载媒体</dt><dd>{step.download_media ? "是" : "否，仅记录数据"}</dd></dl>
    </li>)}</ol>
    {!!workflow.plan.notes?.length && <ul>{workflow.plan.notes.map((note, index) => <li key={index}>{note}</li>)}</ul>}
    {!!workflow.plan.unsupported?.length && <div className="agent-notice agent-notice-warn"><b>以下需求尚未包含在计划中</b><ul>{workflow.plan.unsupported.map((note, index) => <li key={index}>{note}</li>)}</ul></div>}
    {!!workflow.results?.length && <div className="agent-notice agent-notice-ok"><b>已创建的任务（后续执行状态以业务页面为准）</b><ul>{workflow.results.map(item => <li key={item.step}>
      步骤 {item.step} · {item.kind === "create_monitor" ? "监控" : "采集"} #{item.id}{item.replayed ? " · 已复用原提交" : ""}
    </li>)}</ul></div>}
    {workflow.error && <p className="agent-error" role="alert">{workflow.error}</p>}
    {workflow.status !== "applied" && <>
      <label className="agent-check"><input type="checkbox" checked={confirmed} disabled={busy || workflow.status === "applying"}
        onChange={event => setConfirmed(event.target.checked)} />我已核对全部参数，确认创建并交由现有引擎执行</label>
      <button type="button" className="agent-plan-submit" disabled={!confirmed || busy || workflow.status === "applying"} onClick={() => onApply(workflow)}>
        {busy ? "正在提交…" : workflow.status === "draft" ? `确认并创建 ${workflow.plan.steps.length} 项任务` : "确认继续未完成步骤"}
      </button>
    </>}
  </section>;
}

// One instance for the whole application. Hiding it never unmounts drafts or runs.
export function AgentAssistant({ page }) {
  const readPage = () => window.CreatorHubBridge?.getAgentContext?.() || page;
  const [open, setOpen] = useState(!!window.CreatorHubOpenAssistant), [expanded, setExpanded] = useState(false);
  const [view, setView] = useState("chat"), [scope, setScope] = useState("follow");
  const [pageContext, setPageContext] = useState(readPage);
  const platform = scope === "follow" ? page.platform : scope;
  const [settings, setSettings] = useState(null), [dirty, setDirty] = useState(false);
  const [channels, setChannels] = useState([]), [activeChannelId, setActiveChannelId] = useState("default"), [editingChannelId, setEditingChannelId] = useState("default");
  const activeSettings = channels.find(item => item.id === activeChannelId) || settings;
  const [catalog, setCatalog] = useState(null), [catalogLoading, setCatalogLoading] = useState(false), [catalogError, setCatalogError] = useState("");
  const [addingModel, setAddingModel] = useState(false), [modelDraft, setModelDraft] = useState({ id: "", name: "" });
  const [modelDraftError, setModelDraftError] = useState("");
  const [pickerOpen, setPickerOpen] = useState(false), [editingModel, setEditingModel] = useState(null);
  const [context, setContext] = useState(null), [history, setHistory] = useState([]);
  const [prompt, setPrompt] = useState(""), [mode, setMode] = useState("chat");
  const [account, setAccount] = useState(""), [task, setTask] = useState("");
  const [run, setRun] = useState(null), [busy, setBusy] = useState(false);
  const [turns, setTurns] = useState([]), [hasOlderTurns, setHasOlderTurns] = useState(false), [forceNew, setForceNew] = useState(false);
  const [chatModel, setChatModel] = useState(""), [historyQuery, setHistoryQuery] = useState("");
  const [newMessages, setNewMessages] = useState(false);
  const [error, setError] = useState(""), [notice, setNotice] = useState("");
  const [streamIssue, setStreamIssue] = useState("");
  const [reload, setReload] = useState(0);
  const epoch = useRef(0), lock = useRef(false), submission = useRef(null), alert = useRef(null);
  const mounted = useRef(true), openRef = useRef(open), launcher = useRef(null), dialog = useRef(null), title = useRef(null);
  const scroll = useRef(null), composing = useRef(false), stickToBottom = useRef(true), composer = useRef(null), chatOptions = useRef(null);
  const pendingContextTask = useRef(null);
  const savedSettings = useRef(null), addModelButton = useRef(null), modelIdInput = useRef(null);
  const fetchModelsButton = useRef(null), tabsList = useRef(null);
  openRef.current = open;
  const updateTurn = next => {
    setRun(next); setTurns(previous => previous.some(item => item.id === next.id)
      ? previous.map(item => item.id === next.id ? next : item) : [...previous, next]);
  };
  const focusComposer = () => requestAnimationFrame(() => composer.current?.focus({ preventScroll: true }));
  const newConversation = () => {
    setRun(null); setTurns([]); setHasOlderTurns(false); setForceNew(false); setError(""); setNotice("");
    submission.current = null; stickToBottom.current = true; setNewMessages(false); setView("chat"); focusComposer();
  };
  const editChannel = (item, focus = true) => {
    savedSettings.current = item; setEditingChannelId(item.id || ""); setSettings(editableSettings(item)); setDirty(false);
    setCatalog(null); setCatalogError(""); setPickerOpen(false); setAddingModel(false); setEditingModel(null); setModelDraft({ id: "", name: "" }); setModelDraftError("");
    setError(""); setNotice("");
    if (focus) requestAnimationFrame(() => document.getElementById("agent-display-name")?.focus());
  };
  const acceptChannels = data => { setChannels(data.channels); setActiveChannelId(data.active_channel_id); };
  const updateSetting = (key, value) => {
    const connectionChanged = ["provider", "base_url", "api_key", "clear_api_key"].includes(key);
    setDirty(true); setSettings(previous => ({ ...previous, ...(connectionChanged ? { models: [] } : {}), ...(key === "provider" ? { reasoning_mode: "default" } : {}), [key]: value })); setNotice("");
    if (connectionChanged) { setCatalog(null); setCatalogError(""); setModelDraftError(""); setPickerOpen(false); setAddingModel(false); setEditingModel(null); }
  };
  const closeModelEditor = () => {
    setAddingModel(false); setEditingModel(null); setModelDraft({ id: "", name: "" }); setModelDraftError("");
    requestAnimationFrame(() => addModelButton.current?.focus({ preventScroll: true }));
  };
  const addModel = () => {
    const id = modelDraft.id.trim(), name = modelDraft.name.trim() || id;
    if (!id || /[\u0000-\u001f\u007f]/.test(id + name)) { setModelDraftError("请填写有效的模型 ID，字段不含控制字符。"); modelIdInput.current?.focus(); return; }
    if (settings.models.some(item => item.id === id && item.id !== editingModel)) { setModelDraftError("该模型已在目录中，请直接选择。"); modelIdInput.current?.focus(); return; }
    if (!editingModel && settings.models.length >= 1000) { setModelDraftError("目录已达 1000 个模型，请先移除不需要的模型。"); return; }
    setSettings(previous => ({ ...previous, model: !editingModel || previous.model === editingModel ? id : previous.model,
      models: editingModel ? previous.models.map(item => item.id === editingModel ? { id, name } : item) : [...previous.models, { id, name }] }));
    setDirty(true); setNotice(""); closeModelEditor();
  };
  const cancelSettings = () => {
    if (!savedSettings.current) return;
    const restored = editingChannelId ? savedSettings.current : activeSettings;
    setEditingChannelId(restored.id || activeChannelId); savedSettings.current = restored;
    setSettings(editableSettings(restored)); setDirty(false); setError(""); setNotice("");
    setCatalog(null); setCatalogError(""); setAddingModel(false); setModelDraft({ id: "", name: "" }); setModelDraftError("");
    setPickerOpen(false); setEditingModel(null);
    setView("chat");
    requestAnimationFrame(() => dialog.current?.querySelector('[role="tab"][data-state="active"]')?.focus({ preventScroll: true }));
  };
  const minimize = () => { setOpen(false); requestAnimationFrame(() => launcher.current?.focus({ preventScroll: true })); };
  const show = () => { setPageContext(readPage()); setOpen(true); };

  useEffect(() => {
    mounted.current = true;
    window.CreatorHubOpenAssistant = false;
    const refreshPage = () => setPageContext(readPage());
    window.addEventListener("creatorhub:assistant:open", show);
    window.addEventListener("creatorhub:agent-context", refreshPage);
    return () => {
      mounted.current = false;
      window.removeEventListener("creatorhub:assistant:open", show);
      window.removeEventListener("creatorhub:agent-context", refreshPage);
    };
  }, []);
  useEffect(() => setPageContext(readPage()), [page.platform, page.tab]);
  useEffect(() => setChatModel(activeSettings?.model || ""), [activeChannelId, activeSettings?.model]);
  useEffect(() => {
    if (!open) return;
    const cleanMarker = selectionMarker(tabsList.current, '[role="tab"][data-state="active"]');
    title.current?.focus({ preventScroll: true });
    // A nonmodal window leaves the workbench usable. Minimize only if keyboard
    // focus moves to a background control that would be covered by this window.
    const revealFocus = event => {
      const target = event.target;
      if (!dialog.current || dialog.current.contains(target) || launcher.current?.contains(target) || target.closest?.("[data-agent-model-picker],[data-agent-channel-dialog]") || !target.getBoundingClientRect) return;
      const a = target.getBoundingClientRect(), b = dialog.current.getBoundingClientRect();
      if (a.width && a.height && a.left >= b.left && a.right <= b.right && a.top >= b.top && a.bottom <= b.bottom) setOpen(false);
    };
    document.addEventListener("focusin", revealFocus);
    return () => { cleanMarker(); document.removeEventListener("focusin", revealFocus); };
  }, [open]);
  useEffect(() => { if (scroll.current) scroll.current.scrollTop = view === "chat" && turns.length ? scroll.current.scrollHeight : 0; }, [view]);
  useEffect(() => { if (addingModel) modelIdInput.current?.focus(); }, [addingModel]);
  useEffect(() => {
    if (openRef.current && scroll.current && view === "chat" && turns.length) {
      if (stickToBottom.current) { scroll.current.scrollTop = scroll.current.scrollHeight; setNewMessages(false); }
      else setNewMessages(true);
    }
  }, [turns, open, view]);
  // Scope changes affect future requests only: the displayed run keeps its own
  // platform/account provenance, and a pending request is not discarded.
  useEffect(() => {
    epoch.current++; setAccount(""); setTask(pendingContextTask.current || ""); setContext(null);
    pendingContextTask.current = null;
  }, [platform]);
  useEffect(() => {
    if (!open) return;
    const controller = new AbortController(), generation = epoch.current;
    Promise.all([
      request(`/context?platform=${encodeURIComponent(platform)}`, { signal: controller.signal }),
      request("/runs?limit=50", { signal: controller.signal }),
      settings ? Promise.resolve(null) : request("/channels", { signal: controller.signal }),
    ]).then(([data, records, config]) => {
      if (controller.signal.aborted || generation !== epoch.current) return;
      setContext(data); setHistory(records);
      if (config) { acceptChannels(config); editChannel(config.channels.find(item => item.id === config.active_channel_id), false); }
    }).catch(exception => { if (exception.name !== "AbortError" && generation === epoch.current) setError(exception.message); });
    return () => controller.abort();
  }, [platform, open, reload]);

  // Stream an existing run even when minimized. Retry GET subscription only;
  // upstream generation is neither restarted nor switched to non-streaming.
  useEffect(() => {
    if (!running(run)) { setStreamIssue(""); return; }
    const controller = new AbortController();
    let timer, retry = 1200;
    const subscribe = async () => {
      try {
        await watchRun(run.id, next => {
          if (controller.signal.aborted) return;
          updateTurn(next); setStreamIssue(""); retry = 1200;
          if (!running(next)) setReload(value => value + 1);
        }, controller.signal);
      } catch (exception) {
        if (exception.name !== "AbortError" && !controller.signal.aborted) {
          setStreamIssue(exception.message);
          if (exception.retryable !== false) { timer = setTimeout(subscribe, retry); retry = Math.min(10000, retry * 2); }
        }
      }
    };
    subscribe();
    return () => { clearTimeout(timer); controller.abort(); };
  }, [run?.id, !!running(run), reload]);

  async function perform(operation) {
    if (lock.current) return;
    lock.current = true; setBusy(true); setError(""); setNotice("");
    const current = () => mounted.current;
    try { return await operation(current); }
    catch (exception) { if (current()) { setError(exception.message); if (openRef.current) requestAnimationFrame(() => alert.current?.focus()); } }
    finally { lock.current = false; if (current()) setBusy(false); }
  }
  const save = event => { event.preventDefault(); perform(async current => {
    const { api_key_set, ...values } = settings;
    const data = await request(editingChannelId ? `/channels/${editingChannelId}` : "/channels", { method: editingChannelId ? "PUT" : "POST", body: JSON.stringify(values) });
    if (current()) { acceptChannels(data); editChannel(data.channel, false); if (data.channel.id === data.active_channel_id) setForceNew(true); setNotice(data.channel.id === data.active_channel_id
      ? "配置已保存，后续分析使用新配置。运行中的分析保持原配置。" : "配置已保存；点击该渠道的「使用」后切换。当前使用的渠道未变。"); }
  }); };
  const activateChannel = id => perform(async current => {
    const data = await post(`/channels/${id}/activate`);
    if (current()) { acceptChannels(data); editChannel(data.channel, false); setForceNew(true); setNotice("已切换当前渠道。运行中的分析保留原渠道；下条消息将开启新对话，不携带旧渠道历史。"); }
  });
  const removeChannel = id => perform(async current => {
    const data = await request(`/channels/${id}`, { method: "DELETE" });
    if (current()) { acceptChannels(data); if (editingChannelId === id) editChannel(data.channels.find(item => item.id === data.active_channel_id), false); setNotice("渠道已删除，分析历史已保留。"); }
    return true;
  });
  const loadModels = () => perform(async current => {
    setCatalogLoading(true); setCatalogError("");
    try {
      const { provider, base_url, api_key, clear_api_key } = settings;
      const data = await post("/models", { provider, base_url, api_key, clear_api_key, channel_id: editingChannelId });
      if (current()) {
        setCatalog(data);
        if (data.models.length) setPickerOpen(true);
      }
    } catch (exception) {
      if (current()) setCatalogError(exception.message);
    } finally {
      if (current()) setCatalogLoading(false);
    }
  });
  const submit = event => { event.preventDefault(); perform(async current => {
    if (!canSend) return;
    const [task_type, id] = task.split(":");
    const body = { prompt, platform, account_id: account ? Number(account) : null, mode, channel_id: activeChannelId, model: chatModel,
      ...(canContinue ? { parent_run_id: run.id } : {}),
      ...(mode === "diagnose" ? { task_type, task_id: Number(id) } : {}) };
    const signature = JSON.stringify(body);
    if (submission.current?.signature !== signature) submission.current = { signature, id: crypto.randomUUID() };
    const data = await post("/runs", { ...body, request_id: submission.current.id });
    submission.current = null; // Keep this ID on failure, so manual retry replays.
    if (current()) {
      stickToBottom.current = true;
      if (!body.parent_run_id) { setTurns([data]); setHasOlderTurns(false); } else setTurns(previous => [...previous, data].slice(-30));
      if (body.parent_run_id && turns.length >= 30) setHasOlderTurns(true);
      setRun(data); setPrompt(""); setForceNew(false); setReload(value => value + 1); focusComposer();
    }
  }); };
  const apply = (workflow, runId) => perform(async current => {
    const updated = await post(`/workflows/${workflow.id}/apply`, { confirmed: true, plan_hash: workflow.plan_hash });
    if (current()) {
      const patch = item => item.id === runId ? { ...item, workflows: item.workflows.map(plan => plan.id === updated.id ? updated : plan) } : item;
      setTurns(previous => previous.map(patch)); setRun(previous => previous ? patch(previous) : previous);
    }
  });
  const loadRun = id => perform(async current => {
    const data = await request(`/runs/${id}/conversation`);
    if (current()) { setTurns(data.runs); setRun(data.runs.at(-1)); setHasOlderTurns(data.has_more); setForceNew(false); stickToBottom.current = true; setView("chat"); focusComposer(); }
  });
  const stop = () => perform(async current => { const data = await post(`/runs/${run.id}/cancel`); if (current()) updateTurn(data); });
  const accounts = context?.accounts?.accounts || [];
  const tasks = (context?.tasks?.tasks || []).filter(item => !account || String(item.account_id) === account);
  const currentTask = pageContext?.task;
  const useCurrentTask = () => {
    if (platform !== page.platform) pendingContextTask.current = `${currentTask.task_type}:${currentTask.id}`;
    setScope("follow"); setAccount(""); setMode("diagnose"); setView("chat");
    if (chatOptions.current) chatOptions.current.open = true;
    setTask(`${currentTask.task_type}:${currentTask.id}`);
    if (!prompt.trim()) setPrompt(`分析${taskText[currentTask.task_type]}任务 #${currentTask.id} 的状态、可能原因和处理步骤。`);
  };
  const availableTasks = [...tasks];
  if (currentTask && (!platform || platform === page.platform) && !account &&
      !tasks.some(item => item.task_type === currentTask.task_type && item.id === currentTask.id)) availableTasks.unshift(currentTask);
  const metrics = context?.metrics;
  const counts = Object.values(metrics?.task_status_counts || {});
  const total = counts.reduce((sum, group) => sum + Object.values(group).reduce((a, b) => a + b, 0), 0);
  const failures = counts.reduce((sum, group) => sum + (group.failed || 0) + (group.partial || 0) + (group.uncertain || 0), 0);
  const canContinue = !!run?.can_continue && !forceNew && !running(run) && run.platform === platform
    && (run.account_id || null) === (account ? Number(account) : null) && run.mode === mode && run.channel_id === activeChannelId
    && (mode !== "diagnose" || `${run.task_type}:${run.task_id}` === task);
  const canSend = !busy && !dirty && !addingModel && !running(run) && activeSettings?.enabled && !!chatModel.trim() && !!prompt.trim() && (mode !== "diagnose" || !!task);
  const modelChoices = [...(activeSettings?.models || [])];
  if (activeSettings?.model && !modelChoices.some(item => item.id === activeSettings.model)) modelChoices.unshift({ id: activeSettings.model, name: activeSettings.model });
  const conversationMap = new Map();
  history.forEach(item => { const id = item.conversation_id || item.id; if (!conversationMap.has(id)) conversationMap.set(id, item); });
  const conversationList = [...conversationMap.values()].filter(item => `${item.conversation_title || ""} ${item.prompt}`.toLocaleLowerCase().includes(historyQuery.trim().toLocaleLowerCase()));

  return <div className="agent-global">
    <section id="agent-dialog" className={`agent-window agent-workspace${view === "settings" ? " is-configuring" : ""}${expanded ? " is-expanded" : ""}`} hidden={!open}
      role="dialog" aria-modal="false" aria-labelledby="agent-title" aria-describedby="agent-description" ref={dialog}
      onKeyDown={event => { if (event.key === "Escape" && !event.defaultPrevented) { event.preventDefault(); event.stopPropagation(); minimize(); } }}>
      <header className="agent-window-header">
        <span className="agent-avatar"><Icon name="automation" /></span>
        <div className="agent-window-heading"><h2 id="agent-title" tabIndex={-1} ref={title}>AI 助手</h2><p id="agent-description">全局助手 · 分析问题，协助行动</p></div>
        <button type="button" className="ghost agent-icon-button" data-spinning={busy || undefined} disabled={busy} aria-label="刷新助手数据与记录" title="刷新数据与记录"
          onClick={() => { setError(""); setReload(value => value + 1); }}><Icon name="refresh" /></button>
        <button type="button" className="ghost agent-icon-button agent-expand" aria-label={expanded ? "恢复浮窗大小" : "展开助手"} title={expanded ? "恢复浮窗大小" : "展开助手"}
          aria-pressed={expanded} onClick={() => setExpanded(value => !value)}><Icon name={expanded ? "overview" : "scan"} /></button>
        <button type="button" className="ghost agent-icon-button" aria-label="收起 AI 助手" title="收起（保留输入与分析）" onClick={minimize}><Icon name="chevron" /></button>
      </header>
      <Tabs.Root value={view} onValueChange={setView} className="agent-tabs-root">
        <Tabs.List className="agent-tabs" ref={tabsList} aria-label="助手功能">
          <Tabs.Trigger value="chat"><Icon name="sparkles" />助手</Tabs.Trigger>
          <Tabs.Trigger value="history"><Icon name="clock" />历史记录</Tabs.Trigger>
          <Tabs.Trigger value="settings" aria-label="模型设置" title={dirty ? "模型设置（有未保存更改）" : "模型设置"}><Icon name="settings" />模型设置{dirty && <span className="agent-unsaved" aria-hidden="true" />}</Tabs.Trigger>
        </Tabs.List>
        {error && <div className="agent-error agent-banner" role="alert" tabIndex={-1} ref={alert}>{error}</div>}
        {streamIssue && <div className="agent-notice agent-banner agent-stream-issue" role="status">{streamIssue}</div>}
        {notice && <div className="agent-notice agent-banner" role="status">{notice}</div>}
        <div className="agent-chat-toolbar" hidden={view !== "chat"}>
          <label htmlFor="agent-active-channel"><span>当前渠道</span><select id="agent-active-channel" value={activeChannelId} disabled={busy || dirty || addingModel}
            onChange={event => activateChannel(event.target.value)}>{channels.map(item => <option key={item.id} value={item.id} disabled={!item.base_url || !item.model}>{channelName(item)}{!item.enabled ? "（未启用）" : ""}</option>)}</select></label>
          <label htmlFor="agent-chat-model"><span>本次模型</span><select id="agent-chat-model" value={chatModel} disabled={busy || dirty || addingModel || !modelChoices.length} onChange={event => setChatModel(event.target.value)}>
            {!modelChoices.length && <option value="">请先配置模型</option>}{modelChoices.map(item => <option key={item.id} value={item.id}>{item.name || item.id}</option>)}</select></label>
          <button type="button" className="ghost agent-icon-button" disabled={busy || running(run)} aria-label="新建对话" title="新建对话（保留输入草稿）" onClick={newConversation}><Icon name="plus" /></button>
        </div>
        <div className="agent-scroll" ref={scroll} onScroll={() => {
          if (view !== "chat" || !scroll.current) return;
          stickToBottom.current = scroll.current.scrollHeight - scroll.current.scrollTop - scroll.current.clientHeight < 72;
          if (stickToBottom.current) setNewMessages(false);
        }}>
          <Tabs.Content value="chat" forceMount hidden={view !== "chat"} className="agent-chat">
            <details className="agent-chat-options" ref={chatOptions}><summary>对话选项 · {mode === "diagnose" ? "任务诊断" : mode === "workflow" ? "工作流草稿" : "数据问答"} · {platformText[platform] || "全平台"}</summary>
              <div className="agent-form">
                <div className="agent-scope-row"><label htmlFor="agent-scope">分析范围</label><select id="agent-scope" value={scope} onChange={event => setScope(event.target.value)} disabled={busy}>
                  <option value="follow">跟随当前页面 · {platformText[page.platform] || "全平台"}</option><option value="">全平台</option>{Object.entries(platformText).map(([key, name]) => <option key={key} value={key}>{name}</option>)}</select></div>
                <div className="agent-fields"><label htmlFor="agent-mode">工作模式<select id="agent-mode" value={mode} onChange={event => setMode(event.target.value)} disabled={busy}>
                  <option value="chat">数据问答 / 报告</option><option value="diagnose">任务诊断</option><option value="workflow">生成工作流草稿</option></select></label>
                  <label htmlFor="agent-account">分析账号<select id="agent-account" value={account} onChange={event => { setAccount(event.target.value); setTask(""); }} disabled={busy}>
                    <option value="">{platform ? "当前范围全部账号" : "全平台全部账号"}</option>{accounts.map(item => <option key={item.id} value={item.id}>#{item.id} · {item.nickname || "未命名"}{!platform ? ` · ${platformText[item.platform]}` : ""}</option>)}</select></label></div>
                {mode === "diagnose" && <label htmlFor="agent-task">诊断任务<select id="agent-task" value={task} onChange={event => setTask(event.target.value)} disabled={busy}>
                  <option value="">选择最近的任务</option>{availableTasks.map(item => <option key={`${item.task_type}:${item.id}`} value={`${item.task_type}:${item.id}`}>
                    {taskText[item.task_type]} #{item.id} · {item.status || (item.enabled ? "启用" : "暂停")}{!platform && item.platform ? ` · ${platformText[item.platform]}` : ""}{item.error || item.last_error ? " · 有错误记录" : ""}</option>)}</select></label>}
                <details className="agent-details agent-local-summary"><summary>本地数据摘要{metrics ? ` · ${metrics.accounts} 个账号 / ${total} 项任务` : ""}</summary>
                  <p className="field-help">近 7 天创建的任务中，失败 / 部分完成 / 结果不确定：{metrics ? failures : "—"} 项。按 UTC 创建时间筛选，统计当前状态，不代表历史成功率。数据未实时向平台刷新；账号选择仅限制本次分析。</p></details>
              </div>
            </details>
            <p className="agent-page-context">当前页面：{pageContext?.title || "工作台"}{scope === "follow" ? " · 跟随页面范围" : " · 使用指定范围"}</p>
            {currentTask && <button type="button" className="ghost agent-context-action" onClick={useCurrentTask} disabled={busy}>带入当前任务进行诊断 <Icon name="next" /></button>}
            {activeSettings && !activeSettings.enabled && <div className="agent-setup-notice"><span>先配置并启用模型，即可开始对话。</span><button type="button" className="ghost" onClick={() => setView("settings")}>配置模型 <Icon name="next" /></button></div>}
            {!turns.length && <div className="agent-welcome"><span className="agent-welcome-mark"><Icon name="sparkles" /></span><h3>有什么可以帮你？</h3><p>聊数据、查原因，把需求变成待确认的行动。</p>
              <div className="agent-suggestions">{[
                ["分析最近异常", "chat", "分析最近的失败任务，列出证据、可能原因和修复验证步骤。"],
                ["汇总运行情况", "chat", "总结最近七天的任务与账号指标，说明异常和数据缺口。"],
                ["创建工作流", "workflow", "帮我规划关键词采集和作品监控；缺少的参数请先问我。"],
              ].map(([label, nextMode, value]) => <button key={label} type="button" className="ghost" disabled={busy} onClick={() => { setMode(nextMode); setPrompt(value); focusComposer(); }}>{label}<Icon name="next" /></button>)}</div>
            </div>}
            {hasOlderTurns && <p className="field-help">当前显示最近 30 轮消息；更早记录仍保留在本地。</p>}
            <ChatMessages turns={turns} busy={busy || running(run)} statusText={statusText} platformText={platformText}
              onRetry={value => { setPrompt(value); focusComposer(); }} renderWorkflow={(workflow, runId) => <WorkflowCard workflow={workflow} busy={busy} onApply={item => apply(item, runId)} />} />
            <ConversationUsage turns={turns} />
          </Tabs.Content>
          <Tabs.Content value="history" forceMount hidden={view !== "history"}>
            <div className="agent-section-head"><h3>历史对话</h3><button type="button" className="ghost agent-new-chat-btn" disabled={busy || running(run)} onClick={newConversation}><Icon name="plus" />新建对话</button></div>
            <p className="field-help">打开对话不会重新调用模型。最近记录按对话归组；同范围、同渠道可继续追问。</p>
            <label className="agent-history-search" htmlFor="agent-history-search">搜索对话<input id="agent-history-search" type="search" maxLength={200} value={historyQuery} placeholder="搜索标题或最近的问题" onChange={event => setHistoryQuery(event.target.value)} /></label>
            {!conversationList.length ? <div className="agent-empty"><Icon name="clock" /><p>{historyQuery ? "没有匹配的对话" : "还没有对话记录"}</p></div> : <ul className="agent-history">{conversationList.map(item => <li key={item.id}><button type="button" className="ghost" disabled={busy || running(run)}
              aria-pressed={(run?.conversation_id || run?.id) === (item.conversation_id || item.id)} onClick={() => loadRun(item.id)}><span>{item.conversation_title || item.prompt}</span><small>{platformText[item.platform] || "全平台"} · {item.model_name} · {statusText[item.status] || item.status}</small></button></li>)}</ul>}
          </Tabs.Content>
          <Tabs.Content value="settings" forceMount hidden={view !== "settings"}>
            {!!channels.length && <ChannelList channels={channels} activeId={activeChannelId} editingId={editingChannelId} busy={busy} dirty={dirty || addingModel}
              onEdit={editChannel} onActivate={activateChannel} onDelete={removeChannel} onAdd={() => { editChannel(newChannel()); setDirty(true); }} />}
            {settings && <p className="agent-channel-editing">{editingChannelId ? `正在编辑：${channelName({ ...settings, id: editingChannelId })}${editingChannelId === activeChannelId ? " · 当前渠道" : " · 非当前渠道"}` : "添加新渠道 · 保存后再选择使用"}</p>}
            {!settings ? <p role="status">{error ? "配置读取失败，请点击顶部刷新重试。" : "正在读取配置…"}</p> : <form id="agent-settings-form" className="agent-form agent-provider-form" onSubmit={save}>
              <label htmlFor="agent-display-name">显示名称<input id="agent-display-name" value={settings.display_name} maxLength={80} placeholder="例如：我的模型服务"
                onChange={event => updateSetting("display_name", event.target.value)} disabled={busy} /></label>
              <label htmlFor="agent-base">API 地址<input id="agent-base" type="url" value={settings.base_url} maxLength={500} placeholder="https://HOST/v1" aria-describedby="agent-base-hint"
                onChange={event => updateSetting("base_url", event.target.value)} disabled={busy} /></label>
              <p id="agent-base-hint" className="field-help agent-input-hint">可填域名或带 /v1 等前缀的基础地址，不带 /models 或查询参数。自动模式下，纯域名的推理接口使用 /v1 前缀；自定义根路径接口可在高级设置选「原样拼接」。</p>
              <label htmlFor="agent-provider" className="agent-protocol-field">API 协议<select id="agent-provider" value={settings.provider} onChange={event => updateSetting("provider", event.target.value)} disabled={busy}>
                <option value="openai_compatible">OpenAI Chat Completions</option><option value="openai_responses">OpenAI Responses</option><option value="anthropic">Anthropic Messages</option></select></label>
              <label htmlFor="agent-key">API 密钥<input id="agent-key" type="password" autoComplete="off" value={settings.api_key} maxLength={4096}
                placeholder={settings.api_key_set ? "已保存，留空保留" : "输入 API 密钥；本地无鉴权服务可留空"} onChange={event => updateSetting("api_key", event.target.value)} disabled={busy || settings.clear_api_key} /></label>
              {settings.api_key_set && <div className="agent-key-options"><label className="agent-check"><input type="checkbox" checked={settings.clear_api_key}
                onChange={event => updateSetting("clear_api_key", event.target.checked)} disabled={busy} />清除已保存密钥</label>
                <p className="field-help">更换地址或协议需重新输入或清除密钥。</p></div>}
              <div className="agent-model-catalog" aria-busy={catalogLoading}>
                <div className="agent-section-head"><label htmlFor="agent-model-catalog">模型目录</label>
                  <button type="button" className="ghost" ref={fetchModelsButton} data-spinning={catalogLoading || undefined} disabled={busy || addingModel || !settings.base_url.trim()} onClick={loadModels} aria-label={catalogLoading ? "正在获取…" : "获取可用模型"} title="使用当前地址和密钥获取目录，无需先保存，不调用生成接口">
                    <Icon name="refresh" />{catalogLoading ? "正在获取…" : "获取可用模型"}</button></div>
                {!settings.models.length && <div className="agent-catalog-empty">模型目录为空，可获取上游模型或手动添加。<br />目录外 ID 仍可直接使用。</div>}
                {!!settings.models.length && <ModelDirectory models={settings.models} currentModel={settings.model} busy={busy || addingModel}
                  onRename={(id, name) => updateSetting("models", settings.models.map(item => item.id === id ? { ...item, name } : item))}
                  onRemove={id => updateSetting("models", settings.models.filter(item => item.id !== id))}
                  onEdit={item => { setEditingModel(item.id); setModelDraft({ id: item.id, name: item.name }); setModelDraftError(""); setAddingModel(true); }} />}
                {!!settings.models.length && <label htmlFor="agent-model-catalog">当前使用的模型</label>}
                <select id="agent-model-catalog" hidden={!settings.models.length} value={settings.models.some(item => item.id === settings.model) ? settings.model : ""}
                  disabled={busy || !settings.models.length} onChange={event => { if (event.target.value) updateSetting("model", event.target.value); }} aria-describedby="agent-catalog-status">
                  <option value="">{settings.models.length ? "选择模型，或在下方直接填写 ID" : "暂未添加模型"}</option>
                  {settings.models.map(item => <option key={item.id} value={item.id}>{item.name && item.name !== item.id ? `${item.name} · ${item.id}` : item.id}</option>)}
                </select>
                <p id="agent-catalog-status" className="field-help" role="status">{catalogLoading ? "正在读取上游模型目录…" : catalog
                  ? `本次获取 ${catalog.count} 个模型${catalog.truncated ? "（列表可能不完整）" : ""}${catalog.skipped ? `，已忽略 ${catalog.skipped} 个无效条目` : ""}。${catalog.count ? `目录共 ${settings.models.length} 个，勾选更新后再保存配置。` : "上游未返回可选模型，仍可手动填写模型 ID。"}${catalog.path_prefix_added ? "目录已使用 /v1/models 路径；推理路径由高级设置中的路径模式决定。" : ""}`
                  : settings.models.length ? `目录共 ${settings.models.length} 个模型。` : ""}</p>
                {catalogError && <div className="agent-error" role="alert">{catalogError}{catalog?.models.length ? " 上次成功获取的列表已保留。" : ""}</div>}
                <div className="agent-catalog-actions"><button type="button" className="ghost" ref={addModelButton} aria-expanded={addingModel} aria-controls="agent-add-model" disabled={busy || addingModel}
                  onClick={() => { setModelDraftError(""); setAddingModel(true); }}><Icon name="plus" />添加模型</button>
                  {!!catalog?.models.length && <button type="button" className="ghost" disabled={busy || addingModel} onClick={() => setPickerOpen(true)}>重新选择模型</button>}
                  {settings.models.some(item => item.id === settings.model) && <button type="button" className="ghost" disabled={busy || addingModel} aria-label="移出目录" title="仅移出目录，保留当前使用的 ID"
                    onClick={() => updateSetting("models", settings.models.filter(item => item.id !== settings.model))}>移出目录</button>}</div>
                {addingModel && <div id="agent-add-model" className="agent-model-editor" role="group" aria-label={editingModel ? "配置模型" : "添加模型"}
                  onKeyDown={event => { if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); closeModelEditor(); }
                    else if (event.key === "Enter" && event.target.tagName === "INPUT") { event.preventDefault(); if (!busy) addModel(); } }}>
                  <label htmlFor="agent-new-model-id">模型 ID<input id="agent-new-model-id" ref={modelIdInput} maxLength={160} value={modelDraft.id} placeholder="输入上游模型 ID" disabled={busy}
                    aria-invalid={!!modelDraftError} aria-describedby={modelDraftError ? "agent-model-draft-error" : undefined}
                    onChange={event => { setModelDraft(previous => ({ ...previous, id: event.target.value })); setModelDraftError(""); }} /></label>
                  <label htmlFor="agent-new-model-name">模型显示名称（可选）<input id="agent-new-model-name" maxLength={200} value={modelDraft.name} placeholder="留空使用模型 ID" disabled={busy}
                    onChange={event => setModelDraft(previous => ({ ...previous, name: event.target.value }))} /></label>
                  {modelDraftError && <p id="agent-model-draft-error" className="agent-error" role="alert">{modelDraftError}</p>}
                  <div className="agent-catalog-actions"><button type="button" className="ghost" disabled={busy} onClick={closeModelEditor}>{editingModel ? "取消编辑" : "取消添加"}</button><button type="button" disabled={busy} onClick={addModel}>{editingModel ? "保存模型" : "添加并使用"}</button></div>
                </div>}
                <label htmlFor="agent-model">当前使用的模型 ID<input id="agent-model" value={settings.model} maxLength={160} placeholder="从目录选择，或直接填写模型 ID"
                  onChange={event => updateSetting("model", event.target.value)} disabled={busy} /></label>
              </div>
              <label className="agent-check"><input type="checkbox" checked={settings.enabled} onChange={event => updateSetting("enabled", event.target.checked)} disabled={busy} />启用 AI 助手</label>
              <details className="agent-details agent-advanced-settings"><summary>高级设置 · 工具调用与 MCP</summary><div className="agent-form">
                <label htmlFor="agent-path-mode">推理接口路径<select id="agent-path-mode" value={settings.api_path_mode} onChange={event => updateSetting("api_path_mode", event.target.value)} disabled={busy}>
                  <option value="auto">自动：纯域名补 /v1，已有前缀保留</option><option value="exact">原样拼接：保留自定义根路径</option></select></label>
                <label className="agent-check"><input type="checkbox" checked={settings.tool_calling} onChange={event => updateSetting("tool_calling", event.target.checked)} disabled={busy} />模型支持原生工具调用</label>
                <p className="field-help">模型目录不代表工具能力已通过验证。不支持工具调用时可关闭此项。</p>
                <label htmlFor="agent-reasoning-mode">推理 / 思考请求<select id="agent-reasoning-mode" value={settings.reasoning_mode} onChange={event => updateSetting("reasoning_mode", event.target.value)} disabled={busy} aria-describedby="agent-reasoning-hint">
                  <option value="default">跟随上游默认（不附加参数）</option>
                  {settings.provider === "openai_responses" ? <option value="summary">请求可展示的推理摘要</option> : settings.provider === "anthropic" ? <><option value="adaptive">自适应思考（adaptive）</option><option value="enabled">预算思考（enabled，旧版模型）</option></> : <option value="enabled">启用 thinking 参数（如 DeepSeek 兼容接口）</option>}
                </select></label>
                <p id="agent-reasoning-hint" className="field-help">仅在所选模型与渠道支持时启用；可能增加耗时与消耗。所有模式均使用流式请求，随上游事件显示推理内容与回复。仅有加密内容或上游未返回时会明确提示；不生成模拟思考过程，也不回退到非流式。</p>
                {settings.provider === "anthropic" && settings.reasoning_mode === "enabled" && <label htmlFor="agent-thinking-budget">思考预算（token）<input id="agent-thinking-budget" type="number" min="1024" max={Math.min(7168, settings.max_output_tokens - 1)} value={settings.thinking_budget} disabled={busy} onChange={event => updateSetting("thinking_budget", Number(event.target.value))} /><span className="field-help">预算须小于下方单次输出上限，给最终答复预留空间。</span></label>}
                <div className="agent-fields agent-fields-3">
                  <label htmlFor="agent-rounds">最多分析轮数<input id="agent-rounds" type="number" min="1" max="6" value={settings.max_rounds} onChange={event => updateSetting("max_rounds", Number(event.target.value))} disabled={busy} /></label>
                  <label htmlFor="agent-timeout">单次超时（秒）<input id="agent-timeout" type="number" min="5" max="90" value={settings.timeout_seconds} onChange={event => updateSetting("timeout_seconds", Number(event.target.value))} disabled={busy} /></label>
                  <label htmlFor="agent-tokens">单次输出上限<input id="agent-tokens" type="number" min="256" max="8192" value={settings.max_output_tokens} onChange={event => updateSetting("max_output_tokens", Number(event.target.value))} disabled={busy} /></label>
                </div>
                <label htmlFor="agent-token-param">兼容接口输出参数<select id="agent-token-param" value={settings.output_token_parameter} onChange={event => updateSetting("output_token_parameter", event.target.value)} disabled={busy}>
                  <option value="max_tokens">max_tokens</option><option value="max_completion_tokens">max_completion_tokens</option></select></label>
              <label className="agent-check"><input type="checkbox" checked={settings.mcp_enabled} onChange={event => updateSetting("mcp_enabled", event.target.checked)} disabled={busy} />开放本机 MCP 查询</label>
              <p className="field-help">启用开关、工具调用和 MCP 随渠道保存与切换。MCP 使用独立 stdio 进程，仅开放 5 个查询工具。安装与客户端配置见使用指南。</p>
              </div></details>
            </form>}
          </Tabs.Content>
        </div>
      </Tabs.Root>
      <button type="button" className="ghost agent-jump-latest" hidden={!newMessages || view !== "chat"} onClick={() => { stickToBottom.current = true; scroll.current.scrollTop = scroll.current.scrollHeight; setNewMessages(false); }}>查看最新消息 ↓</button>
      <div className="agent-composer-dock" hidden={view !== "chat"}>
        <form id="agent-composer-form" onSubmit={submit}>
          <label className="agent-compose-label" htmlFor="agent-prompt"><span>问题或需求</span><textarea id="agent-prompt" ref={composer} rows={2} maxLength={4000} required value={prompt}
            placeholder={mode === "workflow" ? "描述想创建的任务，先看草稿再确认…" : turns.length ? "继续追问，或描述新的问题…" : "输入消息，Enter 发送…"}
            onChange={event => setPrompt(event.target.value)} disabled={busy}
            onCompositionStart={() => { composing.current = true; }} onCompositionEnd={() => { composing.current = false; }}
            onKeyDown={event => { if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing && !composing.current && event.nativeEvent.keyCode !== 229) {
              event.preventDefault(); if (canSend) event.currentTarget.form.requestSubmit();
            } }} /></label>
          <div className="agent-compose-controls"><span className="agent-compose-state">{dirty || addingModel ? "模型配置有未保存更改，请先保存" : !activeSettings?.enabled ? "请先配置并启用模型" : running(run) ? "分析中 · 可停止或收起" : turns.length ? canContinue ? "连续对话 · 最多参考最近 8 轮" : "范围或渠道已变化 · 下条将新建对话" : "仅发送所选范围的脱敏信息"}</span>
            {running(run) ? <button type="button" className="agent-send-button agent-stop-button" disabled={busy} onClick={stop}><Icon name="x" />停止分析</button> : <button type="submit" className="agent-send-button" disabled={!canSend}><Icon name="send" />{busy ? "发送中…" : mode === "workflow" ? "生成草稿" : "发送"}</button>}
          </div>
        </form>
        <p className="agent-keyboard-help">Enter 发送 · Shift+Enter 换行 · 操作前确认</p>
      </div>
      <div className="agent-settings-footer" hidden={view !== "settings" || !settings}>
        <button type="button" className="ghost agent-test-connection" disabled={busy || dirty || !editingChannelId || !settings?.base_url || !settings?.model}
          onClick={() => perform(async current => { const data = await post(`/channels/${editingChannelId}/test`); if (current()) (data.ok ? setNotice : setError)(data.detail); })}>测试已保存配置</button>
        <button type="button" className="ghost" disabled={busy} onClick={cancelSettings}>取消</button>
        <button type="submit" form="agent-settings-form" disabled={busy || addingModel} aria-label="保存配置" title={addingModel ? "请先添加或取消当前模型" : undefined}>{busy ? "处理中…" : "保存配置"}</button>
      </div>
      <footer className="agent-window-footer"><Icon name="shield" /><span>基于证据分析 · 操作前确认</span><span>{running(run) ? "分析中" : "CreatorHub"}</span></footer>
    </section>
    {settings && <ModelPicker open={pickerOpen && open && view === "settings"} available={catalog?.models || []} configured={settings.models}
      returnFocus={fetchModelsButton} onOpenChange={setPickerOpen} onApply={models => {
        setSettings(previous => ({ ...previous, models, model: previous.model || models[0]?.id || "" })); setDirty(true); setNotice("");
      }} />}
    <button id="agent-launcher" type="button" ref={launcher} className="agent-launcher" aria-label={open ? "收起全局 AI 助手" : "打开全局 AI 助手"}
      aria-expanded={open} aria-haspopup="dialog" aria-controls="agent-dialog" onClick={() => open ? minimize() : show()}>
      <Icon name="automation" /><span>{open ? "收起助手" : running(run) ? "正在分析…" : "AI 助手"}</span>
      {running(run) && <span className="agent-running-dot" aria-hidden="true" />}
    </button>
  </div>;
}
