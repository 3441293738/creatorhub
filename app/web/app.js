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
    opts = { ...(opts || {}) };
    const headers = new Headers(opts.headers || {});
    try {
      const adminToken = sessionStorage.getItem("creatorhub-risk-admin-token") || "";
      if (adminToken) headers.set("X-CreatorHub-Admin-Token", adminToken);
    } catch (e) {}
    headers.set("X-CreatorHub-Actor", "creatorhub-web");
    opts.headers = headers;
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
  else if (e.key === "Enter" && document.activeElement
      && !["TEXTAREA", "BUTTON"].includes(document.activeElement.tagName)) uiModalOk();
}
function uiModalCancel() { _uiClose(_uiCancelVal); }
function uiModalOk() { _uiClose(_uiGetVal ? _uiGetVal() : ""); }
function _uiOpen(title, hint, { okText = "确定", danger = false, wide = false } = {}) {
  const previousExtraAction = $("ui-extra-action");
  if (previousExtraAction) previousExtraAction.remove();
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
function uiPrompt({ title, hint, value, placeholder, multiline, rows, secret = false }) {
  return new Promise(res => {
    _uiResolve = res; _uiCancelVal = null;
    _uiGetVal = () => { const el = $("ui-body").querySelector("select,input,textarea"); return el ? el.value : ""; };
    $("ui-body").innerHTML = multiline
      ? `<textarea id="ui-inp" rows="${rows || 6}" placeholder="${esc(placeholder || "")}">${esc(value || "")}</textarea>`
      : `<input id="ui-inp" type="${secret ? "password" : "text"}" value="${esc(value || "")}" placeholder="${esc(placeholder || "")}" autocomplete="${secret ? "current-password" : "off"}">`;
    _uiOpen(title, hint);
  });
}

// ─── 自定义下拉:渐进增强原生 <select>(美化展开列表)───
// 弹层挂到 body 以避开卡片 overflow；Tab 时显式回到文档顺序，避免焦点落到 body 末尾。
let _openSelectClose = null;
function focusAdjacentControl(origin, backwards = false) {
  const nodes = [...document.querySelectorAll(
    'button:not([disabled]),a[href],input:not([disabled]),textarea:not([disabled]),select:not([disabled]),[tabindex]:not([tabindex="-1"])'
  )].filter(node => node.offsetParent !== null && !node.classList.contains("cs-native") && !node.classList.contains("dt-native") && !node.closest(".cs-panel,.dt-panel"));
  const index = nodes.indexOf(origin);
  const next = nodes[index + (backwards ? -1 : 1)];
  if (next) requestAnimationFrame(() => next.focus({ preventScroll: true }));
}
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
  let panel = null, typeBuffer = "", typeTimer = null;

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
    window.removeEventListener("scroll", close, true);
    window.removeEventListener("resize", close);
    document.removeEventListener("mousedown", onDoc, true);
  }
  function onDoc(e) { if (!wrap.contains(e.target) && (!panel || !panel.contains(e.target))) close(); }
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
      if (target) { target.focus(); target.scrollIntoView({ block: "nearest" }); }
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
          options[(index + (ev.key === "ArrowDown" ? 1 : -1) + options.length) % options.length].focus();
        } else if (ev.key === "Home" || ev.key === "End") {
          ev.preventDefault(); options[ev.key === "Home" ? 0 : options.length - 1].focus();
        } else if (ev.key === "Enter" || ev.key === " ") {
          ev.preventDefault(); choose(i);
        } else if (ev.key === "Escape") {
          ev.preventDefault(); close(); trg.focus({ preventScroll: true });
        } else if (ev.key === "Tab") {
          ev.preventDefault(); close(); focusAdjacentControl(trg, ev.shiftKey);
        } else if (ev.key.length === 1 && !ev.altKey && !ev.ctrlKey && !ev.metaKey) {
          focusTyped(ev.key);
        }
      });
      panel.appendChild(it);
    });
    document.body.appendChild(panel);
    const r = trg.getBoundingClientRect();
    panel.style.left = Math.max(6, Math.min(r.left, window.innerWidth - r.width - 6)) + "px";
    panel.style.width = Math.min(r.width, window.innerWidth - 12) + "px";
    const below = window.innerHeight - r.bottom;
    if (below < 280 && r.top > below) panel.style.bottom = (window.innerHeight - r.top + 5) + "px";
    else panel.style.top = (r.bottom + 5) + "px";
    panel.style.maxWidth = Math.max(180, window.innerWidth - 12) + "px";
    wrap.classList.add("open");
    _openSelectClose = close;
    trg.setAttribute("aria-expanded", "true");
    trg.setAttribute("aria-controls", panel.id);
    window.addEventListener("scroll", close, true);
    window.addEventListener("resize", close);
    setTimeout(() => document.addEventListener("mousedown", onDoc, true), 0);
    if (focusSelected) setTimeout(() => {
      const target = panel && (panel.querySelector(".cs-opt.sel:not(.dis)") || panel.querySelector(".cs-opt:not(.dis)"));
      if (target) { target.focus(); target.scrollIntoView({ block: "nearest" }); }
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
  function close() { if (panel) { panel.remove(); panel = null; } if (_openDateClose === close) _openDateClose = null; wrap.classList.remove("open"); trg.setAttribute("aria-expanded", "false"); trg.removeAttribute("aria-controls"); window.removeEventListener("scroll", close, true); window.removeEventListener("resize", close); document.removeEventListener("mousedown", onDoc, true); }
  function onDoc(e) { if (!wrap.contains(e.target) && (!panel || !panel.contains(e.target))) close(); }
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
    _openDateClose = close;
    trg.setAttribute("aria-expanded", "true"); trg.setAttribute("aria-controls", panel.id);
    panel.addEventListener("keydown", e => {
      if (e.key === "Escape") { e.preventDefault(); close(); trg.focus({ preventScroll: true }); return; }
      if (["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "Home", "End"].includes(e.key) && e.target.classList.contains("dt-day")) {
        e.preventDefault();
        const days = [...panel.querySelectorAll(".dt-day[data-d]")];
        const index = days.indexOf(e.target);
        const delta = e.key === "ArrowLeft" ? -1 : e.key === "ArrowRight" ? 1 : e.key === "ArrowUp" ? -7 : e.key === "ArrowDown" ? 7 : 0;
        const target = e.key === "Home" ? days[0] : e.key === "End" ? days[days.length - 1] : days[Math.max(0, Math.min(days.length - 1, index + delta))];
        if (target) target.focus();
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
function enhanceAllDateTime(root) {
  const scope = root || document;
  if (scope.matches && scope.matches("input[type=datetime-local]:not([data-dt])")) enhanceDateTime(scope);
  if (scope.querySelectorAll) scope.querySelectorAll("input[type=datetime-local]:not([data-dt])").forEach(enhanceDateTime);
}
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

async function exportMonitorReport(explicitBtn = null) {
  const btn = explicitBtn || evtBtn();
  const params = new URLSearchParams({ platform: PLATFORM });
  await withBusy(btn, "导出中", async () => {
    try {
      const response = await fetch("/api/reports/monitor.xlsx?" + params.toString());
      if (!response.ok) {
        let message = response.status;
        try {
          const body = await response.json();
          message = body.detail || message;
        } catch (e) { }
        throw new Error(message);
      }
      const blob = await response.blob();
      const disposition = response.headers.get("content-disposition") || "";
      const matched = disposition.match(/filename="?([^";]+)"?/i);
      const filename = matched ? matched[1] :
        `creatorhub_monitor_report_${new Date().toISOString().slice(0, 19).replace(/[-:T]/g, "")}.xlsx`;
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = filename;
      document.body.appendChild(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
      toast("Excel 监控报告已导出", "ok");
    } catch (e) {
      toast("报告导出失败: " + e.message, "err");
    }
  });
}


async function _downloadExcelReport(path, fallbackName) {
  const response = await fetch(path);
  if (!response.ok) {
    let message = response.status;
    try {
      const body = await response.json();
      message = body.detail || message;
    } catch (e) { }
    throw new Error(message);
  }
  const blob = await response.blob();
  const disposition = response.headers.get("content-disposition") || "";
  const matched = disposition.match(/filename="?([^";]+)"?/i);
  const filename = matched ? matched[1] : fallbackName;
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function _moduleReportParams(module, full) {
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
  if (module === "monitors") {
    put("q", $("mon-search") && $("mon-search").value);
    put("group_name", $("mon-group") && $("mon-group").value);
    put("tag", $("mon-tag") && $("mon-tag").value);
  } else if (module === "contents") {
    put("target_id", CONTENT_SRC);
    put("group_name", CONTENT_GROUP);
    put("tag", CONTENT_TAG);
    put("q", $("content-search") && $("content-search").value);
    put("media_type", $("content-type") && $("content-type").value);
    put("download_status", $("content-status") && $("content-status").value);
    put("min_like_count", $("content-min-likes") && $("content-min-likes").value);
    put("min_comment_count", $("content-min-comments") && $("content-min-comments").value);
    put("sort", $("content-sort") && $("content-sort").value);
  } else if (module === "comment-watches") {
    put("q", $("watch-search") && $("watch-search").value);
    put("group_name", $("watch-group") && $("watch-group").value);
    put("tag", $("watch-tag") && $("watch-tag").value);
  } else if (module === "comments") {
    put("watch_id", COMMENT_SRC);
    put("group_name", COMMENT_GROUP);
    put("tag", COMMENT_TAG);
    put("q", $("comment-query") && $("comment-query").value);
    put("reply_type", $("comment-type") && $("comment-type").value);
    put("min_like_count", $("comment-min-likes") && $("comment-min-likes").value);
    put("sort", $("comment-sort") && $("comment-sort").value);
  } else if (module === "danmaku-watches") {
    put("q", $("danmaku-watch-search") && $("danmaku-watch-search").value);
    put("group_name", $("danmaku-watch-group") && $("danmaku-watch-group").value);
    put("tag", $("danmaku-watch-tag") && $("danmaku-watch-tag").value);
  } else if (module === "danmaku") {
    put("watch_id", DANMAKU_SRC);
    put("q", $("danmaku-query") && $("danmaku-query").value);
    const start = +(($("danmaku-time-start") && $("danmaku-time-start").value) || 0);
    const end = +(($("danmaku-time-end") && $("danmaku-time-end").value) || 0);
    if (start > 0) put("min_video_time_ms", Math.round(start * 1000));
    if (end > 0) put("max_video_time_ms", Math.round(end * 1000));
    put("sort", $("danmaku-sort") && $("danmaku-sort").value);
  }
  return params;
}

const _reportLabels = {
  monitors: "监控列表",
  contents: "作品数据",
  "comment-watches": "评论监控",
  comments: "评论数据",
  "danmaku-watches": "弹幕监控",
  danmaku: "弹幕数据",
};
const _reportCountIds = {
  monitors: "mon-filter-count",
  contents: "content-filter-count",
  "comment-watches": "watch-filter-count",
  comments: "comment-filter-count",
  "danmaku-watches": "danmaku-watch-filter-count",
  danmaku: "danmaku-filter-count",
};
function _lockExportGroup(group, active) {
  if (!group) return () => {};
  const siblings = [...group.querySelectorAll("button")].filter(button => button !== active);
  const states = siblings.map(button => button.disabled);
  siblings.forEach(button => { button.disabled = true; });
  group.classList.add("is-busy");
  group.setAttribute("aria-busy", "true");
  return () => {
    siblings.forEach((button, index) => { button.disabled = states[index]; });
    group.classList.remove("is-busy");
    group.removeAttribute("aria-busy");
  };
}

async function exportModuleReport(module, full = false, explicitBtn = null) {
  const paths = {
    monitors: "/api/reports/monitors.xlsx",
    contents: "/api/reports/contents.xlsx",
    "comment-watches": "/api/reports/comment-watches.xlsx",
    comments: "/api/reports/comments.xlsx",
    "danmaku-watches": "/api/reports/danmaku-watches.xlsx",
    danmaku: "/api/reports/danmaku.xlsx",
  };
  const path = paths[module];
  if (!path) return;
  const btn = explicitBtn || evtBtn();
  const group = btn && btn.closest(".export-actions");
  const unlock = _lockExportGroup(group, btn);
  const label = _reportLabels[module] || "模块数据";
  const params = _moduleReportParams(module, full);
  await withBusy(btn, full ? "全量导出" : "筛选导出", async () => {
    try {
      await _downloadExcelReport(
        path + "?" + params.toString(),
        "creatorhub_" + module + "_report.xlsx",
      );
      const count = $( _reportCountIds[module] )?.textContent?.trim();
      const scope = full ? "全量" : "筛选结果";
      toast(`${label} ${scope} Excel 已导出${!full && count ? `（${count}）` : ""}`, "ok");
    } catch (e) {
      toast("报告导出失败: " + e.message, "err");
    } finally {
      unlock();
    }
  });
}

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
  "risk-control": {
    title: "风控中心", desc: "统一管理风控规则，查看账号状态、触发原因、恢复进度与事件记录。"
  },
  monitors: {
    title: "作品监控", desc: "添加采集目标，管理下载策略并追踪作品状态。"
  },
  collections: {
    title: "关键词批量采集", desc: "批量搜索抖音视频，并按上限采集评论与媒体。"
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
  queue: {
    title: "任务队列", desc: "统一查看采集、发布、评论、账号动作与下载任务的排队、执行和阻塞状态。"
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
  refreshAccounts(); refreshMonitors(); refreshContents(); refreshWatches(); refreshComments(); refreshDanmakuWatches(); refreshDanmaku(); refreshCollections();
  updateTaskQueuePlatformLabel();
  if (CURRENT_TAB === "queue") refreshTaskQueue(true); else refreshTaskQueueBadge();
  if (CURRENT_TAB === "risk-control") refreshRiskCenter(true);
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
  document.querySelectorAll(".collect-only").forEach(e => e.classList.toggle("hidden", PLATFORM !== "douyin"));
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
    : "发布通过账号独立的可见 Chrome 页面完成。提交只点击一次；若显示“结果待确认”，请先到小红书核对，系统不会自动重发。";
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
  applyCollectionForm();
  if (PLATFORM === "douyin") applyDanmakuForm();
  if ($("t-kind") && PLATFORM !== "xhs") $("t-kind").value = "creator";
  // 视频号只有本账号数据,不支持「监控他人」:若正停在这些面板,自动切到「账号管理」
  if (pfIsChannels(PLATFORM)) {
    const cur = (document.querySelector('.navitem.active') || {}).dataset;
    if (cur && ["monitors", "comments", "autocomment"].includes(cur.tab)) switchTab("hub");
    // 视频号本账号只有「我的作品 / 数据」;若停在关注/粉丝/私信子页,切回我的作品
    if (["following", "fans", "dm"].includes(HUB_TAB)) switchHubTab("myworks");
  }
  if (PLATFORM !== "douyin" && CURRENT_TAB === "collections") switchTab("overview");
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
  if (_openSelectClose) _openSelectClose();
  if (_openDateClose) _openDateClose();
  if (OPEN_META_COMBO) OPEN_META_COMBO.close();
  let activePanel = null;
  document.querySelectorAll("[data-panel]").forEach(p => {
    const active = p.dataset.panel === name;
    p.style.display = active ? "" : "none";
    p.classList.remove("panel-enter");
    if (active) activePanel = p;
  });
  if (changed && activePanel) requestAnimationFrame(() => {
    activePanel.classList.add("panel-enter");
    activePanel.addEventListener("animationend", () => activePanel.classList.remove("panel-enter"), { once: true });
  });
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
  if (name === "collections") { populateCollectionAccount(); refreshCollections(); }
  if (name === "queue") refreshTaskQueue();
  if (name === "risk-control") refreshRiskCenter();
}

// ─── 扫码登录(真实浏览器窗口) ───
let qrTimer = null;
let preLoginBrowserBackend = "default";
let preLoginBrowserCatalog = null;
function browserChoiceParts(choice) {
  const [backend, runtimeId = ""] = String(choice || "default").split("::", 2);
  return { backend: backend || "default", runtimeId };
}
function browserChoiceOptions(catalog) {
  const backends = catalog.backends || [];
  const defaultBackend = backends.find(item => item.name === catalog.default);
  const local = backends.find(item => item.name === "local");
  const fingerprint = backends.find(item => item.name === "fingerprint_chromium");
  const runtimes = catalog.runtimes || [];
  const options = [{
    value: "default",
    label: `跟随全局（${defaultBackend ? defaultBackend.label : catalog.default}）`,
    disabled: !!defaultBackend && !defaultBackend.available,
  }];
  if (local) options.push({
    value: "local", label: local.label,
    disabled: !local.available,
  });
  runtimes.forEach(runtime => options.push({
    value: `fingerprint_chromium::${runtime.runtime_id}`,
    label: `${runtime.name}${runtime.version ? ` · ${runtime.version}` : ""}${runtime.is_default ? " · 默认" : ""}`
      + (runtime.available ? "" : ` · 不可用：${runtime.detail || "未配置"}`),
    disabled: !runtime.available,
  }));
  if (!runtimes.length && fingerprint) options.push({
    value: "fingerprint_chromium",
    label: fingerprint.label + (fingerprint.available ? "" : ` · 不可用：${fingerprint.detail || "未配置"}`),
    disabled: !fingerprint.available,
  });
  return options;
}
// 新账号尚未落库，扫码前先确定浏览器内核；成功后该选择随账号持久化。
async function choosePreLoginBrowserBackend() {
  let catalog;
  try {
    catalog = await api("/api/browser-backends");
    preLoginBrowserCatalog = catalog;
  } catch (e) {
    toast("读取登录环境失败：" + e.message, "err");
    return null;
  }
  const options = browserChoiceOptions(catalog);
  const selected = await uiSelect({
    title: "选择扫码登录环境",
    hint: "扫码、Cookie 落地和后续账号任务将使用同一浏览器内核。新的指纹环境会同时打开 BrowserScan 体检标签。",
    options,
    value: preLoginBrowserBackend,
  });
  if (selected !== null) preLoginBrowserBackend = selected;
  return selected;
}
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
function freshPreLoginFingerprint(browserBackend) {
  const choice = browserChoiceParts(browserBackend);
  const runtimes = (preLoginBrowserCatalog || {}).runtimes || [];
  const runtime = runtimes.find(item => item.runtime_id === choice.runtimeId)
    || runtimes.find(item => item.is_default) || {};
  const seed = (globalThis.crypto && typeof globalThis.crypto.randomUUID === "function")
    ? globalThis.crypto.randomUUID().replaceAll("-", "")
    : `${Date.now().toString(16)}${Math.random().toString(16).slice(2)}`;
  return {
    seed, engine_seed: "", fingerprint_id: seed.slice(0, 12),
    source_ip: "", country: "", region: "", city: "",
    timezone: "Asia/Shanghai", locale: "zh-CN", accept_languages: "",
    viewport_w: 1280, viewport_h: 800, geo_lat: 0, geo_lon: 0,
    platform: "", platform_version: "", brand: "", brand_version: "",
    hardware_concurrency: 0, gpu_vendor: "", gpu_renderer: "",
    disable_spoofing: [], language_mode: "auto", timezone_mode: "auto",
    viewport_mode: "auto", location_mode: "auto",
    geolocation_permission: "allow", webrtc_mode: "conceal", extra_args: "",
    runtime_version: runtime.version || "",
  };
}
async function configurePreLoginFingerprint(browserBackend) {
  const choice = browserChoiceParts(browserBackend);
  const effectiveBackend = choice.backend === "default"
    ? (preLoginBrowserCatalog || {}).default
    : choice.backend;
  if (effectiveBackend !== "fingerprint_chromium") return "";
  const draft = freshPreLoginFingerprint(browserBackend);
  const account = {
    nickname: "新账号",
    environment: { runtime_version: draft.runtime_version },
  };
  const action = await uiFingerprintEditor(account, draft, { preLogin: true });
  if (!action) return null;
  return action.action === "auto" ? "" : action.data;
}
function loginStartUrl(path, proxy, browserBackend) {
  const choice = browserChoiceParts(browserBackend);
  return path + "?proxy=" + encodeURIComponent(proxy)
    + "&browser_backend=" + encodeURIComponent(choice.backend)
    + "&browser_runtime_id=" + encodeURIComponent(choice.runtimeId);
}
function loginStartOptions(fingerprint) {
  if (!fingerprint || typeof fingerprint !== "object") return { method: "POST" };
  return {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(fingerprint),
  };
}
async function startLogin() {
  const browserBackend = await choosePreLoginBrowserBackend();
  if (browserBackend === null) return;
  const proxy = await choosePreLoginProxy();
  if (proxy === null) return;
  const fingerprint = await configurePreLoginFingerprint(browserBackend);
  if (fingerprint === null) return;
  $("cookiebox").style.display = "none";
  $("qrbox").style.display = "block";
  $("qrstatus").textContent = "正在打开浏览器窗口…";
  try {
    const res = await api(loginStartUrl("/api/login/browser/start", proxy, browserBackend), loginStartOptions(fingerprint));
    $("qrstatus").innerHTML = `${ic("i-eye")} <b>浏览器窗口已打开</b>，请在该窗口点击「登录」并使用抖音 App 扫码。<br>完成后这里会自动刷新。`;
    pollLogin(res.task_id);
  } catch (e) { $("qrstatus").textContent = "启动失败: " + e.message; toast("登录启动失败:" + e.message, "err"); }
}
function loginEnvironmentText(env) {
  if (!env || !env.backend_label) return "";
  let text = env.backend_label;
  if (env.runtime_version && !text.includes(env.runtime_version)) text += " · " + env.runtime_version;
  if (env.has_proxy) text += " · 账号代理";
  if (env.fallback_reason) text += "（" + env.fallback_reason + "）";
  return text;
}
function pollLogin(tid) {
  clearInterval(qrTimer);
  clearTimeout(qrTimer);
  let accountShown = false;
  const tick = async () => {
    try {
      const res = await api("/api/login/browser/poll?task_id=" + tid);
      const envText = loginEnvironmentText(res.environment);
      if (["opening", "waiting"].includes(res.status) && envText) {
        $("qrstatus").innerHTML = `${ic("i-eye")} 浏览器已打开 · <b>${esc(envText)}</b><br>请在可见窗口完成登录。`;
      }
      if (res.status === "verification") {
        $("qrstatus").innerHTML = `${ic("i-info")} <b>需要完成一次设备安全验证</b><br>${esc(res.hint || "请扫描浏览器中的验证二维码，验证通过后会自动继续登录。")}`;
      } else if (res.status === "persisted") {
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
          const suffix = ["error", "deferred"].includes(res.profile_status) ? "（资料可稍后刷新）" : "";
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
  const browserBackend = await choosePreLoginBrowserBackend();
  if (browserBackend === null) return;
  const proxy = await choosePreLoginProxy();
  if (proxy === null) return;
  const fingerprint = await configurePreLoginFingerprint(browserBackend);
  if (fingerprint === null) return;
  $("cookiebox").style.display = "none";
  $("qrbox").style.display = "block";
  $("qrstatus").textContent = "正在打开创作中心窗口…";
  try {
    const res = await api(loginStartUrl("/api/login/creator/start", proxy, browserBackend), loginStartOptions(fingerprint));
    $("qrstatus").innerHTML = `${ic("i-eye")} <b>创作中心窗口已打开</b>，请在该窗口扫码登录抖音账号。<br>此登录态也可用于公开抓取。`;
    pollLogin(res.task_id);
  } catch (e) { $("qrstatus").textContent = "启动失败: " + e.message; toast("创作者登录启动失败:" + e.message, "err"); }
}

// ─── 小红书扫码登录 ───
async function startXhsLogin() {
  const browserBackend = await choosePreLoginBrowserBackend();
  if (browserBackend === null) return;
  const proxy = await choosePreLoginProxy();
  if (proxy === null) return;
  const fingerprint = await configurePreLoginFingerprint(browserBackend);
  if (fingerprint === null) return;
  $("cookiebox").style.display = "none";
  $("qrbox").style.display = "block";
  $("qrstatus").textContent = "正在打开小红书窗口…";
  try {
    const res = await api(loginStartUrl("/api/login/xhs/start", proxy, browserBackend), loginStartOptions(fingerprint));
    if (res.reused) {
      $("qrstatus").innerHTML = `${ic("i-eye")} <b>已有小红书扫码窗口</b>，已尝试切换到前台，请直接在该窗口继续。`;
      toast("已有扫码窗口，已切换到前台", "info");
    } else {
      $("qrstatus").innerHTML = `${ic("i-eye")} <b>小红书官网首页已打开</b>，请在窗口中点击「登录」并使用小红书 App 扫码。<br>新指纹环境会附带 BrowserScan 体检标签，可先核对 IP、时区和 WebRTC。<br>主站登录成功后会保存读取登录态并自动关闭窗口。<br>如需发布，请随后单独点击「创作者登录」。`;
    }
    pollLogin(res.task_id);
  } catch (e) { $("qrstatus").textContent = "启动失败: " + e.message; toast("小红书登录启动失败:" + e.message, "err"); }
}

// ─── 小红书创作者登录(发布用) ───
async function startXhsCreatorLogin() {
  const browserBackend = await choosePreLoginBrowserBackend();
  if (browserBackend === null) return;
  const proxy = await choosePreLoginProxy();
  if (proxy === null) return;
  const fingerprint = await configurePreLoginFingerprint(browserBackend);
  if (fingerprint === null) return;
  $("cookiebox").style.display = "none";
  $("qrbox").style.display = "block";
  $("qrstatus").textContent = "正在打开小红书创作平台窗口…";
  try {
    const res = await api(loginStartUrl("/api/login/xhs-creator/start", proxy, browserBackend), loginStartOptions(fingerprint));
    if (res.reused) {
      $("qrstatus").innerHTML = `${ic("i-eye")} <b>已有小红书创作平台扫码窗口</b>，已尝试切换到前台，请直接在该窗口继续。`;
      toast("已有创作者扫码窗口，已切换到前台", "info");
    } else {
      $("qrstatus").innerHTML = `${ic("i-eye")} <b>小红书创作平台窗口已打开</b>，请扫码登录，此登录态用于发布。<br>登录成功后请稍等片刻再关闭窗口。`;
    }
    pollLogin(res.task_id);
  } catch (e) { $("qrstatus").textContent = "启动失败: " + e.message; toast("创作者登录启动失败:" + e.message, "err"); }
}

// ─── 快手扫码登录 ───
async function startKsLogin() {
  const browserBackend = await choosePreLoginBrowserBackend();
  if (browserBackend === null) return;
  const proxy = await choosePreLoginProxy();
  if (proxy === null) return;
  const fingerprint = await configurePreLoginFingerprint(browserBackend);
  if (fingerprint === null) return;
  $("cookiebox").style.display = "none";
  $("qrbox").style.display = "block";
  $("qrstatus").textContent = "正在打开快手窗口…";
  try {
    const res = await api(loginStartUrl("/api/login/kuaishou/start", proxy, browserBackend), loginStartOptions(fingerprint));
    $("qrstatus").innerHTML = `${ic("i-eye")} <b>快手窗口已打开</b>，请在该窗口点击「登录」并使用快手 App 扫码。<br>完成后这里会自动刷新。`;
    pollLogin(res.task_id);
  } catch (e) { $("qrstatus").textContent = "启动失败: " + e.message; toast("快手登录启动失败:" + e.message, "err"); }
}

// ─── 快手创作者登录(发布用) ───
async function startKsCreatorLogin() {
  const browserBackend = await choosePreLoginBrowserBackend();
  if (browserBackend === null) return;
  const proxy = await choosePreLoginProxy();
  if (proxy === null) return;
  const fingerprint = await configurePreLoginFingerprint(browserBackend);
  if (fingerprint === null) return;
  $("cookiebox").style.display = "none";
  $("qrbox").style.display = "block";
  $("qrstatus").textContent = "正在打开快手创作平台窗口…";
  try {
    const res = await api(loginStartUrl("/api/login/kuaishou-creator/start", proxy, browserBackend), loginStartOptions(fingerprint));
    $("qrstatus").innerHTML = `${ic("i-eye")} <b>快手创作平台窗口已打开</b>，请扫码登录，此登录态用于发布。<br>登录成功后请稍等片刻再关闭窗口。`;
    pollLogin(res.task_id);
  } catch (e) { $("qrstatus").textContent = "启动失败: " + e.message; toast("创作者登录启动失败:" + e.message, "err"); }
}

// ─── 视频号扫码登录(读取/发布共用,微信扫码) ───
async function startChannelsLogin() {
  const browserBackend = await choosePreLoginBrowserBackend();
  if (browserBackend === null) return;
  const proxy = await choosePreLoginProxy();
  if (proxy === null) return;
  const fingerprint = await configurePreLoginFingerprint(browserBackend);
  if (fingerprint === null) return;
  $("cookiebox").style.display = "none";
  $("qrbox").style.display = "block";
  $("qrstatus").textContent = "正在打开视频号助手窗口…";
  try {
    const res = await api(loginStartUrl("/api/login/shipinhao/start", proxy, browserBackend), loginStartOptions(fingerprint));
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
let BROWSER_RUNTIMES = [];
let BROWSER_RUNTIME_ROOT = "";
let MONITORS = [], WATCHES = [], CONTENTS = [];
let COLLECTION_JOBS = [], COLLECTION_JOB_ID = 0, COLLECTION_PAGE = 1;
let DANMAKU_WATCHES = [];
let CHANNELS = [], PUBLISH_TASKS = [];
let CONTENT_SRC = "", CONTENT_GROUP = "", CONTENT_TAG = "";
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
function onContentSrc() { CONTENT_SRC = $("content-src").value; selContent.clear(); refreshContents(true); }
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
    const loginDetails = isXhs
      ? [a.has_read_login ? "读取登录已保存" : "读取登录未配置",
         a.has_creator ? "创作登录已保存" : "创作登录未配置"]
      : [a.has_storage
          ? (a.status === "invalid" ? "登录态已保存但校验失效" : "登录态有效")
          : "无登录态"];
    const detail = [
      a.aweme_count ? a.aweme_count + (isXhs ? " 笔记" : " 作品") : null,
      a.follower_count ? fmtNum(a.follower_count) + " 粉丝" : null,
      isXhs ? "扫码登录" : (a.login_type === "cookie" ? "Cookie 登录" : "扫码登录"),
      ...loginDetails,
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
      ? `<button class="sm" style="background:var(--warn);border-color:transparent;color:#1a1a1a" onclick="relogin(${a.id},'read')">补读取登录</button>`
      : (a.status === "invalid"
          ? `<button class="sm" style="background:var(--warn);border-color:transparent;color:#1a1a1a" onclick="relogin(${a.id})">重新登录</button>`
          : `<button class="ghost sm" onclick="relogin(${a.id})" title="${isXhs ? "重新扫码登录当前授权" : "重新扫码登录"}">重新登录</button>`);
    const creatorLoginButton = isXhs && !a.has_creator
      ? `<button class="ghost sm" onclick="relogin(${a.id},'creator')">补创作登录</button>` : "";
    const accountStatusLabel = a.status === "invalid"
      ? "登录失效" : (isXhs && !a.has_read_login && a.has_creator ? "创作登录正常" : "正常");
    return `<tr>
      <td>
        <div class="user-cell">
          ${a.avatar ? `<img class="avatar" src="${a.avatar}" alt="" referrerpolicy="no-referrer">` : ""}
          <div>
            <div><b>${esc(a.nickname)}</b> ${pill}</div>
            ${idline ? `<div class="mut" style="font-size:11px;margin-top:2px">${idline}</div>` : ""}
            <div class="mut" style="font-size:11px;margin-top:2px">${esc(detail)}</div>
            ${proxyLine}
            ${browserLine}
            ${fingerprintLine}
            ${isolationLine}
            ${checkLine}
          </div>
        </div>
      </td>
      <td><span class="pill ${a.status}">${accountStatusLabel}</span></td>
      <td class="acttd">
        ${reloginButton}
        ${creatorLoginButton}
        <button class="ghost sm" onclick="refreshProfile(${a.id})">刷新资料</button>
        <button class="ghost sm" onclick="openAccountHub(${a.id})" title="查看该账号的作品 / 关注 / 粉丝 / 私信">数据</button>
        <button class="ghost sm" onclick="openAccountBrowser(${a.id})" title="用该账号登录态弹出真实浏览器窗口,手动收发私信 / 维护 / 抓接口(关窗即保存)">打开浏览器</button>
        <button class="ghost sm" onclick="setBrowserBackend(${a.id})" title="选择本地 Chrome/Patchright 或开源 Fingerprint Chromium 内核">环境</button>
        <button class="ghost sm" onclick="manageFingerprint(${a.id})" title="根据账号当前出口 IP 生成稳定指纹、时区、语言和地理位置">指纹</button>
        ${environmentCheck && environmentCheck.enabled ? `<button class="ghost sm" onclick="checkBrowserEnvironment(${a.id})" title="在该账号独立 Profile 中打开 BrowserScan，查看实际 IP、时区、WebRTC 和指纹">环境检测</button>` : ""}
        <button class="ghost sm" onclick="setProxy(${a.id})" title="设置/分配该账号专属代理(防多账号关联)">代理</button>
        ${a.has_proxy ? `<button class="ghost sm" onclick="testProxy(${a.id})" title="经该代理实连一次,验证可用">测代理</button>` : ""}
        <button class="ghost sm danger" onclick="delAccount(${a.id})" aria-label="删除账号">${ic("i-trash")}删除</button>
      </td>
    </tr>`;
  }).join("") || empty(3, "还没有账号", "i-user", "用上方按钮扫码登录,或粘贴 Cookie 添加一个账号");
  if ($("tb-acc")) $("tb-acc").textContent = accs.length;
  populateAccountSelect();
  populateWatchAccount();
  populateCollectionAccount();
  if (PLATFORM === "douyin") applyDanmakuForm();
  else populateDanmakuAccount();
  populatePubAcc();
  populateAcAccount();
  populateHubAccounts();
  const at = document.querySelector('.navitem.active');
  if (at && at.dataset.tab === "hub") refreshHubPanel();
}

// ═══════════ 风控中心 ═══════════
let RISK_ACCOUNTS = [];
let RISK_CONFIG = null;

function riskDate(value) {
  if (!value) return null;
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? null : date;
}
function riskTime(value) {
  const date = riskDate(value);
  return date ? date.toLocaleString() : "—";
}
function riskDuration(seconds) {
  let left = Math.max(0, Math.ceil(Number(seconds) || 0));
  if (!left) return "已到期";
  const days = Math.floor(left / 86400); left %= 86400;
  const hours = Math.floor(left / 3600); left %= 3600;
  const minutes = Math.ceil(left / 60);
  return [days ? `${days}天` : "", hours ? `${hours}小时` : "", minutes ? `${minutes}分钟` : ""].filter(Boolean).join("");
}
function riskRemaining(value) {
  const date = riskDate(value);
  return date ? riskDuration((date.getTime() - Date.now()) / 1000) : "—";
}
function riskPlatformLabel(platform) { return PF_NAME[platform] || platform || "—"; }
const RISK_KIND_LABELS = {
  read_light: "轻量读取", read_heavy: "重读取", download: "下载", publish: "发布",
  comment: "评论", social: "关注操作", dm: "私信", login: "登录",
};
const RISK_OUTCOME_LABELS = {
  success: "成功", risk: "平台风控", auth: "登录失效", network: "网络异常",
  business: "业务异常", manual: "人工操作",
};

async function refreshRiskCenter(force = false) {
  try {
    const shouldFillConfig = !RISK_CONFIG || force;
    const configPromise = shouldFillConfig ? api("/api/risk-control/config") : Promise.resolve(RISK_CONFIG);
    const platformQuery = "?platform=" + encodeURIComponent(PLATFORM);
    const [summary, accounts, config] = await Promise.all([
      api("/api/risk-control/summary" + platformQuery),
      api("/api/risk-control/accounts" + platformQuery),
      configPromise,
    ]);
    RISK_ACCOUNTS = accounts;
    RISK_CONFIG = config;
    renderRiskSummary(summary);
    renderRiskAccounts();
    if (shouldFillConfig) fillRiskConfig(config);
    if (force) toast("风控状态已刷新", "ok");
  } catch (e) {
    if (force || CURRENT_TAB === "risk-control") toast("风控中心加载失败：" + e.message, "err");
  }
}

function renderRiskSummary(summary) {
  const counts = summary.counts || {};
  $("risk-stat-normal").textContent = counts.normal || 0;
  $("risk-stat-cooldown").textContent = (counts.cooldown || 0) + (counts.network_circuit || 0) + (counts.write_paused || 0);
  $("risk-stat-recovering").textContent = counts.recovering || 0;
  $("risk-stat-invalid").textContent = (counts.auth_invalid || 0) + (counts.proxy_error || 0);
  $("risk-stat-blocked").textContent = summary.blocked_tasks || 0;
  $("risk-stat-today").textContent = summary.risk_events_today || 0;
  if ($("tb-risk")) $("tb-risk").textContent = summary.abnormal || 0;
}

function renderRiskAccounts() {
  const tbody = $("risk-account-table");
  if (!tbody) return;
  const query = ($("risk-account-search")?.value || "").trim().toLowerCase();
  const status = $("risk-status-filter")?.value || "";
  const rows = RISK_ACCOUNTS.filter(account => {
    if (status && account.status !== status) return false;
    if (!query) return true;
    return [account.nickname, account.reason, account.proxy, account.status_label]
      .some(value => String(value || "").toLowerCase().includes(query));
  });
  if ($("risk-filter-count")) $("risk-filter-count").textContent = `显示 ${rows.length} / ${RISK_ACCOUNTS.length}`;
  tbody.innerHTML = rows.map(account => {
    const progress = account.risk_level > 0
      ? Math.min(100, Math.round((account.recovery_successes || 0) * 100 / Math.max(1, account.recovery_target || 1)))
      : 100;
    const timing = account.status === "cooldown" || account.status === "network_circuit"
      ? `<b>${esc(riskRemaining(account.cooldown_until))}</b><small>截止 ${esc(riskTime(account.cooldown_until))}</small>`
      : account.status === "recovering"
        ? `<b>下次探测</b><small>${esc(riskTime(account.next_probe_at))}</small>`
        : `<span class="mut">无需等待</span>`;
    const queue = account.queued_tasks || { total: 0 };
    const reason = account.reason || "未检测到风险信号";
    const nextProbe = riskDate(account.next_probe_at);
    const probeWaiting = !!(nextProbe && nextProbe.getTime() > Date.now());
    const actualAccountId = account.platform_account_id
      ? `${account.platform_account_id_label || "账号 ID"} ${account.platform_account_id}`
      : "尚未获取平台账号 ID";
    return `<tr>
      <td><div class="risk-account"><b>${esc(account.nickname || "未命名账号")}</b><small title="${esc(actualAccountId)}">${esc(riskPlatformLabel(account.platform))} · ${esc(actualAccountId)}</small></div></td>
      <td><span class="risk-status ${esc(account.status_tone)}">${esc(account.status_label)}</span></td>
      <td class="num"><b>L${Number(account.risk_level) || 0}</b></td>
      <td><div class="risk-reason"><span title="${esc(reason)}">${esc(reason)}</span><small>${account.last_risk_at ? "触发于 " + esc(riskTime(account.last_risk_at)) : "暂无风险记录"}</small></div></td>
      <td><div class="risk-account">${timing}</div></td>
      <td><div class="risk-progress"><div class="risk-progress-track"><i style="width:${progress}%"></i></div><span>${account.risk_level > 0 ? `${account.recovery_successes}/${account.recovery_target}` : "完成"}</span></div></td>
      <td><b class="num">${account.blocked_tasks || 0} / ${queue.total || 0}</b><div class="mut" style="font-size:11px" title="${esc(account.latest_block_reason || "")}">受阻 / 待执行${account.task_next_allowed_at ? ` · ${esc(riskRemaining(account.task_next_allowed_at))}` : ""}</div></td>
      <td><div class="risk-account"><span>${account.proxy ? `<code>${esc(account.proxy)}</code>` : "本机直连"}</span><small>${esc(account.proxy_status || "unknown")} · ${esc(account.network_key || "—")}</small></div></td>
      <td class="acttd">
        <button class="ghost sm" onclick="probeRiskAccount(${account.account_id})" ${probeWaiting ? "disabled" : ""} title="${probeWaiting ? `下次探测 ${esc(riskTime(account.next_probe_at))}` : "执行一次受风控闸门约束的轻量账号探测"}">${probeWaiting ? "等待探测" : "探测"}</button>
        <button class="ghost sm" onclick="showRiskEvents(${account.account_id})">记录</button>
        <button class="ghost sm" onclick="openAccountBrowser(${account.account_id})">浏览器</button>
        ${account.status !== "normal" ? `<button class="ghost sm danger" onclick="clearRiskAccount(${account.account_id})">解除</button>` : ""}
      </td>
    </tr>`;
  }).join("") || empty(9, "没有匹配的账号状态", "i-shield", "调整筛选条件或先添加平台账号");
}

function fillRiskConfig(config) {
  if (!config) return;
  const r = config.risk_control || {}, s = config.schedule || {};
  const set = (id, value) => { const el = $(id); if (el) { el.value = value ?? ""; if (el._csSync) el._csSync(); } };
  $("risk-enabled").checked = !!r.enabled;
  set("risk-mode", r.mode); set("risk-retention", r.event_retention_days);
  set("risk-read-light", r.read_light_gap_seconds); set("risk-read-heavy", r.read_heavy_gap_seconds);
  set("risk-recovery-count", r.recovery_successes); set("risk-probe-gap", (r.recovery_probe_gap_seconds || 0) / 60);
  set("risk-cooldown-steps", (r.cooldown_steps_seconds || []).map(v => v / 60).join(", "));
  set("risk-network-concurrency", r.network_group_concurrency); set("risk-network-accounts", r.network_group_risk_accounts);
  set("risk-network-window", (r.network_group_risk_window_seconds || 0) / 60); set("risk-network-cooldown", (r.network_group_cooldown_seconds || 0) / 60);
  set("risk-account-check", (s.account_check_interval_seconds || 0) / 60); set("risk-captcha-wait", (s.douyin_captcha_wait_seconds || 0) / 60);
  $("risk-quiet-enabled").checked = !!s.quiet_hours_enabled;
  set("risk-active-start", s.active_hours_start); set("risk-active-end", s.active_hours_end);
  [["comment", "comment"], ["social", "social"], ["dm", "dm"], ["publish", "publish"]].forEach(([id, key]) => {
    set(`risk-${id}-gap`, (r[`${key}_min_gap_seconds`] || 0) / 60);
    set(`risk-${id}-hourly`, r[`${key}_hourly_cap`]); set(`risk-${id}-daily`, r[`${key}_daily_cap`]);
  });
  set("risk-shared-write", (r.shared_write_gap_seconds || 0) / 60);
  set("risk-combined-hourly", r.combined_action_hourly_cap); set("risk-combined-daily", r.combined_action_daily_cap);
  if ($("risk-admin-token-btn")) {
    $("risk-admin-token-btn").style.display = config.admin_token_required ? "" : "none";
    let configured = false;
    try { configured = !!sessionStorage.getItem("creatorhub-risk-admin-token"); } catch (e) {}
    $("risk-admin-token-btn").innerHTML = `${ic("i-shield")}${configured ? "管理口令已设置" : "设置管理口令"}`;
  }
}

async function setRiskAdminToken() {
  const token = await uiPrompt({
    title: "设置本次会话的风控管理口令",
    hint: "口令只保存在当前浏览器标签会话中。留空会清除已经保存的口令。",
    placeholder: "CREATORHUB_ADMIN_TOKEN", secret: true,
  });
  if (token === null) return;
  try {
    if (token.trim()) sessionStorage.setItem("creatorhub-risk-admin-token", token.trim());
    else sessionStorage.removeItem("creatorhub-risk-admin-token");
  } catch (e) {}
  fillRiskConfig(RISK_CONFIG);
  toast(token.trim() ? "管理口令已保存到当前会话" : "管理口令已清除", "ok");
}

function riskNumber(id, multiplier = 1) {
  const value = Number($(id).value);
  if (!Number.isFinite(value) || value < Number($(id).min || 0)) throw new Error($(id).labels?.[0]?.textContent + "填写不正确");
  return Math.round(value * multiplier);
}

async function saveRiskConfig() {
  const msg = $("risk-config-msg");
  const restore = btnLoading(evtBtn(), "保存中");
  INFLIGHT++; _barSync();
  try {
    const steps = $("risk-cooldown-steps").value.split(/[,，\s]+/).filter(Boolean).map(Number);
    if (!steps.length || steps.some(v => !Number.isFinite(v) || v <= 0)) throw new Error("冷却阶梯需要填写有效的分钟数");
    const r = {
      enabled: $("risk-enabled").checked, mode: $("risk-mode").value,
      network_group_concurrency: riskNumber("risk-network-concurrency"),
      read_light_gap_seconds: riskNumber("risk-read-light"), read_heavy_gap_seconds: riskNumber("risk-read-heavy"),
      shared_write_gap_seconds: riskNumber("risk-shared-write", 60),
      cooldown_steps_seconds: steps.map(v => Math.round(v * 60)),
      recovery_successes: riskNumber("risk-recovery-count"), recovery_probe_gap_seconds: riskNumber("risk-probe-gap", 60),
      event_retention_days: riskNumber("risk-retention"),
      network_group_risk_accounts: riskNumber("risk-network-accounts"),
      network_group_risk_window_seconds: riskNumber("risk-network-window", 60),
      network_group_cooldown_seconds: riskNumber("risk-network-cooldown", 60),
      combined_action_hourly_cap: riskNumber("risk-combined-hourly"), combined_action_daily_cap: riskNumber("risk-combined-daily"),
    };
    ["comment", "social", "dm", "publish"].forEach(key => {
      r[`${key}_min_gap_seconds`] = riskNumber(`risk-${key}-gap`, 60);
      r[`${key}_hourly_cap`] = riskNumber(`risk-${key}-hourly`);
      r[`${key}_daily_cap`] = riskNumber(`risk-${key}-daily`);
    });
    const schedule = {
      quiet_hours_enabled: $("risk-quiet-enabled").checked,
      active_hours_start: riskNumber("risk-active-start"), active_hours_end: riskNumber("risk-active-end"),
      account_check_interval_seconds: riskNumber("risk-account-check", 60),
      douyin_captcha_wait_seconds: riskNumber("risk-captcha-wait", 60),
    };
    msg.textContent = "保存中…";
    RISK_CONFIG = await api("/api/risk-control/config", {
      method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ risk_control: r, schedule }),
    });
    fillRiskConfig(RISK_CONFIG); msg.textContent = "已保存并生效"; toast("风控规则已保存并立即生效", "ok");
    await refreshRiskCenter();
  } catch (e) { msg.textContent = e.message; toast("保存失败：" + e.message, "err"); }
  finally { INFLIGHT--; restore(); _barSync(); }
}

async function probeRiskAccount(accountId) {
  const button = evtBtn();
  await withBusy(button, "探测中", async () => {
    try {
      const response = await api(`/api/risk-control/accounts/${accountId}/probe`, { method: "POST" });
      const result = response.result || {};
      if (result.skipped) toast("当前尚未放行探测：" + (result.reason || "仍处于冷却期"), "info", 7000);
      else toast(`轻量探测成功，恢复进度已更新${response.woken_tasks ? `，已唤醒 ${response.woken_tasks} 条任务` : ""}`, "ok");
    } catch (e) { toast("探测失败：" + e.message, "err", 7000); }
    await refreshRiskCenter();
  });
}

async function clearRiskAccount(accountId) {
  const nickname = RISK_ACCOUNTS.find(account => account.account_id === accountId)?.nickname || `账号 ${accountId}`;
  const reason = await uiPrompt({
    title: "填写解除原因",
    hint: `请说明已对「${nickname}」完成的人工检查。该内容会进入审计记录。`,
    placeholder: "例如：已完成验证码并确认代理出口正常", multiline: true, rows: 4,
  });
  if (reason === null) return;
  if (reason.trim().length < 3) { toast("请填写至少 3 个字符的解除原因", "err"); return; }
  if (!await uiConfirm({ title: "解除账号风控状态", message: `请确认已人工检查「${nickname}」的登录态、验证码和网络出口。解除后待执行任务可能继续运行。`, okText: "确认解除", danger: true })) return;
  try {
    const result = await api(`/api/risk-control/accounts/${accountId}/clear`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ confirmed: true, reason: reason.trim() }),
    });
    toast(`账号风控状态已解除${result.woken_tasks ? `，已唤醒 ${result.woken_tasks} 条任务` : ""}`, "ok"); await refreshRiskCenter();
  } catch (e) { toast("解除失败：" + e.message, "err"); }
}

async function showRiskEvents(accountId) {
  const nickname = RISK_ACCOUNTS.find(account => account.account_id === accountId)?.nickname || `账号 ${accountId}`;
  const modal = $("risk-event-modal");
  $("risk-event-title").textContent = `${nickname || "账号"} · 风险事件`;
  $("risk-event-subtitle").textContent = "正在加载最近事件…";
  $("risk-event-list").innerHTML = '<div class="hint">正在加载事件记录…</div>';
  modal.style.display = "flex"; modalOpened(modal);
  try {
    const data = await api(`/api/risk-control/accounts/${accountId}/events?limit=100`);
    $("risk-event-subtitle").textContent = `最近 ${data.events.length} 条 · 不保存响应正文或账号凭据`;
    $("risk-event-list").innerHTML = data.events.map(event => `<div class="risk-event">
      <time>${esc(riskTime(event.occurred_at))}</time>
      <span class="risk-status ${event.outcome === "success" ? "success" : event.outcome === "manual" || event.outcome === "business" ? "warn" : "danger"}">${esc(RISK_OUTCOME_LABELS[event.outcome] || event.outcome)}</span>
      <b>${esc(RISK_KIND_LABELS[event.operation_kind] || event.operation_kind)}</b>
      <div class="risk-event-detail">${esc(event.detail || event.signal || "无补充说明")}<small>${esc(event.signal || "—")} · ${esc(event.network_key || "—")}</small></div>
    </div>`).join("") || '<div class="hint">暂无风险事件；账号发生平台操作后会在这里形成记录。</div>';
  } catch (e) { $("risk-event-list").innerHTML = `<div class="hint">加载失败：${esc(e.message)}</div>`; }
}
function hideRiskEvents() { const modal = $("risk-event-modal"); modal.style.display = "none"; modalClosed(modal); }
async function showRiskAudit() {
  const modal = $("risk-event-modal");
  $("risk-event-title").textContent = "风控管理变更记录";
  $("risk-event-subtitle").textContent = "正在加载审计记录…";
  $("risk-event-list").innerHTML = '<div class="hint">正在加载变更记录…</div>';
  modal.style.display = "flex"; modalOpened(modal);
  try {
    const rows = await api("/api/risk-control/audit?limit=100");
    $("risk-event-subtitle").textContent = `最近 ${rows.length} 条 · 包含规则修改、人工探测和解除操作`;
    const labels = { policy_updated: "规则修改", manual_probe: "人工探测", account_risk_cleared: "人工解除" };
    $("risk-event-list").innerHTML = rows.map(row => {
      const detail = row.detail || {};
      const changeCount = Object.values(detail.changes || {}).reduce((sum, section) => sum + Object.keys(section || {}).length, 0);
      const summary = detail.reason || (changeCount ? `修改 ${changeCount} 项规则` : detail.skipped ? `探测延后：${detail.reason || "风控闸门未放行"}` : "操作完成");
      return `<div class="risk-event"><time>${esc(riskTime(row.created_at))}</time><span class="risk-status warn">${esc(labels[row.action] || row.action)}</span><b>${row.account_id ? `账号 ${row.account_id}` : "全局"}</b><div class="risk-event-detail">${esc(summary)}<small>${esc(row.actor || "local-ui")}</small></div></div>`;
    }).join("") || '<div class="hint">暂无风控管理变更记录。</div>';
  } catch (e) { $("risk-event-list").innerHTML = `<div class="hint">加载失败：${esc(e.message)}</div>`; }
}
document.addEventListener("keydown", event => {
  if (event.key === "Escape" && $("risk-event-modal")?.style.display !== "none") hideRiskEvents();
});

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
      const result = await api("/api/accounts/" + id + "/open-browser", { method: "POST" });
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
function populateCollectionAccount() {
  const sel = $("col-account"); if (!sel) return;
  const current = sel.value;
  const list = ACCOUNTS.filter(a => a.platform === "douyin" && a.status !== "invalid" && a.has_storage);
  sel.innerHTML = accOptions(list, list.length ? "请选择抖音账号" : "暂无可用抖音账号");
  if (list.some(a => String(a.id) === current)) sel.value = current;
  else if (list.length) sel.value = String(list[0].id);
  if (sel._csSync) sel._csSync();
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
  await withBusy(btn, "\u83b7\u53d6\u4e2d", async () => {
    try {
      const r = await api("/api/accounts/" + id + "/refresh-profile", { method: "POST" });
      if (r.skipped) {
        toast("\u672c\u6b21\u672a\u6267\u884c\u8d44\u6599\u5237\u65b0:" + (r.reason || "\u8d26\u53f7\u5f53\u524d\u4e0d\u53ef\u63a2\u6d4b"), "info");
        return;
      }
      if ((r.platform || PLATFORM) === "xhs" && r.login_scope === "creator" && !r.has_read_login) {
        toast("创作平台登录有效，资料已更新；主站读取登录尚未配置", "info", 8000);
        return;
      }
      const idLbl = (r.platform || PLATFORM) === "xhs" ? " \u00b7 \u5c0f\u7ea2\u4e66\u53f7 " : " \u00b7 \u6296\u97f3\u53f7 ";
      toast("\u8d44\u6599\u5df2\u66f4\u65b0\uff0c\u767b\u5f55\u72b6\u6001\u5df2\u6062\u590d:" + (r.nickname || "") + (r.douyin_id ? idLbl + r.douyin_id : ""), "ok");
    } catch (e) {
      toast("\u5237\u65b0\u5931\u8d25:" + e.message, "err");
    }
  });
  await refreshAccounts();
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

async function setBrowserBackend(id) {
  const account = ACCOUNTS.find(item => item.id === id);
  if (!account) return;
  let catalog;
  try {
    catalog = await api("/api/browser-backends");
  } catch (e) {
    toast("读取浏览器环境失败:" + e.message, "err");
    return;
  }
  const options = browserChoiceOptions(catalog);
  const currentChoice = account.browser_backend === "fingerprint_chromium" && account.browser_runtime_id
    ? `fingerprint_chromium::${account.browser_runtime_id}`
    : (account.browser_backend || "default");
  const selected = await uiSelect({
    title: "账号浏览器环境",
    hint: `${account.nickname} · 切换时会关闭该账号当前浏览器，下次任务使用新内核。`,
    options,
    value: currentChoice,
  });
  if (selected === null) return;
  try {
    const choice = browserChoiceParts(selected);
    const result = await api(`/api/accounts/${id}/browser-backend`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        browser_backend: choice.backend,
        browser_runtime_id: choice.runtimeId,
      }),
    });
    toast("浏览器环境已切换：" + loginEnvironmentText(result.environment), "ok");
    refreshAccounts();
  } catch (e) {
    toast("切换失败:" + e.message, "err");
  }
}

async function refreshBrowserRuntimes() {
  const table = $("runtime-table");
  if (!table) return;
  try {
    const result = await api("/api/browser-runtimes");
    BROWSER_RUNTIMES = result.runtimes || [];
    let rememberedRoot = "";
    try { rememberedRoot = localStorage.getItem("creatorhub-browser-runtime-root") || ""; } catch (e) {}
    BROWSER_RUNTIME_ROOT = rememberedRoot || result.root || "";
    const rootLabel = $("runtime-root");
    if (rootLabel) rootLabel.textContent = BROWSER_RUNTIME_ROOT || "尚未设置，扫描时输入";
    table.querySelector("tbody").innerHTML = BROWSER_RUNTIMES.map(runtime => {
      const state = !runtime.enabled ? "已停用" : runtime.available
        ? (runtime.status === "ok" ? "测试通过" : "可用") : "文件缺失";
      const stateClass = runtime.enabled && runtime.available
        ? "active" : runtime.status === "bad" ? "invalid" : "bare";
      return `<tr>
        <td>
          <div><b>${esc(runtime.name || runtime.runtime_id)}</b>
            ${runtime.is_default ? '<span class="pill active">默认</span>' : ""}
            <span class="pill ${stateClass}">${esc(state)}</span>
          </div>
          <div class="mut" style="font-size:11px;margin-top:3px">版本 ${esc(runtime.version || "未知")} · ${esc(runtime.runtime_id)}</div>
          <div class="mut" style="font-size:11px;margin-top:3px;word-break:break-all"><code>${esc(runtime.executable_path)}</code></div>
          ${runtime.last_error ? `<div style="font-size:11px;margin-top:3px;color:var(--danger)">${esc(runtime.last_error)}</div>` : ""}
        </td>
        <td class="acttd">
          <button class="ghost sm" onclick="testBrowserRuntime('${esc(runtime.runtime_id)}')">测试启动</button>
          ${runtime.is_default ? "" : `<button class="ghost sm" onclick="setDefaultBrowserRuntime('${esc(runtime.runtime_id)}')" ${runtime.enabled ? "" : "disabled"}>设为默认</button>`}
          <button class="ghost sm" onclick="toggleBrowserRuntime('${esc(runtime.runtime_id)}', ${runtime.enabled ? "false" : "true"})">${runtime.enabled ? "停用" : "启用"}</button>
          <button class="ghost sm danger" onclick="deleteBrowserRuntime('${esc(runtime.runtime_id)}')">${ic("i-trash")}移除</button>
        </td>
      </tr>`;
    }).join("") || empty(2, "尚未发现指纹内核", "i-inbox", "点击扫描目录，或手动添加 chrome.exe");
  } catch (e) {
    table.querySelector("tbody").innerHTML = empty(2, "读取内核列表失败", "i-info", e.message);
  }
}

async function scanBrowserRuntimes() {
  const button = evtBtn();
  const root = await uiPrompt({
    title: "扫描 Chromium 内核目录",
    hint: "输入当前机器上存放一个或多个 Chromium 版本的目录。路径按本机配置，不限定盘符或操作系统。",
    value: BROWSER_RUNTIME_ROOT,
    placeholder: "例如：浏览器安装目录或统一内核目录",
  });
  if (root === null) return;
  if (!root.trim()) { toast("请输入要扫描的目录", "err"); return; }
  await withBusy(button, "扫描中", async () => {
    try {
      const result = await api("/api/browser-runtimes/scan", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ root: root.trim() }),
      });
      BROWSER_RUNTIME_ROOT = result.root || root.trim();
      try { localStorage.setItem("creatorhub-browser-runtime-root", BROWSER_RUNTIME_ROOT); } catch (e) {}
      toast(`扫描完成：发现 ${result.found} 个内核，新增 ${result.created} 个`, "ok");
      await refreshBrowserRuntimes();
    } catch (e) { toast("扫描失败：" + e.message, "err"); }
  });
}

