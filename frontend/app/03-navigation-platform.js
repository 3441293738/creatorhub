// 03-navigation-platform.js — 十一平台上下文切换、平台差异化表单显隐与工作台标签页导航。
// ─── 平台切换(抖音 / 小红书) ───

async function exportMonitorReport(explicitBtn = null) {
  const btn = explicitBtn || evtBtn();
  const params = new URLSearchParams({ platform: PLATFORM });
  await withBusy(btn, "导出中", async () => {
    try {
      const response = await apiFetch("/api/reports/monitor.xlsx?" + params.toString());
      if (!response.ok) {
        let message = response.status;
        try {
          const body = await response.json();
          message = body.detail || message;
        } catch (e) { }
        throw new Error(message);
      }
      const blob = await response.blob();
      const disposition = response.headers.get("content-disposition") || "";
      const matched = disposition.match(/filename="?([^";]+)"?/i);
      const filename = matched ? matched[1] :
        `creatorhub_monitor_report_${new Date().toISOString().slice(0, 19).replace(/[-:T]/g, "")}.xlsx`;
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = filename;
      document.body.appendChild(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
      toast("Excel 监控报告已导出", "ok");
    } catch (e) {
      toast("报告导出失败: " + e.message, "err");
    }
  });
}


async function _downloadExcelReport(path, fallbackName) {
  const response = await apiFetch(path);
  if (!response.ok) {
    let message = response.status;
    try {
      const body = await response.json();
      message = body.detail || message;
    } catch (e) { }
    throw new Error(message);
  }
  const blob = await response.blob();
  const disposition = response.headers.get("content-disposition") || "";
  const matched = disposition.match(/filename="?([^";]+)"?/i);
  const filename = matched ? matched[1] : fallbackName;
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function _moduleReportParams(module, full) {
  const params = new URLSearchParams({ platform: PLATFORM });
  if (full) {
    params.set("full", "true");
    return params;
  }
  const put = (key, value) => {
    if (value !== undefined && value !== null && String(value).trim() !== "") {
      params.set(key, String(value).trim());
    }
  };
  if (module === "monitors") {
    put("q", $("mon-search") && $("mon-search").value);
    put("group_name", $("mon-group") && $("mon-group").value);
    put("tag", $("mon-tag") && $("mon-tag").value);
  } else if (module === "contents") {
    put("target_id", CONTENT_SRC);
    put("group_name", CONTENT_GROUP);
    put("tag", CONTENT_TAG);
    put("q", $("content-search") && $("content-search").value);
    put("media_type", $("content-type") && $("content-type").value);
    put("download_status", $("content-status") && $("content-status").value);
    put("min_like_count", $("content-min-likes") && $("content-min-likes").value);
    put("min_comment_count", $("content-min-comments") && $("content-min-comments").value);
    put("sort", $("content-sort") && $("content-sort").value);
    for (const [key, value] of Object.entries(contentCaptureBounds())) put(key, value);
  } else if (module === "comment-watches") {
    put("q", $("watch-search") && $("watch-search").value);
    put("group_name", $("watch-group") && $("watch-group").value);
    put("tag", $("watch-tag") && $("watch-tag").value);
  } else if (module === "comments") {
    put("watch_id", COMMENT_SRC);
    put("group_name", COMMENT_GROUP);
    put("tag", COMMENT_TAG);
    put("q", $("comment-query") && $("comment-query").value);
    put("ip_label", $("comment-ip-label") && $("comment-ip-label").value);
    put("reply_type", $("comment-type") && $("comment-type").value);
    put("min_like_count", $("comment-min-likes") && $("comment-min-likes").value);
    put("sort", $("comment-sort") && $("comment-sort").value);
    for (const [key, value] of Object.entries(contentCaptureBounds("comment"))) put(key, value);
  } else if (module === "danmaku-watches") {
    put("q", $("danmaku-watch-search") && $("danmaku-watch-search").value);
    put("group_name", $("danmaku-watch-group") && $("danmaku-watch-group").value);
    put("tag", $("danmaku-watch-tag") && $("danmaku-watch-tag").value);
  } else if (module === "danmaku") {
    put("watch_id", DANMAKU_SRC);
    put("q", $("danmaku-query") && $("danmaku-query").value);
    const start = +(($("danmaku-time-start") && $("danmaku-time-start").value) || 0);
    const end = +(($("danmaku-time-end") && $("danmaku-time-end").value) || 0);
    if (start > 0) put("min_video_time_ms", Math.round(start * 1000));
    if (end > 0) put("max_video_time_ms", Math.round(end * 1000));
    put("sort", $("danmaku-sort") && $("danmaku-sort").value);
    for (const [key, value] of Object.entries(contentCaptureBounds("danmaku"))) put(key, value);
  }
  return params;
}

const _reportLabels = {
  monitors: "监控列表",
  contents: "作品数据",
  "comment-watches": "评论监控",
  comments: "评论数据",
  "danmaku-watches": "弹幕监控",
  danmaku: "弹幕数据",
};
const _reportCountIds = {
  monitors: "mon-filter-count",
  contents: "content-filter-count",
  "comment-watches": "watch-filter-count",
  comments: "comment-filter-count",
  "danmaku-watches": "danmaku-watch-filter-count",
  danmaku: "danmaku-filter-count",
};
function _lockExportGroup(group, active) {
  if (!group) return () => {};
  const siblings = [...group.querySelectorAll("button")].filter(button => button !== active);
  const states = siblings.map(button => button.disabled);
  siblings.forEach(button => { button.disabled = true; });
  group.classList.add("is-busy");
  group.setAttribute("aria-busy", "true");
  return () => {
    siblings.forEach((button, index) => { button.disabled = states[index]; });
    group.classList.remove("is-busy");
    group.removeAttribute("aria-busy");
  };
}

async function exportModuleReport(module, full = false, explicitBtn = null) {
  const paths = {
    monitors: "/api/reports/monitors.xlsx",
    contents: "/api/reports/contents.xlsx",
    "comment-watches": "/api/reports/comment-watches.xlsx",
    comments: "/api/reports/comments.xlsx",
    "danmaku-watches": "/api/reports/danmaku-watches.xlsx",
    danmaku: "/api/reports/danmaku.xlsx",
  };
  const path = paths[module];
  if (!path) return;
  const btn = explicitBtn || evtBtn();
  const group = btn && btn.closest(".export-actions");
  const unlock = _lockExportGroup(group, btn);
  const label = _reportLabels[module] || "模块数据";
  const params = _moduleReportParams(module, full);
  await withBusy(btn, full ? "全量导出" : "筛选导出", async () => {
    try {
      await _downloadExcelReport(
        path + "?" + params.toString(),
        "creatorhub_" + module + "_report.xlsx",
      );
      const count = $( _reportCountIds[module] )?.textContent?.trim();
      const scope = full ? "全量" : "筛选结果";
      toast(`${label} ${scope} Excel 已导出${!full && count ? `（${count}）` : ""}`, "ok");
    } catch (e) {
      toast("报告导出失败: " + e.message, "err");
    } finally {
      unlock();
    }
  });
}

