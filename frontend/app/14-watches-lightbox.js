// 14-watches-lightbox.js — 独立弹幕监控、独立评论监控及作品图集/视频预览灯箱。
// ─── 短视频弹幕监控(独立) ───
function danmakuWatchBaseName(w) {
  return w.title || w.aweme_id || (w.sec_uid || "").slice(0, 12);
}
function populateDanmakuFacets() {
  setFacetOptions("danmaku-watch-group", "全部分组", DANMAKU_WATCHES.map(x => x.group_name));
  setFacetOptions("danmaku-watch-tag", "全部标签", DANMAKU_WATCHES.flatMap(itemTags));
  populateWatchRecordSource("danmaku");
}
function danmakuWatchRow(w) {
  const base = esc(danmakuWatchBaseName(w));
  const source = w.mode === "creator" ? "创作中心" : "公开视频";
  const error = w.last_error
    ? ' <span class="warn-ic" title="' + esc(w.last_error) + '">' + ic("i-info") + "</span>" : "";
  const avatar = w.avatar
    ? '<img class="avatar" src="' + esc(safeMediaUrl(w.avatar)) + '" referrerpolicy="no-referrer">' : "";
  const alias = w.alias ? '<div class="alias-line">' + esc(w.alias) + "</div>" : "";
  const interval = w.interval_seconds
    ? monitorIntervalText(w.interval_seconds)
    : "跟随全局" + (w.effective_interval_seconds ? "（" + monitorIntervalText(w.effective_interval_seconds) + "）" : "");
  const scope = w.kind === "user"
    ? '<div class="mut" style="font-size:11px;margin-top:2px">' +
      (w.recent_works ? "近 " + w.recent_works + " 个" : "全局 " + (w.effective_recent_works || "") + " 个") +
      " · " + (w.recent_days ? "近 " + w.recent_days + " 天" : "全局 " + (w.effective_recent_days || "") + " 天") +
      "</div>" : "";
  return '<tr>' +
    '<td><div class="user-cell">' + avatar + '<div><span title="' + base + '">' + base + "</span>" + alias + scope + "</div></div></td>" +
    "<td>" + metaChips(w) + "</td>" +
    "<td>" + (w.kind === "video" ? "单条视频" : "账号作品") + ' · <span class="mut">' + source + "</span></td>" +
    `<td class="num"><button type="button" class="ghost sm monitor-record-link" data-danmaku-records="${w.id}" onclick="showWatchRecords('danmaku',${w.id})">查看记录 <span>${fmtNum(w.danmaku_count || 0)}</span></button></td>` +
    '<td class="num">' + interval + "</td>" +
    '<td class="mut">' + (w.last_scan_at ? new Date(w.last_scan_at + "Z").toLocaleString() : "—") + error + autoRunHint(w.next_auto_run_at) + "</td>" +
    '<td><span class="pill ' + (w.enabled ? "active" : "paused") + '">' +
      (w.enabled ? "监控中" : "已暂停") + "</span></td>" +
    '<td class="acttd">' +
      '<button class="ghost sm" onclick="scanDanmakuWatch(' + w.id + ')">立即抓取</button>' +
      '<button class="ghost sm" onclick="editDanmakuWatch(' + w.id + ')">编辑</button>' +
      '<button class="ghost sm" onclick="toggleDanmakuWatch(' + w.id + ", " + (!w.enabled) + ')">' +
        (w.enabled ? "暂停" : "启用") + "</button>" +
      '<button class="ghost sm danger" onclick="delDanmakuWatch(' + w.id + ')">' +
        ic("i-trash") + "删除</button></td></tr>";
}
function renderDanmakuWatchRows() {
  const group = $("danmaku-watch-group") ? $("danmaku-watch-group").value : "";
  const tag = $("danmaku-watch-tag") ? $("danmaku-watch-tag").value : "";
  const status = $("danmaku-watch-status") ? $("danmaku-watch-status").value : "";
  const query = (($("danmaku-watch-search") && $("danmaku-watch-search").value) || "").trim().toLocaleLowerCase();
  const rows = DANMAKU_WATCHES.filter(w => {
    if (!matchesMeta(w, group, tag)) return false;
    if (status === "enabled" && !w.enabled) return false;
    if (status === "paused" && w.enabled) return false;
    if (status === "error" && !w.last_error) return false;
    if (!query) return true;
    return [danmakuWatchBaseName(w), w.alias, w.group_name, ...itemTags(w)]
      .join(" ").toLocaleLowerCase().includes(query);
  });
  if ($("danmaku-watch-filter-count")) {
    $("danmaku-watch-filter-count").textContent =
      "显示 " + rows.length + " / " + DANMAKU_WATCHES.length;
  }
  $("danmaku-watch-table").innerHTML = rows.map(danmakuWatchRow).join("") ||
    empty(8, "没有匹配的弹幕监控", "i-msg",
          DANMAKU_WATCHES.length ? "调整筛选条件" : "添加目标后，在这里查看弹幕监控状态。", DANMAKU_WATCHES.length ? "" : "danmaku");
}
async function addDanmakuWatch() {
  const btn = evtBtn() || document.querySelector('button[onclick="addDanmakuWatch()"]');
  if (btn?.disabled) return;
  await withBusy(btn, "识别中", async () => {
  const normalizedTarget = await normalizeDanmakuTarget(null, { quiet: true });
  if (normalizedTarget === null) return;
  const url = $("d-w-url").value.trim();
  if (!url) { toast("请粘贴视频链接 / 账号主页 / aweme_id", "err"); return; }
  const mode = $("d-w-mode").value;
  if (mode === "creator" && !$("d-w-acc").value) {
    toast("创作中心模式需要选择创作者账号", "err"); return;
  }
  $("d-w-msg").textContent = "解析中…";
  await withBusy(btn, "解析中", async () => {
    try {
      await api("/api/danmaku-watches", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          url_or_id: url, platform: PLATFORM, kind: $("d-w-kind").value, mode: PLATFORM === "douyin" ? mode : "public",
          account_id: $("d-w-acc").value ? +$("d-w-acc").value : null,
          interval_seconds: monitorIntervalSeconds("d-w-interval"),
          recent_works: +$("d-w-recent").value, recent_days: +$("d-w-days").value,
          max_scrolls: +$("d-w-depth").value, alias: $("d-w-alias").value.trim(),
          time_start_ms: Math.round(Math.max(0, +$("d-w-time-start").value || 0) * 1000),
          time_end_ms: Math.round(Math.max(0, +$("d-w-time-end").value || 0) * 1000),
          probe_step_seconds: +$("d-w-probe-step").value || 0,
          include_keywords: parseDanmakuKeywords($("d-w-include").value),
          exclude_keywords: parseDanmakuKeywords($("d-w-exclude").value),
          min_text_length: Math.max(0, +$("d-w-min-len").value || 0),
          max_text_length: Math.max(0, +$("d-w-max-len").value || 0),
          min_like_count: Math.max(0, +$("d-w-min-like").value || 0),
          max_records_per_scan: Math.max(0, +$("d-w-scan-cap").value || 0),
          max_records_total: Math.max(0, +$("d-w-total-cap").value || 0),
          group_name: getMetaValue("d-w-group").trim(),
          tags: parseTags(getMetaValue("d-w-tags")),
        }),
      });
      ["d-w-url", "d-w-alias", "d-w-include", "d-w-exclude"].forEach(id => $(id).value = "");
      ["d-w-time-start", "d-w-time-end", "d-w-min-len", "d-w-max-len", "d-w-min-like", "d-w-scan-cap", "d-w-total-cap"].forEach(id => $(id).value = "0");
      setMetaValue("d-w-group", ""); setMetaValue("d-w-tags", "");
      resetDanmakuTargetState();
      $("d-w-msg").textContent = "已添加 ✓";
      toast("已开始监控弹幕", "ok");
      globalThis.CreatorHubWorkbench?.completed?.("danmaku");
    } catch (e) {
      $("d-w-msg").textContent = "失败: " + e.message;
      toast("添加失败:" + e.message, "err");
    }
  });
  refreshDanmakuWatches();
  });
}
async function refreshDanmakuWatches() {
  const isCurrent = beginViewRequest("danmaku-watches");
  if (!["douyin", "bilibili"].includes(PLATFORM)) return;
  const rows = await api("/api/danmaku-watches?platform=" + encodeURIComponent(PLATFORM));
  if (!isCurrent()) return;
  DANMAKU_WATCHES = rows;
  populateDanmakuFacets();
  if ($("tb-danmaku")) $("tb-danmaku").textContent = rows.length;
  renderDanmakuWatchRows();
}
function onDanmakuSrc() {
  DANMAKU_SRC = $("danmaku-src").value;
  refreshDanmaku(true);
}
async function editDanmakuWatch(id) {
  const item = DANMAKU_WATCHES.find(x => x.id === id);
  if (!item) return;
  const intervalOptions = monitorIntervalOptions(item.interval_seconds || 0, true);
  const recentOptions = numericSelectOptions(item.recent_works || 0, [
    [0, "跟随全局设置"], [3, "最近 3 个作品"], [5, "最近 5 个作品"],
    [10, "最近 10 个作品"], [20, "最近 20 个作品"], [50, "最近 50 个作品"],
  ]);
  const dayOptions = numericSelectOptions(item.recent_days || 0, [
    [0, "跟随全局设置"], [3, "最近 3 天"], [7, "最近 7 天"],
    [14, "最近 14 天"], [30, "最近 30 天"], [90, "最近 90 天"],
  ]);
  const depthOptions = numericSelectOptions(item.max_scrolls || 0, [
    [0, "跟随全局设置"], [3, "浅层"], [6, "标准"], [12, "深度"], [20, "最大"],
  ]);
  const probeOptions = numericSelectOptions(item.probe_step_seconds || 0, [
    [0, "跟随全局设置"], [0.5, "每 0.5 秒"], [1, "每 1 秒"], [2, "每 2 秒"], [5, "每 5 秒"],
  ], " 秒");
  const value = await new Promise(res => {
    _uiResolve = res; _uiCancelVal = null;
    _uiGetVal = () => ({
      interval_seconds: monitorIntervalSeconds("edw-interval"),
      recent_works: +$("edw-recent").value,
      recent_days: +$("edw-days").value,
      max_scrolls: +$("edw-depth").value,
      time_start_ms: Math.round(Math.max(0, +$("edw-start").value || 0) * 1000),
      time_end_ms: Math.round(Math.max(0, +$("edw-end").value || 0) * 1000),
      probe_step_seconds: +$("edw-probe").value || 0,
      include_keywords: parseDanmakuKeywords($("edw-include").value),
      exclude_keywords: parseDanmakuKeywords($("edw-exclude").value),
      min_text_length: Math.max(0, +$("edw-min-len").value || 0),
      max_text_length: Math.max(0, +$("edw-max-len").value || 0),
      min_like_count: Math.max(0, +$("edw-min-like").value || 0),
      max_records_per_scan: Math.max(0, +$("edw-scan-cap").value || 0),
      max_records_total: Math.max(0, +$("edw-total-cap").value || 0),
    });
    $("ui-body").innerHTML = `
      <fieldset class="monitor-config-group"><legend>扫描范围</legend>
        <div class="row">
          <div><label class="field" for="edw-start">视频内起点(秒)</label><input id="edw-start" type="number" min="0" step="0.1" value="${(item.time_start_ms || 0) / 1000}"></div>
          <div><label class="field" for="edw-end">视频内终点(秒)</label><input id="edw-end" type="number" min="0" step="0.1" value="${(item.time_end_ms || 0) / 1000}"></div>
          <div><label class="field" for="edw-probe">时间轴扫描步长</label><select id="edw-probe">${probeOptions}</select></div>
          <div><label class="field" for="edw-interval">检查频率</label><select id="edw-interval">${intervalOptions}</select></div>
        </div>
      </fieldset>
      <fieldset class="monitor-config-group"><legend>账号模式与容量</legend>
        <div class="row">
          <div><label class="field" for="edw-recent">近期作品数</label><select id="edw-recent">${recentOptions}</select></div>
          <div><label class="field" for="edw-days">作品时间范围</label><select id="edw-days">${dayOptions}</select></div>
          <div><label class="field" for="edw-depth">加载轮次</label><select id="edw-depth">${depthOptions}</select></div>
        </div>
        <div class="row">
          <div><label class="field" for="edw-scan-cap">单轮入库上限</label><input id="edw-scan-cap" type="number" min="0" value="${item.max_records_per_scan || 0}"></div>
          <div><label class="field" for="edw-total-cap">总保留上限</label><input id="edw-total-cap" type="number" min="0" value="${item.max_records_total || 0}"></div>
          <div><label class="field" for="edw-min-like">最少点赞数</label><input id="edw-min-like" type="number" min="0" value="${item.min_like_count || 0}"></div>
        </div>
      </fieldset>
      <fieldset class="monitor-config-group"><legend>内容过滤</legend>
        <div class="row">
          <div><label class="field" for="edw-min-len">最短文本长度</label><input id="edw-min-len" type="number" min="0" max="200" value="${item.min_text_length || 0}"></div>
          <div><label class="field" for="edw-max-len">最长文本长度</label><input id="edw-max-len" type="number" min="0" max="200" value="${item.max_text_length || 0}"></div>
        </div>
        <div><label class="field" for="edw-include">包含关键词</label><input id="edw-include" value="${esc((item.include_keywords || []).join(","))}" placeholder="逗号分隔，命中任一项才保留"></div>
        <div><label class="field" for="edw-exclude">排除关键词</label><input id="edw-exclude" value="${esc((item.exclude_keywords || []).join(","))}" placeholder="逗号分隔，命中任一项则丢弃"></div>
      </fieldset>`;
    setupMonitorInterval("edw-interval", item.interval_seconds || 0, true);
    ["edw-interval", "edw-recent", "edw-days", "edw-depth", "edw-probe"].forEach(key => {
      const el = $(key); if (el) enhanceSelect(el);
    });
    $("edw-recent").value = String(item.recent_works || 0);
    $("edw-days").value = String(item.recent_days || 0);
    $("edw-depth").value = String(item.max_scrolls || 0);
    $("edw-probe").value = String(item.probe_step_seconds || 0);
    ["edw-interval", "edw-recent", "edw-days", "edw-depth", "edw-probe"].forEach(key => {
      const el = $(key); if (el && el._csSync) el._csSync();
    });
    _uiOpen("编辑弹幕监控", "监控对象保持不变。调整抓取范围、过滤条件和容量上限。", {
      okText: "保存修改", wide: true, icon: "i-captions",
      submit: value => api("/api/danmaku-watches/" + id, {
        method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(value),
      }),
    });
  });
  if (value === null) return;
  toast("弹幕监控配置已更新", "ok"); refreshDanmakuWatches(); refreshDanmaku();
}
async function scanDanmakuWatch(id) {
  const btn = evtBtn();
  toast("抓取中…正在加载视频弹幕", "info", 7000);
  await withBusy(btn, "抓取中", async () => {
    try {
      const result = await api("/api/danmaku-watches/" + id + "/scan-now", { method: "POST" });
      toast("弹幕抓取完成,新增 " + (result.new_danmaku ?? 0) + " 条", "ok");
    } catch (e) { toast("抓取失败:" + e.message, "err"); }
  });
  refreshDanmakuWatches(); refreshDanmaku();
}
async function toggleDanmakuWatch(id, on) {
  try {
    await api("/api/danmaku-watches/" + id, {
      method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled: on }),
    });
    refreshDanmakuWatches();
  } catch (e) { toast("操作失败:" + e.message, "err"); }
}
async function delDanmakuWatch(id) {
  if (!await uiConfirm({ title: "删除弹幕监控", message: "仅删除该监控配置，已抓取的弹幕记录会保留，并标注原任务已删除。",
                         okText: "删除", danger: true })) return;
  try {
    await api("/api/danmaku-watches/" + id + "?with_records=false", { method: "DELETE" });
    toast("监控已删除，弹幕记录已保留", "ok"); await refreshDanmakuWatches(); await refreshDanmaku(true);
  } catch (e) { toast("删除失败:" + e.message, "err"); }
}
function danmakuTime(ms) {
  const value = Math.max(0, Math.floor(ms || 0));
  const sec = Math.floor(value / 1000);
  const base = Math.floor(sec / 60) + ":" + String(sec % 60).padStart(2, "0");
  const fraction = value % 1000;
  return fraction ? base + "." + String(fraction).padStart(3, "0") : base;
}
function danmakuCapturedAt(value) {
  if (!value) return "—";
  const raw = String(value);
  const d = new Date(/[zZ]$/.test(raw) ? raw : raw + "Z");
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleString() + "." + String(d.getMilliseconds()).padStart(3, "0");
}
function renderDanmakuPager(meta) {
  const pager = $("danmaku-pager");
  if (!pager) return;
  const total = Math.max(0, Number(meta && meta.total || 0));
  const pageSize = Math.max(1, Number(meta && meta.page_size || DANMAKU_PAGE_SIZE));
  const pages = Math.max(1, Number(meta && meta.pages || Math.ceil(total / pageSize) || 1));
  const page = Math.max(1, Number(meta && meta.page || DANMAKU_PAGE));
  DANMAKU_TOTAL = total;
  DANMAKU_PAGE_SIZE = pageSize;
  DANMAKU_PAGE = page;
  if ($("danmaku-page-size")) $("danmaku-page-size").value = String(pageSize);
  if ($("danmaku-page-input")) {
    $("danmaku-page-input").value = String(page);
    $("danmaku-page-input").max = String(pages);
  }
  $("danmaku-page-info").textContent = "第 " + page + " / " + pages + " 页 · 共 " + fmtNum(total) + " 条";
  if ($("danmaku-first")) $("danmaku-first").disabled = page <= 1;
  $("danmaku-prev").disabled = page <= 1;
  $("danmaku-next").disabled = page >= pages;
  if ($("danmaku-last")) $("danmaku-last").disabled = page >= pages;
  pager.hidden = total <= pageSize;
}
async function refreshDanmaku(resetPage = false) {
  const isCurrent = beginViewRequest("danmaku");
  if (!["douyin", "bilibili"].includes(PLATFORM) || !$("danmaku-table")) return;
  const load = prepareWatchRecordLoad("danmaku", resetPage); if (!load) return;
  const params = new URLSearchParams({
    platform: PLATFORM, page: String(DANMAKU_PAGE),
    page_size: String(DANMAKU_PAGE_SIZE), paginate: "true",
  });
  if (DANMAKU_SRC) params.set("watch_id", DANMAKU_SRC);
  for (const [key, value] of Object.entries(load.bounds)) params.set(key, value);
  const query = ($("danmaku-query") && $("danmaku-query").value || "").trim();
  const start = +(($('danmaku-time-start') && $('danmaku-time-start').value) || 0);
  const end = +(($('danmaku-time-end') && $('danmaku-time-end').value) || 0);
  if (query) params.set("q", query);
  if (start > 0) params.set("min_video_time_ms", String(Math.round(start * 1000)));
  if (end > 0) params.set("max_video_time_ms", String(Math.round(end * 1000)));
  params.set("sort", ($("danmaku-sort") && $("danmaku-sort").value) || "captured_desc");
  let payload;
  try { payload = await api("/api/danmaku?" + params.toString()); }
  catch (e) { if (isCurrent()) watchRecordLoadError("danmaku", e, load.clear); return; }
  if (!isCurrent()) return;
  const meta = Array.isArray(payload)
    ? { items: payload, total: payload.length, page: 1, page_size: DANMAKU_PAGE_SIZE,
        pages: Math.max(1, Math.ceil(payload.length / DANMAKU_PAGE_SIZE)) }
    : (payload || {});
  const pages = Math.max(1, Number(meta.pages || 1));
  if (DANMAKU_PAGE > pages) {
    DANMAKU_PAGE = pages;
    return refreshDanmaku();
  }
  const rows = Array.isArray(meta.items) ? meta.items : [];
  cacheWatchRecordSources("danmaku", meta, rows);
  if ($("danmaku-filter-count")) {
    $("danmaku-filter-count").textContent = `显示 ${rows.length} / ${Number(meta.total || rows.length)}`;
  }
  $("danmaku-table").innerHTML = rows.map(r => '<tr>' +
    '<td class="watch-record-main"><div class="watch-record-text">' + esc(r.text || "") + "</div>" + watchRecordSourceMarkup("danmaku", r) + "</td>" +
    '<td class="mut watch-record-user" data-label="用户" title="' + esc(r.user_id || "") + '">' +
      esc(r.user_nickname || (r.user_id ? "用户 ID " + r.user_id : "用户")) + "</td>" +
    '<td class="num watch-record-point" data-label="视频内时间"><code>' + danmakuTime(r.video_time_ms) + "</code></td>" +
    '<td class="watch-record-channel" data-label="获取渠道">' + (r.source === "creator" ? "创作中心" : r.source === "public" ? "播放页" : "未记录") + "</td>" +
    '<td class="watch-record-time">' + watchRecordTimeMarkup("danmaku", r) + "</td>" +
    '<td class="acttd watch-record-actions"><button class="ghost sm danger" onclick="deleteDanmaku(' + r.id + ')" aria-label="删除这条弹幕">' +
      ic("i-trash") + "删除</button></td></tr>").join("") ||
    empty(6, "暂无弹幕", "i-msg", "添加弹幕监控后，带视频时间点的弹幕会显示在这里");
  renderDanmakuPager(meta);
}
function danmakuPageCount() {
  return Math.max(1, Math.ceil(DANMAKU_TOTAL / DANMAKU_PAGE_SIZE));
}
function goDanmakuPage(page) {
  const pages = danmakuPageCount();
  const target = page <= 0 ? pages : Math.min(pages, Math.max(1, Math.round(Number(page) || 1)));
  if (target === DANMAKU_PAGE) return;
  DANMAKU_PAGE = target;
  refreshDanmaku();
}
function changeDanmakuPage(delta) {
  goDanmakuPage(DANMAKU_PAGE + Number(delta || 0));
}
function jumpDanmakuPage() {
  const input = $("danmaku-page-input");
  const value = input ? Number(input.value) : 1;
  if (!Number.isFinite(value) || value < 1) {
    if (input) { input.value = String(DANMAKU_PAGE); input.focus(); }
    return;
  }
  goDanmakuPage(value);
}
function handleDanmakuPageInput(event) {
  if (event && event.key === "Enter") {
    event.preventDefault();
    jumpDanmakuPage();
  }
}
function setDanmakuPageSize() {
  const value = +(($('danmaku-page-size') && $('danmaku-page-size').value) || 10);
  DANMAKU_PAGE_SIZE = [10, 20, 50, 100, 200].includes(value) ? value : 10;
  DANMAKU_PAGE = 1;
  refreshDanmaku();
}
async function deleteDanmaku(id) {
  if (!await uiConfirm({ title: "删除弹幕记录", message: "确认删除这条弹幕记录吗？", okText: "删除", danger: true })) return;
  try { await api("/api/danmaku/" + id, { method: "DELETE" }); toast("弹幕记录已删除", "ok"); refreshDanmaku(); }
  catch (e) { toast("删除失败:" + e.message, "err"); }
}
async function clearDanmaku() {
  if (!await uiConfirm({ title: "清空弹幕", message: "清空所有弹幕记录?",
                         okText: "清空", danger: true })) return;
  try {
    const result = await api("/api/danmaku?platform=" + encodeURIComponent(PLATFORM), { method: "DELETE" });
    toast("已清空 " + result.deleted + " 条弹幕", "ok");
    refreshDanmaku(); refreshDanmakuWatches();
  } catch (e) { toast("清空失败:" + e.message, "err"); }
}

