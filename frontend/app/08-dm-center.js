// 08-dm-center.js — 私信会话收件箱、SSE 实时消息流、历史消息拉取与自动回复规则。
// ── 私信 ──
// ─── 私信实时接收(SSE):进 DM 面板订阅,新消息即时刷新;离开断开 ───
let DM_SSE = null, DM_SSE_ACC = "", DM_AUTO_TASKS = [], DM_AUTO_RULES = [], DM_REFRESH_TIMER = null;
function startDmStream() {
  // 幂等:同账号已连就不重连(避免每次面板刷新/收到消息都断开重来)
  if ((DM_SSE || DM_REFRESH_TIMER) && DM_SSE_ACC === HUB_ACC &&
      (!DM_SSE || DM_SSE.readyState !== 2)) return;
  stopDmStream();
  if (!HUB_ACC) return;
  DM_SSE_ACC = HUB_ACC;
  if (PLATFORM !== "douyin" && PLATFORM !== "xhs") return;
  try {
    DM_SSE = new EventSource(`/api/dm/stream?account_id=${HUB_ACC}`);
    DM_SSE.onmessage = (e) => {
      let evt; try { evt = JSON.parse(e.data); } catch (_) { return; }
      if (!evt || !evt.conv_id) return;
      // 当前打开的会话:实时刷新线程 + 标记已读(不让红点冒出来);否则只刷列表(会有红点)
      if (evt.conv_id === DM_CONV) { refreshDmMessages(); markDmRead(evt.conv_id); }
      else refreshDmConvs();
      if (PLATFORM === "xhs" && evt.type === "auto_reply") refreshDmAutomation();
    };
    DM_SSE.onerror = () => { /* EventSource 自带重连 */ };
  } catch (_) {}
}
function stopDmStream() {
  if (DM_SSE) { try { DM_SSE.close(); } catch (_) {} DM_SSE = null; }
  if (DM_REFRESH_TIMER) { clearInterval(DM_REFRESH_TIMER); DM_REFRESH_TIMER = null; }
  DM_SSE_ACC = "";
}

