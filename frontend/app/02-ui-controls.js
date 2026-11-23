// 02-ui-controls.js — 渐进增强下拉框、自定义悬浮提示、日期时间选择器与总览趋势图。
// ─── 自定义下拉:渐进增强原生 <select>(美化展开列表)───
// 弹层挂到 body 以避开卡片 overflow；Tab 时显式回到文档顺序，避免焦点落到 body 末尾。
let _openSelectClose = null;
function focusPopupItem(item, panel) {
  if (!item || !panel) return;
  item.focus({ preventScroll: true });
  // scrollIntoView can also scroll the document while the popup is entering.
  // Scroll only the popup; its anchor and the user's page position stay stable.
  const target = item.getBoundingClientRect(), bounds = panel.getBoundingClientRect();
  if (target.top < bounds.top + 6) panel.scrollTop -= bounds.top + 6 - target.top;
  else if (target.bottom > bounds.bottom - 6) panel.scrollTop += target.bottom - bounds.bottom + 6;
}
function focusAdjacentControl(origin, backwards = false) {
  const nodes = [...document.querySelectorAll(
    'button:not([disabled]),a[href],input:not([disabled]),textarea:not([disabled]),select:not([disabled]),[tabindex]:not([tabindex="-1"])'
  )].filter(node => node.offsetParent !== null && !node.classList.contains("cs-native") && !node.classList.contains("dt-native") && !node.closest(".cs-panel,.dt-panel"));
  const index = nodes.indexOf(origin);
  const next = nodes[index + (backwards ? -1 : 1)];
  if (next) requestAnimationFrame(() => next.focus({ preventScroll: true }));
}
function enhanceSelect(sel) {
  // React owns the assistant and matrix islands' native controls; reparenting them breaks updates.
  if (sel.dataset.cs || sel.closest(".agent-workspace,.wb-react-island")) return;
  sel.dataset.cs = "1";
  const wrap = document.createElement("div");
  wrap.className = "cs" + (sel.className ? " " + sel.className : "");
  const st = sel.getAttribute("style");
  if (st) wrap.setAttribute("style", st);
  sel.parentNode.insertBefore(wrap, sel);
  wrap.appendChild(sel);
  sel.className = "cs-native";
  sel.removeAttribute("style");
  sel.tabIndex = -1;
  sel.setAttribute("aria-hidden", "true");

  const trg = document.createElement("button");
  trg.type = "button";
  trg.className = "cs-trg";
  trg.innerHTML = `<span class="cs-lbl"></span>` +
    `<svg class="cs-arr" aria-hidden="true"><use href="#i-chevron"/></svg>`;
  trg.setAttribute("aria-haspopup", "listbox");
  trg.setAttribute("aria-expanded", "false");
  const labelEl = sel.id ? document.querySelector(`label[for="${sel.id}"]`) : null;
  const selectLabel = sel.getAttribute("aria-label") || (labelEl || {}).textContent || "选择选项";
  if (labelEl) {
    if (!labelEl.id) labelEl.id = `label-${sel.id}`;
    trg.setAttribute("aria-labelledby", labelEl.id);
    labelEl.addEventListener("click", e => { e.preventDefault(); trg.focus(); });
  } else trg.setAttribute("aria-label", selectLabel.trim());
  wrap.appendChild(trg);
  let panel = null, typeBuffer = "", typeTimer = null, anchor = null;

  function sync() {
    const o = sel.options[sel.selectedIndex];
    trg.querySelector(".cs-lbl").textContent = o ? o.textContent : "";
    trg.classList.toggle("ph", !o || o.value === "");
    trg.disabled = !!sel.disabled;
    trg.setAttribute("aria-disabled", sel.disabled ? "true" : "false");
  }
  function close() {
    if (panel) { panel.remove(); panel = null; }
    if (_openSelectClose === close) _openSelectClose = null;
    wrap.classList.remove("open");
    trg.setAttribute("aria-expanded", "false");
    trg.removeAttribute("aria-controls");
    window.removeEventListener("scroll", onScroll, true);
    window.removeEventListener("resize", close);
    document.removeEventListener("mousedown", onDoc, true);
  }
  function onDoc(e) { if (!wrap.contains(e.target) && (!panel || !panel.contains(e.target))) close(); }
  function onScroll(e) {
    if (!panel || panel.contains(e.target)) return;
    const now = trg.getBoundingClientRect();
    if (anchor && (Math.abs(now.top - anchor.top) > 1 || Math.abs(now.left - anchor.left) > 1)) position();
  }
  function position() {
    if (!panel) return;
    const r = trg.getBoundingClientRect(); anchor = r;
    if (r.bottom < 0 || r.top > window.innerHeight) { close(); return; }
    const viewportWidth = document.documentElement.clientWidth;
    panel.style.left = Math.max(6, Math.min(r.left, viewportWidth - r.width - 6)) + "px";
    panel.style.width = Math.min(r.width, viewportWidth - 12) + "px";
    const below = window.innerHeight - r.bottom - 6, above = r.top - 6;
    const placeAbove = below < 280 && above > below;
    panel.style.top = placeAbove ? "auto" : (r.bottom + 5) + "px";
    panel.style.bottom = placeAbove ? (window.innerHeight - r.top + 5) + "px" : "auto";
    panel.style.maxHeight = Math.max(80, Math.min(280, placeAbove ? above : below)) + "px";
    panel.style.maxWidth = Math.max(180, viewportWidth - 12) + "px";
  }
  function choose(i) {
    if (sel.selectedIndex !== i) {
      sel.selectedIndex = i;
      sel.dispatchEvent(new Event("input", { bubbles: true }));
      sel.dispatchEvent(new Event("change", { bubbles: true }));
    }
    sync(); close(); trg.focus({ preventScroll: true });
  }
  function focusTyped(char) {
    clearTimeout(typeTimer);
    typeBuffer += char.toLocaleLowerCase();
    typeTimer = setTimeout(() => { typeBuffer = ""; }, 650);
    if (!panel) open(false);
    requestAnimationFrame(() => {
      if (!panel) return;
      const options = [...panel.querySelectorAll('.cs-opt:not(.dis)')];
      const from = Math.max(0, options.indexOf(document.activeElement) + 1);
      const ordered = options.slice(from).concat(options.slice(0, from));
      const target = ordered.find(option => option.textContent.trim().toLocaleLowerCase().startsWith(typeBuffer));
      if (target) focusPopupItem(target, panel);
    });
  }
  function open(focusSelected = false) {
    if (sel.disabled) return;
    if (_openSelectClose && _openSelectClose !== close) _openSelectClose();
    panel = document.createElement("div");
    panel.className = "cs-panel";
    panel.id = `cs-panel-${sel.id || Math.random().toString(36).slice(2)}`;
    panel.setAttribute("role", "listbox");
    panel.setAttribute("aria-label", selectLabel.trim());
    Array.from(sel.options).forEach((o, i) => {
      const it = document.createElement("div");
      it.className = "cs-opt" + (i === sel.selectedIndex ? " sel" : "") + (o.disabled ? " dis" : "");
      const optionLabel = document.createElement("span");
      optionLabel.className = "cs-opt-label";
      optionLabel.textContent = o.textContent;
      it.appendChild(optionLabel);
      it.setAttribute("role", "option");
      it.setAttribute("aria-selected", i === sel.selectedIndex ? "true" : "false");
      it.id = `${panel.id}-option-${i}`;
      it.tabIndex = o.disabled ? -1 : 0;
      if (!o.disabled) it.addEventListener("click", ev => { ev.preventDefault(); choose(i); });
      if (!o.disabled) it.addEventListener("keydown", ev => {
        const options = [...panel.querySelectorAll('.cs-opt:not(.dis)')];
        const index = options.indexOf(it);
        if (ev.key === "ArrowDown" || ev.key === "ArrowUp") {
          ev.preventDefault();
          focusPopupItem(options[(index + (ev.key === "ArrowDown" ? 1 : -1) + options.length) % options.length], panel);
        } else if (ev.key === "Home" || ev.key === "End") {
          ev.preventDefault(); focusPopupItem(options[ev.key === "Home" ? 0 : options.length - 1], panel);
        } else if (ev.key === "Enter" || ev.key === " ") {
          ev.preventDefault(); choose(i);
        } else if (ev.key === "Escape") {
          ev.preventDefault(); ev.stopPropagation(); close(); trg.focus({ preventScroll: true });
        } else if (ev.key === "Tab") {
          ev.preventDefault(); close(); focusAdjacentControl(trg, ev.shiftKey);
        } else if (ev.key.length === 1 && !ev.altKey && !ev.ctrlKey && !ev.metaKey) {
          focusTyped(ev.key);
        }
      });
      panel.appendChild(it);
    });
    (trg.closest('.wb-sheet,[role="dialog"]') || document.body).appendChild(panel);
    position();
    wrap.classList.add("open");
    _openSelectClose = close;
    trg.setAttribute("aria-expanded", "true");
    trg.setAttribute("aria-controls", panel.id);
    window.addEventListener("scroll", onScroll, true);
    window.addEventListener("resize", close);
    setTimeout(() => document.addEventListener("mousedown", onDoc, true), 0);
    if (focusSelected) setTimeout(() => {
      const target = panel && (panel.querySelector(".cs-opt.sel:not(.dis)") || panel.querySelector(".cs-opt:not(.dis)"));
      if (target) focusPopupItem(target, panel);
    }, 0);
  }
  trg.addEventListener("click", e => { e.preventDefault(); panel ? close() : open(false); });
  trg.addEventListener("keydown", e => {
    if (["ArrowDown", "ArrowUp", "Enter", " "].includes(e.key)) {
      e.preventDefault(); if (!panel) open(true);
    } else if (e.key === "Escape" && panel) {
      e.preventDefault(); close();
    } else if (e.key.length === 1 && !e.altKey && !e.ctrlKey && !e.metaKey) {
      e.preventDefault(); focusTyped(e.key);
    }
  });
  sel.addEventListener("change", sync);
  sel._csSync = sync;
  new MutationObserver(sync).observe(sel, { childList: true, attributes: true, attributeFilter: ["disabled"] });
  sync();
}
function enhanceAllSelects(root) {
  const scope = root || document;
  if (scope.matches && scope.matches("select:not([data-cs])")) enhanceSelect(scope);
  if (scope.querySelectorAll) scope.querySelectorAll("select:not([data-cs])").forEach(enhanceSelect);
}
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
document.addEventListener("focusin", e => {
  const el = e.target.closest && e.target.closest("[title],[data-tip]");
  if (!el) return;
  if (el.hasAttribute("title")) {
    const t = el.getAttribute("title");
    if (t) { el.setAttribute("data-tip", t); if (!el.hasAttribute("aria-label")) el.setAttribute("aria-label", t); }
    el.removeAttribute("title");
  }
  _tipTarget = el; clearTimeout(_tipTimer); _tipTimer = setTimeout(() => _tipShow(el), 120);
});
document.addEventListener("focusout", e => {
  if (_tipTarget === e.target) _tipHide();
});
window.addEventListener("scroll", _tipHide, true);
document.addEventListener("click", _tipHide);

