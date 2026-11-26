// 16-autocomment-queue-boot.js — 自动评论规则、试跑预览、审核重写、统一任务队列、轮询主循环及 CreatorHubBridge。
// ─── 自动评论 ───
let AC_RULES = [];
let AC_TASKS = [];
let AC_POLICY_META = null;
const AC_SELECTED_TASKS = new Set();

const AC_MODE_T = { auto_reply: "自动回复", auto_comment: "自动评论" };
const AC_KIND_T = { self: "自己近期作品", work: "指定作品", creator: "指定博主", keyword: "关键词" };
const AC_COMPOSE_T = {
  template_only: "本地模板",
  ai_with_fallback: "AI 优先",
  ai_only_draft: "纯 AI (草稿兜底)",
  ai_only_skip: "纯 AI (失败跳过)",
};
const AC_SCOPE_T = { all: "全部评论", top_only: "仅一级评论", reply_only: "仅二级回复" };
const AC_SRC_META = {
  ai: ["AI 生成", "ai"],
  template: ["本地模板", ""],
  ai_fallback: ["AI 失败回退模板", "fallback"],
  ai_empty_draft: ["AI 失败待填", "empty"],
  manual: ["人工编辑", ""],
};
const AC_TASK_ST = { draft: "草稿待审", pending: "排队中", doing: "发送中", uncertain: "结果待确认", done: "已发送", failed: "失败", canceled: "已取消" };
const AC_TASK_PILL = { draft: "downloading", pending: "pending", doing: "downloading", uncertain: "downloading", done: "done", failed: "failed", canceled: "invalid" };
const AC_KEYWORD_PLATFORMS = ["xhs", "bilibili", "instagram", "twitter", "facebook", "youtube"];

