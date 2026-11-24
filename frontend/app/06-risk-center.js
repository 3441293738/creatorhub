// 06-risk-center.js — 风控中心看板、场景方案与分项规则开关、人工验证解除与审计日志。
// ═══════════ 风控中心 ═══════════
let RISK_ACCOUNTS = [];
let RISK_CONFIG = null;
let RISK_DIRTY = false;

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
const RISK_MODULE_LABELS = {
  cooldown: "异常冷却恢复",
  write_limits: "写操作频控",
  pacing: "读写防抖休息",
  quiet_hours: "夜间休眠",
  network_guard: "同出口防连坐",
  jitter: "随机错峰",
  native_write_gate: "写入环境门禁",
};

const RISK_PRESETS = {
  off: {
    label: "关闭风控",
    risk_control: {
      enabled: false,
      cooldown_enabled: false,
      write_limits_enabled: false,
      pacing_enabled: false,
      network_guard_enabled: false,
      jitter_enabled: false,
    },
    schedule: {
      quiet_hours_enabled: false,
      quiet_hours_include_reads: false,
      native_write_gate_enabled: false,
    },
  },
  light: {
    label: "轻度防护",
    risk_control: {
      enabled: true,
      mode: "custom",
      cooldown_enabled: true,
      write_limits_enabled: false,
      pacing_enabled: true,
      network_guard_enabled: false,
      jitter_enabled: true,
      cooldown_steps_seconds: [900, 3600, 10800, 43200],
      recovery_probe_gap_seconds: 600,
      recovery_successes: 2,
      read_light_gap_seconds: 5,
      read_heavy_gap_seconds: 15,
      operation_gap_min_seconds: 3,
      operation_gap_max_seconds: 8,
      session_operation_limit: 0,
      session_rest_min_seconds: 120,
      session_rest_max_seconds: 300,
      network_retry_jitter_seconds: 15,
    },
    schedule: {
      quiet_hours_enabled: false,
      quiet_hours_include_reads: false,
      quiet_hours_wake_jitter_minutes: 0,
      scan_jitter: 0.1,
      comment_jitter: 0.2,
      xhs_dm_poll_jitter: 0.15,
      initial_scan_spread_seconds: 20,
      native_write_gate_enabled: false,
    },
  },
  balanced: {
    label: "稳妥防护",
    risk_control: {
      enabled: true,
      mode: "custom",
      cooldown_enabled: true,
      write_limits_enabled: true,
      pacing_enabled: true,
      network_guard_enabled: true,
      jitter_enabled: true,
      cooldown_steps_seconds: [1800, 7200, 21600, 86400],
      recovery_probe_gap_seconds: 600,
      recovery_successes: 3,
      read_light_gap_seconds: 15,
      read_heavy_gap_seconds: 45,
      operation_gap_min_seconds: 6,
      operation_gap_max_seconds: 18,
      session_operation_limit: 15,
      session_rest_min_seconds: 120,
      session_rest_max_seconds: 300,
      network_group_concurrency: 1,
      network_group_risk_accounts: 2,
      network_group_risk_window_seconds: 900,
      network_group_cooldown_seconds: 7200,
      shared_write_gap_seconds: 180,
      comment_min_gap_seconds: 300, comment_hourly_cap: 6, comment_daily_cap: 20,
      social_min_gap_seconds: 480, social_hourly_cap: 4, social_daily_cap: 15,
      dm_min_gap_seconds: 480, dm_hourly_cap: 4, dm_daily_cap: 15,
      publish_min_gap_seconds: 3600, publish_hourly_cap: 2, publish_daily_cap: 5,
      combined_action_hourly_cap: 6, combined_action_daily_cap: 20,
      network_retry_jitter_seconds: 30,
    },
    schedule: {
      quiet_hours_enabled: true,
      quiet_hours_include_reads: true,
      active_hours_start: 8,
      active_hours_end: 24,
      quiet_hours_wake_jitter_minutes: 30,
      scan_jitter: 0.15,
      comment_jitter: 0.3,
      xhs_dm_poll_jitter: 0.25,
      initial_scan_spread_seconds: 45,
      native_write_gate_enabled: false,
    },
  },
  strict: {
    label: "严格保守",
    risk_control: {
      enabled: true,
      mode: "custom",
      cooldown_enabled: true,
      write_limits_enabled: true,
      pacing_enabled: true,
      network_guard_enabled: true,
      jitter_enabled: true,
      cooldown_steps_seconds: [1800, 7200, 21600, 86400],
      recovery_probe_gap_seconds: 600,
      recovery_successes: 3,
      read_light_gap_seconds: 20,
      read_heavy_gap_seconds: 60,
      operation_gap_min_seconds: 8,
      operation_gap_max_seconds: 25,
      session_operation_limit: 12,
      session_rest_min_seconds: 180,
      session_rest_max_seconds: 480,
      network_group_concurrency: 1,
      network_group_risk_accounts: 2,
      network_group_risk_window_seconds: 900,
      network_group_cooldown_seconds: 7200,
      shared_write_gap_seconds: 300,
      comment_min_gap_seconds: 600, comment_hourly_cap: 3, comment_daily_cap: 10,
      social_min_gap_seconds: 900, social_hourly_cap: 2, social_daily_cap: 8,
      dm_min_gap_seconds: 900, dm_hourly_cap: 2, dm_daily_cap: 8,
      publish_min_gap_seconds: 7200, publish_hourly_cap: 1, publish_daily_cap: 3,
      combined_action_hourly_cap: 3, combined_action_daily_cap: 10,
      network_retry_jitter_seconds: 30,
    },
    schedule: {
      quiet_hours_enabled: true,
      quiet_hours_include_reads: true,
      active_hours_start: 8,
      active_hours_end: 24,
      quiet_hours_wake_jitter_minutes: 35,
      scan_jitter: 0.15,
      comment_jitter: 0.4,
      xhs_dm_poll_jitter: 0.35,
      initial_scan_spread_seconds: 60,
      native_write_gate_enabled: true,
      native_write_require_system_chrome: true,
      native_write_require_verified_proxy: true,
      native_write_proxy_max_age_seconds: 86400,
    },
  },
};