async function refreshDmConvs() {
  const isCurrent = beginViewRequest("dm-convs", () => String(HUB_ACC));
  const box = $("dm-convs"); if (!box) return;
  if (!HUB_ACC) { box.innerHTML = `<div class="empty" style="padding:24px"><div class="empty-t">请先选择账号</div></div>`; return; }
  try {
    const list = await api("/api/dm/conversations?account_id=" + HUB_ACC);
    if (!isCurrent()) return;
    DM_CONVS = list;
    if ($("hb-dm")) $("hb-dm").textContent = list.length;
    box.innerHTML = list.length ? list.map(convRow).join("")
      : `<div class="empty" style="padding:24px"><div class="empty-ic">${ic("i-send")}</div><div class="empty-t">暂无会话</div><div class="empty-sub">点右上「同步私信」</div></div>`;
    if (DM_CONV) { const el = box.querySelector(`.dm-conv[data-conv="${cssAttr(DM_CONV)}"]`); if (el) el.classList.add("active"); }
  } catch (e) { if (!isCurrent()) return; box.innerHTML = `<div class="empty" style="padding:24px"><div class="empty-t">加载失败:${esc(e.message)}</div></div>`; }
}
function cssAttr(s) { return (s || "").toString().replace(/"/g, '\\"'); }
function convRow(c) {
  return `<div class="dm-conv" data-conv="${esc(c.conv_id)}" onclick="openDmConv(this.dataset.conv)">
    ${c.peer_avatar ? `<img class="avatar" src="${esc(safeMediaUrl(c.peer_avatar))}" referrerpolicy="no-referrer" alt="">` : `<span class="avatar"></span>`}
    <div class="meta"><b>${esc(c.peer_nickname)}</b><div class="last">${esc(c.last_text || "")}</div></div>
    ${c.unread_count ? `<span class="unread">${c.unread_count}</span>` : ""}
  </div>`;
}
async function syncDm() {
  if (!HUB_ACC) { toast("请先选择账号", "err"); return; }
  await withBusy(evtBtn(), "同步中", async () => {
    try {
      const r = await api("/api/accounts/" + HUB_ACC + "/dm/sync", { method: "POST" });
      if (r.skipped && r.cached) toast(`检查间隔保护中，已展示 ${r.fetched} 个现有会话`, "ok");
      else if (r.skipped) toast(`检查间隔保护中，请稍后再试`, "ok");
      else toast(`同步完成：${r.fetched} 个会话，新增消息 ${r.added}`, "ok");
    }
    catch (e) { toast("同步失败:" + e.message, "err"); }
  });
  refreshDmConvs();
  refreshDmAutomation();
}
async function openDmConv(convId) {
  DM_CONV = convId;
  DM_NEW_TARGET = "";
  const isCurrent = beginViewRequest("open-dm-conv", () => `${HUB_ACC}:${DM_CONV}`);
  const accountId = HUB_ACC;
  document.querySelectorAll("#dm-convs .dm-conv").forEach(e => e.classList.toggle("active", e.dataset.conv === convId));
  const thread = $("dm-thread");
  if (thread) thread.innerHTML = `<div class="empty"><div class="empty-t">加载聊天记录…</div></div>`;
  // 抖音:点开会话时无头拉历史(imapi get_by_conversation),落库后再渲染
  if (PLATFORM === "douyin" || PLATFORM === "xhs") {
    try { await api(`/api/accounts/${accountId}/dm/conversations/${encodeURIComponent(convId)}/fetch-history`, { method: "POST" }); }
    catch (e) { /* 拉取失败也照常显示库里已有的(最后一条) */ }
  }
  if (!isCurrent()) return;
  markDmRead(convId);
  await refreshDmMessages();
}

function normalizeDmTarget(value) {
  let target = String(value || "").trim();
  if (!target) return "";
  try {
    const url = new URL(target);
    if (url.protocol !== "https:" || !/(^|\.)douyin\.com$/i.test(url.hostname)) return "";
    const match = url.pathname.match(/\/user\/([^/?#]+)/);
    if (!match) return "";
    target = decodeURIComponent(match[1]);
  } catch (_) {}
  return target.replace(/^@/, "").trim();
}

async function startNewDm() {
  if (!HUB_ACC) { toast("请先选择账号", "err"); return; }
  if (PLATFORM !== "douyin") return;
  const raw = await uiPrompt({
    title: "发起新私信",
    hint: "填写对方抖音号、主页链接或 sec_uid。抖音号会先精确解析为内部 UID。",
    placeholder: "抖音号 / https://www.douyin.com/user/...",
  });
  if (raw === null) return;
  const target = normalizeDmTarget(raw);
  if (!target) { toast("目标用户不能为空", "err"); return; }
  DM_CONV = null;
  DM_NEW_TARGET = target;
  document.querySelectorAll("#dm-convs .dm-conv").forEach(e => e.classList.remove("active"));
  const thread = $("dm-thread");
  if (thread) thread.innerHTML = `<div class="empty"><div class="empty-ic">${ic("i-send")}</div><div class="empty-t">新私信</div><div class="empty-sub">目标 ${esc(target)}</div></div>`;
  const input = $("dm-input");
  if (input) { input.placeholder = "输入第一条私信…"; input.focus(); }
}

function dmRuleSummary(rule) {
  const trigger = rule.match_mode === "all" ? "全部文本消息" : (rule.keywords || []).join("、");
  const mode = rule.review_before_send ? "先审核" : "自动入队";
  return `<div class="dm-auto-row">
    <div class="grow"><b>${esc(rule.name)}</b>${rule.enabled ? "" : " · 已停用"}<div class="sub">${esc(trigger)} · ${mode} · 延迟 ${rule.min_delay_seconds}-${rule.max_delay_seconds} 秒 · 冷却 ${Math.round(rule.cooldown_seconds / 3600)} 小时</div></div>
    <button class="ghost sm" onclick="toggleDmRule(${rule.id})">${rule.enabled ? "停用" : "启用"}</button>
    <button class="ghost sm" onclick="deleteDmRule(${rule.id})">删除</button>
  </div>`;
}
function dmDraftSummary(task) {
  return `<div class="dm-auto-row">
    <div class="grow"><b>${esc(task.target_nick || "私信会话")}</b><div class="sub">${esc(task.content || "")}</div></div>
    <button class="ghost sm" onclick="editDmDraft(${task.id})">编辑</button>
    <button class="sm" onclick="approveDmDraft(${task.id})">通过</button>
    <button class="ghost sm" onclick="cancelDmDraft(${task.id})">取消</button>
  </div>`;
}
async function refreshDmAutomation() {
  const isCurrent = beginViewRequest("dm-automation", () => HUB_ACC);
  const panel = $("dm-auto-panel"), rulesBox = $("dm-auto-rules"), tasksBox = $("dm-auto-tasks");
  if (!panel || !rulesBox || !tasksBox) return;
  panel.style.display = PLATFORM === "xhs" ? "" : "none";
  if (PLATFORM !== "xhs" || !HUB_ACC) return;
  try {
    const [rules, tasks, monitor] = await Promise.all([
      api(`/api/dm/auto-reply-rules?account_id=${HUB_ACC}`),
      api(`/api/account-actions?account_id=${HUB_ACC}&limit=100`),
      api(`/api/accounts/${HUB_ACC}/dm/automation/status`)
    ]);
    if (!isCurrent()) return;
    const statusBox = $("dm-monitor-status");
    if (statusBox) {
      const live = monitor.realtime || {};
      const state = live.connected ? "实时监听已连接" : (live.state === "reconnecting" ? "实时监听重连中" : "低频补偿监听");
      statusBox.innerHTML = `<b>${state}</b> · 账号级监控 · 新会话自动纳入 · ${Math.round((monitor.fallback_interval_seconds || 600) / 60)} 分钟完整补偿检查`;
    }
    DM_AUTO_RULES = rules;
    rulesBox.innerHTML = rules.length ? rules.map(dmRuleSummary).join("") : `<div class="hint">暂无规则。建议先使用“先审核”观察一段时间。</div>`;
    DM_AUTO_TASKS = tasks;
    const drafts = tasks.filter(t => t.action === "send_dm" && t.source_rule_id && t.status === "draft");
    tasksBox.innerHTML = drafts.length ? drafts.map(dmDraftSummary).join("") : `<div class="hint">暂无待审核回复</div>`;
  } catch (e) { if (isCurrent()) rulesBox.innerHTML = `<div class="hint">加载失败：${esc(e.message)}</div>`; }
}
async function saveDmRule() {
  if (!HUB_ACC) { toast("请先选择账号", "err"); return; }
  const keywords = ($("dm-rule-keywords").value || "").split(/[，,]/).map(x => x.trim()).filter(Boolean);
  const excludes = ($("dm-rule-excludes").value || "").split(/[，,]/).map(x => x.trim()).filter(Boolean);
  const templates = ($("dm-rule-templates").value || "").split(/\r?\n/).map(x => x.trim()).filter(Boolean);
  const body = {
    account_id: +HUB_ACC, name: ($("dm-rule-name").value || "自动回复").trim(),
    enabled: true, match_mode: "keywords", keywords, exclude_keywords: excludes,
    reply_templates: templates, review_before_send: !$("dm-rule-auto").checked,
    min_delay_seconds: +$("dm-rule-delay-min").value || 75,
    max_delay_seconds: +$("dm-rule-delay-max").value || 300,
    cooldown_seconds: (+$("dm-rule-cooldown").value || 6) * 3600,
    max_message_age_seconds: 1800
  };
  try {
    await api("/api/dm/auto-reply-rules", { method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(body) });
    toast("自动回复规则已保存", "ok"); refreshDmAutomation();
  } catch (e) { toast("保存失败：" + e.message, "err"); }
}
async function deleteDmRule(id) {
  try { await api(`/api/dm/auto-reply-rules/${id}`, {method:"DELETE"}); toast("规则已删除", "ok"); refreshDmAutomation(); }
  catch (e) { toast("删除失败：" + e.message, "err"); }
}
async function toggleDmRule(id) {
  const rule = DM_AUTO_RULES.find(r => +r.id === +id); if (!rule) return;
  const body = Object.assign({}, rule, {enabled: !rule.enabled});
  try {
    await api(`/api/dm/auto-reply-rules/${id}`, {method:"PUT", headers:{"Content-Type":"application/json"}, body:JSON.stringify(body)});
    toast(body.enabled ? "规则已启用" : "规则已停用", "ok"); refreshDmAutomation();
  } catch (e) { toast("更新失败：" + e.message, "err"); }
}
async function approveDmDraft(id) {
  try { await api(`/api/account-actions/${id}/approve`, {method:"POST"}); toast("已进入限速发送队列", "ok"); refreshDmAutomation(); }
  catch (e) { toast("通过失败：" + e.message, "err"); }
}
async function cancelDmDraft(id) {
  try { await api(`/api/account-actions/${id}/cancel`, {method:"POST"}); toast("草稿已取消", "ok"); refreshDmAutomation(); }
  catch (e) { toast("取消失败：" + e.message, "err"); }
}
async function editDmDraft(id) {
  const current = (DM_AUTO_TASKS.find(t => +t.id === +id) || {}).content || "";
  const content = await uiPrompt({ title: "编辑回复内容", hint: "保存只更新草稿，不会发送消息。", value: current, multiline: true, rows: 4,
    submit: value => {
      if (!value.trim()) uiEditorError("回复内容不能为空", "ui-inp");
      return api(`/api/account-actions/${id}`, {method:"PUT", headers:{"Content-Type":"application/json"}, body:JSON.stringify({content:value.trim()})});
    },
  });
  if (content === null) return;
  toast("草稿已更新", "ok"); refreshDmAutomation();
}
// 标记已读:清红点,刷新左侧列表
function markDmRead(convId) {
  if (!HUB_ACC || !convId) return;
  api(`/api/accounts/${HUB_ACC}/dm/conversations/${convId}/mark-read`, { method: "POST" })
    .then(() => refreshDmConvs()).catch(() => {});
}
// 分享视频卡片(msg_type=8):封面+标题+作者,点击跳抖音该视频
function dmVideoCard(c) {
  const url = c.item_id ? `https://www.douyin.com/video/${encodeURIComponent(c.item_id)}` : "#";
  const cover = c.cover
    ? `<img src="${esc(safeMediaUrl(c.cover))}" loading="lazy" referrerpolicy="no-referrer" onerror="this.style.display='none'">`
    : "";
  const avatar = c.avatar
    ? `<img class="av" src="${esc(safeMediaUrl(c.avatar))}" loading="lazy" referrerpolicy="no-referrer" onerror="this.style.display='none'">`
    : "";
  return `<a class="dm-vcard" href="${url}" target="_blank" rel="noopener">
    <div class="cov">${cover}<span class="play">▶</span></div>
    <div class="meta">
      <div class="ttl">${esc(c.title || "[视频]")}</div>
      <div class="au">${avatar}<span>${esc(c.author || "")}</span></div>
    </div>
  </a>`;
}
function dmBody(m) {
  if (m.card && m.card.kind === "video") return dmVideoCard(m.card);
  return esc(m.text);
}
async function refreshDmMessages() {
  const isCurrent = beginViewRequest("dm-messages", () => `${HUB_ACC}:${DM_CONV}`);
  const thread = $("dm-thread"); if (!thread || !HUB_ACC || !DM_CONV) return;
  try {
    const msgs = await api(`/api/dm/messages?account_id=${HUB_ACC}&conv_id=${encodeURIComponent(DM_CONV)}`);
    if (!isCurrent()) return;
    thread.innerHTML = msgs.length
      ? msgs.map(m => `<div class="dm-bubble ${m.direction === "out" ? "out" : "in"}${m.card ? " card" : ""}">${dmBody(m)}<span class="t">${fmtTime(m.create_time)}</span></div>`).join("")
      : `<div class="empty"><div class="empty-t">暂无消息记录</div><div class="empty-sub">该会话没有可拉取的历史(或对方为系统号)</div></div>`;
    thread.scrollTop = thread.scrollHeight;
  } catch (e) { if (!isCurrent()) return; thread.innerHTML = `<div class="empty"><div class="empty-t">加载失败:${esc(e.message)}</div></div>`; }
}
async function sendDm() {
  const inp = $("dm-input"); const text = (inp.value || "").trim();
  if (!HUB_ACC) { toast("请先选择账号", "err"); return; }
  if (!DM_CONV && !DM_NEW_TARGET) { toast("请选择会话或点“新私信”", "err"); return; }
  if (!text) return;
  const c = DM_CONVS.find(x => x.conv_id === DM_CONV) || {};
  const accountId = HUB_ACC, conversationId = DM_CONV, newTarget = DM_NEW_TARGET;
  const numericTarget = /^\d+$/.test(newTarget);
  await withBusy(evtBtn(), "发送中", async () => {
    try {
      const result = await api("/api/account-actions", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          account_id: +accountId, action: "send_dm",
          target_uid: c.peer_uid || (numericTarget ? newTarget : ""),
          target_sec_uid: c.peer_sec_uid || (!numericTarget ? newTarget : ""),
          target_nick: c.peer_nickname || "", conv_id: conversationId || "",
          content: text, run_now: true,
        })
      });
      if (HUB_ACC === accountId && DM_CONV === conversationId && inp.value.trim() === text) inp.value = "";
      toast(result.ran ? "已发送" : `任务 #${result.id} 已保留：${result.execution_error || "等待队列发送"}`, result.ran ? "ok" : "info", 6000);
      // 发完重拉历史,展示刚发出的消息(imapi 有短暂延迟,稍等再拉)
      await new Promise(r => setTimeout(r, 700));
      if (HUB_ACC !== accountId || DM_CONV !== conversationId) return;
      if (conversationId) await openDmConv(conversationId);
      else {
        await refreshDmConvs();
        const created = DM_CONVS.find(row =>
          String(row.peer_uid || "") === newTarget || String(row.peer_sec_uid || "") === newTarget);
        if (created) {
          DM_NEW_TARGET = "";
          inp.placeholder = "输入私信内容…";
          await openDmConv(created.conv_id);
        }
      }
    } catch (e) { toast("发送失败:" + e.message, "err"); }
  });
}
function accOptions(list, ph) {
  return `<option value="">${ph}</option>` +
    list.map(a => `<option value="${a.id}">${esc(a.nickname)}${a.has_creator ? " · 创作号" : ""}</option>`).join("");
}
function populateAccountSelect() {
  const sel = $("t-acc"); if (!sel) return;
  const required = ["xhs", "douyin", "tiktok"].includes(PLATFORM);
  const channels = PLATFORM === "shipinhao";
  const platformName = PF_NAME[PLATFORM] || "当前平台";
  const list = PLATFORM === "tiktok"
    ? ACCOUNTS.filter(a => a.status === "active" && a.has_storage)
    : ACCOUNTS;
  sel.innerHTML = accOptions(
    list,
    required ? `请选择${platformName}账号(必选)` : channels ? "默认走本机微信客户端（无需账号）" : "不指定账号",
  );
  // 抖音匿名主页可能返回风控后的旧快照；作品监控与小红书一样必须使用登录态。
  if (required && list.length) sel.value = String(list[0].id);
  else if (channels) sel.value = "";
  if ($("t-acc-wrap")) $("t-acc-wrap").style.display = channels ? "none" : "";
}
function collectionRequiresAccount(platform = PLATFORM, source = collectionSourceValue()) {
  return !(platform === "shipinhao" && ["keyword", "creator"].includes(source));
}
function populateCollectionAccount() {
  const sel = $("col-account"); if (!sel) return;
  const current = sel.value;
  const platformName = PF_NAME[PLATFORM] || "平台";
  const requiresAccount = collectionRequiresAccount(PLATFORM, collectionSourceValue());
  const channels = PLATFORM === "shipinhao";
  const list = ACCOUNTS.filter(a => a.platform === PLATFORM && a.status !== "invalid" && a.has_storage);
  const placeholder = requiresAccount
    ? (list.length ? `请选择${platformName}账号` : `暂无可用${platformName}账号`)
    : "使用本机微信通道（无需网页账号）";
  sel.innerHTML = accOptions(list, placeholder);
  sel.required = requiresAccount;
  if ($("col-account-wrap")) $("col-account-wrap").style.display = (channels && !requiresAccount) ? "none" : "";
  if (!requiresAccount) {
    setFieldError(sel, "");
    sel.value = "";
  } else if (list.some(a => String(a.id) === current)) sel.value = current;
  else if (list.length) sel.value = String(list[0].id);
  else sel.value = "";
  if (sel._csSync) sel._csSync();
}
function populateWatchAccount() {
  const sel = $("w-acc"); if (!sel) return;
  const xhs = PLATFORM === "xhs";
  const tiktok = PLATFORM === "tiktok";
  const channels = PLATFORM === "shipinhao";
  const required = xhs || PLATFORM === "rednote" || tiktok;
  const creatorOnly = PLATFORM === "douyin" && $("w-mode") && $("w-mode").value === "creator";
  const list = tiktok
    ? ACCOUNTS.filter(a => a.status === "active" && a.has_storage)
    : creatorOnly ? ACCOUNTS.filter(a => a.has_creator) : ACCOUNTS;
  const ph = required ? "请选择" + PF_NAME[PLATFORM] + "账号(必选)"
    : (creatorOnly && list.length === 0 ? "无创作者账号,请先创作者登录" : channels ? "默认走本机微信客户端（无需账号）" : "不指定账号");
  sel.innerHTML = accOptions(list, ph);
  if (required && list.length) sel.value = String(list[0].id);
  else if (channels) sel.value = "";
  if ($("w-acc-wrap")) $("w-acc-wrap").style.display = channels ? "none" : "";
  if (sel._csSync) sel._csSync();
}
function populateDanmakuAccount() {
  const sel = $("d-w-acc"); if (!sel) return;
  const pf = PLATFORM === "bilibili" ? "bilibili" : "douyin";
  const creatorOnly = pf === "douyin" && $("d-w-mode") && $("d-w-mode").value === "creator";
  const list = creatorOnly
    ? ACCOUNTS.filter(a => a.platform === "douyin" && a.has_creator)
    : ACCOUNTS.filter(a => a.platform === pf);
  const ph = creatorOnly && !list.length ? "无创作者账号,请先创作者登录" : "不指定账号";
  sel.innerHTML = accOptions(list, ph);
  if (creatorOnly && list.length) sel.value = String(list[0].id);
  if (sel._csSync) sel._csSync();
}
function applyDanmakuForm() {
  const isBili = PLATFORM === "bilibili";
  if (isBili && $("d-w-mode") && $("d-w-mode").value === "creator") {
    $("d-w-mode").value = "public";
    $("d-w-mode")._csSync?.();
  }
  const kind = $("d-w-kind") ? $("d-w-kind").value : "auto";
  const mode = $("d-w-mode") ? $("d-w-mode").value : "public";
  const isVideo = kind === "video";
  const recentWrap = $("d-w-recent-wrap");
  const daysWrap = $("d-w-days-wrap");
  if (recentWrap) recentWrap.hidden = isVideo;
  if (daysWrap) daysWrap.hidden = isVideo;
  const urlLabel = $("d-w-url-label");
  if (urlLabel) urlLabel.textContent = isBili
    ? (isVideo ? "视频链接 / 完整分享文案 / BV 或 av 号" : kind === "user" ? "UP 主空间主页 / 完整分享文案 / mid" : "视频 BV/av / 空间主页 / 完整分享文案")
    : (isVideo ? "视频 / 完整分享文案 / aweme_id" : kind === "user" ? "账号主页 / 完整分享文案 / sec_uid" : "视频 / 账号主页 / 完整分享文案 / ID");
  if ($("d-w-url")) $("d-w-url").placeholder = isBili
    ? (isVideo ? "粘贴哔哩哔哩视频分享文案、链接或 BV/av 号" : kind === "user" ? "粘贴空间主页链接或 UP 主 mid" : "直接粘贴整段哔哩哔哩分享文案，将自动识别视频或 UP 主")
    : (isVideo ? "粘贴视频分享文案、作品链接或 aweme_id" : kind === "user" ? "粘贴主页分享文案、账号主页或 sec_uid" : "直接粘贴整段抖音分享文案，将自动识别视频或账号");
  resetDanmakuTargetState();
  const accLabel = $("d-w-acc-label");
  if (accLabel) accLabel.textContent = mode === "creator"
    ? "创作中心账号（必选）" : "播放页账号（可选）";
  const depthLabel = $("d-w-depth-label");
  if (depthLabel) depthLabel.textContent = mode === "creator"
    ? "创作中心翻页深度" : "弹幕加载轮次";
  const probeWrap = $("d-w-probe-wrap");
  if (probeWrap) probeWrap.hidden = mode === "creator" || isBili;
  populateDanmakuAccount();
}
async function refreshProfile(id) {
  const btn = evtBtn();
  await withBusy(btn, "\u83b7\u53d6\u4e2d", async () => {
    try {
      const r = await api("/api/accounts/" + id + "/refresh-profile", { method: "POST" });
      if (r.skipped) {
        toast("\u672c\u6b21\u672a\u6267\u884c\u8d44\u6599\u5237\u65b0:" + (r.reason || "\u8d26\u53f7\u5f53\u524d\u4e0d\u53ef\u63a2\u6d4b"), "info");
        return;
      }
      if (["xhs", "rednote"].includes(r.platform || PLATFORM) && r.login_scope === "creator" && !r.has_read_login) {
        toast("创作平台登录有效，资料已更新；主站读取登录尚未配置", "info", 8000);
        return;
      }
      const refreshedPlatform = r.platform || PLATFORM;
      const idLbl = refreshedPlatform === "xhs" ? " · 小红书号 "
        : refreshedPlatform === "kuaishou" ? " · 快手号 "
        : refreshedPlatform === "shipinhao" ? " · 视频号 "
        : refreshedPlatform === "tiktok" ? " · TikTok号 "
        : refreshedPlatform === "rednote" ? " · Rednote号 " : refreshedPlatform === "bilibili" ? " · UID " : " · 抖音号 ";
      toast("\u8d44\u6599\u5df2\u66f4\u65b0\uff0c\u767b\u5f55\u72b6\u6001\u5df2\u6062\u590d:" + (r.nickname || "") + (r.douyin_id ? idLbl + r.douyin_id : ""), "ok");
    } catch (e) {
      toast("\u5237\u65b0\u5931\u8d25:" + e.message, "err");
    }
  });
  await refreshAccounts();
}
async function setProxy(id) {
  const a = findAnyAccount(id);
  let opts = [];
  try { opts = await api("/api/proxies/options"); } catch (e) { }
  const cur = a && a.has_proxy ? a.proxy : "";
  const pf = a ? a.platform : PLATFORM;
  const options = [
    { value: "auto", label: "🔀 自动分配（优先空闲独立节点，池满按最少占用复用）" },
    ...opts.map(p => ({ value: p.url, label: formatProxyOptionLabel(p, pf) })),
    { value: "__custom__", label: "✎ 手动输入地址…" },
    { value: "", label: "⚠️ 清除代理 / 改用本机直连（允许直连，多号或海外请注意风控）" },
  ];
  const v = await uiSelect({
    title: "账号代理配置",
    hint: (a ? a.nickname + " · " : "") + "当前：" + (cur || "本机直连（未配置代理）") + "。建议尽量一号一独立代理；系统不强制拦截本机直连或多号共用代理，存在共用/直连风险时会高亮提醒。",
    options, value: (cur && opts.some(o => o.value === cur)) ? cur : "auto",
  });
  if (v === null) return;
  try {
    let r = null;
    if (v === "auto") {
      r = await api("/api/accounts/" + id + "/assign-proxy", { method: "POST" });
      toast("已从代理池分配：" + r.proxy, "ok");
    } else if (v === "__custom__") {
      const url = await uiPrompt({
        title: "手动输入代理", value: cur,
        hint: "http://user:pass@host:port 或 socks5://host:port；留空=清除代理改用本机直连（允许复用同一代理，建议尽量一号一代理）",
        placeholder: "http://user:pass@host:port" });
      if (url === null) return;
      r = await api("/api/accounts/" + id + "/proxy", {
        method: "PUT", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ proxy: url.trim() }) });
      toast(url.trim() ? "代理已设置：" + r.proxy : "已清除代理，当前改用本机直连", "ok");
    } else {
      r = await api("/api/accounts/" + id + "/proxy", {
        method: "PUT", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ proxy: v }) });
      toast(v ? "代理已设置：" + r.proxy : "已清除代理，当前改用本机直连", "ok");
    }
    if (r && r.warning) {
      setTimeout(() => toast("⚠️ " + r.warning, "info", 7000), 280);
    }
    refreshAccounts(); refreshProxies();
  } catch (e) { toast("设置失败:" + e.message, "err"); }
}