function acKindOptionsFor(platform, mode) {
  if (mode === "auto_comment") {
    let html = '<option value="creator">指定博主</option>';
    if (AC_KEYWORD_PLATFORMS.includes(platform)) html += '<option value="keyword">搜索关键词</option>';
    return html;
  }
  return '<option value="self">自己近期作品</option><option value="work">指定作品</option>';
}
function acTargetMeta(platform, mode, kind) {
  const xhs = platform === "xhs", bili = platform === "bilibili", ig = platform === "instagram", tw = platform === "twitter", fb = platform === "facebook", yt = platform === "youtube";
  if (mode === "auto_reply") {
    if (kind === "self") return { show: false, label: "目标", ph: "" };
    return {
      show: true,
      label: xhs ? "笔记链接 / id" : tw ? "推文链接 / status_id" : fb ? "帖子 / Reel / Watch 链接" : yt ? "YouTube 视频 / Shorts 链接" : "作品链接 / id",
      ph: xhs ? "explore 链接 / xhslink / note_id" : bili ? "视频链接 / BV或av号 / opus链接" : ig ? "instagram.com/p/... 或 /reel/... 链接" : tw ? "x.com/.../status/... 链接或数字推文 ID" : fb ? "facebook.com 帖子/Reel/Watch 链接或 fb.watch 短链" : yt ? "youtube.com/watch?v=...、youtu.be/... 或 /shorts/..." : "作品链接 / 短链 / 数字 id",
    };
  }
  if (kind === "keyword") {
    return {
      show: true,
      label: "搜索关键词",
      ph: tw ? "例如:AI agent / #buildinpublic" : fb ? "例如:跨境电商 / marketing tips" : yt ? "例如:AI tutorial / tech review" : ig ? "例如:#travel / skincare" : "例如:露营装备 / 口红试色",
    };
  }
  return {
    show: true,
    label: xhs ? "博主主页 / id" : bili ? "UP 主空间主页 / mid" : (ig || tw) ? "创作者主页 / @username" : fb ? "主页 / 公共主页 / 群组链接" : yt ? "YouTube 频道 / @handle" : "博主主页 / sec_uid",
    ph: xhs ? "主页链接 / xhslink / user_id" : bili ? "space.bilibili.com 链接 / mid" : ig ? "instagram.com/<username> 或 @username" : tw ? "x.com/<username> 或 @username" : fb ? "facebook.com/<page>、/groups/<id> 或 profile.php?id=..." : yt ? "youtube.com/@handle 或 /channel/UC..." : "主页链接 / 短链 / sec_uid",
  };
}
function acParseKwInput(text) {
  return String(text || "").split(/[,，、;；\r\n]+/).map(s => s.trim()).filter(Boolean);
}
function acNeedsTemplates(composeMode) {
  return composeMode === "template_only" || composeMode === "ai_with_fallback";
}
function acUsesAi(composeMode) {
  return composeMode !== "template_only";
}
function acPresetOptionsHtml(selectedId = "") {
  const presets = AC_POLICY_META?.ai_presets || [];
  const defId = AC_POLICY_META?.ai_default_preset_id || "natural_cn";
  const defName = (presets.find(p => p.id === defId) || {}).name || "默认人设";
  const opts = [`<option value="">跟随全局默认（${esc(defName)}）</option>`];
  presets.forEach(p => {
    opts.push(`<option value="${esc(p.id)}"${p.id === selectedId ? " selected" : ""}>${esc(p.name)}${p.builtin ? "" : "（自定义）"}</option>`);
  });
  if (selectedId && !presets.some(p => p.id === selectedId)) {
    opts.push(`<option value="${esc(selectedId)}" selected>${esc(selectedId)}</option>`);
  }
  return opts.join("");
}
function acChannelOptionsHtml(selectedId = "") {
  const channels = AC_POLICY_META?.ai_channels?.channels || [];
  const opts = [`<option value="">跟随「设置 → AI 文案」默认路由</option>`];
  channels.forEach(c => {
    opts.push(`<option value="${esc(c.id)}"${c.id === selectedId ? " selected" : ""}>${esc(c.display_name)} · ${esc(c.model || "未选模型")}</option>`);
  });
  if (selectedId && !channels.some(c => c.id === selectedId)) {
    opts.push(`<option value="${esc(selectedId)}" selected>渠道 ${esc(selectedId)}</option>`);
  }
  return opts.join("");
}
function populateAcAiSelectors() {
  if ($("ac-ai-preset")) {
    const prev = $("ac-ai-preset").value;
    $("ac-ai-preset").innerHTML = acPresetOptionsHtml(prev);
  }
  if ($("ac-ai-channel")) {
    const prev = $("ac-ai-channel").value;
    $("ac-ai-channel").innerHTML = acChannelOptionsHtml(prev);
  }
  csSyncAll();
}
function renderAcPolicyBars() {
  const p = AC_POLICY_META?.policy;
  if (!p) return;
  const aiOk = p.ai_enabled && p.ai_route?.ready;
  const aiTxt = !p.ai_enabled
    ? "AI 文案总开关未开启"
    : p.ai_route?.ready
      ? `AI 已就绪（${p.ai_route.label}）`
      : `AI 未就绪（${p.ai_route?.reason || "未配置模型"}）`;
  const capParts = [];
  if (p.account_daily_cap > 0) capParts.push(`账号每日上限 ${p.account_daily_cap} 条`);
  if (p.account_hourly_cap > 0) capParts.push(`每小时 ${p.account_hourly_cap} 条`);
  if (p.risk_comment_min_gap_seconds > 0 || p.legacy_comment_min_gap_seconds > 0) {
    capParts.push(`全局最小间隔 ${Math.max(p.risk_comment_min_gap_seconds, p.legacy_comment_min_gap_seconds)} 秒`);
  }
  const limitTxt = capParts.length ? capParts.join(" · ") : "风控评论限额未开启（仅按规则自身参数）";
  const reviewTxt = p.xhs_manual_only
    ? "小红书当前为「仅保存草稿」模式，不会自动发送"
    : p.xhs_review_forced
      ? "小红书已开启「全局先审后发」，所有规则生成后均先入草稿"
      : "审核状态由各规则单独控制";

  const rulesBar = $("ac-rules-policy-bar");
  if (rulesBar) {
    rulesBar.dataset.state = (p.ai_enabled && !p.ai_route?.ready) ? "warn" : "ok";
    if ($("ac-rules-policy-title")) $("ac-rules-policy-title").textContent = `${PF_NAME[PLATFORM] || PLATFORM} 生效策略 · ${aiTxt}`;
    if ($("ac-rules-policy-desc")) $("ac-rules-policy-desc").textContent = `${limitTxt} · ${reviewTxt}`;
  }
  syncAcCreatePolicySummary();
}
function syncAcCreatePolicySummary() {
  const p = AC_POLICY_META?.policy;
  const banner = $("ac-create-policy-banner");
  if (!banner || !p) return;
  const cm = $("ac-compose-mode")?.value || "template_only";
  const ruleGap = Math.max(1, Number($("ac-gap")?.value) || 90);
  const ruleCap = Math.max(0, Number($("ac-cap")?.value) || 0);
  const effGap = Math.max(ruleGap, p.risk_comment_min_gap_seconds || 0, p.legacy_comment_min_gap_seconds || 0);
  const reviewOn = Boolean($("ac-review")?.checked) || p.xhs_review_forced || p.xhs_manual_only;
  const warnReasons = [];
  if (acUsesAi(cm) && !p.ai_enabled) warnReasons.push("当前选了 AI 生成，但「设置 → AI 文案」总开关尚未开启");
  else if (acUsesAi(cm) && !p.ai_route?.ready) warnReasons.push(`AI 模型未就绪（${p.ai_route?.reason || "请先配置"}）`);

  const accId = Number($("ac-acc")?.value || 0);
  const acc = ACCOUNTS.find(a => a.id === accId);
  if ($("ac-mode")?.value === "auto_comment" && acc && String(acc.account_role || "hybrid").toLowerCase() === "publish") {
    warnReasons.push("所选账号为「发布主号 (publish)」，不允许创建主动评论规则");
  }
  if ($("ac-review-help")) {
    $("ac-review-help").textContent = p.xhs_manual_only
      ? "当前小红书评论通道为「仅保存草稿」，无论是否勾选均只存草稿。"
      : p.xhs_review_forced
        ? "当前「设置 → 采集与运行」开启了小红书全局先审后发，本规则会自动先存草稿。"
        : "推荐开启：生成后进入「任务与审核」核对原文与拟发文案，通过后再按节流发出。";
  }
  banner.dataset.state = warnReasons.length ? "warn" : "ok";
  if ($("ac-create-policy-title")) {
    $("ac-create-policy-title").textContent = warnReasons.length
      ? `配置提醒：${warnReasons[0]}`
      : `生效预览：最小间隔 ${effGap} 秒 · ${ruleCap === 0 ? "规则日限不单独限制" : `规则日限 ${ruleCap} 条`}${p.account_daily_cap ? `（账号总上限 ${p.account_daily_cap} 条）` : ""} · ${reviewOn ? "先审后发" : "生成即排队"}`;
  }
  if ($("ac-create-policy-desc")) {
    $("ac-create-policy-desc").textContent = warnReasons.length > 1
      ? warnReasons.join("；")
      : `AI 路由：${p.ai_enabled ? (p.ai_route?.label || "未配置") : "未启用"} · 扫描范围默认最近 ${p.global_recent_works} 条作品 / ${p.global_recent_days} 天。`;
  }
}
async function refreshAcPolicyMeta() {
  try {
    const data = await api("/api/comment-rules/effective-policy?platform=" + encodeURIComponent(PLATFORM));
    AC_POLICY_META = data;
    populateAcAiSelectors();
    renderAcPolicyBars();
  } catch (e) {}
}
function onAcComposeModeChange() {
  const cm = $("ac-compose-mode")?.value || "template_only";
  if ($("ac-ai-opts")) $("ac-ai-opts").hidden = !acUsesAi(cm);
  if ($("ac-tpl-label")) {
    $("ac-tpl-label").textContent = acNeedsTemplates(cm)
      ? (cm === "ai_with_fallback" ? "本地回退模板（必填，AI 失败时自动回退）" : "本地文案模板（每行一条，必填）")
      : "本地文案模板（当前纯 AI 模式可留空）";
  }
  syncAcCreatePolicySummary();
  csSyncAll();
}
function previewAcTemplates(textareaId) {
  const raw = ($(textareaId)?.value || "").split("\n").map(s => s.trim()).filter(Boolean);
  if (!raw.length) { toast("请先输入至少一条模板再预览", "info"); return; }
  const picked = raw[Math.floor(Math.random() * raw.length)];
  const expanded = picked.replace(/\{([^{}|]+(?:\|[^{}|]+)+)\}/g, (_, g) => {
    const parts = g.split("|"); return parts[Math.floor(Math.random() * parts.length)];
  }).replaceAll("{nick}", "山风旅人").replaceAll("{kw}", "露营").replaceAll("{author}", "户外达人").replaceAll("{work_title}", "轻量化天幕实测");
  toast("随机渲染示例：" + expanded, "info", 5500);
}
function onAcMode() {
  const k = $("ac-kind"); if (!k) return;
  const prev = k.value;
  k.innerHTML = acKindOptionsFor(PLATFORM, $("ac-mode").value);
  if ([...k.options].some(o => o.value === prev)) k.value = prev;
  onAcKind();
}
function onAcKind() {
  const mode = $("ac-mode").value, kind = $("ac-kind").value;
  const meta = acTargetMeta(PLATFORM, mode, kind);
  $("ac-target-wrap").style.display = meta.show ? "" : "none";
  $("ac-target-label").textContent = meta.label;
  $("ac-target").placeholder = meta.ph;
  if ($("ac-scope-wrap")) $("ac-scope-wrap").style.display = mode === "auto_reply" ? "" : "none";
  onAcComposeModeChange();
  syncAcCreatePolicySummary();
  csSyncAll();
}
function populateAcAccount() {
  const sel = $("ac-acc"); if (!sel) return;
  sel.innerHTML = accOptions(ACCOUNTS, `请选择${PF_NAME[PLATFORM] || "当前平台"}账号(必选)`);
  if (ACCOUNTS.length) sel.value = String(ACCOUNTS[0].id);
  syncAcCreatePolicySummary();
  csSyncAll();
}
async function addCommentRule() {
  const acc = $("ac-acc").value;
  if (!acc) { toast("请选择账号", "err"); return; }
  const compose_mode = $("ac-compose-mode")?.value || "template_only";
  const templates = $("ac-templates").value.split("\n").map(s => s.trim()).filter(Boolean);
  if (acNeedsTemplates(compose_mode) && !templates.length) {
    toast("当前文案模式需至少写一条模板（如只需 AI 可切换为「仅用 AI」）", "err");
    return;
  }
  const inc = acParseKwInput($("ac-include-kw")?.value || "");
  const exc = acParseKwInput($("ac-skip")?.value || "");
  const body = {
    platform: PLATFORM,
    name: ($("ac-name")?.value || "").trim(),
    mode: $("ac-mode").value,
    account_id: +acc,
    target_kind: $("ac-kind").value,
    target: $("ac-target").value.trim(),
    templates,
    compose_mode,
    use_ai: acUsesAi(compose_mode),
    ai_channel_id: $("ac-ai-channel")?.value || "",
    ai_model: ($("ac-ai-model")?.value || "").trim(),
    ai_preset_id: $("ac-ai-preset")?.value || "",
    ai_prompt: ($("ac-ai-prompt")?.value || "").trim(),
    ai_max_chars: Math.max(0, Number($("ac-ai-max-chars")?.value) || 0),
    require_review: $("ac-review").checked,
    match_mode: $("ac-match-mode")?.value || "any",
    include_keywords: inc,
    exclude_keywords: exc,
    reply_filter: inc.join(", "),
    skip_keywords: exc.join(", "),
    comment_scope: $("ac-comment-scope")?.value || "all",
    min_source_length: Math.max(0, Number($("ac-min-len")?.value) || 0),
    max_source_age_hours: Math.max(0, Number($("ac-max-age")?.value) || 0),
    recent_works: Math.max(0, Number($("ac-recent-works")?.value) || 0),
    recent_days: Math.max(0, Number($("ac-recent-days")?.value) || 0),
    daily_cap: Math.max(0, Number($("ac-cap").value) || 0),
    min_gap_seconds: Math.max(1, Number($("ac-gap").value) || 90),
    max_per_run: Math.max(1, Number($("ac-max").value) || 5),
    interval_seconds: Math.max(60, Number($("ac-interval").value) || 1800),
    enabled: false,
  };
  await withBusy(evtBtn(), "创建中", async () => {
    try {
      await api("/api/comment-rules", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      if ($("ac-name")) $("ac-name").value = "";
      $("ac-templates").value = ""; $("ac-target").value = "";
      if ($("ac-ai-prompt")) $("ac-ai-prompt").value = "";
      if ($("ac-include-kw")) $("ac-include-kw").value = "";
      if ($("ac-skip")) $("ac-skip").value = "";
      $("ac-msg").textContent = "规则已创建（默认关闭），点「试跑预览」核对命中与文案后再启用 ✓";
      toast("规则已创建", "ok"); refreshCommentRules();
      globalThis.CreatorHubWorkbench?.completed?.("autocomment");
    } catch (e) { $("ac-msg").textContent = "失败: " + e.message; toast("创建失败:" + e.message, "err"); }
  });
}

// ─── 编辑规则:独立弹窗（与新建完全同源，支持全平台与全部新字段）───
let EM_PF = "douyin";
function emOnMode() {
  const k = $("em-kind"); if (!k) return;
  const prev = k.value;
  k.innerHTML = acKindOptionsFor(EM_PF, $("em-mode").value);
  if ([...k.options].some(o => o.value === prev)) k.value = prev;
  emOnKind();
}
function emOnKind() {
  const mode = $("em-mode").value, kind = $("em-kind").value;
  const meta = acTargetMeta(EM_PF, mode, kind);
  $("em-target-wrap").style.display = meta.show ? "" : "none";
  $("em-target-label").textContent = meta.label;
  $("em-target").placeholder = meta.ph;
  if ($("em-scope-wrap")) $("em-scope-wrap").style.display = mode === "auto_reply" ? "" : "none";
  csSyncAll();
}
function emOnComposeMode() {
  const cm = $("em-compose-mode")?.value || "template_only";
  if ($("em-ai-opts")) $("em-ai-opts").hidden = !acUsesAi(cm);
  if ($("em-tpl-label")) {
    $("em-tpl-label").textContent = acNeedsTemplates(cm)
      ? (cm === "ai_with_fallback" ? "本地回退模板（必填）" : "本地文案模板（必填）")
      : "本地文案模板（纯 AI 模式可留空）";
  }
  csSyncAll();
}
function editRule(id) {
  const r = AC_RULES.find(x => x.id === id); if (!r) return;
  EM_PF = r.platform;
  const accOpts = accOptions(ACCOUNTS.filter(a => a.platform === EM_PF), `请选择${PF_NAME[EM_PF] || "对应平台"}账号`);
  new Promise(res => {
    _uiResolve = res; _uiCancelVal = null;
    _uiGetVal = () => {
      const compose_mode = $("em-compose-mode").value || "template_only";
      const inc = acParseKwInput($("em-include-kw").value);
      const exc = acParseKwInput($("em-skip").value);
      return {
        name: $("em-name").value.trim(),
        mode: $("em-mode").value,
        target_kind: $("em-kind").value,
        target: $("em-target").value.trim(),
        account_id: +$("em-acc").value || null,
        templates: $("em-templates").value.split("\n").map(s => s.trim()).filter(Boolean),
        compose_mode,
        use_ai: acUsesAi(compose_mode),
        ai_channel_id: $("em-ai-channel").value || "",
        ai_model: $("em-ai-model").value.trim(),
        ai_preset_id: $("em-ai-preset").value || "",
        ai_prompt: $("em-ai-prompt").value.trim(),
        ai_max_chars: Math.max(0, Number($("em-ai-max-chars").value) || 0),
        require_review: $("em-review").checked,
        match_mode: $("em-match-mode").value || "any",
        include_keywords: inc,
        exclude_keywords: exc,
        reply_filter: inc.join(", "),
        skip_keywords: exc.join(", "),
        comment_scope: $("em-comment-scope").value || "all",
        min_source_length: Math.max(0, Number($("em-min-len").value) || 0),
        max_source_age_hours: Math.max(0, Number($("em-max-age").value) || 0),
        recent_works: Math.max(0, Number($("em-recent-works").value) || 0),
        recent_days: Math.max(0, Number($("em-recent-days").value) || 0),
        daily_cap: Math.max(0, Number($("em-cap").value) || 0),
        min_gap_seconds: Math.max(1, Number($("em-gap").value) || 90),
        max_per_run: Math.max(1, Number($("em-max").value) || 5),
        interval_seconds: Math.max(60, Number($("em-interval").value) || 1800),
      };
    };
    $("ui-body").innerHTML = `
      <fieldset class="ui-form-group"><legend>规则与对象</legend>
        <div><label class="field" for="em-name">规则名称</label><input id="em-name" maxlength="100" placeholder="便于识别这条规则"></div>
        <div class="form-grid">
          <div><label class="field" for="em-mode">互动方式</label><select id="em-mode" onchange="emOnMode()"><option value="auto_reply">回复自己作品的评论</option><option value="auto_comment">评论其他人的作品</option></select></div>
          <div><label class="field" for="em-kind">目标范围</label><select id="em-kind" onchange="emOnKind()"></select></div>
        </div>
        <div><label class="field" for="em-acc">使用账号</label><select id="em-acc">${accOpts}</select></div>
        <div id="em-target-wrap"><label class="field" id="em-target-label" for="em-target">目标</label><input id="em-target"></div>
      </fieldset>
      <fieldset class="ui-form-group"><legend>文案与 AI</legend>
        <div><label class="field" for="em-compose-mode">生成方式</label><select id="em-compose-mode" onchange="emOnComposeMode()">
          <option value="template_only">仅使用本地模板库</option>
          <option value="ai_with_fallback">AI 生成优先，失败回退模板</option>
          <option value="ai_only_draft">仅用 AI，失败存空草稿待人工处理</option>
          <option value="ai_only_skip">仅用 AI，失败跳过</option>
        </select></div>
        <div id="em-ai-opts" class="stack" style="gap:10px" hidden>
          <div class="form-grid">
            <div><label class="field" for="em-ai-preset">文案人设预设</label><select id="em-ai-preset">${acPresetOptionsHtml(r.ai_preset_id || "")}</select></div>
            <div><label class="field" for="em-ai-max-chars">回复字数上限（0=跟随预设）</label><input type="number" id="em-ai-max-chars" min="0" max="300"></div>
            <div><label class="field" for="em-ai-channel">指定模型渠道</label><select id="em-ai-channel">${acChannelOptionsHtml(r.ai_channel_id || "")}</select></div>
            <div><label class="field" for="em-ai-model">指定模型 ID（可选）</label><input id="em-ai-model" maxlength="160" placeholder="留空跟随默认"></div>
          </div>
          <div><label class="field" for="em-ai-prompt">规则专属 AI 指令</label><textarea id="em-ai-prompt" rows="2" maxlength="2000" placeholder="可选补充指令"></textarea></div>
        </div>
        <div>
          <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:4px"><label class="field" id="em-tpl-label" for="em-templates" style="margin:0">文案模板</label><button type="button" class="ghost sm" onclick="previewAcTemplates('em-templates')">随机预览</button></div>
          <textarea id="em-templates" rows="4" aria-describedby="em-templates-hint"></textarea>
          <p class="field-help" id="em-templates-hint">每行一条，支持 {nick}、{kw}、{author}、{work_title} 和随机词组 {好|不错|赞}。</p>
        </div>
      </fieldset>
      <fieldset class="ui-form-group"><legend>筛选与发现范围</legend>
        <div class="form-grid">
          <div><label class="field" for="em-match-mode">包含词匹配方式</label><select id="em-match-mode">
            <option value="any">命中任一包含词</option><option value="all_terms">同时命中全部词</option><option value="regex">正则匹配</option><option value="all">不过滤</option>
          </select></div>
          <div id="em-scope-wrap"><label class="field" for="em-comment-scope">评论层级</label><select id="em-comment-scope">
            <option value="all">全部评论</option><option value="top_only">仅一级顶层评论</option><option value="reply_only">仅二级子评论</option>
          </select></div>
          <div><label class="field" for="em-include-kw">包含关键词</label><input id="em-include-kw" placeholder="逗号/中文逗号/换行分隔，留空不限"></div>
          <div><label class="field" for="em-skip">排除 / 跳过关键词</label><input id="em-skip" placeholder="命中任一即跳过"></div>
          <div><label class="field" for="em-min-len">最少字数（0=不限）</label><input type="number" id="em-min-len" min="0" max="500"></div>
          <div><label class="field" for="em-max-age">最近 N 小时内（0=按天）</label><input type="number" id="em-max-age" min="0" max="8760"></div>
          <div><label class="field" for="em-recent-works">扫描近期作品数（0=全局）</label><input type="number" id="em-recent-works" min="0" max="50"></div>
          <div><label class="field" for="em-recent-days">扫描近期天数（0=全局）</label><input type="number" id="em-recent-days" min="0" max="365"></div>
        </div>
      </fieldset>
      <fieldset class="ui-form-group"><legend>审核与执行频率</legend>
        <label class="ui-toggle"><span><b>发布前审核</b><small>先生成草稿，由你核对原文后通过再发送</small></span><input type="checkbox" role="switch" id="em-review"></label>
        <div class="form-grid">
          <div><label class="field" for="em-cap">每日上限（0=跟随账号风控）</label><input type="number" id="em-cap" min="0" inputmode="numeric"></div>
          <div><label class="field" for="em-gap">最小间隔（秒）</label><input type="number" id="em-gap" min="1" inputmode="numeric"></div>
          <div><label class="field" for="em-max">每轮上限（条）</label><input type="number" id="em-max" min="1" max="100" inputmode="numeric"></div>
          <div><label class="field" for="em-interval">检查频率</label><select id="em-interval">${numericSelectOptions(r.interval_seconds || 1800, [[900, "每 15 分钟"], [1800, "每 30 分钟"], [3600, "每小时"], [7200, "每 2 小时"]], " 秒")}</select></div>
        </div>
      </fieldset>`;
    $("em-name").value = r.name || "";
    $("em-mode").value = r.mode; emOnMode();
    $("em-kind").value = r.target_kind; emOnKind();
    if ($("em-acc").querySelector(`option[value="${r.account_id}"]`)) $("em-acc").value = String(r.account_id);
    $("em-target").value = r.mode === "auto_comment"
      ? (r.target_kind === "keyword" ? r.keyword : r.sec_uid)
      : (r.target_kind === "work" ? r.aweme_id : "");
    $("em-compose-mode").value = r.compose_mode || (r.use_ai ? "ai_with_fallback" : "template_only");
    $("em-ai-model").value = r.ai_model || "";
    $("em-ai-prompt").value = r.ai_prompt || "";
    $("em-ai-max-chars").value = String(r.ai_max_chars || 0);
    $("em-templates").value = (r.templates || []).join("\n");
    emOnComposeMode();
    $("em-match-mode").value = r.match_mode || "any";
    $("em-comment-scope").value = r.comment_scope || "all";
    $("em-include-kw").value = (r.include_keywords && r.include_keywords.length ? r.include_keywords.join(", ") : r.reply_filter) || "";
    $("em-skip").value = (r.exclude_keywords && r.exclude_keywords.length ? r.exclude_keywords.join(", ") : r.skip_keywords) || "";
    $("em-min-len").value = String(r.min_source_length || 0);
    $("em-max-age").value = String(r.max_source_age_hours || 0);
    $("em-recent-works").value = String(r.recent_works || 0);
    $("em-recent-days").value = String(r.recent_days || 0);
    $("em-review").checked = !!r.require_review;
    $("em-cap").value = r.daily_cap; $("em-gap").value = r.min_gap_seconds;
    $("em-max").value = r.max_per_run;
    if ([...$("em-interval").options].some(o => o.value === String(r.interval_seconds))) $("em-interval").value = String(r.interval_seconds);
    _uiOpen("编辑规则 #" + id, "调整互动对象、AI 策略、筛选条件与执行频率。", {
      okText: "保存修改", wide: true, icon: "i-automation",
      submit: val => {
        if (acNeedsTemplates(val.compose_mode) && !val.templates.length) uiEditorError("当前文案模式需至少写一条模板", "em-templates");
        if (!val.account_id) uiEditorError("请选择与规则平台一致的账号", "em-acc");
        return api("/api/comment-rules/" + id, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(val) });
      },
    });
    ["em-mode", "em-kind", "em-acc", "em-compose-mode", "em-ai-preset", "em-ai-channel", "em-match-mode", "em-comment-scope", "em-interval"].forEach(idd => { const el = $(idd); if (el) enhanceSelect(el); });
  }).then(async val => {
    if (!val) return;
    toast("规则已更新", "ok"); refreshCommentRules();
  });
}
async function duplicateRule(id) {
  const r = AC_RULES.find(x => x.id === id); if (!r) return;
  const pfAccs = ACCOUNTS.filter(a => a.platform === r.platform);
  const targetAcc = await uiSelect({
    title: `复制规则「${r.name}」`,
    hint: "选择新规则绑定的账号（支持快速复制到同平台其他账号，新规则默认关闭）。",
    value: String(r.account_id || (pfAccs[0]?.id || "")),
    options: pfAccs.map(a => ({ value: String(a.id), label: `${a.nickname || `账号 #${a.id}`}${a.id === r.account_id ? "（当前账号）" : ""}` })),
  });
  if (!targetAcc) return;
  try {
    await api(`/api/comment-rules/${id}/duplicate`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ account_id: Number(targetAcc) }),
    });
    toast("规则已复制（默认关闭）✓", "ok");
    refreshCommentRules();
  } catch (e) { toast("复制失败：" + e.message, "err"); }
}
async function refreshCommentRules() {
  const isCurrent = beginViewRequest("comment-rules");
  if (!$("ac-rule-table")) return;
  refreshAcPolicyMeta();
  const rows = await api("/api/comment-rules?platform=" + PLATFORM);
  if (!isCurrent()) return;
  if ($("tb-ac")) $("tb-ac").textContent = rows.length;
  AC_RULES = rows;
  const presets = AC_POLICY_META?.ai_presets || [];
  $("ac-rule-table").innerHTML = rows.map(r => {
    const tgt = r.mode === "auto_comment"
      ? (r.target_kind === "keyword" ? "#" + esc(r.keyword) : esc((r.sec_uid || "").slice(0, 14)))
      : (r.target_kind === "work" ? esc(r.aweme_id) : "");
    const acc = (ACCOUNTS.find(a => a.id === r.account_id) || {}).nickname || ("#" + r.account_id);
    const cm = r.compose_mode || (r.use_ai ? "ai_with_fallback" : "template_only");
    const presetName = r.ai_preset_id ? (presets.find(p => p.id === r.ai_preset_id)?.name || r.ai_preset_id) : "";
    const badges = [
      `<span class="pill ${acUsesAi(cm) ? "downloading" : "skipped"} bare" style="font-size:10px">${esc(AC_COMPOSE_T[cm] || cm)}</span>`,
      presetName ? `<span class="pill bare" style="font-size:10px">${esc(presetName)}</span>` : "",
      r.require_review ? `<span class="pill pending bare" style="font-size:10px">草稿审核</span>` : "",
      (r.include_keywords?.length) ? `<span class="pill bare" style="font-size:10px">含 ${r.include_keywords.length} 词</span>` : "",
      (r.comment_scope && r.comment_scope !== "all" && r.mode === "auto_reply") ? `<span class="pill bare" style="font-size:10px">${esc(AC_SCOPE_T[r.comment_scope] || r.comment_scope)}</span>` : "",
    ].filter(Boolean).join("");
    const capTxt = r.daily_cap > 0 ? `${r.daily_cap}/日` : "不限(随风控)";
    return `<tr>
      <td><b>${esc(r.name)}</b><div class="ac-rule-meta">${badges}</div></td>
      <td>${AC_MODE_T[r.mode] || r.mode}</td>
      <td class="wrap" style="max-width:180px">${AC_KIND_T[r.target_kind] || r.target_kind}${tgt ? `<div class="mut">${tgt}</div>` : ""}</td>
      <td>${esc(acc)}</td>
      <td class="mut num">${esc(capTxt)} · ${Math.round(r.interval_seconds / 60)}分<div class="mut">单轮≤${r.max_per_run} · ≥${r.min_gap_seconds}s</div></td>
      <td class="mut num">${r.last_run_at ? new Date(r.last_run_at + "Z").toLocaleString() : "—"}${r.last_error ? ` <span class="warn-ic" title="${esc(r.last_error)}">${ic("i-info")}</span>` : ""}${autoRunHint(r.next_auto_run_at)}</td>
      <td><span class="pill ${r.enabled ? "done" : "paused"}">${r.enabled ? "运行中" : "已停用"}</span></td>
      <td class="acttd">
        <button class="ghost sm" onclick="previewRule(${r.id})">试跑预览</button>
        <button class="ghost sm" onclick="editRule(${r.id})">编辑</button>
        <button class="ghost sm" onclick="duplicateRule(${r.id})">复制</button>
        <button class="ghost sm" onclick="toggleRule(${r.id}, ${r.enabled ? "false" : "true"})">${r.enabled ? "停用" : "启用"}</button>
        <button class="ghost sm danger" onclick="delRule(${r.id})">${ic("i-trash")}删除</button>
      </td></tr>`;
  }).join("") || empty(8, "暂无评论规则", "i-msg", "创建规则后可先「试跑预览」核对命中与文案，确认无误再启用。", "autocomment");
}
async function toggleRule(id, en) {
  try { await api("/api/comment-rules/" + id, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ enabled: en }) }); toast(en ? "已启用" : "已停用", "ok"); refreshCommentRules(); }
  catch (e) { toast("操作失败:" + e.message, "err"); }
}
async function previewRule(id) {
  const btn = evtBtn();
  const rule = AC_RULES.find(x => x.id === id);
  toast("正在试跑预览（不会写入队列或发送评论），可能需十余秒…", "info", 8000);
  let res = null;
  await withBusy(btn, "预览中", async () => {
    try { res = await api(`/api/comment-rules/${id}/preview?limit=8`, { method: "POST" }); }
    catch (e) { toast("预览失败：" + e.message, "err", 7000); }
  });
  if (!res) return;
  if (!res.ok) { toast("预览未通过：" + (res.error || "未知原因"), "err", 8000); return; }

  const items = res.items || [];
  const action = await new Promise(resolve => {
    _uiResolve = resolve; _uiCancelVal = null; _uiGetVal = () => "draft";
    $("ui-body").innerHTML = `
      <div class="stack" style="gap:10px">
        <div class="ac-policy-strip" data-state="${res.ai_warning ? "warn" : "ok"}">
          <div class="ac-policy-copy">
            <b>发现 ${res.candidates || 0} 个候选 · 本次命中 ${items.length} 条（纯预览，未发任何评论）</b>
            <p>${esc(res.note || "所有候选均已按当前筛选与去重条件过滤。")}${res.ai_warning ? ` ⚠️ ${esc(res.ai_warning)}` : ""}</p>
          </div>
        </div>
        ${items.length ? `<div class="ac-preview-list">${items.map(it => {
          const [srcLabel, srcCls] = AC_SRC_META[it.content_source] || ["模板", ""];
          return `<div class="ac-preview-item">
            <div class="ac-preview-meta">
              <code>${esc((it.aweme_id || "").slice(0, 20))}</code>
              ${it.target_nick ? `<span>@${esc(it.target_nick)}</span>` : ""}
              <span class="ac-src-tag ${srcCls}">${esc(srcLabel)}</span>
              <span class="pill ${it.would_status === "draft" ? "downloading" : "pending"} bare" style="font-size:10px">${it.would_status === "draft" ? "正式运行将入草稿" : "正式运行将直接排队"}</span>
            </div>
            ${it.work_desc ? `<div class="mut" style="font-size:11px">作品：${esc(it.work_desc.slice(0, 90))}</div>` : ""}
            <div class="ac-task-origin"><b>对方内容：</b>${esc(it.source_text || "（无正文）")}</div>
            <div class="ac-task-reply${it.content ? "" : " empty-reply"}"><b>拟发文案：</b>${esc(it.content || "（空草稿，需人工补充）")}</div>
            ${it.ai_error ? `<div class="mut" style="color:var(--warn);font-size:11px">AI 提示：${esc(it.ai_error)}</div>` : ""}
          </div>`;
        }).join("")}</div>` : `<div class="empty"><div class="empty-t">本次未命中可生成的目标</div><div class="empty-sub">${esc(res.note || "可检查关键词筛选或近期作品范围")}</div></div>`}
        ${items.length ? `<div style="display:flex;justify-content:flex-end;gap:8px;padding-top:4px">
          <button type="button" class="ghost sm" id="ac-prev-run-live">按规则配置正式执行一轮</button>
        </div>` : ""}
      </div>`;
    if ($("ac-prev-run-live")) {
      $("ac-prev-run-live").onclick = () => { _uiClose("live"); };
    }
    _uiOpen(`试跑预览 · ${rule ? rule.name : "#" + id}`, "仅抓取与生成样例，不写入队列也不发送。确认满意后再生成草稿或正式运行。", {
      okText: items.length ? "生成草稿待审" : "关闭",
      wide: true,
      icon: "i-sparkles",
    });
  });
  if (!action || !items.length) return;
  await runRule(id, action === "draft");
}
async function runRule(id, asDraft = false) {
  toast(asDraft ? "正在生成草稿待审…" : "正在正式执行规则…", "info", 8000);
  try {
    const r = await api(`/api/comment-rules/${id}/run-now${asDraft ? "?as_draft=true" : ""}`, { method: "POST" });
    if (!r.ok) toast("未生成:" + (r.error || ""), "err", 7000);
    else if (r.created > 0) toast(`已生成 ${r.created} 条${r.manual_only ? "人工发布草稿" : r.review ? "待审草稿" : "待发任务"}（发现 ${r.candidates} 个目标）`, "ok", 6000);
    else toast(`发现 ${r.candidates} 个目标，生成 0 条` + (r.note ? `：${r.note}` : ""), "info", 9000);
  } catch (e) { toast("执行失败:" + e.message, "err"); }
  refreshCommentRules(); refreshCommentTasks();
}
async function delRule(id) {
  if (!await uiConfirm({ title: "删除规则", message: "删除该规则及其未发送任务?", okText: "删除", danger: true })) return;
  try { await api("/api/comment-rules/" + id, { method: "DELETE" }); toast("已删除", "ok"); refreshCommentRules(); refreshCommentTasks(); }
  catch (e) { toast("删除失败:" + e.message, "err"); }
}
function syncAcTaskSelectionBar() {
  const validIds = new Set(AC_TASKS.map(t => t.id));
  for (const id of [...AC_SELECTED_TASKS]) { if (!validIds.has(id)) AC_SELECTED_TASKS.delete(id); }
  const bar = $("ac-sel-bar"), info = $("ac-sel-info");
  if (bar) bar.hidden = AC_SELECTED_TASKS.size === 0;
  if (info) {
    const selectedRows = AC_TASKS.filter(t => AC_SELECTED_TASKS.has(t.id));
    const draftCnt = selectedRows.filter(t => t.status === "draft").length;
    info.textContent = `已选择 ${selectedRows.length} 条任务（含 ${draftCnt} 条草稿）`;
  }
  const allChk = $("ac-task-check-all");
  if (allChk) {
    const selectable = AC_TASKS.filter(t => ["draft", "pending", "failed"].includes(t.status));
    const chosen = selectable.filter(t => AC_SELECTED_TASKS.has(t.id)).length;
    allChk.checked = selectable.length > 0 && chosen === selectable.length;
    allChk.indeterminate = chosen > 0 && chosen < selectable.length;
  }
}
function onAcTaskSelect(id, checked) {
  if (checked) AC_SELECTED_TASKS.add(Number(id));
  else AC_SELECTED_TASKS.delete(Number(id));
  syncAcTaskSelectionBar();
}
function toggleSelectAllAcTasks(checked) {
  AC_TASKS.filter(t => ["draft", "pending", "failed"].includes(t.status)).forEach(t => {
    if (checked) AC_SELECTED_TASKS.add(t.id); else AC_SELECTED_TASKS.delete(t.id);
  });
  renderCommentTaskRows();
}
function clearAcTaskSelection() {
  AC_SELECTED_TASKS.clear();
  renderCommentTaskRows();
}
async function refreshCommentTasks() {
  const isCurrent = beginViewRequest("comment-tasks");
  if (!$("ac-task-table")) return;
  const st = $("ac-task-filter") ? $("ac-task-filter").value : "";
  const rows = await api("/api/comment-tasks?platform=" + PLATFORM + (st ? "&status=" + st : ""));
  if (!isCurrent()) return;
  AC_TASKS = rows;
  const drafts = rows.filter(t => t.status === "draft");
  const readyDrafts = drafts.filter(t => (t.content || "").trim());
  const emptyDrafts = drafts.length - readyDrafts.length;
  if ($("ac-draft-bar")) {
    $("ac-draft-bar").style.display = drafts.length ? "flex" : "none";
    if (drafts.length) {
      $("ac-draft-count").textContent = `有 ${drafts.length} 条草稿待审核（可发送 ${readyDrafts.length} 条${emptyDrafts ? ` · ${emptyDrafts} 条 AI 失败空草稿待编辑/重写` : ""}）`;
    }
  }
  renderCommentTaskRows();
}
function renderCommentTaskRows() {
  if (!$("ac-task-table")) return;
  const query = (($("ac-task-search") && $("ac-task-search").value) || "").trim().toLocaleLowerCase();
  const filtered = AC_TASKS.filter(t => {
    if (!query) return true;
    const accName = (ACCOUNTS.find(a => a.id === t.account_id) || {}).nickname || "";
    return [t.content, t.target_text, t.work_desc, t.aweme_id, t.target_nick, accName].filter(Boolean).join(" ").toLocaleLowerCase().includes(query);
  });
  if ($("ac-task-count")) $("ac-task-count").textContent = AC_TASKS.length ? `显示 ${filtered.length} / ${AC_TASKS.length}` : "";
  $("ac-task-table").innerHTML = filtered.map(t => {
    const isDraft = t.status === "draft", canSend = t.status === "pending" || t.status === "failed";
    const selectable = isDraft || canSend;
    const checked = AC_SELECTED_TASKS.has(t.id);
    const accName = (ACCOUNTS.find(a => a.id === t.account_id) || {}).nickname || (t.account_id ? `账号 #${t.account_id}` : "");
    const [srcLabel, srcCls] = AC_SRC_META[t.content_source] || (t.content_source ? [t.content_source, ""] : ["", ""]);
    const hasContent = Boolean((t.content || "").trim());
    return `<tr>
    <td><input type="checkbox" ${selectable ? "" : "disabled"} ${checked ? "checked" : ""} onchange="onAcTaskSelect(${t.id}, this.checked)" aria-label="选择任务 #${t.id}"></td>
    <td class="wrap"><div class="ac-task-pair">
      ${(t.target_text || t.work_desc) ? `<div class="ac-task-origin">${t.target_text ? `<b>${t.target_comment_id ? "对方评论：" : "目标作品："}</b>${esc(t.target_text)}` : ""}${t.work_desc && t.work_desc !== t.target_text ? `<div style="margin-top:2px;opacity:.85">作品：${esc(t.work_desc.slice(0, 70))}</div>` : ""}</div>` : ""}
      <div class="ac-task-reply${hasContent ? "" : " empty-reply"}">${hasContent ? esc(t.content) : "（文案为空，请点击「AI 重写」或「编辑」补充）"}</div>
      <div style="display:flex;align-items:center;gap:6px;flex-wrap:wrap">
        ${srcLabel ? `<span class="ac-src-tag ${srcCls}"${t.ai_error ? ` title="${esc(t.ai_error)}"` : ""}>${esc(srcLabel)}</span>` : ""}
        ${t.ai_error ? `<span class="mut" style="font-size:11px;color:var(--warn)" title="${esc(t.ai_error)}">${esc(t.ai_error.slice(0, 48))}</span>` : ""}
      </div>
    </div></td>
    <td><code title="${esc(t.aweme_id || "")}">${esc((t.aweme_id || "—").slice(0, 18))}</code>${accName ? `<div class="mut">${esc(accName)}</div>` : ""}</td>
    <td>${t.target_comment_id ? "回复 " + esc(t.target_nick || "") : "顶层评论"}</td>
    <td class="mut num">${t.scheduled_at ? new Date(t.scheduled_at + "Z").toLocaleString() : "尽快"}${t.next_allowed_at ? `<div class="mut" title="预约保持不变，同时等待风控间隔结束">风控最早 ${esc(riskTime(t.next_allowed_at))}</div>` : ""}</td>
    <td class="mut">${t.method === "browser" ? "浏览器页面" : t.method === "api" ? "API 兼容模式" : t.method === "manual" ? "人工草稿" : "—"}</td>
    <td><span class="pill ${AC_TASK_PILL[t.status] || "pending"}">${AC_TASK_ST[t.status] || t.status}</span>${t.error ? ` <span class="warn-ic" title="${esc(t.error)}">${ic("i-info")}</span>` : ""}</td>
    <td class="acttd">
      ${isDraft && hasContent ? `<button class="sm" onclick="approveTask(${t.id})">通过</button>` : ""}
      ${selectable ? `<button class="ghost sm" onclick="regenerateTask(${t.id})">AI 重写</button>` : ""}
      ${selectable ? `<button class="ghost sm" onclick="editTaskContent(${t.id})">编辑</button>` : ""}
      ${canSend && hasContent ? `<button class="ghost sm" onclick="runTask(${t.id})">立即发</button>` : ""}
      ${selectable ? `<button class="ghost sm" onclick="cancelTask(${t.id})">${isDraft ? "弃用" : "取消"}</button>` : ""}
      ${t.status === "uncertain" ? `<button class="ghost sm" onclick="resolveTaskResult('comments',${t.id})">核对结果</button>` : t.status !== "doing" ? `<button class="ghost sm danger" onclick="delTask(${t.id})">${ic("i-trash")}删除</button>` : ""}
    </td></tr>`;
  }).join("") || empty(8, query ? "没有匹配的评论任务" : "暂无评论任务", "i-msg", query ? "调整搜索词或清除筛选" : "启用规则或点「试跑预览」后，这里会出现待审与待发评论");
  syncAcTaskSelectionBar();
}
async function regenerateTask(id) {
  const btn = evtBtn();
  await withBusy(btn, "重写中", async () => {
    try {
      await api(`/api/comment-tasks/${id}/regenerate`, { method: "POST" });
      toast("已用 AI 生成新文案 ✓", "ok");
      refreshCommentTasks();
    } catch (e) { toast("AI 重写失败：" + e.message, "err", 7000); }
  });
}
async function approveTask(id) {
  try { await api("/api/comment-tasks/" + id + "/approve", { method: "POST" }); toast("已通过,转入待发队列", "ok"); refreshCommentTasks(); }
  catch (e) { toast("操作失败:" + e.message, "err"); }
}
async function approveSelectedDrafts() {
  const platform = PLATFORM;
  const ids = AC_TASKS.filter(t => AC_SELECTED_TASKS.has(t.id) && t.status === "draft" && (t.content || "").trim()).map(t => t.id);
  if (!ids.length) { toast("所选任务中没有可发送的非空草稿", "info"); return; }
  if (!await uiConfirm({ title: "批量通过所选草稿", message: `通过已选的 ${ids.length} 条草稿？通过后引擎按风控上限与最小间隔陆续发出。`, okText: "确认通过" })) return;
  try {
    const r = await api("/api/comment-tasks/batch-approve", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ids, platform }) });
    ids.forEach(id => AC_SELECTED_TASKS.delete(id));
    toast(`已通过 ${r.approved} 条`, "ok"); refreshCommentTasks();
  } catch (e) { toast("操作失败:" + e.message, "err"); }
}
async function cancelSelectedTasks() {
  const ids = AC_TASKS.filter(t => AC_SELECTED_TASKS.has(t.id) && ["draft", "pending", "failed"].includes(t.status)).map(t => t.id);
  if (!ids.length) return;
  if (!await uiConfirm({ title: "批量弃用/取消", message: `确认取消已选的 ${ids.length} 条任务？`, okText: "确认取消", danger: true })) return;
  try {
    const r = await api("/api/comment-tasks/batch-cancel", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ids }) });
    ids.forEach(id => AC_SELECTED_TASKS.delete(id));
    toast(`已取消 ${r.canceled} 条`, "ok"); refreshCommentTasks();
  } catch (e) { toast("操作失败:" + e.message, "err"); }
}
async function approveAllDrafts() {
  const platform = PLATFORM;
  const ids = AC_TASKS.filter(t => t.status === "draft" && (t.content || "").trim()).map(t => t.id);
  if (!ids.length) { toast("当前没有可发送的非空草稿（空草稿请先编辑或 AI 重写）", "info"); return; }
  if (!await uiConfirm({ title: "审核当前列表草稿", message: `通过当前列表中 ${PF_NAME[platform] || platform} 的 ${ids.length} 条可发草稿？通过后引擎按每账号每日上限和最小间隔陆续发出。`, okText: "通过这些草稿" })) return;
  try { const r = await api("/api/comment-tasks/batch-approve", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ids, platform }) }); toast(`已通过 ${r.approved} 条`, "ok"); refreshCommentTasks(); }
  catch (e) { toast("操作失败:" + e.message, "err"); }
}
async function editTaskContent(id) {
  const t = AC_TASKS.find(x => x.id === id); if (!t) return;
  const originHint = t.target_text ? `对方原文：「${t.target_text.slice(0, 120)}」` : "保存只更新文案，不会发送评论。";
  const v = await uiPrompt({ title: "编辑评论文案", hint: originHint, value: t.content || "", multiline: true, rows: 3,
    submit: value => {
      if (!value.trim()) uiEditorError("文案不能为空", "ui-inp");
      return api("/api/comment-tasks/" + id, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ content: value.trim() }) });
    },
  });
  if (v === null) return;
  toast("文案已更新", "ok"); refreshCommentTasks();
}
async function runTask(id) {
  const btn = evtBtn();
  toast("发送中…正在开浏览器发评论(有头窗口会弹出)", "info", 8000);
  await withBusy(btn, "发送中", async () => {
    try { const r = await api("/api/comment-tasks/" + id + "/run-now", { method: "POST" }); toast(r.ok ? "已发送 ✓" : "未成功:" + (r.error || ""), r.ok ? "ok" : "err", 7000); }
    catch (e) { toast("发送失败:" + e.message, "err"); }
  });
  refreshCommentTasks();
}
async function cancelTask(id) {
  try { await api("/api/comment-tasks/" + id + "/cancel", { method: "POST" }); AC_SELECTED_TASKS.delete(id); toast("已取消", "ok"); refreshCommentTasks(); }
  catch (e) { toast("操作失败:" + e.message, "err"); }
}
async function delTask(id) {
  if (!await uiConfirm({ title: "删除评论任务", message: "确认删除这条评论任务或记录吗？", okText: "删除", danger: true })) return;
  try { await api("/api/comment-tasks/" + id, { method: "DELETE" }); AC_SELECTED_TASKS.delete(id); toast("已删除", "ok"); refreshCommentTasks(); }
  catch (e) { toast("删除失败:" + e.message, "err"); }
}
window.addEventListener("creatorhub:ai-config:updated", () => refreshAcPolicyMeta());
window.addEventListener("creatorhub:ai-channels:updated", () => refreshAcPolicyMeta());

