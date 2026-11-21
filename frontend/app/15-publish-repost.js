// 15-publish-repost.js — 多平台图文/视频发布编辑器、已发布作品管理与跨平台转发。
// ─── 多平台发布 ───
const BILI_PARTITION_CACHE = new Map();
function onBiliCopyrightChange() {
  const wrap = $("pub-bili-source-wrap");
  if (wrap) wrap.style.display = $("pub-bili-copyright")?.value === "2" ? "" : "none";
}
async function loadBiliPartitions(accountId) {
  const sel = $("pub-bili-tid");
  if (!sel || !accountId || PLATFORM !== "bilibili") return;
  const current = sel.value || "21";
  let data = BILI_PARTITION_CACHE.get(Number(accountId));
  if (!data) {
    try {
      data = await api("/api/accounts/" + Number(accountId) + "/bili/partitions");
      if (data && Array.isArray(data.partitions) && data.partitions.length) {
        BILI_PARTITION_CACHE.set(Number(accountId), data);
      }
    } catch (_) { return; }
  }
  const groups = Array.isArray(data?.partitions) ? data.partitions : [];
  const opts = [];
  for (const g of groups) {
    const pName = String(g.name || "");
    const children = Array.isArray(g.children) && g.children.length ? g.children : [g];
    for (const c of children) {
      const tid = Number(c.tid || c.id || 0);
      if (!tid) continue;
      const cName = String(c.name || "");
      opts.push({ tid: String(tid), label: pName && pName !== cName ? `${pName} · ${cName}` : cName });
    }
  }
  if (!opts.length) return;
  sel.innerHTML = opts.map(o => `<option value="${esc(o.tid)}">${esc(o.label)}</option>`).join("");
  sel.value = opts.some(o => o.tid === String(current)) ? String(current) : opts[0].tid;
  sel._csSync?.();
}
function populatePubAcc() {
  const sel = $("pub-acc"); if (!sel) return;
  // 小红书发布需创作者号;其余平台有登录态即可(走浏览器自动化)
  const list = PLATFORM === "xhs" ? ACCOUNTS.filter(a => a.has_creator) : ACCOUNTS;
  const ph = list.length ? "选择发布账号"
    : (PLATFORM === "kuaishou" ? "请先完成「快手扫码登录」"
      : PLATFORM === "douyin" ? "请先完成「抖音扫码/创作者登录」"
      : PLATFORM === "tiktok" ? "请先完成「TikTok 登录」"
      : PLATFORM === "bilibili" ? "请先完成「哔哩哔哩扫码登录」"
      : PLATFORM === "instagram" ? "请先完成「Instagram 登录」"
      : PLATFORM === "twitter" ? "请先完成「Twitter 登录」"
      : PLATFORM === "rednote" ? "请先完成「海外小红书创作者登录」"
      : "请先完成「小红书创作者登录」");
  sel.innerHTML = accOptions(list, ph);
  if (list.length) sel.value = String(list[0].id);
  if (!sel._biliBound) {
    sel._biliBound = true;
    sel.addEventListener("change", () => { if (PLATFORM === "bilibili" && sel.value) loadBiliPartitions(sel.value); });
  }
  if (PLATFORM === "bilibili" && sel.value) loadBiliPartitions(sel.value);
}
let pubFilesDT = new DataTransfer();
function onPubType() {
  const v = $("pub-type").value, inp = $("pub-files"), lbl = $("pub-files-label");
  if (!inp) return;
  const ig = PLATFORM === "instagram", tw = PLATFORM === "twitter";
  if (v === "video") {
    inp.accept = ig ? "video/*,image/*" : "video/*";
    inp.multiple = ig;
    lbl.textContent = ig ? "选择视频文件（单个 Reels/Story，或多选最多 10 项混合轮播）" : tw ? "选择视频或 GIF（单个，留空可发纯文本推文）" : "选择视频文件(单个)";
  } else {
    inp.accept = ig ? "image/*,video/*" : "image/*";
    inp.multiple = true;
    lbl.textContent = ig ? "选择图片/视频（可多选，最多 10 项轮播）" : tw ? "选择图片（可多选，最多 4 张；留空可发纯文本推文）" : "选择图片(可多选,最多 18 张)";
  }
  pubFilesClear();
}
function pubFilesClear() { pubFilesDT = new DataTransfer(); _pubSync(); }
function _pubSync() { const inp = $("pub-files"); if (inp) inp.files = pubFilesDT.files; renderPubFiles(); }
function pubAddFiles(files) {
  const isVideo = $("pub-type").value === "video";
  const ig = PLATFORM === "instagram", tw = PLATFORM === "twitter";
  const maxItems = tw ? 4 : ig ? 10 : 18;
  for (const f of files) {
    if (isVideo && !ig) { pubFilesDT = new DataTransfer(); pubFilesDT.items.add(f); break; }
    if ([...pubFilesDT.files].some(x => x.name === f.name && x.size === f.size)) continue;
    if (pubFilesDT.files.length >= maxItems) break;
    pubFilesDT.items.add(f);
  }
  _pubSync();
}
function pubRemoveFile(i) {
  const dt = new DataTransfer();
  [...pubFilesDT.files].forEach((f, idx) => { if (idx !== i) dt.items.add(f); });
  pubFilesDT = dt; _pubSync();
}
let PUB_THUMB_URLS = [];
function renderPubFiles() {
  const box = $("pub-filelist"); if (!box) return;
  PUB_THUMB_URLS.forEach(url => URL.revokeObjectURL(url)); PUB_THUMB_URLS = [];
  box.innerHTML = [...pubFilesDT.files].map((f, i) => {
    const url = f.type.startsWith("image/") ? URL.createObjectURL(f) : "";
    if (url) PUB_THUMB_URLS.push(url);
    const thumb = f.type.startsWith("image/")
      ? `<img src="${url}" alt="">`
      : `<span class="fp-ph">${ic("i-play")}</span>`;
    return `<span class="fp-chip">${thumb}<span title="${esc(f.name)}">${esc(f.name)}</span><button type="button" onclick="pubRemoveFile(${i})" aria-label="移除">${ic("i-x")}</button></span>`;
  }).join("");
  globalThis.CreatorHubWorkbench?.previewUpdated?.();
}
function bindPubFilePicker() {
  const inp = $("pub-files"), zone = $("pub-drop");
  if (!inp || !zone) return;
  inp.addEventListener("change", e => { pubAddFiles(e.target.files); });
  ["dragenter", "dragover"].forEach(ev => zone.addEventListener(ev, e => { e.preventDefault(); zone.classList.add("drag"); }));
  ["dragleave", "drop"].forEach(ev => zone.addEventListener(ev, e => { e.preventDefault(); if (ev === "dragleave" && zone.contains(e.relatedTarget)) return; zone.classList.remove("drag"); }));
  zone.addEventListener("drop", e => { if (e.dataTransfer && e.dataTransfer.files.length) pubAddFiles(e.dataTransfer.files); });
}
let PUB_SUBMITTING = false, PUB_UPLOAD_CACHE = null;
function publishFormPayload() {
  const tiktok = PLATFORM === "tiktok", bili = PLATFORM === "bilibili", ig = PLATFORM === "instagram", tw = PLATFORM === "twitter";
  const mediaType = $("pub-type").value;
  let biliOptions, igOptions, twOptions;
  if (bili) {
    const copyright = +($("pub-bili-copyright")?.value || 1);
    const source = ($("pub-bili-source")?.value || "").trim();
    if (mediaType === "video" && copyright === 2 && !source) {
      throw new Error("转载稿件请填写转载来源");
    }
    const stmtVal = +($("pub-bili-statement")?.value || -1);
    biliOptions = {
      tid: +($("pub-bili-tid")?.value || 21),
      copyright,
      source,
      creation_statement_id: stmtVal,
      declaration_index: stmtVal,
      recreate: +($("pub-bili-recreate")?.value || -1),
      up_close_danmu: $("pub-bili-close-danmu")?.value === "1",
      up_close_reply: $("pub-bili-close-reply")?.value === "1",
      up_selection_reply: $("pub-bili-selection-reply")?.value === "1",
      is_only_self: $("pub-bili-visibility")?.value === "private" ? 1 : 0,
    };
  }
  if (tw) {
    twOptions = {
      reply_permission: $("pub-tw-reply-perm")?.value || "everyone",
      long_text: $("pub-tw-long")?.value === "1",
      first_comment: ($("pub-tw-first-comment")?.value || "").trim(),
    };
  }
  if (ig) {
    igOptions = {
      post_kind: $("pub-ig-kind")?.value || "auto",
      share_to_feed: $("pub-ig-share-feed")?.value !== "0",
    };
  }
  return { account_id: +$("pub-acc").value, media_type: mediaType,
    title: $("pub-title").value.trim(), desc: $("pub-desc").value,
    topics: $("pub-topics").value.trim(), scheduled_at: scheduleToApi($("pub-when").value || null),
    location: $("pub-location") ? $("pub-location").value.trim() : "",
    visibility: bili ? ($("pub-bili-visibility")?.value || "public") : (tiktok ? $("pub-tk-visibility").value : $("pub-visibility") ? $("pub-visibility").value : "public"),
    allow_save: $("pub-allowsave") ? $("pub-allowsave").value !== "0" : true,
    allow_comment: bili ? ($("pub-bili-close-reply")?.value !== "1") : ig ? ($("pub-ig-comment")?.value !== "0") : tw ? (($("pub-tw-reply-perm")?.value || "everyone") !== "mentioned") : (tiktok ? $("pub-tk-comment").value !== "0" : true),
    allow_duet: tiktok ? $("pub-tk-duet").value !== "0" : true,
    allow_stitch: tiktok ? $("pub-tk-stitch").value !== "0" : true,
    ...(bili ? { bili_options: biliOptions } : {}),
    ...(ig ? { ig_options: igOptions } : {}),
    ...(tw ? { tw_options: twOptions } : {}) };
}
async function addPublish() {
  if (PUB_SUBMITTING) return;
  const acc = $("pub-acc").value;
  if (!acc) { toast("请选择" + (PF_NAME[PLATFORM] || "发布") + "账号", "err"); return; }
  const files = Array.from($("pub-files").files);
  if (!files.length && PLATFORM !== "twitter") { toast("请先选择要发布的文件", "err"); return; }
  if (!files.length && PLATFORM === "twitter" && !($("pub-title").value.trim() || $("pub-desc").value.trim() || $("pub-topics").value.trim())) { toast("请填写推文文案或选择媒体文件", "err"); return; }
  let body;
  try { body = publishFormPayload(); }
  catch (e) { $("pub-msg").textContent = e.message; toast(e.message, "err"); return; }
  const btn = evtBtn();
  PUB_SUBMITTING = true;
  $("pub-msg").textContent = "上传中…";
  try { await withBusy(btn, "提交中", async () => {
    try {
      if (!files.length) {
        PUB_UPLOAD_CACHE = { files: [], paths: [] };
      } else if (!PUB_UPLOAD_CACHE || PUB_UPLOAD_CACHE.files.length !== files.length ||
          !PUB_UPLOAD_CACHE.files.every((file, index) => file === files[index])) {
        const fd = new FormData(); for (const f of files) fd.append("files", f);
        const up = await api("/api/publish/upload", { method: "POST", body: fd });
        PUB_UPLOAD_CACHE = { files, paths: (up.files || []).map(f => f.path) };
      }
      const paths = PUB_UPLOAD_CACHE.paths;
      await api("/api/publish", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ...body, media_paths: paths }),
      });
      // A late response must not erase edits made during a slow upload.
      let unchanged = false;
      try { unchanged = JSON.stringify(publishFormPayload()) === JSON.stringify(body) &&
          Array.from($("pub-files").files).length === files.length &&
          Array.from($("pub-files").files).every((file, index) => file === files[index]); } catch (_) {}
      if (unchanged) {
        pubFilesClear(); $("pub-title").value = ""; $("pub-desc").value = ""; $("pub-topics").value = ""; $("pub-when").value = ""; if ($("pub-location")) $("pub-location").value = ""; if ($("pub-tw-first-comment")) $("pub-tw-first-comment").value = ""; dtSyncAll();
        PUB_UPLOAD_CACHE = null;
      }
      $("pub-msg").textContent = body.scheduled_at ? "已加入定时队列 ✓" : "已加入发布队列 ✓";
      toast("已加入发布队列", "ok");
    } catch (e) { $("pub-msg").textContent = "提交未确认: " + e.message + "；重试相同内容会沿用原提交编号。"; toast("提交未确认:" + e.message, "err"); }
  }); } finally { PUB_SUBMITTING = false; }
  refreshPublish();
}
const PUB_ST = { draft: "待确认预约", pending: "排队中", publishing: "发布中", uncertain: "结果待确认", done: "已发布", failed: "失败", canceled: "已取消" };
const PUB_PILL = { draft: "pending", pending: "pending", publishing: "downloading", uncertain: "downloading", done: "done", failed: "failed", canceled: "invalid" };
async function editPublish(id) {
  const task = PUBLISH_TASKS.find(x => x.id === id); if (!task) return;
  const accounts = ACCOUNTS.filter(a => a.platform === task.platform);
  const accountOptions = accounts.map(a =>
    `<option value="${a.id}">${esc(a.nickname)}${a.has_creator ? " · 创作号" : ""}</option>`
  ).join("");
  const value = await new Promise(res => {
    _uiResolve = res; _uiCancelVal = null;
    const curBili = task.bili_options || {};
    _uiGetVal = () => ({
      account_id: +$("ep-account").value,
      title: $("ep-title").value.trim(),
      desc: $("ep-desc").value,
      topics: $("ep-topics").value.trim(),
      scheduled_at: $("ep-when").value || null,
      location: $("ep-location") ? $("ep-location").value.trim() : "",
      visibility: $("ep-visibility") ? $("ep-visibility").value : "public",
      allow_save: $("ep-allowsave") ? $("ep-allowsave").value !== "0" : true,
      allow_comment: $("ep-tk-comment") ? $("ep-tk-comment").value !== "0" : true,
      allow_duet: $("ep-tk-duet") ? $("ep-tk-duet").value !== "0" : true,
      allow_stitch: $("ep-tk-stitch") ? $("ep-tk-stitch").value !== "0" : true,
      ...(task.platform === "bilibili" ? {
        bili_options: {
          ...curBili,
          tid: +($("ep-bili-tid")?.value || curBili.tid || 21),
          copyright: +($("ep-bili-copyright")?.value || curBili.copyright || 1),
          source: ($("ep-bili-source")?.value ?? curBili.source ?? "").trim(),
          is_only_self: ($("ep-visibility")?.value || "public") === "private" ? 1 : 0,
        },
      } : {}),
    });
    const editCap = task.platform === "twitter" ? (task.tw_options?.long_text ? 4000 : 280) : task.platform === "instagram" ? 2200 : task.platform === "bilibili" ? 80 : task.platform === "rednote" ? 40 : task.platform === "douyin" ? 30 : task.platform === "shipinhao" ? 16 : 20;
    const showVisOpts = ["douyin", "xhs", "rednote", "kuaishou"].includes(task.platform);
    $("ui-body").innerHTML = `
      <div><label class="field" for="ep-account">发布账号</label>
        <select id="ep-account">${accountOptions}</select></div>
      <div><label class="field" for="ep-title">标题（≤${editCap} 字）</label>
        <input id="ep-title" maxlength="${editCap}" value="${esc(task.title || "")}"></div>
      <div><label class="field" for="ep-desc">正文</label>
        <textarea id="ep-desc" rows="4">${esc(task.desc || "")}</textarea></div>
      <div><label class="field" for="ep-topics">话题</label>
        <input id="ep-topics" value="${esc(task.topics || "")}" placeholder="逗号分隔，不用带 #"></div>
      <div><label class="field" for="ep-when">定时发布</label>
        <input type="datetime-local" id="ep-when" aria-label="定时发布（留空=尽快发）"></div>
      ${task.platform === "shipinhao" ? `<div><label class="field" for="ep-location">位置</label>
        <input id="ep-location" value="${esc(task.location || "")}" placeholder="城市或地点名"></div>` : ""}
      ${showVisOpts ? `<fieldset class="publish-permissions">
        <legend>互动与权限</legend>
        <div class="publish-permission"><label class="field" for="ep-visibility">谁可以看</label>
          <select id="ep-visibility"><option value="public">公开</option><option value="friends">好友可见</option><option value="private">仅自己可见</option></select></div>
        ${task.platform === "douyin" ? `<div class="publish-permission"><label class="field" for="ep-allowsave">保存权限</label>
          <select id="ep-allowsave"><option value="1">允许他人保存</option><option value="0">不允许</option></select></div>` : ""}
      </fieldset>` : ""}
      ${task.platform === "tiktok" ? `<fieldset class="publish-permissions">
        <legend>TikTok 可见范围与互动</legend>
        <div class="publish-permission"><label class="field" for="ep-visibility">谁可以看</label>
          <select id="ep-visibility"><option value="public">所有人</option><option value="friends">好友</option><option value="private">仅自己</option></select></div>
        <div class="publish-permission"><label class="field" for="ep-tk-comment">评论</label>
          <select id="ep-tk-comment"><option value="1">允许</option><option value="0">关闭</option></select></div>
        <div class="publish-permission"><label class="field" for="ep-tk-duet">Duet</label>
          <select id="ep-tk-duet"><option value="1">允许</option><option value="0">关闭</option></select></div>
        <div class="publish-permission"><label class="field" for="ep-tk-stitch">Stitch</label>
          <select id="ep-tk-stitch"><option value="1">允许</option><option value="0">关闭</option></select></div>
      </fieldset>` : ""}
      ${task.platform === "bilibili" ? `<fieldset class="publish-permissions">
        <legend>哔哩哔哩投稿设置</legend>
        <div class="publish-permission"><label class="field" for="ep-bili-tid">分区 tid</label>
          <input id="ep-bili-tid" type="number" min="1" value="${Number(curBili.tid || 21)}"></div>
        <div class="publish-permission"><label class="field" for="ep-bili-copyright">稿件类型</label>
          <select id="ep-bili-copyright"><option value="1">自制</option><option value="2">转载</option></select></div>
        <div class="publish-permission"><label class="field" for="ep-bili-source">转载来源</label>
          <input id="ep-bili-source" value="${esc(curBili.source || "")}" placeholder="转载时填写出处"></div>
        <div class="publish-permission"><label class="field" for="ep-visibility">可见范围</label>
          <select id="ep-visibility"><option value="public">公开可见</option><option value="private">仅自己可见</option></select></div>
      </fieldset>` : ""}`;
    $("ep-account").value = task.account_id ? String(task.account_id) : "";
    $("ep-when").value = localDateTimeValue(task.scheduled_at);
    if ($("ep-visibility")) $("ep-visibility").value = task.visibility || "public";
    if ($("ep-allowsave")) $("ep-allowsave").value = task.allow_save === false ? "0" : "1";
    if ($("ep-tk-comment")) $("ep-tk-comment").value = task.allow_comment === false ? "0" : "1";
    if ($("ep-tk-duet")) $("ep-tk-duet").value = task.allow_duet === false ? "0" : "1";
    if ($("ep-tk-stitch")) $("ep-tk-stitch").value = task.allow_stitch === false ? "0" : "1";
    if ($("ep-bili-copyright")) $("ep-bili-copyright").value = String(curBili.copyright || 1);
    ["ep-account", "ep-visibility", "ep-allowsave", "ep-tk-comment", "ep-tk-duet", "ep-tk-stitch", "ep-bili-copyright"].forEach(key => { const el = $(key); if (el) enhanceSelect(el); });
    enhanceDateTime($("ep-when"));
    _uiOpen("编辑发布任务", `${task.media_count} 个附件保持不变。调整文案、账号、预约时间与权限。`, {
      okText: "保存修改", wide: true, icon: "i-send",
      submit: value => {
        if (!value.account_id) uiEditorError("请选择发布账号", "ep-account");
        const scheduled_at = scheduleToApi(value.scheduled_at);
        return api("/api/publish/" + id, {
          method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ...value, scheduled_at }),
        });
      },
    });
  });
  if (value === null) return;
  toast("发布任务已更新", "ok"); refreshPublish();
}
async function refreshPublish() {
  const isCurrent = beginViewRequest("publish");
  if (!$("pub-table")) return;
  const rows = await api("/api/publish?platform=" + (pfHasPublish(PLATFORM) ? PLATFORM : "xhs"));
  if (!isCurrent()) return;
  PUBLISH_TASKS = rows;
  if ($("tb-pub")) $("tb-pub").textContent = rows.length;
  $("pub-table").innerHTML = rows.map(t => {
    const pubAcc = (ACCOUNTS.find(a => a.id === t.account_id) || {}).nickname || (t.account_id ? `账号 #${t.account_id}` : "—");
    return `<tr>
    <td class="wrap" style="max-width:240px"><b>${esc(t.title || "(无标题)")}</b></td>
    <td>${esc(pubAcc)}</td>
    <td>${t.platform === "twitter" && !(t.media_count || 0) ? "纯文本" : `${t.media_type === "video" ? "视频" : "图文"} · <span class="mut num">${t.media_count || 0}</span>`}</td>
    <td>${t.source_platform ? esc(PF_NAME[t.source_platform] || t.source_platform) + " 转发" : "手动"}</td>
    <td class="mut num">${t.scheduled_at ? new Date(t.scheduled_at).toLocaleString() : "尽快"}${t.next_allowed_at ? `<div class="mut" title="预约保持不变，同时等待风控间隔结束">风控最早 ${esc(riskTime(t.next_allowed_at))}</div>` : ""}</td>
    <td><span class="pill ${PUB_PILL[t.status] || "pending"}">${PUB_ST[t.status] || t.status}</span>${t.error ? ` <span class="warn-ic" title="${esc(t.error)}">${ic("i-info")}</span>` : ""}${t.result_url ? ((["kuaishou", "shipinhao", "tiktok"].includes(t.platform) || (t.platform === "bilibili" && String(t.result_url).includes("member.bilibili.com"))) ? ` <a href="javascript:void(0)" onclick="openPubInBrowser(${Number(t.account_id)}, ${jsArg(t.result_url)}, ${jsArg(t.platform)})">查看</a>` : ` <a href="${esc(safeMediaUrl(t.result_url))}" target="_blank" rel="noopener noreferrer">查看</a>`) : ""}${t.status === "publishing" ? `<div class="mut" id="pub-stage-${t.id}" style="margin-top:4px;white-space:normal;max-width:200px">${esc(t.live_stage || "执行中，请等待结果")}</div>` : ""}</td>
    <td class="acttd">
      ${["pending", "failed"].includes(t.status) ? `<button class="ghost sm" onclick="runPublish(${t.id})">立即发布</button>` : ""}
      ${["draft", "pending", "failed", "canceled"].includes(t.status) ? `<button class="ghost sm" onclick="editPublish(${t.id})">${t.schedule_needs_confirmation ? "确认预约时间" : "编辑"}</button>` : ""}
      ${t.status === "uncertain" ? `<button class="ghost sm" onclick="resolveTaskResult('publishes',${t.id})">核对结果</button>` : t.status === "publishing" ? "" : `<button class="ghost sm danger" onclick="delPublish(${t.id})">${ic("i-trash")}删除</button>`}
    </td></tr>`;
  }).join("") || empty(7, "暂无发布任务", "i-send",
      PLATFORM === "kuaishou" ? "上传图集/视频加入队列(发布到快手创作平台)"
      : PLATFORM === "douyin" ? "上传图集/视频加入队列(发布到抖音创作平台)"
      : PLATFORM === "tiktok" ? "上传单个视频加入队列(发布到 TikTok Studio)"
      : PLATFORM === "bilibili" ? "上传视频稿件或图文动态加入队列(发布到哔哩哔哩创作中心)"
      : PLATFORM === "instagram" ? "上传单图 / 轮播 / Reels / Story 加入队列(发布到 Instagram)"
      : PLATFORM === "twitter" ? "编写纯文本推文，或上传最多 4 图 / 单个视频加入队列(发布到 X / Twitter)"
      : "上传图集/视频加入队列,或在抖音作品上点「发小红书」转发过来");
}
// 创作平台管理页必须复用账号自己的登录态，不能交给系统浏览器。
async function openPubInBrowser(accountId, url, platform = "shipinhao") {
  if (!accountId) { toast("缺少账号信息", "err"); return; }
  const pageName = platform === "kuaishou" ? "快手作品管理页"
    : platform === "tiktok" ? "TikTok Studio 内容页"
    : platform === "bilibili" ? "哔哩哔哩稿件管理页" : "视频号管理页";
  toast("正在用该账号浏览器打开" + pageName + "…", "info", 5000);
  try {
    await api("/api/accounts/" + accountId + "/open-browser?url=" + encodeURIComponent(url || ""), { method: "POST" });
  } catch (e) { toast("打开失败:" + e.message, "err"); }
}
async function runPublish(id) {
  const btn = evtBtn();
  toast("发布中…会弹出浏览器窗口完成发布", "info", 8000);
  let lastStage = "";
  const stageTimer = setInterval(async () => {
    try {
      const resp = await fetch("/api/publish?platform=" + encodeURIComponent(pfHasPublish(PLATFORM) ? PLATFORM : "xhs"));
      if (!resp.ok) return;
      const rows = await resp.json();
      const row = Array.isArray(rows) ? rows.find(item => item.id === id) : null;
      const stage = String(row?.live_stage || "").trim();
      if (!stage) return;
      _busyHint = stage.includes("安全验证") ? "请在弹出窗口完成验证" : stage.slice(0, 24);
      const rowEl = $(`pub-stage-${id}`);
      if (rowEl) rowEl.textContent = stage;
      if (stage !== lastStage) {
        lastStage = stage;
        if (stage.includes("安全验证") || stage.includes("验证码") || stage.includes("扫码")) {
          toast(stage, "info", 12000);
        }
      }
    } catch (_) {}
  }, 2000);
  await withBusy(btn, "发布中", async () => {
    try { const r = await api("/api/publish/" + id + "/run-now", { method: "POST" }); toast(r.ok ? "发布成功 ✓" : "发布未成功:" + (r.error || ""), r.ok ? "ok" : "err", 6000); }
    catch (e) { toast("发布失败:" + e.message, "err"); }
    finally { clearInterval(stageTimer); _busyHint = ""; }
  });
  refreshPublish();
}
async function delPublish(id) {
  if (!await uiConfirm({ title: "删除发布任务", message: "删除该发布任务?", okText: "删除", danger: true })) return;
  try { await api("/api/publish/" + id, { method: "DELETE" }); toast("已删除", "ok"); refreshPublish(); }
  catch (e) { toast("删除失败:" + e.message, "err"); }
}