let PLATFORM = "douyin";
let GLOBAL_MODE = true;
const PF_NAME = { douyin: "抖音", xhs: "小红书", rednote: "海外小红书", kuaishou: "快手", shipinhao: "视频号", tiktok: "TikTok", bilibili: "哔哩哔哩", instagram: "Instagram", twitter: "Twitter", facebook: "Facebook", youtube: "YouTube" };
let CURRENT_TAB = "overview";
const PAGE_META = {
  overview: {
    title: "指挥概览", desc: "跨平台查看账号健康态势、高赞内容快览与今日发布排期。"
  },
  "media-hub": {
    title: "汇总预览库", desc: "跨平台统一画廊预览监控作品、批量采集、自有作品与本地下载素材，支持右侧分栏快览。"
  },
  accounts: {
    title: "账号矩阵", desc: "按跨平台人设组管理多账号，支持异常快筛、批量配代理与连续扫码补登。"
  },
  "risk-control": {
    title: "风控中心", desc: "统一管理风控规则，查看账号状态、触发原因、恢复进度与事件记录。"
  },
  monitors: {
    title: "作品监控", desc: "追踪关注的创作者，检查采集进度与新作品。"
  },
  collections: {
    title: "批量作品采集", desc: "按平台支持的来源批量采集作品、评论与可用媒体。"
  },
  comments: {
    title: "评论监控", desc: "订阅作品或账号评论，按来源、分组和标签筛选。"
  },
  danmaku: {
    title: "弹幕监控", desc: "监控短视频播放器内的弹幕，保留每条弹幕在视频中的时间点。"
  },
  hub: {
    title: "矩阵数据与私信", desc: "横向对比全部账号涨粉、播放、0播异常与全平台聚合私信，或下钻单账号明细。"
  },
  "tiktok-tools": {
    title: "用户互动", desc: "搜索其他 TikTok 用户并创建关注、取关、私信、评论或回复任务。"
  },
  "youtube-tools": {
    title: "互动与养号", desc: "执行 YouTube 视频/评论互动、按真实播放进度观看、关键词搜索养号、筛选发现与多品牌频道管理。"
  },
  publish: {
    title: "内容发布", desc: "支持单账号精细发布或跨平台多账号错峰矩阵群发。"
  },
  queue: {
    title: "任务队列", desc: "统一查看采集、发布、评论、账号动作与下载任务的排队、执行和阻塞状态。"
  },
  autocomment: {
    title: "自动评论", desc: "配置评论与回复规则，并审核待发布文案。"
  },
  "share-download": {
    title: "链接下载", desc: "从分享文案识别链接，检查媒体信息并下载到本地。"
  },
  notifications: {
    title: "通知渠道", desc: "配置 Bark、钉钉或 Telegram，及时接收任务提醒。"
  },
  settings: {
    title: "设置", desc: "管理外观、下载、AI 文案与采集运行配置。"
  },
};
const SINGLE_PF_ONLY_TABS = ["monitors", "collections", "comments", "danmaku", "tiktok-tools", "youtube-tools", "autocomment"];
function updatePageContext(name = CURRENT_TAB) {
  const meta = PAGE_META[name] || PAGE_META.overview;
  const showGlobalLabel = GLOBAL_MODE && !SINGLE_PF_ONLY_TABS.includes(name);
  const pfName = PF_NAME[PLATFORM] || "当前平台";
  let effectiveTitle = meta.title;
  let effectiveDesc = meta.desc;
  if (GLOBAL_MODE) {
    if (name === "publish") {
      effectiveTitle = "矩阵群发与排期";
      effectiveDesc = "一次上传素材与主文案，按跨平台矩阵组一键错峰群发，并统一追踪全平台发布进度。";
    }
  } else {
    if (name === "overview") {
      effectiveDesc = `查看 ${pfName} 账号健康态势、高赞内容快览与今日发布排期。`;
    } else if (name === "media-hub") {
      effectiveDesc = `统一画廊预览 ${pfName} 监控作品、批量采集、自有作品与本地下载素材，支持右侧分栏快览。`;
    } else if (name === "accounts") {
      effectiveDesc = `管理 ${pfName} 账号登录态、分组归类、独立代理与浏览器指纹环境。`;
    } else if (name === "hub") {
      effectiveTitle = "数据与私信";
      effectiveDesc = `对比 ${pfName} 各账号涨粉、播放、0播异常与聚合私信，或下钻单账号明细。`;
    } else if (name === "publish") {
      effectiveDesc = `支持 ${pfName} 单账号精细发布或同平台多账号错峰群发。`;
    }
  }
  if ($("page-title")) $("page-title").textContent = effectiveTitle;
  if ($("page-desc")) $("page-desc").textContent = effectiveDesc;
  if ($("page-platform")) $("page-platform").textContent = showGlobalLabel ? "全平台矩阵" : pfName;
  if ($("page-kicker")) $("page-kicker").textContent = showGlobalLabel ? "跨 11 大平台全局视角" : (pfIsChannels(PLATFORM) ? "本账号工作台" : "单平台精细工作台");
  document.title = `${effectiveTitle} · ${showGlobalLabel ? "全平台" : (PF_NAME[PLATFORM] || "")} | CreatorHub`;
  globalThis.CreatorHubWorkbench?.navigate?.();
}
// 是否支持「发布」面板
function pfHasPublish(pf) { return ["xhs", "rednote", "kuaishou", "douyin", "shipinhao", "tiktok", "bilibili", "instagram", "twitter", "facebook", "youtube"].includes(pf); }
// 视频号只有「本账号」数据(助手接口本账号),不支持监控他人作品/评论
function pfIsChannels(pf) { return pf === "shipinhao"; }
function switchPlatform(pf) {
  const wantGlobal = pf === "all";
  if (!wantGlobal && !["douyin", "xhs", "rednote", "kuaishou", "shipinhao", "tiktok", "bilibili", "instagram", "twitter", "facebook", "youtube"].includes(pf)) pf = "douyin";
  if (!wantGlobal) globalThis.CreatorHubWorkbench?.platformChanging?.(pf);
  GLOBAL_MODE = wantGlobal;
  if (!wantGlobal) PLATFORM = pf;
  VIEW_REQUESTS.clear();
  CONTENT_PAGE = COMMENT_PAGE = 1;
  COLLECTION_JOBS = []; selCollection.clear(); closeCollectionResults(); renderCollectionJobs(); if ($("tb-col")) $("tb-col").textContent = "0";
  selContent.clear(); selComment.clear();
  monSelClear(); watchSelClear(); danmakuWatchSelClear();
  CONTENT_SRC = CONTENT_GROUP = CONTENT_TAG = "";
  CONTENT_SOURCE_CACHE.clear();
  ["content-captured-from", "content-captured-to"].forEach(id => { if ($(id)) $(id).value = ""; });
  for (const kind of ["comment", "danmaku"]) {
    WATCH_RECORD_STATE[kind].cache.clear();
    [kind + "-captured-from", kind + "-captured-to"].forEach(id => { if ($(id)) $(id).value = ""; });
  }
  COMMENT_SRC = COMMENT_GROUP = COMMENT_TAG = "";
  DANMAKU_SRC = "";
  DANMAKU_PAGE = 1;
  if (OPEN_META_COMBO) OPEN_META_COMBO.close();
  ["t-group", "t-tags", "w-group", "w-tags", "d-w-group", "d-w-tags"].forEach(id => setMetaValue(id, ""));
  ["mon-search", "watch-search", "danmaku-query", "danmaku-time-start", "danmaku-time-end"].forEach(id => { if ($(id)) $(id).value = ""; });
  ["mon-group", "mon-tag", "content-group", "content-tag", "content-src",
    "watch-group", "watch-tag", "comment-group", "comment-tag", "comment-src",
    "danmaku-watch-group", "danmaku-watch-tag", "danmaku-src"].forEach(id => {
    const select = $(id);
    if (select) { select.value = ""; if (select._csSync) select._csSync(); }
  });
  try {
    localStorage.setItem("dym-global", wantGlobal ? "1" : "0");
    if (!wantGlobal) localStorage.setItem("dym-pf", pf);
  } catch (e) {}
  if ($("queue-platform")) {
    $("queue-platform").value = wantGlobal ? "all" : PLATFORM;
    $("queue-platform")._csSync?.();
  }
  if (typeof selShareHistory !== "undefined") selShareHistory.clear();
  applyPlatformUI();
  ["t-interval", "w-interval", "d-w-interval"].forEach(id => $(id)?._monitorIntervalSync?.());
  // 切换后立刻刷新对应数据
  refreshAccounts(); refreshMonitors(); refreshContents(); refreshWatches(); refreshComments(); refreshDanmakuWatches(); refreshDanmaku(); refreshCollections();
  updateTaskQueuePlatformLabel();
  if (CURRENT_TAB === "queue") refreshTaskQueue(true); else refreshTaskQueueBadge();
  if (CURRENT_TAB === "share-download" && typeof renderShareHistoryRows === "function") renderShareHistoryRows(true);
  if (CURRENT_TAB === "risk-control") refreshRiskCenter(true, false);
  resetAcReviewDefaultForPlatform(PLATFORM);
  populateAcAccount(); onAcMode(); refreshCommentRules(); refreshCommentTasks();
  if (pfHasPublish(PLATFORM)) refreshPublish();
  if (CURRENT_TAB === "overview") loop();
}
function applyPlatformUI() {
  document.body.classList.toggle("pf-all", GLOBAL_MODE);
  document.body.classList.toggle("pf-douyin", !GLOBAL_MODE && PLATFORM === "douyin");
  document.body.classList.toggle("pf-xhs", !GLOBAL_MODE && PLATFORM === "xhs");
  document.body.classList.toggle("pf-kuaishou", !GLOBAL_MODE && PLATFORM === "kuaishou");
  document.body.classList.toggle("pf-shipinhao", !GLOBAL_MODE && PLATFORM === "shipinhao");
  document.body.classList.toggle("pf-tiktok", !GLOBAL_MODE && PLATFORM === "tiktok");
  document.body.classList.toggle("pf-bilibili", !GLOBAL_MODE && PLATFORM === "bilibili");
  document.body.classList.toggle("pf-rednote", !GLOBAL_MODE && PLATFORM === "rednote");
  document.body.classList.toggle("pf-instagram", !GLOBAL_MODE && PLATFORM === "instagram");
  document.body.classList.toggle("pf-twitter", !GLOBAL_MODE && PLATFORM === "twitter");
  document.body.classList.toggle("pf-facebook", !GLOBAL_MODE && PLATFORM === "facebook");
  document.body.classList.toggle("pf-youtube", !GLOBAL_MODE && PLATFORM === "youtube");
  // 视频号:只有本账号数据,隐藏「监控他人作品/评论」相关入口(.notsh-only)
  document.body.classList.toggle("pf-channels", !GLOBAL_MODE && pfIsChannels(PLATFORM));
  if (!["douyin", "bilibili"].includes(PLATFORM) && CURRENT_TAB === "danmaku") switchTab("overview");
  document.querySelectorAll(".pswitch button").forEach(b => {
    const active = GLOBAL_MODE ? b.dataset.pf === "all" : b.dataset.pf === PLATFORM;
    b.classList.toggle("active", active);
    b.setAttribute("aria-selected", active ? "true" : "false");
    b.tabIndex = active ? 0 : -1;
    if (active) {
      try {
        b.scrollIntoView({ behavior: "smooth", block: "nearest", inline: "nearest" });
      } catch (_) {}
    }
  });
  if (typeof window._syncPlatformStripOverflow === "function") {
    requestAnimationFrame(window._syncPlatformStripOverflow);
  }
  document.querySelectorAll(".dy-only").forEach(e => e.classList.toggle("hidden", PLATFORM !== "douyin"));
  document.querySelectorAll(".xhs-only").forEach(e => e.classList.toggle("hidden", PLATFORM !== "xhs"));
  document.querySelectorAll(".ks-only").forEach(e => e.classList.toggle("hidden", PLATFORM !== "kuaishou"));
  document.querySelectorAll(".sh-only").forEach(e => e.classList.toggle("hidden", PLATFORM !== "shipinhao"));
  document.querySelectorAll(".tk-only").forEach(e => e.classList.toggle("hidden", PLATFORM !== "tiktok"));
  document.querySelectorAll(".tk-hide").forEach(e => e.classList.toggle("hidden", PLATFORM === "tiktok"));
  document.querySelectorAll(".bili-only").forEach(e => e.classList.toggle("hidden", PLATFORM !== "bilibili"));
  document.querySelectorAll(".bili-hide").forEach(e => e.classList.toggle("hidden", PLATFORM === "bilibili"));
  document.querySelectorAll(".rn-only").forEach(e => e.classList.toggle("hidden", PLATFORM !== "rednote"));
  document.querySelectorAll(".rn-hide").forEach(e => e.classList.toggle("hidden", PLATFORM === "rednote"));
  document.querySelectorAll(".xhs-rn-only").forEach(e => e.classList.toggle("hidden", !["xhs", "rednote"].includes(PLATFORM)));
  document.querySelectorAll(".ig-only").forEach(e => e.classList.toggle("hidden", PLATFORM !== "instagram"));
  document.querySelectorAll(".ig-hide").forEach(e => e.classList.toggle("hidden", PLATFORM === "instagram"));
  document.querySelectorAll(".tw-only").forEach(e => e.classList.toggle("hidden", PLATFORM !== "twitter"));
  document.querySelectorAll(".tw-hide").forEach(e => e.classList.toggle("hidden", PLATFORM === "twitter"));
  document.querySelectorAll(".fb-only").forEach(e => e.classList.toggle("hidden", PLATFORM !== "facebook"));
  document.querySelectorAll(".fb-hide").forEach(e => e.classList.toggle("hidden", PLATFORM === "facebook"));
  document.querySelectorAll(".yt-only").forEach(e => e.classList.toggle("hidden", PLATFORM !== "youtube"));
  document.querySelectorAll(".yt-hide").forEach(e => e.classList.toggle("hidden", PLATFORM === "youtube"));
  document.querySelectorAll(".danmaku-only").forEach(e => e.classList.toggle("hidden", !["douyin", "bilibili"].includes(PLATFORM)));
  document.querySelectorAll(".notsh-only").forEach(e => {
    const allowOnChannels = pfIsChannels(PLATFORM) && (
      e.dataset?.tab === "monitors"
      || e.dataset?.tab === "comments"
      || (e.classList.contains("quick-action") && (e.getAttribute("onclick") || "").includes("'monitors'"))
    );
    const hideByPlatform = (e.classList.contains("tk-hide") && PLATFORM === "tiktok") || (e.classList.contains("bili-hide") && PLATFORM === "bilibili") || (e.classList.contains("rn-hide") && PLATFORM === "rednote") || (e.classList.contains("yt-hide") && PLATFORM === "youtube");
    e.classList.toggle("hidden", hideByPlatform || (pfIsChannels(PLATFORM) && !allowOnChannels));
  });
  document.querySelectorAll(".collect-only").forEach(e => e.classList.toggle(
    "hidden", !["douyin", "xhs", "rednote", "kuaishou", "shipinhao", "tiktok", "bilibili", "instagram", "twitter", "facebook", "youtube"].includes(PLATFORM)));
  // 全平台模式下隐藏所有单平台专属采集/监控/互动入口与分组标题
  if (GLOBAL_MODE) {
    document.querySelectorAll(".single-pf-only").forEach(e => e.classList.add("hidden"));
  } else {
    document.querySelectorAll("#nav-sep-collect").forEach(e => e.classList.remove("hidden"));
  }
  if ($("nav-publish-label")) $("nav-publish-label").textContent = GLOBAL_MODE ? "矩阵群发" : "内容发布";
  if ($("nav-hub-label")) $("nav-hub-label").textContent = GLOBAL_MODE ? "矩阵数据与私信" : "数据与私信";
  document.querySelectorAll(".meta-scope").forEach(e => {
    e.textContent = (PF_NAME[PLATFORM] || "当前平台") + "内独立";
  });
  // 发布面板入口
  document.querySelectorAll(".pub-only").forEach(e => e.classList.toggle("hidden", !GLOBAL_MODE && !pfHasPublish(PLATFORM)));
  // 发布面板文案随平台切换
  const ks = PLATFORM === "kuaishou", dy = PLATFORM === "douyin", sph = PLATFORM === "shipinhao", tk = PLATFORM === "tiktok", bili = PLATFORM === "bilibili", rn = PLATFORM === "rednote", ig = PLATFORM === "instagram", tw = PLATFORM === "twitter", fb = PLATFORM === "facebook", yt = PLATFORM === "youtube";
  const pubType = $("pub-type");
  if (pubType) {
    const before = pubType.value;
    if (tk || yt) pubType.innerHTML = '<option value="video">视频</option>';
    else if (![...pubType.options].some(o => o.value === "images")) {
      pubType.innerHTML = '<option value="images">图文（图集）</option><option value="video">视频</option>';
      pubType.value = before === "video" ? "video" : "images";
    }
    if (tk || yt) pubType.value = "video";
    if (pubType.value !== before) onPubType();
  }
  const pubSub = $("pub-head-sub");
  if (pubSub) pubSub.textContent = dy ? "上传图集 / 视频到抖音创作平台(实验性)"
    : ks ? "上传图集 / 视频到快手创作平台(实验性)"
    : sph ? "上传图集 / 视频到视频号助手(实验性)"
    : tk ? "上传视频到 TikTok Studio(实验性)"
    : bili ? "上传图文动态 / 视频稿件到哔哩哔哩创作中心"
    : ig ? "上传单图 / 轮播 / Reels / Story 到 Instagram"
    : tw ? "发布纯文本 / 图集（最多 4 图） / 视频推文到 X (Twitter)"
    : fb ? "发布纯文本 / 图集（最多 20 图） / 视频到 Facebook 个人主页、公共主页、群组或 Story"
    : yt ? "通过 YouTube Studio 上传常规视频或 Shorts（支持品牌频道切换、标签/播单/缩略图与首评）"
    : rn ? "上传图集 / 视频到海外小红书 creator.rednote.com"
    : "上传图集 / 视频到小红书(实验性)";
  if ($("pub-head-lead")) $("pub-head-lead").textContent = tw ? "发布推文" : fb ? "发布帖子" : yt ? "上传视频" : (ks || dy || sph || tk || bili || ig) ? "发布作品" : "发布笔记";
  const titleCap = fb ? 5000 : tw ? ($("pub-tw-long")?.value === "1" ? 4000 : 280) : ig ? 2200 : yt ? 100 : bili ? 80 : rn ? 40 : dy ? 30 : sph ? 16 : 20;
  if ($("pub-title-label-text")) $("pub-title-label-text").textContent = fb ? `首行标题（可选，正文与话题合并 ≤${titleCap} 字）` : tw ? `推文首行标题（可选，合并总重 ≤${titleCap}）` : yt ? `视频标题（必填，≤${titleCap} 字符）` : rn ? "标题(≤20 汉字 / 40 英文字符)" : `标题(≤${titleCap} 字)`;
  if ($("pub-title")) {
    $("pub-title").maxLength = titleCap;
    $("pub-title").placeholder = fb ? "帖子首行标题（可选，正文与话题会自动合并，可纯文本发布）" : tw ? "推文首行（可选，直接填正文也可；URL 固定计 23 权重）" : yt ? "填写 YouTube 视频标题（最多 100 字符，Shorts 形态会自动追加 #Shorts）" : ig ? "Caption 首行标题（可选，正文与话题自动合并）" : bili ? "填写视频稿件标题（图文动态可选）" : tk ? "视频说明的首行标题" : (ks || dy || sph) ? "给作品起个标题" : "给笔记起个标题";
  }
  if ($("pub-dy-opts")) $("pub-dy-opts").classList.toggle("hidden", !["douyin", "xhs", "rednote", "kuaishou"].includes(PLATFORM));
  if (bili) { onBiliCopyrightChange(); if ($("pub-acc")?.value) loadBiliPartitions($("pub-acc").value); }
  const pubHintText = dy
    ? "发布通过自动化抖音创作平台完成。首次登录或触发验证时，请在弹出窗口中完成短信验证或扫码；视频上传后还需等待转码。注意：定时发布可能因本人验证而暂停，建议发布时在场。"
    : ks
    ? "发布通过自动化快手创作平台完成。若遇验证码或需要补充封面，请在弹出窗口中手动处理；定时任务由后台引擎按计划执行。"
    : sph
    ? "发布通过自动化视频号助手完成。视频需等待转码，发布前可能要求补充封面、实名或人脸验证，请在弹出窗口中处理。注意：平台页面改版后可能需要重新适配。"
    : tk
    ? "发布通过账号独立的可见 TikTok Studio 页面完成，目前支持单视频、话题、可见范围、评论、Duet 与 Stitch。提交只点击一次；若显示“结果待确认”，请先到 TikTok Studio 核对，系统不会自动重发。"
    : bili
    ? "视频稿件通过哔哩哔哩创作中心完成上传，并按所选分区、自制/转载、创作者声明及弹幕/评论权限提交；图文通过动态页发布。提交只执行一次；若显示“结果待确认”，请先到创作中心核对，系统不会自动重发。"
    : ig
    ? "支持单图、混合多图轮播（最多 10 项）、Reels 短视频与 Story 快拍。默认优先走两阶段 rupload + configure 直连协议，并在普通兼容失败时回退独立浏览器 Create 弹窗；若显示“结果待确认”，请先到 Instagram 主页核对。"
    : tw
    ? "支持纯文本推文、最多 4 张图片或单个视频/GIF，可配置回复权限与首评自回复串推。通过独立可见浏览器窗口完成上传并拦截 CreateTweet 响应确认，提交只执行一次；若显示“结果待确认”，请先到 x.com 主页核对。"
    : fb
    ? "支持纯文本、最多 20 张图片或单个视频，可发布到个人动态、公共主页（自动通过 i_user 切换身份并在完成后恢复）、Facebook 群组或 Story，支持可见范围与发布后自动首评。提交只执行一次；若显示“结果待确认”，请先到 facebook.com 主页核对。"
    : yt
    ? "通过账号独立的可见浏览器窗口在 YouTube Studio 上传单个视频或 Shorts，支持切换品牌频道、设置标题/简介/Tags/播放列表/缩略图/儿童向声明/公开·不公开·私享·即时首映及发布后自动追加首评。提交只执行一次；若显示“结果待确认”，请先到 studio.youtube.com 核对。"
    : rn
    ? "发布通过海外小红书 creator.rednote.com 独立可见浏览器窗口完成（拦截 webapi.rednote.com/web_api/sns/v2/note 确认），不混用国内小红书会话。提交只点击一次；若显示“结果待确认”，请先到平台核对。"
    : "发布通过账号独立的可见 Chrome 页面完成。提交只点击一次；若显示“结果待确认”，请先到小红书核对，系统不会自动重发。";
  const pubHint = $("pub-hint");
  if (pubHint) {
    const copy = pubHint.querySelector("span");
    if (copy) copy.textContent = pubHintText;
    else pubHint.textContent = pubHintText;
  }
  // 评论监控「类型」下拉随平台改写文案
  const wk = $("w-kind");
  if (wk) {
    const cur = wk.value;
    wk.innerHTML = (PLATFORM === "xhs" || PLATFORM === "rednote")
      ? '<option value="auto">类型:自动识别</option><option value="video">单条笔记</option><option value="user">创作者近期笔记</option>'
      : '<option value="auto">类型:自动识别</option><option value="video">单条视频</option><option value="user">账号近期作品</option>';
    if ([...wk.options].some(o => o.value === cur)) wk.value = cur;
  }
  const wl = $("w-url-label");
  if (wl) wl.textContent = (PLATFORM === "xhs" || PLATFORM === "rednote")
    ? "笔记 / 创作者主页 / 完整分享文案 / ID"
    : PLATFORM === "kuaishou" ? "作品 / 创作者主页 / 完整分享文案 / ID"
    : PLATFORM === "shipinhao" ? "分享链接 / 作品 objectId / 创作者 v2_...@finder"
    : PLATFORM === "bilibili" ? "视频 BV/av / 图文动态 / 空间主页 / 完整分享文案"
    : PLATFORM === "instagram" ? "帖子 /p/ / Reels /reel/ / 创作者 @username / 主页链接"
    : PLATFORM === "twitter" ? "推文 /status/ / 创作者 @username / x.com 主页链接"
    : PLATFORM === "facebook" ? "帖子 / Reel / Watch 链接或个人/公共主页/群组链接"
    : PLATFORM === "youtube" ? "视频 watch / Shorts / youtu.be 或频道 /@handle 链接"
    : "视频 / 账号主页 / 完整分享文案 / ID";
  if ($("w-url")) $("w-url").placeholder = PLATFORM === "xhs"
    ? "直接粘贴整段小红书分享文案，将自动提取笔记或主页链接"
    : PLATFORM === "kuaishou" ? "直接粘贴整段快手分享文案，将自动提取作品或主页链接"
    : PLATFORM === "shipinhao" ? "粘贴 weixin.qq.com/sph/... 分享链、视频号作品链接、objectId 或 v2_...@finder"
    : PLATFORM === "tiktok" ? "直接粘贴整段 TikTok 分享文案，将自动提取视频或主页链接"
    : PLATFORM === "rednote" ? "粘贴 www.rednote.com/zh/discovery/item/... 笔记链接、user/profile/... 主页链接或 24 位 ID"
    : PLATFORM === "bilibili" ? "直接粘贴整段哔哩哔哩分享文案、BV/av 号、opus 动态链接或 space 主页"
    : PLATFORM === "instagram" ? "粘贴 instagram.com/p/...、/reel/...、创作者主页链接或 @username"
    : PLATFORM === "twitter" ? "粘贴 x.com/.../status/...、创作者主页链接或 @username"
    : PLATFORM === "facebook" ? "粘贴 facebook.com 帖子/Reel/Watch 链接、fb.watch 短链、公共主页或群组链接"
    : PLATFORM === "youtube" ? "粘贴 youtube.com/watch?v=...、youtu.be/...、/shorts/... 或频道 /@handle 链接"
    : "直接粘贴整段抖音分享文案，将自动提取视频或主页链接";
  resetWatchTargetState();
  const ckl = $("ck-label");
  if (ckl) ckl.textContent = PLATFORM === "xhs"
    ? "完整 Cookie(含 a1;发布需创作者会话)"
    : PLATFORM === "kuaishou" ? "完整 Cookie(含 userId 与 web_st)"
    : PLATFORM === "rednote" ? "完整 rednote.com Cookie(含 web_session / a1;禁混用 xiaohongshu.com)"
    : PLATFORM === "bilibili" ? "完整 Cookie(含 SESSDATA 与 bili_jct)"
    : PLATFORM === "instagram" ? "完整 Cookie(含 sessionid、ds_user_id 与 csrftoken)"
    : PLATFORM === "twitter" ? "Token (auth_token) / 多字段凭证 / 完整 Cookie"
    : PLATFORM === "facebook" ? "完整 Facebook Cookie(必须含 c_user 与 xs)"
    : PLATFORM === "youtube" ? "完整 YouTube Cookie(含 SAPISID / SID 等登录项)"
    : "完整 Cookie(含 sessionid)";
  if ($("ck-val")) $("ck-val").placeholder = PLATFORM === "xhs"
    ? "从 creator.xiaohongshu.com 登录后复制完整 Cookie"
    : PLATFORM === "kuaishou" ? "从 www.kuaishou.com 登录后复制完整 Cookie"
    : PLATFORM === "rednote" ? "从 www.rednote.com 或 creator.rednote.com 登录后复制完整 Cookie"
    : PLATFORM === "bilibili" ? "从 www.bilibili.com 或 member.bilibili.com 登录后复制完整 Cookie"
    : PLATFORM === "instagram" ? "从 www.instagram.com 登录后复制完整 Cookie(含 sessionid)"
    : PLATFORM === "twitter" ? "可直接粘贴 40 位 auth_token、账号----密码----Token 文本、JSON 数组或 x.com 完整 Cookie"
    : PLATFORM === "facebook" ? "从 www.facebook.com 登录后复制完整 Cookie（含 c_user 与 xs）或粘贴 JSON Cookie 数组"
    : PLATFORM === "youtube" ? "从 www.youtube.com 复制完整 Cookie（含 SAPISID 与 SID）、Netscape Cookie 文本或 JSON 数组"
    : "从浏览器开发者工具复制完整 Cookie";
  applyMonitorForm();
  applyCollectionForm();
  if (["douyin", "bilibili"].includes(PLATFORM)) applyDanmakuForm();
  if ($("t-kind") && !["xhs", "rednote"].includes(PLATFORM)) $("t-kind").value = "creator";
  // 全平台视角下不展示单平台专属面板：若当前停留在单平台页，自动回到指挥概览
  if (GLOBAL_MODE && SINGLE_PF_ONLY_TABS.includes(CURRENT_TAB)) {
    switchTab("overview");
    return;
  }
  // 视频号不支持自动评论规则:若正停在自动评论面板,切回本账号管理
  if (!GLOBAL_MODE && pfIsChannels(PLATFORM)) {
    const cur = (document.querySelector('.navitem.active') || {}).dataset;
    if (cur && cur.tab === "autocomment") switchTab("hub");
    // 视频号本账号只有「我的作品 / 数据」;若停在关注/粉丝/私信子页,切回我的作品
    if (["following", "fans", "dm"].includes(HUB_TAB)) switchHubTab("myworks");
  }
  if (!GLOBAL_MODE && PLATFORM === "tiktok" && CURRENT_TAB === "autocomment") switchTab("hub");
  if (!GLOBAL_MODE && PLATFORM !== "tiktok" && CURRENT_TAB === "tiktok-tools") switchTab("accounts");
  if (!GLOBAL_MODE && PLATFORM !== "youtube" && CURRENT_TAB === "youtube-tools") switchTab("accounts");
  if (!GLOBAL_MODE && PLATFORM === "tiktok" && ["following", "fans", "dm"].includes(HUB_TAB)) switchHubTab("myworks");
  if (!GLOBAL_MODE && PLATFORM === "rednote" && CURRENT_TAB === "autocomment") switchTab("hub");
  if (!GLOBAL_MODE && PLATFORM === "rednote" && ["following", "fans", "dm"].includes(HUB_TAB)) switchHubTab("myworks");
  if (!GLOBAL_MODE && PLATFORM === "youtube" && ["fans", "dm"].includes(HUB_TAB)) switchHubTab("myworks");
  if (!GLOBAL_MODE && !["douyin", "xhs", "rednote", "kuaishou", "shipinhao", "tiktok", "bilibili", "instagram", "twitter", "facebook", "youtube"].includes(PLATFORM) && CURRENT_TAB === "collections") switchTab("overview");
  // 不支持发布的平台:若正停在该面板则回到总览(当前十一平台均支持,兜底保留)
  if (!GLOBAL_MODE && !pfHasPublish(PLATFORM)) {
    const pub = document.querySelector('[data-panel="publish"]');
    if (pub && pub.style.display !== "none") switchTab("overview");
  }
  csSyncAll();   // 平台切换可能改了下拉选项/值,同步自定义下拉显示
  if (typeof syncShareHistoryPlatformControl === "function") syncShareHistoryPlatformControl();
  if (typeof updateTaskQueuePlatformLabel === "function") updateTaskQueuePlatformLabel();
  requestAnimationFrame(() => window.dispatchEvent(new Event("creatorhub:layout")));
  updatePageContext();
}
const SMART_TARGET_PARSE_SEQ = Object.create(null);

