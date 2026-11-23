// 05-accounts.js — 账号列表渲染、环境指纹编辑、浏览器内核目录与账号级操作。
// ─── 账号 ───
let ACCOUNTS = [], ALL_ACCOUNTS = [];
function findAnyAccount(id) {
  const num = Number(id);
  return ALL_ACCOUNTS.find(x => Number(x.id) === num) || ACCOUNTS.find(x => Number(x.id) === num);
}
let BROWSER_RUNTIMES = [];
let BROWSER_RUNTIME_ROOT = "";
let MONITORS = [], WATCHES = [], CONTENTS = [];
let COLLECTION_JOBS = [], COLLECTION_JOB_ID = 0, COLLECTION_PAGE = 1;
let COLLECTION_COMMENT_CONTENT_ID = 0, COLLECTION_COMMENT_PAGE = 1;
let COLLECTION_COMMENT_TIMER = null, COLLECTION_COMMENT_REQUEST = 0;
let COLLECTION_FAVORITE_BOARDS = [];
let DANMAKU_WATCHES = [];
let CHANNELS = [], PUBLISH_TASKS = [], WEBHOOKS = [], WEBHOOK_EDIT_ID = 0;
let CONTENT_SRC = "", CONTENT_GROUP = "", CONTENT_TAG = "";
const CONTENT_SOURCE_CACHE = new Map();
let CONTENT_RENDER_SCOPE = "";
const WATCH_RECORD_STATE = { comment: { cache: new Map(), scope: "" }, danmaku: { cache: new Map(), scope: "" } };
let COMMENT_SRC = "", COMMENT_GROUP = "", COMMENT_TAG = "";
let DANMAKU_SRC = "";
let CONTENT_PAGE = 1, CONTENT_PAGE_SIZE = 10, CONTENT_TOTAL = 0;
let COMMENT_PAGE = 1, COMMENT_PAGE_SIZE = 10, COMMENT_TOTAL = 0;
let DANMAKU_PAGE = 1, DANMAKU_PAGE_SIZE = 10, DANMAKU_TOTAL = 0;
function parseTags(raw) {
  const seen = new Set();
  return String(raw || "").split(/[,，、;；\s]+/).map(x => x.trim()).filter(x => {
    const key = x.toLocaleLowerCase();
    if (!x || seen.has(key)) return false;
    seen.add(key); return true;
  }).slice(0, 12);
}
function parseDanmakuKeywords(raw) {
  const seen = new Set();
  return String(raw || "").split(/[,，、;；\n]+/).map(x => x.trim()).filter(x => {
    const key = x.toLocaleLowerCase();
    if (!x || seen.has(key)) return false;
    seen.add(key); return true;
  }).slice(0, 12);
}
function itemTags(item) { return Array.isArray(item && item.tags) ? item.tags : []; }
let OPEN_META_COMBO = null;
function metaCatalog(kind) {
  // 两类监控共享当前平台的分类词库；切换平台后不会带入其他平台的数据。
  const items = [...MONITORS, ...WATCHES, ...DANMAKU_WATCHES].filter(item => item.platform === PLATFORM);
  const values = kind === "group"
    ? items.map(item => item.group_name)
    : items.flatMap(itemTags);
  return [...new Set(values.filter(Boolean))]
    .sort((a, b) => a.localeCompare(b, "zh-CN"));
}
function getMetaValue(id) {
  const input = typeof id === "string" ? $(id) : id;
  if (!input) return "";
  if (input._metaControl) return input._metaControl.value();
  return input.value || "";
}
function setMetaValue(id, value) {
  const input = typeof id === "string" ? $(id) : id;
  if (!input) return;
  if (input._metaControl) input._metaControl.set(value);
  else input.value = value || "";
}
function enhanceMetaControl(input, kind) {
  if (!input || input._metaControl) return;
  kind = kind || input.dataset.metaCombo || "group";
  const initial = input.value || "";
  const wrap = document.createElement("div");
  wrap.className = "meta-combo";
  input.parentNode.insertBefore(wrap, input);
  wrap.appendChild(input);
  input.type = "hidden";

  const box = document.createElement("div");
  box.className = "meta-combo-box";
  const query = document.createElement("input");
  query.type = "text";
  query.className = "meta-combo-query";
  query.autocomplete = "off";
  query.maxLength = kind === "group" ? 40 : 24;
  query.placeholder = input.getAttribute("placeholder") || (kind === "group" ? "选择或输入新分组" : "选择或输入新标签");
  query.setAttribute("aria-label", kind === "group" ? "选择或新建分组" : "选择或新建标签");
  const arrow = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  arrow.setAttribute("class", "meta-combo-arr");
  arrow.setAttribute("viewBox", "0 0 24 24");
  arrow.setAttribute("fill", "none");
  arrow.setAttribute("stroke", "currentColor");
  arrow.setAttribute("stroke-width", "2");
  arrow.innerHTML = '<path d="m6 9 6 6 6-6"/>';
  const panel = document.createElement("div");
  panel.className = "meta-combo-panel";
  panel.hidden = true;
  panel.setAttribute("role", "listbox");
  if (kind === "tags") panel.setAttribute("aria-multiselectable", "true");
  box.appendChild(query);
  wrap.appendChild(box);
  wrap.appendChild(arrow);
  wrap.appendChild(panel);

  let selected = kind === "tags" ? parseTags(initial) : String(initial || "").trim();
  function syncHidden() {
    input.value = kind === "tags" ? selected.join(",") : selected;
  }
  function renderTokens() {
    box.querySelectorAll(".meta-token").forEach(node => node.remove());
    if (kind !== "tags") return;
    selected.forEach(tag => {
      const chip = document.createElement("span");
      chip.className = "meta-token";
      const label = document.createElement("span");
      label.textContent = tag;
      const remove = document.createElement("button");
      remove.type = "button";
      remove.textContent = "×";
      remove.setAttribute("aria-label", "移除标签 " + tag);
      remove.addEventListener("click", event => {
        event.stopPropagation();
        selected = selected.filter(value => value !== tag);
        syncHidden(); renderTokens(); renderPanel();
        input.dispatchEvent(new Event("change", { bubbles: true }));
      });
      chip.append(label, remove);
      box.insertBefore(chip, query);
    });
  }
  function choose(value, create = false) {
    value = String(value || "").trim().slice(0, kind === "group" ? 40 : 24);
    if (kind === "group") {
      selected = value;
      query.value = value;
      syncHidden();
      close();
    } else if (value) {
      selected = selected.includes(value)
        ? selected.filter(item => item !== value)
        : [...selected, value].slice(0, 12);
      query.value = "";
      syncHidden(); renderTokens(); renderPanel();
      query.focus();
    }
    input.dispatchEvent(new Event("change", { bubbles: true }));
  }
  function addOption(value, label, { isSelected = false, create = false, clear = false } = {}) {
    const option = document.createElement("button");
    option.type = "button";
    option.className = "meta-combo-opt" + (isSelected ? " selected" : "") + (create ? " create" : "");
    option.setAttribute("role", "option");
    option.setAttribute("aria-selected", isSelected ? "true" : "false");
    const mark = document.createElement("span");
    mark.className = "mark";
    mark.textContent = clear ? "×" : create ? "+" : isSelected ? "✓" : "";
    const text = document.createElement("span");
    text.textContent = label;
    option.append(mark, text);
    option.addEventListener("mousedown", event => event.preventDefault());
    option.addEventListener("click", () => choose(value, create));
    panel.appendChild(option);
  }
  function renderPanel() {
    panel.innerHTML = "";
    const rawQuery = query.value.trim();
    const needle = (kind === "group" && rawQuery === selected)
      ? "" : rawQuery.toLocaleLowerCase();
    let values = metaCatalog(kind);
    if (kind === "tags") values = [...new Set([...selected, ...values])];
    values = values.filter(value => !needle || value.toLocaleLowerCase().includes(needle));
    if (kind === "group" && !needle && selected) {
      addOption("", "不设置分组", { clear: true });
    }
    values.forEach(value => addOption(value, value, {
      isSelected: kind === "group" ? selected === value : selected.includes(value),
    }));
    const raw = rawQuery;
    const exact = metaCatalog(kind).some(value => value.toLocaleLowerCase() === raw.toLocaleLowerCase());
    if (raw && !exact && (kind === "group" ? raw !== selected : !selected.includes(raw))) {
      addOption(raw, `新建${kind === "group" ? "分组" : "标签"}“${raw}”`, { create: true });
    }
    if (!panel.children.length) {
      const empty = document.createElement("div");
      empty.className = "meta-combo-empty";
      empty.textContent = `暂无可选${kind === "group" ? "分组" : "标签"}，输入名称即可新建`;
      panel.appendChild(empty);
    }
  }
  function open() {
    if (OPEN_META_COMBO && OPEN_META_COMBO !== control) OPEN_META_COMBO.close();
    OPEN_META_COMBO = control;
    renderPanel();
    panel.hidden = false;
    wrap.classList.add("open");
  }
  function close() {
    panel.hidden = true;
    wrap.classList.remove("open");
    if (OPEN_META_COMBO === control) OPEN_META_COMBO = null;
  }
  function commit() {
    const raw = query.value.trim();
    if (kind === "tags" && raw) {
      const tag = raw.slice(0, 24);
      if (!selected.includes(tag) && selected.length < 12) selected.push(tag);
      query.value = "";
      syncHidden(); renderTokens();
      input.dispatchEvent(new Event("change", { bubbles: true }));
    }
    else if (kind === "group") {
      selected = raw.slice(0, 40);
      syncHidden();
    }
  }
  const control = {
    close,
    value() { commit(); return input.value || ""; },
    set(value) {
      selected = kind === "tags" ? parseTags(value) : String(value || "").trim().slice(0, 40);
      query.value = kind === "group" ? selected : "";
      syncHidden(); renderTokens();
      if (!panel.hidden) renderPanel();
    },
  };
  input._metaControl = control;
  query.addEventListener("focus", open);
  query.addEventListener("input", () => {
    if (kind === "group") {
      selected = query.value.trim().slice(0, 40);
      syncHidden();
    } else if (/[,，、;；]$/.test(query.value)) {
      parseTags(query.value).forEach(tag => {
        if (!selected.includes(tag) && selected.length < 12) selected.push(tag);
      });
      query.value = ""; syncHidden(); renderTokens();
    }
    open();
  });
  query.addEventListener("keydown", event => {
    if (event.key === "Enter") {
      event.preventDefault(); event.stopPropagation();
      const raw = query.value.trim();
      if (raw) choose(raw, true);
      else if (kind === "group") close();
    } else if (event.key === "Backspace" && kind === "tags" && !query.value && selected.length) {
      selected.pop(); syncHidden(); renderTokens(); renderPanel();
    } else if (event.key === "Escape") {
      close();
    }
  });
  box.addEventListener("mousedown", event => {
    if (event.target !== query && !event.target.closest(".meta-token button")) {
      event.preventDefault(); query.focus(); open();
    }
  });
  if (input.id) {
    const label = document.querySelector(`label[for="${input.id}"]`);
    if (label) label.addEventListener("click", event => {
      event.preventDefault(); query.focus(); open();
    });
  }
  control.set(initial);
}
function enhanceAllMetaControls(root) {
  (root || document).querySelectorAll("input[data-meta-combo]").forEach(input =>
    enhanceMetaControl(input, input.dataset.metaCombo));
}
document.addEventListener("mousedown", event => {
  if (OPEN_META_COMBO && !event.target.closest(".meta-combo")) OPEN_META_COMBO.close();
}, true);
function monitorBaseName(t) { return t.target_kind === "keyword" ? "#" + t.keyword : (t.nickname || (t.sec_uid || "").slice(0, 12)); }
function watchBaseName(w) { return w.title || w.aweme_id || (w.sec_uid || "").slice(0, 12); }
function monitorName(t) { const base = monitorBaseName(t); return t.alias ? `${t.alias} · ${base}` : base; }
function watchName(w) { const base = watchBaseName(w); return w.alias ? `${w.alias} · ${base}` : base; }
function monitorById(id) { return MONITORS.find(t => t.id === id); }
function watchById(id) { return WATCHES.find(w => w.id === id); }
function srcChip(name) { return `<span class="src-chip" title="来源监控:${esc(name)}">${ic("i-target")}${esc(name)}</span>`; }
function metaChips(item, limit = 2) {
  const tags = itemTags(item), shown = tags.slice(0, limit), rest = tags.length - shown.length;
  const parts = [];
  if (item && item.group_name) parts.push(`<span class="meta-chip group" title="分组:${esc(item.group_name)}">${esc(item.group_name)}</span>`);
  shown.forEach(tag => parts.push(`<span class="meta-chip tag" title="标签:${esc(tag)}">#${esc(tag)}</span>`));
  if (rest > 0) parts.push(`<span class="meta-chip more" title="${esc(tags.slice(limit).join("、"))}">+${rest}</span>`);
  return parts.length ? `<div class="meta-stack">${parts.join("")}</div>` : `<span class="meta-empty">未分组</span>`;
}
function sourceMeta(item) {
  if (!item) return "";
  const meta = (item.group_name || itemTags(item).length) ? metaChips(item, 1) : "";
  return `<div style="margin-top:4px">${srcChip(item.alias || (item.target_kind !== undefined ? monitorBaseName(item) : watchBaseName(item)))}</div>${meta ? `<div style="margin-top:4px">${meta}</div>` : ""}`;
}
function setFacetOptions(id, emptyLabel, values) {
  const sel = $(id); if (!sel) return "";
  const old = sel.value;
  const unique = [...new Set(values.filter(Boolean))].sort((a, b) => a.localeCompare(b, "zh-CN"));
  sel.innerHTML = `<option value="">${emptyLabel}</option>` +
    unique.map(v => `<option value="${esc(v)}">${esc(v)}</option>`).join("");
  sel.value = unique.includes(old) ? old : "";
  if (sel._csSync) sel._csSync();
  return sel.value;
}
function populateMonitorFacets() {
  setFacetOptions("mon-group", "全部分组", MONITORS.map(x => x.group_name));
  setFacetOptions("mon-tag", "全部标签", MONITORS.flatMap(itemTags));
  CONTENT_GROUP = setFacetOptions("content-group", "全部分组", MONITORS.map(x => x.group_name));
  CONTENT_TAG = setFacetOptions("content-tag", "全部标签", MONITORS.flatMap(itemTags));
}
function populateWatchFacets() {
  setFacetOptions("watch-group", "全部分组", WATCHES.map(x => x.group_name));
  setFacetOptions("watch-tag", "全部标签", WATCHES.flatMap(itemTags));
  COMMENT_GROUP = setFacetOptions("comment-group", "全部分组", WATCHES.map(x => x.group_name));
  COMMENT_TAG = setFacetOptions("comment-tag", "全部标签", WATCHES.flatMap(itemTags));
}
function populateContentSrc() {
  const sel = $("content-src"); if (!sel) return;
  const sources = new Map(CONTENT_SOURCE_CACHE);
  MONITORS.filter(t => t.platform === PLATFORM).forEach(t => sources.set(String(t.id), {
    id: t.id, name: monitorName(t), deleted: false, target_kind: t.target_kind,
  }));
  if (CONTENT_SRC && !sources.has(CONTENT_SRC)) sources.set(CONTENT_SRC, {
    id: CONTENT_SRC, name: `任务 #${CONTENT_SRC}（来源待确认）`,
  });
  sel.innerHTML = `<option value="">全部监控任务</option>` +
    [...sources.values()].map(t => `<option value="${esc(String(t.id))}">${esc(t.name)}${t.deleted ? "" : ` · #${esc(String(t.id))}`}</option>`).join("");
  sel.value = CONTENT_SRC;
  if (sel._csSync) sel._csSync();
  updateContentScope();
}

function contentSource(r) {
  if (r.source && String(r.source.id) === String(r.target_id)) return r.source;
  const cached = CONTENT_SOURCE_CACHE.get(String(r.target_id));
  if (cached) return cached;
  const t = MONITORS.find(item => item.platform === PLATFORM && String(item.id) === String(r.target_id));
  return t ? { id: t.id, name: monitorName(t), deleted: false, target_kind: t.target_kind }
    : CONTENT_SOURCE_CACHE.get(String(r.target_id)) || { id: r.target_id, name: `任务 #${r.target_id}（来源待确认）` };
}
function contentSourceMarkup(r) {
  const source = contentSource(r), id = Number(source.id);
  const kind = source.deleted ? "原任务已删除，历史记录保留" : source.target_kind === "keyword" ? "关键词监控" : "作品监控";
  const label = esc(source.name || `任务 #${id}`);
  return `<div class="content-origin"><span class="content-origin-label">来源任务</span>
    ${Number.isSafeInteger(id) && id > 0 ? `<button type="button" class="content-source-link" onclick="showMonitorRecords(${id})" title="查看此任务的记录">${label}</button>` : `<span>${label}</span>`}
    <span class="content-origin-meta">${id > 0 ? `任务 #${esc(String(source.id))} · ${kind}` : "来源任务信息未记录"}</span></div>`;
}
function updateContentScope() {
  const title = $("content-scope-name"), hint = $("content-scope-hint");
  const source = CONTENT_SRC ? contentSource({ target_id: CONTENT_SRC }) : null;
  if (title) title.textContent = source ? source.name : "全部监控任务";
  if (hint) hint.textContent = source
    ? `仅查看任务 #${CONTENT_SRC} 的记录${source.deleted ? " · 原任务已删除" : ""}；抓取时间指首次入库，重试不会改写。`
    : "当前平台的任务汇总；点击来源任务可单独查看。抓取时间与作品发布时间分开记录。";
  if ($("content-show-all")) $("content-show-all").hidden = !CONTENT_SRC;
}
function showMonitorRecords(id) {
  const value = id == null ? "" : String(id);
  if (value && !/^[1-9]\d*$/.test(value)) return;
  CONTENT_SRC = value; CONTENT_GROUP = CONTENT_TAG = "";
  ["content-group", "content-tag", "content-search", "content-type", "content-status",
    "content-min-likes", "content-min-comments", "content-captured-from", "content-captured-to"].forEach(key => {
    const node = $(key); if (node) { node.value = ""; if (node._csSync) node._csSync(); }
  });
  if ($("content-sort")) { $("content-sort").value = "captured_desc"; $("content-sort")._csSync?.(); }
  selContent.clear(); populateContentSrc();
  globalThis.CreatorHubBridge?.navigate?.("monitors", true);
  globalThis.CreatorHubWorkbench?.showSection?.("monitors", "records");
  refreshContents(true);
  requestAnimationFrame(() => $("content-scope-name")?.focus({ preventScroll: true }));
}
function backToMonitorTasks() {
  globalThis.CreatorHubWorkbench?.showSection?.("monitors", "targets");
  requestAnimationFrame(() => document.querySelector(`[data-monitor-records="${CONTENT_SRC}"]`)?.focus({ preventScroll: true }));
}
function contentCaptureBounds(prefix = "content") {
  const from = $(prefix + "-captured-from")?.value || "", to = $(prefix + "-captured-to")?.value || "";
  if (from && to && from > to) throw new Error("抓取开始日期应早于或等于结束日期");
  const bounds = {};
  for (const [key, value] of [["captured_from", from], ["captured_before", to]]) {
    if (!value) continue;
    const date = new Date(value + "T00:00:00");
    if (!Number.isFinite(date.getTime())) throw new Error("请填写有效的抓取日期");
    if (key === "captured_before") date.setDate(date.getDate() + 1);
    bounds[key] = date.toISOString();
  }
  return bounds;
}
function contentCapturedTime(raw, milliseconds = false) {
  if (!raw) return `<span class="mut">未记录</span>`;
  const value = String(raw), date = new Date(/(?:Z|[+-]\d{2}:?\d{2})$/i.test(value) ? value : value + "Z");
  if (!Number.isFinite(date.getTime())) return `<span class="mut">未记录</span>`;
  return `<time class="content-captured-time" datetime="${esc(date.toISOString())}"><span>${esc(date.toLocaleDateString("zh-CN", { year: "numeric", month: "2-digit", day: "2-digit" }))}</span><span>${esc(date.toLocaleTimeString("zh-CN", { hour12: false }))}${milliseconds ? "." + String(date.getMilliseconds()).padStart(3, "0") : ""}</span></time>`;
}
function populateCommentSrc() {
  populateWatchRecordSource("comment");
}

// Shared task provenance for comments and danmaku; acquisition channel remains separate.
function watchRecordConfig(kind) {
  if (kind !== "comment" && kind !== "danmaku") throw new Error("未知记录类型");
  return kind === "comment"
    ? { module: "comments", label: "评论", src: COMMENT_SRC, watches: WATCHES, refresh: refreshComments,
        pager: renderCommentPager, pageSize: COMMENT_PAGE_SIZE, state: WATCH_RECORD_STATE.comment }
    : { module: "danmaku", label: "弹幕", src: DANMAKU_SRC, watches: DANMAKU_WATCHES, refresh: refreshDanmaku,
        pager: renderDanmakuPager, pageSize: DANMAKU_PAGE_SIZE, state: WATCH_RECORD_STATE.danmaku };
}
function watchRecordSource(kind, row) {
  const view = watchRecordConfig(kind), id = Number(row.watch_id || 0), source = row.watch_source;
  if (source && source.module === view.module && source.platform === PLATFORM && Number(source.id) === id) return source;
  const watch = view.watches.find(w => w.platform === PLATFORM && Number(w.id) === id);
  if (watch) return { id, module: view.module, platform: PLATFORM, name: watchName(watch), kind: watch.kind, deleted: false };
  const cached = view.state.cache.get(String(id));
  if (cached && cached.module === view.module && cached.platform === PLATFORM) return cached;
  return { id, module: view.module, platform: PLATFORM, unassigned: id === 0,
    name: id > 0 ? `${view.label}任务 #${id}（来源待确认）` : `未关联${view.label}监控` };
}
function updateWatchRecordScope(kind) {
  const view = watchRecordConfig(kind), source = view.src !== "" ? watchRecordSource(kind, { watch_id: view.src }) : null;
  if ($(kind + "-scope-name")) $(kind + "-scope-name").textContent = source ? source.name : `全部${view.label}监控任务`;
  if ($(kind + "-scope-hint")) $(kind + "-scope-hint").textContent = source
    ? `${source.unassigned ? "仅查看未关联监控的记录" : `仅查看${view.label}任务 #${view.src} 的记录`}${source.deleted ? " · 原任务已删除" : ""}；抓取时间指首次入库，不是${view.label}发送时间。`
    : `当前平台的${view.label}记录汇总；点击来源任务可单独查看。抓取时间指首次入库。`;
  if ($(kind + "-show-all")) $(kind + "-show-all").hidden = view.src === "";
}
function populateWatchRecordSource(kind) {
  const view = watchRecordConfig(kind), sel = $(kind + "-src"); if (!sel) return;
  const sources = new Map([...view.state.cache].filter(([, s]) => s.platform === PLATFORM && s.module === view.module));
  view.watches.filter(w => w.platform === PLATFORM).forEach(w => sources.set(String(w.id), watchRecordSource(kind, { watch_id: w.id })));
  sources.set("0", watchRecordSource(kind, { watch_id: 0 }));
  if (view.src !== "" && !sources.has(view.src)) sources.set(view.src, watchRecordSource(kind, { watch_id: view.src }));
  sel.innerHTML = `<option value="">全部${view.label}监控任务</option>` + [...sources.values()].map(s =>
    `<option value="${esc(String(s.id))}">${esc(s.name)}${s.id > 0 && !s.deleted ? ` · #${esc(String(s.id))}` : ""}</option>`).join("");
  sel.value = view.src; sel._csSync?.(); updateWatchRecordScope(kind);
}
function showWatchRecords(kind, id) {
  if (kind !== "comment" && kind !== "danmaku") return;
  const value = id == null ? "" : String(id);
  if (value !== "" && (!/^\d+$/.test(value) || !Number.isSafeInteger(Number(value)))) return;
  const view = watchRecordConfig(kind);
  if (kind === "comment") { COMMENT_SRC = value; COMMENT_GROUP = COMMENT_TAG = ""; selComment.clear(); }
  else DANMAKU_SRC = value;
  for (const suffix of ["query", "ip-label", "group", "tag", "type", "min-likes", "time-start", "time-end", "captured-from", "captured-to"]) {
    const node = $(kind + "-" + suffix); if (node) { node.value = ""; node._csSync?.(); }
  }
  if ($(kind + "-sort")) { $(kind + "-sort").value = "captured_desc"; $(kind + "-sort")._csSync?.(); }
  populateWatchRecordSource(kind);
  globalThis.CreatorHubBridge?.navigate?.(view.module, true);
  globalThis.CreatorHubWorkbench?.showSection?.(view.module, "records");
  view.refresh(true);
  requestAnimationFrame(() => $(kind + "-scope-name")?.focus({ preventScroll: true }));
}
function backToWatchTasks(kind) {
  const view = watchRecordConfig(kind);
  globalThis.CreatorHubWorkbench?.showSection?.(view.module, "targets");
  requestAnimationFrame(() => document.querySelector(`[data-${kind}-records="${view.src}"]`)?.focus({ preventScroll: true }));
}
function watchRecordSourceMarkup(kind, row) {
  const view = watchRecordConfig(kind), source = watchRecordSource(kind, row), id = Number(source.id);
  const label = esc(source.name), linked = Number.isSafeInteger(id) && id >= 0;
  const detail = source.unassigned ? "来源监控信息未记录" : `${view.label}任务 #${id}${source.deleted ? " · 原任务已删除，历史记录保留" : source.kind === "user" ? " · 账号监控" : " · 作品监控"}`;
  return `<div class="content-origin"><span class="content-origin-label">来源任务</span>
    ${linked ? `<button type="button" class="content-source-link" onclick="showWatchRecords('${kind}',${id})" title="查看此来源的记录">${label}</button>` : `<span>${label}</span>`}
    <span class="content-origin-meta">${esc(detail)}</span>
    ${row.aweme_id ? `<span class="watch-record-work">作品 ID：${esc(row.aweme_id)}</span>` : ""}</div>`;
}
function watchRecordTimeMarkup(kind, row) {
  const captured = row.captured_at !== undefined ? row.captured_at : row.created_at;
  return `<span class="content-origin-label">抓取入库</span>${contentCapturedTime(captured, kind === "danmaku")}
    <div class="content-published-time">${kind === "comment" ? "评论发布" : "弹幕发送"} · ${fmtTime(row.create_time)}</div>`;
}
function prepareWatchRecordLoad(kind, resetPage) {
  const view = watchRecordConfig(kind), scope = PLATFORM + ":" + view.src;
  const clear = resetPage || view.state.scope !== scope; view.state.scope = scope;
  updateWatchRecordScope(kind);
  if (clear) {
    if (kind === "comment") { COMMENT_PAGE = 1; selComment.clear(); updateCommentSelBar(); }
    else DANMAKU_PAGE = 1;
    $(kind + "-table").innerHTML = empty(6, "正在读取当前来源的记录…", "i-clock");
    view.pager({ total: 0, page: 1, page_size: view.pageSize });
    if ($(kind + "-filter-count")) $(kind + "-filter-count").textContent = "正在读取…";
  }
  const hint = $(kind + "-capture-help");
  try {
    const bounds = contentCaptureBounds(kind);
    if (hint) { hint.textContent = "按当前设备时区筛选，包含结束当天；抓取时间为首次入库时间。"; delete hint.dataset.error; }
    ["from", "to"].forEach(end => $(kind + "-captured-" + end)?.removeAttribute("aria-invalid"));
    return { bounds, clear };
  } catch (e) {
    if (hint) { hint.textContent = e.message; hint.dataset.error = "true"; }
    ["from", "to"].forEach(end => $(kind + "-captured-" + end)?.setAttribute("aria-invalid", "true"));
    $(kind + "-table").innerHTML = empty(6, e.message, "i-info");
    if ($(kind + "-filter-count")) $(kind + "-filter-count").textContent = "请检查日期范围";
    toast(e.message, "err"); return null;
  }
}
function watchRecordLoadError(kind, error, clear) {
  if (clear) {
    $(kind + "-table").innerHTML = empty(6, "记录加载失败，请重新加载", "i-info");
    if ($(kind + "-filter-count")) $(kind + "-filter-count").textContent = "加载失败";
  }
  toast("记录加载失败：" + error.message, "err");
}
function cacheWatchRecordSources(kind, meta, rows) {
  const view = watchRecordConfig(kind);
  const sources = rows.filter(r => r.watch_source && Number(r.watch_source.id) === Number(r.watch_id || 0)).map(r => r.watch_source);
  if (meta.watch_source && view.src !== "" && String(meta.watch_source.id) === view.src) sources.push(meta.watch_source);
  for (const source of sources) {
    if (source && source.platform === PLATFORM && source.module === view.module && Number.isSafeInteger(Number(source.id)) && Number(source.id) >= 0)
      view.state.cache.set(String(source.id), source);
  }
  populateWatchRecordSource(kind);
}
function onContentSrc() { CONTENT_SRC = $("content-src").value; selContent.clear(); updateContentScope(); refreshContents(true); }
function onCommentSrc() { COMMENT_SRC = $("comment-src").value; selComment.clear(); refreshComments(true); }
function onContentMetaFilter() {
  CONTENT_GROUP = $("content-group").value; CONTENT_TAG = $("content-tag").value;
  selContent.clear(); refreshContents(true);
}
function onCommentMetaFilter() {
  COMMENT_GROUP = $("comment-group").value; COMMENT_TAG = $("comment-tag").value;
  selComment.clear(); refreshComments(true);
}
function matchesMeta(item, groupName, tag) {
  return (!groupName || item.group_name === groupName) && (!tag || itemTags(item).includes(tag));
}
function onMonitorFilter() { renderMonitorRows(); }
function onWatchFilter() { renderWatchRows(); }
// A late refresh must not replace the row underneath an open menu or its dialog.
// Resume on real DOM/focus changes instead of polling or delaying the user's action.
function waitForAccountPaint(isCurrent) {
  const interacting = () => globalThis.CreatorHubWorkbench?.isInteracting?.()
    || !!_visibleModal() || !!document.querySelector("[data-ui-closing]")
    || !!document.activeElement?.closest("[data-account-menu]");
  if (!interacting() || !isCurrent()) return Promise.resolve();
  return new Promise(resolve => {
    const finish = () => {
      if (isCurrent() && interacting()) return;
      observer.disconnect(); document.removeEventListener("focusin", finish);
      document.removeEventListener("focusout", focusChanged); window.removeEventListener("hashchange", finish); resolve();
    };
    const focusChanged = () => queueMicrotask(finish);
    const observer = new MutationObserver(finish);
    observer.observe(document.body, { childList: true, subtree: true, attributes: true, attributeFilter: ["style", "class", "data-state", "data-ui-closing"] });
    document.addEventListener("focusin", finish); document.addEventListener("focusout", focusChanged);
    window.addEventListener("hashchange", finish); finish();
  });
}
async function refreshAccounts() {
  const isCurrent = beginViewRequest("accounts");
  let allAccs, workbench = [];
  const scopePf = GLOBAL_MODE ? "" : PLATFORM;
  try {
    [allAccs, workbench] = await Promise.all([
      api("/api/accounts"),
      api("/api/accounts/workbench" + (scopePf ? "?platform=" + scopePf : "")).catch(() => []),
    ]);
  }
  catch (e) {
    if (isCurrent() && !$("acc-table").querySelector("[data-account-id]")) $("acc-table").querySelector("tbody").innerHTML = empty(3, "账号暂未加载", "i-info", "检查连接后，点击页面右上角刷新。");
    return;
  }
  if (!isCurrent()) return;
  await waitForAccountPaint(isCurrent);
  if (!isCurrent()) return;
  ALL_ACCOUNTS = Array.isArray(allAccs) ? allAccs : [];
  ACCOUNTS = ALL_ACCOUNTS.filter(a => a.platform === PLATFORM);
  const accs = GLOBAL_MODE ? ALL_ACCOUNTS : ACCOUNTS;
  const accountWork = new Map((workbench || []).map(value => [Number(value.account_id), value]));
  $("stat-acc").textContent = accs.length;
  $("acc-table").querySelector("tbody").innerHTML = accs.map(a => {
    const work = accountWork.get(Number(a.id)) || { tasks: {}, unread_dm: 0, browser_open: false };
    const task = work.tasks || {};
    const isXhs = a.platform === "xhs";
    const isKs = a.platform === "kuaishou";
    const isChannels = a.platform === "shipinhao";
    const isTikTok = a.platform === "tiktok";
    const isBili = a.platform === "bilibili";
    const isIg = a.platform === "instagram";
    const isTw = a.platform === "twitter";
    const idName = isXhs ? "小红书号 " : isKs ? "快手号 " : isChannels ? "视频号 " : isTikTok ? "TikTok号 " : isBili ? "UID " : (isIg || isTw) ? "@ " : "抖音号 ";
    const secName = isChannels ? "finder_id " : isKs ? "主页ID " : isXhs ? "user_id " : isTikTok ? "unique_id " : isBili ? "mid " : isIg ? "pk " : isTw ? "screen_name " : "sec_uid ";
    const rawSecUid = String(a.sec_uid || "");
    const shortSecUid = rawSecUid.length > 18 ? rawSecUid.slice(0, 18) + "…" : rawSecUid;
    const idline = [
      a.douyin_id ? idName + esc(a.douyin_id) : null,
      a.sec_uid ? secName + esc(shortSecUid) : null,
    ].filter(Boolean).join(" · ");
    const loginDetails = isXhs
      ? [a.has_read_login ? "读取登录已保存" : "读取登录未配置",
         a.has_creator ? "创作登录已保存" : "创作登录未配置"]
      : [a.has_storage
          ? (a.status === "invalid" ? "登录态已保存但校验失效" : "登录态有效")
          : "无登录态"];
    const ksGender = ({ M: "男", F: "女", MALE: "男", FEMALE: "女" })[
      String(a.gender || "").toUpperCase()
    ] || String(a.gender || "");
    const detail = [
      isKs || a.aweme_count ? fmtNum(a.aweme_count || 0) + (isXhs ? " 笔记" : " 作品") : null,
      isKs ? fmtNum(a.following_count || 0) + " 关注" : null,
      isKs || a.follower_count ? fmtNum(a.follower_count || 0) + " 粉丝" : null,
      isKs ? fmtNum(a.total_favorited || 0) + " 获赞" : null,
      isKs && ksGender ? "性别 " + ksGender : null,
      isXhs ? "扫码登录" : (a.login_type === "cookie" ? "Cookie 登录" : "扫码登录"),
      ...loginDetails,
      `被 ${a.monitor_count} 个监控使用`,
      `任务 ${Number(task.pending || 0)} 待执行 / ${Number(task.running || 0)} 执行中 / ${Number(task.blocked || 0)} 受阻 / ${Number(task.failed || 0)} 失败`,
      work.unread_dm ? `${Number(work.unread_dm)} 条未读私信` : null,
      a.created_at ? "登录于 " + new Date(a.created_at + "Z").toLocaleString() : null,
    ].filter(Boolean).join(" · ");
    const pill = isXhs
      ? (a.has_creator
          ? `<span class="pill active has-ic ic-text" title="已完成创作者登录,可发布">${ic("i-film")}创作者号</span>`
          : `<span class="pill bare has-ic ic-text" title="仅监控/读取,未授权创作平台,不能发布">${ic("i-eye")}读取号</span>`)
      : `<span class="pill ${a.has_creator ? "active" : "bare"} has-ic ic-text" title="${a.has_creator ? "创作者登录,可用于创作中心评论模式,也可抓取" : "普通抓取账号"}">${a.has_creator ? ic("i-film") + "创作者号" : ic("i-card") + "抓取号"}</span>`;
    // 代理(风控隔离):有代理显示脱敏地址 + 状态 + 共用提醒;无代理高亮提醒直连风险(均不强制拦截)
    const pxText = { ok: "代理正常", rotating: "代理正常（轮换出口）", drifted: "代理正常（出口已变化）", bad: "代理不可用", disabled: "池节点已停用", bound: "代理已绑定（待检测）", unknown: "代理已绑定（未检测）", auth_error: "代理认证失败", blocked: "代理被拦截" };
    const pxCls = ["ok", "rotating", "drifted"].includes(a.proxy_status) ? "active" : ["bad", "auth_error", "blocked", "disabled"].includes(a.proxy_status) ? "invalid" : "bare";
    const pxWarnings = Array.isArray(a.proxy_warnings) ? a.proxy_warnings : [];
    const pxWarnTitle = pxWarnings.join("；");
    const sharedBadge = a.shared_proxy
      ? `<span class="pill pending" title="${esc(pxWarnTitle || "同平台多账号正共用此代理或出口 IP，建议尽量一号一独立代理")}">⚠️ 共用代理(${Number(a.shared_proxy_count || 2)}号)</span>`
      : (a.cross_platform_shared_proxy ? `<span class="pill bare" title="${esc(pxWarnTitle || "跨平台复用同一代理")}">跨平台复用</span>` : "");
    const directPill = pxWarnings.length
      ? `<span class="pill pending" title="${esc(pxWarnTitle)}">⚠️ 本机直连</span>`
      : `<span class="mut" title="当前走本机真实网络，多号或海外平台建议配置独立代理">本机直连</span>`;
    const proxyWarnDetail = pxWarnings.length
      ? `<div class="ic-text" style="font-size:11px;margin-top:3px;color:var(--warn)">${ic("i-info")}${esc(pxWarnTitle)}</div>`
      : "";
    const proxyLine = a.has_proxy
      ? `<div class="mut" style="font-size:11px;margin-top:2px">代理 <code>${esc(a.proxy)}</code> <span class="pill ${pxCls}">${pxText[a.proxy_status] || a.proxy_status}</span> ${sharedBadge}</div>${proxyWarnDetail}`
      : `<div class="ic-text" style="font-size:11px;margin-top:2px;color:var(--warn)">${ic("i-info")}${esc(pxWarnTitle || "未配置代理（当前走本机直连；不拦截任务，同平台多账号建议配置独立代理防关联）")}</div>`;
    const browserLine = a.environment
      ? `<div class="mut" style="font-size:11px;margin-top:2px">浏览器 ${esc(loginEnvironmentText(a.environment))}</div>`
      : "";
    const fingerprintPlace = [a.fingerprint_country, a.fingerprint_region, a.fingerprint_city]
      .filter(Boolean).join(" · ");
    const fingerprintLine = a.fingerprint_ip
      ? `<div class="mut" style="font-size:11px;margin-top:2px">指纹 ${esc(a.fingerprint_id || "-")} · IP ${esc(a.fingerprint_ip)} · ${esc(fingerprintPlace || a.fingerprint_timezone || "未知地区")}${a.exit_ip && !a.fingerprint_ip_matches_exit ? ' <span class="pill invalid">与当前出口不一致</span>' : ""}</div>`
      : `<div class="mut" style="font-size:11px;margin-top:2px">指纹尚未按出口 IP 生成</div>`;
    const isolationLine = a.profile_isolated
      ? `<div class="mut" style="font-size:11px;margin-top:2px">环境隔离 <span class="pill active">独立 Profile ${esc(a.profile_isolation_id || "")}</span></div>`
      : `<div class="ic-text" style="font-size:11px;margin-top:2px;color:var(--danger)">${ic("i-info")}环境隔离异常：Profile 与其他账号重复或尚未分配</div>`;
    const environmentCheck = a.environment_check;
    const checkLine = environmentCheck && environmentCheck.enabled
      ? `<div class="mut" style="font-size:11px;margin-top:2px">环境体检 <span class="pill ${environmentCheck.required ? "pending" : "active"}">${environmentCheck.required ? "待打开" : "已提示"}</span>${environmentCheck.last_opened_at ? ` · ${new Date(environmentCheck.last_opened_at).toLocaleString()}` : " · 新环境首次启动自动打开"}</div>`
      : "";
    const reloginButton = isXhs && !a.has_read_login
      ? `<button class="sm warning-action" onclick="relogin(${a.id},'read')">补读取登录</button>`
      : (a.status === "invalid"
          ? `<button class="sm warning-action" onclick="relogin(${a.id})">重新登录</button>`
          : `<button class="ghost sm" onclick="relogin(${a.id})" title="${isXhs ? "重新扫码登录当前授权" : "重新扫码登录"}">重新登录</button>`);
    const creatorLoginButton = isXhs && !a.has_creator
      ? `<button class="ghost sm" onclick="relogin(${a.id},'creator')">补创作登录</button>` : "";
    const accountStatusLabel = a.status === "invalid"
      ? "登录失效" : (isXhs && !a.has_read_login && a.has_creator ? "创作登录正常" : "正常");
    return `<tr data-account-id="${a.id}">
      <td>
        <div class="user-cell">
          ${a.avatar ? `<img class="avatar" src="${esc(safeMediaUrl(a.avatar))}" alt="" referrerpolicy="no-referrer">` : `<span class="wb-avatar-fallback" aria-hidden="true">${esc((a.nickname || "账").slice(0, 1))}</span>`}
          <div>
            <button type="button" class="wb-account-name" data-account-detail="${a.id}" aria-haspopup="dialog">${esc(a.nickname || "未命名账号")}${ic("i-next")}</button>
            <div class="wb-account-meta">${idline || `账号 #${a.id}`} · ${a.monitor_count || 0} 个监控</div>
            <div class="wb-account-pills">${pill}${!a.has_proxy ? directPill : `<span class="pill ${pxCls}">${pxText[a.proxy_status] || "代理状态未知"}</span>${sharedBadge}`}</div>
          </div>
        </div>
        <div data-account-info hidden><div class="wb-inspector-block"><h3>账号资料</h3><p>${idline}</p><p>${esc(detail)}</p></div>
          <div class="wb-inspector-block"><h3>网络与环境</h3>${proxyLine}${browserLine}${fingerprintLine}${isolationLine}${checkLine}</div></div>
      </td>
      <td><span class="pill ${a.status}">${accountStatusLabel}</span>${work.browser_open ? '<div class="mut" style="margin-top:6px"><span class="pill downloading">浏览器已打开</span></div>' : ""}${Number(task.total || 0) ? `<div class="mut" style="margin-top:6px">${Number(task.running || 0)} 执行中 · ${Number(task.blocked || 0)} 受阻 · ${Number(task.failed || 0)} 失败</div>` : ""}${work.unread_dm ? `<div class="mut" style="margin-top:4px">${Number(work.unread_dm)} 条未读私信</div>` : ""}</td>
      <td class="acttd wb-account-actions">
        <button class="ghost sm" onclick="openAccountHub(${a.id})" title="查看该账号自己的作品与数据">本账号管理</button>
        ${a.status === "invalid" ? reloginButton : `<button class="ghost sm" onclick="openAccountBrowser(${a.id})">打开浏览器</button>`}
        <span data-account-menu></span>
        <div data-account-actions>
        ${reloginButton}
        ${creatorLoginButton}
        <button class="ghost sm" onclick="refreshProfile(${a.id})">刷新资料</button>
        <button class="ghost sm" onclick="openAccountHub(${a.id})" title="查看该账号自己的作品与数据">本账号管理</button>
        <button class="ghost sm" onclick="openAccountBrowser(${a.id})" title="用该账号登录态弹出真实浏览器窗口,手动收发私信 / 维护 / 抓接口(关窗即保存)">打开浏览器</button>
        <button class="ghost sm" onclick="setBrowserBackend(${a.id})" title="选择本地 Chrome/Patchright 或开源 Fingerprint Chromium 内核">环境</button>
        <button class="ghost sm" onclick="manageFingerprint(${a.id})" title="根据账号当前出口 IP 生成稳定指纹、时区、语言和地理位置">指纹</button>
        ${environmentCheck && environmentCheck.enabled ? `<button class="ghost sm" onclick="checkBrowserEnvironment(${a.id})" title="在该账号独立 Profile 中打开环境检测页，查看实际 IP、时区、WebRTC 和指纹">环境检测</button>` : ""}
        <button class="ghost sm" onclick="setProxy(${a.id})" title="设置/分配该账号专属代理(防多账号关联)">代理</button>
        ${a.has_proxy ? `<button class="ghost sm" onclick="testProxy(${a.id})" title="经该代理实连一次,验证可用">测代理</button>` : ""}
        <button class="ghost sm danger" onclick="delAccount(${a.id})" aria-label="删除账号">${ic("i-trash")}删除</button>
        </div>
      </td>
    </tr>`;
  }).join("") || empty(3, "还没有账号", "i-user", "用上方按钮扫码登录,或粘贴 Cookie 添加一个账号");
  globalThis.CreatorHubWorkbench?.accountsUpdated?.();
  if ($("tb-acc")) $("tb-acc").textContent = accs.length;
  populateAccountSelect();
  populateWatchAccount();
  populateCollectionAccount();
  if (PLATFORM === "douyin") applyDanmakuForm();
  else populateDanmakuAccount();
  populatePubAcc();
  populateAcAccount();
  populateHubAccounts();
  populateTikTokAccounts();
  const at = document.querySelector('.navitem.active');
  if (at && at.dataset.tab === "hub") refreshHubPanel();
}

let TIKTOK_USERS = [];
function populateTikTokAccounts() {
  const accounts = ACCOUNTS.filter(a => a.platform === "tiktok" && a.status !== "invalid" && a.has_storage);
  const options = accOptions(accounts, accounts.length ? "请选择 TikTok 账号" : "暂无可用 TikTok 账号");
  for (const id of ["tk-account", "tk-comment-account"]) {
    const select = $(id);
    if (!select) continue;
    const previous = select.value;
    select.innerHTML = options;
    if (accounts.some(account => String(account.id) === previous)) select.value = previous;
    else if (accounts.length) select.value = String(accounts[0].id);
    select._csSync?.();
  }
  if (!TIKTOK_USERS.length) renderTikTokUsers();
}
function renderTikTokUsers() {
  const body = $("tk-user-results");
  if (!body) return;
  body.innerHTML = TIKTOK_USERS.map((user, index) => {
    const uniqueId = String(user.unique_id || "");
    const secUid = String(user.sec_uid || "");
    const avatar = user.avatar
      ? '<img class="avatar" src="' + esc(safeMediaUrl(user.avatar)) + '" alt="" referrerpolicy="no-referrer">'
      : '<span class="wb-avatar-fallback" aria-hidden="true">' + esc((user.nickname || uniqueId || "T").slice(0, 1)) + '</span>';
    const disabled = uniqueId ? "" : " disabled";
    return '<tr><td><div class="user-cell">' + avatar + '<div><b>'
      + esc(user.nickname || uniqueId || "未命名用户") + '</b><div class="mut">'
      + (uniqueId ? "@" + esc(uniqueId) : "缺少 unique_id") + '</div></div></div></td>'
      + '<td><code>' + esc(user.uid || "—") + '</code><div class="mut" title="' + esc(secUid)
      + '">sec_uid ' + esc(secUid ? secUid.slice(0, 22) + (secUid.length > 22 ? "…" : "") : "—") + '</div></td>'
      + '<td>' + fmtNum(user.follower_count || 0) + ' 粉丝<div class="mut">'
      + fmtNum(user.following_count || 0) + ' 关注 · ' + fmtNum(user.video_count || 0) + ' 视频</div></td>'
      + '<td class="wrap" style="max-width:260px" title="' + esc(user.signature || "") + '">' + esc((user.signature || "—").slice(0, 120)) + '</td>'
      + '<td class="acttd"><button class="ghost sm"' + disabled + ' onclick="runTikTokUserAction(' + index + ',\'follow\')">关注</button>'
      + '<button class="ghost sm"' + disabled + ' onclick="runTikTokUserAction(' + index + ',\'unfollow\')">取关</button>'
      + '<button class="ghost sm"' + disabled + ' onclick="runTikTokUserAction(' + index + ',\'send_dm\')">私信</button></td></tr>';
  }).join("") || empty(5, "暂无 TikTok 用户结果", "i-user", "选择已登录账号并输入昵称或 unique_id 搜索");
}
async function searchTikTokUsers() {
  const accountId = Number($("tk-account")?.value || 0);
  const keyword = $("tk-user-query")?.value.trim() || "";
  const maxResults = Math.max(1, Math.min(100, Number($("tk-user-limit")?.value || 30)));
  let valid = true;
  valid = setFieldError($("tk-account"), !accountId ? "请选择 TikTok 账号" : "") && valid;
  valid = setFieldError($("tk-user-query"), !keyword ? "请输入用户关键词" : "") && valid;
  if (!valid) return;
  const status = $("tk-user-status");
  if (status) status.textContent = "正在通过已登录页面搜索…";
  await withBusy(evtBtn(), "搜索中", async () => {
    try {
      const result = await api("/api/tiktok/users/search", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ account_id: accountId, keyword, max_results: maxResults }),
      });
      TIKTOK_USERS = Array.isArray(result.items) ? result.items : [];
      renderTikTokUsers();
      if (status) status.textContent = result.error
        ? "返回 " + TIKTOK_USERS.length + " 个结果；" + result.error
        : "找到 " + TIKTOK_USERS.length + " 个用户";
    } catch (e) {
      TIKTOK_USERS = [];
      renderTikTokUsers();
      if (status) status.textContent = "搜索失败：" + e.message;
      toast("TikTok 用户搜索失败：" + e.message, "err");
    }
  });
}
async function runTikTokUserAction(index, action) {
  const user = TIKTOK_USERS[index];
  if (!user) return;
  const accountId = Number($("tk-account")?.value || 0);
  if (!accountId) { toast("请选择 TikTok 执行账号", "err"); return; }
  const labels = { follow: "关注", unfollow: "取关", send_dm: "私信" };
  let content = "";
  if (action === "send_dm") {
    const value = await uiPrompt({
      title: "私信 @" + (user.unique_id || user.nickname || "TikTok 用户"),
      hint: "任务提交后受统一限速和风控控制；无明确回执时需人工核对。",
      multiline: true, rows: 4,
    });
    if (value === null) return;
    content = value.trim();
    if (!content) { toast("私信内容不能为空", "err"); return; }
  } else if (!await uiConfirm({
    title: labels[action] + "确认",
    message: "确认对 @" + (user.unique_id || user.nickname || "该用户") + " 执行" + labels[action] + "？",
    danger: action === "unfollow",
  })) return;
  const runNow = Boolean($("tk-action-run-now")?.checked);
  try {
    const result = await api("/api/account-actions", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        account_id: accountId, action, target_uid: user.uid || "",
        target_sec_uid: user.unique_id || "", target_nick: user.nickname || user.unique_id || "",
        content, run_now: runNow,
      }),
    });
    const uncertain = result.status === "uncertain"
      || String(result.execution_error || "").startsWith("write_uncertain:");
    const message = uncertain
      ? labels[action] + "已提交，结果待核对：" + (result.execution_error || "请到 TikTok 查看")
      : result.ran ? labels[action] + "成功" : labels[action] + "任务 #" + result.id + " 已进入队列";
    toast(message, uncertain ? "info" : result.ran ? "ok" : "info", 7000);
    refreshTaskQueueBadge();
  } catch (e) { toast(labels[action] + "失败：" + e.message, "err"); }
}
async function createTikTokCommentTask() {
  const accountId = Number($("tk-comment-account")?.value || 0);
  const awemeId = $("tk-aweme-id")?.value.trim() || "";
  const authorUniqueId = $("tk-author-id")?.value.trim() || "";
  const content = $("tk-comment-content")?.value.trim() || "";
  const targetCommentId = $("tk-target-comment-id")?.value.trim() || "";
  const targetText = $("tk-target-text")?.value.trim() || "";
  let valid = true;
  valid = setFieldError($("tk-comment-account"), !accountId ? "请选择 TikTok 账号" : "") && valid;
  valid = setFieldError($("tk-aweme-id"), !awemeId ? "请输入作品链接或 ID" : "") && valid;
  valid = setFieldError($("tk-author-id"), /^\d+$/.test(awemeId) && !authorUniqueId ? "只填作品 ID 时需填写作者 @username" : "") && valid;
  valid = setFieldError($("tk-comment-content"), !content ? "请输入评论内容" : "") && valid;
  valid = setFieldError($("tk-target-text"), targetCommentId && !targetText ? "回复任务需填写目标评论原文" : "") && valid;
  if (!valid) return;
  await withBusy(evtBtn(), "提交中", async () => {
    try {
      const result = await api("/api/tiktok/comment-tasks", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          account_id: accountId, aweme_id: awemeId, author_unique_id: authorUniqueId, content,
          target_comment_id: targetCommentId, target_text: targetText,
          target_nick: $("tk-target-nick")?.value.trim() || "",
          run_now: Boolean($("tk-comment-run-now")?.checked),
        }),
      });
      const uncertain = result.status === "uncertain"
        || String(result.execution_error || "").startsWith("write_uncertain:");
      const message = uncertain
        ? "评论已提交，结果待核对：" + (result.execution_error || "请到 TikTok 查看")
        : result.ran ? "评论已发布" : "评论任务 #" + result.id + " 已进入队列";
      toast(message, uncertain ? "info" : result.ran ? "ok" : "info", 7000);
      if (!uncertain) {
        $("tk-comment-content").value = "";
        $("tk-target-comment-id").value = "";
        $("tk-target-text").value = "";
        $("tk-target-nick").value = "";
      }
      refreshCommentTasks();
      refreshTaskQueueBadge();
    } catch (e) { toast("TikTok 评论任务创建失败：" + e.message, "err"); }
  });
}
