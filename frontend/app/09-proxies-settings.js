// 09-proxies-settings.js — 代理池增删改查、连通性归属地探测、兼容矩阵与全局下载/AI 设置。
// ─── 代理池 ───
let PROXIES = [];
let LAST_DETECT = null;   // {url, geo} 判别结果,加入池时一并带上归属地
function dismissProxyFormatHelp(event) {
  const help = $("px-format-help");
  if (!help?.open) return;
  if (event.type === "keydown") {
    if (event.key !== "Escape" || event.isComposing) return;
    help.open = false;
    $("px-format-toggle")?.focus();
    event.preventDefault();
    event.stopPropagation();
  } else if (!help.contains(event.target)) {
    help.open = false;
  }
}
function syncProxyFormatHelp(help) {
  // Native details provides keyboard toggling and expanded-state semantics.
  // Only listen while open; outside click, focus exit and Escape dismiss it.
  const method = help.open ? "addEventListener" : "removeEventListener";
  for (const type of ["pointerdown", "focusin", "keydown"]) {
    document[method](type, dismissProxyFormatHelp, true);
  }
}
function proxyInputOptions() {
  return {
    default_scheme: $("px-proto")?.value || "http",
    input_format: $("px-format")?.value || "auto",
  };
}
async function refreshProxies() {
  const tb = $("proxy-table"); if (!tb) return;
  let rows = [];
  try { rows = await api("/api/proxies"); } catch (e) { return; }
  PROXIES = rows;
  const stCls = s => s === "ok" ? "active" : ["bad", "auth_error", "blocked"].includes(s) ? "invalid" : "bare";
  const stTxt = { ok: "可连接", bad: "连接失败", auth_error: "认证失败", blocked: "检测受限", unknown: "未测" };
  const geoCell = p => {
    if (!p.geo_checked) return `<span class="pill bare">未测</span>`;
    const cls = p.is_mainland ? "active" : "invalid";
    const warn = p.is_mainland ? "" : ' <span title="非中国大陆 IP,与抖音/小红书国内账号时区不符,有风控风险">⚠️</span>';
    return `<div><span class="pill ${cls}">${esc(p.geo_loc || "未知")}</span>${warn}</div>` +
      (p.exit_ip ? `<div class="mut" style="font-size:11px;margin-top:2px">${esc(p.exit_ip)}${p.isp ? " · " + esc(p.isp) : ""}</div>` : "");
  };
  tb.querySelector("tbody").innerHTML = rows.map(p => `<tr>
      <td>
        <div><b>${esc(p.label || "(未命名)")}</b> <span class="pill ${stCls(p.status)}">${stTxt[p.status] || p.status}</span>${p.enabled ? "" : ' <span class="pill bare">已停用</span>'}</div>
        <div class="mut" style="font-size:11px;margin-top:2px"><code>${esc(p.url)}</code></div>
        ${p.note ? `<div class="mut" style="font-size:11px">${esc(p.note)}</div>` : ""}
      </td>
      <td>${geoCell(p)}</td>
      <td><span class="pill ${p.used_by ? "active" : "bare"}">${p.used_by} 个账号</span></td>
      <td class="acttd">
        <button class="ghost sm" onclick="editPoolProxy(${p.id})">编辑</button>
        <button class="ghost sm" onclick="testPoolProxy(${p.id})">测试</button>
        <button class="ghost sm" onclick="togglePoolProxy(${p.id},${p.enabled})">${p.enabled ? "停用" : "启用"}</button>
        <button class="ghost sm danger" onclick="delPoolProxy(${p.id},${p.used_by})">${ic("i-trash")}删除</button>
      </td>
    </tr>`).join("") || empty(4, "代理池为空", "i-shield", "添加住宅/4G 代理,账号即可一号一代理关联使用");
}
async function detectProxy() {
  const raw = $("px-url").value.trim();
  if (!raw) { toast("请先填代理地址", "err"); return; }
  const btn = event.target.closest("button"); btn.disabled = true; const old = btn.textContent; btn.textContent = "判别中…";
  try {
    const r = await api("/api/proxies/detect", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url: raw, ...proxyInputOptions() }) });
    if (!r.ok) { toast("判别失败:" + (r.error || ""), "err"); return; }
    if ($("px-proto") && (r.scheme === "http" || r.scheme === "socks5")) $("px-proto").value = r.scheme;
    $("px-url").value = r.recommend;        // 回填带协议的规范地址
    if ($("px-format")) $("px-format").value = "auto";  // 回填后已是标准 URL
    if (typeof csSyncAll === "function") csSyncAll();
    LAST_DETECT = { url: r.recommend, geo: r.geo || null };
    // 归属地写进备注(若备注为空),方便核对 IP 地区与账号是否一致
    if (r.geo_text && $("px-label") && !$("px-label").value.trim()) {
      const g = r.geo || {};
      $("px-label").value = [g.country, g.region, g.city].filter(Boolean).join("·") || "已判别";
    }
    const tag = r.scheme.toUpperCase() + (r.auth === "required" ? " · 含账密" : " · 免密");
    toast("判别:" + tag + (r.geo_text ? "  |  " + r.geo_text : "  |  归属地未取到"), r.browser_ok ? "ok" : "info");
    if (!r.browser_ok) toast("⚠️ " + r.note, "err", 8000);
  } catch (e) { toast("判别失败:" + e.message, "err"); }
  finally { btn.disabled = false; btn.textContent = old; }
}
async function addProxy() {
  const url = $("px-url").value.trim();
  if (!url) { toast("请填代理地址", "err"); return; }
  // 由后端统一解析，避免给导出格式加前缀后误认用户名/主机。
  const geo = (LAST_DETECT && LAST_DETECT.url === url) ? LAST_DETECT.geo : null;
  try {
    await api("/api/proxies", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url, label: $("px-label").value.trim(), geo, ...proxyInputOptions() }) });
    $("px-url").value = ""; $("px-label").value = "";
    toast("已加入代理池", "ok"); refreshProxies();
  } catch (e) { toast("添加失败:" + e.message, "err"); }
}
async function delPoolProxy(id, used) {
  if (!await uiConfirm({ title: "删除代理", okText: "删除", danger: true,
    message: "删除该代理?" + (used ? `\n⚠️ 有 ${used} 个账号正在用它,删除后这些账号需另选代理。` : "") })) return;
  try { await api("/api/proxies/" + id, { method: "DELETE" }); toast("已删除", "ok"); refreshProxies(); }
  catch (e) { toast("删除失败:" + e.message, "err"); }
}
async function editPoolProxy(id) {
  const p = PROXIES.find(x => x.id === id);
  if (!p) return;
  const label = await uiPrompt({
    title: "编辑代理备注",
    hint: p.url + (p.geo_loc ? "  ·  " + p.geo_loc : ""),
    value: p.label || "", placeholder: "如 住宅-广东-01",
    submit: label => api("/api/proxies/" + id, {
      method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ label: label.trim() }),
    }),
  });
  if (label === null) return;
  toast("备注已更新", "ok"); refreshProxies();
}
async function togglePoolProxy(id, enabled) {
  try {
    await api("/api/proxies/" + id, {
      method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled: !enabled }) });
    refreshProxies();
  } catch (e) { toast("操作失败:" + e.message, "err"); }
}
async function testPoolProxy(id) {
  const btn = event.target.closest("button"); btn.disabled = true; const old = btn.textContent; btn.textContent = "测试中…";
  try { const r = await api("/api/proxies/" + id + "/test", { method: "POST" });
    toast((r.ok ? "通用连接可用 ✓ " : "通用检测未通过 ✗ ") + (r.detail || "") + (r.geo_text ? "  |  " + r.geo_text : ""), r.ok ? "ok" : "err"); }
  catch (e) { toast("测试失败:" + e.message, "err"); }
  finally { btn.disabled = false; btn.textContent = old; refreshProxies(); }
}
async function testAllProxies() {
  if (!PROXIES.length) { toast("代理池为空", "info"); return; }
  toast("开始逐个测试…", "info");
  for (const p of PROXIES) {
    try { await api("/api/proxies/" + p.id + "/test", { method: "POST" }); } catch (e) { }
  }
  toast("测试完成", "ok"); refreshProxies();
}
async function importProxies() {
  const options = proxyInputOptions();
  const text = await uiPrompt({
    title: "批量导入代理",
    hint: "每行一条，支持六种格式、标准 URL、# 注释及空行；备注推荐写「备注 | 地址」。\n使用页面所选输入格式；未带协议时默认 " + options.default_scheme.toUpperCase() + "，已带协议时保留原值。",
    multiline: true, rows: 8,
    placeholder: "住宅-01 | proxy.example:8080:USER:TOKEN\nproxy.example,8080,USER,TOKEN\nsocks5://USER:TOKEN@proxy.example:1080" });
  if (text === null || !text.trim()) return;
  try {
    const r = await api("/api/proxies/import", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text, ...options }) });
    let msg = `导入完成:新增 ${r.added}`;
    if (r.skipped) msg += ` · 重复跳过 ${r.skipped}`;
    if (r.invalid) msg += ` · 格式无效 ${r.invalid}`;
    toast(msg, r.added ? "ok" : "info");
    if (r.invalid_samples?.length) toast(r.invalid_samples.join("\n"), "err", 10000);
    refreshProxies();
  } catch (e) { toast("导入失败:" + e.message, "err"); }
}
async function assignAllProxies() {
  const noProxy = ACCOUNTS.filter(a => !a.has_proxy).length;
  if (!noProxy) { toast("所有账号都已配置代理", "info"); return; }
  if (!await uiConfirm({ title: "批量分配代理", message: `给 ${noProxy} 个未配代理的账号从池里自动分配(均衡,占用最少优先)?` })) return;
  const btn = event.target.closest("button"); if (btn) { btn.disabled = true; btn.textContent = "分配中…"; }
  try {
    const r = await api("/api/accounts/assign-proxies-all", { method: "POST" });
    let msg = `已分配 ${r.assigned} 个账号`;
    if (r.unassigned) msg += `,还有 ${r.unassigned} 个没分到(代理池不够,请再加代理)`;
    toast(msg, r.unassigned ? "info" : "ok");
    refreshAccounts(); refreshProxies();
  } catch (e) { toast("分配失败:" + e.message, "err"); }
  finally { if (btn) { btn.disabled = false; btn.textContent = "给账号批量分配"; } }
}
async function testProxy(id) {
  const btn = event.target.closest("button"); btn.disabled = true; const old = btn.textContent; btn.textContent = "测试中…";
  try {
    const r = await api("/api/accounts/" + id + "/test-proxy", { method: "POST" });
    toast((r.ok ? "代理可用 ✓ " : "代理不可用 ✗ ") + (r.detail || ""), r.ok ? "ok" : "err");
  } catch (e) { toast("测试失败:" + e.message, "err"); }
  finally { btn.disabled = false; btn.textContent = old; refreshAccounts(); }
}
async function relogin(id, scope = "auto") {
  const btn = evtBtn();
  await withBusy(btn, "启动中", async () => {
    try {
      const res = await api("/api/accounts/" + id + "/relogin/start?scope=" + encodeURIComponent(scope), { method: "POST" });
      const label = res.login_scope === "creator" ? "创作平台" : "主站读取";
      toast("已打开" + label + "浏览器窗口,请扫码登录该账号", "info");
      pollReloginTask(res.task_id);
    } catch (e) { toast("启动失败:" + e.message, "err"); }
  });
}
function pollReloginTask(tid) {
  let t = null;
  let persistedShown = false;
  const tick = async () => {
    try {
      const r = await api("/api/login/browser/poll?task_id=" + tid);
      if (r.status === "persisted" && !persistedShown) {
        persistedShown = true;
        toast("扫码已确认，正在校验登录态", "info");
        refreshAccounts();
      } else if (r.status === "confirmed") {
        clearTimeout(t);
        if (r.profile_status === "invalid") {
          toast("登录校验未通过，请重新扫码", "err");
        } else {
          const suffix = ["error", "deferred"].includes(r.profile_status) ? "（资料可稍后刷新）" : "";
          toast("重新登录成功 " + (r.nickname || "") + suffix, r.profile_status === "error" ? "info" : "ok");
        }
        refreshAccounts(); return;
      } else if (r.status === "expired") {
        clearTimeout(t); toast("超时未登录,请重试", "err"); return;
      } else if (r.status === "error") {
        clearTimeout(t); toast("出错:" + (r.error || ""), "err"); return;
      }
      t = setTimeout(tick, 600);
    } catch (e) { clearTimeout(t); }
  };
  tick();
}
async function delAccount(id) {
  const a = ACCOUNTS.find(x => x.id === id);
  const warn = a && a.monitor_count > 0 ? `\n关联的 ${a.monitor_count} 个作品监控也会暂停。` : "";
  if (!await uiConfirm({ title: "删除账号", message: "删除该账号将取消未执行任务、停用关联规则并清理独占登录环境；历史内容保留。执行中的操作需要先结束。" + warn, okText: "删除并停止关联任务", danger: true })) return;
  try {
    const result = await api("/api/accounts/" + id, { method: "DELETE" });
    toast(`账号已删除，取消 ${result.canceled_tasks || 0} 个任务，暂停 ${(result.disabled_rules || 0) + (result.disabled_monitors || 0)} 项规则/监控`, "ok", 6000);
    if (result.profile_cleanup_error) toast(result.profile_cleanup_error, "info", 7000);
    refreshAccounts(); refreshMonitors(); refreshCommentRules(); refreshCommentTasks(); refreshPublish();
  }
  catch (e) { toast("删除失败:" + e.message, "err"); }
}