// ─── 统一任务队列 ───
let TASK_QUEUE_PAGE = 1;
let TASK_QUEUE_PAGE_SIZE = 20;
let TASK_QUEUE_PAGES = 1;
let TASK_QUEUE_LOADING = false;
let TASK_QUEUE_REFRESH_PENDING = false;
let TASK_QUEUE_BADGE_LOADING = false;
const TASK_QUEUE_SELECTED = new Set();

const TASK_QUEUE_RAW_STATUS = {
  draft: "草稿待审", pending: "等待执行", running: "采集中", publishing: "发布中",
  doing: "执行中", downloading: "下载中", done: "已完成", failed: "失败",
  partial: "部分完成", uncertain: "结果待确认", canceled: "已取消", skipped: "已跳过",
};
const TASK_QUEUE_STATE_META = {
  pending: ["等待执行", "pending"], running: ["正在执行", "queue-running"],
  blocked: ["风控延后", "queue-blocked"], failed: ["执行失败", "failed"],
  completed: ["已完成", "queue-completed"],
};

function updateTaskQueuePlatformLabel() {
  const option = $("queue-platform-current");
  if (option) option.textContent = `当前平台（${PF_NAME[PLATFORM] || PLATFORM}）`;
  const select = $("queue-platform");
  if (select && select._csSync) select._csSync();
}

