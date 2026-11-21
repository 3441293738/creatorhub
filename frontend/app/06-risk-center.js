// 06-risk-center.js — 风控中心看板、操作节奏参数配置、人工验证解除与审计日志。
// ═══════════ 风控中心 ═══════════
let RISK_ACCOUNTS = [];
let RISK_CONFIG = null;

function riskDate(value) {
  if (!value) return null;
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? null : date;
}
function riskTime(value) {
  const date = riskDate(value);
  return date ? date.toLocaleString() : "—";
}
function autoRunHint(value) {
  const date = riskDate(value);
  if (!date) return "";
  const label = date.getTime() > Date.now() ? `下次最早 ${riskTime(value)}` : "已到期，等待调度";
  return `<div class="mut" title="自动任务的最早调度时间；仍需满足账号风控、并发和队列条件">${esc(label)}</div>`;
}
function riskDuration(seconds) {
  let left = Math.max(0, Math.ceil(Number(seconds) || 0));
  if (!left) return "已到期";
  const days = Math.floor(left / 86400); left %= 86400;
  const hours = Math.floor(left / 3600); left %= 3600;
  const minutes = Math.floor(left / 60), remainder = left % 60;
  return [days ? `${days}天` : "", hours ? `${hours}小时` : "", minutes ? `${minutes}分钟` : "",
    !days && !hours && remainder ? `${remainder}秒` : ""].filter(Boolean).join("");
}
function riskRemaining(value) {
  const date = riskDate(value);
  return date ? riskDuration((date.getTime() - Date.now()) / 1000) : "—";
}
function riskPlatformLabel(platform) { return PF_NAME[platform] || platform || "—"; }
const RISK_KIND_LABELS = {
  read_light: "轻量读取", read_heavy: "重读取", download: "下载", publish: "发布",
  comment: "评论", social: "关注操作", dm: "私信", login: "登录",
};
const RISK_OUTCOME_LABELS = {
  success: "成功", risk: "平台风控", auth: "登录失效", network: "网络异常",
  business: "业务异常", manual: "人工操作",
};

async function refreshRiskCenter(force = false, announce = force) {
  const isCurrent = beginViewRequest("risk-center");
  try {
    const shouldFillConfig = !RISK_CONFIG || force;
    const configPromise = shouldFillConfig ? api("/api/risk-control/config") : Promise.resolve(RISK_CONFIG);
    const platformQuery = "?platform=" + encodeURIComponent(PLATFORM);
    const [summary, accounts, config] = await Promise.all([
      api("/api/risk-control/summary" + platformQuery),
      api("/api/risk-control/accounts" + platformQuery),
      configPromise,
    ]);
    if (!isCurrent()) return;
    RISK_ACCOUNTS = accounts;
    RISK_CONFIG = config;
    renderRiskSummary(summary);
    renderRiskAccounts();
    if (shouldFillConfig) fillRiskConfig(config);
    if (announce) toast("风控状态已刷新", "ok");
  } catch (e) {
    if (isCurrent() && (force || CURRENT_TAB === "risk-control")) toast("风控中心加载失败：" + e.message, "err");
  }
}

function renderRiskSummary(summary) {
  const counts = summary.counts || {};
  $("risk-stat-normal").textContent = counts.normal || 0;
  $("risk-stat-cooldown").textContent = (counts.cooldown || 0) + (counts.network_circuit || 0) + (counts.write_paused || 0) + (counts.network_backoff || 0);
  $("risk-stat-recovering").textContent = counts.recovering || 0;
  $("risk-stat-invalid").textContent = (counts.auth_invalid || 0) + (counts.proxy_error || 0) + (counts.verification_required || 0);
  $("risk-stat-blocked").textContent = summary.blocked_tasks || 0;
  $("risk-stat-today").textContent = summary.risk_events_today || 0;
  if ($("tb-risk")) $("tb-risk").textContent = summary.abnormal || 0;
}

function riskAccountWait(account, now = Date.now()) {
  if (account.manual_review_required || account.status === "verification_required") {
    return { label: "等待人工验证", until: null, waiting: true, manual: true,
      title: "请在账号浏览器中处理平台验证，确认完成后填写原因并人工解除暂停" };
  }
  const deadlines = [
    [account.cooldown_until, "风险冷却"], [account.next_probe_at, "下次探测"],
    [account.retry_not_before, "网络退避"], [account.session_rest_until, "连续操作休息"],
    [account.operation_not_before, "操作间隔"],
  ].map(([until, label]) => ({ until, label, time: riskDate(until)?.getTime() || 0 }))
    .filter(item => item.time > now).sort((a, b) => b.time - a.time);
  const hold = deadlines[0];
  return hold ? { ...hold, waiting: true, manual: false,
    title: `${hold.label}，最早 ${riskTime(hold.until)} 再探测` }
    : { label: account.status === "recovering" ? "已到探测时间" : "无需等待",
      until: null, waiting: false, manual: false, title: "执行一次受风控闸门约束的轻量账号探测" };
}

