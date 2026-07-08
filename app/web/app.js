const $ = (id) => document.getElementById(id);
// 有用户发起的慢操作(开浏览器抓评论/发评论/解析链接等)在进行时,暂停 8 秒轮询刷新,
// 否则定时重渲染会把按钮的「…中」加载态冲掉。
let INFLIGHT = 0;
// 全局忙碌徽章:>350ms 才显示(快速轮询不闪),圆环转圈 + 已等待秒数 + 并发数。
// 拿不到真实进度百分比(浏览器自动化/接口都是不透明操作),用计时给"在进行"的清晰感知。
// 判忙 = 有未完成请求(_apiActive)或有用户慢操作(INFLIGHT);并发数用 INFLIGHT(用户点的操作数)。
let _apiActive = 0, _barTimer = null, _busyStart = 0, _busyTick = null;
function _isBusy() { return _apiActive > 0 || INFLIGHT > 0; }
function _busyShow() { const sp = $("busy-spinner"); if (sp && _isBusy()) sp.classList.add("on"); }
function _busyLabel() {
  const l = $("bs-label"); if (!l) return;
  const sec = Math.floor((Date.now() - _busyStart) / 1000);
  l.textContent = "处理中 " + (INFLIGHT > 1 ? "×" + INFLIGHT + " · " : "") + sec + " 秒";
}
function _barSync() {
  if (_isBusy()) {
    if (!_barTimer) {                 // 空闲 -> 忙:启动计时,350ms 后才真正显示
      _busyStart = Date.now();
      _barTimer = setTimeout(_busyShow, 350);
      _busyTick = setInterval(_busyLabel, 250);
    }
  } else {                            // 全部结束:清理并隐藏
    clearTimeout(_barTimer); _barTimer = null;
    clearInterval(_busyTick); _busyTick = null;
    const sp = $("busy-spinner"); if (sp) sp.classList.remove("on");
    const l = $("bs-label"); if (l) l.textContent = "处理中";
  }
}
const api = async (path, opts) => {
  _apiActive++; _barSync();
  try {
    const r = await fetch(path, opts);
    if (!r.ok) { const e = await r.json().catch(() => ({})); throw new Error(e.detail || r.status); }
    return await r.json();
  } finally { _apiActive--; _barSync(); }
};

// ─── UI helpers ───
const ic = (id) => `<svg aria-hidden="true"><use href="#${id}"/></svg>`;
// 按钮加载态:换成 spinner+label,返回 restore()。配合 INFLIGHT 暂停轮询,加载态不会被重渲染冲掉。
function btnLoading(btn, label) {
  if (!btn) return () => {};
  const html = btn.innerHTML, dis = btn.disabled;
  btn.disabled = true; btn.classList.add("busy");
  btn.innerHTML = `<span class="spin"></span>${label ? `<span>${esc(label)}</span>` : ""}`;
  return () => { try { btn.innerHTML = html; btn.disabled = dis; btn.classList.remove("busy"); } catch (e) {} };
}
// 包裹一个用户发起的慢操作:按钮转圈 + 暂停轮询(避免 8 秒重渲染冲掉加载态)。
// btn 可为 null(无按钮场景);fn 为实际 async 逻辑。
async function withBusy(btn, label, fn) {
  const restore = btnLoading(btn, label);
  INFLIGHT++; _barSync();
  try { return await fn(); }
  finally { INFLIGHT--; restore(); _barSync(); }
}
// 从内联 onclick 处理器里拿到被点的按钮(event 在同步阶段有效)
function evtBtn() { try { return event.target.closest("button"); } catch (e) { return null; } }
function toast(msg, type = "info", ms = 3600) {
  const box = $("toasts");
  const el = document.createElement("div");
  el.className = `toast ${type}`;
  const sym = type === "ok" ? "i-check" : type === "err" ? "i-x" : "i-info";
  el.innerHTML = `${ic(sym)}<span>${esc(msg)}</span>`;
  box.appendChild(el);
  setTimeout(() => { el.classList.add("hide"); setTimeout(() => el.remove(), 250); }, ms);
}
const empty = (cols, text, icon = "i-inbox", sub = "") =>
  `<tr><td colspan="${cols}"><div class="empty">` +
  `<div class="empty-ic">${ic(icon)}</div><div class="empty-t">${esc(text)}</div>` +
  `${sub ? `<div class="empty-sub">${esc(sub)}</div>` : ""}</div></td></tr>`;
const skeleton = (cols, rows = 3) => {
  let out = "";
  for (let i = 0; i < rows; i++) {
    let tds = "";
    for (let c = 0; c < cols; c++) tds += `<td><span class="sk" style="width:${40 + ((i + c) % 4) * 18}%"></span></td>`;
    out += `<tr>${tds}</tr>`;
  }
  return out;
};

// ─── 通用模态(替代原生 prompt / confirm:下拉 / 文本输入 / 确认)───
let _uiResolve = null, _uiGetVal = null, _uiCancelVal = null;
function _uiClose(val) {
  $("uimodal").style.display = "none";
  document.removeEventListener("keydown", _uiKey);
  const r = _uiResolve; _uiResolve = null; _uiGetVal = null;
  if (r) r(val);
}
function _uiKey(e) {
  if (e.key === "Escape") uiModalCancel();
  else if (e.key === "Enter" && document.activeElement && document.activeElement.tagName !== "TEXTAREA") uiModalOk();
}
function uiModalCancel() { _uiClose(_uiCancelVal); }
function uiModalOk() { _uiClose(_uiGetVal ? _uiGetVal() : ""); }
function _uiOpen(title, hint, { okText = "确定", danger = false } = {}) {
  $("ui-title").textContent = title || "";
  $("ui-hint").textContent = hint || "";
  const ok = $("ui-ok");
  ok.innerHTML = (danger ? "" : `<svg aria-hidden="true"><use href="#i-check"/></svg>`) + esc(okText);
  ok.classList.toggle("danger", !!danger);
  ok.style.cssText = danger ? "flex:0 0 auto;background:var(--danger);border-color:transparent" : "flex:0 0 auto";
  $("uimodal").style.display = "flex";
  document.addEventListener("keydown", _uiKey);
  setTimeout(() => { const el = $("ui-body").querySelector("select,input,textarea,button"); if (el && el.tagName !== "BUTTON") el.focus(); }, 30);
}
// 确认框。返回 true / false。danger=true 时确定按钮红色(危险操作)
function uiConfirm({ title = "确认", message = "", okText = "确定", danger = false } = {}) {
  return new Promise(res => {
    _uiResolve = res; _uiGetVal = () => true; _uiCancelVal = false;
    $("ui-body").innerHTML = "";
    _uiOpen(title, message, { okText, danger });
  });
}
// 下拉选择。options:[{value,label,disabled}]。返回选中 value 或 null(取消)
function uiSelect({ title, hint, options, value }) {
  return new Promise(res => {
    _uiResolve = res; _uiCancelVal = null;
    _uiGetVal = () => { const el = $("ui-body").querySelector("select,input,textarea"); return el ? el.value : ""; };
    $("ui-body").innerHTML =
      `<select id="ui-sel" style="width:100%">` +
      options.map(o => `<option value="${esc(o.value)}"${o.value === value ? " selected" : ""}${o.disabled ? " disabled" : ""}>${esc(o.label)}</option>`).join("") +
      `</select>`;
    enhanceSelect($("ui-sel"));
    _uiOpen(title, hint);
  });
}
// 文本输入(单行或多行)。返回字符串或 null(取消)
function uiPrompt({ title, hint, value, placeholder, multiline, rows }) {
  return new Promise(res => {
    _uiResolve = res; _uiCancelVal = null;
    _uiGetVal = () => { const el = $("ui-body").querySelector("select,input,textarea"); return el ? el.value : ""; };
    $("ui-body").innerHTML = multiline
      ? `<textarea id="ui-inp" rows="${rows || 6}" placeholder="${esc(placeholder || "")}">${esc(value || "")}</textarea>`
      : `<input id="ui-inp" value="${esc(value || "")}" placeholder="${esc(placeholder || "")}">`;
    _uiOpen(title, hint);
  });
}

// ─── 自定义下拉:渐进增强原生 <select>(美化展开列表)───
function enhanceSelect(sel) {
  if (sel.dataset.cs) return;
  sel.dataset.cs = "1";
  const wrap = document.createElement("div");
  wrap.className = "cs" + (sel.className ? " " + sel.className : "");
  const st = sel.getAttribute("style");
  if (st) wrap.setAttribute("style", st);
  sel.parentNode.insertBefore(wrap, sel);
  wrap.appendChild(sel);
  sel.className = "cs-native";
  sel.removeAttribute("style");

  const trg = document.createElement("button");
  trg.type = "button";
  trg.className = "cs-trg";
  trg.innerHTML = `<span class="cs-lbl"></span>` +
    `<svg class="cs-arr" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m6 9 6 6 6-6"/></svg>`;
  wrap.appendChild(trg);
  let panel = null;

  function sync() {
    const o = sel.options[sel.selectedIndex];
    trg.querySelector(".cs-lbl").textContent = o ? o.textContent : "";
    trg.classList.toggle("ph", !o || o.value === "");
  }
  function close() {
    if (panel) { panel.remove(); panel = null; }
    wrap.classList.remove("open");
    window.removeEventListener("scroll", close, true);
    window.removeEventListener("resize", close);
    document.removeEventListener("mousedown", onDoc, true);
  }
  function onDoc(e) { if (!wrap.contains(e.target) && (!panel || !panel.contains(e.target))) close(); }
  function open() {
    if (sel.disabled) return;
    panel = document.createElement("div");
    panel.className = "cs-panel";
    Array.from(sel.options).forEach((o, i) => {
      const it = document.createElement("div");
      it.className = "cs-opt" + (i === sel.selectedIndex ? " sel" : "") + (o.disabled ? " dis" : "");
      it.textContent = o.textContent;
      if (!o.disabled) it.addEventListener("mousedown", ev => {
        ev.preventDefault();
        if (sel.selectedIndex !== i) { sel.selectedIndex = i; sel.dispatchEvent(new Event("change", { bubbles: true })); }
        sync(); close();
      });
      panel.appendChild(it);
    });
    document.body.appendChild(panel);
    const r = trg.getBoundingClientRect();
    panel.style.left = r.left + "px";
    panel.style.minWidth = r.width + "px";
    const below = window.innerHeight - r.bottom;
    if (below < 280 && r.top > below) panel.style.bottom = (window.innerHeight - r.top + 5) + "px";
    else panel.style.top = (r.bottom + 5) + "px";
    wrap.classList.add("open");
    window.addEventListener("scroll", close, true);
    window.addEventListener("resize", close);
    setTimeout(() => document.addEventListener("mousedown", onDoc, true), 0);
  }
  trg.addEventListener("click", e => { e.preventDefault(); panel ? close() : open(); });
  sel.addEventListener("change", sync);
  sel._csSync = sync;
  new MutationObserver(sync).observe(sel, { childList: true });
  sync();
}
function enhanceAllSelects(root) { (root || document).querySelectorAll("select:not([data-cs])").forEach(enhanceSelect); }
function csSyncAll() { document.querySelectorAll("select[data-cs]").forEach(s => s._csSync && s._csSync()); }

// ─── 自定义 tooltip:接管原生 title(首次 hover 时把 title 转 data-tip,避免系统提示)───
const _tip = document.createElement("div"); _tip.className = "tip"; document.body.appendChild(_tip);
let _tipTarget = null, _tipTimer = null;
function _tipShow(el) {
  const text = el.getAttribute("data-tip");
  if (!text || !el.isConnected) { _tipHide(); return; }
  _tip.textContent = text;
  const r = el.getBoundingClientRect(), tr = _tip.getBoundingClientRect();
  let below = false, top = r.top - tr.height - 8;
  if (top < 6) { below = true; top = r.bottom + 8; }
  const left = Math.max(6, Math.min(r.left + r.width / 2 - tr.width / 2, window.innerWidth - tr.width - 6));
  _tip.style.left = left + "px"; _tip.style.top = top + "px";
  _tip.classList.toggle("below", below);
  _tip.classList.add("show");
}
function _tipHide() { _tip.classList.remove("show"); _tipTarget = null; clearTimeout(_tipTimer); }
document.addEventListener("mouseover", e => {
  const el = e.target.closest && e.target.closest("[title],[data-tip]");
  if (!el || el === _tip) return;
  if (el.hasAttribute("title")) {       // 把原生 title 搬到 data-tip,从此不再弹系统提示
    const t = el.getAttribute("title");
    if (t) { el.setAttribute("data-tip", t); if (!el.hasAttribute("aria-label")) el.setAttribute("aria-label", t); }
    el.removeAttribute("title");
  }
  if (el === _tipTarget) return;
  _tipTarget = el;
  clearTimeout(_tipTimer);
  _tipTimer = setTimeout(() => { if (_tipTarget === el) _tipShow(el); }, 300);
});
document.addEventListener("mouseout", e => {
  if (_tipTarget && (!e.relatedTarget || !_tipTarget.contains(e.relatedTarget))) _tipHide();
});
window.addEventListener("scroll", _tipHide, true);
document.addEventListener("click", _tipHide);