async function setBrowserBackend(id) {
  const account = findAnyAccount(id);
  if (!account) return;
  let catalog;
  try {
    catalog = await api("/api/browser-backends");
  } catch (e) {
    toast("读取浏览器环境失败:" + e.message, "err");
    return;
  }
  const isXhs = account.platform === "xhs";
  const options = browserChoiceOptions(catalog, { localOnly: false });
  const currentChoice = account.browser_backend === "fingerprint_chromium" && account.browser_runtime_id
    ? `fingerprint_chromium::${account.browser_runtime_id}`
    : (account.browser_backend || (isXhs ? "local" : "default"));
  const selected = await uiSelect({
    title: "账号浏览器环境",
    hint: isXhs
      ? `${account.nickname} · 单号推荐系统 Chrome/CDP；同机多小红书号推荐切换至 Fingerprint Chromium 隔离硬件指纹。`
      : `${account.nickname} · 切换时会关闭该账号当前浏览器，下次任务使用新内核。`,
    options,
    value: currentChoice,
  });
  if (selected === null) return;
  try {
    const choice = browserChoiceParts(selected);
    const result = await api(`/api/accounts/${id}/browser-backend`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        browser_backend: choice.backend,
        browser_runtime_id: choice.runtimeId,
      }),
    });
    toast("浏览器环境已切换：" + loginEnvironmentText(result.environment), "ok");
    refreshAccounts();
  } catch (e) {
    toast("切换失败:" + e.message, "err");
  }
}