function smartTargetPlatformName(platform = PLATFORM) {
  return PF_NAME[platform] || (platform === "wechat" ? "微信/视频号" : platform === "generic" ? "其他站点" : String(platform || "当前平台"));
}

function setSmartTargetFeedback(feedbackId, message, state = "") {
  const feedback = $(feedbackId);
  if (!feedback) return;
  feedback.textContent = message;
  feedback.className = `smart-target-feedback${state ? ` ${state}` : ""}`;
}

function resetSmartTargetState(inputId, feedbackId, message) {
  const input = $(inputId);
  if (input) input.removeAttribute("aria-invalid");
  SMART_TARGET_PARSE_SEQ[inputId] = (SMART_TARGET_PARSE_SEQ[inputId] || 0) + 1;
  setSmartTargetFeedback(feedbackId, message);
}

function isChannelsWorkUrl(value) {
  const text = String(value || "").trim();
  return /(?:^https?:\/\/)?(?:www\.)?weixin\.qq\.com\/sph\//i.test(text)
    || /(?:^https?:\/\/)?channels\.weixin\.qq\.com\/(?:finder-preview\/pages\/(?:sph|feed)|web\/pages\/feed)/i.test(text)
    || /^\d{16,20}$/.test(text);
}

function isChannelsCreatorId(value) {
  const text = String(value || "").trim();
  return /^v2_[0-9a-f]{16,}(?:@finder)?$/i.test(text) || /^sph[0-9a-z_]{5,}$/i.test(text);
}