// ─── 自定义日期时间选择器:渐进增强 <input type=datetime-local> ───
const _pad2 = n => String(n).padStart(2, "0");
function _dtFmt(d) { return `${d.getFullYear()}-${_pad2(d.getMonth() + 1)}-${_pad2(d.getDate())}T${_pad2(d.getHours())}:${_pad2(d.getMinutes())}`; }
function _dtDisp(d) { return `${d.getFullYear()}-${_pad2(d.getMonth() + 1)}-${_pad2(d.getDate())} ${_pad2(d.getHours())}:${_pad2(d.getMinutes())}`; }
function _dtParse(v) { const m = (v || "").match(/(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/); return m ? new Date(+m[1], +m[2] - 1, +m[3], +m[4], +m[5]) : null; }
function enhanceDateTime(inp) {
  if (inp.dataset.dt) return; inp.dataset.dt = "1";
  const wrap = document.createElement("div");
  wrap.className = "dt" + (inp.className ? " " + inp.className : "");
  const st = inp.getAttribute("style"); if (st) wrap.setAttribute("style", st);
  inp.parentNode.insertBefore(wrap, inp); wrap.appendChild(inp);
  inp.className = "dt-native"; inp.removeAttribute("style");
  const ph = inp.getAttribute("aria-label") || "选择日期时间";
  const trg = document.createElement("button");
  trg.type = "button"; trg.className = "dt-trg";
  trg.innerHTML = `<span class="dt-lbl"></span>` +
    `<svg class="dt-ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="18" rx="2"/><path d="M16 2v4M8 2v4M3 10h18"/></svg>`;
  wrap.appendChild(trg);
  let panel = null;
  function sync() { const d = _dtParse(inp.value); trg.querySelector(".dt-lbl").textContent = d ? _dtDisp(d) : ph; trg.classList.toggle("ph", !d); }
  function close() { if (panel) { panel.remove(); panel = null; } wrap.classList.remove("open"); window.removeEventListener("scroll", close, true); window.removeEventListener("resize", close); document.removeEventListener("mousedown", onDoc, true); }
  function onDoc(e) { if (!wrap.contains(e.target) && (!panel || !panel.contains(e.target))) close(); }
  function open() {
    const init = _dtParse(inp.value) || new Date();
    let view = new Date(init.getFullYear(), init.getMonth(), 1);
    let chosen = _dtParse(inp.value);
    let h = init.getHours(), mi = init.getMinutes();
    panel = document.createElement("div"); panel.className = "dt-panel";
    const getH = () => { const v = parseInt(panel.querySelector(".dt-h").value, 10); return isNaN(v) ? 0 : Math.max(0, Math.min(23, v)); };
    const getM = () => { const v = parseInt(panel.querySelector(".dt-m").value, 10); return isNaN(v) ? 0 : Math.max(0, Math.min(59, v)); };
    function render() {
      const y = view.getFullYear(), m = view.getMonth();
      const lead = (new Date(y, m, 1).getDay() + 6) % 7;   // 周一为首列
      const days = new Date(y, m + 1, 0).getDate();
      const t = new Date();
      let cells = "";
      for (let i = 0; i < lead; i++) cells += `<div class="dt-day off"></div>`;
      for (let d = 1; d <= days; d++) {
        const today = t.getFullYear() === y && t.getMonth() === m && t.getDate() === d;
        const sel = chosen && chosen.getFullYear() === y && chosen.getMonth() === m && chosen.getDate() === d;
        cells += `<div class="dt-day${today ? " today" : ""}${sel ? " sel" : ""}" data-d="${d}">${d}</div>`;
      }
      panel.innerHTML =
        `<div class="dt-head"><button type="button" class="dt-nav" data-nav="-1">‹</button>` +
        `<span class="dt-title">${y} 年 ${m + 1} 月</span>` +
        `<button type="button" class="dt-nav" data-nav="1">›</button></div>` +
        `<div class="dt-wk"><span>一</span><span>二</span><span>三</span><span>四</span><span>五</span><span>六</span><span>日</span></div>` +
        `<div class="dt-grid">${cells}</div>` +
        `<div class="dt-time"><span>时间</span><input type="number" class="dt-h" min="0" max="23" value="${_pad2(h)}"><b>:</b><input type="number" class="dt-m" min="0" max="59" value="${_pad2(mi)}"></div>` +
        `<div class="dt-foot"><button type="button" class="ghost sm" data-act="clear">清除</button><button type="button" class="ghost sm" data-act="now">现在</button><button type="button" class="sm" data-act="ok">确定</button></div>`;
      panel.querySelectorAll(".dt-nav").forEach(b => b.onclick = () => { h = getH(); mi = getM(); view.setMonth(view.getMonth() + (+b.dataset.nav)); render(); });
      panel.querySelectorAll(".dt-day[data-d]").forEach(c => c.onclick = () => { h = getH(); mi = getM(); chosen = new Date(view.getFullYear(), view.getMonth(), +c.dataset.d, h, mi); render(); });
    }
    function commit(d) { inp.value = d ? _dtFmt(d) : ""; inp.dispatchEvent(new Event("change", { bubbles: true })); sync(); close(); }
    render();
    panel.addEventListener("click", e => {
      const a = e.target.closest("[data-act]"); if (!a) return;
      if (a.dataset.act === "clear") commit(null);
      else if (a.dataset.act === "now") commit(new Date());
      else { const base = chosen || new Date(); base.setHours(getH(), getM(), 0, 0); commit(base); }
    });
    document.body.appendChild(panel);
    const r = trg.getBoundingClientRect();
    panel.style.left = Math.max(6, Math.min(r.left, window.innerWidth - 280)) + "px";
    const below = window.innerHeight - r.bottom;
    if (below < 360 && r.top > below) panel.style.bottom = (window.innerHeight - r.top + 5) + "px";
    else panel.style.top = (r.bottom + 5) + "px";
    wrap.classList.add("open");
    window.addEventListener("scroll", close, true); window.addEventListener("resize", close);
    setTimeout(() => document.addEventListener("mousedown", onDoc, true), 0);
  }
  trg.addEventListener("click", e => { e.preventDefault(); panel ? close() : open(); });
  inp.addEventListener("change", sync);
  inp._dtSync = sync;
  sync();
}
function enhanceAllDateTime(root) { (root || document).querySelectorAll("input[type=datetime-local]:not([data-dt])").forEach(enhanceDateTime); }
function dtSyncAll() { document.querySelectorAll("input[type=datetime-local][data-dt]").forEach(i => i._dtSync && i._dtSync()); }

// ─── 总览迷你图表(近 7 天采集,纯 SVG 分组柱状)───
async function refreshOverviewChart() {
  const box = $("overview-chart");
  if (!box) return;
  let d;
  try { d = await api("/api/stats/series?days=7&platform=" + PLATFORM); }
  catch (e) { box.innerHTML = `<div class="chart-empty">图表加载失败</div>`; return; }
  const days = d.days || [], A = d.contents || [], B = d.comments || [];
  const total = A.reduce((s, n) => s + n, 0) + B.reduce((s, n) => s + n, 0);
  if (!days.length || total === 0) {
    box.innerHTML = `<div class="chart-empty">近 7 天暂无采集数据 — 添加监控并「立即抓取」后这里会出现趋势</div>`;
    return;
  }
  // viewBox 坐标系,响应式缩放
  const W = 720, H = 180, padL = 28, padR = 12, padT = 14, padB = 26;
  const iw = W - padL - padR, ih = H - padT - padB;
  const n = days.length, slot = iw / n;
  const maxV = Math.max(1, ...A, ...B);
  // y 轴参考线(0 / 中 / 顶)
  const ticks = [0, Math.round(maxV / 2), maxV].filter((v, i, a) => a.indexOf(v) === i);
  const y = v => padT + ih - (v / maxV) * ih;
  let gl = "", axt = "";
  ticks.forEach(t => {
    const yy = y(t).toFixed(1);
    gl += `<line class="gl" x1="${padL}" y1="${yy}" x2="${W - padR}" y2="${yy}"/>`;
    axt += `<text class="axt" x="${padL - 6}" y="${(+yy + 3).toFixed(1)}" text-anchor="end">${t}</text>`;
  });
  const bw = Math.max(5, Math.min(16, slot / 2 - 4));   // 每根柱宽
  let bars = "", labels = "";
  const md = (s) => s.slice(5);   // MM-DD
  for (let i = 0; i < n; i++) {
    const cx = padL + slot * i + slot / 2;
    const xa = cx - bw - 1, xb = cx + 1;
    const ha = (A[i] / maxV) * ih, hb = (B[i] / maxV) * ih;
    bars += `<rect class="bar" x="${xa.toFixed(1)}" y="${y(A[i]).toFixed(1)}" width="${bw}" height="${ha.toFixed(1)}" rx="2" fill="var(--acc)"><title>${md(days[i])} · 作品 ${A[i]}</title></rect>`;
    bars += `<rect class="bar" x="${xb.toFixed(1)}" y="${y(B[i]).toFixed(1)}" width="${bw}" height="${hb.toFixed(1)}" rx="2" fill="var(--info)"><title>${md(days[i])} · 评论 ${B[i]}</title></rect>`;
    labels += `<text class="axt" x="${cx.toFixed(1)}" y="${H - 8}" text-anchor="middle">${md(days[i])}</text>`;
  }
  box.innerHTML = `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="近 7 天每日新增作品与评论柱状图">${gl}${axt}${bars}${labels}</svg>`;
}

// ─── 平台切换(抖音 / 小红书) ───
let PLATFORM = "douyin";
const PF_NAME = { douyin: "抖音", xhs: "小红书", kuaishou: "快手" };
// 是否支持「发布」面板(抖音 / 小红书 / 快手均有)
function pfHasPublish(pf) { return pf === "xhs" || pf === "kuaishou" || pf === "douyin"; }
function switchPlatform(pf) {
  if (pf !== "douyin" && pf !== "xhs" && pf !== "kuaishou") pf = "douyin";
  PLATFORM = pf;
  try { localStorage.setItem("dym-pf", pf); } catch (e) {}
  applyPlatformUI();
  // 切换后立刻刷新该平台数据
  refreshAccounts(); refreshMonitors(); refreshContents(); refreshWatches(); refreshComments(); refreshOverviewChart();
  populateAcAccount(); onAcMode(); refreshCommentRules(); refreshCommentTasks();
  if (pfHasPublish(PLATFORM)) refreshPublish();
}
function applyPlatformUI() {
  document.body.classList.toggle("pf-douyin", PLATFORM === "douyin");
  document.body.classList.toggle("pf-xhs", PLATFORM === "xhs");
  document.body.classList.toggle("pf-kuaishou", PLATFORM === "kuaishou");
  document.querySelectorAll(".pswitch button").forEach(b =>
    b.classList.toggle("active", b.dataset.pf === PLATFORM));
  document.querySelectorAll(".dy-only").forEach(e => e.classList.toggle("hidden", PLATFORM !== "douyin"));
  document.querySelectorAll(".xhs-only").forEach(e => e.classList.toggle("hidden", PLATFORM !== "xhs"));
  document.querySelectorAll(".ks-only").forEach(e => e.classList.toggle("hidden", PLATFORM !== "kuaishou"));
  // 发布面板入口:抖音 / 小红书 / 快手均显示
  document.querySelectorAll(".pub-only").forEach(e => e.classList.toggle("hidden", !pfHasPublish(PLATFORM)));
  // 发布面板文案随平台切换
  const ks = PLATFORM === "kuaishou", dy = PLATFORM === "douyin";
  const pubSub = $("pub-head-sub");
  if (pubSub) pubSub.textContent = dy ? "上传图集 / 视频到抖音创作平台(实验性)"
    : ks ? "上传图集 / 视频到快手创作平台(实验性)" : "上传图集 / 视频到小红书(实验性)";
  if ($("pub-head-lead")) $("pub-head-lead").textContent = (ks || dy) ? "发布作品" : "发布笔记";
  if ($("pub-title")) $("pub-title").placeholder = (ks || dy) ? "给作品起个标题" : "给笔记起个标题";
  if ($("pub-hint")) $("pub-hint").textContent = dy
    ? "发布通过自动化抖音创作平台(creator.douyin.com)完成,会弹出浏览器窗口。首次或触发风控时抖音会要求「短信验证码/扫码」验证,请在弹出窗口里手动完成(最多等 5 分钟,验证通过后自动继续发布);视频上传后需等转码,发布稍慢。⚠️ 因需本人验证,定时/无人值守发布可能被此步骤挡住,建议发布时在场。"
    : ks
    ? "发布通过自动化快手创作平台(cp.kuaishou.com)完成,会弹出浏览器窗口;若遇验证码/需补封面可在窗口里手动处理。定时任务由后台引擎到点执行。"
    : "发布通过自动化小红书创作平台完成,会弹出浏览器窗口;若遇验证码/需补封面可在窗口里手动处理。定时任务由后台引擎到点执行。";
  // 评论监控「类型」下拉随平台改写文案
  const wk = $("w-kind");
  if (wk) {
    const cur = wk.value;
    wk.innerHTML = PLATFORM === "xhs"
      ? '<option value="auto">类型:自动识别</option><option value="video">单条笔记</option><option value="user">创作者近期笔记</option>'
      : '<option value="auto">类型:自动识别</option><option value="video">单条视频</option><option value="user">账号近期作品</option>';
    if ([...wk.options].some(o => o.value === cur)) wk.value = cur;
  }
  const wl = $("w-url-label");
  if (wl) wl.textContent = PLATFORM === "xhs"
    ? "笔记链接 / 创作者主页 / xhslink 短链 / id"
    : PLATFORM === "kuaishou" ? "作品链接 / 创作者主页 / v.kuaishou.com 短链 / id"
    : "视频链接 / 账号主页 / sec_uid / 视频 id";
  if ($("w-url")) $("w-url").placeholder = PLATFORM === "xhs"
    ? "笔记链接=盯单条笔记;创作者主页或 user_id=盯创作者近期笔记"
    : PLATFORM === "kuaishou" ? "作品链接=盯单条作品;主页或 user_id=盯创作者近期作品"
    : "作品链接=盯单条视频;主页链接或 sec_uid=盯账号近期作品";
  const ckl = $("ck-label");
  if (ckl) ckl.textContent = PLATFORM === "xhs"
    ? "完整 Cookie(含 a1;发布需创作者会话)"
    : PLATFORM === "kuaishou" ? "完整 Cookie(含 userId 与 web_st)" : "完整 Cookie(含 sessionid)";
  if ($("ck-val")) $("ck-val").placeholder = PLATFORM === "xhs"
    ? "从 creator.xiaohongshu.com 登录后复制完整 Cookie"
    : PLATFORM === "kuaishou" ? "从 www.kuaishou.com 登录后复制完整 Cookie"
    : "从浏览器开发者工具复制完整 Cookie";
  applyMonitorForm();
  if ($("t-kind") && PLATFORM !== "xhs") $("t-kind").value = "creator";
  // 不支持发布的平台:若正停在该面板则回到总览(当前三平台均支持,兜底保留)
  if (!pfHasPublish(PLATFORM)) {
    const pub = document.querySelector('[data-panel="publish"]');
    if (pub && pub.style.display !== "none") switchTab("overview");
  }
  csSyncAll();   // 平台切换可能改了下拉选项/值,同步自定义下拉显示
}
function applyMonitorForm() {
  const title = $("mon-add-title");
  const lbl = $("t-url-label");
  if (PLATFORM === "douyin" || PLATFORM === "kuaishou") {
    const isKs = PLATFORM === "kuaishou";
    if (title) title.innerHTML = (isKs ? '添加创作者监控' : '添加作品监控')
      + ' <span class="sub">监控并下载新作品</span>';
    if (lbl) lbl.textContent = isKs ? "创作者主页链接 / 短链 / user_id" : "主页链接 / 短链 / sec_uid";
    $("t-url").placeholder = isKs
      ? "粘贴快手创作者主页链接、v.kuaishou.com 短链或 user_id"
      : "粘贴抖音主页链接、v.douyin.com 短链或 sec_uid";
    return;
  }
  const kind = $("t-kind") ? $("t-kind").value : "creator";
  if (kind === "keyword") {
    if (title) title.innerHTML = '添加关键词监控 <span class="sub">盯一个搜索词的新笔记</span>';
    if (lbl) lbl.textContent = "搜索关键词";
    $("t-url").placeholder = "例如:口红试色 / 露营装备";
  } else {
    if (title) title.innerHTML = '添加创作者监控 <span class="sub">监控并下载新笔记</span>';
    if (lbl) lbl.textContent = "创作者主页链接 / xhslink 短链 / user_id";
    $("t-url").placeholder = "粘贴小红书创作者主页链接、xhslink 短链或 24 位 user_id";
  }
}

// ─── 标签页切换 ───
function switchTab(name) {
  document.querySelectorAll("[data-panel]").forEach(p => { p.style.display = p.dataset.panel === name ? "" : "none"; });
  document.querySelectorAll(".navitem").forEach(t => {
    t.classList.toggle("active", t.dataset.tab === name);
  });
  try { localStorage.setItem("dym-tab", name); } catch (e) {}
  if (name === "hub") { refreshHubSummary(); refreshHubPanel(); }
  else stopDmStream();   // 离开本账号管理即断开私信实时流
}

// ─── 扫码登录(真实浏览器窗口) ───
let qrTimer = null;
// 登录前选代理:返回 "" (不用) | "auto" | 具体url | null(取消)
async function choosePreLoginProxy() {
  let opts = [];
  try { opts = await api("/api/proxies/options"); } catch (e) { }
  const options = [
    { value: "auto", label: opts.length ? "🔀 自动分配(占用最少)" : "🔀 自动分配(池为空→不用代理)" },
    ...opts.map(p => ({ value: p.url, label: `${p.label} · ${p.status} · 占用${p.used_by} · ${p.masked}${p.enabled ? "" : " · 已停用"}` })),
    { value: "__custom__", label: "✎ 手动输入指定代理…" },
    { value: "", label: "🚫 不用代理(走本机真实 IP)" },
  ];
  const v = await uiSelect({
    title: "选择本次登录使用的代理",
    hint: "整个登录/扫码过程都走它,从一开始就绑定这条 IP(最稳)。",
    options, value: "auto",
  });
  if (v === null) return null;
  if (v === "__custom__") {
    const url = await uiPrompt({
      title: "手动输入指定代理",
      hint: "http://user:pass@host:port 或 socks5://host:port;裸 ip:port 默认 HTTP",
      placeholder: "http://user:pass@host:port" });
    if (url === null || !url.trim()) return null;
    return url.trim();
  }
  return v;
}
function loginStartUrl(path, proxy) {
  return path + "?proxy=" + encodeURIComponent(proxy);
}
async function startLogin() {
  const proxy = await choosePreLoginProxy();
  if (proxy === null) return;
  $("cookiebox").style.display = "none";
  $("qrbox").style.display = "block";
  $("qrstatus").textContent = "正在打开浏览器窗口…";
  try {
    const res = await api(loginStartUrl("/api/login/browser/start", proxy), { method: "POST" });
    $("qrstatus").innerHTML = "🪟 已弹出浏览器窗口,请在<b>那个窗口</b>里点击「登录」并用抖音 App 扫码。<br>完成后这里会自动刷新。";
    pollLogin(res.task_id);
  } catch (e) { $("qrstatus").textContent = "启动失败: " + e.message; toast("登录启动失败:" + e.message, "err"); }
}
function pollLogin(tid) {
  clearInterval(qrTimer);
  qrTimer = setInterval(async () => {
    try {
      const res = await api("/api/login/browser/poll?task_id=" + tid);
      if (res.status === "confirmed") {
        clearInterval(qrTimer);
        $("qrstatus").textContent = "登录成功 ✓ " + (res.nickname || "");
        toast("登录成功 " + (res.nickname || ""), "ok");
        setTimeout(() => { $("qrbox").style.display = "none"; refreshAccounts(); }, 1200);
      } else if (res.status === "expired") {
        clearInterval(qrTimer); $("qrstatus").textContent = "超时未登录,请重试"; toast("二维码超时,请重试", "err");
      } else if (res.status === "error") {
        clearInterval(qrTimer); $("qrstatus").textContent = "出错: " + (res.error || ""); toast("登录出错:" + (res.error || ""), "err");
      }
    } catch (e) { clearInterval(qrTimer); $("qrstatus").textContent = e.message; }
  }, 2000);
}

// ─── 创作者登录(自有账号评论模式用) ───
async function startCreatorLogin() {
  const proxy = await choosePreLoginProxy();
  if (proxy === null) return;
  $("cookiebox").style.display = "none";
  $("qrbox").style.display = "block";
  $("qrstatus").textContent = "正在打开创作中心窗口…";
  try {
    const res = await api(loginStartUrl("/api/login/creator/start", proxy), { method: "POST" });
    $("qrstatus").innerHTML = "🪟 已弹出<b>创作中心</b>窗口,请在那个窗口里扫码登录你的抖音号。<br>登录态同样可用于公开抓取。";
    pollLogin(res.task_id);
  } catch (e) { $("qrstatus").textContent = "启动失败: " + e.message; toast("创作者登录启动失败:" + e.message, "err"); }
}

// ─── 小红书扫码登录 ───
async function startXhsLogin() {
  const proxy = await choosePreLoginProxy();
  if (proxy === null) return;
  $("cookiebox").style.display = "none";
  $("qrbox").style.display = "block";
  $("qrstatus").textContent = "正在打开小红书窗口…";
  try {
    const res = await api(loginStartUrl("/api/login/xhs/start", proxy), { method: "POST" });
    $("qrstatus").innerHTML = "🪟 已弹出<b>小红书</b>窗口,扫码登录后会<b>自动跳到创作平台</b>:<br>· 只看/评论/预览 → 扫完 www 即可,不用管创作平台;<br>· 还要<b>发布</b> → 若创作平台提示登录/同意,请在窗口里<b>完成它</b>(拿到后会自动收尾)。<br>整个过程<b>别急着关窗口</b>,完成后这里自动刷新。";
    pollLogin(res.task_id);
  } catch (e) { $("qrstatus").textContent = "启动失败: " + e.message; toast("小红书登录启动失败:" + e.message, "err"); }
}

// ─── 小红书创作者登录(发布用) ───
async function startXhsCreatorLogin() {
  const proxy = await choosePreLoginProxy();
  if (proxy === null) return;
  $("cookiebox").style.display = "none";
  $("qrbox").style.display = "block";
  $("qrstatus").textContent = "正在打开小红书创作平台窗口…";
  try {
    const res = await api(loginStartUrl("/api/login/xhs-creator/start", proxy), { method: "POST" });
    $("qrstatus").innerHTML = "🪟 已弹出<b>小红书创作平台</b>窗口,请扫码登录(此登录态用于<b>发布</b>)。<br>登录成功后稍等一两秒再关窗口。";
    pollLogin(res.task_id);
  } catch (e) { $("qrstatus").textContent = "启动失败: " + e.message; toast("创作者登录启动失败:" + e.message, "err"); }
}

// ─── 快手扫码登录 ───
async function startKsLogin() {
  const proxy = await choosePreLoginProxy();
  if (proxy === null) return;
  $("cookiebox").style.display = "none";
  $("qrbox").style.display = "block";
  $("qrstatus").textContent = "正在打开快手窗口…";
  try {
    const res = await api(loginStartUrl("/api/login/kuaishou/start", proxy), { method: "POST" });
    $("qrstatus").innerHTML = "🪟 已弹出<b>快手</b>窗口,请在那个窗口里点击「登录」并用<b>快手 App</b> 扫码。<br>完成后这里会自动刷新。";
    pollLogin(res.task_id);
  } catch (e) { $("qrstatus").textContent = "启动失败: " + e.message; toast("快手登录启动失败:" + e.message, "err"); }
}

// ─── 快手创作者登录(发布用) ───
async function startKsCreatorLogin() {
  const proxy = await choosePreLoginProxy();
  if (proxy === null) return;
  $("cookiebox").style.display = "none";
  $("qrbox").style.display = "block";
  $("qrstatus").textContent = "正在打开快手创作平台窗口…";
  try {
    const res = await api(loginStartUrl("/api/login/kuaishou-creator/start", proxy), { method: "POST" });
    $("qrstatus").innerHTML = "🪟 已弹出<b>快手创作平台</b>窗口(cp.kuaishou.com),请扫码登录(此登录态用于<b>发布</b>)。<br>登录成功后稍等一两秒再关窗口。";
    pollLogin(res.task_id);
  } catch (e) { $("qrstatus").textContent = "启动失败: " + e.message; toast("创作者登录启动失败:" + e.message, "err"); }
}

// ─── Cookie 登录 ───
function toggleCookie() {
  $("qrbox").style.display = "none";
  clearInterval(qrTimer);
  const b = $("cookiebox");
  b.style.display = b.style.display === "none" ? "block" : "none";
}
async function saveCookie() {
  const cookie = $("ck-val").value.trim();
  if (!cookie) { toast("请先粘贴 Cookie", "err"); return; }
  try {
    await api("/api/login/cookie", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ cookie, nickname: $("ck-nick").value.trim(), platform: PLATFORM }),
    });
    $("ck-val").value = ""; $("cookiebox").style.display = "none";
    toast("Cookie 已保存", "ok"); refreshAccounts();
  } catch (e) { toast("保存失败:" + e.message, "err"); }
}