async function refreshBrowserRuntimes() {
  const table = $("runtime-table");
  if (!table) return;
  try {
    const result = await api("/api/browser-runtimes");
    BROWSER_RUNTIMES = result.runtimes || [];
    let rememberedRoot = "";
    try { rememberedRoot = localStorage.getItem("creatorhub-browser-runtime-root") || ""; } catch (e) {}
    BROWSER_RUNTIME_ROOT = rememberedRoot || result.root || "";
    const rootLabel = $("runtime-root");
    if (rootLabel) rootLabel.textContent = BROWSER_RUNTIME_ROOT || "尚未设置，扫描时输入";
    table.querySelector("tbody").innerHTML = BROWSER_RUNTIMES.map(runtime => {
      const state = !runtime.enabled ? "已停用" : runtime.available
        ? (runtime.status === "ok" ? "测试通过" : "可用") : "文件缺失";
      const stateClass = runtime.enabled && runtime.available
        ? "active" : runtime.status === "bad" ? "invalid" : "bare";
      return `<tr>
        <td>
          <div><b>${esc(runtime.name || runtime.runtime_id)}</b>
            ${runtime.is_default ? '<span class="pill active">默认</span>' : ""}
            <span class="pill ${stateClass}">${esc(state)}</span>
          </div>
          <div class="mut" style="font-size:11px;margin-top:3px">版本 ${esc(runtime.version || "未知")} · ${esc(runtime.runtime_id)}</div>
          <div class="mut" style="font-size:11px;margin-top:3px;word-break:break-all"><code>${esc(runtime.executable_path)}</code></div>
          ${runtime.last_error ? `<div style="font-size:11px;margin-top:3px;color:var(--danger)">${esc(runtime.last_error)}</div>` : ""}
        </td>
        <td class="acttd">
          <button class="ghost sm" onclick="testBrowserRuntime('${esc(runtime.runtime_id)}')">测试启动</button>
          ${runtime.is_default ? "" : `<button class="ghost sm" onclick="setDefaultBrowserRuntime('${esc(runtime.runtime_id)}')" ${runtime.enabled ? "" : "disabled"}>设为默认</button>`}
          <button class="ghost sm" onclick="toggleBrowserRuntime('${esc(runtime.runtime_id)}', ${runtime.enabled ? "false" : "true"})">${runtime.enabled ? "停用" : "启用"}</button>
          <button class="ghost sm danger" onclick="deleteBrowserRuntime('${esc(runtime.runtime_id)}')">${ic("i-trash")}移除</button>
        </td>
      </tr>`;
    }).join("") || empty(2, "尚未发现指纹内核", "i-inbox", "点击扫描目录，或手动添加 chrome.exe");
  } catch (e) {
    table.querySelector("tbody").innerHTML = empty(2, "读取内核列表失败", "i-info", e.message);
  }
}