function taskQueuePlatform() {
  const value = $("queue-platform")?.value || "current";
  return value === "current" ? PLATFORM : value;
}

function taskQueueDate(value) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  return date.toLocaleString("zh-CN", {
    month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit",
    hour12: false,
  });
}

function taskQueueSourceLabel(tab) {
  return ({
    collections: "采集任务", publish: "发布任务", autocomment: "评论任务",
    hub: "账号管理", monitors: "作品监控",
  })[tab] || "查看";
}

function queueProgressTaskKey(item) {
  const id = Number(item?.id || 0);
  if (!id) return "";
  if (item.queue_type === "collections") return `collection:${id}`;
  if (item.queue_type === "publishes") return `publish:${id}`;
  if (item.queue_type === "monitor_downloads") return `download:content:${id}`;
  if (item.queue_type === "collection_downloads") return `download:collection:${id}`;
  return "";
}

function renderTaskQueueProgressHtml(item, liveTask = null) {
  const prog = window.CreatorHubProgress;
  const p = liveTask
    ? {
        percent: liveTask.percent,
        determinate: !!liveTask.determinate,
        phase_label: liveTask.phase_label || "",
        speed_bps: liveTask.speed_bps,
        eta_seconds: liveTask.eta_seconds,
        current: liveTask.current,
        total: liveTask.total,
        unit: liveTask.unit,
        status: liveTask.status,
        next_allowed_at: liveTask.next_allowed_at || item.next_allowed_at,
      }
    : (item.progress || null);
  const st = liveTask?.status === "running"
    ? "running"
    : liveTask?.status === "waiting_user" || liveTask?.status === "blocked"
      ? "blocked"
      : liveTask?.status === "completed"
        ? "completed"
        : liveTask?.status === "failed"
          ? "failed"
          : (item.state || "pending");
  const nextAt = p?.next_allowed_at || item.next_allowed_at;
  if (st === "blocked" && nextAt) {
    const cd = prog?.fmtCountdown?.(nextAt) || "等待恢复";
    const phase = p?.phase_label || "冷却中";
    return `<div class="queue-progress-cell"><div class="queue-progress-head"><span>${esc(phase)}</span></div><div class="queue-progress-meta progress-num" data-countdown-iso="${esc(nextAt)}">${esc(cd)}</div></div>`;
  }
  if (st === "running") {
    const det = !!(p && p.determinate && p.percent !== null && p.percent !== undefined && Number.isFinite(Number(p.percent)));
    const pct = det ? Math.max(0, Math.min(100, Math.round(Number(p.percent)))) : null;
    const bar = prog?.renderProgressBar
      ? prog.renderProgressBar({ determinate: det, percent: pct, tone: "info", label: item.title || "执行进度", compact: true })
      : `<div class="progress-track is-compact${det ? "" : " is-indeterminate"}"><div class="progress-fill" style="${det ? `width:${pct}%` : ""}"></div></div>`;
    const meta = [];
    if (p?.unit === "bytes" && Number(p.total) > 0) {
      meta.push(`${prog?.fmtBytes?.(p.current) || ""} / ${prog?.fmtBytes?.(p.total) || ""}`);
    } else if (p?.unit === "bytes" && Number(p.current) > 0) {
      meta.push(prog?.fmtBytes?.(p.current) || "");
    }
    const spd = p?.speed_bps ? (prog?.fmtSpeed?.(p.speed_bps) || "") : "";
    if (spd) meta.push(spd);
    const eta = p?.eta_seconds ? (prog?.fmtEta?.(p.eta_seconds) || "") : "";
    if (eta) meta.push(eta);
    return `<div class="queue-progress-cell"><div class="queue-progress-head"><span title="${esc(p?.phase_label || "执行中")}">${esc(p?.phase_label || "执行中")}</span><b class="progress-num">${det ? `${pct}%` : "进行中"}</b></div>${bar}${meta.length ? `<div class="queue-progress-meta progress-num">${esc(meta.join(" · "))}</div>` : ""}</div>`;
  }
  if (st === "completed") {
    return `<div class="queue-progress-cell"><div class="queue-progress-meta">${esc(p?.phase_label || (item.status === "canceled" ? "已取消" : item.status === "skipped" ? "已跳过" : "100% · 已完成"))}</div></div>`;
  }
  if (st === "failed") {
    return `<div class="queue-progress-cell"><div class="queue-progress-meta" style="color:var(--danger)">${esc(p?.phase_label || "执行中断")}</div></div>`;
  }
  if (p && p.determinate && Number.isFinite(Number(p.percent)) && Number(p.percent) > 0) {
    const pct = Math.max(0, Math.min(100, Math.round(Number(p.percent))));
    const bar = prog?.renderProgressBar
      ? prog.renderProgressBar({ determinate: true, percent: pct, tone: "warn", label: item.title || "任务进度", compact: true })
      : `<div class="progress-track is-compact"><div class="progress-fill tone-warn" style="width:${pct}%"></div></div>`;
    return `<div class="queue-progress-cell"><div class="queue-progress-head"><span>${esc(p.phase_label || "等待继续")}</span><b class="progress-num">${pct}%</b></div>${bar}</div>`;
  }
  return `<div class="queue-progress-cell"><div class="queue-progress-meta">${esc(p?.phase_label || "等待调度")}</div></div>`;
}

