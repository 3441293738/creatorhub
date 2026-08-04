const $ = (id) => document.getElementById(id);
// 有用户发起的慢操作(开浏览器抓评论/发评论/解析链接等)在进行时,暂停 8 秒轮询刷新,
// 否则定时重渲染会把按钮的「…中」加载态冲掉。
let INFLIGHT = 0;
// 全局忙碌徽章:>350ms 才显示(快速轮询不闪),圆环转圈 + 已等待秒数 + 并发数。
// 拿不到真实进度百分比(浏览器自动化/接口都是不透明操作),用计时给"在进行"的清晰感知。
// 判忙 = 有未完成请求(_apiActive)或有用户慢操作(INFLIGHT);并发数用 INFLIGHT(用户点的操作数)。
let _apiActive = 0, _barTimer = null, _busyStart = 0, _busyTick = null;
function _isBusy() { return _apiActive > 0 || INFLIGHT > 0; }
function _busyShow() {
  const sp = $("busy-spinner");
  if (sp && _isBusy()) { sp.classList.add("on"); sp.setAttribute("aria-hidden", "false"); }
}
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
    const sp = $("busy-spinner"); if (sp) { sp.classList.remove("on"); sp.setAttribute("aria-hidden", "true"); }
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
  el.setAttribute("role", type === "err" ? "alert" : "status");
  const sym = type === "ok" ? "i-check" : type === "err" ? "i-x" : "i-info";
  el.innerHTML = `${ic(sym)}<span>${esc(msg)}</span>` +
    `<button class="toast-close" type="button" aria-label="关闭提示">${ic("i-x")}</button>`;
  box.appendChild(el);
  let timer = null;
  const dismiss = () => {
    if (!el.isConnected || el.classList.contains("hide")) return;
    clearTimeout(timer); el.classList.add("hide"); setTimeout(() => el.remove(), 250);
  };
  el.querySelector(".toast-close").addEventListener("click", dismiss);
  timer = setTimeout(dismiss, ms);
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

// ─── form interaction helpers ───
function setFieldError(el, message = "") {
  if (!el) return false;
  const field = el.closest(".form-field") || el.parentElement;
  let error = field && field.querySelector(".field-error");
  if (message) {
    el.setAttribute("aria-invalid", "true");
    if (!error && field) {
      error = document.createElement("p");
      error.className = "field-error";
      error.setAttribute("role", "alert");
      field.appendChild(error);
    }
    if (error) error.textContent = message;
    return false;
  }
  el.removeAttribute("aria-invalid");
  if (error) error.remove();
  return true;
}
function toggleSecretInput(id, btn) {
  const input = $(id);
  if (!input) return;
  const show = input.type === "password";
  input.type = show ? "text" : "password";
  if (btn) {
    btn.setAttribute("aria-pressed", show ? "true" : "false");
    btn.setAttribute("aria-label", show ? "隐藏 API Key" : "显示 API Key");
  }
  input.focus({ preventScroll: true });
}
function validateAiField(el, required = false) {
  if (!el) return true;
  const value = el.value.trim();
  if (required && !value) return setFieldError(el, el.id === "ai-model" ? "请输入模型名称" : "请输入接口地址");
  if (el.id === "ai-base" && value) {
    try {
      const url = new URL(value);
      if (!/^https?:$/.test(url.protocol)) throw new Error("protocol");
    } catch (e) { return setFieldError(el, "请输入以 http:// 或 https:// 开头的有效地址"); }
  }
  if (el.id === "ai-temp" && value) {
    const n = Number(value);
    if (!Number.isFinite(n) || n < 0 || n > 2) return setFieldError(el, "温度需填写 0–2 之间的数字");
  }
  return setFieldError(el, "");
}
function validateAiSettings(requireConfigured = false) {
  const required = requireConfigured || $("ai-enabled").checked;
  const fields = [$("ai-base"), $("ai-model"), $("ai-temp")];
  const valid = fields.map(el => validateAiField(el, required && (el.id === "ai-base" || el.id === "ai-model"))).every(Boolean);
  if (!valid) {
    const first = fields.find(el => el.getAttribute("aria-invalid") === "true");
    if (first) first.focus({ preventScroll: false });
    $("ai-msg").textContent = "请先修正标红的配置项";
  }
  return valid;
}
function validateNotificationConfig() {
  const el = $("n-config");
  try {
    const value = JSON.parse(el.value || "{}");
    if (!value || Array.isArray(value) || typeof value !== "object") throw new Error("object");
    return setFieldError(el, "");
  } catch (e) {
    return setFieldError(el, "请输入合法的 JSON 对象，例如 {\"token\":\"...\"}");
  }
}

// ─── dialog focus / scroll management ───
const _modalTriggers = new WeakMap();
function _visibleModal() {
  return [...document.querySelectorAll(".pv-overlay[role='dialog']")].reverse()
    .find(el => getComputedStyle(el).display !== "none");
}
function modalOpened(el) {
  if (!el) return;
  const active = document.activeElement;
  if (active && active !== document.body) _modalTriggers.set(el, active);
  document.body.classList.add("modal-open");
}
function modalClosed(el) {
  if (!el) return;
  if (!_visibleModal()) document.body.classList.remove("modal-open");
  const trigger = _modalTriggers.get(el);
  _modalTriggers.delete(el);
  if (trigger && trigger.isConnected && typeof trigger.focus === "function") {
    setTimeout(() => trigger.focus({ preventScroll: true }), 0);
  }
}
function _modalFocusables(el) {
  return [...el.querySelectorAll(
    'button:not([disabled]),a[href],input:not([disabled]),textarea:not([disabled]),select:not([disabled]),[tabindex]:not([tabindex="-1"])'
  )].filter(node => node.offsetParent !== null);
}