async function scanBrowserRuntimes() {
  const button = evtBtn();
  const root = await uiPrompt({
    title: "扫描 Chromium 内核目录",
    hint: "输入当前机器上存放一个或多个 Chromium 版本的目录。路径按本机配置，不限定盘符或操作系统。",
    value: BROWSER_RUNTIME_ROOT,
    placeholder: "例如：浏览器安装目录或统一内核目录",
  });
  if (root === null) return;
  if (!root.trim()) { toast("请输入要扫描的目录", "err"); return; }
  await withBusy(button, "扫描中", async () => {
    try {
      const result = await api("/api/browser-runtimes/scan", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ root: root.trim() }),
      });
      BROWSER_RUNTIME_ROOT = result.root || root.trim();
      try { localStorage.setItem("creatorhub-browser-runtime-root", BROWSER_RUNTIME_ROOT); } catch (e) {}
      toast(`扫描完成：发现 ${result.found} 个内核，新增 ${result.created} 个`, "ok");
      await refreshBrowserRuntimes();
    } catch (e) { toast("扫描失败：" + e.message, "err"); }
  });
}

async function addBrowserRuntime() {
  const path = await uiPrompt({
    title: "添加 Chromium 内核",
    hint: "填写当前机器上 chrome/chromium 主程序的完整路径。程序只记录路径，不复制或移动浏览器文件。",
    value: "",
    placeholder: "chrome.exe、chrome 或 chromium 的完整路径",
  });
  if (path === null || !path.trim()) return;
  try {
    const result = await api("/api/browser-runtimes", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ executable_path: path.trim() }),
    });
    toast(`${result.created ? "已添加" : "已更新"}：${result.runtime.name}`, "ok");
    await refreshBrowserRuntimes();
  } catch (e) { toast("添加失败：" + e.message, "err"); }
}