// ─── 自定义日期时间选择器:渐进增强 <input type=datetime-local> ───
const _pad2 = n => String(n).padStart(2, "0");
let _openDateClose = null;
function _dtFmt(d) { return `${d.getFullYear()}-${_pad2(d.getMonth() + 1)}-${_pad2(d.getDate())}T${_pad2(d.getHours())}:${_pad2(d.getMinutes())}`; }
function _dtDisp(d) { return `${d.getFullYear()}-${_pad2(d.getMonth() + 1)}-${_pad2(d.getDate())} ${_pad2(d.getHours())}:${_pad2(d.getMinutes())}`; }
function _dtParse(v) { const m = (v || "").match(/(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/); return m ? new Date(+m[1], +m[2] - 1, +m[3], +m[4], +m[5]) : null; }
function enhanceDateTime(inp) {
  if (inp.dataset.dt || inp.closest(".agent-workspace,.wb-react-island")) return; inp.dataset.dt = "1";
  const wrap = document.createElement("div");
  wrap.className = "dt" + (inp.className ? " " + inp.className : "");
  const st = inp.getAttribute("style"); if (st) wrap.setAttribute("style", st);
  inp.parentNode.insertBefore(wrap, inp); wrap.appendChild(inp);
  inp.className = "dt-native"; inp.removeAttribute("style");
  inp.tabIndex = -1; inp.setAttribute("aria-hidden", "true");
  const labelEl = inp.id ? document.querySelector(`label[for="${inp.id}"]`) : null;
  const ph = inp.getAttribute("aria-label") || (labelEl || {}).textContent || "选择日期时间";
  const trg = document.createElement("button");
  trg.type = "button"; trg.className = "dt-trg";
  trg.innerHTML = `<span class="dt-lbl"></span>` +
    `<svg class="dt-ic" aria-hidden="true"><use href="#i-calendar"/></svg>`;
  trg.setAttribute("aria-haspopup", "dialog"); trg.setAttribute("aria-expanded", "false");
  if (labelEl) {
    if (!labelEl.id) labelEl.id = `label-${inp.id}`;
    trg.setAttribute("aria-labelledby", labelEl.id);
    labelEl.addEventListener("click", e => { e.preventDefault(); trg.focus(); });
  } else trg.setAttribute("aria-label", ph.trim());
  wrap.appendChild(trg);
  let panel = null, anchor = null;
  function sync() { const d = _dtParse(inp.value); trg.querySelector(".dt-lbl").textContent = d ? _dtDisp(d) : ph; trg.classList.toggle("ph", !d); trg.disabled = !!inp.disabled; }
  function close() { if (panel) { panel.remove(); panel = null; } if (_openDateClose === close) _openDateClose = null; wrap.classList.remove("open"); trg.setAttribute("aria-expanded", "false"); trg.removeAttribute("aria-controls"); window.removeEventListener("scroll", onScroll, true); window.removeEventListener("resize", close); document.removeEventListener("mousedown", onDoc, true); }
  function onDoc(e) { if (!wrap.contains(e.target) && (!panel || !panel.contains(e.target))) close(); }
  function onScroll(e) {
    if (!panel || panel.contains(e.target)) return;
    const now = trg.getBoundingClientRect();
    if (anchor && (Math.abs(now.top - anchor.top) > 1 || Math.abs(now.left - anchor.left) > 1)) position();
  }
  function position() {
    if (!panel) return;
    const r = trg.getBoundingClientRect(); anchor = r;
    if (r.bottom < 0 || r.top > window.innerHeight) { close(); return; }
    panel.style.maxHeight = "";
    const viewportWidth = document.documentElement.clientWidth;
    panel.style.maxWidth = (viewportWidth - 12) + "px";
    const popupWidth = Math.min(panel.getBoundingClientRect().width || 280, viewportWidth - 12);
    panel.style.left = Math.max(6, Math.min(r.left, viewportWidth - popupWidth - 6)) + "px";
    const below = window.innerHeight - r.bottom - 6, above = r.top - 6;
    const placeAbove = panel.getBoundingClientRect().height > below && above > below;
    panel.style.top = placeAbove ? "auto" : (r.bottom + 5) + "px";
    panel.style.bottom = placeAbove ? (window.innerHeight - r.top + 5) + "px" : "auto";
    panel.style.maxHeight = Math.max(120, placeAbove ? above : below) + "px";
  }
  function open() {
    if (inp.disabled) return;
    if (_openSelectClose) _openSelectClose();
    if (_openDateClose && _openDateClose !== close) _openDateClose();
    const init = _dtParse(inp.value) || new Date();
    let view = new Date(init.getFullYear(), init.getMonth(), 1);
    let chosen = _dtParse(inp.value);
    let h = init.getHours(), mi = init.getMinutes();
    panel = document.createElement("div"); panel.className = "dt-panel";
    panel.id = `dt-panel-${inp.id || Math.random().toString(36).slice(2)}`;
    panel.setAttribute("role", "dialog"); panel.setAttribute("aria-label", ph.trim());
    const getH = () => { const v = parseInt(panel.querySelector(".dt-h").value, 10); return isNaN(v) ? 0 : Math.max(0, Math.min(23, v)); };
    const getM = () => { const v = parseInt(panel.querySelector(".dt-m").value, 10); return isNaN(v) ? 0 : Math.max(0, Math.min(59, v)); };
    function render() {
      const y = view.getFullYear(), m = view.getMonth();
      const lead = (new Date(y, m, 1).getDay() + 6) % 7;   // 周一为首列
      const days = new Date(y, m + 1, 0).getDate();
      const t = new Date();
      const chosenHere = chosen && chosen.getFullYear() === y && chosen.getMonth() === m;
      let cells = "";
      for (let i = 0; i < lead; i++) cells += `<span class="dt-day off"></span>`;
      for (let d = 1; d <= days; d++) {
        const today = t.getFullYear() === y && t.getMonth() === m && t.getDate() === d;
        const sel = chosen && chosen.getFullYear() === y && chosen.getMonth() === m && chosen.getDate() === d;
        cells += `<button type="button" class="dt-day${today ? " today" : ""}${sel ? " sel" : ""}" data-d="${d}" tabindex="${sel || (!chosenHere && d === 1) ? 0 : -1}" aria-label="${y} 年 ${m + 1} 月 ${d} 日${today ? "，今天" : ""}"${sel ? ' aria-current="date"' : ""}>${d}</button>`;
      }
      panel.innerHTML =
        `<div class="dt-head"><button type="button" class="dt-nav" data-nav="-1" aria-label="上个月">${ic("i-prev")}</button>` +
        `<span class="dt-title">${y} 年 ${m + 1} 月</span>` +
        `<button type="button" class="dt-nav" data-nav="1" aria-label="下个月">${ic("i-next")}</button></div>` +
        `<div class="dt-wk"><span>一</span><span>二</span><span>三</span><span>四</span><span>五</span><span>六</span><span>日</span></div>` +
        `<div class="dt-grid">${cells}</div>` +
        `<div class="dt-time"><span>时间</span><input type="number" class="dt-h" min="0" max="23" value="${_pad2(h)}" aria-label="小时"><b>:</b><input type="number" class="dt-m" min="0" max="59" value="${_pad2(mi)}" aria-label="分钟"></div>` +
        `<div class="dt-foot"><button type="button" class="ghost sm" data-act="clear">清除</button><button type="button" class="ghost sm" data-act="now">现在</button><button type="button" class="sm" data-act="ok">确定</button></div>`;
      panel.querySelectorAll(".dt-nav").forEach(b => b.onclick = () => { h = getH(); mi = getM(); view.setMonth(view.getMonth() + (+b.dataset.nav)); render(); });
      panel.querySelectorAll(".dt-day[data-d]").forEach(c => c.onclick = () => { h = getH(); mi = getM(); chosen = new Date(view.getFullYear(), view.getMonth(), +c.dataset.d, h, mi); render(); });
      if (panel.isConnected) requestAnimationFrame(() => {
        const day = panel && (panel.querySelector(".dt-day.sel") || panel.querySelector(".dt-day[data-d]"));
        if (day) focusPopupItem(day, panel);
      });
    }
    function commit(d) { inp.value = d ? _dtFmt(d) : ""; inp.dispatchEvent(new Event("change", { bubbles: true })); sync(); close(); trg.focus({ preventScroll: true }); }
    render();
    panel.addEventListener("click", e => {
      const a = e.target.closest("[data-act]"); if (!a) return;
      if (a.dataset.act === "clear") commit(null);
      else if (a.dataset.act === "now") commit(new Date());
      else { const base = chosen || new Date(); base.setHours(getH(), getM(), 0, 0); commit(base); }
    });
    (trg.closest('.wb-sheet,[role="dialog"]') || document.body).appendChild(panel);
    position();
    wrap.classList.add("open");
    _openDateClose = close;
    trg.setAttribute("aria-expanded", "true"); trg.setAttribute("aria-controls", panel.id);
    panel.addEventListener("keydown", e => {
      if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); close(); trg.focus({ preventScroll: true }); return; }
      if (["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "Home", "End"].includes(e.key) && e.target.classList.contains("dt-day")) {
        e.preventDefault();
        const days = [...panel.querySelectorAll(".dt-day[data-d]")];
        const index = days.indexOf(e.target);
        const delta = e.key === "ArrowLeft" ? -1 : e.key === "ArrowRight" ? 1 : e.key === "ArrowUp" ? -7 : e.key === "ArrowDown" ? 7 : 0;
        const target = e.key === "Home" ? days[0] : e.key === "End" ? days[days.length - 1] : days[Math.max(0, Math.min(days.length - 1, index + delta))];
        if (target) focusPopupItem(target, panel);
        return;
      }
      if (e.key === "Tab") {
        const focusables = [...panel.querySelectorAll('button:not([disabled]):not([tabindex="-1"]),input:not([disabled])')];
        const first = focusables[0], last = focusables[focusables.length - 1];
        if ((!e.shiftKey && e.target === last) || (e.shiftKey && e.target === first)) {
          e.preventDefault(); close(); focusAdjacentControl(trg, e.shiftKey);
        }
      }
    });
    window.addEventListener("scroll", onScroll, true); window.addEventListener("resize", close);
    setTimeout(() => document.addEventListener("mousedown", onDoc, true), 0);
    setTimeout(() => { const day = panel && (panel.querySelector(".dt-day.sel") || panel.querySelector(".dt-day[data-d]")); if (day) focusPopupItem(day, panel); }, 0);
  }
  trg.addEventListener("click", e => { e.preventDefault(); panel ? close() : open(); });
  inp.addEventListener("change", sync);
  inp._dtSync = sync;
  new MutationObserver(sync).observe(inp, { attributes: true, attributeFilter: ["disabled"] });
  sync();
}
function enhanceAllDateTime(root) {
  const scope = root || document;
  if (scope.matches && scope.matches("input[type=datetime-local]:not([data-dt])")) enhanceDateTime(scope);
  if (scope.querySelectorAll) scope.querySelectorAll("input[type=datetime-local]:not([data-dt])").forEach(enhanceDateTime);
}
function dtSyncAll() { document.querySelectorAll("input[type=datetime-local][data-dt]").forEach(i => i._dtSync && i._dtSync()); }