// ─── 评论监控(独立) ───
const SRC = { public: "公开", creator: "创作中心" };
async function addWatch() {
  const btn = evtBtn() || document.querySelector('button[onclick="addWatch()"]');
  if (btn?.disabled) return;
  await withBusy(btn, "识别中", async () => {
  const normalizedTarget = await normalizeWatchTarget(null, { quiet: true });
  if (normalizedTarget === null) return;
  const url_or_id = $("w-url").value.trim();
  if (!url_or_id) { toast("请粘贴视频链接 / 账号主页 / sec_uid", "err"); return; }
  if (["xhs", "tiktok"].includes(PLATFORM) && !$("w-acc").value) {
    const platformName = PF_NAME[PLATFORM] || "当前平台";
    const available = PLATFORM === "tiktok"
      ? ACCOUNTS.filter(a => a.status === "active" && a.has_storage)
      : ACCOUNTS;
    if (!available.length) { toast("请先在「账号」里完成" + platformName + "登录", "err"); switchTab("accounts"); return; }
    toast(platformName + "评论监控必须选择一个已登录账号", "err"); return;
  }
  $("w-msg").textContent = "解析中…";
  await withBusy(btn, "解析中", async () => {
    try {
      await api("/api/comment-watches", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          url_or_id, platform: PLATFORM, kind: $("w-kind").value,
          mode: PLATFORM === "douyin" ? $("w-mode").value : "public",
          account_id: PLATFORM === "shipinhao" ? null : ($("w-acc").value ? +$("w-acc").value : null),
          interval_seconds: monitorIntervalSeconds("w-interval"),
          recent_works: +$("w-recent").value,
          recent_days: +$("w-days").value,
          max_scrolls: +$("w-depth").value,
          alias: $("w-alias").value.trim(), group_name: getMetaValue("w-group").trim(),
          tags: parseTags(getMetaValue("w-tags")),
        }),
      });
      ["w-url", "w-alias"].forEach(id => $(id).value = "");
      setMetaValue("w-group", ""); setMetaValue("w-tags", "");
      resetWatchTargetState();
      $("w-msg").textContent = "已添加 ✓"; toast("已开始监控评论", "ok");
      globalThis.CreatorHubWorkbench?.completed?.("comments");
    } catch (e) { $("w-msg").textContent = "失败: " + e.message; toast("添加失败:" + e.message, "err"); }
  });
  refreshWatches();
  });
}
function watchRow(w) {
  const base = esc(watchBaseName(w));
  const isNotePlatform = w.platform === "xhs" || w.platform === "rednote";
  const kindLabel = w.kind === "video" ? (isNotePlatform ? "笔记" : "视频") : (isNotePlatform ? "创作者" : "账号");
  const srcLabel = isNotePlatform ? "公开" : (SRC[w.mode] || w.mode);
  return `<tr>
    <td><div class="user-cell">${w.avatar ? `<img class="avatar" src="${esc(safeMediaUrl(w.avatar))}" referrerpolicy="no-referrer">` : `<span class="wb-avatar-fallback" style="width:30px;height:30px;flex:0 0 30px;font-size:13px;border-radius:50%" aria-hidden="true">${esc((watchBaseName(w) || "评").slice(0, 1))}</span>`}<div><span title="${base}">${base}</span>${w.alias ? `<div class="alias-line" title="${esc(w.alias)}">${esc(w.alias)}</div>` : ""}</div></div></td>
    <td>${metaChips(w)}</td>
    <td>${kindLabel} · <span class="mut">${srcLabel}</span></td>
    <td class="num"><button type="button" class="ghost sm monitor-record-link" data-comment-records="${w.id}" onclick="showWatchRecords('comment',${w.id})">查看记录 <span>${fmtNum(w.comment_count || 0)}</span></button></td>
    <td class="num">${monitorIntervalText(w.interval_seconds)}
      ${w.kind === "user" && (w.recent_works || w.recent_days) ? `<div class="mut" style="font-size:11px">${w.recent_works ? `近 ${w.recent_works} 个` : "全局作品数"} · ${w.recent_days ? `${w.recent_days} 天` : "全局天数"}</div>` : ""}</td>
    <td class="mut">${w.last_scan_at ? new Date(w.last_scan_at + "Z").toLocaleString() : "—"}${w.last_error ? ` <span class="warn-ic" title="${esc(w.last_error)}">${ic("i-info")}</span>` : ""}${autoRunHint(w.next_auto_run_at)}</td>
    <td><span class="pill ${w.enabled ? "active" : "paused"}">${w.enabled ? "监控中" : "已暂停"}</span></td>
    <td class="acttd">
      <button class="ghost sm" onclick="scanWatch(${w.id})">立即抓取</button>
      <button class="ghost sm" onclick="editWatchMeta(${w.id})">编辑</button>
      <button class="ghost sm" onclick="toggleWatch(${w.id}, ${!w.enabled})">${w.enabled ? "暂停" : "启用"}</button>
      <button class="ghost sm danger" onclick="delWatch(${w.id})">${ic("i-trash")}删除</button>
    </td></tr>`;
}
function renderWatchRows() {
  const groupName = $("watch-group") ? $("watch-group").value : "";
  const tag = $("watch-tag") ? $("watch-tag").value : "";
  const status = $("watch-status") ? $("watch-status").value : "";
  const query = (($("watch-search") && $("watch-search").value) || "").trim().toLocaleLowerCase();
  const rows = WATCHES.filter(w => {
    if (!matchesMeta(w, groupName, tag)) return false;
    if (status === "enabled" && !w.enabled) return false;
    if (status === "paused" && w.enabled) return false;
    if (status === "error" && !w.last_error) return false;
    if (!query) return true;
    return [watchBaseName(w), w.alias, w.group_name, ...itemTags(w)]
      .join(" ").toLocaleLowerCase().includes(query);
  });
  if ($("watch-filter-count")) $("watch-filter-count").textContent = `显示 ${rows.length} / ${WATCHES.length}`;
  $("watch-table").innerHTML = rows.map(watchRow).join("")
    || empty(8, "没有匹配的评论监控", "i-msg", WATCHES.length ? "调整分组、标签或搜索条件" : "添加作品或账号，开始收集新评论。", WATCHES.length ? "" : "comments");
}
async function refreshWatches() {
  const isCurrent = beginViewRequest("watches");
  const ws = await api("/api/comment-watches?platform=" + PLATFORM);
  if (!isCurrent()) return;
  WATCHES = ws; populateWatchFacets(); populateCommentSrc();
  if ($("tb-watch")) $("tb-watch").textContent = ws.length;
  renderWatchRows();
}
async function editWatchMeta(id) {
  const item = watchById(id); if (!item) return;
  const accounts = ACCOUNTS.filter(a => a.platform === item.platform && a.status !== "invalid");
  const canCreator = item.platform === "douyin" && item.kind === "user";
  const accountOptions = [
    `<option value="">${item.account_id ? "保持当前绑定" : "不指定账号"}</option>`,
    ...accounts.map(a => `<option value="${a.id}">${esc(a.nickname)}${a.has_creator ? " · 创作号" : ""}</option>`),
  ].join("");
  const intervalOptions = monitorIntervalOptions(item.interval_seconds || 600);
  const recentOptions = numericSelectOptions(item.recent_works || 0, [
    [0, "跟随全局设置"], [3, "最近 3 个作品"], [5, "最近 5 个作品"],
    [10, "最近 10 个作品"], [20, "最近 20 个作品"], [50, "最近 50 个作品"],
  ]);
  const dayOptions = numericSelectOptions(item.recent_days || 0, [
    [0, "跟随全局设置"], [3, "最近 3 天"], [7, "最近 7 天"],
    [14, "最近 14 天"], [30, "最近 30 天"], [90, "最近 90 天"],
  ]);
  const depthOptions = numericSelectOptions(item.max_scrolls || 0, [
    [0, "跟随全局设置"], [3, "浅层抓取"], [6, "标准抓取"],
    [12, "深度抓取"], [20, "最大抓取"],
  ]);
  const value = await new Promise(res => {
    _uiResolve = res; _uiCancelVal = null;
    _uiGetVal = () => ({
      alias: $("ew-alias").value.trim(),
      group_name: getMetaValue("ew-group").trim(),
      tags: parseTags(getMetaValue("ew-tags")),
      interval_seconds: monitorIntervalSeconds("ew-interval"),
      account_id: item.platform === "shipinhao"
        ? null
        : ($("ew-account") && $("ew-account").value ? +$("ew-account").value : null),
      mode: $("ew-mode").value,
      recent_works: $("ew-recent") ? +$("ew-recent").value : item.recent_works || 0,
      recent_days: $("ew-days") ? +$("ew-days").value : item.recent_days || 0,
      max_scrolls: $("ew-depth") ? +$("ew-depth").value : item.max_scrolls || 0,
    });
    $("ui-body").innerHTML = `
      <fieldset class="monitor-config-group">
        <legend>标识与归类</legend>
        <div><label class="field" for="ew-alias">管理别名</label>
          <input id="ew-alias" maxlength="60" value="${esc(item.alias || "")}" placeholder="便于快速识别"></div>
        <div class="row">
          <div><label class="field" for="ew-group">分组</label><input id="ew-group" data-meta-combo="group"></div>
          <div><label class="field" for="ew-tags">标签</label><input id="ew-tags" data-meta-combo="tags"></div>
        </div>
      </fieldset>
      <fieldset class="monitor-config-group">
        <legend>抓取策略</legend>
        <div class="row">
          <div><label class="field" for="ew-interval">抓取频率</label>
            <select id="ew-interval">${intervalOptions}</select></div>
          ${item.platform === "shipinhao"
            ? ""
            : `<div><label class="field" for="ew-account">抓取账号</label><select id="ew-account">${accountOptions}</select></div>`}
        </div>
        <div><label class="field" for="ew-mode">评论来源</label>
          <select id="ew-mode"><option value="public">公开评论区</option>${canCreator ? '<option value="creator">创作中心（仅自有账号）</option>' : ""}</select></div>
        ${item.kind === "user" ? `<div class="row">
          <div><label class="field" for="ew-recent">检查近期作品数</label><select id="ew-recent">${recentOptions}</select></div>
          <div><label class="field" for="ew-days">作品时间范围</label><select id="ew-days">${dayOptions}</select></div>
        </div>` : ""}
        ${item.platform === "xhs" ? "" : `<div><label class="field" for="ew-depth">评论区抓取深度</label>
          <select id="ew-depth">${depthOptions}</select></div>`}
      </fieldset>`;
    enhanceMetaControl($("ew-group"), "group"); enhanceMetaControl($("ew-tags"), "tags");
    setMetaValue("ew-group", item.group_name || ""); setMetaValue("ew-tags", itemTags(item).join(","));
    setupMonitorInterval("ew-interval", item.interval_seconds || 600);
    if ($("ew-account")) $("ew-account").value = item.account_id ? String(item.account_id) : "";
    $("ew-mode").value = canCreator ? (item.mode || "public") : "public";
    if ($("ew-recent")) $("ew-recent").value = String(item.recent_works || 0);
    if ($("ew-days")) $("ew-days").value = String(item.recent_days || 0);
    if ($("ew-depth")) $("ew-depth").value = String(item.max_scrolls || 0);
    ["ew-interval", "ew-account", "ew-mode", "ew-recent", "ew-days", "ew-depth"]
      .forEach(key => { const el = $(key); if (el) enhanceSelect(el); });
    _uiOpen("编辑评论监控", "监控对象保持不变。更换作品或创作者，请新建评论监控。", {
      okText: "保存修改", wide: true, icon: "i-msg",
      submit: value => api("/api/comment-watches/" + id, {
        method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(value),
      }),
    });
  });
  if (value === null) return;
  toast("评论监控配置已更新", "ok"); refreshWatches(); refreshComments();
}
async function scanWatch(id) {
  const btn = evtBtn();
  toast("抓取中…正在拉取评论区", "info", 7000);
  await withBusy(btn, "抓取中", async () => {
    try { const r = await api("/api/comment-watches/" + id + "/scan-now", { method: "POST" }); toast(`评论抓取完成,新增 ${r.new_comments ?? 0} 条`, "ok"); }
    catch (e) { toast("抓取失败:" + e.message, "err"); }
  });
  refreshWatches(); refreshComments();
}
async function toggleWatch(id, on) { try { await api("/api/comment-watches/" + id, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ enabled: on }) }); refreshWatches(); } catch (e) { toast("操作失败:" + e.message, "err"); } }
async function delWatch(id) {
  if (!await uiConfirm({ title: "删除评论监控", message: "仅删除该监控配置，已抓取的评论记录会保留，并标注原任务已删除。", okText: "删除", danger: true })) return;
  try {
    await api("/api/comment-watches/" + id + "?with_comments=false", { method: "DELETE" });
    toast("监控已删除，评论记录已保留", "ok"); await refreshWatches(); await refreshComments(true);
  } catch (e) { toast("删除失败:" + e.message, "err"); }
}