async function addBrowserRuntime() {
  const path = await uiPrompt({
    title: "添加 Chromium 内核",
    hint: "填写当前机器上 chrome/chromium 主程序的完整路径。程序只记录路径，不复制或移动浏览器文件。",
    value: "",
    placeholder: "chrome.exe、chrome 或 chromium 的完整路径",
  });
  if (path === null || !path.trim()) return;
  try {
    const result = await api("/api/browser-runtimes", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ executable_path: path.trim() }),
    });
    toast(`${result.created ? "已添加" : "已更新"}：${result.runtime.name}`, "ok");
    await refreshBrowserRuntimes();
  } catch (e) { toast("添加失败：" + e.message, "err"); }
}

async function testBrowserRuntime(runtimeId) {
  const button = evtBtn();
  await withBusy(button, "测试中", async () => {
    try {
      const result = await api(`/api/browser-runtimes/${encodeURIComponent(runtimeId)}/test`, { method: "POST" });
      toast(`内核启动正常：${result.user_agent || runtimeId}`, "ok", 7000);
    } catch (e) { toast("内核测试失败：" + e.message, "err", 7000); }
    await refreshBrowserRuntimes();
  });
}

async function setDefaultBrowserRuntime(runtimeId) {
  try {
    await api(`/api/browser-runtimes/${encodeURIComponent(runtimeId)}`, {
      method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ is_default: true }),
    });
    toast("默认指纹内核已切换", "ok");
    await refreshBrowserRuntimes(); await refreshAccounts();
  } catch (e) { toast("切换失败：" + e.message, "err"); }
}