let PUB_NOTES = [], PUB_ACC = "", PUB_GOOD = false;
async function loadPublished() {
  const acc = $("pub-acc").value;
  if (!acc) { toast("请先选择" + (PF_NAME[PLATFORM] || "小红书") + "账号", "err"); return; }
  PUB_ACC = acc;
  const btn = evtBtn();
  $("published-msg").textContent = "拉取中…(走创作平台,可能需几秒)";
  $("published-grid").innerHTML = "";
  await withBusy(btn, "拉取中", async () => {
    try {
      const d = await api("/api/publish/published?account_id=" + acc);
      PUB_NOTES = d.notes || []; PUB_GOOD = !!d.good_tokens;
      $("published-msg").innerHTML = `共 ${d.total} 条` + (PUB_GOOD ? "" :
        ` · <span style="color:var(--warn)">视频预览/评论需先对该账号做「小红书扫码登录」(读取登录)</span>`);
      $("published-grid").innerHTML = PUB_NOTES.map((n, i) => `<div class="ncard">
        ${n.cover ? `<img class="ncard-cover" src="${esc(safeMediaUrl(n.cover))}" referrerpolicy="no-referrer" loading="lazy" alt="" onclick="pubPreview(${i})">` : `<div class="ncard-cover ph" onclick="pubPreview(${i})">${ic("i-image")}</div>`}
        <span class="ncard-type">${ic(n.type === "video" ? "i-play" : "i-image")}${n.type === "video" ? "视频" : "图文"}</span>
        <div class="ncard-body"><p class="ncard-title">${esc(n.title || "(无标题)")}</p>
          <div class="ncard-foot"><span>${n.time ? new Date((n.time + "").length > 10 ? n.time : n.time * 1000).toLocaleDateString() : ""}</span><span class="like">${ic("i-heart")}${fmtNum(n.like)}</span></div>
          <div class="ncard-actions"><button class="ghost sm" onclick="pubComments(${i})">${ic("i-msg")}评论</button></div>
        </div></div>`).join("") || `<div class="mut" style="columns:1">该账号暂无已发布作品</div>`;
    } catch (e) { $("published-msg").textContent = "失败:" + e.message; toast("拉取失败:" + e.message, "err"); }
  });
}
function pubPreview(i) {
  const n = PUB_NOTES[i]; if (!n) return;
  if (n.images && n.images.length) {   // 图文:直接用列表里的全图,无需再请求
    return _pvOpen(async () => ({
      media_type: "images", desc: n.title || "",
      medias: n.images.map((u, idx) => ({ url: u, kind: "image", ext: "jpeg", index: idx })),
    }));
  }
  return openPubPreview(PUB_ACC, n.note_id, n.xsec_token, n.xsec_source);  // 视频走详情接口
}
function pubComments(i) {
  const n = PUB_NOTES[i]; if (!n) return;
  return openPubComments(PUB_ACC, n.note_id, n.xsec_token, n.xsec_source);
}

