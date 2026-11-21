// 11-share-notifications.js — 通用分享链接无水印下载、下载历史管理及通知渠道与 Webhook。
// ─── 通用分享链接下载 ───
let SHARE_LINKS = [], SHARE_LINK_INDEX = 0, SHARE_SOURCE = "", SHARE_ACCOUNTS = [];
let SHARE_HISTORY = [], SHARE_HISTORY_PAGE = 1, SHARE_HISTORY_PAGE_SIZE = 10, SHARE_HISTORY_TOTAL = 0;
const selShareHistory = new Set();

async function loadShareAccounts() {
  const sel = $("sd-account");
  if (!sel) return;
  try {
    SHARE_ACCOUNTS = await api("/api/accounts");
    filterShareAccounts();
  } catch (e) {}
}

function setShareLinkIndex(index) {
  SHARE_LINK_INDEX = Number(index) || 0;
  filterShareAccounts();
}

function shareAccountPlatform() {
  const platform = (SHARE_LINKS[SHARE_LINK_INDEX] || {}).platform || "";
  // 链接识别名与账号表平台名的少量映射。
  return platform === "wechat" ? "shipinhao" : platform;
}

function filterShareAccounts() {
  const sel = $("sd-account");
  if (!sel) return;
  const old = sel.value;
  const platform = shareAccountPlatform();
  const hasDetectedLink = !!SHARE_LINKS.length;
  const knownAccountPlatform = ["douyin", "xhs", "rednote", "kuaishou", "shipinhao", "tiktok", "bilibili", "instagram", "twitter"].includes(platform);
  const rows = knownAccountPlatform
    ? SHARE_ACCOUNTS.filter(a => a.platform === platform)
    : [];
  const platformLabel = PF_NAME[platform] || platform || "";
  const emptyLabel = !hasDetectedLink
    ? "先识别链接，再选择对应平台账号"
    : knownAccountPlatform
      ? `不使用${platformLabel}账号登录态`
      : "该链接无需或暂无可复用账号";
  sel.innerHTML =
    `<option value="">${esc(emptyLabel)}</option>` +
    rows.map(a =>
      `<option value="${a.id}">${esc(PF_NAME[a.platform] || a.platform || "账号")} · ${esc(a.nickname || ("账号 " + a.id))}${a.status === "invalid" ? "（登录态可能失效）" : ""}</option>`
    ).join("");

  if (rows.some(a => String(a.id) === old)) {
    sel.value = old;
  } else {
    // 已识别为具体平台且只有一个可用账号时直接选中，图文下载无需用户再手选。
    const active = rows.filter(a => a.status !== "invalid");
    if (knownAccountPlatform && active.length === 1) sel.value = String(active[0].id);
  }
  csSyncAll();
}

function renderShareLinks(links) {
  const box = $("sd-links");
  SHARE_LINKS = links || [];
  SHARE_LINK_INDEX = Math.min(SHARE_LINK_INDEX, Math.max(0, SHARE_LINKS.length - 1));
  filterShareAccounts();
  if (!SHARE_LINKS.length) {
    box.style.display = "block";
    box.innerHTML = `<b>未识别到链接。</b> 请检查是否粘贴了完整分享内容。`;
    return;
  }
  const labels = SHARE_LINKS.map((link, i) => `
    <label style="display:flex;align-items:flex-start;gap:8px;margin-top:8px;cursor:pointer">
      <input type="radio" name="sd-link" value="${i}" ${i === SHARE_LINK_INDEX ? "checked" : ""}
        onchange="setShareLinkIndex(this.value)" style="width:auto;margin-top:3px">
      <span><b>${esc(link.platform === "generic" ? "通用站点" : (PF_NAME[link.platform] || link.platform))}</b>
      · ${esc(link.host)}<br><code style="word-break:break-all">${esc(link.url)}</code></span>
    </label>`).join("");
  box.style.display = "block";
  box.innerHTML = `<b>已识别 ${SHARE_LINKS.length} 条候选链接</b>${labels}`;
}

async function parseShareLinks(button = null) {
  const text = $("sd-text").value.trim();
  if (!text) { toast("请粘贴分享链接或完整分享文案", "err"); return null; }
  const btn = button || evtBtn();
  $("sd-msg").textContent = "正在清洗文案并识别链接…";
  return await withBusy(btn, "识别中", async () => {
    try {
      const result = await api("/api/share-download/links", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ share_text: text }),
      });
      SHARE_SOURCE = text;
      renderShareLinks(result.links);
      $("sd-msg").textContent = result.count ? `已识别 ${result.count} 条链接 ✓` : "未识别到链接";
      if (!result.count) toast("没有识别到 http(s) 链接", "err");
      return result;
    } catch (e) {
      $("sd-msg").textContent = "识别失败：" + e.message;
      toast("识别失败：" + e.message, "err");
      return null;
    }
  });
}

function shareRequestBody(download) {
  const text = $("sd-text").value.trim();
  const maxSize = Number($("sd-max-size").value || 0);
  const accountId = Number($("sd-account").value || 0);
  return {
    share_text: text,
    download,
    all_links: $("sd-all-links").checked,
    link_index: SHARE_LINK_INDEX,
    quality: $("sd-quality").value,
    output_dir: $("sd-dir").value.trim() || null,
    save_metadata: $("sd-metadata").checked,
    save_thumbnail: $("sd-thumbnail").checked,
    save_subtitles: $("sd-subtitles").checked,
    max_filesize_mb: Number.isFinite(maxSize) && maxSize > 0 ? Math.floor(maxSize) : 0,
    account_id: accountId || null,
  };
}