// ─── 账号 ───
let ACCOUNTS = [];
let MONITORS = [], WATCHES = [], CONTENT_SRC = "", COMMENT_SRC = "", CONTENTS = [];
function monitorName(t) { return t.target_kind === "keyword" ? "#" + t.keyword : (t.nickname || (t.sec_uid || "").slice(0, 12)); }
function watchName(w) { return w.title || w.aweme_id || (w.sec_uid || "").slice(0, 12); }
function monitorById(id) { return MONITORS.find(t => t.id === id); }
function watchById(id) { return WATCHES.find(w => w.id === id); }
function srcChip(name) { return `<span class="src-chip" title="来源监控:${esc(name)}">${ic("i-target")}${esc(name)}</span>`; }
function populateContentSrc() {
  const sel = $("content-src"); if (!sel) return;
  sel.innerHTML = `<option value="">全部来源</option>` +
    MONITORS.map(t => `<option value="${t.id}">${esc(monitorName(t))}</option>`).join("");
  if (!MONITORS.some(t => String(t.id) === CONTENT_SRC)) CONTENT_SRC = "";
  sel.value = CONTENT_SRC;
}
function populateCommentSrc() {
  const sel = $("comment-src"); if (!sel) return;
  sel.innerHTML = `<option value="">全部来源</option>` +
    WATCHES.map(w => `<option value="${w.id}">${esc(watchName(w))}</option>`).join("");
  if (!WATCHES.some(w => String(w.id) === COMMENT_SRC)) COMMENT_SRC = "";
  sel.value = COMMENT_SRC;
}
function onContentSrc() { CONTENT_SRC = $("content-src").value; selContent.clear(); refreshContents(); }
function onCommentSrc() { COMMENT_SRC = $("comment-src").value; selComment.clear(); refreshComments(); }
async function refreshAccounts() {
  const accs = await api("/api/accounts?platform=" + PLATFORM);
  ACCOUNTS = accs;
  $("stat-acc").textContent = accs.length;
  $("acc-table").querySelector("tbody").innerHTML = accs.map(a => {
    const isXhs = a.platform === "xhs";
    const isKs = a.platform === "kuaishou";
    const idName = isXhs ? "小红书号 " : isKs ? "快手号 " : "抖音号 ";
    const secName = (isXhs || isKs) ? "user_id " : "sec_uid ";
    const idline = [
      a.douyin_id ? idName + esc(a.douyin_id) : null,
      a.sec_uid ? secName + esc(a.sec_uid).slice(0, 16) + "…" : null,
    ].filter(Boolean).join(" · ");
    const detail = [
      a.aweme_count ? a.aweme_count + (isXhs ? " 笔记" : " 作品") : null,
      a.follower_count ? fmtNum(a.follower_count) + " 粉丝" : null,
      isXhs ? "扫码登录" : (a.login_type === "cookie" ? "Cookie 登录" : "扫码登录"),
      a.has_storage ? "登录态有效" : "无登录态",
      `被 ${a.monitor_count} 个监控使用`,
      a.created_at ? "登录于 " + new Date(a.created_at + "Z").toLocaleString() : null,
    ].filter(Boolean).join(" · ");
    const pill = isXhs
      ? (a.has_creator
          ? `<span class="pill active has-ic ic-text" title="已完成创作者登录,可发布">${ic("i-film")}创作者号</span>`
          : `<span class="pill bare has-ic ic-text" title="仅监控/读取,未授权创作平台,不能发布">${ic("i-eye")}读取号</span>`)
      : `<span class="pill ${a.has_creator ? "active" : "bare"} has-ic ic-text" title="${a.has_creator ? "创作者登录,可用于创作中心评论模式,也可抓取" : "普通抓取账号"}">${a.has_creator ? ic("i-film") + "创作者号" : ic("i-card") + "抓取号"}</span>`;
    // 代理(风控隔离):有代理显示脱敏地址 + 状态;无代理高亮提醒(多账号同 IP 有关联风险)
    const pxText = { ok: "代理正常", bad: "代理不可用", unknown: "代理未测" };
    const pxCls = a.proxy_status === "ok" ? "active" : a.proxy_status === "bad" ? "invalid" : "bare";
    const proxyLine = a.has_proxy
      ? `<div class="mut" style="font-size:11px;margin-top:2px">代理 <code>${esc(a.proxy)}</code> <span class="pill ${pxCls}">${pxText[a.proxy_status] || a.proxy_status}</span></div>`
      : `<div class="ic-text" style="font-size:11px;margin-top:2px;color:var(--warn)">${ic("i-info")}未配置代理(走本机真实 IP,多账号有关联风险)</div>`;
    return `<tr>
      <td>
        <div class="user-cell">
          ${a.avatar ? `<img class="avatar" src="${a.avatar}" alt="" referrerpolicy="no-referrer">` : ""}
          <div>
            <div><b>${esc(a.nickname)}</b> ${pill}</div>
            ${idline ? `<div class="mut" style="font-size:11px;margin-top:2px">${idline}</div>` : ""}
            <div class="mut" style="font-size:11px;margin-top:2px">${esc(detail)}</div>
            ${proxyLine}
          </div>
        </div>
      </td>
      <td><span class="pill ${a.status}">${a.status === "invalid" ? "登录失效" : "正常"}</span></td>
      <td class="acttd">
        ${a.status === "invalid"
          ? `<button class="sm" style="background:var(--warn);border-color:transparent;color:#1a1a1a" onclick="relogin(${a.id})">重新登录</button>`
          : `<button class="ghost sm" onclick="relogin(${a.id})" title="${isXhs ? "重登可升级创作平台授权(发布需要)" : "重新扫码登录"}">重新登录</button>`}
        <button class="ghost sm" onclick="refreshProfile(${a.id})">刷新资料</button>
        <button class="ghost sm" onclick="openAccountHub(${a.id})" title="查看该账号的作品 / 关注 / 粉丝 / 私信">数据</button>
        <button class="ghost sm" onclick="openAccountBrowser(${a.id})" title="用该账号登录态弹出真实浏览器窗口,手动收发私信 / 维护 / 抓接口(关窗即保存)">打开浏览器</button>
        <button class="ghost sm" onclick="setProxy(${a.id})" title="设置/分配该账号专属代理(防多账号关联)">代理</button>
        ${a.has_proxy ? `<button class="ghost sm" onclick="testProxy(${a.id})" title="经该代理实连一次,验证可用">测代理</button>` : ""}
        <button class="ghost sm" onclick="delAccount(${a.id})" aria-label="删除账号">删除</button>
      </td>
    </tr>`;
  }).join("") || empty(3, "还没有账号", "i-user", "用上方按钮扫码登录,或粘贴 Cookie 添加一个账号");
  if ($("tb-acc")) $("tb-acc").textContent = accs.length;
  populateAccountSelect();
  populateWatchAccount();
  populatePubAcc();
  populateAcAccount();
  populateHubAccounts();
  const at = document.querySelector('.navitem.active');
  if (at && at.dataset.tab === "hub") refreshHubPanel();
}

// ═══════════ 账号管理(独立面板:我的作品 / 关注 / 粉丝 / 私信)═══════════
// 当前操作的账号 id —— 按平台各记各的,切平台不串号、不串数
let HUB_ACC = "";
let HUB_TAB = (() => { try { return localStorage.getItem("dym-hubtab") || "myworks"; } catch (e) { return "myworks"; } })();
let DM_CONV = null;     // 当前打开的会话 id
let DM_CONVS = [];      // 会话缓存(供发送时取 peer 信息)
function hubAccKey() { return "dym-hubacc:" + PLATFORM; }
function loadHubAcc() { try { HUB_ACC = localStorage.getItem(hubAccKey()) || ""; } catch (e) { HUB_ACC = ""; } }
function setHubAcc(id) { HUB_ACC = String(id || ""); try { localStorage.setItem(hubAccKey(), HUB_ACC); } catch (e) {} if (HUB_TAB === "dm") startDmStream(); }

// 用该账号登录态弹出真实浏览器窗口,留给用户手动操作(收发私信 / 维护 / F12 抓接口)
async function openAccountBrowser(id) {
  await withBusy(evtBtn(), "打开中", async () => {
    try {
      await api("/api/accounts/" + id + "/open-browser", { method: "POST" });
      toast("已弹出该账号浏览器窗口;用完请关窗(关窗即保存登录态)。窗口开着时该账号后台同步会暂停", "ok", 6000);
    } catch (e) { toast("打开失败:" + e.message, "err"); }
  });
}

// 私信页:用当前选中账号打开真实浏览器手动收发(抖音私信走 WS,只能这样)
function openHubAccountBrowser() {
  if (!HUB_ACC) { toast("请先选择账号", "err"); return; }
  openAccountBrowser(+HUB_ACC);
}

// 从「账号」面板某行跳转查看该账号的本账号数据(作品/关注/粉丝/私信)
function openAccountHub(id) {
  setHubAcc(id);
  const s = $("hub-acc"); if (s) { s.value = HUB_ACC; if (s._csSync) s._csSync(); }
  DM_CONV = null;
  refreshHubSummary();
  switchTab("hub");
  switchHubTab("myworks");   // 默认落到「我的作品」,可再切关注/粉丝/私信
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
  DM_CONV = null;
  refreshHubSummary();
  refreshHubPanel();
}
// 面板内子标签(我的作品/关注/粉丝/私信)切换
function switchHubTab(name) {
  HUB_TAB = name;
  try { localStorage.setItem("dym-hubtab", name); } catch (e) {}
  document.querySelectorAll("[data-hubpanel]").forEach(p => { p.style.display = p.dataset.hubpanel === name ? "" : "none"; });
  document.querySelectorAll("[data-hubtab]").forEach(t => t.classList.toggle("active", t.dataset.hubtab === name));
  if (name === "dm") startDmStream(); else stopDmStream();
  refreshHubPanel();
}
// 计数徽章:纯查库汇总,进面板/换账号/切平台即刷新,不用点进子页才有数
async function refreshHubSummary() {
  const ids = { works: "hb-myworks", following: "hb-following", fans: "hb-fans", dm: "hb-dm" };
  const setAll = r => Object.entries(ids).forEach(([k, i]) => { const el = $(i); if (el) el.textContent = (r && r[k]) || 0; });
  if (!HUB_ACC) { setAll(null); return; }
  try { setAll(await api("/api/hub/summary?account_id=" + HUB_ACC)); }
  catch (e) { setAll(null); }
}
function refreshHubPanel() {
  const active = document.querySelector('.navitem.active');
  if (!active || active.dataset.tab !== "hub") return;
  if (HUB_TAB === "myworks") refreshMyWorks();
  else if (HUB_TAB === "following") refreshFollows("following");
  else if (HUB_TAB === "fans") refreshFollows("fan");
  else if (HUB_TAB === "dm") { refreshDmConvs(); startDmStream(); }
}
function hubGridEmpty(text, sub = "") {
  return `<div class="empty" style="width:100%;column-span:all;break-inside:avoid"><div class="empty-ic">${ic("i-inbox")}</div>` +
    `<div class="empty-t">${esc(text)}</div>${sub ? `<div class="empty-sub">${esc(sub)}</div>` : ""}</div>`;
}

// ── 我的作品 ──
async function refreshMyWorks() {
  const grid = $("mw-grid"); if (!grid) return;
  if (!HUB_ACC) { grid.innerHTML = hubGridEmpty("请先选择已登录账号"); return; }
  try {
    const list = await api("/api/account-works?account_id=" + HUB_ACC);
    if ($("hb-myworks")) $("hb-myworks").textContent = list.length;
    grid.innerHTML = list.length ? list.map(workCard).join("")
      : hubGridEmpty("暂无作品", "点右上「同步作品」抓取本账号已发布作品");
  } catch (e) { grid.innerHTML = hubGridEmpty("加载失败:" + e.message); }
}
function workLink(platform, id) {
  id = encodeURIComponent(id);
  if (platform === "xhs") return "https://www.xiaohongshu.com/explore/" + id;
  if (platform === "kuaishou") return "https://www.kuaishou.com/short-video/" + id;
  return "https://www.douyin.com/video/" + id;
}
function openWork(platform, id) { try { window.open(workLink(platform, id), "_blank", "noopener"); } catch (e) {} }
function workCard(w) {
  const oc = `onclick="openWork('${esc(w.platform)}','${esc(w.item_id).replace(/'/g, "\\'")}')"`;
  // 图裂时回退占位(onerror 换成灰底图标),避免绝对角标压到标题
  const cover = w.cover_url
    ? `<img class="ncard-cover" src="${w.cover_url}" referrerpolicy="no-referrer" loading="lazy" alt="" ${oc}
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
        <button class="ghost sm" onclick="openWorkComments(${w.id},'${esc(w.platform)}','${title.replace(/'/g, "\\'")}')">${ic("i-msg")}评论</button>
      </div>
    </div>
  </div>`;
}
async function syncMyWorks() {
  if (!HUB_ACC) { toast("请先选择账号", "err"); return; }
  await withBusy(evtBtn(), "同步中", async () => {
    try { const r = await api("/api/accounts/" + HUB_ACC + "/works/sync", { method: "POST" }); toast(`同步完成:抓到 ${r.fetched} 条,新增 ${r.added}`, "ok"); }
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
  await loadWorkComments();
}
function hideWorkComments() { $("wcmodal").style.display = "none"; WC_WORK = null; }
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
    <div class="wc-text">${esc(c.text || "")}</div>
    <div class="wc-meta">${ic("i-heart")}${fmtNum(c.like_count)}${c.is_reply ? " · 回复" : ""}</div>
  </div>`;
}
async function syncWorkComments() {
  if (!WC_WORK) return;
  await withBusy(evtBtn(), "抓取中", async () => {
    try { const r = await api("/api/account-works/" + WC_WORK.id + "/comments/sync", { method: "POST" }); toast(`抓到 ${r.fetched} 条,新增 ${r.added}`, "ok"); }
    catch (e) { toast("抓取失败:" + e.message, "err"); }
  });
  await loadWorkComments();
}

// ── 关注 / 粉丝 ──
// 小红书网页端不提供关注/粉丝列表(App 专属:实测无接口、无弹层),不做无用的同步
const XHS_FOLLOW_NA = "小红书网页端不提供关注 / 粉丝列表(仅 App 可见),无法同步。抖音 / 快手可正常同步。";
async function refreshFollows(direction) {
  const tbody = $(direction === "fan" ? "fans-table" : "following-table"); if (!tbody) return;
  if (PLATFORM === "xhs") {
    const badge = $(direction === "fan" ? "hb-fans" : "hb-following");
    if (badge) badge.textContent = "—";
    tbody.innerHTML = empty(3, direction === "fan" ? "粉丝列表网页端不可用" : "关注列表网页端不可用",
      "i-info", XHS_FOLLOW_NA);
    return;
  }
  if (!HUB_ACC) { tbody.innerHTML = empty(3, "请先选择已登录账号", "i-user"); return; }
  try {
    const list = await api(`/api/follows?account_id=${HUB_ACC}&direction=${direction}`);
    const badge = $(direction === "fan" ? "hb-fans" : "hb-following");
    if (badge) badge.textContent = list.length;
    tbody.innerHTML = list.length ? list.map(f => followRow(f, direction)).join("")
      : empty(3, direction === "fan" ? "暂无粉丝数据" : "暂无关注数据", "i-user", "点右上「同步」抓取");
  } catch (e) { tbody.innerHTML = empty(3, "加载失败:" + e.message, "i-info"); }
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
      ${f.avatar ? `<img class="avatar" src="${f.avatar}" referrerpolicy="no-referrer" alt="">` : `<span class="avatar"></span>`}
      <div><div><b>${esc(f.nickname)}</b></div>${f.signature ? `<div class="fu-sign">${esc(f.signature)}</div>` : ""}</div>
    </div></td>
    <td>${rel}</td>
    <td class="acttd">${act}</td>
  </tr>`;
}
async function syncFollows(direction) {
  if (PLATFORM === "xhs") { toast(XHS_FOLLOW_NA, "info", 6000); return; }
  if (!HUB_ACC) { toast("请先选择账号", "err"); return; }
  await withBusy(evtBtn(), "同步中", async () => {
    try { const r = await api(`/api/accounts/${HUB_ACC}/follows/sync?direction=${direction}`, { method: "POST" }); toast(`同步完成:抓到 ${r.fetched} 条,新增 ${r.added}`, "ok"); }
    catch (e) { toast("同步失败:" + e.message, "err"); }
  });
  refreshFollows(direction);
}
async function actFollow(action, edgeId) {
  // 取该行 follow 边的目标信息(从已渲染列表里拿)
  const dir = HUB_TAB === "fans" ? "fan" : "following";
  let edge = null;
  try { const list = await api(`/api/follows?account_id=${HUB_ACC}&direction=${dir}`); edge = list.find(x => x.id === edgeId); } catch (e) {}
  if (!edge) { toast("找不到该用户,请重新同步", "err"); return; }
  const label = action === "unfollow" ? "取关" : "回关";
  if (!await uiConfirm({ title: label + "确认", message: `确认对「${edge.nickname}」${label}?将打开浏览器窗口执行(有头窗口,可手动过验证码)。`, danger: action === "unfollow" })) return;
  await withBusy(evtBtn(), label + "中", async () => {
    try {
      await api("/api/account-actions", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ account_id: +HUB_ACC, action, target_uid: edge.uid, target_sec_uid: edge.sec_uid || "", target_nick: edge.nickname, run_now: true })
      });
      toast(label + "成功", "ok");
    } catch (e) { toast(label + "失败:" + e.message, "err"); }
  });
  refreshFollows(dir);
}