async function toggleBrowserRuntime(runtimeId, enabled) {
  try {
    await api(`/api/browser-runtimes/${encodeURIComponent(runtimeId)}`, {
      method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled }),
    });
    toast(enabled ? "内核已启用" : "内核已停用", "ok");
    await refreshBrowserRuntimes(); await refreshAccounts();
  } catch (e) { toast("操作失败：" + e.message, "err"); }
}

async function deleteBrowserRuntime(runtimeId) {
  if (!await uiConfirm({
    title: "移除内核记录",
    message: "仅移除 CreatorHub 中的内核记录，不会删除原安装目录中的浏览器文件。",
    okText: "移除",
  })) return;
  try {
    await api(`/api/browser-runtimes/${encodeURIComponent(runtimeId)}`, { method: "DELETE" });
    toast("内核记录已移除", "ok");
    await refreshBrowserRuntimes();
  } catch (e) { toast("移除失败：" + e.message, "err"); }
}

function uiFingerprintEditor(account, fp, options = {}) {
  const preLogin = Boolean(options.preLogin);
  const disabled = new Set(fp.disable_spoofing || []);
  const runtimeVersion = String((account.environment || {}).runtime_version || "");
  const runtimeMajor = Number(runtimeVersion.split(".")[0]) || 0;
  const gpuCustomSupported = runtimeMajor >= 139 && runtimeMajor < 144;
  const modeButtons = (name, options, current) => `<div class="fp-segment" data-fp-mode="${esc(name)}" role="group">` +
    options.map(option => `<button type="button" class="${option.value === current ? "active" : ""}" data-value="${esc(option.value)}" aria-pressed="${option.value === current ? "true" : "false"}"${option.disabled ? " disabled" : ""}>${esc(option.label)}</button>`).join("") + `</div>`;
  const surfaceMode = (name, label, custom = false) => {
    let current = disabled.has(name) ? "off" : "random";
    if (custom && gpuCustomSupported && (fp.gpu_vendor || fp.gpu_renderer)) current = "custom";
    const options = [{ value: "random", label: "随机" }];
    if (custom) options.push({ value: "custom", label: "自定义", disabled: !gpuCustomSupported });
    options.push({ value: "off", label: "关闭" });
    return `<div class="fp-config-row"><div><b>${esc(label)}</b><span>每个账号使用稳定且不同的取值</span></div>${modeButtons(name, options, current)}</div>`;
  };
  const platformMode = fp.platform ? "custom" : "auto";
  const brandMode = fp.brand ? "custom" : "auto";
  const cpuMode = Number(fp.hardware_concurrency) > 0 ? "custom" : "auto";
  return new Promise(resolve => {
    _uiResolve = resolve; _uiCancelVal = null;
    $("ui-body").innerHTML = `
      <div class="hint" style="margin:0 0 14px">${preLogin
        ? "自动项会在启动前根据所选代理或本机出口 IP 生成；切换为自定义后可在第一次登录前逐项覆盖。登录成功后，此配置会随账号和独立 Profile 一起保存。"
        : "自动项会跟随来源 IP 生成；切换为自定义后可逐项编辑。保存后关闭该账号当前浏览器，下次启动应用新配置。"}</div>
      <div class="fp-edit-tabs" role="tablist" aria-label="浏览器指纹设置">
        <button type="button" class="ghost sm active" role="tab" aria-selected="true" data-fp-tab="basic">基础设置</button>
        <button type="button" class="ghost sm" role="tab" aria-selected="false" data-fp-tab="advanced">高级设置</button>
      </div>
      <div class="fp-edit-panel active" data-fp-panel="basic">
        <div class="fp-runtime-summary">
          <div><span>浏览器内核</span><b>${esc(runtimeVersion || "跟随所选运行时")}</b></div>
          <div><span>设备类型</span><b>桌面设备</b></div>
          <div><span>指纹编号</span><b>${esc(fp.fingerprint_id || "-")}</b></div>
        </div>
        <div class="fp-settings">
          <div class="fp-config-row"><div><b>操作系统</b><span>影响 navigator.platform 与 Client Hints</span></div>${modeButtons("platform", [{value:"auto",label:"自动"},{value:"custom",label:"自定义"}], platformMode)}</div>
          <div class="form-grid fp-mode-fields" data-fp-custom="platform">
            <div class="form-field"><label for="fp-edit-platform">系统类型</label><select id="fp-edit-platform"><option value="windows"${fp.platform === "windows" ? " selected" : ""}>Windows</option><option value="macos"${fp.platform === "macos" ? " selected" : ""}>macOS</option><option value="linux"${fp.platform === "linux" ? " selected" : ""}>Linux</option></select></div>
            <div class="form-field"><label for="fp-edit-platform-version">系统版本</label><input id="fp-edit-platform-version" value="${esc(fp.platform_version || "")}" placeholder="例如 10.0.19045"></div>
          </div>
          <div class="fp-config-row"><div><b>浏览器品牌与版本</b><span>影响 User-Agent 与 UA Data</span></div>${modeButtons("brand", [{value:"auto",label:"自动"},{value:"custom",label:"自定义"}], brandMode)}</div>
          <div class="form-grid fp-mode-fields" data-fp-custom="brand">
            <div class="form-field"><label for="fp-edit-brand">浏览器品牌</label><input id="fp-edit-brand" value="${esc(fp.brand || "")}" placeholder="Chrome / Edge"></div>
            <div class="form-field"><label for="fp-edit-brand-version">浏览器版本</label><input id="fp-edit-brand-version" value="${esc(fp.brand_version || "")}" placeholder="例如 148.0.0.0"></div>
          </div>
          ${fp.actual_ua ? `<div class="fp-readonly"><span>User Agent</span><code>${esc(fp.actual_ua)}</code></div>` : ""}
          <div class="fp-config-row"><div><b>语言</b><span>可跟随来源 IP 自动匹配</span></div>${modeButtons("language", [{value:"auto",label:"跟随 IP"},{value:"custom",label:"自定义"}], fp.language_mode || "auto")}</div>
          <div class="form-grid fp-mode-fields" data-fp-custom="language">
            <div class="form-field"><label for="fp-edit-locale">浏览器语言</label><input id="fp-edit-locale" value="${esc(fp.locale || "zh-CN")}"></div>
            <div class="form-field"><label for="fp-edit-accept">Accept-Language</label><input id="fp-edit-accept" value="${esc(fp.accept_languages || "")}" placeholder="例如 zh-CN,zh"></div>
          </div>
          <div class="fp-config-row"><div><b>时区</b><span>可跟随来源 IP 匹配 IANA 时区</span></div>${modeButtons("timezone", [{value:"auto",label:"跟随 IP"},{value:"custom",label:"自定义"}], fp.timezone_mode || "auto")}</div>
          <div class="fp-mode-fields" data-fp-custom="timezone"><div class="form-field"><label for="fp-edit-timezone">IANA 时区</label><input id="fp-edit-timezone" value="${esc(fp.timezone || "Asia/Shanghai")}"></div></div>
          <div class="fp-config-row"><div><b>WebRTC</b><span>隐藏模式阻止非代理 UDP 暴露本机 IP</span></div>${modeButtons("webrtc", [{value:"conceal",label:"隐藏"},{value:"allow",label:"允许"}], fp.webrtc_mode || "conceal")}</div>
          <div class="fp-config-row"><div><b>地理位置权限</b><span>控制网站读取浏览器定位的权限</span></div>${modeButtons("geo-permission", [{value:"ask",label:"询问"},{value:"allow",label:"允许"},{value:"deny",label:"禁止"}], fp.geolocation_permission || "allow")}</div>
          <div class="fp-config-row"><div><b>地理位置数据</b><span>可跟随来源 IP 自动生成坐标</span></div>${modeButtons("location", [{value:"auto",label:"跟随 IP"},{value:"custom",label:"自定义"}], fp.location_mode || "auto")}</div>
          <div class="form-grid fp-mode-fields" data-fp-custom="location">
            <div class="form-field"><label for="fp-edit-ip">来源 IP</label><input id="fp-edit-ip" value="${esc(fp.source_ip || "")}"></div>
            <div class="form-field"><label for="fp-edit-country">国家/地区</label><input id="fp-edit-country" value="${esc(fp.country || "")}"></div>
            <div class="form-field"><label for="fp-edit-region">区域</label><input id="fp-edit-region" value="${esc(fp.region || "")}"></div>
            <div class="form-field"><label for="fp-edit-city">城市</label><input id="fp-edit-city" value="${esc(fp.city || "")}"></div>
            <div class="form-field"><label for="fp-edit-lat">纬度</label><input id="fp-edit-lat" type="number" min="-90" max="90" step="0.000001" value="${Number(fp.geo_lat) || 0}"></div>
            <div class="form-field"><label for="fp-edit-lon">经度</label><input id="fp-edit-lon" type="number" min="-180" max="180" step="0.000001" value="${Number(fp.geo_lon) || 0}"></div>
          </div>
          <div class="fp-config-row"><div><b>窗口尺寸</b><span>设置浏览器窗口打开时的大小</span></div>${modeButtons("viewport", [{value:"auto",label:"自动"},{value:"custom",label:"自定义"}], fp.viewport_mode || "auto")}</div>
          <div class="form-grid fp-mode-fields" data-fp-custom="viewport">
            <div class="form-field"><label for="fp-edit-vw">宽度</label><input id="fp-edit-vw" type="number" min="320" max="7680" value="${Number(fp.viewport_w) || 1280}"></div>
            <div class="form-field"><label for="fp-edit-vh">高度</label><input id="fp-edit-vh" type="number" min="240" max="4320" value="${Number(fp.viewport_h) || 800}"></div>
          </div>
        </div>
      </div>
      <div class="fp-edit-panel" data-fp-panel="advanced">
        <div class="fp-settings">
          <div class="fp-config-row"><div><b>指纹种子</b><span>Canvas、Audio 等随机值由种子稳定派生</span></div><div class="fp-seed-badge">${preLogin ? "内核启动时生成" : `uint32 ${esc(String(fp.engine_seed ?? ""))}`}</div></div>
          <div class="form-field"><label for="fp-edit-seed">种子值</label><input id="fp-edit-seed" value="${esc(fp.seed || "")}" maxlength="128"></div>
          ${surfaceMode("font", "字体")}
          ${surfaceMode("canvas", "Canvas")}
          ${surfaceMode("gpu", "WebGL / GPU", true)}
          <div class="form-grid fp-mode-fields" data-fp-custom="gpu">
            <div class="form-field"><label for="fp-edit-gpu-vendor">WebGL Vendor</label><input id="fp-edit-gpu-vendor" value="${esc(fp.gpu_vendor || "")}" placeholder="例如 Google Inc. (Intel)"></div>
            <div class="form-field"><label for="fp-edit-gpu-renderer">WebGL Renderer</label><input id="fp-edit-gpu-renderer" value="${esc(fp.gpu_renderer || "")}" placeholder="仅 139–143 内核支持"></div>
          </div>
          ${surfaceMode("audio", "AudioContext")}
          ${surfaceMode("clientrects", "ClientRects")}
          <div class="fp-config-row"><div><b>硬件并发数</b><span>navigator.hardwareConcurrency</span></div>${modeButtons("cpu", [{value:"auto",label:"自动"},{value:"custom",label:"自定义"}], cpuMode)}</div>
          <div class="fp-mode-fields" data-fp-custom="cpu"><div class="form-field"><label for="fp-edit-cpu">CPU 逻辑核心</label><input id="fp-edit-cpu" type="number" min="1" max="256" value="${Number(fp.hardware_concurrency) || 8}"></div></div>
          <div class="form-field"><label for="fp-edit-extra">附加启动参数</label><textarea id="fp-edit-extra" rows="3" placeholder="每行一个安全参数，例如 --mute-audio">${esc(fp.extra_args || "")}</textarea><div class="mut" style="font-size:11px">仅接受安全的 Chromium 参数；代理、调试端口、用户目录和指纹核心参数由系统统一管理。</div></div>
        </div>
      </div>
      `;
    enhanceAllSelects($("ui-body"));
    const body = $("ui-body");
    const selectedMode = name => {
      const active = body.querySelector(`[data-fp-mode="${name}"] button.active`);
      return active ? active.dataset.value : "";
    };
    const syncCustomFields = name => {
      const enabled = selectedMode(name) === "custom";
      body.querySelectorAll(`[data-fp-custom="${name}"] input,[data-fp-custom="${name}"] select,[data-fp-custom="${name}"] textarea`).forEach(control => {
        control.disabled = !enabled;
        if (control._csSync) control._csSync();
      });
      body.querySelectorAll(`[data-fp-custom="${name}"]`).forEach(group => group.classList.toggle("enabled", enabled));
    };
    body.querySelectorAll("[data-fp-mode] button").forEach(button => {
      button.addEventListener("click", () => {
        if (button.disabled) return;
        const segment = button.closest("[data-fp-mode]");
        segment.querySelectorAll("button").forEach(item => {
          const active = item === button;
          item.classList.toggle("active", active);
          item.setAttribute("aria-pressed", active ? "true" : "false");
        });
        syncCustomFields(segment.dataset.fpMode);
      });
    });
    ["platform", "brand", "language", "timezone", "location", "viewport", "gpu", "cpu"].forEach(syncCustomFields);
    body.querySelectorAll("[data-fp-tab]").forEach(tab => {
      tab.addEventListener("click", () => {
        const selected = tab.dataset.fpTab;
        body.querySelectorAll("[data-fp-tab]").forEach(item => {
          const active = item.dataset.fpTab === selected;
          item.classList.toggle("active", active);
          item.setAttribute("aria-selected", active ? "true" : "false");
        });
        body.querySelectorAll("[data-fp-panel]").forEach(panel => panel.classList.toggle("active", panel.dataset.fpPanel === selected));
        body.scrollTo({ top: 0, behavior: "smooth" });
      });
    });
    const value = id => ($(id) || {}).value || "";
    const number = (id, fallback = 0) => { const parsed = Number(value(id)); return Number.isFinite(parsed) ? parsed : fallback; };
    _uiGetVal = () => {
      const platformCustom = selectedMode("platform") === "custom";
      const brandCustom = selectedMode("brand") === "custom";
      const gpuCustom = selectedMode("gpu") === "custom";
      const surfaces = ["font", "audio", "canvas", "clientrects", "gpu"];
      return { action: "save", data: {
        seed: value("fp-edit-seed"), source_ip: value("fp-edit-ip"),
        country: value("fp-edit-country"), region: value("fp-edit-region"), city: value("fp-edit-city"),
        timezone: value("fp-edit-timezone"), locale: value("fp-edit-locale"), accept_languages: value("fp-edit-accept"),
        viewport_w: number("fp-edit-vw", 1280), viewport_h: number("fp-edit-vh", 800),
        geo_lat: number("fp-edit-lat"), geo_lon: number("fp-edit-lon"),
        platform: platformCustom ? value("fp-edit-platform") : "",
        platform_version: platformCustom ? value("fp-edit-platform-version") : "",
        brand: brandCustom ? value("fp-edit-brand") : "",
        brand_version: brandCustom ? value("fp-edit-brand-version") : "",
        hardware_concurrency: selectedMode("cpu") === "custom" ? number("fp-edit-cpu", 8) : 0,
        gpu_vendor: gpuCustom ? value("fp-edit-gpu-vendor") : "",
        gpu_renderer: gpuCustom ? value("fp-edit-gpu-renderer") : "",
        disable_spoofing: surfaces.filter(name => selectedMode(name) === "off"),
        language_mode: selectedMode("language") || "auto",
        timezone_mode: selectedMode("timezone") || "auto",
        viewport_mode: selectedMode("viewport") || "auto",
        location_mode: selectedMode("location") || "auto",
        geolocation_permission: selectedMode("geo-permission") || "allow",
        webrtc_mode: selectedMode("webrtc") || "conceal",
        extra_args: value("fp-edit-extra"),
      }};
    };
    _uiOpen(
      `${account.nickname} · ${preLogin ? "登录前指纹配置" : "浏览器指纹"}`,
      preLogin ? "确认后使用这套指纹创建独立登录环境" : `指纹 ${fp.fingerprint_id || "-"}`,
      { okText: preLogin ? "使用此指纹登录" : "保存配置", wide: true },
    );
    const autoButton = document.createElement("button");
    autoButton.id = "ui-extra-action";
    autoButton.type = "button";
    autoButton.className = "ghost";
    autoButton.textContent = preLogin ? "使用出口 IP 自动配置" : "按 IP 自动生成";
    autoButton.addEventListener("click", () => _uiClose({ action: "auto" }));
    $("ui-actions").insertBefore(autoButton, $("ui-actions").firstElementChild);
  });
}