function openRiskRulesSection() {
  if (typeof switchTab === "function") switchTab("risk-control", true);
  globalThis.CreatorHubWorkbench?.showSection?.("risk-control", "rules");
}

function setRiskConfigMsg(text, state = "") {
  const el = $("risk-config-msg");
  if (!el) return;
  el.textContent = text || "";
  if (state) el.dataset.state = state;
  else el.removeAttribute("data-state");
}

async function refreshRiskCenter(force = false, announce = force) {
  const isCurrent = beginViewRequest("risk-center");
  try {
    const shouldFillConfig = (!RISK_CONFIG || (force && !RISK_DIRTY));
    const configPromise = shouldFillConfig ? api("/api/risk-control/config") : Promise.resolve(RISK_CONFIG);
    const platformQuery = GLOBAL_MODE ? "" : ("?platform=" + encodeURIComponent(PLATFORM));
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

  const banner = $("risk-status-policy-banner");
  if (banner) {
    const modules = Array.isArray(summary.active_modules) ? summary.active_modules : [];
    const enabled = Boolean(summary.policy_enabled);
    const active = (enabled && modules.length > 0) || Boolean(summary.native_write_gate_enabled);
    banner.dataset.state = active ? "on" : "off";
    const titleEl = $("risk-status-policy-title");
    const descEl = $("risk-status-policy-desc");
    if (!active) {
      if (titleEl) titleEl.textContent = "风控规则当前未开启";
      if (descEl) descEl.textContent = "所有采集、发布、评论与互动任务将直接执行，不做额外拦截或冷却。";
    } else {
      const names = modules.map(key => RISK_MODULE_LABELS[key] || key);
      if (titleEl) titleEl.textContent = `风控保护运行中（${names.length} 项）`;
      if (descEl) descEl.textContent = "已生效：" + names.join(" · ");
    }
  }
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
      <td><div class="risk-account"><span>${account.proxy ? `<code>${esc(account.proxy)}</code>` : '<span style="color:var(--warn)">⚠️ 本机直连</span>'}</span><small>${esc(account.proxy_status || "unknown")} · ${esc(account.network_key || "—")}</small>${account.proxy_warning ? `<small style="color:var(--warn)" title="${esc(account.proxy_warning)}">⚠️ ${esc(account.proxy_warning)}</small>` : ""}</div></td>
      <td class="acttd">
        <button class="ghost sm" onclick="probeRiskAccount(${account.account_id})" ${wait.waiting ? "disabled" : ""} title="${esc(wait.title)}">${wait.manual ? "待验证" : wait.waiting ? "等待探测" : "探测"}</button>
        <button class="ghost sm" onclick="showRiskEvents(${account.account_id})">记录</button>
        <button class="ghost sm" onclick="openAccountBrowser(${account.account_id})">浏览器</button>
        ${account.status !== "normal" ? `<button class="ghost sm danger" onclick="clearRiskAccount(${account.account_id})">解除</button>` : ""}
      </td>
    </tr>`;
  }).join("") || empty(7, "没有匹配的账号状态", "i-shield", "调整筛选条件或先添加平台账号");
}

function _riskValNum(id, fallback = 0) {
  const v = Number($(id)?.value);
  return Number.isFinite(v) ? v : fallback;
}
function _riskFmtHour(hour) {
  const h = Number(hour) || 0;
  if (h <= 24) return `${String(h).padStart(2, "0")}:00`;
  return `次日${String(h - 24).padStart(2, "0")}:00`;
}

function detectRiskPreset() {
  const enabled = !!$("risk-enabled")?.checked;
  const nativeGate = !!$("risk-native-gate-enabled")?.checked;
  if (!enabled && !nativeGate) return "off";
  if (!enabled) return "";
  const flags = {
    cooldown: !!$("risk-cooldown-enabled")?.checked,
    write_limits: !!$("risk-write-limits-enabled")?.checked,
    pacing: !!$("risk-pacing-enabled")?.checked,
    quiet_hours: !!$("risk-quiet-enabled")?.checked,
    network_guard: !!$("risk-network-guard-enabled")?.checked,
    jitter: !!$("risk-jitter-enabled")?.checked,
    native_gate: nativeGate,
  };
  if (flags.cooldown && !flags.write_limits && flags.pacing && !flags.quiet_hours && !flags.network_guard && flags.jitter && !flags.native_gate
      && _riskValNum("risk-read-light") === 5 && _riskValNum("risk-session-limit") === 0) {
    return "light";
  }
  if (flags.cooldown && flags.write_limits && flags.pacing && flags.quiet_hours && flags.network_guard && flags.jitter && !flags.native_gate
      && _riskValNum("risk-comment-gap") === 5 && _riskValNum("risk-publish-gap") === 60) {
    return "balanced";
  }
  if (flags.cooldown && flags.write_limits && flags.pacing && flags.quiet_hours && flags.network_guard && flags.jitter && flags.native_gate
      && _riskValNum("risk-comment-gap") === 10 && _riskValNum("risk-publish-gap") === 120) {
    return "strict";
  }
  return "";
}

function syncRiskConfigUI() {
  const enabled = !!$("risk-enabled")?.checked;
  const modulesGrid = $("risk-modules-grid");
  if (modulesGrid) modulesGrid.classList.toggle("is-master-off", !enabled);
  const offHint = $("risk-master-off-hint");
  if (offHint) offHint.hidden = enabled;

  const nativeOn = !!$("risk-native-gate-enabled")?.checked;
  const nativeSub = $("risk-native-gate-sub");
  if (nativeSub) nativeSub.classList.toggle("is-disabled", !nativeOn);

  const moduleChecks = [
    ["cooldown", "risk-cooldown-enabled"],
    ["write_limits", "risk-write-limits-enabled"],
    ["pacing", "risk-pacing-enabled"],
    ["quiet_hours", "risk-quiet-enabled"],
    ["network_guard", "risk-network-guard-enabled"],
    ["jitter", "risk-jitter-enabled"],
  ];

  const activeNames = [];
  moduleChecks.forEach(([key, id]) => {
    const checked = !!$(id)?.checked;
    const effective = enabled && checked;
    const card = document.querySelector(`.risk-module[data-risk-module="${key}"]`);
    if (card) card.classList.toggle("is-active", effective);
    const badge = $(`risk-badge-${key}`);
    if (badge) {
      badge.textContent = effective ? "生效中" : (checked && !enabled ? "总开关已关" : "未开启");
    }
    if (effective) activeNames.push(key);
  });

  // 各模块精简参数预览
  const stepsStr = ($("risk-cooldown-steps")?.value || "").split(/[,，\s]+/).filter(Boolean).join("/");
  if ($("risk-summary-cooldown")) {
    $("risk-summary-cooldown").textContent =
      `阶梯 ${stepsStr || "—"} 分 · 探测间隔 ${_riskValNum("risk-probe-gap")} 分 · 成功 ${_riskValNum("risk-recovery-count")} 次恢复`;
  }
  if ($("risk-summary-write_limits")) {
    const cGap = _riskValNum("risk-comment-gap"), cH = _riskValNum("risk-comment-hourly"), cD = _riskValNum("risk-comment-daily");
    const pGap = _riskValNum("risk-publish-gap"), pD = _riskValNum("risk-publish-daily");
    const sh = _riskValNum("risk-shared-write");
    const parts = [];
    if (sh > 0) parts.push(`共享写间隔 ${sh}分`);
    parts.push(`评论 ${cGap ? cGap + "分" : "不限"}/${cH ? cH + "次/时" : "不限"}/${cD ? cD + "次/日" : "不限"}`);
    parts.push(`发布 ${pGap ? pGap + "分" : "不限"}/${pD ? pD + "次/日" : "不限"}`);
    $("risk-summary-write_limits").textContent = parts.join(" · ");
  }
  if ($("risk-summary-pacing")) {
    const rl = _riskValNum("risk-read-light"), rh = _riskValNum("risk-read-heavy");
    const omin = _riskValNum("risk-operation-min"), omax = _riskValNum("risk-operation-max");
    const slim = _riskValNum("risk-session-limit"), rmin = _riskValNum("risk-rest-min"), rmax = _riskValNum("risk-rest-max");
    $("risk-summary-pacing").textContent =
      `轻/重读 ${rl}/${rh}秒 · 跨功能 ${omin}–${omax}秒 · ${slim > 0 ? `连续 ${slim}次休 ${rmin}–${rmax}秒` : "关闭连续休息"}`;
  }
  if ($("risk-summary-quiet_hours")) {
    const st = _riskFmtHour(_riskValNum("risk-active-start", 8));
    const ed = _riskFmtHour(_riskValNum("risk-active-end", 24));
    const inc = !!$("risk-quiet-include-reads")?.checked;
    const jit = _riskValNum("risk-quiet-jitter");
    $("risk-summary-quiet_hours").textContent =
      `活跃 ${st}–${ed} · ${inc ? "含后台监控与采集" : "仅限制自动写操作"}${jit > 0 ? ` · ±${jit}分错峰` : ""}`;
  }
  if ($("risk-summary-network_guard")) {
    const conc = _riskValNum("risk-network-concurrency", 1);
    const accs = _riskValNum("risk-network-accounts");
    const win = _riskValNum("risk-network-window");
    const cd = _riskValNum("risk-network-cooldown");
    $("risk-summary-network_guard").textContent =
      `同出口并发 ${conc} · ${accs >= 2 ? `${win}分内 ${accs}号命中熔断 ${cd}分` : "关闭多号熔断"}`;
  }
  if ($("risk-summary-jitter")) {
    const sj = _riskValNum("risk-scan-jitter"), cj = _riskValNum("risk-comment-jitter");
    const dj = _riskValNum("risk-dm-poll-jitter"), sp = _riskValNum("risk-initial-spread");
    const rj = _riskValNum("risk-retry-jitter");
    $("risk-summary-jitter").textContent =
      `扫描 +${sj}% · 评论 +${cj}% · 私信 +${dj}% · 首跑 ${sp}秒 · 重试 ${rj}秒`;
  }

  // 场景方案高亮
  const activePreset = detectRiskPreset();
  document.querySelectorAll(".risk-preset[data-risk-preset]").forEach(btn => {
    btn.classList.toggle("active", btn.dataset.riskPreset === activePreset);
  });

  // 顶部态势 Hero
  const hero = $("risk-rules-hero");
  const titleEl = $("risk-hero-title");
  const pillEl = $("risk-hero-pill");
  const descEl = $("risk-hero-desc");
  const chipsEl = $("risk-hero-chips");
  if (!hero) return;

  if (!enabled && !nativeOn) {
    hero.dataset.state = "off";
    titleEl.textContent = "风控规则当前全部关闭";
    pillEl.textContent = "默认不拦截";
    descEl.textContent = "所有采集、发布、评论与互动任务将直接按计划执行，不做额外拦截、冷却或随机延迟。如需防护，可在下方一键套用方案或单独勾选规则。";
    if (chipsEl) { chipsEl.hidden = true; chipsEl.innerHTML = ""; }
  } else if (!enabled && nativeOn) {
    hero.dataset.state = "empty";
    titleEl.textContent = "统一风控已关闭（仅 native 写入环境门禁开启）";
    pillEl.textContent = "1 项环境门禁";
    descEl.textContent = "统一频控、冷却与休眠均未开启；仅高级维护中的 native 写入环境门禁生效，可在下方「高级与维护参数」中关闭。";
    if (chipsEl) {
      chipsEl.hidden = false;
      chipsEl.innerHTML = `<span class="risk-active-chip env">native 写入环境门禁</span>`;
    }
  } else if (enabled && activeNames.length === 0 && !nativeOn) {
    hero.dataset.state = "empty";
    titleEl.textContent = "总开关已开启，但尚未启用任何子规则";
    pillEl.textContent = "0 项生效";
    descEl.textContent = "当前仍不会对任务做额外拦截或延迟。请点击下方快捷方案，或勾选需要的分项规则开关后保存。";
    if (chipsEl) { chipsEl.hidden = true; chipsEl.innerHTML = ""; }
  } else {
    hero.dataset.state = "on";
    const totalActive = activeNames.length + (nativeOn ? 1 : 0);
    const presetLabel = activePreset && RISK_PRESETS[activePreset] ? RISK_PRESETS[activePreset].label : "自定义组合";
    titleEl.textContent = `统一风控已开启 · ${presetLabel}`;
    pillEl.textContent = `已启用 ${totalActive} 项规则`;
    descEl.textContent = "仅下方标注「生效中」的规则会参与后台调度；所有参数所见即所得，保存后立即生效。";
    if (chipsEl) {
      chipsEl.hidden = false;
      const chipHtml = activeNames.map(key => `<span class="risk-active-chip">${esc(RISK_MODULE_LABELS[key] || key)}</span>`);
      if (nativeOn) chipHtml.push(`<span class="risk-active-chip env">写入环境门禁</span>`);
      chipsEl.innerHTML = chipHtml.join("");
    }
  }
}

function onRiskMasterToggle() {
  RISK_DIRTY = true;
  syncRiskConfigUI();
  setRiskConfigMsg("有未保存修改 · 点击「保存并立即生效」应用", "dirty");
}

function onRiskModuleToggle(moduleKey) {
  const idMap = {
    cooldown: "risk-cooldown-enabled",
    write_limits: "risk-write-limits-enabled",
    pacing: "risk-pacing-enabled",
    quiet_hours: "risk-quiet-enabled",
    network_guard: "risk-network-guard-enabled",
    jitter: "risk-jitter-enabled",
  };
  const checked = !!$(idMap[moduleKey])?.checked;
  if (checked && $("risk-enabled") && !$("risk-enabled").checked) {
    $("risk-enabled").checked = true;
  }
  // 首次单独开启随机错峰时若参数全为 0，自动填入温和默认错峰参数，避免开启后无效果
  if (checked && moduleKey === "jitter") {
    const allZero = ["risk-scan-jitter", "risk-comment-jitter", "risk-dm-poll-jitter", "risk-initial-spread", "risk-retry-jitter"]
      .every(id => _riskValNum(id) <= 0);
    if (allZero) {
      $("risk-scan-jitter").value = 15;
      $("risk-comment-jitter").value = 30;
      $("risk-dm-poll-jitter").value = 25;
      $("risk-initial-spread").value = 45;
      $("risk-retry-jitter").value = 30;
    }
  }
  if (checked && moduleKey === "quiet_hours") {
    if (_riskValNum("risk-active-end") <= _riskValNum("risk-active-start")) {
      $("risk-active-start").value = 8;
      $("risk-active-end").value = 24;
    }
  }
  RISK_DIRTY = true;
  syncRiskConfigUI();
  setRiskConfigMsg("有未保存修改 · 点击「保存并立即生效」应用", "dirty");
}

function onRiskFieldChange() {
  RISK_DIRTY = true;
  syncRiskConfigUI();
  setRiskConfigMsg("有未保存修改 · 点击「保存并立即生效」应用", "dirty");
}

function applyRiskPreset(presetKey) {
  const preset = RISK_PRESETS[presetKey];
  if (!preset) return;
  const r = preset.risk_control || {};
  const s = preset.schedule || {};
  const setVal = (id, val) => { const el = $(id); if (el && val !== undefined) el.value = val; };
  const setChk = (id, val) => { const el = $(id); if (el && val !== undefined) el.checked = !!val; };

  setChk("risk-enabled", r.enabled);
  setChk("risk-cooldown-enabled", r.cooldown_enabled);
  setChk("risk-write-limits-enabled", r.write_limits_enabled);
  setChk("risk-pacing-enabled", r.pacing_enabled);
  setChk("risk-network-guard-enabled", r.network_guard_enabled);
  setChk("risk-jitter-enabled", r.jitter_enabled);

  if (Array.isArray(r.cooldown_steps_seconds)) setVal("risk-cooldown-steps", r.cooldown_steps_seconds.map(v => v / 60).join(", "));
  if (r.recovery_probe_gap_seconds !== undefined) setVal("risk-probe-gap", r.recovery_probe_gap_seconds / 60);
  setVal("risk-recovery-count", r.recovery_successes);
  setVal("risk-read-light", r.read_light_gap_seconds);
  setVal("risk-read-heavy", r.read_heavy_gap_seconds);
  setVal("risk-operation-min", r.operation_gap_min_seconds);
  setVal("risk-operation-max", r.operation_gap_max_seconds);
  setVal("risk-session-limit", r.session_operation_limit);
  setVal("risk-rest-min", r.session_rest_min_seconds);
  setVal("risk-rest-max", r.session_rest_max_seconds);
  setVal("risk-retry-jitter", r.network_retry_jitter_seconds);
  setVal("risk-network-concurrency", r.network_group_concurrency);
  setVal("risk-network-accounts", r.network_group_risk_accounts);
  if (r.network_group_risk_window_seconds !== undefined) setVal("risk-network-window", r.network_group_risk_window_seconds / 60);
  if (r.network_group_cooldown_seconds !== undefined) setVal("risk-network-cooldown", r.network_group_cooldown_seconds / 60);
  if (r.shared_write_gap_seconds !== undefined) setVal("risk-shared-write", r.shared_write_gap_seconds / 60);
  setVal("risk-combined-hourly", r.combined_action_hourly_cap);
  setVal("risk-combined-daily", r.combined_action_daily_cap);
  ["comment", "social", "dm", "publish"].forEach(key => {
    if (r[`${key}_min_gap_seconds`] !== undefined) setVal(`risk-${key}-gap`, r[`${key}_min_gap_seconds`] / 60);
    setVal(`risk-${key}-hourly`, r[`${key}_hourly_cap`]);
    setVal(`risk-${key}-daily`, r[`${key}_daily_cap`]);
  });

  setChk("risk-quiet-enabled", s.quiet_hours_enabled);
  setChk("risk-quiet-include-reads", s.quiet_hours_include_reads);
  setVal("risk-active-start", s.active_hours_start);
  setVal("risk-active-end", s.active_hours_end);
  setVal("risk-quiet-jitter", s.quiet_hours_wake_jitter_minutes);
  if (s.scan_jitter !== undefined) setVal("risk-scan-jitter", Math.round(s.scan_jitter * 100));
  if (s.comment_jitter !== undefined) setVal("risk-comment-jitter", Math.round(s.comment_jitter * 100));
  if (s.xhs_dm_poll_jitter !== undefined) setVal("risk-dm-poll-jitter", Math.round(s.xhs_dm_poll_jitter * 100));
  setVal("risk-initial-spread", s.initial_scan_spread_seconds);
  setChk("risk-native-gate-enabled", s.native_write_gate_enabled);
  setChk("risk-native-chrome", s.native_write_require_system_chrome);
  setChk("risk-native-proxy", s.native_write_require_verified_proxy);
  if (s.native_write_proxy_max_age_seconds !== undefined) setVal("risk-native-proxy-age", Math.round(s.native_write_proxy_max_age_seconds / 3600));

  RISK_DIRTY = true;
  syncRiskConfigUI();
  setRiskConfigMsg(`已套用「${preset.label}」方案 · 点击「保存并立即生效」应用`, "dirty");
}

function resetRiskConfigToOff() {
  applyRiskPreset("off");
}

function fillRiskConfig(config) {
  if (!config) return;
  const r = config.risk_control || {}, s = config.schedule || {};
  const set = (id, value) => { const el = $(id); if (el) { el.value = value ?? ""; if (el._csSync) el._csSync(); } };
  const setChk = (id, value) => { const el = $(id); if (el) el.checked = !!value; };

  setChk("risk-enabled", r.enabled);
  setChk("risk-cooldown-enabled", r.cooldown_enabled);
  setChk("risk-write-limits-enabled", r.write_limits_enabled);
  setChk("risk-pacing-enabled", r.pacing_enabled);
  setChk("risk-network-guard-enabled", r.network_guard_enabled);
  setChk("risk-jitter-enabled", r.jitter_enabled);

  set("risk-retention", r.event_retention_days);
  set("risk-read-light", r.read_light_gap_seconds);
  set("risk-read-heavy", r.read_heavy_gap_seconds);
  set("risk-operation-min", r.operation_gap_min_seconds);
  set("risk-operation-max", r.operation_gap_max_seconds);
  set("risk-session-limit", r.session_operation_limit);
  set("risk-rest-min", r.session_rest_min_seconds);
  set("risk-rest-max", r.session_rest_max_seconds);
  set("risk-retry-jitter", r.network_retry_jitter_seconds);
  set("risk-scan-jitter", Math.round((s.scan_jitter || 0) * 100));
  set("risk-comment-jitter", Math.round((s.comment_jitter || 0) * 100));
  set("risk-dm-poll-jitter", Math.round((s.xhs_dm_poll_jitter || 0) * 100));
  set("risk-initial-spread", s.initial_scan_spread_seconds);
  set("risk-recovery-count", r.recovery_successes);
  set("risk-probe-gap", (r.recovery_probe_gap_seconds || 0) / 60);
  set("risk-cooldown-steps", (r.cooldown_steps_seconds || []).map(v => v / 60).join(", "));
  set("risk-network-concurrency", r.network_group_concurrency);
  set("risk-network-accounts", r.network_group_risk_accounts);
  set("risk-network-window", (r.network_group_risk_window_seconds || 0) / 60);
  set("risk-network-cooldown", (r.network_group_cooldown_seconds || 0) / 60);
  set("risk-account-check", (s.account_check_interval_seconds || 0) / 60);
  set("risk-captcha-wait", (s.douyin_captcha_wait_seconds || 0) / 60);
  setChk("risk-quiet-enabled", s.quiet_hours_enabled);
  setChk("risk-quiet-include-reads", s.quiet_hours_include_reads);
  set("risk-active-start", s.active_hours_start);
  set("risk-active-end", s.active_hours_end);
  set("risk-quiet-jitter", s.quiet_hours_wake_jitter_minutes ?? 0);
  setChk("risk-native-gate-enabled", s.native_write_gate_enabled);
  setChk("risk-native-chrome", s.native_write_require_system_chrome !== false);
  setChk("risk-native-proxy", s.native_write_require_verified_proxy !== false);
  set("risk-native-proxy-age", Math.round((s.native_write_proxy_max_age_seconds ?? 86400) / 3600));
  [["comment", "comment"], ["social", "social"], ["dm", "dm"], ["publish", "publish"]].forEach(([id, key]) => {
    set(`risk-${id}-gap`, (r[`${key}_min_gap_seconds`] || 0) / 60);
    set(`risk-${id}-hourly`, r[`${key}_hourly_cap`]);
    set(`risk-${id}-daily`, r[`${key}_daily_cap`]);
  });
  set("risk-shared-write", (r.shared_write_gap_seconds || 0) / 60);
  set("risk-combined-hourly", r.combined_action_hourly_cap);
  set("risk-combined-daily", r.combined_action_daily_cap);

  const legacyNote = $("risk-legacy-mode-note");
  if (legacyNote) legacyNote.hidden = !(r.enabled && r.mode === "conservative");

  if ($("risk-admin-token-btn")) {
    $("risk-admin-token-btn").style.display = config.admin_token_required ? "" : "none";
    let configured = false;
    try { configured = !!sessionStorage.getItem("creatorhub-risk-admin-token"); } catch (e) {}
    $("risk-admin-token-btn").innerHTML = `${ic("i-shield")}${configured ? "管理口令已设置" : "设置管理口令"}`;
  }
  RISK_DIRTY = false;
  syncRiskConfigUI();
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

function _revealRiskField(field) {
  if (!field) return;
  for (let d = field.closest("details"); d; d = d.parentElement?.closest("details")) d.open = true;
  field.focus?.();
}

function riskNumber(id, multiplier = 1) {
  const field = $(id), raw = String(field.value ?? "").trim(), value = Number(raw);
  field.removeAttribute("aria-invalid");
  if (!raw || !Number.isFinite(value) || value < Number(field.min || 0)
      || (field.max !== undefined && field.max !== "" && value > Number(field.max))) {
    field.setAttribute("aria-invalid", "true");
    _revealRiskField(field);
    throw new Error((field.labels?.[0]?.textContent || id) + "填写不正确，请检查取值范围");
  }
  return Math.round(value * multiplier);
}

async function saveRiskConfig() {
  const restore = btnLoading(evtBtn() || $("risk-save-btn"), "保存中");
  INFLIGHT++; _barSync();
  try {
    const stepsField = $("risk-cooldown-steps");
    const steps = stepsField.value.split(/[,，\s]+/).filter(Boolean).map(Number);
    if (!steps.length || steps.length > 8 || steps.some(v => !Number.isFinite(v) || v <= 0)) {
      _revealRiskField(stepsField);
      throw new Error("阶梯冷却时长需要填写 1–8 个正数（分钟）");
    }
    const stepSeconds = steps.map(v => Math.round(v * 60));
    for (let i = 1; i < stepSeconds.length; i++) {
      if (stepSeconds[i] < stepSeconds[i - 1]) {
        _revealRiskField(stepsField);
        throw new Error("阶梯冷却时长须按由短到长排列（例如 30, 120, 360, 1440）");
      }
    }
    const r = {
      enabled: $("risk-enabled").checked,
      mode: "custom",
      cooldown_enabled: $("risk-cooldown-enabled").checked,
      write_limits_enabled: $("risk-write-limits-enabled").checked,
      pacing_enabled: $("risk-pacing-enabled").checked,
      network_guard_enabled: $("risk-network-guard-enabled").checked,
      jitter_enabled: $("risk-jitter-enabled").checked,
      network_group_concurrency: riskNumber("risk-network-concurrency"),
      read_light_gap_seconds: riskNumber("risk-read-light"),
      read_heavy_gap_seconds: riskNumber("risk-read-heavy"),
      operation_gap_min_seconds: riskNumber("risk-operation-min"),
      operation_gap_max_seconds: riskNumber("risk-operation-max"),
      session_operation_limit: riskNumber("risk-session-limit"),
      session_rest_min_seconds: riskNumber("risk-rest-min"),
      session_rest_max_seconds: riskNumber("risk-rest-max"),
      network_retry_jitter_seconds: riskNumber("risk-retry-jitter"),
      shared_write_gap_seconds: riskNumber("risk-shared-write", 60),
      cooldown_steps_seconds: stepSeconds,
      recovery_successes: riskNumber("risk-recovery-count"),
      recovery_probe_gap_seconds: riskNumber("risk-probe-gap", 60),
      event_retention_days: riskNumber("risk-retention"),
      network_group_risk_accounts: riskNumber("risk-network-accounts"),
      network_group_risk_window_seconds: riskNumber("risk-network-window", 60),
      network_group_cooldown_seconds: riskNumber("risk-network-cooldown", 60),
      combined_action_hourly_cap: riskNumber("risk-combined-hourly"),
      combined_action_daily_cap: riskNumber("risk-combined-daily"),
    };
    if (r.operation_gap_max_seconds < r.operation_gap_min_seconds) {
      _revealRiskField($("risk-operation-max"));
      throw new Error("跨功能最大间隔应不小于最小间隔");
    }
    if (r.session_rest_max_seconds < r.session_rest_min_seconds) {
      _revealRiskField($("risk-rest-max"));
      throw new Error("休息最长时长应不小于最短时长");
    }
    ["comment", "social", "dm", "publish"].forEach(key => {
      r[`${key}_min_gap_seconds`] = riskNumber(`risk-${key}-gap`, 60);
      r[`${key}_hourly_cap`] = riskNumber(`risk-${key}-hourly`);
      r[`${key}_daily_cap`] = riskNumber(`risk-${key}-daily`);
    });
    const schedule = {
      quiet_hours_enabled: $("risk-quiet-enabled").checked,
      quiet_hours_include_reads: $("risk-quiet-include-reads").checked,
      quiet_hours_wake_jitter_minutes: riskNumber("risk-quiet-jitter"),
      active_hours_start: riskNumber("risk-active-start"),
      active_hours_end: riskNumber("risk-active-end"),
      account_check_interval_seconds: riskNumber("risk-account-check", 60),
      douyin_captcha_wait_seconds: riskNumber("risk-captcha-wait", 60),
      scan_jitter: riskNumber("risk-scan-jitter") / 100,
      comment_jitter: riskNumber("risk-comment-jitter") / 100,
      xhs_dm_poll_jitter: riskNumber("risk-dm-poll-jitter") / 100,
      initial_scan_spread_seconds: riskNumber("risk-initial-spread"),
      native_write_gate_enabled: $("risk-native-gate-enabled").checked,
      native_write_require_system_chrome: $("risk-native-chrome").checked,
      native_write_require_verified_proxy: $("risk-native-proxy").checked,
      native_write_proxy_max_age_seconds: riskNumber("risk-native-proxy-age", 3600),
    };
    if (schedule.active_hours_end <= schedule.active_hours_start) {
      _revealRiskField($("risk-active-end"));
      throw new Error("活跃结束小时必须晚于开始小时");
    }
    setRiskConfigMsg("保存中…");
    RISK_CONFIG = await api("/api/risk-control/config", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ risk_control: r, schedule }),
    });
    const woken = Number(RISK_CONFIG.woken_tasks || 0);
    fillRiskConfig(RISK_CONFIG);
    setRiskConfigMsg(woken ? `已保存并生效（已放行 ${woken} 条等待任务）` : "已保存并生效", "ok");
    toast(woken ? `风控规则已保存并生效，已自动放行 ${woken} 条等待任务` : "风控规则已保存并立即生效", "ok");
    globalThis.CreatorHubEngineSettings?.load?.();
    await refreshRiskCenter();
  } catch (e) {
    setRiskConfigMsg(e.message, "err");
    toast("保存失败：" + e.message, "err");
  }
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