async function testBrowserRuntime(runtimeId) {
  const button = evtBtn();
  await withBusy(button, "测试中", async () => {
    try {
      const result = await api(`/api/browser-runtimes/${encodeURIComponent(runtimeId)}/test`, { method: "POST" });
      toast(`内核启动正常：${result.user_agent || runtimeId}`, "ok", 7000);
    } catch (e) { toast("内核测试失败：" + e.message, "err", 7000); }
    await refreshBrowserRuntimes();
  });
}

async function setDefaultBrowserRuntime(runtimeId) {
  try {
    await api(`/api/browser-runtimes/${encodeURIComponent(runtimeId)}`, {
      method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ is_default: true }),
    });
    toast("默认指纹内核已切换", "ok");
    await refreshBrowserRuntimes(); await refreshAccounts();
  } catch (e) { toast("切换失败：" + e.message, "err"); }
}

async function toggleBrowserRuntime(runtimeId, enabled) {
  try {
    await api(`/api/browser-runtimes/${encodeURIComponent(runtimeId)}`, {
      method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled }),
    });
    toast(enabled ? "内核已启用" : "内核已停用", "ok");
    await refreshBrowserRuntimes(); await refreshAccounts();
  } catch (e) { toast("操作失败：" + e.message, "err"); }
}

async function deleteBrowserRuntime(runtimeId) {
  if (!await uiConfirm({
    title: "移除内核记录",
    message: "仅移除 CreatorHub 中的内核记录，不会删除原安装目录中的浏览器文件。",
    okText: "移除",
  })) return;
  try {
    await api(`/api/browser-runtimes/${encodeURIComponent(runtimeId)}`, { method: "DELETE" });
    toast("内核记录已移除", "ok");
    await refreshBrowserRuntimes();
  } catch (e) { toast("移除失败：" + e.message, "err"); }
}