async function manageFingerprint(id) {
  const account = ACCOUNTS.find(item => item.id === id);
  if (!account) return;
  let current;
  try { current = await api(`/api/accounts/${id}/fingerprint`); }
  catch (e) { toast("读取指纹失败:" + e.message, "err"); return; }
  const action = await uiFingerprintEditor(account, current);
  if (!action) return;
  if (action.action === "auto") {
    const confirmed = await uiConfirm({
      title: "恢复自动指纹",
      message: "将根据账号当前代理或本机出口 IP 重新计算地域、时区、语言、窗口与种子派生项，并清除手动覆盖。",
      okText: "恢复自动",
    });
    if (!confirmed) return;
    try {
      const result = await api(`/api/accounts/${id}/fingerprint/from-ip`, { method: "POST" });
      toast(`已恢复自动指纹：${result.fingerprint.fingerprint_id || "-"}`, "ok", 7000);
      await refreshAccounts();
    } catch (e) { toast("自动生成失败:" + e.message, "err", 8000); }
    return;
  }
  try {
    const result = await api(`/api/accounts/${id}/fingerprint`, {
      method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(action.data),
    });
    toast(`指纹已保存：${result.fingerprint.fingerprint_id || "-"}`, "ok", 7000);
    await refreshAccounts();
  } catch (e) { toast("保存指纹失败:" + e.message, "err", 8000); }
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
async function relogin(id, scope = "auto") {
  const btn = evtBtn();
  await withBusy(btn, "启动中", async () => {
    try {
      const res = await api("/api/accounts/" + id + "/relogin/start?scope=" + encodeURIComponent(scope), { method: "POST" });
      const label = res.login_scope === "creator" ? "创作平台" : "主站读取";
      toast("已打开" + label + "浏览器窗口,请扫码登录该账号", "info");
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
          const suffix = ["error", "deferred"].includes(r.profile_status) ? "（资料可稍后刷新）" : "";
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
  return value === "images" || value === "image" || value === "图文" ? "images" : value === "video" || value === "视频" ? "video" : value;
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
  if (!path) return `<span class="local-path-empty">—</span>`;
  const p = contentPathMeta({ local_path: path, aweme_id: row.item_id });
  const files = shareHistoryFiles(row);
  const totalSize = files.reduce((sum, file) => sum + Number(file.size || 0), 0);
  const fileHint = files.length > 1
    ? `${files.length} 个文件${totalSize ? ` · ${fmtShareSize(totalSize)}` : ""}`
    : (totalSize ? fmtShareSize(totalSize) : "");
  return `<div class="local-path">
    <div class="local-path-info">
      <div class="local-path-file"><span class="local-path-name">${esc(p ? p.name : path)}</span>${p && p.ext ? `<span class="local-path-ext">${esc(p.ext)}</span>` : ""}</div>
      <div class="local-path-dir">${esc(p ? (p.dir || "当前目录") : "当前目录")}</div>
      ${fileHint ? `<span class="share-history-file-count">${esc(fileHint)}</span>` : ""}
    </div>
    <button type="button" class="ghost local-path-action reveal" onclick="revealShareHistoryPath(${Number(row.id)},this)" data-tip="在文件夹中显示" aria-label="在文件夹中显示">${ic("i-folder")}</button>
  </div>`;
}
function populateShareHistoryFacets() {
  const select = $("sd-history-platform");
  if (!select) return;
  const old = select.value;
  const platforms = [...new Set(SHARE_HISTORY.map(shareHistoryPlatform).filter(Boolean))].sort((a, b) => a.localeCompare(b, "zh-CN"));
  select.innerHTML = `<option value="">全部平台</option>` + platforms.map(platform =>
    `<option value="${esc(platform)}">${esc(PF_NAME[platform] || (platform === "generic" ? "通用站点" : platform))}</option>`).join("");
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
  const platformName = PF_NAME[platform] || (platform === "generic" ? "通用站点" : platform || "通用站点");
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
  return `<tr>
    <td class="content-check-cell"><input type="checkbox" data-id="${Number(row.id)}" onchange="shareHistoryToggleOne(${Number(row.id)},this.checked)" ${selShareHistory.has(row.id) ? "checked" : ""} aria-label="选择下载记录"></td>
    <td class="content-cover-cell">${cover ? `<img class="thumb" src="${esc(cover)}" alt="${esc(title.slice(0, 20))}" referrerpolicy="no-referrer" loading="lazy" onclick="openShareHistoryPreview(${Number(row.id)})">` : `<span class="content-cover-empty" onclick="openShareHistoryPreview(${Number(row.id)})">${ic(type === "images" ? "i-image" : "i-film")}</span>`}</td>
    <td class="content-desc-cell"><div class="content-desc-text" title="${esc(title)}">${esc(title)}</div>${descriptionMeta}</td>
    <td><span class="content-kind">${esc(typeName)}</span>${quality ? `<span class="content-quality">${esc(quality)}</span>` : ""}${mediaCount ? `<span class="content-quality">${mediaCount} 个媒体</span>` : ""}</td>
    <td class="mut num">${contentTimeCell(createTime)}</td>
    <td class="content-metrics num"><span class="metric like">${ic("i-heart")}${fmtNum(likeCount)}</span>${commentCount ? `<span class="metric">${ic("i-msg")}${fmtNum(commentCount)}</span>` : ""}${duration ? `<span class="metric">${ic("i-clock")}${fmtDur(duration)}</span>` : ""}${!files.length && mediaCount ? `<span class="metric">${ic("i-film")}${mediaCount}</span>` : ""}</td>
    <td class="content-action-cell"><div class="content-status-row"><span class="pill ${status}">${contentStatusLabel(status)}</span>${error}</div><div class="content-action-buttons"><button class="ghost sm content-action-delete danger" onclick="deleteShareHistory(${Number(row.id)})" data-tip="删除记录" aria-label="删除下载记录">${ic("i-trash")}</button></div>${row.error ? `<div class="mut" style="max-width:180px;white-space:normal;margin-top:5px">${esc(row.error)}</div>` : ""}</td>
    <td class="local-path-cell">${shareHistoryPathCell(row)}</td>
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
  $("sd-history-selcount").textContent = "已选 " + count;
  $("sd-history-selbar").style.display = count ? "inline-flex" : "none";
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
// ═══════════ 关键词批量采集（当前版本：抖音）═══════════
function parseCollectionKeywords(raw) {
  const seen = new Set();
  return String(raw || "")
    .split(/[,，、;；\n]+/).map(x => x.trim()).filter(x => {
      const key = x.toLocaleLowerCase();
      if (!x || seen.has(key)) return false;
      seen.add(key); return true;
    }).slice(0, 21);
}
function collectionKeywords() {
  return parseCollectionKeywords($("col-keywords") ? $("col-keywords").value : "");
}
function applyCollectionForm() {
  const enabled = !!($("col-download") && $("col-download").checked);
  if ($("col-dir-wrap")) $("col-dir-wrap").style.display = enabled ? "" : "none";
}
function collectionStatus(status) {
  const labels = { pending: "等待中", running: "采集中", done: "已完成", partial: "部分完成", failed: "失败", canceled: "已取消" };
  const classes = { pending: "pending", running: "downloading", done: "done", partial: "pending", failed: "failed", canceled: "skipped" };
  return { label: labels[status] || status, cls: classes[status] || "skipped" };
}
function collectionLastError(job) {
  const lines = String(job && job.error || "").split(/\r?\n/).map(line => line.trim()).filter(Boolean);
  return lines.length ? lines[lines.length - 1] : "";
}
function collectionDate(value) {
  if (!value) return "—";
  const parsed = new Date(value.endsWith && value.endsWith("Z") ? value : value + "Z");
  return Number.isNaN(parsed.getTime()) ? esc(value) : parsed.toLocaleString();
}
async function createCollection() {
  const keywords = collectionKeywords();
  const accountId = Number($("col-account").value || 0);
  const contentLimit = Number($("col-content-limit").value || 0);
  const commentLimit = Number($("col-comment-limit").value || 0);
  const pageLimit = Number($("col-page-limit").value || 0);
  const stagnantPages = Number($("col-stagnant-pages").value || 0);
  const minLikes = Number($("col-min-likes").value || 0);
  const minComments = Number($("col-min-comments").value || 0);
  let valid = true;
  valid = setFieldError($("col-keywords"), !keywords.length ? "请至少填写一个关键词" : keywords.length > 20 ? "单个任务最多 20 个关键词" : "") && valid;
  valid = setFieldError($("col-account"), !accountId ? "请选择一个已登录账号" : "") && valid;
  valid = setFieldError($("col-content-limit"), contentLimit < 1 || contentLimit > 100 ? "请输入 1–100" : "") && valid;
  valid = setFieldError($("col-comment-limit"), commentLimit < 0 || commentLimit > 200 ? "请输入 0–200" : "") && valid;
  valid = setFieldError($("col-page-limit"), pageLimit < 1 || pageLimit > 40 ? "请输入 1–40" : "") && valid;
  valid = setFieldError($("col-stagnant-pages"), stagnantPages < 1 || stagnantPages > 8 ? "请输入 1–8" : "") && valid;
  valid = setFieldError($("col-min-likes"), minLikes < 0 ? "请输入非负整数" : "") && valid;
  valid = setFieldError($("col-min-comments"), minComments < 0 ? "请输入非负整数" : "") && valid;
  if (!valid) {
    const first = document.querySelector('[data-panel="collections"] [aria-invalid="true"]');
    if (first) first.focus();
    return;
  }
  const btn = evtBtn();
  await withBusy(btn, "创建中", async () => {
    try {
      const job = await api("/api/collections", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          platform: "douyin", account_id: accountId, keywords,
          max_contents_per_keyword: contentLimit,
          max_pages_per_keyword: pageLimit,
          stagnant_pages: stagnantPages,
          search_sort: $("col-sort").value || "general",
          publish_time: $("col-publish-time").value || "all",
          content_type: $("col-content-type").value || "all",
          min_likes: minLikes,
          min_comments: minComments,
          max_comments_per_content: commentLimit,
          include_replies: $("col-replies").checked,
          download_media: $("col-download").checked,
          video_quality: $("col-quality").value || "highest",
          download_dir: $("col-download-dir").value.trim(),
        }),
      });
      $("col-create-msg").textContent = `任务 #${job.id} 已进入队列`;
      $("col-keywords").value = "";
      toast("关键词采集任务已创建", "ok");
      await refreshCollections();
    } catch (e) {
      $("col-create-msg").textContent = "创建失败：" + e.message;
      toast("创建失败：" + e.message, "err");
    }
  });
}
function collectionTaskSkeleton(count = 3) {
  return Array.from({ length: count }, () => `<div class="collection-task-skeleton" aria-hidden="true">
    <span class="sk" style="height:28px"></span><span class="sk" style="height:42px"></span>
    <span class="sk" style="height:42px"></span><span class="sk" style="height:52px"></span>
  </div>`).join("");
}
function collectionTaskEmpty() {
  return `<div class="empty collection-task-empty"><div class="empty-ic">${ic("i-hash")}</div>
    <div class="empty-t">还没有关键词采集任务</div><div class="empty-sub">在上方批量输入关键词并开始采集</div></div>`;
}
function renderCollectionJobs() {
  const body = $("collection-job-table"); if (!body) return;
  body.innerHTML = COLLECTION_JOBS.map(job => {
    const status = collectionStatus(job.status);
    const planned = Math.max(1, Number(job.planned_content_count || 0));
    const percent = Math.min(100, Math.round(Number(job.content_count || 0) * 100 / planned));
    const keywords = (job.keywords || []).slice(0, 5).map(k => `<span class="meta-chip">${esc(k)}</span>`).join("") +
      ((job.keywords || []).length > 5 ? `<span class="meta-chip more">+${job.keywords.length - 5}</span>` : "");
    const canCancel = ["pending", "running"].includes(job.status);
    const canRetry = ["done", "partial", "failed", "canceled"].includes(job.status);
    const canEdit = canRetry && job.platform === "douyin";
    const errorText = collectionLastError(job);
    const sortLabel = { general: "综合", latest: "最新", most_liked: "最多点赞" }[job.search_sort] || "综合";
    const timeLabel = { all: "不限时间", day: "一天内", week: "一周内", half_year: "半年内" }[job.publish_time] || "不限时间";
    const typeLabel = { all: "全部类型", video: "视频", images: "图文" }[job.content_type] || "全部类型";
    const threshold = [Number(job.min_likes) > 0 ? `≥${fmtNum(job.min_likes)} 赞` : "", Number(job.min_comments) > 0 ? `≥${fmtNum(job.min_comments)} 评` : ""].filter(Boolean).join(" · ");
    return `<article class="collection-task" role="listitem" aria-label="任务 ${job.id}，${status.label}">
      <div class="collection-task-meta"><div><span class="collection-task-label">任务状态</span><span class="pill ${status.cls}">${status.label}</span></div><time class="collection-task-created" datetime="${esc(job.created_at || "")}">${collectionDate(job.created_at)}</time></div>
      <div class="collection-task-keywords"><span class="collection-task-label">关键词</span><div class="keyword-stack">${keywords}</div>${job.current_keyword ? `<div class="collection-step">当前：${esc(job.current_keyword)}</div>` : ""}</div>
      <div class="collection-task-config"><span class="collection-task-label">采集配置</span><div class="collection-task-config-main">${job.max_contents_per_keyword} 作品/词 · ${job.max_pages_per_keyword || 12} 深度页 · ${sortLabel}</div><div class="collection-task-config-sub">${timeLabel} · ${typeLabel}${threshold ? ` · ${threshold}` : ""} · ${job.max_comments_per_content} 评论/作品<br>${job.include_replies ? "含二级评论 · " : ""}${job.download_media ? "下载媒体" : "仅采数据"} · 连续 ${job.stagnant_pages || 3} 页无新增停止</div></div>
      <div class="collection-task-progress"><span class="collection-task-label">执行进度</span><div class="job-progress"><div class="job-progress-head"><span>${esc(job.current_step || "等待执行")}</span><b>${job.content_count}/${job.planned_content_count}</b></div><div class="progress-track" role="progressbar" aria-label="任务 ${job.id} 进度" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${percent}"><div class="progress-fill" style="width:${percent}%"></div></div><div class="collection-step">已采评论 ${fmtNum(job.comment_count)}</div></div></div>
      ${job.error_count ? `<button type="button" class="collection-task-error" onclick="openCollectionResults(${job.id})" title="${esc(errorText)}">${ic("i-info")}<span class="collection-task-error-text">${job.error_count} 条异常 · ${esc(errorText)}</span><span class="collection-task-error-link">查看详情</span></button>` : ""}
      <div class="collection-task-actions" aria-label="任务 ${job.id} 操作">
        <button type="button" class="sm collection-task-primary" onclick="openCollectionResults(${job.id})">${ic("i-eye")}查看结果</button>
        ${canEdit ? `<button type="button" class="ghost sm" onclick="editCollection(${job.id})">${ic("i-settings")}编辑</button>` : ""}
        <button type="button" class="ghost sm" onclick="exportCollection(${job.id})"${job.content_count ? "" : " disabled"}>${ic("i-download")}导出</button>
        ${canCancel ? `<button type="button" class="ghost sm" onclick="cancelCollection(${job.id})">${ic("i-x")}取消任务</button>` : ""}
        ${canRetry ? `<button type="button" class="ghost sm" onclick="retryCollection(${job.id})">${ic("i-play")}续跑</button>` : ""}
        ${job.status !== "running" ? `<button type="button" class="ghost sm danger collection-task-delete" onclick="deleteCollection(${job.id})" aria-label="删除任务 ${job.id}">${ic("i-trash")}删除</button>` : ""}
      </div>
    </article>`;
  }).join("") || collectionTaskEmpty();
}
async function refreshCollections() {
  if (!$("collection-job-table") || PLATFORM !== "douyin") return;
  try {
    COLLECTION_JOBS = await api("/api/collections?platform=douyin");
    const active = COLLECTION_JOBS.filter(j => ["pending", "running"].includes(j.status)).length;
    if ($("tb-col")) $("tb-col").textContent = active || COLLECTION_JOBS.length;
    renderCollectionJobs();
    if (COLLECTION_JOB_ID) {
      const job = COLLECTION_JOBS.find(j => j.id === COLLECTION_JOB_ID);
      if (job) {
        updateCollectionResultStats(job);
        if (CURRENT_TAB === "collections") await loadCollectionContents(COLLECTION_PAGE, true);
      }
      else closeCollectionResults();
    }
  } catch (e) {
    if (CURRENT_TAB === "collections") toast("采集任务刷新失败：" + e.message, "err");
  }
}
async function editCollection(jobId, draft = null) {
  const job = COLLECTION_JOBS.find(item => item.id === Number(jobId));
  if (!job) return;
  const accounts = ACCOUNTS.filter(a => a.platform === "douyin" && a.status !== "invalid" && a.has_storage);
  const initial = draft || {
    account_id: job.account_id,
    keywords: (job.keywords || []).join("\n"),
    max_contents_per_keyword: job.max_contents_per_keyword,
    max_pages_per_keyword: job.max_pages_per_keyword || 12,
    stagnant_pages: job.stagnant_pages || 3,
    search_sort: job.search_sort || "general",
    publish_time: job.publish_time || "all",
    content_type: job.content_type || "all",
    min_likes: Number(job.min_likes || 0),
    min_comments: Number(job.min_comments || 0),
    max_comments_per_content: job.max_comments_per_content,
    include_replies: !!job.include_replies,
    download_media: !!job.download_media,
    video_quality: job.video_quality || "highest",
    download_dir: job.download_dir || "",
  };
  const value = await new Promise(resolve => {
    _uiResolve = resolve; _uiCancelVal = null;
    $("ui-body").innerHTML = `
      <div class="form-field"><label for="ecol-keywords">关键词 <span class="field-scope">最多 20 个</span></label>
        <textarea id="ecol-keywords" rows="5" placeholder="每行一个关键词">${esc(initial.keywords)}</textarea></div>
      <div class="form-grid">
        <div class="form-field"><label for="ecol-account">使用账号</label><select id="ecol-account">${accOptions(accounts, accounts.length ? "请选择抖音账号" : "暂无可用抖音账号")}</select></div>
        <div class="form-field"><label for="ecol-quality">视频画质</label><select id="ecol-quality"><option value="highest">原画 / 最高</option><option value="1080">1080P</option><option value="720">720P</option><option value="540">540P</option><option value="lowest">最低省流</option></select></div>
        <div class="form-field"><label for="ecol-content-limit">每词作品上限</label><input id="ecol-content-limit" type="number" min="1" max="100" value="${Number(initial.max_contents_per_keyword) || 20}"></div>
        <div class="form-field"><label for="ecol-comment-limit">每作品评论上限</label><input id="ecol-comment-limit" type="number" min="0" max="200" value="${Number(initial.max_comments_per_content) || 0}"></div>
      </div>
      <div class="form-grid collection-filter-grid">
        <div class="form-field"><label for="ecol-page-limit">每词采集深度</label><input id="ecol-page-limit" type="number" min="1" max="40" value="${Number(initial.max_pages_per_keyword) || 12}"></div>
        <div class="form-field"><label for="ecol-sort">搜索排序</label><select id="ecol-sort"><option value="general">综合排序</option><option value="latest">最新发布</option><option value="most_liked">最多点赞</option></select></div>
        <div class="form-field"><label for="ecol-publish-time">发布时间</label><select id="ecol-publish-time"><option value="all">不限</option><option value="day">一天内</option><option value="week">一周内</option><option value="half_year">半年内</option></select></div>
        <div class="form-field"><label for="ecol-content-type">内容类型</label><select id="ecol-content-type"><option value="all">全部作品</option><option value="video">视频</option><option value="images">图文 / 图集</option></select></div>
        <div class="form-field"><label for="ecol-min-likes">最低点赞数</label><input id="ecol-min-likes" type="number" min="0" value="${Number(initial.min_likes) || 0}"></div>
        <div class="form-field"><label for="ecol-min-comments">最低评论数</label><input id="ecol-min-comments" type="number" min="0" value="${Number(initial.min_comments) || 0}"></div>
        <div class="form-field"><label for="ecol-stagnant-pages">连续无新增停止</label><input id="ecol-stagnant-pages" type="number" min="1" max="8" value="${Number(initial.stagnant_pages) || 3}"></div>
      </div>
      <div class="option-grid" aria-label="采集选项">
        <label class="switch-row"><input type="checkbox" id="ecol-download"${initial.download_media ? " checked" : ""} onchange="$('ecol-dir-wrap').style.display=this.checked?'':'none'"><span class="switch-copy"><b>下载媒体</b><span>保存视频和封面来源</span></span></label>
        <label class="switch-row"><input type="checkbox" id="ecol-replies"${initial.include_replies ? " checked" : ""}><span class="switch-copy"><b>包含二级评论</b><span>采集抖音当前可返回的回复</span></span></label>
      </div>
      <div class="form-field" id="ecol-dir-wrap" style="display:${initial.download_media ? "" : "none"}"><label for="ecol-download-dir">下载目录（可选）</label><input id="ecol-download-dir" value="${esc(initial.download_dir)}" placeholder="留空使用默认目录"></div>`;
    $("ecol-account").value = String(initial.account_id || "");
    $("ecol-quality").value = initial.video_quality || "highest";
    $("ecol-sort").value = initial.search_sort || "general";
    $("ecol-publish-time").value = initial.publish_time || "all";
    $("ecol-content-type").value = initial.content_type || "all";
    enhanceAllSelects($("ui-body")); csSyncAll();
    _uiGetVal = () => ({
      account_id: Number($("ecol-account").value || 0),
      keywords: $("ecol-keywords").value,
      max_contents_per_keyword: Number($("ecol-content-limit").value || 0),
      max_pages_per_keyword: Number($("ecol-page-limit").value || 0),
      stagnant_pages: Number($("ecol-stagnant-pages").value || 0),
      search_sort: $("ecol-sort").value || "general",
      publish_time: $("ecol-publish-time").value || "all",
      content_type: $("ecol-content-type").value || "all",
      min_likes: Number($("ecol-min-likes").value || 0),
      min_comments: Number($("ecol-min-comments").value || 0),
      max_comments_per_content: Number($("ecol-comment-limit").value || 0),
      include_replies: $("ecol-replies").checked,
      download_media: $("ecol-download").checked,
      video_quality: $("ecol-quality").value || "highest",
      download_dir: $("ecol-download-dir").value.trim(),
    });
    _uiOpen(`编辑采集任务 #${job.id}`, "保存配置不会删除已有作品和评论；修改后点击“续跑”应用新配置，系统会自动去重。", { okText: "保存配置", wide: true });
  });
  if (value === null) return;
  const keywords = parseCollectionKeywords(value.keywords);
  let error = "";
  if (!keywords.length) error = "请至少填写一个关键词";
  else if (keywords.length > 20) error = "单个任务最多 20 个关键词";
  else if (!value.account_id) error = "请选择一个可用抖音账号";
  else if (value.max_contents_per_keyword < 1 || value.max_contents_per_keyword > 100) error = "每词作品上限须为 1–100";
  else if (value.max_pages_per_keyword < 1 || value.max_pages_per_keyword > 40) error = "每词采集深度须为 1–40 页";
  else if (value.stagnant_pages < 1 || value.stagnant_pages > 8) error = "连续无新增停止阈值须为 1–8 页";
  else if (value.min_likes < 0 || value.min_comments < 0) error = "点赞和评论门槛须为非负整数";
  else if (value.max_comments_per_content < 0 || value.max_comments_per_content > 200) error = "每作品评论上限须为 0–200";
  if (error) { toast(error, "err"); return editCollection(jobId, value); }
  try {
    await api(`/api/collections/${job.id}`, {
      method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ...value, platform: "douyin", keywords }),
    });
    toast("任务配置已保存，点击“续跑”后生效", "ok");
    await refreshCollections();
  } catch (e) { toast("编辑失败：" + e.message, "err"); }
}
function updateCollectionResultStats(job) {
  if (!job) return;
  $("col-stat-content").textContent = fmtNum(job.content_count);
  $("col-stat-comments").textContent = fmtNum(job.comment_count);
  $("col-stat-errors").textContent = fmtNum(job.error_count);
  const errorBox = $("collection-results-error");
  const errorText = collectionLastError(job);
  errorBox.style.display = errorText ? "" : "none";
  errorBox.textContent = errorText ? `最近异常：${errorText}` : "";
  $("collection-results-title").childNodes[0].nodeValue = `任务 #${job.id} 采集结果 `;
  $("collection-results-sub").textContent = `${(job.keywords || []).join("、")} · ${collectionStatus(job.status).label}`;
}
async function openCollectionResults(jobId) {
  const btn = evtBtn();
  COLLECTION_JOB_ID = Number(jobId); COLLECTION_PAGE = 1;
  const job = COLLECTION_JOBS.find(j => j.id === COLLECTION_JOB_ID);
  if (job) updateCollectionResultStats(job);
  $("collection-results-card").style.display = "";
  await withBusy(btn, "加载中", async () => {
    await loadCollectionContents(1);
    const reducedMotion = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    $("collection-results-card").scrollIntoView({ behavior: reducedMotion ? "auto" : "smooth", block: "start" });
  });
}
function closeCollectionResults() {
  COLLECTION_JOB_ID = 0; COLLECTION_PAGE = 1;
  if ($("collection-results-card")) $("collection-results-card").style.display = "none";
}
function collectionResultSkeleton(count = 4) {
  return Array.from({ length: count }, () => `<div class="collection-result-skeleton" aria-hidden="true">
    <span class="sk" style="height:120px"></span><span class="sk" style="width:72%;margin-top:14px"></span>
    <span class="sk" style="width:92%;margin-top:10px"></span><span class="sk" style="height:42px;margin-top:18px"></span>
  </div>`).join("");
}
function collectionEmpty(title, detail = "") {
  return `<div class="empty collection-result-empty"><div class="empty-ic">${ic("i-film")}</div>
    <div class="empty-t">${esc(title)}</div>${detail ? `<div class="empty-sub">${esc(detail)}</div>` : ""}</div>`;
}
function collectionFileDisplay(item) {
  const path = String(item.local_path || "").trim();
  const pathMeta = contentPathMeta(item);
  const count = Number(item.media_count || 0);
  const name = count > 1 ? `${count} 个媒体文件` : (pathMeta ? pathMeta.name + (pathMeta.ext ? `.${pathMeta.ext}` : "") : "本地媒体");
  const bits = [item.file_size ? fmtShareSize(item.file_size) : "", pathMeta && pathMeta.dir ? pathMeta.dir : ""].filter(Boolean);
  return { path, name, meta: bits.join(" · ") || "本地文件可用" };
}
function collectionResultCard(item) {
  const title = item.desc || item.aweme_id || "未命名作品";
  const isGallery = item.media_type === "images";
  const typeLabel = isGallery ? `图集${item.media_count > 1 ? ` · ${item.media_count} 张` : ""}` : "视频";
  const canPreview = Boolean(item.preview_available);
  const file = collectionFileDisplay(item);
  const fileExists = Boolean(item.local_exists);
  const downloadClass = fileExists ? "done" : item.download_status === "failed" ? "failed" : "skipped";
  const downloadLabel = fileExists ? "已下载" : item.download_status === "failed" ? "下载失败" : item.download_status === "done" ? "文件缺失" : "未下载";
  const byline = [item.author_name || "未知作者", item.create_time ? fmtTime(item.create_time) : "", item.aweme_id || ""].filter(Boolean);
  const cover = item.cover_url
    ? `<img src="${esc(item.cover_url)}" alt="${esc(title.slice(0, 60))}封面" loading="lazy" referrerpolicy="no-referrer">`
    : `<span class="collection-cover-empty">${ic(isGallery ? "i-image" : "i-film")}</span>`;
  const filePanel = fileExists ? `<div class="collection-file-panel" title="${esc(file.path)}">
    <div class="collection-file-main">${ic("i-folder")}<div class="collection-file-info">
      <div class="collection-file-name">${esc(file.name)}</div><div class="collection-file-meta">${esc(file.meta)}</div>
    </div></div>
    <div class="collection-file-actions">
      <button type="button" class="ghost collection-icon-action" onclick="openCollectionFile(${item.job_id},${item.id},this)" data-tip="用本机默认程序打开" aria-label="用本机默认程序打开">${ic("i-external")}</button>
      <button type="button" class="ghost collection-icon-action" onclick="revealCollectionFile(${item.job_id},${item.id},this)" data-tip="在文件夹中显示" aria-label="在文件夹中显示">${ic("i-folder")}</button>
      <button type="button" class="ghost collection-icon-action" data-path="${esc(file.path)}" onclick="copyCollectionPath(this)" data-tip="复制本地路径" aria-label="复制本地路径">${ic("i-copy")}</button>
    </div>
  </div>` : `<div class="collection-download-note${item.download_status === "failed" ? " failed" : ""}">${item.error ? esc(item.error) : "本地文件尚不可用，可先预览平台媒体或打开原作品"}</div>`;
  return `<article class="collection-result-item">
    <button type="button" class="collection-result-cover" onclick="openCollectionPreview(${item.job_id},${item.id})" aria-label="预览：${esc(title.slice(0, 80))}"${canPreview ? "" : " disabled"}>
      ${cover}<span class="collection-cover-type">${esc(typeLabel)}</span>
      <span class="collection-cover-preview">${ic("i-play")}${canPreview ? "站内预览" : "暂无预览"}</span>
    </button>
    <div class="collection-result-body">
      <div class="collection-result-tags"><span class="meta-chip group">#${esc(item.keyword)}</span><span class="pill ${downloadClass}" title="${esc(item.error || "")}">${downloadLabel}</span></div>
      <a class="collection-result-title" href="${esc(item.url)}" target="_blank" rel="noopener noreferrer" title="${esc(title)}">${esc(title)}</a>
      <div class="collection-result-byline">${byline.map((part, index) => `<span${index === byline.length - 1 ? ' class="collection-result-id"' : ""}>${esc(part)}</span>`).join("<span>·</span>")}</div>
      <div class="collection-result-metrics" aria-label="作品数据">
        <div class="collection-result-metric"><b>${ic("i-heart")}${fmtNum(item.like_count)}</b><span>点赞</span></div>
        <div class="collection-result-metric"><b>${ic("i-msg")}${fmtNum(item.comment_count)}</b><span>平台评论</span></div>
        <div class="collection-result-metric"><b>${ic("i-inbox")}${fmtNum(item.collected_comment_count)}</b><span>已采评论</span></div>
      </div>
      ${filePanel}
      <div class="collection-result-actions">
        <button type="button" class="sm collection-preview-primary" onclick="openCollectionPreview(${item.job_id},${item.id})"${canPreview ? "" : " disabled"}>${ic("i-eye")}预览</button>
        ${fileExists ? `<button type="button" class="ghost sm" onclick="openCollectionFile(${item.job_id},${item.id},this)">${ic("i-external")}打开文件</button>` : ""}
        <button type="button" class="ghost sm" onclick="showCollectionComments(${item.id})"${item.collected_comment_count ? "" : " disabled"}>${ic("i-msg")}评论 ${item.collected_comment_count}</button>
        <a class="collection-source-link" href="${esc(item.url)}" target="_blank" rel="noopener noreferrer">${ic("i-external")}原作品</a>
      </div>
    </div>
  </article>`;
}
function openCollectionPreview(jobId, contentId, startIdx = 0) {
  return _pvOpen(() => api(`/api/collections/${jobId}/contents/${contentId}/media`), startIdx);
}
async function collectionLocalAction(jobId, contentId, action, button) {
  const old = button && button.innerHTML;
  if (button) { button.disabled = true; button.innerHTML = `<span class="spin"></span>`; }
  try {
    await api(`/api/collections/${jobId}/contents/${contentId}/${action}`, {
      method: "POST", headers: { "X-CreatorHub-Local-Action": action },
    });
    toast(action === "open" ? "已调用本机默认程序打开文件" : "已在文件夹中显示", "ok", 1800);
  } catch (e) {
    toast((action === "open" ? "打开文件失败：" : "打开文件夹失败：") + e.message, "err");
  } finally {
    if (button && button.isConnected) { button.disabled = false; button.innerHTML = old; }
  }
}
function openCollectionFile(jobId, contentId, button) { return collectionLocalAction(jobId, contentId, "open", button); }
function revealCollectionFile(jobId, contentId, button) { return collectionLocalAction(jobId, contentId, "reveal", button); }
async function copyCollectionPath(button) {
  const value = String(button && button.dataset.path || "");
  if (!value) return;
  try {
    await navigator.clipboard.writeText(value);
    toast("本地路径已复制", "ok", 1800);
  } catch (e) {
    const field = document.createElement("textarea");
    field.value = value; field.style.position = "fixed"; field.style.opacity = "0";
    document.body.appendChild(field); field.select();
    const copied = document.execCommand("copy"); field.remove();
    toast(copied ? "本地路径已复制" : "复制失败，请手动复制路径", copied ? "ok" : "err");
  }
}
async function loadCollectionContents(page = COLLECTION_PAGE, quiet = false) {
  if (!COLLECTION_JOB_ID) return;
  COLLECTION_PAGE = Math.max(1, Number(page) || 1);
  const body = $("collection-content-list");
  if (!quiet) body.innerHTML = collectionResultSkeleton(4);
  try {
    const result = await api(`/api/collections/${COLLECTION_JOB_ID}/contents?page=${COLLECTION_PAGE}&page_size=20`);
    COLLECTION_PAGE = result.page;
    body.innerHTML = (result.items || []).map(collectionResultCard).join("") || collectionEmpty("任务暂时没有作品结果", "采集中可稍后刷新；失败任务可查看错误并续跑");
    $("collection-result-count").textContent = `共 ${fmtNum(result.total)} 个作品`;
    const pager = $("collection-pager");
    pager.innerHTML = `<button class="ghost sm" onclick="loadCollectionContents(${result.page - 1})"${result.page <= 1 ? " disabled" : ""}>${ic("i-prev")}上一页</button><span class="mut">第 ${result.page} / ${result.pages} 页 · 共 ${fmtNum(result.total)} 条</span><button class="ghost sm" onclick="loadCollectionContents(${result.page + 1})"${result.page >= result.pages ? " disabled" : ""}>下一页${ic("i-next")}</button>`;
    pager.hidden = result.pages <= 1;
  } catch (e) {
    body.innerHTML = collectionEmpty("结果加载失败", e.message);
    $("collection-result-count").textContent = "";
  }
}
async function showCollectionComments(contentId) {
  const modal = $("collection-comments-modal");
  const list = $("collection-comments-list");
  list.innerHTML = `<div class="empty"><div class="empty-t">加载中…</div></div>`;
  modal.style.display = "flex"; modalOpened(modal);
  setTimeout(() => modal.querySelector(".pv-close").focus(), 0);
  try {
    const comments = await api(`/api/collections/${COLLECTION_JOB_ID}/comments?content_id=${contentId}&limit=500`);
    $("collection-comments-count").textContent = `共 ${comments.length} 条本次采集评论`;
    list.innerHTML = comments.map(comment => `<article class="collection-comment"><div class="collection-comment-head"><b>${esc(comment.user_nickname || "匿名用户")}</b><span>${fmtTime(comment.create_time)} · ${fmtNum(comment.like_count)} 赞${comment.reply_to ? " · 回复" : ""}</span></div><p>${esc(comment.text || "（空评论）")}</p></article>`).join("") || `<div class="empty"><div class="empty-t">没有已采评论</div></div>`;
  } catch (e) {
    list.innerHTML = `<div class="empty"><div class="empty-t">加载失败</div><div class="empty-sub">${esc(e.message)}</div></div>`;
  }
}
function hideCollectionComments() {
  const modal = $("collection-comments-modal");
  modal.style.display = "none"; modalClosed(modal);
}
async function cancelCollection(jobId) {
  const btn = evtBtn();
  if (!await uiConfirm({ title: "取消采集任务", message: "任务会在当前请求完成后安全停止，已经保存的结果会保留。", okText: "取消任务", danger: true })) return;
  await withBusy(btn, "取消中", async () => {
    try { await api(`/api/collections/${jobId}/cancel`, { method: "POST" }); toast("已请求停止任务", "ok"); await refreshCollections(); }
    catch (e) { toast("取消失败：" + e.message, "err"); }
  });
}
async function retryCollection(jobId) {
  const btn = evtBtn();
  await withBusy(btn, "提交中", async () => {
    try { await api(`/api/collections/${jobId}/retry`, { method: "POST" }); toast("任务已重新进入队列，已有结果会自动去重", "ok"); await refreshCollections(); }
    catch (e) { toast("续跑失败：" + e.message, "err"); }
  });
}
async function deleteCollection(jobId) {
  const btn = evtBtn();
  if (!await uiConfirm({ title: "删除采集任务", message: "将删除任务及其作品、评论记录；本地已下载文件不会被删除。", okText: "删除", danger: true })) return;
  await withBusy(btn, "删除中", async () => {
    try { await api(`/api/collections/${jobId}`, { method: "DELETE" }); if (COLLECTION_JOB_ID === jobId) closeCollectionResults(); toast("任务记录已删除，本地文件已保留", "ok"); await refreshCollections(); }
    catch (e) { toast("删除失败：" + e.message, "err"); }
  });
}
function exportCollection(jobId) {
  const link = document.createElement("a");
  link.href = `/api/collections/${jobId}/export.xlsx`;
  link.download = `keyword-collection-${jobId}.xlsx`;
  document.body.appendChild(link); link.click(); link.remove();
}

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
        interval_seconds: +$("em-interval").value,
        account_id: $("em-account").value ? +$("em-account").value : null,
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
          <div><label class="field" for="em-account">抓取账号</label><select id="em-account">${accountOptions}</select></div>
        </div>
        ${item.last_scan_at ? "" : `<div><label class="field" for="em-backfill">首次历史回填</label>
          <select id="em-backfill">${backfillOptions}</select></div>`}
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
    $("em-interval").value = String(item.interval_seconds || 300);
    $("em-account").value = item.account_id ? String(item.account_id) : "";
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
  return `<div style="display:flex;gap:4px;flex-wrap:wrap;margin-top:4px" title="${esc(filterLabel)}"><span class="pill q bare">深度 ${depth}</span><span class="pill q bare">上限 ${limit || "不限"}</span>${filters.length ? `<span class="pill q bare">${esc(filterLabel)}</span>` : ""}</div>`;
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
      ${monitorStrategySummary(t)}
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
  const n = selComment.size; const c = $("comment-selcount"), b = $("comment-batchbtn");
  c.textContent = "已选 " + n; c.style.display = n ? "inline" : "none"; b.style.display = n ? "inline-flex" : "none";
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
        <span class="pill ${r.download_status}" style="flex:1;justify-content:center" title="${esc(r.error || "")}">${contentStatusLabel(r.download_status)}${r.error ? " ⓘ" : ""}</span>
        ${["failed", "skipped"].includes(r.download_status) ? `<button class="ghost sm" onclick="retryDl(${r.id})">${r.download_status === "skipped" ? "下载" : "重试"}</button>` : ""}
        ${(PLATFORM === "xhs" && r.download_status === "done") ? `<button class="ghost sm" onclick="repostDouyin(${r.id})">发抖音</button>` : ""}
        <button class="ghost sm danger" onclick="delContent(${r.id})">${ic("i-trash")}删除</button>
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
  if (resetPage) CONTENT_PAGE = 1;
  const params = new URLSearchParams({
    platform: PLATFORM, page: String(CONTENT_PAGE),
    page_size: String(CONTENT_PAGE_SIZE), paginate: "true",
  });
  if (CONTENT_SRC) params.set("target_id", CONTENT_SRC);
  if (CONTENT_GROUP) params.set("group_name", CONTENT_GROUP);
  if (CONTENT_TAG) params.set("tag", CONTENT_TAG);
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
  params.set("sort", ($('content-sort') && $('content-sort').value) || "create_desc");
  const payload = await api("/api/contents?" + params.toString());
  const meta = Array.isArray(payload)
    ? { items: payload, total: payload.length, page: 1, page_size: CONTENT_PAGE_SIZE,
        pages: Math.max(1, Math.ceil(payload.length / CONTENT_PAGE_SIZE)) }
    : (payload || {});
  const pages = Math.max(1, Number(meta.pages || 1));
  if (CONTENT_PAGE > pages) { CONTENT_PAGE = pages; return refreshContents(); }
  const rows = Array.isArray(meta.items) ? meta.items : [];
  CONTENTS = rows;
  $("stat-dl").textContent = rows.filter(r => r.download_status === "done").length;
  if ($("content-filter-count")) $("content-filter-count").textContent =
    `显示 ${rows.length} / ${Number(meta.total || rows.length)}`;
  const xhs = PLATFORM === "xhs";
  $("content-title").textContent = xhs ? "最新笔记 / 下载状态" : "最新作品 / 下载状态";
  $("content-table-wrap").style.display = xhs ? "none" : "";
  $("content-cards").style.display = xhs ? "" : "none";
  if (xhs) {
    $("content-cards").innerHTML = rows.map(noteCard).join("")
      || `<div class="empty" style="columns:1">${ic("i-image")}<div class="empty-t">暂无笔记</div></div>`;
    updateContentSelBar(); renderContentPager(meta);
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
  if ($("danmaku-filter-count")) {
    $("danmaku-filter-count").textContent = `显示 ${rows.length} / ${Number(meta.total || rows.length)}`;
  }
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
  if (resetPage) COMMENT_PAGE = 1;
  const params = new URLSearchParams({
    platform: PLATFORM, page: String(COMMENT_PAGE),
    page_size: String(COMMENT_PAGE_SIZE), paginate: "true",
  });
  if (COMMENT_SRC) params.set("watch_id", COMMENT_SRC);
  if (COMMENT_GROUP) params.set("group_name", COMMENT_GROUP);
  if (COMMENT_TAG) params.set("tag", COMMENT_TAG);
  const query = (($('comment-query') && $('comment-query').value) || "").trim();
  const replyType = ($('comment-type') && $('comment-type').value) || "";
  const minLikes = +(($('comment-min-likes') && $('comment-min-likes').value) || 0);
  if (query) params.set("q", query);
  if (replyType) params.set("reply_type", replyType);
  if (Number.isFinite(minLikes) && minLikes > 0) params.set("min_like_count", String(Math.floor(minLikes)));
  params.set("sort", ($('comment-sort') && $('comment-sort').value) || "latest");
  const payload = await api("/api/comments?" + params.toString());
  const meta = Array.isArray(payload)
    ? { items: payload, total: payload.length, page: 1, page_size: COMMENT_PAGE_SIZE,
        pages: Math.max(1, Math.ceil(payload.length / COMMENT_PAGE_SIZE)) }
    : (payload || {});
  const pages = Math.max(1, Number(meta.pages || 1));
  if (COMMENT_PAGE > pages) { COMMENT_PAGE = pages; return refreshComments(); }
  const rows = Array.isArray(meta.items) ? meta.items : [];
  $("stat-cmt").textContent = rows.length;
  if ($("comment-filter-count")) $("comment-filter-count").textContent =
    `显示 ${rows.length} / ${Number(meta.total || rows.length)}`;
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
  updateCommentSelBar(); renderCommentPager(meta);
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
    else if (modal === $("collection-comments-modal")) hideCollectionComments();
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
const PUB_ST = { pending: "排队中", publishing: "发布中", uncertain: "结果待确认", done: "已发布", failed: "失败", canceled: "已取消" };
const PUB_PILL = { pending: "pending", publishing: "downloading", uncertain: "downloading", done: "done", failed: "failed", canceled: "invalid" };
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
const AC_TASK_ST = { draft: "草稿待审", pending: "排队中", doing: "发送中", uncertain: "结果待确认", done: "已发送", failed: "失败", canceled: "已取消" };
const AC_TASK_PILL = { draft: "downloading", pending: "pending", doing: "downloading", uncertain: "downloading", done: "done", failed: "failed", canceled: "invalid" };
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
      else if (r.created > 0) toast(`生成 ${r.created} 条${r.manual_only ? "人工发布草稿(未调用评论接口)" : r.review ? "草稿(待人工通过)" : "任务"}(发现 ${r.candidates} 个目标)`, "ok", 6000);
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
    <td class="mut">${t.method === "browser" ? "浏览器页面" : t.method === "api" ? "API 兼容模式" : t.method === "manual" ? "人工草稿" : "—"}</td>
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

// ─── 统一任务队列 ───
let TASK_QUEUE_PAGE = 1;
let TASK_QUEUE_PAGE_SIZE = 20;
let TASK_QUEUE_PAGES = 1;
let TASK_QUEUE_LOADING = false;
let TASK_QUEUE_REFRESH_PENDING = false;
let TASK_QUEUE_BADGE_LOADING = false;

const TASK_QUEUE_RAW_STATUS = {
  draft: "草稿待审", pending: "等待执行", running: "采集中", publishing: "发布中",
  doing: "执行中", downloading: "下载中", done: "已完成", failed: "失败",
  partial: "部分完成", uncertain: "结果待确认", canceled: "已取消", skipped: "已跳过",
};
const TASK_QUEUE_STATE_META = {
  pending: ["等待执行", "pending"], running: ["正在执行", "queue-running"],
  blocked: ["风控延后", "queue-blocked"], failed: ["执行失败", "failed"],
  completed: ["已完成", "queue-completed"],
};

function updateTaskQueuePlatformLabel() {
  const option = $("queue-platform-current");
  if (option) option.textContent = `当前平台（${PF_NAME[PLATFORM] || PLATFORM}）`;
  const select = $("queue-platform");
  if (select && select._csSync) select._csSync();
}

function taskQueuePlatform() {
  const value = $("queue-platform")?.value || "current";
  return value === "current" ? PLATFORM : value;
}

function taskQueueDate(value) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  return date.toLocaleString("zh-CN", {
    month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit",
    hour12: false,
  });
}