// ─── 通用模态(替代原生 prompt / confirm:下拉 / 文本输入 / 确认)───
let _uiResolve = null, _uiGetVal = null, _uiCancelVal = null;
function _uiClose(val) {
  if (typeof OPEN_META_COMBO !== "undefined" && OPEN_META_COMBO) OPEN_META_COMBO.close();
  const modal = $("uimodal");
  modal.style.display = "none";
  modalClosed(modal);
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
function _uiOpen(title, hint, { okText = "确定", danger = false, wide = false } = {}) {
  $("ui-title").textContent = title || "";
  $("ui-hint").textContent = hint || "";
  const ok = $("ui-ok");
  ok.innerHTML = `<svg aria-hidden="true"><use href="#${danger ? "i-trash" : "i-check"}"/></svg>` + esc(okText);
  ok.classList.toggle("danger", !!danger);
  ok.style.cssText = "flex:0 0 auto";
  const modal = $("uimodal");
  modal.querySelector(".rp-box").style.width = wide ? "min(94vw,680px)" : "min(94vw,480px)";
  modal.style.display = "flex";
  modalOpened(modal);
  document.addEventListener("keydown", _uiKey);
  setTimeout(() => {
    const el = [...$("ui-body").querySelectorAll("input,textarea,.cs-trg,.dt-trg,select,button")]
      .find(node => node.offsetParent !== null && !node.classList.contains("cs-native") && !node.classList.contains("dt-native"));
    if (el) el.focus();
  }, 30);
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
  sel.tabIndex = -1;
  sel.setAttribute("aria-hidden", "true");

  const trg = document.createElement("button");
  trg.type = "button";
  trg.className = "cs-trg";
  trg.innerHTML = `<span class="cs-lbl"></span>` +
    `<svg class="cs-arr" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m6 9 6 6 6-6"/></svg>`;
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
  let panel = null;

  function sync() {
    const o = sel.options[sel.selectedIndex];
    trg.querySelector(".cs-lbl").textContent = o ? o.textContent : "";
    trg.classList.toggle("ph", !o || o.value === "");
    trg.disabled = !!sel.disabled;
    trg.setAttribute("aria-disabled", sel.disabled ? "true" : "false");
  }
  function close() {
    if (panel) { panel.remove(); panel = null; }
    wrap.classList.remove("open");
    trg.setAttribute("aria-expanded", "false");
    trg.removeAttribute("aria-controls");
    window.removeEventListener("scroll", close, true);
    window.removeEventListener("resize", close);
    document.removeEventListener("mousedown", onDoc, true);
  }
  function onDoc(e) { if (!wrap.contains(e.target) && (!panel || !panel.contains(e.target))) close(); }
  function open(focusSelected = false) {
    if (sel.disabled) return;
    panel = document.createElement("div");
    panel.className = "cs-panel";
    panel.id = `cs-panel-${sel.id || Math.random().toString(36).slice(2)}`;
    panel.setAttribute("role", "listbox");
    panel.setAttribute("aria-label", selectLabel.trim());
    Array.from(sel.options).forEach((o, i) => {
      const it = document.createElement("div");
      it.className = "cs-opt" + (i === sel.selectedIndex ? " sel" : "") + (o.disabled ? " dis" : "");
      it.textContent = o.textContent;
      it.setAttribute("role", "option");
      it.setAttribute("aria-selected", i === sel.selectedIndex ? "true" : "false");
      it.tabIndex = o.disabled ? -1 : 0;
      if (!o.disabled) it.addEventListener("mousedown", ev => {
        ev.preventDefault();
        if (sel.selectedIndex !== i) { sel.selectedIndex = i; sel.dispatchEvent(new Event("change", { bubbles: true })); }
        sync(); close(); trg.focus();
      });
      if (!o.disabled) it.addEventListener("keydown", ev => {
        const options = [...panel.querySelectorAll('.cs-opt:not(.dis)')];
        const index = options.indexOf(it);
        if (ev.key === "ArrowDown" || ev.key === "ArrowUp") {
          ev.preventDefault();
          options[(index + (ev.key === "ArrowDown" ? 1 : -1) + options.length) % options.length].focus();
        } else if (ev.key === "Home" || ev.key === "End") {
          ev.preventDefault(); options[ev.key === "Home" ? 0 : options.length - 1].focus();
        } else if (ev.key === "Enter" || ev.key === " ") {
          ev.preventDefault();
          if (sel.selectedIndex !== i) { sel.selectedIndex = i; sel.dispatchEvent(new Event("change", { bubbles: true })); }
          sync(); close(); trg.focus();
        } else if (ev.key === "Escape" || ev.key === "Tab") {
          ev.preventDefault(); close(); trg.focus();
        }
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
    trg.setAttribute("aria-expanded", "true");
    trg.setAttribute("aria-controls", panel.id);
    window.addEventListener("scroll", close, true);
    window.addEventListener("resize", close);
    setTimeout(() => document.addEventListener("mousedown", onDoc, true), 0);
    if (focusSelected) setTimeout(() => {
      const target = panel && (panel.querySelector(".cs-opt.sel:not(.dis)") || panel.querySelector(".cs-opt:not(.dis)"));
      if (target) target.focus();
    }, 0);
  }
  trg.addEventListener("click", e => { e.preventDefault(); panel ? close() : open(false); });
  trg.addEventListener("keydown", e => {
    if (["ArrowDown", "ArrowUp", "Enter", " "].includes(e.key)) {
      e.preventDefault(); if (!panel) open(true);
    } else if (e.key === "Escape" && panel) {
      e.preventDefault(); close();
    }
  });
  sel.addEventListener("change", sync);
  sel._csSync = sync;
  new MutationObserver(sync).observe(sel, { childList: true, attributes: true, attributeFilter: ["disabled"] });
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
  inp.tabIndex = -1; inp.setAttribute("aria-hidden", "true");
  const labelEl = inp.id ? document.querySelector(`label[for="${inp.id}"]`) : null;
  const ph = inp.getAttribute("aria-label") || (labelEl || {}).textContent || "选择日期时间";
  const trg = document.createElement("button");
  trg.type = "button"; trg.className = "dt-trg";
  trg.innerHTML = `<span class="dt-lbl"></span>` +
    `<svg class="dt-ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="18" rx="2"/><path d="M16 2v4M8 2v4M3 10h18"/></svg>`;
  trg.setAttribute("aria-haspopup", "dialog"); trg.setAttribute("aria-expanded", "false");
  if (labelEl) {
    if (!labelEl.id) labelEl.id = `label-${inp.id}`;
    trg.setAttribute("aria-labelledby", labelEl.id);
    labelEl.addEventListener("click", e => { e.preventDefault(); trg.focus(); });
  } else trg.setAttribute("aria-label", ph.trim());
  wrap.appendChild(trg);
  let panel = null;
  function sync() { const d = _dtParse(inp.value); trg.querySelector(".dt-lbl").textContent = d ? _dtDisp(d) : ph; trg.classList.toggle("ph", !d); trg.disabled = !!inp.disabled; }
  function close() { if (panel) { panel.remove(); panel = null; } wrap.classList.remove("open"); trg.setAttribute("aria-expanded", "false"); trg.removeAttribute("aria-controls"); window.removeEventListener("scroll", close, true); window.removeEventListener("resize", close); document.removeEventListener("mousedown", onDoc, true); }
  function onDoc(e) { if (!wrap.contains(e.target) && (!panel || !panel.contains(e.target))) close(); }
  function open() {
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
      let cells = "";
      for (let i = 0; i < lead; i++) cells += `<span class="dt-day off"></span>`;
      for (let d = 1; d <= days; d++) {
        const today = t.getFullYear() === y && t.getMonth() === m && t.getDate() === d;
        const sel = chosen && chosen.getFullYear() === y && chosen.getMonth() === m && chosen.getDate() === d;
        cells += `<button type="button" class="dt-day${today ? " today" : ""}${sel ? " sel" : ""}" data-d="${d}" aria-label="${y} 年 ${m + 1} 月 ${d} 日${today ? "，今天" : ""}"${sel ? ' aria-current="date"' : ""}>${d}</button>`;
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
        if (day) day.focus();
      });
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
    trg.setAttribute("aria-expanded", "true"); trg.setAttribute("aria-controls", panel.id);
    panel.addEventListener("keydown", e => { if (e.key === "Escape") { e.preventDefault(); close(); trg.focus(); } });
    window.addEventListener("scroll", close, true); window.addEventListener("resize", close);
    setTimeout(() => document.addEventListener("mousedown", onDoc, true), 0);
    setTimeout(() => { const day = panel && (panel.querySelector(".dt-day.sel") || panel.querySelector(".dt-day[data-d]")); if (day) day.focus(); }, 0);
  }
  trg.addEventListener("click", e => { e.preventDefault(); panel ? close() : open(); });
  inp.addEventListener("change", sync);
  inp._dtSync = sync;
  new MutationObserver(sync).observe(inp, { attributes: true, attributeFilter: ["disabled"] });
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
const PF_NAME = { douyin: "抖音", xhs: "小红书", kuaishou: "快手", shipinhao: "视频号" };
let CURRENT_TAB = "overview";
const PAGE_META = {
  overview: {
    title: "总览", desc: "集中查看账号状态、采集规模与近 7 天数据变化。"
  },
  accounts: {
    title: "账号与网络", desc: "管理登录状态、账号资料与独立代理绑定。"
  },
  monitors: {
    title: "作品监控", desc: "添加采集目标，管理下载策略并追踪作品状态。"
  },
  comments: {
    title: "评论监控", desc: "订阅作品或账号评论，按来源、分组和标签筛选。"
  },
  danmaku: {
    title: "弹幕监控", desc: "监控短视频播放器内的弹幕，保留每条弹幕在视频中的时间点。"
  },
  hub: {
    title: "本账号管理", desc: "同步自己的作品、关系、私信与账号数据。"
  },
  publish: {
    title: "内容发布", desc: "准备素材与文案，创建立即或定时发布任务。"
  },
  autocomment: {
    title: "自动评论", desc: "配置评论与回复规则，并审核待发布文案。"
  },
  "share-download": {
    title: "链接下载", desc: "从分享文案识别链接，检查媒体信息并下载到本地。"
  },
  notifications: {
    title: "通知渠道", desc: "配置 Bark、钉钉或 Telegram，及时接收任务提醒。"
  },
  settings: {
    title: "系统设置", desc: "调整下载偏好与 AI 文案服务配置。"
  },
};
function updatePageContext(name = CURRENT_TAB) {
  const meta = PAGE_META[name] || PAGE_META.overview;
  if ($("page-title")) $("page-title").textContent = meta.title;
  if ($("page-desc")) $("page-desc").textContent = meta.desc;
  if ($("page-platform")) $("page-platform").textContent = PF_NAME[PLATFORM] || "当前平台";
  if ($("page-kicker")) $("page-kicker").textContent = pfIsChannels(PLATFORM) ? "本账号工作台" : "多平台工作台";
  document.title = `${meta.title} · ${PF_NAME[PLATFORM] || ""} | CreatorHub`;
}
// 是否支持「发布」面板(四平台均有)
function pfHasPublish(pf) { return pf === "xhs" || pf === "kuaishou" || pf === "douyin" || pf === "shipinhao"; }
// 视频号只有「本账号」数据(助手接口本账号),不支持监控他人作品/评论
function pfIsChannels(pf) { return pf === "shipinhao"; }
function switchPlatform(pf) {
  if (!["douyin", "xhs", "kuaishou", "shipinhao"].includes(pf)) pf = "douyin";
  PLATFORM = pf;
  CONTENT_SRC = CONTENT_GROUP = CONTENT_TAG = "";
  COMMENT_SRC = COMMENT_GROUP = COMMENT_TAG = "";
  DANMAKU_SRC = "";
  DANMAKU_PAGE = 1;
  if (OPEN_META_COMBO) OPEN_META_COMBO.close();
  ["t-group", "t-tags", "w-group", "w-tags", "d-w-group", "d-w-tags"].forEach(id => setMetaValue(id, ""));
  ["mon-search", "watch-search", "danmaku-query", "danmaku-time-start", "danmaku-time-end"].forEach(id => { if ($(id)) $(id).value = ""; });
  ["mon-group", "mon-tag", "content-group", "content-tag", "content-src",
    "watch-group", "watch-tag", "comment-group", "comment-tag", "comment-src",
    "danmaku-watch-group", "danmaku-watch-tag", "danmaku-src"].forEach(id => {
    const select = $(id);
    if (select) { select.value = ""; if (select._csSync) select._csSync(); }
  });
  try { localStorage.setItem("dym-pf", pf); } catch (e) {}
  applyPlatformUI();
  // 切换后立刻刷新该平台数据
  refreshAccounts(); refreshMonitors(); refreshContents(); refreshWatches(); refreshComments(); refreshDanmakuWatches(); refreshDanmaku();
  populateAcAccount(); onAcMode(); refreshCommentRules(); refreshCommentTasks();
  if (pfHasPublish(PLATFORM)) refreshPublish();
}
function applyPlatformUI() {
  document.body.classList.toggle("pf-douyin", PLATFORM === "douyin");
  document.body.classList.toggle("pf-xhs", PLATFORM === "xhs");
  document.body.classList.toggle("pf-kuaishou", PLATFORM === "kuaishou");
  document.body.classList.toggle("pf-shipinhao", PLATFORM === "shipinhao");
  // 视频号:只有本账号数据,隐藏「监控他人作品/评论」相关入口(.notsh-only)
  document.body.classList.toggle("pf-channels", pfIsChannels(PLATFORM));
  if (PLATFORM !== "douyin" && CURRENT_TAB === "danmaku") switchTab("overview");
  document.querySelectorAll(".pswitch button").forEach(b => {
    const active = b.dataset.pf === PLATFORM;
    b.classList.toggle("active", active);
    b.setAttribute("aria-selected", active ? "true" : "false");
    b.tabIndex = active ? 0 : -1;
  });
  document.querySelectorAll(".dy-only").forEach(e => e.classList.toggle("hidden", PLATFORM !== "douyin"));
  document.querySelectorAll(".xhs-only").forEach(e => e.classList.toggle("hidden", PLATFORM !== "xhs"));
  document.querySelectorAll(".ks-only").forEach(e => e.classList.toggle("hidden", PLATFORM !== "kuaishou"));
  document.querySelectorAll(".sh-only").forEach(e => e.classList.toggle("hidden", PLATFORM !== "shipinhao"));
  document.querySelectorAll(".notsh-only").forEach(e => e.classList.toggle("hidden", pfIsChannels(PLATFORM)));
  document.querySelectorAll(".meta-scope").forEach(e => {
    e.textContent = (PF_NAME[PLATFORM] || "当前平台") + "内独立";
  });
  // 发布面板入口:抖音 / 小红书 / 快手均显示
  document.querySelectorAll(".pub-only").forEach(e => e.classList.toggle("hidden", !pfHasPublish(PLATFORM)));
  // 发布面板文案随平台切换
  const ks = PLATFORM === "kuaishou", dy = PLATFORM === "douyin", sph = PLATFORM === "shipinhao";
  const pubSub = $("pub-head-sub");
  if (pubSub) pubSub.textContent = dy ? "上传图集 / 视频到抖音创作平台(实验性)"
    : ks ? "上传图集 / 视频到快手创作平台(实验性)"
    : sph ? "上传视频到视频号助手(实验性)" : "上传图集 / 视频到小红书(实验性)";
  if ($("pub-head-lead")) $("pub-head-lead").textContent = (ks || dy || sph) ? "发布作品" : "发布笔记";
  if ($("pub-title")) $("pub-title").placeholder = (ks || dy || sph) ? "给作品起个标题" : "给笔记起个标题";
  const pubHintText = dy
    ? "发布通过自动化抖音创作平台完成。首次登录或触发验证时，请在弹出窗口中完成短信验证或扫码；视频上传后还需等待转码。注意：定时发布可能因本人验证而暂停，建议发布时在场。"
    : ks
    ? "发布通过自动化快手创作平台完成。若遇验证码或需要补充封面，请在弹出窗口中手动处理；定时任务由后台引擎按计划执行。"
    : sph
    ? "发布通过自动化视频号助手完成。视频需等待转码，发布前可能要求补充封面、实名或人脸验证，请在弹出窗口中处理。注意：平台页面改版后可能需要重新适配。"
    : "发布通过自动化小红书创作平台完成。若遇验证码或需要补充封面，请在弹出窗口中手动处理；定时任务由后台引擎按计划执行。";
  const pubHint = $("pub-hint");
  if (pubHint) {
    const copy = pubHint.querySelector("span");
    if (copy) copy.textContent = pubHintText;
    else pubHint.textContent = pubHintText;
  }
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
  if (PLATFORM === "douyin") applyDanmakuForm();
  if ($("t-kind") && PLATFORM !== "xhs") $("t-kind").value = "creator";
  // 视频号只有本账号数据,不支持「监控他人」:若正停在这些面板,自动切到「账号管理」
  if (pfIsChannels(PLATFORM)) {
    const cur = (document.querySelector('.navitem.active') || {}).dataset;
    if (cur && ["monitors", "comments", "autocomment"].includes(cur.tab)) switchTab("hub");
    // 视频号本账号只有「我的作品 / 数据」;若停在关注/粉丝/私信子页,切回我的作品
    if (["following", "fans", "dm"].includes(HUB_TAB)) switchHubTab("myworks");
  }
  // 不支持发布的平台:若正停在该面板则回到总览(当前四平台均支持,兜底保留)
  if (!pfHasPublish(PLATFORM)) {
    const pub = document.querySelector('[data-panel="publish"]');
    if (pub && pub.style.display !== "none") switchTab("overview");
  }
  csSyncAll();   // 平台切换可能改了下拉选项/值,同步自定义下拉显示
  updatePageContext();
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
function switchTab(name, pushHistory = false) {
  if (!PAGE_META[name]) name = "overview";
  const changed = CURRENT_TAB !== name;
  CURRENT_TAB = name;
  document.querySelectorAll("[data-panel]").forEach(p => { p.style.display = p.dataset.panel === name ? "" : "none"; });
  document.querySelectorAll(".navitem").forEach(t => {
    const active = t.dataset.tab === name;
    t.classList.toggle("active", active);
    if (active) t.setAttribute("aria-current", "page");
    else t.removeAttribute("aria-current");
  });
  try { localStorage.setItem("dym-tab", name); } catch (e) {}
  try {
    if (pushHistory && changed) history.pushState(null, "", "#" + name);
    else if (location.hash !== "#" + name) history.replaceState(null, "", "#" + name);
  } catch (e) {}
  updatePageContext(name);
  window.scrollTo({ top: 0, behavior: "auto" });
  if (changed) requestAnimationFrame(() => {
    const title = $("page-title");
    if (title) title.focus({ preventScroll: true });
  });
  if (name === "hub") { refreshHubSummary(); refreshHubPanel(); }
  else stopDmStream();   // 离开本账号管理即断开私信实时流
  if (name === "share-download") {
    loadShareAccounts();
    refreshShareHistory();
  }
}

// ─── 扫码登录(真实浏览器窗口) ───
let qrTimer = null;
// 登录前选代理:返回 "" (不用) | "auto" | 具体url | null(取消)
async function choosePreLoginProxy() {
  let opts = [];
  try { opts = await api("/api/proxies/options"); } catch (e) { }
  const options = [
    { value: "auto", label: opts.length ? "自动分配（占用最少）" : "自动分配（池为空时不用代理）" },
    ...opts.map(p => ({ value: p.url, label: `${p.label} · ${p.status} · 占用${p.used_by} · ${p.masked}${p.enabled ? "" : " · 已停用"}` })),
    { value: "__custom__", label: "✎ 手动输入指定代理…" },
    { value: "", label: "不用代理（使用本机网络）" },
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
    $("qrstatus").innerHTML = `${ic("i-eye")} <b>浏览器窗口已打开</b>，请在该窗口点击「登录」并使用抖音 App 扫码。<br>完成后这里会自动刷新。`;
    pollLogin(res.task_id);
  } catch (e) { $("qrstatus").textContent = "启动失败: " + e.message; toast("登录启动失败:" + e.message, "err"); }
}
function pollLogin(tid) {
  clearInterval(qrTimer);
  clearTimeout(qrTimer);
  let accountShown = false;
  const tick = async () => {
    try {
      const res = await api("/api/login/browser/poll?task_id=" + tid);
      if (res.status === "persisted") {
        $("qrstatus").textContent = "扫码已确认，正在校验登录态并同步账号资料…";
        if (!accountShown) {
          accountShown = true;
          toast("扫码已确认，正在校验登录态", "info");
          refreshAccounts();
        }
      } else if (res.status === "confirmed") {
        clearTimeout(qrTimer);
        if (res.profile_status === "invalid") {
          $("qrstatus").textContent = "登录校验未通过，请重新扫码";
          toast((PF_NAME[PLATFORM] || "账号") + "登录校验未通过，请重新扫码", "err");
        } else {
          const suffix = res.profile_status === "error" ? "（资料稍后同步）" : "";
          $("qrstatus").textContent = "登录成功 ✓ " + (res.nickname || "") + suffix;
          toast("登录成功 " + (res.nickname || "") + suffix, res.profile_status === "error" ? "info" : "ok");
          setTimeout(() => { $("qrbox").style.display = "none"; }, 650);
        }
        refreshAccounts();
        return;
      } else if (res.status === "expired") {
        clearTimeout(qrTimer); $("qrstatus").textContent = "超时未登录,请重试"; toast("二维码超时,请重试", "err");
        return;
      } else if (res.status === "error") {
        clearTimeout(qrTimer); $("qrstatus").textContent = "出错: " + (res.error || ""); toast("登录出错:" + (res.error || ""), "err");
        return;
      }
      qrTimer = setTimeout(tick, 600);
    } catch (e) { clearTimeout(qrTimer); $("qrstatus").textContent = e.message; }
  };
  tick();
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
    $("qrstatus").innerHTML = `${ic("i-eye")} <b>创作中心窗口已打开</b>，请在该窗口扫码登录抖音账号。<br>此登录态也可用于公开抓取。`;
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
    $("qrstatus").innerHTML = `${ic("i-eye")} <b>小红书窗口已打开</b>，扫码后会自动进入创作平台：<br>· 只需查看、评论或预览：完成主站登录即可；<br>· 还需发布：请在创作平台完成登录或授权。<br>处理完成前请保持窗口开启，本页会自动刷新。`;
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
    $("qrstatus").innerHTML = `${ic("i-eye")} <b>小红书创作平台窗口已打开</b>，请扫码登录，此登录态用于发布。<br>登录成功后请稍等片刻再关闭窗口。`;
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
    $("qrstatus").innerHTML = `${ic("i-eye")} <b>快手窗口已打开</b>，请在该窗口点击「登录」并使用快手 App 扫码。<br>完成后这里会自动刷新。`;
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
    $("qrstatus").innerHTML = `${ic("i-eye")} <b>快手创作平台窗口已打开</b>，请扫码登录，此登录态用于发布。<br>登录成功后请稍等片刻再关闭窗口。`;
    pollLogin(res.task_id);
  } catch (e) { $("qrstatus").textContent = "启动失败: " + e.message; toast("创作者登录启动失败:" + e.message, "err"); }
}

// ─── 视频号扫码登录(读取/发布共用,微信扫码) ───
async function startChannelsLogin() {
  const proxy = await choosePreLoginProxy();
  if (proxy === null) return;
  $("cookiebox").style.display = "none";
  $("qrbox").style.display = "block";
  $("qrstatus").textContent = "正在打开视频号助手窗口…";
  try {
    const res = await api(loginStartUrl("/api/login/shipinhao/start", proxy), { method: "POST" });
    $("qrstatus").innerHTML = `${ic("i-eye")} <b>视频号助手窗口已打开</b>，请使用微信扫码登录，读取和发布共用此登录态。<br>登录成功后请稍等片刻再关闭窗口。`;
    pollLogin(res.task_id);
  } catch (e) { $("qrstatus").textContent = "启动失败: " + e.message; toast("视频号登录启动失败:" + e.message, "err"); }
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
let MONITORS = [], WATCHES = [], CONTENTS = [];
let DANMAKU_WATCHES = [];
let CHANNELS = [], PUBLISH_TASKS = [];
let CONTENT_SRC = "", CONTENT_GROUP = "", CONTENT_TAG = "";
let COMMENT_SRC = "", COMMENT_GROUP = "", COMMENT_TAG = "";
let DANMAKU_SRC = "";
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
  sel.innerHTML = `<option value="">全部来源</option>` +
    MONITORS.map(t => `<option value="${t.id}">${esc(monitorName(t))}</option>`).join("");
  if (!MONITORS.some(t => String(t.id) === CONTENT_SRC)) CONTENT_SRC = "";
  sel.value = CONTENT_SRC;
  if (sel._csSync) sel._csSync();
}
function populateCommentSrc() {
  const sel = $("comment-src"); if (!sel) return;
  sel.innerHTML = `<option value="">全部来源</option>` +
    WATCHES.map(w => `<option value="${w.id}">${esc(watchName(w))}</option>`).join("");
  if (!WATCHES.some(w => String(w.id) === COMMENT_SRC)) COMMENT_SRC = "";
  sel.value = COMMENT_SRC;
  if (sel._csSync) sel._csSync();
}
function onContentSrc() { CONTENT_SRC = $("content-src").value; selContent.clear(); refreshContents(); }
function onCommentSrc() { COMMENT_SRC = $("comment-src").value; selComment.clear(); refreshComments(); }
function onContentMetaFilter() {
  CONTENT_GROUP = $("content-group").value; CONTENT_TAG = $("content-tag").value;
  selContent.clear(); refreshContents();
}
function onCommentMetaFilter() {
  COMMENT_GROUP = $("comment-group").value; COMMENT_TAG = $("comment-tag").value;
  selComment.clear(); refreshComments();
}
function matchesMeta(item, groupName, tag) {
  return (!groupName || item.group_name === groupName) && (!tag || itemTags(item).includes(tag));
}
function onMonitorFilter() { renderMonitorRows(); }
function onWatchFilter() { renderWatchRows(); }
async function refreshAccounts() {
  const accs = await api("/api/accounts?platform=" + PLATFORM);
  ACCOUNTS = accs;
  $("stat-acc").textContent = accs.length;
  $("acc-table").querySelector("tbody").innerHTML = accs.map(a => {
    const isXhs = a.platform === "xhs";
    const isKs = a.platform === "kuaishou";
    const isChannels = a.platform === "shipinhao";
    const idName = isXhs ? "小红书号 " : isKs ? "快手号 " : isChannels ? "视频号 " : "抖音号 ";
    const secName = isChannels ? "finder_id " : (isXhs || isKs) ? "user_id " : "sec_uid ";
    const idline = [
      a.douyin_id ? idName + esc(a.douyin_id) : null,
      a.sec_uid ? secName + esc(a.sec_uid).slice(0, 16) + "…" : null,
    ].filter(Boolean).join(" · ");
    const detail = [
      a.aweme_count ? a.aweme_count + (isXhs ? " 笔记" : " 作品") : null,
      a.follower_count ? fmtNum(a.follower_count) + " 粉丝" : null,
      isXhs ? "扫码登录" : (a.login_type === "cookie" ? "Cookie 登录" : "扫码登录"),
      a.has_storage
        ? (a.status === "invalid" ? "登录态已保存但校验失效" : "登录态有效")
        : "无登录态",
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
        <button class="ghost sm danger" onclick="delAccount(${a.id})" aria-label="删除账号">${ic("i-trash")}删除</button>
      </td>
    </tr>`;
  }).join("") || empty(3, "还没有账号", "i-user", "用上方按钮扫码登录,或粘贴 Cookie 添加一个账号");
  if ($("tb-acc")) $("tb-acc").textContent = accs.length;
  populateAccountSelect();
  populateWatchAccount();
  if (PLATFORM === "douyin") applyDanmakuForm();
  else populateDanmakuAccount();
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
  const kpi = $("stats-kpi"), tr = $("stats-trend"), wb = $("stats-works");
  if (!kpi) return;
  if (!HUB_ACC) { kpi.innerHTML = ""; if (tr) tr.innerHTML = ""; if (wb) wb.innerHTML = `<tr><td colspan="5" class="mut">请先选择账号</td></tr>`; return; }
  try {
    const d = await api("/api/account-stats/" + HUB_ACC + "?days=30");
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
      ? d.works.map(w => `<tr><td>${esc((w.desc || w.item_id || "").slice(0, 30))}</td>`
        + `<td class="num">${fmtNum(w.play_count || 0)}</td><td class="num">${fmtNum(w.like_count || 0)}</td><td class="num">${fmtNum(w.comment_count || 0)}</td>`
        + `<td><span class="pill bare">${esc(w.status || "—")}</span></td></tr>`).join("")
      : `<tr><td colspan="5" class="mut">暂无作品数据,先到「我的作品」点「同步作品」</td></tr>`;
  } catch (e) {
    kpi.innerHTML = `<div class="mut">加载失败:${esc(e.message)}</div>`;
  }
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
  if (platform === "shipinhao") return "https://channels.weixin.qq.com/platform/post/list";
  return "https://www.douyin.com/video/" + id;
}
function openWork(platform, id) { try { window.open(workLink(platform, id), "_blank", "noopener"); } catch (e) {} }
function workCard(w) {
  const oc = `onclick="openWork('${esc(w.platform)}','${esc(w.item_id).replace(/'/g, "\'")}')"`;
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
        ${w.platform === "douyin" ? '<button class="ghost sm" onclick="monitorOwnWorkDanmaku(\'' + esc(w.item_id) + '\',' + (w.account_id || "null") + ')">' + ic("i-msg") + '弹幕</button>' : ""}
        <button class="ghost sm" onclick="openWorkComments(${w.id},'${esc(w.platform)}','${title.replace(/'/g, "\'")}')">${ic("i-msg")}评论</button>
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
  return `<div class="dm-conv" data-conv="${esc(c.conv_id)}" onclick="openDmConv('${esc(c.conv_id).replace(/'/g, "\'")}')">
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
  const required = PLATFORM === "xhs" || PLATFORM === "douyin";
  const platformName = PLATFORM === "xhs" ? "小红书" : "抖音";
  sel.innerHTML = accOptions(ACCOUNTS, required ? `请选择${platformName}账号(必选)` : "不指定账号");
  // 抖音匿名主页可能返回风控后的旧快照；作品监控与小红书一样必须使用登录态。
  if (required && ACCOUNTS.length) sel.value = String(ACCOUNTS[0].id);
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
function populateDanmakuAccount() {
  const sel = $("d-w-acc"); if (!sel) return;
  const creatorOnly = $("d-w-mode") && $("d-w-mode").value === "creator";
  const list = creatorOnly
    ? ACCOUNTS.filter(a => a.platform === "douyin" && a.has_creator)
    : ACCOUNTS.filter(a => a.platform === "douyin");
  const ph = creatorOnly && !list.length ? "无创作者账号,请先创作者登录" : "不指定账号";
  sel.innerHTML = accOptions(list, ph);
  if (creatorOnly && list.length) sel.value = String(list[0].id);
}
function applyDanmakuForm() {
  const kind = $("d-w-kind") ? $("d-w-kind").value : "auto";
  const mode = $("d-w-mode") ? $("d-w-mode").value : "public";
  const isVideo = kind === "video";
  const recentWrap = $("d-w-recent-wrap");
  const daysWrap = $("d-w-days-wrap");
  if (recentWrap) recentWrap.hidden = isVideo;
  if (daysWrap) daysWrap.hidden = isVideo;
  const urlLabel = $("d-w-url-label");
  if (urlLabel) urlLabel.textContent = isVideo
    ? "视频链接 / aweme_id"
    : kind === "user" ? "账号主页 / sec_uid" : "视频链接 / 账号主页 / aweme_id";
  if ($("d-w-url")) $("d-w-url").placeholder = isVideo
    ? "作品链接或 aweme_id=监控单条视频弹幕"
    : kind === "user" ? "账号主页或 sec_uid=监控账号近期作品"
    : "作品链接=单条视频；主页链接=账号近期作品";
  const accLabel = $("d-w-acc-label");
  if (accLabel) accLabel.textContent = mode === "creator"
    ? "创作中心账号（必选）" : "播放页账号（可选）";
  const depthLabel = $("d-w-depth-label");
  if (depthLabel) depthLabel.textContent = mode === "creator"
    ? "创作中心翻页深度" : "弹幕加载轮次";
  const probeWrap = $("d-w-probe-wrap");
  if (probeWrap) probeWrap.hidden = mode === "creator";
  populateDanmakuAccount();
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
        <button class="ghost sm danger" onclick="delPoolProxy(${p.id},${p.used_by})">${ic("i-trash")}删除</button>
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
  let t = null;
  let persistedShown = false;
  const tick = async () => {
    try {
      const r = await api("/api/login/browser/poll?task_id=" + tid);
      if (r.status === "persisted" && !persistedShown) {
        persistedShown = true;
        toast("扫码已确认，正在校验登录态", "info");
        refreshAccounts();
      } else if (r.status === "confirmed") {
        clearTimeout(t);
        if (r.profile_status === "invalid") {
          toast("登录校验未通过，请重新扫码", "err");
        } else {
          const suffix = r.profile_status === "error" ? "（资料稍后同步）" : "";
          toast("重新登录成功 " + (r.nickname || "") + suffix, r.profile_status === "error" ? "info" : "ok");
        }
        refreshAccounts(); return;
      } else if (r.status === "expired") {
        clearTimeout(t); toast("超时未登录,请重试", "err"); return;
      } else if (r.status === "error") {
        clearTimeout(t); toast("出错:" + (r.error || ""), "err"); return;
      }
      t = setTimeout(tick, 600);
    } catch (e) { clearTimeout(t); }
  };
  tick();
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
  if (!validateAiSettings(false)) return;
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
  if (!validateAiSettings(true)) return;
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

// ─── 通用分享链接下载 ───
let SHARE_LINKS = [], SHARE_LINK_INDEX = 0, SHARE_SOURCE = "", SHARE_ACCOUNTS = [];

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
  const knownAccountPlatform = ["douyin", "xhs", "kuaishou", "shipinhao"].includes(platform);
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

async function refreshShareHistory() {
  const body = $("sd-history-body");
  if (!body) return;
  body.innerHTML = skeleton(7, 3);
  try {
    const rows = await api("/api/share-download/history?limit=100");
    $("sd-history-count").textContent = `${rows.length} 条`;
    body.innerHTML = rows.map(row => {
      const files = Array.isArray(row.files) ? row.files : [];
      const mediaFiles = files.filter(file => file.role === "media");
      const totalSize = files.reduce((sum, file) => sum + Number(file.size || 0), 0);
      const platformName = PF_NAME[row.platform] || (row.platform === "generic" ? "通用站点" : row.platform || "—");
      const typeName = row.media_type === "images" ? "图文" : row.media_type === "video" ? "视频" : (row.media_type || "媒体");
      const status = row.status === "done"
        ? `<span class="pill bare" style="color:var(--ok)">成功</span>`
        : `<span class="pill bare" style="color:var(--danger)" title="${esc(row.error || "")}">失败</span>`;
      const workSub = [row.author, row.item_id ? `ID ${row.item_id}` : ""].filter(Boolean).join(" · ");
      const path = row.output_dir || ((files[0] || {}).path || "");
      return `<tr>
        <td>${esc(fmtShareHistoryTime(row.created_at))}</td>
        <td>${esc(platformName)}</td>
        <td style="white-space:normal;min-width:220px;max-width:420px">
          <div style="font-weight:650">${esc(row.title || (row.status === "failed" ? "下载失败" : "未命名作品"))}</div>
          <div class="mut" style="margin-top:3px">${esc(workSub || row.source_url || "")}</div>
        </td>
        <td>${esc(typeName)} · ${mediaFiles.length || row.media_count || 0} 个媒体<br><span class="mut">${files.length} 个文件${totalSize ? ` · ${fmtShareSize(totalSize)}` : ""}</span></td>
        <td>${status}${row.error ? `<div class="mut" style="max-width:220px;white-space:normal;margin-top:4px">${esc(row.error)}</div>` : ""}</td>
        <td style="white-space:normal;max-width:300px"><code style="overflow-wrap:anywhere">${esc(path || "—")}</code></td>
        <td>
          <div class="row" style="gap:6px;flex-wrap:nowrap">
            ${path ? `<button class="ghost sm" data-path="${esc(path)}" onclick="copySharePath(this)">复制路径</button>` : ""}
            <button class="ghost sm danger" onclick="deleteShareHistory(${Number(row.id)})">${ic("i-trash")}删除记录</button>
          </div>
        </td>
      </tr>`;
    }).join("") || empty(7, "暂无下载历史", "i-download", "开始下载后会自动记录；旧下载会从元数据文件补录");
  } catch (e) {
    $("sd-history-count").textContent = "读取失败";
    body.innerHTML = empty(7, "历史记录读取失败", "i-info", e.message);
  }
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

async function runShareDownload(download) {
  const btn = evtBtn();
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

function inspectShareLink() { return runShareDownload(false); }
function downloadShareLink() { return runShareDownload(true); }

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
  CHANNELS = cs;
  $("n-table").querySelector("tbody").innerHTML = cs.map(c => `<tr>
    <td>${esc(c.name)} <span class="mut">${c.type}</span></td>
    <td><span class="pill ${c.enabled ? "active" : "invalid"}">${c.enabled ? "启用" : "停用"}</span></td>
    <td class="acttd">
      <button class="ghost sm" onclick="editChannel(${c.id})">编辑</button>
      <button class="ghost sm" onclick="testChannel(${c.id})">测试</button>
      <button class="ghost sm" onclick="toggleChannel(${c.id}, ${!c.enabled})">${c.enabled ? "停用" : "启用"}</button>
      <button class="ghost sm danger" onclick="delChannel(${c.id})">${ic("i-trash")}删除</button>
    </td></tr>`).join("") || empty(3, "还没有通知渠道", "i-bell", "添加 Bark / 钉钉 / Telegram 渠道，有新作品或新评论时推送给你");
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
        <textarea id="ec-config" rows="9" spellcheck="false">${esc(initial.raw)}</textarea></div>`;
    _uiOpen("编辑通知渠道", `类型：${c.type} · 修改密钥或地址后建议立即发送测试通知。`, { okText: "保存修改", wide: true });
  });
  if (value === null) return;
  let config;
  try { config = JSON.parse(value.raw || "{}"); }
  catch (e) {
    toast("配置不是合法 JSON，请修正后再保存", "err");
    return editChannel(id, value);
  }
  try {
    await api("/api/notifications/" + id, {
      method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: value.name || c.type, config }),
    });
    toast("通知渠道已更新", "ok"); refreshChannels();
  } catch (e) { toast("更新失败:" + e.message, "err"); }
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
  if ((PLATFORM === "xhs" || PLATFORM === "douyin") && !$("t-acc").value) {
    const platformName = PLATFORM === "xhs" ? "小红书" : "抖音";
    if (!ACCOUNTS.length) { toast(`请先在「账号」里完成${platformName}扫码登录`, "err"); switchTab("accounts"); return; }
    toast(`${platformName}监控必须选择一个已登录账号`, "err"); return;
  }
  const btn = evtBtn();
  const downloadMode = $("t-download").value;
  $("add-msg").textContent = "解析中…";
  await withBusy(btn, "解析中", async () => {
    try {
      await api("/api/monitors", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          url_or_secuid, platform: PLATFORM, target_kind,
          account_id: $("t-acc").value ? +$("t-acc").value : null,
          interval_seconds: +$("t-interval").value,
          initial_backfill_count: PLATFORM === "douyin" ? +$("t-backfill").value : 0,
          download_dir: $("t-dir").value.trim(),
          video_quality: PLATFORM === "xhs" ? "" : $("t-quality").value,
          download_enabled: downloadMode !== "none",
          media_filter: downloadMode === "none" ? "all" : downloadMode,
          alias: $("t-alias").value.trim(), group_name: getMetaValue("t-group").trim(),
          tags: parseTags(getMetaValue("t-tags")),
        }),
      });
      ["t-url", "t-dir", "t-alias"].forEach(id => $(id).value = "");
      setMetaValue("t-group", ""); setMetaValue("t-tags", "");
      $("add-msg").textContent = "已添加 ✓";
      toast("已开始监控", "ok");
    } catch (e) { $("add-msg").textContent = "失败: " + e.message; toast("添加失败:" + e.message, "err"); }
  });
  refreshMonitors();
}
function numericSelectOptions(current, choices, unit = "") {
  const values = choices.map(([value]) => String(value));
  const rows = values.includes(String(current)) || current == null
    ? choices : [[current, `${current}${unit}（当前）`], ...choices];
  return rows.map(([value, label]) =>
    `<option value="${value}">${esc(label)}</option>`).join("");
}
async function editMonitor(id) {
  const item = monitorById(id); if (!item) return;
  const accounts = ACCOUNTS.filter(a => a.platform === item.platform && a.status !== "invalid");
  const accountOptions = [
    `<option value="">${item.account_id ? "保持当前绑定" : "不指定账号"}</option>`,
    ...accounts.map(a => `<option value="${a.id}">${esc(a.nickname)}${a.has_creator ? " · 创作号" : ""}</option>`),
  ].join("");
  const intervalOptions = numericSelectOptions(item.interval_seconds || 300, [
    [60, "每 1 分钟"], [300, "每 5 分钟"], [600, "每 10 分钟"],
    [1800, "每 30 分钟"], [3600, "每小时"], [21600, "每 6 小时"], [86400, "每天"],
  ], " 秒");
  const backfillOptions = numericSelectOptions(item.initial_backfill_count ?? 0, [
    [0, "不回填历史"], [5, "最近 5 条"], [20, "最近 20 条"], [-1, "尽可能全量"],
  ], " 条");
  const value = await new Promise(res => {
    _uiResolve = res; _uiCancelVal = null;
    _uiGetVal = () => {
      const downloadMode = $("em-download").value;
      const result = {
        alias: $("em-alias").value.trim(),
        group_name: getMetaValue("em-group").trim(),
        tags: parseTags(getMetaValue("em-tags")),
        interval_seconds: +$("em-interval").value,
        account_id: $("em-account").value ? +$("em-account").value : null,
        download_dir: $("em-dir").value.trim(),
        video_quality: $("em-quality") ? $("em-quality").value : "",
        download_enabled: downloadMode !== "none",
        media_filter: downloadMode === "none" ? "all" : downloadMode,
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
          <div><label class="field" for="em-account">抓取账号</label><select id="em-account">${accountOptions}</select></div>
        </div>
        ${item.last_scan_at ? "" : `<div><label class="field" for="em-backfill">首次历史回填</label>
          <select id="em-backfill">${backfillOptions}</select></div>`}
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
    $("em-interval").value = String(item.interval_seconds || 300);
    $("em-account").value = item.account_id ? String(item.account_id) : "";
    if ($("em-backfill")) $("em-backfill").value = String(item.initial_backfill_count ?? 0);
    if ($("em-quality")) $("em-quality").value = item.video_quality || "";
    $("em-download").value = item.download_enabled === false ? "none" : (item.media_filter || "all");
    ["em-interval", "em-account", "em-backfill", "em-quality", "em-download"]
      .forEach(key => { const el = $(key); if (el) enhanceSelect(el); });
    _uiOpen("编辑作品监控", "监控对象不可修改；需要更换主页、创作者或关键词时，请新建监控。", { okText: "保存修改", wide: true });
  });
  if (value === null) return;
  try {
    await api("/api/monitors/" + id, {
      method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(value),
    });
    toast("作品监控配置已更新", "ok"); refreshMonitors(); refreshContents();
  } catch (e) { toast("更新失败:" + e.message, "err"); }
}
function monRow(t) {
  const label = t.target_kind === "keyword"
    ? `<span class="ic-text">${ic("i-hash")}${esc(t.keyword)}</span>` : esc(t.nickname || (t.sec_uid || "").slice(0, 12));
  const acc = ACCOUNTS.find(a => a.id === t.account_id);
  // 抖音/小红书都显示绑定账号:抖音未登录抓主页易拿到风控过的旧快照,绑号才稳定
  const accTag = acc
    ? `<div class="mut" style="font-size:11px;margin-top:2px">账号:${esc(acc.nickname)}</div>`
    : `<div class="ic-text" style="font-size:11px;margin-top:2px;color:var(--danger)">${ic("i-info")}未绑定账号</div>`;
  const downloadLabel = t.download_enabled === false ? "仅记录"
    : ({ all: "全部下载", video: "仅视频", images: "仅图集" }[t.media_filter] || "全部下载");
  return `<tr>
    <td><div class="user-cell">${t.avatar ? `<img class="avatar" src="${t.avatar}" alt="" referrerpolicy="no-referrer">` : ""}<div><span>${label}</span>${t.alias ? `<div class="alias-line">${esc(t.alias)}</div>` : ""}${accTag}</div></div></td>
    <td>${metaChips(t)}</td>
    <td class="num">${t.content_count}</td>
    <td class="num">${Math.round(t.interval_seconds / 60)} 分</td>
    <td class="wrap" style="max-width:230px">
      <div style="display:flex;gap:4px;flex-wrap:wrap;margin-bottom:4px"><span class="pill q bare">${downloadLabel}</span></div>
      ${t.platform === "xhs" ? "" : `<span class="pill q bare">${QMAP[t.video_quality] || "默认画质"}</span> `}
      <span class="mut" title="${esc(t.download_dir || "默认目录")}" style="display:inline-block;max-width:170px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;vertical-align:middle">${esc(t.download_dir || "默认")}</span></td>
    <td class="mut">${t.last_scan_at ? new Date(t.last_scan_at + "Z").toLocaleString() : "—"}${t.last_error ? ` <span class="warn-ic" title="${esc(t.last_error)}">${ic("i-info")}</span>` : ""}</td>
    <td><span class="pill ${t.enabled ? "active" : "invalid"}">${t.enabled ? "监控中" : "已暂停"}</span></td>
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
  const query = (($("mon-search") && $("mon-search").value) || "").trim().toLocaleLowerCase();
  const rows = MONITORS.filter(t => {
    if (!matchesMeta(t, groupName, tag)) return false;
    if (!query) return true;
    return [monitorBaseName(t), t.alias, t.group_name, ...itemTags(t)]
      .join(" ").toLocaleLowerCase().includes(query);
  });
  if ($("mon-filter-count")) $("mon-filter-count").textContent = `显示 ${rows.length} / ${MONITORS.length}`;
  $("mon-table").innerHTML = rows.map(monRow).join("")
    || empty(8, "没有匹配的监控", "i-target", MONITORS.length ? "调整分组、标签或搜索条件" : "在上方添加一个作品监控");
}
async function refreshMonitors() {
  const ts = await api("/api/monitors?platform=" + PLATFORM);
  MONITORS = ts; populateMonitorFacets(); populateContentSrc();
  $("stat-mon").textContent = ts.filter(t => t.enabled).length;
  if ($("tb-mon")) $("tb-mon").textContent = ts.length;
  renderMonitorRows();
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
  if (!p) return `<span class="local-path-empty">—</span>`;
  return `<div class="local-path">
    <div class="local-path-info">
      <div class="local-path-file"><span class="local-path-name">${esc(p.name)}</span>${p.ext ? `<span class="local-path-ext">${esc(p.ext)}</span>` : ""}</div>
      <div class="local-path-dir">${esc(p.dir || "当前目录")}</div>
    </div>
    <button type="button" class="ghost local-path-action reveal" onclick="revealContentPath(${r.id},this)" data-tip="在文件夹中显示" aria-label="在文件夹中显示">${ic("i-folder")}</button>
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
  return t ? `<div style="margin:0 0 8px">${sourceMeta(t)}</div>` : "";
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
        ${["failed", "skipped"].includes(r.download_status) ? `<button class="ghost sm" onclick="retryDl(${r.id})">${r.download_status === "skipped" ? "下载" : "重试"}</button>` : ""}
        ${(PLATFORM === "xhs" && r.download_status === "done") ? `<button class="ghost sm" onclick="repostDouyin(${r.id})">发抖音</button>` : ""}
        <button class="ghost sm danger" onclick="delContent(${r.id})">${ic("i-trash")}删除</button>
      </div>
    </div>
  </div>`;
}
async function refreshContents() {
  const params = new URLSearchParams({ limit: "60", platform: PLATFORM });
  if (CONTENT_SRC) params.set("target_id", CONTENT_SRC);
  if (CONTENT_GROUP) params.set("group_name", CONTENT_GROUP);
  if (CONTENT_TAG) params.set("tag", CONTENT_TAG);
  const rows = await api("/api/contents?" + params.toString());
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
  $("content-table").innerHTML = rows.map(r => {
    const monitor = monitorById(r.target_id);
    const description = esc(r.desc || "(无描述)");
    return `<tr>
      <td class="content-check-cell"><input type="checkbox" data-id="${r.id}" onchange="contentToggleOne(${r.id}, this.checked)" ${selContent.has(r.id) ? "checked" : ""}></td>
      <td class="content-cover-cell">${r.cover_url ? `<img class="thumb" src="${r.cover_url}" alt="封面" referrerpolicy="no-referrer" onclick="openPreview(${r.id})">` : `<span class="content-cover-empty">${ic(r.media_type === "images" ? "i-image" : "i-film")}</span>`}</td>
      <td class="content-desc-cell">
        <div class="content-desc-text" title="${description}">${description}</div>
        ${monitor ? `<div class="content-desc-meta">${sourceMeta(monitor)}</div>` : ""}
      </td>
      <td><span class="content-kind">${r.media_type === "images" ? "图集" : "视频"}</span>${r.quality ? `<span class="content-quality">${esc(r.quality)}</span>` : ""}</td>
      <td class="mut num">${contentTimeCell(r.create_time)}</td>
      <td class="content-metrics num"><span class="metric like">${ic("i-heart")}${fmtNum(r.like_count)}</span>${r.duration ? `<span class="metric">${ic("i-clock")}${fmtDur(r.duration)}</span>` : ""}</td>
      <td class="content-action-cell">
        <div class="content-status-row"><span class="pill ${r.download_status}">${contentStatusLabel(r.download_status)}</span>${r.error ? `<span class="warn-ic" data-tip="${esc(r.error)}">${ic("i-info")}</span>` : ""}</div>
        <div class="content-action-buttons">
          ${["failed", "skipped"].includes(r.download_status) ? `<button class="ghost sm" onclick="retryDl(${r.id})">${r.download_status === "skipped" ? "下载" : "重试"}</button>` : ""}
          ${(PLATFORM === "douyin" && r.download_status === "done") ? `<button class="ghost sm content-action-primary" onclick="pickRepostTarget(${r.id})">${ic("i-send")}转发</button>` : ""}
          ${(PLATFORM === "xhs" && r.download_status === "done") ? `<button class="ghost sm content-action-primary" onclick="repostDouyin(${r.id})">${ic("i-send")}发抖音</button>` : ""}
          <button class="ghost sm content-action-delete danger" onclick="delContent(${r.id})" data-tip="删除作品" aria-label="删除作品">${ic("i-trash")}</button>
        </div>
      </td>
      <td class="local-path-cell">${contentPathCell(r)}</td>
    </tr>`;
  }).join("") || empty(8, "暂无作品", "i-film", "监控目标有新作品时会自动抓取并下载,显示在这里");
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

// ─── 短视频弹幕监控(独立) ───
function danmakuWatchBaseName(w) {
  return w.title || w.aweme_id || (w.sec_uid || "").slice(0, 12);
}
function populateDanmakuFacets() {
  setFacetOptions("danmaku-watch-group", "全部分组", DANMAKU_WATCHES.map(x => x.group_name));
  setFacetOptions("danmaku-watch-tag", "全部标签", DANMAKU_WATCHES.flatMap(itemTags));
  const sel = $("danmaku-src"); if (!sel) return;
  const old = DANMAKU_SRC;
  sel.innerHTML = '<option value="">全部来源</option>' +
    DANMAKU_WATCHES.map(x => x.id ? '<option value="' + x.id + '">' +
      esc(danmakuWatchBaseName(x)) + '</option>' : "").join("");
  DANMAKU_SRC = [...sel.options].some(o => o.value === old) ? old : "";
  sel.value = DANMAKU_SRC;
}
function danmakuWatchRow(w) {
  const base = esc(danmakuWatchBaseName(w));
  const source = w.mode === "creator" ? "创作中心" : "公开视频";
  const error = w.last_error
    ? ' <span class="warn-ic" title="' + esc(w.last_error) + '">' + ic("i-info") + "</span>" : "";
  const avatar = w.avatar
    ? '<img class="avatar" src="' + esc(w.avatar) + '" referrerpolicy="no-referrer">' : "";
  const alias = w.alias ? '<div class="alias-line">' + esc(w.alias) + "</div>" : "";
  const interval = w.interval_seconds
    ? Math.round(w.interval_seconds / 60) + " 分"
    : "跟随全局" + (w.effective_interval_seconds ? "（" + Math.round(w.effective_interval_seconds / 60) + " 分）" : "");
  const scope = w.kind === "user"
    ? '<div class="mut" style="font-size:11px;margin-top:2px">' +
      (w.recent_works ? "近 " + w.recent_works + " 个" : "全局 " + (w.effective_recent_works || "") + " 个") +
      " · " + (w.recent_days ? "近 " + w.recent_days + " 天" : "全局 " + (w.effective_recent_days || "") + " 天") +
      "</div>" : "";
  return '<tr>' +
    '<td><div class="user-cell">' + avatar + '<div><span>' + base + "</span>" + alias + scope + "</div></div></td>" +
    "<td>" + (w.kind === "video" ? "单条视频" : "账号作品") + "</td>" +
    "<td>" + source + "</td>" +
    '<td class="num">' + fmtNum(w.danmaku_count || 0) + "</td>" +
    '<td class="num">' + interval + "</td>" +
    '<td class="mut">' + (w.last_scan_at ? new Date(w.last_scan_at + "Z").toLocaleString() : "—") + error + "</td>" +
    '<td><span class="pill ' + (w.enabled ? "active" : "invalid") + '">' +
      (w.enabled ? "监控中" : "已暂停") + "</span></td>" +
    '<td class="acttd">' +
      '<button class="ghost sm" onclick="editDanmakuWatch(' + w.id + ')">编辑</button>' +
      '<button class="ghost sm" onclick="scanDanmakuWatch(' + w.id + ')">立即抓取</button>' +
      '<button class="ghost sm" onclick="toggleDanmakuWatch(' + w.id + ", " + (!w.enabled) + ')">' +
        (w.enabled ? "暂停" : "启用") + "</button>" +
      '<button class="ghost sm danger" onclick="delDanmakuWatch(' + w.id + ')">' +
        ic("i-trash") + "删除</button></td></tr>";
}
function renderDanmakuWatchRows() {
  const group = $("danmaku-watch-group") ? $("danmaku-watch-group").value : "";
  const tag = $("danmaku-watch-tag") ? $("danmaku-watch-tag").value : "";
  const query = (($("danmaku-watch-search") && $("danmaku-watch-search").value) || "").trim().toLocaleLowerCase();
  const rows = DANMAKU_WATCHES.filter(w => {
    if (!matchesMeta(w, group, tag)) return false;
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
          DANMAKU_WATCHES.length ? "调整筛选条件" : "在上方添加一个弹幕监控");
}
async function addDanmakuWatch() {
  const url = $("d-w-url").value.trim();
  if (!url) { toast("请粘贴视频链接 / 账号主页 / aweme_id", "err"); return; }
  const mode = $("d-w-mode").value;
  if (mode === "creator" && !$("d-w-acc").value) {
    toast("创作中心模式需要选择创作者账号", "err"); return;
  }
  const btn = evtBtn();
  $("d-w-msg").textContent = "解析中…";
  await withBusy(btn, "解析中", async () => {
    try {
      await api("/api/danmaku-watches", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          url_or_id: url, platform: "douyin", kind: $("d-w-kind").value, mode: mode,
          account_id: $("d-w-acc").value ? +$("d-w-acc").value : null,
          interval_seconds: +$("d-w-interval").value,
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
      $("d-w-msg").textContent = "已添加 ✓";
      toast("已开始监控弹幕", "ok");
    } catch (e) {
      $("d-w-msg").textContent = "失败: " + e.message;
      toast("添加失败:" + e.message, "err");
    }
  });
  refreshDanmakuWatches();
}
async function refreshDanmakuWatches() {
  if (PLATFORM !== "douyin") return;
  const rows = await api("/api/danmaku-watches?platform=douyin");
  DANMAKU_WATCHES = rows;
  populateDanmakuFacets();
  if ($("tb-danmaku")) $("tb-danmaku").textContent = rows.length;
  renderDanmakuWatchRows();
}
function onDanmakuSrc() {
  DANMAKU_SRC = $("danmaku-src").value;
  DANMAKU_PAGE = 1;
  refreshDanmaku();
}
async function editDanmakuWatch(id) {
  const item = DANMAKU_WATCHES.find(x => x.id === id);
  if (!item) return;
  const intervalOptions = numericSelectOptions(item.interval_seconds || 0, [
    [0, "跟随全局设置"], [60, "每 1 分钟"], [300, "每 5 分钟"],
    [600, "每 10 分钟"], [1800, "每 30 分钟"], [3600, "每小时"], [86400, "每天"],
  ]);
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
      interval_seconds: +$("edw-interval").value,
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
    ["edw-interval", "edw-recent", "edw-days", "edw-depth", "edw-probe"].forEach(key => {
      const el = $(key); if (el) enhanceSelect(el);
    });
    $("edw-interval").value = String(item.interval_seconds || 0);
    $("edw-recent").value = String(item.recent_works || 0);
    $("edw-days").value = String(item.recent_days || 0);
    $("edw-depth").value = String(item.max_scrolls || 0);
    $("edw-probe").value = String(item.probe_step_seconds || 0);
    ["edw-interval", "edw-recent", "edw-days", "edw-depth", "edw-probe"].forEach(key => {
      const el = $(key); if (el && el._csSync) el._csSync();
    });
    _uiOpen("编辑弹幕监控", "监控对象保持不变；可调整视频内时间范围、过滤条件和容量上限。", { okText: "保存修改", wide: true });
  });
  if (value === null) return;
  try {
    await api("/api/danmaku-watches/" + id, {
      method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(value),
    });
    toast("弹幕监控配置已更新", "ok"); refreshDanmakuWatches(); refreshDanmaku();
  } catch (e) { toast("更新失败:" + e.message, "err"); }
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
  if (!await uiConfirm({ title: "删除弹幕监控", message: "删除该监控及其抓到的弹幕?",
                         okText: "删除", danger: true })) return;
  try {
    await api("/api/danmaku-watches/" + id, { method: "DELETE" });
    toast("已删除", "ok"); refreshDanmakuWatches(); refreshDanmaku();
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
  if (PLATFORM !== "douyin" || !$("danmaku-table")) return;
  if (resetPage) DANMAKU_PAGE = 1;
  const params = new URLSearchParams({
    platform: "douyin", page: String(DANMAKU_PAGE),
    page_size: String(DANMAKU_PAGE_SIZE), paginate: "true",
  });
  if (DANMAKU_SRC) params.set("watch_id", DANMAKU_SRC);
  const query = ($("danmaku-query") && $("danmaku-query").value || "").trim();
  const start = +(($('danmaku-time-start') && $('danmaku-time-start').value) || 0);
  const end = +(($('danmaku-time-end') && $('danmaku-time-end').value) || 0);
  if (query) params.set("q", query);
  if (start > 0) params.set("min_video_time_ms", String(Math.round(start * 1000)));
  if (end > 0) params.set("max_video_time_ms", String(Math.round(end * 1000)));
  params.set("sort", ($("danmaku-sort") && $("danmaku-sort").value) || "video_asc");
  const payload = await api("/api/danmaku?" + params.toString());
  const meta = Array.isArray(payload)
    ? { items: payload, total: payload.length, page: 1, page_size: DANMAKU_PAGE_SIZE,
        pages: Math.max(1, Math.ceil(payload.length / DANMAKU_PAGE_SIZE)) }
    : (payload || {});
  const pages = Math.max(1, Number(meta.pages || 1));
  if (DANMAKU_PAGE > pages && Number(meta.total || 0) > 0) {
    DANMAKU_PAGE = pages;
    return refreshDanmaku();
  }
  const rows = Array.isArray(meta.items) ? meta.items : [];
  $("danmaku-table").innerHTML = rows.map(r => '<tr>' +
    '<td class="wrap" style="max-width:360px">' + esc(r.text || "") + "</td>" +
    '<td class="mut" title="' + esc(r.user_id || "") + '">' +
      esc(r.user_nickname || (r.user_id ? "用户 ID " + r.user_id : "用户")) + "</td>" +
    '<td class="num"><code>' + danmakuTime(r.video_time_ms) + "</code></td>" +
    "<td>" + (r.source === "creator" ? "创作中心" : "播放页") + "</td>" +
    '<td class="mut">' + (r.created_at ? danmakuCapturedAt(r.created_at) :
      (r.create_time ? fmtTime(r.create_time) : "—")) + "</td>" +
    '<td class="acttd"><button class="ghost sm danger" onclick="deleteDanmaku(' + r.id + ')">' +
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
  try { await api("/api/danmaku/" + id, { method: "DELETE" }); refreshDanmaku(); }
  catch (e) { toast("删除失败:" + e.message, "err"); }
}
async function clearDanmaku() {
  if (!await uiConfirm({ title: "清空弹幕", message: "清空所有弹幕记录?",
                         okText: "清空", danger: true })) return;
  try {
    const result = await api("/api/danmaku", { method: "DELETE" });
    toast("已清空 " + result.deleted + " 条弹幕", "ok");
    refreshDanmaku(); refreshDanmakuWatches();
  } catch (e) { toast("清空失败:" + e.message, "err"); }
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
        body: JSON.stringify({
          url_or_id, platform: PLATFORM, kind: $("w-kind").value,
          mode: PLATFORM === "xhs" ? "public" : $("w-mode").value,
          account_id: $("w-acc").value ? +$("w-acc").value : null,
          interval_seconds: +$("w-interval").value,
          recent_works: +$("w-recent").value,
          recent_days: +$("w-days").value,
          max_scrolls: +$("w-depth").value,
          alias: $("w-alias").value.trim(), group_name: getMetaValue("w-group").trim(),
          tags: parseTags(getMetaValue("w-tags")),
        }),
      });
      ["w-url", "w-alias"].forEach(id => $(id).value = "");
      setMetaValue("w-group", ""); setMetaValue("w-tags", "");
      $("w-msg").textContent = "已添加 ✓"; toast("已开始监控评论", "ok");
    } catch (e) { $("w-msg").textContent = "失败: " + e.message; toast("添加失败:" + e.message, "err"); }
  });
  refreshWatches();
}
function watchRow(w) {
  const base = esc(watchBaseName(w));
  return `<tr>
    <td><div class="user-cell">${w.avatar ? `<img class="avatar" src="${w.avatar}" referrerpolicy="no-referrer">` : ""}<div><span>${base}</span>${w.alias ? `<div class="alias-line">${esc(w.alias)}</div>` : ""}</div></div></td>
    <td>${metaChips(w)}</td>
    <td>${w.kind === "video" ? (w.platform === "xhs" ? "笔记" : "视频") : (w.platform === "xhs" ? "创作者" : "账号")}</td>
    <td>${w.platform === "xhs" ? "公开" : (SRC[w.mode] || w.mode)}</td>
    <td class="num">${w.comment_count}</td>
    <td class="num">${Math.round(w.interval_seconds / 60)} 分
      ${w.kind === "user" && (w.recent_works || w.recent_days) ? `<div class="mut" style="font-size:11px">${w.recent_works ? `近 ${w.recent_works} 个` : "全局作品数"} · ${w.recent_days ? `${w.recent_days} 天` : "全局天数"}</div>` : ""}</td>
    <td class="mut">${w.last_scan_at ? new Date(w.last_scan_at + "Z").toLocaleString() : "—"}${w.last_error ? ` <span class="warn-ic" title="${esc(w.last_error)}">${ic("i-info")}</span>` : ""}</td>
    <td><span class="pill ${w.enabled ? "active" : "invalid"}">${w.enabled ? "监控中" : "已暂停"}</span></td>
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
  const query = (($("watch-search") && $("watch-search").value) || "").trim().toLocaleLowerCase();
  const rows = WATCHES.filter(w => {
    if (!matchesMeta(w, groupName, tag)) return false;
    if (!query) return true;
    return [watchBaseName(w), w.alias, w.group_name, ...itemTags(w)]
      .join(" ").toLocaleLowerCase().includes(query);
  });
  if ($("watch-filter-count")) $("watch-filter-count").textContent = `显示 ${rows.length} / ${WATCHES.length}`;
  $("watch-table").innerHTML = rows.map(watchRow).join("")
    || empty(9, "没有匹配的评论监控", "i-msg", WATCHES.length ? "调整分组、标签或搜索条件" : "在上方添加一个评论监控");
}
async function refreshWatches() {
  const ws = await api("/api/comment-watches?platform=" + PLATFORM);
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
  const intervalOptions = numericSelectOptions(item.interval_seconds || 600, [
    [60, "每 1 分钟"], [300, "每 5 分钟"], [600, "每 10 分钟"],
    [1800, "每 30 分钟"], [3600, "每小时"], [21600, "每 6 小时"], [86400, "每天"],
  ], " 秒");
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
      interval_seconds: +$("ew-interval").value,
      account_id: $("ew-account").value ? +$("ew-account").value : null,
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
          <div><label class="field" for="ew-account">抓取账号</label><select id="ew-account">${accountOptions}</select></div>
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
    $("ew-interval").value = String(item.interval_seconds || 600);
    $("ew-account").value = item.account_id ? String(item.account_id) : "";
    $("ew-mode").value = canCreator ? (item.mode || "public") : "public";
    if ($("ew-recent")) $("ew-recent").value = String(item.recent_works || 0);
    if ($("ew-days")) $("ew-days").value = String(item.recent_days || 0);
    if ($("ew-depth")) $("ew-depth").value = String(item.max_scrolls || 0);
    ["ew-interval", "ew-account", "ew-mode", "ew-recent", "ew-days", "ew-depth"]
      .forEach(key => { const el = $(key); if (el) enhanceSelect(el); });
    _uiOpen("编辑评论监控", "监控目标保持不变；需要更换作品或被监控的创作者时，请新建评论监控。", { okText: "保存修改", wide: true });
  });
  if (value === null) return;
  try {
    await api("/api/comment-watches/" + id, {
      method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(value),
    });
    toast("评论监控配置已更新", "ok"); refreshWatches(); refreshComments();
  } catch (e) { toast("更新失败:" + e.message, "err"); }
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
  const params = new URLSearchParams({ limit: "80", platform: PLATFORM });
  if (COMMENT_SRC) params.set("watch_id", COMMENT_SRC);
  if (COMMENT_GROUP) params.set("group_name", COMMENT_GROUP);
  if (COMMENT_TAG) params.set("tag", COMMENT_TAG);
  const rows = await api("/api/comments?" + params.toString());
  $("stat-cmt").textContent = rows.length;
  $("comment-table").innerHTML = rows.map(r => {
    const w = watchById(r.watch_id);
    const src = w ? sourceMeta(w) : "";
    return `<tr>
    <td><input type="checkbox" data-id="${r.id}" onchange="commentToggleOne(${r.id}, this.checked)" ${selComment.has(r.id) ? "checked" : ""}></td>
    <td class="wrap" style="max-width:360px">${r.is_reply ? '<span class="mut">↳</span> ' : ""}${esc(r.text || "").slice(0, 60)}${src}</td>
    <td class="mut">${esc(r.user_nickname || "")}</td>
    <td class="mut num">${fmtNum(r.like_count)}</td>
    <td class="mut num">${fmtTime(r.create_time)}</td>
    <td class="acttd"><button class="ghost sm danger" onclick="delComment(${r.id})">${ic("i-trash")}删除</button></td>
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
let PV_N = 0, PV_I = 0, PV_REQ = 0;
function _pvRender(d) {
  const box = $("pv-media"), cap = $("pv-cap");
  const vid = (d.medias || []).find(m => m.kind === "video");
  if (d.media_type === "video" && (d.local_url || vid)) {
    const videoUrl = d.local_url || vid.url;
    box.innerHTML = `<video src="${esc(videoUrl)}" controls autoplay playsinline preload="metadata" poster="${esc(d.cover_url || "")}" referrerpolicy="no-referrer"></video>`;
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
    else if (modal === $("preview")) hidePreview();
    return;
  }
  if (modal === $("preview") && e.key === "ArrowLeft") pvNav(-1);
  else if (modal === $("preview") && e.key === "ArrowRight") pvNav(1);
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
    return `<span class="fp-chip">${thumb}<span title="${esc(f.name)}">${esc(f.name)}</span><button type="button" onclick="pubRemoveFile(${i})" aria-label="移除">${ic("i-x")}</button></span>`;
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
          location: $("pub-location") ? $("pub-location").value.trim() : "",
          visibility: $("pub-visibility") ? $("pub-visibility").value : "public",
          allow_save: $("pub-allowsave") ? $("pub-allowsave").value !== "0" : true }),
      });
      pubFilesClear(); $("pub-title").value = ""; $("pub-desc").value = ""; $("pub-topics").value = ""; $("pub-when").value = ""; if ($("pub-location")) $("pub-location").value = ""; dtSyncAll();
      $("pub-msg").textContent = when ? "已加入定时队列 ✓" : "已加入队列,即将发布 ✓";
      toast("已加入发布队列", "ok");
    } catch (e) { $("pub-msg").textContent = "失败: " + e.message; toast("发布失败:" + e.message, "err"); }
  });
  refreshPublish();
}
const PUB_ST = { pending: "排队中", publishing: "发布中", done: "已发布", failed: "失败", canceled: "已取消" };
const PUB_PILL = { pending: "pending", publishing: "downloading", done: "done", failed: "failed", canceled: "invalid" };
async function editPublish(id) {
  const task = PUBLISH_TASKS.find(x => x.id === id); if (!task) return;
  const accounts = ACCOUNTS.filter(a => a.platform === task.platform);
  const accountOptions = accounts.map(a =>
    `<option value="${a.id}">${esc(a.nickname)}${a.has_creator ? " · 创作号" : ""}</option>`
  ).join("");
  const value = await new Promise(res => {
    _uiResolve = res; _uiCancelVal = null;
    _uiGetVal = () => ({
      account_id: +$("ep-account").value,
      title: $("ep-title").value.trim(),
      desc: $("ep-desc").value,
      topics: $("ep-topics").value.trim(),
      scheduled_at: $("ep-when").value || null,
      location: $("ep-location") ? $("ep-location").value.trim() : "",
      visibility: $("ep-visibility") ? $("ep-visibility").value : "public",
      allow_save: $("ep-allowsave") ? $("ep-allowsave").value !== "0" : true,
    });
    $("ui-body").innerHTML = `
      <div><label class="field" for="ep-account">发布账号</label>
        <select id="ep-account">${accountOptions}</select></div>
      <div><label class="field" for="ep-title">标题（≤20 字）</label>
        <input id="ep-title" maxlength="20" value="${esc(task.title || "")}"></div>
      <div><label class="field" for="ep-desc">正文</label>
        <textarea id="ep-desc" rows="4">${esc(task.desc || "")}</textarea></div>
      <div><label class="field" for="ep-topics">话题</label>
        <input id="ep-topics" value="${esc(task.topics || "")}" placeholder="逗号分隔，不用带 #"></div>
      <div><label class="field" for="ep-when">定时发布</label>
        <input type="datetime-local" id="ep-when" aria-label="定时发布（留空=尽快发）"></div>
      ${task.platform === "shipinhao" ? `<div><label class="field" for="ep-location">位置</label>
        <input id="ep-location" value="${esc(task.location || "")}" placeholder="城市或地点名"></div>` : ""}
      ${task.platform === "douyin" ? `<fieldset class="publish-permissions">
        <legend>互动与权限</legend>
        <div class="publish-permission"><label class="field" for="ep-visibility">谁可以看</label>
          <select id="ep-visibility"><option value="public">公开</option><option value="friends">好友可见</option><option value="private">仅自己可见</option></select></div>
        <div class="publish-permission"><label class="field" for="ep-allowsave">保存权限</label>
          <select id="ep-allowsave"><option value="1">允许他人保存</option><option value="0">不允许</option></select></div>
      </fieldset>` : ""}`;
    $("ep-account").value = task.account_id ? String(task.account_id) : "";
    $("ep-when").value = task.scheduled_at ? task.scheduled_at.slice(0, 16) : "";
    if ($("ep-visibility")) $("ep-visibility").value = task.visibility || "public";
    if ($("ep-allowsave")) $("ep-allowsave").value = task.allow_save === false ? "0" : "1";
    ["ep-account", "ep-visibility", "ep-allowsave"].forEach(key => { const el = $(key); if (el) enhanceSelect(el); });
    enhanceDateTime($("ep-when"));
    _uiOpen("编辑发布任务", `可修改文案、账号、时间和权限；${task.media_count} 个附件如需更换，请删除任务后重建。`, { okText: "保存修改", wide: true });
  });
  if (value === null) return;
  if (!value.account_id) { toast("请选择发布账号", "err"); return; }
  try {
    await api("/api/publish/" + id, {
      method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(value),
    });
    toast("发布任务已更新", "ok"); refreshPublish();
  } catch (e) { toast("更新失败:" + e.message, "err"); }
}
async function refreshPublish() {
  if (!$("pub-table")) return;
  const rows = await api("/api/publish?platform=" + (pfHasPublish(PLATFORM) ? PLATFORM : "xhs"));
  PUBLISH_TASKS = rows;
  if ($("tb-pub")) $("tb-pub").textContent = rows.length;
  $("pub-table").innerHTML = rows.map(t => `<tr>
    <td class="wrap" style="max-width:220px">${esc(t.title || "(无标题)")}</td>
    <td>${t.media_type === "video" ? "视频" : "图文"}</td>
    <td class="num">${t.media_count}</td>
    <td>${t.source_platform ? esc(t.source_platform) + " 转发" : "手动"}</td>
    <td class="mut num">${t.scheduled_at ? new Date(t.scheduled_at).toLocaleString() : "尽快"}</td>
    <td><span class="pill ${PUB_PILL[t.status] || "pending"}">${PUB_ST[t.status] || t.status}</span>${t.error ? ` <span class="warn-ic" title="${esc(t.error)}">${ic("i-info")}</span>` : ""}${t.result_url ? (t.platform === "shipinhao" ? ` <a href="javascript:void(0)" onclick="openPubInBrowser(${t.account_id}, '${esc(t.result_url)}')">查看</a>` : ` <a href="${esc(t.result_url)}" target="_blank">查看</a>`) : ""}</td>
    <td class="acttd">
      ${["pending", "failed", "canceled"].includes(t.status) ? `<button class="ghost sm" onclick="editPublish(${t.id})">编辑</button>` : ""}
      ${["pending", "failed"].includes(t.status) ? `<button class="ghost sm" onclick="runPublish(${t.id})">立即发布</button>` : ""}
      <button class="ghost sm danger" onclick="delPublish(${t.id})">${ic("i-trash")}删除</button>
    </td></tr>`).join("") || empty(7, "暂无发布任务", "i-send",
      PLATFORM === "kuaishou" ? "上传图集/视频加入队列(发布到快手创作平台)"
      : PLATFORM === "douyin" ? "上传图集/视频加入队列(发布到抖音创作平台)"
      : "上传图集/视频加入队列,或在抖音作品上点「发小红书」转发过来");
}
// 视频号作品无公开链接:用该账号已登录浏览器打开图文/视频管理页查看
async function openPubInBrowser(accountId, url) {
  if (!accountId) { toast("缺少账号信息", "err"); return; }
  toast("正在用该账号浏览器打开视频号管理页…", "info", 5000);
  try {
    await api("/api/accounts/" + accountId + "/open-browser?url=" + encodeURIComponent(url || ""), { method: "POST" });
  } catch (e) { toast("打开失败:" + e.message, "err"); }
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
let REPOST_ID = null;
let REPOST_TARGET = "xhs";           // xhs / douyin / shipinhao
const repostXhs = (id) => openRepost(id, "xhs");
const repostDouyin = (id) => openRepost(id, "douyin");
const repostChannels = (id) => openRepost(id, "shipinhao");
async function pickRepostTarget(id) {
  const target = await uiSelect({
    title: "转发作品",
    hint: "选择要发布到的平台，下一步可以继续编辑标题、文案和发布时间。",
    options: [
      { value: "xhs", label: "小红书" },
      { value: "shipinhao", label: "视频号" },
    ],
    value: "shipinhao",
  });
  if (target === null) return;
  openRepost(id, target);
}
async function openRepost(id, target) {
  const rec = CONTENTS.find(r => r.id === id);
  // 拉取目标平台可发布账号:小红书需创作号;抖音/视频号需任一登录态
  const all = await api("/api/accounts?platform=" + target);
  const accs = target === "xhs"
    ? all.filter(a => a.has_creator)
    : all.filter(a => a.has_storage || a.has_creator);
  if (!accs.length) {
    const loginHint = target === "xhs"
      ? "请先在小红书账号页完成「创作者登录」(发布用)"
      : target === "shipinhao"
        ? "请先在视频号账号页完成「视频号登录」"
        : "请先在抖音账号页完成登录(扫码/创作者/Cookie)";
    toast(loginHint, "err");
    return;
  }
  REPOST_ID = id; REPOST_TARGET = target;
  const isDy = target === "douyin";
  const isChannels = target === "shipinhao";
  const cap = isDy ? 30 : isChannels ? 16 : 20;
  const pname = isDy ? "抖音" : isChannels ? "视频号" : "小红书";
  $("rp-head").textContent = "发" + pname + " · 编辑后推送";
  $("rp-title-label").textContent = `标题(≤${cap} 字)`;
  $("rp-title").maxLength = cap;
  $("rp-title").placeholder = target === "xhs" ? "给笔记起个标题" : "给作品起个标题";
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
  modalOpened($("repost"));
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
    if (d.media_type === "video" && (d.local_url || vid)) {
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
      <button type="button" class="rp-th-x" title="移除这张" aria-label="移除这张" onclick="rpImgRemove(${pos})">${ic("i-x")}</button>
      <div class="rp-th-mv">
        <button type="button" onclick="rpImgMove(${pos},-1)" ${pos === 0 ? "disabled" : ""} title="前移(移到最前=封面)" aria-label="前移">${ic("i-prev")}</button>
        <button type="button" onclick="rpImgMove(${pos},1)" ${pos === n - 1 ? "disabled" : ""} title="后移" aria-label="后移">${ic("i-next")}</button>
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
function hideRepost() {
  $("repost").style.display = "none"; REPOST_ID = null;
  modalClosed($("repost"));
}
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
  const pname = REPOST_TARGET === "douyin" ? "抖音"
    : REPOST_TARGET === "shipinhao" ? "视频号" : "小红书";
  try {
    const r = await api("/api/contents/" + REPOST_ID + "/repost-" + REPOST_TARGET, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    });
    toast((body.scheduled_at ? "已加入定时发布队列" : `已加入${pname}发布队列`) + "(任务 #" + r.task_id + ")", "ok");
    hideRepost();
    if (typeof refreshPublish === "function") refreshPublish();
  } catch (e) { $("rp-msg").textContent = "失败:" + e.message; toast("转发失败:" + e.message, "err"); btn.disabled = false; }
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
    _uiOpen("编辑规则 #" + id, "改了「目标/关键词」会重新解析;账号需与规则平台一致", { okText: "保存修改", wide: true });
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
        <button class="ghost sm danger" onclick="delRule(${r.id})">${ic("i-trash")}删除</button>
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
      <button class="ghost sm danger" onclick="delTask(${t.id})">${ic("i-trash")}删除</button>
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
  if (INFLIGHT > 0 || document.hidden) return;   // 慢操作/后台标签页不刷新,减少干扰与无效请求
  refreshMonitors(); refreshContents(); refreshWatches(); refreshComments(); refreshDanmakuWatches(); refreshDanmaku(); refreshOverviewChart(); refreshCommentRules(); refreshCommentTasks(); if (pfHasPublish(PLATFORM)) refreshPublish();
}

// initial skeletons while data loads
$("mon-table").innerHTML = skeleton(8);
$("content-table").innerHTML = skeleton(8);
$("watch-table").innerHTML = skeleton(9);
$("comment-table").innerHTML = skeleton(6);
$("danmaku-watch-table").innerHTML = skeleton(8);
$("danmaku-table").innerHTML = skeleton(6);

// restore last-selected section (default: 总览);旧版四个独立页已并入「账号管理」
const VALID_TABS = ["overview", "accounts", "monitors", "comments", "danmaku", "hub", "publish", "autocomment", "share-download", "notifications", "settings"];
const LEGACY_HUB_TABS = ["myworks", "following", "fans", "dm"];
switchTab((() => {
  try {
    const hashTab = decodeURIComponent(location.hash.replace(/^#/, ""));
    if (VALID_TABS.includes(hashTab)) return hashTab;
    const t = localStorage.getItem("dym-tab");
    if (LEGACY_HUB_TABS.includes(t)) { HUB_TAB = t; return "hub"; }
    return VALID_TABS.includes(t) ? t : "overview";
  } catch (e) { return "overview"; }
})());
switchHubTab(HUB_TAB);   // 恢复上次停留的子标签(我的作品/关注/粉丝/私信)

// restore last-selected platform (default: 抖音)
PLATFORM = (() => { try { const p = localStorage.getItem("dym-pf"); return ["xhs", "douyin", "kuaishou", "shipinhao"].includes(p) ? p : "douyin"; } catch (e) { return "douyin"; } })();
applyPlatformUI();

onTypeChange(); bindPubFilePicker(); onPubType(); populateWatchAccount(); applyDanmakuForm(); onAcMode(); loadSettings(); refreshAccounts(); refreshProxies(); refreshChannels(); loop();
enhanceAllSelects();   // 把所有原生 <select> 升级为美化下拉
enhanceAllMetaControls(); // 分组/标签：当前平台词库下拉，可搜索并新增
enhanceAllDateTime();  // 把 datetime-local 升级为自定义日期选择器

// shell 交互：浏览器前进/后退、平台键盘切换、长页面返回顶部。
window.addEventListener("hashchange", () => {
  const tab = decodeURIComponent(location.hash.replace(/^#/, ""));
  if (VALID_TABS.includes(tab) && tab !== CURRENT_TAB) switchTab(tab);
});
document.querySelector(".pswitch").addEventListener("keydown", e => {
  if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(e.key)) return;
  e.preventDefault();
  const buttons = [...document.querySelectorAll(".pswitch button")];
  let index = buttons.indexOf(document.activeElement);
  if (e.key === "Home") index = 0;
  else if (e.key === "End") index = buttons.length - 1;
  else index = (index + (e.key === "ArrowRight" ? 1 : -1) + buttons.length) % buttons.length;
  buttons[index].focus();
  switchPlatform(buttons[index].dataset.pf);
});
let _backTopTick = false;
window.addEventListener("scroll", () => {
  if (_backTopTick) return;
  _backTopTick = true;
  requestAnimationFrame(() => {
    $("backtop").classList.toggle("show", window.scrollY > 520);
    _backTopTick = false;
  });
}, { passive: true });
document.addEventListener("visibilitychange", () => { if (!document.hidden) loop(); });
setInterval(loop, 8000);