function renderRiskAccounts() {
  const tbody = $("risk-account-table");
  if (!tbody) return;
  const query = ($("risk-account-search")?.value || "").trim().toLowerCase();
  const status = $("risk-status-filter")?.value || "";
  const rows = RISK_ACCOUNTS.filter(account => {
    if (status && account.status !== status) return false;
    if (!query) return true;
    return [account.nickname, account.reason, account.proxy, account.status_label]
      .some(value => String(value || "").toLowerCase().includes(query));
  });
  if ($("risk-filter-count")) $("risk-filter-count").textContent = `显示 ${rows.length} / ${RISK_ACCOUNTS.length}`;
  tbody.innerHTML = rows.map(account => {
    const progress = account.risk_level > 0
      ? Math.min(100, Math.round((account.recovery_successes || 0) * 100 / Math.max(1, account.recovery_target || 1)))
      : 100;
    const wait = riskAccountWait(account);
    const timing = wait.until
      ? `<b>${esc(wait.label)} · ${esc(riskRemaining(wait.until))}</b><small>最早 ${esc(riskTime(wait.until))}</small>`
      : `<span class="mut">${esc(wait.label)}</span>${wait.manual ? "<small>浏览器处理后人工解除</small>" : ""}`;
    const queue = account.queued_tasks || { total: 0 };
    const reason = account.reason || "未检测到风险信号";
    const actualAccountId = account.platform_account_id
      ? `${account.platform_account_id_label || "账号 ID"} ${account.platform_account_id}`
      : "尚未获取平台账号 ID";
    return `<tr>
      <td><div class="risk-account"><b>${esc(account.nickname || "未命名账号")}</b><small title="${esc(actualAccountId)}">${esc(riskPlatformLabel(account.platform))} · ${esc(actualAccountId)}</small></div></td>
      <td><div style="display:flex;align-items:center;gap:5px;flex-wrap:wrap"><span class="risk-status ${esc(account.status_tone)}">${esc(account.status_label)}</span><span class="pill bare num">L${Number(account.risk_level) || 0}</span></div></td>
      <td><div class="risk-reason"><span title="${esc(reason)}">${esc(reason)}</span><small>${account.last_risk_at ? "触发于 " + esc(riskTime(account.last_risk_at)) : "暂无风险记录"}</small></div></td>
      <td><div class="risk-account">${timing}<div class="risk-progress" style="margin-top:4px"><div class="risk-progress-track"><i style="width:${progress}%"></i></div><span>${account.risk_level > 0 ? `${account.recovery_successes}/${account.recovery_target}` : "完成"}</span></div></div></td>
      <td><b class="num">${account.blocked_tasks || 0} / ${queue.total || 0}</b><div class="mut" style="font-size:11px" title="${esc(account.latest_block_reason || "")}">受阻 / 待执行${account.task_next_allowed_at ? ` · ${esc(riskRemaining(account.task_next_allowed_at))}` : ""}</div></td>
      <td><div class="risk-account"><span>${account.proxy ? `<code>${esc(account.proxy)}</code>` : "本机直连"}</span><small>${esc(account.proxy_status || "unknown")} · ${esc(account.network_key || "—")}</small></div></td>
      <td class="acttd">
        <button class="ghost sm" onclick="probeRiskAccount(${account.account_id})" ${wait.waiting ? "disabled" : ""} title="${esc(wait.title)}">${wait.manual ? "待验证" : wait.waiting ? "等待探测" : "探测"}</button>
        <button class="ghost sm" onclick="showRiskEvents(${account.account_id})">记录</button>
        <button class="ghost sm" onclick="openAccountBrowser(${account.account_id})">浏览器</button>
        ${account.status !== "normal" ? `<button class="ghost sm danger" onclick="clearRiskAccount(${account.account_id})">解除</button>` : ""}
      </td>
    </tr>`;
  }).join("") || empty(7, "没有匹配的账号状态", "i-shield", "调整筛选条件或先添加平台账号");
}