async function copyTextValue(value, okMsg = "已复制", errMsg = "复制失败，请手动复制") {
  const text = String(value || "").trim();
  if (!text) return false;
  try {
    if (navigator.clipboard && typeof navigator.clipboard.writeText === "function") {
      await navigator.clipboard.writeText(text);
      toast(okMsg, "ok", 1800);
      return true;
    }
  } catch (e) {}
  try {
    const field = document.createElement("textarea");
    field.value = text;
    field.style.position = "fixed";
    field.style.opacity = "0";
    document.body.appendChild(field);
    field.select();
    const copied = document.execCommand("copy");
    field.remove();
    toast(copied ? okMsg : errMsg, copied ? "ok" : "err", copied ? 1800 : 2800);
    return copied;
  } catch (e) {
    toast(errMsg, "err");
    return false;
  }
}

async function normalizePlatformShareTarget({
  inputId, feedbackId, platform, button = null, quiet = false, resolveChannelsCreator = false,
}) {
  const input = $(inputId);
  if (!input) return null;
  const raw = input.value.trim();
  if (!raw) return raw;

  const seq = (SMART_TARGET_PARSE_SEQ[inputId] || 0) + 1;
  SMART_TARGET_PARSE_SEQ[inputId] = seq;
  const old = button && button.innerHTML;
  if (button) {
    button.disabled = true;
    button.innerHTML = `<span class="spin"></span><span>识别中</span>`;
  }
  input.removeAttribute("aria-invalid");
  setSmartTargetFeedback(feedbackId, "正在清洗分享文案并识别链接…", "parsing");
  try {
    const result = await api("/api/share-download/links", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ share_text: raw, limit: 20 }),
    });
    if (seq !== SMART_TARGET_PARSE_SEQ[inputId]) return input.value.trim();
    const links = Array.isArray(result.links) ? result.links : [];
    let candidate = raw;
    let changed = false;
    if (!links.length) {
      if (platform === "shipinhao" && isChannelsCreatorId(raw)) {
        setSmartTargetFeedback(feedbackId, "视频号创作者标识已识别 ✓", "success");
        return raw;
      }
      if (!(platform === "shipinhao" && resolveChannelsCreator && isChannelsWorkUrl(raw))) {
        setSmartTargetFeedback(feedbackId, "未发现链接，将按作品或用户 ID 直接解析");
        if (!quiet) toast("没有发现链接；也可以直接填写作品或用户 ID", "err");
        return raw;
      }
    } else {
      const expectedPlatform = platform === "shipinhao" ? "wechat" : platform;
      const matched = links.find(item => item && item.platform === expectedPlatform);
      if (!matched) {
        const names = [...new Set(links.map(item => smartTargetPlatformName(item.platform)))].join("、");
        const message = `识别到${names}链接，与当前${smartTargetPlatformName(platform)}页面不匹配`;
        input.setAttribute("aria-invalid", "true");
        setSmartTargetFeedback(feedbackId, message, "error");
        if (!quiet) toast(message, "err");
        return null;
      }
      candidate = String(matched.url || "").trim() || raw;
      changed = candidate !== raw;
      if (changed) input.value = candidate;
    }

    if (platform === "shipinhao" && resolveChannelsCreator && isChannelsWorkUrl(candidate)) {
      if (quiet) {
        setSmartTargetFeedback(
          feedbackId,
          "已识别视频号作品链接，点击「识别」或提交后将自动反查作者 v2_...@finder ✓",
          "success",
        );
        return candidate;
      }
      setSmartTargetFeedback(feedbackId, "正在从视频号作品反查创作者 finderUsername…", "parsing");
      try {
        const resolved = await api("/api/channels/resolve-creator", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ target: candidate }),
        });
        if (seq !== SMART_TARGET_PARSE_SEQ[inputId]) return input.value.trim();
        const username = String(resolved?.username || "").trim();
        const nickname = String(resolved?.nickname || "").trim();
        if (username) {
          input.value = username;
          const label = nickname ? `${nickname} (${username})` : username;
          setSmartTargetFeedback(feedbackId, `已反查到创作者：${label} ✓`, "success");
          toast(`已反查到创作者：${label}`, "ok", 2400);
          return username;
        }
      } catch (resolveErr) {
        if (seq !== SMART_TARGET_PARSE_SEQ[inputId]) return input.value.trim();
        input.setAttribute("aria-invalid", "true");
        setSmartTargetFeedback(feedbackId, resolveErr.message, "error");
        toast(resolveErr.message, "err");
        return null;
      }
    }

    setSmartTargetFeedback(
      feedbackId,
      changed
        ? `已去除无关文案，提取出${smartTargetPlatformName(platform)}链接 ✓`
        : `${smartTargetPlatformName(platform)}链接已识别 ✓`,
      "success",
    );
    return candidate || raw;
  } catch (e) {
    if (seq !== SMART_TARGET_PARSE_SEQ[inputId]) return input.value.trim();
    setSmartTargetFeedback(feedbackId, "自动识别暂未完成，提交时仍会在服务端解析", "error");
    if (!quiet) toast("识别失败：" + e.message, "err");
    return raw;
  } finally {
    if (button && button.isConnected) {
      button.disabled = false;
      button.innerHTML = old;
    }
  }
}