// ── 私信 ──
// ─── 私信实时接收(SSE):进 DM 面板订阅,新消息即时刷新;离开断开 ───
let DM_SSE = null, DM_SSE_ACC = "";
function startDmStream() {
  // 幂等:同账号已连就不重连(避免每次面板刷新/收到消息都断开重来)
  if (DM_SSE && DM_SSE_ACC === HUB_ACC && DM_SSE.readyState !== 2) return;
  stopDmStream();
  if (!HUB_ACC || PLATFORM !== "douyin") return;
  DM_SSE_ACC = HUB_ACC;
  try {
    DM_SSE = new EventSource(`/api/dm/stream?account_id=${HUB_ACC}`);
    DM_SSE.onmessage = (e) => {
      let evt; try { evt = JSON.parse(e.data); } catch (_) { return; }
      if (!evt || !evt.conv_id) return;
      // 当前打开的会话:实时刷新线程 + 标记已读(不让红点冒出来);否则只刷列表(会有红点)
      if (evt.conv_id === DM_CONV) { refreshDmMessages(); markDmRead(evt.conv_id); }
      else refreshDmConvs();
    };
    DM_SSE.onerror = () => { /* EventSource 自带重连 */ };
  } catch (_) {}
}
function stopDmStream() { if (DM_SSE) { try { DM_SSE.close(); } catch (_) {} DM_SSE = null; DM_SSE_ACC = ""; } }

async function refreshDmConvs() {
  const box = $("dm-convs"); if (!box) return;
  if (!HUB_ACC) { box.innerHTML = `<div class="empty" style="padding:24px"><div class="empty-t">请先选择账号</div></div>`; return; }
  try {
    const list = await api("/api/dm/conversations?account_id=" + HUB_ACC);
    DM_CONVS = list;
    if ($("hb-dm")) $("hb-dm").textContent = list.length;
    box.innerHTML = list.length ? list.map(convRow).join("")
      : `<div class="empty" style="padding:24px"><div class="empty-ic">${ic("i-send")}</div><div class="empty-t">暂无会话</div><div class="empty-sub">点右上「同步私信」</div></div>`;
    if (DM_CONV) { const el = box.querySelector(`.dm-conv[data-conv="${cssAttr(DM_CONV)}"]`); if (el) el.classList.add("active"); }
  } catch (e) { box.innerHTML = `<div class="empty" style="padding:24px"><div class="empty-t">加载失败:${esc(e.message)}</div></div>`; }
}
function cssAttr(s) { return (s || "").toString().replace(/"/g, '\\"'); }
function convRow(c) {
  return `<div class="dm-conv" data-conv="${esc(c.conv_id)}" onclick="openDmConv('${esc(c.conv_id).replace(/'/g, "\\'")}')">
    ${c.peer_avatar ? `<img class="avatar" src="${c.peer_avatar}" referrerpolicy="no-referrer" alt="">` : `<span class="avatar"></span>`}
    <div class="meta"><b>${esc(c.peer_nickname)}</b><div class="last">${esc(c.last_text || "")}</div></div>
    ${c.unread_count ? `<span class="unread">${c.unread_count}</span>` : ""}
  </div>`;
}
async function syncDm() {
  if (!HUB_ACC) { toast("请先选择账号", "err"); return; }
  await withBusy(evtBtn(), "同步中", async () => {
    try { const r = await api("/api/accounts/" + HUB_ACC + "/dm/sync", { method: "POST" }); toast(`同步完成:抓到 ${r.fetched} 个会话,新增 ${r.added}`, "ok"); }
    catch (e) { toast("同步失败:" + e.message, "err"); }
  });
  refreshDmConvs();
}
async function openDmConv(convId) {
  DM_CONV = convId;
  document.querySelectorAll("#dm-convs .dm-conv").forEach(e => e.classList.toggle("active", e.dataset.conv === convId));
  const thread = $("dm-thread");
  if (thread) thread.innerHTML = `<div class="empty"><div class="empty-t">加载聊天记录…</div></div>`;
  // 抖音:点开会话时无头拉历史(imapi get_by_conversation),落库后再渲染
  if (PLATFORM === "douyin") {
    try { await api(`/api/accounts/${HUB_ACC}/dm/conversations/${convId}/fetch-history`, { method: "POST" }); }
    catch (e) { /* 拉取失败也照常显示库里已有的(最后一条) */ }
  }
  markDmRead(convId);
  await refreshDmMessages();
}
// 标记已读:清红点,刷新左侧列表
function markDmRead(convId) {
  if (!HUB_ACC || !convId) return;
  api(`/api/accounts/${HUB_ACC}/dm/conversations/${convId}/mark-read`, { method: "POST" })
    .then(() => refreshDmConvs()).catch(() => {});
}
// 分享视频卡片(msg_type=8):封面+标题+作者,点击跳抖音该视频
function dmVideoCard(c) {
  const url = c.item_id ? `https://www.douyin.com/video/${encodeURIComponent(c.item_id)}` : "#";
  const cover = c.cover
    ? `<img src="${esc(c.cover)}" loading="lazy" referrerpolicy="no-referrer" onerror="this.style.display='none'">`
    : "";
  const avatar = c.avatar
    ? `<img class="av" src="${esc(c.avatar)}" loading="lazy" referrerpolicy="no-referrer" onerror="this.style.display='none'">`
    : "";
  return `<a class="dm-vcard" href="${url}" target="_blank" rel="noopener">
    <div class="cov">${cover}<span class="play">▶</span></div>
    <div class="meta">
      <div class="ttl">${esc(c.title || "[视频]")}</div>
      <div class="au">${avatar}<span>${esc(c.author || "")}</span></div>
    </div>
  </a>`;
}
function dmBody(m) {
  if (m.card && m.card.kind === "video") return dmVideoCard(m.card);
  return esc(m.text);
}
async function refreshDmMessages() {
  const thread = $("dm-thread"); if (!thread || !HUB_ACC || !DM_CONV) return;
  try {
    const msgs = await api(`/api/dm/messages?account_id=${HUB_ACC}&conv_id=${encodeURIComponent(DM_CONV)}`);
    thread.innerHTML = msgs.length
      ? msgs.map(m => `<div class="dm-bubble ${m.direction === "out" ? "out" : "in"}${m.card ? " card" : ""}">${dmBody(m)}<span class="t">${fmtTime(m.create_time)}</span></div>`).join("")
      : `<div class="empty"><div class="empty-t">暂无消息记录</div><div class="empty-sub">该会话没有可拉取的历史(或对方为系统号)</div></div>`;
    thread.scrollTop = thread.scrollHeight;
  } catch (e) { thread.innerHTML = `<div class="empty"><div class="empty-t">加载失败:${esc(e.message)}</div></div>`; }
}
async function sendDm() {
  const inp = $("dm-input"); const text = (inp.value || "").trim();
  if (!HUB_ACC) { toast("请先选择账号", "err"); return; }
  if (!DM_CONV) { toast("请先选择左侧会话", "err"); return; }
  if (!text) return;
  const c = DM_CONVS.find(x => x.conv_id === DM_CONV) || {};
  await withBusy(evtBtn(), "发送中", async () => {
    try {
      await api("/api/account-actions", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ account_id: +HUB_ACC, action: "send_dm", target_uid: c.peer_uid || "", target_sec_uid: c.peer_sec_uid || "", target_nick: c.peer_nickname || "", conv_id: DM_CONV, content: text, run_now: true })
      });
      inp.value = ""; toast("已发送", "ok");
      // 发完重拉历史,展示刚发出的消息(imapi 有短暂延迟,稍等再拉)
      await new Promise(r => setTimeout(r, 700));
      await openDmConv(DM_CONV);
    } catch (e) { toast("发送失败:" + e.message, "err"); }
  });
}
function accOptions(list, ph) {
  return `<option value="">${ph}</option>` +
    list.map(a => `<option value="${a.id}">${esc(a.nickname)}${a.has_creator ? " · 创作号" : ""}</option>`).join("");
}
function populateAccountSelect() {
  const sel = $("t-acc"); if (!sel) return;
  const xhs = PLATFORM === "xhs";
  sel.innerHTML = accOptions(ACCOUNTS, xhs ? "请选择小红书账号(必选)" : "不指定账号");
  // 小红书所有页面都要登录:自动选中第一个账号,避免漏选导致被弹登录墙
  if (xhs && ACCOUNTS.length) sel.value = String(ACCOUNTS[0].id);
}
function populateWatchAccount() {
  const sel = $("w-acc"); if (!sel) return;
  const xhs = PLATFORM === "xhs";
  const creatorOnly = !xhs && $("w-mode") && $("w-mode").value === "creator";
  const list = creatorOnly ? ACCOUNTS.filter(a => a.has_creator) : ACCOUNTS;
  const ph = xhs ? "请选择小红书账号(必选)"
    : (creatorOnly && list.length === 0 ? "无创作者账号,请先创作者登录" : "不指定账号");
  sel.innerHTML = accOptions(list, ph);
  if (xhs && list.length) sel.value = String(list[0].id);
}
async function refreshProfile(id) {
  const btn = evtBtn();
  await withBusy(btn, "拉取中", async () => {
    try { const r = await api("/api/accounts/" + id + "/refresh-profile", { method: "POST" }); const idLbl = (r.platform || PLATFORM) === "xhs" ? " · 小红书号 " : " · 抖音号 "; toast("资料已更新:" + (r.nickname || "") + (r.douyin_id ? idLbl + r.douyin_id : ""), "ok"); }
    catch (e) { toast("刷新失败:" + e.message, "err"); }
  });
  refreshAccounts();
}
async function setProxy(id) {
  const a = ACCOUNTS.find(x => x.id === id);
  let opts = [];
  try { opts = await api("/api/proxies/options"); } catch (e) { }
  const cur = a && a.has_proxy ? a.proxy : "";
  const options = [
    { value: "auto", label: "🔀 自动分配(占用最少)" },
    ...opts.map(p => ({ value: p.url, label: `${p.label} · ${p.status} · 占用${p.used_by} · ${p.masked}${p.enabled ? "" : " · 已停用"}` })),
    { value: "__custom__", label: "✎ 手动输入地址…" },
    { value: "", label: "🚫 清除代理(走真实 IP)" },
  ];
  const v = await uiSelect({
    title: "账号代理",
    hint: (a ? a.nickname + " · " : "") + "当前:" + (cur || "未配置"),
    options, value: (cur && opts.some(o => o.value === cur)) ? cur : "auto",
  });
  if (v === null) return;
  try {
    if (v === "auto") {
      const r = await api("/api/accounts/" + id + "/assign-proxy", { method: "POST" });
      toast("已从代理池分配:" + r.proxy, "ok");
    } else if (v === "__custom__") {
      const url = await uiPrompt({
        title: "手动输入代理", value: cur,
        hint: "http://user:pass@host:port 或 socks5://host:port;留空=清除",
        placeholder: "http://user:pass@host:port" });
      if (url === null) return;
      const r = await api("/api/accounts/" + id + "/proxy", {
        method: "PUT", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ proxy: url.trim() }) });
      toast(url.trim() ? "代理已设置:" + r.proxy : "代理已清除", "ok");
    } else {
      const r = await api("/api/accounts/" + id + "/proxy", {
        method: "PUT", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ proxy: v }) });
      toast(v ? "代理已设置:" + r.proxy : "代理已清除", "ok");
    }
    refreshAccounts(); refreshProxies();
  } catch (e) { toast("设置失败:" + e.message, "err"); }
}

// ─── 代理池 ───
let PROXIES = [];
let LAST_DETECT = null;   // {url, geo} 判别结果,加入池时一并带上归属地
async function refreshProxies() {
  const tb = $("proxy-table"); if (!tb) return;
  let rows = [];
  try { rows = await api("/api/proxies"); } catch (e) { return; }
  PROXIES = rows;
  const stCls = s => s === "ok" ? "active" : s === "bad" ? "invalid" : "bare";
  const stTxt = { ok: "正常", bad: "不可用", unknown: "未测" };
  const geoCell = p => {
    if (!p.geo_checked) return `<span class="pill bare">未测</span>`;
    const cls = p.is_mainland ? "active" : "invalid";
    const warn = p.is_mainland ? "" : ' <span title="非中国大陆 IP,与抖音/小红书国内账号时区不符,有风控风险">⚠️</span>';
    return `<div><span class="pill ${cls}">${esc(p.geo_loc || "未知")}</span>${warn}</div>` +
      (p.exit_ip ? `<div class="mut" style="font-size:11px;margin-top:2px">${esc(p.exit_ip)}${p.isp ? " · " + esc(p.isp) : ""}</div>` : "");
  };
  tb.querySelector("tbody").innerHTML = rows.map(p => `<tr>
      <td>
        <div><b>${esc(p.label || "(未命名)")}</b> <span class="pill ${stCls(p.status)}">${stTxt[p.status] || p.status}</span>${p.enabled ? "" : ' <span class="pill bare">已停用</span>'}</div>
        <div class="mut" style="font-size:11px;margin-top:2px"><code>${esc(p.url)}</code></div>
        ${p.note ? `<div class="mut" style="font-size:11px">${esc(p.note)}</div>` : ""}
      </td>
      <td>${geoCell(p)}</td>
      <td><span class="pill ${p.used_by ? "active" : "bare"}">${p.used_by} 个账号</span></td>
      <td class="acttd">
        <button class="ghost sm" onclick="editPoolProxy(${p.id})">编辑</button>
        <button class="ghost sm" onclick="testPoolProxy(${p.id})">测试</button>
        <button class="ghost sm" onclick="togglePoolProxy(${p.id},${p.enabled})">${p.enabled ? "停用" : "启用"}</button>
        <button class="ghost sm" onclick="delPoolProxy(${p.id},${p.used_by})">删除</button>
      </td>
    </tr>`).join("") || empty(4, "代理池为空", "i-shield", "添加住宅/4G 代理,账号即可一号一代理关联使用");
}
async function detectProxy() {
  const raw = $("px-url").value.trim();
  if (!raw) { toast("请先填代理地址", "err"); return; }
  const btn = event.target.closest("button"); btn.disabled = true; const old = btn.textContent; btn.textContent = "判别中…";
  try {
    const r = await api("/api/proxies/detect", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url: raw }) });
    if (!r.ok) { toast("判别失败:" + (r.error || ""), "err"); return; }
    if ($("px-proto") && (r.scheme === "http" || r.scheme === "socks5")) $("px-proto").value = r.scheme;
    $("px-url").value = r.recommend;        // 回填带协议的规范地址
    LAST_DETECT = { url: r.recommend, geo: r.geo || null };
    // 归属地写进备注(若备注为空),方便核对 IP 地区与账号是否一致
    if (r.geo_text && $("px-label") && !$("px-label").value.trim()) {
      const g = r.geo || {};
      $("px-label").value = [g.country, g.region, g.city].filter(Boolean).join("·") || "已判别";
    }
    const tag = r.scheme.toUpperCase() + (r.auth === "required" ? " · 需账密" : " · 免密");
    toast("判别:" + tag + (r.geo_text ? "  |  " + r.geo_text : "  |  归属地未取到"), r.browser_ok ? "ok" : "info");
    if (!r.browser_ok) toast("⚠️ " + r.note, "err", 8000);
  } catch (e) { toast("判别失败:" + e.message, "err"); }
  finally { btn.disabled = false; btn.textContent = old; }
}
async function addProxy() {
  let url = $("px-url").value.trim();
  if (!url) { toast("请填代理地址", "err"); return; }
  // 裸 ip:port 按所选协议补全;已带协议头则尊重原值
  if (!/:\/\//.test(url)) url = ($("px-proto") ? $("px-proto").value : "http") + "://" + url;
  const geo = (LAST_DETECT && LAST_DETECT.url === url) ? LAST_DETECT.geo : null;
  try {
    await api("/api/proxies", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url, label: $("px-label").value.trim(), geo }) });
    $("px-url").value = ""; $("px-label").value = "";
    toast("已加入代理池", "ok"); refreshProxies();
  } catch (e) { toast("添加失败:" + e.message, "err"); }
}
async function delPoolProxy(id, used) {
  if (!await uiConfirm({ title: "删除代理", okText: "删除", danger: true,
    message: "删除该代理?" + (used ? `\n⚠️ 有 ${used} 个账号正在用它,删除后这些账号需另选代理。` : "") })) return;
  try { await api("/api/proxies/" + id, { method: "DELETE" }); toast("已删除", "ok"); refreshProxies(); }
  catch (e) { toast("删除失败:" + e.message, "err"); }
}
async function editPoolProxy(id) {
  const p = PROXIES.find(x => x.id === id);
  if (!p) return;
  const label = await uiPrompt({
    title: "编辑代理备注",
    hint: p.url + (p.geo_loc ? "  ·  " + p.geo_loc : ""),
    value: p.label || "", placeholder: "如 住宅-广东-01" });
  if (label === null) return;
  try {
    await api("/api/proxies/" + id, {
      method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ label: label.trim() }) });
    toast("备注已更新", "ok"); refreshProxies();
  } catch (e) { toast("更新失败:" + e.message, "err"); }
}
async function togglePoolProxy(id, enabled) {
  try {
    await api("/api/proxies/" + id, {
      method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled: !enabled }) });
    refreshProxies();
  } catch (e) { toast("操作失败:" + e.message, "err"); }
}
async function testPoolProxy(id) {
  const btn = event.target.closest("button"); btn.disabled = true; const old = btn.textContent; btn.textContent = "测试中…";
  try { const r = await api("/api/proxies/" + id + "/test", { method: "POST" });
    toast((r.ok ? "可用 ✓ " : "不可用 ✗ ") + (r.detail || "") + (r.geo_text ? "  |  " + r.geo_text : ""), r.ok ? "ok" : "err"); }
  catch (e) { toast("测试失败:" + e.message, "err"); }
  finally { btn.disabled = false; btn.textContent = old; refreshProxies(); }
}
async function testAllProxies() {
  if (!PROXIES.length) { toast("代理池为空", "info"); return; }
  toast("开始逐个测试…", "info");
  for (const p of PROXIES) {
    try { await api("/api/proxies/" + p.id + "/test", { method: "POST" }); } catch (e) { }
  }
  toast("测试完成", "ok"); refreshProxies();
}
async function importProxies() {
  const text = await uiPrompt({
    title: "批量导入代理",
    hint: "每行一个,支持 # 注释、空行;可写「备注,地址」。\n⚠️ 裸 ip:port 默认 HTTP;SOCKS5 需加 socks5:// 前缀。",
    multiline: true, rows: 8,
    placeholder: "住宅-01,1.2.3.4:8080\nsocks5://user:pass@5.6.7.8:1080" });
  if (text === null || !text.trim()) return;
  try {
    const r = await api("/api/proxies/import", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text }) });
    let msg = `导入完成:新增 ${r.added}`;
    if (r.skipped) msg += ` · 重复跳过 ${r.skipped}`;
    if (r.invalid) msg += ` · 格式无效 ${r.invalid}`;
    toast(msg, r.added ? "ok" : "info");
    refreshProxies();
  } catch (e) { toast("导入失败:" + e.message, "err"); }
}
async function assignAllProxies() {
  const noProxy = ACCOUNTS.filter(a => !a.has_proxy).length;
  if (!noProxy) { toast("所有账号都已配置代理", "info"); return; }
  if (!await uiConfirm({ title: "批量分配代理", message: `给 ${noProxy} 个未配代理的账号从池里自动分配(均衡,占用最少优先)?` })) return;
  const btn = event.target.closest("button"); if (btn) { btn.disabled = true; btn.textContent = "分配中…"; }
  try {
    const r = await api("/api/accounts/assign-proxies-all", { method: "POST" });
    let msg = `已分配 ${r.assigned} 个账号`;
    if (r.unassigned) msg += `,还有 ${r.unassigned} 个没分到(代理池不够,请再加代理)`;
    toast(msg, r.unassigned ? "info" : "ok");
    refreshAccounts(); refreshProxies();
  } catch (e) { toast("分配失败:" + e.message, "err"); }
  finally { if (btn) { btn.disabled = false; btn.textContent = "给账号批量分配"; } }
}
async function testProxy(id) {
  const btn = event.target.closest("button"); btn.disabled = true; const old = btn.textContent; btn.textContent = "测试中…";
  try {
    const r = await api("/api/accounts/" + id + "/test-proxy", { method: "POST" });
    toast((r.ok ? "代理可用 ✓ " : "代理不可用 ✗ ") + (r.detail || ""), r.ok ? "ok" : "err");
  } catch (e) { toast("测试失败:" + e.message, "err"); }
  finally { btn.disabled = false; btn.textContent = old; refreshAccounts(); }
}
async function relogin(id) {
  const btn = evtBtn();
  await withBusy(btn, "启动中", async () => {
    try {
      const res = await api("/api/accounts/" + id + "/relogin/start", { method: "POST" });
      toast("已打开浏览器窗口,请扫码重新登录该账号", "info");
      pollReloginTask(res.task_id);
    } catch (e) { toast("启动失败:" + e.message, "err"); }
  });
}
function pollReloginTask(tid) {
  const t = setInterval(async () => {
    try {
      const r = await api("/api/login/browser/poll?task_id=" + tid);
      if (r.status === "confirmed") { clearInterval(t); toast("重新登录成功 " + (r.nickname || ""), "ok"); refreshAccounts(); }
      else if (r.status === "expired") { clearInterval(t); toast("超时未登录,请重试", "err"); }
      else if (r.status === "error") { clearInterval(t); toast("出错:" + (r.error || ""), "err"); }
    } catch (e) { clearInterval(t); }
  }, 2000);
}
async function delAccount(id) {
  const a = ACCOUNTS.find(x => x.id === id);
  const warn = a && a.monitor_count > 0 ? `\n⚠️ 有 ${a.monitor_count} 个监控正在用它,删除后这些监控将无登录态(需改用其它账号)。` : "";
  if (!await uiConfirm({ title: "删除账号", message: "删除该账号?" + warn, okText: "删除", danger: true })) return;
  try { await api("/api/accounts/" + id, { method: "DELETE" }); toast("账号已删除", "ok"); refreshAccounts(); }
  catch (e) { toast("删除失败:" + e.message, "err"); }
}

