// 15-publish-repost.js — 多平台图文/视频发布编辑器、已发布作品管理与跨平台转发。
// ─── 多平台发布 ───
const BILI_PARTITION_CACHE = new Map();
function onBiliCopyrightChange() {
  const wrap = $("pub-bili-source-wrap");
  const isVideo = ($("pub-type")?.value || "video") === "video";
  if (wrap) wrap.style.display = (isVideo && $("pub-bili-copyright")?.value === "2") ? "" : "none";
}
function syncPublishPlatformFields() {
  const mediaType = $("pub-type")?.value || "images";
  // B站：图文动态隐藏仅视频投稿适用的分区/自制转载/二创/弹幕项
  if (PLATFORM === "bilibili") {
    const isVideo = mediaType === "video";
    ["pub-bili-tid-wrap", "pub-bili-copyright-wrap", "pub-bili-statement-wrap", "pub-bili-recreate-wrap", "pub-bili-close-danmu-wrap"].forEach(id => {
      if ($(id)) $(id).style.display = isVideo ? "" : "none";
    });
    onBiliCopyrightChange();
  }
  // Facebook：按发布目标（个人动态/公共主页/群组/快拍）联动 Page ID、群组链接、可见范围与首评
  if (PLATFORM === "facebook") {
    const target = $("pub-fb-target")?.value || "profile";
    if ($("pub-fb-page-wrap")) $("pub-fb-page-wrap").style.display = target === "profile" ? "none" : "";
    if ($("pub-fb-group-wrap")) $("pub-fb-group-wrap").style.display = target === "group" ? "" : "none";
    if ($("pub-fb-privacy-wrap")) $("pub-fb-privacy-wrap").style.display = target === "profile" ? "" : "none";
    if ($("pub-fb-first-comment-wrap")) $("pub-fb-first-comment-wrap").style.display = target === "story" ? "none" : "";
  }
  // YouTube：即时首映仅公开常规视频有效；切为 Shorts 或非公开时自动重置并隐藏
  if (PLATFORM === "youtube") {
    const ytKind = $("pub-yt-kind")?.value || "video";
    const ytVis = $("pub-yt-visibility")?.value || "public";
    const allowPremiere = ytKind === "video" && ytVis === "public";
    if ($("pub-yt-premiere-wrap")) $("pub-yt-premiere-wrap").style.display = allowPremiere ? "" : "none";
    if (!allowPremiere && $("pub-yt-premiere") && $("pub-yt-premiere").value !== "0") {
      $("pub-yt-premiere").value = "0";
      $("pub-yt-premiere")._csSync?.();
    }
  }
  // Instagram：Reels 同步主页仅 auto/reels 显示；Story 快拍隐藏评论开关
  if (PLATFORM === "instagram") {
    const igKind = $("pub-ig-kind")?.value || "auto";
    if ($("pub-ig-share-feed-wrap")) $("pub-ig-share-feed-wrap").style.display = (igKind === "auto" || igKind === "reels") ? "" : "none";
    if ($("pub-ig-comment-wrap")) $("pub-ig-comment-wrap").style.display = igKind === "story" ? "none" : "";
  }
  onPubActionChange();
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
      : PLATFORM === "facebook" ? "请先完成「Facebook 登录」"
      : PLATFORM === "youtube" ? "请先完成「YouTube 登录」"
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
let pubCoverFile = null;
let pubCoverPreviewUrl = "";
let PUB_COVER_UPLOAD_CACHE = null;

function updatePubCoverHelp() {
  const help = $("pub-cover-help");
  if (!help) return;
  const mediaType = $("pub-type")?.value || "images";
  if (mediaType === "images") {
    help.textContent = ["xhs", "rednote"].includes(PLATFORM)
      ? "图文默认以首图为封面；如额外上传自定义封面，小红书/海外小红书将尝试设为封面图。"
      : "图文发布默认以首图为封面；视频发布时自动上传为自定义封面（留空使用平台默认）。";
    return;
  }
  if (PLATFORM === "twitter") {
    help.textContent = "提示：X / Twitter 标准网页发推页通常不支持单独修改视频缩略图；若当前账号支持媒体编辑将自动尝试设置，否则沿用默认首帧。";
  } else if (PLATFORM === "youtube") {
    help.textContent = "YouTube Studio 将自动上传为自定义缩略图（与下方本地路径二选一，优先使用此处上传的封面）。";
  } else if (PLATFORM === "bilibili") {
    help.textContent = "哔哩哔哩创作中心将自动上传自定义封面并覆盖默认视频帧。";
  } else {
    help.textContent = "视频发布时自动上传为自定义封面（全平台适用，留空则使用平台默认/视频首帧）。";
  }
}

function onPubActionChange() {
  const action = $("pub-action")?.value || "publish";
  const lbl = $("pub-submit-label");
  if (lbl) lbl.textContent = action === "draft" ? "加入平台草稿队列" : "加入发布队列";
  const help = $("pub-action-help");
  if (!help) return;
  const mediaType = $("pub-type")?.value || "images";
  if (action === "draft") {
    if (PLATFORM === "bilibili" && mediaType === "images") {
      help.textContent = "提示：哔哩哔哩图文动态无官方草稿箱，仅视频稿件支持「暂存到平台草稿箱」；如需本地暂存请点下方「存为本地草稿」。";
    } else if (PLATFORM === "facebook" && ($("pub-fb-target")?.value || "profile") === "story") {
      help.textContent = "提示：Facebook Story 快拍不支持平台草稿箱，请改选帖子形态或使用下方「存为本地草稿」。";
    } else if (PLATFORM === "youtube") {
      help.textContent = "将在 YouTube Studio 上传视频、自定义封面并填完元数据后关闭弹窗，自动保存为 YouTube 草稿（不公开发布）。";
    } else if (["xhs", "rednote", "instagram"].includes(PLATFORM)) {
      help.textContent = "将自动切换独立浏览器模式，上传素材与封面并填好文案后点击平台存草稿按钮，不公开发布。";
    } else {
      help.textContent = "将自动上传素材与封面、填好文案后点击目标平台存草稿按钮，不会公开发布。";
    }
  } else {
    help.textContent = "按计划时间自动上传素材与封面并直接发布；若只想先保存在 CreatorHub 本地稍后编辑，请点下方「存为本地草稿」。";
  }
}

function pubSetCover(file) {
  if (!file) { pubCoverClear(); return; }
  const ext = (file.name || "").split(".").pop().toLowerCase();
  if (!["jpg", "jpeg", "png", "webp"].includes(ext) && !(file.type || "").startsWith("image/")) {
    toast("自定义封面仅支持 JPG / PNG / WEBP 图片", "err");
    return;
  }
  pubCoverFile = file;
  PUB_COVER_UPLOAD_CACHE = null;
  renderPubCover();
}

function pubCoverClear() {
  pubCoverFile = null;
  PUB_COVER_UPLOAD_CACHE = null;
  const inp = $("pub-cover");
  if (inp) inp.value = "";
  renderPubCover();
}

function renderPubCover() {
  const box = $("pub-cover-list");
  if (!box) return;
  if (pubCoverPreviewUrl) { URL.revokeObjectURL(pubCoverPreviewUrl); pubCoverPreviewUrl = ""; }
  if (!pubCoverFile) { box.innerHTML = ""; return; }
  pubCoverPreviewUrl = URL.createObjectURL(pubCoverFile);
  box.innerHTML = `<span class="fp-chip"><img src="${pubCoverPreviewUrl}" alt="自定义封面"><span title="${esc(pubCoverFile.name)}">${esc(pubCoverFile.name)}</span><button type="button" onclick="pubCoverClear()" aria-label="移除封面">${ic("i-x")}</button></span>`;
}

function bindPubCoverPicker() {
  const inp = $("pub-cover"), zone = $("pub-cover-drop");
  if (!inp || !zone || inp._coverBound) return;
  inp._coverBound = true;
  inp.addEventListener("change", e => {
    const f = e.target.files && e.target.files[0];
    if (f) pubSetCover(f);
  });
  ["dragenter", "dragover"].forEach(ev => zone.addEventListener(ev, e => { e.preventDefault(); zone.classList.add("drag"); }));
  ["dragleave", "drop"].forEach(ev => zone.addEventListener(ev, e => {
    e.preventDefault();
    if (ev === "dragleave" && zone.contains(e.relatedTarget)) return;
    zone.classList.remove("drag");
  }));
  zone.addEventListener("drop", e => {
    const f = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
    if (f) pubSetCover(f);
  });
  const fbTarget = $("pub-fb-target");
  if (fbTarget && !fbTarget._draftHelpBound) {
    fbTarget._draftHelpBound = true;
    fbTarget.addEventListener("change", () => onPubActionChange());
  }
}

async function uploadSingleCoverFile(file) {
  if (!file) return "";
  if (PUB_COVER_UPLOAD_CACHE && PUB_COVER_UPLOAD_CACHE.file === file && PUB_COVER_UPLOAD_CACHE.path) {
    return PUB_COVER_UPLOAD_CACHE.path;
  }
  const fd = new FormData();
  fd.append("files", file);
  const up = await api("/api/publish/upload", { method: "POST", body: fd });
  const path = (up.files && up.files[0] && up.files[0].path) || "";
  if (!path) throw new Error("自定义封面上传失败，未返回有效路径");
  PUB_COVER_UPLOAD_CACHE = { file, path };
  return path;
}

function onPubType() {
  const v = $("pub-type").value, inp = $("pub-files"), lbl = $("pub-files-label");
  if (!inp) return;
  const ig = PLATFORM === "instagram", tw = PLATFORM === "twitter", fb = PLATFORM === "facebook";
  if (v === "video") {
    inp.accept = ig ? "video/*,image/*" : "video/*";
    inp.multiple = ig;
    lbl.textContent = ig ? "选择视频文件（单个 Reels/Story，或多选最多 10 项混合轮播）" : tw ? "选择视频或 GIF（单个，留空可发纯文本推文）" : fb ? "选择视频文件（单个，留空可发纯文本帖子）" : "选择视频文件(单个)";
  } else {
    inp.accept = ig ? "image/*,video/*" : "image/*";
    inp.multiple = true;
    lbl.textContent = ig ? "选择图片/视频（可多选，最多 10 项轮播）" : tw ? "选择图片（可多选，最多 4 张；留空可发纯文本推文）" : fb ? "选择图片（可多选，最多 20 张；留空可发纯文本帖子）" : "选择图片(可多选,最多 18 张)";
  }
  pubFilesClear();
  updatePubCoverHelp();
  syncPublishPlatformFields();
}
function pubFilesClear() { pubFilesDT = new DataTransfer(); _pubSync(); }
function _pubSync() { const inp = $("pub-files"); if (inp) inp.files = pubFilesDT.files; renderPubFiles(); }
function pubAddFiles(files) {
  const isVideo = $("pub-type").value === "video";
  const ig = PLATFORM === "instagram", tw = PLATFORM === "twitter", fb = PLATFORM === "facebook";
  const maxItems = tw ? 4 : ig ? 10 : fb ? 20 : 18;
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
  if (inp && zone && !inp._pubBound) {
    inp._pubBound = true;
    inp.addEventListener("change", e => { pubAddFiles(e.target.files); });
    ["dragenter", "dragover"].forEach(ev => zone.addEventListener(ev, e => { e.preventDefault(); zone.classList.add("drag"); }));
    ["dragleave", "drop"].forEach(ev => zone.addEventListener(ev, e => { e.preventDefault(); if (ev === "dragleave" && zone.contains(e.relatedTarget)) return; zone.classList.remove("drag"); }));
    zone.addEventListener("drop", e => { if (e.dataTransfer && e.dataTransfer.files.length) pubAddFiles(e.dataTransfer.files); });
  }
  bindPubCoverPicker();
  bindRepostCoverPicker();
  updatePubCoverHelp();
  syncPublishPlatformFields();
}
let PUB_SUBMITTING = false, PUB_UPLOAD_CACHE = null, PUB_UPLOAD_ABORT = null;
function renderPubUploadProgress(info) {
  const box = $("pub-upload-progress");
  if (!box) return;
  if (!info) { box.hidden = true; box.innerHTML = ""; return; }
  const P = window.CreatorHubProgress;
  const pct = info.percent != null ? `${Math.round(info.percent)}%` : "上传中";
  const bytes = info.total > 0 && P ? `${P.fmtBytes(info.loaded)} / ${P.fmtBytes(info.total)}` : "";
  const spd = info.speedBps > 0 && P ? P.fmtSpeed(info.speedBps) : "";
  const eta = info.etaSeconds > 0 && P ? P.fmtEta(info.etaSeconds) : "";
  const meta = [bytes, spd, eta].filter(Boolean).join(" · ");
  box.innerHTML = `
    <div class="pub-upload-progress-head">
      <span>正在上传素材${meta ? ` · <span class="mut progress-num">${esc(meta)}</span>` : ""}</span>
      <span style="display:inline-flex;align-items:center;gap:8px">
        <b class="progress-num">${esc(pct)}</b>
        ${info.percent < 100 ? `<button type="button" class="ghost sm danger" style="min-height:22px;padding:1px 7px" onclick="cancelPubUpload()">取消</button>` : ""}
      </span>
    </div>
    ${P ? P.renderProgressBar({ determinate: info.percent != null, percent: info.percent, tone: info.percent === 100 ? "ok" : "info", label: "素材上传进度", compact: true }) : ""}`;
  box.hidden = false;
}
function cancelPubUpload() {
  if (PUB_UPLOAD_ABORT) { try { PUB_UPLOAD_ABORT.abort(); } catch (_) {} }
}
function publishFormPayload(opts = {}) {
  const saveAsDraft = Boolean(opts && opts.saveAsDraft);
  const tiktok = PLATFORM === "tiktok", bili = PLATFORM === "bilibili", ig = PLATFORM === "instagram", tw = PLATFORM === "twitter", fb = PLATFORM === "facebook", yt = PLATFORM === "youtube";
  const mediaType = $("pub-type").value;
  const publishAction = $("pub-action")?.value === "draft" ? "draft" : "publish";
  let biliOptions, igOptions, twOptions, fbOptions, ytOptions;
  if (bili) {
    const copyright = +($("pub-bili-copyright")?.value || 1);
    const source = ($("pub-bili-source")?.value || "").trim();
    if (!saveAsDraft && mediaType === "video" && copyright === 2 && !source) {
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
  if (fb) {
    const targetType = $("pub-fb-target")?.value || "profile";
    const groupUrl = ($("pub-fb-group-url")?.value || "").trim();
    const pageId = ($("pub-fb-page-id")?.value || "").trim();
    if (!saveAsDraft && targetType === "group" && !groupUrl) {
      throw new Error("发布到 Facebook 群组时请填写目标群组链接或群组 ID");
    }
    fbOptions = {
      target_type: targetType,
      page_id: pageId,
      group_url: groupUrl,
      privacy: $("pub-fb-privacy")?.value || "public",
      first_comment: ($("pub-fb-first-comment")?.value || "").trim(),
    };
  }
  if (yt) {
    const rawTags = ($("pub-yt-tags")?.value || "").trim();
    const rawPlaylists = ($("pub-yt-playlists")?.value || "").trim();
    ytOptions = {
      post_kind: $("pub-yt-kind")?.value || "video",
      visibility: $("pub-yt-visibility")?.value || "public",
      made_for_kids: $("pub-yt-mfk")?.value === "1",
      channel_name: ($("pub-yt-channel")?.value || "").trim(),
      tags: rawTags ? rawTags.split(/[,，\n]/).map(s => s.trim()).filter(Boolean) : [],
      playlists: rawPlaylists ? rawPlaylists.split(/[,，\n]/).map(s => s.trim()).filter(Boolean) : [],
      thumbnail_path: ($("pub-yt-thumbnail")?.value || "").trim(),
      premiere: $("pub-yt-premiere")?.value === "1",
      first_comment: ($("pub-yt-first-comment")?.value || "").trim(),
    };
  }
  if (ig) {
    igOptions = {
      post_kind: $("pub-ig-kind")?.value || "auto",
      share_to_feed: $("pub-ig-share-feed")?.value !== "0",
    };
  }
  return { account_id: +$("pub-acc").value, media_type: mediaType,
    publish_action: publishAction,
    title: $("pub-title").value.trim(), desc: $("pub-desc").value,
    topics: $("pub-topics").value.trim(), scheduled_at: scheduleToApi($("pub-when").value || null),
    location: $("pub-location") ? $("pub-location").value.trim() : "",
    visibility: yt ? ($("pub-yt-visibility")?.value || "public") : fb ? (($("pub-fb-privacy")?.value || "public") === "only_me" ? "private" : ($("pub-fb-privacy")?.value || "public")) : bili ? ($("pub-bili-visibility")?.value || "public") : (tiktok ? $("pub-tk-visibility").value : $("pub-visibility") ? $("pub-visibility").value : "public"),
    allow_save: $("pub-allowsave") ? $("pub-allowsave").value !== "0" : true,
    allow_comment: bili ? ($("pub-bili-close-reply")?.value !== "1") : ig ? ($("pub-ig-comment")?.value !== "0") : tw ? (($("pub-tw-reply-perm")?.value || "everyone") !== "mentioned") : (tiktok ? $("pub-tk-comment").value !== "0" : true),
    allow_duet: tiktok ? $("pub-tk-duet").value !== "0" : true,
    allow_stitch: tiktok ? $("pub-tk-stitch").value !== "0" : true,
    ...(bili ? { bili_options: biliOptions } : {}),
    ...(ig ? { ig_options: igOptions } : {}),
    ...(tw ? { tw_options: twOptions } : {}),
    ...(fb ? { fb_options: fbOptions } : {}),
    ...(yt ? { yt_options: ytOptions } : {}) };
}
async function addPublish(opts = {}) {
  if (PUB_SUBMITTING) return;
  const saveAsDraft = Boolean(opts && opts.saveAsDraft);
  const acc = $("pub-acc").value;
  if (!acc) { toast("请选择" + (PF_NAME[PLATFORM] || "发布") + "账号", "err"); return; }
  const files = Array.from($("pub-files").files);
  const coverSnap = pubCoverFile;
  const hasText = Boolean($("pub-title").value.trim() || $("pub-desc").value.trim() || $("pub-topics").value.trim());
  const ytThumbText = PLATFORM === "youtube" ? ($("pub-yt-thumbnail")?.value || "").trim() : "";
  if (saveAsDraft) {
    if (!files.length && !coverSnap && !ytThumbText && !hasText) {
      toast("暂存本地草稿请至少填写标题/正文，或选择媒体/封面文件", "err");
      return;
    }
  } else {
    if (!files.length && PLATFORM !== "twitter" && PLATFORM !== "facebook") { toast("请先选择要发布的文件", "err"); return; }
    if (!files.length && (PLATFORM === "twitter" || PLATFORM === "facebook") && !hasText) { toast(PLATFORM === "facebook" ? "请填写帖子文案或选择媒体文件" : "请填写推文文案或选择媒体文件", "err"); return; }
  }
  let body;
  try { body = publishFormPayload({ saveAsDraft }); }
  catch (e) { $("pub-msg").textContent = e.message; toast(e.message, "err"); return; }
  const btn = evtBtn() || (saveAsDraft ? $("pub-draft-btn") : $("pub-submit-btn"));
  PUB_SUBMITTING = true;
  $("pub-msg").textContent = files.length || coverSnap ? "上传中…" : "保存中…";
  try { await withBusy(btn, saveAsDraft ? "暂存中" : "提交中", async () => {
    try {
      if (!files.length) {
        PUB_UPLOAD_CACHE = { files: [], paths: [] };
      } else if (!PUB_UPLOAD_CACHE || PUB_UPLOAD_CACHE.files.length !== files.length ||
          !PUB_UPLOAD_CACHE.files.every((file, index) => file === files[index])) {
        const fd = new FormData(); for (const f of files) fd.append("files", f);
        PUB_UPLOAD_ABORT = new AbortController();
        renderPubUploadProgress({ loaded: 0, total: files.reduce((s, f) => s + (f.size || 0), 0), percent: 0, speedBps: 0 });
        try {
          const up = await apiUploadWithProgress("/api/publish/upload", fd, {
            signal: PUB_UPLOAD_ABORT.signal,
            onProgress: prog => {
              renderPubUploadProgress(prog);
              if (prog.percent != null) $("pub-msg").textContent = `上传素材中 ${prog.percent}%…`;
            },
          });
          PUB_UPLOAD_CACHE = { files, paths: (up.files || []).map(f => f.path) };
        } finally {
          PUB_UPLOAD_ABORT = null;
          renderPubUploadProgress(null);
        }
      }
      let coverPath = ytThumbText;
      if (coverSnap) {
        $("pub-msg").textContent = "上传自定义封面…";
        coverPath = await uploadSingleCoverFile(coverSnap);
      }
      const paths = PUB_UPLOAD_CACHE.paths;
      await api("/api/publish", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ...body, media_paths: paths, cover_path: coverPath, save_as_draft: saveAsDraft }),
      });
      // A late response must not erase edits made during a slow upload.
      let unchanged = false;
      try { unchanged = JSON.stringify(publishFormPayload({ saveAsDraft })) === JSON.stringify(body) &&
          Array.from($("pub-files").files).length === files.length &&
          Array.from($("pub-files").files).every((file, index) => file === files[index]) &&
          pubCoverFile === coverSnap; } catch (_) {}
      if (unchanged) {
        pubFilesClear(); pubCoverClear();
        $("pub-title").value = ""; $("pub-desc").value = ""; $("pub-topics").value = ""; $("pub-when").value = "";
        if ($("pub-location")) $("pub-location").value = "";
        if ($("pub-tw-first-comment")) $("pub-tw-first-comment").value = "";
        if ($("pub-fb-first-comment")) $("pub-fb-first-comment").value = "";
        if ($("pub-yt-first-comment")) $("pub-yt-first-comment").value = "";
        if ($("pub-yt-thumbnail")) $("pub-yt-thumbnail").value = "";
        dtSyncAll();
        PUB_UPLOAD_CACHE = null;
        PUB_COVER_UPLOAD_CACHE = null;
      }
      if (saveAsDraft) {
        $("pub-msg").textContent = "已存为本地草稿 ✓";
        toast("已存为本地草稿（可随时编辑或入队执行）", "ok");
      } else if (body.publish_action === "draft") {
        $("pub-msg").textContent = body.scheduled_at ? "已加入定时平台草稿队列 ✓" : "已加入平台草稿队列 ✓";
        toast("已加入平台草稿队列", "ok");
      } else {
        $("pub-msg").textContent = body.scheduled_at ? "已加入定时队列 ✓" : "已加入发布队列 ✓";
        toast("已加入发布队列", "ok");
      }
    } catch (e) { $("pub-msg").textContent = "提交未确认: " + e.message + "；重试相同内容会沿用原提交编号。"; toast("提交未确认:" + e.message, "err"); }
  }); } finally { PUB_SUBMITTING = false; }
  refreshPublish();
}
const PUB_ST = { draft: "本地草稿", pending: "排队中", publishing: "发布中", uncertain: "结果待确认", done: "已发布", failed: "失败", canceled: "已取消" };
const PUB_PILL = { draft: "draft", pending: "pending", publishing: "downloading", uncertain: "downloading", done: "done", failed: "failed", canceled: "invalid" };
function pubStatusLabel(t) {
  if (!t) return "";
  if (t.status === "draft") return t.schedule_needs_confirmation ? "待确认预约" : "本地草稿";
  if (t.status === "done" && t.publish_action === "draft") return "已存平台草稿";
  if (t.status === "publishing" && t.publish_action === "draft") return "存草稿中";
  return PUB_ST[t.status] || t.status;
}
function pubStatusPill(t) {
  if (!t) return "pending";
  if (t.status === "draft") return t.schedule_needs_confirmation ? "pending" : "draft";
  return PUB_PILL[t.status] || "pending";
}
function openPubCoverPreview(taskId, title = "自定义封面") {
  return _pvOpen(async () => ({
    media_type: "images",
    desc: title || "自定义封面",
    medias: [{ url: `/api/publish/${taskId}/cover?t=${Date.now()}`, kind: "image", ext: "jpg", index: 0 }],
  }));
}
async function enqueuePublish(id) {
  const btn = evtBtn();
  await withBusy(btn, "入队中", async () => {
    try {
      await api("/api/publish/" + id + "/enqueue", { method: "POST" });
      toast("草稿已加入执行队列 ✓", "ok");
    } catch (e) {
      toast("入队失败:" + e.message, "err");
    }
  });
  refreshPublish();
}
async function editPublish(id) {
  const task = PUBLISH_TASKS.find(x => x.id === id); if (!task) return;
  const accounts = (ALL_ACCOUNTS.length ? ALL_ACCOUNTS : ACCOUNTS).filter(a => a.platform === task.platform);
  const accountOptions = accounts.map(a =>
    `<option value="${a.id}">${esc(a.nickname)}${a.has_creator ? " · 创作号" : ""}</option>`
  ).join("");
  const isLocalDraft = task.status === "draft" && !task.schedule_needs_confirmation;
  let epCoverRemoved = false;
  let epPreviewBlobUrl = "";
  const value = await new Promise(res => {
    _uiResolve = res; _uiCancelVal = null;
    const curBili = task.bili_options || {};
    _uiGetVal = () => ({
      account_id: +$("ep-account").value,
      title: $("ep-title").value.trim(),
      desc: $("ep-desc").value,
      topics: $("ep-topics").value.trim(),
      scheduled_at: $("ep-when").value || null,
      publish_action: $("ep-action") ? $("ep-action").value : (task.publish_action || "publish"),
      keep_draft: $("ep-save-mode") ? $("ep-save-mode").value === "draft" : false,
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
    const editCap = task.platform === "facebook" ? 5000 : task.platform === "twitter" ? (task.tw_options?.long_text ? 4000 : 280) : task.platform === "instagram" ? 2200 : task.platform === "youtube" ? 100 : task.platform === "bilibili" ? 80 : task.platform === "rednote" ? 40 : task.platform === "douyin" ? 30 : task.platform === "shipinhao" ? 16 : 20;
    const showVisOpts = ["douyin", "xhs", "rednote", "kuaishou"].includes(task.platform);
    const mediaAccept = task.media_type === "video"
      ? (task.platform === "instagram" ? "video/*,image/*" : "video/*")
      : (task.platform === "instagram" ? "image/*,video/*" : "image/*");
    const mediaMulti = task.media_type !== "video" || task.platform === "instagram";
    $("ui-body").innerHTML = `
      <div><label class="field" for="ep-account">发布账号</label>
        <select id="ep-account">${accountOptions}</select></div>
      <div><label class="field" for="ep-title">标题（≤${editCap} 字）</label>
        <input id="ep-title" maxlength="${editCap}" value="${esc(task.title || "")}"></div>
      <div><label class="field" for="ep-desc">正文</label>
        <textarea id="ep-desc" rows="4">${esc(task.desc || "")}</textarea></div>
      <div><label class="field" for="ep-topics">话题</label>
        <input id="ep-topics" value="${esc(task.topics || "")}" placeholder="逗号分隔，不用带 #"></div>
      <div class="form-grid">
        <div><label class="field" for="ep-when">定时发布</label>
          <input type="datetime-local" id="ep-when" aria-label="定时发布（留空=尽快发）"></div>
        <div><label class="field" for="ep-action">提交模式</label>
          <select id="ep-action">
            <option value="publish">直接发布作品</option>
            <option value="draft">暂存到平台草稿箱</option>
          </select></div>
      </div>
      <div>
        <label class="field" for="ep-save-mode">保存后的队列状态</label>
        <select id="ep-save-mode">
          <option value="enqueue">保存并加入执行队列（排队等待执行）</option>
          <option value="draft">保存为 CreatorHub 本地草稿（暂不自动执行）</option>
        </select>
      </div>
      <div>
        <label class="field" for="ep-cover-file">自定义封面（JPG / PNG / WEBP，可选）</label>
        <div class="ep-cover-row">
          ${task.has_cover ? `<img id="ep-cover-img" class="ep-cover-preview" src="${esc(task.cover_url || (`/api/publish/${task.id}/cover`))}?t=${Date.now()}" alt="当前封面" title="点击预览" onclick="openPubCoverPreview(${task.id}, ${jsArg(task.title || '自定义封面')})">` : `<img id="ep-cover-img" class="ep-cover-preview" style="display:none" alt="封面预览">`}
          <div class="ep-cover-meta">
            <b id="ep-cover-name">${task.has_cover ? esc(task.cover_name || "已设置自定义封面") : "未设置自定义封面（使用平台默认）"}</b>
            <div class="row" style="gap:8px;flex-wrap:wrap">
              <input type="file" id="ep-cover-file" accept=".jpg,.jpeg,.png,.webp,image/jpeg,image/png,image/webp" style="max-width:220px;font-size:12px">
              <button type="button" class="ghost sm danger" id="ep-cover-clear" style="${task.has_cover ? "" : "display:none"}">移除封面</button>
            </div>
          </div>
        </div>
      </div>
      <div>
        <label class="field" for="ep-media-files">${task.media_count ? `替换媒体附件（当前已附 ${task.media_count} 个；留空则保持不变）` : "补充媒体附件（当前无附件，入队发布前请先上传）"}</label>
        <input type="file" id="ep-media-files" accept="${mediaAccept}" ${mediaMulti ? "multiple" : ""}>
      </div>
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
    if ($("ep-action")) $("ep-action").value = task.publish_action === "draft" ? "draft" : "publish";
    if ($("ep-save-mode")) $("ep-save-mode").value = isLocalDraft ? "draft" : "enqueue";
    if ($("ep-visibility")) $("ep-visibility").value = task.visibility || "public";
    if ($("ep-allowsave")) $("ep-allowsave").value = task.allow_save === false ? "0" : "1";
    if ($("ep-tk-comment")) $("ep-tk-comment").value = task.allow_comment === false ? "0" : "1";
    if ($("ep-tk-duet")) $("ep-tk-duet").value = task.allow_duet === false ? "0" : "1";
    if ($("ep-tk-stitch")) $("ep-tk-stitch").value = task.allow_stitch === false ? "0" : "1";
    if ($("ep-bili-copyright")) $("ep-bili-copyright").value = String(curBili.copyright || 1);
    const epCoverInput = $("ep-cover-file");
    const epCoverImg = $("ep-cover-img");
    const epCoverName = $("ep-cover-name");
    const epCoverClear = $("ep-cover-clear");
    if (epCoverInput) {
      epCoverInput.addEventListener("change", () => {
        const f = epCoverInput.files && epCoverInput.files[0];
        if (!f) return;
        epCoverRemoved = false;
        if (epPreviewBlobUrl) URL.revokeObjectURL(epPreviewBlobUrl);
        epPreviewBlobUrl = URL.createObjectURL(f);
        if (epCoverImg) { epCoverImg.src = epPreviewBlobUrl; epCoverImg.style.display = ""; epCoverImg.onclick = null; }
        if (epCoverName) epCoverName.textContent = `新封面：${f.name}`;
        if (epCoverClear) epCoverClear.style.display = "";
      });
    }
    if (epCoverClear) {
      epCoverClear.addEventListener("click", () => {
        epCoverRemoved = true;
        if (epCoverInput) epCoverInput.value = "";
        if (epCoverImg) epCoverImg.style.display = "none";
        if (epCoverName) epCoverName.textContent = "已移除自定义封面（保存后生效）";
        epCoverClear.style.display = "none";
      });
    }
    ["ep-account", "ep-action", "ep-save-mode", "ep-visibility", "ep-allowsave", "ep-tk-comment", "ep-tk-duet", "ep-tk-stitch", "ep-bili-copyright"].forEach(key => { const el = $(key); if (el) enhanceSelect(el); });
    enhanceDateTime($("ep-when"));
    _uiOpen(isLocalDraft ? "编辑本地草稿" : "编辑发布任务", `当前已附 ${task.media_count || 0} 个媒体文件。可调整文案、自定义封面、草稿/发布模式与附件。`, {
      okText: "保存修改", wide: true, icon: "i-send",
      submit: async value => {
        if (!value.account_id) uiEditorError("请选择发布账号", "ep-account");
        const payload = { ...value, scheduled_at: scheduleToApi(value.scheduled_at) };
        const newCoverFile = epCoverInput && epCoverInput.files && epCoverInput.files[0];
        if (newCoverFile) {
          payload.cover_path = await uploadSingleCoverFile(newCoverFile);
        } else if (epCoverRemoved) {
          payload.cover_path = "";
        }
        const newMediaFiles = Array.from(($("ep-media-files") && $("ep-media-files").files) || []);
        if (newMediaFiles.length) {
          const fd = new FormData();
          for (const f of newMediaFiles) fd.append("files", f);
          const up = await api("/api/publish/upload", { method: "POST", body: fd });
          payload.media_paths = (up.files || []).map(f => f.path);
        }
        return api("/api/publish/" + id, {
          method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
        });
      },
    });
  });
  if (epPreviewBlobUrl) { URL.revokeObjectURL(epPreviewBlobUrl); epPreviewBlobUrl = ""; }
  if (value === null) return;
  toast(value.keep_draft ? "本地草稿已保存" : "发布任务已更新", "ok"); refreshPublish();
}
function renderPublishStepsHtml(steps = []) {
  if (!Array.isArray(steps) || !steps.length) return "";
  return `<div class="pub-stepper">${steps.map(st => {
    const cls = st.state === "done" ? "done" : st.state === "warn" ? "warn" : st.state === "active" ? "active" : "";
    return `<span class="pub-step ${cls}">${esc(st.label)}</span>`;
  }).join("")}</div>`;
}
function inferPublishStepsClient(stage) {
  const labels = ["准备环境", "上传媒体", "填写文案", "校验/验证", "提交确认"];
  const text = String(stage || "");
  let idx = 0;
  if (/提交|点击发布|确认发布|完成发布|等待结果/.test(text)) idx = 4;
  else if (/验证|滑块|扫码|验证码|校验|风控/.test(text)) idx = 3;
  else if (/标题|正文|文案|话题|分区|标签|填写/.test(text)) idx = 2;
  else if (/上传|素材|视频|图片|封面|转码/.test(text)) idx = 1;
  const warn = /安全验证|验证码|滑块|扫码/.test(text);
  return labels.map((label, i) => ({
    label,
    state: i < idx ? "done" : i === idx ? (warn ? "warn" : "active") : "pending",
  }));
}
const PUB_VERIFY_TOASTED = new Set();
if (typeof window !== "undefined") {
  window.addEventListener("creatorhub:progress:update", e => {
    const task = e.detail?.task;
    if (!task || task.category !== "publish") return;
    const tid = Number(task.extra?.task_id || 0);
    if (!tid) return;
    const stageText = String(task.phase_label || "").trim();
    const rowStage = $(`pub-stage-${tid}`);
    if (rowStage && stageText) rowStage.textContent = stageText;
    const rowStepper = $(`pub-stepper-${tid}`);
    if (rowStepper) {
      rowStepper.innerHTML = renderPublishStepsHtml(task.steps?.length ? task.steps : inferPublishStepsClient(stageText));
    }
    if (task.status === "waiting_user" && stageText && !PUB_VERIFY_TOASTED.has(`${tid}:${stageText}`)) {
      PUB_VERIFY_TOASTED.add(`${tid}:${stageText}`);
      toast(stageText, "info", 10000);
    }
    if (["completed", "failed", "canceled"].includes(task.status)) {
      const isDraftTask = task.extra?.publish_action === "draft" || /草稿/.test(stageText);
      if (task.status === "completed") toast(`「${task.title}」${isDraftTask ? "已存平台草稿 ✓" : "发布成功 ✓"}`, "ok", 5500);
      else if (task.status === "failed" && task.error) toast(`${isDraftTask ? "平台草稿保存" : "发布"}未成功：${task.error}`, "err", 7000);
      if (typeof CURRENT_TAB !== "undefined" && CURRENT_TAB === "publish") refreshPublish();
    }
  });
}

function renderPublishRows() {
  const tbody = $("pub-table");
  if (!tbody) return;
  const filter = $("pub-status-filter")?.value || "";
  const allRows = Array.isArray(PUBLISH_TASKS) ? PUBLISH_TASKS : [];
  const rows = filter ? allRows.filter(t => t.status === filter) : allRows;
  if ($("tb-pub")) $("tb-pub").textContent = allRows.length;
  tbody.innerHTML = rows.map(t => {
    const accObj = (typeof findAnyAccount === "function" && findAnyAccount(t.account_id)) || ACCOUNTS.find(a => a.id === t.account_id);
    const pubAcc = accObj ? (accObj.alias || accObj.nickname || `账号 #${t.account_id}`) : (t.account_id ? `账号 #${t.account_id}` : "—");
    const pfBadge = GLOBAL_MODE ? `<span class="mx-mini-pf inline" style="margin-right:6px">${esc(PF_NAME[t.platform] || t.platform)}</span>` : "";
    const liveTask = window.CreatorHubProgress?.getTask?.(`publish:${t.id}`);
    const stageText = String(liveTask?.phase_label || t.live_stage || "执行中，请等待结果");
    const stepsHtml = t.status === "publishing"
      ? `<div id="pub-stepper-${t.id}">${renderPublishStepsHtml(liveTask?.steps?.length ? liveTask.steps : inferPublishStepsClient(stageText))}</div>`
      : "";
    const isLocalDraft = t.status === "draft" && !t.schedule_needs_confirmation;
    const isPlatformDraft = t.publish_action === "draft";
    const metaBadges = [];
    if (isPlatformDraft) {
      metaBadges.push(`<span class="pub-badge-mini draft" title="执行后保存到目标平台草稿箱，不公开发布">平台草稿</span>`);
    }
    if (t.has_cover) {
      const coverSrc = esc(t.cover_url || `/api/publish/${t.id}/cover`);
      metaBadges.push(`<img class="pub-cover-thumb" src="${coverSrc}" alt="自定义封面" title="点击预览自定义封面" onclick="openPubCoverPreview(${t.id}, ${jsArg(t.title || '自定义封面')})">`);
      metaBadges.push(`<span class="pub-badge-mini cover" title="${esc(t.cover_name || '已设置自定义封面')}">自定义封面</span>`);
    }
    const mediaCell = isLocalDraft && !(t.media_count || 0) && t.platform !== "twitter" && t.platform !== "facebook"
      ? `<span class="mut">未选素材（草稿）</span>`
      : ((t.platform === "twitter" || t.platform === "facebook") && !(t.media_count || 0)
        ? "纯文本"
        : `${t.media_type === "video" ? "视频" : "图文"} · <span class="mut num">${t.media_count || 0}</span>`);
    const openInCreatorBrowser = Boolean(
      isPlatformDraft
      || ["kuaishou", "shipinhao", "tiktok"].includes(t.platform)
      || (t.platform === "bilibili" && String(t.result_url || "").includes("member.bilibili.com"))
    );
    const resultLink = t.result_url
      ? (openInCreatorBrowser
        ? ` <a href="javascript:void(0)" onclick="openPubInBrowser(${Number(t.account_id)}, ${jsArg(t.result_url)}, ${jsArg(t.platform)}, ${jsArg(t.publish_action || 'publish')})">${isPlatformDraft ? "打开草稿箱" : "查看"}</a>`
        : ` <a href="${esc(safeHttpUrl(t.result_url))}" target="_blank" rel="noopener noreferrer">查看</a>`)
      : "";
    const runLabel = isPlatformDraft ? "立即存草稿" : "立即发布";
    return `<tr>
    <td class="wrap" style="max-width:250px"><b>${esc(t.title || "(无标题)")}</b>${metaBadges.length ? `<div class="pub-meta-badges">${metaBadges.join("")}</div>` : ""}</td>
    <td>${pfBadge}${esc(pubAcc)}</td>
    <td>${mediaCell}</td>
    <td>${t.source_platform ? esc(PF_NAME[t.source_platform] || t.source_platform) + " 转发" : "手动"}</td>
    <td class="mut num">${t.scheduled_at ? new Date(t.scheduled_at).toLocaleString() : "尽快"}${t.next_allowed_at ? `<div class="mut" title="预约保持不变，同时等待风控间隔结束">风控最早 ${esc(riskTime(t.next_allowed_at))}</div>` : ""}</td>
    <td><span class="pill ${pubStatusPill(t)}">${pubStatusLabel(t)}</span>${t.error ? ` <span class="warn-ic" title="${esc(t.error)}">${ic("i-info")}</span>` : ""}${resultLink}${t.status === "publishing" ? `<div class="mut" id="pub-stage-${t.id}" style="margin-top:4px;white-space:normal;max-width:240px">${esc(stageText)}</div>${stepsHtml}` : (t.status === "pending" && t.blocked_reason ? `<div class="mut" style="margin-top:4px;white-space:normal;max-width:220px;color:var(--warn)">${esc(t.blocked_reason)}</div>` : "")}</td>
    <td class="acttd">
      ${isLocalDraft ? `<button class="ghost sm" onclick="enqueuePublish(${t.id})">${isPlatformDraft ? "加入草稿队列" : "入队发布"}</button>` : ""}
      ${(isLocalDraft || ["pending", "failed"].includes(t.status)) ? `<button class="ghost sm" onclick="runPublish(${t.id})">${runLabel}</button>` : ""}
      ${["draft", "pending", "failed", "canceled"].includes(t.status) ? `<button class="ghost sm" onclick="editPublish(${t.id})">${t.schedule_needs_confirmation ? "确认预约时间" : "编辑"}</button>` : ""}
      ${t.status === "uncertain" ? `<button class="ghost sm" onclick="resolveTaskResult('publishes',${t.id})">核对结果</button>` : t.status === "publishing" ? "" : `<button class="ghost sm danger" onclick="delPublish(${t.id})">${ic("i-trash")}删除</button>`}
    </td></tr>`;
  }).join("") || empty(7, filter ? "当前筛选状态下暂无发布任务" : "暂无发布任务", "i-send",
      filter ? "可切换顶部状态筛选查看本地草稿、排队中或已完成记录"
      : PLATFORM === "kuaishou" ? "上传图集/视频加入队列或存为本地草稿(发布到快手创作平台)"
      : PLATFORM === "douyin" ? "上传图集/视频加入队列或存为本地草稿(发布到抖音创作平台)"
      : PLATFORM === "tiktok" ? "上传单个视频加入队列或存为本地草稿(发布到 TikTok Studio)"
      : PLATFORM === "bilibili" ? "上传视频稿件或图文动态加入队列或存为本地草稿(发布到哔哩哔哩创作中心)"
      : PLATFORM === "instagram" ? "上传单图 / 轮播 / Reels / Story 加入队列(发布到 Instagram)"
      : PLATFORM === "twitter" ? "编写纯文本推文，或上传最多 4 图 / 单个视频加入队列(发布到 X / Twitter)"
      : PLATFORM === "facebook" ? "编写纯文本帖子，或上传最多 20 图 / 单个视频加入队列(发布到 Facebook 主页/公共主页/群组/Story)"
      : PLATFORM === "youtube" ? "上传视频或 Shorts 加入队列(通过 YouTube Studio 自动完成填写与发布)"
      : "上传图集/视频加入队列、存为本地草稿，或在作品列表一键跨平台转发");
}

async function refreshPublish() {
  const isCurrent = beginViewRequest("publish");
  if (!$("pub-table")) return;
  const rows = await api("/api/publish" + (GLOBAL_MODE ? "" : "?platform=" + (pfHasPublish(PLATFORM) ? PLATFORM : "xhs")));
  if (!isCurrent()) return;
  PUBLISH_TASKS = rows;
  renderPublishRows();
}
// 创作平台管理页/草稿箱必须复用账号自己的登录态，不能交给系统浏览器。
async function openPubInBrowser(accountId, url, platform = "shipinhao", publishAction = "publish") {
  if (!accountId) { toast("缺少账号信息", "err"); return; }
  const isDraft = publishAction === "draft";
  const pfLabel = PF_NAME[platform] || "创作平台";
  const pageName = isDraft
    ? `${pfLabel}草稿箱`
    : (platform === "kuaishou" ? "快手作品管理页"
      : platform === "tiktok" ? "TikTok Studio 内容页"
      : platform === "bilibili" ? "哔哩哔哩稿件管理页"
      : platform === "shipinhao" ? "视频号管理页" : `${pfLabel}创作页`);
  toast("正在用该账号浏览器打开" + pageName + "…", "info", 5000);
  try {
    await api("/api/accounts/" + accountId + "/open-browser?url=" + encodeURIComponent(url || ""), { method: "POST" });
  } catch (e) { toast("打开失败:" + e.message, "err"); }
}
async function runPublish(id, closeOpenBrowser = false) {
  const btn = closeOpenBrowser ? null : evtBtn();
  const taskMeta = PUBLISH_TASKS.find(item => item.id === id);
  const isPlatformDraft = taskMeta?.publish_action === "draft";
  let res = null;
  await withBusy(btn, "启动中", async () => {
    try {
      const qs = `?async_mode=true${closeOpenBrowser ? "&close_open_browser=true" : ""}`;
      res = await api("/api/publish/" + id + "/run-now" + qs, { method: "POST" });
      if (!res?.ok && res?.blocked_by === "open_browser" && !closeOpenBrowser) {
        return;
      }
      if (res?.ok && res?.queued) {
        toast(closeOpenBrowser
          ? `已关闭手动窗口并启动后台${isPlatformDraft ? "保存平台草稿" : "发布"}，步骤进度将实时推送`
          : `已启动后台${isPlatformDraft ? "保存平台草稿" : "发布"}，步骤进度可在列表与右上角活动中心实时查看`, "info", 5500);
      } else {
        toast(res.ok ? (isPlatformDraft ? "已存平台草稿 ✓" : "发布成功 ✓") : "执行未成功:" + (res.error || ""), res.ok ? "ok" : "err", 6000);
      }
    } catch (e) { toast("启动失败:" + e.message, "err"); }
  });
  if (res && !res.ok && res.blocked_by === "open_browser" && !closeOpenBrowser) {
    const task = PUBLISH_TASKS.find(item => item.id === id);
    const aid = res.account_id || task?.account_id;
    const accObj = aid ? ((typeof findAnyAccount === "function" && findAnyAccount(aid)) || ACCOUNTS.find(a => a.id === aid)) : null;
    const accLabel = accObj ? (accObj.alias || accObj.nickname || `账号 #${aid}`) : (aid ? `账号 #${aid}` : "该账号");
    const ok = await uiConfirm({
      title: "该账号的浏览器窗口仍开着",
      message: `账号「${accLabel}」手动打开的独立浏览器窗口尚未关闭，为避免 Profile 冲突当前无法同时执行。是否先自动保存登录态并关闭该窗口，然后立即${isPlatformDraft ? "保存平台草稿" : "发布"}？`,
      okText: isPlatformDraft ? "关闭窗口并存草稿" : "关闭窗口并发布",
    });
    if (ok) {
      return runPublish(id, true);
    }
    toast("已暂缓执行，关闭该账号浏览器窗口后将自动执行", "info", 5000);
  }
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
const repostFacebook = (id) => openRepost(id, "facebook");
const repostYouTube = (id) => openRepost(id, "youtube");
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
    { value: "facebook", label: "Facebook" },
  ];
  if (!rec || rec.media_type === "video") {
    options.push({ value: "tiktok", label: "TikTok" });
    options.push({ value: "youtube", label: "YouTube" });
  }
  const target = await uiSelect({
    title: "转发作品",
    hint: "选择要发布到的平台，下一步可以继续编辑标题、文案和发布时间。",
    options,
    value: "shipinhao",
  });
  if (target === null) return;
  openRepost(id, target);
}
let rpCoverFile = null;
let rpCoverPreviewUrl = "";
function rpSetCover(file) {
  if (!file) { rpClearCover(); return; }
  const ext = (file.name || "").split(".").pop().toLowerCase();
  if (!["jpg", "jpeg", "png", "webp"].includes(ext) && !(file.type || "").startsWith("image/")) {
    toast("自定义封面仅支持 JPG / PNG / WEBP 图片", "err");
    return;
  }
  rpCoverFile = file;
  renderRpCover();
}
function rpClearCover() {
  rpCoverFile = null;
  const inp = $("rp-cover");
  if (inp) inp.value = "";
  renderRpCover();
}
function renderRpCover() {
  const box = $("rp-cover-list");
  if (!box) return;
  if (rpCoverPreviewUrl) { URL.revokeObjectURL(rpCoverPreviewUrl); rpCoverPreviewUrl = ""; }
  if (!rpCoverFile) { box.innerHTML = ""; return; }
  rpCoverPreviewUrl = URL.createObjectURL(rpCoverFile);
  box.innerHTML = `<span class="fp-chip"><img src="${rpCoverPreviewUrl}" alt="自定义封面"><span title="${esc(rpCoverFile.name)}">${esc(rpCoverFile.name)}</span><button type="button" onclick="rpClearCover()" aria-label="移除封面">${ic("i-x")}</button></span>`;
}
function onRpActionChange() {
  const action = $("rp-action")?.value || "publish";
  const btn = $("rp-submit");
  if (btn && !btn.disabled) {
    btn.innerHTML = `<svg aria-hidden="true"><use href="#i-send"/></svg>${action === "draft" ? "推送至平台草稿" : "确认推送"}`;
  }
  const msg = $("rp-msg");
  if (msg && msg.dataset.tone === "neutral") {
    msg.textContent = action === "draft" ? "确认后加入目标平台草稿队列（不公开发布）" : "确认后加入发布队列";
  }
}
function bindRepostCoverPicker() {
  const inp = $("rp-cover"), zone = $("rp-cover-drop");
  if (inp && zone && !inp._rpCoverBound) {
    inp._rpCoverBound = true;
    inp.addEventListener("change", e => {
      const f = e.target.files && e.target.files[0];
      if (f) rpSetCover(f);
    });
    ["dragenter", "dragover"].forEach(ev => zone.addEventListener(ev, e => { e.preventDefault(); zone.classList.add("drag"); }));
    ["dragleave", "drop"].forEach(ev => zone.addEventListener(ev, e => {
      e.preventDefault();
      if (ev === "dragleave" && zone.contains(e.relatedTarget)) return;
      zone.classList.remove("drag");
    }));
    zone.addEventListener("drop", e => {
      const f = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
      if (f) rpSetCover(f);
    });
  }
  const actionSel = $("rp-action");
  if (actionSel && !actionSel._rpActionBound) {
    actionSel._rpActionBound = true;
    actionSel.addEventListener("change", () => onRpActionChange());
  }
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
                  : target === "facebook"
                    ? "请先在 Facebook 账号页完成登录"
                  : target === "youtube"
                    ? "请先在 YouTube 账号页完成登录"
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
  const isFb = target === "facebook";
  const isYt = target === "youtube";
  const cap = isFb ? 5000 : isTw ? 280 : isIg ? 2200 : isYt ? 100 : isBili ? 80 : isRn ? 40 : isDy ? 30 : isChannels ? 16 : 20;
  const pname = isDy ? "抖音" : isChannels ? "视频号" : isKs ? "快手" : isTk ? "TikTok" : isBili ? "哔哩哔哩" : isIg ? "Instagram" : isTw ? "Twitter" : isFb ? "Facebook" : isYt ? "YouTube" : isRn ? "海外小红书" : "小红书";
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
  if ($("rp-action")) { $("rp-action").value = "publish"; $("rp-action")._csSync?.(); }
  rpClearCover();
  $("rp-msg").textContent = "确认后加入发布队列";
  $("rp-msg").dataset.tone = "neutral";
  $("rp-msg").setAttribute("role", "status");
  $("rp-src").textContent = rec ? `来源:${rec.media_type === "images" ? "图集" : "视频"} · ${esc((rec.desc || "(无描述)").slice(0, 30))}` : "";
  // 抖音发布设置(可见性 / 保存权限)仅目标为抖音时显示
  if ($("rp-dy-opts")) $("rp-dy-opts").style.display = isDy ? "flex" : "none";
  if (isDy) { if ($("rp-visibility")) $("rp-visibility").value = "public"; if ($("rp-allowsave")) $("rp-allowsave").value = "1"; }
  renderRepostThumbs(id);   // 异步拉媒体缩略图,不阻塞弹窗
  $("rp-submit").disabled = false;
  if ($("rp-save-draft")) $("rp-save-draft").disabled = false;
  onRpActionChange();
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
      <img src="${esc(safeMediaUrl(m.url))}" referrerpolicy="no-referrer" draggable="false" alt="" title="点击看大图" onclick="openPreview(${REPOST_ID},${m.idx})">
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
  if ($("rp-submit").disabled || $("rp-save-draft")?.disabled) return;
  rpClearCover();
  $("repost").style.display = "none"; REPOST_ID = null;
  modalClosed($("repost"));
}
async function submitRepost(opts = {}) {
  const saveAsDraft = Boolean(opts && opts.saveAsDraft);
  if (REPOST_ID === null || $("rp-submit").disabled || $("rp-save-draft")?.disabled) return;
  const accId = +$("rp-acc").value;
  if (!accId) { toast("请选择发布账号", "err"); return; }
  const btn = saveAsDraft ? ($("rp-save-draft") || $("rp-submit")) : $("rp-submit");
  const modal = $("repost");
  let success = false;
  $("rp-body").inert = true; modal.setAttribute("aria-busy", "true");
  modal.querySelectorAll(".pv-close,#rp-actions button").forEach(button => { button.disabled = true; });
  $("rp-msg").dataset.tone = "neutral"; $("rp-msg").setAttribute("role", "status");
  $("rp-msg").textContent = rpCoverFile ? "上传自定义封面…" : "提交中…";
  $("rp-msg").focus({ preventScroll: true });
  const publishAction = $("rp-action")?.value === "draft" ? "draft" : "publish";
  const body = {
    account_id: accId,
    title: $("rp-title").value.trim(),
    desc: $("rp-desc").value,
    topics: $("rp-topics").value.trim(),
    scheduled_at: $("rp-when").value,
    visibility: $("rp-visibility") ? $("rp-visibility").value : "public",
    allow_save: $("rp-allowsave") ? $("rp-allowsave").value !== "0" : true,
    media_order: rpMediaOrder(),
    cover_path: "",
    publish_action: publishAction,
    save_as_draft: saveAsDraft,
  };
  const pname = REPOST_TARGET === "douyin" ? "抖音"
    : REPOST_TARGET === "shipinhao" ? "视频号"
    : REPOST_TARGET === "kuaishou" ? "快手"
    : REPOST_TARGET === "tiktok" ? "TikTok"
    : REPOST_TARGET === "bilibili" ? "哔哩哔哩"
    : REPOST_TARGET === "instagram" ? "Instagram"
    : REPOST_TARGET === "twitter" ? "Twitter"
    : REPOST_TARGET === "facebook" ? "Facebook"
    : REPOST_TARGET === "youtube" ? "YouTube"
    : REPOST_TARGET === "rednote" ? "海外小红书" : "小红书";
  try {
    body.scheduled_at = scheduleToApi(body.scheduled_at);
    const r = await withBusy(btn, saveAsDraft ? "暂存中" : "提交中", async () => {
      if (rpCoverFile) {
        body.cover_path = await uploadSingleCoverFile(rpCoverFile);
        $("rp-msg").textContent = "提交中…";
      }
      return api("/api/contents/" + REPOST_ID + "/repost-" + REPOST_TARGET, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
      });
    });
    if (saveAsDraft) {
      toast(`已存为${pname}本地草稿 (任务 #${r.task_id})`, "ok");
    } else if (publishAction === "draft") {
      toast(`已加入${pname}平台草稿队列 (任务 #${r.task_id})`, "ok");
    } else {
      toast((body.scheduled_at ? "已加入定时发布队列" : `已加入${pname}发布队列`) + "(任务 #" + r.task_id + ")", "ok");
    }
    success = true;
  } catch (e) {
    $("rp-msg").textContent = "提交未完成：" + e.message;
    $("rp-msg").dataset.tone = "error"; $("rp-msg").setAttribute("role", "alert");
    $("rp-msg").focus({ preventScroll: true });
  } finally {
    $("rp-body").inert = false; modal.removeAttribute("aria-busy");
    modal.querySelectorAll(".pv-close,#rp-actions button").forEach(button => { button.disabled = false; });
  }
  if (success) { hideRepost(); if (typeof refreshPublish === "function") refreshPublish(); }
}