function resetMonitorTargetState() {
  const keywordMode = ["xhs", "rednote"].includes(PLATFORM) && $("t-kind") && $("t-kind").value === "keyword";
  resetSmartTargetState("t-url", "t-url-feedback", keywordMode
    ? "输入要持续监控的搜索关键词"
    : PLATFORM === "shipinhao"
    ? "支持视频号主页链接、v2_...@finder、sph 标识，或单条视频 weixin.qq.com/sph/... 链接（自动反查作者）"
    : "支持完整分享文案、主页长链、平台短链或用户 ID");
}

async function normalizeMonitorTarget(button = null, { quiet = false } = {}) {
  const keywordMode = ["xhs", "rednote"].includes(PLATFORM) && $("t-kind") && $("t-kind").value === "keyword";
  if (keywordMode) return $("t-url").value.trim();
  return normalizePlatformShareTarget({
    inputId: "t-url", feedbackId: "t-url-feedback", platform: PLATFORM, button, quiet,
    resolveChannelsCreator: PLATFORM === "shipinhao",
  });
}

function handleMonitorTargetPaste() {
  setTimeout(() => normalizeMonitorTarget(null, { quiet: true }), 0);
}

function handleMonitorTargetKeydown(event) {
  if (!event || event.key !== "Enter" || event.isComposing) return;
  event.preventDefault();
  addMonitor();
}