function uiFingerprintEditor(account, fp, options = {}) {
  const preLogin = Boolean(options.preLogin);
  const disabled = new Set(fp.disable_spoofing || []);
  const runtimeVersion = String((account.environment || {}).runtime_version || "");
  const runtimeMajor = Number(runtimeVersion.split(".")[0]) || 0;
  const gpuCustomSupported = runtimeMajor >= 139 && runtimeMajor < 144;
  const modeButtons = (name, options, current) => `<div class="fp-segment" data-fp-mode="${esc(name)}" role="group">` +
    options.map(option => `<button type="button" class="${option.value === current ? "active" : ""}" data-value="${esc(option.value)}" aria-pressed="${option.value === current ? "true" : "false"}"${option.disabled ? " disabled" : ""}>${esc(option.label)}</button>`).join("") + `</div>`;
  const surfaceMode = (name, label, custom = false) => {
    let current = disabled.has(name) ? "off" : "random";
    if (custom && gpuCustomSupported && (fp.gpu_vendor || fp.gpu_renderer)) current = "custom";
    const options = [{ value: "random", label: "随机" }];
    if (custom) options.push({ value: "custom", label: "自定义", disabled: !gpuCustomSupported });
    options.push({ value: "off", label: "关闭" });
    return `<div class="fp-config-row"><div><b>${esc(label)}</b><span>每个账号使用稳定且不同的取值</span></div>${modeButtons(name, options, current)}</div>`;
  };
  const platformMode = fp.platform ? "custom" : "auto";
  const brandMode = fp.brand ? "custom" : "auto";
  const cpuMode = Number(fp.hardware_concurrency) > 0 ? "custom" : "auto";
  return new Promise(resolve => {
    _uiResolve = resolve; _uiCancelVal = null;
    $("ui-body").innerHTML = `
      <div class="hint" style="margin:0 0 14px">${preLogin
        ? "自动项会在启动前根据所选代理或本机出口 IP 生成；切换为自定义后可在第一次登录前逐项覆盖。登录成功后，此配置会随账号和独立 Profile 一起保存。"
        : "自动项会跟随来源 IP 生成；切换为自定义后可逐项编辑。保存后关闭该账号当前浏览器，下次启动应用新配置。"}</div>
      <div class="fp-edit-tabs" role="tablist" aria-label="浏览器指纹设置">
        <button type="button" class="ghost sm active" role="tab" id="fp-tab-basic" aria-controls="fp-panel-basic" tabindex="0" aria-selected="true" data-fp-tab="basic">基础设置</button>
        <button type="button" class="ghost sm" role="tab" id="fp-tab-advanced" aria-controls="fp-panel-advanced" tabindex="-1" aria-selected="false" data-fp-tab="advanced">高级设置</button>
      </div>
      <div class="fp-edit-panel active" id="fp-panel-basic" role="tabpanel" aria-labelledby="fp-tab-basic" data-fp-panel="basic">
        <div class="fp-runtime-summary">
          <div><span>浏览器内核</span><b>${esc(runtimeVersion || "跟随所选运行时")}</b></div>
          <div><span>设备类型</span><b>桌面设备</b></div>
          <div><span>指纹编号</span><b>${esc(fp.fingerprint_id || "-")}</b></div>
        </div>
        <div class="fp-settings">
          <div class="fp-config-row"><div><b>操作系统</b><span>影响 navigator.platform 与 Client Hints</span></div>${modeButtons("platform", [{value:"auto",label:"自动"},{value:"custom",label:"自定义"}], platformMode)}</div>
          <div class="form-grid fp-mode-fields" data-fp-custom="platform">
            <div class="form-field"><label for="fp-edit-platform">系统类型</label><select id="fp-edit-platform"><option value="windows"${fp.platform === "windows" ? " selected" : ""}>Windows</option><option value="macos"${fp.platform === "macos" ? " selected" : ""}>macOS</option><option value="linux"${fp.platform === "linux" ? " selected" : ""}>Linux</option></select></div>
            <div class="form-field"><label for="fp-edit-platform-version">系统版本</label><input id="fp-edit-platform-version" value="${esc(fp.platform_version || "")}" placeholder="例如 10.0.19045"></div>
          </div>
          <div class="fp-config-row"><div><b>浏览器品牌与版本</b><span>影响 User-Agent 与 UA Data</span></div>${modeButtons("brand", [{value:"auto",label:"自动"},{value:"custom",label:"自定义"}], brandMode)}</div>
          <div class="form-grid fp-mode-fields" data-fp-custom="brand">
            <div class="form-field"><label for="fp-edit-brand">浏览器品牌</label><input id="fp-edit-brand" value="${esc(fp.brand || "")}" placeholder="Chrome / Edge"></div>
            <div class="form-field"><label for="fp-edit-brand-version">浏览器版本</label><input id="fp-edit-brand-version" value="${esc(fp.brand_version || "")}" placeholder="例如 148.0.0.0"></div>
          </div>
          ${fp.actual_ua ? `<div class="fp-readonly"><span>User Agent</span><code>${esc(fp.actual_ua)}</code></div>` : ""}
          <div class="fp-config-row"><div><b>语言</b><span>可跟随来源 IP 自动匹配</span></div>${modeButtons("language", [{value:"auto",label:"跟随 IP"},{value:"custom",label:"自定义"}], fp.language_mode || "auto")}</div>
          <div class="form-grid fp-mode-fields" data-fp-custom="language">
            <div class="form-field"><label for="fp-edit-locale">浏览器语言</label><input id="fp-edit-locale" value="${esc(fp.locale || "zh-CN")}"></div>
            <div class="form-field"><label for="fp-edit-accept">Accept-Language</label><input id="fp-edit-accept" value="${esc(fp.accept_languages || "")}" placeholder="例如 zh-CN,zh"></div>
          </div>
          <div class="fp-config-row"><div><b>时区</b><span>可跟随来源 IP 匹配 IANA 时区</span></div>${modeButtons("timezone", [{value:"auto",label:"跟随 IP"},{value:"custom",label:"自定义"}], fp.timezone_mode || "auto")}</div>
          <div class="fp-mode-fields" data-fp-custom="timezone"><div class="form-field"><label for="fp-edit-timezone">IANA 时区</label><input id="fp-edit-timezone" value="${esc(fp.timezone || "Asia/Shanghai")}"></div></div>
          <div class="fp-config-row"><div><b>WebRTC</b><span>隐藏模式阻止非代理 UDP 暴露本机 IP</span></div>${modeButtons("webrtc", [{value:"conceal",label:"隐藏"},{value:"allow",label:"允许"}], fp.webrtc_mode || "conceal")}</div>
          <div class="fp-config-row"><div><b>地理位置权限</b><span>控制网站读取浏览器定位的权限</span></div>${modeButtons("geo-permission", [{value:"ask",label:"询问"},{value:"allow",label:"允许"},{value:"deny",label:"禁止"}], fp.geolocation_permission || "allow")}</div>
          <div class="fp-config-row"><div><b>地理位置数据</b><span>可跟随来源 IP 自动生成坐标</span></div>${modeButtons("location", [{value:"auto",label:"跟随 IP"},{value:"custom",label:"自定义"}], fp.location_mode || "auto")}</div>
          <div class="form-grid fp-mode-fields" data-fp-custom="location">
            <div class="form-field"><label for="fp-edit-ip">来源 IP</label><input id="fp-edit-ip" value="${esc(fp.source_ip || "")}"></div>
            <div class="form-field"><label for="fp-edit-country">国家/地区</label><input id="fp-edit-country" value="${esc(fp.country || "")}"></div>
            <div class="form-field"><label for="fp-edit-region">区域</label><input id="fp-edit-region" value="${esc(fp.region || "")}"></div>
            <div class="form-field"><label for="fp-edit-city">城市</label><input id="fp-edit-city" value="${esc(fp.city || "")}"></div>
            <div class="form-field"><label for="fp-edit-lat">纬度</label><input id="fp-edit-lat" type="number" min="-90" max="90" step="0.000001" value="${Number(fp.geo_lat) || 0}"></div>
            <div class="form-field"><label for="fp-edit-lon">经度</label><input id="fp-edit-lon" type="number" min="-180" max="180" step="0.000001" value="${Number(fp.geo_lon) || 0}"></div>
          </div>
          <div class="fp-config-row"><div><b>窗口尺寸</b><span>设置浏览器窗口打开时的大小</span></div>${modeButtons("viewport", [{value:"auto",label:"自动"},{value:"custom",label:"自定义"}], fp.viewport_mode || "auto")}</div>
          <div class="form-grid fp-mode-fields" data-fp-custom="viewport">
            <div class="form-field"><label for="fp-edit-vw">宽度</label><input id="fp-edit-vw" type="number" min="320" max="7680" value="${Number(fp.viewport_w) || 1280}"></div>
            <div class="form-field"><label for="fp-edit-vh">高度</label><input id="fp-edit-vh" type="number" min="240" max="4320" value="${Number(fp.viewport_h) || 800}"></div>
          </div>
        </div>
      </div>
      <div class="fp-edit-panel" id="fp-panel-advanced" role="tabpanel" aria-labelledby="fp-tab-advanced" data-fp-panel="advanced">
        <div class="fp-settings">
          <div class="fp-config-row"><div><b>指纹种子</b><span>Canvas、Audio 等随机值由种子稳定派生</span></div><div class="fp-seed-badge">${preLogin ? "内核启动时生成" : `uint32 ${esc(String(fp.engine_seed ?? ""))}`}</div></div>
          <div class="form-field"><label for="fp-edit-seed">种子值</label><input id="fp-edit-seed" value="${esc(fp.seed || "")}" maxlength="128"></div>
          ${surfaceMode("font", "字体")}
          ${surfaceMode("canvas", "Canvas")}
          ${surfaceMode("gpu", "WebGL / GPU", true)}
          <div class="form-grid fp-mode-fields" data-fp-custom="gpu">
            <div class="form-field"><label for="fp-edit-gpu-vendor">WebGL Vendor</label><input id="fp-edit-gpu-vendor" value="${esc(fp.gpu_vendor || "")}" placeholder="例如 Google Inc. (Intel)"></div>
            <div class="form-field"><label for="fp-edit-gpu-renderer">WebGL Renderer</label><input id="fp-edit-gpu-renderer" value="${esc(fp.gpu_renderer || "")}" placeholder="仅 139–143 内核支持"></div>
          </div>
          ${surfaceMode("audio", "AudioContext")}
          ${surfaceMode("clientrects", "ClientRects")}
          <div class="fp-config-row"><div><b>硬件并发数</b><span>navigator.hardwareConcurrency</span></div>${modeButtons("cpu", [{value:"auto",label:"自动"},{value:"custom",label:"自定义"}], cpuMode)}</div>
          <div class="fp-mode-fields" data-fp-custom="cpu"><div class="form-field"><label for="fp-edit-cpu">CPU 逻辑核心</label><input id="fp-edit-cpu" type="number" min="1" max="256" value="${Number(fp.hardware_concurrency) || 8}"></div></div>
          <div class="form-field"><label for="fp-edit-extra">附加启动参数</label><textarea id="fp-edit-extra" rows="3" placeholder="每行一个安全参数，例如 --mute-audio">${esc(fp.extra_args || "")}</textarea><div class="mut" style="font-size:11px">仅接受安全的 Chromium 参数；代理、调试端口、用户目录和指纹核心参数由系统统一管理。</div></div>
        </div>
      </div>
      `;
    enhanceAllSelects($("ui-body"));
    const body = $("ui-body");
    const selectedMode = name => {
      const active = body.querySelector(`[data-fp-mode="${name}"] button.active`);
      return active ? active.dataset.value : "";
    };
    const syncCustomFields = name => {
      const enabled = selectedMode(name) === "custom";
      body.querySelectorAll(`[data-fp-custom="${name}"] input,[data-fp-custom="${name}"] select,[data-fp-custom="${name}"] textarea`).forEach(control => {
        control.disabled = !enabled;
        if (control._csSync) control._csSync();
      });
      body.querySelectorAll(`[data-fp-custom="${name}"]`).forEach(group => group.classList.toggle("enabled", enabled));
    };
    body.querySelectorAll("[data-fp-mode] button").forEach(button => {
      button.addEventListener("click", () => {
        if (button.disabled) return;
        const segment = button.closest("[data-fp-mode]");
        segment.querySelectorAll("button").forEach(item => {
          const active = item === button;
          item.classList.toggle("active", active);
          item.setAttribute("aria-pressed", active ? "true" : "false");
        });
        syncCustomFields(segment.dataset.fpMode);
        globalThis.CreatorHubMotion?.reveal?.(body.querySelector(`[data-fp-custom="${segment.dataset.fpMode}"].enabled`));
      });
    });
    ["platform", "brand", "language", "timezone", "location", "viewport", "gpu", "cpu"].forEach(syncCustomFields);
    body.querySelectorAll("[data-fp-mode]").forEach(segment => {
      const label = segment.closest(".fp-config-row")?.querySelector("b");
      if (label) { label.id = "fp-label-" + segment.dataset.fpMode; segment.setAttribute("aria-labelledby", label.id); }
    });
    body.querySelectorAll("[data-fp-tab]").forEach(tab => {
      tab.addEventListener("keydown", event => {
        if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
        event.preventDefault();
        const tabs = [...body.querySelectorAll("[data-fp-tab]")], index = tabs.indexOf(tab);
        const next = event.key === "Home" ? tabs[0] : event.key === "End" ? tabs[tabs.length - 1] : tabs[(index + (event.key === "ArrowRight" ? 1 : tabs.length - 1)) % tabs.length];
        next.click(); next.focus({ preventScroll: true });
      });
      tab.addEventListener("click", () => {
        const selected = tab.dataset.fpTab;
        body.querySelectorAll("[data-fp-tab]").forEach(item => {
          const active = item.dataset.fpTab === selected;
          item.classList.toggle("active", active);
          item.setAttribute("aria-selected", active ? "true" : "false");
          item.tabIndex = active ? 0 : -1;
        });
        body.querySelectorAll("[data-fp-panel]").forEach(panel => panel.classList.toggle("active", panel.dataset.fpPanel === selected));
        body.scrollTo({ top: 0, behavior: "instant" });
        globalThis.CreatorHubMotion?.reveal?.(body.querySelector(`[data-fp-panel="${selected}"]`), "x", selected === "basic" ? -1 : 1);
      });
    });
    const value = id => ($(id) || {}).value || "";
    const number = (id, fallback = 0) => { const parsed = Number(value(id)); return Number.isFinite(parsed) ? parsed : fallback; };
    _uiGetVal = () => {
      const platformCustom = selectedMode("platform") === "custom";
      const brandCustom = selectedMode("brand") === "custom";
      const gpuCustom = selectedMode("gpu") === "custom";
      const surfaces = ["font", "audio", "canvas", "clientrects", "gpu"];
      return { action: "save", data: {
        seed: value("fp-edit-seed"), source_ip: value("fp-edit-ip"),
        country: value("fp-edit-country"), region: value("fp-edit-region"), city: value("fp-edit-city"),
        timezone: value("fp-edit-timezone"), locale: value("fp-edit-locale"), accept_languages: value("fp-edit-accept"),
        viewport_w: number("fp-edit-vw", 1280), viewport_h: number("fp-edit-vh", 800),
        geo_lat: number("fp-edit-lat"), geo_lon: number("fp-edit-lon"),
        platform: platformCustom ? value("fp-edit-platform") : "",
        platform_version: platformCustom ? value("fp-edit-platform-version") : "",
        brand: brandCustom ? value("fp-edit-brand") : "",
        brand_version: brandCustom ? value("fp-edit-brand-version") : "",
        hardware_concurrency: selectedMode("cpu") === "custom" ? number("fp-edit-cpu", 8) : 0,
        gpu_vendor: gpuCustom ? value("fp-edit-gpu-vendor") : "",
        gpu_renderer: gpuCustom ? value("fp-edit-gpu-renderer") : "",
        disable_spoofing: surfaces.filter(name => selectedMode(name) === "off"),
        language_mode: selectedMode("language") || "auto",
        timezone_mode: selectedMode("timezone") || "auto",
        viewport_mode: selectedMode("viewport") || "auto",
        location_mode: selectedMode("location") || "auto",
        geolocation_permission: selectedMode("geo-permission") || "allow",
        webrtc_mode: selectedMode("webrtc") || "conceal",
        extra_args: value("fp-edit-extra"),
      }};
    };
    _uiOpen(
      `${account.nickname} · ${preLogin ? "登录前指纹配置" : "浏览器指纹"}`,
      preLogin ? "确认后使用这套指纹创建独立登录环境" : `指纹 ${fp.fingerprint_id || "-"}`,
      { okText: preLogin ? "使用此指纹登录" : "保存配置", wide: true, icon: "i-fingerprint", submit: options.submit || null },
    );
    const autoButton = document.createElement("button");
    autoButton.id = "ui-extra-action";
    autoButton.type = "button";
    autoButton.className = "ghost";
    autoButton.textContent = preLogin ? "使用出口 IP 自动配置" : "按 IP 自动生成";
    autoButton.addEventListener("click", () => _uiClose({ action: "auto" }));
    $("ui-actions").insertBefore(autoButton, $("ui-actions").firstElementChild);
  });
}