function fillRiskConfig(config) {
  if (!config) return;
  const r = config.risk_control || {}, s = config.schedule || {};
  const set = (id, value) => { const el = $(id); if (el) { el.value = value ?? ""; if (el._csSync) el._csSync(); } };
  $("risk-enabled").checked = !!r.enabled;
  set("risk-mode", r.mode); set("risk-retention", r.event_retention_days);
  set("risk-read-light", r.read_light_gap_seconds); set("risk-read-heavy", r.read_heavy_gap_seconds);
  set("risk-operation-min", r.operation_gap_min_seconds); set("risk-operation-max", r.operation_gap_max_seconds);
  set("risk-session-limit", r.session_operation_limit);
  set("risk-rest-min", r.session_rest_min_seconds); set("risk-rest-max", r.session_rest_max_seconds);
  set("risk-retry-jitter", r.network_retry_jitter_seconds);
  set("risk-scan-jitter", Math.round((s.scan_jitter || 0) * 100));
  set("risk-comment-jitter", Math.round((s.comment_jitter || 0) * 100));
  set("risk-dm-poll-jitter", Math.round((s.xhs_dm_poll_jitter || 0) * 100));
  set("risk-initial-spread", s.initial_scan_spread_seconds);
  set("risk-recovery-count", r.recovery_successes); set("risk-probe-gap", (r.recovery_probe_gap_seconds || 0) / 60);
  set("risk-cooldown-steps", (r.cooldown_steps_seconds || []).map(v => v / 60).join(", "));
  set("risk-network-concurrency", r.network_group_concurrency); set("risk-network-accounts", r.network_group_risk_accounts);
  set("risk-network-window", (r.network_group_risk_window_seconds || 0) / 60); set("risk-network-cooldown", (r.network_group_cooldown_seconds || 0) / 60);
  set("risk-account-check", (s.account_check_interval_seconds || 0) / 60); set("risk-captcha-wait", (s.douyin_captcha_wait_seconds || 0) / 60);
  $("risk-quiet-enabled").checked = !!s.quiet_hours_enabled;
  set("risk-active-start", s.active_hours_start); set("risk-active-end", s.active_hours_end);
  [["comment", "comment"], ["social", "social"], ["dm", "dm"], ["publish", "publish"]].forEach(([id, key]) => {
    set(`risk-${id}-gap`, (r[`${key}_min_gap_seconds`] || 0) / 60);
    set(`risk-${id}-hourly`, r[`${key}_hourly_cap`]); set(`risk-${id}-daily`, r[`${key}_daily_cap`]);
  });
  set("risk-shared-write", (r.shared_write_gap_seconds || 0) / 60);
  set("risk-combined-hourly", r.combined_action_hourly_cap); set("risk-combined-daily", r.combined_action_daily_cap);
  if ($("risk-admin-token-btn")) {
    $("risk-admin-token-btn").style.display = config.admin_token_required ? "" : "none";
    let configured = false;
    try { configured = !!sessionStorage.getItem("creatorhub-risk-admin-token"); } catch (e) {}
    $("risk-admin-token-btn").innerHTML = `${ic("i-shield")}${configured ? "管理口令已设置" : "设置管理口令"}`;
  }
}

async function setRiskAdminToken() {
  const token = await uiPrompt({
    title: "设置本次会话的风控管理口令",
    hint: "口令只保存在当前浏览器标签会话中。留空会清除已经保存的口令。",
    placeholder: "CREATORHUB_ADMIN_TOKEN", secret: true,
  });
  if (token === null) return;
  try {
    if (token.trim()) sessionStorage.setItem("creatorhub-risk-admin-token", token.trim());
    else sessionStorage.removeItem("creatorhub-risk-admin-token");
  } catch (e) {}
  fillRiskConfig(RISK_CONFIG);
  toast(token.trim() ? "管理口令已保存到当前会话" : "管理口令已清除", "ok");
}

function riskNumber(id, multiplier = 1) {
  const field = $(id), raw = String(field.value ?? "").trim(), value = Number(raw);
  field.removeAttribute("aria-invalid");
  if (!raw || !Number.isFinite(value) || value < Number(field.min || 0)
      || (field.max !== undefined && field.max !== "" && value > Number(field.max))) {
    field.setAttribute("aria-invalid", "true"); field.focus?.();
    throw new Error((field.labels?.[0]?.textContent || id) + "填写不正确，请检查取值范围");
  }
  return Math.round(value * multiplier);
}

