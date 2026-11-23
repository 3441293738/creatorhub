// 16-autocomment-queue-boot.js — 自动评论规则与审核、统一任务队列、轮询主循环及 CreatorHubBridge。
// ─── 自动评论 ───
let AC_RULES = [];
const AC_MODE_T = { auto_reply: "自动回复", auto_comment: "自动评论" };
const AC_KIND_T = { self: "自己近期作品", work: "指定作品", creator: "指定博主", keyword: "关键词" };
const AC_TASK_ST = { draft: "草稿待审", pending: "排队中", doing: "发送中", uncertain: "结果待确认", done: "已发送", failed: "失败", canceled: "已取消" };
const AC_TASK_PILL = { draft: "downloading", pending: "pending", doing: "downloading", uncertain: "downloading", done: "done", failed: "failed", canceled: "invalid" };
let AC_TASKS = [];

function acKindOptions() {
  if ($("ac-mode").value === "auto_comment") {
    let html = '<option value="creator">指定博主</option>';
    if (["xhs", "bilibili", "instagram", "twitter"].includes(PLATFORM)) html += '<option value="keyword">搜索关键词</option>';
    return html;
  }
  return '<option value="self">自己近期作品</option><option value="work">指定作品</option>';
}
function onAcMode() {
  const k = $("ac-kind"); if (!k) return;
  const prev = k.value;
  k.innerHTML = acKindOptions();
  if ([...k.options].some(o => o.value === prev)) k.value = prev;
  onAcKind();
}
function onAcKind() {
  const mode = $("ac-mode").value, kind = $("ac-kind").value, xhs = PLATFORM === "xhs", bili = PLATFORM === "bilibili", ig = PLATFORM === "instagram", tw = PLATFORM === "twitter";
  let show = true, label = "目标", ph = "";
  if (mode === "auto_reply") {
    if (kind === "self") show = false;
    else { label = xhs ? "笔记链接 / id" : tw ? "推文链接 / status_id" : "作品链接 / id"; ph = xhs ? "explore 链接 / xhslink / note_id" : bili ? "视频链接 / BV或av号 / opus链接" : ig ? "instagram.com/p/... 或 /reel/... 链接" : tw ? "x.com/.../status/... 链接或数字推文 ID" : "作品链接 / 短链 / 数字 id"; }
  } else {
    if (kind === "keyword") { label = "搜索关键词"; ph = tw ? "例如:AI agent / #buildinpublic" : ig ? "例如:#travel / skincare" : "例如:露营装备 / 口红试色"; }
    else { label = xhs ? "博主主页 / id" : bili ? "UP 主空间主页 / mid" : (ig || tw) ? "创作者主页 / @username" : "博主主页 / sec_uid"; ph = xhs ? "主页链接 / xhslink / user_id" : bili ? "space.bilibili.com 链接 / mid" : ig ? "instagram.com/<username> 或 @username" : tw ? "x.com/<username> 或 @username" : "主页链接 / 短链 / sec_uid"; }
  }
  $("ac-target-wrap").style.display = show ? "" : "none";
  $("ac-target-label").textContent = label; $("ac-target").placeholder = ph;
  $("ac-reply-filter").style.display = mode === "auto_reply" ? "" : "none";
  csSyncAll();
}
function populateAcAccount() {
  const sel = $("ac-acc"); if (!sel) return;
  sel.innerHTML = accOptions(ACCOUNTS, `请选择${PF_NAME[PLATFORM] || "当前平台"}账号(必选)`);
  if (ACCOUNTS.length) sel.value = String(ACCOUNTS[0].id);
  csSyncAll();
}
async function addCommentRule() {
  const acc = $("ac-acc").value;
  if (!acc) { toast("请选择账号", "err"); return; }
  const templates = $("ac-templates").value.split("\n").map(s => s.trim()).filter(Boolean);
  if (!templates.length) { toast("请至少写一条文案模板(AI 失败时回退用)", "err"); return; }
  const body = {
    platform: PLATFORM, mode: $("ac-mode").value, account_id: +acc,
    target_kind: $("ac-kind").value, target: $("ac-target").value.trim(),
    templates, use_ai: $("ac-use-ai").checked, require_review: $("ac-review").checked,
    reply_filter: $("ac-reply-filter").value.trim(), skip_keywords: $("ac-skip").value.trim(),
    daily_cap: +$("ac-cap").value || 0, min_gap_seconds: +$("ac-gap").value || 60,
    max_per_run: +$("ac-max").value || 5, interval_seconds: +$("ac-interval").value || 1800, enabled: false,
  };
  await withBusy(evtBtn(), "创建中", async () => {
  try {
    await api("/api/comment-rules", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    $("ac-templates").value = ""; $("ac-target").value = "";
    $("ac-msg").textContent = "规则已创建(默认关闭),可在下方「试跑」预览文案 ✓";
    toast("规则已创建", "ok"); refreshCommentRules();
    globalThis.CreatorHubWorkbench?.completed?.("autocomment");
  } catch (e) { $("ac-msg").textContent = "失败: " + e.message; toast("创建失败:" + e.message, "err"); }
  });
}

// ─── 编辑规则:独立弹窗(复用 uimodal 壳)───
let EM_PF = "douyin";
function emKindOptions() {
  if ($("em-mode").value === "auto_comment") {
    let h = '<option value="creator">指定博主</option>';
    if (EM_PF === "xhs") h += '<option value="keyword">搜索关键词</option>';
    return h;
  }
  return '<option value="self">自己近期作品</option><option value="work">指定作品</option>';
}
function emOnMode() {
  const k = $("em-kind"); if (!k) return;
  const prev = k.value;
  k.innerHTML = emKindOptions();
  if ([...k.options].some(o => o.value === prev)) k.value = prev;
  emOnKind();
}
function emOnKind() {
  const mode = $("em-mode").value, kind = $("em-kind").value, xhs = EM_PF === "xhs";
  let show = true, label = "目标", ph = "";
  if (mode === "auto_reply") {
    if (kind === "self") show = false;
    else { label = xhs ? "笔记链接 / id" : "作品链接 / id"; ph = xhs ? "explore / xhslink / note_id" : "作品链接 / 短链 / 数字 id"; }
  } else {
    if (kind === "keyword") { label = "搜索关键词"; ph = "例如:露营装备 / 口红试色"; }
    else { label = xhs ? "博主主页 / id" : "博主主页 / sec_uid"; ph = xhs ? "主页 / xhslink / user_id" : "主页 / 短链 / sec_uid"; }
  }
  $("em-target-wrap").style.display = show ? "" : "none";
  $("em-target-label").textContent = label; $("em-target").placeholder = ph;
  $("em-filter-wrap").style.display = mode === "auto_reply" ? "" : "none";
  $("em-reply-filter").style.display = mode === "auto_reply" ? "" : "none";
  csSyncAll();
}
function editRule(id) {
  const r = AC_RULES.find(x => x.id === id); if (!r) return;
  EM_PF = r.platform;
  const accOpts = accOptions(ACCOUNTS.filter(a => a.platform === EM_PF), `请选择${PF_NAME[EM_PF] || "对应平台"}账号`);
  new Promise(res => {
    _uiResolve = res; _uiCancelVal = null;
    _uiGetVal = () => ({
      name: $("em-name").value.trim(), mode: $("em-mode").value,
      target_kind: $("em-kind").value, target: $("em-target").value.trim(),
      account_id: +$("em-acc").value || null,
      templates: $("em-templates").value.split("\n").map(s => s.trim()).filter(Boolean),
      use_ai: $("em-use-ai").checked, require_review: $("em-review").checked,
      reply_filter: $("em-reply-filter").value.trim(), skip_keywords: $("em-skip").value.trim(),
      daily_cap: +$("em-cap").value || 0, min_gap_seconds: +$("em-gap").value || 60,
      max_per_run: +$("em-max").value || 5, interval_seconds: +$("em-interval").value || 1800,
    });
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
      <fieldset class="ui-form-group"><legend>文案与审核</legend>
        <div><label class="field" for="em-templates">文案模板</label><textarea id="em-templates" rows="4" aria-describedby="em-templates-hint"></textarea>
          <p class="field-help" id="em-templates-hint">每行一条，支持 {nick}、{kw} 和随机词组 {好|不错|赞}。</p></div>
        <label class="ui-toggle"><span><b>AI 生成文案</b><small>生成失败时使用上面的模板</small></span><input type="checkbox" role="switch" id="em-use-ai"></label>
        <label class="ui-toggle"><span><b>发布前审核</b><small>只生成草稿，由你确认后再发送</small></span><input type="checkbox" role="switch" id="em-review"></label>
        <div class="form-grid" id="em-filter-wrap">
          <div><label class="field" for="em-reply-filter">只回复包含关键词的评论</label><input id="em-reply-filter" placeholder="留空不限制"></div>
          <div><label class="field" for="em-skip">跳过关键词</label><input id="em-skip" placeholder="多个词用逗号分隔"></div>
        </div>
      </fieldset>
      <fieldset class="ui-form-group"><legend>执行频率</legend>
        <div class="form-grid">
          <div><label class="field" for="em-cap">每日上限（条）</label><input type="number" id="em-cap" min="0" inputmode="numeric"></div>
          <div><label class="field" for="em-gap">最小间隔（秒）</label><input type="number" id="em-gap" min="1" inputmode="numeric"></div>
          <div><label class="field" for="em-max">每轮上限（条）</label><input type="number" id="em-max" min="1" inputmode="numeric"></div>
          <div><label class="field" for="em-interval">检查频率</label><select id="em-interval">${numericSelectOptions(r.interval_seconds || 1800, [[900, "每 15 分钟"], [1800, "每 30 分钟"], [3600, "每小时"]], " 秒")}</select></div>
        </div>
      </fieldset>`;
    // 回填值
    $("em-name").value = r.name || "";
    $("em-mode").value = r.mode; emOnMode();
    $("em-kind").value = r.target_kind; emOnKind();
    if ($("em-acc").querySelector(`option[value="${r.account_id}"]`)) $("em-acc").value = String(r.account_id);
    $("em-target").value = r.mode === "auto_comment"
      ? (r.target_kind === "keyword" ? r.keyword : r.sec_uid)
      : (r.target_kind === "work" ? r.aweme_id : "");
    $("em-templates").value = (r.templates || []).join("\n");
    $("em-use-ai").checked = !!r.use_ai;
    $("em-review").checked = !!r.require_review;
    $("em-reply-filter").value = r.reply_filter || "";
    $("em-skip").value = r.skip_keywords || "";
    $("em-cap").value = r.daily_cap; $("em-gap").value = r.min_gap_seconds;
    $("em-max").value = r.max_per_run;
    if ([...$("em-interval").options].some(o => o.value === String(r.interval_seconds))) $("em-interval").value = String(r.interval_seconds);
    _uiOpen("编辑规则 #" + id, "调整互动对象、文案和执行频率。修改目标或关键词后会重新解析。", {
      okText: "保存修改", wide: true, icon: "i-automation",
      submit: val => {
        if (!val.templates.length) uiEditorError("请至少写一条文案模板", "em-templates");
        if (!val.account_id) uiEditorError("请选择与规则平台一致的账号", "em-acc");
        return api("/api/comment-rules/" + id, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(val) });
      },
    });
    ["em-mode", "em-kind", "em-acc", "em-interval"].forEach(idd => { const el = $(idd); if (el) enhanceSelect(el); });
  }).then(async val => {
    if (!val) return;   // 取消
    toast("规则已更新", "ok"); refreshCommentRules();
  });
}
async function refreshCommentRules() {
  const isCurrent = beginViewRequest("comment-rules");
  if (!$("ac-rule-table")) return;
  const rows = await api("/api/comment-rules?platform=" + PLATFORM);
  if (!isCurrent()) return;
  if ($("tb-ac")) $("tb-ac").textContent = rows.length;
  AC_RULES = rows;
  $("ac-rule-table").innerHTML = rows.map(r => {
    const tgt = r.mode === "auto_comment"
      ? (r.target_kind === "keyword" ? "#" + esc(r.keyword) : esc((r.sec_uid || "").slice(0, 14)))
      : (r.target_kind === "work" ? esc(r.aweme_id) : "");
    const acc = (ACCOUNTS.find(a => a.id === r.account_id) || {}).nickname || ("#" + r.account_id);
    const tags = [r.use_ai ? "AI文案" : "", r.require_review ? "草稿审核" : ""].filter(Boolean)
      .map(x => `<span class="pill downloading" style="margin-left:4px;font-size:10px">${x}</span>`).join("");
    return `<tr>
      <td><b>${esc(r.name)}</b>${tags}</td>
      <td>${AC_MODE_T[r.mode] || r.mode}</td>
      <td class="wrap" style="max-width:180px">${AC_KIND_T[r.target_kind] || r.target_kind}${tgt ? `<div class="mut">${tgt}</div>` : ""}</td>
      <td>${esc(acc)}</td>
      <td class="mut num">${r.daily_cap}/日 · ${Math.round(r.interval_seconds / 60)}分</td>
      <td class="mut num">${r.last_run_at ? new Date(r.last_run_at + "Z").toLocaleString() : "—"}${r.last_error ? ` <span class="warn-ic" title="${esc(r.last_error)}">${ic("i-info")}</span>` : ""}${autoRunHint(r.next_auto_run_at)}</td>
      <td><span class="pill ${r.enabled ? "done" : "paused"}">${r.enabled ? "运行中" : "已停用"}</span></td>
      <td class="acttd">
        <button class="ghost sm" onclick="runRule(${r.id})">试跑</button>
        <button class="ghost sm" onclick="editRule(${r.id})">编辑</button>
        <button class="ghost sm" onclick="toggleRule(${r.id}, ${r.enabled ? "false" : "true"})">${r.enabled ? "停用" : "启用"}</button>
        <button class="ghost sm danger" onclick="delRule(${r.id})">${ic("i-trash")}删除</button>
      </td></tr>`;
  }).join("") || empty(8, "暂无评论规则", "i-msg", "创建规则后可先试跑，确认内容再启用。", "autocomment");
}
async function toggleRule(id, en) {
  try { await api("/api/comment-rules/" + id, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ enabled: en }) }); toast(en ? "已启用" : "已停用", "ok"); refreshCommentRules(); }
  catch (e) { toast("操作失败:" + e.message, "err"); }
}
async function runRule(id) {
  const btn = evtBtn();
  toast("试跑中…正在抓取目标评论,可能要十几秒", "info", 8000);
  await withBusy(btn, "试跑中", async () => {
    try {
      const r = await api("/api/comment-rules/" + id + "/run-now", { method: "POST" });
      if (!r.ok) toast("未生成:" + (r.error || ""), "err", 7000);
      else if (r.created > 0) toast(`生成 ${r.created} 条${r.manual_only ? "人工发布草稿(未调用评论接口)" : r.review ? "草稿(待人工通过)" : "任务"}(发现 ${r.candidates} 个目标)`, "ok", 6000);
      else toast(`发现 ${r.candidates} 个目标,生成 0 条` + (r.note ? `:${r.note}` : "(可能都已生成过)"), "info", 9000);
    } catch (e) { toast("试跑失败:" + e.message, "err"); }
  });
  refreshCommentRules(); refreshCommentTasks();
}
async function delRule(id) {
  if (!await uiConfirm({ title: "删除规则", message: "删除该规则及其未发送任务?", okText: "删除", danger: true })) return;
  try { await api("/api/comment-rules/" + id, { method: "DELETE" }); toast("已删除", "ok"); refreshCommentRules(); refreshCommentTasks(); }
  catch (e) { toast("删除失败:" + e.message, "err"); }
}
async function refreshCommentTasks() {
  const isCurrent = beginViewRequest("comment-tasks");
  if (!$("ac-task-table")) return;
  const st = $("ac-task-filter") ? $("ac-task-filter").value : "";
  const rows = await api("/api/comment-tasks?platform=" + PLATFORM + (st ? "&status=" + st : ""));
  if (!isCurrent()) return;
  AC_TASKS = rows;
  const drafts = rows.filter(t => t.status === "draft");
  if ($("ac-draft-bar")) {
    $("ac-draft-bar").style.display = drafts.length ? "flex" : "none";
    if (drafts.length) $("ac-draft-count").textContent = `有 ${drafts.length} 条草稿待审核——逐条「通过/编辑」,或一键全部通过后由引擎按节流发出`;
  }
  renderCommentTaskRows();
}
function renderCommentTaskRows() {
  if (!$("ac-task-table")) return;
  const query = (($("ac-task-search") && $("ac-task-search").value) || "").trim().toLocaleLowerCase();
  const filtered = AC_TASKS.filter(t => {
    if (!query) return true;
    const accName = (ACCOUNTS.find(a => a.id === t.account_id) || {}).nickname || "";
    return [t.content, t.aweme_id, t.target_nick, accName].filter(Boolean).join(" ").toLocaleLowerCase().includes(query);
  });
  if ($("ac-task-count")) $("ac-task-count").textContent = AC_TASKS.length ? `显示 ${filtered.length} / ${AC_TASKS.length}` : "";
  $("ac-task-table").innerHTML = filtered.map(t => {
    const isDraft = t.status === "draft", canSend = t.status === "pending" || t.status === "failed";
    const accName = (ACCOUNTS.find(a => a.id === t.account_id) || {}).nickname || (t.account_id ? `账号 #${t.account_id}` : "");
    return `<tr>
    <td class="wrap" style="max-width:260px">${esc(t.content)}</td>
    <td><code title="${esc(t.aweme_id || "")}">${esc((t.aweme_id || "—").slice(0, 18))}</code>${accName ? `<div class="mut">${esc(accName)}</div>` : ""}</td>
    <td>${t.target_comment_id ? "回复 " + esc(t.target_nick || "") : "顶层评论"}</td>
    <td class="mut num">${t.scheduled_at ? new Date(t.scheduled_at + "Z").toLocaleString() : "尽快"}${t.next_allowed_at ? `<div class="mut" title="预约保持不变，同时等待风控间隔结束">风控最早 ${esc(riskTime(t.next_allowed_at))}</div>` : ""}</td>
    <td class="mut">${t.method === "browser" ? "浏览器页面" : t.method === "api" ? "API 兼容模式" : t.method === "manual" ? "人工草稿" : "—"}</td>
    <td><span class="pill ${AC_TASK_PILL[t.status] || "pending"}">${AC_TASK_ST[t.status] || t.status}</span>${t.error ? ` <span class="warn-ic" title="${esc(t.error)}">${ic("i-info")}</span>` : ""}</td>
    <td class="acttd">
      ${isDraft ? `<button class="sm" onclick="approveTask(${t.id})">通过</button>` : ""}
      ${(isDraft || canSend) ? `<button class="ghost sm" onclick="editTaskContent(${t.id})">编辑</button>` : ""}
      ${canSend ? `<button class="ghost sm" onclick="runTask(${t.id})">立即发</button>` : ""}
      ${(isDraft || canSend) ? `<button class="ghost sm" onclick="cancelTask(${t.id})">${isDraft ? "弃用" : "取消"}</button>` : ""}
      ${t.status === "uncertain" ? `<button class="ghost sm" onclick="resolveTaskResult('comments',${t.id})">核对结果</button>` : t.status !== "doing" ? `<button class="ghost sm danger" onclick="delTask(${t.id})">${ic("i-trash")}删除</button>` : ""}
    </td></tr>`;
  }).join("") || empty(7, query ? "没有匹配的评论任务" : "暂无评论任务", "i-msg", query ? "调整搜索词或清除筛选" : "启用规则或点「试跑」后,这里会出现待发评论");
}
async function approveTask(id) {
  try { await api("/api/comment-tasks/" + id + "/approve", { method: "POST" }); toast("已通过,转入待发队列", "ok"); refreshCommentTasks(); }
  catch (e) { toast("操作失败:" + e.message, "err"); }
}
async function approveAllDrafts() {
  const platform = PLATFORM;
  const ids = AC_TASKS.filter(t => t.status === "draft").map(t => t.id);
  if (!ids.length) return;
  if (!await uiConfirm({ title: "审核当前列表草稿", message: `通过当前列表中 ${PF_NAME[platform] || platform} 的 ${ids.length} 条草稿？通过后引擎按每账号每日上限和最小间隔陆续发出。`, okText: "通过这些草稿" })) return;
  try { const r = await api("/api/comment-tasks/batch-approve", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ids, platform }) }); toast(`已通过 ${r.approved} 条`, "ok"); refreshCommentTasks(); }
  catch (e) { toast("操作失败:" + e.message, "err"); }
}
async function editTaskContent(id) {
  const t = AC_TASKS.find(x => x.id === id); if (!t) return;
  const v = await uiPrompt({ title: "编辑评论文案", hint: "保存只更新文案，不会发送评论。", value: t.content || "", multiline: true, rows: 3,
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
  try { await api("/api/comment-tasks/" + id + "/cancel", { method: "POST" }); toast("已取消", "ok"); refreshCommentTasks(); }
  catch (e) { toast("操作失败:" + e.message, "err"); }
}
async function delTask(id) {
  if (!await uiConfirm({ title: "删除评论任务", message: "确认删除这条评论任务或记录吗？", okText: "删除", danger: true })) return;
  try { await api("/api/comment-tasks/" + id, { method: "DELETE" }); toast("已删除", "ok"); refreshCommentTasks(); }
  catch (e) { toast("删除失败:" + e.message, "err"); }
}

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
  const selectable = ["collections", "publishes", "comments", "actions"].includes(item.queue_type)
    && !["done", "canceled", "skipped", "uncertain"].includes(item.status);
  return `<tr>
    <td><input type="checkbox" class="queue-select" data-queue-type="${esc(item.queue_type)}" data-task-id="${Number(item.id)}" aria-label="选择任务 ${Number(item.id)}" onchange="toggleTaskQueueItem(this)"${selectable ? "" : " disabled"}${TASK_QUEUE_SELECTED.has(stopKey) ? " checked" : ""}></td>
    <td><span class="pill q bare">${esc(item.queue_label)}</span><small class="mut" style="display:block;margin-top:5px">${esc(PF_NAME[item.platform] || item.platform || "—")}</small></td>
    <td><div class="queue-copy"><b title="${esc(item.title)}">${esc(item.title)}</b>${item.detail ? `<small>${esc(item.detail)}</small>` : ""}</div></td>
    <td><div class="queue-account"><b>${esc(account)}</b>${item.account_id ? `<small>ID ${Number(item.account_id)}</small>` : ""}</div></td>
    <td><div class="queue-time">${scheduled || "尽快执行"}${created}</div></td>
    <td><span class="pill ${stateMeta[1]}">${esc(stateMeta[0])}</span><small class="mut" style="display:block;margin-top:5px">${esc(rawStatus)}</small></td>
    <td><div class="queue-reason${reasonClass}">${esc(reason)}${signal}${nextAllowed}</div></td>
    <td class="acttd">${item.status === "uncertain" ? `<button type="button" class="ghost sm" onclick="resolveTaskResult(${jsArg(item.queue_type)},${Number(item.id)})">核对结果</button>` : ""}<button type="button" class="ghost sm" onclick="openTaskQueueSource(${jsArg(item.source_tab)})">${esc(taskQueueSourceLabel(item.source_tab))}</button></td>
  </tr>`;
}

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
    body.innerHTML = data.items?.length
      ? data.items.map(taskQueueRow).join("")
      : empty(8, "当前筛选范围内没有任务", "i-inbox", "切换状态或平台范围后再查看");
    updateTaskQueueSelection();
    renderTaskQueueSummary(data.summary || {});
    renderTaskQueuePager(data);
  } catch (e) {
    if (!isCurrent()) return;
    body.innerHTML = empty(8, "任务队列加载失败", "i-info", e.message || "请稍后重试");
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
    const params = new URLSearchParams({ platform: GLOBAL_MODE ? "all" : PLATFORM, state: "active", page_size: "1" });
    const data = await api("/api/task-queue?" + params.toString());
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
$("queue-table").innerHTML = skeleton(8);

// restore last-selected platform & global mode BEFORE restoring tab so global-mode tab guards apply cleanly
PLATFORM = (() => { try { const p = localStorage.getItem("dym-pf"); return ["xhs", "rednote", "douyin", "kuaishou", "shipinhao", "tiktok", "bilibili", "instagram", "twitter"].includes(p) ? p : "douyin"; } catch (e) { return "douyin"; } })();
GLOBAL_MODE = (() => { try { const g = localStorage.getItem("dym-global"); return g === null ? true : g === "1"; } catch (e) { return true; } })();
if ($("queue-platform") && GLOBAL_MODE) $("queue-platform").value = "all";

// restore last-selected section (default: 总览);旧版四个独立页已并入「账号管理」
const VALID_TABS = ["overview", "media-hub", "accounts", "risk-control", "queue", "collections", "monitors", "comments", "danmaku", "hub", "tiktok-tools", "publish", "autocomment", "share-download", "notifications", "settings"];
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
onTypeChange(); bindPubFilePicker(); onPubType(); populateWatchAccount(); applyDanmakuForm(); onAcMode(); loadSettings(); refreshAccounts(); refreshBrowserRuntimes(); refreshProxies(); refreshChannels(); loop();
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
  e.preventDefault();
  const buttons = [...document.querySelectorAll(".pswitch button")];
  let index = buttons.indexOf(document.activeElement);
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
