// 07-account-hub.js — 账号中心（本账号作品、关注列表、粉丝列表与异步同步进度）。
// ═══════════ 账号管理(独立面板:我的作品 / 关注 / 粉丝 / 私信)═══════════
// 当前操作的账号 id —— 按平台各记各的,切平台不串号、不串数
let HUB_ACC = "";
let HUB_TAB = (() => { try { return localStorage.getItem("dym-hubtab") || "myworks"; } catch (e) { return "myworks"; } })();
let DM_CONV = null;     // 当前打开的会话 id
let DM_CONVS = [];      // 会话缓存(供发送时取 peer 信息)
let DM_NEW_TARGET = ""; // 尚未建会的抖音号或 sec_uid
function hubAccKey() { return "dym-hubacc:" + PLATFORM; }
function loadHubAcc() { try { HUB_ACC = localStorage.getItem(hubAccKey()) || ""; } catch (e) { HUB_ACC = ""; } }
function setHubAcc(id) { HUB_ACC = String(id || ""); try { localStorage.setItem(hubAccKey(), HUB_ACC); } catch (e) {} if (HUB_TAB === "dm") startDmStream(); }

// 用该账号登录态弹出真实浏览器窗口,留给用户手动操作(收发私信 / 维护 / F12 抓接口)
async function openAccountBrowser(id, url = "") {
  await withBusy(evtBtn(), "打开中", async () => {
    try {
      const qs = url ? "?url=" + encodeURIComponent(url) : "";
      const result = await api("/api/accounts/" + id + "/open-browser" + qs, { method: "POST" });
      const checkHint = result.environment_check_opened
        ? " 已同时打开 BrowserScan 环境体检标签。" : "";
      if (result.logged_out) {
        toast("该账号登录态已失效，请关闭当前窗口后点「重新登录」完成扫码。" + checkHint, "err", 8000);
        refreshAccounts();
      } else if (result.login_state === "verification") {
        toast("浏览器已打开；小红书要求安全验证，请在窗口中按提示完成。" + checkHint, "info", 8000);
      } else if (result.login_state === "unconfirmed") {
        toast("浏览器已打开；页面尚未返回登录校验结果，不会因此把账号标记为登录失败。" + checkHint, "info", 7000);
      } else {
        const scope = result.login_scope === "creator" ? "创作平台" : "主站读取";
        toast("已弹出该账号" + scope + "浏览器窗口;用完请关窗(关窗即保存登录态)。窗口开着时该账号后台同步会暂停。" + checkHint, "ok", 7000);
      }
    } catch (e) { toast("打开失败:" + e.message, "err"); }
  });
}

async function checkBrowserEnvironment(id) {
  const btn = evtBtn();
  const confirmed = await uiConfirm({
    title: "打开第三方环境检测",
    message: "将在该账号的独立指纹环境中访问 BrowserScan。该站点会看到当前出口 IP 和浏览器指纹；检测结果仅用于环境核对，不代表平台风控一定通过。",
    okText: "打开检测页",
  });
  if (!confirmed) return;
  await withBusy(btn, "打开中", async () => {
    try {
      await api("/api/accounts/" + id + "/environment-check", { method: "POST" });
      toast("BrowserScan 已在该账号独立环境中打开，请核对 IP、时区、WebRTC 与指纹一致性", "ok", 8000);
      await refreshAccounts();
    } catch (e) {
      toast("环境检测打开失败:" + e.message, "err", 8000);
    }
  });
}

// 私信页:用当前选中账号打开真实浏览器手动收发。
function openHubAccountBrowser() {
  if (!HUB_ACC) { toast("请先选择账号", "err"); return; }
  openAccountBrowser(+HUB_ACC);
}

// 从「账号」面板某行跳转查看该账号的本账号数据(作品/关注/粉丝/私信)
function openAccountHub(id) {
  setHubAcc(id);
  const s = $("hub-acc"); if (s) { s.value = HUB_ACC; if (s._csSync) s._csSync(); }
  DM_CONV = null;
  DM_NEW_TARGET = "";
  refreshHubSummary();
  switchTab("hub");
  switchHubTab("myworks");   // 默认落到「我的作品」,可再切关注/粉丝/私信
}
function openAggregatedDm(accountId, convId) {
  setHubAcc(accountId);
  const select = $("hub-acc"); if (select) { select.value = HUB_ACC; select._csSync?.(); }
  switchTab("hub");
  switchHubTab("dm");
  setTimeout(async () => { await refreshDmConvs(); await openDmConv(convId); }, 0);
}