function fmtShareSize(bytes) {
  let n = Number(bytes || 0);
  if (n < 1024) return n + " B";
  const units = ["KB", "MB", "GB", "TB"];
  let i = -1;
  do { n /= 1024; i++; } while (n >= 1024 && i < units.length - 1);
  return n.toFixed(n >= 10 ? 1 : 2) + " " + units[i];
}

function copySharePath(button) {
  const value = button.dataset.path || "";
  navigator.clipboard.writeText(value).then(
    () => toast("本地路径已复制", "ok"),
    () => toast("复制失败，请手动复制路径", "err")
  );
}

function fmtShareHistoryTime(value) {
  if (!value) return "—";
  let text = String(value);
  if (!/[zZ]$|[+-]\d\d:\d\d$/.test(text)) text += "Z";
  const date = new Date(text);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString();
}

function shareHistoryMetadata(row) {
  return row && row.metadata && typeof row.metadata === "object" ? row.metadata : {};
}
function shareHistoryNumber(row, key) {
  const raw = row && row[key] != null ? row[key] : shareHistoryMetadata(row)[key];
  const value = Number(raw || 0);
  return Number.isFinite(value) ? value : 0;
}
function shareHistoryTitle(row) {
  const metadata = shareHistoryMetadata(row);
  return String((row && (row.desc || row.title)) || metadata.title || metadata.description ||
    ((row && row.status) === "failed" ? "下载失败" : "未命名作品"));
}
function shareHistoryType(row) {
  const value = String((row && (row.media_type || row.type)) || shareHistoryMetadata(row).media_type || "").toLowerCase();
  if (value === "images" || value === "image" || value === "图文") return "images";
  if (value === "video" || value === "视频") return "video";
  const path = shareHistoryFirstPath(row).toLowerCase();
  if (/\.(?:mp4|mov|webm|mkv|m4v|flv|avi)(?:$|[?#])/.test(path) || shareHistoryNumber(row, "duration") > 0) return "video";
  if (/\.(?:jpe?g|png|webp|gif|avif|heic)(?:$|[?#])/.test(path)) return "images";
  return value;
}
function shareHistoryCreateTime(row) {
  const direct = Number(row && row.create_time || 0);
  if (Number.isFinite(direct) && direct > 0) return direct;
  const metadata = shareHistoryMetadata(row);
  const timestamp = Number(metadata.timestamp || 0);
  if (Number.isFinite(timestamp) && timestamp > 0) return timestamp;
  const uploadDate = String(metadata.upload_date || "");
  if (/^\d{8}$/.test(uploadDate)) {
    const date = new Date(`${uploadDate.slice(0, 4)}-${uploadDate.slice(4, 6)}-${uploadDate.slice(6, 8)}T00:00:00`);
    if (!Number.isNaN(date.getTime())) return Math.floor(date.getTime() / 1000);
  }
  return 0;
}
function shareHistoryQuality(row) {
  const metadata = shareHistoryMetadata(row);
  const raw = String((row && row.quality) || metadata.format || metadata.format_id || "").replace(/\s+/g, " ").trim();
  if (!raw) return "";
  const width = Number(row && row.width || metadata.width || 0);
  const height = Number(row && row.height || metadata.height || 0);
  if (Number.isFinite(width) && Number.isFinite(height) && width > 0 && height > 0) return `${width}×${height}`;
  const level = raw.match(/(?:^|[_\s-])(\d{3,4})p(?:$|[_\s-])/i);
  if (level) return `${level[1]}P`;
  return raw.length > 14 ? `${raw.slice(0, 13)}…` : raw;
}
function shareHistoryPlatform(row) {
  return String((row && row.platform) || shareHistoryMetadata(row).platform || "generic");
}
function shareHistoryFiles(row) {
  return Array.isArray(row && row.files) ? row.files.filter(file => file && typeof file === "object") : [];
}
function shareHistoryMediaFiles(row) {
  return shareHistoryFiles(row).filter(file => file.role === "media");
}
function shareHistoryFirstPath(row) {
  const files = shareHistoryMediaFiles(row);
  const first = files[0] || shareHistoryFiles(row)[0] || {};
  return String(first.path || first.relative_path || "");
}
function shareHistoryPathCell(row) {
  const path = shareHistoryFirstPath(row);
  if (!path) return "";
  const p = contentPathMeta({ local_path: path, aweme_id: row.item_id });
  const files = shareHistoryFiles(row);
  const totalSize = files.reduce((sum, file) => sum + Number(file.size || 0), 0);
  const fileHint = files.length > 1
    ? `${files.length} 个文件${totalSize ? ` · ${fmtShareSize(totalSize)}` : ""}`
    : (totalSize ? fmtShareSize(totalSize) : "");
  const dirLabel = [p ? (p.dir || "当前目录") : "当前目录", fileHint].filter(Boolean).join(" · ");
  return `<div class="local-path" title="${esc(path)}">
    <div class="local-path-info">
      <div class="local-path-file"><span class="local-path-name" title="${esc(path)}">${esc(p ? p.name : path)}</span>${p && p.ext ? `<span class="local-path-ext">${esc(p.ext)}</span>` : ""}</div>
      <div class="local-path-dir" title="${esc(dirLabel)}">${esc(dirLabel)}</div>
    </div>
  </div>`;
}
function populateShareHistoryFacets() {
  const select = $("sd-history-platform");
  if (!select) return;
  const old = select.value;
  const platforms = [...new Set(SHARE_HISTORY.map(shareHistoryPlatform).filter(Boolean))].sort((a, b) => a.localeCompare(b, "zh-CN"));
  const sharePf = { wechat: "视频号", channels: "视频号", youtube: "YouTube", weibo: "微博", twitter: "X", x: "X", generic: "通用站点" };
  select.innerHTML = `<option value="">全部平台</option>` + platforms.map(platform =>
    `<option value="${esc(platform)}">${esc(PF_NAME[platform] || sharePf[platform] || platform)}</option>`).join("");
  select.value = platforms.includes(old) ? old : "";
  if (select._csSync) select._csSync();
}
function shareHistoryFilteredRows() {
  const query = (($('sd-history-search') && $('sd-history-search').value) || "").trim().toLocaleLowerCase();
  const platform = ($('sd-history-platform') && $('sd-history-platform').value) || "";
  const type = ($('sd-history-type') && $('sd-history-type').value) || "";
  const status = ($('sd-history-status') && $('sd-history-status').value) || "";
  return SHARE_HISTORY.filter(row => {
    if (platform && shareHistoryPlatform(row) !== platform) return false;
    if (type && shareHistoryType(row) !== type) return false;
    if (status && (row.status || row.download_status) !== status) return false;
    if (!query) return true;
    const metadata = shareHistoryMetadata(row);
    return [shareHistoryTitle(row), row.author, row.item_id, row.source_url, metadata.uploader, metadata.channel]
      .filter(Boolean).join(" ").toLocaleLowerCase().includes(query);
  });
}
function shareHistoryStatus(row) {
  const value = String((row && (row.status || row.download_status)) || "failed");
  return ["done", "failed"].includes(value) ? value : "failed";
}
function shareHistoryRow(row) {
  const metadata = shareHistoryMetadata(row);
  const type = shareHistoryType(row);
  const typeName = type === "images" ? "图文" : type === "video" ? "视频" : (row.media_type || "媒体");
  const status = shareHistoryStatus(row);
  const platform = shareHistoryPlatform(row);
  const sharePf = { wechat: "视频号", channels: "视频号", youtube: "YouTube", weibo: "微博", twitter: "X", x: "X", generic: "通用站点" };
  const platformName = PF_NAME[platform] || sharePf[platform] || platform || "通用站点";
  const cover = row.cover_url || metadata.thumbnail || "";
  const title = shareHistoryTitle(row);
  const author = row.author || metadata.uploader || metadata.channel || "";
  const itemId = row.item_id || row.aweme_id || metadata.id || "";
  const createTime = shareHistoryCreateTime(row);
  const likeCount = shareHistoryNumber(row, "like_count");
  const commentCount = shareHistoryNumber(row, "comment_count");
  const duration = shareHistoryNumber(row, "duration");
  const mediaCount = shareHistoryMediaFiles(row).length || Number(row.media_count || 0);
  const files = shareHistoryFiles(row);
  const error = row.error ? `<span class="warn-ic" data-tip="${esc(row.error)}">${ic("i-info")}</span>` : "";
  const downloadTime = row.created_at ? fmtShareHistoryTime(row.created_at) : "";
  const descriptionMeta = `<div class="share-history-meta">
    <span class="src-chip" title="${esc(platformName)}">${ic("i-link")}${esc(platformName)}</span>
    ${author ? `<span class="share-history-author" title="${esc(author)}">${esc(author)}</span>` : ""}
    ${itemId ? `<span class="share-history-id" title="ID ${esc(itemId)}">ID ${esc(itemId)}</span>` : ""}
    ${downloadTime ? `<span class="share-history-download-time" title="下载于 ${esc(downloadTime)}">下载于 ${esc(downloadTime)}</span>` : ""}
  </div>`;
  const quality = shareHistoryQuality(row);
  const hasPath = Boolean(shareHistoryFirstPath(row));
  const durBadge = duration ? `<span class="thumb-dur">${esc(fmtDur(duration))}</span>` : "";
  const specSub = [quality, mediaCount > 1 ? `${mediaCount} 媒体` : (duration ? fmtDur(duration) : "")].filter(Boolean).join(" · ");
  return `<tr>
    <td class="content-check-cell"><input type="checkbox" data-id="${Number(row.id)}" onchange="shareHistoryToggleOne(${Number(row.id)},this.checked)" ${selShareHistory.has(row.id) ? "checked" : ""} aria-label="选择下载记录"></td>
    <td class="content-cover-cell"><span class="content-cover-box" onclick="openShareHistoryPreview(${Number(row.id)})" title="点击站内预览">${cover ? `<img class="thumb" src="${esc(cover)}" alt="${esc(title.slice(0, 20))}" referrerpolicy="no-referrer" loading="lazy">` : `<span class="content-cover-empty">${ic(type === "images" ? "i-image" : "i-film")}</span>`}${durBadge}</span></td>
    <td class="content-desc-cell"><div class="content-desc-text clickable" title="${esc(title)}" onclick="openShareHistoryPreview(${Number(row.id)})">${esc(title)}</div>${descriptionMeta}</td>
    <td><span class="content-kind">${esc(typeName)}</span>${specSub ? `<span class="content-quality">${esc(specSub)}</span>` : ""}</td>
    <td class="mut num">${contentTimeCell(createTime)}</td>
    <td class="content-metrics num"><span class="metric like">${ic("i-heart")}${fmtNum(likeCount)}</span>${commentCount ? `<span class="metric">${ic("i-msg")}${fmtNum(commentCount)}</span>` : ""}</td>
    <td class="content-status-cell"><div class="content-status-row"><span class="pill ${status}">${contentStatusLabel(status)}</span>${error}</div>${shareHistoryPathCell(row)}${row.error ? `<div class="content-error-preview" data-tip="${esc(row.error)}" title="${esc(row.error)}">${esc(row.error)}</div>` : ""}</td>
    <td class="content-action-cell acttd"><div class="content-action-buttons"><button type="button" class="ghost sm content-icon-btn" onclick="openShareHistoryPreview(${Number(row.id)})" data-tip="站内预览" title="站内预览" aria-label="预览媒体">${ic("i-eye")}</button>${hasPath ? `<button type="button" class="ghost sm content-icon-btn local-path-action reveal" onclick="revealShareHistoryPath(${Number(row.id)},this)" data-tip="在文件夹中显示" title="在文件夹中显示" aria-label="在文件夹中显示">${ic("i-folder")}</button>` : ""}<button type="button" class="ghost sm content-action-delete danger" onclick="deleteShareHistory(${Number(row.id)})" data-tip="删除记录" title="删除记录" aria-label="删除下载记录">${ic("i-trash")}</button></div></td>
  </tr>`;
}
function renderShareHistoryPager(total) {
  const pager = $("sd-history-pager");
  if (!pager) return;
  const pages = Math.max(1, Math.ceil(total / SHARE_HISTORY_PAGE_SIZE));
  if ($("sd-history-page-size")) $("sd-history-page-size").value = String(SHARE_HISTORY_PAGE_SIZE);
  if ($("sd-history-page-input")) {
    $("sd-history-page-input").value = String(SHARE_HISTORY_PAGE);
    $("sd-history-page-input").max = String(pages);
  }
  $("sd-history-page-info").textContent = `第 ${SHARE_HISTORY_PAGE} / ${pages} 页 · 共 ${fmtNum(total)} 条`;
  $("sd-history-first").disabled = SHARE_HISTORY_PAGE <= 1;
  $("sd-history-prev").disabled = SHARE_HISTORY_PAGE <= 1;
  $("sd-history-next").disabled = SHARE_HISTORY_PAGE >= pages;
  $("sd-history-last").disabled = SHARE_HISTORY_PAGE >= pages;
  pager.hidden = total <= SHARE_HISTORY_PAGE_SIZE;
}
function updateShareHistorySelBar() {
  const count = selShareHistory.size;
  $("sd-history-selcount").textContent = "已选 " + count + " 项";
  $("sd-history-selbar").style.display = count ? "inline-flex" : "none";
  const barWrap = $("sd-history-selbar")?.closest?.(".wb-bulk-bar");
  if (barWrap) barWrap.hidden = !count;
  const ids = [...document.querySelectorAll('#sd-history-body input[type="checkbox"]')].map(cb => +cb.dataset.id).filter(Boolean);
  const allSelected = ids.length > 0 && ids.every(id => selShareHistory.has(id));
  const selectedOnPage = ids.filter(id => selShareHistory.has(id)).length;
  const toggle = $("sd-history-selall-btn"); if (toggle) toggle.textContent = allSelected ? "取消全选" : "全选";
  const checkbox = $("sd-history-selall"); if (checkbox) { checkbox.checked = allSelected; checkbox.indeterminate = selectedOnPage > 0 && !allSelected; }
}
function renderShareHistoryRows(resetPage = false) {
  if (resetPage) SHARE_HISTORY_PAGE = 1;
  const body = $("sd-history-body");
  if (!body) return;
  const rows = shareHistoryFilteredRows();
  const pages = Math.max(1, Math.ceil(rows.length / SHARE_HISTORY_PAGE_SIZE));
  if (SHARE_HISTORY_PAGE > pages) { SHARE_HISTORY_PAGE = pages; return renderShareHistoryRows(); }
  SHARE_HISTORY_TOTAL = rows.length;
  const start = (SHARE_HISTORY_PAGE - 1) * SHARE_HISTORY_PAGE_SIZE;
  const pageRows = rows.slice(start, start + SHARE_HISTORY_PAGE_SIZE);
  $("sd-history-count").textContent = `${SHARE_HISTORY.length} 条`;
  if ($("sd-history-filter-count")) $("sd-history-filter-count").textContent = `显示 ${rows.length} / ${SHARE_HISTORY.length}`;
  body.innerHTML = pageRows.map(shareHistoryRow).join("") || empty(8, rows.length ? "暂无下载历史" : (SHARE_HISTORY.length ? "没有匹配的下载历史" : "暂无下载历史"), "i-download", SHARE_HISTORY.length ? "调整筛选条件" : "开始下载后会自动记录；旧下载会从元数据文件补录");
  updateShareHistorySelBar();
  renderShareHistoryPager(rows.length);
}
async function refreshShareHistory() {
  const body = $("sd-history-body");
  if (!body) return;
  body.innerHTML = skeleton(8, 3);
  try {
    const rows = await api("/api/share-download/history?limit=500");
    SHARE_HISTORY = Array.isArray(rows) ? rows : [];
    populateShareHistoryFacets();
    const validIds = new Set(SHARE_HISTORY.map(row => row.id));
    [...selShareHistory].forEach(id => { if (!validIds.has(id)) selShareHistory.delete(id); });
    renderShareHistoryRows();
  } catch (e) {
    $("sd-history-count").textContent = "读取失败";
    if ($("sd-history-filter-count")) $("sd-history-filter-count").textContent = "";
    body.innerHTML = empty(8, "历史记录读取失败", "i-info", e.message);
  }
}

function _shareHistoryReportParams(full) {
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
  put("q", $("sd-history-search") && $("sd-history-search").value);
  put("platform", $("sd-history-platform") && $("sd-history-platform").value);
  put("media_type", $("sd-history-type") && $("sd-history-type").value);
  put("status", $("sd-history-status") && $("sd-history-status").value);
  return params;
}

async function exportShareHistoryReport(full = false, explicitBtn = null) {
  const btn = explicitBtn || evtBtn();
  const group = btn && btn.closest(".export-actions");
  const unlock = _lockExportGroup(group, btn);
  await withBusy(btn, full ? "全量导出" : "筛选导出", async () => {
    try {
      await _downloadExcelReport(
        "/api/reports/share-download-history.xlsx?" + _shareHistoryReportParams(full).toString(),
        "creatorhub_share_download_history.xlsx",
      );
      const count = $("sd-history-filter-count")?.textContent?.trim();
      toast(`链接下载历史 ${full ? "全量" : "筛选结果"} Excel 已导出${!full && count ? `（${count}）` : ""}`, "ok");
    } catch (e) {
      toast("下载历史导出失败: " + e.message, "err");
    } finally {
      unlock();
    }
  });
}

function shareHistoryToggleOne(id, on) { on ? selShareHistory.add(id) : selShareHistory.delete(id); updateShareHistorySelBar(); }
function shareHistoryToggleAll(on) {
  document.querySelectorAll('#sd-history-body input[type="checkbox"]').forEach(cb => {
    const id = +cb.dataset.id; if (!id) return;
    cb.checked = on; on ? selShareHistory.add(id) : selShareHistory.delete(id);
  });
  updateShareHistorySelBar();
}
function shareHistorySelAllToggle() {
  const ids = [...document.querySelectorAll('#sd-history-body input[type="checkbox"]')].map(cb => +cb.dataset.id).filter(Boolean);
  const allSelected = ids.length > 0 && ids.every(id => selShareHistory.has(id));
  shareHistoryToggleAll(!allSelected);
}
function shareHistorySelClear() { selShareHistory.clear(); renderShareHistoryRows(); }
async function shareHistoryBatchDelete() {
  if (!selShareHistory.size) return;
  if (!await uiConfirm({ title: "批量删除下载历史", message: `删除选中的 ${selShareHistory.size} 条历史记录?本地媒体文件会保留。`, okText: "删除记录", danger: true })) return;
  try {
    const result = await api("/api/share-download/history/batch-delete", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ids: [...selShareHistory] }),
    });
    toast(`已删除 ${result.deleted || 0} 条历史记录，本地文件未删除`, "ok");
    selShareHistory.clear();
    refreshShareHistory();
  } catch (e) { toast("批量删除失败:" + e.message, "err"); }
}
function goShareHistoryPage(page) {
  const pages = Math.max(1, Math.ceil(SHARE_HISTORY_TOTAL / SHARE_HISTORY_PAGE_SIZE));
  const target = page <= 0 ? pages : Math.min(pages, Math.max(1, Math.round(Number(page) || 1)));
  if (target === SHARE_HISTORY_PAGE) return;
  SHARE_HISTORY_PAGE = target; renderShareHistoryRows();
}
function changeShareHistoryPage(delta) { goShareHistoryPage(SHARE_HISTORY_PAGE + Number(delta || 0)); }
function jumpShareHistoryPage() {
  const input = $("sd-history-page-input");
  const value = input ? Number(input.value) : 1;
  if (!Number.isFinite(value) || value < 1) { if (input) input.value = String(SHARE_HISTORY_PAGE); return; }
  goShareHistoryPage(value);
}
function handleShareHistoryPageInput(event) { if (event && event.key === "Enter") { event.preventDefault(); jumpShareHistoryPage(); } }
function setShareHistoryPageSize() {
  const value = +(($('sd-history-page-size') && $('sd-history-page-size').value) || 10);
  SHARE_HISTORY_PAGE_SIZE = [10, 20, 50].includes(value) ? value : 10;
  SHARE_HISTORY_PAGE = 1; renderShareHistoryRows();
}
async function revealShareHistoryPath(id, btn) {
  const old = btn && btn.innerHTML;
  if (btn) { btn.disabled = true; btn.innerHTML = `<span class="spin"></span>`; }
  try {
    await api(`/api/share-download/history/${id}/reveal`, { method: "POST", headers: { "X-CreatorHub-Local-Action": "reveal" } });
    toast("已在文件夹中显示", "ok", 1800);
  } catch (e) { toast("打开文件夹失败:" + e.message, "err"); }
  finally { if (btn && btn.isConnected) { btn.disabled = false; btn.innerHTML = old; } }
}
function openShareHistoryPreview(id, startIdx) {
  return _pvOpen(() => api(`/api/share-download/history/${id}/preview`), startIdx || 0);
}
async function deleteShareHistory(id) {
  const ok = await uiConfirm({
    title: "删除下载历史",
    message: "只删除这条历史记录，本地媒体文件会保留。",
    okText: "删除记录",
    danger: true,
  });
  if (!ok) return;
  try {
    await api(`/api/share-download/history/${id}`, { method: "DELETE" });
    toast("历史记录已删除，本地文件未删除", "ok");
    selShareHistory.delete(id);
    refreshShareHistory();
  } catch (e) {
    toast("删除历史失败：" + e.message, "err");
  }
}

function renderShareResult(response, download) {
  const card = $("sd-result-card"), box = $("sd-result");
  const results = response.results || [];
  card.style.display = "block";
  $("sd-result-summary").textContent = `${results.filter(x => x.ok).length}/${results.length} 成功`;
  box.innerHTML = results.map((item, index) => {
    if (!item.ok) return `<div class="hint" style="margin-bottom:10px;border-color:var(--danger)">
      <b>第 ${index + 1} 条处理失败</b><br><span style="color:var(--danger)">${esc(item.error || "未知错误")}</span>
      <br><code style="word-break:break-all">${esc(item.url || "")}</code></div>`;
    const m = item.metadata || {};
    const files = item.files || [];
    const warnings = item.warnings || [];
    const dataBits = [
      m.uploader ? `作者：${esc(m.uploader)}` : "",
      m.duration ? `时长：${esc(fmtDur(Math.round(m.duration)))}` : "",
      m.width && m.height ? `画面：${m.width}×${m.height}` : "",
      m.view_count != null ? `播放：${fmtNum(m.view_count)}` : "",
      m.like_count != null ? `点赞：${fmtNum(m.like_count)}` : "",
    ].filter(Boolean).join(" · ");
    const fileHtml = files.length ? files.map(file => `
      <div style="display:flex;gap:10px;align-items:center;padding:7px 0;border-top:1px solid var(--line-soft)">
        <span class="pill bare">${esc(file.role || "file")}</span>
        <code style="flex:1;min-width:0;overflow-wrap:anywhere">${esc(file.relative_path || file.name)}</code>
        <span class="mut">${fmtShareSize(file.size)}</span>
        <button class="ghost sm" data-path="${esc(file.path || "")}" onclick="copySharePath(this)">复制路径</button>
      </div>`).join("") : "";
    return `<div style="margin-bottom:${index + 1 < results.length ? "18px" : "0"}">
      <div style="font-size:16px;font-weight:700;margin-bottom:5px">${esc(m.title || "作品信息")}</div>
      <div class="mut">${dataBits || esc(item.input_platform || "")}</div>
      ${m.description ? `<div class="hint" style="margin-top:9px;white-space:pre-wrap;max-height:130px;overflow:auto">${esc(m.description)}</div>` : ""}
      ${warnings.length ? `<div class="hint" style="margin-top:9px;color:var(--warn)">${warnings.map(esc).join("<br>")}</div>` : ""}
      ${download ? `<div class="mut" style="margin-top:10px">保存目录：<code>${esc(item.output_dir || "")}</code></div>${fileHtml}` : ""}
    </div>`;
  }).join("") || `<div class="hint">没有返回处理结果</div>`;
  card.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

async function runShareDownload(download, button = null) {
  const btn = button || evtBtn();
  const text = $("sd-text").value.trim();
  if (!text) { toast("请粘贴分享链接或完整分享文案", "err"); return; }
  // 文案发生变化时先在本地重新识别，确保单选下标对应当前输入。
  if (SHARE_SOURCE !== text || !SHARE_LINKS.length) {
    const parsed = await parseShareLinks(null);
    if (!parsed || !parsed.count) return;
  }
  $("sd-msg").textContent = download ? "正在解析并下载，较大视频需要等待…" : "正在读取远端作品信息…";
  await withBusy(btn, download ? "下载中" : "读取中", async () => {
    try {
      const response = await api("/api/share-download", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(shareRequestBody(download)),
      });
      renderShareResult(response, download);
      if (download) refreshShareHistory();
      if (response.ok) {
        $("sd-msg").textContent = download ? "下载完成 ✓" : "作品信息读取完成 ✓";
        toast(download ? "链接作品下载完成" : "作品信息读取完成", "ok");
      } else {
        const first = (response.results || []).find(x => !x.ok);
        $("sd-msg").textContent = "处理完成，但有失败项：" + ((first && first.error) || "");
        toast("有链接处理失败，请查看结果", "err", 7000);
      }
    } catch (e) {
      $("sd-msg").textContent = "处理失败：" + e.message;
      toast("处理失败：" + e.message, "err", 7000);
    }
  });
}

function inspectShareLink(button = null) { return runShareDownload(false, button); }
function downloadShareLink(button = null) { return runShareDownload(true, button); }

// ─── 通知渠道 ───
const N_TEMPLATES = {
  bark: '{\n  "key": "你的Bark设备key",\n  "server": "https://api.day.app"\n}',
  dingtalk: '{\n  "webhook": "https://oapi.dingtalk.com/robot/send?access_token=xxx",\n  "secret": "加签密钥(可选)",\n  "keyword": "关键词(可选)"\n}',
  telegram: '{\n  "bot_token": "123:abc",\n  "chat_id": "你的chat_id"\n}',
};
function onTypeChange() {
  $("n-config").value = N_TEMPLATES[$("n-type").value] || "";
  setFieldError($("n-config"), "");
}
async function addChannel() {
  if (!validateNotificationConfig()) { $("n-msg").textContent = "请先修正渠道配置"; return; }
  let config;
  try { config = JSON.parse($("n-config").value || "{}"); }
  catch (e) { $("n-msg").textContent = "配置不是合法 JSON"; toast("配置不是合法 JSON", "err"); return; }
  $("n-msg").textContent = "添加中…";
  await withBusy(evtBtn(), "添加中", async () => {
  try {
    await api("/api/notifications", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: $("n-name").value.trim(), type: $("n-type").value, config }),
    });
    $("n-name").value = ""; $("n-msg").textContent = "已添加 ✓"; toast("通知渠道已添加", "ok");
    globalThis.CreatorHubWorkbench?.completed?.("notifications");
    refreshChannels();
  } catch (e) { $("n-msg").textContent = "失败: " + e.message; toast("添加失败:" + e.message, "err"); }
  });
}
async function refreshChannels() {
  const cs = await api("/api/notifications");
  CHANNELS = cs;
  $("n-table").querySelector("tbody").innerHTML = cs.map(c => `<tr>
    <td>${esc(c.name)} <span class="mut">${c.type}</span></td>
    <td><span class="pill ${c.enabled ? "active" : "invalid"}">${c.enabled ? "启用" : "停用"}</span></td>
    <td class="acttd">
      <button class="ghost sm" onclick="editChannel(${c.id})">编辑</button>
      <button class="ghost sm" onclick="testChannel(${c.id})">测试</button>
      <button class="ghost sm" onclick="toggleChannel(${c.id}, ${!c.enabled})">${c.enabled ? "停用" : "启用"}</button>
      <button class="ghost sm danger" onclick="delChannel(${c.id})">${ic("i-trash")}删除</button>
    </td></tr>`).join("") || empty(3, "还没有通知渠道", "i-bell", "配置 Bark、钉钉或 Telegram，接收新作品与评论提醒。", "notifications");
}
async function refreshWebhooks() {
  if (!$("wh-table")) return;
  try {
    const [subscriptions, events] = await Promise.all([
      api("/api/webhooks"), api("/api/webhook-events?limit=30"),
    ]);
    WEBHOOKS = subscriptions || [];
    $("wh-table").innerHTML = WEBHOOKS.map(row => `<tr>
      <td><b>${esc(row.name || "Webhook")}</b><div class="mut">${row.secret_configured ? "签名密钥已配置" : "未配置签名密钥"}</div></td>
      <td class="wrap" style="max-width:280px;word-break:break-all"><code title="${esc(row.url)}">${esc(row.url)}</code></td>
      <td>${(row.event_types || []).map(value => `<span class="meta-chip">${esc(value)}</span>`).join("")}</td>
      <td><span class="pill ${row.enabled ? "active" : "invalid"}">${row.enabled ? "启用" : "停用"}</span></td>
      <td class="acttd"><button class="ghost sm" onclick="editWebhook(${row.id})">编辑</button><button class="ghost sm" onclick="toggleWebhook(${row.id},${!row.enabled})">${row.enabled ? "停用" : "启用"}</button><button class="ghost sm danger" onclick="deleteWebhook(${row.id})">${ic("i-trash")}删除</button></td></tr>`).join("") || empty(5, "还没有 Webhook 订阅", "i-bolt", "添加订阅后，账号操作和业务变化会进入事件投递队列");
    const whSt = { delivered: "已投递", failed: "投递失败", pending: "待投递", sending: "投递中" };
    $("wh-event-table").innerHTML = (events || []).map(row => `<tr>
      <td><b>${esc(row.event_type)}</b></td><td>${esc([row.aggregate_type, row.aggregate_id].filter(Boolean).join(" / ") || "—")}</td>
      <td><span class="pill ${row.status === "delivered" ? "active" : row.status === "failed" ? "invalid" : "pending"}">${esc(whSt[row.status] || row.status)}</span>${row.last_error ? `<div class="mut" title="${esc(row.last_error)}">${esc(row.last_error.slice(0, 100))}</div>` : ""}</td>
      <td>${Number(row.delivered_count || 0)} 成功 / ${Number(row.attempts || 0)} 次</td><td>${collectionDate(row.created_at)}</td>
      <td class="acttd"><button class="ghost sm" onclick="dispatchWebhookEvent(${row.id})">${ic("i-play")}投递</button></td></tr>`).join("") || empty(6, "还没有事件", "i-inbox", "业务事件产生后会显示在这里");
  } catch (e) { toast("Webhook 事件中心加载失败：" + e.message, "err"); }
}
function cancelWebhookEdit() {
  WEBHOOK_EDIT_ID = 0;
  ["wh-name", "wh-url", "wh-secret"].forEach(id => { if ($(id)) $(id).value = ""; });
  if ($("wh-events")) $("wh-events").value = "*";
  $("wh-cancel").hidden = true; $("wh-save").lastChild.textContent = "添加订阅";
  $("wh-msg").textContent = "";
}
function editWebhook(id) {
  const row = WEBHOOKS.find(value => value.id === Number(id)); if (!row) return;
  WEBHOOK_EDIT_ID = row.id;
  $("wh-name").value = row.name || ""; $("wh-url").value = row.url || "";
  $("wh-events").value = (row.event_types || ["*"]).join(","); $("wh-secret").value = "";
  $("wh-cancel").hidden = false; $("wh-save").lastChild.textContent = "保存订阅";
  $("wh-url").focus();
}
async function saveWebhook() {
  const url = $("wh-url").value.trim();
  if (!url) { setFieldError($("wh-url"), "请填写接收地址"); return; }
  setFieldError($("wh-url"), "");
  const payload = { name: $("wh-name").value.trim(), url,
    event_types: parseCollectionKeywords($("wh-events").value) };
  const secret = $("wh-secret").value;
  if (!WEBHOOK_EDIT_ID || secret) payload.secret = secret;
  try {
    await api(WEBHOOK_EDIT_ID ? `/api/webhooks/${WEBHOOK_EDIT_ID}` : "/api/webhooks", {
      method: WEBHOOK_EDIT_ID ? "PATCH" : "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
    });
    toast(WEBHOOK_EDIT_ID ? "Webhook 订阅已更新" : "Webhook 订阅已添加", "ok");
    cancelWebhookEdit(); await refreshWebhooks();
  } catch (e) { $("wh-msg").textContent = "保存失败：" + e.message; }
}
async function toggleWebhook(id, enabled) {
  try { await api(`/api/webhooks/${id}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ enabled }) }); await refreshWebhooks(); }
  catch (e) { toast("更新订阅失败：" + e.message, "err"); }
}
async function deleteWebhook(id) {
  if (!await uiConfirm({ title: "删除 Webhook 订阅", message: "历史事件仍会保留。", okText: "删除", danger: true })) return;
  try { await api(`/api/webhooks/${id}`, { method: "DELETE" }); if (WEBHOOK_EDIT_ID === id) cancelWebhookEdit(); await refreshWebhooks(); }
  catch (e) { toast("删除订阅失败：" + e.message, "err"); }
}
async function dispatchWebhookEvent(id) {
  try { const result = await api(`/api/webhook-events/${id}/dispatch`, { method: "POST" }); toast(result.ok ? "事件已投递" : "事件部分投递失败", result.ok ? "ok" : "err"); await refreshWebhooks(); }
  catch (e) { toast("事件投递失败：" + e.message, "err"); }
}
async function dispatchPendingWebhooks() {
  try { const result = await api("/api/webhook-events/dispatch-pending", { method: "POST" }); toast(result.ok ? "待处理事件已投递" : "部分事件投递失败", result.ok ? "ok" : "err"); await refreshWebhooks(); }
  catch (e) { toast("批量投递失败：" + e.message, "err"); }
}
async function editChannel(id, draft = null) {
  const c = CHANNELS.find(x => x.id === id); if (!c) return;
  const initial = draft || { name: c.name || "", raw: JSON.stringify(c.config || {}, null, 2) };
  const value = await new Promise(res => {
    _uiResolve = res; _uiCancelVal = null;
    _uiGetVal = () => ({
      name: $("ec-name").value.trim(),
      raw: $("ec-config").value.trim(),
    });
    $("ui-body").innerHTML = `
      <div><label class="field" for="ec-name">渠道名称</label>
        <input id="ec-name" value="${esc(initial.name)}" maxlength="60"></div>
      <div><label class="field" for="ec-config">配置 JSON</label>
        <textarea id="ec-config" rows="9" spellcheck="false" aria-describedby="ec-config-hint">${esc(initial.raw)}</textarea>
        <p id="ec-config-hint" class="mut">密钥以 ******** 显示。保留占位或省略字段即保留原值；填写新值则替换，填写空字符串或 null 才会清空。</p></div>`;
    _uiOpen("编辑通知渠道", `类型：${c.type} · 密钥不回传到页面。修改后可发送测试通知。`, {
      okText: "保存修改", wide: true, icon: "i-bell",
      submit: value => {
        let config;
        try { config = JSON.parse(value.raw || "{}"); }
        catch (_) { uiEditorError("配置不是合法 JSON，请修正后再保存", "ec-config"); }
        if (!config || Array.isArray(config) || typeof config !== "object") uiEditorError("请使用 JSON 对象填写配置", "ec-config");
        return api("/api/notifications/" + id, {
          method: "PUT", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ name: value.name || c.type, config }),
        });
      },
    });
  });
  if (value === null) return;
  toast("通知渠道已更新", "ok"); refreshChannels();
}
async function testChannel(id) {
  const btn = event.target.closest("button"); btn.disabled = true; btn.textContent = "发送中…";
  try { const r = await api("/api/notifications/" + id + "/test", { method: "POST" }); btn.textContent = r.ok ? "成功 ✓" : "失败"; toast(r.ok ? "测试推送已发送" : "发送失败:" + (r.detail || ""), r.ok ? "ok" : "err"); }
  catch (e) { btn.textContent = "失败"; toast("发送失败:" + e.message, "err"); }
  setTimeout(() => { btn.disabled = false; btn.textContent = "测试"; }, 1500);
}
async function toggleChannel(id, enabled) { try { await api("/api/notifications/" + id, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ enabled }) }); refreshChannels(); } catch (e) { toast("操作失败:" + e.message, "err"); } }
async function delChannel(id) { if (await uiConfirm({ title: "删除渠道", message: "删除该通知渠道?", okText: "删除", danger: true })) { try { await api("/api/notifications/" + id, { method: "DELETE" }); toast("渠道已删除", "ok"); refreshChannels(); } catch (e) { toast("删除失败:" + e.message, "err"); } } }