function taskQueueSourceLabel(tab) {
  return ({
    collections: "采集任务", publish: "发布任务", autocomment: "评论任务",
    hub: "账号管理", monitors: "作品监控",
  })[tab] || "查看";
}

function taskQueueRow(item) {
  const stateMeta = TASK_QUEUE_STATE_META[item.state] || [item.state || "未知", "skipped"];
  const rawStatus = TASK_QUEUE_RAW_STATUS[item.status] || item.status || "未知";
  const account = item.account_name || (item.account_id ? `账号 #${item.account_id}` : "未绑定账号");
  const scheduled = item.scheduled_at ? `<b>计划 ${taskQueueDate(item.scheduled_at)}</b>` : "";
  const created = item.created_at ? `<small>创建 ${taskQueueDate(item.created_at)}</small>` : "";
  const nextAllowed = item.next_allowed_at ? `<small>最早继续：${taskQueueDate(item.next_allowed_at)}</small>` : "";
  const reason = item.blocked_reason || item.error || "—";
  const reasonClass = item.error && !item.blocked_reason ? " has-error" : "";
  const signal = item.blocked_signal ? `<small>信号：${esc(item.blocked_signal)}</small>` : "";
  return `<tr>
    <td><span class="pill q bare">${esc(item.queue_label)}</span><small class="mut" style="display:block;margin-top:5px">${esc(PF_NAME[item.platform] || item.platform || "—")}</small></td>
    <td><div class="queue-copy"><b title="${esc(item.title)}">${esc(item.title)}</b>${item.detail ? `<small>${esc(item.detail)}</small>` : ""}</div></td>
    <td><div class="queue-account"><b>${esc(account)}</b>${item.account_id ? `<small>ID ${Number(item.account_id)}</small>` : ""}</div></td>
    <td><div class="queue-time">${scheduled || "尽快执行"}${created}</div></td>
    <td><span class="pill ${stateMeta[1]}">${esc(stateMeta[0])}</span><small class="mut" style="display:block;margin-top:5px">${esc(rawStatus)}</small></td>
    <td><div class="queue-reason${reasonClass}">${esc(reason)}${signal}${nextAllowed}</div></td>
    <td class="acttd"><button type="button" class="ghost sm" onclick="openTaskQueueSource('${esc(item.source_tab)}')">${esc(taskQueueSourceLabel(item.source_tab))}</button></td>
  </tr>`;
}

