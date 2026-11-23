// 13-monitors-contents.js — 作品监控目标管理、已捕获内容库检索、批量操作与本地文件定位。
async function addMonitor() {
  const btn = evtBtn() || document.querySelector('button[onclick="addMonitor()"]');
  if (btn?.disabled) return;
  await withBusy(btn, "识别中", async () => {
  const target_kind = (["xhs", "rednote"].includes(PLATFORM) && $("t-kind")) ? $("t-kind").value : "creator";
  const normalizedTarget = target_kind === "keyword"
    ? $("t-url").value.trim()
    : await normalizeMonitorTarget(null, { quiet: true });
  if (normalizedTarget === null) return;
  const url_or_secuid = $("t-url").value.trim();
  if (!url_or_secuid) { toast(target_kind === "keyword" ? "请输入搜索关键词" : "请输入主页链接 / 短链 / id", "err"); return; }
  if (["xhs", "rednote", "douyin", "tiktok"].includes(PLATFORM) && !$("t-acc").value) {
    const platformName = PF_NAME[PLATFORM] || "当前平台";
    const available = PLATFORM === "tiktok"
      ? ACCOUNTS.filter(a => a.status === "active" && a.has_storage)
      : ACCOUNTS;
    if (!available.length) { toast(`请先在「账号」里完成${platformName}扫码登录`, "err"); switchTab("accounts"); return; }
    toast(`${platformName}监控必须选择一个已登录账号`, "err"); return;
  }
  const downloadMode = $("t-download").value;
  $("add-msg").textContent = "解析中…";
  await withBusy(btn, "解析中", async () => {
    try {
      await api("/api/monitors", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          url_or_secuid, platform: PLATFORM, target_kind,
          account_id: PLATFORM === "shipinhao" ? null : ($("t-acc").value ? +$("t-acc").value : null),
          interval_seconds: monitorIntervalSeconds("t-interval"),
          initial_backfill_count: !["xhs", "rednote"].includes(PLATFORM)
            ? ($("t-backfill").value === "" ? null : +$("t-backfill").value) : 0,
          download_dir: $("t-dir").value.trim(),
          video_quality: ["xhs", "rednote"].includes(PLATFORM) ? "" : $("t-quality").value,
          download_enabled: downloadMode !== "none",
          media_filter: downloadMode === "none" ? "all" : downloadMode,
          max_scrolls: +$("t-max-scrolls").value,
          max_items_per_scan: +$("t-max-items").value,
          record_media_filter: $("t-record-media").value,
          recent_days: Math.max(0, +$("t-recent-days").value || 0),
          min_like_count: Math.max(0, +$("t-min-likes").value || 0),
          min_comment_count: Math.max(0, +$("t-min-comments").value || 0),
          include_keywords: parseDanmakuKeywords($("t-include-keywords").value),
          exclude_keywords: parseDanmakuKeywords($("t-exclude-keywords").value),
          alias: $("t-alias").value.trim(), group_name: getMetaValue("t-group").trim(),
          tags: parseTags(getMetaValue("t-tags")),
        }),
      });
      ["t-url", "t-dir", "t-alias"].forEach(id => $(id).value = "");
      setMetaValue("t-group", ""); setMetaValue("t-tags", "");
      $("add-msg").textContent = "已添加 ✓";
      toast("已开始监控", "ok");
      globalThis.CreatorHubWorkbench?.completed?.("monitors");
    } catch (e) { $("add-msg").textContent = "失败: " + e.message; toast("添加失败:" + e.message, "err"); }
  });
  refreshMonitors();
  });
}
function numericSelectOptions(current, choices, unit = "") {
  const values = choices.map(([value]) => String(value));
  const rows = values.includes(String(current)) || current == null
    ? choices : [[current, `${current}${unit}（当前）`], ...choices];
  return rows.map(([value, label]) =>
    `<option value="${value}">${esc(label)}</option>`).join("");
}
function monitorIntervalText(seconds) {
  let left = Number(seconds);
  if (!Number.isFinite(left) || left <= 0) return "—";
  const parts = [];
  for (const [size, unit] of [[86400, "天"], [3600, "小时"], [60, "分钟"]]) {
    const count = Math.floor(left / size);
    if (count) parts.push(`${count} ${unit}`);
    left %= size;
  }
  if (left) parts.push(`${left} 秒`);
  return parts.join(" ");
}
function monitorIntervalOptions(current, allowGlobal = false) {
  const choices = [1, 5, 10, 15, 30, 60, 300, 600, 1800, 3600, 21600, 86400]
    .map(seconds => [seconds, `每 ${monitorIntervalText(seconds)}`]);
  if (allowGlobal) choices.unshift([0, "跟随全局设置"]);
  const selected = choices.some(([seconds]) => seconds === Number(current)) ? String(current) : "custom";
  choices.push(["custom", "自定义…"]);
  return choices.map(([value, label]) =>
    `<option value="${value}"${String(value) === selected ? " selected" : ""}>${esc(label)}</option>`).join("");
}
function setupMonitorInterval(id, seconds, allowGlobal = false) {
  const select = $(id);
  select.innerHTML = monitorIntervalOptions(seconds, allowGlobal);
  select.dataset.allowGlobalInterval = String(allowGlobal);
  select.insertAdjacentHTML("afterend", `<div id="${id}-custom" class="monitor-interval-custom" hidden>
    <div class="form-field"><label for="${id}-amount">间隔数值</label>
      <input id="${id}-amount" type="number" inputmode="decimal" required aria-describedby="${id}-custom-help"></div>
    <div class="form-field"><label for="${id}-unit">时间单位</label>
      <select id="${id}-unit"><option value="1">秒</option><option value="60">分钟</option></select></div>
    <p id="${id}-custom-help" class="field-help">范围 1–86400 秒；分钟支持小数，换算后须为整秒。</p>
  </div>`);
  const amount = $(id + "-amount"), unit = $(id + "-unit"), custom = $(id + "-custom");
  const initial = seconds > 0 ? seconds : 300;
  let previousScale = initial % 60 === 0 ? 60 : 1;
  unit.value = String(previousScale); amount.value = String(initial / previousScale);
  const sync = () => {
    const active = select.value === "custom", scale = Number(unit.value);
    custom.hidden = !active; amount.disabled = unit.disabled = !active;
    amount.min = String(1 / scale); amount.max = String(86400 / scale);
    amount.step = scale === 1 ? "1" : "any";
    previousScale = scale;
    setFieldError(amount, "");
    unit._csSync?.(); select._csSync?.();
  };
  select._monitorIntervalSync = sync;
  select.addEventListener("change", sync);
  unit.addEventListener("change", () => {
    if (amount.value.trim() && Number.isFinite(Number(amount.value))) {
      amount.value = String(Number(amount.value) * previousScale / Number(unit.value));
    }
    sync();
  });
  amount.addEventListener("input", () => setFieldError(amount, ""));
  sync();
}
function monitorIntervalSeconds(id) {
  const select = $(id), custom = select.value === "custom";
  const input = custom ? $(id + "-amount") : select;
  const raw = input.value.trim();
  const scale = custom ? Number($(id + "-unit").value) : 1;
  const seconds = Number(raw) * scale, rounded = Math.round(seconds);
  if (!custom && raw === "0" && select.dataset.allowGlobalInterval === "true") return 0;
  if (!raw || ![1, 60].includes(scale) || !Number.isFinite(seconds)
      || rounded < 1 || rounded > 86400 || Math.abs(seconds - rounded) > 1e-6) {
    const message = "请输入 1–86400 秒的间隔，换算后须为整秒";
    setFieldError(input, message); input.focus();
    uiEditorError(message, input.id);
  }
  setFieldError(input, "");
  return rounded;
}
async function editMonitor(id) {
  const item = monitorById(id); if (!item) return;
  const accounts = ACCOUNTS.filter(a => a.platform === item.platform && a.status !== "invalid");
  const accountOptions = [
    `<option value="">${item.account_id ? "保持当前绑定" : "不指定账号"}</option>`,
    ...accounts.map(a => `<option value="${a.id}">${esc(a.nickname)}${a.has_creator ? " · 创作号" : ""}</option>`),
  ].join("");
  const intervalOptions = monitorIntervalOptions(item.interval_seconds || 300);
  const backfillOptions = numericSelectOptions(item.initial_backfill_count ?? 0, [
    [0, "不回填历史"], [5, "最近 5 条"], [20, "最近 20 条"], [-1, "尽可能全量"],
  ], " 条");
  const depthOptions = numericSelectOptions(item.max_scrolls ?? 0, [
    [0, "平台默认"], [3, "3 次下滑"], [6, "6 次下滑"],
    [12, "12 次下滑"], [20, "20 次下滑"], [30, "30 次下滑"],
  ]);
  const itemLimitOptions = numericSelectOptions(item.max_items_per_scan ?? 0, [
    [0, "平台默认"], [5, "5 条"], [12, "12 条"], [20, "20 条"],
    [50, "50 条"], [100, "100 条"],
  ]);
  const value = await new Promise(res => {
    _uiResolve = res; _uiCancelVal = null;
    _uiGetVal = () => {
      const downloadMode = $("em-download").value;
      const result = {
        alias: $("em-alias").value.trim(),
        group_name: getMetaValue("em-group").trim(),
        tags: parseTags(getMetaValue("em-tags")),
        interval_seconds: monitorIntervalSeconds("em-interval"),
        account_id: item.platform === "shipinhao"
          ? null
          : ($("em-account") && $("em-account").value ? +$("em-account").value : null),
        download_dir: $("em-dir").value.trim(),
        video_quality: $("em-quality") ? $("em-quality").value : "",
        download_enabled: downloadMode !== "none",
        media_filter: downloadMode === "none" ? "all" : downloadMode,
        max_scrolls: +$("em-max-scrolls").value,
        max_items_per_scan: +$("em-max-items").value,
        record_media_filter: $("em-record-media").value,
        recent_days: Math.max(0, +$("em-recent-days").value || 0),
        min_like_count: Math.max(0, +$("em-min-likes").value || 0),
        min_comment_count: Math.max(0, +$("em-min-comments").value || 0),
        include_keywords: parseDanmakuKeywords($("em-include-keywords").value),
        exclude_keywords: parseDanmakuKeywords($("em-exclude-keywords").value),
      };
      if ($("em-backfill")) result.initial_backfill_count = +$("em-backfill").value;
      return result;
    };
    $("ui-body").innerHTML = `
      <fieldset class="monitor-config-group">
        <legend>标识与归类</legend>
        <div><label class="field" for="em-alias">管理别名</label>
          <input id="em-alias" maxlength="60" value="${esc(item.alias || "")}" placeholder="便于快速识别"></div>
        <div class="row">
          <div><label class="field" for="em-group">分组</label><input id="em-group" data-meta-combo="group"></div>
          <div><label class="field" for="em-tags">标签</label><input id="em-tags" data-meta-combo="tags"></div>
        </div>
      </fieldset>
      <fieldset class="monitor-config-group">
        <legend>抓取策略</legend>
        <div class="row">
          <div><label class="field" for="em-interval">抓取频率</label>
            <select id="em-interval">${intervalOptions}</select></div>
          ${item.platform === "shipinhao"
            ? ""
            : `<div><label class="field" for="em-account">抓取账号</label><select id="em-account">${accountOptions}</select></div>`}
        </div>
        ${(item.platform !== "xhs" && (!item.last_scan_at || !(item.content_count > 0))) ? `<div><label class="field" for="em-backfill">首次历史回填</label>
          <select id="em-backfill">${backfillOptions}</select></div>` : ""}
        <div class="form-grid cols-4">
          <div><label class="field" for="em-max-scrolls">抓取深度</label><select id="em-max-scrolls">${depthOptions}</select></div>
          <div><label class="field" for="em-max-items">每轮作品上限</label><select id="em-max-items">${itemLimitOptions}</select></div>
          <div><label class="field" for="em-record-media">作品类型</label><select id="em-record-media"><option value="all">全部入库</option><option value="video">仅视频</option><option value="images">仅图文/图集</option></select></div>
          <div><label class="field" for="em-recent-days">发布时间范围</label><input id="em-recent-days" type="number" min="0" max="3650" inputmode="numeric"><div class="field-help">最近 N 天；0 表示不限</div></div>
        </div>
        <div class="form-grid cols-4">
          <div><label class="field" for="em-min-likes">最低点赞数</label><input id="em-min-likes" type="number" min="0" max="1000000000" inputmode="numeric"></div>
          <div><label class="field" for="em-min-comments">最低评论数</label><input id="em-min-comments" type="number" min="0" max="1000000000" inputmode="numeric"></div>
          <div><label class="field" for="em-include-keywords">必须包含</label><input id="em-include-keywords" maxlength="320" placeholder="多个词用逗号分隔"></div>
          <div><label class="field" for="em-exclude-keywords">排除关键词</label><input id="em-exclude-keywords" maxlength="320" placeholder="多个词用逗号分隔"></div>
        </div>
        <div class="field-help">筛选决定是否入库；抓取深度越高，单轮耗时和账号访问频率越高。</div>
      </fieldset>
      <fieldset class="monitor-config-group">
        <legend>记录与下载</legend>
        <div class="row">
          <div><label class="field" for="em-download">自动下载范围</label>
            <select id="em-download"><option value="all">全部作品</option><option value="video">仅视频</option><option value="images">仅图集</option><option value="none">仅记录，不下载</option></select></div>
          ${item.platform === "xhs" ? "" : `<div><label class="field" for="em-quality">视频画质</label>
            <select id="em-quality"><option value="">跟随全局默认</option><option value="highest">原画/最高</option><option value="1080">1080P</option><option value="720">720P</option><option value="540">540P</option><option value="lowest">最低省流</option></select></div>`}
        </div>
        <div><label class="field" for="em-dir">下载目录</label>
          <input id="em-dir" value="${esc(item.download_dir || "")}" placeholder="留空跟随全局默认"></div>
      </fieldset>`;
    enhanceMetaControl($("em-group"), "group"); enhanceMetaControl($("em-tags"), "tags");
    setMetaValue("em-group", item.group_name || ""); setMetaValue("em-tags", itemTags(item).join(","));
    setupMonitorInterval("em-interval", item.interval_seconds || 300);
    if ($("em-account")) $("em-account").value = item.account_id ? String(item.account_id) : "";
    if ($("em-backfill")) $("em-backfill").value = String(item.initial_backfill_count ?? 0);
    if ($("em-quality")) $("em-quality").value = item.video_quality || "";
    $("em-download").value = item.download_enabled === false ? "none" : (item.media_filter || "all");
    $("em-max-scrolls").value = String(item.max_scrolls ?? 0);
    $("em-max-items").value = String(item.max_items_per_scan ?? 0);
    $("em-record-media").value = item.record_media_filter || "all";
    $("em-recent-days").value = String(item.recent_days || 0);
    $("em-min-likes").value = String(item.min_like_count || 0);
    $("em-min-comments").value = String(item.min_comment_count || 0);
    $("em-include-keywords").value = (item.include_keywords || []).join(", ");
    $("em-exclude-keywords").value = (item.exclude_keywords || []).join(", ");
    ["em-interval", "em-account", "em-backfill", "em-quality", "em-download",
      "em-max-scrolls", "em-max-items", "em-record-media"]
      .forEach(key => { const el = $(key); if (el) enhanceSelect(el); });
    _uiOpen("编辑作品监控", "监控对象保持不变。更换创作者或关键词，请新建监控。", {
      okText: "保存修改", wide: true, icon: "i-eye",
      submit: value => api("/api/monitors/" + id, {
        method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(value),
      }),
    });
  });
  if (value === null) return;
  toast("作品监控配置已更新", "ok"); refreshMonitors(); refreshContents();
}
function monitorStrategySummary(t) {
  const depth = t.max_scrolls || (t.platform === "xhs" ? 6 : 12);
  const limit = t.max_items_per_scan || (t.platform === "xhs" ? 12 : 0);
  const filters = [];
  if (t.record_media_filter && t.record_media_filter !== "all") filters.push(t.record_media_filter === "video" ? "视频" : "图文");
  if (t.recent_days) filters.push(`${t.recent_days}天内`);
  if (t.min_like_count) filters.push(`赞≥${fmtNum(t.min_like_count)}`);
  if (t.min_comment_count) filters.push(`评≥${fmtNum(t.min_comment_count)}`);
  if ((t.include_keywords || []).length) filters.push(`含 ${t.include_keywords.join("/")}`);
  if ((t.exclude_keywords || []).length) filters.push(`排除 ${t.exclude_keywords.join("/")}`);
  const filterLabel = filters.length ? filters.join(" · ") : "无入库筛选";
  return `<span class="pill q bare" title="${esc(filterLabel)}">深度 ${depth}</span><span class="pill q bare" title="${esc(filterLabel)}">上限 ${limit || "不限"}</span>${filters.length ? `<span class="pill q bare" title="${esc(filterLabel)}">${esc(filterLabel)}</span>` : ""}`;
}
function monRow(t) {
  const plainLabel = t.target_kind === "keyword"
    ? `#${t.keyword || ""}` : (t.nickname || (t.sec_uid || "").slice(0, 12));
  const label = t.target_kind === "keyword"
    ? `<span class="ic-text">${ic("i-hash")}${esc(t.keyword)}</span>` : esc(t.nickname || (t.sec_uid || "").slice(0, 12));
  const acc = ACCOUNTS.find(a => a.id === t.account_id);
  // 抖音/小红书都显示绑定账号:抖音未登录抓主页易拿到风控过的旧快照,绑号才稳定;视频号走本机微信客户端,无需绑定账号
  const accTag = t.platform === "shipinhao"
    ? `<div class="mut" style="font-size:11px;margin-top:2px">${acc ? `通道：本机微信客户端（附账号:${esc(acc.nickname)}）` : "通道：本机微信客户端"}</div>`
    : acc
    ? `<div class="mut" style="font-size:11px;margin-top:2px">账号:${esc(acc.nickname)}</div>`
    : `<div class="ic-text" style="font-size:11px;margin-top:2px;color:var(--danger)">${ic("i-info")}未绑定账号</div>`;
  const downloadLabel = t.download_enabled === false ? "仅记录"
    : ({ all: "全部下载", video: "仅视频", images: "仅图集" }[t.media_filter] || "全部下载");
  return `<tr>
    <td><div class="user-cell">${t.avatar ? `<img class="avatar" src="${esc(safeMediaUrl(t.avatar))}" alt="" referrerpolicy="no-referrer">` : `<span class="wb-avatar-fallback" style="width:30px;height:30px;flex:0 0 30px;font-size:13px;border-radius:50%" aria-hidden="true">${esc((plainLabel || "监").replace(/^#/, "").slice(0, 1))}</span>`}<div><span title="${esc(plainLabel)}">${label}</span>${t.alias ? `<div class="alias-line" title="${esc(t.alias)}">${esc(t.alias)}</div>` : ""}${accTag}</div></div></td>
    <td>${metaChips(t)}</td>
    <td class="num"><button type="button" class="ghost sm monitor-record-link" data-monitor-records="${t.id}" onclick="showMonitorRecords(${t.id})">查看记录 <span>${t.content_count || 0}</span></button></td>
    <td class="num">${monitorIntervalText(t.interval_seconds)}</td>
    <td class="wrap" style="max-width:240px">
      <div style="display:flex;gap:4px;flex-wrap:wrap;align-items:center"><span class="pill q bare">${downloadLabel}</span>${(t.platform === "xhs" || t.platform === "rednote") ? "" : `<span class="pill q bare">${QMAP[t.video_quality] || "默认画质"}</span>`}${monitorStrategySummary(t)}</div>
      ${t.download_dir ? `<div class="mut" title="${esc(t.download_dir)}" style="font-size:11px;margin-top:3px;max-width:210px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">目录：${esc(t.download_dir)}</div>` : ""}</td>
    <td class="mut">${t.last_scan_at ? new Date(t.last_scan_at + "Z").toLocaleString() : "—"}${t.last_error ? ` <span class="warn-ic" title="${esc(t.last_error)}">${ic("i-info")}</span>` : ""}${autoRunHint(t.next_auto_run_at)}</td>
    <td><span class="pill ${t.enabled ? "active" : "paused"}">${t.enabled ? "监控中" : "已暂停"}</span></td>
    <td class="acttd">
      <button class="ghost sm" onclick="runNow(${t.id})">立即抓取</button>
      <button class="ghost sm" onclick="editMonitor(${t.id})">编辑</button>
      <button class="ghost sm" onclick="toggleMon(${t.id})">${t.enabled ? "暂停" : "启用"}</button>
      <button class="ghost sm danger" onclick="delMon(${t.id})">${ic("i-trash")}删除</button>
    </td></tr>`;
}
function renderMonitorRows() {
  const groupName = $("mon-group") ? $("mon-group").value : "";
  const tag = $("mon-tag") ? $("mon-tag").value : "";
  const status = $("mon-status") ? $("mon-status").value : "";
  const query = (($("mon-search") && $("mon-search").value) || "").trim().toLocaleLowerCase();
  const rows = MONITORS.filter(t => {
    if (!matchesMeta(t, groupName, tag)) return false;
    if (status === "enabled" && !t.enabled) return false;
    if (status === "paused" && t.enabled) return false;
    if (status === "error" && !t.last_error) return false;
    if (!query) return true;
    return [monitorBaseName(t), t.alias, t.group_name, ...itemTags(t)]
      .join(" ").toLocaleLowerCase().includes(query);
  });
  if ($("mon-filter-count")) $("mon-filter-count").textContent = `显示 ${rows.length} / ${MONITORS.length}`;
  $("mon-table").innerHTML = rows.map(monRow).join("")
    || empty(8, "没有匹配的监控", "i-target", MONITORS.length ? "调整分组、标签或搜索条件" : "添加目标后，在这里查看采集进度。", MONITORS.length ? "" : "monitors");
}
async function refreshMonitors() {
  const isCurrent = beginViewRequest("monitors");
  let ts;
  try { ts = await api("/api/monitors?platform=" + PLATFORM); }
  catch (e) {
    if (isCurrent() && !MONITORS.length) $("mon-table").innerHTML = empty(8, "监控暂未加载", "i-info", "请检查连接并重新加载。");
    return;
  }
  if (!isCurrent()) return;
  MONITORS = ts; populateMonitorFacets(); populateContentSrc();
  $("stat-mon").textContent = ts.filter(t => t.enabled).length;
  if ($("tb-mon")) $("tb-mon").textContent = ts.length;
  renderMonitorRows();
}
async function runNow(id) {
  const btn = evtBtn();
  toast("抓取中…正在按配置的获取方式读取作品", "info", 7000);
  await withBusy(btn, "抓取中", async () => {
    try {
      const r = await api("/api/monitors/" + id + "/run-now", { method: "POST" });
      if (r.skipped) toast(r.reason || "本轮已跳过，请查看监控状态", "info", 6000);
      else if (r.partial) toast(`部分完成：已获取 ${r.captured ?? r.new} 条作品，${r.failed || 0} 条详情失败。${r.error || ""}`, "info", 10000);
      else if (r.error) toast("抓取未成功:" + r.error, "err", 10000);
      else if (r.refreshed) toast(`抓取完成，新增 ${r.new} 条记录；已刷新 ${r.refreshed} 条失败记录的访问参数，可在作品记录中手动重试。`, "info", 10000);
      else if ((r.scanned ?? 0) === 0 && (r.new ?? 0) === 0 && !r.filtered && (r.backfill_count === 0 || monitorById(id)?.initial_backfill_count === 0)) {
        toast("未抓取到作品：当前「首次历史回填」为“不回填历史(0 条)”，仅监控新建之后的新发布；可在「编辑」中改为“最近 5 条”后重抓。", "info", 8000);
      }
      else toast(`抓取完成,检查 ${r.scanned ?? r.new} 条，筛除 ${r.filtered || 0} 条，新增 ${r.new} 条`, "ok");
    } catch (e) { toast("抓取失败:" + e.message, "err"); }
  });
  refreshMonitors(); refreshContents();
}
async function toggleMon(id) { try { await api("/api/monitors/" + id + "/toggle", { method: "POST" }); refreshMonitors(); } catch (e) { toast("操作失败:" + e.message, "err"); } }
async function delMon(id) { if (await uiConfirm({ title: "删除监控", message: "删除该监控?", okText: "删除", danger: true })) { try { await api("/api/monitors/" + id, { method: "DELETE" }); toast("监控已删除", "ok"); refreshMonitors(); } catch (e) { toast("删除失败:" + e.message, "err"); } } }

// ─── 内容 ───
function fmtTime(unix) { return unix ? new Date(unix * 1000).toLocaleString() : "—"; }
function fmtDur(sec) { if (!sec) return ""; const m = Math.floor(sec / 60), s = sec % 60; return `${m}:${String(s).padStart(2, "0")}`; }
function fmtNum(n) { return n >= 10000 ? (n / 10000).toFixed(1) + "w" : (n || 0); }
function contentTimeCell(unix) {
  if (!unix) return `<span class="mut">—</span>`;
  const date = new Date(unix * 1000);
  const day = date.toLocaleDateString("zh-CN", { year: "numeric", month: "2-digit", day: "2-digit" });
  const time = date.toLocaleTimeString("zh-CN", { hour12: false });
  return `<div class="content-time"><span>${esc(day)}</span><span>${esc(time)}</span></div>`;
}
function contentStatusLabel(status) {
  return ({ pending: "等待中", downloading: "下载中", done: "已下载", failed: "失败", skipped: "仅记录" })[status] || status || "未知";
}

function contentPathMeta(r) {
  const raw = String(r.local_path || "").trim();
  if (!raw) return null;
  const splitAt = Math.max(raw.lastIndexOf("\\"), raw.lastIndexOf("/"));
  const parent = splitAt >= 0 ? raw.slice(0, splitAt) : "";
  let leaf = splitAt >= 0 ? raw.slice(splitAt + 1) : raw;
  const prefix = String(r.aweme_id || "") + "_";
  if (r.aweme_id && leaf.startsWith(prefix)) leaf = leaf.slice(prefix.length);
  const dot = leaf.lastIndexOf(".");
  const hasExt = dot > 0 && leaf.length - dot <= 10;
  const name = hasExt ? leaf.slice(0, dot) : leaf;
  const ext = hasExt ? leaf.slice(dot + 1).toUpperCase() : "";
  const dirs = parent.split(/[\\/]+/).filter(Boolean);
  return { name: name || leaf, ext, dir: dirs.slice(-2).join("\\") };
}
function contentPathCell(r) {
  const p = contentPathMeta(r);
  if (!p) return "";
  const rawPath = String(r.local_path || "").trim();
  return `<div class="local-path" title="${esc(rawPath)}">
    <div class="local-path-info">
      <div class="local-path-file"><span class="local-path-name" title="${esc(rawPath)}">${esc(p.name)}</span>${p.ext ? `<span class="local-path-ext">${esc(p.ext)}</span>` : ""}</div>
      <div class="local-path-dir" title="${esc(p.dir || "当前目录")}">${esc(p.dir || "当前目录")}</div>
    </div>
  </div>`;
}
async function revealContentPath(id, btn) {
  const old = btn && btn.innerHTML;
  if (btn) { btn.disabled = true; btn.innerHTML = `<span class="spin"></span>`; }
  try {
    await api(`/api/contents/${id}/reveal`, {
      method: "POST", headers: { "X-CreatorHub-Local-Action": "reveal" },
    });
    toast("已在文件夹中显示", "ok", 1800);
  } catch (e) {
    toast("打开文件夹失败:" + e.message, "err");
  } finally {
    if (btn && btn.isConnected) { btn.disabled = false; btn.innerHTML = old; }
  }
}

// ─── 批量选择 ───
const selContent = new Set(), selComment = new Set();
function pruneSel(set, ids) { const p = new Set(ids); [...set].forEach(id => { if (!p.has(id)) set.delete(id); }); }
const CONTENT_CBS = '#content-table input[type="checkbox"], #content-cards input[type="checkbox"]';
function contentToggleOne(id, on) { on ? selContent.add(id) : selContent.delete(id); updateContentSelBar(); }
function contentToggleAll(on) { document.querySelectorAll(CONTENT_CBS).forEach(cb => { const id = +cb.dataset.id; if (!id) return; cb.checked = on; on ? selContent.add(id) : selContent.delete(id); }); updateContentSelBar(); }
function contentSelAllToggle() {
  const ids = [...document.querySelectorAll(CONTENT_CBS)].map(cb => +cb.dataset.id).filter(Boolean);
  const allSel = ids.length > 0 && ids.every(id => selContent.has(id));
  contentToggleAll(!allSel);
}
function contentSelClear() { selContent.clear(); const sa = $("content-selall"); if (sa) sa.checked = false; refreshContents(); }
function updateContentSelBar() {
  const n = selContent.size;
  $("content-selcount").textContent = "已选 " + n + " 项";
  $("content-selbar").style.display = n ? "inline-flex" : "none";
  const barWrap = $("content-selbar")?.closest?.(".wb-bulk-bar");
  if (barWrap) barWrap.hidden = !n;
  const ids = [...document.querySelectorAll(CONTENT_CBS)].map(cb => +cb.dataset.id).filter(Boolean);
  const allSel = ids.length > 0 && ids.every(id => selContent.has(id));
  const selectedOnPage = ids.filter(id => selContent.has(id)).length;
  const btn = $("content-selall-btn"); if (btn) btn.textContent = allSel ? "取消全选" : "全选";
  const sa = $("content-selall"); if (sa) { sa.checked = allSel; sa.indeterminate = selectedOnPage > 0 && !allSel; }
}
async function contentBatchDelete() {
  if (!selContent.size) return;
  if (!await uiConfirm({ title: "批量删除作品", message: `删除选中的 ${selContent.size} 条作品及其本地文件?`, okText: "删除", danger: true })) return;
  try { const r = await api("/api/contents/batch-delete", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ids: [...selContent], with_file: true }) }); toast(`已删除 ${r.deleted} 条(清理 ${r.files_removed} 个文件)`, "ok"); selContent.clear(); refreshContents(); }
  catch (e) { toast("批量删除失败:" + e.message, "err"); }
}
const COMMENT_CBS = '#comment-table input[type="checkbox"]';
function commentToggleOne(id, on) { on ? selComment.add(id) : selComment.delete(id); updateCommentSelBar(); }
function commentToggleAll(on) { document.querySelectorAll(COMMENT_CBS).forEach(cb => { const id = +cb.dataset.id; if (!id) return; cb.checked = on; on ? selComment.add(id) : selComment.delete(id); }); updateCommentSelBar(); }
function commentSelAllToggle() {
  const ids = [...document.querySelectorAll(COMMENT_CBS)].map(cb => +cb.dataset.id).filter(Boolean);
  const allSel = ids.length > 0 && ids.every(id => selComment.has(id));
  commentToggleAll(!allSel);
}
function commentSelClear() { selComment.clear(); const sa = $("comment-selall"); if (sa) sa.checked = false; refreshComments(); }
function updateCommentSelBar() {
  const n = selComment.size; const c = $("comment-selcount"), bar = $("comment-selbar");
  if (c) c.textContent = "已选 " + n + " 项";
  if (bar) bar.style.display = n ? "inline-flex" : "none";
  const barWrap = bar?.closest?.(".wb-bulk-bar");
  if (barWrap) barWrap.hidden = !n;
  const ids = [...document.querySelectorAll(COMMENT_CBS)].map(cb => +cb.dataset.id).filter(Boolean);
  const allSel = ids.length > 0 && ids.every(id => selComment.has(id));
  const selectedOnPage = ids.filter(id => selComment.has(id)).length;
  const btn = $("comment-selall-btn"); if (btn) btn.textContent = allSel ? "取消全选" : "全选";
  const sa = $("comment-selall"); if (sa) { sa.checked = allSel; sa.indeterminate = selectedOnPage > 0 && !allSel; }
}
async function commentBatchDelete() {
  if (!selComment.size) return;
  if (!await uiConfirm({ title: "批量删除评论", message: `删除选中的 ${selComment.size} 条评论?`, okText: "删除", danger: true })) return;
  try { const r = await api("/api/comments/batch-delete", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ids: [...selComment] }) }); toast(`已删除 ${r.deleted} 条评论`, "ok"); selComment.clear(); refreshComments(); }
  catch (e) { toast("批量删除失败:" + e.message, "err"); }
}

function srcOf(r) {
  return contentSourceMarkup(r);
}
let CONTENT_VIEW_MODE = (() => {
  try { return localStorage.getItem("dym-content-view") || "auto"; } catch (e) { return "auto"; }
})();
function effectiveContentViewMode() {
  if (CONTENT_VIEW_MODE === "cards" || CONTENT_VIEW_MODE === "table") return CONTENT_VIEW_MODE;
  return PLATFORM === "xhs" ? "cards" : "cards";
}
function setContentViewMode(mode) {
  CONTENT_VIEW_MODE = mode === "table" ? "table" : "cards";
  try { localStorage.setItem("dym-content-view", CONTENT_VIEW_MODE); } catch (e) {}
  refreshContents(false);
}
function noteCard(r) {
  const typeIc = r.media_type === "images" ? "i-image" : "i-play";
  const durText = r.duration ? (typeof fmtDur === "function" ? " " + fmtDur(r.duration) : "") : "";
  const typeLabel = (r.media_type === "images" ? "图文" : "视频") + durText;
  const cover = r.cover_url
    ? `<img class="ncard-cover" src="${esc(safeMediaUrl(r.cover_url))}" alt="${esc((r.desc || "作品").slice(0, 20))}" referrerpolicy="no-referrer" loading="lazy" onclick="openPreview(${r.id})">`
    : `<div class="ncard-cover ph" onclick="openPreview(${r.id})">${ic(r.media_type === "images" ? "i-image" : "i-film")}</div>`;
  const hasLocal = Boolean(String(r.local_path || "").trim());
  const canPickRepost = ["douyin", "bilibili", "instagram", "twitter"].includes(PLATFORM) && r.download_status === "done";
  return `<div class="ncard">
    ${cover}
    <span class="ncard-type">${ic(typeIc)}${esc(typeLabel)}</span>
    <label class="ncard-selection" title="选择这条作品"><input type="checkbox" class="ncard-sel" data-id="${r.id}" aria-label="选择这条作品" onchange="contentToggleOne(${r.id}, this.checked)" ${selContent.has(r.id) ? "checked" : ""}></label>
    <div class="ncard-body">
      <p class="ncard-title" style="cursor:pointer" onclick="openPreview(${r.id})">${esc(r.desc || "(无标题)")}</p>
      ${srcOf(r)}
      <div class="ncard-captured"><span class="content-origin-label">抓取入库</span>${contentCapturedTime(r.captured_at)}</div>
      <div class="ncard-foot">
        <span>${fmtTime(r.create_time)}</span>
        <span class="like">${ic("i-heart")}${fmtNum(r.like_count)} · 💬${fmtNum(r.comment_count)}</span>
      </div>
      <div class="ncard-actions">
        <span class="pill ${r.download_status}" style="flex:1;justify-content:center" title="${esc(r.error || "")}">${contentStatusLabel(r.download_status)}${r.error ? " ⓘ" : ""}</span>
        ${hasLocal ? `<button class="ghost sm" style="flex:0 0 auto" onclick="revealContentPath(${r.id},this)" title="打开文件夹">${ic("i-folder")}</button>` : ""}
        ${["failed", "skipped"].includes(r.download_status) ? `<button class="ghost sm" onclick="retryDl(${r.id})">${r.download_status === "skipped" ? "下载" : "重试"}</button>` : ""}
        ${canPickRepost ? `<button class="ghost sm" onclick="pickRepostTarget(${r.id})">转发</button>` : ""}
        ${(PLATFORM === "xhs" && r.download_status === "done") ? `<button class="ghost sm" onclick="repostDouyin(${r.id})">发抖音</button>` : ""}
        <button class="ghost sm danger" style="flex:0 0 auto" onclick="delContent(${r.id})" aria-label="删除">${ic("i-trash")}</button>
      </div>
    </div>
  </div>`;
}
function renderContentPager(meta) {
  const pager = $("content-pager");
  if (!pager) return;
  const total = Math.max(0, Number(meta && meta.total || 0));
  const pageSize = Math.max(1, Number(meta && meta.page_size || CONTENT_PAGE_SIZE));
  const pages = Math.max(1, Number(meta && meta.pages || Math.ceil(total / pageSize) || 1));
  const page = Math.max(1, Number(meta && meta.page || CONTENT_PAGE));
  CONTENT_TOTAL = total;
  CONTENT_PAGE_SIZE = pageSize;
  CONTENT_PAGE = page;
  if ($("content-page-size")) $("content-page-size").value = String(pageSize);
  if ($("content-page-input")) {
    $("content-page-input").value = String(page);
    $("content-page-input").max = String(pages);
  }
  if ($("content-page-info")) $("content-page-info").textContent =
    "第 " + page + " / " + pages + " 页 · 共 " + fmtNum(total) + " 条";
  if ($("content-first")) $("content-first").disabled = page <= 1;
  if ($("content-prev")) $("content-prev").disabled = page <= 1;
  if ($("content-next")) $("content-next").disabled = page >= pages;
  if ($("content-last")) $("content-last").disabled = page >= pages;
  pager.hidden = total <= pageSize;
}
function contentPageCount() {
  return Math.max(1, Math.ceil(CONTENT_TOTAL / CONTENT_PAGE_SIZE));
}
function goContentPage(page) {
  const pages = contentPageCount();
  const target = page <= 0 ? pages : Math.min(pages, Math.max(1, Math.round(Number(page) || 1)));
  if (target === CONTENT_PAGE) return;
  CONTENT_PAGE = target;
  refreshContents();
}
function changeContentPage(delta) { goContentPage(CONTENT_PAGE + Number(delta || 0)); }
function jumpContentPage() {
  const input = $("content-page-input");
  const value = input ? Number(input.value) : 1;
  if (!Number.isFinite(value) || value < 1) {
    if (input) { input.value = String(CONTENT_PAGE); input.focus(); }
    return;
  }
  goContentPage(value);
}
function handleContentPageInput(event) {
  if (event && event.key === "Enter") { event.preventDefault(); jumpContentPage(); }
}
function setContentPageSize() {
  const value = +(($('content-page-size') && $('content-page-size').value) || 10);
  CONTENT_PAGE_SIZE = [10, 20, 50, 100, 200].includes(value) ? value : 10;
  CONTENT_PAGE = 1;
  refreshContents();
}
async function refreshContents(resetPage = false) {
  const isCurrent = beginViewRequest("contents");
  const scope = PLATFORM + ":" + CONTENT_SRC;
  const clearResults = resetPage || scope !== CONTENT_RENDER_SCOPE;
  CONTENT_RENDER_SCOPE = scope;
  if (clearResults) {
    CONTENT_PAGE = 1;
    CONTENTS = []; selContent.clear();
    $("content-cards").innerHTML = `<div class="empty" role="status">正在读取当前任务的记录…</div>`;
    $("content-table").innerHTML = empty(8, "正在读取当前任务的记录…", "i-clock");
    renderContentPager({total:0, page:1, page_size:CONTENT_PAGE_SIZE});
    if ($("content-filter-count")) $("content-filter-count").textContent = "正在读取…";
    updateContentSelBar();
  }
  let captureBounds;
  const dateHint = $("content-capture-help");
  try { captureBounds = contentCaptureBounds(); }
  catch (e) {
    if (dateHint) { dateHint.textContent = e.message; dateHint.dataset.error = "true"; }
    ["content-captured-from", "content-captured-to"].forEach(id => $(id)?.setAttribute("aria-invalid", "true"));
    if (clearResults) {
      $("content-cards").innerHTML = `<div class="empty" role="status">${esc(e.message)}</div>`;
      $("content-table").innerHTML = empty(8, e.message, "i-info");
      if ($("content-filter-count")) $("content-filter-count").textContent = "请检查日期范围";
    }
    toast(e.message, "err"); return;
  }
  if (dateHint) {
    dateHint.textContent = "按当前设备时区筛选，包含结束当天；抓取时间为首次入库时间。";
    delete dateHint.dataset.error;
  }
  ["content-captured-from", "content-captured-to"].forEach(id => $(id)?.removeAttribute("aria-invalid"));
  const params = new URLSearchParams({
    platform: PLATFORM, page: String(CONTENT_PAGE),
    page_size: String(CONTENT_PAGE_SIZE), paginate: "true",
  });
  if (CONTENT_SRC) params.set("target_id", CONTENT_SRC);
  if (CONTENT_GROUP) params.set("group_name", CONTENT_GROUP);
  if (CONTENT_TAG) params.set("tag", CONTENT_TAG);
  for (const [key, value] of Object.entries(captureBounds)) params.set(key, value);
  const query = (($('content-search') && $('content-search').value) || "").trim();
  const mediaType = ($('content-type') && $('content-type').value) || "";
  const status = ($('content-status') && $('content-status').value) || "";
  const minLikes = +(($('content-min-likes') && $('content-min-likes').value) || 0);
  const minComments = +(($('content-min-comments') && $('content-min-comments').value) || 0);
  if (query) params.set("q", query);
  if (mediaType) params.set("media_type", mediaType);
  if (status) params.set("download_status", status);
  if (Number.isFinite(minLikes) && minLikes > 0) params.set("min_like_count", String(Math.floor(minLikes)));
  if (Number.isFinite(minComments) && minComments > 0) params.set("min_comment_count", String(Math.floor(minComments)));
  params.set("sort", ($('content-sort') && $('content-sort').value) || "captured_desc");
  let payload;
  try { payload = await api("/api/contents?" + params.toString()); }
  catch (e) {
    if (isCurrent()) {
      if (clearResults) {
        $("content-cards").innerHTML = `<div class="empty" role="status">记录加载失败，请重新加载。</div>`;
        $("content-table").innerHTML = empty(8, "记录加载失败，请重新加载", "i-info");
        if ($("content-filter-count")) $("content-filter-count").textContent = "加载失败";
      }
      toast("记录加载失败：" + e.message, "err");
    }
    return;
  }
  if (!isCurrent()) return;
  const meta = Array.isArray(payload)
    ? { items: payload, total: payload.length, page: 1, page_size: CONTENT_PAGE_SIZE,
        pages: Math.max(1, Math.ceil(payload.length / CONTENT_PAGE_SIZE)) }
    : (payload || {});
  const pages = Math.max(1, Number(meta.pages || 1));
  if (CONTENT_PAGE > pages) { CONTENT_PAGE = pages; return refreshContents(); }
  const rows = Array.isArray(meta.items) ? meta.items : [];
  rows.forEach(row => {
    if (row.source && row.source.platform === PLATFORM && String(row.source.id) === String(row.target_id))
      CONTENT_SOURCE_CACHE.set(String(row.target_id), row.source);
  });
  if (meta.source && meta.source.platform === PLATFORM && String(meta.source.id) === CONTENT_SRC)
    CONTENT_SOURCE_CACHE.set(CONTENT_SRC, meta.source);
  populateContentSrc();
  CONTENTS = rows;
  if ($("content-filter-count")) $("content-filter-count").textContent =
    `显示 ${rows.length} / ${Number(meta.total || rows.length)}`;
  const xhs = PLATFORM === "xhs";
  const useCards = effectiveContentViewMode() === "cards";
  $("content-title").textContent = xhs ? "笔记记录" : "作品记录";
  if ($("content-view-cards")) $("content-view-cards").classList.toggle("active", useCards);
  if ($("content-view-table")) $("content-view-table").classList.toggle("active", !useCards);
  $("content-table-wrap").style.display = useCards ? "none" : "";
  $("content-cards").style.display = useCards ? "" : "none";
  if (useCards) {
    setHtmlIfChanged($("content-cards"), rows.map(noteCard).join("")
      || `<div class="empty" style="columns:1">${ic("i-image")}<div class="empty-t">暂无作品记录</div></div>`);
    updateContentSelBar(); renderContentPager(meta);
    return;
  }
  const tableHtml = rows.map(r => {
    const description = esc(r.desc || "（未填写作品文案）");
    const durText = r.duration ? (typeof fmtDur === "function" ? fmtDur(r.duration) : String(r.duration)) : "";
    const durBadge = durText ? `<span class="thumb-dur">${esc(durText)}</span>` : "";
    const specSub = [r.quality, durText].filter(Boolean).join(" · ");
    const hasLocal = Boolean(String(r.local_path || "").trim());
    return `<tr>
      <td class="content-check-cell"><label class="content-selection" title="选择这条作品"><input type="checkbox" data-id="${r.id}" aria-label="选择这条作品" onchange="contentToggleOne(${r.id}, this.checked)" ${selContent.has(r.id) ? "checked" : ""}></label></td>
      <td class="content-cover-cell"><span class="content-cover-box" onclick="openPreview(${r.id})" title="点击站内预览">${r.cover_url ? `<img class="thumb" src="${esc(safeMediaUrl(r.cover_url))}" alt="封面" loading="lazy" referrerpolicy="no-referrer">` : `<span class="content-cover-empty">${ic(r.media_type === "images" ? "i-image" : "i-film")}</span>`}${durBadge}</span></td>
      <td class="content-desc-cell">
        <div class="content-desc-text clickable" title="${description}" onclick="openPreview(${r.id})">${description}</div>
        ${contentSourceMarkup(r)}
      </td>
      <td class="content-type-cell"><span class="content-kind">${(PLATFORM === "twitter" && r.media_type === "images" && !r.cover_url && !hasLocal && !r.quality) ? "纯文本" : (r.media_type === "images" ? "图集" : "视频")}</span>${specSub ? `<span class="content-quality">${esc(specSub)}</span>` : ""}</td>
      <td class="content-record-time"><span class="content-origin-label">抓取入库</span>${contentCapturedTime(r.captured_at)}<div class="content-published-time">发布 · ${fmtTime(r.create_time)}</div></td>
      <td class="content-metrics num"><span class="metric like">${ic("i-heart")}${fmtNum(r.like_count)}</span><span class="metric">${ic("i-msg")}${fmtNum(r.comment_count)}</span></td>
      <td class="content-status-cell">
        <div class="content-status-row"><span class="pill ${r.download_status}">${contentStatusLabel(r.download_status)}</span>${r.error ? `<span class="warn-ic" data-tip="${esc(r.error)}" title="${esc(r.error)}">${ic("i-info")}</span>` : ""}</div>
        ${contentPathCell(r)}
      </td>
      <td class="content-action-cell acttd">
        <div class="content-action-buttons">
          <button type="button" class="ghost sm content-icon-btn" onclick="openPreview(${r.id})" data-tip="站内预览" title="站内预览" aria-label="预览作品">${ic("i-eye")}</button>
          ${hasLocal ? `<button type="button" class="ghost sm content-icon-btn local-path-action reveal" onclick="revealContentPath(${r.id},this)" data-tip="在文件夹中显示" title="在文件夹中显示" aria-label="在文件夹中显示">${ic("i-folder")}</button>` : ""}
          ${["failed", "skipped"].includes(r.download_status) ? `<button type="button" class="ghost sm" onclick="retryDl(${r.id})">${r.download_status === "skipped" ? "下载" : "重试"}</button>` : ""}
          ${(["douyin", "bilibili", "instagram", "twitter"].includes(PLATFORM) && r.download_status === "done") ? `<button type="button" class="ghost sm content-action-primary" onclick="pickRepostTarget(${r.id})">${ic("i-send")}转发</button>` : ""}
          ${(PLATFORM === "xhs" && r.download_status === "done") ? `<button class="ghost sm content-action-primary" onclick="repostDouyin(${r.id})">${ic("i-send")}发抖音</button>` : ""}
          <button type="button" class="ghost sm content-action-delete danger" onclick="delContent(${r.id})" data-tip="删除作品" title="删除作品" aria-label="删除作品">${ic("i-trash")}</button>
        </div>
      </td>
    </tr>`;
  }).join("") || empty(8, "暂无作品", "i-film", "监控目标有新作品时会自动抓取并下载,显示在这里");
  setHtmlIfChanged($("content-table"), tableHtml);
  updateContentSelBar(); renderContentPager(meta);
}
async function retryDl(id) {
  const btn = evtBtn();
  await withBusy(btn, "重试中", async () => {
    try {
      const result = await api("/api/contents/" + id + "/retry-download", { method: "POST" });
      if (!result.ok) throw new Error(result.error || "下载未完成");
      toast("重试成功，作品已下载", "ok");
    } catch (e) { toast("重试失败:" + e.message, "err", 7000); }
  });
  await refreshContents();
}
async function delContent(id) {
  if (!await uiConfirm({ title: "删除作品", message: "删除这条作品记录及其已下载的本地文件?", okText: "删除", danger: true })) return;
  try { const r = await api("/api/contents/" + id + "?with_file=true", { method: "DELETE" }); toast(`已删除(清理 ${r.files_removed} 个文件)`, "ok"); refreshContents(); }
  catch (e) { toast("删除失败:" + e.message, "err"); }
}