function populateHubAccounts() {
  const sel = $("hub-acc"); if (!sel) return;
  const list = ACCOUNTS;
  loadHubAcc();   // 账号按平台各记各的:先取当前平台上次选中的
  if (!list.some(a => String(a.id) === HUB_ACC)) setHubAcc(list.length ? list[0].id : "");
  sel.innerHTML = list.length
    ? list.map(a => `<option value="${a.id}">${esc(a.nickname || ("账号#" + a.id))}${a.status === "invalid" ? " · 登录失效" : ""}</option>`).join("")
    : `<option value="">无已登录账号</option>`;
  sel.value = HUB_ACC;
  if (sel._csSync) sel._csSync();
  refreshHubSummary();   // 账号列表/选中账号变了(含切平台)→ 立刻刷新计数徽章
}
function onHubAcc() {
  const sel = $("hub-acc"); if (!sel) return;
  setHubAcc(sel.value);
  for (const direction of ["following", "fan"]) {
    if (typeof FOLLOW_STATE !== "undefined") {
      FOLLOW_STATE[direction] = { page: 1, pages: 1, total: 0, query: "" };
      FOLLOW_JOB_IDS[direction] = "";
    }
    const search = $(`${direction}-search`); if (search) search.value = "";
  }
  DM_CONV = null;
  DM_NEW_TARGET = "";
  refreshHubSummary();
  refreshHubPanel();
}
// 面板内子标签(我的作品/关注/粉丝/私信)切换
function switchHubTab(name) {
  if (PLATFORM === "tiktok" && ["following", "fans", "dm"].includes(name)) name = "myworks";
  HUB_TAB = name;
  try { localStorage.setItem("dym-hubtab", name); } catch (e) {}
  document.querySelectorAll("[data-hubpanel]").forEach(p => { p.style.display = p.dataset.hubpanel === name ? "" : "none"; });
  document.querySelectorAll("[data-hubtab]").forEach(t => t.classList.toggle("active", t.dataset.hubtab === name));
  if (name === "dm") startDmStream(); else stopDmStream();
  refreshHubPanel();
}
// 计数徽章:纯查库汇总,进面板/换账号/切平台即刷新,不用点进子页才有数
async function refreshHubSummary() {
  const isCurrent = beginViewRequest("hub-summary", () => String(HUB_ACC));
  const ids = { works: "hb-myworks", following: "hb-following", fans: "hb-fans", dm: "hb-dm" };
  const setAll = r => Object.entries(ids).forEach(([k, i]) => { const el = $(i); if (el) el.textContent = (r && r[k]) || 0; });
  if (!HUB_ACC) { setAll(null); return; }
  try { const data = await api("/api/hub/summary?account_id=" + HUB_ACC); if (isCurrent()) setAll(data); }
  catch (e) { if (isCurrent()) setAll(null); }
}
function refreshHubPanel() {
  const active = document.querySelector('.navitem.active');
  if (!active || active.dataset.tab !== "hub") return;
  if (HUB_TAB === "myworks") refreshMyWorks();
  else if (HUB_TAB === "following") refreshFollows("following");
  else if (HUB_TAB === "fans") refreshFollows("fan");
  else if (HUB_TAB === "dm") { refreshDmConvs(); refreshDmAutomation(); startDmStream(); }
  else if (HUB_TAB === "stats") loadHubStats();
}