function renderTaskQueuePager(data) {
  const pager = $("queue-pager");
  if (!pager) return;
  TASK_QUEUE_PAGE = Math.max(1, Number(data.page || 1));
  TASK_QUEUE_PAGE_SIZE = Math.max(1, Number(data.page_size || TASK_QUEUE_PAGE_SIZE));
  TASK_QUEUE_PAGES = Math.max(1, Number(data.pages || 1));
  $("queue-page-info").textContent = `第 ${TASK_QUEUE_PAGE} / ${TASK_QUEUE_PAGES} 页 · 共 ${fmtNum(data.total || 0)} 条`;
  $("queue-first").disabled = TASK_QUEUE_PAGE <= 1;
  $("queue-prev").disabled = TASK_QUEUE_PAGE <= 1;
  $("queue-next").disabled = TASK_QUEUE_PAGE >= TASK_QUEUE_PAGES;
  $("queue-last").disabled = TASK_QUEUE_PAGE >= TASK_QUEUE_PAGES;
  if ($("queue-page-size")) $("queue-page-size").value = String(TASK_QUEUE_PAGE_SIZE);
  pager.hidden = false;
}

function renderTaskQueueSummary(summary = {}) {
  ["active", "pending", "running", "blocked", "failed"].forEach(name => {
    const el = $(`queue-stat-${name}`);
    if (el) el.textContent = fmtNum(Number(summary[name] || 0));
  });
  const badge = $("tb-queue");
  if (badge) badge.textContent = fmtNum(Number(summary.active || 0));
  const selected = $("queue-state")?.value || "active";
  document.querySelectorAll("[data-queue-state]").forEach(button =>
    button.classList.toggle("active", button.dataset.queueState === selected));
}

