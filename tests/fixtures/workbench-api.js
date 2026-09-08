// Browser acceptance only. Appended to the isolated demo, never shipped live.
(() => {
  const demo = window.fetch;
  const jobs = [];
  const edits = {};
  const json = (data, status = 200) => new Response(JSON.stringify(data), {status, headers: {"Content-Type": "application/json"}});
  const job = body => ({id: jobs.length + 1, platform: "douyin", status: "pending", created_at: "2026-09-08T01:00:00Z",
    content_count: 0, comment_count: 0, error_count: 0, planned_content_count: 20, ...body});
  window.fetch = async (input, init = {}) => {
    const url = new URL(typeof input === "string" ? input : input.url, location.href);
    const method = (init.method || "GET").toUpperCase(), flags = document.documentElement.dataset;
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