function taskQueueRow(item) {
  const stateMeta = TASK_QUEUE_STATE_META[item.state] || [item.state || "未知", "skipped"];
  const rawStatus = TASK_QUEUE_RAW_STATUS[item.status] || item.status || "未知";
  const account = item.account_name || (item.account_id ? `账号 #${item.account_id}` : item.platform === "shipinhao" ? "本机微信客户端" : "未绑定账号");
  const scheduled = item.scheduled_at ? `<b>计划 ${taskQueueDate(item.scheduled_at)}${item.schedule_needs_confirmation ? "（待确认时区）" : ""}</b>` : "";
  const created = item.created_at ? `<small>创建 ${taskQueueDate(item.created_at)}</small>` : "";
  const nextAllowed = item.next_allowed_at ? `<small>最早继续：${taskQueueDate(item.next_allowed_at)}</small>` : "";
  const reason = item.blocked_reason || item.error || "—";
  const reasonClass = item.error && !item.blocked_reason ? " has-error" : "";
  const signal = item.blocked_signal ? `<small>信号：${esc(item.blocked_signal)}</small>` : "";
  const stopKey = `${item.queue_type}:${Number(item.id)}`;
  const progKey = queueProgressTaskKey(item);
  const liveTask = progKey ? window.CreatorHubProgress?.getTask?.(progKey) : null;
  const selectable = ["collections", "publishes", "comments", "actions"].includes(item.queue_type)
    && !["done", "canceled", "skipped", "uncertain"].includes(item.status);
  return `<tr data-queue-key="${esc(stopKey)}"${progKey ? ` data-progress-key="${esc(progKey)}"` : ""}>
    <td><input type="checkbox" class="queue-select" data-queue-type="${esc(item.queue_type)}" data-task-id="${Number(item.id)}" aria-label="选择任务 ${Number(item.id)}" onchange="toggleTaskQueueItem(this)"${selectable ? "" : " disabled"}${TASK_QUEUE_SELECTED.has(stopKey) ? " checked" : ""}></td>
    <td><span class="pill q bare">${esc(item.queue_label)}</span><small class="mut" style="display:block;margin-top:5px">${esc(PF_NAME[item.platform] || item.platform || "—")}</small></td>
    <td><div class="queue-copy"><b title="${esc(item.title)}">${esc(item.title)}</b>${item.detail ? `<small>${esc(item.detail)}</small>` : ""}</div></td>
    <td><div class="queue-account"><b>${esc(account)}</b>${item.account_id ? `<small>ID ${Number(item.account_id)}</small>` : ""}</div></td>
    <td><div class="queue-time">${scheduled || "尽快执行"}${created}</div></td>
    <td><span class="pill ${stateMeta[1]}">${esc(stateMeta[0])}</span><small class="mut" style="display:block;margin-top:5px">${esc(rawStatus)}</small></td>
    <td class="queue-col-progress" data-queue-prog-cell="${esc(progKey || stopKey)}">${renderTaskQueueProgressHtml(item, liveTask)}</td>
    <td><div class="queue-reason${reasonClass}">${esc(reason)}${signal}${nextAllowed}</div></td>
    <td class="acttd">${item.status === "uncertain" ? `<button type="button" class="ghost sm" onclick="resolveTaskResult(${jsArg(item.queue_type)},${Number(item.id)})">核对结果</button>` : ""}<button type="button" class="ghost sm" onclick="openTaskQueueSource(${jsArg(item.source_tab)})">${esc(taskQueueSourceLabel(item.source_tab))}</button></td>
  </tr>`;
}