async function refreshTaskQueue(resetPage = false) {
  const body = $("queue-table");
  if (resetPage) TASK_QUEUE_PAGE = 1;
  if (!body) return;
  if (TASK_QUEUE_LOADING) { TASK_QUEUE_REFRESH_PENDING = true; return; }
  TASK_QUEUE_LOADING = true;
  $("queue-table-wrap")?.classList.add("stale");
  const params = new URLSearchParams({
    platform: taskQueuePlatform(),
    queue_type: $("queue-type")?.value || "",
    state: $("queue-state")?.value || "active",
    q: $("queue-query")?.value.trim() || "",
    page: String(TASK_QUEUE_PAGE),
    page_size: String(TASK_QUEUE_PAGE_SIZE),
  });
  try {
    const data = await api("/api/task-queue?" + params.toString());
    body.innerHTML = data.items?.length
      ? data.items.map(taskQueueRow).join("")
      : empty(7, "当前筛选范围内没有任务", "i-inbox", "切换状态或平台范围后再查看");
    renderTaskQueueSummary(data.summary || {});
    renderTaskQueuePager(data);
  } catch (e) {
    body.innerHTML = empty(7, "任务队列加载失败", "i-info", e.message || "请稍后重试");
    if (CURRENT_TAB === "queue") toast("任务队列加载失败：" + e.message, "err");
  } finally {
    TASK_QUEUE_LOADING = false;
    $("queue-table-wrap")?.classList.remove("stale");
    if (TASK_QUEUE_REFRESH_PENDING) {
      TASK_QUEUE_REFRESH_PENDING = false;
      setTimeout(() => refreshTaskQueue(), 0);
    }
  }
}