// ─── 下载设置 ───
async function loadSettings() {
  try {
    const s = await api("/api/settings");
    $("dl-dir").value = s.download_dir || "";
    $("dl-quality").value = s.video_quality || "highest";
    if ($("ai-enabled")) {
      $("ai-enabled").checked = !!s.ai_enabled;
      $("ai-base").value = s.ai_base_url || "";
      $("ai-model").value = s.ai_model || "";
      $("ai-temp").value = s.ai_temperature || "0.9";
      $("ai-prompt").value = s.ai_prompt || "";
      $("ai-key").placeholder = s.ai_api_key_set ? "已保存(留空=不修改)" : "API Key";
    }
    csSyncAll();
  } catch (e) {}
}
async function saveAiSettings() {
  $("ai-msg").textContent = "保存中…";
  const body = {
    ai_enabled: $("ai-enabled").checked, ai_base_url: $("ai-base").value.trim(),
    ai_model: $("ai-model").value.trim(), ai_temperature: $("ai-temp").value.trim() || "0.9",
    ai_prompt: $("ai-prompt").value,
  };
  const key = $("ai-key").value.trim();
  if (key) body.ai_api_key = key;
  try {
    const s = await api("/api/settings", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    $("ai-key").value = ""; $("ai-key").placeholder = s.ai_api_key_set ? "已保存(留空=不修改)" : "API Key";
    $("ai-msg").textContent = "已保存 ✓ " + (s.ai_enabled ? "(规则勾选「用 AI」即生效)" : "(当前未启用)");
    toast("AI 设置已保存", "ok");
  } catch (e) { $("ai-msg").textContent = "失败: " + e.message; toast("保存失败:" + e.message, "err"); }
}
async function testAi() {
  const btn = evtBtn();
  $("ai-msg").textContent = "测试中…";
  // 用当前表单值测(key 留空则用已保存的),方便保存前先验证
  const body = {
    base_url: $("ai-base").value.trim(), model: $("ai-model").value.trim(),
    prompt: $("ai-prompt").value, temperature: $("ai-temp").value.trim() || "0.9",
  };
  const key = $("ai-key").value.trim();
  if (key) body.api_key = key;
  await withBusy(btn, "测试中", async () => {
    try {
      const r = await api("/api/settings/ai-test", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      if (r.ok) { $("ai-msg").innerHTML = `连通正常 ✓ 样例文案:<b>${esc(r.sample || "")}</b>`; toast("AI 连通正常 ✓", "ok", 6000); }
      else { $("ai-msg").textContent = "连通失败:" + (r.error || ""); toast("AI 连通失败:" + (r.error || ""), "err", 8000); }
    } catch (e) { $("ai-msg").textContent = "失败:" + e.message; toast("测试失败:" + e.message, "err"); }
  });
}
async function saveSettings() {
  $("dl-msg").textContent = "保存中…";
  try {
    const s = await api("/api/settings", {
      method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ download_dir: $("dl-dir").value.trim(), video_quality: $("dl-quality").value }),
    });
    $("dl-dir").value = s.download_dir || "";
    $("dl-quality").value = s.video_quality || "highest";
    csSyncAll();
    $("dl-msg").textContent = "已保存 ✓ 新作品将按此设置下载";
    toast("下载设置已保存", "ok");
  } catch (e) { $("dl-msg").textContent = "失败: " + e.message; toast("保存失败:" + e.message, "err"); }
}
const QMAP = { "": "默认", highest: "原画", "1080": "1080P", "720": "720P", "540": "540P", lowest: "省流" };

// ─── 通知渠道 ───
const N_TEMPLATES = {
  bark: '{\n  "key": "你的Bark设备key",\n  "server": "https://api.day.app"\n}',
  dingtalk: '{\n  "webhook": "https://oapi.dingtalk.com/robot/send?access_token=xxx",\n  "secret": "加签密钥(可选)",\n  "keyword": "关键词(可选)"\n}',
  telegram: '{\n  "bot_token": "123:abc",\n  "chat_id": "你的chat_id"\n}',
};
function onTypeChange() { $("n-config").value = N_TEMPLATES[$("n-type").value] || ""; }
async function addChannel() {
  let config;
  try { config = JSON.parse($("n-config").value || "{}"); }
  catch (e) { $("n-msg").textContent = "配置不是合法 JSON"; toast("配置不是合法 JSON", "err"); return; }
  $("n-msg").textContent = "添加中…";
  try {
    await api("/api/notifications", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: $("n-name").value.trim(), type: $("n-type").value, config }),
    });
    $("n-name").value = ""; $("n-msg").textContent = "已添加 ✓"; toast("通知渠道已添加", "ok");
    refreshChannels();
  } catch (e) { $("n-msg").textContent = "失败: " + e.message; toast("添加失败:" + e.message, "err"); }
}
async function refreshChannels() {
  const cs = await api("/api/notifications");
  $("n-table").querySelector("tbody").innerHTML = cs.map(c => `<tr>
    <td>${esc(c.name)} <span class="mut">${c.type}</span></td>
    <td><span class="pill ${c.enabled ? "active" : "invalid"}">${c.enabled ? "启用" : "停用"}</span></td>
    <td class="acttd">
      <button class="ghost sm" onclick="testChannel(${c.id})">测试</button>
      <button class="ghost sm" onclick="toggleChannel(${c.id}, ${!c.enabled})">${c.enabled ? "停用" : "启用"}</button>
      <button class="ghost sm" onclick="delChannel(${c.id})">删除</button>
    </td></tr>`).join("") || empty(3, "还没有通知渠道", "i-bell", "添加 Bark / 飞书 / Webhook 等渠道,有新作品或新评论时推送给你");
}
async function testChannel(id) {
  const btn = event.target.closest("button"); btn.disabled = true; btn.textContent = "发送中…";
  try { const r = await api("/api/notifications/" + id + "/test", { method: "POST" }); btn.textContent = r.ok ? "成功 ✓" : "失败"; toast(r.ok ? "测试推送已发送" : "发送失败:" + (r.detail || ""), r.ok ? "ok" : "err"); }
  catch (e) { btn.textContent = "失败"; toast("发送失败:" + e.message, "err"); }
  setTimeout(() => { btn.disabled = false; btn.textContent = "测试"; }, 1500);
}
async function toggleChannel(id, enabled) { try { await api("/api/notifications/" + id, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ enabled }) }); refreshChannels(); } catch (e) { toast("操作失败:" + e.message, "err"); } }
async function delChannel(id) { if (await uiConfirm({ title: "删除渠道", message: "删除该通知渠道?", okText: "删除", danger: true })) { try { await api("/api/notifications/" + id, { method: "DELETE" }); toast("渠道已删除", "ok"); refreshChannels(); } catch (e) { toast("删除失败:" + e.message, "err"); } } }