window.addEventListener("creatorhub:progress:update", e => {
  const task = e?.detail?.task;
  if (!task || !task.task_key) return;
  if (typeof CURRENT_TAB !== "undefined" && CURRENT_TAB !== "queue") return;
  const cells = document.querySelectorAll(`#queue-table td[data-queue-prog-cell]`);
  for (const cell of cells) {
    if (cell.dataset.queueProgCell === task.task_key) {
      cell.innerHTML = renderTaskQueueProgressHtml({ title: task.title, state: task.status, status: task.status, next_allowed_at: task.next_allowed_at }, task);
    }
  }
});

setInterval(() => {
  if (document.hidden || typeof CURRENT_TAB === "undefined" || CURRENT_TAB !== "queue") return;
  const prog = window.CreatorHubProgress;
  if (!prog?.fmtCountdown) return;
  document.querySelectorAll("#queue-table [data-countdown-iso]").forEach(el => {
    const iso = el.dataset.countdownIso;
    if (iso) el.textContent = prog.fmtCountdown(iso) || "即将恢复";
  });
}, 1000);

function updateTaskQueueSelection() {
  const button = $("queue-batch-stop");
  if (button) {
    button.disabled = TASK_QUEUE_SELECTED.size === 0;
    button.lastChild.textContent = TASK_QUEUE_SELECTED.size ? `停止所选（${TASK_QUEUE_SELECTED.size}）` : "停止所选";
  }
}
function toggleTaskQueueItem(input) {
  const key = `${input.dataset.queueType}:${Number(input.dataset.taskId)}`;
  if (input.checked) TASK_QUEUE_SELECTED.add(key); else TASK_QUEUE_SELECTED.delete(key);
  updateTaskQueueSelection();
}
function toggleTaskQueuePage(checked) {
  document.querySelectorAll("#queue-table .queue-select:not(:disabled)").forEach(input => {
    input.checked = checked; toggleTaskQueueItem(input);
  });
}
async function batchStopSelectedTasks() {
  if (!TASK_QUEUE_SELECTED.size) return;
  if (!await uiConfirm({ title: "批量停止任务", message: `将安全停止 ${TASK_QUEUE_SELECTED.size} 个所选任务。执行中的发布、评论和私信不会被强制中断。`, okText: "停止所选", danger: true })) return;
  const items = [...TASK_QUEUE_SELECTED].map(value => {
    const split = value.lastIndexOf(":");
    return { queue_type: value.slice(0, split), id: Number(value.slice(split + 1)) };
  });
  try {
    const result = await api("/api/task-queue/batch-stop", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ items }),
    });
    (result.stopped || []).forEach(item => TASK_QUEUE_SELECTED.delete(`${item.queue_type}:${item.id}`));
    toast(`已停止 ${result.stopped_count || 0} 个任务${result.skipped_count ? `，跳过 ${result.skipped_count} 个执行中任务` : ""}`, result.skipped_count ? "info" : "ok");
    updateTaskQueueSelection();
    await refreshTaskQueue();
  } catch (e) { toast("批量停止失败：" + e.message, "err"); }
}