async function refreshTaskQueueBadge() {
  if (TASK_QUEUE_BADGE_LOADING || CURRENT_TAB === "queue") return;
  TASK_QUEUE_BADGE_LOADING = true;
  try {
    const params = new URLSearchParams({ platform: PLATFORM, state: "active", page_size: "1" });
    const data = await api("/api/task-queue?" + params.toString());
    const badge = $("tb-queue");
    if (badge) badge.textContent = fmtNum(Number(data.summary?.active || 0));
  } catch (e) {
    // 导航徽章是辅助信息，失败时保留上次值，不打断当前页面操作。
  } finally { TASK_QUEUE_BADGE_LOADING = false; }
}

function setTaskQueueState(state) {
  const select = $("queue-state");
  if (!select) return;
  select.value = state;
  if (select._csSync) select._csSync();
  refreshTaskQueue(true);
}

function goTaskQueuePage(page) {
  const target = Math.max(1, Math.min(TASK_QUEUE_PAGES, Number(page) || 1));
  if (target === TASK_QUEUE_PAGE) return;
  TASK_QUEUE_PAGE = target;
  refreshTaskQueue();
}

function setTaskQueuePageSize(value) {
  TASK_QUEUE_PAGE_SIZE = Math.max(10, Math.min(100, Number(value) || 20));
  refreshTaskQueue(true);
}

function openTaskQueueSource(tab) {
  if (!PAGE_META[tab]) return;
  switchTab(tab, true);
}

function esc(s) { return (s || "").toString().replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])); }

function loop() {
  if (INFLIGHT > 0 || document.hidden) return;   // 慢操作/后台标签页不刷新,减少干扰与无效请求
  refreshMonitors(); refreshContents(); refreshWatches(); refreshComments(); refreshDanmakuWatches(); refreshDanmaku(); refreshOverviewChart(); refreshCommentRules(); refreshCommentTasks(); if (pfHasPublish(PLATFORM)) refreshPublish();
  if (CURRENT_TAB === "collections") refreshCollections();
  if (CURRENT_TAB === "risk-control") refreshRiskCenter();
  if (CURRENT_TAB === "queue") refreshTaskQueue(); else refreshTaskQueueBadge();
}

// initial skeletons while data loads
$("mon-table").innerHTML = skeleton(8);
$("content-table").innerHTML = skeleton(8);
$("sd-history-body").innerHTML = skeleton(8);
$("watch-table").innerHTML = skeleton(9);
$("comment-table").innerHTML = skeleton(6);
$("danmaku-watch-table").innerHTML = skeleton(8);
$("danmaku-table").innerHTML = skeleton(6);
$("collection-job-table").innerHTML = collectionTaskSkeleton(3);
$("collection-content-list").innerHTML = collectionResultSkeleton(4);
$("queue-table").innerHTML = skeleton(7);

// restore last-selected section (default: 总览);旧版四个独立页已并入「账号管理」
const VALID_TABS = ["overview", "accounts", "risk-control", "queue", "collections", "monitors", "comments", "danmaku", "hub", "publish", "autocomment", "share-download", "notifications", "settings"];
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
updateTaskQueuePlatformLabel();

onTypeChange(); bindPubFilePicker(); onPubType(); populateWatchAccount(); applyDanmakuForm(); onAcMode(); loadSettings(); refreshAccounts(); refreshBrowserRuntimes(); refreshProxies(); refreshChannels(); loop();
enhanceAllSelects();   // 把所有原生 <select> 升级为美化下拉
enhanceAllMetaControls(); // 分组/标签：当前平台词库下拉，可搜索并新增
enhanceAllDateTime();  // 把 datetime-local 升级为自定义日期选择器
// 编辑弹窗和异步列表会动态插入控件；统一做渐进增强，避免新旧样式混用。
const controlEnhancer = new MutationObserver(records => {
  records.forEach(record => record.addedNodes.forEach(node => {
    if (node.nodeType !== 1) return;
    enhanceAllSelects(node);
    enhanceAllDateTime(node);
  }));
});
controlEnhancer.observe(document.body, { childList: true, subtree: true });

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