// ── 本账号数据分析(B4)──
function _kpiCard(label, val, delta) {
  const d = (delta === undefined || delta === null || delta === 0) ? ""
    : `<span class="kpi-delta ${delta > 0 ? "pos" : "neg"}">较上次 ${delta > 0 ? "+" : "−"}${fmtNum(Math.abs(delta))}</span>`;
  return `<div class="kpi-card"><div class="kpi-label">${esc(label)}</div>`
    + `<div class="kpi-value">${fmtNum(val)}${d}</div></div>`;
}
function _spark(vals) {
  // 极简 SVG 折线(粉丝趋势),无外部依赖
  vals = vals.filter(v => typeof v === "number");
  if (vals.length < 2) return '<div class="empty" style="padding:18px 8px"><div class="empty-t">趋势数据不足</div><div class="empty-sub">运行几天后会生成连续曲线</div></div>';
  const w = 480, h = 60, mn = Math.min(...vals), mx = Math.max(...vals), rng = (mx - mn) || 1;
  const points = vals.map((v, i) => ({ x: +(i / (vals.length - 1) * w).toFixed(1), y: +(h - (v - mn) / rng * (h - 10) - 5).toFixed(1) }));
  const pts = points.map(p => `${p.x},${p.y}`).join(" "), last = points[points.length - 1];
  return `<svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" aria-hidden="true">
    <defs><linearGradient id="spark-fill" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="var(--acc)" stop-opacity=".24"/><stop offset="1" stop-color="var(--acc)" stop-opacity="0"/></linearGradient></defs>
    <line x1="0" y1="${h - 5}" x2="${w}" y2="${h - 5}" stroke="var(--line-soft)" stroke-width="1"/>
    <polygon points="0,${h} ${pts} ${w},${h}" fill="url(#spark-fill)"/>
    <polyline fill="none" stroke="var(--acc)" stroke-width="2.4" vector-effect="non-scaling-stroke" points="${pts}"/>
    <circle cx="${last.x}" cy="${last.y}" r="3.5" fill="var(--surface)" stroke="var(--acc)" stroke-width="2" vector-effect="non-scaling-stroke"/>
  </svg>`;
}
async function loadHubStats() {
  const isCurrent = beginViewRequest("hub-stats", () => String(HUB_ACC));
  const kpi = $("stats-kpi"), tr = $("stats-trend"), wb = $("stats-works");
  if (!kpi) return;
  if (!HUB_ACC) { kpi.innerHTML = ""; if (tr) tr.innerHTML = ""; if (wb) wb.innerHTML = `<tr><td colspan="5" class="mut">请先选择账号</td></tr>`; return; }
  try {
    const d = await api("/api/account-stats/" + HUB_ACC + "?days=30");
    if (!isCurrent()) return;
    if ($("hb-stats")) $("hb-stats").textContent = (d.works || []).length;
    kpi.innerHTML = _kpiCard("粉丝", d.account.follower_count || 0, d.fans_delta)
      + _kpiCard("作品数", d.account.aweme_count || 0)
      + _kpiCard("近30天快照", (d.trend || []).length);
    if (tr) {
      const vals = (d.trend || []).map(x => x.follower_count);
      const summary = vals.length > 1 ? `粉丝数从 ${fmtNum(vals[0])} 变化到 ${fmtNum(vals[vals.length - 1])}` : "粉丝趋势数据不足";
      tr.innerHTML = `<div class="trend-panel"><div class="trend-head"><b>粉丝趋势</b><span>近 30 天</span></div>`
        + `<div class="spark-wrap" role="img" aria-label="${summary}">${_spark(vals)}</div></div>`;
    }
    if (wb) wb.innerHTML = (d.works || []).length
      ? d.works.map(w => {
        const fullTitle = w.desc || w.item_id || "无描述";
        return `<tr><td class="wrap" style="max-width:340px"><span class="content-desc-text clickable" title="${esc(fullTitle)}" onclick="openWork(${jsArg(w.platform || PLATFORM)},${jsArg(w.item_id)})">${esc(fullTitle)}</span></td>`
          + `<td class="num">${fmtNum(w.play_count || 0)}</td><td class="num">${fmtNum(w.like_count || 0)}</td><td class="num">${fmtNum(w.comment_count || 0)}</td>`
          + `<td><span class="pill bare">${esc(w.status || "—")}</span></td></tr>`;
      }).join("")
      : `<tr><td colspan="5" class="mut">暂无作品数据,先到「我的作品」点「同步作品」</td></tr>`;
  } catch (e) { if (!isCurrent()) return;
    kpi.innerHTML = `<div class="mut">加载失败:${esc(e.message)}</div>`;
  }
}
function hubGridEmpty(text, sub = "") {
  return `<div class="empty" style="width:100%;column-span:all;break-inside:avoid"><div class="empty-ic">${ic("i-inbox")}</div>` +
    `<div class="empty-t">${esc(text)}</div>${sub ? `<div class="empty-sub">${esc(sub)}</div>` : ""}</div>`;
}