async function saveRiskConfig() {
  const msg = $("risk-config-msg");
  const restore = btnLoading(evtBtn(), "保存中");
  INFLIGHT++; _barSync();
  try {
    const steps = $("risk-cooldown-steps").value.split(/[,，\s]+/).filter(Boolean).map(Number);
    if (!steps.length || steps.some(v => !Number.isFinite(v) || v <= 0)) throw new Error("冷却阶梯需要填写有效的分钟数");
    const r = {
      enabled: $("risk-enabled").checked, mode: $("risk-mode").value,
      network_group_concurrency: riskNumber("risk-network-concurrency"),
      read_light_gap_seconds: riskNumber("risk-read-light"), read_heavy_gap_seconds: riskNumber("risk-read-heavy"),
      operation_gap_min_seconds: riskNumber("risk-operation-min"), operation_gap_max_seconds: riskNumber("risk-operation-max"),
      session_operation_limit: riskNumber("risk-session-limit"),
      session_rest_min_seconds: riskNumber("risk-rest-min"), session_rest_max_seconds: riskNumber("risk-rest-max"),
      network_retry_jitter_seconds: riskNumber("risk-retry-jitter"),
      shared_write_gap_seconds: riskNumber("risk-shared-write", 60),
      cooldown_steps_seconds: steps.map(v => Math.round(v * 60)),
      recovery_successes: riskNumber("risk-recovery-count"), recovery_probe_gap_seconds: riskNumber("risk-probe-gap", 60),
      event_retention_days: riskNumber("risk-retention"),
      network_group_risk_accounts: riskNumber("risk-network-accounts"),
      network_group_risk_window_seconds: riskNumber("risk-network-window", 60),
      network_group_cooldown_seconds: riskNumber("risk-network-cooldown", 60),
      combined_action_hourly_cap: riskNumber("risk-combined-hourly"), combined_action_daily_cap: riskNumber("risk-combined-daily"),
    };
    if (r.operation_gap_max_seconds < r.operation_gap_min_seconds) throw new Error("跨功能间隔上限应不小于下限");
    if (r.session_rest_max_seconds < r.session_rest_min_seconds) throw new Error("休息上限应不小于下限");
    ["comment", "social", "dm", "publish"].forEach(key => {
      r[`${key}_min_gap_seconds`] = riskNumber(`risk-${key}-gap`, 60);
      r[`${key}_hourly_cap`] = riskNumber(`risk-${key}-hourly`);
      r[`${key}_daily_cap`] = riskNumber(`risk-${key}-daily`);
    });
    const schedule = {
      quiet_hours_enabled: $("risk-quiet-enabled").checked,
      active_hours_start: riskNumber("risk-active-start"), active_hours_end: riskNumber("risk-active-end"),
      account_check_interval_seconds: riskNumber("risk-account-check", 60),
      douyin_captcha_wait_seconds: riskNumber("risk-captcha-wait", 60),
      scan_jitter: riskNumber("risk-scan-jitter") / 100,
      comment_jitter: riskNumber("risk-comment-jitter") / 100,
      xhs_dm_poll_jitter: riskNumber("risk-dm-poll-jitter") / 100,
      initial_scan_spread_seconds: riskNumber("risk-initial-spread"),
    };
    msg.textContent = "保存中…";
    RISK_CONFIG = await api("/api/risk-control/config", {
      method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ risk_control: r, schedule }),
    });
    fillRiskConfig(RISK_CONFIG); msg.textContent = "已保存并生效"; toast("风控规则已保存并立即生效", "ok");
    await refreshRiskCenter();
  } catch (e) { msg.textContent = e.message; toast("保存失败：" + e.message, "err"); }
  finally { INFLIGHT--; restore(); _barSync(); }
}

async function probeRiskAccount(accountId) {
  const button = evtBtn();
  await withBusy(button, "探测中", async () => {
    try {
      const response = await api(`/api/risk-control/accounts/${accountId}/probe`, { method: "POST" });
      const result = response.result || {};
      if (result.skipped) toast("当前尚未放行探测：" + (result.reason || "仍处于冷却期"), "info", 7000);
      else toast(`轻量探测成功，恢复进度已更新${response.woken_tasks ? `，已唤醒 ${response.woken_tasks} 条任务` : ""}`, "ok");
    } catch (e) { toast("探测失败：" + e.message, "err", 7000); }
    await refreshRiskCenter();
  });
}

