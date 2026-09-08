// Browser acceptance only. Appended to the isolated demo, never shipped live.
(() => {
  const demo = window.fetch;
  const jobs = [];
  const edits = {};
  if (new URL(location.href).searchParams.has("engine-read-fail")) document.documentElement.dataset.fixtureEngineRead = "fail";
  // Patchright evaluates in an isolated world; DOM events exercise page-world
  // handlers without exposing application globals or real service connections.
  document.addEventListener("fixture-engine-load", async () => {
    await window.CreatorHubEngineSettings.load();
    document.documentElement.dataset.fixtureEngineLoadDone = String(Number(document.documentElement.dataset.fixtureEngineLoadDone || 0) + 1);
  });
  document.addEventListener("fixture-engine-save", () => window.CreatorHubEngineSettings.save());
  document.addEventListener("fixture-record-refresh", async () => {
    await Promise.all([refreshMonitors(), refreshContents(true)]);
    document.documentElement.dataset.fixtureRecordReady = "true";
  });
  document.addEventListener("fixture-engine-state", () => {
    document.documentElement.dataset.fixtureEngineDirty = String(window.CreatorHubEngineSettings.isDirty());
  });
  const json = (data, status = 200) => new Response(JSON.stringify(data), {status, headers: {"Content-Type": "application/json"}});
  const job = body => ({id: jobs.length + 1, platform: "douyin", status: "pending", created_at: "2026-09-08T01:00:00Z",
    content_count: 0, comment_count: 0, error_count: 0, planned_content_count: 20, ...body});
  window.fetch = async (input, init = {}) => {
    const url = new URL(typeof input === "string" ? input : input.url, location.href);
    const method = (init.method || "GET").toUpperCase(), flags = document.documentElement.dataset;
    if (flags.fixtureRecords && method === "GET" && ["/api/monitors", "/api/contents"].includes(url.pathname)) {
      const platform = url.searchParams.get("platform") || "douyin";
      const targets = [
        {id:11, platform, target_kind:"keyword", keyword:"城市漫游", alias:"品牌 A 城市选题", group_name:"品牌 A", tags:["日常"],
          enabled:true, interval_seconds:3600, content_count:2, account_id:platform === "xhs" ? 2 : 1},
        {id:12, platform, target_kind:"keyword", keyword:"旅行摄影", alias:"品牌 B 长任务名称验证移动端自动换行并保留完整来源", group_name:"品牌 B", tags:[],
          enabled:true, interval_seconds:3600, content_count:1, account_id:platform === "xhs" ? 2 : 1},
      ];
      if (url.pathname === "/api/monitors") return json(targets);
      const taskSource = id => {
        const t = targets.find(item => item.id === id);
        return t ? {id,platform,name:`${t.alias} · #${t.keyword}`,target_kind:t.target_kind,deleted:false}
          : {id,platform,name:`已删除任务 #${id}`,target_kind:"",deleted:true};
      };
      flags.fixtureRecordsQuery = url.search;
      let rows = [
        {id:101,target_id:11,desc:"任务 A 本次抓取的作品",captured_at:"2026-09-08T08:10:00Z"},
        {id:102,target_id:12,desc:"任务 B 独立记录同一作品",captured_at:"2026-09-08T09:00:00Z"},
        {id:103,target_id:99,desc:"已删除任务的历史记录",captured_at:null},
        {id:104,target_id:11,desc:"任务 A 昨天抓取的作品",captured_at:"2026-09-07T08:00:00Z"},
      ].map(row => ({...row,platform,aweme_id:"shared-note",media_type:platform === "xhs" ? "images" : "video",create_time:1756684800,
        download_status:"skipped",cover_url:"",like_count:12,comment_count:3,source:taskSource(row.target_id)}));
      const targetId = url.searchParams.get("target_id");
      if (targetId) rows = rows.filter(row => String(row.target_id) === targetId);
      const from = url.searchParams.get("captured_from"), to = url.searchParams.get("captured_before");
      if (from) rows = rows.filter(row => row.captured_at && Date.parse(row.captured_at) >= Date.parse(from));
      if (to) rows = rows.filter(row => row.captured_at && Date.parse(row.captured_at) < Date.parse(to));
      const sort = url.searchParams.get("sort");
      if (sort === "captured_desc" || sort === "captured_asc") rows.sort((a,b) =>
        (Date.parse(a.captured_at || 0) - Date.parse(b.captured_at || 0)) * (sort === "captured_desc" ? -1 : 1));
      const page = Number(url.searchParams.get("page") || 1), size = Number(url.searchParams.get("page_size") || 10), total = rows.length;
      return json({items:rows.slice((page-1)*size,page*size),total,page,page_size:size,pages:Math.max(1,Math.ceil(total/size)),source:targetId ? taskSource(Number(targetId)) : null});
    }
    if (url.pathname === "/api/settings/engine") {
      if (method === "GET" && flags.fixtureEngineRead === "fail") return json({detail: "测试：配置读取暂时中断"}, 503);
      if (method === "PUT") {
        flags.fixtureEngineCount = String(Number(flags.fixtureEngineCount || 0) + 1);
        flags.fixtureEngineBody = init.body;
        await new Promise(resolve => setTimeout(resolve, flags.fixtureEngineSlow ? 650 : 90));
        if (flags.fixtureEngineWrite === "fail") return json({detail: "测试：设置尚未保存"}, 503);
        if (flags.fixtureEngineWrite === "invalid") return json({detail: [
          {loc: ["body", "xhs_read_mode"], type: "literal_error", msg: "Invalid mode"},
        ]}, 422);
      }
      return demo(input, init);
    }
    if (url.pathname === "/api/monitors" && method === "POST") flags.fixtureMonitorBody = init.body;
    // Explicit opt-in for legacy editor acceptance. Never reaches a real service.
    if (flags.fixtureEditors === "true") {
      if (!jobs.length) jobs.push(job({ status: "done", account_id: 1, keywords: ["日常创作", "城市漫游"], max_contents_per_keyword: 20,
        max_comments_per_content: 30, max_pages_per_keyword: 12, stagnant_pages: 3, include_replies: true, download_media: false }));
      if (method === "POST" && /\/contents\/\d+\/repost-/.test(url.pathname)) {
        flags.fixtureRepostCount = String(Number(flags.fixtureRepostCount || 0) + 1); flags.fixtureRepostBody = init.body;
        await new Promise(resolve => setTimeout(resolve, 550));
        return flags.fixtureEditFail ? json({detail: "测试：尚未加入发布队列"}, 503) : json({task_id: 901});
      }
      if (method === "PUT") {
        flags.fixtureEditCount = String(Number(flags.fixtureEditCount || 0) + 1);
        flags.fixtureEditPath = url.pathname; flags.fixtureEditBody = init.body;
        await new Promise(resolve => setTimeout(resolve, flags.fixtureEditSlow ? 650 : 90));
        if (flags.fixtureEditFail) return json({ detail: "测试：服务暂时忙碌，修改尚未保存" }, 503);
        const body = JSON.parse(init.body); edits[url.pathname] = body;
        if (url.pathname.startsWith("/api/collections/")) Object.assign(jobs.find(item => item.id === Number(url.pathname.split("/").pop())), body);
        return json({ ok: true, fingerprint: { fingerprint_id: "fixture-fingerprint", ...body } });
      }
      if (method === "GET" && url.pathname === "/api/danmaku-watches") return json([
        { id: 31, platform: "douyin", kind: "user", title: "示例账号的弹幕", alias: "日常弹幕观察", enabled: true,
          interval_seconds: 1800, recent_works: 5, recent_days: 7, max_scrolls: 6, probe_step_seconds: 15,
          ...edits["/api/danmaku-watches/31"] },
      ]);
      if (method === "GET" && url.pathname === "/api/account-actions") return json([
        { id: 81, account_id: 2, platform: "xhs", action: "send_dm", source_rule_id: 1, status: "draft",
          target_nick: "示例私信会话", content: "等待审核的回复", ...edits["/api/account-actions/81"] },
      ]);
      if (method === "GET" && /\/accounts\/\d+\/fingerprint$/.test(url.pathname)) return json({
        fingerprint_id: "fixture-fingerprint", seed: "fixture-stable-seed", locale: "zh-CN", timezone: "Asia/Shanghai",
        country: "中国", region: "广东", city: "深圳", viewport_w: 1280, viewport_h: 800, language_mode: "auto",
        viewport_mode: "auto", ...edits[url.pathname],
      });
      if (method === "GET" && /^\/api\/(monitors|comment-watches|comment-rules|comment-tasks|publish|notifications|proxies)$/.test(url.pathname)) {
        const response = await demo(input, init), rows = await response.json();
        return json(rows.map(item => ({ ...item, ...edits[url.pathname + "/" + item.id] })));
      }
    }
    if (flags.fixtureRead === "slow" && url.pathname === "/api/accounts") {
      flags.fixtureReadCount = String(Number(flags.fixtureReadCount || 0) + 1);
      await new Promise(resolve => setTimeout(resolve, 650));
    }
    if (flags.fixtureRead === "fail" && url.pathname === "/api/accounts") return json({detail: "测试：连接暂时中断"}, 503);
    if (url.pathname === "/api/task-queue") return json({
      summary: {active: 3, pending: 2, running: 1, blocked: 1, failed: 2}, total: 1, page: 1, pages: 1, page_size: 20,
      items: [{id: 77, queue_type: "publish", queue_label: "发布", state: "failed", status: "failed", platform: "douyin",
        title: "测试失败任务", error: "测试：需要检查素材", account_name: "示例账号", source_tab: "publish"}],
    });
    if (url.pathname === "/api/collections" && method === "POST") {
      flags.fixtureWriteCount = String(Number(flags.fixtureWriteCount || 0) + 1);
      await new Promise(resolve => setTimeout(resolve, flags.fixtureSlowWrite ? 650 : 120));
      if (flags.fixtureWrite === "fail") return json({detail: "测试：暂时忙碌，请稍后重试"}, 503);
      const created = job(JSON.parse(init.body)); jobs.push(created); return json(created);
    }
    if (url.pathname === "/api/collections" && method === "GET") return json(jobs);
    if (/\/api\/collections\/\d+\/contents/.test(url.pathname)) return json({items: [], total: 0, page: 1, pages: 1, page_size: 20});
    return demo(input, init);
  };
})();