// ── 我的作品 ──
async function refreshMyWorks() {
  const isCurrent = beginViewRequest("my-works", () => String(HUB_ACC));
  const grid = $("mw-grid"); if (!grid) return;
  if (!HUB_ACC) { grid.innerHTML = hubGridEmpty("请先选择已登录账号"); return; }
  try {
    const list = await api("/api/account-works?account_id=" + HUB_ACC);
    if (!isCurrent()) return;
    if ($("hb-myworks")) $("hb-myworks").textContent = list.length;
    grid.innerHTML = list.length ? list.map(workCard).join("")
      : hubGridEmpty("暂无作品", "点右上「同步作品」抓取本账号已发布作品");
  } catch (e) { if (!isCurrent()) return; grid.innerHTML = hubGridEmpty("加载失败:" + e.message); }
}
function workLink(platform, id) {
  id = encodeURIComponent(id);
  if (platform === "xhs") return "https://www.xiaohongshu.com/explore/" + id;
  if (platform === "rednote") return "https://www.rednote.com/zh/discovery/item/" + id;
  if (platform === "kuaishou") return "https://www.kuaishou.com/short-video/" + id;
  if (platform === "shipinhao") return "https://channels.weixin.qq.com/platform/post/list";
  if (platform === "tiktok") {
    const account = ACCOUNTS.find(a => String(a.id) === String(HUB_ACC));
    const author = String(account?.douyin_id || "").replace(/^@/, "");
    return author ? `https://www.tiktok.com/@${encodeURIComponent(author)}/video/${id}`
      : `https://www.tiktok.com/search?q=${id}`;
  }
  if (platform === "bilibili") {
    const raw = decodeURIComponent(id);
    if (/^BV[0-9A-Za-z]+$/i.test(raw)) return "https://www.bilibili.com/video/" + encodeURIComponent(raw);
    if (/^av\d+$/i.test(raw)) return "https://www.bilibili.com/video/" + encodeURIComponent(raw.toLowerCase());
    if (/^\d{15,}$/.test(raw)) return "https://www.bilibili.com/opus/" + encodeURIComponent(raw);
    return "https://www.bilibili.com/video/av" + id;
  }
  if (platform === "twitter") {
    return "https://x.com/i/web/status/" + id;
  }
  if (platform === "instagram") {
    return "https://www.instagram.com/p/" + id + "/";
  }
  return "https://www.douyin.com/video/" + id;
}
function openWork(platform, id) { try { window.open(workLink(platform, id), "_blank", "noopener"); } catch (e) {} }
function workCard(w) {
  const oc = `onclick="openWork(${jsArg(w.platform)},${jsArg(w.item_id)})"`;
  // 图裂时回退占位(onerror 换成灰底图标),避免绝对角标压到标题
  const cover = w.cover_url
    ? `<img class="ncard-cover" src="${esc(safeMediaUrl(w.cover_url))}" referrerpolicy="no-referrer" loading="lazy" alt="" ${oc}
         onerror="this.onerror=null;this.removeAttribute('src');this.style.visibility='hidden'">`
    : `<div class="ncard-cover ph" ${oc}>${ic("i-image")}</div>`;
  const title = esc(w.desc || "无描述");
  return `<div class="ncard">
    ${cover}
    <span class="ncard-type">${ic(w.media_type === "video" ? "i-play" : "i-image")}${w.media_type === "video" ? "视频" : "图文"}</span>
    <div class="ncard-body">
      <p class="ncard-title" style="cursor:pointer" title="${title}" ${oc}>${title}</p>
      <div class="ncard-foot">
        <span class="metric like">${ic("i-heart")}${fmtNum(w.like_count)}</span>
        <span class="metric">${ic("i-msg")}${fmtNum(w.comment_count)}</span>
        ${w.play_count ? `<span class="metric">${ic("i-play")}${fmtNum(w.play_count)}</span>` : ""}
        <span class="like">${fmtTime(w.create_time)}</span>
      </div>
      <div class="ncard-actions">
        ${w.platform === "douyin" ? `<button class="ghost sm" onclick="monitorOwnWorkDanmaku(${jsArg(w.item_id)},${Number(w.account_id) || "null"})">${ic("i-msg")}弹幕</button>` : ""}
        <button class="ghost sm" onclick="openWorkComments(${Number(w.id)},${jsArg(w.platform)},${jsArg(w.desc || "无描述")})">${ic("i-msg")}评论</button>
        ${w.platform === "rednote" ? `<button class="ghost sm" onclick="toggleRednoteWorkVisibility(${Number(w.id)},${jsArg(w.status || "")})">${w.status === "仅自己可见" ? "设为公开" : "设为私密"}</button><button class="ghost sm danger" onclick="deleteRednoteWork(${Number(w.id)})">${ic("i-trash")}</button>` : ""}
      </div>
    </div>
  </div>`;
}
function monitorOwnWorkDanmaku(itemId, accountId) {
  if (PLATFORM !== "douyin") switchPlatform("douyin");
  switchTab("danmaku");
  if ($("d-w-url")) $("d-w-url").value = itemId || "";
  if ($("d-w-kind")) $("d-w-kind").value = "video";
  if ($("d-w-mode")) $("d-w-mode").value = "creator";
  applyDanmakuForm();
  if ($("d-w-acc") && accountId) $("d-w-acc").value = String(accountId);
  toast("已填入作品 ID，请确认后开始弹幕监控", "info", 5000);
}
async function toggleRednoteWorkVisibility(workId, currentStatus) {
  const nextVis = currentStatus === "仅自己可见" ? "public" : "private";
  try {
    await api(`/api/account-works/${workId}/rednote-visibility`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ visibility: nextVis }),
    });
    toast(nextVis === "private" ? "已设为仅自己可见" : "已设为公开可见", "ok");
    refreshMyWorks();
  } catch (e) { toast("修改可见性失败:" + e.message, "err"); }
}
async function deleteRednoteWork(workId) {
  if (!await uiConfirm({ title: "删除海外小红书笔记", message: "确定从海外小红书平台删除这条已发布笔记吗？删除后不可恢复。", okText: "删除笔记", danger: true })) return;
  try {
    await api(`/api/account-works/${workId}/rednote-delete`, { method: "DELETE" });
    toast("已从海外小红书删除该笔记", "ok");
    refreshMyWorks();
  } catch (e) { toast("删除失败:" + e.message, "err"); }
}
async function syncMyWorks() {
  if (!HUB_ACC) { toast("请先选择账号", "err"); return; }
  await withBusy(evtBtn(), "同步中", async () => {
    try {
      const r = await api("/api/accounts/" + HUB_ACC + "/works/sync", { method: "POST" });
      if (r.skipped) { toast(`同步暂缓:${r.reason || "操作间隔尚未结束"}`, "info", 5000); return; }
      toast(`同步完成:抓到 ${Number(r.fetched) || 0} 条,新增 ${Number(r.added) || 0}${transportSourceSuffix(r.source)}`, "ok");
    }
    catch (e) { toast("同步失败:" + e.message, "err"); }
  });
  refreshMyWorks();
}