function renderCommentPager(meta) {
  const pager = $("comment-pager");
  if (!pager) return;
  const total = Math.max(0, Number(meta && meta.total || 0));
  const pageSize = Math.max(1, Number(meta && meta.page_size || COMMENT_PAGE_SIZE));
  const pages = Math.max(1, Number(meta && meta.pages || Math.ceil(total / pageSize) || 1));
  const page = Math.max(1, Number(meta && meta.page || COMMENT_PAGE));
  COMMENT_TOTAL = total;
  COMMENT_PAGE_SIZE = pageSize;
  COMMENT_PAGE = page;
  if ($("comment-page-size")) $("comment-page-size").value = String(pageSize);
  if ($("comment-page-input")) {
    $("comment-page-input").value = String(page);
    $("comment-page-input").max = String(pages);
  }
  if ($("comment-page-info")) $("comment-page-info").textContent =
    "第 " + page + " / " + pages + " 页 · 共 " + fmtNum(total) + " 条";
  if ($("comment-first")) $("comment-first").disabled = page <= 1;
  if ($("comment-prev")) $("comment-prev").disabled = page <= 1;
  if ($("comment-next")) $("comment-next").disabled = page >= pages;
  if ($("comment-last")) $("comment-last").disabled = page >= pages;
  pager.hidden = total <= pageSize;
}
function commentPageCount() {
  return Math.max(1, Math.ceil(COMMENT_TOTAL / COMMENT_PAGE_SIZE));
}
function goCommentPage(page) {
  const pages = commentPageCount();
  const target = page <= 0 ? pages : Math.min(pages, Math.max(1, Math.round(Number(page) || 1)));
  if (target === COMMENT_PAGE) return;
  COMMENT_PAGE = target;
  refreshComments();
}
function changeCommentPage(delta) { goCommentPage(COMMENT_PAGE + Number(delta || 0)); }
function jumpCommentPage() {
  const input = $("comment-page-input");
  const value = input ? Number(input.value) : 1;
  if (!Number.isFinite(value) || value < 1) {
    if (input) { input.value = String(COMMENT_PAGE); input.focus(); }
    return;
  }
  goCommentPage(value);
}
function handleCommentPageInput(event) {
  if (event && event.key === "Enter") { event.preventDefault(); jumpCommentPage(); }
}
function setCommentPageSize() {
  const value = +(($('comment-page-size') && $('comment-page-size').value) || 10);
  COMMENT_PAGE_SIZE = [10, 20, 50, 100, 200].includes(value) ? value : 10;
  COMMENT_PAGE = 1;
  refreshComments();
}
async function refreshComments(resetPage = false) {
  const isCurrent = beginViewRequest("comments");
  const load = prepareWatchRecordLoad("comment", resetPage); if (!load) return;
  const params = new URLSearchParams({
    platform: PLATFORM, page: String(COMMENT_PAGE),
    page_size: String(COMMENT_PAGE_SIZE), paginate: "true",
  });
  if (COMMENT_SRC) params.set("watch_id", COMMENT_SRC);
  for (const [key, value] of Object.entries(load.bounds)) params.set(key, value);
  if (COMMENT_GROUP) params.set("group_name", COMMENT_GROUP);
  if (COMMENT_TAG) params.set("tag", COMMENT_TAG);
  const query = (($('comment-query') && $('comment-query').value) || "").trim();
  const ipLabel = (($('comment-ip-label') && $('comment-ip-label').value) || "").trim();
  const replyType = ($('comment-type') && $('comment-type').value) || "";
  const minLikes = +(($('comment-min-likes') && $('comment-min-likes').value) || 0);
  if (query) params.set("q", query);
  if (ipLabel) params.set("ip_label", ipLabel);
  if (replyType) params.set("reply_type", replyType);
  if (Number.isFinite(minLikes) && minLikes > 0) params.set("min_like_count", String(Math.floor(minLikes)));
  params.set("sort", ($('comment-sort') && $('comment-sort').value) || "captured_desc");
  let payload;
  try { payload = await api("/api/comments?" + params.toString()); }
  catch (e) { if (isCurrent()) watchRecordLoadError("comment", e, load.clear); return; }
  if (!isCurrent()) return;
  const meta = Array.isArray(payload)
    ? { items: payload, total: payload.length, page: 1, page_size: COMMENT_PAGE_SIZE,
        pages: Math.max(1, Math.ceil(payload.length / COMMENT_PAGE_SIZE)) }
    : (payload || {});
  const pages = Math.max(1, Number(meta.pages || 1));
  if (COMMENT_PAGE > pages) { COMMENT_PAGE = pages; return refreshComments(); }
  const rows = Array.isArray(meta.items) ? meta.items : [];
  cacheWatchRecordSources("comment", meta, rows);
  if ($("comment-filter-count")) $("comment-filter-count").textContent =
    `显示 ${rows.length} / ${Number(meta.total || rows.length)}`;
  $("comment-table").innerHTML = rows.map(r => {
    return `<tr>
    <td class="watch-record-check"><label class="watch-record-selection" title="选择这条评论"><input type="checkbox" data-id="${r.id}" aria-label="选择这条评论" onchange="commentToggleOne(${r.id}, this.checked)" ${selComment.has(r.id) ? "checked" : ""}></label></td>
    <td class="watch-record-main"><div class="watch-record-text">${r.is_reply ? '<span class="mut">回复 · </span>' : ""}${esc(r.text || "")}</div>${watchRecordSourceMarkup("comment", r)}</td>
    <td class="mut comment-user watch-record-user" data-label="用户"><div>${esc(r.user_nickname || "")}${r.ip_label ? ` · ${esc(r.ip_label)}` : ""}</div>${r.user_sec_uid ? `<div class="comment-user-sec" title="${esc(r.user_sec_uid)}">sec_uid: ${esc(r.user_sec_uid)}</div>` : ""}</td>
    <td class="mut num watch-record-likes" data-label="赞">${fmtNum(r.like_count)}</td>
    <td class="watch-record-time">${watchRecordTimeMarkup("comment", r)}</td>
    <td class="acttd watch-record-actions"><button class="ghost sm danger" onclick="delComment(${r.id})">${ic("i-trash")}删除</button></td>
  </tr>`;
  }).join("") || empty(6, "暂无评论", "i-msg", "添加评论监控后,抓到的新评论会显示在这里,并可推送通知");
  updateCommentSelBar(); renderCommentPager(meta);
}
async function delComment(id) {
  if (!await uiConfirm({ title: "删除评论记录", message: "确认删除这条评论记录吗？", okText: "删除", danger: true })) return;
  try { await api("/api/comments/" + id, { method: "DELETE" }); toast("评论记录已删除", "ok"); selComment.delete(id); refreshComments(); }
  catch (e) { toast("删除失败:" + e.message, "err"); }
}
async function clearComments() {
  if (!await uiConfirm({ title: "清空评论", message: "清空所有评论记录?", okText: "清空", danger: true })) return;
  try { const r = await api("/api/comments", { method: "DELETE" }); toast(`已清空 ${r.deleted} 条评论`, "ok"); refreshComments(); }
  catch (e) { toast("清空失败:" + e.message, "err"); }
}