function resetCollectionCreatorState() {
  resetSmartTargetState(
    "col-source-ref", "col-source-ref-feedback",
    PLATFORM === "shipinhao"
      ? "可粘贴视频号 v2_...@finder、主页链接，或直接粘贴单条视频 weixin.qq.com/sph/... 链接（自动反查作者）"
      : PLATFORM === "kuaishou"
      ? "使用公开主页作品列表；可粘贴完整分享文案、主页链接、v.kuaishou.com 短链或 user_id"
      : "使用公开主页作品列表，评论和下载继续沿用下方设置",
  );
}

async function normalizeCollectionCreatorTarget(button = null, { quiet = false } = {}) {
  return normalizePlatformShareTarget({
    inputId: "col-source-ref", feedbackId: "col-source-ref-feedback", platform: PLATFORM,
    button, quiet, resolveChannelsCreator: PLATFORM === "shipinhao",
  });
}

function handleCollectionCreatorPaste() {
  setTimeout(() => normalizeCollectionCreatorTarget(null, { quiet: true }), 0);
}

function resetWatchTargetState() {
  resetSmartTargetState(
    "w-url", "w-url-feedback",
    PLATFORM === "shipinhao" && $("w-kind")?.value === "user"
      ? "已选「账号近期作品」：可粘贴 v2_...@finder、主页链接或单条视频 weixin.qq.com/sph/...（自动反查作者）"
      : "支持完整分享文案、作品链接、创作者主页、平台短链或 ID",
  );
}

function normalizeWatchTarget(button = null, options = {}) {
  return normalizePlatformShareTarget({
    inputId: "w-url", feedbackId: "w-url-feedback", platform: PLATFORM,
    button, quiet: !!options.quiet,
    resolveChannelsCreator: PLATFORM === "shipinhao" && $("w-kind")?.value === "user",
  });
}