async function resolveTaskResult(queueType, id) {
  const outcome = await uiSelect({
    title: `核对任务 #${id} 的平台结果`,
    hint: "请先到平台检查是否已生效。本操作只记录核对结果，不会发送或重试。尚未查清请取消。",
    value: "",
    options: [{ value: "", label: "请选择核对结论", disabled: true },
      { value: "done", label: "已核实：平台操作已成功" },
      { value: "canceled", label: "取消后续处理，不再重试" }],
  });
  if (!["done", "canceled"].includes(outcome)) return;
  const note = await uiPrompt({ title: "填写核对说明", hint: "例如平台作品链接、核对时间和结果；请勿填写账号凭据。", placeholder: "必填，最多 500 字" });
  if (note === null) return;
  if (!note.trim() || note.trim().length > 500) { toast("核对说明需填写 1–500 字", "err"); return; }
  try {
    await api(`/api/task-queue/${encodeURIComponent(queueType)}/${Number(id)}/resolve`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ outcome, note: note.trim() }),
    });
    toast("核对结果已保存，任务不会自动重试", "ok");
    if (CURRENT_TAB === "publish") await refreshPublish();
    else if (CURRENT_TAB === "autocomment") await refreshCommentTasks();
    else if (CURRENT_TAB === "queue") await refreshTaskQueue();
    await refreshTaskQueueBadge();
  } catch (e) { toast("保存核对结果失败：" + e.message, "err"); }
}

function renderTaskQueuePager(data) {
  const pager = $("queue-pager");
  if (!pager) return;
  TASK_QUEUE_PAGE = Math.max(1, Number(data.page || 1));
  TASK_QUEUE_PAGE_SIZE = Math.max(1, Number(data.page_size || TASK_QUEUE_PAGE_SIZE));
  TASK_QUEUE_PAGES = Math.max(1, Number(data.pages || 1));
  $("queue-page-info").textContent = `第 ${TASK_QUEUE_PAGE} / ${TASK_QUEUE_PAGES} 页 · 共 ${fmtNum(data.total || 0)} 条`;
  $("queue-first").disabled = TASK_QUEUE_PAGE <= 1;
  $("queue-prev").disabled = TASK_QUEUE_PAGE <= 1;
  $("queue-next").disabled = TASK_QUEUE_PAGE >= TASK_QUEUE_PAGES;
  $("queue-last").disabled = TASK_QUEUE_PAGE >= TASK_QUEUE_PAGES;
  if ($("queue-page-size")) $("queue-page-size").value = String(TASK_QUEUE_PAGE_SIZE);
  pager.hidden = Number(data.total || 0) <= TASK_QUEUE_PAGE_SIZE && TASK_QUEUE_PAGES <= 1;
}

function renderTaskQueueSummary(summary = {}) {
  if (taskQueuePlatform() === PLATFORM) globalThis.CreatorHubWorkbench?.queueSummary?.(summary);
  ["active", "pending", "running", "blocked", "failed"].forEach(name => {
    const el = $(`queue-stat-${name}`);
    if (el) el.textContent = fmtNum(Number(summary[name] || 0));
  });
  const badge = $("tb-queue");
  if (badge) badge.textContent = fmtNum(Number(summary.active || 0));
  const selected = $("queue-state")?.value || "active";
  document.querySelectorAll("[data-queue-state]").forEach(button =>
    button.classList.toggle("active", button.dataset.queueState === selected));
}

async function refreshTaskQueue(resetPage = false) {
  const body = $("queue-table");
  if (resetPage) TASK_QUEUE_PAGE = 1;
  if (!body) return;
  if (TASK_QUEUE_LOADING) { VIEW_REQUESTS.delete("task-queue"); TASK_QUEUE_REFRESH_PENDING = true; return; }
  const isCurrent = beginViewRequest("task-queue");
  TASK_QUEUE_LOADING = true;
  $("queue-table-wrap")?.classList.add("stale");
  const params = new URLSearchParams({
    platform: taskQueuePlatform(),
    queue_type: $("queue-type")?.value || "",
    state: $("queue-state")?.value || "active",
    q: $("queue-query")?.value.trim() || "",
    page: String(TASK_QUEUE_PAGE),
    page_size: String(TASK_QUEUE_PAGE_SIZE),
  });
  try {
    const data = await api("/api/task-queue?" + params.toString());
    if (!isCurrent()) return;
    const html = data.items?.length
      ? data.items.map(taskQueueRow).join("")
      : empty(9, "当前筛选范围内没有任务", "i-inbox", "切换状态或平台范围后再查看");
    setHtmlIfChanged(body, html);
    updateTaskQueueSelection();
    renderTaskQueueSummary(data.summary || {});
    renderTaskQueuePager(data);
  } catch (e) {
    if (!isCurrent()) return;
    setHtmlIfChanged(body, empty(9, "任务队列加载失败", "i-info", e.message || "请稍后重试"));
    if (CURRENT_TAB === "queue") toast("任务队列加载失败：" + e.message, "err");
  } finally {
    TASK_QUEUE_LOADING = false;
    $("queue-table-wrap")?.classList.remove("stale");
    if (TASK_QUEUE_REFRESH_PENDING) {
      TASK_QUEUE_REFRESH_PENDING = false;
      setTimeout(() => refreshTaskQueue(), 0);
    }
  }
}

async function refreshTaskQueueBadge() {
  if (TASK_QUEUE_BADGE_LOADING || CURRENT_TAB === "queue") return;
  const isCurrent = beginViewRequest("task-queue-badge");
  TASK_QUEUE_BADGE_LOADING = true;
  try {
    const params = new URLSearchParams({ platform: GLOBAL_MODE ? "all" : PLATFORM });
    const data = await api("/api/task-queue/summary?" + params.toString());
    if (!isCurrent() || CURRENT_TAB === "queue") return;
    globalThis.CreatorHubWorkbench?.queueSummary?.(data.summary || {});
    const badge = $("tb-queue");
    if (badge) badge.textContent = fmtNum(Number(data.summary?.active || 0));
  } catch (e) {
    // 导航徽章是辅助信息，失败时保留上次值，不打断当前页面操作。
  } finally { TASK_QUEUE_BADGE_LOADING = false; }
}

function setTaskQueueState(state) {
  const select = $("queue-state");
  if (!select) return;
  select.value = state;
  if (select._csSync) select._csSync();
  refreshTaskQueue(true);
}

function goTaskQueuePage(page) {
  const target = Math.max(1, Math.min(TASK_QUEUE_PAGES, Number(page) || 1));
  if (target === TASK_QUEUE_PAGE) return;
  TASK_QUEUE_PAGE = target;
  refreshTaskQueue();
}

function setTaskQueuePageSize(value) {
  TASK_QUEUE_PAGE_SIZE = Math.max(10, Math.min(100, Number(value) || 20));
  refreshTaskQueue(true);
}

function openTaskQueueSource(tab) {
  if (!PAGE_META[tab]) return;
  switchTab(tab, true);
}

function esc(s) { return (s || "").toString().replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])); }

async function refreshOverviewSummary() {
  const isCurrent = beginViewRequest("overview-summary");
  const data = await api("/api/overview/summary?platform=" + PLATFORM);
  if (!isCurrent()) return;
  for (const [key, id] of Object.entries({ accounts: "stat-acc", monitors: "stat-mon", downloaded: "stat-dl", comments: "stat-cmt" })) {
    if ($(id)) $(id).textContent = fmtNum(data[key]);
  }
}
let POLL_RUNNING = false, POLL_TIMER = null, POLL_DELAY = 8000;
async function loop() {
  if (POLL_RUNNING) return;
  clearTimeout(POLL_TIMER);
  if (INFLIGHT > 0 || document.hidden || globalThis.CreatorHubWorkbench?.isInteracting?.()) {
    POLL_TIMER = setTimeout(loop, POLL_DELAY);
    return;
  }
  POLL_RUNNING = true;
  const tab = CURRENT_TAB, platform = PLATFORM;
  const failuresBefore = _apiFailures;
  const focused = document.activeElement;
  const editing = focused && focused.matches("input, textarea, select, .cs-trg, .dt-trg, [contenteditable='true']");
  const refreshers = {
    overview: [refreshOverviewSummary, refreshOverviewChart],
    accounts: [refreshAccounts],
    monitors: [refreshMonitors, refreshContents],
    comments: [refreshWatches, refreshComments],
    danmaku: [refreshDanmakuWatches, refreshDanmaku],
    autocomment: [refreshCommentRules, refreshCommentTasks],
    publish: [refreshPublish],
    collections: [refreshCollections],
    "risk-control": [refreshRiskCenter],
    queue: [refreshTaskQueue],
    hub: [refreshHubSummary],
  };
  const jobs = editing ? [] : (refreshers[tab] || []);
  if (tab !== "queue") jobs.push(refreshTaskQueueBadge);
  try {
    const results = await Promise.allSettled(jobs.map(fn => Promise.resolve().then(() => fn())));
    const failed = _apiFailures > failuresBefore || results.some(result => result.status === "rejected");
    POLL_DELAY = failed ? Math.min(POLL_DELAY * 2, 60000) : 8000;
    const panel = document.querySelector(`[data-panel="${tab}"]`);
    if (panel && CURRENT_TAB === tab && PLATFORM === platform) {
      panel.dataset.refreshState = failed ? "stale" : "ready";
      if (failed) panel.setAttribute("aria-label", "刷新暂时失败，显示上次结果，将自动重试");
      else panel.removeAttribute("aria-label");
    }
  } finally {
    POLL_RUNNING = false;
    POLL_TIMER = setTimeout(loop, CURRENT_TAB !== tab || PLATFORM !== platform ? 0 : POLL_DELAY);
  }
}