// ── 作品评论(弹窗:抖音直连分页 / 小红书客户端 / 快手拦截,落库后展示)──
let WC_WORK = null;   // 当前查看评论的作品 {id, platform, title}
async function openWorkComments(workId, platform, title) {
  WC_WORK = { id: workId, platform, title: title || "" };
  $("wc-title").textContent = "评论 · " + (title || "");
  $("wc-count").textContent = "加载中…";
  $("wc-list").innerHTML = "";
  $("wcmodal").style.display = "flex";
  modalOpened($("wcmodal"));
  setTimeout(() => $("wcmodal").querySelector(".pv-close").focus(), 0);
  await loadWorkComments();
}
function hideWorkComments() {
  $("wcmodal").style.display = "none"; WC_WORK = null;
  modalClosed($("wcmodal"));
}
async function loadWorkComments() {
  if (!WC_WORK) return;
  try {
    const list = await api("/api/account-works/" + WC_WORK.id + "/comments");
    $("wc-count").textContent = list.length ? (list.length + " 条(含回复)") : "暂无评论";
    $("wc-list").innerHTML = list.length ? list.map(cmtRow).join("")
      : `<div class="empty" style="padding:26px"><div class="empty-ic">${ic("i-msg")}</div><div class="empty-t">还没抓到评论</div><div class="empty-sub">点右上「抓取评论」用该账号登录态拉取</div></div>`;
  } catch (e) {
    $("wc-count").textContent = "—";
    $("wc-list").innerHTML = `<div class="empty" style="padding:24px"><div class="empty-t">加载失败:${esc(e.message)}</div></div>`;
  }
}
function cmtRow(c) {
  return `<div class="wc-item${c.is_reply ? " reply" : ""}">
    <div class="wc-head"><b>${esc(c.user_nickname || "匿名")}</b><span class="wc-time">${fmtTime(c.create_time)}</span></div>
    ${c.user_sec_uid ? `<div class="comment-user-sec" title="${esc(c.user_sec_uid)}">sec_uid: ${esc(c.user_sec_uid)}</div>` : ""}
    <div class="wc-text">${esc(c.text || "")}</div>
    <div class="wc-meta">${ic("i-heart")}${fmtNum(c.like_count)}${c.is_reply ? " · 回复" : ""}</div>
  </div>`;
}
async function syncWorkComments() {
  if (!WC_WORK) return;
  await withBusy(evtBtn(), "抓取中", async () => {
    try {
      const r = await api("/api/account-works/" + WC_WORK.id + "/comments/sync", { method: "POST" });
      if (r.skipped) { toast(`抓取暂缓:${r.reason || "操作间隔尚未结束"}`, "info", 5000); return; }
      toast(`抓到 ${Number(r.fetched) || 0} 条,新增 ${Number(r.added) || 0}${transportSourceSuffix(r.source)}`, "ok");
    }
    catch (e) { toast("抓取失败:" + e.message, "err"); }
  });
  await loadWorkComments();
}