// ─── 总览迷你图表(近 7 天采集,纯 SVG 分组柱状)───
async function refreshOverviewChart() {
  const isCurrent = beginViewRequest("overview-chart");
  const box = $("overview-chart");
  if (!box) return;
  let d;
  try { d = await api("/api/stats/series?days=7&platform=" + PLATFORM); }
  catch (e) { if (isCurrent()) box.innerHTML = `<div class="chart-empty"><b>趋势暂未加载</b><button class="ghost sm" onclick="refreshOverviewChart()">重新加载</button></div>`; return; }
  if (!isCurrent()) return;
  const days = d.days || [], A = d.contents || [], B = d.comments || [];
  const total = A.reduce((s, n) => s + n, 0) + B.reduce((s, n) => s + n, 0);
  if (!days.length || total === 0) {
    box.innerHTML = `<div class="chart-empty">${ic("i-film")}<b>近 7 天还没有新增采集</b><span>开始采集后，这里会记录作品与评论的变化。</span></div>`;
    return;
  }
  // viewBox 坐标系,响应式缩放
  const W = Math.max(320, Math.round(box.clientWidth) || 720), H = 180, padL = 36, padR = 12, padT = 14, padB = 30;
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
    bars += `<rect class="bar" x="${xb.toFixed(1)}" y="${y(B[i]).toFixed(1)}" width="${bw}" height="${hb.toFixed(1)}" rx="2" fill="var(--chart-secondary)"><title>${md(days[i])} · 评论 ${B[i]}</title></rect>`;
    labels += `<text class="axt" x="${cx.toFixed(1)}" y="${H - 8}" text-anchor="middle">${md(days[i])}</text>`;
  }
  box.innerHTML = `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="近 7 天每日新增作品与评论柱状图">${gl}${axt}${bars}${labels}</svg><details class="wb-chart-data"><summary>查看每日数据</summary><table><caption class="sr-only">每日新增作品与评论</caption><thead><tr><th>日期</th><th>作品</th><th>评论</th></tr></thead><tbody>${days.map((day, i) => `<tr><th>${esc(day)}</th><td>${Number(A[i]) || 0}</td><td>${Number(B[i]) || 0}</td></tr>`).join("")}</tbody></table></details>`;
}