// ─── 跨平台:抖音作品 → 小红书 ───
let REPOST_ID = null;
let REPOST_TARGET = "xhs";           // xhs / douyin / shipinhao
const repostXhs = (id) => openRepost(id, "xhs");
const repostDouyin = (id) => openRepost(id, "douyin");
const repostChannels = (id) => openRepost(id, "shipinhao");
const repostKuaishou = (id) => openRepost(id, "kuaishou");
const repostTiktok = (id) => openRepost(id, "tiktok");
const repostBilibili = (id) => openRepost(id, "bilibili");
const repostRednote = (id) => openRepost(id, "rednote");
const repostInstagram = (id) => openRepost(id, "instagram");
const repostTwitter = (id) => openRepost(id, "twitter");
async function pickRepostTarget(id) {
  const rec = CONTENTS.find(r => r.id === id);
  const options = [
    { value: "xhs", label: "小红书" },
    { value: "rednote", label: "海外小红书" },
    { value: "douyin", label: "抖音" },
    { value: "kuaishou", label: "快手" },
    { value: "shipinhao", label: "视频号" },
    { value: "bilibili", label: "哔哩哔哩" },
    { value: "instagram", label: "Instagram" },
    { value: "twitter", label: "Twitter" },
  ];
  if (!rec || rec.media_type === "video") options.push({ value: "tiktok", label: "TikTok" });
  const target = await uiSelect({
    title: "转发作品",
    hint: "选择要发布到的平台，下一步可以继续编辑标题、文案和发布时间。",
    options,
    value: "shipinhao",
  });
  if (target === null) return;
  openRepost(id, target);
}
async function openRepost(id, target) {
  const rec = CONTENTS.find(r => r.id === id);
  // 拉取目标平台可发布账号:小红书需创作号;其余平台需任一登录态
  const all = await api("/api/accounts?platform=" + target);
  const accs = target === "xhs"
    ? all.filter(a => a.has_creator)
    : all.filter(a => a.has_storage || a.has_creator);
  if (!accs.length) {
    const loginHint = target === "xhs"
      ? "请先在小红书账号页完成「创作者登录」(发布用)"
      : target === "rednote"
        ? "请先在海外小红书账号页完成登录"
        : target === "shipinhao"
        ? "请先在视频号账号页完成「视频号登录」"
        : target === "kuaishou"
          ? "请先在快手账号页完成登录"
          : target === "tiktok"
            ? "请先在 TikTok 账号页完成登录"
            : target === "bilibili"
              ? "请先在哔哩哔哩账号页完成登录"


              : target === "instagram" ? "请先在 Instagram 账号页完成登录"
                : target === "twitter"
                  ? "请先在 Twitter 账号页完成登录"
                : "请先在抖音账号页完成登录(扫码/创作者/Cookie)";
    toast(loginHint, "err");
    return;
  }
  REPOST_ID = id; REPOST_TARGET = target;
  const isDy = target === "douyin";
  const isChannels = target === "shipinhao";
  const isKs = target === "kuaishou";
  const isTk = target === "tiktok";
  const isBili = target === "bilibili";
  const isRn = target === "rednote";
  const isIg = target === "instagram";
  const isTw = target === "twitter";
  const cap = isTw ? 280 : isIg ? 2200 : isBili ? 80 : isRn ? 40 : isDy ? 30 : isChannels ? 16 : 20;
  const pname = isDy ? "抖音" : isChannels ? "视频号" : isKs ? "快手" : isTk ? "TikTok" : isBili ? "哔哩哔哩" : isIg ? "Instagram" : isTw ? "Twitter" : isRn ? "海外小红书" : "小红书";
  $("rp-head").textContent = "发" + pname + " · 编辑后推送";
  $("rp-title-label").textContent = `标题(≤${cap} 字)`;
  $("rp-title").maxLength = cap;
  $("rp-title").placeholder = target === "xhs" ? "给笔记起个标题" : "给作品起个标题";
  $("rp-acc").innerHTML = accs.map(a => `<option value="${a.id}">${esc(a.nickname)}</option>`).join("");
  const desc = (rec && rec.desc) || "";
  $("rp-title").value = desc.slice(0, cap);   // 默认用作品描述前若干字当标题
  $("rp-desc").value = desc;
  $("rp-topics").value = "";
  $("rp-when").value = ""; dtSyncAll();
  $("rp-msg").textContent = "确认后加入发布队列";
  $("rp-msg").dataset.tone = "neutral";
  $("rp-msg").setAttribute("role", "status");
  $("rp-src").textContent = rec ? `来源:${rec.media_type === "images" ? "图集" : "视频"} · ${esc((rec.desc || "(无描述)").slice(0, 30))}` : "";
  // 抖音发布设置(可见性 / 保存权限)仅目标为抖音时显示
  if ($("rp-dy-opts")) $("rp-dy-opts").style.display = isDy ? "flex" : "none";
  if (isDy) { if ($("rp-visibility")) $("rp-visibility").value = "public"; if ($("rp-allowsave")) $("rp-allowsave").value = "1"; }
  renderRepostThumbs(id);   // 异步拉媒体缩略图,不阻塞弹窗
  $("rp-submit").disabled = false;
  $("repost").style.display = "flex";
  $("rp-body").scrollTop = 0;
  modalOpened($("repost"));
  $("rp-head").focus({ preventScroll: true });
}
let RP_MEDIA = [];         // 可编辑图集:[{url, idx}](idx=原始序号,提交时回传)
let RP_MEDIA_LEN = 0;      // 原始图片总数(判断是否被编辑过)
let RP_IS_VIDEO = false;
async function renderRepostThumbs(id) {
  const box = $("rp-thumbs"); if (!box) return;
  RP_MEDIA = []; RP_MEDIA_LEN = 0; RP_IS_VIDEO = false;
  box.style.display = "none"; box.innerHTML = "";
  try {
    const d = await api("/api/contents/" + id + "/media");
    if (REPOST_ID !== id) return;   // 弹窗已切换/关闭
    const vid = (d.medias || []).find(m => m.kind === "video");
    if (d.media_type === "video" && (d.local_url || vid)) {
      RP_IS_VIDEO = true;
      box.innerHTML = `<div class="rp-th-ph" onclick="openPreview(${id})" title="点击预览视频">${ic("i-play")}</div>`;
      box.style.display = "flex";
      return;
    }
    const imgs = (d.medias || []).filter(m => m.kind === "image").map(m => m.url);
    const all = imgs.length ? imgs : (d.cover_url ? [d.cover_url] : []);
    RP_MEDIA = all.map((u, i) => ({ url: u, idx: i }));
    RP_MEDIA_LEN = RP_MEDIA.length;
    rpDrawThumbs();
  } catch (e) { /* 预览失败不影响转发 */ }
}
function rpDrawThumbs() {
  const box = $("rp-thumbs"); if (!box) return;
  if (!RP_MEDIA.length) { box.style.display = "none"; box.innerHTML = ""; return; }
  const n = RP_MEDIA.length;
  box.innerHTML = RP_MEDIA.map((m, pos) => `
    <div class="rp-th" draggable="true" data-pos="${pos}"
         ondragstart="rpDragStart(${pos},event)" ondragover="rpDragOver(${pos},event)"
         ondragleave="rpDragLeave(event)" ondrop="rpDrop(${pos},event)" ondragend="rpDragEnd()">
      <img src="${esc(m.url)}" referrerpolicy="no-referrer" draggable="false" alt="" title="点击看大图" onclick="openPreview(${REPOST_ID},${m.idx})">
      <span class="rp-th-badge${pos === 0 ? " cover" : ""}">${pos === 0 ? "封面" : pos + 1}</span>
      <button type="button" class="rp-th-x" title="移除这张" aria-label="移除这张" onclick="rpImgRemove(${pos})">${ic("i-x")}</button>
      <div class="rp-th-mv">
        <button type="button" onclick="rpImgMove(${pos},-1)" ${pos === 0 ? "disabled" : ""} title="前移(移到最前=封面)" aria-label="前移">${ic("i-prev")}</button>
        <button type="button" onclick="rpImgMove(${pos},1)" ${pos === n - 1 ? "disabled" : ""} title="后移" aria-label="后移">${ic("i-next")}</button>
      </div>
    </div>`).join("") + `<span class="rp-th-more">共 ${n} 张 · 拖拽排序 · 首图为封面</span>`;
  box.style.display = "flex";
}
let RP_DRAG = -1;
function rpDragStart(pos, ev) {
  RP_DRAG = pos;
  try { ev.dataTransfer.effectAllowed = "move"; ev.dataTransfer.setData("text/plain", String(pos)); } catch (e) {}
}
function rpDragOver(pos, ev) {
  ev.preventDefault();
  try { ev.dataTransfer.dropEffect = "move"; } catch (e) {}
  if (RP_DRAG !== -1 && pos !== RP_DRAG && ev.currentTarget) ev.currentTarget.classList.add("dragover");
}
function rpDragLeave(ev) { if (ev.currentTarget) ev.currentTarget.classList.remove("dragover"); }
function rpDrop(pos, ev) {
  ev.preventDefault();
  const from = RP_DRAG; RP_DRAG = -1;
  if (from < 0 || from >= RP_MEDIA.length || from === pos) { rpDrawThumbs(); return; }
  const [item] = RP_MEDIA.splice(from, 1);
  RP_MEDIA.splice(pos, 0, item);   // 拖到目标位置(其余顺延)
  rpDrawThumbs();
}
function rpDragEnd() {
  RP_DRAG = -1;
  document.querySelectorAll("#rp-thumbs .rp-th.dragover").forEach(e => e.classList.remove("dragover"));
}
function rpImgRemove(pos) {
  if (RP_MEDIA.length <= 1) { toast("至少保留一张图片", "err"); return; }
  RP_MEDIA.splice(pos, 1); rpDrawThumbs();
}
function rpImgMove(pos, dir) {
  const j = pos + dir;
  if (j < 0 || j >= RP_MEDIA.length) return;
  [RP_MEDIA[pos], RP_MEDIA[j]] = [RP_MEDIA[j], RP_MEDIA[pos]];
  rpDrawThumbs();
}
// 图片被编辑过(删了 / 调了序)才回传 media_order;未动则 null 用全部原序
function rpMediaOrder() {
  if (RP_IS_VIDEO || !RP_MEDIA.length) return null;
  const order = RP_MEDIA.map(m => m.idx);
  const unchanged = order.length === RP_MEDIA_LEN && order.every((v, i) => v === i);
  return unchanged ? null : order;
}
function hideRepost() {
  if ($("rp-submit").disabled) return;
  $("repost").style.display = "none"; REPOST_ID = null;
  modalClosed($("repost"));
}
async function submitRepost() {
  if (REPOST_ID === null || $("rp-submit").disabled) return;
  const accId = +$("rp-acc").value;
  if (!accId) { toast("请选择发布账号", "err"); return; }
  const btn = $("rp-submit"), modal = $("repost");
  let success = false;
  $("rp-body").inert = true; modal.setAttribute("aria-busy", "true");
  modal.querySelectorAll(".pv-close,#rp-actions .ghost").forEach(button => { button.disabled = true; });
  $("rp-msg").dataset.tone = "neutral"; $("rp-msg").setAttribute("role", "status");
  $("rp-msg").textContent = "提交中…";
  $("rp-msg").focus({ preventScroll: true });
  const body = {
    account_id: accId,
    title: $("rp-title").value.trim(),
    desc: $("rp-desc").value,
    topics: $("rp-topics").value.trim(),
    scheduled_at: $("rp-when").value,
    visibility: $("rp-visibility") ? $("rp-visibility").value : "public",
    allow_save: $("rp-allowsave") ? $("rp-allowsave").value !== "0" : true,
    media_order: rpMediaOrder(),
  };
  const pname = REPOST_TARGET === "douyin" ? "抖音"
    : REPOST_TARGET === "shipinhao" ? "视频号"
    : REPOST_TARGET === "kuaishou" ? "快手"
    : REPOST_TARGET === "tiktok" ? "TikTok"
    : REPOST_TARGET === "bilibili" ? "哔哩哔哩"
    : REPOST_TARGET === "instagram" ? "Instagram"
    : REPOST_TARGET === "twitter" ? "Twitter"
    : REPOST_TARGET === "rednote" ? "海外小红书" : "小红书";
  try {
    body.scheduled_at = scheduleToApi(body.scheduled_at);
    const r = await withBusy(btn, "提交中", () => api("/api/contents/" + REPOST_ID + "/repost-" + REPOST_TARGET, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    }));
    toast((body.scheduled_at ? "已加入定时发布队列" : `已加入${pname}发布队列`) + "(任务 #" + r.task_id + ")", "ok");
    success = true;
  } catch (e) {
    $("rp-msg").textContent = "提交未完成：" + e.message;
    $("rp-msg").dataset.tone = "error"; $("rp-msg").setAttribute("role", "alert");
    $("rp-msg").focus({ preventScroll: true });
  } finally {
    $("rp-body").inert = false; modal.removeAttribute("aria-busy");
    modal.querySelectorAll(".pv-close,#rp-actions .ghost").forEach(button => { button.disabled = false; });
  }
  if (success) { hideRepost(); if (typeof refreshPublish === "function") refreshPublish(); }
}