function handleWatchTargetPaste() {
  setTimeout(() => normalizeWatchTarget(null, { quiet: true }), 0);
}

function handleWatchTargetKeydown(event) {
  if (!event || event.key !== "Enter" || event.isComposing) return;
  event.preventDefault();
  addWatch();
}

function resetDanmakuTargetState() {
  resetSmartTargetState(
    "d-w-url", "d-w-url-feedback",
    PLATFORM === "bilibili"
      ? "支持完整哔哩哔哩分享文案、BV/av 号、视频链接、空间主页或短链"
      : "支持完整抖音分享文案、视频链接、账号主页、短链或 ID",
  );
}

function normalizeDanmakuTarget(button = null, options = {}) {
  return normalizePlatformShareTarget({
    inputId: "d-w-url", feedbackId: "d-w-url-feedback", platform: PLATFORM,
    button, quiet: !!options.quiet,
  });
}

function handleDanmakuTargetPaste() {
  setTimeout(() => normalizeDanmakuTarget(null, { quiet: true }), 0);
}

function handleDanmakuTargetKeydown(event) {
  if (!event || event.key !== "Enter" || event.isComposing) return;
  event.preventDefault();
  addDanmakuWatch();
}

let _LAST_MONITOR_PLATFORM = "";

function applyMonitorForm() {
  const title = $("mon-add-title");
  const lbl = $("t-url-label");
  const parseButton = document.querySelector('[data-panel="monitors"] .smart-target-action');
  const isSph = PLATFORM === "shipinhao";
  if ($("t-acc-wrap")) $("t-acc-wrap").style.display = isSph ? "none" : "";
  if (isSph && $("t-acc")) $("t-acc").value = "";
  const supportsBackfill = !["xhs", "rednote"].includes(PLATFORM);
  if ($("t-backfill-wrap")) $("t-backfill-wrap").style.display = supportsBackfill ? "" : "none";
  if ($("t-backfill") && _LAST_MONITOR_PLATFORM !== PLATFORM) {
    $("t-backfill").value = ["shipinhao", "kuaishou", "tiktok", "bilibili", "instagram", "twitter", "facebook", "youtube"].includes(PLATFORM) ? "5" : "";
    $("t-backfill")._csSync?.();
  }
  _LAST_MONITOR_PLATFORM = PLATFORM;
  if (["douyin", "kuaishou", "shipinhao", "tiktok", "bilibili", "instagram", "twitter", "facebook", "youtube"].includes(PLATFORM)) {
    const isKs = PLATFORM === "kuaishou";
    const isTikTok = PLATFORM === "tiktok";
    const isBili = PLATFORM === "bilibili";
    const isIg = PLATFORM === "instagram";
    const isTw = PLATFORM === "twitter";
    const isFb = PLATFORM === "facebook";
    const isYt = PLATFORM === "youtube";
    if (title) title.innerHTML = (isKs || isSph || isBili || isIg || isTw || isFb || isYt ? '添加创作者监控' : '添加作品监控')
      + ' <span class="sub">监控并下载新作品</span>';
    if (lbl) lbl.textContent = isTikTok
      ? "创作者主页 / 完整分享文案 / @username"
      : isIg
      ? "Instagram 主页链接 / @username / 数字 UID"
      : isTw
      ? "Twitter (X) 主页链接 / @username / 数字 UID"
      : isFb
      ? "Facebook 个人主页 / 公共主页 / 群组链接或数字 ID"
      : isYt
      ? "YouTube 频道链接 / @handle / UC... 频道 ID"
      : isSph
      ? "创作者主页 / v2_...@finder / 单条视频分享链接"
      : isKs
      ? "创作者主页 / 完整分享文案 / user_id"
      : isBili
      ? "UP 主空间主页 / 完整分享文案 / mid"
      : "创作者主页 / 完整分享文案 / sec_uid";
    $("t-url").placeholder = isTikTok
      ? "直接粘贴整段 TikTok 主页或作品分享文案，将自动提取 @username"
      : isIg
      ? "粘贴 https://www.instagram.com/<username>/ 或填写 @username"
      : isTw
      ? "粘贴 https://x.com/<username> 或填写 @username"
      : isFb
      ? "粘贴 https://www.facebook.com/<page>、/groups/<id> 或 profile.php?id=..."
      : isYt
      ? "粘贴 https://www.youtube.com/@handle、/channel/UC... 或填写 @handle"
      : isSph
      ? "粘贴视频号主页链接、v2_...@finder、sph 标识或单条视频 weixin.qq.com/sph/... 链接"
      : isKs
      ? "直接粘贴整段快手主页分享文案，将自动提取有效链接"
      : isBili
      ? "直接粘贴整段哔哩哔哩空间或视频分享文案，或填写 UP 主 mid"
      : "直接粘贴整段抖音主页分享文案，将自动提取有效链接";
    if (parseButton) parseButton.classList.remove("hidden");
    resetMonitorTargetState();
    return;
  }
  const kind = $("t-kind") ? $("t-kind").value : "creator";
  if (kind === "keyword") {
    if (title) title.innerHTML = '添加关键词监控 <span class="sub">盯一个搜索词的新笔记</span>';
    if (lbl) lbl.textContent = "搜索关键词";
    $("t-url").placeholder = "例如:口红试色 / 露营装备";
    if (parseButton) parseButton.classList.add("hidden");
  } else {
    if (title) title.innerHTML = '添加创作者监控 <span class="sub">监控并下载新笔记</span>';
    if (lbl) lbl.textContent = "创作者主页 / 完整分享文案 / user_id";
    $("t-url").placeholder = PLATFORM === "rednote" ? "粘贴 www.rednote.com/zh/user/profile/... 主页链接或 24 位 user_id" : "直接粘贴整段小红书主页分享文案，将自动提取有效链接";
    if (parseButton) parseButton.classList.remove("hidden");
  }
  resetMonitorTargetState();
}

// ─── 标签页切换 ───
function switchTab(name, pushHistory = false) {
  if (name === "agent") {
    window.dispatchEvent(new Event("creatorhub:assistant:open"));
    return;
  }
  if (!PAGE_META[name]) name = "overview";
  // 若从全平台入口主动跳转到某个单平台功能（如去添加作品监控），自动退出全平台模式聚焦当前底层平台
  if (GLOBAL_MODE && SINGLE_PF_ONLY_TABS.includes(name)) {
    GLOBAL_MODE = false;
    try { localStorage.setItem("dym-global", "0"); } catch (e) {}
    CURRENT_TAB = name;
    applyPlatformUI();
  }
  globalThis.CreatorHubWorkbench?.beforeNavigate?.();
  const changed = CURRENT_TAB !== name;
  CURRENT_TAB = name;
  if (_openSelectClose) _openSelectClose();
  if (_openDateClose) _openDateClose();
  if (OPEN_META_COMBO) OPEN_META_COMBO.close();
  let activePanel = null;
  document.querySelectorAll("[data-panel]").forEach(p => {
    const active = p.dataset.panel === name;
    p.style.display = active ? "" : "none";
    p.classList.remove("panel-enter");
    if (active) activePanel = p;
  });
  if (changed && activePanel) requestAnimationFrame(() => {
    activePanel.classList.add("panel-enter");
    activePanel.addEventListener("animationend", () => activePanel.classList.remove("panel-enter"), { once: true });
  });
  document.querySelectorAll(".navitem").forEach(t => {
    const active = t.dataset.tab === name;
    t.classList.toggle("active", active);
    if (active) t.setAttribute("aria-current", "page");
    else t.removeAttribute("aria-current");
  });
  try { localStorage.setItem("dym-tab", name); } catch (e) {}
  try {
    if (pushHistory && changed) history.pushState(null, "", "#" + name);
    else if (location.hash !== "#" + name) history.replaceState(null, "", "#" + name);
  } catch (e) {}
  updatePageContext(name);
  window.scrollTo({ top: 0, behavior: "auto" });
  if (changed) requestAnimationFrame(() => {
    const title = $("page-title");
    if (title && (!activePanel || !activePanel.contains(document.activeElement))) title.focus({ preventScroll: true });
  });
  if (name === "hub") { refreshHubSummary(); refreshHubPanel(); }
  else stopDmStream();   // 离开本账号管理即断开私信实时流
  if (name === "share-download") {
    loadShareAccounts();
    refreshShareHistory();
  }
  if (name === "collections") { populateCollectionAccount(); refreshCollections(); }
  if (name === "tiktok-tools") populateTikTokAccounts();
  if (name === "youtube-tools") populateYouTubeToolsAccounts();
  if (name === "queue") refreshTaskQueue();
  if (name === "risk-control") refreshRiskCenter();
  if (changed && !["hub", "share-download", "collections", "queue", "risk-control"].includes(name)) loop();
}