async function clearRiskAccount(accountId) {
  const nickname = RISK_ACCOUNTS.find(account => account.account_id === accountId)?.nickname
    || ACCOUNTS.find(account => account.id === accountId)?.nickname
    || `账号 ${accountId}`;
  const reason = await uiPrompt({
    title: "填写解除原因",
    hint: `请说明已对「${nickname}」完成的人工检查。该内容会进入审计记录。`,
    placeholder: "例如：已完成验证码并确认代理出口正常", multiline: true, rows: 4,
  });
  if (reason === null) return;
  if (reason.trim().length < 3) { toast("请填写至少 3 个字符的解除原因", "err"); return; }
  if (!await uiConfirm({ title: "解除账号风控状态", message: `请确认已人工检查「${nickname}」的登录态、验证码和网络出口。解除后待执行任务可能继续运行。`, okText: "确认解除", danger: true })) return;
  try {
    const result = await api(`/api/risk-control/accounts/${accountId}/clear`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ confirmed: true, reason: reason.trim() }),
    });
    toast(`账号风控状态已解除${result.woken_tasks ? `，已唤醒 ${result.woken_tasks} 条任务` : ""}`, "ok");
    await refreshRiskCenter();
    if (CURRENT_TAB === "collections") await refreshCollections();
  } catch (e) { toast("解除失败：" + e.message, "err"); }
}

async function showRiskEvents(accountId) {
  const nickname = RISK_ACCOUNTS.find(account => account.account_id === accountId)?.nickname || `账号 ${accountId}`;
  const modal = $("risk-event-modal");
  $("risk-event-title").textContent = `${nickname || "账号"} · 风险事件`;
  $("risk-event-subtitle").textContent = "正在加载最近事件…";
  $("risk-event-list").innerHTML = '<div class="hint">正在加载事件记录…</div>';
  modal.style.display = "flex"; modalOpened(modal);
  try {
    const data = await api(`/api/risk-control/accounts/${accountId}/events?limit=100`);
    $("risk-event-subtitle").textContent = `最近 ${data.events.length} 条 · 不保存响应正文或账号凭据`;
    $("risk-event-list").innerHTML = data.events.map(event => `<div class="risk-event">
      <time>${esc(riskTime(event.occurred_at))}</time>
      <span class="risk-status ${event.outcome === "success" ? "success" : event.outcome === "manual" || event.outcome === "business" ? "warn" : "danger"}">${esc(RISK_OUTCOME_LABELS[event.outcome] || event.outcome)}</span>
      <b>${esc(RISK_KIND_LABELS[event.operation_kind] || event.operation_kind)}</b>
      <div class="risk-event-detail">${esc(event.detail || event.signal || "无补充说明")}<small>${esc(event.signal || "—")} · ${esc(event.network_key || "—")}</small></div>
    </div>`).join("") || '<div class="hint">暂无风险事件；账号发生平台操作后会在这里形成记录。</div>';
  } catch (e) { $("risk-event-list").innerHTML = `<div class="hint">加载失败：${esc(e.message)}</div>`; }
}
function hideRiskEvents() { const modal = $("risk-event-modal"); modal.style.display = "none"; modalClosed(modal); }
async function showRiskAudit() {
  const modal = $("risk-event-modal");
  $("risk-event-title").textContent = "风控管理变更记录";
  $("risk-event-subtitle").textContent = "正在加载审计记录…";
  $("risk-event-list").innerHTML = '<div class="hint">正在加载变更记录…</div>';
  modal.style.display = "flex"; modalOpened(modal);
  try {
    const rows = await api("/api/risk-control/audit?limit=100");
    $("risk-event-subtitle").textContent = `最近 ${rows.length} 条 · 包含规则修改、人工探测和解除操作`;
    const labels = { policy_updated: "规则修改", manual_probe: "人工探测", account_risk_cleared: "人工解除", task_result_resolved: "任务结果核对" };
    $("risk-event-list").innerHTML = rows.map(row => {
      const detail = row.detail || {};
      const changeCount = Object.values(detail.changes || {}).reduce((sum, section) => sum + Object.keys(section || {}).length, 0);
      const summary = detail.note || detail.reason || (changeCount ? `修改 ${changeCount} 项规则` : detail.skipped ? `探测延后：${detail.reason || "风控闸门未放行"}` : "操作完成");
      return `<div class="risk-event"><time>${esc(riskTime(row.created_at))}</time><span class="risk-status warn">${esc(labels[row.action] || row.action)}</span><b>${row.account_id ? `账号 ${row.account_id}` : "全局"}</b><div class="risk-event-detail">${esc(summary)}<small>${esc(row.actor || "local-ui")}</small></div></div>`;
    }).join("") || '<div class="hint">暂无风控管理变更记录。</div>';
  } catch (e) { $("risk-event-list").innerHTML = `<div class="hint">加载失败：${esc(e.message)}</div>`; }
}
document.addEventListener("keydown", event => {
  if (event.key === "Escape" && $("risk-event-modal")?.style.display !== "none") hideRiskEvents();
});