// ─── 监控 ───
async function addMonitor() {
  const url_or_secuid = $("t-url").value.trim();
  const target_kind = (PLATFORM === "xhs" && $("t-kind")) ? $("t-kind").value : "creator";
  if (!url_or_secuid) { toast(target_kind === "keyword" ? "请输入搜索关键词" : "请输入主页链接 / 短链 / id", "err"); return; }
  if (PLATFORM === "xhs" && !$("t-acc").value) {
    if (!ACCOUNTS.length) { toast("请先在「账号」里完成小红书扫码登录", "err"); switchTab("accounts"); return; }
    toast("小红书监控必须选择一个已登录账号", "err"); return;
  }
  const btn = evtBtn();
  $("add-msg").textContent = "解析中…";
  await withBusy(btn, "解析中", async () => {
    try {
      await api("/api/monitors", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url_or_secuid, platform: PLATFORM, target_kind, account_id: $("t-acc").value ? +$("t-acc").value : null, interval_seconds: +$("t-interval").value, download_dir: $("t-dir").value.trim(), video_quality: PLATFORM === "xhs" ? "" : $("t-quality").value }),
      });
      $("t-url").value = ""; $("t-dir").value = ""; $("add-msg").textContent = "已添加 ✓";
      toast("已开始监控", "ok");
    } catch (e) { $("add-msg").textContent = "失败: " + e.message; toast("添加失败:" + e.message, "err"); }
  });
  refreshMonitors();
}
async function editDir(id, cur) {
  const v = await uiPrompt({ title: "下载目录", hint: "留空=用默认目录", value: cur || "",
    placeholder: "例如 D:\\downloads\\抖音" });
  if (v === null) return;
  try { await api("/api/monitors/" + id, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ download_dir: v.trim() }) }); toast("目录已更新", "ok"); refreshMonitors(); }
  catch (e) { toast("失败:" + e.message, "err"); }
}
async function editQuality(id, cur) {
  const v = await uiSelect({
    title: "视频画质", hint: "留空=跟随全局默认",
    options: [
      { value: "", label: "跟随全局默认" },
      { value: "highest", label: "highest(原画)" },
      { value: "1080", label: "1080" }, { value: "720", label: "720" },
      { value: "540", label: "540" }, { value: "lowest", label: "lowest(省流)" },
    ], value: cur || "" });
  if (v === null) return;
  try { await api("/api/monitors/" + id, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ video_quality: v.trim() }) }); toast("画质已更新", "ok"); refreshMonitors(); }
  catch (e) { toast("失败:" + e.message, "err"); }
}
function monRow(t) {
  const label = t.target_kind === "keyword"
    ? `<span class="ic-text">${ic("i-hash")}${esc(t.keyword)}</span>` : esc(t.nickname || (t.sec_uid || "").slice(0, 12));
  const acc = ACCOUNTS.find(a => a.id === t.account_id);
  // 抖音/小红书都显示绑定账号:抖音未登录抓主页易拿到风控过的旧快照,绑号才稳定
  const accTag = acc
    ? `<div class="mut" style="font-size:11px;margin-top:2px">账号:${esc(acc.nickname)}</div>`
    : `<div class="ic-text" style="font-size:11px;margin-top:2px;color:var(--danger)">${ic("i-info")}未绑定账号</div>`;
  const bindBtn = acc
    ? `<button class="ghost sm" onclick="bindAccount(${t.id})">换账号</button>`
    : `<button class="sm" style="background:var(--warn);border-color:transparent;color:#1a1a1a" onclick="bindAccount(${t.id})">绑定账号</button>`;
  return `<tr>
    <td><div class="user-cell">${t.avatar ? `<img class="avatar" src="${t.avatar}" alt="" referrerpolicy="no-referrer">` : ""}<div><span>${label}</span>${accTag}</div></div></td>
    <td class="num">${t.content_count}</td>
    <td class="num">${Math.round(t.interval_seconds / 60)} 分</td>
    <td class="wrap" style="max-width:230px">
      ${t.platform === "xhs" ? "" : `<span class="pill q bare">${QMAP[t.video_quality] || "默认"}</span> `}
      <span class="mut" title="${esc(t.download_dir || "默认目录")}" style="display:inline-block;max-width:170px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;vertical-align:middle">${esc(t.download_dir || "默认")}</span></td>
    <td class="mut">${t.last_scan_at ? new Date(t.last_scan_at + "Z").toLocaleString() : "—"}${t.last_error ? ` <span class="warn-ic" title="${esc(t.last_error)}">${ic("i-info")}</span>` : ""}</td>
    <td><span class="pill ${t.enabled ? "active" : "invalid"}">${t.enabled ? "监控中" : "已暂停"}</span></td>
    <td class="acttd">
      <button class="ghost sm" onclick="runNow(${t.id})">立即抓取</button>
      <button class="ghost sm" onclick="editDir(${t.id}, ${JSON.stringify(t.download_dir || "").replace(/"/g, "&quot;")})">目录</button>
      ${t.platform === "xhs" ? "" : `<button class="ghost sm" onclick="editQuality(${t.id}, ${JSON.stringify(t.video_quality || "").replace(/"/g, "&quot;")})">画质</button>`}
      ${bindBtn}
      ${t.platform === "douyin" ? `<button class="ghost sm" onclick="relayMon(${t.id})" title="${t.relay_to_xhs_account_id ? "下载后自动转发到小红书(已开启)" : "下载后自动转发到小红书"}">转发${t.relay_to_xhs_account_id ? " ✓" : ""}</button>` : ""}
      <button class="ghost sm" onclick="toggleMon(${t.id})">${t.enabled ? "暂停" : "启用"}</button>
      <button class="ghost sm" onclick="delMon(${t.id})">删除</button>
    </td></tr>`;
}
async function bindAccount(id) {
  const pName = PLATFORM === "xhs" ? "小红书" : "抖音";
  if (!ACCOUNTS.length) { toast(`请先在「账号」里完成${pName}登录`, "err"); switchTab("accounts"); return; }
  const v = await uiSelect({
    title: `绑定${pName}账号`,
    hint: "选择用哪个已登录账号的登录态来抓取该目标。",
    options: ACCOUNTS.map(a => ({ value: String(a.id), label: a.nickname + (a.has_creator ? " · 创作号" : "") })),
    value: String(ACCOUNTS[0].id),
  });
  if (v === null) return;
  try { await api("/api/monitors/" + id, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ account_id: +v }) }); toast("已绑定账号,可点「立即抓取」试试", "ok"); refreshMonitors(); }
  catch (e) { toast("绑定失败:" + e.message, "err"); }
}
async function refreshMonitors() {
  const ts = await api("/api/monitors?platform=" + PLATFORM);
  MONITORS = ts; populateContentSrc();
  $("stat-mon").textContent = ts.filter(t => t.enabled).length;
  if ($("tb-mon")) $("tb-mon").textContent = ts.length;
  $("mon-table").innerHTML = ts.map(monRow).join("")
    || empty(7, "暂无监控", "i-target", "在上方粘贴主页链接或 sec_uid,开始监控一个账号的新作品");
}
async function runNow(id) {
  const btn = evtBtn();
  toast("抓取中…正在开浏览器拉取新作品", "info", 7000);
  await withBusy(btn, "抓取中", async () => {
    try {
      const r = await api("/api/monitors/" + id + "/run-now", { method: "POST" });
      if (r.error) toast("抓取未成功:" + r.error, "err", 6000);
      else toast(`抓取完成,新增 ${r.new} 条`, "ok");
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
  $("content-selcount").textContent = "已选 " + n;
  $("content-selbar").style.display = n ? "inline-flex" : "none";
  const ids = [...document.querySelectorAll(CONTENT_CBS)].map(cb => +cb.dataset.id).filter(Boolean);
  const allSel = ids.length > 0 && ids.every(id => selContent.has(id));
  const btn = $("content-selall-btn"); if (btn) btn.textContent = allSel ? "取消全选" : "全选";
  const sa = $("content-selall"); if (sa) sa.checked = allSel;
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
  const n = selComment.size; const c = $("comment-selcount"), b = $("comment-batchbtn");
  c.textContent = "已选 " + n; c.style.display = n ? "inline" : "none"; b.style.display = n ? "inline-flex" : "none";
  const ids = [...document.querySelectorAll(COMMENT_CBS)].map(cb => +cb.dataset.id).filter(Boolean);
  const allSel = ids.length > 0 && ids.every(id => selComment.has(id));
  const btn = $("comment-selall-btn"); if (btn) btn.textContent = allSel ? "取消全选" : "全选";
  const sa = $("comment-selall"); if (sa) sa.checked = allSel;
}
async function commentBatchDelete() {
  if (!selComment.size) return;
  if (!await uiConfirm({ title: "批量删除评论", message: `删除选中的 ${selComment.size} 条评论?`, okText: "删除", danger: true })) return;
  try { const r = await api("/api/comments/batch-delete", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ids: [...selComment] }) }); toast(`已删除 ${r.deleted} 条评论`, "ok"); selComment.clear(); refreshComments(); }
  catch (e) { toast("批量删除失败:" + e.message, "err"); }
}

function srcOf(r) {
  const t = monitorById(r.target_id);
  return t ? `<div style="margin:0 0 8px">${srcChip(monitorName(t))}</div>` : "";
}
function noteCard(r) {
  const typeIc = r.media_type === "images" ? "i-image" : "i-play";
  const typeLabel = r.media_type === "images" ? "图文" : "视频";
  const cover = r.cover_url
    ? `<img class="ncard-cover" src="${r.cover_url}" alt="${esc((r.desc || "笔记").slice(0, 20))}" referrerpolicy="no-referrer" loading="lazy" onclick="openPreview(${r.id})">`
    : `<div class="ncard-cover ph" onclick="openPreview(${r.id})">${ic("i-image")}</div>`;
  return `<div class="ncard">
    ${cover}
    <span class="ncard-type">${ic(typeIc)}${typeLabel}</span>
    <input type="checkbox" class="ncard-sel" data-id="${r.id}" aria-label="选择" onchange="contentToggleOne(${r.id}, this.checked)" ${selContent.has(r.id) ? "checked" : ""}>
    <div class="ncard-body">
      <p class="ncard-title">${esc(r.desc || "(无标题)")}</p>
      ${srcOf(r)}
      <div class="ncard-foot">
        <span>${fmtTime(r.create_time)}</span>
        <span class="like">${ic("i-heart")}${fmtNum(r.like_count)}</span>
      </div>
      <div class="ncard-actions">
        <span class="pill ${r.download_status}" style="flex:1;justify-content:center" title="${esc(r.error || "")}">${r.download_status}${r.error ? " ⓘ" : ""}</span>
        ${r.download_status === "failed" ? `<button class="ghost sm" onclick="retryDl(${r.id})">重试</button>` : ""}
        ${(PLATFORM === "xhs" && r.download_status === "done") ? `<button class="ghost sm" onclick="repostDouyin(${r.id})">发抖音</button>` : ""}
        <button class="ghost sm" onclick="delContent(${r.id})">删除</button>
      </div>
    </div>
  </div>`;
}
async function refreshContents() {
  const rows = await api("/api/contents?limit=60&platform=" + PLATFORM +
    (CONTENT_SRC ? "&target_id=" + CONTENT_SRC : ""));
  CONTENTS = rows;
  $("stat-dl").textContent = rows.filter(r => r.download_status === "done").length;
  const xhs = PLATFORM === "xhs";
  $("content-title").textContent = xhs ? "最新笔记 / 下载状态" : "最新作品 / 下载状态";
  $("content-table-wrap").style.display = xhs ? "none" : "";
  $("content-cards").style.display = xhs ? "" : "none";
  if (xhs) {
    $("content-cards").innerHTML = rows.map(noteCard).join("")
      || `<div class="empty" style="columns:1">${ic("i-image")}<div class="empty-t">暂无笔记</div></div>`;
    pruneSel(selContent, rows.map(r => r.id)); updateContentSelBar();
    return;
  }
  $("content-table").innerHTML = rows.map(r => `<tr>
    <td><input type="checkbox" data-id="${r.id}" onchange="contentToggleOne(${r.id}, this.checked)" ${selContent.has(r.id) ? "checked" : ""}></td>
    <td>${r.cover_url ? `<img class="thumb" src="${r.cover_url}" alt="封面" referrerpolicy="no-referrer" onclick="openPreview(${r.id})">` : ""}</td>
    <td class="wrap" style="max-width:260px">${esc(r.desc || "(无描述)").slice(0, 50)}${(() => { const t = monitorById(r.target_id); return t ? `<div style="margin-top:4px">${srcChip(monitorName(t))}</div>` : ""; })()}</td>
    <td>${r.media_type === "images" ? "图集" : "视频"}${r.quality ? ` <span class="mut">${esc(r.quality)}</span>` : ""}</td>
    <td class="mut num">${fmtTime(r.create_time)}</td>
    <td class="num"><span class="metric like">${ic("i-heart")}${fmtNum(r.like_count)}</span>${r.duration ? `<span class="metric">${ic("i-clock")}${fmtDur(r.duration)}</span>` : ""}</td>
    <td class="acttd">
      <span class="pill ${r.download_status}">${r.download_status}</span>${r.error ? ` <span class="warn-ic" title="${esc(r.error)}">${ic("i-info")}</span>` : ""}
      ${r.download_status === "failed" ? ` <button class="ghost sm" onclick="retryDl(${r.id})">重试</button>` : ""}
      ${(PLATFORM === "douyin" && r.download_status === "done") ? ` <button class="ghost sm" onclick="repostXhs(${r.id})">发小红书</button>` : ""}
      ${(PLATFORM === "xhs" && r.download_status === "done") ? ` <button class="ghost sm" onclick="repostDouyin(${r.id})">发抖音</button>` : ""}
      <button class="ghost sm" onclick="delContent(${r.id})">删除</button>
    </td>
    <td class="mut num" style="max-width:220px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${esc(r.local_path || "")}">${esc(r.local_path || "")}</td>
  </tr>`).join("") || empty(8, "暂无作品", "i-film", "监控目标有新作品时会自动抓取并下载,显示在这里");
  pruneSel(selContent, rows.map(r => r.id)); updateContentSelBar();
}
async function retryDl(id) {
  const btn = event.target.closest("button"); btn.disabled = true; btn.textContent = "重试中…";
  try { await api("/api/contents/" + id + "/retry-download", { method: "POST" }); toast("已重新加入下载队列", "ok"); }
  catch (e) { toast("重试失败:" + e.message, "err"); }
  setTimeout(() => refreshContents(), 1200);
}
async function delContent(id) {
  if (!await uiConfirm({ title: "删除作品", message: "删除这条作品记录及其已下载的本地文件?", okText: "删除", danger: true })) return;
  try { const r = await api("/api/contents/" + id + "?with_file=true", { method: "DELETE" }); toast(`已删除(清理 ${r.files_removed} 个文件)`, "ok"); refreshContents(); }
  catch (e) { toast("删除失败:" + e.message, "err"); }
}

// ─── 评论监控(独立) ───
const SRC = { public: "公开", creator: "创作中心" };
async function addWatch() {
  const url_or_id = $("w-url").value.trim();
  if (!url_or_id) { toast("请粘贴视频链接 / 账号主页 / sec_uid", "err"); return; }
  if (PLATFORM === "xhs" && !$("w-acc").value) {
    if (!ACCOUNTS.length) { toast("请先在「账号」里完成小红书扫码登录", "err"); switchTab("accounts"); return; }
    toast("小红书评论监控必须选择一个已登录账号", "err"); return;
  }
  const btn = evtBtn();
  $("w-msg").textContent = "解析中…";
  await withBusy(btn, "解析中", async () => {
    try {
      await api("/api/comment-watches", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url_or_id, platform: PLATFORM, kind: $("w-kind").value, mode: PLATFORM === "xhs" ? "public" : $("w-mode").value, account_id: $("w-acc").value ? +$("w-acc").value : null, interval_seconds: +$("w-interval").value }),
      });
      $("w-url").value = ""; $("w-msg").textContent = "已添加 ✓"; toast("已开始监控评论", "ok");
    } catch (e) { $("w-msg").textContent = "失败: " + e.message; toast("添加失败:" + e.message, "err"); }
  });
  refreshWatches();
}
async function refreshWatches() {
  const ws = await api("/api/comment-watches?platform=" + PLATFORM);
  WATCHES = ws; populateCommentSrc();
  if ($("tb-watch")) $("tb-watch").textContent = ws.length;
  $("watch-table").innerHTML = ws.map(w => `<tr>
    <td><div class="user-cell">${w.avatar ? `<img class="avatar" src="${w.avatar}" referrerpolicy="no-referrer">` : ""}<span>${esc(w.title || w.aweme_id || (w.sec_uid || "").slice(0, 12))}</span></div></td>
    <td>${w.kind === "video" ? (w.platform === "xhs" ? "笔记" : "视频") : (w.platform === "xhs" ? "创作者" : "账号")}</td>
    <td>${w.platform === "xhs" ? "公开" : (SRC[w.mode] || w.mode)}</td>
    <td class="num">${w.comment_count}</td>
    <td class="num">${Math.round(w.interval_seconds / 60)} 分</td>
    <td class="mut">${w.last_scan_at ? new Date(w.last_scan_at + "Z").toLocaleString() : "—"}${w.last_error ? ` <span class="warn-ic" title="${esc(w.last_error)}">${ic("i-info")}</span>` : ""}</td>
    <td><span class="pill ${w.enabled ? "active" : "invalid"}">${w.enabled ? "监控中" : "已暂停"}</span></td>
    <td class="acttd">
      <button class="ghost sm" onclick="scanWatch(${w.id})">立即抓取</button>
      <button class="ghost sm" onclick="toggleWatch(${w.id}, ${!w.enabled})">${w.enabled ? "暂停" : "启用"}</button>
      <button class="ghost sm" onclick="delWatch(${w.id})">删除</button>
    </td></tr>`).join("") || empty(8, "暂无评论监控", "i-msg", "粘贴一条视频/笔记链接盯单条,或粘贴主页盯创作者近期作品的评论");
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
async function delWatch(id) { if (await uiConfirm({ title: "删除评论监控", message: "删除该评论监控及其抓到的评论?", okText: "删除", danger: true })) { try { await api("/api/comment-watches/" + id, { method: "DELETE" }); toast("已删除", "ok"); refreshWatches(); refreshComments(); } catch (e) { toast("删除失败:" + e.message, "err"); } } }

async function refreshComments() {
  const rows = await api("/api/comments?limit=80&platform=" + PLATFORM +
    (COMMENT_SRC ? "&watch_id=" + COMMENT_SRC : ""));
  $("stat-cmt").textContent = rows.length;
  $("comment-table").innerHTML = rows.map(r => {
    const w = watchById(r.watch_id);
    const src = w ? `<div style="margin-top:4px">${srcChip(watchName(w))}</div>` : "";
    return `<tr>
    <td><input type="checkbox" data-id="${r.id}" onchange="commentToggleOne(${r.id}, this.checked)" ${selComment.has(r.id) ? "checked" : ""}></td>
    <td class="wrap" style="max-width:360px">${r.is_reply ? '<span class="mut">↳</span> ' : ""}${esc(r.text || "").slice(0, 60)}${src}</td>
    <td class="mut">${esc(r.user_nickname || "")}</td>
    <td class="mut num">${fmtNum(r.like_count)}</td>
    <td class="mut num">${fmtTime(r.create_time)}</td>
    <td class="acttd"><button class="ghost sm" onclick="delComment(${r.id})">删除</button></td>
  </tr>`;
  }).join("") || empty(6, "暂无评论", "i-msg", "添加评论监控后,抓到的新评论会显示在这里,并可推送通知");
  pruneSel(selComment, rows.map(r => r.id)); updateCommentSelBar();
}
async function delComment(id) {
  try { await api("/api/comments/" + id, { method: "DELETE" }); refreshComments(); }
  catch (e) { toast("删除失败:" + e.message, "err"); }
}
async function clearComments() {
  if (!await uiConfirm({ title: "清空评论", message: "清空所有评论记录?", okText: "清空", danger: true })) return;
  try { const r = await api("/api/comments", { method: "DELETE" }); toast(`已清空 ${r.deleted} 条评论`, "ok"); refreshComments(); }
  catch (e) { toast("清空失败:" + e.message, "err"); }
}

// ─── 预览 lightbox(图集左右翻动)───
let PV_N = 0, PV_I = 0;
function _pvRender(d) {
  const box = $("pv-media"), cap = $("pv-cap");
  const vid = (d.medias || []).find(m => m.kind === "video");
  if (d.media_type === "video" && vid) {
    box.innerHTML = `<video src="${vid.url}" controls autoplay playsinline preload="metadata" poster="${esc(d.cover_url || "")}" referrerpolicy="no-referrer"></video>`;
  } else {
    const imgs = (d.medias || []).filter(m => m.kind === "image");
    const list = imgs.length ? imgs : (d.cover_url ? [{ url: d.cover_url }] : []);
    if (!list.length) {
      box.innerHTML = `<div class="pv-loading">暂无可预览的媒体</div>`;
    } else {
      PV_N = list.length; PV_I = 0;
      const slides = list.map(m => `<div class="pv-slide"><img src="${m.url}" referrerpolicy="no-referrer" alt=""></div>`).join("");
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
  PV_N = 0; PV_I = 0;
  box.innerHTML = `<div class="pv-loading">加载中…</div>`; $("pv-cap").textContent = "";
  ov.style.display = "flex";
  try {
    _pvRender(await fetcher());
    if (startIdx && PV_N > 1) { PV_I = Math.max(0, Math.min(startIdx, PV_N - 1)); pvUpdate(); }
  }
  catch (e) { box.innerHTML = `<div class="pv-loading">预览失败:${esc(e.message)}</div>`; }
}
function openPreview(id, startIdx) {
  return _pvOpen(() => api("/api/contents/" + id + "/media"), startIdx || 0);
}
function openPubPreview(accId, noteId, tok, src) {
  return _pvOpen(() => api(`/api/publish/note-media?account_id=${accId}&note_id=${encodeURIComponent(noteId)}&xsec_token=${encodeURIComponent(tok || "")}&xsec_source=${encodeURIComponent(src || "")}`));
}
async function openPubComments(accId, noteId, tok, src) {
  const ov = $("preview"), box = $("pv-media"), cap = $("pv-cap");
  PV_N = 0; PV_I = 0;
  box.innerHTML = `<div class="pv-loading">加载评论…</div>`; cap.textContent = ""; ov.style.display = "flex";
  try {
    const d = await api(`/api/publish/note-comments?account_id=${accId}&note_id=${encodeURIComponent(noteId)}&xsec_token=${encodeURIComponent(tok || "")}&xsec_source=${encodeURIComponent(src || "")}`);
    cap.textContent = `共 ${d.total} 条评论` + (d.has_more ? "(仅首页)" : "");
    box.innerHTML = `<div class="cmt-wrap">` + ((d.comments || []).map(c => `
      <div class="cmt-item">
        <div class="cmt-head"><b>${esc(c.user_nickname || "用户")}</b><span class="like">${ic("i-heart")}${fmtNum(c.like_count)}</span></div>
        <div class="cmt-text">${c.is_reply ? '<span class="mut">↳ </span>' : ""}${esc(c.text || "")}</div>
        <div class="cmt-time">${fmtTime(c.create_time)}</div>
      </div>`).join("") || `<div class="pv-loading">暂无评论</div>`) + `</div>`;
  } catch (e) { box.innerHTML = `<div class="pv-loading">加载失败:${esc(e.message)}</div>`; }
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
  const v = $("pv-media").querySelector("video"); if (v) { try { v.pause(); } catch (e) {} }
  $("preview").style.display = "none"; $("pv-media").innerHTML = ""; $("pv-cap").textContent = "";
  PV_N = 0; PV_I = 0;
}
document.addEventListener("keydown", e => {
  if (e.key === "Escape" && $("repost") && $("repost").style.display !== "none") { hideRepost(); return; }
  if ($("preview").style.display === "none") return;
  if (e.key === "Escape") hidePreview();
  else if (e.key === "ArrowLeft") pvNav(-1);
  else if (e.key === "ArrowRight") pvNav(1);
});

// ─── 发布到小红书 ───
function populatePubAcc() {
  const sel = $("pub-acc"); if (!sel) return;
  // 小红书发布需创作者号;抖音 / 快手发布有登录态即可(走浏览器自动化)
  const list = PLATFORM === "xhs" ? ACCOUNTS.filter(a => a.has_creator) : ACCOUNTS;
  const ph = list.length ? "选择发布账号"
    : (PLATFORM === "kuaishou" ? "请先完成「快手扫码/创作者登录」"
      : PLATFORM === "douyin" ? "请先完成「抖音扫码/创作者登录」" : "请先完成「小红书创作者登录」");
  sel.innerHTML = accOptions(list, ph);
  if (list.length) sel.value = String(list[0].id);
}
let pubFilesDT = new DataTransfer();
function onPubType() {
  const v = $("pub-type").value, inp = $("pub-files"), lbl = $("pub-files-label");
  if (!inp) return;
  if (v === "video") { inp.accept = "video/*"; inp.multiple = false; lbl.textContent = "选择视频文件(单个)"; }
  else { inp.accept = "image/*"; inp.multiple = true; lbl.textContent = "选择图片(可多选,最多 18 张)"; }
  pubFilesClear();
}
function pubFilesClear() { pubFilesDT = new DataTransfer(); _pubSync(); }
function _pubSync() { const inp = $("pub-files"); if (inp) inp.files = pubFilesDT.files; renderPubFiles(); }
function pubAddFiles(files) {
  const isVideo = $("pub-type").value === "video";
  for (const f of files) {
    if (isVideo) { pubFilesDT = new DataTransfer(); pubFilesDT.items.add(f); break; }
    if ([...pubFilesDT.files].some(x => x.name === f.name && x.size === f.size)) continue;
    if (pubFilesDT.files.length >= 18) break;
    pubFilesDT.items.add(f);
  }
  _pubSync();
}
function pubRemoveFile(i) {
  const dt = new DataTransfer();
  [...pubFilesDT.files].forEach((f, idx) => { if (idx !== i) dt.items.add(f); });
  pubFilesDT = dt; _pubSync();
}
function renderPubFiles() {
  const box = $("pub-filelist"); if (!box) return;
  box.innerHTML = [...pubFilesDT.files].map((f, i) => {
    const thumb = f.type.startsWith("image/")
      ? `<img src="${URL.createObjectURL(f)}" alt="">`
      : `<span class="fp-ph">${ic("i-play")}</span>`;
    return `<span class="fp-chip">${thumb}<span title="${esc(f.name)}">${esc(f.name)}</span><button type="button" onclick="pubRemoveFile(${i})" aria-label="移除">✕</button></span>`;
  }).join("");
}
function bindPubFilePicker() {
  const inp = $("pub-files"), zone = $("pub-drop");
  if (!inp || !zone) return;
  inp.addEventListener("change", e => { pubAddFiles(e.target.files); });
  ["dragenter", "dragover"].forEach(ev => zone.addEventListener(ev, e => { e.preventDefault(); zone.classList.add("drag"); }));
  ["dragleave", "drop"].forEach(ev => zone.addEventListener(ev, e => { e.preventDefault(); if (ev === "dragleave" && zone.contains(e.relatedTarget)) return; zone.classList.remove("drag"); }));
  zone.addEventListener("drop", e => { if (e.dataTransfer && e.dataTransfer.files.length) pubAddFiles(e.dataTransfer.files); });
}
async function addPublish() {
  const acc = $("pub-acc").value;
  if (!acc) { toast("请选择" + (PF_NAME[PLATFORM] || "发布") + "账号", "err"); return; }
  const files = $("pub-files").files;
  if (!files.length) { toast("请先选择要发布的文件", "err"); return; }
  const btn = evtBtn();
  $("pub-msg").textContent = "上传中…";
  await withBusy(btn, "上传中", async () => {
    try {
      const fd = new FormData(); for (const f of files) fd.append("files", f);
      const ur = await fetch("/api/publish/upload", { method: "POST", body: fd });
      if (!ur.ok) throw new Error("上传失败 " + ur.status);
      const up = await ur.json();
      const paths = (up.files || []).map(f => f.path);
      const when = $("pub-when").value || null;
      await api("/api/publish", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ account_id: +acc, media_type: $("pub-type").value, title: $("pub-title").value.trim(), desc: $("pub-desc").value, topics: $("pub-topics").value.trim(), media_paths: paths, scheduled_at: when,
          visibility: $("pub-visibility") ? $("pub-visibility").value : "public",
          allow_save: $("pub-allowsave") ? $("pub-allowsave").value !== "0" : true }),
      });
      pubFilesClear(); $("pub-title").value = ""; $("pub-desc").value = ""; $("pub-topics").value = ""; $("pub-when").value = ""; dtSyncAll();
      $("pub-msg").textContent = when ? "已加入定时队列 ✓" : "已加入队列,即将发布 ✓";
      toast("已加入发布队列", "ok");
    } catch (e) { $("pub-msg").textContent = "失败: " + e.message; toast("发布失败:" + e.message, "err"); }
  });
  refreshPublish();
}
const PUB_ST = { pending: "排队中", publishing: "发布中", done: "已发布", failed: "失败", canceled: "已取消" };
const PUB_PILL = { pending: "pending", publishing: "downloading", done: "done", failed: "failed", canceled: "invalid" };
async function refreshPublish() {
  if (!$("pub-table")) return;
  const rows = await api("/api/publish?platform=" + (pfHasPublish(PLATFORM) ? PLATFORM : "xhs"));
  if ($("tb-pub")) $("tb-pub").textContent = rows.length;
  $("pub-table").innerHTML = rows.map(t => `<tr>
    <td class="wrap" style="max-width:220px">${esc(t.title || "(无标题)")}</td>
    <td>${t.media_type === "video" ? "视频" : "图文"}</td>
    <td class="num">${t.media_count}</td>
    <td>${t.source_platform ? esc(t.source_platform) + " 转发" : "手动"}</td>
    <td class="mut num">${t.scheduled_at ? new Date(t.scheduled_at + "Z").toLocaleString() : "尽快"}</td>
    <td><span class="pill ${PUB_PILL[t.status] || "pending"}">${PUB_ST[t.status] || t.status}</span>${t.error ? ` <span class="warn-ic" title="${esc(t.error)}">${ic("i-info")}</span>` : ""}${t.result_url ? ` <a href="${esc(t.result_url)}" target="_blank">查看</a>` : ""}</td>
    <td class="acttd">
      ${(t.status !== "done" && t.status !== "publishing") ? `<button class="ghost sm" onclick="runPublish(${t.id})">立即发布</button>` : ""}
      <button class="ghost sm" onclick="delPublish(${t.id})">删除</button>
    </td></tr>`).join("") || empty(7, "暂无发布任务", "i-send",
      PLATFORM === "kuaishou" ? "上传图集/视频加入队列(发布到快手创作平台)"
      : PLATFORM === "douyin" ? "上传图集/视频加入队列(发布到抖音创作平台)"
      : "上传图集/视频加入队列,或在抖音作品上点「发小红书」转发过来");
}
async function runPublish(id) {
  const btn = evtBtn();
  toast("发布中…会弹出浏览器窗口完成发布", "info", 8000);
  await withBusy(btn, "发布中", async () => {
    try { const r = await api("/api/publish/" + id + "/run-now", { method: "POST" }); toast(r.ok ? "发布成功 ✓" : "发布未成功:" + (r.error || ""), r.ok ? "ok" : "err", 6000); }
    catch (e) { toast("发布失败:" + e.message, "err"); }
  });
  refreshPublish();
}
async function delPublish(id) {
  if (!await uiConfirm({ title: "删除发布任务", message: "删除该发布任务?", okText: "删除", danger: true })) return;
  try { await api("/api/publish/" + id, { method: "DELETE" }); toast("已删除", "ok"); refreshPublish(); }
  catch (e) { toast("删除失败:" + e.message, "err"); }
}

let PUB_NOTES = [], PUB_ACC = "", PUB_GOOD = false;
async function loadPublished() {
  const acc = $("pub-acc").value;
  if (!acc) { toast("请先选择小红书账号", "err"); return; }
  PUB_ACC = acc;
  const btn = evtBtn();
  $("published-msg").textContent = "拉取中…(走创作平台,可能需几秒)";
  $("published-grid").innerHTML = "";
  await withBusy(btn, "拉取中", async () => {
    try {
      const d = await api("/api/publish/published?account_id=" + acc);
      PUB_NOTES = d.notes || []; PUB_GOOD = !!d.good_tokens;
      $("published-msg").innerHTML = `共 ${d.total} 条` + (PUB_GOOD ? "" :
        ` · <span style="color:var(--warn)">视频预览/评论需先对该账号做「小红书扫码登录」(读取登录)</span>`);
      $("published-grid").innerHTML = PUB_NOTES.map((n, i) => `<div class="ncard">
        ${n.cover ? `<img class="ncard-cover" src="${n.cover}" referrerpolicy="no-referrer" loading="lazy" alt="" onclick="pubPreview(${i})">` : `<div class="ncard-cover ph" onclick="pubPreview(${i})">${ic("i-image")}</div>`}
        <span class="ncard-type">${ic(n.type === "video" ? "i-play" : "i-image")}${n.type === "video" ? "视频" : "图文"}</span>
        <div class="ncard-body"><p class="ncard-title">${esc(n.title || "(无标题)")}</p>
          <div class="ncard-foot"><span>${n.time ? new Date((n.time + "").length > 10 ? n.time : n.time * 1000).toLocaleDateString() : ""}</span><span class="like">${ic("i-heart")}${fmtNum(n.like)}</span></div>
          <div class="ncard-actions"><button class="ghost sm" onclick="pubComments(${i})">${ic("i-msg")}评论</button></div>
        </div></div>`).join("") || `<div class="mut" style="columns:1">该账号暂无已发布作品</div>`;
    } catch (e) { $("published-msg").textContent = "失败:" + e.message; toast("拉取失败:" + e.message, "err"); }
  });
}
function pubPreview(i) {
  const n = PUB_NOTES[i]; if (!n) return;
  if (n.images && n.images.length) {   // 图文:直接用列表里的全图,无需再请求
    return _pvOpen(async () => ({
      media_type: "images", desc: n.title || "",
      medias: n.images.map((u, idx) => ({ url: u, kind: "image", ext: "jpeg", index: idx })),
    }));
  }
  return openPubPreview(PUB_ACC, n.note_id, n.xsec_token, n.xsec_source);  // 视频走详情接口
}
function pubComments(i) {
  const n = PUB_NOTES[i]; if (!n) return;
  return openPubComments(PUB_ACC, n.note_id, n.xsec_token, n.xsec_source);
}

// ─── 跨平台:抖音作品 → 小红书 ───
async function _pickXhsAccount(withOff) {
  const all = await api("/api/accounts?platform=xhs");
  const accs = all.filter(a => a.has_creator);   // 发布需创作者号
  if (!accs.length) { toast("请先在小红书账号页完成「创作者登录」(发布用)", "err"); return undefined; }
  if (!withOff && accs.length === 1) return accs[0].id;
  const options = (withOff ? [{ value: "-1", label: "🚫 关闭转发" }] : [])
    .concat(accs.map(a => ({ value: String(a.id), label: a.nickname })));
  const v = await uiSelect({
    title: withOff ? "下载后自动转发到…" : "发布到哪个小红书账号",
    options, value: withOff ? "-1" : String(accs[0].id),
  });
  if (v === null) return undefined;
  return +v;
}
let REPOST_ID = null;
let REPOST_TARGET = "xhs";           // 转发目标平台:xhs(抖音→小红书) | douyin(小红书→抖音)
const repostXhs = (id) => openRepost(id, "xhs");
const repostDouyin = (id) => openRepost(id, "douyin");
async function openRepost(id, target) {
  const rec = CONTENTS.find(r => r.id === id);
  // 拉取目标平台可发布账号:小红书需创作号;抖音需任一登录态(走浏览器自动化)
  const all = await api("/api/accounts?platform=" + target);
  const accs = target === "xhs"
    ? all.filter(a => a.has_creator)
    : all.filter(a => a.has_storage || a.has_creator);
  if (!accs.length) {
    toast(target === "xhs" ? "请先在小红书账号页完成「创作者登录」(发布用)"
      : "请先在抖音账号页完成登录(扫码/创作者/Cookie)", "err");
    return;
  }
  REPOST_ID = id; REPOST_TARGET = target;
  const isDy = target === "douyin", cap = isDy ? 30 : 20;
  $("rp-head").textContent = (isDy ? "发抖音" : "发小红书") + " · 编辑后推送";
  $("rp-title-label").textContent = `标题(≤${cap} 字)`;
  $("rp-title").maxLength = cap;
  $("rp-title").placeholder = isDy ? "给作品起个标题" : "给笔记起个标题";
  $("rp-acc").innerHTML = accs.map(a => `<option value="${a.id}">${esc(a.nickname)}</option>`).join("");
  const desc = (rec && rec.desc) || "";
  $("rp-title").value = desc.slice(0, cap);   // 默认用作品描述前若干字当标题
  $("rp-desc").value = desc;
  $("rp-topics").value = "";
  $("rp-when").value = ""; dtSyncAll();
  $("rp-msg").textContent = "";
  $("rp-src").textContent = rec ? `来源:${rec.media_type === "images" ? "图集" : "视频"} · ${esc((rec.desc || "(无描述)").slice(0, 30))}` : "";
  // 抖音发布设置(可见性 / 保存权限)仅目标为抖音时显示
  if ($("rp-dy-opts")) $("rp-dy-opts").style.display = isDy ? "flex" : "none";
  if (isDy) { if ($("rp-visibility")) $("rp-visibility").value = "public"; if ($("rp-allowsave")) $("rp-allowsave").value = "1"; }
  renderRepostThumbs(id);   // 异步拉媒体缩略图,不阻塞弹窗
  $("rp-submit").disabled = false;
  $("repost").style.display = "flex";
  $("rp-title").focus();
}
let RP_MEDIA = [];         // 可编辑图集:[{url, idx}](idx=原始序号,提交时回传)
let RP_MEDIA_LEN = 0;      // 原始图片总数(判断是否被编辑过)
let RP_IS_VIDEO = false;
async function renderRepostThumbs(id) {
  const box = $("rp-thumbs"); if (!box) return;
  RP_MEDIA = []; RP_MEDIA_LEN = 0; RP_IS_VIDEO = false;
  box.style.display = "none"; box.innerHTML = "";
  try {
    const d = await api("/api/contents/" + id + "/media");
    if (REPOST_ID !== id) return;   // 弹窗已切换/关闭
    const vid = (d.medias || []).find(m => m.kind === "video");
    if (d.media_type === "video" && vid) {
      RP_IS_VIDEO = true;
      box.innerHTML = `<div class="rp-th-ph" onclick="openPreview(${id})" title="点击预览视频">${ic("i-play")}</div>`;
      box.style.display = "flex";
      return;
    }
    const imgs = (d.medias || []).filter(m => m.kind === "image").map(m => m.url);
    const all = imgs.length ? imgs : (d.cover_url ? [d.cover_url] : []);
    RP_MEDIA = all.map((u, i) => ({ url: u, idx: i }));
    RP_MEDIA_LEN = RP_MEDIA.length;
    rpDrawThumbs();
  } catch (e) { /* 预览失败不影响转发 */ }
}
function rpDrawThumbs() {
  const box = $("rp-thumbs"); if (!box) return;
  if (!RP_MEDIA.length) { box.style.display = "none"; box.innerHTML = ""; return; }
  const n = RP_MEDIA.length;
  box.innerHTML = RP_MEDIA.map((m, pos) => `
    <div class="rp-th" draggable="true" data-pos="${pos}"
         ondragstart="rpDragStart(${pos},event)" ondragover="rpDragOver(${pos},event)"
         ondragleave="rpDragLeave(event)" ondrop="rpDrop(${pos},event)" ondragend="rpDragEnd()">
      <img src="${esc(m.url)}" referrerpolicy="no-referrer" draggable="false" alt="" title="点击看大图" onclick="openPreview(${REPOST_ID},${m.idx})">
      <span class="rp-th-badge${pos === 0 ? " cover" : ""}">${pos === 0 ? "封面" : pos + 1}</span>
      <button type="button" class="rp-th-x" title="移除这张" onclick="rpImgRemove(${pos})">✕</button>
      <div class="rp-th-mv">
        <button type="button" onclick="rpImgMove(${pos},-1)" ${pos === 0 ? "disabled" : ""} title="前移(移到最前=封面)">‹</button>
        <button type="button" onclick="rpImgMove(${pos},1)" ${pos === n - 1 ? "disabled" : ""} title="后移">›</button>
      </div>
    </div>`).join("") + `<span class="rp-th-more">共 ${n} 张 · 拖拽排序 · 首图为封面</span>`;
  box.style.display = "flex";
}
let RP_DRAG = -1;
function rpDragStart(pos, ev) {
  RP_DRAG = pos;
  try { ev.dataTransfer.effectAllowed = "move"; ev.dataTransfer.setData("text/plain", String(pos)); } catch (e) {}
}
function rpDragOver(pos, ev) {
  ev.preventDefault();
  try { ev.dataTransfer.dropEffect = "move"; } catch (e) {}
  if (RP_DRAG !== -1 && pos !== RP_DRAG && ev.currentTarget) ev.currentTarget.classList.add("dragover");
}
function rpDragLeave(ev) { if (ev.currentTarget) ev.currentTarget.classList.remove("dragover"); }
function rpDrop(pos, ev) {
  ev.preventDefault();
  const from = RP_DRAG; RP_DRAG = -1;
  if (from < 0 || from >= RP_MEDIA.length || from === pos) { rpDrawThumbs(); return; }
  const [item] = RP_MEDIA.splice(from, 1);
  RP_MEDIA.splice(pos, 0, item);   // 拖到目标位置(其余顺延)
  rpDrawThumbs();
}
function rpDragEnd() {
  RP_DRAG = -1;
  document.querySelectorAll("#rp-thumbs .rp-th.dragover").forEach(e => e.classList.remove("dragover"));
}
function rpImgRemove(pos) {
  if (RP_MEDIA.length <= 1) { toast("至少保留一张图片", "err"); return; }
  RP_MEDIA.splice(pos, 1); rpDrawThumbs();
}
function rpImgMove(pos, dir) {
  const j = pos + dir;
  if (j < 0 || j >= RP_MEDIA.length) return;
  [RP_MEDIA[pos], RP_MEDIA[j]] = [RP_MEDIA[j], RP_MEDIA[pos]];
  rpDrawThumbs();
}
// 图片被编辑过(删了 / 调了序)才回传 media_order;未动则 null 用全部原序
function rpMediaOrder() {
  if (RP_IS_VIDEO || !RP_MEDIA.length) return null;
  const order = RP_MEDIA.map(m => m.idx);
  const unchanged = order.length === RP_MEDIA_LEN && order.every((v, i) => v === i);
  return unchanged ? null : order;
}
function hideRepost() { $("repost").style.display = "none"; REPOST_ID = null; }
async function submitRepost() {
  if (REPOST_ID === null) return;
  const accId = +$("rp-acc").value;
  if (!accId) { toast("请选择发布账号", "err"); return; }
  const btn = $("rp-submit"); btn.disabled = true;
  $("rp-msg").textContent = "提交中…";
  const body = {
    account_id: accId,
    title: $("rp-title").value.trim(),
    desc: $("rp-desc").value,
    topics: $("rp-topics").value.trim(),
    scheduled_at: $("rp-when").value || null,
    visibility: $("rp-visibility") ? $("rp-visibility").value : "public",
    allow_save: $("rp-allowsave") ? $("rp-allowsave").value !== "0" : true,
    media_order: rpMediaOrder(),
  };
  const pname = REPOST_TARGET === "douyin" ? "抖音" : "小红书";
  try {
    const r = await api("/api/contents/" + REPOST_ID + "/repost-" + REPOST_TARGET, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    });
    toast((body.scheduled_at ? "已加入定时发布队列" : `已加入${pname}发布队列`) + "(任务 #" + r.task_id + ")", "ok");
    hideRepost();
    if (typeof refreshPublish === "function") refreshPublish();
  } catch (e) { $("rp-msg").textContent = "失败:" + e.message; toast("转发失败:" + e.message, "err"); btn.disabled = false; }
}
async function relayMon(id) {
  const accId = await _pickXhsAccount(true);
  if (accId === undefined) return;
  try { await api("/api/monitors/" + id, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ relay_to_xhs_account_id: accId }) }); toast(accId === -1 ? "已关闭自动转发" : "已设置:下载后自动转发到小红书", "ok"); refreshMonitors(); }
  catch (e) { toast("设置失败:" + e.message, "err"); }
}

// ─── 自动评论 ───
let AC_RULES = [];
const AC_MODE_T = { auto_reply: "自动回复", auto_comment: "自动评论" };
const AC_KIND_T = { self: "自己近期作品", work: "指定作品", creator: "指定博主", keyword: "关键词" };
const AC_TASK_ST = { draft: "草稿待审", pending: "排队中", doing: "发送中", done: "已发送", failed: "失败", canceled: "已取消" };
const AC_TASK_PILL = { draft: "downloading", pending: "pending", doing: "downloading", done: "done", failed: "failed", canceled: "invalid" };
let AC_TASKS = [];

function acKindOptions() {
  if ($("ac-mode").value === "auto_comment") {
    let html = '<option value="creator">指定博主</option>';
    if (PLATFORM === "xhs") html += '<option value="keyword">搜索关键词</option>';
    return html;
  }
  return '<option value="self">自己近期作品</option><option value="work">指定作品</option>';
}
function onAcMode() {
  const k = $("ac-kind"); if (!k) return;
  const prev = k.value;
  k.innerHTML = acKindOptions();
  if ([...k.options].some(o => o.value === prev)) k.value = prev;
  onAcKind();
}
function onAcKind() {
  const mode = $("ac-mode").value, kind = $("ac-kind").value, xhs = PLATFORM === "xhs";
  let show = true, label = "目标", ph = "";
  if (mode === "auto_reply") {
    if (kind === "self") show = false;
    else { label = xhs ? "笔记链接 / id" : "作品链接 / id"; ph = xhs ? "explore 链接 / xhslink / note_id" : "作品链接 / 短链 / 数字 id"; }
  } else {
    if (kind === "keyword") { label = "搜索关键词"; ph = "例如:露营装备 / 口红试色"; }
    else { label = xhs ? "博主主页 / id" : "博主主页 / sec_uid"; ph = xhs ? "主页链接 / xhslink / user_id" : "主页链接 / 短链 / sec_uid"; }
  }
  $("ac-target-wrap").style.display = show ? "" : "none";
  $("ac-target-label").textContent = label; $("ac-target").placeholder = ph;
  $("ac-reply-filter").style.display = mode === "auto_reply" ? "" : "none";
  csSyncAll();
}
function populateAcAccount() {
  const sel = $("ac-acc"); if (!sel) return;
  const xhs = PLATFORM === "xhs";
  sel.innerHTML = accOptions(ACCOUNTS, xhs ? "请选择小红书账号(必选)" : "请选择抖音账号(必选)");
  if (ACCOUNTS.length) sel.value = String(ACCOUNTS[0].id);
  csSyncAll();
}
async function addCommentRule() {
  const acc = $("ac-acc").value;
  if (!acc) { toast("请选择账号", "err"); return; }
  const templates = $("ac-templates").value.split("\n").map(s => s.trim()).filter(Boolean);
  if (!templates.length) { toast("请至少写一条文案模板(AI 失败时回退用)", "err"); return; }
  const body = {
    platform: PLATFORM, mode: $("ac-mode").value, account_id: +acc,
    target_kind: $("ac-kind").value, target: $("ac-target").value.trim(),
    templates, use_ai: $("ac-use-ai").checked, require_review: $("ac-review").checked,
    reply_filter: $("ac-reply-filter").value.trim(), skip_keywords: $("ac-skip").value.trim(),
    daily_cap: +$("ac-cap").value || 0, min_gap_seconds: +$("ac-gap").value || 60,
    max_per_run: +$("ac-max").value || 5, interval_seconds: +$("ac-interval").value || 1800, enabled: false,
  };
  try {
    await api("/api/comment-rules", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    $("ac-templates").value = ""; $("ac-target").value = "";
    $("ac-msg").textContent = "规则已创建(默认关闭),可在下方「试跑」预览文案 ✓";
    toast("规则已创建", "ok"); refreshCommentRules();
  } catch (e) { $("ac-msg").textContent = "失败: " + e.message; toast("创建失败:" + e.message, "err"); }
}

// ─── 编辑规则:独立弹窗(复用 uimodal 壳)───
let EM_PF = "douyin";
function emKindOptions() {
  if ($("em-mode").value === "auto_comment") {
    let h = '<option value="creator">指定博主</option>';
    if (EM_PF === "xhs") h += '<option value="keyword">搜索关键词</option>';
    return h;
  }
  return '<option value="self">自己近期作品</option><option value="work">指定作品</option>';
}
function emOnMode() {
  const k = $("em-kind"); if (!k) return;
  const prev = k.value;
  k.innerHTML = emKindOptions();
  if ([...k.options].some(o => o.value === prev)) k.value = prev;
  emOnKind();
}
function emOnKind() {
  const mode = $("em-mode").value, kind = $("em-kind").value, xhs = EM_PF === "xhs";
  let show = true, label = "目标", ph = "";
  if (mode === "auto_reply") {
    if (kind === "self") show = false;
    else { label = xhs ? "笔记链接 / id" : "作品链接 / id"; ph = xhs ? "explore / xhslink / note_id" : "作品链接 / 短链 / 数字 id"; }
  } else {
    if (kind === "keyword") { label = "搜索关键词"; ph = "例如:露营装备 / 口红试色"; }
    else { label = xhs ? "博主主页 / id" : "博主主页 / sec_uid"; ph = xhs ? "主页 / xhslink / user_id" : "主页 / 短链 / sec_uid"; }
  }
  $("em-target-wrap").style.display = show ? "" : "none";
  $("em-target-label").textContent = label; $("em-target").placeholder = ph;
  $("em-filter-wrap").style.display = mode === "auto_reply" ? "" : "none";
  $("em-reply-filter").style.display = mode === "auto_reply" ? "" : "none";
  csSyncAll();
}
function editRule(id) {
  const r = AC_RULES.find(x => x.id === id); if (!r) return;
  EM_PF = r.platform;
  const accOpts = accOptions(ACCOUNTS, EM_PF === "xhs" ? "请选择小红书账号" : "请选择抖音账号");
  new Promise(res => {
    _uiResolve = res; _uiCancelVal = null;
    _uiGetVal = () => ({
      name: $("em-name").value.trim(), mode: $("em-mode").value,
      target_kind: $("em-kind").value, target: $("em-target").value.trim(),
      account_id: +$("em-acc").value || null,
      templates: $("em-templates").value.split("\n").map(s => s.trim()).filter(Boolean),
      use_ai: $("em-use-ai").checked, require_review: $("em-review").checked,
      reply_filter: $("em-reply-filter").value.trim(), skip_keywords: $("em-skip").value.trim(),
      daily_cap: +$("em-cap").value || 0, min_gap_seconds: +$("em-gap").value || 60,
      max_per_run: +$("em-max").value || 5, interval_seconds: +$("em-interval").value || 1800,
    });
    $("ui-body").innerHTML = `
      <input id="em-name" placeholder="规则名称">
      <div class="row">
        <select id="em-mode" onchange="emOnMode()"><option value="auto_reply">自动回复(回自己作品)</option><option value="auto_comment">自动评论(去别人帖子)</option></select>
        <select id="em-kind" onchange="emOnKind()"></select>
      </div>
      <select id="em-acc">${accOpts}</select>
      <div id="em-target-wrap"><label class="field" id="em-target-label">目标</label><input id="em-target"></div>
      <div><label class="field">文案模板(每行一条;{nick} {kw} {好|不错|赞})</label><textarea id="em-templates" rows="4"></textarea></div>
      <label class="mut" style="display:flex;align-items:center;gap:8px"><input type="checkbox" id="em-use-ai" style="width:auto"> 用大模型生成文案(失败回退模板)</label>
      <label class="mut" style="display:flex;align-items:center;gap:8px"><input type="checkbox" id="em-review" style="width:auto"> 草稿审核(只生成不自动发)</label>
      <div class="row" id="em-filter-wrap"><input id="em-reply-filter" placeholder="仅回复含此关键词的评论"><input id="em-skip" placeholder="跳过含这些词(逗号分隔)"></div>
      <div class="row" style="flex-wrap:wrap;gap:10px">
        <label class="mut" style="display:flex;align-items:center;gap:6px">每日上限 <input type="number" id="em-cap" min="0" style="width:70px"></label>
        <label class="mut" style="display:flex;align-items:center;gap:6px">最小间隔秒 <input type="number" id="em-gap" min="1" style="width:82px"></label>
        <label class="mut" style="display:flex;align-items:center;gap:6px">每轮最多 <input type="number" id="em-max" min="1" style="width:70px"></label>
        <select id="em-interval"><option value="900">每 15 分钟</option><option value="1800">每 30 分钟</option><option value="3600">每小时</option></select>
      </div>`;
    // 回填值
    $("em-name").value = r.name || "";
    $("em-mode").value = r.mode; emOnMode();
    $("em-kind").value = r.target_kind; emOnKind();
    if ($("em-acc").querySelector(`option[value="${r.account_id}"]`)) $("em-acc").value = String(r.account_id);
    $("em-target").value = r.mode === "auto_comment"
      ? (r.target_kind === "keyword" ? r.keyword : r.sec_uid)
      : (r.target_kind === "work" ? r.aweme_id : "");
    $("em-templates").value = (r.templates || []).join("\n");
    $("em-use-ai").checked = !!r.use_ai;
    $("em-review").checked = !!r.require_review;
    $("em-reply-filter").value = r.reply_filter || "";
    $("em-skip").value = r.skip_keywords || "";
    $("em-cap").value = r.daily_cap; $("em-gap").value = r.min_gap_seconds;
    $("em-max").value = r.max_per_run;
    if ([...$("em-interval").options].some(o => o.value === String(r.interval_seconds))) $("em-interval").value = String(r.interval_seconds);
    _uiOpen("编辑规则 #" + id, "改了「目标/关键词」会重新解析;账号需与规则平台一致", { okText: "保存修改" });
    ["em-mode", "em-kind", "em-acc", "em-interval"].forEach(idd => { const el = $(idd); if (el) enhanceSelect(el); });
  }).then(async val => {
    if (!val) return;   // 取消
    if (!val.templates.length) { toast("请至少写一条文案模板", "err"); return; }
    try {
      await api("/api/comment-rules/" + id, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(val) });
      toast("规则已更新 ✓", "ok"); refreshCommentRules();
    } catch (e) { toast("更新失败:" + e.message, "err"); }
  });
}
async function refreshCommentRules() {
  if (!$("ac-rule-table")) return;
  const rows = await api("/api/comment-rules?platform=" + PLATFORM);
  if ($("tb-ac")) $("tb-ac").textContent = rows.length;
  AC_RULES = rows;
  $("ac-rule-table").innerHTML = rows.map(r => {
    const tgt = r.mode === "auto_comment"
      ? (r.target_kind === "keyword" ? "#" + esc(r.keyword) : esc((r.sec_uid || "").slice(0, 14)))
      : (r.target_kind === "work" ? esc(r.aweme_id) : "自己近期作品");
    const acc = (ACCOUNTS.find(a => a.id === r.account_id) || {}).nickname || ("#" + r.account_id);
    const tags = [r.use_ai ? "AI文案" : "", r.require_review ? "草稿审核" : ""].filter(Boolean)
      .map(x => `<span class="pill downloading" style="margin-left:4px;font-size:10px">${x}</span>`).join("");
    return `<tr>
      <td>${esc(r.name)}${tags}</td>
      <td>${AC_MODE_T[r.mode] || r.mode}</td>
      <td class="wrap" style="max-width:160px">${AC_KIND_T[r.target_kind] || r.target_kind}<br><span class="mut">${tgt}</span></td>
      <td>${esc(acc)}</td>
      <td class="mut num">${r.daily_cap}/日 · ${Math.round(r.interval_seconds / 60)}分</td>
      <td class="mut num">${r.last_run_at ? new Date(r.last_run_at + "Z").toLocaleString() : "—"}${r.last_error ? ` <span class="warn-ic" title="${esc(r.last_error)}">${ic("i-info")}</span>` : ""}</td>
      <td><span class="pill ${r.enabled ? "done" : "invalid"}">${r.enabled ? "运行中" : "已停用"}</span></td>
      <td class="acttd">
        <button class="ghost sm" onclick="toggleRule(${r.id}, ${r.enabled ? "false" : "true"})">${r.enabled ? "停用" : "启用"}</button>
        <button class="ghost sm" onclick="editRule(${r.id})">编辑</button>
        <button class="ghost sm" onclick="runRule(${r.id})">试跑</button>
        <button class="ghost sm" onclick="delRule(${r.id})">删除</button>
      </td></tr>`;
  }).join("") || empty(8, "暂无评论规则", "i-msg", "在上方创建一条自动回复或自动评论规则");
}
async function toggleRule(id, en) {
  try { await api("/api/comment-rules/" + id, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ enabled: en }) }); toast(en ? "已启用" : "已停用", "ok"); refreshCommentRules(); }
  catch (e) { toast("操作失败:" + e.message, "err"); }
}
async function runRule(id) {
  const btn = evtBtn();
  toast("试跑中…正在抓取目标评论,可能要十几秒", "info", 8000);
  await withBusy(btn, "试跑中", async () => {
    try {
      const r = await api("/api/comment-rules/" + id + "/run-now", { method: "POST" });
      if (!r.ok) toast("未生成:" + (r.error || ""), "err", 7000);
      else if (r.created > 0) toast(`生成 ${r.created} 条${r.review ? "草稿(待人工通过)" : "任务"}(发现 ${r.candidates} 个目标)`, "ok", 6000);
      else toast(`发现 ${r.candidates} 个目标,生成 0 条` + (r.note ? `:${r.note}` : "(可能都已生成过)"), "info", 9000);
    } catch (e) { toast("试跑失败:" + e.message, "err"); }
  });
  refreshCommentRules(); refreshCommentTasks();
}
async function delRule(id) {
  if (!await uiConfirm({ title: "删除规则", message: "删除该规则及其未发送任务?", okText: "删除", danger: true })) return;
  try { await api("/api/comment-rules/" + id, { method: "DELETE" }); toast("已删除", "ok"); refreshCommentRules(); refreshCommentTasks(); }
  catch (e) { toast("删除失败:" + e.message, "err"); }
}
async function refreshCommentTasks() {
  if (!$("ac-task-table")) return;
  const st = $("ac-task-filter") ? $("ac-task-filter").value : "";
  const rows = await api("/api/comment-tasks?platform=" + PLATFORM + (st ? "&status=" + st : ""));
  AC_TASKS = rows;
  const drafts = rows.filter(t => t.status === "draft");
  if ($("ac-draft-bar")) {
    $("ac-draft-bar").style.display = drafts.length ? "flex" : "none";
    if (drafts.length) $("ac-draft-count").textContent = `有 ${drafts.length} 条草稿待审核——逐条「通过/编辑」,或一键全部通过后由引擎按节流发出`;
  }
  $("ac-task-table").innerHTML = rows.map(t => {
    const isDraft = t.status === "draft", canSend = t.status === "pending" || t.status === "failed";
    return `<tr>
    <td class="wrap" style="max-width:240px">${esc(t.content)}</td>
    <td class="mut">${esc((t.aweme_id || "").slice(0, 16))}</td>
    <td>${t.target_comment_id ? "回复 " + esc(t.target_nick || "") : "顶层评论"}</td>
    <td class="mut num">${t.scheduled_at ? new Date(t.scheduled_at + "Z").toLocaleString() : "尽快"}</td>
    <td class="mut">${t.method === "browser" ? "浏览器" : t.method === "api" ? "API" : "—"}</td>
    <td><span class="pill ${AC_TASK_PILL[t.status] || "pending"}">${AC_TASK_ST[t.status] || t.status}</span>${t.error ? ` <span class="warn-ic" title="${esc(t.error)}">${ic("i-info")}</span>` : ""}</td>
    <td class="acttd">
      ${isDraft ? `<button class="sm" onclick="approveTask(${t.id})">通过</button>` : ""}
      ${(isDraft || canSend) ? `<button class="ghost sm" onclick="editTaskContent(${t.id})">编辑</button>` : ""}
      ${canSend ? `<button class="ghost sm" onclick="runTask(${t.id})">立即发</button>` : ""}
      ${(isDraft || canSend) ? `<button class="ghost sm" onclick="cancelTask(${t.id})">${isDraft ? "弃用" : "取消"}</button>` : ""}
      <button class="ghost sm" onclick="delTask(${t.id})">删除</button>
    </td></tr>`;
  }).join("") || empty(7, "暂无评论任务", "i-msg", "启用规则或点「试跑」后,这里会出现待发评论");
}
async function approveTask(id) {
  try { await api("/api/comment-tasks/" + id + "/approve", { method: "POST" }); toast("已通过,转入待发队列", "ok"); refreshCommentTasks(); }
  catch (e) { toast("操作失败:" + e.message, "err"); }
}
async function approveAllDrafts() {
  const ids = AC_TASKS.filter(t => t.status === "draft").map(t => t.id);
  if (!ids.length) return;
  if (!await uiConfirm({ title: "全部通过草稿", message: `通过 ${ids.length} 条草稿?通过后引擎按节流(每账号每日上限/最小间隔)陆续发出。`, okText: "全部通过" })) return;
  try { const r = await api("/api/comment-tasks/batch-approve", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ids }) }); toast(`已通过 ${r.approved} 条`, "ok"); refreshCommentTasks(); }
  catch (e) { toast("操作失败:" + e.message, "err"); }
}
async function editTaskContent(id) {
  const t = AC_TASKS.find(x => x.id === id); if (!t) return;
  const v = await uiPrompt({ title: "编辑评论文案", hint: "发出前可微调这条评论的内容", value: t.content || "", multiline: true, rows: 3 });
  if (v === null) return;
  const content = v.trim();
  if (!content) { toast("文案不能为空", "err"); return; }
  try { await api("/api/comment-tasks/" + id, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ content }) }); toast("文案已更新", "ok"); refreshCommentTasks(); }
  catch (e) { toast("更新失败:" + e.message, "err"); }
}
async function runTask(id) {
  const btn = evtBtn();
  toast("发送中…正在开浏览器发评论(有头窗口会弹出)", "info", 8000);
  await withBusy(btn, "发送中", async () => {
    try { const r = await api("/api/comment-tasks/" + id + "/run-now", { method: "POST" }); toast(r.ok ? "已发送 ✓" : "未成功:" + (r.error || ""), r.ok ? "ok" : "err", 7000); }
    catch (e) { toast("发送失败:" + e.message, "err"); }
  });
  refreshCommentTasks();
}
async function cancelTask(id) {
  try { await api("/api/comment-tasks/" + id + "/cancel", { method: "POST" }); toast("已取消", "ok"); refreshCommentTasks(); }
  catch (e) { toast("操作失败:" + e.message, "err"); }
}
async function delTask(id) {
  try { await api("/api/comment-tasks/" + id, { method: "DELETE" }); toast("已删除", "ok"); refreshCommentTasks(); }
  catch (e) { toast("删除失败:" + e.message, "err"); }
}

function esc(s) { return (s || "").toString().replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])); }

function loop() {
  if (INFLIGHT > 0) return;   // 有慢操作进行中,别重渲染(保住按钮加载态)
  refreshMonitors(); refreshContents(); refreshWatches(); refreshComments(); refreshOverviewChart(); refreshCommentRules(); refreshCommentTasks(); if (pfHasPublish(PLATFORM)) refreshPublish();
}

// initial skeletons while data loads
$("mon-table").innerHTML = skeleton(7);
$("content-table").innerHTML = skeleton(8);
$("watch-table").innerHTML = skeleton(8);
$("comment-table").innerHTML = skeleton(6);

// restore last-selected section (default: 总览);旧版四个独立页已并入「账号管理」
const VALID_TABS = ["overview", "accounts", "monitors", "comments", "hub", "publish", "autocomment", "notifications", "settings"];
const LEGACY_HUB_TABS = ["myworks", "following", "fans", "dm"];
switchTab((() => {
  try {
    const t = localStorage.getItem("dym-tab");
    if (LEGACY_HUB_TABS.includes(t)) { HUB_TAB = t; return "hub"; }
    return VALID_TABS.includes(t) ? t : "overview";
  } catch (e) { return "overview"; }
})());
switchHubTab(HUB_TAB);   // 恢复上次停留的子标签(我的作品/关注/粉丝/私信)

// restore last-selected platform (default: 抖音)
PLATFORM = (() => { try { const p = localStorage.getItem("dym-pf"); return (p === "xhs" || p === "douyin" || p === "kuaishou") ? p : "douyin"; } catch (e) { return "douyin"; } })();
applyPlatformUI();

onTypeChange(); bindPubFilePicker(); onPubType(); populateWatchAccount(); onAcMode(); loadSettings(); refreshAccounts(); refreshProxies(); refreshChannels(); loop();
enhanceAllSelects();   // 把所有原生 <select> 升级为美化下拉
enhanceAllDateTime();  // 把 datetime-local 升级为自定义日期选择器
setInterval(loop, 8000);