// ─── API 兼容矩阵 / 多账号环境隔离 ───
let TRANSPORT_MATRIX = null;
function transportModeLabel(mode) {
  return ({ api: "API 直连", browser: "浏览器", hybrid: "API → 浏览器回退",
    auto: "原生协议优先", protocol: "仅原生协议",
    manual: "人工草稿", unavailable: "当前不可用", deferred: "已暂缓" })[mode] || mode || "未知";
}
function transportModeTag(mode) {
  const safe = ["api", "browser", "hybrid", "manual", "unavailable", "deferred"].includes(mode) ? mode : "manual";
  return `<span class="transport-mode ${safe}">${esc(transportModeLabel(mode))}</span>`;
}
function transportSupportTag(enabled, label) {
  return enabled
    ? `<span class="pill done bare">${esc(label)}可用</span>`
    : `<span class="pill skipped bare">${esc(label)}—</span>`;
}
function transportRoute(platform, operation) {
  return TRANSPORT_MATRIX?.rows?.find(row => row.platform === platform && row.operation === operation) || null;
}
function transportSourceSuffix(source) {
  const labels = { api: "API 直连", web_api: "API 直连", browser: "浏览器",
    browser_fallback: "浏览器回退", api_browser_context: "浏览器上下文 API" };
  return labels[source] ? ` · ${labels[source]}` : "";
}
function renderTransportMatrix(data) {
  const body = $("transport-matrix-body"), isolation = $("transport-isolation-body");
  if (!body || !isolation) return;
  const platformNames = { douyin: "抖音", xhs: "小红书", rednote: "海外小红书", kuaishou: "快手", shipinhao: "视频号", tiktok: "TikTok", bilibili: "哔哩哔哩", instagram: "Instagram", twitter: "Twitter" };
  const rows = Array.isArray(data?.rows) ? data.rows : [];
  body.innerHTML = rows.length ? rows.map(row => `<tr>
    <td><b>${esc(platformNames[row.platform] || row.platform)}</b></td>
    <td>${esc(row.label || row.operation)}</td>
    <td>${transportSupportTag(!!row.api, "API")}</td>
    <td>${transportSupportTag(!!row.browser, "浏览器")}</td>
    <td>${transportModeTag(row.effective_mode)}${row.configured_mode !== row.effective_mode ? `<div class="isolation-note">配置：${esc(transportModeLabel(row.configured_mode))}</div>` : ""}</td>
    <td>${esc(row.reason || row.note || (row.fallback === "browser_on_confirmed_failure" ? "明确失败时回退；不确定写结果不重试" : "—"))}${row.platform === "shipinhao" && row.requires_channels_cdp ? `<div class="isolation-note"><a class="channels-inline-jump" href="#channels-protocol-card" onclick="event.preventDefault();openChannelsProtocolSettings()">${data?.settings?.channels_cdp_ready ? "管理本机微信通道 →" : "去配置本机微信通道 →"}</a></div>` : ""}</td>
  </tr>`).join("") : empty(6, "尚无兼容矩阵", "i-info");
  const accounts = Array.isArray(data?.accounts) ? data.accounts : [];
  isolation.innerHTML = accounts.length ? accounts.map(account => {
    const badge = (ok, yes, no) => `<span class="pill ${ok ? "done" : "pending"} bare">${esc(ok ? yes : no)}</span>`;
    return `<tr>
      <td><b>${esc(account.nickname || `账号 #${account.account_id}`)}</b><div class="isolation-note">${esc(platformNames[account.platform] || account.platform)} · #${Number(account.account_id) || "-"}</div></td>
      <td>${badge(account.profile_isolated, "独立", "缺失/重复")}</td>
      <td><div class="isolation-stack">${badge(account.credential_isolated, "登录态独立", "登录态待检查")}${badge(account.api_session_isolated, "会话独立", "会话共享")}${badge(account.api_environment_aligned, "参数对齐", "参数未对齐")}</div></td>
      <td>${badge(account.network_isolated, "专属代理", account.network_scope || "共享出口")}</td>
      <td><code>${esc(account.environment_id || "-")}</code>${account.warnings?.length ? `<div class="isolation-note">${account.warnings.map(esc).join("；")}</div>` : ""}</td>
    </tr>`;
  }).join("") : empty(5, "尚未添加账号", "i-user");
}
async function refreshTransportMatrix() {
  const status = $("transport-matrix-status");
  try {
    const data = await api("/api/settings/transport-matrix");
    TRANSPORT_MATRIX = data;
    renderTransportMatrix(data);
    if (status) status.textContent = `已核对 ${data.rows?.length || 0} 项能力、${data.accounts?.length || 0} 个账号环境`;
    return data;
  } catch (error) {
    if (status) status.textContent = "兼容矩阵读取失败：" + error.message;
    return null;
  }
}
globalThis.CreatorHubTransportMatrix = { load: refreshTransportMatrix, route: transportRoute };

// ─── 下载设置 ───
async function loadSettings() {
  globalThis.CreatorHubEngineSettings?.load();
  globalThis.CreatorHubChannelsProtocol?.load?.();
  refreshTransportMatrix();
  try {
    const s = await api("/api/settings");
    const assign = (id, property, value) => {
      if (!globalThis.CreatorHubWorkbench?.isDirty?.(id)) $(id)[property] = value;
    };
    assign("dl-dir", "value", s.download_dir || "");
    assign("dl-quality", "value", s.video_quality || "highest");
    if ($("ai-enabled")) {
      assign("ai-enabled", "checked", !!s.ai_enabled);
      assign("ai-base", "value", s.ai_base_url || "");
      assign("ai-model", "value", s.ai_model || "");
      assign("ai-temp", "value", s.ai_temperature || "0.9");
      assign("ai-prompt", "value", s.ai_prompt || "");
      $("ai-key").placeholder = s.ai_api_key_set ? "已保存(留空=不修改)" : "API Key";
    }
    csSyncAll();
  } catch (e) {}
}
async function saveAiSettings() {
  if (!validateAiSettings(false)) return;
  $("ai-msg").textContent = "保存中…";
  const body = {
    ai_enabled: $("ai-enabled").checked, ai_base_url: $("ai-base").value.trim(),
    ai_model: $("ai-model").value.trim(), ai_temperature: $("ai-temp").value.trim() || "0.9",
    ai_prompt: $("ai-prompt").value,
  };
  const key = $("ai-key").value.trim();
  if (key) body.ai_api_key = key;
  await withBusy(evtBtn(), "保存中", async () => {
  try {
    const s = await api("/api/settings", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    $("ai-key").value = ""; $("ai-key").placeholder = s.ai_api_key_set ? "已保存(留空=不修改)" : "API Key";
    $("ai-msg").textContent = "已保存 ✓ " + (s.ai_enabled ? "(规则勾选「用 AI」即生效)" : "(当前未启用)");
    toast("AI 设置已保存", "ok");
  } catch (e) { $("ai-msg").textContent = "失败: " + e.message; toast("保存失败:" + e.message, "err"); }
  });
}
async function testAi() {
  const btn = evtBtn();
  if (!validateAiSettings(true)) return;
  $("ai-msg").textContent = "测试中…";
  // 用当前表单值测(key 留空则用已保存的),方便保存前先验证
  const body = {
    base_url: $("ai-base").value.trim(), model: $("ai-model").value.trim(),
    prompt: $("ai-prompt").value, temperature: $("ai-temp").value.trim() || "0.9",
  };
  const key = $("ai-key").value.trim();
  if (key) body.api_key = key;
  await withBusy(btn, "测试中", async () => {
    try {
      const r = await api("/api/settings/ai-test", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      if (r.ok) { $("ai-msg").innerHTML = `连通正常 ✓ 样例文案:<b>${esc(r.sample || "")}</b>`; toast("AI 连通正常 ✓", "ok", 6000); }
      else { $("ai-msg").textContent = "连通失败:" + (r.error || ""); toast("AI 连通失败:" + (r.error || ""), "err", 8000); }
    } catch (e) { $("ai-msg").textContent = "失败:" + e.message; toast("测试失败:" + e.message, "err"); }
  });
}
async function saveSettings() {
  $("dl-msg").textContent = "保存中…";
  await withBusy(evtBtn(), "保存中", async () => {
  try {
    const s = await api("/api/settings", {
      method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ download_dir: $("dl-dir").value.trim(), video_quality: $("dl-quality").value }),
    });
    $("dl-dir").value = s.download_dir || "";
    $("dl-quality").value = s.video_quality || "highest";
    csSyncAll();
    $("dl-msg").textContent = "已保存 ✓ 新作品将按此设置下载";
    toast("下载设置已保存", "ok");
  } catch (e) { $("dl-msg").textContent = "失败: " + e.message; toast("保存失败:" + e.message, "err"); }
  });
}