// ── 关注 / 粉丝 ──
// 小红书网页端不提供关注/粉丝列表(App 专属:实测无接口、无弹层),不做无用的同步
const XHS_FOLLOW_NA = "小红书网页端不提供关注 / 粉丝列表(仅 App 可见),无法同步。抖音 / 快手可正常同步。";
const FOLLOW_PAGE_SIZE = 50;
const FOLLOW_STATE = {
  following: { page: 1, pages: 1, total: 0, query: "" },
  fan: { page: 1, pages: 1, total: 0, query: "" },
};
const FOLLOW_JOB_IDS = { following: "", fan: "" };
const FOLLOW_JOB_POLLING = { following: "", fan: "" };
const FOLLOW_SEARCH_TIMERS = { following: null, fan: null };

function followElementPrefix(direction) { return direction === "fan" ? "fan" : "following"; }
function queueFollowSearch(direction) {
  clearTimeout(FOLLOW_SEARCH_TIMERS[direction]);
  FOLLOW_SEARCH_TIMERS[direction] = setTimeout(() => {
    const input = $(`${followElementPrefix(direction)}-search`);
    FOLLOW_STATE[direction].query = String(input?.value || "").trim();
    refreshFollows(direction, 1);
  }, 250);
}
function renderFollowPager(direction, payload) {
  const prefix = followElementPrefix(direction), state = FOLLOW_STATE[direction];
  state.page = Math.max(1, Number(payload.page || 1));
  state.pages = Math.max(1, Number(payload.pages || 1));
  state.total = Math.max(0, Number(payload.total || 0));
  const pager = $(`${prefix}-pager`), info = $(`${prefix}-page-info`);
  if (!pager || !info) return;
  info.textContent = `第 ${state.page} / ${state.pages} 页 · 共 ${fmtNum(state.total)} 条`;
  const buttons = pager.querySelectorAll("button");
  if (buttons[0]) buttons[0].disabled = state.page <= 1;
  if (buttons[1]) buttons[1].disabled = state.page >= state.pages;
  const synced = $(`${prefix}-synced-at`);
  if (synced) synced.textContent = payload.synced_at ? `同步于 ${fmtTime(payload.synced_at)}` : "";
  pager.hidden = state.total <= 0;
}
function changeFollowPage(direction, delta) {
  const state = FOLLOW_STATE[direction];
  refreshFollows(direction, Math.max(1, Math.min(state.pages, state.page + Number(delta || 0))));
}
function renderFollowSyncStatus(direction, job) {
  const box = $(`${followElementPrefix(direction)}-sync-status`); if (!box) return;
  if (!job || job.status === "idle") {
    FOLLOW_JOB_IDS[direction] = "";
    box.hidden = true; box.innerHTML = ""; return;
  }
  FOLLOW_JOB_IDS[direction] = job.id || FOLLOW_JOB_IDS[direction];
  const phase = {
    queued: "等待同步", fetching: "正在获取", saving: "正在写入",
    completed: "同步完成", failed: "同步失败", canceled: "已取消",
    canceling: "正在取消", deferred: "同步暂缓",
  }[job.phase || job.status] || "同步中";
  const fetched = Math.max(0, Number(job.fetched || 0));
  const expected = Math.max(0, Number(job.expected_total || 0));
  const saved = Math.max(0, Number(job.saved || 0));
  const cappedNote = job.status === "completed" && (job.capped || (expected > fetched && fetched > 0))
    ? "（平台仅返回可见用户）" : "";
  const detail = job.phase === "saving"
    ? `已写入 ${fmtNum(saved)} / ${fmtNum(fetched)} 条`
    : expected ? `已获取 ${fmtNum(fetched)} / 约 ${fmtNum(expected)} 条${cappedNote} · ${fmtNum(job.pages || 0)} 页`
      : `已获取 ${fmtNum(fetched)} 条 · ${fmtNum(job.pages || 0)} 页`;
  const percent = job.percent == null ? 0 : Math.max(0, Math.min(100, Number(job.percent)));
  const error = job.error ? `<div class="mut" style="margin-top:6px;color:var(--danger)">${esc(job.error)}</div>` : "";
  box.innerHTML = `<div><div class="follow-sync-copy"><b>${esc(phase)}</b><span>${esc(detail)}</span></div>`
    + `<div class="progress-track" role="progressbar" aria-label="${esc(phase)}" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${percent}"><div class="progress-fill" style="width:${percent}%"></div></div>${error}</div>`
    + (job.cancelable ? `<button class="ghost sm danger" onclick="cancelFollowSync(${jsArg(direction)})">${ic("i-x")}取消</button>` : "");
  box.hidden = false;
}
async function pollFollowSync(direction, jobId) {
  if (!jobId || FOLLOW_JOB_POLLING[direction] === jobId) return;
  FOLLOW_JOB_POLLING[direction] = jobId;
  const accountId = HUB_ACC;
  try {
    while (FOLLOW_JOB_IDS[direction] === jobId && HUB_ACC === accountId) {
      const job = await api(`/api/follow-sync-jobs/${encodeURIComponent(jobId)}`);
      renderFollowSyncStatus(direction, job);
      if (["completed", "failed", "canceled", "deferred"].includes(job.status)) {
        if (job.status === "completed") {
          toast(`同步完成:共 ${fmtNum(job.saved || job.fetched || 0)} 条,新增 ${fmtNum(job.added || 0)}${transportSourceSuffix(job.source)}`, "ok", 5000);
          await refreshFollows(direction, 1);
          refreshHubSummary();
        } else if (job.status === "failed") toast("同步失败:" + (job.error || "未知错误"), "err");
        else if (job.status === "deferred") toast("同步暂缓:" + (job.error || "操作间隔尚未结束"), "info", 5000);
        return;
      }
      await new Promise(resolve => setTimeout(resolve, 1200));
    }
  } catch (e) {
    if (HUB_ACC === accountId) toast("读取同步进度失败:" + e.message, "err");
  } finally {
    if (FOLLOW_JOB_POLLING[direction] === jobId) FOLLOW_JOB_POLLING[direction] = "";
  }
}
async function refreshFollowSyncJob(direction) {
  if (!HUB_ACC) return;
  try {
    const job = await api(`/api/accounts/${HUB_ACC}/follows/sync-job?direction=${direction}`);
    renderFollowSyncStatus(direction, job);
    if (["queued", "running", "canceling"].includes(job.status)) pollFollowSync(direction, job.id);
  } catch (e) {}
}
async function refreshFollows(direction, page = null) {
  const isCurrent = beginViewRequest(`follows:${direction}`, () => String(HUB_ACC));
  const tbody = $(direction === "fan" ? "fans-table" : "following-table"); if (!tbody) return;
  if (PLATFORM === "xhs") {
    const badge = $(direction === "fan" ? "hb-fans" : "hb-following");
    if (badge) badge.textContent = "—";
    const pager = $(`${followElementPrefix(direction)}-pager`); if (pager) pager.hidden = true;
    tbody.innerHTML = empty(3, direction === "fan" ? "粉丝列表网页端不可用" : "关注列表网页端不可用",
      "i-info", XHS_FOLLOW_NA);
    return;
  }
  if (!HUB_ACC) { tbody.innerHTML = empty(3, "请先选择已登录账号", "i-user"); return; }
  const state = FOLLOW_STATE[direction];
  if (page != null) state.page = Math.max(1, Number(page || 1));
  try {
    const params = new URLSearchParams({ account_id: HUB_ACC, direction,
      page: String(state.page), page_size: String(FOLLOW_PAGE_SIZE) });
    if (state.query) params.set("query", state.query);
    const payload = await api(`/api/follows?${params}`);
    if (!isCurrent()) return;
    const list = payload.items || [];
    const badge = $(direction === "fan" ? "hb-fans" : "hb-following");
    if (badge && !state.query) badge.textContent = payload.total || 0;
    tbody.innerHTML = list.length ? list.map(f => followRow(f, direction)).join("")
      : empty(3, state.query ? "没有匹配用户" : (direction === "fan" ? "暂无粉丝数据" : "暂无关注数据"), "i-user");
    renderFollowPager(direction, payload);
    refreshFollowSyncJob(direction);
  } catch (e) { if (!isCurrent()) return; tbody.innerHTML = empty(3, "加载失败:" + e.message, "i-info"); }
}
function followRow(f, direction) {
  const rel = f.is_mutual ? `<span class="pill active bare">互相关注</span>`
    : f.is_following ? `<span class="pill bare">已关注</span>`
      : `<span class="pill bare" style="color:var(--mut)">未关注</span>`;
  const act = f.is_following
    ? `<button class="ghost sm" onclick="actFollow('unfollow',${f.id})">取关</button>`
    : `<button class="ghost sm" onclick="actFollow('follow',${f.id})">回关</button>`;
  return `<tr>
    <td><div class="fu-cell">
      ${f.avatar ? `<img class="avatar" src="${esc(safeMediaUrl(f.avatar))}" referrerpolicy="no-referrer" alt="">` : `<span class="wb-avatar-fallback" style="width:36px;height:36px;flex:0 0 36px;font-size:15px" aria-hidden="true">${esc((f.nickname || "用").slice(0, 1))}</span>`}
      <div><div><b>${esc(f.nickname)}</b></div>${f.signature ? `<div class="fu-sign">${esc(f.signature)}</div>` : ""}</div>
    </div></td>
    <td>${rel}</td>
    <td class="acttd">${act}</td>
  </tr>`;
}
async function syncFollows(direction) {
  if (PLATFORM === "xhs") { toast(XHS_FOLLOW_NA, "info", 6000); return; }
  if (!HUB_ACC) { toast("请先选择账号", "err"); return; }
  let job = null;
  try {
    await withBusy(evtBtn(), "启动中", async () => {
      job = await api(`/api/accounts/${HUB_ACC}/follows/sync-jobs?direction=${direction}`, { method: "POST" });
    });
  } catch (e) { toast("同步失败:" + e.message, "err"); return; }
  renderFollowSyncStatus(direction, job);
  toast(job.status === "queued" ? "同步任务已创建" : "同步任务正在运行", "info");
  pollFollowSync(direction, job.id);
}
async function cancelFollowSync(direction) {
  const jobId = FOLLOW_JOB_IDS[direction]; if (!jobId) return;
  try {
    const job = await api(`/api/follow-sync-jobs/${encodeURIComponent(jobId)}/cancel`, { method: "POST" });
    renderFollowSyncStatus(direction, job);
  } catch (e) { toast("取消失败:" + e.message, "err"); }
}
async function actFollow(action, edgeId) {
  // 取该行 follow 边的目标信息(从已渲染列表里拿)
  const accountId = HUB_ACC;
  const dir = HUB_TAB === "fans" ? "fan" : "following";
  let edge = null;
  try { edge = await api(`/api/follows/${edgeId}?account_id=${accountId}`); } catch (e) {}
  if (HUB_ACC !== accountId) return;
  if (!edge) { toast("找不到该用户,请重新同步", "err"); return; }
  const label = action === "unfollow" ? "取关" : "回关";
  // Keep the confirmation flow usable in lightweight/offline embeds where
  // the optional transport-matrix module has not been loaded yet.
  const route = typeof transportRoute === "function"
    ? transportRoute("douyin", "follow_write") : null;
  const routeHint = route?.effective_mode === "api"
    ? "将通过该账号的独立 API 会话执行，不会打开浏览器。"
    : route?.effective_mode === "hybrid"
      ? "将优先通过该账号的独立 API 会话执行；仅明确拒绝时回退账号浏览器。"
      : "将打开该账号自己的浏览器 Profile 执行。";
  if (!await uiConfirm({ title: label + "确认", message: `确认对「${edge.nickname}」${label}?${routeHint}`, danger: action === "unfollow" })) return;
  if (HUB_ACC !== accountId) { toast("账号已切换，本次操作已取消", "info"); return; }
  await withBusy(evtBtn(), label + "中", async () => {
    try {
      const result = await api("/api/account-actions", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ account_id: +accountId, action, target_uid: edge.uid, target_sec_uid: edge.sec_uid || "", target_nick: edge.nickname, run_now: true })
      });
      toast(result.ran ? `${label}成功${transportSourceSuffix(result.method)}` : `任务 #${result.id} 已保留：${result.execution_error || "等待队列执行"}`, result.ran ? "ok" : "info", 6000);
    } catch (e) { toast(label + "失败:" + e.message, "err"); }
  });
  if (HUB_ACC === accountId) refreshFollows(dir);
}
