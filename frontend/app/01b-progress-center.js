// 01b-progress-center.js — 全局统一 SSE 实时进度总线与顶栏「活动任务中心」浮层。
(() => {
  const ACTIVE_STATES = new Set(["queued", "running", "waiting_user", "blocked"]);
  const TERMINAL_STATES = new Set(["completed", "failed", "canceled"]);
  const CATEGORY_META = {
    collection: { label: "关键词采集", tab: "collections" },
    share_download: { label: "链接下载", tab: "share-download" },
    publish: { label: "内容发布", tab: "publish" },
    monitor_download: { label: "监控下载", tab: "monitors" },
    collection_download: { label: "采集下载", tab: "collections" },
    monitor_scan: { label: "主页作品监控", tab: "monitors" },
    follow_sync: { label: "关注/粉丝", tab: "hub" },
    matrix_batch: { label: "矩阵批处理", tab: "accounts" },
    works_sync: { label: "作品同步", tab: "hub" },
    comment_rule: { label: "自动评论", tab: "autocomment" },
    runtime: { label: "系统组件", tab: "accounts" },
    task: { label: "后台任务", tab: "queue" },
  };
  const STATUS_META = {
    queued: { label: "排队中", tone: "neutral" },
    running: { label: "执行中", tone: "info" },
    waiting_user: { label: "需人工处理", tone: "warn" },
    blocked: { label: "风控延后", tone: "warn" },
    completed: { label: "已完成", tone: "ok" },
    failed: { label: "失败", tone: "danger" },
    canceled: { label: "已取消", tone: "neutral" },
  };

  const tasks = new Map();
  const listeners = new Set();
  const eventListeners = new Map();
  let es = null;
  let connected = false;
  let reconnectTimer = null;
  let fallbackTimer = null;
  let tickTimer = null;
  let drawerOpen = false;
  let started = false;

  function fmtBytes(bytes) {
    let n = Number(bytes || 0);
    if (!Number.isFinite(n) || n <= 0) return "0 B";
    if (n < 1024) return `${Math.round(n)} B`;
    const units = ["KB", "MB", "GB", "TB"];
    let i = -1;
    do { n /= 1024; i++; } while (n >= 1024 && i < units.length - 1);
    return `${n.toFixed(n >= 100 ? 0 : n >= 10 ? 1 : 2)} ${units[i]}`;
  }

  function fmtSpeed(bps) {
    const n = Number(bps || 0);
    if (!Number.isFinite(n) || n <= 0) return "";
    return `${fmtBytes(n)}/s`;
  }

  function fmtDuration(sec) {
    const s = Math.max(0, Math.round(Number(sec || 0)));
    if (s < 60) return `${s}s`;
    const m = Math.floor(s / 60);
    const rem = s % 60;
    if (m < 60) return rem ? `${m}分${String(rem).padStart(2, "0")}秒` : `${m}分`;
    const h = Math.floor(m / 60);
    return `${h}小时${m % 60}分`;
  }

  function fmtEta(sec) {
    const n = Number(sec);
    if (!Number.isFinite(n) || n <= 0) return "";
    return `剩余约 ${fmtDuration(n)}`;
  }

  function fmtCountdown(iso) {
    if (!iso) return "";
    const ts = Date.parse(iso.endsWith("Z") ? iso : iso + "Z");
    if (!Number.isFinite(ts)) return "";
    const diff = Math.ceil((ts - Date.now()) / 1000);
    if (diff <= 0) return "即将恢复";
    const m = Math.floor(diff / 60);
    const s = diff % 60;
    if (m >= 60) return `${Math.floor(m / 60)}:${String(m % 60).padStart(2, "0")}:${String(s).padStart(2, "0")} 后恢复`;
    return `${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")} 后恢复`;
  }

  function elapsedSeconds(task) {
    if (TERMINAL_STATES.has(task.status)) return Math.max(0, Number(task.elapsed_seconds || 0));
    const startedTs = task.started_at ? Date.parse(task.started_at.endsWith("Z") ? task.started_at : task.started_at + "Z") : NaN;
    if (Number.isFinite(startedTs)) return Math.max(0, Math.floor((Date.now() - startedTs) / 1000));
    return Math.max(0, Number(task.elapsed_seconds || 0));
  }

  function renderProgressBar({ determinate = false, percent = null, tone = "info", label = "任务进度", compact = false } = {}) {
    const hasPct = determinate && percent !== null && percent !== undefined && Number.isFinite(Number(percent));
    const pct = hasPct ? Math.max(0, Math.min(100, Number(percent))) : 0;
    const cls = [
      "progress-track",
      compact ? "is-compact" : "",
      hasPct ? "" : "is-indeterminate",
    ].filter(Boolean).join(" ");
    const fillCls = ["progress-fill", tone && tone !== "info" ? `tone-${tone}` : ""].filter(Boolean).join(" ");
    const widthStyle = hasPct ? `width:${pct.toFixed(1)}%` : "";
    const ariaVal = hasPct ? ` aria-valuenow="${Math.round(pct)}"` : "";
    return `<div class="${cls}" role="progressbar" aria-label="${esc(label)}" aria-valuemin="0" aria-valuemax="100"${ariaVal}><div class="${fillCls}" style="${widthStyle}"></div></div>`;
  }

  function sortedTasks() {
    const rank = { waiting_user: 0, running: 1, queued: 2, blocked: 3, failed: 4, canceled: 5, completed: 6 };
    return [...tasks.values()].sort((a, b) => {
      const ra = rank[a.status] ?? 9;
      const rb = rank[b.status] ?? 9;
      if (ra !== rb) return ra - rb;
      return String(b.updated_at || "").localeCompare(String(a.updated_at || ""));
    });
  }

  function notify(changedTask = null, kind = "update") {
    for (const fn of listeners) {
      try { fn(changedTask, kind); } catch (_) {}
    }
    renderTrigger();
    if (drawerOpen) renderDrawer();
    ensureTickLoop();
    try {
      window.dispatchEvent(new CustomEvent("creatorhub:progress:update", { detail: { task: changedTask, kind } }));
    } catch (_) {}
  }

  function emitBusinessEvent(name, payload) {
    const set = eventListeners.get(name);
    if (set) {
      for (const fn of set) {
        try { fn(payload); } catch (_) {}
      }
    }
    try {
      window.dispatchEvent(new CustomEvent(`creatorhub:sse:${name}`, { detail: payload }));
    } catch (_) {}
  }

  function applyTask(task) {
    if (!task || !task.task_key) return;
    const prev = tasks.get(task.task_key);
    tasks.set(task.task_key, task);
    if (prev && ACTIVE_STATES.has(prev.status) && TERMINAL_STATES.has(task.status)) {
      onTaskFinished(task);
    }
    if (task.category === "follow_sync") {
      syncFollowUiFromTask(task);
    }
    notify(task, "task");
  }

  function syncFollowUiFromTask(task) {
    if (typeof renderFollowSyncStatus !== "function") return;
    const dir = task.extra?.direction;
    if (!dir || String(task.account_id || "") !== String(typeof HUB_ACC !== "undefined" ? HUB_ACC : "")) return;
    const stMap = { queued: "queued", running: "running", blocked: "deferred", completed: "completed", failed: "failed", canceled: "canceled" };
    const view = {
      id: task.extra?.job_id || "",
      account_id: task.account_id,
      direction: dir,
      status: stMap[task.status] || task.status,
      phase: task.phase,
      pages: task.metrics?.pages || 0,
      fetched: task.metrics?.fetched || 0,
      saved: task.metrics?.saved || 0,
      added: task.metrics?.added || 0,
      expected_total: task.total || 0,
      percent: task.percent,
      capped: !!task.extra?.capped,
      source: task.extra?.source || "",
      error: task.error || "",
      cancelable: !!task.cancelable,
    };
    renderFollowSyncStatus(dir, view);
    if (TERMINAL_STATES.has(task.status) || task.status === "blocked") {
      if (task.status === "completed" && typeof refreshFollows === "function") {
        refreshFollows(dir, 1);
        if (typeof refreshHubSummary === "function") refreshHubSummary();
      }
    }
  }

  function onTaskFinished(task) {
    // 重要后台任务完成时刷新相关视图角标；避免与用户当前操作冲突。
    if (typeof refreshTaskQueueBadge === "function") refreshTaskQueueBadge();
    if (typeof CURRENT_TAB !== "undefined" && CURRENT_TAB === "queue" && typeof refreshTaskQueue === "function") {
      refreshTaskQueue();
    }
    if (task.category === "share_download" && task.status === "completed") {
      if (typeof refreshShareHistory === "function") refreshShareHistory();
    } else if (task.category === "collection") {
      if (typeof CURRENT_TAB !== "undefined" && CURRENT_TAB === "collections" && typeof refreshCollections === "function") {
        refreshCollections();
      }
    } else if (task.category === "publish") {
      if (typeof CURRENT_TAB !== "undefined" && CURRENT_TAB === "publish" && typeof refreshPublish === "function") {
        refreshPublish();
      }
    } else if (task.category === "monitor_scan" || task.category === "monitor_download") {
      if (typeof CURRENT_TAB !== "undefined" && CURRENT_TAB === "monitors") {
        if (typeof refreshMonitors === "function") refreshMonitors();
        if (typeof refreshContents === "function") refreshContents();
      }
    } else if (task.category === "works_sync") {
      if (typeof CURRENT_TAB !== "undefined" && CURRENT_TAB === "hub") {
        if (typeof refreshMyWorks === "function") refreshMyWorks();
        if (typeof refreshHubSummary === "function") refreshHubSummary();
      }
    }
  }

  function applySnapshot(list) {
    tasks.clear();
    if (Array.isArray(list)) {
      list.forEach(item => { if (item && item.task_key) tasks.set(item.task_key, item); });
    }
    notify(null, "snapshot");
  }

  function renderTrigger() {
    const btn = $("activity-trigger");
    if (!btn) return;
    const all = [...tasks.values()];
    const active = all.filter(t => ACTIVE_STATES.has(t.status));
    const waiting = active.filter(t => t.status === "waiting_user");
    const running = active.filter(t => t.status === "running" || t.status === "queued");
    const failedRecent = all.filter(t => t.status === "failed");

    const labelEl = $("activity-trigger-label");
    const countEl = $("activity-trigger-count");
    const barEl = $("activity-trigger-bar");

    const isIdle = active.length === 0 && failedRecent.length === 0;
    btn.classList.toggle("is-idle", isIdle);
    btn.classList.toggle("is-active", active.length > 0);
    btn.classList.toggle("is-warn", waiting.length > 0);
    btn.classList.toggle("is-error", waiting.length === 0 && active.length === 0 && failedRecent.length > 0);
    btn.title = active.length > 0
      ? `活动任务中心 · ${active.length} 项进行中`
      : (failedRecent.length > 0 ? `活动任务中心 · ${failedRecent.length} 项失败` : "活动任务中心 · 查看后台实时进度");

    const detTasks = running.filter(t => t.determinate && Number.isFinite(Number(t.percent)));
    const avgPct = detTasks.length
      ? Math.round(detTasks.reduce((sum, t) => sum + Math.max(0, Math.min(100, Number(t.percent))), 0) / detTasks.length)
      : null;

    if (waiting.length > 0) {
      if (labelEl) labelEl.textContent = `需处理 ${waiting.length}`;
    } else if (running.length > 0) {
      if (labelEl) labelEl.textContent = avgPct !== null ? `进行中 · ${avgPct}%` : `进行中`;
    } else if (active.length > 0) {
      if (labelEl) labelEl.textContent = `等待恢复 ${active.length}`;
    } else if (failedRecent.length > 0) {
      if (labelEl) labelEl.textContent = `失败 ${failedRecent.length}`;
    } else {
      if (labelEl) labelEl.textContent = "活动中心";
    }

    if (countEl) {
      const badgeNum = active.length || all.length;
      countEl.hidden = badgeNum <= 0;
      countEl.textContent = String(badgeNum);
    }

    if (barEl) {
      const inner = barEl.firstElementChild;
      if (running.length > 0 && inner) {
        barEl.classList.add("on");
        barEl.classList.toggle("indeterminate", avgPct === null);
        inner.style.width = avgPct !== null ? `${avgPct}%` : "";
      } else {
        barEl.classList.remove("on", "indeterminate");
        if (inner) inner.style.width = "0%";
      }
    }
  }

  function taskMetaLine(task) {
    const bits = [];
    if (task.unit === "bytes" && Number(task.total) > 0) {
      bits.push(`${fmtBytes(task.current)} / ${fmtBytes(task.total)}`);
    } else if (task.unit === "bytes" && Number(task.current) > 0) {
      bits.push(`已下载 ${fmtBytes(task.current)}`);
    } else if (Number(task.total) > 0 && task.unit === "items") {
      bits.push(`${fmtNum(task.current)} / ${fmtNum(task.total)}`);
    }
    const spd = fmtSpeed(task.speed_bps);
    if (spd) bits.push(spd);
    const eta = fmtEta(task.eta_seconds);
    if (eta) bits.push(eta);
    if (task.status === "blocked" && task.next_allowed_at) {
      const cd = fmtCountdown(task.next_allowed_at);
      if (cd) bits.push(cd);
    }
    const sec = elapsedSeconds(task);
    if (sec > 0 && ACTIVE_STATES.has(task.status)) bits.push(`已用 ${fmtDuration(sec)}`);
    return bits.join(" · ");
  }

  function renderDrawerItem(task) {
    const cat = CATEGORY_META[task.category] || CATEGORY_META.task;
    const st = STATUS_META[task.status] || STATUS_META.running;
    const pfName = task.platform && typeof PF_NAME !== "undefined" ? (PF_NAME[task.platform] || task.platform) : "";
    const barTone = st.tone === "danger" ? "danger" : st.tone === "warn" ? "warn" : st.tone === "ok" ? "ok" : "info";
    const hasPct = task.determinate && task.percent !== null && task.percent !== undefined && Number.isFinite(Number(task.percent));
    const pctText = hasPct ? `${Math.round(Number(task.percent))}%` : (ACTIVE_STATES.has(task.status) ? "进行中" : st.label);
    const meta = taskMetaLine(task);
    const showBar = ACTIVE_STATES.has(task.status) || task.status === "completed";
    const actionBtn = task.action_required
      ? `<button type="button" class="sm" onclick="window.CreatorHubProgress.handleTaskAction(${jsArg(task.task_key)})">${esc(task.action_required.label || "去处理")}</button>`
      : "";
    return `<article class="act-item tone-${st.tone}" data-task-key="${esc(task.task_key)}">
      <div class="act-item-head">
        <div class="act-item-badges">
          <span class="pill bare">${esc(cat.label)}</span>
          ${pfName ? `<span class="pill bare">${esc(pfName)}</span>` : ""}
          <span class="pill ${st.tone === "ok" ? "done" : st.tone === "danger" ? "failed" : st.tone === "warn" ? "pending" : "downloading"} bare">${esc(st.label)}</span>
        </div>
        <span class="act-item-pct progress-num">${esc(pctText)}</span>
      </div>
      <div class="act-item-title" title="${esc(task.title)}">${esc(task.title)}</div>
      <div class="act-item-phase">
        <span>${esc(task.phase_label || st.label)}</span>
        ${task.detail ? `<span class="mut"> · ${esc(task.detail)}</span>` : ""}
      </div>
      ${showBar ? renderProgressBar({ determinate: task.determinate, percent: task.percent, tone: barTone, label: task.title, compact: true }) : ""}
      ${meta ? `<div class="act-item-meta progress-num">${esc(meta)}</div>` : ""}
      ${task.error ? `<div class="act-item-err" title="${esc(task.error)}">${esc(task.error)}</div>` : ""}
      <div class="act-item-actions">
        ${actionBtn}
        ${task.cancelable ? `<button type="button" class="ghost sm danger" onclick="window.CreatorHubProgress.cancelTask(${jsArg(task.task_key)}, this)">取消</button>` : ""}
        <button type="button" class="ghost sm" onclick="window.CreatorHubProgress.jumpToTask(${jsArg(task.task_key)})">定位</button>
      </div>
    </article>`;
  }

  function renderDrawer() {
    const body = $("activity-drawer-body");
    const sub = $("activity-drawer-sub");
    const clearBtn = $("activity-clear-btn");
    if (!body) return;
    const all = sortedTasks();
    const active = all.filter(t => ACTIVE_STATES.has(t.status));
    const finished = all.filter(t => TERMINAL_STATES.has(t.status));
    if (clearBtn) clearBtn.hidden = finished.length === 0;
    if (sub) {
      const connHint = connected ? "实时推送已连接" : "自动重连中";
      sub.textContent = active.length
        ? `${active.length} 项进行中 · ${connHint}`
        : (finished.length ? `近期 ${finished.length} 项已结束 · ${connHint}` : `当前无后台活动任务 · ${connHint}`);
    }
    if (!all.length) {
      setHtmlIfChanged(body, `<div class="act-empty"><div class="empty-ic">${ic("i-check")}</div><b>一切空闲</b><span>发起采集、链接下载、发布或批量检测后，可在这里跨页查看实时进度。</span></div>`);
      return;
    }
    const groups = [
      ["需要处理", all.filter(t => t.status === "waiting_user" || t.status === "blocked")],
      ["正在执行", all.filter(t => t.status === "running" || t.status === "queued")],
      ["最近结束", finished],
    ].filter(([, list]) => list.length > 0);
    const html = groups.map(([title, list]) =>
      `<section class="act-group"><h4 class="act-group-title">${esc(title)} <span>${list.length}</span></h4><div class="act-list">${list.map(renderDrawerItem).join("")}</div></section>`
    ).join("");
    setHtmlIfChanged(body, html);
  }

  function toggleDrawer(force) {
    const drawer = $("activity-drawer");
    const trigger = $("activity-trigger");
    if (!drawer) return;
    drawerOpen = typeof force === "boolean" ? force : !drawerOpen;
    drawer.hidden = !drawerOpen;
    if (trigger) trigger.setAttribute("aria-expanded", String(drawerOpen));
    if (drawerOpen) {
      renderDrawer();
      ensureTickLoop();
    }
  }

  function ensureTickLoop() {
    const hasLive = [...tasks.values()].some(t => ACTIVE_STATES.has(t.status));
    if (drawerOpen && hasLive) {
      if (!tickTimer) {
        tickTimer = setInterval(() => {
          if (drawerOpen && !document.hidden) renderDrawer();
        }, 1000);
      }
    } else if (tickTimer) {
      clearInterval(tickTimer);
      tickTimer = null;
    }
  }

  async function cancelTask(taskKey, btn = null) {
    if (!taskKey) return;
    const run = async () => {
      try {
        const res = await api(`/api/progress/tasks/${encodeURIComponent(taskKey)}/cancel`, { method: "POST" });
        if (res && res.ok) toast("已发送取消请求", "info", 3000);
      } catch (e) {
        toast("取消失败：" + e.message, "err");
      }
    };
    if (btn) await withBusy(btn, "取消中", run);
    else await run();
  }

  async function clearFinished() {
    try {
      await api("/api/progress/clear-finished", { method: "POST" });
      for (const [key, item] of [...tasks.entries()]) {
        if (TERMINAL_STATES.has(item.status)) tasks.delete(key);
      }
      notify(null, "clear_finished");
    } catch (e) {
      toast("清理失败：" + e.message, "err");
    }
  }

  function jumpToTask(taskKey) {
    const task = tasks.get(taskKey);
    if (!task) return;
    const cat = CATEGORY_META[task.category] || CATEGORY_META.task;
    const targetTab = task.source_tab || cat.tab || "queue";
    if (task.platform && typeof PLATFORM !== "undefined" && task.platform !== PLATFORM && targetTab !== "share-download" && targetTab !== "accounts" && targetTab !== "queue") {
      if (typeof switchPlatform === "function") switchPlatform(task.platform);
    }
    if (typeof switchTab === "function") switchTab(targetTab, true);
    toggleDrawer(false);
  }

  function handleTaskAction(taskKey) {
    const task = tasks.get(taskKey);
    const act = task && task.action_required;
    if (!act) { jumpToTask(taskKey); return; }
    if (act.kind === "open_browser" && act.account_id && typeof openAccountBrowser === "function") {
      openAccountBrowser(act.account_id, act.url || "");
      return;
    }
    if (act.kind === "close_and_publish" && act.task_id && typeof runPublish === "function") {
      toggleDrawer(false);
      runPublish(act.task_id, true);
      return;
    }
    jumpToTask(taskKey);
  }

  async function fetchSnapshotFallback() {
    if (document.documentElement?.dataset?.preview === "true") return;
    try {
      const r = await apiFetch("/api/progress/tasks");
      if (!r.ok) return;
      const data = await r.json();
      if (data && Array.isArray(data.tasks)) applySnapshot(data.tasks);
    } catch (_) {}
  }

  function connect() {
    if (document.documentElement?.dataset?.preview === "true") return;
    if (typeof EventSource === "undefined") {
      fetchSnapshotFallback();
      if (!fallbackTimer) fallbackTimer = setInterval(fetchSnapshotFallback, 3000);
      return;
    }
    if (es) { try { es.close(); } catch (_) {} es = null; }
    clearTimeout(reconnectTimer);
    try {
      es = new EventSource("/api/progress/stream");
      es.addEventListener("snapshot", e => {
        connected = true;
        if (fallbackTimer) { clearInterval(fallbackTimer); fallbackTimer = null; }
        try {
          const data = JSON.parse(e.data || "{}");
          applySnapshot(data.tasks || []);
        } catch (_) {}
      });
      es.addEventListener("task", e => {
        connected = true;
        try { applyTask(JSON.parse(e.data || "{}")); } catch (_) {}
      });
      es.addEventListener("remove", e => {
        try {
          const data = JSON.parse(e.data || "{}");
          if (data.task_key && tasks.delete(data.task_key)) notify(null, "remove");
        } catch (_) {}
      });
      es.addEventListener("clear_finished", e => {
        try {
          const data = JSON.parse(e.data || "{}");
          (data.task_keys || []).forEach(k => tasks.delete(k));
          notify(null, "clear_finished");
        } catch (_) {}
      });
      es.addEventListener("event", e => {
        try {
          const data = JSON.parse(e.data || "{}");
          if (data.name) emitBusinessEvent(data.name, data.payload || {});
        } catch (_) {}
      });
      es.onerror = () => {
        connected = false;
        if (drawerOpen) renderDrawer();
        if (es) { try { es.close(); } catch (_) {} es = null; }
        if (!fallbackTimer) fallbackTimer = setInterval(fetchSnapshotFallback, 4000);
        clearTimeout(reconnectTimer);
        reconnectTimer = setTimeout(connect, 3500);
      };
    } catch (_) {
      connected = false;
      if (!fallbackTimer) fallbackTimer = setInterval(fetchSnapshotFallback, 4000);
    }
  }

  function start() {
    if (started) return;
    started = true;
    document.addEventListener("click", e => {
      if (!drawerOpen) return;
      const drawer = $("activity-drawer");
      const trigger = $("activity-trigger");
      if (drawer && !drawer.contains(e.target) && (!trigger || !trigger.contains(e.target))) {
        toggleDrawer(false);
      }
    });
    document.addEventListener("keydown", e => {
      if (e.key === "Escape" && drawerOpen) toggleDrawer(false);
    });
    const spinner = $("busy-spinner");
    if (spinner) {
      spinner.style.cursor = "pointer";
      spinner.title = "点击打开活动任务中心";
      spinner.addEventListener("click", () => {
        if (tasks.size > 0) toggleDrawer(true);
      });
    }
    connect();
  }

  window.CreatorHubProgress = {
    start,
    isConnected: () => connected,
    getTasks: sortedTasks,
    getTask: key => tasks.get(key) || null,
    getActiveCount: () => [...tasks.values()].filter(t => ACTIVE_STATES.has(t.status)).length,
    subscribe(fn) { listeners.add(fn); return () => listeners.delete(fn); },
    onEvent(name, fn) {
      if (!eventListeners.has(name)) eventListeners.set(name, new Set());
      eventListeners.get(name).add(fn);
      return () => eventListeners.get(name)?.delete(fn);
    },
    toggleDrawer,
    cancelTask,
    clearFinished,
    jumpToTask,
    handleTaskAction,
    fmtBytes,
    fmtSpeed,
    fmtDuration,
    fmtEta,
    fmtCountdown,
    renderProgressBar,
  };
})();