async function manageFingerprint(id) {
  const account = findAnyAccount(id);
  if (!account) return;
  let current;
  try { current = await api(`/api/accounts/${id}/fingerprint`); }
  catch (e) { toast("读取指纹失败:" + e.message, "err"); return; }
  let savedFingerprint;
  const action = await uiFingerprintEditor(account, current, {
    submit: async action => {
      savedFingerprint = await api(`/api/accounts/${id}/fingerprint`, {
        method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(action.data),
      });
    },
  });
  if (!action) return;
  if (action.action === "auto") {
    const confirmed = await uiConfirm({
      title: "恢复自动指纹",
      message: "将根据账号当前代理或本机出口 IP 重新计算地域、时区、语言、窗口与种子派生项，并清除手动覆盖。",
      okText: "恢复自动",
    });
    if (!confirmed) return;
    try {
      const result = await api(`/api/accounts/${id}/fingerprint/from-ip`, { method: "POST" });
      toast(`已恢复自动指纹：${result.fingerprint.fingerprint_id || "-"}`, "ok", 7000);
      await refreshAccounts();
    } catch (e) { toast("自动生成失败:" + e.message, "err", 8000); }
    return;
  }
  toast(`指纹已保存：${savedFingerprint?.fingerprint?.fingerprint_id || "-"}`, "ok", 7000);
  await refreshAccounts();
}
