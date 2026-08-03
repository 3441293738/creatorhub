(function () {
  "use strict";

  const realFetch = window.fetch.bind(window);
  const now = Math.floor(Date.now() / 1000);
  const iso = (secondsAgo) => new Date(Date.now() - secondsAgo * 1000).toISOString().replace(/Z$/, "");

  const accounts = [
    { id: 1, platform: "douyin", nickname: "示例账号 A", douyin_id: "creator_demo", sec_uid: "DEMO_DOUYIN_ACCOUNT", aweme_count: 56, follower_count: 1280, login_type: "browser", has_storage: true, has_creator: true, status: "active", monitor_count: 2, created_at: iso(86400 * 2), has_proxy: true, proxy: "http://***:***@HOST:PORT", proxy_status: "ok" },
    { id: 2, platform: "xhs", nickname: "小红书示例号", douyin_id: "red_demo", sec_uid: "DEMO_XHS_ACCOUNT", aweme_count: 24, follower_count: 860, login_type: "browser", has_storage: true, has_creator: true, status: "active", monitor_count: 1, created_at: iso(86400 * 3), has_proxy: true, proxy: "http://***:***@HOST:PORT", proxy_status: "ok" },
    { id: 3, platform: "kuaishou", nickname: "快手示例号", douyin_id: "ks_demo", sec_uid: "DEMO_KS_ACCOUNT", aweme_count: 38, follower_count: 620, login_type: "browser", has_storage: true, has_creator: true, status: "active", monitor_count: 1, created_at: iso(86400 * 4), has_proxy: false, proxy_status: "unknown" },
    { id: 4, platform: "shipinhao", nickname: "视频号示例号", douyin_id: "channels_demo", sec_uid: "DEMO_CHANNELS_ACCOUNT", aweme_count: 18, follower_count: 430, login_type: "browser", has_storage: true, has_creator: true, status: "active", monitor_count: 0, created_at: iso(86400 * 5), has_proxy: false, proxy_status: "unknown" },
  ];

  const platformAccount = (platform) => accounts.find((item) => item.platform === platform) || accounts[0];
  const platformName = { douyin: "抖音", xhs: "小红书", kuaishou: "快手", shipinhao: "视频号" };
  const monitoredName = { douyin: "示例创作者", xhs: "城市生活研究所", kuaishou: "影像日记", shipinhao: "本账号内容" };

  function monitor(platform) {
    const account = platformAccount(platform);
    return {
      id: 11, platform, target_kind: "creator", nickname: monitoredName[platform], sec_uid: `DEMO_${platform.toUpperCase()}_CREATOR`,
      alias: "重点内容源", group_name: "示例分组", tags: ["旅行", "日常"], account_id: account.id,
      content_count: platform === "xhs" ? 24 : 56, interval_seconds: 1800, download_enabled: true,
      media_filter: "all", video_quality: "highest", download_dir: "", last_scan_at: iso(900), enabled: true,
    };
  }

  function contents(platform) {
    const isXhs = platform === "xhs";
    return [
      { id: 101, target_id: 11, platform, aweme_id: "DEMO_WORK_001", desc: isXhs ? "周末城市漫游路线分享" : "把普通的一天剪成一段小电影", media_type: isXhs ? "images" : "video", quality: isXhs ? "" : "1080P", create_time: now - 3600, like_count: 328, duration: isXhs ? 0 : 42, download_status: "done", local_path: "data/media/demo/work-001.mp4", cover_url: "" },
      { id: 102, target_id: 11, platform, aweme_id: "DEMO_WORK_002", desc: isXhs ? "高效整理素材的五个习惯" : "镜头里的夏日城市与晚风", media_type: "video", quality: "1080P", create_time: now - 10800, like_count: 186, duration: 35, download_status: "done", local_path: "data/media/demo/work-002.mp4", cover_url: "" },
    ];
  }

  function watches(platform) {
    return [{ id: 21, platform, kind: "user", title: "示例账号近期作品", sec_uid: `DEMO_${platform.toUpperCase()}_WATCH`, alias: "重点评论区", group_name: "互动观察", tags: ["高互动"], mode: "public", comment_count: 18, interval_seconds: 1800, last_scan_at: iso(1200), enabled: true, recent_works: 5, recent_days: 7, max_scrolls: 6 }];
  }

  function comments(platform) {
    return [
      { id: 301, watch_id: 21, platform, text: "这个内容很实用，已经收藏了。", user_nickname: "示例用户 1", like_count: 12, create_time: now - 1800, is_reply: false },
      { id: 302, watch_id: 21, platform, text: "期待下一期，也想看看完整流程。", user_nickname: "示例用户 2", like_count: 7, create_time: now - 3200, is_reply: false },
    ];
  }

  const proxies = [{ id: 41, label: "住宅代理 · 广东", url: "http://***:***@HOST:PORT", note: "在线演示数据", status: "ok", enabled: true, used_by: 1, geo_checked: true, is_mainland: true, geo_loc: "中国 · 广东", exit_ip: "113.***.***.26", isp: "住宅网络" }];

  function publishTasks(platform) {
    return [{ id: 51, platform, account_id: platformAccount(platform).id, title: "夏日城市漫游", media_type: "images", media_count: 6, source_platform: "", scheduled_at: null, status: "pending", error: "", result_url: "" }];
  }

  function commentRules(platform) {
    return [{ id: 61, platform, name: "示例自动回复规则", mode: "auto_reply", target_kind: "self", account_id: platformAccount(platform).id, templates: ["谢谢支持，欢迎常来。"], use_ai: false, require_review: true, reply_filter: "", skip_keywords: "", daily_cap: 20, min_gap_seconds: 90, max_per_run: 5, interval_seconds: 1800, enabled: false, last_run_at: iso(86400), last_error: "" }];
  }

  function commentTasks(platform) {
    return [{ id: 71, platform, content: "谢谢支持，欢迎常来看看。", aweme_id: "DEMO_WORK_001", target_comment_id: "DEMO_COMMENT", target_nick: "示例用户", scheduled_at: iso(-1800), method: "browser", status: "draft", error: "" }];
  }

  const shareHistory = [
    { id: 81, created_at: iso(1200), platform: "douyin", title: "示例作品 · 城市漫游", author: "示例创作者", item_id: "DEMO_001", media_type: "video", media_count: 1, status: "done", source_url: "https://v.douyin.com/DEMO/", output_dir: "data/media/demo", files: [{ role: "media", path: "data/media/demo/demo.mp4", size: 18_600_000 }] },
    { id: 82, created_at: iso(7200), platform: "xhs", title: "示例笔记 · 周末记录", author: "示例创作者", item_id: "DEMO_002", media_type: "images", media_count: 6, status: "done", source_url: "https://www.xiaohongshu.com/explore/DEMO", output_dir: "data/media/demo", files: [{ role: "media", path: "data/media/demo/01.jpg", size: 2_100_000 }] },
  ];

  function series() {
    const days = [];
    for (let index = 6; index >= 0; index -= 1) days.push(new Date(Date.now() - index * 86400000).toISOString().slice(0, 10));
    return { days, contents: [3, 5, 4, 8, 6, 10, 7], comments: [5, 8, 6, 12, 9, 15, 11] };
  }

  function getData(url) {
    const path = url.pathname;
    const platform = url.searchParams.get("platform") || "douyin";
    if (path === "/api/accounts") return url.searchParams.has("platform") ? accounts.filter((item) => item.platform === platform) : accounts;
    if (path === "/api/proxies/options") return proxies.map((item) => ({ url: item.url, label: item.label, status: item.status, used_by: item.used_by, masked: item.url, enabled: item.enabled }));
    if (path === "/api/proxies") return proxies;
    if (path === "/api/monitors") return platform === "shipinhao" ? [] : [monitor(platform)];
    if (path === "/api/contents") return platform === "shipinhao" ? [] : contents(platform);
    if (path === "/api/comment-watches") return platform === "shipinhao" ? [] : watches(platform);
    if (path === "/api/comments") return platform === "shipinhao" ? [] : comments(platform);
    if (path === "/api/stats/series") return series();
    if (path === "/api/publish") return publishTasks(platform);
    if (path === "/api/publish/published") return [];
    if (path === "/api/comment-rules") return platform === "shipinhao" ? [] : commentRules(platform);
    if (path === "/api/comment-tasks") return platform === "shipinhao" ? [] : commentTasks(platform);
    if (path === "/api/share-download/history") return shareHistory;
    if (path === "/api/notifications") return [{ id: 91, name: "演示通知渠道", type: "bark", enabled: true, config: {} }];
    if (path === "/api/settings") return { download_dir: "data/media", video_quality: "highest", ai_enabled: false, ai_base_url: "", ai_model: "", ai_temperature: "0.9", ai_prompt: "", ai_api_key_set: false };
    if (path === "/api/hub/summary") return { works: 3, following: 20, fans: 168, dm: 4 };
    if (path === "/api/account-works") return contents(platform).map((item, index) => ({ ...item, id: 201 + index, item_id: item.aweme_id, play_count: 6800 - index * 1200, comment_count: 32 - index * 7, status: "正常" }));
    if (/^\/api\/account-works\/\d+\/comments$/.test(path)) return comments(platform).map((item) => ({ ...item, user_nickname: item.user_nickname }));
    if (path === "/api/follows") return [{ id: 221, nickname: "示例好友", signature: "记录生活与创作", is_mutual: true, is_following: true, uid: "DEMO_UID", sec_uid: "DEMO_SEC_UID", avatar: "" }];
    if (path === "/api/dm/conversations") return [{ conv_id: "DEMO_CONVERSATION", peer_nickname: "示例联系人", peer_avatar: "", last_text: "最近一条消息预览", unread_count: 2, peer_uid: "DEMO_UID", peer_sec_uid: "DEMO_SEC_UID" }];
    if (path === "/api/dm/messages") return [{ id: 231, direction: "in", text: "你好，想了解一下这个功能。", create_time: now - 600 }, { id: 232, direction: "out", text: "你好，这里是在线演示界面。", create_time: now - 420 }];
    if (/^\/api\/account-stats\/\d+$/.test(path)) return { account: { follower_count: 1280, aweme_count: 56 }, fans_delta: 86, trend: [1120, 1148, 1175, 1204, 1232, 1260, 1280].map((follower_count, index) => ({ follower_count, date: index })), works: contents(platform).map((item) => ({ desc: item.desc, play_count: 6800, like_count: item.like_count, comment_count: 28, status: "正常" })) };
    if (/^\/api\/contents\/\d+\/media$/.test(path)) return { media_type: "images", cover_url: "", medias: [] };
    return [];
  }

  function actionData(path) {
    if (path === "/api/share-download/links") return { count: 1, links: [{ platform: "douyin", host: "v.douyin.com", url: "https://v.douyin.com/DEMO/" }] };
    if (path === "/api/share-download") return { ok: true, results: [{ ok: true, platform: "douyin", title: "示例作品", author: "示例创作者", media_type: "video", media_count: 1, output_dir: "data/media/demo", files: [] }] };
    if (/\/run-now$/.test(path)) return { ok: true, created: 1, candidates: 1, review: true, new: 1, new_comments: 1 };
    if (/\/sync$/.test(path)) return { ok: true, fetched: 3, added: 1 };
    if (/\/test$/.test(path)) return { ok: true, detail: "在线演示" };
    return { ok: true, deleted: 1, files_removed: 0, approved: 1, fetched: 1, added: 1, new: 1 };
  }

  window.fetch = async function demoFetch(input, init) {
    const raw = typeof input === "string" ? input : input && input.url;
    const url = new URL(raw || "", window.location.href);
    if (!url.pathname.startsWith("/api/")) return realFetch(input, init);
    await new Promise((resolve) => setTimeout(resolve, 90));
    const method = String((init && init.method) || "GET").toUpperCase();
    const data = method === "GET" ? getData(url) : actionData(url.pathname);
    return new Response(JSON.stringify(data), { status: 200, headers: { "Content-Type": "application/json; charset=utf-8" } });
  };

  window.EventSource = class DemoEventSource {
    constructor() { this.readyState = 1; }
    close() { this.readyState = 2; }
  };

  document.addEventListener("DOMContentLoaded", function () {
    const engine = document.getElementById("engine-status");
    const label = engine && engine.querySelector(".engine-label");
    if (engine) engine.title = "GitHub Pages 静态在线演示 · 使用示例数据";
    if (label) label.textContent = "在线演示";
    const brandSub = document.querySelector(".brand-sub");
    if (brandSub) brandSub.textContent = "多平台内容工作台 · DEMO";
  });
})();
