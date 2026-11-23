(function () {
  "use strict";

  let demoDiagLevel = "info";
  const realFetch = window.fetch.bind(window);
  const engineDefaults = {
    xhs_read_mode: "api", rednote_read_mode: "hybrid", douyin_read_mode: "hybrid", douyin_write_mode: "browser",
    tiktok_read_mode: "native", channels_read_mode: "auto",
    bilibili_read_mode: "hybrid", bilibili_write_mode: "hybrid",
    instagram_read_mode: "hybrid", instagram_write_mode: "hybrid",
    twitter_read_mode: "hybrid", twitter_write_mode: "browser",
    douyin_profile_mode: "hybrid", douyin_followers_mode: "hybrid",
    douyin_dm_sync_mode: "hybrid", douyin_creator_danmaku_mode: "hybrid",
    douyin_publish_mode: "browser",
    douyin_keyword_gap_seconds: 8, xhs_keyword_gap_seconds: 10,
    block_media_resources: false, route_download_via_proxy: true, comment_browser_headed: true,
    monitor_initial_backfill_count: 0, comment_recent_works: 5,
    comment_recent_days: 7, comment_max_scrolls: 6,
    danmaku_recent_works: 5, danmaku_recent_days: 7, danmaku_max_scrolls: 6,
    request_timeout_seconds: 20,
    download_timeout_seconds: 120, xhs_item_gap_seconds: 2.5, xhs_request_jitter: .35,
    xhs_publish_mode: "browser", rednote_publish_mode: "browser", xhs_comment_write_mode: "browser", xhs_comment_review_before_publish: true,
    work_health_enabled: false, work_health_interval_seconds: 3600, work_health_zero_play_hours: 6,
    work_health_recent_days: 7, work_health_stat_snapshots: true,
    scan_interval_seconds: 300, idle_keepalive_hours: 6,
    danmaku_probe_step_seconds: 1, danmaku_max_probe_points: 120,
    danmaku_max_records_per_scan: 1000, danmaku_max_records_total: 0,
    xhs_dm_monitor_enabled: false, xhs_dm_poll_interval_seconds: 120,
    xhs_dm_realtime_enabled: true, xhs_dm_realtime_debounce_seconds: 1.5,
    xhs_dm_fallback_interval_seconds: 600, xhs_dm_max_conversations_per_poll: 2,
    xhs_dm_auto_reply_enabled: false,
    comment_daily_cap_per_account: 30, comment_min_gap_seconds: 60,
    comment_hourly_cap_per_account: 10,
    action_daily_cap_per_account: 20, action_hourly_cap_per_account: 6,
    action_min_gap_seconds: 90, verify_proxy_region: true,
    strict_proxy_required: false, forbid_same_platform_shared_proxy: false,
    quiet_hours_include_reads: true, quiet_hours_wake_jitter_minutes: 35,
    native_write_gate_enabled: true, native_write_require_system_chrome: true,
    native_write_require_verified_proxy: true, native_write_proxy_max_age_seconds: 86400,
  };
  const engineValues = { ...engineDefaults };
  const channelsProto = {
    connection_mode: "none",
    proxy_enabled: false,
    proxy_port: 2023,
    proxy_running: false,
    ca_trusted: false,
    cdp_endpoint: "",
    cdp_endpoint_file: "",
    bridge_running: false,
  };
  function channelsProtoPayload() {
    const readMode = engineValues.channels_read_mode || "auto";
    const configured = Boolean(channelsProto.proxy_enabled || channelsProto.cdp_endpoint || channelsProto.cdp_endpoint_file);
    const protocolUsable = channelsProto.proxy_enabled ? channelsProto.proxy_running : configured;
    return {
      platform: "shipinhao",
      read_mode: readMode,
      configured,
      proxy_configured: Boolean(channelsProto.proxy_enabled),
      endpoint_configured: Boolean(channelsProto.cdp_endpoint),
      endpoint_file_configured: Boolean(channelsProto.cdp_endpoint_file),
      config: {
        read_mode: readMode,
        connection_mode: channelsProto.connection_mode,
        proxy_enabled: Boolean(channelsProto.proxy_enabled),
        proxy_port: Number(channelsProto.proxy_port || 2023),
        cdp_endpoint: channelsProto.cdp_endpoint,
        cdp_endpoint_file: channelsProto.cdp_endpoint_file,
        configured,
        endpoint_file_info: channelsProto.cdp_endpoint_file
          ? { path: channelsProto.cdp_endpoint_file, exists: channelsProto.bridge_running, valid: channelsProto.bridge_running, server_pid: channelsProto.bridge_running ? 1001 : null, server_running: channelsProto.bridge_running, error: channelsProto.bridge_running ? "" : "口令文件不存在" }
          : null,
        saved_fields: configured ? ["channels_proxy_enabled"] : [],
      },
      bridge: {
        running: channelsProto.bridge_running,
        bind_host: "127.0.0.1",
        debug_listening_port: channelsProto.bridge_running ? 9421 : null,
        cdp_listening_port: channelsProto.bridge_running ? 9422 : null,
        allowed_native_pids: channelsProto.bridge_running ? [1001] : [],
        native_connected: false,
        cdp_connected: false,
        commands_forwarded: 0,
        results_forwarded: 0,
        rejected_connections_or_packets: 0,
        wechat_processes: [],
      },
      proxy: {
        running: channelsProto.proxy_running,
        state: channelsProto.proxy_running ? "waiting_wechat" : "stopped",
        listen_host: "127.0.0.1",
        proxy_port: Number(channelsProto.proxy_port || 2023),
        system_proxy_managed: channelsProto.proxy_running,
        system_proxy: { supported: true, proxy_enable: channelsProto.proxy_running, proxy_server: channelsProto.proxy_running ? `127.0.0.1:${channelsProto.proxy_port}` : "" },
        upstream_proxy: { enabled: false, host: "", port: 0, server: "", note: "" },
        ca: { exists: channelsProto.ca_trusted, trusted: channelsProto.ca_trusted, thumbprint: channelsProto.ca_trusted ? "DEMO1234567890" : "", store: channelsProto.ca_trusted ? "CurrentUser\\Root" : "" },
        connected_pages: 0,
        ready_pages: 0,
        pages: [],
        wechat_traffic_seen: false,
        liteapp_detected: false,
        bridge_entry_url: "https://channels.weixin.qq.com/platform/bridge",
        alt_bridge_entry_url: "https://channels.weixin.qq.com/web/pages/bridge",
        commands_forwarded: 0,
        commands_succeeded: 0,
      },
      capabilities: {
        account_works: true,
        keyword_search: protocolUsable && readMode !== "browser",
        creator_works: protocolUsable && readMode !== "browser",
        full_comment_threads: protocolUsable && readMode !== "browser",
        encrypted_video_decode: true,
        share_preview: true,
      },
    };
  }
  const now = Math.floor(Date.now() / 1000);
  const iso = (secondsAgo) => new Date(Date.now() - secondsAgo * 1000).toISOString().replace(/Z$/, "");

  const accounts = [
    { id: 1, platform: "douyin", nickname: "示例账号 A", douyin_id: "creator_demo", sec_uid: "DEMO_DOUYIN_ACCOUNT", aweme_count: 56, follower_count: 1280, login_type: "browser", has_storage: true, has_creator: true, status: "active", monitor_count: 2, created_at: iso(86400 * 2), has_proxy: true, proxy: "http://***:***@HOST:PORT", proxy_status: "ok" },
    { id: 2, platform: "xhs", nickname: "小红书示例号", douyin_id: "red_demo", sec_uid: "DEMO_XHS_ACCOUNT", aweme_count: 24, follower_count: 860, login_type: "browser", has_storage: true, has_creator: true, status: "active", monitor_count: 1, created_at: iso(86400 * 3), has_proxy: true, proxy: "http://***:***@HOST:PORT", proxy_status: "ok" },
    { id: 3, platform: "kuaishou", nickname: "快手示例号", douyin_id: "ks_demo", sec_uid: "DEMO_KS_ACCOUNT", aweme_count: 38, follower_count: 620, login_type: "browser", has_storage: true, has_creator: true, status: "active", monitor_count: 1, created_at: iso(86400 * 4), has_proxy: false, proxy_status: "unknown" },
    { id: 4, platform: "shipinhao", nickname: "视频号示例号", douyin_id: "channels_demo", sec_uid: "DEMO_CHANNELS_ACCOUNT", aweme_count: 18, follower_count: 430, login_type: "browser", has_storage: true, has_creator: true, status: "active", monitor_count: 0, created_at: iso(86400 * 5), has_proxy: false, proxy_status: "unknown" },
    { id: 5, platform: "tiktok", nickname: "TikTok 示例号", douyin_id: "tiktok_demo", sec_uid: "DEMO_TIKTOK_ACCOUNT", aweme_count: 15, follower_count: 920, login_type: "browser", has_storage: true, has_creator: true, status: "active", monitor_count: 1, created_at: iso(86400 * 3), has_proxy: true, proxy: "http://***:***@HOST:PORT", proxy_status: "ok" },
    { id: 6, platform: "bilibili", nickname: "B站示例 UP 主", douyin_id: "1008601", sec_uid: "1008601", aweme_count: 42, follower_count: 3560, login_type: "browser", has_storage: true, has_creator: true, status: "active", monitor_count: 1, created_at: iso(86400 * 2), has_proxy: false, proxy_status: "unknown" },
    { id: 7, platform: "rednote", nickname: "Rednote 示例号", douyin_id: "rn_demo", sec_uid: "DEMO_REDNOTE_ACCOUNT", aweme_count: 19, follower_count: 540, login_type: "browser", has_storage: true, has_creator: true, status: "active", monitor_count: 1, created_at: iso(86400 * 2), has_proxy: true, proxy: "socks5://***:***@HOST:PORT", proxy_status: "ok" },
    { id: 8, platform: "instagram", nickname: "Instagram 示例号", douyin_id: "ig_creator_demo", sec_uid: "900100200", aweme_count: 31, follower_count: 2150, login_type: "browser", has_storage: true, has_creator: true, status: "active", monitor_count: 1, created_at: iso(86400 * 2), has_proxy: true, proxy: "http://***:***@HOST:PORT", proxy_status: "ok" },
    { id: 9, platform: "twitter", nickname: "Twitter 示例号", douyin_id: "x_creator_demo", sec_uid: "x_creator_demo", aweme_count: 48, follower_count: 3420, login_type: "browser", has_storage: true, has_creator: true, status: "active", monitor_count: 1, created_at: iso(86400 * 2), has_proxy: true, proxy: "http://***:***@HOST:PORT", proxy_status: "ok" },
  ];

  const platformAccount = (platform) => accounts.find((item) => item.platform === platform) || accounts[0];
  const platformName = { douyin: "抖音", xhs: "小红书", rednote: "海外小红书", kuaishou: "快手", shipinhao: "视频号", tiktok: "TikTok", bilibili: "哔哩哔哩", instagram: "Instagram", twitter: "Twitter" };
  const monitoredName = { douyin: "示例创作者", xhs: "城市生活研究所", rednote: "Studio Nomad", kuaishou: "影像日记", shipinhao: "本账号内容", tiktok: "GlobalCreator", bilibili: "科技美学示例 UP", instagram: "StudioVisuals", twitter: "IndieBuilder" };

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
    ].map((row, index) => ({ ...row, captured_at: iso(900 + index * 600) + "Z",
      source: {id:11, platform, name:`重点内容源 · ${monitoredName[platform]}`, target_kind:"creator", deleted:false} }));
  }

  function watches(platform) {
    return [{ id: 21, platform, kind: "user", title: "示例账号近期作品", sec_uid: `DEMO_${platform.toUpperCase()}_WATCH`, alias: "重点评论区", group_name: "互动观察", tags: ["高互动"], mode: "public", comment_count: 18, interval_seconds: 1800, last_scan_at: iso(1200), enabled: true, recent_works: 5, recent_days: 7, max_scrolls: 6 }];
  }

  function comments(platform) {
    return [
      { id: 301, watch_id: 21, platform, text: "这个内容很实用，已经收藏了。", user_nickname: "示例用户 1", like_count: 12, create_time: now - 1800, is_reply: false },
      { id: 302, watch_id: 21, platform, text: "期待下一期，也想看看完整流程。", user_nickname: "示例用户 2", like_count: 7, create_time: now - 3200, is_reply: false },
    ].map((row,index) => ({...row,aweme_id:"DEMO_WORK_001",captured_at:iso(900+index*600)+"Z",
      watch_source:{id:21,module:"comments",platform,name:"重点评论区 · 示例账号近期作品",kind:"user",deleted:false,unassigned:false}}));
  }

  function danmakuWatches(platform = "douyin") {
    return [{id:31,platform,kind:"video",title:platform === "bilibili" ? "示例投稿视频弹幕" : "示例短视频弹幕",alias:"内容反馈",mode:"public",enabled:true,
      group_name:"互动观察",tags:[],interval_seconds:1800,danmaku_count:2,last_scan_at:iso(900)}];
  }
  function demoWatchSource(module,platform,watchId) {
    const watch = (module === "comments" ? watches(platform) : danmakuWatches(platform)).find(w=>String(w.id)===String(watchId));
    return {id:Number(watchId),module,platform,deleted:!watch&&Number(watchId)>0,unassigned:Number(watchId)===0,kind:watch?.kind||"",
      name:watch?`${watch.alias} · ${watch.title}`:Number(watchId)>0?`已删除${module==="comments"?"评论":"弹幕"}任务 #${watchId}`:`未关联${module==="comments"?"评论":"弹幕"}监控`};
  }
  function watchRecordsPage(url, rows, module, platform) {
    const params=url.searchParams, watchId=params.get("watch_id");
    if(watchId!==null) rows=rows.filter(row=>String(row.watch_id||0)===watchId);
    const from=params.get("captured_from"),before=params.get("captured_before");
    if(from) rows=rows.filter(row=>row.captured_at&&Date.parse(row.captured_at)>=Date.parse(from));
    if(before) rows=rows.filter(row=>row.captured_at&&Date.parse(row.captured_at)<Date.parse(before));
    const sort=params.get("sort")||"captured_desc";
    rows.sort((a,b)=> sort.startsWith("captured_") ? (Date.parse(a.captured_at)-Date.parse(b.captured_at))*(sort==="captured_desc"?-1:1)
      : sort==="likes_desc" ? b.like_count-a.like_count : sort.startsWith("video_") ? (a.video_time_ms-b.video_time_ms)*(sort==="video_desc"?-1:1)
      : (a.create_time-b.create_time)*(sort==="oldest"?1:-1));
    if(params.get("paginate")!=="true") return rows;
    const total=rows.length,page=Number(params.get("page")||1),size=Number(params.get("page_size")||10);
    return {items:rows.slice((page-1)*size,page*size),total,page,page_size:size,pages:Math.max(1,Math.ceil(total/size)),
      watch_source:watchId!==null?demoWatchSource(module,platform,watchId):null};
  }

  const proxies = [
    {
      id: 41, label: "住宅代理 · 广东深圳", url: "http://***@cn-gd.demo.proxy:8080", url_full: "http://demo_cn01:pass@cn-gd.demo.proxy:8080",
      scheme: "http", host_port: "cn-gd.demo.proxy:8080", user_hint: "dem…cn01",
      note: "国内平台独享静态住宅（示例）", status: "ok", enabled: true, used_by: 1,
      bound_accounts: [{ id: 1, nickname: "创作者示例号", platform: "douyin" }],
      same_platform_shared: false,
      geo_checked: true, is_mainland: true, country: "CN", region: "广东", city: "深圳",
      geo_loc: "CN · 广东 · 深圳", exit_ip: "113.***.***.26", isp: "China Telecom",
    },
    {
      id: 42, label: "美区静态 · 洛杉矶", url: "socks5://***@us-la.demo.proxy:1080", url_full: "socks5://zone_us_la02:pass@us-la.demo.proxy:1080",
      scheme: "socks5", host_port: "us-la.demo.proxy:1080", user_hint: "zon…la02",
      note: "TikTok / X / IG 海外专用（示例）", status: "ok", enabled: true, used_by: 0,
      bound_accounts: [],
      same_platform_shared: false,
      geo_checked: true, is_mainland: false, country: "US", region: "California", city: "Los Angeles",
      geo_loc: "US · California · Los Angeles", exit_ip: "75.***.***.84", isp: "Comcast Cable",
    },
  ];

  function publishTasks(platform) {
    return [{ id: 51, platform, account_id: platformAccount(platform).id, title: "夏日城市漫游", media_type: "images", media_count: 6, source_platform: "", scheduled_at: null, status: "pending", error: "", result_url: "" }];
  }

  function taskQueue(url) {
    const params = url.searchParams;
    const platform = params.get("platform") || "all";
    const platforms = platform === "all" ? Object.keys(platformName) : [platform];
    const tasks = platforms.flatMap(pf => publishTasks(pf).map(task => ({
      ...task, queue_type: "publish", queue_label: "发布", state: "pending",
      account_name: platformAccount(pf).nickname, source_tab: "publish", created_at: iso(1800),
    })));
    const state = params.get("state") || "active", type = params.get("queue_type"), query = (params.get("q") || "").toLowerCase();
    const matching = tasks.filter(task => (!type || task.queue_type === type)
      && `${task.queue_label} ${task.title} ${task.account_name} ${task.platform} ${task.status}`.toLowerCase().includes(query));
    const summary = { total: matching.length, active: matching.length, pending: matching.length, running: 0, blocked: 0, failed: 0, completed: 0 };
    const rows = matching.filter(task => ["all", "active"].includes(state) || task.state === state);
    const size = Math.max(1, Number(params.get("page_size")) || 20);
    const pages = Math.max(1, Math.ceil(rows.length / size));
    const page = Math.max(1, Math.min(pages, Number(params.get("page")) || 1));
    return { items: rows.slice((page - 1) * size, page * size), summary, total: rows.length, page, pages, page_size: size };
  }

  function commentRules(platform) {
    return [{ id: 61, platform, name: "示例自动回复规则", mode: "auto_reply", target_kind: "self", account_id: platformAccount(platform).id, templates: ["谢谢支持，欢迎常来。"], use_ai: false, require_review: true, reply_filter: "", skip_keywords: "", daily_cap: 20, min_gap_seconds: 90, max_per_run: 5, interval_seconds: 1800, enabled: false, last_run_at: iso(86400), last_error: "" }];
  }

  function commentTasks(platform) {
    return [{ id: 71, platform, content: "谢谢支持，欢迎常来看看。", aweme_id: "DEMO_WORK_001", target_comment_id: "DEMO_COMMENT", target_nick: "示例用户", scheduled_at: iso(-1800), method: "browser", status: "draft", error: "" }];
  }

  const shareHistory = [
    { id: 81, created_at: iso(1200), platform: "douyin", title: "示例作品 · 城市漫游", author: "示例创作者", item_id: "DEMO_001", media_type: "video", create_time: now - 1800, like_count: 9829, comment_count: 126, duration: 42, quality: "1080P", status: "done", source_url: "https://v.douyin.com/DEMO/", output_dir: "data/media/demo", files: [{ role: "media", path: "data/media/demo/demo.mp4", size: 18_600_000 }] },
    { id: 82, created_at: iso(7200), platform: "xhs", title: "示例笔记 · 周末记录", author: "示例创作者", item_id: "DEMO_002", media_type: "images", create_time: now - 7200, like_count: 2840, comment_count: 64, duration: 0, media_count: 6, status: "done", source_url: "https://www.xiaohongshu.com/explore/DEMO", output_dir: "data/media/demo", files: [{ role: "media", path: "data/media/demo/01.jpg", size: 2_100_000 }] },
  ];

  function series() {
    const days = [];
    for (let index = 6; index >= 0; index -= 1) days.push(new Date(Date.now() - index * 86400000).toISOString().slice(0, 10));
    return { days, contents: [3, 5, 4, 8, 6, 10, 7], comments: [5, 8, 6, 12, 9, 15, 11] };
  }

  function transportMatrix() {
    const row = (platform, operation, label, setting, api, browser, note = "") => {
      const configured = setting ? engineValues[setting] : (api ? "api" : browser ? "browser" : "unavailable");
      let effective = configured;
      if (configured === "hybrid" && !(api && browser)) effective = api ? "api" : browser ? "browser" : "unavailable";
      if (configured === "api" && !api) effective = "unavailable";
      return { platform, operation, label, setting, api, browser, note,
        configured_mode: configured, effective_mode: effective,
        fallback: effective === "hybrid" ? "browser_on_confirmed_failure" : "none",
        opens_browser: ["browser", "hybrid"].includes(effective),
        reason: effective === "unavailable" ? note : "" };
    };
    return { settings: { ...engineValues }, rows: [
      row("douyin", "keyword_collection", "关键词采集", "douyin_read_mode", true, true),
      row("douyin", "public_monitor", "公开作品/评论/弹幕监控", "douyin_read_mode", true, true),
      row("douyin", "account_works", "本账号作品同步", "douyin_read_mode", true, true),
      row("douyin", "own_work_comments", "本账号作品评论抓取", "douyin_read_mode", true, true),
      row("douyin", "following_list", "关注列表同步", "douyin_read_mode", true, true),
      row("douyin", "account_profile", "手动刷新账号资料", "douyin_profile_mode", true, true),
      row("douyin", "followers_list", "粉丝列表同步", "douyin_followers_mode", true, true, "直连接口受账号风控影响时，混合模式会回退浏览器"),
      row("douyin", "dm_sync", "私信会话/历史同步", "douyin_dm_sync_mode", true, true, "API 使用 imapi protobuf 初始化会话并按需读取历史"),
      row("douyin", "creator_danmaku", "本账号创作中心弹幕", "douyin_creator_danmaku_mode", true, true, "API 按本账号作品 ID 读取弹幕；浏览器模式保留创作中心流程"),
      row("douyin", "comment_write", "评论/回复发送", "douyin_write_mode", true, true),
      row("douyin", "follow_write", "关注/取关", "douyin_write_mode", true, true),
      row("douyin", "dm_send", "已有会话私信发送", "douyin_write_mode", true, true),
      row("douyin", "publish", "发布作品", "douyin_publish_mode", false, true, "上传鉴权和提交签名仍依赖创作中心页面"),
      row("xhs", "public_monitor", "作品/关键词/公开评论读取", "xhs_read_mode", true, true),
      row("xhs", "comment_write", "评论/回复发送", "xhs_comment_write_mode", true, true),
      row("xhs", "publish", "发布作品", "xhs_publish_mode", true, true),
      row("rednote", "public_monitor", "作品/关键词/收藏/只读评论读取", "rednote_read_mode", true, true),
      row("rednote", "account_works", "本账号作品同步与可见性/删除管理", "rednote_read_mode", true, true),
      row("rednote", "publish", "图文与视频笔记发布", "rednote_publish_mode", true, true, "支持 creator.rednote.com 页面流或实验性 XYS + b1 纯协议直发"),
      row("rednote", "comment_write", "自动评论/回复规则", "", false, false, "海外版暂未开放自动评论与互动写入"),
      row("rednote", "dm_sync", "私信会话同步与发送", "", false, false, "海外版无国内 RWP 私信长连接与网页私信接口"),
      row("rednote", "follows", "关注/粉丝列表同步", "", false, false, "海外版暂未验证独立关注/粉丝列表读取通道"),
      row("kuaishou", "account_management", "作品/评论/关注/写入", "", false, true),
      row("shipinhao", "account_management", "作品/评论/发布", "", false, true),
      row("bilibili", "public_monitor", "搜索/空间/评论/XML弹幕读取", "bilibili_read_mode", true, true),
      row("bilibili", "account_works", "创作中心稿件/关注/粉丝读取", "bilibili_read_mode", true, true),
      row("bilibili", "dm_sync", "私信会话与 Broadcast 实时流", "bilibili_read_mode", true, true),
      row("bilibili", "comment_write", "评论/回复/点赞/关注/可见性", "bilibili_write_mode", true, true),
      row("bilibili", "dm_send", "WBI 签名私信发送", "bilibili_write_mode", true, true),
      row("bilibili", "publish", "创作中心视频投稿与图文动态", "", false, true),
      row("instagram", "public_monitor", "主页/Reels/话题搜索/评论读取", "instagram_read_mode", true, true),
      row("instagram", "account_works", "本账号作品/关注/粉丝读取", "instagram_read_mode", true, true),
      row("instagram", "dm_sync", "Direct v2 私信会话同步", "instagram_read_mode", true, true),
      row("instagram", "comment_write", "评论/回复/点赞/关注", "instagram_write_mode", true, true),
      row("instagram", "dm_send", "Direct v2 私信发送", "instagram_write_mode", true, true),
      row("instagram", "publish", "单图/轮播/Reels/Story 发布", "instagram_write_mode", true, true),
      row("twitter", "public_monitor", "推文详情/时间线/搜索/回复读取", "twitter_read_mode", true, true),
      row("twitter", "account_works", "本账号推文/关注/粉丝读取", "twitter_read_mode", true, true),
      row("twitter", "dm_sync", "私信收件箱与历史消息同步", "twitter_read_mode", true, true),
      row("twitter", "comment_write", "回复/点赞/转推/引用/关注", "twitter_write_mode", false, true),
      row("twitter", "dm_send", "私信发送", "twitter_write_mode", false, true),
      row("twitter", "publish", "纯文本/最多4图/单视频推文与首评串推", "twitter_write_mode", false, true),
    ], accounts: accounts.map(account => ({
      account_id: account.id, platform: account.platform, nickname: account.nickname,
      profile_isolated: true, credential_isolated: true, api_session_isolated: true,
      network_isolated: !!account.has_proxy, network_scope: account.has_proxy ? "专属代理" : "未配代理",
      api_environment_aligned: true, environment_id: `DEMO-${account.id}`,
      warnings: account.has_proxy ? [] : ["风险提醒：未绑定代理（当前走本机直连，海外或多号同平台建议配置独立代理防关联）"],
    })) };
  }

  function getData(url) {
    const path = url.pathname;
    if (path === "/api/agent/settings") return { display_name:"", models:[], enabled:false, mcp_enabled:false, provider:"openai_compatible", base_url:"", model:"", api_key_set:false, tool_calling:true, output_token_parameter:"max_tokens", timeout_seconds:45, max_rounds:4, max_output_tokens:2048 };
    if (path === "/api/agent/channels") return { active_channel_id:"default", channels:[{id:"default", display_name:"", models:[], enabled:false, mcp_enabled:false, provider:"openai_compatible", base_url:"", model:"", api_key_set:false, api_path_mode:"auto", tool_calling:true, output_token_parameter:"max_tokens", timeout_seconds:45, max_rounds:4, max_output_tokens:2048}] };
    if (path === "/api/agent/runs") return [];
    if (path === "/api/agent/context") return { accounts:{accounts:[]}, tasks:{tasks:[]}, metrics:{accounts:0, task_status_counts:{}} };
    const platform = url.searchParams.get("platform") || "douyin";
    if (path === "/api/overview/summary") return {
      platform, accounts: accounts.filter(item => item.platform === platform).length,
      monitors: platform === "shipinhao" ? 0 : Number(monitor(platform).enabled),
      downloaded: platform === "shipinhao" ? 0 : contents(platform).filter(item => item.download_status === "done").length,
      comments: platform === "shipinhao" ? 0 : comments(platform).length,
    };
    if (path === "/api/task-queue") return taskQueue(url);
    if (path === "/api/accounts") return url.searchParams.has("platform") ? accounts.filter((item) => item.platform === platform) : accounts;
    if (path === "/api/proxies/options") return proxies.map((item) => ({
      id: item.id, url: item.url_full || item.url, label: item.label, raw_label: item.label,
      status: item.status, used_by: item.used_by,
      used_by_platform: item.is_mainland ? { douyin: 1 } : {},
      masked: item.url, enabled: item.enabled, note: item.note || "",
      exit_ip: item.exit_ip || "", country: item.country || "", region: item.region || "", city: item.city || "", isp: item.isp || "",
      geo_checked: item.geo_checked, is_mainland: item.is_mainland, geo_loc: item.geo_loc,
    }));
    if (path === "/api/proxies") return proxies;
    if (path === "/api/monitors") return platform === "shipinhao" ? [] : [monitor(platform)];
    if (path === "/api/contents") {
      let rows = platform === "shipinhao" ? [] : contents(platform);
      if (url.searchParams.get("target_id")) rows = rows.filter(row => String(row.target_id) === url.searchParams.get("target_id"));
      if (url.searchParams.get("captured_from")) rows = rows.filter(row => Date.parse(row.captured_at) >= Date.parse(url.searchParams.get("captured_from")));
      if (url.searchParams.get("captured_before")) rows = rows.filter(row => Date.parse(row.captured_at) < Date.parse(url.searchParams.get("captured_before")));
      if (url.searchParams.get("sort") === "captured_asc") rows.reverse();
      return rows;
    }
    if (path === "/api/comment-watches") return platform === "shipinhao" ? [] : watches(platform);
    if (path === "/api/comments") return watchRecordsPage(url, platform === "shipinhao" ? [] : comments(platform), "comments", platform);
    if (path === "/api/danmaku-watches") return ["douyin", "bilibili"].includes(platform) ? danmakuWatches(platform) : [];
    if (path === "/api/danmaku") return watchRecordsPage(url, ["douyin", "bilibili"].includes(platform) ? [
      {id:401,watch_id:31,aweme_id:"DEMO_WORK_001",text:"前方高能，这个镜头很精彩",user_nickname:"示例用户",video_time_ms:12300,source:"public",create_time:now-1800},
      {id:402,watch_id:31,aweme_id:"DEMO_WORK_001",text:"学到了新的剪辑思路",user_nickname:"示例观众",video_time_ms:26100,source:platform === "bilibili" ? "public" : "creator",create_time:now-3200},
    ].map((row,index)=>({...row,platform,captured_at:iso(900+index*600)+"Z",watch_source:demoWatchSource("danmaku",platform,31)})) : [], "danmaku", platform);
    if (path === "/api/stats/series") return series();
    if (path === "/api/publish") return publishTasks(platform);
    if (path === "/api/publish/published") return [];
    if (path === "/api/comment-rules") return ["shipinhao", "rednote", "tiktok"].includes(platform) ? [] : commentRules(platform);
    if (path === "/api/comment-tasks") return ["shipinhao", "rednote", "tiktok"].includes(platform) ? [] : commentTasks(platform);
    if (path === "/api/share-download/history") return shareHistory;
    if (/^\/api\/share-download\/history\/\d+\/preview$/.test(path)) return { media_type: "video", cover_url: "", medias: [] };
    if (path === "/api/notifications") return [{ id: 91, name: "演示通知渠道", type: "bark", enabled: true, config: {} }];
    if (path === "/api/settings") return { download_dir: "data/media", video_quality: "highest", ai_enabled: false, ai_base_url: "", ai_model: "", ai_temperature: "0.9", ai_prompt: "", ai_api_key_set: false };
    if (path === "/api/channels/protocol/status" || path === "/api/channels/protocol/config") return channelsProtoPayload();
    if (path === "/api/channels/protocol/discover") return { endpoints: [], wechat_processes: [], bridge: channelsProtoPayload().bridge };
    if (path === "/api/settings/engine") return {values: {...engineValues}, defaults: engineDefaults, saved_fields: [], apply_scope: "next_operation"};
    if (path === "/api/settings/transport-matrix") return transportMatrix();
    if (path === "/api/hub/summary") return { works: 3, following: 20, fans: 168, dm: 4 };
    if (path === "/api/account-works") return contents(platform).map((item, index) => ({ ...item, id: 201 + index, item_id: item.aweme_id, play_count: 6800 - index * 1200, comment_count: 32 - index * 7, status: "正常" }));
    if (/^\/api\/account-works\/\d+\/comments$/.test(path)) return comments(platform).map((item) => ({ ...item, user_nickname: item.user_nickname }));
    if (path === "/api/follows") return [{ id: 221, nickname: "示例好友", signature: "记录生活与创作", is_mutual: true, is_following: true, uid: "DEMO_UID", sec_uid: "DEMO_SEC_UID", avatar: "" }];
    if (path === "/api/dm/conversations") return [{ conv_id: "DEMO_CONVERSATION", peer_nickname: "示例联系人", peer_avatar: "", last_text: "最近一条消息预览", unread_count: 2, peer_uid: "DEMO_UID", peer_sec_uid: "DEMO_SEC_UID" }];
    if (path === "/api/dm/messages") return [{ id: 231, direction: "in", text: "你好，想了解一下这个功能。", create_time: now - 600 }, { id: 232, direction: "out", text: "你好，这里是在线演示界面。", create_time: now - 420 }];
    if (/^\/api\/account-stats\/\d+$/.test(path)) return { account: { follower_count: 1280, aweme_count: 56 }, fans_delta: 86, trend: [1120, 1148, 1175, 1204, 1232, 1260, 1280].map((follower_count, index) => ({ follower_count, date: index })), works: contents(platform).map((item) => ({ desc: item.desc, play_count: 6800, like_count: item.like_count, comment_count: 28, status: "正常" })) };
    if (path === "/api/dm/inbox") {
      const items = accounts.slice(0, 4).map((acc, idx) => ({
        id: 500 + idx, platform: acc.platform, account_id: acc.id, account_name: acc.nickname,
        conv_id: "DEMO_CONV_" + acc.id, peer_uid: "PEER_" + idx, peer_nickname: "合作咨询用户 " + (idx + 1),
        last_text: "你好，看到你的近期作品数据不错，想沟通商务推广档期。", last_time: now - idx * 900,
        unread_count: idx < 2 ? 2 - idx : 0, group_chat: false,
      }));
      return { items, unread: items.reduce((s, x) => s + x.unread_count, 0) };
    }
    if (path === "/api/matrix/overview") {
      const pfs = ["douyin", "xhs", "rednote", "kuaishou", "shipinhao", "tiktok", "bilibili", "instagram", "twitter"];
      const strip = pfs.map((pf, idx) => {
        const pfAccs = accounts.filter(a => a.platform === pf);
        return {
          platform: pf, accounts_total: pfAccs.length, accounts_active: pfAccs.length,
          accounts_invalid: 0, accounts_risk: 0, accounts_proxy_issue: 0, accounts_shared_proxy: 0, accounts_no_proxy: 0,
          followers_total: pfAccs.reduce((s, a) => s + (a.follower_count || 1280), 0),
          fans_delta: pfAccs.length ? 48 - idx * 4 : 0,
          monitors_enabled: pf === "shipinhao" ? 0 : 1,
          today_contents: pfAccs.length ? 6 - (idx % 3) : 0,
          today_comments: pfAccs.length ? 18 - (idx % 5) : 0,
          unread_dm: idx < 2 ? 2 - idx : 0, active_tasks: idx === 0 ? 1 : 0, failed_tasks: 0, blocked_tasks: 0,
        };
      });
      const s7 = series();
      const hot = ["douyin", "xhs", "bilibili", "instagram"].flatMap(pf => contents(pf).slice(0, 2).map(c => ({
        uid: `monitor:${pf}-${c.id}`, source_kind: "monitor", id: c.id, platform: pf,
        aweme_id: c.aweme_id, desc: c.desc, cover_url: c.cover_url || "", media_type: c.media_type,
        like_count: c.like_count, comment_count: c.comment_count || 26, author_name: "示例监控源",
        source_label: "作品监控", download_status: c.download_status, local_ready: c.download_status === "done",
        create_time: c.create_time, captured_at: c.captured_at,
      })));
      return {
        scope: url.searchParams.get("platform") || "all",
        platform_strip: strip,
        alerts: { invalid_accounts: 0, proxy_issues: 0, shared_proxy_accounts: 0, risk_accounts: 0, no_proxy_accounts: 0, unread_dm: 3, failed_tasks: 0, blocked_tasks: 0, active_tasks: 1, pending_reviews: 0, abnormal_works: 0 },
        totals: { accounts: accounts.length, active_accounts: accounts.length, followers: accounts.length * 1680, fans_delta: 186, monitors: 8, contents_total: 42, downloaded_total: 28, comments_total: 310, own_works_total: 24, today_contents: 19, today_comments: 64 },
        series_7d: { days: s7.days, contents: s7.contents, comments: s7.comments, publishes: [1, 2, 1, 3, 2, 4, 3] },
        hot_contents: hot,
        schedule_items: publishTasks("douyin").slice(0, 3).map(t => ({ ...t, account_name: "主IP·抖音号", group_name: "户外露营主IP" })),
      };
    }
    if (path === "/api/matrix/accounts") {
      const items = accounts.map((a, idx) => ({
        ...a, alias: idx < 3 ? `主IP-${a.nickname}` : "", display_name: idx < 3 ? `主IP-${a.nickname}` : a.nickname,
        group_name: idx < 4 ? "户外露营主IP" : "采集工具号池", tags: idx < 4 ? ["日更", "主力"] : ["采集池"],
        account_role: idx < 4 ? "publish" : "crawler", pinned: idx < 2, sort_order: idx,
        health_state: "healthy", follower_count: a.follower_count || 1680 + idx * 420, fans_delta: 36 - idx * 3,
        aweme_count: a.aweme_count || 28, unread_dm: idx < 2 ? 2 - idx : 0, monitor_count: 1,
        shared_proxy: false, shared_proxy_count: a.has_proxy ? 1 : 0, cross_platform_shared: false, proxy_warnings: [],
        ip_matches_exit: true, browser_open: idx === 0, worker_running: false, profile_isolated: true,
        tasks: { total: 0, pending: 0, running: 0, blocked: 0, failed: 0 },
      }));
      return {
        items,
        groups: [
          { id: 1, name: "户外露营主IP", color: "blue", description: "抖音+小红书+B站跨平台同步矩阵", preferred_country: "CN", sort_order: 0 },
          { id: 2, name: "采集工具号池", color: "amber", description: "专供关键词与评论监控轮询", preferred_country: "CN", sort_order: 1 },
        ],
        all_tags: ["主力", "日更", "采集池"],
      };
    }
    if (path === "/api/matrix/hub-analytics") {
      const ranked = accounts.map((a, idx) => ({
        id: a.id, platform: a.platform, display_name: a.nickname, nickname: a.nickname, alias: "",
        avatar: a.avatar || "", douyin_id: a.douyin_id || "", group_name: idx < 4 ? "户外露营主IP" : "采集工具号池",
        account_role: idx < 4 ? "publish" : "crawler", pinned: idx < 2, status: "active",
        follower_count: 2400 + idx * 510, fans_delta: 42 - idx * 4, aweme_count: 18 + idx * 2,
        works_synced: 6, recent_7d_works: 3, plays_total: 18600 + idx * 2100, likes_total: 1420 + idx * 180,
        comments_total: 168, zero_play_count: 0, abnormal_count: 0, unread_dm: idx < 2 ? 1 : 0,
      }));
      const worksItems = contents("douyin").map((c, idx) => ({
        id: 600 + idx, platform: idx % 2 === 0 ? "douyin" : "xhs", account_id: accounts[0]?.id || 1,
        account_name: accounts[0]?.nickname || "示例账号", account_avatar: "", group_name: "户外露营主IP",
        item_id: c.aweme_id, desc: c.desc, media_type: c.media_type, cover_url: c.cover_url || "",
        create_time: c.create_time, play_count: 9200 - idx * 1100, like_count: c.like_count, comment_count: 34, collect_count: 19,
        status: "正常", zero_play_alerted: false, status_alerted: "",
      }));
      return {
        kpi: { accounts_count: ranked.length, followers_total: ranked.reduce((s, x) => s + x.follower_count, 0), fans_delta_total: 168, works_total: 86, plays_total: 128000, likes_total: 9600, comments_total: 840, abnormal_works_total: 0, unread_dm_total: 2 },
        accounts: ranked,
        works: { items: worksItems, total: worksItems.length, page: 1, page_size: 24, pages: 1 },
      };
    }
    if (path === "/api/matrix/media-hub") {
      const items = ["douyin", "xhs", "bilibili"].flatMap(pf => contents(pf).map(c => ({
        uid: `monitor:${c.id}-${pf}`, source_kind: "monitor", id: c.id, platform: pf, aweme_id: c.aweme_id,
        desc: c.desc, author_name: "示例创作者", group_name: "行业精选", source_label: "作品监控",
        media_type: c.media_type, cover_url: c.cover_url || "", duration: 42,
        like_count: c.like_count, comment_count: 28, play_count: 0, download_status: c.download_status,
        local_ready: c.download_status === "done", local_path: c.local_path || "", create_time: c.create_time, captured_at: c.captured_at,
      })));
      return { items, total: items.length, page: 1, page_size: 24, pages: 1, source_counts: { all: items.length, monitor: items.length, collection: 0, own: 0, share: 0 } };
    }
    if (path === "/api/matrix/media-hub/detail") {
      const base = contents("douyin")[0] || { id: 1, desc: "示例作品详情文案", cover_url: "", like_count: 520 };
      return {
        source_kind: "monitor", id: base.id, platform: "douyin", aweme_id: "DEMO_WORK_001",
        desc: base.desc, cover_url: base.cover_url || "", media_type: "images",
        like_count: base.like_count, comment_count: 12, local_path: "data/media/demo", local_file_paths: ["data/media/demo.jpg"],
        local_medias: [], remote_medias: [], source_url: "", can_repost: true,
        comments: comments("douyin").slice(0, 5),
      };
    }
    if (/^\/api\/contents\/\d+\/media$/.test(path)) return { media_type: "images", cover_url: "", medias: [] };
    if (path === "/api/diagnostics/logs") {
      const lvl = (url.searchParams.get("level") || "warn").toLowerCase();
      const q = (url.searchParams.get("q") || "").trim().toLowerCase();
      const allEntries = [
        { timestamp: "2026-04-20 17:42:18.312", level: "WARNING", logger: "risk", req_id: "", message: "账号 #2 触发平台频控冷却保护，已暂停自动写操作 30 分钟 (xsec_token=[REDACTED])", exception: "" },
        { timestamp: "2026-04-20 17:38:05.104", level: "INFO", logger: "publish.xhs", req_id: "8f31c2a9", message: "发布任务 #18(xhs, 账号#1) 已完成", exception: "" },
        { timestamp: "2026-04-20 17:30:11.882", level: "INFO", logger: "engine", req_id: "", message: "小红书监控扫描(target=3, kind=creator): 候选=12 新增=2 详情=2 过滤=0", exception: "" },
        { timestamp: "2026-04-20 17:20:01.045", level: "INFO", logger: "startup", req_id: "", message: "服务初始化就绪 (backend=local | runtimes=1 | persisted=risk,engine) | logs=./logs", exception: "" },
      ];
      const filtered = allEntries.filter(item => {
        if (lvl === "error" && item.level !== "ERROR") return false;
        if (lvl === "warn" && !["WARNING", "ERROR"].includes(item.level)) return false;
        if (q && !`${item.logger} ${item.message}`.toLowerCase().includes(q)) return false;
        return true;
      });
      return {
        log_dir: "./logs",
        level: demoDiagLevel,
        total_bytes: 48640,
        files: [{ name: "app.log", size_bytes: 42496 }, { name: "error.log", size_bytes: 6144 }],
        memory_counts: { total: allEntries.length, warnings: 1, errors: 0 },
        source: url.searchParams.get("source") || "memory",
        entries: filtered,
      };
    }
    if (path === "/api/diagnostics/summary") {
      return { app_version: "demo", run_mode: "preview", logging: { level: demoDiagLevel, total_bytes: 48640 } };
    }
    return [];
  }

  function actionData(path) {
    if (path === "/api/channels/protocol/probe") return { ok: false, ready: false, configured: false, error: "在线演示模式不连接本机微信进程" };
    if (path === "/api/channels/protocol/proxy/start") {
      channelsProto.ca_trusted = true;
      channelsProto.proxy_running = true;
      channelsProto.proxy_enabled = true;
      channelsProto.connection_mode = "proxy";
      return { ok: true, cert_installed_now: false, ...getData(new URL("/api/channels/protocol/status", window.location.href)) };
    }
    if (path === "/api/channels/protocol/proxy/stop") {
      channelsProto.proxy_running = false;
      channelsProto.proxy_enabled = false;
      channelsProto.connection_mode = "none";
      return { ok: true, ...getData(new URL("/api/channels/protocol/status", window.location.href)) };
    }
    if (path === "/api/channels/protocol/wechat/open") {
      return { ok: true, message: "在线演示模式：真实桌面环境会自动唤起电脑微信", proxy: channelsProtoPayload().proxy };
    }
    if (path === "/api/channels/protocol/wechat/restart") {
      return { ok: true, message: "在线演示模式：真实桌面环境会自动重启电脑微信以应用代理", proxy: channelsProtoPayload().proxy };
    }
    if (path === "/api/channels/protocol/proxy/cert/install") {
      channelsProto.ca_trusted = true;
      return { ok: true, message: "演示模式已模拟信任本地 CA", ...getData(new URL("/api/channels/protocol/status", window.location.href)) };
    }
    if (path === "/api/channels/protocol/proxy/cert/uninstall") {
      channelsProto.ca_trusted = false;
      return { ok: true, message: "演示模式已移除根证书信任", ...getData(new URL("/api/channels/protocol/status", window.location.href)) };
    }
    if (path === "/api/channels/protocol/config" || path === "/api/channels/protocol/bridge/start" || path === "/api/channels/protocol/bridge/stop") {
      return getData(new URL("/api/channels/protocol/status", window.location.href));
    }
    if (path === "/api/share-download/links") return { count: 1, links: [{ platform: "douyin", host: "v.douyin.com", url: "https://v.douyin.com/DEMO/" }] };
    if (path === "/api/share-download") return { ok: true, results: [{ ok: true, platform: "douyin", title: "示例作品", author: "示例创作者", media_type: "video", media_count: 1, output_dir: "data/media/demo", files: [] }] };
    if (/\/run-now$/.test(path)) return { ok: true, created: 1, candidates: 1, review: true, new: 1, new_comments: 1 };
    if (/\/sync$/.test(path)) return { ok: true, fetched: 3, added: 1, source: "api" };
    if (/\/test$/.test(path)) return { ok: true, detail: "在线演示" };
    return { ok: true, deleted: 1, files_removed: 0, approved: 1, fetched: 1, added: 1, new: 1 };
  }

  window.fetch = async function demoFetch(input, init) {
    const raw = typeof input === "string" ? input : input && input.url;
    const url = new URL(raw || "", window.location.href);
    if (!url.pathname.startsWith("/api/")) return realFetch(input, init);
    if (url.pathname.startsWith("/api/agent/") && (init?.method || "GET").toUpperCase() !== "GET") {
      return new Response(JSON.stringify({detail:"静态演示不连接模型、不保存密钥。请在本地运行 CreatorHub 后使用 AI 助手。"}), {status:403, headers:{"Content-Type":"application/json"}});
    }
    await new Promise((resolve) => setTimeout(resolve, 90));
    const method = String((init && init.method) || "GET").toUpperCase();
    if (method === "GET" && url.pathname === "/api/reports/share-download-history.xlsx") {
      return new Response("CreatorHub demo share-download history export", {
        status: 200,
        headers: {
          "Content-Type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
          "Content-Disposition": 'attachment; filename="creatorhub_share_download_history_demo.xlsx"',
        },
      });
    }
    if (method === "GET" && url.pathname === "/api/diagnostics/export.zip") {
      return new Response("CreatorHub sanitized diagnostic bundle (online preview placeholder)", {
        status: 200,
        headers: {
          "Content-Type": "application/zip",
          "Content-Disposition": 'attachment; filename="creatorhub-diagnostics-demo.zip"',
        },
      });
    }
    if (method === "PUT" && url.pathname === "/api/diagnostics/log-level") {
      const body = JSON.parse(init.body || "{}");
      if (["info", "debug", "warning"].includes(body.level)) demoDiagLevel = body.level;
      return new Response(JSON.stringify({ ok: true, level: demoDiagLevel, note: `在线演示已切换为 ${demoDiagLevel.toUpperCase()} 级别` }), { status: 200, headers: { "Content-Type": "application/json; charset=utf-8" } });
    }
    if (method === "POST" && url.pathname === "/api/diagnostics/report-text") {
      const body = JSON.parse(init.body || "{}");
      const text = `### CreatorHub 问题排查摘要 (在线演示)\n- **版本/模式**: \`demo\` · 日志级别 \`${demoDiagLevel.toUpperCase()}\`\n` + (body.note ? `- **用户问题描述**: ${body.note}\n` : "") + "- **近期告警**: 账号 #2 触发频控冷却保护 (敏感凭据已自动脱敏)\n";
      return new Response(JSON.stringify({ ok: true, text }), { status: 200, headers: { "Content-Type": "application/json; charset=utf-8" } });
    }
    if (method === "POST" && (url.pathname === "/api/diagnostics/open-dir" || url.pathname === "/api/diagnostics/clear-logs")) {
      return new Response(JSON.stringify({ ok: true, cleared: 2, log_dir: "./logs" }), { status: 200, headers: { "Content-Type": "application/json; charset=utf-8" } });
    }
    if (method === "PUT" && url.pathname === "/api/settings/engine") Object.assign(engineValues, JSON.parse(init.body));
    if (url.pathname === "/api/channels/protocol/config") {
      if (method === "PUT") {
        const body = JSON.parse(init.body || "{}");
        if (body.channels_read_mode) engineValues.channels_read_mode = body.channels_read_mode;
        if (body.connection_mode) channelsProto.connection_mode = body.connection_mode;
        if (body.connection_mode === "proxy") channelsProto.proxy_enabled = true;
        else if (body.connection_mode) channelsProto.proxy_enabled = false;
        if ("channels_proxy_enabled" in body) channelsProto.proxy_enabled = Boolean(body.channels_proxy_enabled);
        if ("channels_proxy_port" in body && body.channels_proxy_port) channelsProto.proxy_port = Number(body.channels_proxy_port);
        if ("channels_cdp_endpoint" in body) channelsProto.cdp_endpoint = body.channels_cdp_endpoint || "";
        if ("channels_cdp_endpoint_file" in body) channelsProto.cdp_endpoint_file = body.channels_cdp_endpoint_file || "";
      } else if (method === "DELETE") {
        channelsProto.connection_mode = "none";
        channelsProto.proxy_enabled = false;
        channelsProto.proxy_port = 2023;
        channelsProto.cdp_endpoint = "";
        channelsProto.cdp_endpoint_file = "";
      }
      return new Response(JSON.stringify(channelsProtoPayload()), { status: 200, headers: { "Content-Type": "application/json; charset=utf-8" } });
    }
    const data = method === "GET" || url.pathname === "/api/settings/engine" ? getData(url) : actionData(url.pathname);
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
