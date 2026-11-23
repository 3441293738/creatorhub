// 12-collections.js — 关键词与创作者批量作品采集任务管理、结果筛选与报表导出。
// ─── 监控 ───
// ═══════════ 抖音 / 小红书关键词批量采集 ═══════════
function parseCollectionKeywords(raw) {
  const seen = new Set();
  return String(raw || "")
    .split(/[,，、;；\n]+/).map(x => x.trim()).filter(x => {
      const key = x.toLocaleLowerCase();
      if (!x || seen.has(key)) return false;
      seen.add(key); return true;
    }).slice(0, 21);
}
function collectionKeywords() {
  return parseCollectionKeywords($("col-keywords") ? $("col-keywords").value : "");
}
function collectionSourceValue() {
  return $("col-source") ? ($("col-source").value || "keyword") : "keyword";
}
async function loadCollectionFavoriteBoards() {
  const accountId = Number($("col-account")?.value || 0);
  const select = $("col-board");
  if (!select || !accountId || !["xhs", "rednote"].includes(PLATFORM)) return;
  const requestedAccount = accountId;
  select.disabled = true;
  select.innerHTML = '<option value="">正在加载收藏夹…</option>';
  $("col-board-help").textContent = "";
  try {
    const boards = await api(`/api/accounts/${accountId}/${PLATFORM === "rednote" ? "rednote" : "xhs"}/favorite-boards`);
    if (Number($("col-account")?.value || 0) !== requestedAccount) return;
    COLLECTION_FAVORITE_BOARDS = boards || [];
    select.innerHTML = '<option value="">请选择收藏夹</option>' + COLLECTION_FAVORITE_BOARDS.map(board =>
      `<option value="${esc(board.id)}">${esc(board.name)}${Number(board.total) ? `（${fmtNum(board.total)}）` : ""}</option>`).join("");
    $("col-board-help").textContent = boards.length ? `已读取 ${boards.length} 个收藏夹` : "当前账号没有可用收藏夹";
  } catch (e) {
    COLLECTION_FAVORITE_BOARDS = [];
    select.innerHTML = '<option value="">加载失败，请重新选择账号</option>';
    $("col-board-help").textContent = e.message;
  } finally {
    select.disabled = false;
    select._csSync?.();
  }
}
function collectionAccountChanged() {
  if (collectionSourceValue() === "favorite_board") loadCollectionFavoriteBoards();
}
function collectionSourceChanged() {
  applyCollectionForm();
  if (collectionSourceValue() === "favorite_board") loadCollectionFavoriteBoards();
}
// 采集来源下拉按平台重建:不适用当前平台的选项直接移除(而非 disabled/hidden),
// 部分浏览器(如 Firefox)对 <option hidden> 不生效,仍会显示灰色禁用项。
const COL_SOURCE_DEFS = [
  { value: "keyword", label: "关键词搜索" },
  { value: "creator", label: "创作者主页" },
  { value: "favorite_all", label: "全部收藏", xhs: true },
  { value: "favorite_board", label: "指定收藏夹", xhs: true },
  { value: "account_works", label: "本账号作品", channels: true },
];
function rebuildCollectionSources(xhs, channels, tiktok) {
  const sel = $("col-source");
  if (!sel) return;
  const current = sel.value;
  const bilibili = PLATFORM === "bilibili";
  const rednote = PLATFORM === "rednote";
  const instagram = PLATFORM === "instagram";
  const twitter = PLATFORM === "twitter";
  sel.innerHTML = "";
  for (const o of COL_SOURCE_DEFS) {
    let keep;
    if (o.channels) keep = channels || bilibili || instagram || twitter;
    else if (o.xhs) keep = xhs || rednote;
    else keep = !(tiktok && o.value !== "keyword");
    if (!keep) continue;
    const opt = document.createElement("option");
    opt.value = o.value;
    opt.textContent = o.label;
    sel.appendChild(opt);
  }
  const hasActiveChannelsAccount = channels && ACCOUNTS.some(
    a => a.platform === "shipinhao" && a.status !== "invalid" && a.has_storage
  );
  if ([...sel.options].some(o => o.value === current)) sel.value = current;
  else if (channels) sel.value = hasActiveChannelsAccount ? "account_works" : "keyword";
  else if (tiktok) sel.value = "keyword";
  else sel.value = "keyword";
}
function applyCollectionForm() {
  const enabled = !!($("col-download") && $("col-download").checked);
  if ($("col-dir-wrap")) $("col-dir-wrap").style.display = enabled ? "" : "none";
  const xhs = PLATFORM === "xhs";
  const channels = PLATFORM === "shipinhao";
  const tiktok = PLATFORM === "tiktok";
  const platformName = PF_NAME[PLATFORM] || "平台";
  rebuildCollectionSources(xhs, channels, tiktok);
  populateCollectionAccount();
  const source = collectionSourceValue();
  const channelsAccountOnly = channels && source === "account_works";
  const biliAccountOnly = PLATFORM === "bilibili" && source === "account_works";
  const igAccountOnly = PLATFORM === "instagram" && source === "account_works";
  const twAccountOnly = PLATFORM === "twitter" && source === "account_works";
  const accountWorksOnly = channelsAccountOnly || biliAccountOnly || igAccountOnly || twAccountOnly;
  const favorite = ["favorite_all", "favorite_board"].includes(source);
  const alternate = source !== "keyword";
  if ($("col-keywords-wrap")) $("col-keywords-wrap").style.display = alternate ? "none" : "";
  if ($("col-board-wrap")) $("col-board-wrap").style.display = source === "favorite_board" ? "" : "none";
  if ($("col-source-ref-wrap")) $("col-source-ref-wrap").style.display = source === "creator" ? "" : "none";
  if ($("collection-create-title")) {
    $("collection-create-title").textContent = channelsAccountOnly
      ? "新建视频号本账号作品采集"
      : biliAccountOnly
      ? "新建哔哩哔哩本账号作品采集"
      : igAccountOnly
      ? "新建 Instagram 本账号作品采集"
      : twAccountOnly
      ? "新建 Twitter 本账号作品采集"
      : favorite
      ? `新建${platformName}收藏采集`
      : source === "creator"
        ? `新建${platformName}创作者采集`
        : `新建${platformName}关键词采集`;
  }
  if ($("collection-create-sub")) $("collection-create-sub").textContent = channelsAccountOnly ? "同步当前登录账号的视频与图文，并按需采集评论与解密媒体" : biliAccountOnly ? "同步当前登录账号的投稿稿件，并按需采集评论与下载视频" : igAccountOnly ? "同步当前登录账号的帖子与 Reels，并按需采集评论与下载原图/视频" : twAccountOnly ? "同步当前登录账号的推文与媒体，并按需采集回复与下载高清原图/视频" : channels ? "通过微信原生 Finder 协议采集作品、解密视频并拉取全量评论线程" : favorite ? "导入收藏笔记并继续抓取详情与评论" : source === "creator" ? "导入创作者公开作品并抓取评论" : xhs ? "批量搜索笔记并抓取评论" : tiktok ? "通过离线签名 API 批量搜索 TikTok 视频并抓取评论" : PLATFORM === "bilibili" ? "通过离线 WBI 签名接口批量搜索视频并抓取评论" : PLATFORM === "instagram" ? "通过 Instagram 双栈协议接口批量搜索话题/帖子并抓取评论" : PLATFORM === "twitter" ? "通过 Twitter 混合协议搜索推文并采集回复线程" : "批量搜索视频并抓取评论";
  if ($("nav-collections-label")) $("nav-collections-label").textContent = channels ? "作品批量采集" : "关键词采集";
  if ($("col-content-limit-label")) $("col-content-limit-label").textContent = accountWorksOnly ? "作品上限" : "每词作品上限";
  if ($("col-page-limit-label")) $("col-page-limit-label").innerHTML = accountWorksOnly ? '作品页采集深度 <span class="field-scope">1–40 页</span>' : '每词采集深度 <span class="field-scope">1–40 页</span>';
  if ($("col-sort-wrap")) $("col-sort-wrap").style.display = (accountWorksOnly || source === "creator") ? "none" : "";
  if ($("col-page-help")) $("col-page-help").textContent = channelsAccountOnly ? "依次读取视频与图文管理页(或复用微信原生 Finder 主页协议)。" : biliAccountOnly ? "依次读取创作中心稿件列表接口分页。" : igAccountOnly ? "依次读取 Instagram 用户时间线 feed/user 分页。" : twAccountOnly ? "依次读取当前账号时间线 UserTweets 分页。" : channels ? "一页对应一次微信原生 Finder 搜索或主页翻页请求。" : (xhs || PLATFORM === "rednote") ? "一页对应一次搜索结果下滑或接口翻页。" : PLATFORM === "bilibili" ? "一页对应一次 WBI 搜索或 UP 主空间投稿接口翻页。" : PLATFORM === "instagram" ? "一页对应一次 tags/sections 或 feed/user 接口翻页。" : PLATFORM === "twitter" ? "一页对应一次 SearchTimeline 或 UserTweets 时间线翻页。" : "一页对应一次搜索结果下滑。";
  if ($("col-source-ref")) $("col-source-ref").placeholder = PLATFORM === "douyin" ? "抖音 sec_uid" : PLATFORM === "xhs" ? "小红书 user_id" : PLATFORM === "rednote" ? "海外小红书 user_id / www.rednote.com 主页链接" : PLATFORM === "shipinhao" ? "视频号 v2_...@finder / sph 标识 / 主页链接" : PLATFORM === "bilibili" ? "哔哩哔哩 UP 主 mid / space 主页链接 / 视频分享文案" : PLATFORM === "instagram" ? "Instagram @username / 主页链接 / 数字 UID" : PLATFORM === "twitter" ? "Twitter @username / x.com 主页链接 / 数字 UID" : "快手 user_id / 主页链接 / 短链 / 分享文案";
  resetCollectionCreatorState();
  if ($("col-replies-help")) $("col-replies-help").textContent = `采集${platformName}当前可返回的回复`;
  if ($("collection-callout-title")) $("collection-callout-title").textContent = channelsAccountOnly ? "支持创作者助手本账号同步与微信原生 Finder 协议增强" : channels ? "连接本机已登录微信即可搜索、采主页、抓全量评论与解密下载（无需网页账号）" : "作品上限、采集深度和停止条件任一满足即结束";
  if ($("collection-callout-copy")) $("collection-callout-copy").textContent = channels
    ? "视频号采用双通道架构：「关键词搜索 / 创作者主页」直接走「设置 → 视频号微信通道」一键连接的本机微信，无需登录创作者助手；「本账号作品」则读取创作者助手中已登录的本账号视频与图文。"
    : tiktok
    ? "TikTok 当前默认 native：关键词、评论和二级回复均由离线签名 API 直连，全程不打开浏览器；只有在设置中手动选择 auto/browser 才会使用页面。直播不在采集范围；视频发布请使用独立发布入口。"
    : PLATFORM === "bilibili"
    ? "哔哩哔哩默认使用离线 WBI 签名 API 直连搜索视频、读取 UP 主空间与本账号稿件、抓取主评论与楼中楼回复，并携带 Referer 下载渐进式 MP4 媒体；仅在普通接口兼容失败时按设置回退浏览器。"
    : PLATFORM === "instagram"
    ? "Instagram 默认使用 Mobile v1 / Web API 直连搜索标签与关键词、读取创作者主页与 Reels、通过 stream_comments 抓取评论与回复，并按序号保存单帖或混合轮播原图/视频；普通兼容失败时自动回退独立浏览器。"
    : PLATFORM === "twitter"
    ? "Twitter 默认结合轻量 Syndication/REST 与独立浏览器拦截 GraphQL 时间线，支持关键词搜索、创作者主页、本账号推文及回复线程采集，并自动选取最高码率 MP4 与 ?name=orig 原图下载。"
    : xhs
    ? "小红书采集复用所选账号的可见浏览器或显式 API 读取模式；最新、类型、时间和数据门槛会在入库前复核；任务串行执行，可随时取消，已入库结果会保留。"
    : PLATFORM === "kuaishou"
      ? "快手采集复用所选账号的登录浏览器，拦截页面签名后的搜索、主页和评论响应；类型、时间和数据门槛会在入库前复核。"
      : "抖音采集会临时打开可见浏览器窗口；平台筛选后还会在本地复核类型、时间和数据门槛；任务串行执行，可随时取消，已入库结果会保留。";
}
function collectionStatus(status) {
  const labels = { pending: "等待中", running: "采集中", done: "已完成", partial: "部分完成", failed: "失败", canceled: "已取消" };
  const classes = { pending: "pending", running: "downloading", done: "done", partial: "pending", failed: "failed", canceled: "skipped" };
  return { label: labels[status] || status, cls: classes[status] || "skipped" };
}
function collectionLastError(job) {
  const lines = String(job && job.error || "").split(/\r?\n/).map(line => line.trim()).filter(Boolean);
  return lines.length ? lines[lines.length - 1] : "";
}
function collectionDate(value) {
  if (!value) return "—";
  const parsed = new Date(value.endsWith && value.endsWith("Z") ? value : value + "Z");
  return Number.isNaN(parsed.getTime()) ? esc(value) : parsed.toLocaleString();
}
async function createCollection() {
  const keywords = collectionKeywords();
  const sourceType = collectionSourceValue();
  const board = $("col-board");
  if (sourceType === "creator") {
    const normalizedCreator = await normalizeCollectionCreatorTarget(null, { quiet: true });
    if (normalizedCreator === null) return;
  }
  const sourceRef = sourceType === "favorite_board" ? (board?.value || "") : sourceType === "creator" ? ($("col-source-ref")?.value.trim() || "") : "";
  const sourceName = sourceType === "account_works" ? "本账号作品" : sourceType === "favorite_all" ? "全部收藏" : sourceType === "favorite_board" ? (board?.selectedOptions?.[0]?.textContent || "").replace(/（[\d,.万亿]+）$/, "") : sourceType === "creator" ? `创作者 ${sourceRef}` : "";
  const requiresAccount = collectionRequiresAccount(PLATFORM, sourceType);
  const accountId = requiresAccount || PLATFORM !== "shipinhao" ? Number($("col-account").value || 0) : 0;
  const contentLimit = Number($("col-content-limit").value || 0);
  const commentLimit = Number($("col-comment-limit").value || 0);
  const pageLimit = Number($("col-page-limit").value || 0);
  const stagnantPages = Number($("col-stagnant-pages").value || 0);
  const minLikes = Number($("col-min-likes").value || 0);
  const minComments = Number($("col-min-comments").value || 0);
  let valid = true;
  valid = setFieldError($("col-keywords"), sourceType === "keyword" && !keywords.length ? "请至少填写一个关键词" : keywords.length > 20 ? "单个任务最多 20 个关键词" : "") && valid;
  if (sourceType === "favorite_board") valid = setFieldError(board, !sourceRef ? "请选择收藏夹" : "") && valid;
  if (sourceType === "creator") valid = setFieldError($("col-source-ref"), !sourceRef ? "请填写创作者用户 ID" : "") && valid;
  valid = requiresAccount
    ? setFieldError($("col-account"), !accountId ? "请选择一个已登录账号" : "") && valid
    : setFieldError($("col-account"), "") && valid;
  valid = setFieldError($("col-content-limit"), contentLimit < 1 || contentLimit > 100 ? "请输入 1–100" : "") && valid;
  valid = setFieldError($("col-comment-limit"), commentLimit < 0 || commentLimit > 200 ? "请输入 0–200" : "") && valid;
  valid = setFieldError($("col-page-limit"), pageLimit < 1 || pageLimit > 40 ? "请输入 1–40" : "") && valid;
  valid = setFieldError($("col-stagnant-pages"), stagnantPages < 1 || stagnantPages > 8 ? "请输入 1–8" : "") && valid;
  valid = setFieldError($("col-min-likes"), minLikes < 0 ? "请输入非负整数" : "") && valid;
  valid = setFieldError($("col-min-comments"), minComments < 0 ? "请输入非负整数" : "") && valid;
  if (!valid) {
    const form = $("col-keywords").closest(".card");
    const first = form?.querySelector('[aria-invalid="true"]');
    if (first) first.focus();
    return;
  }
  const btn = evtBtn();
  await withBusy(btn, "创建中", async () => {
    try {
      const job = await api("/api/collections", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          platform: PLATFORM, account_id: accountId, keywords,
          source_type: sourceType, source_ref: sourceRef, source_name: sourceName,
          max_contents_per_keyword: contentLimit,
          max_pages_per_keyword: pageLimit,
          stagnant_pages: stagnantPages,
          search_sort: $("col-sort").value || "general",
          publish_time: $("col-publish-time").value || "all",
          content_type: $("col-content-type").value || "all",
          min_likes: minLikes,
          min_comments: minComments,
          max_comments_per_content: commentLimit,
          include_replies: $("col-replies").checked,
          download_media: $("col-download").checked,
          video_quality: $("col-quality").value || "highest",
          download_dir: $("col-download-dir").value.trim(),
        }),
      });
      $("col-create-msg").textContent = `任务 #${job.id} 已进入队列`;
      if (sourceType === "keyword") $("col-keywords").value = "";
      toast(`${sourceType === "account_works" ? "本账号作品" : sourceType === "keyword" ? "关键词" : sourceType === "creator" ? "创作者" : "收藏"}采集任务已创建`, "ok");
      globalThis.CreatorHubWorkbench?.completed?.("collections");
      await refreshCollections();
    } catch (e) {
      $("col-create-msg").textContent = "创建失败：" + e.message;
      toast("创建失败：" + e.message, "err");
    }
  });
}
function collectionTaskSkeleton(count = 3) {
  return Array.from({ length: count }, () => `<div class="collection-task-skeleton" aria-hidden="true">
    <span class="sk" style="height:28px"></span><span class="sk" style="height:42px"></span>
    <span class="sk" style="height:42px"></span><span class="sk" style="height:52px"></span>
  </div>`).join("");
}
function collectionTaskEmpty() {
  return `<div class="empty collection-task-empty"><div class="empty-ic">${ic("i-hash")}</div>
    <div class="empty-t">还没有采集任务</div><div class="empty-sub">可按平台支持的来源创建作品采集任务。</div><button class="ghost sm" data-open-composer="collections">新建采集任务</button></div>`;
}
function collectionProgress(job) {
  if (job.status === "done") return 100;
  const totalKeywords = Math.max(0, Number(job.total_keyword_count || 0));
  if (!totalKeywords) return 0;
  const processedKeywords = Math.min(totalKeywords, Math.max(0, Number(job.processed_keyword_count || 0)));
  const itemTotal = Math.max(0, Number(job.current_item_total || 0));
  const itemIndex = Math.min(itemTotal, Math.max(0, Number(job.current_item_index || 0)));
  const currentFraction = itemTotal && processedKeywords < totalKeywords ? itemIndex / itemTotal : 0;
  return Math.min(100, Math.round((processedKeywords + currentFraction) * 100 / totalKeywords));
}
function openCollectionVerifyBrowser(jobId) {
  const job = COLLECTION_JOBS.find(item => item.id === Number(jobId));
  if (!job || !job.account_id) return;
  let targetUrl = "";
  if (job.platform === "douyin" && job.source_type === "keyword") {
    const kw = String(job.current_keyword || (job.keywords && job.keywords[0]) || "").trim();
    if (kw) targetUrl = "https://www.douyin.com/search/" + encodeURIComponent(kw);
  }
  openAccountBrowser(job.account_id, targetUrl);
}
let COLLECTION_RENDERED_JOB_ID = 0;
let COLLECTION_RENDERED_SIG = "";
function collectionJobResultSig(job, page = COLLECTION_PAGE) {
  if (!job) return "";
  return [
    job.id,
    Math.max(1, Number(page) || 1),
    job.status,
    Number(job.content_count || 0),
    Number(job.comment_count || 0),
    Number(job.error_count || 0),
    Number(job.current_item_index || 0),
  ].join(":");
}
function renderCollectionJobs() {
  const body = $("collection-job-table"); if (!body) return;
  const html = COLLECTION_JOBS.map(job => {
    const status = collectionStatus(job.status);
    const percent = collectionProgress(job);
    const sourceLabel = job.source_type === "keyword" ? "关键词" : "来源";
    const keywordProgress = `${Number(job.processed_keyword_count || 0)}/${Number(job.total_keyword_count || 0)} ${sourceLabel}`;
    const itemProgress = Number(job.current_item_total || 0) > 0
      ? ` · 当前作品 ${Number(job.current_item_index || 0)}/${Number(job.current_item_total || 0)}` : "";
    const sourceValues = job.source_type === "keyword" ? (job.keywords || []) : [job.source_name || (job.source_type === "account_works" ? "本账号作品" : job.source_type === "favorite_all" ? "全部收藏" : "收藏夹")];
    const keywords = sourceValues.slice(0, 5).map(k => `<span class="meta-chip">${esc(k)}</span>`).join("") +
      (sourceValues.length > 5 ? `<span class="meta-chip more">+${sourceValues.length - 5}</span>` : "");
    const canCancel = ["pending", "running"].includes(job.status);
    const canRetry = ["done", "partial", "failed", "canceled"].includes(job.status);
    const canEdit = canRetry && ["douyin", "xhs", "rednote", "kuaishou", "shipinhao", "tiktok", "bilibili", "instagram", "twitter"].includes(job.platform);
    const needsManualClear = !!(job.manual_review_required && Number(job.account_id) > 0);
    const errorText = collectionLastError(job);
    const sortLabel = { general: "综合", latest: "最新", most_liked: "最多点赞" }[job.search_sort] || "综合";
    const timeLabel = { all: "不限时间", day: "一天内", week: "一周内", half_year: "半年内" }[job.publish_time] || "不限时间";
    const typeLabel = { all: "全部类型", video: "视频", images: "图文" }[job.content_type] || "全部类型";
    const threshold = [Number(job.min_likes) > 0 ? `≥${fmtNum(job.min_likes)} 赞` : "", Number(job.min_comments) > 0 ? `≥${fmtNum(job.min_comments)} 评` : ""].filter(Boolean).join(" · ");
    return `<article class="collection-task" role="listitem" aria-label="任务 ${job.id}，${status.label}">
      <div class="collection-task-meta"><div><span class="collection-task-label">任务状态</span><span class="pill ${status.cls}">${status.label}</span></div><time class="collection-task-created" datetime="${esc(job.created_at || "")}">${collectionDate(job.created_at)}</time></div>
      <div class="collection-task-keywords"><span class="collection-task-label">${sourceLabel}</span><div class="keyword-stack">${keywords}</div>${job.current_keyword ? `<div class="collection-step">当前：${esc(job.current_keyword)}</div>` : ""}</div>
      <div class="collection-task-config"><span class="collection-task-label">采集配置</span><div class="collection-task-config-main">${job.max_contents_per_keyword} 作品/${job.source_type === "keyword" ? "词" : "来源"} · ${job.max_pages_per_keyword || 12} 深度页${job.source_type === "keyword" ? ` · ${sortLabel}` : ""}</div><div class="collection-task-config-sub">${timeLabel} · ${typeLabel}${threshold ? ` · ${threshold}` : ""} · ${job.max_comments_per_content} 评论/作品<br>${job.include_replies ? "含二级评论 · " : ""}${job.download_media ? "下载媒体" : "仅采数据"} · 连续 ${job.stagnant_pages || 3} 页无新增停止</div></div>
      <div class="collection-task-progress"><span class="collection-task-label">执行进度</span><div class="job-progress"><div class="job-progress-head"><span>${esc(job.current_step || "等待执行")}</span><b>${percent}%</b></div><div class="progress-track" role="progressbar" aria-label="任务 ${job.id} 进度" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${percent}"><div class="progress-fill" style="width:${percent}%"></div></div><div class="collection-step">${keywordProgress}${itemProgress} · 已采 ${fmtNum(job.content_count)} 作品 / ${fmtNum(job.comment_count)} 评论</div></div></div>
      ${job.error_count ? `<button type="button" class="collection-task-error" onclick="openCollectionResults(${job.id})" title="${esc(errorText)}">${ic("i-info")}<span class="collection-task-error-text">${job.error_count} 条异常 · ${esc(errorText)}</span><span class="collection-task-error-link">查看详情</span></button>` : ""}
      <div class="collection-task-actions" aria-label="任务 ${job.id} 操作">
        <button type="button" class="sm collection-task-primary" onclick="openCollectionResults(${job.id})">${ic("i-eye")}查看结果</button>
        ${needsManualClear ? `<button type="button" class="ghost sm" onclick="openCollectionVerifyBrowser(${job.id})" title="直接打开对应搜索/站点页面检查并完成验证">打开验证页</button><button type="button" class="ghost sm" onclick="clearRiskAccount(${job.account_id})" title="确认验证已完成或无需验证后，解除风控并立即恢复任务">解除风控继续</button>` : ""}
        ${canEdit ? `<button type="button" class="ghost sm" onclick="editCollection(${job.id})">${ic("i-settings")}编辑</button>` : ""}
        <button type="button" class="ghost sm" onclick="exportCollection(${job.id})"${job.content_count ? "" : " disabled"}>${ic("i-download")}导出</button>
        ${canCancel ? `<button type="button" class="ghost sm" onclick="cancelCollection(${job.id})">${ic("i-x")}取消任务</button>` : ""}
        ${canRetry ? `<button type="button" class="ghost sm" onclick="retryCollection(${job.id})">${ic("i-play")}续跑</button>` : ""}
        ${job.status !== "running" ? `<button type="button" class="ghost sm danger collection-task-delete" onclick="deleteCollection(${job.id})" aria-label="删除任务 ${job.id}">${ic("i-trash")}删除</button>` : ""}
      </div>
    </article>`;
  }).join("") || collectionTaskEmpty();
  setHtmlIfChanged(body, html);
}
async function refreshCollections() {
  const isCurrent = beginViewRequest("collections");
  if (!$("collection-job-table") || !["douyin", "xhs", "rednote", "kuaishou", "shipinhao", "tiktok", "bilibili", "instagram", "twitter"].includes(PLATFORM)) return;
  try {
    const requestedPlatform = PLATFORM;
    const jobs = await api("/api/collections?platform=" + encodeURIComponent(requestedPlatform));
    if (!isCurrent()) return;
    // Keep platform data isolated even if an older backend ignores the filter.
    COLLECTION_JOBS = (Array.isArray(jobs) ? jobs : [])
      .filter(job => job.platform === requestedPlatform);
    const active = COLLECTION_JOBS.filter(j => ["pending", "running"].includes(j.status)).length;
    if ($("tb-col")) $("tb-col").textContent = active || COLLECTION_JOBS.length;
    renderCollectionJobs();
    if (COLLECTION_JOB_ID) {
      const job = COLLECTION_JOBS.find(j => j.id === COLLECTION_JOB_ID);
      if (job) {
        updateCollectionResultStats(job);
        const nextSig = collectionJobResultSig(job, COLLECTION_PAGE);
        if (CURRENT_TAB === "collections" && nextSig !== COLLECTION_RENDERED_SIG) {
          await loadCollectionContents(COLLECTION_PAGE, true);
        }
      }
      else closeCollectionResults();
    }
  } catch (e) {
    if (isCurrent() && CURRENT_TAB === "collections") toast("采集任务刷新失败：" + e.message, "err");
  }
}
async function editCollection(jobId, draft = null) {
  const job = COLLECTION_JOBS.find(item => item.id === Number(jobId));
  if (!job) return;
  const platformName = PF_NAME[job.platform] || "平台";
  const accounts = ACCOUNTS.filter(a => a.platform === job.platform && a.status !== "invalid" && a.has_storage);
  const initial = draft || {
    account_id: job.account_id,
    source_type: job.source_type || "keyword",
    source_ref: job.source_ref || "",
    source_name: job.source_name || "",
    keywords: (job.keywords || []).join("\n"),
    max_contents_per_keyword: job.max_contents_per_keyword,
    max_pages_per_keyword: job.max_pages_per_keyword || 12,
    stagnant_pages: job.stagnant_pages || 3,
    search_sort: job.search_sort || "general",
    publish_time: job.publish_time || "all",
    content_type: job.content_type || "all",
    min_likes: Number(job.min_likes || 0),
    min_comments: Number(job.min_comments || 0),
    max_comments_per_content: job.max_comments_per_content,
    include_replies: !!job.include_replies,
    download_media: !!job.download_media,
    video_quality: job.video_quality || "highest",
    download_dir: job.download_dir || "",
  };
  const value = await new Promise(resolve => {
    _uiResolve = resolve; _uiCancelVal = null;
    $("ui-body").innerHTML = `
      <fieldset class="ui-form-group"><legend>采集范围</legend>
      ${initial.source_type === "keyword" ? `<div class="form-field"><label for="ecol-keywords">关键词 <span class="field-scope">最多 20 个</span></label>
        <textarea id="ecol-keywords" rows="5" placeholder="每行一个关键词">${esc(initial.keywords)}</textarea></div>` : `<div class="form-note"><svg aria-hidden="true"><use href="#i-library"/></svg><span>采集来源：<b>${esc(initial.source_name || (initial.source_type === "account_works" ? "本账号作品" : initial.source_type === "favorite_all" ? "全部收藏" : "收藏夹"))}</b>。来源不可在续跑时切换，可新建任务选择其他来源。</span></div>`}
      <div class="form-grid">
        ${collectionRequiresAccount(job.platform, initial.source_type) || job.platform !== "shipinhao"
          ? `<div class="form-field"><label for="ecol-account">使用账号</label><select id="ecol-account">${accOptions(accounts, !collectionRequiresAccount(job.platform, initial.source_type) ? "使用本机微信通道（无需网页账号）" : (accounts.length ? `请选择${platformName}账号` : `暂无可用${platformName}账号`))}</select></div>`
          : ""}
        ${job.platform === "douyin" ? '<div class="form-field"><label for="ecol-quality">视频画质</label><select id="ecol-quality"><option value="highest">原画 / 最高</option><option value="1080">1080P</option><option value="720">720P</option><option value="540">540P</option><option value="lowest">最低省流</option></select></div>' : ""}
        <div class="form-field"><label for="ecol-content-limit">每词作品上限</label><input id="ecol-content-limit" type="number" min="1" max="100" value="${Number(initial.max_contents_per_keyword) || 20}"></div>
        <div class="form-field"><label for="ecol-comment-limit">每作品评论上限</label><input id="ecol-comment-limit" type="number" min="0" max="200" value="${Number(initial.max_comments_per_content) || 0}"></div>
      </div>
      </fieldset>
      <fieldset class="ui-form-group"><legend>搜索与停止条件</legend>
      <div class="form-grid collection-filter-grid">
        <div class="form-field"><label for="ecol-page-limit">每词采集深度</label><input id="ecol-page-limit" type="number" min="1" max="40" value="${Number(initial.max_pages_per_keyword) || 12}"></div>
        <div class="form-field"><label for="ecol-sort">搜索排序</label><select id="ecol-sort"><option value="general">综合排序</option><option value="latest">最新发布</option><option value="most_liked">最多点赞</option></select></div>
        <div class="form-field"><label for="ecol-publish-time">发布时间</label><select id="ecol-publish-time"><option value="all">不限</option><option value="day">一天内</option><option value="week">一周内</option><option value="half_year">半年内</option></select></div>
        <div class="form-field"><label for="ecol-content-type">内容类型</label><select id="ecol-content-type"><option value="all">全部作品</option><option value="video">视频</option><option value="images">图文 / 图集</option></select></div>
        <div class="form-field"><label for="ecol-min-likes">最低点赞数</label><input id="ecol-min-likes" type="number" min="0" value="${Number(initial.min_likes) || 0}"></div>
        <div class="form-field"><label for="ecol-min-comments">最低评论数</label><input id="ecol-min-comments" type="number" min="0" value="${Number(initial.min_comments) || 0}"></div>
        <div class="form-field"><label for="ecol-stagnant-pages">连续无新增停止</label><input id="ecol-stagnant-pages" type="number" min="1" max="8" value="${Number(initial.stagnant_pages) || 3}"></div>
      </div>
      </fieldset>
      <fieldset class="ui-form-group"><legend>评论与下载</legend>
      <div class="option-grid" aria-label="采集选项">
        <label class="switch-row"><input type="checkbox" id="ecol-download"${initial.download_media ? " checked" : ""} onchange="$('ecol-dir-wrap').style.display=this.checked?'':'none'"><span class="switch-copy"><b>下载媒体</b><span>保存视频和封面来源</span></span></label>
        <label class="switch-row"><input type="checkbox" id="ecol-replies"${initial.include_replies ? " checked" : ""}><span class="switch-copy"><b>包含二级评论</b><span>采集${platformName}当前可返回的回复</span></span></label>
      </div>
      <div class="form-field" id="ecol-dir-wrap" style="display:${initial.download_media ? "" : "none"}"><label for="ecol-download-dir">下载目录（可选）</label><input id="ecol-download-dir" value="${esc(initial.download_dir)}" placeholder="留空使用默认目录"></div>
      </fieldset>`;
    if ($("ecol-account")) $("ecol-account").value = String(initial.account_id || "");
    if ($("ecol-quality")) $("ecol-quality").value = initial.video_quality || "highest";
    $("ecol-sort").value = initial.search_sort || "general";
    $("ecol-publish-time").value = initial.publish_time || "all";
    $("ecol-content-type").value = initial.content_type || "all";
    enhanceAllSelects($("ui-body")); csSyncAll();
    _uiGetVal = () => ({
      account_id: $("ecol-account") ? Number($("ecol-account").value || 0) : 0,
      source_type: initial.source_type,
      source_ref: initial.source_ref,
      source_name: initial.source_name,
      keywords: $("ecol-keywords") ? $("ecol-keywords").value : "",
      max_contents_per_keyword: Number($("ecol-content-limit").value || 0),
      max_pages_per_keyword: Number($("ecol-page-limit").value || 0),
      stagnant_pages: Number($("ecol-stagnant-pages").value || 0),
      search_sort: $("ecol-sort").value || "general",
      publish_time: $("ecol-publish-time").value || "all",
      content_type: $("ecol-content-type").value || "all",
      min_likes: Number($("ecol-min-likes").value || 0),
      min_comments: Number($("ecol-min-comments").value || 0),
      max_comments_per_content: Number($("ecol-comment-limit").value || 0),
      include_replies: $("ecol-replies").checked,
      download_media: $("ecol-download").checked,
      video_quality: $("ecol-quality") ? ($("ecol-quality").value || "highest") : "highest",
      download_dir: $("ecol-download-dir").value.trim(),
    });
    _uiOpen(`编辑采集任务 #${job.id}`, "已有作品和评论会保留。保存后点击「续跑」应用新配置，系统会自动去重。", {
      okText: "保存配置", wide: true, icon: "i-library",
      submit: value => {
        const keywords = parseCollectionKeywords(value.keywords);
        if (value.source_type === "keyword" && !keywords.length) uiEditorError("请至少填写一个关键词", "ecol-keywords");
        if (keywords.length > 20) uiEditorError("单个任务最多 20 个关键词", "ecol-keywords");
        if (collectionRequiresAccount(job.platform, value.source_type) && !value.account_id) uiEditorError(`请选择一个可用${platformName}账号`, "ecol-account");
        if (value.max_contents_per_keyword < 1 || value.max_contents_per_keyword > 100) uiEditorError("每词作品上限须为 1–100", "ecol-content-limit");
        if (value.max_pages_per_keyword < 1 || value.max_pages_per_keyword > 40) uiEditorError("每词采集深度须为 1–40 页", "ecol-page-limit");
        if (value.stagnant_pages < 1 || value.stagnant_pages > 8) uiEditorError("连续无新增停止阈值须为 1–8 页", "ecol-stagnant-pages");
        if (value.min_likes < 0 || value.min_comments < 0) uiEditorError("点赞和评论门槛须为非负整数", "ecol-min-likes");
        if (value.max_comments_per_content < 0 || value.max_comments_per_content > 200) uiEditorError("每作品评论上限须为 0–200", "ecol-comment-limit");
        return api(`/api/collections/${job.id}`, {
          method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ...value, platform: job.platform, keywords }),
        });
      },
    });
  });
  if (value === null) return;
  toast("任务配置已保存，点击“续跑”后生效", "ok"); refreshCollections();
}
function updateCollectionResultStats(job) {
  if (!job) return;
  $("col-stat-content").textContent = fmtNum(job.content_count);
  $("col-stat-comments").textContent = fmtNum(job.comment_count);
  $("col-stat-errors").textContent = fmtNum(job.error_count);
  const errorBox = $("collection-results-error");
  const errorText = collectionLastError(job);
  errorBox.style.display = errorText ? "" : "none";
  errorBox.textContent = errorText ? `最近异常：${errorText}` : "";
  $("collection-results-title").childNodes[0].nodeValue = `任务 #${job.id} 采集结果 `;
  $("collection-results-sub").textContent = `${job.source_type === "keyword" ? (job.keywords || []).join("、") : (job.source_name || "收藏来源")} · ${collectionStatus(job.status).label}`;
}
async function openCollectionResults(jobId) {
  const btn = evtBtn();
  const nextJobId = Number(jobId);
  const sameRenderedJob = COLLECTION_RENDERED_JOB_ID === nextJobId;
  COLLECTION_JOB_ID = nextJobId; COLLECTION_PAGE = 1;
  window.dispatchEvent(new Event("creatorhub:agent-context"));
  const job = COLLECTION_JOBS.find(j => j.id === COLLECTION_JOB_ID);
  if (job) updateCollectionResultStats(job);
  $("collection-results-card").style.display = "";
  globalThis.CreatorHubWorkbench?.collectionDetail?.(true);
  const targetSig = collectionJobResultSig(job, 1);
  const hasCachedDom = sameRenderedJob && targetSig && targetSig === COLLECTION_RENDERED_SIG
    && Boolean($("collection-content-list")?.querySelector(".collection-result-item"));
  if (hasCachedDom) {
    const reducedMotion = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    $("collection-results-card").scrollIntoView({ behavior: reducedMotion ? "auto" : "smooth", block: "start" });
    return;
  }
  await withBusy(btn, "加载中", async () => {
    await loadCollectionContents(1, sameRenderedJob);
    const reducedMotion = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    $("collection-results-card").scrollIntoView({ behavior: reducedMotion ? "auto" : "smooth", block: "start" });
  });
}
function closeCollectionResults() {
  COLLECTION_JOB_ID = 0; COLLECTION_PAGE = 1;
  window.dispatchEvent(new Event("creatorhub:agent-context"));
  if ($("collection-results-card")) $("collection-results-card").style.display = "none";
  globalThis.CreatorHubWorkbench?.collectionDetail?.(false);
}
function collectionResultSkeleton(count = 4) {
  return Array.from({ length: count }, () => `<div class="collection-result-skeleton" aria-hidden="true">
    <span class="sk" style="height:120px"></span><span class="sk" style="width:72%;margin-top:14px"></span>
    <span class="sk" style="width:92%;margin-top:10px"></span><span class="sk" style="height:42px;margin-top:18px"></span>
  </div>`).join("");
}
function collectionEmpty(title, detail = "") {
  return `<div class="empty collection-result-empty"><div class="empty-ic">${ic("i-film")}</div>
    <div class="empty-t">${esc(title)}</div>${detail ? `<div class="empty-sub">${esc(detail)}</div>` : ""}</div>`;
}
function collectionFileDisplay(item) {
  const path = String(item.local_path || "").trim();
  const pathMeta = contentPathMeta(item);
  const count = Number(item.media_count || 0);
  const name = count > 1 ? `${count} 个媒体文件` : (pathMeta ? pathMeta.name + (pathMeta.ext ? `.${pathMeta.ext}` : "") : "本地媒体");
  const bits = [item.file_size ? fmtShareSize(item.file_size) : "", pathMeta && pathMeta.dir ? pathMeta.dir : ""].filter(Boolean);
  return { path, name, meta: bits.join(" · ") || "本地文件可用" };
}
function collectionResultCard(item) {
  const title = item.desc || item.aweme_id || "未命名作品";
  const isGallery = item.media_type === "images";
  const isTextOnly = item.platform === "twitter" && !item.media_count && !item.cover_url && item.media_type !== "video";
  const typeLabel = isTextOnly ? "纯文本" : (isGallery ? `图集${item.media_count > 1 ? ` · ${item.media_count} 张` : ""}` : "视频");
  const canPreview = Boolean(item.preview_available);
  const file = collectionFileDisplay(item);
  const fileExists = Boolean(item.local_exists);
  const downloadClass = fileExists ? "done" : (!isTextOnly && item.download_status === "failed") ? "failed" : "skipped";
  const downloadLabel = fileExists ? "已下载" : isTextOnly ? "纯文字推文" : item.download_status === "failed" ? "下载失败" : item.download_status === "done" ? "文件缺失" : "未下载";
  const byline = [item.author_name || "未知作者", item.create_time ? fmtTime(item.create_time) : "", item.aweme_id || ""].filter(Boolean);
  const authorId = String(item.author_id || "").trim();
  const authorRow = authorId
    ? `<div class="collection-author-row"><button type="button" class="collection-author-chip" data-author-id="${esc(authorId)}" onclick="copyCollectionAuthorId(this)" title="点击复制创作者 ID：${esc(authorId)}" aria-label="复制创作者 ID：${esc(authorId)}"><span>创作者 ID: ${esc(authorId)}</span>${ic("i-copy")}</button></div>`
    : "";
  const cover = item.cover_url
    ? `<img src="${esc(safeMediaUrl(item.cover_url))}" alt="${esc(title.slice(0, 60))}封面" loading="lazy" referrerpolicy="no-referrer">`
    : `<span class="collection-cover-empty">${ic(isGallery ? "i-image" : "i-film")}</span>`;
  const filePanel = fileExists ? `<div class="collection-file-panel" title="${esc(file.path)}">
    <div class="collection-file-main">${ic("i-folder")}<div class="collection-file-info">
      <div class="collection-file-name">${esc(file.name)}</div><div class="collection-file-meta">${esc(file.meta)}</div>
    </div></div>
    <div class="collection-file-actions">
      <button type="button" class="ghost collection-icon-action" onclick="openCollectionFile(${item.job_id},${item.id},this)" data-tip="用本机默认程序打开" aria-label="用本机默认程序打开">${ic("i-external")}</button>
      <button type="button" class="ghost collection-icon-action" onclick="revealCollectionFile(${item.job_id},${item.id},this)" data-tip="在文件夹中显示" aria-label="在文件夹中显示">${ic("i-folder")}</button>
      <button type="button" class="ghost collection-icon-action" data-path="${esc(file.path)}" onclick="copyCollectionPath(this)" data-tip="复制本地路径" aria-label="复制本地路径">${ic("i-copy")}</button>
    </div>
  </div>` : `<div class="collection-download-note${(!isTextOnly && item.download_status === "failed") ? " failed" : ""}">${isTextOnly ? "纯文字推文（无图片/视频附件），已完整采集正文与评论" : (item.error ? esc(item.error) : "本地文件尚不可用，可先预览平台媒体或打开原作品")}</div>`;
  return `<article class="collection-result-item">
    <button type="button" class="collection-result-cover" onclick="openCollectionPreview(${item.job_id},${item.id})" aria-label="预览：${esc(title.slice(0, 80))}"${canPreview ? "" : " disabled"}>
      ${cover}<span class="collection-cover-type">${esc(typeLabel)}</span>
      <span class="collection-cover-preview">${ic("i-play")}${canPreview ? "站内预览" : "暂无预览"}</span>
    </button>
    <div class="collection-result-body">
      <div class="collection-result-tags"><span class="meta-chip group">#${esc(item.keyword)}</span><span class="pill ${downloadClass}" title="${esc(item.error || "")}">${downloadLabel}</span></div>
      <a class="collection-result-title" href="${esc(item.url)}" target="_blank" rel="noopener noreferrer" title="${esc(title)}">${esc(title)}</a>
      <div class="collection-result-byline">${byline.map((part, index) => `<span${index === byline.length - 1 ? ' class="collection-result-id"' : ""}>${esc(part)}</span>`).join("<span>·</span>")}</div>
      ${authorRow}
      <div class="collection-result-metrics" aria-label="作品数据">
        <div class="collection-result-metric"><b>${ic("i-heart")}${fmtNum(item.like_count)}</b><span>点赞</span></div>
        <div class="collection-result-metric"><b>${ic("i-msg")}${fmtNum(item.comment_count)}</b><span>平台评论</span></div>
        <div class="collection-result-metric"><b>${ic("i-inbox")}${fmtNum(item.collected_comment_count)}</b><span>已采评论</span></div>
      </div>
      ${filePanel}
      <div class="collection-result-actions">
        <button type="button" class="sm collection-preview-primary" onclick="openCollectionPreview(${item.job_id},${item.id})"${canPreview ? "" : " disabled"}>${ic("i-eye")}预览</button>
        ${fileExists ? `<button type="button" class="ghost sm" onclick="openCollectionFile(${item.job_id},${item.id},this)">${ic("i-external")}打开文件</button>` : ""}
        <button type="button" class="ghost sm" onclick="showCollectionComments(${item.id})"${item.collected_comment_count ? "" : " disabled"}>${ic("i-msg")}评论 ${item.collected_comment_count}</button>
        <a class="collection-source-link" href="${esc(item.url)}" target="_blank" rel="noopener noreferrer">${ic("i-external")}原作品</a>
      </div>
    </div>
  </article>`;
}
function openCollectionPreview(jobId, contentId, startIdx = 0) {
  return _pvOpen(() => api(`/api/collections/${jobId}/contents/${contentId}/media`), startIdx);
}
async function collectionLocalAction(jobId, contentId, action, button) {
  const old = button && button.innerHTML;
  if (button) { button.disabled = true; button.innerHTML = `<span class="spin"></span>`; }
  try {
    await api(`/api/collections/${jobId}/contents/${contentId}/${action}`, {
      method: "POST", headers: { "X-CreatorHub-Local-Action": action },
    });
    toast(action === "open" ? "已调用本机默认程序打开文件" : "已在文件夹中显示", "ok", 1800);
  } catch (e) {
    toast((action === "open" ? "打开文件失败：" : "打开文件夹失败：") + e.message, "err");
  } finally {
    if (button && button.isConnected) { button.disabled = false; button.innerHTML = old; }
  }
}
function openCollectionFile(jobId, contentId, button) { return collectionLocalAction(jobId, contentId, "open", button); }
function revealCollectionFile(jobId, contentId, button) { return collectionLocalAction(jobId, contentId, "reveal", button); }
function copyCollectionAuthorId(button) {
  return copyTextValue(
    button?.dataset?.authorId || "",
    "创作者 ID 已复制，可用于创作者采集或作品监控",
    "复制失败，请手动复制创作者 ID",
  );
}
function copyCollectionPath(button) {
  return copyTextValue(
    button?.dataset?.path || "",
    "本地路径已复制",
    "复制失败，请手动复制路径",
  );
}
async function loadCollectionContents(page = COLLECTION_PAGE, quiet = false) {
  if (!COLLECTION_JOB_ID) return;
  const targetJobId = COLLECTION_JOB_ID;
  COLLECTION_PAGE = Math.max(1, Number(page) || 1);
  const isCurrent = beginViewRequest("collection-contents", () => `${COLLECTION_JOB_ID}:${COLLECTION_PAGE}`);
  const body = $("collection-content-list");
  if (!quiet) setHtmlIfChanged(body, collectionResultSkeleton(4));
  try {
    const result = await api(`/api/collections/${targetJobId}/contents?page=${COLLECTION_PAGE}&page_size=20`);
    if (!isCurrent()) return;
    COLLECTION_PAGE = result.page;
    const html = (result.items || []).map(collectionResultCard).join("") || collectionEmpty("任务暂时没有作品结果", "采集中可稍后刷新；失败任务可查看错误并续跑");
    setHtmlIfChanged(body, html);
    const job = COLLECTION_JOBS.find(j => j.id === targetJobId);
    COLLECTION_RENDERED_JOB_ID = targetJobId;
    COLLECTION_RENDERED_SIG = collectionJobResultSig(job, COLLECTION_PAGE);
    $("collection-result-count").textContent = `共 ${fmtNum(result.total)} 个作品`;
    const pager = $("collection-pager");
    setHtmlIfChanged(pager, `<button class="ghost sm" onclick="loadCollectionContents(${result.page - 1})"${result.page <= 1 ? " disabled" : ""}>${ic("i-prev")}上一页</button><span class="mut">第 ${result.page} / ${result.pages} 页 · 共 ${fmtNum(result.total)} 条</span><button class="ghost sm" onclick="loadCollectionContents(${result.page + 1})"${result.page >= result.pages ? " disabled" : ""}>下一页${ic("i-next")}</button>`);
    pager.hidden = result.pages <= 1;
  } catch (e) {
    if (!isCurrent()) return;
    COLLECTION_RENDERED_SIG = "";
    setHtmlIfChanged(body, collectionEmpty("结果加载失败", e.message));
    $("collection-result-count").textContent = "";
  }
}
function collectionCommentParams({ exportMode = false } = {}) {
  const params = new URLSearchParams();
  const scope = $("collection-comments-scope");
  const contentId = scope ? String(scope.value || "") : String(COLLECTION_COMMENT_CONTENT_ID || "");
  const q = $("collection-comments-q")?.value.trim() || "";
  const region = $("collection-comments-region")?.value.trim() || "";
  const secUid = $("collection-comments-secuid")?.value.trim() || "";
  const dedupe = Boolean($("collection-comments-dedupe")?.checked);
  if (contentId) params.set("content_id", contentId);
  if (q) params.set("q", q);
  if (region) params.set("ip_label", region);
  if (secUid) params.set("user_sec_uid", secUid);
  if (dedupe) params.set("dedupe_users", "true");
  if (!exportMode) {
    params.set("paginate", "true");
    params.set("page", String(COLLECTION_COMMENT_PAGE));
    params.set("page_size", "40");
  }
  return params;
}
function collectionCommentMarkup(comment) {
  const source = [comment.source_keyword ? `#${comment.source_keyword}` : "", comment.source_desc || ""]
    .filter(Boolean).join(" · ");
  return `<article class="collection-comment"><div class="collection-comment-head"><div><b>${esc(comment.user_nickname || "匿名用户")}</b>${comment.ip_label ? ` · ${esc(comment.ip_label)}` : ""}${comment.user_sec_uid ? `<div class="comment-user-sec" title="${esc(comment.user_sec_uid)}">sec_uid: ${esc(comment.user_sec_uid)}</div>` : ""}${source ? `<div class="collection-comment-source">${esc(source)}</div>` : ""}</div><span>${fmtTime(comment.create_time)} · ${fmtNum(comment.like_count)} 赞${comment.reply_to ? " · 回复" : ""}</span></div><p>${esc(comment.text || "（空评论）")}</p></article>`;
}
async function loadCollectionComments(page = COLLECTION_COMMENT_PAGE) {
  const modal = $("collection-comments-modal");
  const list = $("collection-comments-list");
  if (!modal || !list || !COLLECTION_JOB_ID) return;
  COLLECTION_COMMENT_PAGE = Math.max(1, Number(page) || 1);
  const request = ++COLLECTION_COMMENT_REQUEST;
  list.innerHTML = `<div class="empty"><div class="empty-t">加载中…</div></div>`;
  try {
    const result = await api(`/api/collections/${COLLECTION_JOB_ID}/comments?${collectionCommentParams().toString()}`);
    if (request !== COLLECTION_COMMENT_REQUEST || modal.style.display === "none") return;
    const payload = Array.isArray(result)
      ? { items: result, total: result.length, page: 1, pages: 1, has_prev: false, has_next: false }
      : result;
    COLLECTION_COMMENT_PAGE = Number(payload.page || 1);
    $("collection-comments-count").textContent = `共 ${fmtNum(payload.total || 0)} 条符合条件的评论`;
    list.innerHTML = (payload.items || []).map(collectionCommentMarkup).join("") || `<div class="empty"><div class="empty-t">没有符合条件的评论</div><div class="empty-sub">调整筛选条件后重试</div></div>`;
    const pager = $("collection-comments-pager");
    if (pager) {
      pager.hidden = Number(payload.pages || 1) <= 1;
      pager.innerHTML = `<button type="button" class="ghost sm" onclick="loadCollectionComments(${Math.max(1, Number(payload.page || 1) - 1)})"${payload.has_prev ? "" : " disabled"}>${ic("i-prev")}上一页</button><span class="mut">第 ${payload.page || 1} / ${payload.pages || 1} 页</span><button type="button" class="ghost sm" onclick="loadCollectionComments(${Number(payload.page || 1) + 1})"${payload.has_next ? "" : " disabled"}>下一页${ic("i-next")}</button>`;
    }
  } catch (e) {
    if (request !== COLLECTION_COMMENT_REQUEST) return;
    list.innerHTML = `<div class="empty"><div class="empty-t">加载失败</div><div class="empty-sub">${esc(e.message)}</div></div>`;
  }
}
function scheduleCollectionCommentFilter() {
  const scope = $("collection-comments-scope");
  COLLECTION_COMMENT_CONTENT_ID = Number(scope?.value || 0);
  COLLECTION_COMMENT_PAGE = 1;
  clearTimeout(COLLECTION_COMMENT_TIMER);
  COLLECTION_COMMENT_TIMER = setTimeout(() => loadCollectionComments(1), 220);
}
function resetCollectionCommentFilters({ reload = true } = {}) {
  ["collection-comments-q", "collection-comments-region", "collection-comments-secuid"].forEach(id => { const el = $(id); if (el) el.value = ""; });
  const dedupe = $("collection-comments-dedupe");
  if (dedupe) dedupe.checked = false;
  const scope = $("collection-comments-scope");
  if (scope) scope.value = String(COLLECTION_COMMENT_CONTENT_ID || "");
  COLLECTION_COMMENT_PAGE = 1;
  if (reload) loadCollectionComments(1);
}
async function showCollectionComments(contentId) {
  const modal = $("collection-comments-modal");
  const scope = $("collection-comments-scope");
  COLLECTION_COMMENT_CONTENT_ID = Number(contentId) || 0;
  if (scope) {
    scope.innerHTML = `<option value="">全部作品</option><option value="${COLLECTION_COMMENT_CONTENT_ID}">当前作品 #${COLLECTION_COMMENT_CONTENT_ID}</option>`;
    scope.value = String(COLLECTION_COMMENT_CONTENT_ID || "");
  }
  resetCollectionCommentFilters({ reload: false });
  modal.style.display = "flex"; modalOpened(modal);
  setTimeout(() => modal.querySelector(".pv-close")?.focus(), 0);
  await loadCollectionComments(1);
}
async function exportFilteredCollectionComments(button) {
  if (!COLLECTION_JOB_ID) return;
  await withBusy(button, "导出中", async () => {
    try {
      const params = collectionCommentParams({ exportMode: true });
      await _downloadExcelReport(`/api/collections/${COLLECTION_JOB_ID}/export.xlsx?${params.toString()}`, `keyword-collection-${COLLECTION_JOB_ID}-filtered.xlsx`);
      toast("当前筛选结果已导出", "ok");
    } catch (e) { toast("导出失败：" + e.message, "err"); }
  });
}
function hideCollectionComments() {
  const modal = $("collection-comments-modal");
  ++COLLECTION_COMMENT_REQUEST;
  clearTimeout(COLLECTION_COMMENT_TIMER);
  modal.style.display = "none"; modalClosed(modal);
}
async function cancelCollection(jobId) {
  const btn = evtBtn();
  if (!await uiConfirm({ title: "取消采集任务", message: "任务会在当前请求完成后安全停止，已经保存的结果会保留。", okText: "取消任务", danger: true })) return;
  await withBusy(btn, "取消中", async () => {
    try { await api(`/api/collections/${jobId}/cancel`, { method: "POST" }); toast("已请求停止任务", "ok"); await refreshCollections(); }
    catch (e) { toast("取消失败：" + e.message, "err"); }
  });
}
async function retryCollection(jobId) {
  const btn = evtBtn();
  await withBusy(btn, "提交中", async () => {
    try { await api(`/api/collections/${jobId}/retry`, { method: "POST" }); toast("任务已重新进入队列，已有结果会自动去重", "ok"); await refreshCollections(); }
    catch (e) { toast("续跑失败：" + e.message, "err"); }
  });
}
async function deleteCollection(jobId) {
  const btn = evtBtn();
  if (!await uiConfirm({ title: "删除采集任务", message: "将删除任务及其作品、评论记录；本地已下载文件不会被删除。", okText: "删除", danger: true })) return;
  await withBusy(btn, "删除中", async () => {
    try { await api(`/api/collections/${jobId}`, { method: "DELETE" }); if (COLLECTION_RENDERED_JOB_ID === Number(jobId)) { COLLECTION_RENDERED_JOB_ID = 0; COLLECTION_RENDERED_SIG = ""; } if (COLLECTION_JOB_ID === jobId) closeCollectionResults(); toast("任务记录已删除，本地文件已保留", "ok"); await refreshCollections(); }
    catch (e) { toast("删除失败：" + e.message, "err"); }
  });
}
function exportCollection(jobId) {
  const link = document.createElement("a");
  link.href = `/api/collections/${jobId}/export.xlsx`;
  link.download = `keyword-collection-${jobId}.xlsx`;
  document.body.appendChild(link); link.click(); link.remove();
}