// initial skeletons while data loads
$("mon-table").innerHTML = skeleton(8);
$("content-table").innerHTML = skeleton(8);
$("sd-history-body").innerHTML = skeleton(8);
$("watch-table").innerHTML = skeleton(8);
$("comment-table").innerHTML = skeleton(6);
$("danmaku-watch-table").innerHTML = skeleton(8);
$("danmaku-table").innerHTML = skeleton(6);
$("collection-job-table").innerHTML = collectionTaskSkeleton(3);
$("collection-content-list").innerHTML = collectionResultSkeleton(4);
$("queue-table").innerHTML = skeleton(9);

// restore last-selected platform & global mode BEFORE restoring tab so global-mode tab guards apply cleanly
PLATFORM = (() => { try { const p = localStorage.getItem("dym-pf"); return ["xhs", "rednote", "douyin", "kuaishou", "shipinhao", "tiktok", "bilibili", "instagram", "twitter", "facebook", "youtube"].includes(p) ? p : "douyin"; } catch (e) { return "douyin"; } })();
GLOBAL_MODE = (() => { try { const g = localStorage.getItem("dym-global"); return g === null ? true : g === "1"; } catch (e) { return true; } })();
if ($("queue-platform") && GLOBAL_MODE) $("queue-platform").value = "all";

// restore last-selected section (default: 总览);旧版四个独立页已并入「账号管理」
const VALID_TABS = ["overview", "media-hub", "accounts", "risk-control", "queue", "collections", "monitors", "comments", "danmaku", "hub", "tiktok-tools", "youtube-tools", "publish", "autocomment", "share-download", "notifications", "settings"];
const LEGACY_HUB_TABS = ["myworks", "following", "fans", "dm"];
switchTab((() => {
  try {
    const hashTab = decodeURIComponent(location.hash.replace(/^#/, ""));
    if (hashTab === "agent" || (!hashTab && localStorage.getItem("dym-tab") === "agent")) {
      window.CreatorHubOpenAssistant = true;
      return "overview";
    }
    const candidate = VALID_TABS.includes(hashTab)
      ? hashTab
      : (() => {
          const t = localStorage.getItem("dym-tab");
          if (LEGACY_HUB_TABS.includes(t)) { HUB_TAB = t; return "hub"; }
          return VALID_TABS.includes(t) ? t : "overview";
        })();
    if (GLOBAL_MODE && SINGLE_PF_ONLY_TABS.includes(candidate)) return "overview";
    return candidate;
  } catch (e) { return "overview"; }
})());
switchHubTab(HUB_TAB);   // 恢复上次停留的子标签(我的作品/关注/粉丝/私信)
applyPlatformUI();
updateTaskQueuePlatformLabel();

setupMonitorInterval("t-interval", 300);
setupMonitorInterval("w-interval", 600);
setupMonitorInterval("d-w-interval", 0, true);
onTypeChange(); bindPubFilePicker(); onPubType(); populateWatchAccount(); applyDanmakuForm(); onAcMode(); loadSettings(); refreshAccounts(); refreshBrowserRuntimes(); refreshProxies(); refreshChannels(); window.CreatorHubProgress?.start?.(); loop();
enhanceAllSelects();   // 把所有原生 <select> 升级为美化下拉
enhanceAllMetaControls(); // 分组/标签：当前平台词库下拉，可搜索并新增
enhanceAllDateTime();  // 把 datetime-local 升级为自定义日期选择器
// 编辑弹窗和异步列表会动态插入控件；统一做渐进增强，避免新旧样式混用。
const controlEnhancer = new MutationObserver(records => {
  records.forEach(record => record.addedNodes.forEach(node => {
    if (node.nodeType !== 1) return;
    enhanceAllSelects(node);
    enhanceAllDateTime(node);
  }));
});
controlEnhancer.observe(document.body, { childList: true, subtree: true });

// shell 交互：浏览器前进/后退、平台键盘切换、长页面返回顶部。
window.addEventListener("hashchange", () => {
  const tab = decodeURIComponent(location.hash.replace(/^#/, ""));
  if (tab === "agent") {
    history.replaceState(null, "", "#" + CURRENT_TAB);
    window.dispatchEvent(new Event("creatorhub:assistant:open"));
    return;
  }
  if (VALID_TABS.includes(tab) && tab !== CURRENT_TAB) {
    switchTab(tab);
    globalThis.CreatorHubWorkbench?.restoreScroll?.();
  }
});
document.querySelector(".pswitch").addEventListener("keydown", e => {
  if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(e.key)) return;
  const buttons = [...document.querySelectorAll('.pswitch button[role="tab"]:not(.ps-overflow-hidden):not(.ps-idle-hidden)')];
  if (!buttons.length) return;
  let index = buttons.indexOf(document.activeElement);
  if (index < 0) return;
  e.preventDefault();
  if (e.key === "Home") index = 0;
  else if (e.key === "End") index = buttons.length - 1;
  else index = (index + (e.key === "ArrowRight" ? 1 : -1) + buttons.length) % buttons.length;
  buttons[index].focus();
  switchPlatform(buttons[index].dataset.pf);
});
let _backTopTick = false;
window.addEventListener("scroll", () => {
  if (_backTopTick) return;
  _backTopTick = true;
  requestAnimationFrame(() => {
    const visible = window.scrollY > 520, button = $("backtop");
    button.classList.toggle("show", visible);
    button.tabIndex = visible ? 0 : -1;
    button.setAttribute("aria-hidden", String(!visible));
    _backTopTick = false;
  });
}, { passive: true });
document.addEventListener("visibilitychange", () => { if (!document.hidden) loop(); });

function launchPlatformLogin(pf, mode = "main") {
  if (pf && pf !== "all" && PLATFORM !== pf) {
    PLATFORM = pf;
    ACCOUNTS = ALL_ACCOUNTS.filter(a => a.platform === PLATFORM);
    applyPlatformUI();
  }
  if (mode === "cookie") {
    toggleCookie();
    requestAnimationFrame(() => $("ck-val")?.focus());
    return;
  }
  const creator = mode === "creator";
  if (pf === "xhs") return creator ? startXhsCreatorLogin() : startXhsLogin();
  if (pf === "rednote") return creator ? startRednoteCreatorLogin() : startRednoteLogin();
  if (pf === "kuaishou") return creator ? startKsCreatorLogin() : startKsLogin();
  if (pf === "shipinhao") return startChannelsLogin();
  if (pf === "tiktok") return startTikTokLogin();
  if (pf === "bilibili") return startBilibiliLogin();
  if (pf === "instagram") return startInstagramLogin();
  if (pf === "twitter") return startTwitterLogin();
  if (pf === "facebook") return startFacebookLogin();
  if (pf === "youtube") return startYouTubeLogin();
  return creator ? startCreatorLogin() : startLogin();
}

// Narrow bridge for React islands; business mutations remain in their original handlers.
window.CreatorHubBridge = {
  selectPlatform: switchPlatform,
  getContext: () => ({ tab: CURRENT_TAB, platform: PLATFORM, scope: GLOBAL_MODE ? "all" : PLATFORM, globalMode: GLOBAL_MODE }),
  getAgentContext() {
    const job = CURRENT_TAB === "collections" && COLLECTION_JOB_ID
      ? COLLECTION_JOBS.find(item => item.id === COLLECTION_JOB_ID && item.platform === PLATFORM) : null;
    return { tab: CURRENT_TAB, platform: GLOBAL_MODE ? "all" : PLATFORM, title: (PAGE_META[CURRENT_TAB] || PAGE_META.overview).title,
      task: job ? { task_type: "collections", id: job.id, platform: job.platform, status: job.status } : null };
  },
  navigate: switchTab,
  openAccount: openAccountHub,
  openAggregatedDm,
  launchPlatformLogin,
  accountActions: {
    openBrowser: id => openAccountBrowser(Number(id)),
    relogin: (id, scope = "auto") => relogin(Number(id), scope),
    refreshProfile: id => refreshProfile(Number(id)),
    setProxy: id => setProxy(Number(id)),
    testProxy: id => testProxy(Number(id)),
    setBrowserBackend: id => setBrowserBackend(Number(id)),
    manageFingerprint: id => manageFingerprint(Number(id)),
    checkEnvironment: id => checkBrowserEnvironment(Number(id)),
    delAccount: id => delAccount(Number(id)),
    refreshAll: () => { refreshAccounts(); refreshProxies(); },
  },
  openQueue(state, allPlatforms = GLOBAL_MODE) {
    if ($("queue-platform")) { $("queue-platform").value = allPlatforms ? "all" : "current"; $("queue-platform")._csSync?.(); }
    $("queue-state").value = state; $("queue-state")._csSync?.();
    $("queue-query").value = ""; $("queue-type").value = ""; $("queue-type")._csSync?.();
    TASK_QUEUE_PAGE = 1;
    switchTab("queue", true);
  },
  async retry() {
    const refreshers = {
      overview: [refreshOverviewSummary, refreshOverviewChart, refreshTaskQueueBadge, () => window.dispatchEvent(new Event("creatorhub:matrix:refresh"))],
      "media-hub": [() => window.dispatchEvent(new Event("creatorhub:matrix:refresh"))],
      accounts: [refreshAccounts, refreshBrowserRuntimes, refreshProxies, () => window.dispatchEvent(new Event("creatorhub:matrix:refresh"))],
      monitors: [refreshMonitors, refreshContents], comments: [refreshWatches, refreshComments],
      danmaku: [refreshDanmakuWatches, refreshDanmaku], collections: [refreshCollections],
      queue: [refreshTaskQueue], publish: [refreshPublish, () => window.dispatchEvent(new Event("creatorhub:matrix:refresh"))],
      autocomment: [refreshCommentRules, refreshCommentTasks],
      notifications: [refreshChannels, refreshWebhooks], settings: [loadSettings],
      "risk-control": [refreshRiskCenter], hub: [refreshHubSummary, refreshHubPanel, () => window.dispatchEvent(new Event("creatorhub:matrix:refresh"))],
      "share-download": [refreshShareHistory],
    };
    await Promise.allSettled((refreshers[CURRENT_TAB] || []).map(fn => Promise.resolve().then(() => fn())));
  },
};