// ── 单行自适应 Priority+ 平台栏：空间充足全展开，窄屏自动折叠尾部到「+N ▾」并始终保留当前平台可见 ──
(() => {
  const host = document.getElementById("pswitch-host");
  const list = host ? host.querySelector(".pswitch") : document.querySelector(".pswitch");
  const moreBtn = document.getElementById("pswitch-more-btn");
  const moreLabel = document.getElementById("pswitch-more-label");
  const moreDot = document.getElementById("pswitch-more-dot");
  const popover = document.getElementById("pswitch-popover");
  if (!host || !list || !moreBtn) return;

  let popOpen = false;
  let rafId = 0;
  let lastLayoutSig = "";
  const moOpts = { childList: true, subtree: true, characterData: true, attributes: true, attributeFilter: ["class"] };
  const mo = new MutationObserver(() => { lastLayoutSig = ""; scheduleLayout(); });

  function tabButtons() {
    return [...list.querySelectorAll('button[role="tab"][data-pf]')];
  }

  function layoutPriority() {
    rafId = 0;
    const available = Math.floor(host.clientWidth);
    if (available <= 0) return;
    const allBtns = tabButtons();
    const activePf = allBtns.find(b => b.classList.contains("active"))?.dataset.pf || "";
    const sig = `${available}:${activePf}:${allBtns.map(b => b.classList.contains("ps-idle-hidden") ? "0" : b.textContent.trim()).join("|")}`;
    if (sig === lastLayoutSig) return;
    lastLayoutSig = sig;

    mo.disconnect();
    try {
      const candidates = allBtns.filter(b => !b.classList.contains("ps-idle-hidden"));
      // 1. 先恢复全部可见以便准确测量自然宽度
      allBtns.forEach(b => b.classList.remove("ps-overflow-hidden"));
      moreBtn.hidden = true;

      const widths = candidates.map(b => Math.ceil(b.getBoundingClientRect().width));
      const gap = 2;
      const pad = 8;
      const totalW = pad + widths.reduce((sum, w) => sum + w, 0) + Math.max(0, candidates.length - 1) * gap;

      if (totalW <= available) {
        if (popOpen) togglePopover(false);
        window.dispatchEvent(new Event("creatorhub:layout"));
        return;
      }

      // 2. 空间不足：按 Priority+ 折叠，保留「全平台」+ 尽可能多的平台 + 当前选中平台 + 「+N ▾」
      moreBtn.hidden = false;
      if (moreLabel) moreLabel.textContent = `+${candidates.length}`;
      const moreW = Math.max(54, Math.ceil(moreBtn.getBoundingClientRect().width) + gap);
      const budget = Math.max(120, available - pad - moreW);

      const activeIdx = candidates.findIndex(b => b.classList.contains("active"));
      const keep = new Set();
      let used = 0;

      if (candidates.length > 0) {
        keep.add(0);
        used += widths[0];
      }
      if (activeIdx > 0 && used + gap + widths[activeIdx] <= budget) {
        keep.add(activeIdx);
        used += gap + widths[activeIdx];
      }
      for (let i = 1; i < candidates.length; i++) {
        if (keep.has(i)) continue;
        if (used + gap + widths[i] <= budget) {
          keep.add(i);
          used += gap + widths[i];
        } else {
          break;
        }
      }

      let hiddenCount = 0;
      let hiddenDanger = false;
      let hiddenWarn = false;
      candidates.forEach((b, idx) => {
        const hide = !keep.has(idx);
        b.classList.toggle("ps-overflow-hidden", hide);
        if (hide) {
          hiddenCount++;
          const cnt = b.querySelector(".ps-cnt");
          if (cnt?.classList.contains("danger")) hiddenDanger = true;
          else if (cnt?.classList.contains("warn")) hiddenWarn = true;
        }
      });

      if (hiddenCount <= 0) {
        moreBtn.hidden = true;
        if (popOpen) togglePopover(false);
      } else {
        moreBtn.hidden = false;
        if (moreLabel) moreLabel.textContent = `+${hiddenCount}`;
        moreBtn.title = `展开其余 ${hiddenCount} 个平台`;
        if (moreDot) {
          moreDot.hidden = !(hiddenDanger || hiddenWarn);
          moreDot.className = "ps-more-dot" + (hiddenDanger ? " danger" : hiddenWarn ? " warn" : "");
        }
      }
      if (popOpen) renderPopover();
      window.dispatchEvent(new Event("creatorhub:layout"));
    } finally {
      mo.observe(list, moOpts);
    }
  }

  function scheduleLayout() {
    if (rafId) cancelAnimationFrame(rafId);
    rafId = requestAnimationFrame(layoutPriority);
  }

  function renderPopover() {
    if (!popover) return;
    const btns = tabButtons().filter(b => !b.classList.contains("ps-idle-hidden"));
    const items = btns.map(b => {
      const pf = b.dataset.pf || "";
      const active = b.classList.contains("active");
      const folded = b.classList.contains("ps-overflow-hidden");
      const label = b.querySelector(".ps-lbl")?.textContent?.trim() || b.dataset.baseLabel || b.textContent.trim();
      const cntEl = b.querySelector(".ps-cnt");
      const cntHtml = cntEl ? cntEl.outerHTML : "";
      return `<button type="button" class="pswitch-pop-item${active ? " active" : ""}${folded ? " is-folded" : ""}" data-pf="${esc(pf)}"><span class="pswitch-pop-name">${esc(label)}</span>${cntHtml}</button>`;
    }).join("");
    popover.innerHTML = `<div class="pswitch-pop-head"><b>全部平台</b><span class="mut">共 ${btns.length} 项</span></div><div class="pswitch-pop-grid">${items}</div>`;
    popover.querySelectorAll(".pswitch-pop-item").forEach(btn => {
      btn.addEventListener("click", () => {
        const pf = btn.dataset.pf;
        togglePopover(false);
        if (pf && typeof switchPlatform === "function") switchPlatform(pf);
      });
    });
  }

  function positionPopover() {
    if (!popover || !moreBtn) return;
    const rect = moreBtn.getBoundingClientRect();
    const popW = Math.min(420, window.innerWidth - 24);
    const left = Math.max(12, Math.min(window.innerWidth - popW - 12, rect.right - popW));
    popover.style.left = `${left}px`;
    popover.style.right = "auto";
    popover.style.top = `${Math.round(rect.bottom + 8)}px`;
  }

  function togglePopover(force) {
    if (!popover) return;
    popOpen = typeof force === "boolean" ? force : !popOpen;
    popover.hidden = !popOpen;
    moreBtn.setAttribute("aria-expanded", String(popOpen));
    if (popOpen) {
      renderPopover();
      positionPopover();
    }
  }

  moreBtn.addEventListener("click", e => {
    e.stopPropagation();
    togglePopover();
  });

  document.addEventListener("click", e => {
    if (!popOpen) return;
    if (popover && !popover.contains(e.target) && !moreBtn.contains(e.target)) {
      togglePopover(false);
    }
  });
  document.addEventListener("keydown", e => {
    if (e.key === "Escape" && popOpen) togglePopover(false);
  });

  if (typeof ResizeObserver !== "undefined") {
    const ro = new ResizeObserver(scheduleLayout);
    ro.observe(host);
  }
  window.addEventListener("resize", () => {
    scheduleLayout();
    if (popOpen) positionPopover();
  }, { passive: true });
  // 当 syncHeaderPlatformBadges 更新计数徽标后重新计算一次折叠
  mo.observe(list, moOpts);

  window._syncPlatformStripOverflow = () => { lastLayoutSig = ""; scheduleLayout(); };
  scheduleLayout();
})();