// ─── 预览 lightbox(图集左右翻动)───
let PV_N = 0, PV_I = 0, PV_REQ = 0;
function _pvRender(d) {
  const box = $("pv-media"), cap = $("pv-cap");
  const vid = (d.medias || []).find(m => m.kind === "video");
  if (d.media_type === "video" && (d.local_url || vid)) {
    const videoUrl = d.local_url || vid.url;
    box.innerHTML = `<video src="${esc(videoUrl)}" controls autoplay playsinline preload="metadata" poster="${esc(safeMediaUrl(d.cover_url || ""))}" referrerpolicy="no-referrer"></video>`;
    const video = box.querySelector("video");
    let triedRemote = !d.local_url || !vid || !vid.url;
    video.addEventListener("error", () => {
      if (video !== box.querySelector("video")) return;
      if (!triedRemote) {
        triedRemote = true;
        video.src = vid.url;
        video.load();
        return;
      }
      const reason = d.local_url && vid && vid.url
        ? "本地文件和原始链接均不可用"
        : (d.local_url ? "请检查本地文件是否完整" : "原始视频链接可能已失效");
      box.innerHTML = `<div class="pv-loading">视频加载失败,${reason}</div>`;
    });
  } else {
    const imgs = (d.medias || []).filter(m => m.kind === "image");
    const list = imgs.length ? imgs : (d.cover_url ? [{ url: d.cover_url }] : []);
    if (!list.length) {
      box.innerHTML = `<div class="pv-loading">暂无可预览的媒体</div>`;
    } else {
      PV_N = list.length; PV_I = 0;
      const slides = list.map(m => `<div class="pv-slide"><img src="${esc(safeMediaUrl(m.url))}" referrerpolicy="no-referrer" alt=""></div>`).join("");
      const nav = PV_N > 1 ? `
        <button class="pv-arrow left" id="pv-prev" onclick="pvNav(-1)" aria-label="上一张">${ic("i-prev")}</button>
        <button class="pv-arrow right" id="pv-next" onclick="pvNav(1)" aria-label="下一张">${ic("i-next")}</button>
        <div class="pv-counter" id="pv-counter"></div>` : "";
      box.innerHTML = `<div class="pv-carousel"><div class="pv-track" id="pv-track">${slides}</div>${nav}</div>`;
      _pvBindSwipe();
      pvUpdate();
    }
  }
  cap.textContent = d.desc || "";
}
async function _pvOpen(fetcher, startIdx) {
  const ov = $("preview"), box = $("pv-media");
  const req = ++PV_REQ;
  PV_N = 0; PV_I = 0;
  box.innerHTML = `<div class="pv-loading">加载中…</div>`; $("pv-cap").textContent = "";
  ov.style.display = "flex";
  modalOpened(ov);
  setTimeout(() => ov.querySelector(".pv-close").focus(), 0);
  try {
    const data = await fetcher();
    if (req !== PV_REQ) return;
    _pvRender(data);
    if (startIdx && PV_N > 1) { PV_I = Math.max(0, Math.min(startIdx, PV_N - 1)); pvUpdate(); }
  }
  catch (e) {
    if (req === PV_REQ) box.innerHTML = `<div class="pv-loading">预览失败:${esc(e.message)}</div>`;
  }
}
function openPreview(id, startIdx) {
  return _pvOpen(() => api("/api/contents/" + id + "/media"), startIdx || 0);
}
function openPubPreview(accId, noteId, tok, src) {
  return _pvOpen(() => api(`/api/publish/note-media?account_id=${accId}&note_id=${encodeURIComponent(noteId)}&xsec_token=${encodeURIComponent(tok || "")}&xsec_source=${encodeURIComponent(src || "")}`));
}
async function openPubComments(accId, noteId, tok, src) {
  const ov = $("preview"), box = $("pv-media"), cap = $("pv-cap");
  const req = ++PV_REQ;
  PV_N = 0; PV_I = 0;
  box.innerHTML = `<div class="pv-loading">加载评论…</div>`; cap.textContent = ""; ov.style.display = "flex";
  modalOpened(ov);
  setTimeout(() => ov.querySelector(".pv-close").focus(), 0);
  try {
    const d = await api(`/api/publish/note-comments?account_id=${accId}&note_id=${encodeURIComponent(noteId)}&xsec_token=${encodeURIComponent(tok || "")}&xsec_source=${encodeURIComponent(src || "")}`);
    if (req !== PV_REQ) return;
    cap.textContent = `共 ${d.total} 条评论` + (d.has_more ? "(仅首页)" : "");
    box.innerHTML = `<div class="cmt-wrap">` + ((d.comments || []).map(c => `
      <div class="cmt-item">
        <div class="cmt-head"><b>${esc(c.user_nickname || "用户")}</b><span class="like">${ic("i-heart")}${fmtNum(c.like_count)}</span></div>
        <div class="cmt-text">${c.is_reply ? '<span class="mut">↳ </span>' : ""}${esc(c.text || "")}</div>
        <div class="cmt-time">${fmtTime(c.create_time)}</div>
      </div>`).join("") || `<div class="pv-loading">暂无评论</div>`) + `</div>`;
  } catch (e) {
    if (req === PV_REQ) box.innerHTML = `<div class="pv-loading">加载失败:${esc(e.message)}</div>`;
  }
}
function pvUpdate() {
  const tr = $("pv-track"); if (!tr) return;
  tr.style.transform = `translateX(-${PV_I * 100}%)`;
  const c = $("pv-counter"); if (c) c.textContent = `${PV_I + 1} / ${PV_N}`;
  const p = $("pv-prev"), n = $("pv-next");
  if (p) p.disabled = PV_I <= 0;
  if (n) n.disabled = PV_I >= PV_N - 1;
}
function pvNav(delta) {
  if (!PV_N) return;
  PV_I = Math.max(0, Math.min(PV_N - 1, PV_I + delta));
  pvUpdate();
}
function _pvBindSwipe() {
  const tr = $("pv-track"); if (!tr) return;
  let x0 = null;
  tr.addEventListener("touchstart", e => { x0 = e.touches[0].clientX; }, { passive: true });
  tr.addEventListener("touchend", e => {
    if (x0 === null) return;
    const dx = e.changedTouches[0].clientX - x0;
    if (Math.abs(dx) > 40) pvNav(dx < 0 ? 1 : -1);
    x0 = null;
  }, { passive: true });
}
function hidePreview() {
  PV_REQ++;
  const v = $("pv-media").querySelector("video"); if (v) { try { v.pause(); } catch (e) {} }
  $("preview").style.display = "none"; $("pv-media").innerHTML = ""; $("pv-cap").textContent = "";
  PV_N = 0; PV_I = 0;
  modalClosed($("preview"));
}
document.addEventListener("keydown", e => {
  const modal = _visibleModal();
  if (!modal) return;
  if (e.key === "Tab") {
    const items = _modalFocusables(modal);
    if (!items.length) { e.preventDefault(); modal.focus(); return; }
    const first = items[0], last = items[items.length - 1];
    if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    return;
  }
  if (modal === $("uimodal")) return; // 通用模态由 _uiKey 处理确认与取消
  if (e.key === "Escape") {
    e.preventDefault();
    if (modal === $("repost")) hideRepost();
    else if (modal === $("wcmodal")) hideWorkComments();
    else if (modal === $("collection-comments-modal")) hideCollectionComments();
    else if (modal === $("risk-event-modal")) hideRiskEvents();
    else if (modal === $("preview")) hidePreview();
    return;
  }
  if (modal === $("preview") && e.key === "ArrowLeft") pvNav(-1);
  else if (modal === $("preview") && e.key === "ArrowRight") pvNav(1);
});
