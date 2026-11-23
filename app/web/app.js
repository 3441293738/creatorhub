/* AUTO-GENERATED FROM frontend/app/*.js BY npm run build:ui — EDIT SOURCE MODULES IN frontend/app/ */
// 01-core-modals.js — 核心网络请求封装、全局忙碌指示器、表单辅助与通用对话框模态。
const $ = (id) => document.getElementById(id);
// 有用户发起的慢操作(开浏览器抓评论/发评论/解析链接等)在进行时,暂停 8 秒轮询刷新,
// 否则定时重渲染会把按钮的「…中」加载态冲掉。
let INFLIGHT = 0;
// 全局忙碌徽章:>350ms 才显示(快速轮询不闪),圆环转圈 + 已等待秒数 + 并发数。
// 拿不到真实进度百分比(浏览器自动化/接口都是不透明操作),用计时给"在进行"的清晰感知。
// 判忙 = 有未完成请求(_apiActive)或有用户慢操作(INFLIGHT);并发数用 INFLIGHT(用户点的操作数)。
let _apiActive = 0, _apiFailures = 0, _barTimer = null, _busyStart = 0, _busyTick = null, _busyHint = "";
function _isBusy() { return _apiActive > 0 || INFLIGHT > 0; }
function _busyShow() {
  const sp = $("busy-spinner");
  if (sp && _isBusy()) { sp.classList.add("on"); sp.setAttribute("aria-hidden", "false"); }
}
function _busyLabel() {
  const l = $("bs-label"); if (!l) return;
  const sec = Math.floor((Date.now() - _busyStart) / 1000);
  l.textContent = "处理中 " + (INFLIGHT > 1 ? "×" + INFLIGHT + " · " : "") + sec + " 秒" + (_busyHint ? " · " + _busyHint : "");
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
    _busyHint = "";
    const sp = $("busy-spinner"); if (sp) { sp.classList.remove("on"); sp.setAttribute("aria-hidden", "true"); }
    const l = $("bs-label"); if (l) l.textContent = "处理中";
  }
}
// A late response must never repaint another platform/account or an older query.
const VIEW_REQUESTS = new Map();
let VIEW_SERIAL = 0;
function beginViewRequest(key, scope = () => "") {
  const serial = ++VIEW_SERIAL, platform = PLATFORM, extra = scope();
  VIEW_REQUESTS.set(key, serial);
  return () => VIEW_REQUESTS.get(key) === serial && PLATFORM === platform && scope() === extra;
}
function apiErrorMessage(detail, status) {
  if (Array.isArray(detail)) return detail.map(item => {
    const field = (item.loc || []).filter(x => x !== "body").join(".");
    return `${field ? field + ": " : ""}${item.msg || "输入格式有误"}`;
  }).join("；");
  return typeof detail === "string" ? detail : `请求失败（${status}）`;
}
function scheduleToApi(value) {
  if (!value) return null;
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) throw new Error("预约时间格式无效");
  if (localDateTimeValue(date.toISOString()) !== value.slice(0, 16)) throw new Error("该本地时间不存在，请重新选择");
  return date.toISOString();
}
function localDateTimeValue(value) {
  if (!value) return "";
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) return "";
  const pad = n => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
}
function safeHttpUrl(value) {
  const raw = String(value || "").trim();
  if (!raw) return "";
  try {
    const url = new URL(raw, typeof location === "undefined" ? "http://localhost" : location.href);
    if (!["http:", "https:"].includes(url.protocol)) return "";
    return url.href;
  } catch (_) { return ""; }
}
function safeMediaUrl(value) {
  const raw = String(value || "").trim();
  if (!raw) return "";
  if (raw.startsWith("/api/") || raw.startsWith("blob:") || raw.startsWith("data:image/")) return raw;
  try {
    const baseHref = typeof location === "undefined" ? "http://localhost" : location.href;
    const pageOrigin = typeof location === "undefined" ? "http://localhost" : location.origin;
    const url = new URL(raw, baseHref);
    if (!["http:", "https:"].includes(url.protocol)) return "";
    const host = (url.hostname || "").toLowerCase();
    const isPreview = typeof document !== "undefined" && document.documentElement?.dataset?.preview === "true";
    if (!isPreview && url.protocol === "https:" && (host === "twimg.com" || host.endsWith(".twimg.com"))) {
      return `/api/tw-media-thumb?url=${encodeURIComponent(url.href)}`;
    }
    if (!isPreview && url.protocol === "https:" && (host.endsWith(".cdninstagram.com") || host.endsWith(".fbcdn.net"))) {
      return `/api/ig-media-thumb?url=${encodeURIComponent(url.href)}`;
    }
    if (!isPreview && url.origin !== pageOrigin) {
      return `/api/thumb?url=${encodeURIComponent(url.href)}`;
    }
    return url.href;
  } catch (_) { return ""; }
}
function setHtmlIfChanged(el, nextHtml) {
  if (!el) return false;
  const html = String(nextHtml ?? "");
  if (el._lastHtml === html && el._lastDomHtml === el.innerHTML && el.childNodes.length > 0) return false;
  el._lastHtml = html;
  el.innerHTML = html;
  el._lastDomHtml = el.innerHTML;
  return true;
}
// JSON encoding handles the JS string, then HTML encoding handles the attribute.
function jsArg(value) { return esc(JSON.stringify(String(value ?? ""))); }

function apiFetch(path, options) {
  return fetch(path, { ...options, credentials: "same-origin" });
}

const api = async (path, opts) => {
  const workbenchRequest = globalThis.CreatorHubWorkbench?.requestStarted?.({ path, method: (opts?.method || "GET").toUpperCase() });
  _apiActive++; _barSync();
  let timeout = null, timedOut = false;
  try {
    opts = { ...(opts || {}) };
    if (typeof navigator !== "undefined" && !navigator.onLine && opts.method && opts.method.toUpperCase() !== "GET") {
      throw new Error("当前离线，内容已保留；连接恢复后请手动提交");
    }
    // Bound read-only refreshes; never abort a write and imply it was not sent.
    if ((!opts.method || opts.method.toUpperCase() === "GET") && !opts.signal) {
      const controller = new AbortController();
      opts.signal = controller.signal;
      timeout = setTimeout(() => { timedOut = true; controller.abort(); }, 60000);
    }
    const headers = new Headers(opts.headers || {});
    try {
      const adminToken = sessionStorage.getItem("creatorhub-risk-admin-token") || "";
      if (adminToken) headers.set("X-CreatorHub-Admin-Token", adminToken);
    } catch (e) {}
    headers.set("X-CreatorHub-Actor", "creatorhub-web");
    opts.headers = headers;
    const send = async options => {
      const r = await apiFetch(path, options);
      if (!r.ok) {
        const body = await r.json().catch(() => ({}));
        const error = new Error(apiErrorMessage(body.detail, r.status));
        error.status = r.status;
        error.detail = body.detail;
        throw error;
      }
      return await r.json();
    };
    const result = window.CreatorHubSubmissions ? await window.CreatorHubSubmissions.run(path, opts, send) : await send(opts);
    globalThis.CreatorHubWorkbench?.requestResult?.({ path, request: workbenchRequest, ok: true, method: (opts.method || "GET").toUpperCase() });
    return result;
  } catch (e) {
    _apiFailures++;
    globalThis.CreatorHubWorkbench?.requestResult?.({ path, request: workbenchRequest, ok: false, method: (opts?.method || "GET").toUpperCase() });
    if (timedOut) throw new Error("读取超时，请稍后重试");
    throw e;
  } finally { clearTimeout(timeout); _apiActive--; _barSync(); }
};

// ─── UI helpers ───
const ic = (id) => `<svg aria-hidden="true"><use href="#${id}"/></svg>`;
// 按钮加载态:换成 spinner+label,返回 restore()。配合 INFLIGHT 暂停轮询,加载态不会被重渲染冲掉。
function btnLoading(btn, label) {
  if (!btn) return () => {};
  const html = btn.innerHTML, dis = btn.disabled;
  // Preserve occupied width through nested busy states, without freezing mobile layout.
  const minWidth = btn.style.minWidth, width = btn.style.width;
  const measured = typeof btn.getBoundingClientRect === "function" ? Math.ceil(btn.getBoundingClientRect().width) : 0;
  if (measured) btn.style.width = btn.style.minWidth = `min(${measured}px, 100%)`;
  const busy = btn.getAttribute("aria-busy");
  btn.setAttribute("aria-busy", "true");
  btn.disabled = true; btn.classList.add("busy");
  btn.innerHTML = `<span class="spin"></span>${label ? `<span>${esc(label)}</span>` : ""}`;
  return () => { try {
    btn.innerHTML = html; btn.disabled = dis; btn.style.minWidth = minWidth; btn.style.width = width; btn.classList.remove("busy");
    if (busy === null) btn.removeAttribute("aria-busy"); else btn.setAttribute("aria-busy", busy);
  } catch (e) {} };
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
  // Composer errors belong beside its return action, not over the retry button.
  if (globalThis.CreatorHubWorkbench?.feedback?.({ message: msg, type })) return;
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
  // Long errors remain readable; pointer/keyboard interaction pauses dismissal.
  const duration = type === "err" ? Math.max(ms, 8000) : ms;
  const resume = () => {
    clearTimeout(timer);
    if (!el.matches(":hover") && !el.contains(document.activeElement)) timer = setTimeout(dismiss, duration);
  };
  el.addEventListener("mouseenter", () => clearTimeout(timer));
  el.addEventListener("mouseleave", resume);
  el.addEventListener("focusin", () => clearTimeout(timer));
  el.addEventListener("focusout", resume);
  timer = setTimeout(dismiss, duration);
}
const empty = (cols, text, icon = "i-inbox", sub = "", composer = "") =>
  `<tr><td colspan="${cols}"><div class="empty">` +
  `<div class="empty-ic">${ic(icon)}</div><div class="empty-t">${esc(text)}</div>` +
  `${sub ? `<div class="empty-sub">${esc(sub)}</div>` : ""}${composer ? `<button type="button" class="ghost sm" data-open-composer="${esc(composer)}">${ic("i-plus")}新建</button>` : ""}</div></td></tr>`;
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
  const errorId = el.id ? el.id + "-error" : "";
  let error = errorId ? $(errorId) : field && field.querySelector(".field-error");
  if (message) {
    el.setAttribute("aria-invalid", "true");
    if (!error && field) {
      error = document.createElement("p");
      error.className = "field-error";
      if (errorId) error.id = errorId;
      error.setAttribute("role", "alert");
      field.appendChild(error);
    }
    if (error) {
      error.textContent = message;
      if (errorId) {
        const ids = new Set((el.getAttribute("aria-describedby") || "").split(/\s+/).filter(Boolean));
        ids.add(errorId); el.setAttribute("aria-describedby", [...ids].join(" "));
      }
    }
    return false;
  }
  el.removeAttribute("aria-invalid");
  if (errorId) {
    const ids = (el.getAttribute("aria-describedby") || "").split(/\s+/).filter(id => id && id !== errorId);
    if (ids.length) el.setAttribute("aria-describedby", ids.join(" "));
    else el.removeAttribute("aria-describedby");
  }
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
    btn.innerHTML = ic(show ? "i-eye-off" : "i-eye");
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
    .find(el => !el.hasAttribute("data-ui-closing") && getComputedStyle(el).display !== "none");
}
function modalOpened(el) {
  if (!el) return;
  const active = document.activeElement;
  if (active && active !== document.body) _modalTriggers.set(el, active);
  document.body.classList.add("modal-open");
  globalThis.CreatorHubMotion?.modalOpened?.(el);
}
function modalClosed(el) {
  if (!el) return;
  globalThis.CreatorHubMotion?.modalClosed?.(el);
  if (!_visibleModal()) document.body.classList.remove("modal-open");
  const trigger = _modalTriggers.get(el);
  _modalTriggers.delete(el);
  if (trigger && trigger.isConnected && typeof trigger.focus === "function") {
    setTimeout(() => { if (!_visibleModal()) trigger.focus({ preventScroll: true }); }, 0);
  }
}
function _modalFocusables(el) {
  return [...el.querySelectorAll(
    'button:not([disabled]),a[href],input:not([disabled]),textarea:not([disabled]),select:not([disabled]),[tabindex]:not([tabindex="-1"])'
  )].filter(node => node.offsetParent !== null);
}

// ─── 通用模态(替代原生 prompt / confirm:下拉 / 文本输入 / 确认)───
let _uiResolve = null, _uiGetVal = null, _uiCancelVal = null, _uiSubmit = null, _uiBusy = false, _uiBusyText = "保存中";
function _uiClose(val) {
  if (_uiBusy) return;
  if (typeof _openSelectClose === "function") _openSelectClose();
  if (typeof _openDateClose === "function") _openDateClose();
  if (typeof OPEN_META_COMBO !== "undefined" && OPEN_META_COMBO) OPEN_META_COMBO.close();
  const modal = $("uimodal");
  modal.style.display = "none";
  modalClosed(modal);
  document.removeEventListener("keydown", _uiKey);
  const r = _uiResolve; _uiResolve = null; _uiGetVal = null; _uiSubmit = null;
  if (r) r(val);
}
function _uiKey(e) {
  if (e.isComposing || e.defaultPrevented) return;
  if (e.key === "Escape") uiModalCancel();
  else if (e.key === "Enter" && document.activeElement
      && !["TEXTAREA", "BUTTON"].includes(document.activeElement.tagName)) uiModalOk();
}
function uiModalCancel() { _uiClose(_uiCancelVal); }
function uiEditorError(message, fieldId = "") {
  const error = new Error(message); error.fieldId = fieldId; throw error;
}
function uiEditorNotice(message, error = false) {
  const feedback = $("ui-feedback");
  feedback.hidden = !message;
  feedback.dataset.tone = error ? "error" : "neutral";
  feedback.setAttribute("role", error ? "alert" : "status");
  feedback.textContent = message;
}
async function uiModalOk() {
  if (_uiBusy) return;
  if (!_uiSubmit) { _uiClose(_uiGetVal ? _uiGetVal() : ""); return; }
  const body = $("ui-body"), modal = $("uimodal");
  let value, success = false;
  try {
    const invalid = [...body.querySelectorAll("input,textarea,select")].find(el => el.willValidate && !el.validity.valid);
    if (invalid) uiEditorError(invalid.validationMessage, invalid.id);
    value = _uiGetVal ? _uiGetVal() : "";
    _uiBusy = true; body.inert = true; modal.setAttribute("aria-busy", "true");
    modal.querySelectorAll(".pv-close,#ui-actions .ghost").forEach(button => { button.disabled = true; });
    uiEditorNotice(_uiBusyText === "保存中" ? "正在保存，请稍候…" : `${_uiBusyText}，请稍候…`);
    $("ui-feedback").focus({ preventScroll: true });
    success = await withBusy($("ui-ok"), _uiBusyText, () => _uiSubmit(value)) !== false;
  } catch (error) {
    uiEditorNotice(error.fieldId ? error.message : (_uiBusyText === "保存中" ? "保存未完成：" : "") + error.message, true);
    body.inert = false;
    const field = error.fieldId && $(error.fieldId);
    if (field) {
      setFieldError(field, error.message);
      const details = field.closest("details"); if (details) details.open = true;
      const target = field.closest(".cs,.dt")?.querySelector(".cs-trg,.dt-trg") || field;
      if (target !== field) {
        target.setAttribute("aria-invalid", "true");
        target.setAttribute("aria-describedby", field.getAttribute("aria-describedby") || "");
      }
      target.focus();
    } else $("ui-feedback").focus({ preventScroll: true });
  } finally {
    _uiBusy = false; body.inert = false; modal.removeAttribute("aria-busy");
    modal.querySelectorAll(".pv-close,#ui-actions .ghost").forEach(button => { button.disabled = false; });
  }
  if (success) _uiClose(value);
}
function _uiOpen(title, hint, { okText = "确定", danger = false, wide = false, submit = null, icon = "i-edit", busyText = "保存中", noticeText = undefined } = {}) {
  const previousExtraAction = $("ui-extra-action");
  if (previousExtraAction) previousExtraAction.remove();
  $("ui-title").textContent = title || "";
  $("ui-hint").textContent = hint || "";
  $("ui-hint").hidden = !hint;
  $("ui-icon").setAttribute("href", "#" + (danger ? "i-trash" : icon));
  _uiSubmit = submit; _uiBusy = false; _uiBusyText = busyText || "保存中";
  uiEditorNotice(noticeText !== undefined ? noticeText : (submit ? "更改仅在保存后生效" : ""));
  const ok = $("ui-ok");
  ok.innerHTML = `<svg aria-hidden="true"><use href="#${danger ? "i-trash" : "i-check"}"/></svg>` + esc(okText);
  ok.classList.toggle("danger", !!danger);
  ok.style.cssText = "flex:0 0 auto";
  const modal = $("uimodal");
  modal.dataset.editor = String(wide);
  modal.dataset.danger = String(!!danger);
  modal.querySelector(".rp-box").style.width = wide ? "min(94vw,720px)" : "min(94vw,480px)";
  const body = $("ui-body"); body.inert = false; body.scrollTop = 0;
  body.oninput = body.onchange = event => {
    const field = event.target;
    if (field.getAttribute("aria-invalid") !== "true") return;
    setFieldError(field, "");
    const target = field.closest(".cs,.dt")?.querySelector(".cs-trg,.dt-trg");
    if (target) {
      target.removeAttribute("aria-invalid");
      if (field.getAttribute("aria-describedby")) target.setAttribute("aria-describedby", field.getAttribute("aria-describedby"));
      else target.removeAttribute("aria-describedby");
    }
  };
  modal.style.display = "flex";
  modalOpened(modal);
  document.addEventListener("keydown", _uiKey);
  setTimeout(() => {
    if (modal.style.display === "none" || modal.dataset.uiClosing || _uiBusy) return;
    const el = [...body.querySelectorAll("input,textarea,.cs-trg,.dt-trg,select,button")]
      .find(node => node.offsetParent !== null && !node.classList.contains("cs-native") && !node.classList.contains("dt-native"));
    if (wide) { $("ui-title").tabIndex = -1; $("ui-title").focus({ preventScroll: true }); }
    else if (el) el.focus({ preventScroll: true });
    else (danger ? modal.querySelector("#ui-actions .ghost") : ok).focus();
  }, 30);
}
// 确认框。返回 true / false。danger=true 时确定按钮红色(危险操作)
function uiConfirm({ title = "确认", message = "", okText = "确定", danger = false } = {}) {
  return new Promise(res => {
    _uiResolve = res; _uiGetVal = () => true; _uiCancelVal = false;
    $("ui-body").innerHTML = "";
    _uiOpen(title, message, { okText, danger, icon: "i-info" });
  });
}
// 下拉选择。options:[{value,label,disabled}]。返回选中 value 或 null(取消)
function uiSelect({ title, hint, options, value }) {
  return new Promise(res => {
    _uiResolve = res; _uiCancelVal = null;
    _uiGetVal = () => { const el = $("ui-body").querySelector("select,input,textarea"); return el ? el.value : ""; };
    $("ui-body").innerHTML =
      `<select id="ui-sel" style="width:100%" aria-labelledby="ui-title">` +
      options.map(o => `<option value="${esc(o.value)}"${o.value === value ? " selected" : ""}${o.disabled ? " disabled" : ""}>${esc(o.label)}</option>`).join("") +
      `</select>`;
    enhanceSelect($("ui-sel"));
    _uiOpen(title, hint, { icon: "i-list" });
  });
}
// 文本输入(单行或多行)。返回字符串或 null(取消)
function uiPrompt({ title, hint, value, placeholder, multiline, rows, secret = false, submit = null }) {
  return new Promise(res => {
    _uiResolve = res; _uiCancelVal = null;
    _uiGetVal = () => { const el = $("ui-body").querySelector("select,input,textarea"); return el ? el.value : ""; };
    $("ui-body").innerHTML = multiline
      ? `<textarea id="ui-inp" aria-labelledby="ui-title" rows="${rows || 6}" placeholder="${esc(placeholder || "")}">${esc(value || "")}</textarea>`
      : `<input id="ui-inp" aria-labelledby="ui-title" type="${secret ? "password" : "text"}" value="${esc(value || "")}" placeholder="${esc(placeholder || "")}" autocomplete="${secret ? "current-password" : "off"}">`;
    _uiOpen(title, hint, { submit, wide: !!multiline, okText: submit ? "保存修改" : "确定" });
  });
}

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

// 03-navigation-platform.js — 九平台上下文切换、平台差异化表单显隐与工作台标签页导航。
// ─── 平台切换(抖音 / 小红书) ───

async function exportMonitorReport(explicitBtn = null) {
  const btn = explicitBtn || evtBtn();
  const params = new URLSearchParams({ platform: PLATFORM });
  await withBusy(btn, "导出中", async () => {
    try {
      const response = await apiFetch("/api/reports/monitor.xlsx?" + params.toString());
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
  const response = await apiFetch(path);
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
    for (const [key, value] of Object.entries(contentCaptureBounds())) put(key, value);
  } else if (module === "comment-watches") {
    put("q", $("watch-search") && $("watch-search").value);
    put("group_name", $("watch-group") && $("watch-group").value);
    put("tag", $("watch-tag") && $("watch-tag").value);
  } else if (module === "comments") {
    put("watch_id", COMMENT_SRC);
    put("group_name", COMMENT_GROUP);
    put("tag", COMMENT_TAG);
    put("q", $("comment-query") && $("comment-query").value);
    put("ip_label", $("comment-ip-label") && $("comment-ip-label").value);
    put("reply_type", $("comment-type") && $("comment-type").value);
    put("min_like_count", $("comment-min-likes") && $("comment-min-likes").value);
    put("sort", $("comment-sort") && $("comment-sort").value);
    for (const [key, value] of Object.entries(contentCaptureBounds("comment"))) put(key, value);
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
    for (const [key, value] of Object.entries(contentCaptureBounds("danmaku"))) put(key, value);
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
let GLOBAL_MODE = true;
const PF_NAME = { douyin: "抖音", xhs: "小红书", rednote: "海外小红书", kuaishou: "快手", shipinhao: "视频号", tiktok: "TikTok", bilibili: "哔哩哔哩", instagram: "Instagram", twitter: "Twitter" };
let CURRENT_TAB = "overview";
const PAGE_META = {
  overview: {
    title: "指挥概览", desc: "跨平台查看账号健康态势、高赞内容快览与今日发布排期。"
  },
  "media-hub": {
    title: "汇总预览库", desc: "跨平台统一画廊预览监控作品、批量采集、自有作品与本地下载素材，支持右侧分栏快览。"
  },
  accounts: {
    title: "账号矩阵", desc: "按跨平台人设组管理多账号，支持异常快筛、批量配代理与连续扫码补登。"
  },
  "risk-control": {
    title: "风控中心", desc: "统一管理风控规则，查看账号状态、触发原因、恢复进度与事件记录。"
  },
  monitors: {
    title: "作品监控", desc: "追踪关注的创作者，检查采集进度与新作品。"
  },
  collections: {
    title: "批量作品采集", desc: "按平台支持的来源批量采集作品、评论与可用媒体。"
  },
  comments: {
    title: "评论监控", desc: "订阅作品或账号评论，按来源、分组和标签筛选。"
  },
  danmaku: {
    title: "弹幕监控", desc: "监控短视频播放器内的弹幕，保留每条弹幕在视频中的时间点。"
  },
  hub: {
    title: "矩阵数据与私信", desc: "横向对比全部账号涨粉、播放、0播异常与全平台聚合私信，或下钻单账号明细。"
  },
  "tiktok-tools": {
    title: "用户互动", desc: "搜索其他 TikTok 用户并创建关注、取关、私信、评论或回复任务。"
  },
  publish: {
    title: "内容发布", desc: "支持单账号精细发布或跨平台多账号错峰矩阵群发。"
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
    title: "设置", desc: "管理外观、下载、AI 文案与采集运行配置。"
  },
};
const SINGLE_PF_ONLY_TABS = ["monitors", "collections", "comments", "danmaku", "tiktok-tools", "autocomment"];
function updatePageContext(name = CURRENT_TAB) {
  const meta = PAGE_META[name] || PAGE_META.overview;
  const showGlobalLabel = GLOBAL_MODE && !SINGLE_PF_ONLY_TABS.includes(name);
  const effectiveTitle = (GLOBAL_MODE && name === "publish") ? "矩阵群发与排期" : meta.title;
  const effectiveDesc = (GLOBAL_MODE && name === "publish")
    ? "一次上传素材与主文案，按跨平台矩阵组一键错峰群发，并统一追踪全平台发布进度。"
    : meta.desc;
  if ($("page-title")) $("page-title").textContent = effectiveTitle;
  if ($("page-desc")) $("page-desc").textContent = effectiveDesc;
  if ($("page-platform")) $("page-platform").textContent = showGlobalLabel ? "全平台矩阵" : (PF_NAME[PLATFORM] || "当前平台");
  if ($("page-kicker")) $("page-kicker").textContent = showGlobalLabel ? "跨 9 大平台全局视角" : (pfIsChannels(PLATFORM) ? "本账号工作台" : "单平台精细工作台");
  document.title = `${effectiveTitle} · ${showGlobalLabel ? "全平台" : (PF_NAME[PLATFORM] || "")} | CreatorHub`;
  globalThis.CreatorHubWorkbench?.navigate?.();
}
// 是否支持「发布」面板
function pfHasPublish(pf) { return ["xhs", "rednote", "kuaishou", "douyin", "shipinhao", "tiktok", "bilibili", "instagram", "twitter"].includes(pf); }
// 视频号只有「本账号」数据(助手接口本账号),不支持监控他人作品/评论
function pfIsChannels(pf) { return pf === "shipinhao"; }
function switchPlatform(pf) {
  const wantGlobal = pf === "all";
  if (!wantGlobal && !["douyin", "xhs", "rednote", "kuaishou", "shipinhao", "tiktok", "bilibili", "instagram", "twitter"].includes(pf)) pf = "douyin";
  if (!wantGlobal) globalThis.CreatorHubWorkbench?.platformChanging?.(pf);
  GLOBAL_MODE = wantGlobal;
  if (!wantGlobal) PLATFORM = pf;
  VIEW_REQUESTS.clear();
  CONTENT_PAGE = COMMENT_PAGE = 1;
  COLLECTION_JOBS = []; closeCollectionResults(); renderCollectionJobs(); if ($("tb-col")) $("tb-col").textContent = "0";
  selContent.clear(); selComment.clear();
  CONTENT_SRC = CONTENT_GROUP = CONTENT_TAG = "";
  CONTENT_SOURCE_CACHE.clear();
  ["content-captured-from", "content-captured-to"].forEach(id => { if ($(id)) $(id).value = ""; });
  for (const kind of ["comment", "danmaku"]) {
    WATCH_RECORD_STATE[kind].cache.clear();
    [kind + "-captured-from", kind + "-captured-to"].forEach(id => { if ($(id)) $(id).value = ""; });
  }
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
  try {
    localStorage.setItem("dym-global", wantGlobal ? "1" : "0");
    if (!wantGlobal) localStorage.setItem("dym-pf", pf);
  } catch (e) {}
  if ($("queue-platform")) {
    $("queue-platform").value = wantGlobal ? "all" : "current";
    $("queue-platform")._csSync?.();
  }
  applyPlatformUI();
  ["t-interval", "w-interval", "d-w-interval"].forEach(id => $(id)?._monitorIntervalSync?.());
  // 切换后立刻刷新对应数据
  refreshAccounts(); refreshMonitors(); refreshContents(); refreshWatches(); refreshComments(); refreshDanmakuWatches(); refreshDanmaku(); refreshCollections();
  updateTaskQueuePlatformLabel();
  if (CURRENT_TAB === "queue") refreshTaskQueue(true); else refreshTaskQueueBadge();
  if (CURRENT_TAB === "risk-control") refreshRiskCenter(true, false);
  populateAcAccount(); onAcMode(); refreshCommentRules(); refreshCommentTasks();
  if (pfHasPublish(PLATFORM)) refreshPublish();
  if (CURRENT_TAB === "overview") loop();
}
function applyPlatformUI() {
  document.body.classList.toggle("pf-all", GLOBAL_MODE);
  document.body.classList.toggle("pf-douyin", !GLOBAL_MODE && PLATFORM === "douyin");
  document.body.classList.toggle("pf-xhs", !GLOBAL_MODE && PLATFORM === "xhs");
  document.body.classList.toggle("pf-kuaishou", !GLOBAL_MODE && PLATFORM === "kuaishou");
  document.body.classList.toggle("pf-shipinhao", !GLOBAL_MODE && PLATFORM === "shipinhao");
  document.body.classList.toggle("pf-tiktok", !GLOBAL_MODE && PLATFORM === "tiktok");
  document.body.classList.toggle("pf-bilibili", !GLOBAL_MODE && PLATFORM === "bilibili");
  document.body.classList.toggle("pf-rednote", !GLOBAL_MODE && PLATFORM === "rednote");
  document.body.classList.toggle("pf-instagram", !GLOBAL_MODE && PLATFORM === "instagram");
  document.body.classList.toggle("pf-twitter", !GLOBAL_MODE && PLATFORM === "twitter");
  // 视频号:只有本账号数据,隐藏「监控他人作品/评论」相关入口(.notsh-only)
  document.body.classList.toggle("pf-channels", !GLOBAL_MODE && pfIsChannels(PLATFORM));
  if (!["douyin", "bilibili"].includes(PLATFORM) && CURRENT_TAB === "danmaku") switchTab("overview");
  document.querySelectorAll(".pswitch button").forEach(b => {
    const active = GLOBAL_MODE ? b.dataset.pf === "all" : b.dataset.pf === PLATFORM;
    b.classList.toggle("active", active);
    b.setAttribute("aria-selected", active ? "true" : "false");
    b.tabIndex = active ? 0 : -1;
  });
  document.querySelectorAll(".dy-only").forEach(e => e.classList.toggle("hidden", PLATFORM !== "douyin"));
  document.querySelectorAll(".xhs-only").forEach(e => e.classList.toggle("hidden", PLATFORM !== "xhs"));
  document.querySelectorAll(".ks-only").forEach(e => e.classList.toggle("hidden", PLATFORM !== "kuaishou"));
  document.querySelectorAll(".sh-only").forEach(e => e.classList.toggle("hidden", PLATFORM !== "shipinhao"));
  document.querySelectorAll(".tk-only").forEach(e => e.classList.toggle("hidden", PLATFORM !== "tiktok"));
  document.querySelectorAll(".tk-hide").forEach(e => e.classList.toggle("hidden", PLATFORM === "tiktok"));
  document.querySelectorAll(".bili-only").forEach(e => e.classList.toggle("hidden", PLATFORM !== "bilibili"));
  document.querySelectorAll(".bili-hide").forEach(e => e.classList.toggle("hidden", PLATFORM === "bilibili"));
  document.querySelectorAll(".rn-only").forEach(e => e.classList.toggle("hidden", PLATFORM !== "rednote"));
  document.querySelectorAll(".rn-hide").forEach(e => e.classList.toggle("hidden", PLATFORM === "rednote"));
  document.querySelectorAll(".xhs-rn-only").forEach(e => e.classList.toggle("hidden", !["xhs", "rednote"].includes(PLATFORM)));
  document.querySelectorAll(".ig-only").forEach(e => e.classList.toggle("hidden", PLATFORM !== "instagram"));
  document.querySelectorAll(".ig-hide").forEach(e => e.classList.toggle("hidden", PLATFORM === "instagram"));
  document.querySelectorAll(".tw-only").forEach(e => e.classList.toggle("hidden", PLATFORM !== "twitter"));
  document.querySelectorAll(".tw-hide").forEach(e => e.classList.toggle("hidden", PLATFORM === "twitter"));
  document.querySelectorAll(".danmaku-only").forEach(e => e.classList.toggle("hidden", !["douyin", "bilibili"].includes(PLATFORM)));
  document.querySelectorAll(".notsh-only").forEach(e => {
    const allowOnChannels = pfIsChannels(PLATFORM) && (
      e.dataset?.tab === "monitors"
      || e.dataset?.tab === "comments"
      || (e.classList.contains("quick-action") && (e.getAttribute("onclick") || "").includes("'monitors'"))
    );
    const hideByPlatform = (e.classList.contains("tk-hide") && PLATFORM === "tiktok") || (e.classList.contains("bili-hide") && PLATFORM === "bilibili") || (e.classList.contains("rn-hide") && PLATFORM === "rednote");
    e.classList.toggle("hidden", hideByPlatform || (pfIsChannels(PLATFORM) && !allowOnChannels));
  });
  document.querySelectorAll(".collect-only").forEach(e => e.classList.toggle(
    "hidden", !["douyin", "xhs", "rednote", "kuaishou", "shipinhao", "tiktok", "bilibili", "instagram", "twitter"].includes(PLATFORM)));
  // 全平台模式下隐藏所有单平台专属采集/监控/互动入口与分组标题
  if (GLOBAL_MODE) {
    document.querySelectorAll(".single-pf-only").forEach(e => e.classList.add("hidden"));
  } else {
    document.querySelectorAll("#nav-sep-collect").forEach(e => e.classList.remove("hidden"));
  }
  if ($("nav-publish-label")) $("nav-publish-label").textContent = GLOBAL_MODE ? "矩阵群发" : "内容发布";
  if ($("nav-hub-label")) $("nav-hub-label").textContent = GLOBAL_MODE ? "矩阵数据与私信" : "数据与私信";
  document.querySelectorAll(".meta-scope").forEach(e => {
    e.textContent = (PF_NAME[PLATFORM] || "当前平台") + "内独立";
  });
  // 发布面板入口
  document.querySelectorAll(".pub-only").forEach(e => e.classList.toggle("hidden", !GLOBAL_MODE && !pfHasPublish(PLATFORM)));
  // 发布面板文案随平台切换
  const ks = PLATFORM === "kuaishou", dy = PLATFORM === "douyin", sph = PLATFORM === "shipinhao", tk = PLATFORM === "tiktok", bili = PLATFORM === "bilibili", rn = PLATFORM === "rednote", ig = PLATFORM === "instagram", tw = PLATFORM === "twitter";
  const pubType = $("pub-type");
  if (pubType) {
    const before = pubType.value;
    if (tk) pubType.innerHTML = '<option value="video">视频</option>';
    else if (![...pubType.options].some(o => o.value === "images")) {
      pubType.innerHTML = '<option value="images">图文（图集）</option><option value="video">视频</option>';
      pubType.value = before === "video" ? "video" : "images";
    }
    if (tk) pubType.value = "video";
    if (pubType.value !== before) onPubType();
  }
  const pubSub = $("pub-head-sub");
  if (pubSub) pubSub.textContent = dy ? "上传图集 / 视频到抖音创作平台(实验性)"
    : ks ? "上传图集 / 视频到快手创作平台(实验性)"
    : sph ? "上传图集 / 视频到视频号助手(实验性)"
    : tk ? "上传视频到 TikTok Studio(实验性)"
    : bili ? "上传图文动态 / 视频稿件到哔哩哔哩创作中心"
    : ig ? "上传单图 / 轮播 / Reels / Story 到 Instagram"
    : tw ? "发布纯文本 / 图集（最多 4 图） / 视频推文到 X (Twitter)"
    : rn ? "上传图集 / 视频到海外小红书 creator.rednote.com"
    : "上传图集 / 视频到小红书(实验性)";
  if ($("pub-head-lead")) $("pub-head-lead").textContent = tw ? "发布推文" : (ks || dy || sph || tk || bili || ig) ? "发布作品" : "发布笔记";
  const titleCap = tw ? ($("pub-tw-long")?.value === "1" ? 4000 : 280) : ig ? 2200 : bili ? 80 : rn ? 40 : dy ? 30 : sph ? 16 : 20;
  if ($("pub-title-label-text")) $("pub-title-label-text").textContent = tw ? `推文首行标题（可选，合并总重 ≤${titleCap}）` : rn ? "标题(≤20 汉字 / 40 英文字符)" : `标题(≤${titleCap} 字)`;
  if ($("pub-title")) {
    $("pub-title").maxLength = titleCap;
    $("pub-title").placeholder = tw ? "推文首行（可选，直接填正文也可；URL 固定计 23 权重）" : ig ? "Caption 首行标题（可选，正文与话题自动合并）" : bili ? "填写视频稿件标题（图文动态可选）" : tk ? "视频说明的首行标题" : (ks || dy || sph) ? "给作品起个标题" : "给笔记起个标题";
  }
  if ($("pub-dy-opts")) $("pub-dy-opts").classList.toggle("hidden", !["douyin", "xhs", "rednote", "kuaishou"].includes(PLATFORM));
  if (bili) { onBiliCopyrightChange(); if ($("pub-acc")?.value) loadBiliPartitions($("pub-acc").value); }
  const pubHintText = dy
    ? "发布通过自动化抖音创作平台完成。首次登录或触发验证时，请在弹出窗口中完成短信验证或扫码；视频上传后还需等待转码。注意：定时发布可能因本人验证而暂停，建议发布时在场。"
    : ks
    ? "发布通过自动化快手创作平台完成。若遇验证码或需要补充封面，请在弹出窗口中手动处理；定时任务由后台引擎按计划执行。"
    : sph
    ? "发布通过自动化视频号助手完成。视频需等待转码，发布前可能要求补充封面、实名或人脸验证，请在弹出窗口中处理。注意：平台页面改版后可能需要重新适配。"
    : tk
    ? "发布通过账号独立的可见 TikTok Studio 页面完成，目前支持单视频、话题、可见范围、评论、Duet 与 Stitch。提交只点击一次；若显示“结果待确认”，请先到 TikTok Studio 核对，系统不会自动重发。"
    : bili
    ? "视频稿件通过哔哩哔哩创作中心完成上传，并按所选分区、自制/转载、创作者声明及弹幕/评论权限提交；图文通过动态页发布。提交只执行一次；若显示“结果待确认”，请先到创作中心核对，系统不会自动重发。"
    : ig
    ? "支持单图、混合多图轮播（最多 10 项）、Reels 短视频与 Story 快拍。默认优先走两阶段 rupload + configure 直连协议，并在普通兼容失败时回退独立浏览器 Create 弹窗；若显示“结果待确认”，请先到 Instagram 主页核对。"
    : tw
    ? "支持纯文本推文、最多 4 张图片或单个视频/GIF，可配置回复权限与首评自回复串推。通过独立可见浏览器窗口完成上传并拦截 CreateTweet 响应确认，提交只执行一次；若显示“结果待确认”，请先到 x.com 主页核对。"
    : rn
    ? "发布通过海外小红书 creator.rednote.com 独立可见浏览器窗口完成（拦截 webapi.rednote.com/web_api/sns/v2/note 确认），不混用国内小红书会话。提交只点击一次；若显示“结果待确认”，请先到平台核对。"
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
    wk.innerHTML = (PLATFORM === "xhs" || PLATFORM === "rednote")
      ? '<option value="auto">类型:自动识别</option><option value="video">单条笔记</option><option value="user">创作者近期笔记</option>'
      : '<option value="auto">类型:自动识别</option><option value="video">单条视频</option><option value="user">账号近期作品</option>';
    if ([...wk.options].some(o => o.value === cur)) wk.value = cur;
  }
  const wl = $("w-url-label");
  if (wl) wl.textContent = (PLATFORM === "xhs" || PLATFORM === "rednote")
    ? "笔记 / 创作者主页 / 完整分享文案 / ID"
    : PLATFORM === "kuaishou" ? "作品 / 创作者主页 / 完整分享文案 / ID"
    : PLATFORM === "shipinhao" ? "分享链接 / 作品 objectId / 创作者 v2_...@finder"
    : PLATFORM === "bilibili" ? "视频 BV/av / 图文动态 / 空间主页 / 完整分享文案"
    : PLATFORM === "instagram" ? "帖子 /p/ / Reels /reel/ / 创作者 @username / 主页链接"
    : PLATFORM === "twitter" ? "推文 /status/ / 创作者 @username / x.com 主页链接"
    : "视频 / 账号主页 / 完整分享文案 / ID";
  if ($("w-url")) $("w-url").placeholder = PLATFORM === "xhs"
    ? "直接粘贴整段小红书分享文案，将自动提取笔记或主页链接"
    : PLATFORM === "kuaishou" ? "直接粘贴整段快手分享文案，将自动提取作品或主页链接"
    : PLATFORM === "shipinhao" ? "粘贴 weixin.qq.com/sph/... 分享链、视频号作品链接、objectId 或 v2_...@finder"
    : PLATFORM === "tiktok" ? "直接粘贴整段 TikTok 分享文案，将自动提取视频或主页链接"
    : PLATFORM === "rednote" ? "粘贴 www.rednote.com/zh/discovery/item/... 笔记链接、user/profile/... 主页链接或 24 位 ID"
    : PLATFORM === "bilibili" ? "直接粘贴整段哔哩哔哩分享文案、BV/av 号、opus 动态链接或 space 主页"
    : PLATFORM === "instagram" ? "粘贴 instagram.com/p/...、/reel/...、创作者主页链接或 @username"
    : PLATFORM === "twitter" ? "粘贴 x.com/.../status/...、创作者主页链接或 @username"
    : "直接粘贴整段抖音分享文案，将自动提取视频或主页链接";
  resetWatchTargetState();
  const ckl = $("ck-label");
  if (ckl) ckl.textContent = PLATFORM === "xhs"
    ? "完整 Cookie(含 a1;发布需创作者会话)"
    : PLATFORM === "kuaishou" ? "完整 Cookie(含 userId 与 web_st)"
    : PLATFORM === "rednote" ? "完整 rednote.com Cookie(含 web_session / a1;禁混用 xiaohongshu.com)"
    : PLATFORM === "bilibili" ? "完整 Cookie(含 SESSDATA 与 bili_jct)"
    : PLATFORM === "instagram" ? "完整 Cookie(含 sessionid、ds_user_id 与 csrftoken)"
    : PLATFORM === "twitter" ? "Token (auth_token) / 多字段凭证 / 完整 Cookie"
    : "完整 Cookie(含 sessionid)";
  if ($("ck-val")) $("ck-val").placeholder = PLATFORM === "xhs"
    ? "从 creator.xiaohongshu.com 登录后复制完整 Cookie"
    : PLATFORM === "kuaishou" ? "从 www.kuaishou.com 登录后复制完整 Cookie"
    : PLATFORM === "rednote" ? "从 www.rednote.com 或 creator.rednote.com 登录后复制完整 Cookie"
    : PLATFORM === "bilibili" ? "从 www.bilibili.com 或 member.bilibili.com 登录后复制完整 Cookie"
    : PLATFORM === "instagram" ? "从 www.instagram.com 登录后复制完整 Cookie(含 sessionid)"
    : PLATFORM === "twitter" ? "可直接粘贴 40 位 auth_token、账号----密码----Token 文本、JSON 数组或 x.com 完整 Cookie"
    : "从浏览器开发者工具复制完整 Cookie";
  applyMonitorForm();
  applyCollectionForm();
  if (["douyin", "bilibili"].includes(PLATFORM)) applyDanmakuForm();
  if ($("t-kind") && !["xhs", "rednote"].includes(PLATFORM)) $("t-kind").value = "creator";
  // 全平台视角下不展示单平台专属面板：若当前停留在单平台页，自动回到指挥概览
  if (GLOBAL_MODE && SINGLE_PF_ONLY_TABS.includes(CURRENT_TAB)) {
    switchTab("overview");
    return;
  }
  // 视频号不支持自动评论规则:若正停在自动评论面板,切回本账号管理
  if (!GLOBAL_MODE && pfIsChannels(PLATFORM)) {
    const cur = (document.querySelector('.navitem.active') || {}).dataset;
    if (cur && cur.tab === "autocomment") switchTab("hub");
    // 视频号本账号只有「我的作品 / 数据」;若停在关注/粉丝/私信子页,切回我的作品
    if (["following", "fans", "dm"].includes(HUB_TAB)) switchHubTab("myworks");
  }
  if (!GLOBAL_MODE && PLATFORM === "tiktok" && CURRENT_TAB === "autocomment") switchTab("hub");
  if (!GLOBAL_MODE && PLATFORM !== "tiktok" && CURRENT_TAB === "tiktok-tools") switchTab("accounts");
  if (!GLOBAL_MODE && PLATFORM === "tiktok" && ["following", "fans", "dm"].includes(HUB_TAB)) switchHubTab("myworks");
  if (!GLOBAL_MODE && PLATFORM === "rednote" && CURRENT_TAB === "autocomment") switchTab("hub");
  if (!GLOBAL_MODE && PLATFORM === "rednote" && ["following", "fans", "dm"].includes(HUB_TAB)) switchHubTab("myworks");
  if (!GLOBAL_MODE && !["douyin", "xhs", "rednote", "kuaishou", "shipinhao", "tiktok", "bilibili", "instagram", "twitter"].includes(PLATFORM) && CURRENT_TAB === "collections") switchTab("overview");
  // 不支持发布的平台:若正停在该面板则回到总览(当前九平台均支持,兜底保留)
  if (!GLOBAL_MODE && !pfHasPublish(PLATFORM)) {
    const pub = document.querySelector('[data-panel="publish"]');
    if (pub && pub.style.display !== "none") switchTab("overview");
  }
  csSyncAll();   // 平台切换可能改了下拉选项/值,同步自定义下拉显示
  requestAnimationFrame(() => window.dispatchEvent(new Event("creatorhub:layout")));
  updatePageContext();
}
const SMART_TARGET_PARSE_SEQ = Object.create(null);

function smartTargetPlatformName(platform = PLATFORM) {
  return PF_NAME[platform] || (platform === "wechat" ? "微信/视频号" : platform === "generic" ? "其他站点" : String(platform || "当前平台"));
}

function setSmartTargetFeedback(feedbackId, message, state = "") {
  const feedback = $(feedbackId);
  if (!feedback) return;
  feedback.textContent = message;
  feedback.className = `smart-target-feedback${state ? ` ${state}` : ""}`;
}

function resetSmartTargetState(inputId, feedbackId, message) {
  const input = $(inputId);
  if (input) input.removeAttribute("aria-invalid");
  SMART_TARGET_PARSE_SEQ[inputId] = (SMART_TARGET_PARSE_SEQ[inputId] || 0) + 1;
  setSmartTargetFeedback(feedbackId, message);
}

function isChannelsWorkUrl(value) {
  const text = String(value || "").trim();
  return /(?:^https?:\/\/)?(?:www\.)?weixin\.qq\.com\/sph\//i.test(text)
    || /(?:^https?:\/\/)?channels\.weixin\.qq\.com\/(?:finder-preview\/pages\/(?:sph|feed)|web\/pages\/feed)/i.test(text)
    || /^\d{16,20}$/.test(text);
}

function isChannelsCreatorId(value) {
  const text = String(value || "").trim();
  return /^v2_[0-9a-f]{16,}(?:@finder)?$/i.test(text) || /^sph[0-9a-z_]{5,}$/i.test(text);
}

async function copyTextValue(value, okMsg = "已复制", errMsg = "复制失败，请手动复制") {
  const text = String(value || "").trim();
  if (!text) return false;
  try {
    if (navigator.clipboard && typeof navigator.clipboard.writeText === "function") {
      await navigator.clipboard.writeText(text);
      toast(okMsg, "ok", 1800);
      return true;
    }
  } catch (e) {}
  try {
    const field = document.createElement("textarea");
    field.value = text;
    field.style.position = "fixed";
    field.style.opacity = "0";
    document.body.appendChild(field);
    field.select();
    const copied = document.execCommand("copy");
    field.remove();
    toast(copied ? okMsg : errMsg, copied ? "ok" : "err", copied ? 1800 : 2800);
    return copied;
  } catch (e) {
    toast(errMsg, "err");
    return false;
  }
}

async function normalizePlatformShareTarget({
  inputId, feedbackId, platform, button = null, quiet = false, resolveChannelsCreator = false,
}) {
  const input = $(inputId);
  if (!input) return null;
  const raw = input.value.trim();
  if (!raw) return raw;

  const seq = (SMART_TARGET_PARSE_SEQ[inputId] || 0) + 1;
  SMART_TARGET_PARSE_SEQ[inputId] = seq;
  const old = button && button.innerHTML;
  if (button) {
    button.disabled = true;
    button.innerHTML = `<span class="spin"></span><span>识别中</span>`;
  }
  input.removeAttribute("aria-invalid");
  setSmartTargetFeedback(feedbackId, "正在清洗分享文案并识别链接…", "parsing");
  try {
    const result = await api("/api/share-download/links", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ share_text: raw, limit: 20 }),
    });
    if (seq !== SMART_TARGET_PARSE_SEQ[inputId]) return input.value.trim();
    const links = Array.isArray(result.links) ? result.links : [];
    let candidate = raw;
    let changed = false;
    if (!links.length) {
      if (platform === "shipinhao" && isChannelsCreatorId(raw)) {
        setSmartTargetFeedback(feedbackId, "视频号创作者标识已识别 ✓", "success");
        return raw;
      }
      if (!(platform === "shipinhao" && resolveChannelsCreator && isChannelsWorkUrl(raw))) {
        setSmartTargetFeedback(feedbackId, "未发现链接，将按作品或用户 ID 直接解析");
        if (!quiet) toast("没有发现链接；也可以直接填写作品或用户 ID", "err");
        return raw;
      }
    } else {
      const expectedPlatform = platform === "shipinhao" ? "wechat" : platform;
      const matched = links.find(item => item && item.platform === expectedPlatform);
      if (!matched) {
        const names = [...new Set(links.map(item => smartTargetPlatformName(item.platform)))].join("、");
        const message = `识别到${names}链接，与当前${smartTargetPlatformName(platform)}页面不匹配`;
        input.setAttribute("aria-invalid", "true");
        setSmartTargetFeedback(feedbackId, message, "error");
        if (!quiet) toast(message, "err");
        return null;
      }
      candidate = String(matched.url || "").trim() || raw;
      changed = candidate !== raw;
      if (changed) input.value = candidate;
    }

    if (platform === "shipinhao" && resolveChannelsCreator && isChannelsWorkUrl(candidate)) {
      if (quiet) {
        setSmartTargetFeedback(
          feedbackId,
          "已识别视频号作品链接，点击「识别」或提交后将自动反查作者 v2_...@finder ✓",
          "success",
        );
        return candidate;
      }
      setSmartTargetFeedback(feedbackId, "正在从视频号作品反查创作者 finderUsername…", "parsing");
      try {
        const resolved = await api("/api/channels/resolve-creator", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ target: candidate }),
        });
        if (seq !== SMART_TARGET_PARSE_SEQ[inputId]) return input.value.trim();
        const username = String(resolved?.username || "").trim();
        const nickname = String(resolved?.nickname || "").trim();
        if (username) {
          input.value = username;
          const label = nickname ? `${nickname} (${username})` : username;
          setSmartTargetFeedback(feedbackId, `已反查到创作者：${label} ✓`, "success");
          toast(`已反查到创作者：${label}`, "ok", 2400);
          return username;
        }
      } catch (resolveErr) {
        if (seq !== SMART_TARGET_PARSE_SEQ[inputId]) return input.value.trim();
        input.setAttribute("aria-invalid", "true");
        setSmartTargetFeedback(feedbackId, resolveErr.message, "error");
        toast(resolveErr.message, "err");
        return null;
      }
    }

    setSmartTargetFeedback(
      feedbackId,
      changed
        ? `已去除无关文案，提取出${smartTargetPlatformName(platform)}链接 ✓`
        : `${smartTargetPlatformName(platform)}链接已识别 ✓`,
      "success",
    );
    return candidate || raw;
  } catch (e) {
    if (seq !== SMART_TARGET_PARSE_SEQ[inputId]) return input.value.trim();
    setSmartTargetFeedback(feedbackId, "自动识别暂未完成，提交时仍会在服务端解析", "error");
    if (!quiet) toast("识别失败：" + e.message, "err");
    return raw;
  } finally {
    if (button && button.isConnected) {
      button.disabled = false;
      button.innerHTML = old;
    }
  }
}

function resetMonitorTargetState() {
  const keywordMode = ["xhs", "rednote"].includes(PLATFORM) && $("t-kind") && $("t-kind").value === "keyword";
  resetSmartTargetState("t-url", "t-url-feedback", keywordMode
    ? "输入要持续监控的搜索关键词"
    : PLATFORM === "shipinhao"
    ? "支持视频号主页链接、v2_...@finder、sph 标识，或单条视频 weixin.qq.com/sph/... 链接（自动反查作者）"
    : "支持完整分享文案、主页长链、平台短链或用户 ID");
}

async function normalizeMonitorTarget(button = null, { quiet = false } = {}) {
  const keywordMode = ["xhs", "rednote"].includes(PLATFORM) && $("t-kind") && $("t-kind").value === "keyword";
  if (keywordMode) return $("t-url").value.trim();
  return normalizePlatformShareTarget({
    inputId: "t-url", feedbackId: "t-url-feedback", platform: PLATFORM, button, quiet,
    resolveChannelsCreator: PLATFORM === "shipinhao",
  });
}

function handleMonitorTargetPaste() {
  setTimeout(() => normalizeMonitorTarget(null, { quiet: true }), 0);
}

function handleMonitorTargetKeydown(event) {
  if (!event || event.key !== "Enter" || event.isComposing) return;
  event.preventDefault();
  addMonitor();
}

function resetCollectionCreatorState() {
  resetSmartTargetState(
    "col-source-ref", "col-source-ref-feedback",
    PLATFORM === "shipinhao"
      ? "可粘贴视频号 v2_...@finder、主页链接，或直接粘贴单条视频 weixin.qq.com/sph/... 链接（自动反查作者）"
      : PLATFORM === "kuaishou"
      ? "使用公开主页作品列表；可粘贴完整分享文案、主页链接、v.kuaishou.com 短链或 user_id"
      : "使用公开主页作品列表，评论和下载继续沿用下方设置",
  );
}

async function normalizeCollectionCreatorTarget(button = null, { quiet = false } = {}) {
  return normalizePlatformShareTarget({
    inputId: "col-source-ref", feedbackId: "col-source-ref-feedback", platform: PLATFORM,
    button, quiet, resolveChannelsCreator: PLATFORM === "shipinhao",
  });
}

function handleCollectionCreatorPaste() {
  setTimeout(() => normalizeCollectionCreatorTarget(null, { quiet: true }), 0);
}

function resetWatchTargetState() {
  resetSmartTargetState(
    "w-url", "w-url-feedback",
    PLATFORM === "shipinhao" && $("w-kind")?.value === "user"
      ? "已选「账号近期作品」：可粘贴 v2_...@finder、主页链接或单条视频 weixin.qq.com/sph/...（自动反查作者）"
      : "支持完整分享文案、作品链接、创作者主页、平台短链或 ID",
  );
}

function normalizeWatchTarget(button = null, options = {}) {
  return normalizePlatformShareTarget({
    inputId: "w-url", feedbackId: "w-url-feedback", platform: PLATFORM,
    button, quiet: !!options.quiet,
    resolveChannelsCreator: PLATFORM === "shipinhao" && $("w-kind")?.value === "user",
  });
}

function handleWatchTargetPaste() {
  setTimeout(() => normalizeWatchTarget(null, { quiet: true }), 0);
}

function handleWatchTargetKeydown(event) {
  if (!event || event.key !== "Enter" || event.isComposing) return;
  event.preventDefault();
  addWatch();
}

function resetDanmakuTargetState() {
  resetSmartTargetState(
    "d-w-url", "d-w-url-feedback",
    PLATFORM === "bilibili"
      ? "支持完整哔哩哔哩分享文案、BV/av 号、视频链接、空间主页或短链"
      : "支持完整抖音分享文案、视频链接、账号主页、短链或 ID",
  );
}

function normalizeDanmakuTarget(button = null, options = {}) {
  return normalizePlatformShareTarget({
    inputId: "d-w-url", feedbackId: "d-w-url-feedback", platform: PLATFORM,
    button, quiet: !!options.quiet,
  });
}

function handleDanmakuTargetPaste() {
  setTimeout(() => normalizeDanmakuTarget(null, { quiet: true }), 0);
}

function handleDanmakuTargetKeydown(event) {
  if (!event || event.key !== "Enter" || event.isComposing) return;
  event.preventDefault();
  addDanmakuWatch();
}

let _LAST_MONITOR_PLATFORM = "";

function applyMonitorForm() {
  const title = $("mon-add-title");
  const lbl = $("t-url-label");
  const parseButton = document.querySelector('[data-panel="monitors"] .smart-target-action');
  const isSph = PLATFORM === "shipinhao";
  if ($("t-acc-wrap")) $("t-acc-wrap").style.display = isSph ? "none" : "";
  if (isSph && $("t-acc")) $("t-acc").value = "";
  const supportsBackfill = !["xhs", "rednote"].includes(PLATFORM);
  if ($("t-backfill-wrap")) $("t-backfill-wrap").style.display = supportsBackfill ? "" : "none";
  if ($("t-backfill") && _LAST_MONITOR_PLATFORM !== PLATFORM) {
    $("t-backfill").value = ["shipinhao", "kuaishou", "tiktok", "bilibili", "instagram", "twitter"].includes(PLATFORM) ? "5" : "";
    $("t-backfill")._csSync?.();
  }
  _LAST_MONITOR_PLATFORM = PLATFORM;
  if (["douyin", "kuaishou", "shipinhao", "tiktok", "bilibili", "instagram", "twitter"].includes(PLATFORM)) {
    const isKs = PLATFORM === "kuaishou";
    const isTikTok = PLATFORM === "tiktok";
    const isBili = PLATFORM === "bilibili";
    const isIg = PLATFORM === "instagram";
    const isTw = PLATFORM === "twitter";
    if (title) title.innerHTML = (isKs || isSph || isBili || isIg || isTw ? '添加创作者监控' : '添加作品监控')
      + ' <span class="sub">监控并下载新作品</span>';
    if (lbl) lbl.textContent = isTikTok
      ? "创作者主页 / 完整分享文案 / @username"
      : isIg
      ? "Instagram 主页链接 / @username / 数字 UID"
      : isTw
      ? "Twitter (X) 主页链接 / @username / 数字 UID"
      : isSph
      ? "创作者主页 / v2_...@finder / 单条视频分享链接"
      : isKs
      ? "创作者主页 / 完整分享文案 / user_id"
      : isBili
      ? "UP 主空间主页 / 完整分享文案 / mid"
      : "创作者主页 / 完整分享文案 / sec_uid";
    $("t-url").placeholder = isTikTok
      ? "直接粘贴整段 TikTok 主页或作品分享文案，将自动提取 @username"
      : isIg
      ? "粘贴 https://www.instagram.com/<username>/ 或填写 @username"
      : isTw
      ? "粘贴 https://x.com/<username> 或填写 @username"
      : isSph
      ? "粘贴视频号主页链接、v2_...@finder、sph 标识或单条视频 weixin.qq.com/sph/... 链接"
      : isKs
      ? "直接粘贴整段快手主页分享文案，将自动提取有效链接"
      : isBili
      ? "直接粘贴整段哔哩哔哩空间或视频分享文案，或填写 UP 主 mid"
      : "直接粘贴整段抖音主页分享文案，将自动提取有效链接";
    if (parseButton) parseButton.classList.remove("hidden");
    resetMonitorTargetState();
    return;
  }
  const kind = $("t-kind") ? $("t-kind").value : "creator";
  if (kind === "keyword") {
    if (title) title.innerHTML = '添加关键词监控 <span class="sub">盯一个搜索词的新笔记</span>';
    if (lbl) lbl.textContent = "搜索关键词";
    $("t-url").placeholder = "例如:口红试色 / 露营装备";
    if (parseButton) parseButton.classList.add("hidden");
  } else {
    if (title) title.innerHTML = '添加创作者监控 <span class="sub">监控并下载新笔记</span>';
    if (lbl) lbl.textContent = "创作者主页 / 完整分享文案 / user_id";
    $("t-url").placeholder = PLATFORM === "rednote" ? "粘贴 www.rednote.com/zh/user/profile/... 主页链接或 24 位 user_id" : "直接粘贴整段小红书主页分享文案，将自动提取有效链接";
    if (parseButton) parseButton.classList.remove("hidden");
  }
  resetMonitorTargetState();
}

// ─── 标签页切换 ───
function switchTab(name, pushHistory = false) {
  if (name === "agent") {
    window.dispatchEvent(new Event("creatorhub:assistant:open"));
    return;
  }
  if (!PAGE_META[name]) name = "overview";
  // 若从全平台入口主动跳转到某个单平台功能（如去添加作品监控），自动退出全平台模式聚焦当前底层平台
  if (GLOBAL_MODE && SINGLE_PF_ONLY_TABS.includes(name)) {
    GLOBAL_MODE = false;
    try { localStorage.setItem("dym-global", "0"); } catch (e) {}
    CURRENT_TAB = name;
    applyPlatformUI();
  }
  globalThis.CreatorHubWorkbench?.beforeNavigate?.();
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
    if (title && (!activePanel || !activePanel.contains(document.activeElement))) title.focus({ preventScroll: true });
  });
  if (name === "hub") { refreshHubSummary(); refreshHubPanel(); }
  else stopDmStream();   // 离开本账号管理即断开私信实时流
  if (name === "share-download") {
    loadShareAccounts();
    refreshShareHistory();
  }
  if (name === "collections") { populateCollectionAccount(); refreshCollections(); }
  if (name === "tiktok-tools") populateTikTokAccounts();
  if (name === "queue") refreshTaskQueue();
  if (name === "risk-control") refreshRiskCenter();
  if (changed && !["hub", "share-download", "collections", "queue", "risk-control"].includes(name)) loop();
}

// 04-auth-login.js — 多平台浏览器扫码登录、预登录指纹选择与 Cookie/Token 凭证导入。
// ─── 扫码登录(真实浏览器窗口) ───
let qrTimer = null;
let preLoginBrowserBackend = "default";
let preLoginBrowserCatalog = null;
function browserChoiceParts(choice) {
  const [backend, runtimeId = ""] = String(choice || "default").split("::", 2);
  return { backend: backend || "default", runtimeId };
}
function browserChoiceOptions(catalog, { localOnly = false } = {}) {
  const backends = catalog.backends || [];
  const defaultBackend = backends.find(item => item.name === catalog.default);
  const local = backends.find(item => item.name === "local");
  const fingerprint = backends.find(item => item.name === "fingerprint_chromium");
  const runtimes = catalog.runtimes || [];
  const options = [];
  if (!localOnly || catalog.default === "local") options.push({
      value: "default",
      label: `跟随全局（${defaultBackend ? defaultBackend.label : catalog.default}）`,
      disabled: !!defaultBackend && !defaultBackend.available,
  });
  if (local) options.push({
    value: "local", label: local.label,
    disabled: !local.available,
  });
  if (!localOnly) runtimes.forEach(runtime => options.push({
    value: `fingerprint_chromium::${runtime.runtime_id}`,
    label: `${runtime.name}${runtime.version ? ` · ${runtime.version}` : ""}${runtime.is_default ? " · 默认" : ""}`
      + (runtime.available ? "" : ` · 不可用：${runtime.detail || "未配置"}`),
    disabled: !runtime.available,
  }));
  if (!localOnly && !runtimes.length && fingerprint) options.push({
    value: "fingerprint_chromium",
    label: fingerprint.label + (fingerprint.available ? "" : ` · 不可用：${fingerprint.detail || "未配置"}`),
    disabled: !fingerprint.available,
  });
  return options;
}
// 新账号尚未落库，扫码前先确定浏览器内核；成功后该选择随账号持久化。
function isDomesticPlatform(pf) {
  return !["tiktok", "rednote", "instagram", "twitter"].includes(pf || "douyin");
}
function isMainlandGeoClient(country, region, city) {
  const c = String(country || "").trim().toUpperCase();
  const r = `${region || ""} ${city || ""}`.toLowerCase();
  if (!c) return false;
  if (["HK", "MO", "TW", "HONG KONG", "MACAU", "MACAO", "TAIWAN"].includes(c)) return false;
  if (/(hong\s*kong|香港|macau|macao|澳门|taiwan|台湾)/i.test(r)) return false;
  return c === "CN" || c === "CHINA" || /中国/.test(String(country || ""));
}
function proxyEndpointSummary(p) {
  const sample = String(p.masked || p.url || "").trim();
  const m = sample.match(/^([a-zA-Z0-9+.-]+):\/\/(?:[^@]*@)?([^/?#]+)/);
  const scheme = m ? m[1].toUpperCase() : "HTTP";
  const hostPort = m ? m[2] : sample;
  const rawLabel = String(p.raw_label ?? "").trim()
    || ((p.label && p.label !== p.masked) ? String(p.label).trim() : "");
  return {
    scheme,
    hostPort,
    rawLabel,
    displayName: rawLabel || hostPort || p.masked || p.label || "未命名代理",
  };
}
function proxyOptionMeta(p, platform = PLATFORM, { currentProxy = "", currentProxyId = null } = {}) {
  const pf = platform || "douyin";
  const domestic = isDomesticPlatform(pf);
  const ep = proxyEndpointSummary(p);
  const isCurrent = Boolean(
    (currentProxyId && Number(p.id) === Number(currentProxyId))
    || (currentProxy && p.masked && p.masked === currentProxy)
  );
  const rawSamePf = Number((p.used_by_platform || {})[pf] || 0);
  const rawTotal = Number(p.used_by || 0);
  const otherSamePf = Math.max(0, rawSamePf - (isCurrent ? 1 : 0));
  const otherTotal = Math.max(0, rawTotal - (isCurrent ? 1 : 0));
  const healthy = p.enabled !== false && !["bad", "auth_error", "blocked"].includes(p.status);
  const geoChecked = Boolean(p.geo_checked || p.exit_ip || p.country);
  const geoMatched = !geoChecked ? null : (domestic ? Boolean(p.is_mainland) : !p.is_mainland);
  const rank = [
    isCurrent ? 0 : 1,
    p.enabled === false ? 2 : (healthy ? 0 : 1),
    geoMatched === true ? 0 : (geoMatched === null ? 1 : 2),
    otherSamePf === 0 ? 0 : 1,
    otherSamePf,
    otherTotal,
    Number(p.id || 0),
  ];
  return { ...ep, pf, domestic, isCurrent, otherSamePf, otherTotal, healthy, geoChecked, geoMatched, rank };
}
function compareProxyRank(a, b) {
  for (let i = 0; i < a.length; i++) {
    if (a[i] !== b[i]) return a[i] - b[i];
  }
  return 0;
}
function formatProxyOptionLabel(p, platform = PLATFORM) {
  const meta = proxyOptionMeta(p, platform);
  const stTxt = { ok: "可连接", bad: "连接失败", auth_error: "认证失败", blocked: "检测受限", unknown: "未测" };
  const geo = p.geo_loc ? ` · ${p.geo_loc}` : "";
  const geoWarn = meta.geoMatched === false
    ? (meta.domestic ? " · ⚠️非大陆" : " · ⚠️大陆节点")
    : "";
  const usageTag = meta.otherSamePf > 0
    ? `⚠️同平台共用(${meta.otherSamePf})`
    : (meta.otherTotal > 0 ? `跨平台(${meta.otherTotal})` : "空闲独立");
  return `${meta.displayName}${geo}${geoWarn} · ${usageTag} · ${stTxt[p.status] || p.status}${p.enabled === false ? " · 已停用" : ""}`;
}
function renderProxyNodeCard(p, meta, { selected = false, preview = false } = {}) {
  const stMap = {
    ok: { cls: "active", txt: "可连接" },
    bad: { cls: "invalid", txt: "连接失败" },
    auth_error: { cls: "invalid", txt: "认证失败" },
    blocked: { cls: "invalid", txt: "检测受限" },
    unknown: { cls: "bare", txt: "未测" },
  };
  const st = p.enabled === false ? { cls: "bare", txt: "已停用" } : (stMap[p.status] || stMap.unknown);
  const geoBadge = !meta.geoChecked
    ? `<span class="pill bare">地区未测</span>`
    : meta.geoMatched
      ? `<span class="pill active bare">${meta.domestic ? "大陆匹配" : "境外匹配"}</span>`
      : `<span class="pill pending bare">⚠️ ${meta.domestic ? "非大陆节点" : "大陆节点"}</span>`;
  const usageBadge = meta.otherSamePf > 0
    ? `<span class="pill pending bare" title="同平台已有 ${meta.otherSamePf} 个其他账号绑定该代理，共用出口 IP 存在关联风险">⚠️ 同平台共用(${meta.otherSamePf})</span>`
    : meta.otherTotal > 0
      ? `<span class="pill bare" title="其他平台有 ${meta.otherTotal} 个账号使用，当前平台内独立">跨平台(${meta.otherTotal})</span>`
      : `<span class="pill active bare">空闲独立</span>`;
  const curBadge = meta.isCurrent && !preview ? `<span class="pill downloading bare">当前绑定</span>` : "";
  const subParts = [p.masked || p.url];
  if (p.exit_ip) subParts.push(`出口 ${p.exit_ip}`);
  if (p.note) subParts.push(p.note);
  const testBtn = p.id && !preview
    ? `<button type="button" class="ghost sm pxp-test-btn" data-pxp-test="${p.id}" title="检测该节点连通性与出口归属地">测试</button>`
    : "";
  const cls = ["pxp-item", selected ? "selected" : "", preview ? "preview" : "", p.enabled === false ? "disabled-node" : ""]
    .filter(Boolean).join(" ");
  const attrs = preview
    ? ""
    : ` data-pxp-url="${esc(p.url)}" role="option" tabindex="0" aria-selected="${selected ? "true" : "false"}"`;
  return `<div class="${cls}"${attrs}>
    <div class="pxp-main">
      ${preview ? "" : `<span class="pxp-radio" aria-hidden="true"></span>`}
      <div class="pxp-copy">
        <div class="pxp-title-line">
          <span class="pxp-name" title="${esc(meta.displayName)}">${esc(meta.displayName)}</span>
          <span class="pxp-proto">${esc(meta.scheme)}</span>
          ${p.geo_loc ? `<span class="pxp-geo">${esc(p.geo_loc)}</span>` : ""}
        </div>
        <div class="pxp-sub-line" title="${esc(subParts.join(" · "))}">${esc(subParts.join(" · "))}</div>
      </div>
    </div>
    <div class="pxp-badges">${curBadge}${geoBadge}${usageBadge}<span class="pill ${st.cls}">${esc(st.txt)}</span>${testBtn}</div>
  </div>`;
}
function mountProxyPickerControl(container, {
  opts = [],
  platform = PLATFORM,
  currentProxy = "",
  currentProxyId = null,
  isAccountEdit = false,
  onSummary = null,
} = {}) {
  const pf = platform || "douyin";
  const domestic = isDomesticPlatform(pf);
  const pfName = (typeof PF_NAME !== "undefined" && PF_NAME[pf]) ? PF_NAME[pf] : pf;
  const computeItems = () => opts
    .map(p => ({ p, meta: proxyOptionMeta(p, pf, { currentProxy, currentProxyId }) }))
    .sort((a, b) => compareProxyRank(a.meta.rank, b.meta.rank));

  let items = computeItems();
  const initialCurrent = items.find(x => x.meta.isCurrent);
  const hasEnabledHealthy = items.some(x => x.meta.healthy);
  const hasCompatibleHealthy = items.some(x => x.meta.healthy && x.meta.geoMatched !== false);
  let mode = isAccountEdit
    ? (initialCurrent
        ? "pool"
        : (currentProxy ? "custom" : (hasCompatibleHealthy ? "auto" : (items.length ? "pool" : "custom"))))
    : (hasCompatibleHealthy
        ? "auto"
        : (domestic ? "direct" : (items.length ? "pool" : "custom")));
  let selectedUrl = initialCurrent
    ? initialCurrent.p.url
    : ((items.find(x => x.meta.healthy && x.meta.geoMatched !== false) || items.find(x => x.meta.healthy) || items[0])?.p?.url || "");
  let filter = "all";
  let query = "";
  let detected = null;

  const currentBarHtml = isAccountEdit ? (() => {
    if (initialCurrent) {
      return `<div class="pxp-cur-bar"><span>当前状态：已绑定代理池节点 <b>${esc(initialCurrent.meta.displayName)}</b></span><code>${esc(initialCurrent.p.masked)}</code></div>`;
    }
    if (currentProxy) {
      return `<div class="pxp-cur-bar"><span>当前状态：使用独立手动代理</span><code>${esc(currentProxy)}</code></div>`;
    }
    return `<div class="pxp-cur-bar"><span>当前状态：<b>本机直连</b>（尚未绑定代理）</span><span>建议绑定独立代理防多号关联</span></div>`;
  })() : "";

  container.innerHTML = `<div class="pxp-wrap">
    ${currentBarHtml}
    <div class="pxp-modes" role="tablist" aria-label="代理分配模式">
      <button type="button" class="pxp-mode-btn" data-pxp-mode="auto"><span class="pxp-mode-dot" id="pxp-auto-dot"></span><span>自动分配</span></button>
      <button type="button" class="pxp-mode-btn" data-pxp-mode="pool" id="pxp-tab-pool">代理池 (${opts.length})</button>
      <button type="button" class="pxp-mode-btn" data-pxp-mode="custom">手动输入</button>
      <button type="button" class="pxp-mode-btn" data-pxp-mode="direct">${isAccountEdit && currentProxy ? "清除(直连)" : "本机直连"}</button>
    </div>
    <div id="pxp-panel-auto" class="pxp-card"></div>
    <div id="pxp-panel-pool" class="stack" style="gap:8px">
      <div class="pxp-toolbar">
        <div class="pxp-filters" id="pxp-filters">
          <button type="button" class="pxp-flt-btn" data-pxp-flt="all" id="pxp-flt-all">全部</button>
          <button type="button" class="pxp-flt-btn" data-pxp-flt="idle" id="pxp-flt-idle">本平台空闲</button>
          <button type="button" class="pxp-flt-btn" data-pxp-flt="match" id="pxp-flt-match">${domestic ? "大陆匹配" : "境外匹配"}</button>
        </div>
        <input id="pxp-search" class="pxp-search" type="search" placeholder="搜索备注、地区、IP 或端口…" aria-label="搜索代理池节点" autocomplete="off">
      </div>
      <div id="pxp-list" class="pxp-list" role="listbox" aria-label="代理池节点列表"></div>
      <div id="pxp-pool-warn" hidden></div>
    </div>
    <div id="pxp-panel-custom" class="pxp-card">
      <div class="pxp-card-head">
        <b>填写指定代理地址</b>
        <span class="mut" style="font-size:11px">支持 HTTP / SOCKS5 及裸 host:port:user:pass</span>
      </div>
      <div class="pxp-custom-row">
        <input id="pxp-custom-url" type="text" placeholder="http://user:pass@host:port 或 socks5://host:port" autocomplete="off" aria-label="代理地址">
        <button type="button" class="ghost sm" id="pxp-btn-detect">${ic("i-network")}检测连通性</button>
      </div>
      <div id="pxp-detect-msg" class="pxp-detect-msg">可点击「检测连通性」自动识别 HTTP/SOCKS5 协议并核验出口归属地。${isAccountEdit && currentProxy && !initialCurrent ? `留空请切至「清除(直连)」。` : ""}</div>
      <div class="pxp-save-row">
        <label class="pxp-save-chk">
          <input type="checkbox" id="pxp-save-pool" checked>
          <span>同时保存到代理池（方便后续一键选用）</span>
        </label>
        <input id="pxp-custom-label" class="pxp-label-inp" type="text" placeholder="代理池备注（可选，如 住宅-广东-01）" autocomplete="off" aria-label="代理池备注名">
      </div>
    </div>
    <div id="pxp-panel-direct" class="pxp-card">
      <div class="pxp-card-head">
        <b>${isAccountEdit && currentProxy ? "清除代理并改用本机直连" : "使用本机网络直连（不走代理）"}</b>
        <span class="pill pending bare">⚠️ 暴露本机真实出口 IP</span>
      </div>
      <div id="pxp-direct-guard" hidden></div>
      <p>${domestic
        ? `系统不强制拦截本机直连，单账号本地运营可正常使用；但若在「${esc(pfName)}」下同时运营多个账号，共用本机真实出口 IP 容易触发关联限流或设备安全验证。`
        : `⚠️ 「${esc(pfName)}」属于海外平台，强烈建议绑定境外独立代理。本机直连可能受本地网络环境限制导致连接超时，且多账号共用同一 IP 易触发风控。`}</p>
    </div>
  </div>`;

  const searchInp = container.querySelector("#pxp-search");
  const customUrlInp = container.querySelector("#pxp-custom-url");
  const customLblInp = container.querySelector("#pxp-custom-label");
  const savePoolChk = container.querySelector("#pxp-save-pool");
  const detectMsgEl = container.querySelector("#pxp-detect-msg");
  const detectBtn = container.querySelector("#pxp-btn-detect");

  const updateUI = () => {
    items = computeItems();
    const healthyItems = items.filter(x => x.meta.healthy);
    const compatibleHealthyItems = healthyItems.filter(x => x.meta.geoMatched !== false);
    const idleItems = items.filter(x => x.p.enabled !== false && x.meta.otherSamePf === 0);
    const matchItems = items.filter(x => x.p.enabled !== false && x.meta.geoMatched === true);
    const idealAutoItems = compatibleHealthyItems.filter(x => x.meta.otherSamePf === 0);
    const autoCandidate = idealAutoItems[0] || compatibleHealthyItems[0] || null;

    const directGuard = container.querySelector("#pxp-direct-guard");
    if (directGuard) {
      const showGuard = !isAccountEdit && domestic && healthyItems.length > 0 && !compatibleHealthyItems.length;
      directGuard.hidden = !showGuard;
      directGuard.innerHTML = showGuard
        ? `<div class="pxp-inline-warn">${ic("i-shield")}<span>已自动防呆切至「本机直连」：检测到代理池内现有可用节点均为境外 IP，海外住宅代理商通常会屏蔽「${esc(pfName)}」等大陆域名导致连接失败（ERR_SOCKS_CONNECTION_FAILED）。如需多号防关联，请点「手动输入」添加中国大陆代理。</span></div>`
        : "";
    }

    container.querySelectorAll("[data-pxp-mode]").forEach(btn => {
      btn.classList.toggle("active", btn.dataset.pxpMode === mode);
    });
    const dot = container.querySelector("#pxp-auto-dot");
    if (dot) dot.className = "pxp-mode-dot " + (idealAutoItems.length > 0 ? "ok" : "warn");
    const tabPool = container.querySelector("#pxp-tab-pool");
    if (tabPool) tabPool.textContent = `代理池 (${items.length})`;

    ["auto", "pool", "custom", "direct"].forEach(m => {
      const panel = container.querySelector(`#pxp-panel-${m}`);
      if (panel) panel.hidden = mode !== m;
    });

    // Auto panel content
    const autoPanel = container.querySelector("#pxp-panel-auto");
    if (autoPanel) {
      const statsHtml = `<div style="display:flex;flex-wrap:wrap;gap:5px">
        <span class="pill ${healthyItems.length ? "active" : "bare"} bare">可用 ${healthyItems.length}/${items.length}</span>
        <span class="pill ${idleItems.length ? "active" : "pending"} bare">本平台空闲 ${idleItems.length}</span>
        <span class="pill ${matchItems.length ? "active" : "bare"} bare">${domestic ? "大陆匹配" : "境外匹配"} ${matchItems.length}</span>
      </div>`;
      if (!items.length || !healthyItems.length) {
        autoPanel.innerHTML = `<div class="pxp-card-head"><b>自动分配策略</b>${statsHtml}</div>
          <p>${!items.length
            ? `当前代理池为空，${isAccountEdit ? "无法自动分配，请先在「手动输入」中填写代理并勾选保存到代理池。" : "继续登录将自动回退为本机直连；建议切换至「手动输入」添加首个代理节点。"}`
            : `代理池内暂无启用且连通正常的节点，${isAccountEdit ? "请切换至「代理池」测试启用节点或使用「手动输入」。" : "继续登录将回退为本机直连，也可去「代理池」重测节点。"}`}</p>
          <div class="row" style="gap:8px">
            <button type="button" class="ghost sm" data-pxp-jump="custom">${ic("i-plus")}手动输入新代理</button>
            ${items.length ? `<button type="button" class="ghost sm" data-pxp-jump="pool">查看池内节点 (${items.length})</button>` : ""}
          </div>`;
      } else if (!compatibleHealthyItems.length) {
        autoPanel.innerHTML = `<div class="pxp-card-head"><b>自动分配策略（地区防错配保护）</b>${statsHtml}</div>
          <p>${domestic
            ? `⚠️ 代理池内现有 ${healthyItems.length} 个可用节点均为境外出口 IP（无中国大陆节点）。由于海外代理商普遍屏蔽大陆网站，自动分配已启用防呆保护，${isAccountEdit ? "不会强行绑定境外节点，请添加大陆代理或切至「清除(直连)」。" : "继续登录将自动走「本机直连」，避免出现 ERR_SOCKS_CONNECTION_FAILED。"}`
            : `⚠️ 代理池内现有可用节点均为中国大陆出口 IP，无法直达「${esc(pfName)}」，请切换至「手动输入」添加境外独立代理。`}</p>
          <div class="row" style="gap:8px">
            <button type="button" class="ghost sm" data-pxp-jump="custom">${ic("i-plus")}手动输入${domestic ? "大陆" : "境外"}代理</button>
            ${domestic ? `<button type="button" class="ghost sm" data-pxp-jump="direct">改选本机直连</button>` : ""}
          </div>`;
      } else {
        const desc = idealAutoItems.length > 0
          ? `将优先从池内分配「空闲独立 + 地区匹配」节点，确保「${esc(pfName)}」一号一独立出口 IP。`
          : `⚠️ 池内暂无空闲的匹配地区节点，自动分配将按最少占用原则复用现有同区可用节点；也可切换「手动输入」补充新节点。`;
        autoPanel.innerHTML = `<div class="pxp-card-head"><b>自动分配最优节点</b>${statsHtml}</div>
          <p>${desc}</p>
          <div style="display:grid;gap:6px">
            <div style="display:flex;align-items:center;justify-content:space-between;font-size:11.5px;color:var(--mut)">
              <span>预计优先命中节点预览：</span>
              <button type="button" class="ghost sm" style="min-height:24px;padding:2px 8px;font-size:11px" data-pxp-jump="pool">手动指定其他节点 →</button>
            </div>
            ${renderProxyNodeCard(autoCandidate.p, autoCandidate.meta, { preview: true })}
          </div>`;
      }
      autoPanel.querySelectorAll("[data-pxp-jump]").forEach(btn => {
        btn.onclick = () => { mode = btn.dataset.pxpJump; updateUI(); };
      });
    }

    // Pool panel filters & list
    const fltAll = container.querySelector("#pxp-flt-all");
    const fltIdle = container.querySelector("#pxp-flt-idle");
    const fltMatch = container.querySelector("#pxp-flt-match");
    if (fltAll) fltAll.textContent = `全部 (${items.length})`;
    if (fltIdle) fltIdle.textContent = `本平台空闲 (${idleItems.length})`;
    if (fltMatch) fltMatch.textContent = `${domestic ? "大陆匹配" : "境外匹配"} (${matchItems.length})`;
    container.querySelectorAll("[data-pxp-flt]").forEach(btn => {
      btn.classList.toggle("active", btn.dataset.pxpFlt === filter);
    });

    if (selectedUrl && !items.some(x => x.p.url === selectedUrl)) {
      selectedUrl = (items.find(x => x.meta.healthy) || items[0])?.p?.url || "";
    }
    const q = query.trim().toLowerCase();
    const filtered = items.filter(({ p, meta }) => {
      if (filter === "idle" && !(p.enabled !== false && meta.otherSamePf === 0)) return false;
      if (filter === "match" && !(p.enabled !== false && meta.geoMatched === true)) return false;
      if (q) {
        const hay = `${meta.displayName} ${p.masked || ""} ${p.geo_loc || ""} ${p.exit_ip || ""} ${p.note || ""}`.toLowerCase();
        if (!hay.includes(q)) return false;
      }
      return true;
    });

    const listEl = container.querySelector("#pxp-list");
    if (listEl) {
      if (!items.length) {
        listEl.innerHTML = `<div class="pxp-empty">
          <span>代理池暂无节点，可在「手动输入」中添加并勾选保存到代理池</span>
          <button type="button" class="ghost sm" id="pxp-empty-to-custom">${ic("i-plus")}切换至手动输入</button>
        </div>`;
        const jumpBtn = listEl.querySelector("#pxp-empty-to-custom");
        if (jumpBtn) jumpBtn.onclick = () => { mode = "custom"; updateUI(); customUrlInp?.focus(); };
      } else if (!filtered.length) {
        listEl.innerHTML = `<div class="pxp-empty">
          <span>当前筛选条件下没有匹配的代理节点</span>
          <button type="button" class="ghost sm" id="pxp-reset-flt">显示全部节点 (${items.length})</button>
        </div>`;
        const rst = listEl.querySelector("#pxp-reset-flt");
        if (rst) rst.onclick = () => { filter = "all"; query = ""; if (searchInp) searchInp.value = ""; updateUI(); };
      } else {
        listEl.innerHTML = filtered.map(({ p, meta }) =>
          renderProxyNodeCard(p, meta, { selected: p.url === selectedUrl })
        ).join("");
        listEl.querySelectorAll("[data-pxp-url]").forEach(row => {
          const pickRow = () => { selectedUrl = row.dataset.pxpUrl; updateUI(); };
          row.addEventListener("click", ev => {
            if (ev.target.closest("[data-pxp-test]")) return;
            pickRow();
          });
          row.addEventListener("keydown", ev => {
            if (ev.target.closest("[data-pxp-test]")) return;
            if (ev.key === " " || ev.key === "Enter") { ev.preventDefault(); ev.stopPropagation(); pickRow(); }
          });
        });
        listEl.querySelectorAll("[data-pxp-test]").forEach(btn => {
          btn.addEventListener("click", async ev => {
            ev.preventDefault(); ev.stopPropagation();
            const pid = Number(btn.dataset.pxpTest);
            const target = opts.find(o => Number(o.id) === pid);
            if (!target || btn.disabled) return;
            btn.disabled = true; btn.textContent = "测试中…";
            try {
              const r = await api(`/api/proxies/${pid}/test`, { method: "POST" });
              target.status = r.ok ? "ok" : "bad";
              if (r.geo) {
                target.exit_ip = r.geo.ip || target.exit_ip || "";
                target.country = r.geo.country || target.country || "";
                target.region = r.geo.region || target.region || "";
                target.city = r.geo.city || target.city || "";
                target.isp = r.geo.isp || target.isp || "";
                target.geo_loc = [target.country, target.region, target.city].filter(Boolean).join(" · ");
                target.geo_checked = Boolean(target.exit_ip || target.country);
                target.is_mainland = isMainlandGeoClient(target.country, target.region, target.city);
              }
              if (typeof refreshProxies === "function") refreshProxies();
            } catch (e) {
              toast("节点测试失败：" + e.message, "err");
            } finally {
              updateUI();
            }
          });
        });
      }
    }

    // Selected pool node inline warning
    const warnEl = container.querySelector("#pxp-pool-warn");
    const pickedItem = items.find(x => x.p.url === selectedUrl);
    if (warnEl) {
      const reasons = [];
      if (pickedItem) {
        if (pickedItem.p.enabled === false) reasons.push("该节点当前处于「已停用」状态");
        if (["bad", "auth_error", "blocked"].includes(pickedItem.p.status)) reasons.push("最近一次连通性检测未通过，建议先点右侧「测试」确认可用");
        if (pickedItem.meta.geoMatched === false) reasons.push(domestic ? "该节点为境外出口 IP，绝大多数海外住宅代理商（如 IPWO 等）会直接屏蔽大陆网站导致报 ERR_SOCKS_CONNECTION_FAILED，建议改选本机直连或大陆代理" : "该节点为中国大陆出口 IP，通常无法直达海外平台，建议更换境外代理");
        if (pickedItem.meta.otherSamePf > 0) reasons.push(`同平台已有 ${pickedItem.meta.otherSamePf} 个其他账号共用此代理，存在同 IP 关联风险`);
      }
      warnEl.hidden = !reasons.length;
      warnEl.innerHTML = reasons.length
        ? `<div class="pxp-inline-warn">${ic("i-alert")}<span>${esc(reasons.join("；"))}（系统不强制拦截，仍可继续使用）。</span></div>`
        : "";
    }

    if (customLblInp && savePoolChk) {
      customLblInp.disabled = !savePoolChk.checked;
      customLblInp.style.opacity = savePoolChk.checked ? "1" : "0.5";
    }

    if (typeof onSummary === "function") {
      let summary = "";
      if (mode === "auto") {
        summary = autoCandidate
          ? `当前策略：自动分配（预计优先：${autoCandidate.meta.displayName}）`
          : `当前策略：自动分配${!items.length && !isAccountEdit ? "（池空将走本机直连）" : ""}`;
      } else if (mode === "pool") {
        summary = pickedItem
          ? `已选池节点：${pickedItem.meta.displayName}${pickedItem.p.geo_loc ? " · " + pickedItem.p.geo_loc : ""}`
          : "请从列表中选择一个代理节点";
      } else if (mode === "custom") {
        const raw = customUrlInp?.value.trim() || "";
        summary = raw ? "已填写手动代理地址" : "请输入代理服务器地址";
      } else {
        summary = isAccountEdit && currentProxy ? "将清除代理并改走本机直连" : "当前策略：本机直连（无代理隔离）";
      }
      onSummary(summary);
    }
  };

  container.querySelectorAll("[data-pxp-mode]").forEach(btn => {
    btn.addEventListener("click", () => {
      mode = btn.dataset.pxpMode;
      updateUI();
      if (mode === "custom") requestAnimationFrame(() => customUrlInp?.focus());
    });
  });
  container.querySelectorAll("[data-pxp-flt]").forEach(btn => {
    btn.addEventListener("click", () => { filter = btn.dataset.pxpFlt; updateUI(); });
  });
  if (searchInp) {
    searchInp.addEventListener("input", () => { query = searchInp.value; updateUI(); });
    searchInp.addEventListener("keydown", ev => { if (ev.key === "Enter") ev.preventDefault(); });
  }
  if (customUrlInp) {
    customUrlInp.addEventListener("input", () => updateUI());
  }
  if (savePoolChk) {
    savePoolChk.addEventListener("change", () => updateUI());
  }
  if (detectBtn && customUrlInp && detectMsgEl) {
    detectBtn.addEventListener("click", async () => {
      const raw = customUrlInp.value.trim();
      if (!raw) {
        detectMsgEl.dataset.tone = "err";
        detectMsgEl.textContent = "请先输入代理地址后再点击检测";
        customUrlInp.focus();
        return;
      }
      detectBtn.disabled = true;
      const oldHtml = detectBtn.innerHTML;
      detectBtn.textContent = "检测中…";
      detectMsgEl.removeAttribute("data-tone");
      detectMsgEl.textContent = "正在自动探测协议与出口 IP 归属地…";
      try {
        const r = await api("/api/proxies/detect", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ url: raw }),
        });
        if (!r.ok) {
          detectMsgEl.dataset.tone = "err";
          detectMsgEl.textContent = "检测未通过：" + (r.error || "连接失败");
          return;
        }
        customUrlInp.value = r.recommend || raw;
        detected = { url: customUrlInp.value, geo: r.geo || null };
        if (customLblInp && !customLblInp.value.trim() && r.geo) {
          const g = r.geo;
          customLblInp.value = [g.country, g.region, g.city].filter(Boolean).join("·");
        }
        const tag = String(r.scheme || "http").toUpperCase() + (r.auth === "required" ? " · 含认证" : " · 免密");
        detectMsgEl.dataset.tone = "ok";
        detectMsgEl.textContent = `✓ 连通正常（${tag}）${r.geo_text ? " · " + r.geo_text : ""}`;
        updateUI();
      } catch (e) {
        detectMsgEl.dataset.tone = "err";
        detectMsgEl.textContent = "检测失败：" + e.message;
      } finally {
        detectBtn.disabled = false;
        detectBtn.innerHTML = oldHtml;
      }
    });
  }

  updateUI();

  return {
    getValue() {
      if (mode === "auto") return { mode: "auto", proxy: "auto", pickedNode: null };
      if (mode === "direct") return { mode: "direct", proxy: "", pickedNode: null };
      if (mode === "pool") {
        const found = items.find(x => x.p.url === selectedUrl);
        return { mode: "pool", proxy: found ? found.p.url : "", pickedNode: found ? found.p : null, pickedMeta: found ? found.meta : null };
      }
      return {
        mode: "custom",
        proxy: customUrlInp ? customUrlInp.value.trim() : "",
        saveToPool: Boolean(savePoolChk && savePoolChk.checked),
        customLabel: customLblInp ? customLblInp.value.trim() : "",
        pickedNode: null,
      };
    },
    async validateAndCommitCustom(choice) {
      if (choice.mode === "pool" && !choice.proxy) {
        uiEditorError("请先从代理池列表中选择一个节点，或切换其他代理模式");
      }
      if (choice.mode === "custom") {
        if (!choice.proxy) {
          uiEditorError("请填写代理地址，或切换为「自动分配 / 本机直连」", "pxp-custom-url");
        }
        if (choice.saveToPool) {
          const geo = (detected && detected.url === choice.proxy) ? detected.geo : null;
          try {
            const created = await api("/api/proxies", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ url: choice.proxy, label: choice.customLabel || "", geo }),
            });
            if (created && created.url_full) choice.proxy = created.url_full;
            if (typeof refreshProxies === "function") refreshProxies();
          } catch (err) {
            if (err && err.status === 409) {
              // 已存在于代理池中，无需中断后续绑定/登录
            } else {
              uiEditorError(err.message || "代理格式无效", "pxp-custom-url");
            }
          }
        }
      }
      return choice;
    },
  };
}
async function uiProxyPicker({
  title = "选择代理",
  hint = "",
  platform = PLATFORM,
  currentProxy = "",
  currentProxyId = null,
  isAccountEdit = false,
  okText = "确定",
  busyText = "保存中",
  submit = null,
} = {}) {
  let opts = [];
  try { opts = await api("/api/proxies/options"); } catch (e) { }
  return new Promise(resolve => {
    _uiResolve = resolve; _uiCancelVal = null;
    let latestSummary = "";
    const body = $("ui-body");
    body.innerHTML = `<div id="pxp-standalone-mount"></div>`;
    const picker = mountProxyPickerControl($("pxp-standalone-mount"), {
      opts, platform, currentProxy, currentProxyId, isAccountEdit,
      onSummary: text => {
        latestSummary = text;
        if (!_uiBusy && $("ui-feedback")) uiEditorNotice(text, false);
      },
    });
    _uiGetVal = () => picker.getValue();
    _uiOpen(title, hint, {
      okText,
      wide: true,
      icon: "i-globe",
      busyText,
      noticeText: latestSummary,
      submit: async choice => {
        await picker.validateAndCommitCustom(choice);
        if (submit) return await submit(choice);
      },
    });
  });
}
async function choosePreLoginBrowserBackend({ platform = "" } = {}) {
  let catalog;
  try {
    catalog = await api("/api/browser-backends");
    preLoginBrowserCatalog = catalog;
  } catch (e) {
    toast("读取登录环境失败：" + e.message, "err");
    return null;
  }
  const isXhs = platform === "xhs";
  const options = browserChoiceOptions(catalog, { localOnly: false });
  const selected = await uiSelect({
    title: "选择扫码登录环境",
    hint: isXhs
      ? "单号推荐使用系统 Chrome/CDP 原生环境；同机运营多个小红书号时可选 Fingerprint Chromium 实现底层硬件指纹隔离。"
      : "扫码、Cookie 落地和后续账号任务将使用同一浏览器内核。新的指纹环境会同时打开环境体检标签。",
    options,
    value: isXhs && preLoginBrowserBackend === "default" ? "local" : preLoginBrowserBackend,
  });
  if (selected !== null) preLoginBrowserBackend = selected;
  return selected;
}
function notifyProxyChoiceRisk(choice, pf) {
  if (!choice) return;
  if (choice.proxy === "") {
    toast("⚠️ 提醒：本次登录使用本机直连，多账号或海外平台请注意真实 IP 关联与网络风控风险", "info", 5500);
  } else if (choice.mode === "pool" && choice.pickedMeta && choice.pickedMeta.otherSamePf > 0) {
    toast(`⚠️ 提醒：所选代理已在同平台绑定 ${choice.pickedMeta.otherSamePf} 个账号，共用同一出口 IP 存在关联风险`, "info", 6000);
  }
}
// 登录前选代理(兼容独立调用):返回 "" (不用) | "auto" | 具体url | null(取消)
async function choosePreLoginProxy({ platform = PLATFORM } = {}) {
  const pf = platform || "douyin";
  const domestic = isDomesticPlatform(pf);
  const hint = domestic
    ? "建议尽量保持「一号一独立代理」。系统不强制拦截本机直连或多号共用代理，但同平台多号共用出口 IP 容易触发关联限流。"
    : "⚠️ 海外平台强烈建议绑定境外独立代理；系统不强制拦截本机直连或多号共用代理，但直连易受网络限制或触发风控。";
  const choice = await uiProxyPicker({
    title: "选择本次登录使用的代理",
    hint,
    platform: pf,
    isAccountEdit: false,
    okText: "确认代理并继续",
    busyText: "校验中",
  });
  if (!choice) return null;
  notifyProxyChoiceRisk(choice, pf);
  return choice.proxy;
}
function freshPreLoginFingerprint(browserBackend, platform = PLATFORM) {
  const choice = browserChoiceParts(browserBackend);
  const runtimes = (preLoginBrowserCatalog || {}).runtimes || [];
  const runtime = runtimes.find(item => item.runtime_id === choice.runtimeId)
    || runtimes.find(item => item.is_default) || {};
  const seed = (globalThis.crypto && typeof globalThis.crypto.randomUUID === "function")
    ? globalThis.crypto.randomUUID().replaceAll("-", "")
    : `${Date.now().toString(16)}${Math.random().toString(16).slice(2)}`;
  const overseas = !isDomesticPlatform(platform || "");
  return {
    seed, engine_seed: "", fingerprint_id: seed.slice(0, 12),
    source_ip: "", country: "", region: "", city: "",
    timezone: overseas ? "America/New_York" : "Asia/Shanghai",
    locale: overseas ? "en-US" : "zh-CN",
    accept_languages: "",
    viewport_w: 1280, viewport_h: 800, geo_lat: 0, geo_lon: 0,
    platform: "", platform_version: "", brand: "", brand_version: "",
    hardware_concurrency: 0, gpu_vendor: "", gpu_renderer: "",
    disable_spoofing: [], language_mode: "auto", timezone_mode: "auto",
    viewport_mode: "auto", location_mode: "auto",
    geolocation_permission: "allow", webrtc_mode: "conceal", extra_args: "",
    runtime_version: runtime.version || "",
  };
}
async function configurePreLoginFingerprint(browserBackend, platform = PLATFORM) {
  const choice = browserChoiceParts(browserBackend);
  const effectiveBackend = choice.backend === "default"
    ? (preLoginBrowserCatalog || {}).default
    : choice.backend;
  if (effectiveBackend !== "fingerprint_chromium") return "";
  const draft = freshPreLoginFingerprint(browserBackend, platform);
  const account = {
    nickname: "新账号",
    environment: { runtime_version: draft.runtime_version },
  };
  const action = await uiFingerprintEditor(account, draft, { preLogin: true });
  if (!action) return null;
  return action.action === "auto" ? "" : action.data;
}
// 扫码前一体化环境与代理配置：将「选浏览器内核 + 指纹模式 + 选代理」合并为单步弹窗
async function preparePreLoginEnvironment({ platform = PLATFORM } = {}) {
  const pf = platform || "douyin";
  const domestic = isDomesticPlatform(pf);
  const isXhs = pf === "xhs";
  const pfName = (typeof PF_NAME !== "undefined" && PF_NAME[pf]) ? PF_NAME[pf] : pf;
  let catalog, opts = [];
  try {
    [catalog, opts] = await Promise.all([
      api("/api/browser-backends"),
      api("/api/proxies/options").catch(() => []),
    ]);
    preLoginBrowserCatalog = catalog;
  } catch (e) {
    toast("读取登录环境失败：" + e.message, "err");
    return null;
  }
  const backendOpts = browserChoiceOptions(catalog, { localOnly: false });
  const initialBackend = isXhs && preLoginBrowserBackend === "default" ? "local" : preLoginBrowserBackend;
  const effectiveFor = val => {
    const parts = browserChoiceParts(val);
    return parts.backend === "default" ? catalog.default : parts.backend;
  };

  const result = await new Promise(resolve => {
    _uiResolve = resolve; _uiCancelVal = null;
    let fpMode = "auto";
    let latestSummary = "";
    const body = $("ui-body");
    body.innerHTML = `
      <div class="prelogin-section">
        <div class="prelogin-sec-head">
          <span class="prelogin-sec-title">${ic("i-monitor")}<span>浏览器内核与指纹隔离</span></span>
          <span class="pill ${domestic ? "bare" : "pending bare"}">${esc(pfName)} · ${domestic ? "国内平台" : "海外平台"}</span>
        </div>
        <select id="pl-backend" style="width:100%" aria-label="浏览器内核">
          ${backendOpts.map(o => `<option value="${esc(o.value)}"${o.value === initialBackend ? " selected" : ""}${o.disabled ? " disabled" : ""}>${esc(o.label)}</option>`).join("")}
        </select>
        <p class="prelogin-env-hint">${isXhs
          ? "单号推荐使用系统 Chrome/CDP 原生环境；同机运营多个小红书号可选 Fingerprint Chromium 隔离底层硬件指纹。"
          : "扫码、Cookie 落地与后续任务共用同一独立 Profile，确保登录态与环境一致。"}</p>
        <div class="prelogin-fp-row" id="pl-fp-row" hidden>
          <div>
            <b>硬件与环境指纹</b>
            <span>默认按代理/本机出口 IP 自动匹配时区、语言与硬件种子，无需额外弹窗</span>
          </div>
          <div class="fp-segment" role="group" aria-label="指纹生成方式">
            <button type="button" class="active" data-pl-fp="auto">跟随出口 IP 自动</button>
            <button type="button" data-pl-fp="custom">下一步手动设置…</button>
          </div>
        </div>
      </div>
      <div class="prelogin-section" style="margin-bottom:0">
        <div class="prelogin-sec-head">
          <span class="prelogin-sec-title">${ic("i-globe")}<span>本次登录使用的代理</span></span>
          <span class="mut" style="font-size:11px">建议一号一独立代理防关联</span>
        </div>
        <div id="pl-proxy-mount"></div>
      </div>`;

    const backendSel = $("pl-backend");
    enhanceSelect(backendSel);
    const fpRow = $("pl-fp-row");
    const syncOkLabel = () => {
      const isFp = effectiveFor(backendSel.value) === "fingerprint_chromium";
      if (fpRow) fpRow.hidden = !isFp;
      const okBtn = $("ui-ok");
      if (okBtn && !_uiBusy) {
        const nextTxt = (isFp && fpMode === "custom") ? "下一步：配置指纹" : "开始扫码登录";
        okBtn.innerHTML = `<svg aria-hidden="true"><use href="#${(isFp && fpMode === "custom") ? "i-fingerprint" : "i-qr"}"/></svg>${esc(nextTxt)}`;
      }
    };
    backendSel.addEventListener("change", syncOkLabel);
    if (fpRow) {
      fpRow.querySelectorAll("[data-pl-fp]").forEach(btn => {
        btn.addEventListener("click", () => {
          fpMode = btn.dataset.plFp;
          fpRow.querySelectorAll("[data-pl-fp]").forEach(b => b.classList.toggle("active", b === btn));
          syncOkLabel();
        });
      });
    }

    const picker = mountProxyPickerControl($("pl-proxy-mount"), {
      opts,
      platform: pf,
      isAccountEdit: false,
      onSummary: text => {
        latestSummary = text;
        if (!_uiBusy && $("ui-feedback")) uiEditorNotice(text, false);
      },
    });

    _uiGetVal = () => ({
      browserBackend: backendSel.value,
      fpMode: effectiveFor(backendSel.value) === "fingerprint_chromium" ? fpMode : "off",
      choice: picker.getValue(),
    });
    _uiOpen(
      `配置扫码登录环境 · ${pfName}`,
      domestic
        ? "一次确认浏览器内核与独立代理即可直接弹出扫码窗口。"
        : "海外平台建议绑定境外独立代理，确认后直接打开独立登录窗口。",
      {
        okText: "开始扫码登录",
        wide: true,
        icon: "i-qr",
        busyText: "准备中",
        noticeText: latestSummary,
        submit: async payload => {
          await picker.validateAndCommitCustom(payload.choice);
        },
      },
    );
    syncOkLabel();
  });

  if (!result) return null;
  preLoginBrowserBackend = result.browserBackend;
  notifyProxyChoiceRisk(result.choice, pf);
  let fingerprint = "";
  if (result.fpMode === "custom") {
    fingerprint = await configurePreLoginFingerprint(result.browserBackend, pf);
    if (fingerprint === null) return null;
  }
  return {
    browserBackend: result.browserBackend,
    proxy: result.choice.proxy,
    fingerprint,
  };
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
  const session = await preparePreLoginEnvironment({ platform: "douyin" });
  if (!session) return;
  const { browserBackend, proxy, fingerprint } = session;
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
  text += env.has_proxy ? " · 账号代理" : " · 本机直连";
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
      const pxWarn = res.warning || res.environment?.proxy_warning || "";
      const warnHtml = pxWarn ? `<div style="margin-top:6px;color:var(--warn);font-size:12px">⚠️ ${esc(pxWarn)}</div>` : "";
      if (["opening", "waiting"].includes(res.status) && envText) {
        $("qrstatus").innerHTML = `${ic("i-eye")} 浏览器已打开 · <b>${esc(envText)}</b><br>请在可见窗口完成登录。${warnHtml}`;
      }
      if (res.status === "verification") {
        $("qrstatus").innerHTML = `${ic("i-info")} <b>需要完成一次设备安全验证</b><br>${esc(res.hint || "请扫描浏览器中的验证二维码，验证通过后会自动继续登录。")}${warnHtml}`;
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
          if (pxWarn) setTimeout(() => toast("⚠️ " + pxWarn, "info", 6500), 350);
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
  const session = await preparePreLoginEnvironment({ platform: "douyin" });
  if (!session) return;
  const { browserBackend, proxy, fingerprint } = session;
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
  const session = await preparePreLoginEnvironment({ platform: "xhs" });
  if (!session) return;
  const { browserBackend, proxy, fingerprint } = session;
  $("cookiebox").style.display = "none";
  $("qrbox").style.display = "block";
  $("qrstatus").textContent = "正在打开小红书窗口…";
  try {
    const res = await api(loginStartUrl("/api/login/xhs/start", proxy, browserBackend), loginStartOptions(fingerprint));
    if (res.reused) {
      $("qrstatus").innerHTML = `${ic("i-eye")} <b>已有小红书扫码窗口</b>，已尝试切换到前台，请直接在该窗口继续。`;
      toast("已有扫码窗口，已切换到前台", "info");
    } else {
      $("qrstatus").innerHTML = `${ic("i-eye")} <b>小红书官网首页已用系统 Chrome 打开</b>，请在窗口中点击「登录」并使用小红书 App 扫码。<br>如出现平台安全验证，自动任务会暂停，请只在当前窗口按提示完成。<br>主站登录成功后会保存读取登录态并自动关闭窗口。<br>如需发布，请随后单独点击「创作者登录」。`;
    }
    pollLogin(res.task_id);
  } catch (e) { $("qrstatus").textContent = "启动失败: " + e.message; toast("小红书登录启动失败:" + e.message, "err"); }
}

// ─── 小红书创作者登录(发布用) ───
async function startXhsCreatorLogin() {
  const session = await preparePreLoginEnvironment({ platform: "xhs" });
  if (!session) return;
  const { browserBackend, proxy, fingerprint } = session;
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
  const session = await preparePreLoginEnvironment({ platform: "kuaishou" });
  if (!session) return;
  const { browserBackend, proxy, fingerprint } = session;
  $("cookiebox").style.display = "none";
  $("qrbox").style.display = "block";
  $("qrstatus").textContent = "正在打开快手窗口…";
  try {
    const res = await api(loginStartUrl("/api/login/kuaishou/start", proxy, browserBackend), loginStartOptions(fingerprint));
    $("qrstatus").innerHTML = `${ic("i-eye")} <b>快手扫码窗口已打开</b>，登录二维码会自动弹出，请使用快手 App 扫码。<br>完成后这里会自动识别并刷新资料。`;
    pollLogin(res.task_id);
  } catch (e) { $("qrstatus").textContent = "启动失败: " + e.message; toast("快手登录启动失败:" + e.message, "err"); }
}

// ─── 快手创作者登录(发布用) ───
async function startKsCreatorLogin() {
  const session = await preparePreLoginEnvironment({ platform: "kuaishou" });
  if (!session) return;
  const { browserBackend, proxy, fingerprint } = session;
  $("cookiebox").style.display = "none";
  $("qrbox").style.display = "block";
  $("qrstatus").textContent = "正在打开快手创作平台窗口…";
  try {
    const res = await api(loginStartUrl("/api/login/kuaishou-creator/start", proxy, browserBackend), loginStartOptions(fingerprint));
    $("qrstatus").innerHTML = `${ic("i-eye")} <b>快手创作平台窗口已打开</b>。普通快手扫码登录已可同时用于发布；仅在创作平台仍提示登录时使用此备用入口。`;
    pollLogin(res.task_id);
  } catch (e) { $("qrstatus").textContent = "启动失败: " + e.message; toast("创作者登录启动失败:" + e.message, "err"); }
}

// ─── 视频号扫码登录(读取/发布共用,微信扫码) ───
async function startChannelsLogin() {
  const session = await preparePreLoginEnvironment({ platform: "shipinhao" });
  if (!session) return;
  const { browserBackend, proxy, fingerprint } = session;
  $("cookiebox").style.display = "none";
  $("qrbox").style.display = "block";
  $("qrstatus").textContent = "正在打开视频号助手窗口…";
  try {
    const res = await api(loginStartUrl("/api/login/shipinhao/start", proxy, browserBackend), loginStartOptions(fingerprint));
    $("qrstatus").innerHTML = `${ic("i-eye")} <b>视频号助手窗口已打开</b>，请使用微信扫码登录，读取和发布共用此登录态。<br>登录成功后请稍等片刻再关闭窗口。`;
    pollLogin(res.task_id);
  } catch (e) { $("qrstatus").textContent = "启动失败: " + e.message; toast("视频号登录启动失败:" + e.message, "err"); }
}

// ─── TikTok Web 登录（读取、采集和写操作共用） ───
async function startTikTokLogin() {
  const session = await preparePreLoginEnvironment({ platform: "tiktok" });
  if (!session) return;
  const { browserBackend, proxy, fingerprint } = session;
  $("cookiebox").style.display = "none";
  $("qrbox").style.display = "block";
  $("qrstatus").textContent = "正在打开 TikTok 窗口…";
  try {
    const res = await api(loginStartUrl("/api/login/tiktok/start", proxy, browserBackend), loginStartOptions(fingerprint));
    $("qrstatus").innerHTML = `${ic("i-eye")} <b>TikTok Web 窗口已打开</b>，请在该窗口完成登录。<br>登录成功后，此账号 Profile 会用于页面动态签名、批量采集、关注、私信和评论。`;
    pollLogin(res.task_id);
  } catch (e) { $("qrstatus").textContent = "启动失败: " + e.message; toast("TikTok 登录启动失败:" + e.message, "err"); }
}

// ─── 哔哩哔哩扫码登录（读取、创作中心与写操作共用） ───
async function startBilibiliLogin() {
  const session = await preparePreLoginEnvironment({ platform: "bilibili" });
  if (!session) return;
  const { browserBackend, proxy, fingerprint } = session;
  $("cookiebox").style.display = "none";
  $("qrbox").style.display = "block";
  $("qrstatus").textContent = "正在打开哔哩哔哩窗口…";
  try {
    const res = await api(loginStartUrl("/api/login/bilibili/start", proxy, browserBackend), loginStartOptions(fingerprint));
    $("qrstatus").innerHTML = `${ic("i-eye")} <b>哔哩哔哩窗口已打开</b>，请使用哔哩哔哩 App 扫码完成登录。<br>登录成功后会自动同步创作中心会话，可用于采集、弹幕、互动与投稿。`;
    pollLogin(res.task_id);
  } catch (e) { $("qrstatus").textContent = "启动失败: " + e.message; toast("哔哩哔哩登录启动失败:" + e.message, "err"); }
}

// ─── 海外小红书（Rednote）登录 ───
async function startRednoteLogin() {
  const session = await preparePreLoginEnvironment({ platform: "rednote" });
  if (!session) return;
  const { browserBackend, proxy, fingerprint } = session;
  $("cookiebox").style.display = "none";
  $("qrbox").style.display = "block";
  $("qrstatus").textContent = "正在打开海外小红书（www.rednote.com）窗口…";
  try {
    const res = await api(loginStartUrl("/api/login/rednote/start", proxy, browserBackend), loginStartOptions(fingerprint));
    $("qrstatus").innerHTML = `${ic("i-eye")} <b>海外小红书（www.rednote.com）窗口已打开</b>，请使用 Rednote / 小红书海外版扫码或网页登录。<br>会话严格隔离在 <code>.rednote.com</code> 域，绝不混用国内小红书 Cookie。`;
    pollLogin(res.task_id);
  } catch (e) { $("qrstatus").textContent = "启动失败: " + e.message; toast("海外小红书登录启动失败:" + e.message, "err"); }
}

async function startRednoteCreatorLogin() {
  const session = await preparePreLoginEnvironment({ platform: "rednote" });
  if (!session) return;
  const { browserBackend, proxy, fingerprint } = session;
  $("cookiebox").style.display = "none";
  $("qrbox").style.display = "block";
  $("qrstatus").textContent = "正在打开海外小红书创作服务平台（creator.rednote.com）窗口…";
  try {
    const res = await api(loginStartUrl("/api/login/rednote-creator/start", proxy, browserBackend), loginStartOptions(fingerprint));
    $("qrstatus").innerHTML = `${ic("i-eye")} <b>海外小红书创作平台（creator.rednote.com）窗口已打开</b>，请完成创作者扫码登录，用于图文/视频笔记发布与作品管理。`;
    pollLogin(res.task_id);
  } catch (e) { $("qrstatus").textContent = "启动失败: " + e.message; toast("创作者登录启动失败:" + e.message, "err"); }
}

// ─── Instagram 独立浏览器登录（Mobile API 与 Web 端共用会话） ───
async function startInstagramLogin() {
  const session = await preparePreLoginEnvironment({ platform: "instagram" });
  if (!session) return;
  const { browserBackend, proxy, fingerprint } = session;
  $("cookiebox").style.display = "none";
  $("qrbox").style.display = "block";
  $("qrstatus").textContent = "正在打开 Instagram 窗口…";
  try {
    const res = await api(loginStartUrl("/api/login/instagram/start", proxy, browserBackend), loginStartOptions(fingerprint));
    $("qrstatus").innerHTML = `${ic("i-eye")} <b>Instagram 窗口已打开</b>，请在该窗口完成登录（含两步验证/TOTP）。<br>登录成功后会自动同步 <code>sessionid</code> 与令牌，支持采集、互动、私信与发布。`;
    pollLogin(res.task_id);
  } catch (e) { $("qrstatus").textContent = "启动失败: " + e.message; toast("Instagram 登录启动失败:" + e.message, "err"); }
}

// ─── Twitter (X) 独立浏览器登录（Web 与 Syndication/REST 共用会话） ───
async function startTwitterLogin() {
  const session = await preparePreLoginEnvironment({ platform: "twitter" });
  if (!session) return;
  const { browserBackend, proxy, fingerprint } = session;
  $("cookiebox").style.display = "none";
  $("qrbox").style.display = "block";
  $("qrstatus").textContent = "正在打开 X (Twitter) 登录窗口…";
  try {
    const res = await api(loginStartUrl("/api/login/twitter/start", proxy, browserBackend), loginStartOptions(fingerprint));
    $("qrstatus").innerHTML = `${ic("i-eye")} <b>X (Twitter) 窗口已打开</b>，请在该窗口完成账号登录（含两步验证/TOTP）。<br>登录成功后会自动同步 <code>auth_token</code> 与 <code>ct0</code>，支持采集、互动、私信与发推。`;
    pollLogin(res.task_id);
  } catch (e) { $("qrstatus").textContent = "启动失败: " + e.message; toast("Twitter 登录启动失败:" + e.message, "err"); }
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
    const res = await api("/api/login/cookie", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ cookie, nickname: $("ck-nick").value.trim(), platform: PLATFORM }),
    });
    $("ck-val").value = ""; $("cookiebox").style.display = "none";
    if (res && res.warning) toast(`账号已保存(${res.nickname || ""})，但状态异常：${res.warning}`, "err");
    else toast(PLATFORM === "twitter" && res && res.nickname ? `Token / Cookie 已导入：${res.nickname}` : "Cookie 已保存", "ok");
    if (res && res.proxy_warning) setTimeout(() => toast("⚠️ " + res.proxy_warning, "info", 6500), 300);
    refreshAccounts();
  } catch (e) { toast("保存失败:" + e.message, "err"); }
}

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

// 06-risk-center.js — 风控中心看板、操作节奏参数配置、人工验证解除与审计日志。
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
function autoRunHint(value) {
  const date = riskDate(value);
  if (!date) return "";
  const label = date.getTime() > Date.now() ? `下次最早 ${riskTime(value)}` : "已到期，等待调度";
  return `<div class="mut" title="自动任务的最早调度时间；仍需满足账号风控、并发和队列条件">${esc(label)}</div>`;
}
function riskDuration(seconds) {
  let left = Math.max(0, Math.ceil(Number(seconds) || 0));
  if (!left) return "已到期";
  const days = Math.floor(left / 86400); left %= 86400;
  const hours = Math.floor(left / 3600); left %= 3600;
  const minutes = Math.floor(left / 60), remainder = left % 60;
  return [days ? `${days}天` : "", hours ? `${hours}小时` : "", minutes ? `${minutes}分钟` : "",
    !days && !hours && remainder ? `${remainder}秒` : ""].filter(Boolean).join("");
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

async function refreshRiskCenter(force = false, announce = force) {
  const isCurrent = beginViewRequest("risk-center");
  try {
    const shouldFillConfig = !RISK_CONFIG || force;
    const configPromise = shouldFillConfig ? api("/api/risk-control/config") : Promise.resolve(RISK_CONFIG);
    const platformQuery = GLOBAL_MODE ? "" : ("?platform=" + encodeURIComponent(PLATFORM));
    const [summary, accounts, config] = await Promise.all([
      api("/api/risk-control/summary" + platformQuery),
      api("/api/risk-control/accounts" + platformQuery),
      configPromise,
    ]);
    if (!isCurrent()) return;
    RISK_ACCOUNTS = accounts;
    RISK_CONFIG = config;
    renderRiskSummary(summary);
    renderRiskAccounts();
    if (shouldFillConfig) fillRiskConfig(config);
    if (announce) toast("风控状态已刷新", "ok");
  } catch (e) {
    if (isCurrent() && (force || CURRENT_TAB === "risk-control")) toast("风控中心加载失败：" + e.message, "err");
  }
}

function renderRiskSummary(summary) {
  const counts = summary.counts || {};
  $("risk-stat-normal").textContent = counts.normal || 0;
  $("risk-stat-cooldown").textContent = (counts.cooldown || 0) + (counts.network_circuit || 0) + (counts.write_paused || 0) + (counts.network_backoff || 0);
  $("risk-stat-recovering").textContent = counts.recovering || 0;
  $("risk-stat-invalid").textContent = (counts.auth_invalid || 0) + (counts.proxy_error || 0) + (counts.verification_required || 0);
  $("risk-stat-blocked").textContent = summary.blocked_tasks || 0;
  $("risk-stat-today").textContent = summary.risk_events_today || 0;
  if ($("tb-risk")) $("tb-risk").textContent = summary.abnormal || 0;
}

function riskAccountWait(account, now = Date.now()) {
  if (account.manual_review_required || account.status === "verification_required") {
    return { label: "等待人工验证", until: null, waiting: true, manual: true,
      title: "请在账号浏览器中处理平台验证，确认完成后填写原因并人工解除暂停" };
  }
  const deadlines = [
    [account.cooldown_until, "风险冷却"], [account.next_probe_at, "下次探测"],
    [account.retry_not_before, "网络退避"], [account.session_rest_until, "连续操作休息"],
    [account.operation_not_before, "操作间隔"],
  ].map(([until, label]) => ({ until, label, time: riskDate(until)?.getTime() || 0 }))
    .filter(item => item.time > now).sort((a, b) => b.time - a.time);
  const hold = deadlines[0];
  return hold ? { ...hold, waiting: true, manual: false,
    title: `${hold.label}，最早 ${riskTime(hold.until)} 再探测` }
    : { label: account.status === "recovering" ? "已到探测时间" : "无需等待",
      until: null, waiting: false, manual: false, title: "执行一次受风控闸门约束的轻量账号探测" };
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
    const wait = riskAccountWait(account);
    const timing = wait.until
      ? `<b>${esc(wait.label)} · ${esc(riskRemaining(wait.until))}</b><small>最早 ${esc(riskTime(wait.until))}</small>`
      : `<span class="mut">${esc(wait.label)}</span>${wait.manual ? "<small>浏览器处理后人工解除</small>" : ""}`;
    const queue = account.queued_tasks || { total: 0 };
    const reason = account.reason || "未检测到风险信号";
    const actualAccountId = account.platform_account_id
      ? `${account.platform_account_id_label || "账号 ID"} ${account.platform_account_id}`
      : "尚未获取平台账号 ID";
    return `<tr>
      <td><div class="risk-account"><b>${esc(account.nickname || "未命名账号")}</b><small title="${esc(actualAccountId)}">${esc(riskPlatformLabel(account.platform))} · ${esc(actualAccountId)}</small></div></td>
      <td><div style="display:flex;align-items:center;gap:5px;flex-wrap:wrap"><span class="risk-status ${esc(account.status_tone)}">${esc(account.status_label)}</span><span class="pill bare num">L${Number(account.risk_level) || 0}</span></div></td>
      <td><div class="risk-reason"><span title="${esc(reason)}">${esc(reason)}</span><small>${account.last_risk_at ? "触发于 " + esc(riskTime(account.last_risk_at)) : "暂无风险记录"}</small></div></td>
      <td><div class="risk-account">${timing}<div class="risk-progress" style="margin-top:4px"><div class="risk-progress-track"><i style="width:${progress}%"></i></div><span>${account.risk_level > 0 ? `${account.recovery_successes}/${account.recovery_target}` : "完成"}</span></div></div></td>
      <td><b class="num">${account.blocked_tasks || 0} / ${queue.total || 0}</b><div class="mut" style="font-size:11px" title="${esc(account.latest_block_reason || "")}">受阻 / 待执行${account.task_next_allowed_at ? ` · ${esc(riskRemaining(account.task_next_allowed_at))}` : ""}</div></td>
      <td><div class="risk-account"><span>${account.proxy ? `<code>${esc(account.proxy)}</code>` : '<span style="color:var(--warn)">⚠️ 本机直连</span>'}</span><small>${esc(account.proxy_status || "unknown")} · ${esc(account.network_key || "—")}</small>${account.proxy_warning ? `<small style="color:var(--warn)" title="${esc(account.proxy_warning)}">⚠️ ${esc(account.proxy_warning)}</small>` : ""}</div></td>
      <td class="acttd">
        <button class="ghost sm" onclick="probeRiskAccount(${account.account_id})" ${wait.waiting ? "disabled" : ""} title="${esc(wait.title)}">${wait.manual ? "待验证" : wait.waiting ? "等待探测" : "探测"}</button>
        <button class="ghost sm" onclick="showRiskEvents(${account.account_id})">记录</button>
        <button class="ghost sm" onclick="openAccountBrowser(${account.account_id})">浏览器</button>
        ${account.status !== "normal" ? `<button class="ghost sm danger" onclick="clearRiskAccount(${account.account_id})">解除</button>` : ""}
      </td>
    </tr>`;
  }).join("") || empty(7, "没有匹配的账号状态", "i-shield", "调整筛选条件或先添加平台账号");
}

function fillRiskConfig(config) {
  if (!config) return;
  const r = config.risk_control || {}, s = config.schedule || {};
  const set = (id, value) => { const el = $(id); if (el) { el.value = value ?? ""; if (el._csSync) el._csSync(); } };
  $("risk-enabled").checked = !!r.enabled;
  set("risk-mode", r.mode); set("risk-retention", r.event_retention_days);
  set("risk-read-light", r.read_light_gap_seconds); set("risk-read-heavy", r.read_heavy_gap_seconds);
  set("risk-operation-min", r.operation_gap_min_seconds); set("risk-operation-max", r.operation_gap_max_seconds);
  set("risk-session-limit", r.session_operation_limit);
  set("risk-rest-min", r.session_rest_min_seconds); set("risk-rest-max", r.session_rest_max_seconds);
  set("risk-retry-jitter", r.network_retry_jitter_seconds);
  set("risk-scan-jitter", Math.round((s.scan_jitter || 0) * 100));
  set("risk-comment-jitter", Math.round((s.comment_jitter || 0) * 100));
  set("risk-dm-poll-jitter", Math.round((s.xhs_dm_poll_jitter || 0) * 100));
  set("risk-initial-spread", s.initial_scan_spread_seconds);
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
  const field = $(id), raw = String(field.value ?? "").trim(), value = Number(raw);
  field.removeAttribute("aria-invalid");
  if (!raw || !Number.isFinite(value) || value < Number(field.min || 0)
      || (field.max !== undefined && field.max !== "" && value > Number(field.max))) {
    field.setAttribute("aria-invalid", "true"); field.focus?.();
    throw new Error((field.labels?.[0]?.textContent || id) + "填写不正确，请检查取值范围");
  }
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
      operation_gap_min_seconds: riskNumber("risk-operation-min"), operation_gap_max_seconds: riskNumber("risk-operation-max"),
      session_operation_limit: riskNumber("risk-session-limit"),
      session_rest_min_seconds: riskNumber("risk-rest-min"), session_rest_max_seconds: riskNumber("risk-rest-max"),
      network_retry_jitter_seconds: riskNumber("risk-retry-jitter"),
      shared_write_gap_seconds: riskNumber("risk-shared-write", 60),
      cooldown_steps_seconds: steps.map(v => Math.round(v * 60)),
      recovery_successes: riskNumber("risk-recovery-count"), recovery_probe_gap_seconds: riskNumber("risk-probe-gap", 60),
      event_retention_days: riskNumber("risk-retention"),
      network_group_risk_accounts: riskNumber("risk-network-accounts"),
      network_group_risk_window_seconds: riskNumber("risk-network-window", 60),
      network_group_cooldown_seconds: riskNumber("risk-network-cooldown", 60),
      combined_action_hourly_cap: riskNumber("risk-combined-hourly"), combined_action_daily_cap: riskNumber("risk-combined-daily"),
    };
    if (r.operation_gap_max_seconds < r.operation_gap_min_seconds) throw new Error("跨功能间隔上限应不小于下限");
    if (r.session_rest_max_seconds < r.session_rest_min_seconds) throw new Error("休息上限应不小于下限");
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
      scan_jitter: riskNumber("risk-scan-jitter") / 100,
      comment_jitter: riskNumber("risk-comment-jitter") / 100,
      xhs_dm_poll_jitter: riskNumber("risk-dm-poll-jitter") / 100,
      initial_scan_spread_seconds: riskNumber("risk-initial-spread"),
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
  const nickname = RISK_ACCOUNTS.find(account => account.account_id === accountId)?.nickname
    || ACCOUNTS.find(account => account.id === accountId)?.nickname
    || `账号 ${accountId}`;
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
    toast(`账号风控状态已解除${result.woken_tasks ? `，已唤醒 ${result.woken_tasks} 条任务` : ""}`, "ok");
    await refreshRiskCenter();
    if (CURRENT_TAB === "collections") await refreshCollections();
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
    const labels = { policy_updated: "规则修改", manual_probe: "人工探测", account_risk_cleared: "人工解除", task_result_resolved: "任务结果核对" };
    $("risk-event-list").innerHTML = rows.map(row => {
      const detail = row.detail || {};
      const changeCount = Object.values(detail.changes || {}).reduce((sum, section) => sum + Object.keys(section || {}).length, 0);
      const summary = detail.note || detail.reason || (changeCount ? `修改 ${changeCount} 项规则` : detail.skipped ? `探测延后：${detail.reason || "风控闸门未放行"}` : "操作完成");
      return `<div class="risk-event"><time>${esc(riskTime(row.created_at))}</time><span class="risk-status warn">${esc(labels[row.action] || row.action)}</span><b>${row.account_id ? `账号 ${row.account_id}` : "全局"}</b><div class="risk-event-detail">${esc(summary)}<small>${esc(row.actor || "local-ui")}</small></div></div>`;
    }).join("") || '<div class="hint">暂无风控管理变更记录。</div>';
  } catch (e) { $("risk-event-list").innerHTML = `<div class="hint">加载失败：${esc(e.message)}</div>`; }
}
document.addEventListener("keydown", event => {
  if (event.key === "Escape" && $("risk-event-modal")?.style.display !== "none") hideRiskEvents();
});

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
        ? " 已同时打开浏览器环境体检标签。" : "";
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
    title: "打开浏览器环境检测",
    message: "将在该账号的独立指纹环境中打开外部环境检测页。检测站点会读取当前出口 IP 和浏览器特征；结果仅用于环境核对，不代表平台风控一定通过。",
    okText: "打开检测页",
  });
  if (!confirmed) return;
  await withBusy(btn, "打开中", async () => {
    try {
      await api("/api/accounts/" + id + "/environment-check", { method: "POST" });
      toast("环境检测页已在该账号独立环境中打开，请核对 IP、时区、WebRTC 与指纹一致性", "ok", 8000);
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

// 从「账号」面板或跨平台矩阵跳转查看该账号的本账号数据(作品/关注/粉丝/私信)
function openAccountHub(id) {
  const targetAcc = typeof findAnyAccount === "function" ? findAnyAccount(id) : null;
  if (targetAcc && targetAcc.platform && (GLOBAL_MODE || targetAcc.platform !== PLATFORM)) {
    GLOBAL_MODE = false;
    PLATFORM = targetAcc.platform;
    try {
      localStorage.setItem("dym-global", "0");
      localStorage.setItem("dym-pf", PLATFORM);
    } catch (e) {}
    ACCOUNTS = ALL_ACCOUNTS.filter(a => a.platform === PLATFORM);
    applyPlatformUI();
    populateHubAccounts();
  }
  setHubAcc(id);
  const s = $("hub-acc"); if (s) { s.value = HUB_ACC; if (s._csSync) s._csSync(); }
  DM_CONV = null;
  DM_NEW_TARGET = "";
  window.dispatchEvent(new CustomEvent("creatorhub:hub-mode", { detail: { mode: "single", accountId: Number(id) } }));
  refreshHubSummary();
  switchTab("hub", true);
  switchHubTab("myworks");   // 默认落到「我的作品」,可再切关注/粉丝/私信
}
function openAggregatedDm(accountId, convId) {
  const targetAcc = typeof findAnyAccount === "function" ? findAnyAccount(accountId) : null;
  if (targetAcc && targetAcc.platform && (GLOBAL_MODE || targetAcc.platform !== PLATFORM)) {
    GLOBAL_MODE = false;
    PLATFORM = targetAcc.platform;
    try {
      localStorage.setItem("dym-global", "0");
      localStorage.setItem("dym-pf", PLATFORM);
    } catch (e) {}
    ACCOUNTS = ALL_ACCOUNTS.filter(a => a.platform === PLATFORM);
    applyPlatformUI();
    populateHubAccounts();
  }
  setHubAcc(accountId);
  const select = $("hub-acc"); if (select) { select.value = HUB_ACC; select._csSync?.(); }
  window.dispatchEvent(new CustomEvent("creatorhub:hub-mode", { detail: { mode: "single", accountId: Number(accountId) } }));
  switchTab("hub", true);
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

// 08-dm-center.js — 私信会话收件箱、SSE 实时消息流、历史消息拉取与自动回复规则。
// ── 私信 ──
// ─── 私信实时接收(SSE):进 DM 面板订阅,新消息即时刷新;离开断开 ───
let DM_SSE = null, DM_SSE_ACC = "", DM_AUTO_TASKS = [], DM_AUTO_RULES = [], DM_REFRESH_TIMER = null;
function startDmStream() {
  // 幂等:同账号已连就不重连(避免每次面板刷新/收到消息都断开重来)
  if ((DM_SSE || DM_REFRESH_TIMER) && DM_SSE_ACC === HUB_ACC &&
      (!DM_SSE || DM_SSE.readyState !== 2)) return;
  stopDmStream();
  if (!HUB_ACC) return;
  DM_SSE_ACC = HUB_ACC;
  if (PLATFORM !== "douyin" && PLATFORM !== "xhs") return;
  try {
    DM_SSE = new EventSource(`/api/dm/stream?account_id=${HUB_ACC}`);
    DM_SSE.onmessage = (e) => {
      let evt; try { evt = JSON.parse(e.data); } catch (_) { return; }
      if (!evt || !evt.conv_id) return;
      // 当前打开的会话:实时刷新线程 + 标记已读(不让红点冒出来);否则只刷列表(会有红点)
      if (evt.conv_id === DM_CONV) { refreshDmMessages(); markDmRead(evt.conv_id); }
      else refreshDmConvs();
      if (PLATFORM === "xhs" && evt.type === "auto_reply") refreshDmAutomation();
    };
    DM_SSE.onerror = () => { /* EventSource 自带重连 */ };
  } catch (_) {}
}
function stopDmStream() {
  if (DM_SSE) { try { DM_SSE.close(); } catch (_) {} DM_SSE = null; }
  if (DM_REFRESH_TIMER) { clearInterval(DM_REFRESH_TIMER); DM_REFRESH_TIMER = null; }
  DM_SSE_ACC = "";
}

async function refreshDmConvs() {
  const isCurrent = beginViewRequest("dm-convs", () => String(HUB_ACC));
  const box = $("dm-convs"); if (!box) return;
  if (!HUB_ACC) { box.innerHTML = `<div class="empty" style="padding:24px"><div class="empty-t">请先选择账号</div></div>`; return; }
  try {
    const list = await api("/api/dm/conversations?account_id=" + HUB_ACC);
    if (!isCurrent()) return;
    DM_CONVS = list;
    if ($("hb-dm")) $("hb-dm").textContent = list.length;
    box.innerHTML = list.length ? list.map(convRow).join("")
      : `<div class="empty" style="padding:24px"><div class="empty-ic">${ic("i-send")}</div><div class="empty-t">暂无会话</div><div class="empty-sub">点右上「同步私信」</div></div>`;
    if (DM_CONV) { const el = box.querySelector(`.dm-conv[data-conv="${cssAttr(DM_CONV)}"]`); if (el) el.classList.add("active"); }
  } catch (e) { if (!isCurrent()) return; box.innerHTML = `<div class="empty" style="padding:24px"><div class="empty-t">加载失败:${esc(e.message)}</div></div>`; }
}
function cssAttr(s) { return (s || "").toString().replace(/"/g, '\\"'); }
function convRow(c) {
  return `<div class="dm-conv" data-conv="${esc(c.conv_id)}" onclick="openDmConv(this.dataset.conv)">
    ${c.peer_avatar ? `<img class="avatar" src="${esc(safeMediaUrl(c.peer_avatar))}" referrerpolicy="no-referrer" alt="">` : `<span class="avatar"></span>`}
    <div class="meta"><b>${esc(c.peer_nickname)}</b><div class="last">${esc(c.last_text || "")}</div></div>
    ${c.unread_count ? `<span class="unread">${c.unread_count}</span>` : ""}
  </div>`;
}
async function syncDm() {
  if (!HUB_ACC) { toast("请先选择账号", "err"); return; }
  await withBusy(evtBtn(), "同步中", async () => {
    try {
      const r = await api("/api/accounts/" + HUB_ACC + "/dm/sync", { method: "POST" });
      if (r.skipped && r.cached) toast(`检查间隔保护中，已展示 ${r.fetched} 个现有会话`, "ok");
      else if (r.skipped) toast(`检查间隔保护中，请稍后再试`, "ok");
      else toast(`同步完成：${r.fetched} 个会话，新增消息 ${r.added}`, "ok");
    }
    catch (e) { toast("同步失败:" + e.message, "err"); }
  });
  refreshDmConvs();
  refreshDmAutomation();
}
async function openDmConv(convId) {
  DM_CONV = convId;
  DM_NEW_TARGET = "";
  const isCurrent = beginViewRequest("open-dm-conv", () => `${HUB_ACC}:${DM_CONV}`);
  const accountId = HUB_ACC;
  document.querySelectorAll("#dm-convs .dm-conv").forEach(e => e.classList.toggle("active", e.dataset.conv === convId));
  const thread = $("dm-thread");
  if (thread) thread.innerHTML = `<div class="empty"><div class="empty-t">加载聊天记录…</div></div>`;
  // 抖音:点开会话时无头拉历史(imapi get_by_conversation),落库后再渲染
  if (PLATFORM === "douyin" || PLATFORM === "xhs") {
    try { await api(`/api/accounts/${accountId}/dm/conversations/${encodeURIComponent(convId)}/fetch-history`, { method: "POST" }); }
    catch (e) { /* 拉取失败也照常显示库里已有的(最后一条) */ }
  }
  if (!isCurrent()) return;
  markDmRead(convId);
  await refreshDmMessages();
}

function normalizeDmTarget(value) {
  let target = String(value || "").trim();
  if (!target) return "";
  try {
    const url = new URL(target);
    if (url.protocol !== "https:" || !/(^|\.)douyin\.com$/i.test(url.hostname)) return "";
    const match = url.pathname.match(/\/user\/([^/?#]+)/);
    if (!match) return "";
    target = decodeURIComponent(match[1]);
  } catch (_) {}
  return target.replace(/^@/, "").trim();
}

async function startNewDm() {
  if (!HUB_ACC) { toast("请先选择账号", "err"); return; }
  if (PLATFORM !== "douyin") return;
  const raw = await uiPrompt({
    title: "发起新私信",
    hint: "填写对方抖音号、主页链接或 sec_uid。抖音号会先精确解析为内部 UID。",
    placeholder: "抖音号 / https://www.douyin.com/user/...",
  });
  if (raw === null) return;
  const target = normalizeDmTarget(raw);
  if (!target) { toast("目标用户不能为空", "err"); return; }
  DM_CONV = null;
  DM_NEW_TARGET = target;
  document.querySelectorAll("#dm-convs .dm-conv").forEach(e => e.classList.remove("active"));
  const thread = $("dm-thread");
  if (thread) thread.innerHTML = `<div class="empty"><div class="empty-ic">${ic("i-send")}</div><div class="empty-t">新私信</div><div class="empty-sub">目标 ${esc(target)}</div></div>`;
  const input = $("dm-input");
  if (input) { input.placeholder = "输入第一条私信…"; input.focus(); }
}

function dmRuleSummary(rule) {
  const trigger = rule.match_mode === "all" ? "全部文本消息" : (rule.keywords || []).join("、");
  const mode = rule.review_before_send ? "先审核" : "自动入队";
  return `<div class="dm-auto-row">
    <div class="grow"><b>${esc(rule.name)}</b>${rule.enabled ? "" : " · 已停用"}<div class="sub">${esc(trigger)} · ${mode} · 延迟 ${rule.min_delay_seconds}-${rule.max_delay_seconds} 秒 · 冷却 ${Math.round(rule.cooldown_seconds / 3600)} 小时</div></div>
    <button class="ghost sm" onclick="toggleDmRule(${rule.id})">${rule.enabled ? "停用" : "启用"}</button>
    <button class="ghost sm" onclick="deleteDmRule(${rule.id})">删除</button>
  </div>`;
}
function dmDraftSummary(task) {
  return `<div class="dm-auto-row">
    <div class="grow"><b>${esc(task.target_nick || "私信会话")}</b><div class="sub">${esc(task.content || "")}</div></div>
    <button class="ghost sm" onclick="editDmDraft(${task.id})">编辑</button>
    <button class="sm" onclick="approveDmDraft(${task.id})">通过</button>
    <button class="ghost sm" onclick="cancelDmDraft(${task.id})">取消</button>
  </div>`;
}
async function refreshDmAutomation() {
  const isCurrent = beginViewRequest("dm-automation", () => HUB_ACC);
  const panel = $("dm-auto-panel"), rulesBox = $("dm-auto-rules"), tasksBox = $("dm-auto-tasks");
  if (!panel || !rulesBox || !tasksBox) return;
  panel.style.display = PLATFORM === "xhs" ? "" : "none";
  if (PLATFORM !== "xhs" || !HUB_ACC) return;
  try {
    const [rules, tasks, monitor] = await Promise.all([
      api(`/api/dm/auto-reply-rules?account_id=${HUB_ACC}`),
      api(`/api/account-actions?account_id=${HUB_ACC}&limit=100`),
      api(`/api/accounts/${HUB_ACC}/dm/automation/status`)
    ]);
    if (!isCurrent()) return;
    const statusBox = $("dm-monitor-status");
    if (statusBox) {
      const live = monitor.realtime || {};
      const state = live.connected ? "实时监听已连接" : (live.state === "reconnecting" ? "实时监听重连中" : "低频补偿监听");
      statusBox.innerHTML = `<b>${state}</b> · 账号级监控 · 新会话自动纳入 · ${Math.round((monitor.fallback_interval_seconds || 600) / 60)} 分钟完整补偿检查`;
    }
    DM_AUTO_RULES = rules;
    rulesBox.innerHTML = rules.length ? rules.map(dmRuleSummary).join("") : `<div class="hint">暂无规则。建议先使用“先审核”观察一段时间。</div>`;
    DM_AUTO_TASKS = tasks;
    const drafts = tasks.filter(t => t.action === "send_dm" && t.source_rule_id && t.status === "draft");
    tasksBox.innerHTML = drafts.length ? drafts.map(dmDraftSummary).join("") : `<div class="hint">暂无待审核回复</div>`;
  } catch (e) { if (isCurrent()) rulesBox.innerHTML = `<div class="hint">加载失败：${esc(e.message)}</div>`; }
}
async function saveDmRule() {
  if (!HUB_ACC) { toast("请先选择账号", "err"); return; }
  const keywords = ($("dm-rule-keywords").value || "").split(/[，,]/).map(x => x.trim()).filter(Boolean);
  const excludes = ($("dm-rule-excludes").value || "").split(/[，,]/).map(x => x.trim()).filter(Boolean);
  const templates = ($("dm-rule-templates").value || "").split(/\r?\n/).map(x => x.trim()).filter(Boolean);
  const body = {
    account_id: +HUB_ACC, name: ($("dm-rule-name").value || "自动回复").trim(),
    enabled: true, match_mode: "keywords", keywords, exclude_keywords: excludes,
    reply_templates: templates, review_before_send: !$("dm-rule-auto").checked,
    min_delay_seconds: +$("dm-rule-delay-min").value || 75,
    max_delay_seconds: +$("dm-rule-delay-max").value || 300,
    cooldown_seconds: (+$("dm-rule-cooldown").value || 6) * 3600,
    max_message_age_seconds: 1800
  };
  try {
    await api("/api/dm/auto-reply-rules", { method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(body) });
    toast("自动回复规则已保存", "ok"); refreshDmAutomation();
  } catch (e) { toast("保存失败：" + e.message, "err"); }
}
async function deleteDmRule(id) {
  try { await api(`/api/dm/auto-reply-rules/${id}`, {method:"DELETE"}); toast("规则已删除", "ok"); refreshDmAutomation(); }
  catch (e) { toast("删除失败：" + e.message, "err"); }
}
async function toggleDmRule(id) {
  const rule = DM_AUTO_RULES.find(r => +r.id === +id); if (!rule) return;
  const body = Object.assign({}, rule, {enabled: !rule.enabled});
  try {
    await api(`/api/dm/auto-reply-rules/${id}`, {method:"PUT", headers:{"Content-Type":"application/json"}, body:JSON.stringify(body)});
    toast(body.enabled ? "规则已启用" : "规则已停用", "ok"); refreshDmAutomation();
  } catch (e) { toast("更新失败：" + e.message, "err"); }
}
async function approveDmDraft(id) {
  try { await api(`/api/account-actions/${id}/approve`, {method:"POST"}); toast("已进入限速发送队列", "ok"); refreshDmAutomation(); }
  catch (e) { toast("通过失败：" + e.message, "err"); }
}
async function cancelDmDraft(id) {
  try { await api(`/api/account-actions/${id}/cancel`, {method:"POST"}); toast("草稿已取消", "ok"); refreshDmAutomation(); }
  catch (e) { toast("取消失败：" + e.message, "err"); }
}
async function editDmDraft(id) {
  const current = (DM_AUTO_TASKS.find(t => +t.id === +id) || {}).content || "";
  const content = await uiPrompt({ title: "编辑回复内容", hint: "保存只更新草稿，不会发送消息。", value: current, multiline: true, rows: 4,
    submit: value => {
      if (!value.trim()) uiEditorError("回复内容不能为空", "ui-inp");
      return api(`/api/account-actions/${id}`, {method:"PUT", headers:{"Content-Type":"application/json"}, body:JSON.stringify({content:value.trim()})});
    },
  });
  if (content === null) return;
  toast("草稿已更新", "ok"); refreshDmAutomation();
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
    ? `<img src="${esc(safeMediaUrl(c.cover))}" loading="lazy" referrerpolicy="no-referrer" onerror="this.style.display='none'">`
    : "";
  const avatar = c.avatar
    ? `<img class="av" src="${esc(safeMediaUrl(c.avatar))}" loading="lazy" referrerpolicy="no-referrer" onerror="this.style.display='none'">`
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
  const isCurrent = beginViewRequest("dm-messages", () => `${HUB_ACC}:${DM_CONV}`);
  const thread = $("dm-thread"); if (!thread || !HUB_ACC || !DM_CONV) return;
  try {
    const msgs = await api(`/api/dm/messages?account_id=${HUB_ACC}&conv_id=${encodeURIComponent(DM_CONV)}`);
    if (!isCurrent()) return;
    thread.innerHTML = msgs.length
      ? msgs.map(m => `<div class="dm-bubble ${m.direction === "out" ? "out" : "in"}${m.card ? " card" : ""}">${dmBody(m)}<span class="t">${fmtTime(m.create_time)}</span></div>`).join("")
      : `<div class="empty"><div class="empty-t">暂无消息记录</div><div class="empty-sub">该会话没有可拉取的历史(或对方为系统号)</div></div>`;
    thread.scrollTop = thread.scrollHeight;
  } catch (e) { if (!isCurrent()) return; thread.innerHTML = `<div class="empty"><div class="empty-t">加载失败:${esc(e.message)}</div></div>`; }
}
async function sendDm() {
  const inp = $("dm-input"); const text = (inp.value || "").trim();
  if (!HUB_ACC) { toast("请先选择账号", "err"); return; }
  if (!DM_CONV && !DM_NEW_TARGET) { toast("请选择会话或点“新私信”", "err"); return; }
  if (!text) return;
  const c = DM_CONVS.find(x => x.conv_id === DM_CONV) || {};
  const accountId = HUB_ACC, conversationId = DM_CONV, newTarget = DM_NEW_TARGET;
  const numericTarget = /^\d+$/.test(newTarget);
  await withBusy(evtBtn(), "发送中", async () => {
    try {
      const result = await api("/api/account-actions", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          account_id: +accountId, action: "send_dm",
          target_uid: c.peer_uid || (numericTarget ? newTarget : ""),
          target_sec_uid: c.peer_sec_uid || (!numericTarget ? newTarget : ""),
          target_nick: c.peer_nickname || "", conv_id: conversationId || "",
          content: text, run_now: true,
        })
      });
      if (HUB_ACC === accountId && DM_CONV === conversationId && inp.value.trim() === text) inp.value = "";
      toast(result.ran ? "已发送" : `任务 #${result.id} 已保留：${result.execution_error || "等待队列发送"}`, result.ran ? "ok" : "info", 6000);
      // 发完重拉历史,展示刚发出的消息(imapi 有短暂延迟,稍等再拉)
      await new Promise(r => setTimeout(r, 700));
      if (HUB_ACC !== accountId || DM_CONV !== conversationId) return;
      if (conversationId) await openDmConv(conversationId);
      else {
        await refreshDmConvs();
        const created = DM_CONVS.find(row =>
          String(row.peer_uid || "") === newTarget || String(row.peer_sec_uid || "") === newTarget);
        if (created) {
          DM_NEW_TARGET = "";
          inp.placeholder = "输入私信内容…";
          await openDmConv(created.conv_id);
        }
      }
    } catch (e) { toast("发送失败:" + e.message, "err"); }
  });
}
function accOptions(list, ph) {
  return `<option value="">${ph}</option>` +
    list.map(a => `<option value="${a.id}">${esc(a.nickname)}${a.has_creator ? " · 创作号" : ""}</option>`).join("");
}
function populateAccountSelect() {
  const sel = $("t-acc"); if (!sel) return;
  const required = ["xhs", "douyin", "tiktok"].includes(PLATFORM);
  const channels = PLATFORM === "shipinhao";
  const platformName = PF_NAME[PLATFORM] || "当前平台";
  const list = PLATFORM === "tiktok"
    ? ACCOUNTS.filter(a => a.status === "active" && a.has_storage)
    : ACCOUNTS;
  sel.innerHTML = accOptions(
    list,
    required ? `请选择${platformName}账号(必选)` : channels ? "默认走本机微信客户端（无需账号）" : "不指定账号",
  );
  // 抖音匿名主页可能返回风控后的旧快照；作品监控与小红书一样必须使用登录态。
  if (required && list.length) sel.value = String(list[0].id);
  else if (channels) sel.value = "";
  if ($("t-acc-wrap")) $("t-acc-wrap").style.display = channels ? "none" : "";
}
function collectionRequiresAccount(platform = PLATFORM, source = collectionSourceValue()) {
  return !(platform === "shipinhao" && ["keyword", "creator"].includes(source));
}
function populateCollectionAccount() {
  const sel = $("col-account"); if (!sel) return;
  const current = sel.value;
  const platformName = PF_NAME[PLATFORM] || "平台";
  const requiresAccount = collectionRequiresAccount(PLATFORM, collectionSourceValue());
  const channels = PLATFORM === "shipinhao";
  const list = ACCOUNTS.filter(a => a.platform === PLATFORM && a.status !== "invalid" && a.has_storage);
  const placeholder = requiresAccount
    ? (list.length ? `请选择${platformName}账号` : `暂无可用${platformName}账号`)
    : "使用本机微信通道（无需网页账号）";
  sel.innerHTML = accOptions(list, placeholder);
  sel.required = requiresAccount;
  if ($("col-account-wrap")) $("col-account-wrap").style.display = (channels && !requiresAccount) ? "none" : "";
  if (!requiresAccount) {
    setFieldError(sel, "");
    sel.value = "";
  } else if (list.some(a => String(a.id) === current)) sel.value = current;
  else if (list.length) sel.value = String(list[0].id);
  else sel.value = "";
  if (sel._csSync) sel._csSync();
}
function populateWatchAccount() {
  const sel = $("w-acc"); if (!sel) return;
  const xhs = PLATFORM === "xhs";
  const tiktok = PLATFORM === "tiktok";
  const channels = PLATFORM === "shipinhao";
  const required = xhs || PLATFORM === "rednote" || tiktok;
  const creatorOnly = PLATFORM === "douyin" && $("w-mode") && $("w-mode").value === "creator";
  const list = tiktok
    ? ACCOUNTS.filter(a => a.status === "active" && a.has_storage)
    : creatorOnly ? ACCOUNTS.filter(a => a.has_creator) : ACCOUNTS;
  const ph = required ? "请选择" + PF_NAME[PLATFORM] + "账号(必选)"
    : (creatorOnly && list.length === 0 ? "无创作者账号,请先创作者登录" : channels ? "默认走本机微信客户端（无需账号）" : "不指定账号");
  sel.innerHTML = accOptions(list, ph);
  if (required && list.length) sel.value = String(list[0].id);
  else if (channels) sel.value = "";
  if ($("w-acc-wrap")) $("w-acc-wrap").style.display = channels ? "none" : "";
  if (sel._csSync) sel._csSync();
}
function populateDanmakuAccount() {
  const sel = $("d-w-acc"); if (!sel) return;
  const pf = PLATFORM === "bilibili" ? "bilibili" : "douyin";
  const creatorOnly = pf === "douyin" && $("d-w-mode") && $("d-w-mode").value === "creator";
  const list = creatorOnly
    ? ACCOUNTS.filter(a => a.platform === "douyin" && a.has_creator)
    : ACCOUNTS.filter(a => a.platform === pf);
  const ph = creatorOnly && !list.length ? "无创作者账号,请先创作者登录" : "不指定账号";
  sel.innerHTML = accOptions(list, ph);
  if (creatorOnly && list.length) sel.value = String(list[0].id);
  if (sel._csSync) sel._csSync();
}
function applyDanmakuForm() {
  const isBili = PLATFORM === "bilibili";
  if (isBili && $("d-w-mode") && $("d-w-mode").value === "creator") {
    $("d-w-mode").value = "public";
    $("d-w-mode")._csSync?.();
  }
  const kind = $("d-w-kind") ? $("d-w-kind").value : "auto";
  const mode = $("d-w-mode") ? $("d-w-mode").value : "public";
  const isVideo = kind === "video";
  const recentWrap = $("d-w-recent-wrap");
  const daysWrap = $("d-w-days-wrap");
  if (recentWrap) recentWrap.hidden = isVideo;
  if (daysWrap) daysWrap.hidden = isVideo;
  const urlLabel = $("d-w-url-label");
  if (urlLabel) urlLabel.textContent = isBili
    ? (isVideo ? "视频链接 / 完整分享文案 / BV 或 av 号" : kind === "user" ? "UP 主空间主页 / 完整分享文案 / mid" : "视频 BV/av / 空间主页 / 完整分享文案")
    : (isVideo ? "视频 / 完整分享文案 / aweme_id" : kind === "user" ? "账号主页 / 完整分享文案 / sec_uid" : "视频 / 账号主页 / 完整分享文案 / ID");
  if ($("d-w-url")) $("d-w-url").placeholder = isBili
    ? (isVideo ? "粘贴哔哩哔哩视频分享文案、链接或 BV/av 号" : kind === "user" ? "粘贴空间主页链接或 UP 主 mid" : "直接粘贴整段哔哩哔哩分享文案，将自动识别视频或 UP 主")
    : (isVideo ? "粘贴视频分享文案、作品链接或 aweme_id" : kind === "user" ? "粘贴主页分享文案、账号主页或 sec_uid" : "直接粘贴整段抖音分享文案，将自动识别视频或账号");
  resetDanmakuTargetState();
  const accLabel = $("d-w-acc-label");
  if (accLabel) accLabel.textContent = mode === "creator"
    ? "创作中心账号（必选）" : "播放页账号（可选）";
  const depthLabel = $("d-w-depth-label");
  if (depthLabel) depthLabel.textContent = mode === "creator"
    ? "创作中心翻页深度" : "弹幕加载轮次";
  const probeWrap = $("d-w-probe-wrap");
  if (probeWrap) probeWrap.hidden = mode === "creator" || isBili;
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
      if (["xhs", "rednote"].includes(r.platform || PLATFORM) && r.login_scope === "creator" && !r.has_read_login) {
        toast("创作平台登录有效，资料已更新；主站读取登录尚未配置", "info", 8000);
        return;
      }
      const refreshedPlatform = r.platform || PLATFORM;
      const idLbl = refreshedPlatform === "xhs" ? " · 小红书号 "
        : refreshedPlatform === "kuaishou" ? " · 快手号 "
        : refreshedPlatform === "shipinhao" ? " · 视频号 "
        : refreshedPlatform === "tiktok" ? " · TikTok号 "
        : refreshedPlatform === "rednote" ? " · Rednote号 " : refreshedPlatform === "bilibili" ? " · UID " : " · 抖音号 ";
      toast("\u8d44\u6599\u5df2\u66f4\u65b0\uff0c\u767b\u5f55\u72b6\u6001\u5df2\u6062\u590d:" + (r.nickname || "") + (r.douyin_id ? idLbl + r.douyin_id : ""), "ok");
    } catch (e) {
      toast("\u5237\u65b0\u5931\u8d25:" + e.message, "err");
    }
  });
  await refreshAccounts();
}
async function setProxy(id) {
  const a = findAnyAccount(id);
  const cur = a && a.has_proxy ? a.proxy : "";
  const curId = a ? a.proxy_id : null;
  const pf = a ? a.platform : PLATFORM;
  const pfLabel = (typeof PF_NAME !== "undefined" && PF_NAME[pf]) ? PF_NAME[pf] : (pf || "当前平台");
  let apiResult = null;
  const choice = await uiProxyPicker({
    title: `账号代理配置${a && a.nickname ? " · " + a.nickname : ""}`,
    hint: `所属平台：${pfLabel}。建议尽量保持「一号一独立代理」；切换代理后会自动关闭该账号当前常驻浏览器上下文，下次任务使用新网络出口。`,
    platform: pf,
    currentProxy: cur,
    currentProxyId: curId,
    isAccountEdit: true,
    okText: "保存代理配置",
    busyText: "保存中",
    submit: async picked => {
      if (picked.mode === "auto") {
        apiResult = await api("/api/accounts/" + id + "/assign-proxy", { method: "POST" });
      } else {
        apiResult = await api("/api/accounts/" + id + "/proxy", {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ proxy: picked.proxy }),
        });
      }
    },
  });
  if (!choice || !apiResult) return;
  if (choice.mode === "auto") {
    toast("已从代理池分配：" + apiResult.proxy, "ok");
  } else {
    toast(choice.proxy ? "代理已设置：" + apiResult.proxy : "已清除代理，当前改用本机直连", "ok");
  }
  if (apiResult.warning) {
    setTimeout(() => toast("⚠️ " + apiResult.warning, "info", 7000), 280);
  }
  refreshAccounts();
  refreshProxies();
  window.dispatchEvent(new Event("creatorhub:matrix:refresh"));
}

async function setBrowserBackend(id) {
  const account = findAnyAccount(id);
  if (!account) return;
  let catalog;
  try {
    catalog = await api("/api/browser-backends");
  } catch (e) {
    toast("读取浏览器环境失败:" + e.message, "err");
    return;
  }
  const isXhs = account.platform === "xhs";
  const options = browserChoiceOptions(catalog, { localOnly: false });
  const currentChoice = account.browser_backend === "fingerprint_chromium" && account.browser_runtime_id
    ? `fingerprint_chromium::${account.browser_runtime_id}`
    : (account.browser_backend || (isXhs ? "local" : "default"));
  const selected = await uiSelect({
    title: "账号浏览器环境",
    hint: isXhs
      ? `${account.nickname} · 单号推荐系统 Chrome/CDP；同机多小红书号推荐切换至 Fingerprint Chromium 隔离硬件指纹。`
      : `${account.nickname} · 切换时会关闭该账号当前浏览器，下次任务使用新内核。`,
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
        <button type="button" class="ghost sm active" role="tab" id="fp-tab-basic" aria-controls="fp-panel-basic" tabindex="0" aria-selected="true" data-fp-tab="basic">基础设置</button>
        <button type="button" class="ghost sm" role="tab" id="fp-tab-advanced" aria-controls="fp-panel-advanced" tabindex="-1" aria-selected="false" data-fp-tab="advanced">高级设置</button>
      </div>
      <div class="fp-edit-panel active" id="fp-panel-basic" role="tabpanel" aria-labelledby="fp-tab-basic" data-fp-panel="basic">
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
      <div class="fp-edit-panel" id="fp-panel-advanced" role="tabpanel" aria-labelledby="fp-tab-advanced" data-fp-panel="advanced">
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
        globalThis.CreatorHubMotion?.reveal?.(body.querySelector(`[data-fp-custom="${segment.dataset.fpMode}"].enabled`));
      });
    });
    ["platform", "brand", "language", "timezone", "location", "viewport", "gpu", "cpu"].forEach(syncCustomFields);
    body.querySelectorAll("[data-fp-mode]").forEach(segment => {
      const label = segment.closest(".fp-config-row")?.querySelector("b");
      if (label) { label.id = "fp-label-" + segment.dataset.fpMode; segment.setAttribute("aria-labelledby", label.id); }
    });
    body.querySelectorAll("[data-fp-tab]").forEach(tab => {
      tab.addEventListener("keydown", event => {
        if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
        event.preventDefault();
        const tabs = [...body.querySelectorAll("[data-fp-tab]")], index = tabs.indexOf(tab);
        const next = event.key === "Home" ? tabs[0] : event.key === "End" ? tabs[tabs.length - 1] : tabs[(index + (event.key === "ArrowRight" ? 1 : tabs.length - 1)) % tabs.length];
        next.click(); next.focus({ preventScroll: true });
      });
      tab.addEventListener("click", () => {
        const selected = tab.dataset.fpTab;
        body.querySelectorAll("[data-fp-tab]").forEach(item => {
          const active = item.dataset.fpTab === selected;
          item.classList.toggle("active", active);
          item.setAttribute("aria-selected", active ? "true" : "false");
          item.tabIndex = active ? 0 : -1;
        });
        body.querySelectorAll("[data-fp-panel]").forEach(panel => panel.classList.toggle("active", panel.dataset.fpPanel === selected));
        body.scrollTo({ top: 0, behavior: "instant" });
        globalThis.CreatorHubMotion?.reveal?.(body.querySelector(`[data-fp-panel="${selected}"]`), "x", selected === "basic" ? -1 : 1);
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
      { okText: preLogin ? "使用此指纹登录" : "保存配置", wide: true, icon: "i-fingerprint", submit: options.submit || null },
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
  const account = findAnyAccount(id);
  if (!account) return;
  let current;
  try { current = await api(`/api/accounts/${id}/fingerprint`); }
  catch (e) { toast("读取指纹失败:" + e.message, "err"); return; }
  let savedFingerprint;
  const action = await uiFingerprintEditor(account, current, {
    submit: async action => {
      savedFingerprint = await api(`/api/accounts/${id}/fingerprint`, {
        method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(action.data),
      });
    },
  });
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
  toast(`指纹已保存：${savedFingerprint?.fingerprint?.fingerprint_id || "-"}`, "ok", 7000);
  await refreshAccounts();
}

// 09-proxies-settings.js — 代理池增删改查、连通性归属地探测、兼容矩阵与全局下载/AI 设置。
// ─── 代理池 ───
let PROXIES = [];
let LAST_DETECT = null;   // {url, geo} 判别结果,加入池时一并带上归属地
let PROXY_FILTER = "all";
const SELECTED_PROXY_IDS = new Set();
const PROXY_TESTING_IDS = new Set();

function dismissProxyFormatHelp(event) {
  const help = $("px-format-help");
  if (!help?.open) return;
  if (event.type === "keydown") {
    if (event.key !== "Escape" || event.isComposing) return;
    help.open = false;
    $("px-format-toggle")?.focus();
    event.preventDefault();
    event.stopPropagation();
  }
}
function syncProxyFormatHelp(help) {
  const method = help.open ? "addEventListener" : "removeEventListener";
  document[method]("keydown", dismissProxyFormatHelp, true);
}
function proxyInputOptions() {
  return {
    default_scheme: $("px-proto")?.value || "http",
    input_format: $("px-format")?.value || "auto",
  };
}
function suggestGeoLabel(g, existingLabels = new Set()) {
  if (!g) return "";
  const parts = [g.country, g.city || g.region].map(x => String(x || "").trim()).filter(Boolean);
  const base = parts.join("·");
  if (!base) return "";
  if (!existingLabels.has(base)) return base;
  let seq = 2;
  while (existingLabels.has(`${base}-${String(seq).padStart(2, "0")}`)) seq++;
  return `${base}-${String(seq).padStart(2, "0")}`;
}
function showProxyDetectBanner(message, tone = "neutral") {
  const el = $("px-detect-banner");
  if (!el) return;
  if (!message) { el.hidden = true; el.innerHTML = ""; return; }
  el.hidden = false;
  if (tone === "ok" || tone === "err") el.dataset.tone = tone;
  else el.removeAttribute("data-tone");
  el.innerHTML = `<span>${esc(message)}</span><button type="button" class="ghost sm" style="min-height:22px;padding:1px 6px" onclick="showProxyDetectBanner('')">关闭</button>`;
}
function filteredPoolProxies() {
  return PROXIES.filter(p => {
    if (PROXY_FILTER === "ok") return p.enabled !== false && p.status === "ok";
    if (PROXY_FILTER === "idle") return p.enabled !== false && !Number(p.used_by || 0);
    if (PROXY_FILTER === "bound") return Number(p.used_by || 0) > 0;
    if (PROXY_FILTER === "mainland") return Boolean(p.geo_checked && p.is_mainland);
    if (PROXY_FILTER === "overseas") return Boolean(p.geo_checked && !p.is_mainland);
    if (PROXY_FILTER === "issue") return p.enabled === false || ["bad", "auth_error", "blocked", "unknown"].includes(p.status);
    return true;
  });
}
function syncProxyBatchBar() {
  const validIds = new Set(PROXIES.map(p => p.id));
  for (const id of [...SELECTED_PROXY_IDS]) {
    if (!validIds.has(id)) SELECTED_PROXY_IDS.delete(id);
  }
  const bar = $("px-batch-bar");
  const cnt = $("px-sel-count");
  if (cnt) cnt.textContent = String(SELECTED_PROXY_IDS.size);
  if (bar) bar.hidden = SELECTED_PROXY_IDS.size === 0;
  const checkAll = $("px-check-all");
  if (checkAll) {
    const visible = filteredPoolProxies();
    const selVisible = visible.filter(p => SELECTED_PROXY_IDS.has(p.id)).length;
    checkAll.checked = visible.length > 0 && selVisible === visible.length;
    checkAll.indeterminate = selVisible > 0 && selVisible < visible.length;
  }
}
function setProxyPoolFilter(flt) {
  PROXY_FILTER = flt || "all";
  renderProxyPoolView();
}
function onRowProxySelect(id, checked) {
  if (checked) SELECTED_PROXY_IDS.add(Number(id));
  else SELECTED_PROXY_IDS.delete(Number(id));
  syncProxyBatchBar();
}
function toggleSelectAllProxies(checked) {
  filteredPoolProxies().forEach(p => {
    if (checked) SELECTED_PROXY_IDS.add(p.id);
    else SELECTED_PROXY_IDS.delete(p.id);
  });
  renderProxyPoolView();
}
function clearProxySelection() {
  SELECTED_PROXY_IDS.clear();
  renderProxyPoolView();
}
async function copyPoolProxyUrl(id) {
  const p = PROXIES.find(x => x.id === id);
  const text = p && (p.url_full || p.url);
  if (!text) return;
  try {
    await navigator.clipboard.writeText(text);
    toast("已复制完整代理地址", "ok", 2200);
  } catch (e) {
    toast("复制失败，可在「编辑」中查看完整地址", "err");
  }
}
function renderProxyPoolView() {
  const tb = $("proxy-table");
  if (!tb) return;
  const kpiEl = $("px-kpi-bar");
  if (kpiEl) {
    const counts = {
      all: PROXIES.length,
      ok: PROXIES.filter(p => p.enabled !== false && p.status === "ok").length,
      idle: PROXIES.filter(p => p.enabled !== false && !Number(p.used_by || 0)).length,
      bound: PROXIES.filter(p => Number(p.used_by || 0) > 0).length,
      mainland: PROXIES.filter(p => p.geo_checked && p.is_mainland).length,
      overseas: PROXIES.filter(p => p.geo_checked && !p.is_mainland).length,
      issue: PROXIES.filter(p => p.enabled === false || ["bad", "auth_error", "blocked", "unknown"].includes(p.status)).length,
    };
    const specs = [
      ["all", "全部节点", counts.all],
      ["ok", "连通正常", counts.ok],
      ["idle", "空闲未绑", counts.idle],
      ["bound", "已绑账号", counts.bound],
      ["mainland", "大陆节点", counts.mainland],
      ["overseas", "境外节点", counts.overseas],
      ["issue", "待测 / 异常", counts.issue],
    ];
    kpiEl.innerHTML = specs.map(([key, label, num]) =>
      `<button type="button" class="px-kpi-chip${PROXY_FILTER === key ? " active" : ""}" onclick="setProxyPoolFilter('${key}')"><span>${esc(label)}</span><b>${num}</b></button>`
    ).join("");
  }

  const stCls = s => s === "ok" ? "active" : ["bad", "auth_error", "blocked"].includes(s) ? "invalid" : "bare";
  const stTxt = { ok: "可连接", bad: "连接失败", auth_error: "认证失败", blocked: "检测受限", unknown: "未测" };
  const geoCell = p => {
    if (!p.geo_checked) {
      return `<span class="pill bare">尚未检测</span><div class="mut" style="font-size:11px;margin-top:3px">点击右侧「测试」获取出口 IP</div>`;
    }
    const regionBadge = p.is_mainland
      ? `<span class="pill active bare">大陆 · ${esc(p.geo_loc || "CN")}</span>`
      : `<span class="pill downloading bare" title="境外/港澳台出口 IP，适配 TikTok / Instagram / Twitter / Rednote；国内平台登录可能触发异地验证">境外 · ${esc(p.geo_loc || "境外")}</span>`;
    return `<div>${regionBadge}</div>` +
      (p.exit_ip ? `<div class="mut" style="font-size:11px;margin-top:3px"><code>${esc(p.exit_ip)}</code>${p.isp ? " · " + esc(p.isp) : ""}</div>` : "");
  };
  const boundCell = p => {
    const accs = Array.isArray(p.bound_accounts) ? p.bound_accounts : [];
    const count = Number(p.used_by || accs.length || 0);
    if (!count) return `<span class="pill bare">空闲独立</span>`;
    if (!accs.length) {
      return `<span class="pill ${count > 1 ? "pending" : "active"}">${count > 1 ? `⚠️ 共用 ${count} 号` : `${count} 个账号`}</span>`;
    }
    const visibleAccs = accs.slice(0, 3);
    const rest = accs.length - visibleAccs.length;
    const allNames = accs.map(a => `${(typeof PF_NAME !== "undefined" && PF_NAME[a.platform]) || a.platform} · ${a.nickname}`).join("；");
    return `<div class="px-acc-list" title="${esc(allNames)}">
      ${visibleAccs.map(a => `<span class="px-acc-chip"><small>${esc((typeof PF_NAME !== "undefined" && PF_NAME[a.platform]) || a.platform)}</small>${esc(a.nickname)}</span>`).join("")}
      ${rest > 0 ? `<span class="pill bare">+${rest}</span>` : ""}
      ${p.same_platform_shared ? `<span class="pill pending bare" title="同平台下有多个账号共用该代理，建议尽量一号一独立代理">⚠️ 同平台共用</span>` : ""}
    </div>`;
  };

  const rows = filteredPoolProxies();
  if (!PROXIES.length) {
    tb.querySelector("tbody").innerHTML = empty(5, "代理池为空", "i-shield", "在上方粘贴地址快速添加，或点击右上角「批量导入」");
  } else if (!rows.length) {
    tb.querySelector("tbody").innerHTML = `<tr><td colspan="5"><div class="empty"><div class="empty-t">当前筛选条件下暂无节点</div><div class="empty-sub"><button type="button" class="ghost sm" onclick="setProxyPoolFilter('all')">查看全部节点 (${PROXIES.length})</button></div></div></td></tr>`;
  } else {
    tb.querySelector("tbody").innerHTML = rows.map(p => {
      const testing = PROXY_TESTING_IDS.has(p.id);
      const checked = SELECTED_PROXY_IDS.has(p.id);
      const scheme = String(p.scheme || (String(p.url || "").split("://")[0]) || "http").toUpperCase();
      const autoHint = !p.label && p.geo_checked ? suggestGeoLabel(p) : "";
      const titleHtml = p.label
        ? `<b>${esc(p.label)}</b>`
        : `<span class="mut"><b>${esc(autoHint || p.host_port || "(未命名)")}</b> <small>(未设备注)</small></span>`;
      return `<tr>
        <td><input type="checkbox" ${checked ? "checked" : ""} onchange="onRowProxySelect(${p.id}, this.checked)" aria-label="选择代理 ${esc(p.label || p.url)}"></td>
        <td>
          <div class="px-node-head">
            ${titleHtml}
            <span class="pxp-proto">${esc(scheme)}</span>
            <span class="pill ${stCls(p.status)}">${stTxt[p.status] || p.status}</span>
            ${p.enabled === false ? '<span class="pill bare">已停用</span>' : ""}
          </div>
          <div class="px-node-sub">
            <code>${esc(p.url)}</code>
            ${p.user_hint ? `<span class="px-user-hint" title="认证用户名首尾特征（密码已脱敏），用于区分同一网关下的不同通道">账号 ${esc(p.user_hint)}</span>` : ""}
            <button type="button" class="ghost sm px-mini-copy" onclick="copyPoolProxyUrl(${p.id})" title="复制含账密的完整代理地址">复制</button>
          </div>
          ${p.note ? `<div class="mut" style="font-size:11px;margin-top:3px">${esc(p.note)}</div>` : ""}
        </td>
        <td>${geoCell(p)}</td>
        <td>${boundCell(p)}</td>
        <td class="acttd">
          <button class="ghost sm" ${testing ? "disabled" : ""} onclick="testPoolProxy(${p.id})">${testing ? "测试中…" : "测试"}</button>
          <button class="ghost sm" onclick="editPoolProxy(${p.id})">编辑</button>
          <button class="ghost sm" onclick="togglePoolProxy(${p.id},${p.enabled})">${p.enabled ? "停用" : "启用"}</button>
          <button class="ghost sm danger" onclick="delPoolProxy(${p.id},${p.used_by})">${ic("i-trash")}删除</button>
        </td>
      </tr>`;
    }).join("");
  }
  syncProxyBatchBar();
}
async function refreshProxies() {
  const tb = $("proxy-table"); if (!tb) return;
  let rows = [];
  try { rows = await api("/api/proxies"); } catch (e) { return; }
  PROXIES = rows;
  renderProxyPoolView();
}
async function detectProxy() {
  const raw = $("px-url").value.trim();
  if (!raw) { toast("请先填写代理地址", "err"); $("px-url").focus(); return; }
  const btn = $("px-btn-detect") || (typeof event !== "undefined" && event?.target?.closest?.("button"));
  const oldHtml = btn ? btn.innerHTML : "";
  if (btn) { btn.disabled = true; btn.textContent = "检测中…"; }
  showProxyDetectBanner("正在自动探测 HTTP / SOCKS5 协议并查询出口 IP 归属地…");
  try {
    const r = await api("/api/proxies/detect", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url: raw, ...proxyInputOptions() }) });
    if (!r.ok) {
      showProxyDetectBanner("检测未通过：" + (r.error || "连接失败"), "err");
      return;
    }
    if ($("px-proto") && (r.scheme === "http" || r.scheme === "socks5")) $("px-proto").value = r.scheme;
    $("px-url").value = r.recommend;
    if ($("px-format")) $("px-format").value = "auto";
    if (typeof csSyncAll === "function") csSyncAll();
    LAST_DETECT = { url: r.recommend, geo: r.geo || null };
    if (r.geo && $("px-label") && !$("px-label").value.trim()) {
      const existing = new Set(PROXIES.map(x => x.label).filter(Boolean));
      $("px-label").value = suggestGeoLabel(r.geo, existing) || "已验证节点";
    }
    const tag = r.scheme.toUpperCase() + (r.auth === "required" ? " · 含认证" : " · 免密");
    showProxyDetectBanner(`✓ 连通正常（${tag}）${r.geo_text ? " · " + r.geo_text : ""} — 点击「加入代理池」即可保存`, "ok");
    if (!r.browser_ok && r.note) toast("⚠️ " + r.note, "err", 8000);
  } catch (e) {
    showProxyDetectBanner("检测失败：" + e.message, "err");
  } finally {
    if (btn) { btn.disabled = false; btn.innerHTML = oldHtml; }
  }
}
async function addProxy() {
  const url = $("px-url").value.trim();
  if (!url) { toast("请先粘贴代理地址", "err"); $("px-url").focus(); return; }
  const matchedDetect = (LAST_DETECT && LAST_DETECT.url === url) ? LAST_DETECT : null;
  const geo = matchedDetect ? matchedDetect.geo : null;
  const existingLabels = new Set(PROXIES.map(x => x.label).filter(Boolean));
  let label = $("px-label").value.trim();
  if (!label && geo) label = suggestGeoLabel(geo, existingLabels);
  const note = $("px-note") ? $("px-note").value.trim() : "";
  try {
    const created = await api("/api/proxies", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url, label, note, geo, ...proxyInputOptions() }) });
    $("px-url").value = "";
    $("px-label").value = "";
    if ($("px-note")) $("px-note").value = "";
    LAST_DETECT = null;
    showProxyDetectBanner("");
    toast("已加入代理池" + (!geo ? "，正在后台自动检测出口…" : ""), "ok");
    await refreshProxies();
    // 未提前点「检测并识别」时，加入后自动探测一次出口与归属地（若无备注顺手按地区补齐）
    if (created && created.id && !geo) {
      runConcurrentProxyTests([created.id], { autoNameUnnamed: !label, quiet: true });
    }
  } catch (e) { toast("添加失败：" + e.message, "err"); }
}
async function delPoolProxy(id, used) {
  if (!await uiConfirm({ title: "删除代理节点", okText: "确认删除", danger: true,
    message: "确认从代理池删除该节点？" + (used ? `\n⚠️ 当前有 ${used} 个账号正在引用该代理，删除后请及时核对这些账号的代理设置。` : "") })) return;
  try {
    const res = await api("/api/proxies/" + id, { method: "DELETE" });
    SELECTED_PROXY_IDS.delete(id);
    toast("已从代理池删除", "ok");
    if (res && res.warning) setTimeout(() => toast("⚠️ " + res.warning, "info", 6500), 260);
    refreshProxies(); refreshAccounts();
  } catch (e) { toast("删除失败：" + e.message, "err"); }
}
async function editPoolProxy(id) {
  const p = PROXIES.find(x => x.id === id);
  if (!p) return;
  let detectedGeo = null;
  const saved = await new Promise(resolve => {
    _uiResolve = resolve; _uiCancelVal = null;
    const body = $("ui-body");
    const canSuggest = Boolean(p.country || p.city || p.region);
    body.innerHTML = `
      <div class="stack" style="gap:12px">
        <div class="form-field">
          <div style="display:flex;align-items:center;justify-content:space-between;gap:8px;margin-bottom:6px">
            <label for="pxe-label" style="margin:0;color:var(--mut);font-size:12px;font-weight:550">节点备注</label>
            ${canSuggest ? `<button type="button" class="ghost sm" id="pxe-auto-lbl" style="min-height:22px;padding:1px 8px;font-size:11px">按当前归属地生成</button>` : ""}
          </div>
          <input id="pxe-label" value="${esc(p.label || "")}" placeholder="例如：美区静态-01 / 住宅-广东深圳">
        </div>
        <div class="form-field">
          <div style="display:flex;align-items:center;justify-content:space-between;gap:8px;margin-bottom:6px">
            <label for="pxe-url" style="margin:0;color:var(--mut);font-size:12px;font-weight:550">代理地址（修改后会自动同步给已绑定的账号）</label>
            <button type="button" class="ghost sm" id="pxe-toggle-eye" style="min-height:22px;padding:1px 8px;font-size:11px">显示完整账密</button>
          </div>
          <div class="pxp-custom-row">
            <input id="pxe-url" type="password" value="${esc(p.url_full || "")}" placeholder="http://user:pass@host:port 或 socks5://host:port" autocomplete="off" spellcheck="false">
            <button type="button" class="ghost sm" id="pxe-detect">${ic("i-network")}检测连通性</button>
          </div>
          <div id="pxe-detect-msg" class="pxp-detect-msg" style="margin-top:8px">${p.geo_checked ? `当前记录出口：${esc(p.exit_ip || "-")} · ${esc(p.geo_loc || "未知")}` : "尚未记录出口 IP，可点击「检测连通性」核验"}</div>
        </div>
        <div class="form-field">
          <label for="pxe-note">补充说明（可选）</label>
          <input id="pxe-note" value="${esc(p.note || "")}" placeholder="如：独享静态住宅 / 2026-12 到期 / TikTok 主号专用">
        </div>
        <label class="pxp-save-chk" style="padding-top:2px">
          <input type="checkbox" id="pxe-enabled" ${p.enabled !== false ? "checked" : ""}>
          <span>启用该节点（允许参与自动分配与健康调度）</span>
        </label>
      </div>`;

    const urlInp = $("pxe-url");
    const lblInp = $("pxe-label");
    const eyeBtn = $("pxe-toggle-eye");
    const detBtn = $("pxe-detect");
    const detMsg = $("pxe-detect-msg");
    const autoLblBtn = $("pxe-auto-lbl");
    if (autoLblBtn) {
      autoLblBtn.onclick = () => {
        const existing = new Set(PROXIES.filter(x => x.id !== id).map(x => x.label).filter(Boolean));
        lblInp.value = suggestGeoLabel(detectedGeo || p, existing) || lblInp.value;
      };
    }
    if (eyeBtn && urlInp) {
      eyeBtn.onclick = () => {
        const hidden = urlInp.type === "password";
        urlInp.type = hidden ? "text" : "password";
        eyeBtn.textContent = hidden ? "隐藏账密" : "显示完整账密";
      };
    }
    if (detBtn && urlInp && detMsg) {
      detBtn.onclick = async () => {
        const raw = urlInp.value.trim();
        if (!raw) { detMsg.dataset.tone = "err"; detMsg.textContent = "请先填写代理地址"; urlInp.focus(); return; }
        detBtn.disabled = true; const old = detBtn.innerHTML; detBtn.textContent = "检测中…";
        detMsg.removeAttribute("data-tone"); detMsg.textContent = "正在检测协议连通性与出口归属地…";
        try {
          const r = await api("/api/proxies/detect", {
            method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ url: raw }) });
          if (!r.ok) { detMsg.dataset.tone = "err"; detMsg.textContent = "检测未通过：" + (r.error || "连接失败"); return; }
          urlInp.value = r.recommend || raw;
          detectedGeo = r.geo || null;
          if (!lblInp.value.trim() && r.geo) {
            const existing = new Set(PROXIES.filter(x => x.id !== id).map(x => x.label).filter(Boolean));
            lblInp.value = suggestGeoLabel(r.geo, existing);
          }
          detMsg.dataset.tone = "ok";
          detMsg.textContent = `✓ 连通正常（${String(r.scheme || "").toUpperCase()}）${r.geo_text ? " · " + r.geo_text : ""}`;
        } catch (e) {
          detMsg.dataset.tone = "err"; detMsg.textContent = "检测失败：" + e.message;
        } finally { detBtn.disabled = false; detBtn.innerHTML = old; }
      };
    }
    _uiGetVal = () => ({
      label: $("pxe-label").value.trim(),
      url: $("pxe-url").value.trim(),
      note: $("pxe-note").value.trim(),
      enabled: $("pxe-enabled").checked,
    });
    _uiOpen("编辑代理节点", p.used_by ? `当前已关联 ${p.used_by} 个账号，保存后会自动同步最新配置。` : "可同时修改备注、代理地址、账密或启用状态。", {
      okText: "保存修改",
      wide: true,
      icon: "i-edit",
      submit: async val => {
        if (!val.url) uiEditorError("代理地址不能为空", "pxe-url");
        await api("/api/proxies/" + id, {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ ...val, geo: detectedGeo }),
        });
      },
    });
  });
  if (!saved) return;
  toast("代理节点已更新", "ok");
  refreshProxies();
  refreshAccounts();
  window.dispatchEvent(new Event("creatorhub:matrix:refresh"));
}
async function togglePoolProxy(id, enabled) {
  try {
    await api("/api/proxies/" + id, {
      method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled: !enabled }) });
    refreshProxies(); refreshAccounts();
  } catch (e) { toast("操作失败：" + e.message, "err"); }
}
async function runConcurrentProxyTests(ids, { autoNameUnnamed = false, quiet = false, triggerBtn = null } = {}) {
  const targetIds = [...new Set(ids.map(Number).filter(Boolean))];
  if (!targetIds.length) return { okCount: 0, failCount: 0 };
  targetIds.forEach(id => PROXY_TESTING_IDS.add(id));
  renderProxyPoolView();
  const oldHtml = triggerBtn ? triggerBtn.innerHTML : "";
  if (triggerBtn) triggerBtn.disabled = true;
  let done = 0, okCount = 0, failCount = 0;
  const syncProgress = () => {
    if (triggerBtn) triggerBtn.textContent = `测试中 ${done}/${targetIds.length}…`;
  };
  syncProgress();
  const queue = [...targetIds];
  const existingLabels = new Set(PROXIES.map(x => x.label).filter(Boolean));
  const worker = async () => {
    while (queue.length) {
      const pid = queue.shift();
      try {
        const r = await api("/api/proxies/" + pid + "/test", { method: "POST" });
        if (r.ok) okCount++; else failCount++;
        const row = PROXIES.find(x => x.id === pid);
        if (row) {
          row.status = r.ok ? "ok" : "bad";
          if (r.geo) {
            row.exit_ip = r.geo.ip || row.exit_ip || "";
            row.country = r.geo.country || row.country || "";
            row.region = r.geo.region || row.region || "";
            row.city = r.geo.city || row.city || "";
            row.isp = r.geo.isp || row.isp || "";
            row.geo_loc = [row.country, row.region, row.city].filter(Boolean).join(" · ");
            row.geo_checked = Boolean(row.exit_ip || row.country);
            if (typeof isMainlandGeoClient === "function") {
              row.is_mainland = isMainlandGeoClient(row.country, row.region, row.city);
            }
            if (autoNameUnnamed && !row.label) {
              const newLabel = suggestGeoLabel(r.geo, existingLabels);
              if (newLabel) {
                existingLabels.add(newLabel);
                row.label = newLabel;
                await api("/api/proxies/" + pid, {
                  method: "PUT", headers: { "Content-Type": "application/json" },
                  body: JSON.stringify({ label: newLabel }),
                }).catch(() => {});
              }
            }
          }
        }
      } catch (e) {
        failCount++;
      } finally {
        done++;
        PROXY_TESTING_IDS.delete(pid);
        syncProgress();
        renderProxyPoolView();
      }
    }
  };
  await Promise.all(Array.from({ length: Math.min(3, targetIds.length) }, () => worker()));
  if (triggerBtn) { triggerBtn.disabled = false; triggerBtn.innerHTML = oldHtml; }
  await refreshProxies();
  if (!quiet) {
    toast(`检测完成：可连接 ${okCount} 个${failCount ? ` · 未通过 ${failCount} 个` : ""}`, failCount && !okCount ? "err" : "ok");
  }
  return { okCount, failCount };
}
async function testPoolProxy(id) {
  if (PROXY_TESTING_IDS.has(id)) return;
  PROXY_TESTING_IDS.add(id);
  renderProxyPoolView();
  try {
    const r = await api("/api/proxies/" + id + "/test", { method: "POST" });
    toast((r.ok ? "连接可用 ✓ " : "检测未通过 ✗ ") + (r.geo_text || r.detail || ""), r.ok ? "ok" : "err");
  } catch (e) {
    toast("测试失败：" + e.message, "err");
  } finally {
    PROXY_TESTING_IDS.delete(id);
    refreshProxies();
  }
}
async function testAllProxies() {
  if (!PROXIES.length) { toast("代理池为空，请先添加节点", "info"); return; }
  const targets = filteredPoolProxies();
  const list = targets.length ? targets : PROXIES;
  await runConcurrentProxyTests(list.map(p => p.id), { triggerBtn: $("px-btn-test-all") });
}
async function batchTestPoolProxies() {
  const ids = [...SELECTED_PROXY_IDS];
  if (!ids.length) return;
  await runConcurrentProxyTests(ids, { triggerBtn: typeof event !== "undefined" ? event?.target?.closest?.("button") : null });
}
async function batchAutoNameProxies() {
  const targets = PROXIES.filter(p => SELECTED_PROXY_IDS.has(p.id) && !p.label && p.geo_checked);
  if (!targets.length) {
    toast("所选节点均已有备注，或尚未检测出归属地（可先执行批量测试）", "info");
    return;
  }
  const usedLabels = new Set(PROXIES.map(x => x.label).filter(Boolean));
  let updated = 0;
  for (const p of targets) {
    const nextLbl = suggestGeoLabel(p, usedLabels);
    if (!nextLbl) continue;
    usedLabels.add(nextLbl);
    try {
      await api("/api/proxies/" + p.id, {
        method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ label: nextLbl }) });
      updated++;
    } catch (e) {}
  }
  toast(`已按归属地自动命名 ${updated} 个节点`, "ok");
  refreshProxies();
}
async function batchTogglePoolProxies(enabled) {
  const ids = [...SELECTED_PROXY_IDS];
  if (!ids.length) return;
  let count = 0;
  for (const id of ids) {
    try {
      await api("/api/proxies/" + id, {
        method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ enabled: !!enabled }) });
      count++;
    } catch (e) {}
  }
  toast(`已批量${enabled ? "启用" : "停用"} ${count} 个节点`, "ok");
  refreshProxies(); refreshAccounts();
}
async function batchDeletePoolProxies() {
  const rows = PROXIES.filter(p => SELECTED_PROXY_IDS.has(p.id));
  if (!rows.length) return;
  const boundTotal = rows.reduce((s, p) => s + Number(p.used_by || 0), 0);
  if (!await uiConfirm({
    title: `批量删除代理（共 ${rows.length} 个）`,
    okText: "确认批量删除",
    danger: true,
    message: `确定从代理池删除已选的 ${rows.length} 个节点？` + (boundTotal ? `\n⚠️ 其中关联了 ${boundTotal} 个账号引用，删除后请核对相关账号代理设置。` : ""),
  })) return;
  let deleted = 0;
  for (const p of rows) {
    try {
      await api("/api/proxies/" + p.id, { method: "DELETE" });
      SELECTED_PROXY_IDS.delete(p.id);
      deleted++;
    } catch (e) {}
  }
  toast(`已删除 ${deleted} 个代理节点`, "ok");
  refreshProxies(); refreshAccounts();
}
async function importProxies() {
  const defaults = proxyInputOptions();
  let importRes = null;
  const result = await new Promise(resolve => {
    _uiResolve = resolve; _uiCancelVal = null;
    const body = $("ui-body");
    body.innerHTML = `
      <div class="stack" style="gap:12px">
        <div class="form-grid" style="grid-template-columns:1fr 1.3fr;gap:10px">
          <div class="form-field">
            <label for="pxi-proto">未带协议时默认按</label>
            <select id="pxi-proto">
              <option value="http"${defaults.default_scheme === "http" ? " selected" : ""}>HTTP</option>
              <option value="socks5"${defaults.default_scheme === "socks5" ? " selected" : ""}>SOCKS5</option>
            </select>
          </div>
          <div class="form-field">
            <label for="pxi-format">账密分隔格式</label>
            <select id="pxi-format">
              <option value="auto"${defaults.input_format === "auto" ? " selected" : ""}>自动识别（推荐）</option>
              <option value="host_colon">主机:端口:用户名:密码</option>
              <option value="host_csv">主机,端口,用户名,密码</option>
              <option value="host_at">主机:端口@用户名:密码</option>
              <option value="auth_colon">用户名:密码:主机:端口</option>
              <option value="auth_at">用户名:密码@主机:端口</option>
              <option value="host_hash">主机:端口##用户名##密码</option>
            </select>
          </div>
        </div>
        <div class="form-field">
          <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:6px">
            <label for="pxi-text" style="margin:0;color:var(--mut);font-size:12px;font-weight:550">粘贴代理列表（每行一条，可写「备注 | 地址」）</label>
            <span class="mut" id="pxi-count" style="font-size:11.5px">待粘贴</span>
          </div>
          <textarea id="pxi-text" rows="7" spellcheck="false" style="font-family:var(--mono);font-size:12px" placeholder="美区静态-01 | socks5://user:pass@us.example.com:1080&#10;proxy.example.com:8080:USER:TOKEN&#10;proxy.example.com,8080,USER,TOKEN"></textarea>
        </div>
        <label class="pxp-save-chk">
          <input type="checkbox" id="pxi-autotest" checked>
          <span>导入后自动并发检测连通性与归属地（并为空备注节点自动按地区命名）</span>
        </label>
      </div>`;
    enhanceSelect($("pxi-proto"));
    enhanceSelect($("pxi-format"));
    const txt = $("pxi-text"), cnt = $("pxi-count");
    txt.addEventListener("input", () => {
      const n = txt.value.split(/\r?\n/).map(l => l.trim()).filter(l => l && !l.startsWith("#")).length;
      cnt.textContent = n ? `已识别 ${n} 行待导入` : "待粘贴";
    });
    _uiGetVal = () => ({
      text: txt.value,
      default_scheme: $("pxi-proto").value,
      input_format: $("pxi-format").value,
      autoTest: $("pxi-autotest").checked,
    });
    _uiOpen("批量导入代理", "自动校验格式并过滤池内重复节点；已带 http:// 或 socks5:// 前缀的行会保留原协议。", {
      okText: "开始导入",
      wide: true,
      icon: "i-plus",
      busyText: "导入中",
      noticeText: "",
      submit: async payload => {
        if (!payload.text.trim()) uiEditorError("请先粘贴至少一条代理记录", "pxi-text");
        importRes = await api("/api/proxies/import", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            text: payload.text,
            default_scheme: payload.default_scheme,
            input_format: payload.input_format,
          }),
        });
      },
    });
  });
  if (!result || !importRes) return;
  let msg = `导入完成：新增 ${importRes.added} 条`;
  if (importRes.skipped) msg += ` · 重复跳过 ${importRes.skipped} 条`;
  if (importRes.invalid) msg += ` · 格式无效 ${importRes.invalid} 条`;
  toast(msg, importRes.added ? "ok" : "info");
  if (importRes.invalid_samples?.length) toast(importRes.invalid_samples.join("\n"), "err", 10000);
  await refreshProxies();
  if (result.autoTest && Array.isArray(importRes.added_ids) && importRes.added_ids.length) {
    runConcurrentProxyTests(importRes.added_ids, { autoNameUnnamed: true, triggerBtn: $("px-btn-test-all") });
  }
}
async function assignAllProxies() {
  const poolTargets = (ALL_ACCOUNTS && ALL_ACCOUNTS.length) ? ALL_ACCOUNTS : ACCOUNTS;
  const noProxy = poolTargets.filter(a => !a.has_proxy).length;
  if (!noProxy) { toast("所有账号都已配置代理", "info"); return; }
  if (!await uiConfirm({ title: "批量分配代理", message: `将为 ${noProxy} 个尚未配置代理的账号从池内自动分配（优先空闲独立且地区匹配的节点，池余量不足时按最少占用复用）。是否继续？` })) return;
  const btn = typeof event !== "undefined" ? event?.target?.closest?.("button") : null;
  const oldHtml = btn ? btn.innerHTML : "";
  if (btn) { btn.disabled = true; btn.textContent = "分配中…"; }
  try {
    const r = await api("/api/accounts/assign-proxies-all", { method: "POST" });
    let msg = `已为 ${r.assigned} 个账号分配代理`;
    if (r.unassigned) msg += `，仍有 ${r.unassigned} 个未分到`;
    toast(msg, r.unassigned ? "info" : "ok");
    if (r.warning) setTimeout(() => toast("⚠️ " + r.warning, "info", 7000), 280);
    refreshAccounts(); refreshProxies();
    window.dispatchEvent(new Event("creatorhub:matrix:refresh"));
  } catch (e) { toast("分配失败：" + e.message, "err"); }
  finally { if (btn) { btn.disabled = false; btn.innerHTML = oldHtml; } }
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
          const pxWarn = r.warning || r.environment?.proxy_warning || "";
          if (pxWarn) setTimeout(() => toast("⚠️ " + pxWarn, "info", 6500), 320);
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
  const a = findAnyAccount(id);
  const warn = a && a.monitor_count > 0 ? `\n关联的 ${a.monitor_count} 个作品监控也会暂停。` : "";
  if (!await uiConfirm({ title: "删除账号", message: "删除该账号将取消未执行任务、停用关联规则并清理独占登录环境；历史内容保留。执行中的操作需要先结束。" + warn, okText: "删除并停止关联任务", danger: true })) return;
  try {
    const result = await api("/api/accounts/" + id, { method: "DELETE" });
    toast(`账号已删除，取消 ${result.canceled_tasks || 0} 个任务，暂停 ${(result.disabled_rules || 0) + (result.disabled_monitors || 0)} 项规则/监控`, "ok", 6000);
    if (result.profile_cleanup_error) toast(result.profile_cleanup_error, "info", 7000);
    refreshAccounts(); refreshMonitors(); refreshCommentRules(); refreshCommentTasks(); refreshPublish();
  }
  catch (e) { toast("删除失败:" + e.message, "err"); }
}

// ─── API 兼容矩阵 / 多账号环境隔离 ───
let TRANSPORT_MATRIX = null;
function transportModeLabel(mode) {
  return ({ api: "API 直连", browser: "浏览器", hybrid: "API → 浏览器回退",
    auto: "原生协议优先", protocol: "仅原生协议",
    manual: "人工草稿", unavailable: "当前不可用", deferred: "已暂缓" })[mode] || mode || "未知";
}
function transportModeTag(mode) {
  const safe = ["api", "browser", "hybrid", "manual", "unavailable", "deferred"].includes(mode) ? mode : "manual";
  return `<span class="transport-mode ${safe}">${esc(transportModeLabel(mode))}</span>`;
}
function transportSupportTag(enabled, label) {
  return enabled
    ? `<span class="pill done bare">${esc(label)}可用</span>`
    : `<span class="pill skipped bare">${esc(label)}—</span>`;
}
function transportRoute(platform, operation) {
  return TRANSPORT_MATRIX?.rows?.find(row => row.platform === platform && row.operation === operation) || null;
}
function transportSourceSuffix(source) {
  const labels = { api: "API 直连", web_api: "API 直连", browser: "浏览器",
    browser_fallback: "浏览器回退", api_browser_context: "浏览器上下文 API" };
  return labels[source] ? ` · ${labels[source]}` : "";
}
function renderTransportMatrix(data) {
  const body = $("transport-matrix-body"), isolation = $("transport-isolation-body");
  if (!body || !isolation) return;
  const platformNames = { douyin: "抖音", xhs: "小红书", rednote: "海外小红书", kuaishou: "快手", shipinhao: "视频号", tiktok: "TikTok", bilibili: "哔哩哔哩", instagram: "Instagram", twitter: "Twitter" };
  const rows = Array.isArray(data?.rows) ? data.rows : [];
  body.innerHTML = rows.length ? rows.map(row => `<tr>
    <td><b>${esc(platformNames[row.platform] || row.platform)}</b></td>
    <td>${esc(row.label || row.operation)}</td>
    <td>${transportSupportTag(!!row.api, "API")}</td>
    <td>${transportSupportTag(!!row.browser, "浏览器")}</td>
    <td>${transportModeTag(row.effective_mode)}${row.configured_mode !== row.effective_mode ? `<div class="isolation-note">配置：${esc(transportModeLabel(row.configured_mode))}</div>` : ""}</td>
    <td>${esc(row.reason || row.note || (row.fallback === "browser_on_confirmed_failure" ? "明确失败时回退；不确定写结果不重试" : "—"))}${row.platform === "shipinhao" && row.requires_channels_cdp ? `<div class="isolation-note"><a class="channels-inline-jump" href="#channels-protocol-card" onclick="event.preventDefault();openChannelsProtocolSettings()">${data?.settings?.channels_cdp_ready ? "管理本机微信通道 →" : "去配置本机微信通道 →"}</a></div>` : ""}</td>
  </tr>`).join("") : empty(6, "尚无兼容矩阵", "i-info");
  const accounts = Array.isArray(data?.accounts) ? data.accounts : [];
  isolation.innerHTML = accounts.length ? accounts.map(account => {
    const badge = (ok, yes, no) => `<span class="pill ${ok ? "done" : "pending"} bare">${esc(ok ? yes : no)}</span>`;
    return `<tr>
      <td><b>${esc(account.nickname || `账号 #${account.account_id}`)}</b><div class="isolation-note">${esc(platformNames[account.platform] || account.platform)} · #${Number(account.account_id) || "-"}</div></td>
      <td>${badge(account.profile_isolated, "独立", "缺失/重复")}</td>
      <td><div class="isolation-stack">${badge(account.credential_isolated, "登录态独立", "登录态待检查")}${badge(account.api_session_isolated, "会话独立", "会话共享")}${badge(account.api_environment_aligned, "参数对齐", "参数未对齐")}</div></td>
      <td>${badge(account.network_isolated, "专属代理", account.network_scope || "共享出口")}</td>
      <td><code>${esc(account.environment_id || "-")}</code>${account.warnings?.length ? `<div class="isolation-note">${account.warnings.map(esc).join("；")}</div>` : ""}</td>
    </tr>`;
  }).join("") : empty(5, "尚未添加账号", "i-user");
}
async function refreshTransportMatrix() {
  const status = $("transport-matrix-status");
  try {
    const data = await api("/api/settings/transport-matrix");
    TRANSPORT_MATRIX = data;
    renderTransportMatrix(data);
    if (status) status.textContent = `已核对 ${data.rows?.length || 0} 项能力、${data.accounts?.length || 0} 个账号环境`;
    return data;
  } catch (error) {
    if (status) status.textContent = "兼容矩阵读取失败：" + error.message;
    return null;
  }
}
globalThis.CreatorHubTransportMatrix = { load: refreshTransportMatrix, route: transportRoute };

// ─── 下载设置 ───
async function loadSettings() {
  globalThis.CreatorHubEngineSettings?.load();
  globalThis.CreatorHubChannelsProtocol?.load?.();
  globalThis.CreatorHubDiagnostics?.load?.();
  refreshTransportMatrix();
  try {
    const s = await api("/api/settings");
    const assign = (id, property, value) => {
      if (!globalThis.CreatorHubWorkbench?.isDirty?.(id)) $(id)[property] = value;
    };
    assign("dl-dir", "value", s.download_dir || "");
    assign("dl-quality", "value", s.video_quality || "highest");
    if ($("ai-enabled")) {
      assign("ai-enabled", "checked", !!s.ai_enabled);
      assign("ai-base", "value", s.ai_base_url || "");
      assign("ai-model", "value", s.ai_model || "");
      assign("ai-temp", "value", s.ai_temperature || "0.9");
      assign("ai-prompt", "value", s.ai_prompt || "");
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
  await withBusy(evtBtn(), "保存中", async () => {
  try {
    const s = await api("/api/settings", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    $("ai-key").value = ""; $("ai-key").placeholder = s.ai_api_key_set ? "已保存(留空=不修改)" : "API Key";
    $("ai-msg").textContent = "已保存 ✓ " + (s.ai_enabled ? "(规则勾选「用 AI」即生效)" : "(当前未启用)");
    toast("AI 设置已保存", "ok");
  } catch (e) { $("ai-msg").textContent = "失败: " + e.message; toast("保存失败:" + e.message, "err"); }
  });
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
  await withBusy(evtBtn(), "保存中", async () => {
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
  });
}

// 10-channels-bridge.js — 微信视频号本地通道引导、系统代理与证书安装及桌面微信控制。
// ─── 视频号本机微信通道可视化配置 ───
function openChannelsProtocolSettings() {
  switchTab("settings");
  globalThis.CreatorHubWorkbench?.showSection?.("settings", "channels");
  globalThis.CreatorHubChannelsProtocol?.load?.();
}

const CreatorHubChannelsProtocol = (() => {
  const dirtyIds = [
    "ch-proto-read-mode",
    "ch-proto-mode",
    "ch-proto-proxy-port",
    "ch-proto-endpoint",
    "ch-proto-endpoint-file",
  ];
  const _STRIP_QUERY_KEYS = new Set([
    "exportkey", "pass_ticket", "wx_header", "uin", "key",
    "ascene", "devicetype", "version", "nettype", "abtest_cookie", "lang", "fontgear",
  ]);
  let lastStatus = null;
  let pollTimer = null;
  let pollInFlight = false;
  let prevReadyPages = 0;

  function cleanFinderUrl(rawUrl) {
    const text = String(rawUrl || "").trim();
    const qIdx = text.indexOf("?");
    if (qIdx === -1) return text.split("#")[0];
    const base = text.slice(0, qIdx);
    const queryRaw = text.slice(qIdx + 1).split("#")[0];
    const kept = queryRaw.split("&").filter(part => {
      if (!part) return false;
      const eq = part.indexOf("=");
      const k = (eq === -1 ? part : part.slice(0, eq)).trim().toLowerCase();
      return k && !_STRIP_QUERY_KEYS.has(k);
    });
    return kept.length ? `${base}?${kept.join("&")}` : base;
  }

  function stopAutoPoll() {
    if (pollTimer !== null && typeof clearTimeout === "function") {
      clearTimeout(pollTimer);
    }
    pollTimer = null;
  }

  function isCardVisibleForPoll() {
    if (typeof document === "undefined" || document.visibilityState === "hidden") return false;
    const card = $("channels-protocol-card");
    if (!card || card.hidden) return false;
    if (typeof card.closest === "function" && card.closest("[hidden]")) return false;
    return true;
  }

  async function pollOnce() {
    pollTimer = null;
    if (pollInFlight) return;
    if (!isCardVisibleForPoll()) {
      if (lastStatus?.proxy?.running && Number(lastStatus?.proxy?.ready_pages || 0) === 0 && typeof setTimeout === "function" && typeof document !== "undefined") {
        pollTimer = setTimeout(pollOnce, 3000);
      }
      return;
    }
    pollInFlight = true;
    try {
      const data = await api("/api/channels/protocol/status");
      const wasWaiting = prevReadyPages === 0;
      const nowReady = Number(data?.proxy?.ready_pages || 0);
      applyConfigToForm(data, false);
      renderStatus(data);
      if (wasWaiting && nowReady > 0) {
        setMsg("已自动检测到微信桥接页面接入 ✓ 现在可直接使用搜索、主页采集、二级评论与视频解密", "success");
        toast("微信视频号通道已自动连通 ✓", "ok");
        globalThis.CreatorHubTransportMatrix?.load?.();
      }
    } catch {
      // Ignore transient network hiccups during background poll
    } finally {
      pollInFlight = false;
    }
  }

  function syncAutoPoll(proxy) {
    const running = !!proxy?.running;
    const readyPages = Number(proxy?.ready_pages || 0);
    prevReadyPages = readyPages;
    if (!running || readyPages > 0) {
      stopAutoPoll();
      return;
    }
    if (pollTimer === null && typeof setTimeout === "function" && typeof document !== "undefined") {
      pollTimer = setTimeout(pollOnce, 1800);
    }
  }

  function setMsg(text, state = "") {
    const el = $("ch-proto-msg");
    if (!el) return;
    el.textContent = text;
    el.dataset.state = state;
  }

  function clearDirtyFlags() {
    for (const id of dirtyIds) {
      const el = $(id);
      if (el) el.dataset.wbEdited = "false";
    }
  }

  function onModeChange() {
    const mode = $("ch-proto-mode")?.value || "none";
    const proxyWrap = $("ch-proto-proxy-wrap");
    const epWrap = $("ch-proto-endpoint-wrap");
    const fileWrap = $("ch-proto-file-wrap");
    if (proxyWrap) proxyWrap.hidden = mode !== "proxy";
    if (epWrap) epWrap.hidden = mode !== "endpoint";
    if (fileWrap) fileWrap.hidden = mode !== "endpoint_file";
    const proxyPanel = $("channels-proxy-panel");
    const bridgePanel = $("channels-bridge-panel");
    const advDetails = $("channels-advanced-details");
    if (mode === "proxy" && proxyPanel) proxyPanel.open = true;
    if (mode === "endpoint_file" && bridgePanel) bridgePanel.open = true;
    if ((mode === "endpoint" || mode === "endpoint_file") && advDetails) advDetails.open = true;
  }

  function renderCapabilities(caps, readMode) {
    const box = $("ch-proto-caps");
    if (!box) return;
    const c = caps || {};
    const items = [
      ["account_works", "本账号作品同步"],
      ["keyword_search", "关键词搜索"],
      ["creator_works", "任意创作者主页"],
      ["full_comment_threads", "全量二级评论"],
      ["encrypted_video_decode", "WxIsaac64 视频解密"],
      ["share_preview", "公开分享预览"],
    ];
    box.innerHTML = items.map(([key, label]) => {
      const ok = !!c[key];
      const cls = ok ? "done" : "skipped";
      const suffix = !ok && readMode === "browser" && ["keyword_search", "creator_works", "full_comment_threads"].includes(key)
        ? " · 浏览器模式禁用"
        : "";
      return `<span class="pill ${cls} bare">${esc(label)}${esc(suffix)}</span>`;
    }).join("");
  }

  function renderProbeBox(probe) {
    const box = $("ch-proto-probe-box");
    if (!box) return;
    if (!probe) {
      box.hidden = true;
      box.innerHTML = "";
      box.dataset.state = "";
      return;
    }
    box.hidden = false;
    if (probe.ok && probe.ready) {
      box.dataset.state = "ready";
      if (probe.transport === "channels-local-proxy") {
        const rawCaps = probe.capabilities;
        const capList = Array.isArray(rawCaps)
          ? rawCaps.map(x => String(x || "").trim()).filter(Boolean)
          : Object.entries(rawCaps || {}).filter(([, v]) => !!v).map(([k]) => k);
        const cleanProbeUrl = cleanFinderUrl(probe.url || "https://channels.weixin.qq.com/web/pages/");
        box.innerHTML = `<b>握手成功 · 内置微信视频号代理已就绪</b>`
          + `<div>页面：<code>${esc(cleanProbeUrl)}</code>`
          + (probe.title ? ` · 标题：<b>${esc(probe.title)}</b>` : "")
          + ` · 已连接页面数：<b>${Number(probe.connected_pages || 1)}</b></div>`
          + (capList.length ? `<div>已挂载服务：<code>${esc(capList.join(" / "))}</code></div>` : "");
        return;
      }
      const env = probe.environment || {};
      const methods = Array.isArray(probe.service?.capabilities) ? probe.service.capabilities : [];
      box.innerHTML = `<b>握手成功 · 微信原生视频号页面就绪</b>`
        + `<div>页面：<code>${esc((env.host || "channels.weixin.qq.com") + (env.path || ""))}</code>`
        + ` · WeixinJSBridge：<b>${env.nativeBridge ? "可用" : "缺失"}</b>`
        + ` · 微信 UA：<b>${env.wechatUA ? "匹配" : "不匹配"}</b></div>`
        + (methods.length ? `<div>已挂载服务：<code>${esc(methods.join(" / "))}</code></div>` : "");
    } else {
      box.dataset.state = "error";
      box.innerHTML = `<b>通道未就绪</b><div>${esc(probe.error || "无法连接本地微信 Finder 通道")}</div>`;
    }
  }

  function renderProcesses(procs) {
    const box = $("ch-bridge-procs");
    if (!box) return;
    const list = Array.isArray(procs) ? procs : [];
    if (!list.length) {
      box.innerHTML = '<span class="field-help" style="margin:0">当前未检测到运行中的桌面微信（Weixin.exe / WeChat.exe / WeChatAppEx.exe）进程。</span>';
      return;
    }
    const preview = list.slice(0, 12).map(p =>
      `<span class="pill bare">${esc(p.name)} · PID ${Number(p.pid)}</span>`
    ).join("");
    const extra = list.length > 12 ? `<span class="field-help" style="margin:0">等共 ${list.length} 个微信相关进程</span>` : "";
    box.innerHTML = preview + extra;
  }

  function renderProxy(proxy) {
    if (!proxy) return;
    const badge = $("ch-proxy-badge");
    const statusText = $("ch-proxy-status-text");
    const startBtn = $("ch-proxy-start-btn");
    const stopBtn = $("ch-proxy-stop-btn");
    const caBadge = $("ch-proxy-ca-badge");
    const caText = $("ch-proxy-ca-text");
    const caInstallBtn = $("ch-proxy-ca-install-btn");
    const caUninstallBtn = $("ch-proxy-ca-uninstall-btn");
    const pagesBox = $("ch-proxy-pages");

    const running = !!proxy.running;
    const readyPages = Number(proxy.ready_pages || 0);
    const connectedPages = Number(proxy.connected_pages || 0);
    const ca = proxy.ca || {};
    const caTrusted = !!ca.trusted;

    if (badge) {
      if (!running) {
        badge.className = "pill skipped";
        badge.textContent = "未启动";
      } else if (readyPages > 0) {
        badge.className = "pill done";
        badge.textContent = "运行中 · 页面就绪";
      } else if (connectedPages > 0) {
        badge.className = "pill pending";
        badge.textContent = "运行中 · 加载中";
      } else {
        badge.className = "pill pending";
        badge.textContent = "运行中 · 待开微信";
      }
    }
    if (statusText) {
      if (running) {
        const sysProxy = proxy.system_proxy || {};
        const sysLabel = proxy.system_proxy_managed && sysProxy.proxy_enable
          ? `WinINet 系统代理已指向 ${sysProxy.proxy_server || ("127.0.0.1:" + (proxy.proxy_port || 2023))}`
          : "系统代理未托管（手动模式）";
        const upProxy = proxy.upstream_proxy || {};
        const upLabel = upProxy.enabled && upProxy.server
          ? ` · 链式上游 ${upProxy.server}`
          : "";
        const connLabel = readyPages > 0
          ? `已就绪 ${readyPages} 个视频号页面`
          : (connectedPages > 0 ? `已接入 ${connectedPages} 页，等待模块加载` : "请在微信内打开桥接链接或视频号网页");
        statusText.textContent = `监听 127.0.0.1:${proxy.proxy_port || 2023} · ${sysLabel}${upLabel} · ${connLabel} · 已转发 ${Number(proxy.commands_forwarded || 0)} 条请求`;
      } else if (proxy.system_proxy?.low_integrity) {
        statusText.textContent = "检测到后端处于 Windows 低完整性级别（Low IL），无法自动改写系统代理；建议用外部 Python 启动 uvicorn，或取消勾选下方开关后手动将系统代理指向 127.0.0.1:" + (proxy.proxy_port || 2023) + "。";
      } else {
        statusText.textContent = "仅拦截 channels.weixin.qq.com，其余网站流量原样加密直通（自动链式复用原本地代理），停止或退出软件时自动恢复系统代理。";
      }
    }
    if (startBtn) {
      startBtn.disabled = running;
      if (!running) {
        startBtn.innerHTML = '<svg aria-hidden="true"><use href="#i-play"/></svg>一键连接本机微信';
      } else if (readyPages > 0) {
        startBtn.innerHTML = '<svg aria-hidden="true"><use href="#i-check"/></svg>已连通（重启自动恢复）';
      } else {
        startBtn.innerHTML = '<svg aria-hidden="true"><use href="#i-refresh"/></svg>正在自动等待微信接入…';
      }
    }
    if (stopBtn) {
      stopBtn.disabled = !running;
      stopBtn.hidden = !running;
    }

    if (caBadge) {
      if (caTrusted) {
        caBadge.className = "pill done";
        caBadge.textContent = "根证书已受信任";
      } else if (ca.exists) {
        caBadge.className = "pill pending";
        caBadge.textContent = "证书已生成 · 未信任";
      } else {
        caBadge.className = "pill skipped";
        caBadge.textContent = "根证书未安装";
      }
    }
    if (caText) {
      if (caTrusted) {
        caText.textContent = `已安装到 ${ca.store || "CurrentUser\\Root"}（指纹 ${String(ca.thumbprint || "").slice(0, 12)}…）。`;
      } else {
        caText.textContent = "点击上方「一键连接本机微信」会自动安装本机根证书；安装时请在 Windows 安全弹窗点击「是」。";
      }
    }
    if (caInstallBtn) caInstallBtn.disabled = caTrusted;
    if (caUninstallBtn) caUninstallBtn.disabled = !caTrusted;

    if (pagesBox) {
      const pages = Array.isArray(proxy.pages) ? proxy.pages : [];
      const bridgeUrl = String(proxy.bridge_entry_url || "https://channels.weixin.qq.com/platform/bridge");
      if (!running && !pages.length) {
        pagesBox.hidden = true;
        pagesBox.innerHTML = "";
      } else if (!pages.length) {
        pagesBox.hidden = false;
        if (!caTrusted && proxy.system_proxy_managed) {
          pagesBox.innerHTML = `<div class="channels-proxy-page-row" data-guide="waiting"><div class="channels-proxy-page-copy"><span class="field-help" style="margin:0"><b>需要信任本机证书</b>：点击右侧按钮并在系统弹窗点击「是」，即可让微信信任本地视频号桥接。</span></div><div class="channels-proxy-page-meta"><button type="button" class="sm" onclick="CreatorHubChannelsProtocol.installProxyCert()"><svg aria-hidden="true"><use href="#i-shield"/></svg>安装根证书</button></div></div>`;
        } else if (proxy.wechat_needs_restart) {
          const tipHtml = `<b>开启前微信已在运行 · 按顺序点右侧两步即可</b>：先点「<b>① 一键重开微信</b>」让微信加载新代理，登录后点「<b>② 复制桥接链接并打开微信</b>」发到「文件传输助手」点开一次（点开即自动变绿，以后重启自动恢复）。`;
          pagesBox.innerHTML = `<div class="channels-proxy-page-row" data-guide="waiting"><div class="channels-proxy-page-copy"><span class="field-help" style="margin:0">${tipHtml}</span></div><div class="channels-proxy-page-meta"><button type="button" class="ghost sm" onclick="CreatorHubChannelsProtocol.restartWechat()">① 一键重开微信</button><button type="button" class="sm" onclick="CreatorHubChannelsProtocol.copyAndOpenWechat(${jsArg(bridgeUrl)})"><svg aria-hidden="true"><use href="#i-copy"/></svg>② 复制桥接链接并打开微信</button></div></div>`;
        } else {
          const tipHtml = proxy.liteapp_detected
            ? `<b>已检测到微信 4.2+ LiteApp 流量 · 最后一步</b>：点击右侧按钮自动复制 <code>${esc(bridgeUrl)}</code> 并唤起微信，粘贴到「文件传输助手」发送并点开一次即可（点开后本页自动变绿，保持窗口最小化即可）。`
            : `<b>最后一步：在电脑微信点开一次桥接链接</b>：点击右侧按钮自动复制 <code>${esc(bridgeUrl)}</code> 并唤起微信，粘贴到「文件传输助手」发送并点开一次即可（点开后本页自动变绿；保留该链接下次点一下即可连）。`;
          const restartBtn = proxy.wechat_running
            ? `<button type="button" class="ghost sm" onclick="CreatorHubChannelsProtocol.restartWechat()">重开微信</button>`
            : "";
          pagesBox.innerHTML = `<div class="channels-proxy-page-row" data-guide="waiting"><div class="channels-proxy-page-copy"><span class="field-help" style="margin:0">${tipHtml}</span></div><div class="channels-proxy-page-meta"><button type="button" class="sm" onclick="CreatorHubChannelsProtocol.copyAndOpenWechat(${jsArg(bridgeUrl)})"><svg aria-hidden="true"><use href="#i-copy"/></svg>复制桥接链接并打开微信</button>${restartBtn}<button type="button" class="ghost sm" onclick="CreatorHubChannelsProtocol.copyPageUrl(${jsArg(bridgeUrl)})">仅复制链接</button></div></div>`;
        }
      } else {
        pagesBox.hidden = false;
        pagesBox.innerHTML = pages.map(p => {
          const cleanUrl = cleanFinderUrl(p.url);
          const pill = p.ready
            ? '<span class="pill done bare">协议就绪 · 自动重连</span>'
            : `<span class="pill pending bare">${esc(p.reason || "等待加载")}</span>`;
          const copyBtn = cleanUrl
            ? `<button type="button" class="ghost sm" onclick="CreatorHubChannelsProtocol.copyPageUrl(${jsArg(cleanUrl)})">复制链接</button>`
            : "";
          return `<div class="channels-proxy-page-row" data-guide="${p.ready ? "ready" : "waiting"}"><div class="channels-proxy-page-copy"><b>${esc(p.title || "微信视频号页面")}</b> <code>${esc(cleanUrl)}</code></div><div class="channels-proxy-page-meta">${copyBtn}${pill}</div></div>`;
        }).join("");
      }
    }
  }

  function renderBridge(bridge) {
    if (!bridge) return;
    const badge = $("ch-bridge-badge");
    const statusText = $("ch-bridge-status-text");
    const startBtn = $("ch-bridge-start-btn");
    const stopBtn = $("ch-bridge-stop-btn");
    const running = !!bridge.running;
    if (badge) {
      badge.className = `pill ${running ? "done" : "skipped"}`;
      badge.textContent = running ? "运行中" : "未启动";
    }
    if (statusText) {
      if (running) {
        const nativeState = bridge.native_connected ? "微信已接入" : "等待微信接入";
        statusText.textContent = `监听 127.0.0.1:${bridge.debug_listening_port || "-"} → CDP :${bridge.cdp_listening_port || "-"} · ${nativeState} · 已转发 ${Number(bridge.commands_forwarded || 0)} 条命令 / ${Number(bridge.results_forwarded || 0)} 条结果`;
      } else {
        statusText.textContent = "仅在需要由 CreatorHub 托管本机 127.0.0.1 WMPF 转发桥时启动。";
      }
    }
    if (startBtn) startBtn.disabled = running;
    if (stopBtn) stopBtn.disabled = !running;
    if (Array.isArray(bridge.wechat_processes)) renderProcesses(bridge.wechat_processes);
  }

  function renderStatus(data, probeOverride = null) {
    if (!data) return;
    lastStatus = data;
    const cfgState = data.config || {};
    const epInfo = cfgState.endpoint_file_info;
    const proxyState = data.proxy || {};
    const badge = $("ch-proto-badge");
    const summaryText = $("ch-proto-summary-text");
    const probe = probeOverride || data.probe || null;

    if (badge && summaryText) {
      const proxyReady = cfgState.connection_mode === "proxy" && proxyState.running && Number(proxyState.ready_pages || 0) > 0;
      if ((probe && probe.ok && probe.ready) || proxyReady) {
        badge.className = "pill done";
        badge.textContent = "已连通微信视频号";
        summaryText.textContent = "本机桌面微信通道运行正常，可执行搜索、创作者主页采集、全量二级评论与视频解密（下次打开软件自动恢复）。";
      } else if (probe && !probe.ok) {
        badge.className = "pill failed";
        badge.textContent = "连接未就绪";
        summaryText.textContent = probe.error || "本地微信 Finder 通道暂不可达";
      } else if (data.configured) {
        if (cfgState.connection_mode === "proxy") {
          if (proxyState.running) {
            badge.className = "pill pending";
            badge.textContent = "等待微信点开链接";
            summaryText.textContent = "本地代理已就绪并正在实时监听，请在电脑微信「文件传输助手」点开桥接链接（点开后自动变绿，无需手动检测）。";
          } else {
            badge.className = "pill pending";
            badge.textContent = "已保存 · 未启动";
            summaryText.textContent = "点击下方「一键连接本机微信」即可立即开启通道。";
          }
        } else if (cfgState.connection_mode === "endpoint_file" && epInfo && !epInfo.exists) {
          badge.className = "pill pending";
          badge.textContent = "口令文件未就绪";
          summaryText.textContent = "已配置口令文件路径，但文件尚不存在；请先启动对应 WMPF 桥接。";
        } else if (cfgState.connection_mode === "endpoint_file" && epInfo && !epInfo.valid) {
          badge.className = "pill failed";
          badge.textContent = "口令文件无效";
          summaryText.textContent = epInfo.error || "口令文件未通过回环校验";
        } else {
          badge.className = "pill pending";
          badge.textContent = "已配置 · 待测试";
          summaryText.textContent = cfgState.connection_mode === "endpoint_file"
            ? "已绑定 WMPF 控制口令文件；点击「测试通道连接」校验桌面微信页面。"
            : `已配置端点 ${cfgState.cdp_endpoint}；点击「测试通道连接」校验桌面微信页面。`;
        }
      } else {
        badge.className = "pill skipped";
        badge.textContent = "未配置";
        summaryText.textContent = "点击下方「一键连接本机微信」，一键完成证书检查、本地代理与开机恢复（仅本账号同步与发布可直接使用创作者助手）。";
      }
    }

    const fileHelp = $("ch-proto-file-help");
    if (fileHelp) {
      if (epInfo && epInfo.path) {
        fileHelp.textContent = epInfo.exists
          ? (epInfo.valid ? `口令文件有效（server PID ${epInfo.server_pid || "未知"}${epInfo.server_running === false ? "，进程已退出" : ""}）。` : `口令文件异常：${epInfo.error}`)
          : "当前路径尚未生成口令文件；可先启动下方 WMPF 托管桥接。";
      } else {
        fileHelp.innerHTML = "指向 <code>wmpf_cdp.LoopbackBridge</code> 生成的 JSON 描述文件；若使用下方托管桥接会自动填入。";
      }
    }

    renderCapabilities(data.capabilities, data.read_mode || cfgState.read_mode);
    renderProbeBox(probe);
    renderProxy(data.proxy);
    renderBridge(data.bridge);
    syncAutoPoll(data.proxy);
  }

  function applyConfigToForm(data, force = false) {
    const cfgState = data?.config || {};
    const assign = (id, val) => {
      const el = $(id);
      if (!el) return;
      if (force || !globalThis.CreatorHubWorkbench?.isDirty?.(id)) el.value = val;
    };
    assign("ch-proto-read-mode", cfgState.read_mode || data?.read_mode || "auto");
    assign("ch-proto-mode", cfgState.connection_mode || "none");
    assign("ch-proto-proxy-port", String(cfgState.proxy_port || data?.proxy?.proxy_port || 2023));
    assign("ch-proto-endpoint", cfgState.cdp_endpoint || "");
    assign("ch-proto-endpoint-file", cfgState.cdp_endpoint_file || "");
    const sysCheck = $("ch-proxy-manage-sysproxy");
    if (sysCheck && force && typeof cfgState.proxy_manage_sysproxy === "boolean") {
      sysCheck.checked = cfgState.proxy_manage_sysproxy;
    }
    if (force) {
      clearDirtyFlags();
    }
    onModeChange();
    csSyncAll();
  }

  async function load(force = false) {
    if (!$("channels-protocol-card")) return null;
    try {
      const data = await api("/api/channels/protocol/status");
      applyConfigToForm(data, force);
      renderStatus(data);
      return data;
    } catch (e) {
      setMsg("读取通道状态失败：" + e.message, "error");
      return null;
    }
  }

  function fillEndpoint(url) {
    if ($("ch-proto-mode")) $("ch-proto-mode").value = "endpoint";
    if ($("ch-proto-endpoint")) $("ch-proto-endpoint").value = url;
    onModeChange();
    csSyncAll();
    setMsg(`已填入端点 ${url}，点击「测试通道连接」或「保存通道配置」`, "warning");
  }

  async function discover() {
    const btn = $("ch-proto-scan-btn");
    const box = $("ch-proto-discovered");
    await withBusy(btn, "扫描中", async () => {
      try {
        const res = await api("/api/channels/protocol/discover");
        renderProcesses(res.wechat_processes);
        renderBridge(res.bridge);
        const list = Array.isArray(res.endpoints) ? res.endpoints : [];
        if (box) {
          box.hidden = false;
          if (!list.length) {
            box.innerHTML = '<div class="field-help" style="margin:0">未扫描到本机回环 CDP HTTP 端口（已检查 9222/9223/9229/9333）。标准零售版桌面微信默认不开放 CDP 端口，建议直接使用「内置本机微信代理桥接」。</div>';
          } else {
            box.innerHTML = list.map(item => {
              const targets = Array.isArray(item.finder_targets) ? item.finder_targets : [];
              const tag = item.has_finder_page
                ? `<span class="pill done bare">发现 ${targets.length} 个视频号页</span>`
                : `<span class="pill skipped bare">${Number(item.total_pages || 0)} 个非视频号页</span>`;
              const detail = targets.length
                ? targets.map(t => `${t.title || "视频号"} (${t.path})`).join("、")
                : "当前端口暂无 channels.weixin.qq.com/web/pages/* 页面";
              return `<div class="channels-proto-port">
                <div class="channels-proto-port-copy"><div><code>${esc(item.endpoint)}</code> ${tag}</div><div class="field-help" style="margin:2px 0 0">${esc(detail)}</div></div>
                <button type="button" class="ghost sm" onclick="CreatorHubChannelsProtocol.fillEndpoint(${jsArg(item.endpoint)})">选用此端点</button>
              </div>`;
            }).join("");
          }
        }
        setMsg(list.some(x => x.has_finder_page) ? "已发现本机微信视频号 CDP 端口" : "端口扫描完成", list.some(x => x.has_finder_page) ? "success" : "");
      } catch (e) {
        setMsg("端口扫描失败：" + e.message, "error");
      }
    });
  }

  async function probe(explicitMode = null) {
    const useProxyDirect = explicitMode === "proxy";
    const btn = useProxyDirect
      ? ($("ch-proxy-probe-btn") || $("ch-proto-probe-btn"))
      : $("ch-proto-probe-btn");
    const mode = useProxyDirect ? "proxy" : ($("ch-proto-mode")?.value || "none");
    const endpoint = $("ch-proto-endpoint")?.value.trim() || "";
    const endpointFile = $("ch-proto-endpoint-file")?.value.trim() || "";
    setMsg("正在握手探测本机微信 Finder 页面…");
    await withBusy(btn, "探测中", async () => {
      try {
        const res = await api("/api/channels/protocol/probe", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            connection_mode: mode,
            cdp_endpoint: useProxyDirect ? "" : endpoint,
            cdp_endpoint_file: useProxyDirect ? "" : endpointFile,
          }),
        });
        const probeObj = res.probe || { ok: !!res.ok, ready: !!res.ready, error: res.error || "" };
        if (lastStatus) {
          if (res.proxy) lastStatus = { ...lastStatus, proxy: res.proxy };
          renderStatus(lastStatus, probeObj);
        } else {
          if (res.proxy) renderProxy(res.proxy);
          renderProbeBox(probeObj);
        }
        if (probeObj.ok && probeObj.ready) {
          setMsg("通道握手成功 ✓ 已命中微信原生视频号页面", "success");
          toast("微信视频号通道连通正常 ✓", "ok");
        } else {
          setMsg(probeObj.error || "通道未就绪", "error");
          toast(probeObj.error || "通道未就绪", "err", 6000);
        }
      } catch (e) {
        setMsg("探测失败：" + e.message, "error");
        toast("探测失败：" + e.message, "err", 6000);
      }
    });
  }

  async function save() {
    const btn = $("ch-proto-save-btn");
    const mode = $("ch-proto-mode")?.value || "none";
    const readMode = $("ch-proto-read-mode")?.value || "auto";
    const proxyPort = Number($("ch-proto-proxy-port")?.value || 2023);
    const endpoint = $("ch-proto-endpoint")?.value.trim() || "";
    const endpointFile = $("ch-proto-endpoint-file")?.value.trim() || "";

    if (mode === "proxy" && (!Number.isInteger(proxyPort) || proxyPort < 1024 || proxyPort > 65535)) {
      setMsg("内置代理端口必须是 1024-65535 之间的整数", "error");
      $("ch-proto-proxy-port")?.focus();
      return;
    }
    if (mode === "endpoint" && !endpoint) {
      setMsg("直连模式下请先填写本机回环 CDP 端点地址", "error");
      $("ch-proto-endpoint")?.focus();
      return;
    }
    if (mode === "endpoint_file" && !endpointFile) {
      setMsg("口令文件模式下请填写 WMPF 控制口令文件路径", "error");
      $("ch-proto-endpoint-file")?.focus();
      return;
    }

    setMsg("正在保存…");
    await withBusy(btn, "保存中", async () => {
      try {
        const payload = {
          channels_read_mode: readMode,
          connection_mode: mode,
          channels_cdp_endpoint: mode === "endpoint" ? endpoint : "",
          channels_cdp_endpoint_file: mode === "endpoint_file" ? endpointFile : "",
        };
        if (mode === "proxy" || Number.isInteger(proxyPort)) {
          payload.channels_proxy_port = proxyPort;
        }
        const data = await api("/api/channels/protocol/config", {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        });
        applyConfigToForm(data, true);
        renderStatus(data);
        setMsg("已保存 ✓ 后续视频号采集、监控与分享下载将按此配置执行", "success");
        toast("视频号微信通道配置已保存", "ok");
        globalThis.CreatorHubEngineSettings?.load?.();
        globalThis.CreatorHubTransportMatrix?.load?.();
      } catch (e) {
        setMsg("保存失败：" + e.message, "error");
        toast("保存失败：" + e.message, "err", 6000);
      }
    });
    if (lastStatus?.proxy) renderProxy(lastStatus.proxy);
    if (lastStatus?.bridge) renderBridge(lastStatus.bridge);
  }

  async function reset() {
    const btn = $("ch-proto-reset-btn");
    setMsg("正在恢复默认配置…");
    await withBusy(btn, "恢复中", async () => {
      try {
        const data = await api("/api/channels/protocol/config", { method: "DELETE" });
        applyConfigToForm(data, true);
        renderStatus(data);
        setMsg("已恢复 config.yaml / 环境变量默认配置", "success");
        toast("已恢复默认配置", "ok");
        globalThis.CreatorHubEngineSettings?.load?.();
        globalThis.CreatorHubTransportMatrix?.load?.();
      } catch (e) {
        setMsg("恢复失败：" + e.message, "error");
      }
    });
    if (lastStatus?.proxy) renderProxy(lastStatus.proxy);
    if (lastStatus?.bridge) renderBridge(lastStatus.bridge);
  }

  async function silentCopyText(text) {
    const clean = String(text || "").trim();
    if (!clean) return false;
    try {
      if (navigator?.clipboard?.writeText) {
        await navigator.clipboard.writeText(clean);
        return true;
      }
    } catch {}
    return false;
  }

  async function startProxy() {
    const btn = $("ch-proxy-start-btn");
    const proxyPort = Number($("ch-proto-proxy-port")?.value || 2023);
    const sysProxyCheck = $("ch-proxy-manage-sysproxy");
    const manageSysProxy = sysProxyCheck ? !!sysProxyCheck.checked : true;
    if (!Number.isInteger(proxyPort) || proxyPort < 1024 || proxyPort > 65535) {
      setMsg("内置代理端口必须是 1024-65535 之间的整数", "error");
      $("ch-proto-proxy-port")?.focus();
      return;
    }
    const caAlreadyTrusted = !!lastStatus?.proxy?.ca?.trusted;
    setMsg(
      manageSysProxy && !caAlreadyTrusted
        ? "正在自动检查根证书（首次请在系统弹窗点「是」）并开启微信通道…"
        : (manageSysProxy ? "正在一键开启本地视频号通道并保存自动恢复…" : "正在启动内置本地视频号代理（不修改系统代理）…")
    );
    await withBusy(btn, "连接中", async () => {
      try {
        const data = await api("/api/channels/protocol/proxy/start", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            proxy_port: proxyPort,
            enable_system_proxy: manageSysProxy,
            apply_to_config: true,
            auto_install_cert: true,
          }),
        });
        applyConfigToForm(data, true);
        renderStatus(data);
        const readyNow = Number(data?.proxy?.ready_pages || 0) > 0;
        const bridgeUrl = String(data?.proxy?.bridge_entry_url || "https://channels.weixin.qq.com/platform/bridge");
        if (readyNow) {
          setMsg("微信视频号通道已连通 ✓ 下次打开软件将自动恢复", "success");
          toast("微信视频号通道已连通 ✓", "ok");
        } else {
          const copied = await silentCopyText(bridgeUrl);
          const copyNote = copied ? "（桥接链接已自动复制）" : "";
          if (!manageSysProxy) {
            setMsg(`内置视频号代理已监听 127.0.0.1:${proxyPort}${copyNote}，请将 channels.weixin.qq.com 分流至该端口`, "success");
          } else if (data?.proxy?.wechat_needs_restart) {
            setMsg(`通道已开启${copyNote} ✓ 请按上方引导点「① 一键重开微信」→「② 复制桥接链接并打开微信」`, "success");
          } else {
            setMsg(`通道已开启${copyNote} ✓ 点击上方「复制桥接链接并打开微信」，在「文件传输助手」粘贴点开即自动连通`, "success");
          }
          toast("本地通道已开启，请在微信点开桥接链接", "ok");
        }
        globalThis.CreatorHubEngineSettings?.load?.();
        globalThis.CreatorHubTransportMatrix?.load?.();
      } catch (e) {
        setMsg("一键连接失败：" + e.message, "error");
        toast("一键连接失败：" + e.message, "err", 7000);
      }
    });
    if (lastStatus?.proxy) renderProxy(lastStatus.proxy);
  }

  async function stopProxy() {
    const btn = $("ch-proxy-stop-btn");
    stopAutoPoll();
    setMsg("正在断开通道并恢复 Windows 系统代理…");
    await withBusy(btn, "断开中", async () => {
      try {
        const data = await api("/api/channels/protocol/proxy/stop", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ disable_auto_start: true }),
        });
        applyConfigToForm(data, true);
        renderStatus(data);
        setMsg("已关闭视频号微信通道，系统代理已恢复原状", "");
        toast("视频号微信通道已关闭", "info");
        globalThis.CreatorHubEngineSettings?.load?.();
        globalThis.CreatorHubTransportMatrix?.load?.();
      } catch (e) {
        setMsg("关闭通道失败：" + e.message, "error");
      }
    });
    if (lastStatus?.proxy) renderProxy(lastStatus.proxy);
  }

  async function copyAndOpenWechat(url) {
    const bridgeUrl = String(url || lastStatus?.proxy?.bridge_entry_url || "https://channels.weixin.qq.com/platform/bridge").trim();
    const copied = await silentCopyText(bridgeUrl);
    try {
      const res = await api("/api/channels/protocol/wechat/open", { method: "POST" });
      if (lastStatus && res?.proxy) {
        lastStatus = { ...lastStatus, proxy: res.proxy };
        renderStatus(lastStatus);
      }
      if (res?.ok) {
        setMsg(
          (copied ? "桥接链接已复制，已唤起电脑微信 ✓ " : "已唤起电脑微信 ✓ ")
            + "请在「文件传输助手」粘贴发送并点击打开一次，本页将自动变绿",
          "success"
        );
        toast("链接已复制并已打开微信，请在「文件传输助手」粘贴点开", "ok", 4500);
      } else {
        setMsg(
          (copied ? "桥接链接已复制 ✓ " : "")
            + (res?.error || "请手动切换到电脑微信，在「文件传输助手」粘贴发送并点开"),
          copied ? "success" : "warning"
        );
        toast(copied ? "桥接链接已复制，请切到微信粘贴点开" : (res?.error || "请手动打开电脑微信"), copied ? "ok" : "err", 4500);
      }
    } catch (e) {
      if (copied) {
        setMsg("桥接链接已复制 ✓ 请切换到电脑微信「文件传输助手」粘贴发送并点开", "success");
        toast("桥接链接已复制，请切到微信粘贴点开", "ok", 4000);
      } else {
        setMsg("唤起微信失败：" + e.message, "error");
      }
    }
  }

  async function restartWechat() {
    const bridgeUrl = String(lastStatus?.proxy?.bridge_entry_url || "https://channels.weixin.qq.com/platform/bridge").trim();
    await silentCopyText(bridgeUrl);
    setMsg("正在重启电脑微信以应用新代理…");
    try {
      const res = await api("/api/channels/protocol/wechat/restart", { method: "POST" });
      if (lastStatus && res?.proxy) {
        lastStatus = { ...lastStatus, proxy: res.proxy };
        renderStatus(lastStatus);
      }
      if (res?.ok) {
        setMsg(res.message || "已重开电脑微信 ✓ 点击「进入微信」后在「文件传输助手」粘贴并点开桥接链接", "success");
        toast("电脑微信已重开（桥接链接已复制）✓", "ok", 5000);
      } else {
        setMsg(res?.error || "未能自动重开微信，请手动在右下角托盘退出微信后重新打开", "warning");
        toast(res?.error || "请手动退出并重开电脑微信", "err", 6000);
      }
    } catch (e) {
      setMsg("重启微信失败：" + e.message, "error");
      toast("重启微信失败：" + e.message, "err", 6000);
    }
  }

  async function installProxyCert() {
    const btn = $("ch-proxy-ca-install-btn");
    setMsg("正在请求安装本地 CA 根证书（请在 Windows 安全弹窗点击「是」）…");
    await withBusy(btn, "安装中", async () => {
      try {
        const res = await api("/api/channels/protocol/proxy/cert/install", { method: "POST" });
        if (lastStatus && res.proxy) {
          lastStatus = { ...lastStatus, proxy: res.proxy };
          renderStatus(lastStatus);
        } else if (res.proxy) {
          renderProxy(res.proxy);
        }
        if (res.ok) {
          setMsg(res.message || "本地 CA 根证书已受信任 ✓", "success");
          toast("根证书安装成功 ✓", "ok");
        } else {
          setMsg(res.error || "根证书未完成安装", "error");
          toast(res.error || "根证书未完成安装", "err", 7000);
        }
      } catch (e) {
        setMsg("安装根证书失败：" + e.message, "error");
        toast("安装根证书失败：" + e.message, "err", 7000);
      }
    });
    if (lastStatus?.proxy) renderProxy(lastStatus.proxy);
  }

  async function uninstallProxyCert() {
    const btn = $("ch-proxy-ca-uninstall-btn");
    setMsg("正在从当前用户受信任根证书卸载…");
    await withBusy(btn, "卸载中", async () => {
      try {
        const res = await api("/api/channels/protocol/proxy/cert/uninstall", { method: "POST" });
        if (lastStatus && res.proxy) {
          lastStatus = { ...lastStatus, proxy: res.proxy };
          renderStatus(lastStatus);
        } else if (res.proxy) {
          renderProxy(res.proxy);
        }
        if (res.ok) {
          setMsg(res.message || "已卸载本地根证书", "");
          toast("已卸载本地根证书", "info");
        } else {
          setMsg(res.error || "卸载根证书失败", "error");
          toast(res.error || "卸载根证书失败", "err", 7000);
        }
      } catch (e) {
        setMsg("卸载根证书失败：" + e.message, "error");
      }
    });
    if (lastStatus?.proxy) renderProxy(lastStatus.proxy);
  }

  function downloadProxyCert() {
    const a = document.createElement("a");
    a.href = "/api/channels/protocol/proxy/ca.cer";
    a.download = "creatorhub-channels-ca.cer";
    document.body.appendChild(a);
    a.click();
    a.remove();
  }

  async function startBridge() {
    const btn = $("ch-bridge-start-btn");
    const debugPort = Number($("ch-bridge-debug-port")?.value || 9421);
    const cdpPort = Number($("ch-bridge-cdp-port")?.value || 9422);
    const rawPids = ($("ch-bridge-pids")?.value || "").trim();
    let allowedPids = null;
    if (rawPids) {
      const parts = rawPids.split(/[\s,，]+/).filter(Boolean);
      const parsed = parts.map(Number);
      if (parsed.some(n => !Number.isInteger(n) || n <= 0)) {
        setMsg("允许的 PID 格式有误，请填写正整数（多个用逗号分隔）", "error");
        $("ch-bridge-pids")?.focus();
        return;
      }
      allowedPids = parsed;
    }
    setMsg("正在启动本地 WMPF 回环桥接…");
    await withBusy(btn, "启动中", async () => {
      try {
        const data = await api("/api/channels/protocol/bridge/start", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            debug_port: debugPort,
            cdp_port: cdpPort,
            allowed_pids: allowedPids,
            apply_to_config: true,
          }),
        });
        applyConfigToForm(data, true);
        renderStatus(data);
        setMsg("WMPF 本地回环桥接已启动并写入当前配置 ✓", "success");
        toast("WMPF 本地桥接已启动", "ok");
        globalThis.CreatorHubTransportMatrix?.load?.();
      } catch (e) {
        setMsg("启动桥接失败：" + e.message, "error");
        toast("启动桥接失败：" + e.message, "err", 7000);
      }
    });
    if (lastStatus?.bridge) renderBridge(lastStatus.bridge);
  }

  async function stopBridge() {
    const btn = $("ch-bridge-stop-btn");
    setMsg("正在停止桥接…");
    await withBusy(btn, "停止中", async () => {
      try {
        const data = await api("/api/channels/protocol/bridge/stop", { method: "POST" });
        applyConfigToForm(data, true);
        renderStatus(data);
        setMsg("WMPF 本地回环桥接已停止", "");
        toast("WMPF 本地桥接已停止", "info");
        globalThis.CreatorHubTransportMatrix?.load?.();
      } catch (e) {
        setMsg("停止桥接失败：" + e.message, "error");
      }
    });
    if (lastStatus?.bridge) renderBridge(lastStatus.bridge);
  }

  async function copyPageUrl(url) {
    const text = String(url || "").trim();
    if (!text) return false;
    if (typeof copyTextValue === "function") {
      return copyTextValue(text, "视频号页面链接已复制", "复制失败，请手动复制链接");
    }
    try {
      await navigator.clipboard.writeText(text);
      toast("视频号页面链接已复制", "ok", 1800);
      return true;
    } catch (e) {
      toast("复制失败，请手动复制链接", "err");
      return false;
    }
  }

  return {
    load,
    onModeChange,
    fillEndpoint,
    discover,
    probe,
    save,
    reset,
    startProxy,
    stopProxy,
    installProxyCert,
    uninstallProxyCert,
    downloadProxyCert,
    copyPageUrl,
    copyAndOpenWechat,
    restartWechat,
    pollOnce,
    stopAutoPoll,
    startBridge,
    stopBridge,
  };
})();
globalThis.CreatorHubChannelsProtocol = CreatorHubChannelsProtocol;
const QMAP = { "": "默认", highest: "原画", "1080": "1080P", "720": "720P", "540": "540P", lowest: "省流" };

// 10b-diagnostics.js — 系统精简日志查看、运行期调试级别切换、一键复制排查摘要与脱敏诊断包导出。
function openDiagnosticsSettings() {
  switchTab("settings");
  globalThis.CreatorHubWorkbench?.showSection?.("settings", "diagnostics");
  globalThis.CreatorHubDiagnostics?.load?.();
}

const CreatorHubDiagnostics = (() => {
  let filterLevel = "warn";
  let logSource = "memory";
  let searchTimer = null;
  let lastLogDir = "";
  let cachedEntries = [];

  function fmtBytes(bytes) {
    const n = Number(bytes || 0);
    if (n < 1024) return n + " B";
    if (n < 1024 * 1024) return (n / 1024).toFixed(1) + " KB";
    return (n / (1024 * 1024)).toFixed(2) + " MB";
  }

  function compactTimestamp(ts) {
    const s = String(ts || "").trim();
    // 2026-11-23 17:43:13.931 -> 11-23 17:43:13.931 (保留完整时间供悬浮提示)
    return /^\d{4}-\d{2}-\d{2}\s+/.test(s) ? s.slice(5) : (s || "-");
  }

  function levelBadge(level) {
    const lv = String(level || "INFO").toUpperCase();
    if (lv === "ERROR" || lv === "CRITICAL") return `<span class="pill failed bare">${esc(lv === "CRITICAL" ? "CRIT" : "ERROR")}</span>`;
    if (lv === "WARNING" || lv === "WARN") return `<span class="pill pending bare">WARN</span>`;
    if (lv === "DEBUG") return `<span class="pill skipped bare">DEBUG</span>`;
    return `<span class="pill downloading bare">INFO</span>`;
  }

  function rowToneClass(level) {
    const lv = String(level || "").toUpperCase();
    if (lv === "ERROR" || lv === "CRITICAL") return " lvl-error";
    if (lv === "WARNING" || lv === "WARN") return " lvl-warn";
    return "";
  }

  function syncFilterButtons() {
    document.querySelectorAll("#diagnostics-card [data-diag-lvl]").forEach(btn => {
      btn.classList.toggle("active", btn.dataset.diagLvl === filterLevel);
    });
    const warnCard = $("diag-card-warn");
    const errCard = $("diag-card-err");
    if (warnCard) warnCard.classList.toggle("is-active-filter", filterLevel === "warn");
    if (errCard) errCard.classList.toggle("is-active-filter", filterLevel === "error");
  }

  function syncSearchClearButton() {
    const input = $("diag-search-input");
    const clearBtn = $("diag-search-clear");
    if (clearBtn && input) clearBtn.hidden = !input.value.length;
  }

  function renderMeta(data) {
    const counts = data?.memory_counts || {};
    const warns = Number(counts.warnings || 0);
    const errs = Number(counts.errors || 0);
    const total = Number(counts.total || 0);
    const warnPlusErr = warns + errs;
    const activeLvl = String(data?.level || "info").toLowerCase();

    // 1. 记录模式卡片与分段开关
    const lvlPill = $("diag-level-pill");
    if (lvlPill) {
      const modeMap = {
        info: ["is-info", "INFO 精简"],
        debug: ["is-debug", "DEBUG 调试"],
        warning: ["is-warn", "WARN 仅告警"],
      };
      const [tone, label] = modeMap[activeLvl] || modeMap.info;
      lvlPill.className = "diag-mode-pill " + tone;
      lvlPill.innerHTML = `<i class="diag-dot"></i>${esc(label)}`;
    }
    document.querySelectorAll("#diagnostics-card [data-diag-mode]").forEach(btn => {
      btn.classList.toggle("active", btn.dataset.diagMode === activeLvl);
    });
    const modeHint = $("diag-mode-hint");
    if (modeHint) {
      modeHint.textContent = activeLvl === "debug"
        ? "正在记录详细调试信息，复现后建议切回精简"
        : activeLvl === "warning"
          ? "仅记录告警与异常错误，常规操作静默"
          : "默认静默高频轮询，仅留存关键轨迹与异常";
    }

    // 2. 告警与错误指标卡
    const warnNum = $("diag-warn-num");
    if (warnNum) warnNum.textContent = String(warns);
    const errNum = $("diag-err-num");
    if (errNum) errNum.textContent = String(errs);
    const warnCard = $("diag-card-warn");
    if (warnCard) warnCard.classList.toggle("has-warn", warns > 0);
    const errCard = $("diag-card-err");
    if (errCard) errCard.classList.toggle("has-err", errs > 0);

    // 3. 本地存储占用卡
    const filesCount = Array.isArray(data?.files) ? data.files.length : 0;
    const fileCntEl = $("diag-file-cnt");
    if (fileCntEl) fileCntEl.textContent = `${filesCount} 个文件`;
    const sizeValEl = $("diag-size-val");
    if (sizeValEl) sizeValEl.textContent = fmtBytes(data?.total_bytes || 0);

    // 4. 控制台顶栏计数胶囊
    const cntWarn = $("diag-cnt-warn");
    if (cntWarn) cntWarn.textContent = String(warnPlusErr);
    const cntErr = $("diag-cnt-err");
    if (cntErr) cntErr.textContent = String(errs);
    const cntAll = $("diag-cnt-all");
    if (cntAll) cntAll.textContent = String(total);

    // 5. 底部目录路径展示
    if (data?.log_dir) {
      lastLogDir = String(data.log_dir);
      const dirCode = $("diag-dir-code");
      if (dirCode) {
        dirCode.textContent = lastLogDir;
        dirCode.title = lastLogDir;
      }
    }
  }

  function renderEntries(entries, totalMemoryCount = 0) {
    const box = $("diag-log-list");
    if (!box) return;
    cachedEntries = Array.isArray(entries) ? entries : [];

    if (!cachedEntries.length) {
      const hasQuery = Boolean($("diag-search-input")?.value?.trim());
      if (hasQuery) {
        box.innerHTML = `<div class="diag-empty-state">
          <div class="diag-empty-halo is-neutral"><svg aria-hidden="true"><use href="#i-search"/></svg></div>
          <div class="diag-empty-title">没有匹配的日志记录</div>
          <div class="diag-empty-sub">请尝试缩短搜索关键字，或切换日志读取来源后重试。</div>
          <div class="diag-empty-act"><button type="button" class="ghost sm" onclick="CreatorHubDiagnostics.clearSearch()">清除搜索条件</button></div>
        </div>`;
        return;
      }
      const isFiltered = filterLevel !== "all";
      const actionBtn = isFiltered
        ? `<div class="diag-empty-act"><button type="button" class="ghost sm" onclick="CreatorHubDiagnostics.setFilter('all')">查看全部精简运行记录${totalMemoryCount ? ` (${totalMemoryCount} 条)` : ""}</button></div>`
        : "";
      box.innerHTML = `<div class="diag-empty-state">
        <div class="diag-empty-halo"><svg aria-hidden="true"><use href="#i-check"/></svg></div>
        <div class="diag-empty-title">${filterLevel === "error" ? "当前暂无错误记录" : isFiltered ? "当前运行健康，暂无告警或错误" : "暂无运行日志"}</div>
        <div class="diag-empty-sub">服务启动、写操作轨迹与后台调度均正常运作；高频页面轮询已自动静默，不会产生冗余刷屏。</div>
        ${actionBtn}
      </div>`;
      return;
    }

    box.innerHTML = cachedEntries.map((item, idx) => {
      const reqBadge = item.req_id ? `<span class="diag-log-req">req=${esc(item.req_id)}</span>` : "";
      const excBlock = item.exception
        ? `<details class="diag-log-exc"><summary>展开异常堆栈详情</summary><pre>${esc(item.exception)}</pre></details>`
        : "";
      return `<div class="diag-log-row${rowToneClass(item.level)}">
        <span class="diag-log-ts" title="${esc(item.timestamp || "")}">${esc(compactTimestamp(item.timestamp))}</span>
        <span>${levelBadge(item.level)}</span>
        <span><span class="diag-log-mod" title="${esc(item.logger || "")}">${esc(item.logger || "app")}</span></span>
        <span class="diag-log-msg">${reqBadge}${esc(item.message || "")}</span>
        <button type="button" class="diag-row-copy" onclick="CreatorHubDiagnostics.copyEntry(${idx})" title="复制此条日志">复制</button>
        ${excBlock}
      </div>`;
    }).join("");
  }

  async function load() {
    if (!$("diagnostics-card")) return;
    syncFilterButtons();
    syncSearchClearButton();
    const q = $("diag-search-input")?.value?.trim() || "";
    const params = new URLSearchParams({
      level: filterLevel,
      source: logSource,
      limit: "150",
    });
    if (q) params.set("q", q);
    try {
      const data = await api("/api/diagnostics/logs?" + params.toString());
      renderMeta(data);
      renderEntries(data.entries || [], Number(data?.memory_counts?.total || 0));
    } catch (e) {
      const box = $("diag-log-list");
      if (box) {
        box.innerHTML = `<div class="diag-empty-state">
          <div class="diag-empty-halo is-neutral"><svg aria-hidden="true"><use href="#i-alert"/></svg></div>
          <div class="diag-empty-title">读取日志失败</div>
          <div class="diag-empty-sub">${esc(e.message)}</div>
        </div>`;
      }
    }
  }

  function setFilter(nextLevel) {
    filterLevel = ["all", "warn", "error"].includes(nextLevel) ? nextLevel : "warn";
    syncFilterButtons();
    load();
  }

  function setSource(nextSource) {
    logSource = ["memory", "error.log", "app.log"].includes(nextSource) ? nextSource : "memory";
    load();
  }

  function onSearchInput() {
    syncSearchClearButton();
    clearTimeout(searchTimer);
    searchTimer = setTimeout(load, 200);
  }

  function clearSearch() {
    const input = $("diag-search-input");
    if (!input) return;
    input.value = "";
    syncSearchClearButton();
    input.focus();
    load();
  }

  async function changeLevel(nextLevel) {
    const msgEl = $("diag-status-msg");
    try {
      const res = await api("/api/diagnostics/log-level", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ level: nextLevel }),
      });
      if (msgEl) msgEl.textContent = res.note || "";
      toast(res.note || "日志级别已更新", nextLevel === "debug" ? "info" : "ok", 4200);
      await load();
    } catch (e) {
      toast("切换日志级别失败：" + e.message, "err");
      await load();
    }
  }

  async function openDir() {
    try {
      await api("/api/diagnostics/open-dir", { method: "POST" });
      toast("已在文件管理器打开日志目录", "ok", 2200);
    } catch (e) {
      toast("打开日志目录失败：" + e.message, "err");
    }
  }

  async function clearLogs() {
    if (!await uiConfirm({
      title: "清理本地历史日志",
      message: "确认清空当前 app.log、error.log 及历史滚动备份日志？不会影响任何账号资料、数据库或媒体文件。",
      okText: "确认清理",
      danger: true,
    })) return;
    try {
      await api("/api/diagnostics/clear-logs", { method: "POST" });
      toast("已清理历史日志文件", "ok");
      await load();
    } catch (e) {
      toast("清理日志失败：" + e.message, "err");
    }
  }

  async function copyTextFallback(text) {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
      return;
    }
    const area = document.createElement("textarea");
    area.value = text;
    area.style.position = "fixed";
    area.style.opacity = "0";
    document.body.appendChild(area);
    area.select();
    document.execCommand("copy");
    area.remove();
  }

  async function copyDirPath() {
    if (!lastLogDir) return;
    try {
      await copyTextFallback(lastLogDir);
      toast("已复制日志目录路径", "ok", 2000);
    } catch {
      toast("复制路径失败", "err");
    }
  }

  async function copyEntry(index) {
    const item = cachedEntries[index];
    if (!item) return;
    const reqPart = item.req_id ? `[req=${item.req_id}] ` : "";
    const text = `${item.timestamp || ""} | ${item.level || "INFO"} | ${item.logger || "app"} | ${reqPart}${item.message || ""}`
      + (item.exception ? `\n${item.exception}` : "");
    try {
      await copyTextFallback(text);
      toast("已复制该条日志", "ok", 1800);
    } catch {
      toast("复制失败", "err");
    }
  }

  async function copyReport() {
    const btn = $("diag-copy-report-btn");
    const note = $("diag-issue-note")?.value?.trim() || "";
    await withBusy(btn, "正在生成", async () => {
      try {
        const res = await api("/api/diagnostics/report-text", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ note }),
        });
        await copyTextFallback(res.text || "");
        toast("已复制脱敏排查摘要，可直接粘贴发送给开发者", "ok", 4000);
      } catch (e) {
        toast("复制排查摘要失败：" + e.message, "err");
      }
    });
  }

  function exportZip() {
    const note = $("diag-issue-note")?.value?.trim() || "";
    const params = new URLSearchParams();
    if (note) params.set("note", note);
    const url = "/api/diagnostics/export.zip" + (params.toString() ? "?" + params.toString() : "");
    const link = document.createElement("a");
    link.href = url;
    link.rel = "noopener";
    document.body.appendChild(link);
    link.click();
    link.remove();
    toast("正在导出脱敏诊断压缩包 (.zip)…", "ok", 3200);
  }

  return {
    load,
    setFilter,
    setSource,
    onSearchInput,
    clearSearch,
    changeLevel,
    openDir,
    clearLogs,
    copyDirPath,
    copyEntry,
    copyReport,
    exportZip,
  };
})();

globalThis.CreatorHubDiagnostics = CreatorHubDiagnostics;

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
    <td class="content-cover-cell"><span class="content-cover-box" onclick="openShareHistoryPreview(${Number(row.id)})" title="点击站内预览">${cover ? `<img class="thumb" src="${esc(safeMediaUrl(cover))}" alt="${esc(title.slice(0, 20))}" referrerpolicy="no-referrer" loading="lazy">` : `<span class="content-cover-empty">${ic(type === "images" ? "i-image" : "i-film")}</span>`}${durBadge}</span></td>
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

// 12-collections.js — 关键词与创作者批量作品采集任务管理、结果筛选与报表导出。
// ─── 监控 ───
// ═══════════ 抖音 / 小红书关键词批量采集 ═══════════
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
function collectionSourceValue() {
  return $("col-source") ? ($("col-source").value || "keyword") : "keyword";
}
async function loadCollectionFavoriteBoards() {
  const accountId = Number($("col-account")?.value || 0);
  const select = $("col-board");
  if (!select || !accountId || !["xhs", "rednote"].includes(PLATFORM)) return;
  const requestedAccount = accountId;
  select.disabled = true;
  select.innerHTML = '<option value="">正在加载收藏夹…</option>';
  $("col-board-help").textContent = "";
  try {
    const boards = await api(`/api/accounts/${accountId}/${PLATFORM === "rednote" ? "rednote" : "xhs"}/favorite-boards`);
    if (Number($("col-account")?.value || 0) !== requestedAccount) return;
    COLLECTION_FAVORITE_BOARDS = boards || [];
    select.innerHTML = '<option value="">请选择收藏夹</option>' + COLLECTION_FAVORITE_BOARDS.map(board =>
      `<option value="${esc(board.id)}">${esc(board.name)}${Number(board.total) ? `（${fmtNum(board.total)}）` : ""}</option>`).join("");
    $("col-board-help").textContent = boards.length ? `已读取 ${boards.length} 个收藏夹` : "当前账号没有可用收藏夹";
  } catch (e) {
    COLLECTION_FAVORITE_BOARDS = [];
    select.innerHTML = '<option value="">加载失败，请重新选择账号</option>';
    $("col-board-help").textContent = e.message;
  } finally {
    select.disabled = false;
    select._csSync?.();
  }
}
function collectionAccountChanged() {
  if (collectionSourceValue() === "favorite_board") loadCollectionFavoriteBoards();
}
function collectionSourceChanged() {
  applyCollectionForm();
  if (collectionSourceValue() === "favorite_board") loadCollectionFavoriteBoards();
}
// 采集来源下拉按平台重建:不适用当前平台的选项直接移除(而非 disabled/hidden),
// 部分浏览器(如 Firefox)对 <option hidden> 不生效,仍会显示灰色禁用项。
const COL_SOURCE_DEFS = [
  { value: "keyword", label: "关键词搜索" },
  { value: "creator", label: "创作者主页" },
  { value: "favorite_all", label: "全部收藏", xhs: true },
  { value: "favorite_board", label: "指定收藏夹", xhs: true },
  { value: "account_works", label: "本账号作品", channels: true },
];
function rebuildCollectionSources(xhs, channels, tiktok) {
  const sel = $("col-source");
  if (!sel) return;
  const current = sel.value;
  const bilibili = PLATFORM === "bilibili";
  const rednote = PLATFORM === "rednote";
  const instagram = PLATFORM === "instagram";
  const twitter = PLATFORM === "twitter";
  sel.innerHTML = "";
  for (const o of COL_SOURCE_DEFS) {
    let keep;
    if (o.channels) keep = channels || bilibili || instagram || twitter;
    else if (o.xhs) keep = xhs || rednote;
    else keep = !(tiktok && o.value !== "keyword");
    if (!keep) continue;
    const opt = document.createElement("option");
    opt.value = o.value;
    opt.textContent = o.label;
    sel.appendChild(opt);
  }
  const hasActiveChannelsAccount = channels && ACCOUNTS.some(
    a => a.platform === "shipinhao" && a.status !== "invalid" && a.has_storage
  );
  if ([...sel.options].some(o => o.value === current)) sel.value = current;
  else if (channels) sel.value = hasActiveChannelsAccount ? "account_works" : "keyword";
  else if (tiktok) sel.value = "keyword";
  else sel.value = "keyword";
}
function applyCollectionForm() {
  const enabled = !!($("col-download") && $("col-download").checked);
  if ($("col-dir-wrap")) $("col-dir-wrap").style.display = enabled ? "" : "none";
  const xhs = PLATFORM === "xhs";
  const channels = PLATFORM === "shipinhao";
  const tiktok = PLATFORM === "tiktok";
  const platformName = PF_NAME[PLATFORM] || "平台";
  rebuildCollectionSources(xhs, channels, tiktok);
  populateCollectionAccount();
  const source = collectionSourceValue();
  const channelsAccountOnly = channels && source === "account_works";
  const biliAccountOnly = PLATFORM === "bilibili" && source === "account_works";
  const igAccountOnly = PLATFORM === "instagram" && source === "account_works";
  const twAccountOnly = PLATFORM === "twitter" && source === "account_works";
  const accountWorksOnly = channelsAccountOnly || biliAccountOnly || igAccountOnly || twAccountOnly;
  const favorite = ["favorite_all", "favorite_board"].includes(source);
  const alternate = source !== "keyword";
  if ($("col-keywords-wrap")) $("col-keywords-wrap").style.display = alternate ? "none" : "";
  if ($("col-board-wrap")) $("col-board-wrap").style.display = source === "favorite_board" ? "" : "none";
  if ($("col-source-ref-wrap")) $("col-source-ref-wrap").style.display = source === "creator" ? "" : "none";
  if ($("collection-create-title")) {
    $("collection-create-title").textContent = channelsAccountOnly
      ? "新建视频号本账号作品采集"
      : biliAccountOnly
      ? "新建哔哩哔哩本账号作品采集"
      : igAccountOnly
      ? "新建 Instagram 本账号作品采集"
      : twAccountOnly
      ? "新建 Twitter 本账号作品采集"
      : favorite
      ? `新建${platformName}收藏采集`
      : source === "creator"
        ? `新建${platformName}创作者采集`
        : `新建${platformName}关键词采集`;
  }
  if ($("collection-create-sub")) $("collection-create-sub").textContent = channelsAccountOnly ? "同步当前登录账号的视频与图文，并按需采集评论与解密媒体" : biliAccountOnly ? "同步当前登录账号的投稿稿件，并按需采集评论与下载视频" : igAccountOnly ? "同步当前登录账号的帖子与 Reels，并按需采集评论与下载原图/视频" : twAccountOnly ? "同步当前登录账号的推文与媒体，并按需采集回复与下载高清原图/视频" : channels ? "通过微信原生 Finder 协议采集作品、解密视频并拉取全量评论线程" : favorite ? "导入收藏笔记并继续抓取详情与评论" : source === "creator" ? "导入创作者公开作品并抓取评论" : xhs ? "批量搜索笔记并抓取评论" : tiktok ? "通过离线签名 API 批量搜索 TikTok 视频并抓取评论" : PLATFORM === "bilibili" ? "通过离线 WBI 签名接口批量搜索视频并抓取评论" : PLATFORM === "instagram" ? "通过 Instagram 双栈协议接口批量搜索话题/帖子并抓取评论" : PLATFORM === "twitter" ? "通过 Twitter 混合协议搜索推文并采集回复线程" : "批量搜索视频并抓取评论";
  if ($("nav-collections-label")) $("nav-collections-label").textContent = channels ? "作品批量采集" : "关键词采集";
  if ($("col-content-limit-label")) $("col-content-limit-label").textContent = accountWorksOnly ? "作品上限" : "每词作品上限";
  if ($("col-page-limit-label")) $("col-page-limit-label").innerHTML = accountWorksOnly ? '作品页采集深度 <span class="field-scope">1–40 页</span>' : '每词采集深度 <span class="field-scope">1–40 页</span>';
  if ($("col-sort-wrap")) $("col-sort-wrap").style.display = (accountWorksOnly || source === "creator") ? "none" : "";
  if ($("col-page-help")) $("col-page-help").textContent = channelsAccountOnly ? "依次读取视频与图文管理页(或复用微信原生 Finder 主页协议)。" : biliAccountOnly ? "依次读取创作中心稿件列表接口分页。" : igAccountOnly ? "依次读取 Instagram 用户时间线 feed/user 分页。" : twAccountOnly ? "依次读取当前账号时间线 UserTweets 分页。" : channels ? "一页对应一次微信原生 Finder 搜索或主页翻页请求。" : (xhs || PLATFORM === "rednote") ? "一页对应一次搜索结果下滑或接口翻页。" : PLATFORM === "bilibili" ? "一页对应一次 WBI 搜索或 UP 主空间投稿接口翻页。" : PLATFORM === "instagram" ? "一页对应一次 tags/sections 或 feed/user 接口翻页。" : PLATFORM === "twitter" ? "一页对应一次 SearchTimeline 或 UserTweets 时间线翻页。" : "一页对应一次搜索结果下滑。";
  if ($("col-source-ref")) $("col-source-ref").placeholder = PLATFORM === "douyin" ? "抖音 sec_uid" : PLATFORM === "xhs" ? "小红书 user_id" : PLATFORM === "rednote" ? "海外小红书 user_id / www.rednote.com 主页链接" : PLATFORM === "shipinhao" ? "视频号 v2_...@finder / sph 标识 / 主页链接" : PLATFORM === "bilibili" ? "哔哩哔哩 UP 主 mid / space 主页链接 / 视频分享文案" : PLATFORM === "instagram" ? "Instagram @username / 主页链接 / 数字 UID" : PLATFORM === "twitter" ? "Twitter @username / x.com 主页链接 / 数字 UID" : "快手 user_id / 主页链接 / 短链 / 分享文案";
  resetCollectionCreatorState();
  if ($("col-replies-help")) $("col-replies-help").textContent = `采集${platformName}当前可返回的回复`;
  if ($("collection-callout-title")) $("collection-callout-title").textContent = channelsAccountOnly ? "支持创作者助手本账号同步与微信原生 Finder 协议增强" : channels ? "连接本机已登录微信即可搜索、采主页、抓全量评论与解密下载（无需网页账号）" : "作品上限、采集深度和停止条件任一满足即结束";
  if ($("collection-callout-copy")) $("collection-callout-copy").textContent = channels
    ? "视频号采用双通道架构：「关键词搜索 / 创作者主页」直接走「设置 → 视频号微信通道」一键连接的本机微信，无需登录创作者助手；「本账号作品」则读取创作者助手中已登录的本账号视频与图文。"
    : tiktok
    ? "TikTok 当前默认 native：关键词、评论和二级回复均由离线签名 API 直连，全程不打开浏览器；只有在设置中手动选择 auto/browser 才会使用页面。直播不在采集范围；视频发布请使用独立发布入口。"
    : PLATFORM === "bilibili"
    ? "哔哩哔哩默认使用离线 WBI 签名 API 直连搜索视频、读取 UP 主空间与本账号稿件、抓取主评论与楼中楼回复，并携带 Referer 下载渐进式 MP4 媒体；仅在普通接口兼容失败时按设置回退浏览器。"
    : PLATFORM === "instagram"
    ? "Instagram 默认使用 Mobile v1 / Web API 直连搜索标签与关键词、读取创作者主页与 Reels、通过 stream_comments 抓取评论与回复，并按序号保存单帖或混合轮播原图/视频；普通兼容失败时自动回退独立浏览器。"
    : PLATFORM === "twitter"
    ? "Twitter 默认结合轻量 Syndication/REST 与独立浏览器拦截 GraphQL 时间线，支持关键词搜索、创作者主页、本账号推文及回复线程采集，并自动选取最高码率 MP4 与 ?name=orig 原图下载。"
    : xhs
    ? "小红书采集复用所选账号的可见浏览器或显式 API 读取模式；最新、类型、时间和数据门槛会在入库前复核；任务串行执行，可随时取消，已入库结果会保留。"
    : PLATFORM === "kuaishou"
      ? "快手采集复用所选账号的登录浏览器，拦截页面签名后的搜索、主页和评论响应；类型、时间和数据门槛会在入库前复核。"
      : "抖音采集会临时打开可见浏览器窗口；平台筛选后还会在本地复核类型、时间和数据门槛；任务串行执行，可随时取消，已入库结果会保留。";
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
  const sourceType = collectionSourceValue();
  const board = $("col-board");
  if (sourceType === "creator") {
    const normalizedCreator = await normalizeCollectionCreatorTarget(null, { quiet: true });
    if (normalizedCreator === null) return;
  }
  const sourceRef = sourceType === "favorite_board" ? (board?.value || "") : sourceType === "creator" ? ($("col-source-ref")?.value.trim() || "") : "";
  const sourceName = sourceType === "account_works" ? "本账号作品" : sourceType === "favorite_all" ? "全部收藏" : sourceType === "favorite_board" ? (board?.selectedOptions?.[0]?.textContent || "").replace(/（[\d,.万亿]+）$/, "") : sourceType === "creator" ? `创作者 ${sourceRef}` : "";
  const requiresAccount = collectionRequiresAccount(PLATFORM, sourceType);
  const accountId = requiresAccount || PLATFORM !== "shipinhao" ? Number($("col-account").value || 0) : 0;
  const contentLimit = Number($("col-content-limit").value || 0);
  const commentLimit = Number($("col-comment-limit").value || 0);
  const pageLimit = Number($("col-page-limit").value || 0);
  const stagnantPages = Number($("col-stagnant-pages").value || 0);
  const minLikes = Number($("col-min-likes").value || 0);
  const minComments = Number($("col-min-comments").value || 0);
  let valid = true;
  valid = setFieldError($("col-keywords"), sourceType === "keyword" && !keywords.length ? "请至少填写一个关键词" : keywords.length > 20 ? "单个任务最多 20 个关键词" : "") && valid;
  if (sourceType === "favorite_board") valid = setFieldError(board, !sourceRef ? "请选择收藏夹" : "") && valid;
  if (sourceType === "creator") valid = setFieldError($("col-source-ref"), !sourceRef ? "请填写创作者用户 ID" : "") && valid;
  valid = requiresAccount
    ? setFieldError($("col-account"), !accountId ? "请选择一个已登录账号" : "") && valid
    : setFieldError($("col-account"), "") && valid;
  valid = setFieldError($("col-content-limit"), contentLimit < 1 || contentLimit > 100 ? "请输入 1–100" : "") && valid;
  valid = setFieldError($("col-comment-limit"), commentLimit < 0 || commentLimit > 200 ? "请输入 0–200" : "") && valid;
  valid = setFieldError($("col-page-limit"), pageLimit < 1 || pageLimit > 40 ? "请输入 1–40" : "") && valid;
  valid = setFieldError($("col-stagnant-pages"), stagnantPages < 1 || stagnantPages > 8 ? "请输入 1–8" : "") && valid;
  valid = setFieldError($("col-min-likes"), minLikes < 0 ? "请输入非负整数" : "") && valid;
  valid = setFieldError($("col-min-comments"), minComments < 0 ? "请输入非负整数" : "") && valid;
  if (!valid) {
    const form = $("col-keywords").closest(".card");
    const first = form?.querySelector('[aria-invalid="true"]');
    if (first) first.focus();
    return;
  }
  const btn = evtBtn();
  await withBusy(btn, "创建中", async () => {
    try {
      const job = await api("/api/collections", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          platform: PLATFORM, account_id: accountId, keywords,
          source_type: sourceType, source_ref: sourceRef, source_name: sourceName,
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
      if (sourceType === "keyword") $("col-keywords").value = "";
      toast(`${sourceType === "account_works" ? "本账号作品" : sourceType === "keyword" ? "关键词" : sourceType === "creator" ? "创作者" : "收藏"}采集任务已创建`, "ok");
      globalThis.CreatorHubWorkbench?.completed?.("collections");
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
    <div class="empty-t">还没有采集任务</div><div class="empty-sub">可按平台支持的来源创建作品采集任务。</div><button class="ghost sm" data-open-composer="collections">新建采集任务</button></div>`;
}
function collectionProgress(job) {
  if (job.status === "done") return 100;
  const totalKeywords = Math.max(0, Number(job.total_keyword_count || 0));
  if (!totalKeywords) return 0;
  const processedKeywords = Math.min(totalKeywords, Math.max(0, Number(job.processed_keyword_count || 0)));
  const itemTotal = Math.max(0, Number(job.current_item_total || 0));
  const itemIndex = Math.min(itemTotal, Math.max(0, Number(job.current_item_index || 0)));
  const currentFraction = itemTotal && processedKeywords < totalKeywords ? itemIndex / itemTotal : 0;
  return Math.min(100, Math.round((processedKeywords + currentFraction) * 100 / totalKeywords));
}
function openCollectionVerifyBrowser(jobId) {
  const job = COLLECTION_JOBS.find(item => item.id === Number(jobId));
  if (!job || !job.account_id) return;
  let targetUrl = "";
  if (job.platform === "douyin" && job.source_type === "keyword") {
    const kw = String(job.current_keyword || (job.keywords && job.keywords[0]) || "").trim();
    if (kw) targetUrl = "https://www.douyin.com/search/" + encodeURIComponent(kw);
  }
  openAccountBrowser(job.account_id, targetUrl);
}
let COLLECTION_RENDERED_JOB_ID = 0;
let COLLECTION_RENDERED_SIG = "";
function collectionJobResultSig(job, page = COLLECTION_PAGE) {
  if (!job) return "";
  return [
    job.id,
    Math.max(1, Number(page) || 1),
    job.status,
    Number(job.content_count || 0),
    Number(job.comment_count || 0),
    Number(job.error_count || 0),
    Number(job.current_item_index || 0),
  ].join(":");
}
function renderCollectionJobs() {
  const body = $("collection-job-table"); if (!body) return;
  const html = COLLECTION_JOBS.map(job => {
    const status = collectionStatus(job.status);
    const percent = collectionProgress(job);
    const sourceLabel = job.source_type === "keyword" ? "关键词" : "来源";
    const keywordProgress = `${Number(job.processed_keyword_count || 0)}/${Number(job.total_keyword_count || 0)} ${sourceLabel}`;
    const itemProgress = Number(job.current_item_total || 0) > 0
      ? ` · 当前作品 ${Number(job.current_item_index || 0)}/${Number(job.current_item_total || 0)}` : "";
    const sourceValues = job.source_type === "keyword" ? (job.keywords || []) : [job.source_name || (job.source_type === "account_works" ? "本账号作品" : job.source_type === "favorite_all" ? "全部收藏" : "收藏夹")];
    const keywords = sourceValues.slice(0, 5).map(k => `<span class="meta-chip">${esc(k)}</span>`).join("") +
      (sourceValues.length > 5 ? `<span class="meta-chip more">+${sourceValues.length - 5}</span>` : "");
    const canCancel = ["pending", "running"].includes(job.status);
    const canRetry = ["done", "partial", "failed", "canceled"].includes(job.status);
    const canEdit = canRetry && ["douyin", "xhs", "rednote", "kuaishou", "shipinhao", "tiktok", "bilibili", "instagram", "twitter"].includes(job.platform);
    const needsManualClear = !!(job.manual_review_required && Number(job.account_id) > 0);
    const errorText = collectionLastError(job);
    const sortLabel = { general: "综合", latest: "最新", most_liked: "最多点赞" }[job.search_sort] || "综合";
    const timeLabel = { all: "不限时间", day: "一天内", week: "一周内", half_year: "半年内" }[job.publish_time] || "不限时间";
    const typeLabel = { all: "全部类型", video: "视频", images: "图文" }[job.content_type] || "全部类型";
    const threshold = [Number(job.min_likes) > 0 ? `≥${fmtNum(job.min_likes)} 赞` : "", Number(job.min_comments) > 0 ? `≥${fmtNum(job.min_comments)} 评` : ""].filter(Boolean).join(" · ");
    return `<article class="collection-task" role="listitem" aria-label="任务 ${job.id}，${status.label}">
      <div class="collection-task-meta"><div><span class="collection-task-label">任务状态</span><span class="pill ${status.cls}">${status.label}</span></div><time class="collection-task-created" datetime="${esc(job.created_at || "")}">${collectionDate(job.created_at)}</time></div>
      <div class="collection-task-keywords"><span class="collection-task-label">${sourceLabel}</span><div class="keyword-stack">${keywords}</div>${job.current_keyword ? `<div class="collection-step">当前：${esc(job.current_keyword)}</div>` : ""}</div>
      <div class="collection-task-config"><span class="collection-task-label">采集配置</span><div class="collection-task-config-main">${job.max_contents_per_keyword} 作品/${job.source_type === "keyword" ? "词" : "来源"} · ${job.max_pages_per_keyword || 12} 深度页${job.source_type === "keyword" ? ` · ${sortLabel}` : ""}</div><div class="collection-task-config-sub">${timeLabel} · ${typeLabel}${threshold ? ` · ${threshold}` : ""} · ${job.max_comments_per_content} 评论/作品<br>${job.include_replies ? "含二级评论 · " : ""}${job.download_media ? "下载媒体" : "仅采数据"} · 连续 ${job.stagnant_pages || 3} 页无新增停止</div></div>
      <div class="collection-task-progress"><span class="collection-task-label">执行进度</span><div class="job-progress"><div class="job-progress-head"><span>${esc(job.current_step || "等待执行")}</span><b>${percent}%</b></div><div class="progress-track" role="progressbar" aria-label="任务 ${job.id} 进度" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${percent}"><div class="progress-fill" style="width:${percent}%"></div></div><div class="collection-step">${keywordProgress}${itemProgress} · 已采 ${fmtNum(job.content_count)} 作品 / ${fmtNum(job.comment_count)} 评论</div></div></div>
      ${job.error_count ? `<button type="button" class="collection-task-error" onclick="openCollectionResults(${job.id})" title="${esc(errorText)}">${ic("i-info")}<span class="collection-task-error-text">${job.error_count} 条异常 · ${esc(errorText)}</span><span class="collection-task-error-link">查看详情</span></button>` : ""}
      <div class="collection-task-actions" aria-label="任务 ${job.id} 操作">
        <button type="button" class="sm collection-task-primary" onclick="openCollectionResults(${job.id})">${ic("i-eye")}查看结果</button>
        ${needsManualClear ? `<button type="button" class="ghost sm" onclick="openCollectionVerifyBrowser(${job.id})" title="直接打开对应搜索/站点页面检查并完成验证">打开验证页</button><button type="button" class="ghost sm" onclick="clearRiskAccount(${job.account_id})" title="确认验证已完成或无需验证后，解除风控并立即恢复任务">解除风控继续</button>` : ""}
        ${canEdit ? `<button type="button" class="ghost sm" onclick="editCollection(${job.id})">${ic("i-settings")}编辑</button>` : ""}
        <button type="button" class="ghost sm" onclick="exportCollection(${job.id})"${job.content_count ? "" : " disabled"}>${ic("i-download")}导出</button>
        ${canCancel ? `<button type="button" class="ghost sm" onclick="cancelCollection(${job.id})">${ic("i-x")}取消任务</button>` : ""}
        ${canRetry ? `<button type="button" class="ghost sm" onclick="retryCollection(${job.id})">${ic("i-play")}续跑</button>` : ""}
        ${job.status !== "running" ? `<button type="button" class="ghost sm danger collection-task-delete" onclick="deleteCollection(${job.id})" aria-label="删除任务 ${job.id}">${ic("i-trash")}删除</button>` : ""}
      </div>
    </article>`;
  }).join("") || collectionTaskEmpty();
  setHtmlIfChanged(body, html);
}
async function refreshCollections() {
  const isCurrent = beginViewRequest("collections");
  if (!$("collection-job-table") || !["douyin", "xhs", "rednote", "kuaishou", "shipinhao", "tiktok", "bilibili", "instagram", "twitter"].includes(PLATFORM)) return;
  try {
    const requestedPlatform = PLATFORM;
    const jobs = await api("/api/collections?platform=" + encodeURIComponent(requestedPlatform));
    if (!isCurrent()) return;
    // Keep platform data isolated even if an older backend ignores the filter.
    COLLECTION_JOBS = (Array.isArray(jobs) ? jobs : [])
      .filter(job => job.platform === requestedPlatform);
    const active = COLLECTION_JOBS.filter(j => ["pending", "running"].includes(j.status)).length;
    if ($("tb-col")) $("tb-col").textContent = active || COLLECTION_JOBS.length;
    renderCollectionJobs();
    if (COLLECTION_JOB_ID) {
      const job = COLLECTION_JOBS.find(j => j.id === COLLECTION_JOB_ID);
      if (job) {
        updateCollectionResultStats(job);
        const nextSig = collectionJobResultSig(job, COLLECTION_PAGE);
        if (CURRENT_TAB === "collections" && nextSig !== COLLECTION_RENDERED_SIG) {
          await loadCollectionContents(COLLECTION_PAGE, true);
        }
      }
      else closeCollectionResults();
    }
  } catch (e) {
    if (isCurrent() && CURRENT_TAB === "collections") toast("采集任务刷新失败：" + e.message, "err");
  }
}
async function editCollection(jobId, draft = null) {
  const job = COLLECTION_JOBS.find(item => item.id === Number(jobId));
  if (!job) return;
  const platformName = PF_NAME[job.platform] || "平台";
  const accounts = ACCOUNTS.filter(a => a.platform === job.platform && a.status !== "invalid" && a.has_storage);
  const initial = draft || {
    account_id: job.account_id,
    source_type: job.source_type || "keyword",
    source_ref: job.source_ref || "",
    source_name: job.source_name || "",
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
      <fieldset class="ui-form-group"><legend>采集范围</legend>
      ${initial.source_type === "keyword" ? `<div class="form-field"><label for="ecol-keywords">关键词 <span class="field-scope">最多 20 个</span></label>
        <textarea id="ecol-keywords" rows="5" placeholder="每行一个关键词">${esc(initial.keywords)}</textarea></div>` : `<div class="form-note"><svg aria-hidden="true"><use href="#i-library"/></svg><span>采集来源：<b>${esc(initial.source_name || (initial.source_type === "account_works" ? "本账号作品" : initial.source_type === "favorite_all" ? "全部收藏" : "收藏夹"))}</b>。来源不可在续跑时切换，可新建任务选择其他来源。</span></div>`}
      <div class="form-grid">
        ${collectionRequiresAccount(job.platform, initial.source_type) || job.platform !== "shipinhao"
          ? `<div class="form-field"><label for="ecol-account">使用账号</label><select id="ecol-account">${accOptions(accounts, !collectionRequiresAccount(job.platform, initial.source_type) ? "使用本机微信通道（无需网页账号）" : (accounts.length ? `请选择${platformName}账号` : `暂无可用${platformName}账号`))}</select></div>`
          : ""}
        ${job.platform === "douyin" ? '<div class="form-field"><label for="ecol-quality">视频画质</label><select id="ecol-quality"><option value="highest">原画 / 最高</option><option value="1080">1080P</option><option value="720">720P</option><option value="540">540P</option><option value="lowest">最低省流</option></select></div>' : ""}
        <div class="form-field"><label for="ecol-content-limit">每词作品上限</label><input id="ecol-content-limit" type="number" min="1" max="100" value="${Number(initial.max_contents_per_keyword) || 20}"></div>
        <div class="form-field"><label for="ecol-comment-limit">每作品评论上限</label><input id="ecol-comment-limit" type="number" min="0" max="200" value="${Number(initial.max_comments_per_content) || 0}"></div>
      </div>
      </fieldset>
      <fieldset class="ui-form-group"><legend>搜索与停止条件</legend>
      <div class="form-grid collection-filter-grid">
        <div class="form-field"><label for="ecol-page-limit">每词采集深度</label><input id="ecol-page-limit" type="number" min="1" max="40" value="${Number(initial.max_pages_per_keyword) || 12}"></div>
        <div class="form-field"><label for="ecol-sort">搜索排序</label><select id="ecol-sort"><option value="general">综合排序</option><option value="latest">最新发布</option><option value="most_liked">最多点赞</option></select></div>
        <div class="form-field"><label for="ecol-publish-time">发布时间</label><select id="ecol-publish-time"><option value="all">不限</option><option value="day">一天内</option><option value="week">一周内</option><option value="half_year">半年内</option></select></div>
        <div class="form-field"><label for="ecol-content-type">内容类型</label><select id="ecol-content-type"><option value="all">全部作品</option><option value="video">视频</option><option value="images">图文 / 图集</option></select></div>
        <div class="form-field"><label for="ecol-min-likes">最低点赞数</label><input id="ecol-min-likes" type="number" min="0" value="${Number(initial.min_likes) || 0}"></div>
        <div class="form-field"><label for="ecol-min-comments">最低评论数</label><input id="ecol-min-comments" type="number" min="0" value="${Number(initial.min_comments) || 0}"></div>
        <div class="form-field"><label for="ecol-stagnant-pages">连续无新增停止</label><input id="ecol-stagnant-pages" type="number" min="1" max="8" value="${Number(initial.stagnant_pages) || 3}"></div>
      </div>
      </fieldset>
      <fieldset class="ui-form-group"><legend>评论与下载</legend>
      <div class="option-grid" aria-label="采集选项">
        <label class="switch-row"><input type="checkbox" id="ecol-download"${initial.download_media ? " checked" : ""} onchange="$('ecol-dir-wrap').style.display=this.checked?'':'none'"><span class="switch-copy"><b>下载媒体</b><span>保存视频和封面来源</span></span></label>
        <label class="switch-row"><input type="checkbox" id="ecol-replies"${initial.include_replies ? " checked" : ""}><span class="switch-copy"><b>包含二级评论</b><span>采集${platformName}当前可返回的回复</span></span></label>
      </div>
      <div class="form-field" id="ecol-dir-wrap" style="display:${initial.download_media ? "" : "none"}"><label for="ecol-download-dir">下载目录（可选）</label><input id="ecol-download-dir" value="${esc(initial.download_dir)}" placeholder="留空使用默认目录"></div>
      </fieldset>`;
    if ($("ecol-account")) $("ecol-account").value = String(initial.account_id || "");
    if ($("ecol-quality")) $("ecol-quality").value = initial.video_quality || "highest";
    $("ecol-sort").value = initial.search_sort || "general";
    $("ecol-publish-time").value = initial.publish_time || "all";
    $("ecol-content-type").value = initial.content_type || "all";
    enhanceAllSelects($("ui-body")); csSyncAll();
    _uiGetVal = () => ({
      account_id: $("ecol-account") ? Number($("ecol-account").value || 0) : 0,
      source_type: initial.source_type,
      source_ref: initial.source_ref,
      source_name: initial.source_name,
      keywords: $("ecol-keywords") ? $("ecol-keywords").value : "",
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
      video_quality: $("ecol-quality") ? ($("ecol-quality").value || "highest") : "highest",
      download_dir: $("ecol-download-dir").value.trim(),
    });
    _uiOpen(`编辑采集任务 #${job.id}`, "已有作品和评论会保留。保存后点击「续跑」应用新配置，系统会自动去重。", {
      okText: "保存配置", wide: true, icon: "i-library",
      submit: value => {
        const keywords = parseCollectionKeywords(value.keywords);
        if (value.source_type === "keyword" && !keywords.length) uiEditorError("请至少填写一个关键词", "ecol-keywords");
        if (keywords.length > 20) uiEditorError("单个任务最多 20 个关键词", "ecol-keywords");
        if (collectionRequiresAccount(job.platform, value.source_type) && !value.account_id) uiEditorError(`请选择一个可用${platformName}账号`, "ecol-account");
        if (value.max_contents_per_keyword < 1 || value.max_contents_per_keyword > 100) uiEditorError("每词作品上限须为 1–100", "ecol-content-limit");
        if (value.max_pages_per_keyword < 1 || value.max_pages_per_keyword > 40) uiEditorError("每词采集深度须为 1–40 页", "ecol-page-limit");
        if (value.stagnant_pages < 1 || value.stagnant_pages > 8) uiEditorError("连续无新增停止阈值须为 1–8 页", "ecol-stagnant-pages");
        if (value.min_likes < 0 || value.min_comments < 0) uiEditorError("点赞和评论门槛须为非负整数", "ecol-min-likes");
        if (value.max_comments_per_content < 0 || value.max_comments_per_content > 200) uiEditorError("每作品评论上限须为 0–200", "ecol-comment-limit");
        return api(`/api/collections/${job.id}`, {
          method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ...value, platform: job.platform, keywords }),
        });
      },
    });
  });
  if (value === null) return;
  toast("任务配置已保存，点击“续跑”后生效", "ok"); refreshCollections();
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
  $("collection-results-sub").textContent = `${job.source_type === "keyword" ? (job.keywords || []).join("、") : (job.source_name || "收藏来源")} · ${collectionStatus(job.status).label}`;
}
async function openCollectionResults(jobId) {
  const btn = evtBtn();
  const nextJobId = Number(jobId);
  const sameRenderedJob = COLLECTION_RENDERED_JOB_ID === nextJobId;
  COLLECTION_JOB_ID = nextJobId; COLLECTION_PAGE = 1;
  window.dispatchEvent(new Event("creatorhub:agent-context"));
  const job = COLLECTION_JOBS.find(j => j.id === COLLECTION_JOB_ID);
  if (job) updateCollectionResultStats(job);
  $("collection-results-card").style.display = "";
  globalThis.CreatorHubWorkbench?.collectionDetail?.(true);
  const targetSig = collectionJobResultSig(job, 1);
  const hasCachedDom = sameRenderedJob && targetSig && targetSig === COLLECTION_RENDERED_SIG
    && Boolean($("collection-content-list")?.querySelector(".collection-result-item"));
  if (hasCachedDom) {
    const reducedMotion = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    $("collection-results-card").scrollIntoView({ behavior: reducedMotion ? "auto" : "smooth", block: "start" });
    return;
  }
  await withBusy(btn, "加载中", async () => {
    await loadCollectionContents(1, sameRenderedJob);
    const reducedMotion = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    $("collection-results-card").scrollIntoView({ behavior: reducedMotion ? "auto" : "smooth", block: "start" });
  });
}
function closeCollectionResults() {
  COLLECTION_JOB_ID = 0; COLLECTION_PAGE = 1;
  window.dispatchEvent(new Event("creatorhub:agent-context"));
  if ($("collection-results-card")) $("collection-results-card").style.display = "none";
  globalThis.CreatorHubWorkbench?.collectionDetail?.(false);
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
  const isTextOnly = item.platform === "twitter" && !item.media_count && !item.cover_url && item.media_type !== "video";
  const typeLabel = isTextOnly ? "纯文本" : (isGallery ? `图集${item.media_count > 1 ? ` · ${item.media_count} 张` : ""}` : "视频");
  const canPreview = Boolean(item.preview_available);
  const file = collectionFileDisplay(item);
  const fileExists = Boolean(item.local_exists);
  const downloadClass = fileExists ? "done" : (!isTextOnly && item.download_status === "failed") ? "failed" : "skipped";
  const downloadLabel = fileExists ? "已下载" : isTextOnly ? "纯文字推文" : item.download_status === "failed" ? "下载失败" : item.download_status === "done" ? "文件缺失" : "未下载";
  const byline = [item.author_name || "未知作者", item.create_time ? fmtTime(item.create_time) : "", item.aweme_id || ""].filter(Boolean);
  const authorId = String(item.author_id || "").trim();
  const authorRow = authorId
    ? `<div class="collection-author-row"><button type="button" class="collection-author-chip" data-author-id="${esc(authorId)}" onclick="copyCollectionAuthorId(this)" title="点击复制创作者 ID：${esc(authorId)}" aria-label="复制创作者 ID：${esc(authorId)}"><span>创作者 ID: ${esc(authorId)}</span>${ic("i-copy")}</button></div>`
    : "";
  const cover = item.cover_url
    ? `<img src="${esc(safeMediaUrl(item.cover_url))}" alt="${esc(title.slice(0, 60))}封面" loading="lazy" referrerpolicy="no-referrer">`
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
  </div>` : `<div class="collection-download-note${(!isTextOnly && item.download_status === "failed") ? " failed" : ""}">${isTextOnly ? "纯文字推文（无图片/视频附件），已完整采集正文与评论" : (item.error ? esc(item.error) : "本地文件尚不可用，可先预览平台媒体或打开原作品")}</div>`;
  return `<article class="collection-result-item">
    <button type="button" class="collection-result-cover" onclick="openCollectionPreview(${item.job_id},${item.id})" aria-label="预览：${esc(title.slice(0, 80))}"${canPreview ? "" : " disabled"}>
      ${cover}<span class="collection-cover-type">${esc(typeLabel)}</span>
      <span class="collection-cover-preview">${ic("i-play")}${canPreview ? "站内预览" : "暂无预览"}</span>
    </button>
    <div class="collection-result-body">
      <div class="collection-result-tags"><span class="meta-chip group">#${esc(item.keyword)}</span><span class="pill ${downloadClass}" title="${esc(item.error || "")}">${downloadLabel}</span></div>
      <a class="collection-result-title" href="${esc(item.url)}" target="_blank" rel="noopener noreferrer" title="${esc(title)}">${esc(title)}</a>
      <div class="collection-result-byline">${byline.map((part, index) => `<span${index === byline.length - 1 ? ' class="collection-result-id"' : ""}>${esc(part)}</span>`).join("<span>·</span>")}</div>
      ${authorRow}
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
function copyCollectionAuthorId(button) {
  return copyTextValue(
    button?.dataset?.authorId || "",
    "创作者 ID 已复制，可用于创作者采集或作品监控",
    "复制失败，请手动复制创作者 ID",
  );
}
function copyCollectionPath(button) {
  return copyTextValue(
    button?.dataset?.path || "",
    "本地路径已复制",
    "复制失败，请手动复制路径",
  );
}
async function loadCollectionContents(page = COLLECTION_PAGE, quiet = false) {
  if (!COLLECTION_JOB_ID) return;
  const targetJobId = COLLECTION_JOB_ID;
  COLLECTION_PAGE = Math.max(1, Number(page) || 1);
  const isCurrent = beginViewRequest("collection-contents", () => `${COLLECTION_JOB_ID}:${COLLECTION_PAGE}`);
  const body = $("collection-content-list");
  if (!quiet) setHtmlIfChanged(body, collectionResultSkeleton(4));
  try {
    const result = await api(`/api/collections/${targetJobId}/contents?page=${COLLECTION_PAGE}&page_size=20`);
    if (!isCurrent()) return;
    COLLECTION_PAGE = result.page;
    const html = (result.items || []).map(collectionResultCard).join("") || collectionEmpty("任务暂时没有作品结果", "采集中可稍后刷新；失败任务可查看错误并续跑");
    setHtmlIfChanged(body, html);
    const job = COLLECTION_JOBS.find(j => j.id === targetJobId);
    COLLECTION_RENDERED_JOB_ID = targetJobId;
    COLLECTION_RENDERED_SIG = collectionJobResultSig(job, COLLECTION_PAGE);
    $("collection-result-count").textContent = `共 ${fmtNum(result.total)} 个作品`;
    const pager = $("collection-pager");
    setHtmlIfChanged(pager, `<button class="ghost sm" onclick="loadCollectionContents(${result.page - 1})"${result.page <= 1 ? " disabled" : ""}>${ic("i-prev")}上一页</button><span class="mut">第 ${result.page} / ${result.pages} 页 · 共 ${fmtNum(result.total)} 条</span><button class="ghost sm" onclick="loadCollectionContents(${result.page + 1})"${result.page >= result.pages ? " disabled" : ""}>下一页${ic("i-next")}</button>`);
    pager.hidden = result.pages <= 1;
  } catch (e) {
    if (!isCurrent()) return;
    COLLECTION_RENDERED_SIG = "";
    setHtmlIfChanged(body, collectionEmpty("结果加载失败", e.message));
    $("collection-result-count").textContent = "";
  }
}
function collectionCommentParams({ exportMode = false } = {}) {
  const params = new URLSearchParams();
  const scope = $("collection-comments-scope");
  const contentId = scope ? String(scope.value || "") : String(COLLECTION_COMMENT_CONTENT_ID || "");
  const q = $("collection-comments-q")?.value.trim() || "";
  const region = $("collection-comments-region")?.value.trim() || "";
  const secUid = $("collection-comments-secuid")?.value.trim() || "";
  const dedupe = Boolean($("collection-comments-dedupe")?.checked);
  if (contentId) params.set("content_id", contentId);
  if (q) params.set("q", q);
  if (region) params.set("ip_label", region);
  if (secUid) params.set("user_sec_uid", secUid);
  if (dedupe) params.set("dedupe_users", "true");
  if (!exportMode) {
    params.set("paginate", "true");
    params.set("page", String(COLLECTION_COMMENT_PAGE));
    params.set("page_size", "40");
  }
  return params;
}
function collectionCommentMarkup(comment) {
  const source = [comment.source_keyword ? `#${comment.source_keyword}` : "", comment.source_desc || ""]
    .filter(Boolean).join(" · ");
  return `<article class="collection-comment"><div class="collection-comment-head"><div><b>${esc(comment.user_nickname || "匿名用户")}</b>${comment.ip_label ? ` · ${esc(comment.ip_label)}` : ""}${comment.user_sec_uid ? `<div class="comment-user-sec" title="${esc(comment.user_sec_uid)}">sec_uid: ${esc(comment.user_sec_uid)}</div>` : ""}${source ? `<div class="collection-comment-source">${esc(source)}</div>` : ""}</div><span>${fmtTime(comment.create_time)} · ${fmtNum(comment.like_count)} 赞${comment.reply_to ? " · 回复" : ""}</span></div><p>${esc(comment.text || "（空评论）")}</p></article>`;
}
async function loadCollectionComments(page = COLLECTION_COMMENT_PAGE) {
  const modal = $("collection-comments-modal");
  const list = $("collection-comments-list");
  if (!modal || !list || !COLLECTION_JOB_ID) return;
  COLLECTION_COMMENT_PAGE = Math.max(1, Number(page) || 1);
  const request = ++COLLECTION_COMMENT_REQUEST;
  list.innerHTML = `<div class="empty"><div class="empty-t">加载中…</div></div>`;
  try {
    const result = await api(`/api/collections/${COLLECTION_JOB_ID}/comments?${collectionCommentParams().toString()}`);
    if (request !== COLLECTION_COMMENT_REQUEST || modal.style.display === "none") return;
    const payload = Array.isArray(result)
      ? { items: result, total: result.length, page: 1, pages: 1, has_prev: false, has_next: false }
      : result;
    COLLECTION_COMMENT_PAGE = Number(payload.page || 1);
    $("collection-comments-count").textContent = `共 ${fmtNum(payload.total || 0)} 条符合条件的评论`;
    list.innerHTML = (payload.items || []).map(collectionCommentMarkup).join("") || `<div class="empty"><div class="empty-t">没有符合条件的评论</div><div class="empty-sub">调整筛选条件后重试</div></div>`;
    const pager = $("collection-comments-pager");
    if (pager) {
      pager.hidden = Number(payload.pages || 1) <= 1;
      pager.innerHTML = `<button type="button" class="ghost sm" onclick="loadCollectionComments(${Math.max(1, Number(payload.page || 1) - 1)})"${payload.has_prev ? "" : " disabled"}>${ic("i-prev")}上一页</button><span class="mut">第 ${payload.page || 1} / ${payload.pages || 1} 页</span><button type="button" class="ghost sm" onclick="loadCollectionComments(${Number(payload.page || 1) + 1})"${payload.has_next ? "" : " disabled"}>下一页${ic("i-next")}</button>`;
    }
  } catch (e) {
    if (request !== COLLECTION_COMMENT_REQUEST) return;
    list.innerHTML = `<div class="empty"><div class="empty-t">加载失败</div><div class="empty-sub">${esc(e.message)}</div></div>`;
  }
}
function scheduleCollectionCommentFilter() {
  const scope = $("collection-comments-scope");
  COLLECTION_COMMENT_CONTENT_ID = Number(scope?.value || 0);
  COLLECTION_COMMENT_PAGE = 1;
  clearTimeout(COLLECTION_COMMENT_TIMER);
  COLLECTION_COMMENT_TIMER = setTimeout(() => loadCollectionComments(1), 220);
}
function resetCollectionCommentFilters({ reload = true } = {}) {
  ["collection-comments-q", "collection-comments-region", "collection-comments-secuid"].forEach(id => { const el = $(id); if (el) el.value = ""; });
  const dedupe = $("collection-comments-dedupe");
  if (dedupe) dedupe.checked = false;
  const scope = $("collection-comments-scope");
  if (scope) scope.value = String(COLLECTION_COMMENT_CONTENT_ID || "");
  COLLECTION_COMMENT_PAGE = 1;
  if (reload) loadCollectionComments(1);
}
async function showCollectionComments(contentId) {
  const modal = $("collection-comments-modal");
  const scope = $("collection-comments-scope");
  COLLECTION_COMMENT_CONTENT_ID = Number(contentId) || 0;
  if (scope) {
    scope.innerHTML = `<option value="">全部作品</option><option value="${COLLECTION_COMMENT_CONTENT_ID}">当前作品 #${COLLECTION_COMMENT_CONTENT_ID}</option>`;
    scope.value = String(COLLECTION_COMMENT_CONTENT_ID || "");
  }
  resetCollectionCommentFilters({ reload: false });
  modal.style.display = "flex"; modalOpened(modal);
  setTimeout(() => modal.querySelector(".pv-close")?.focus(), 0);
  await loadCollectionComments(1);
}
async function exportFilteredCollectionComments(button) {
  if (!COLLECTION_JOB_ID) return;
  await withBusy(button, "导出中", async () => {
    try {
      const params = collectionCommentParams({ exportMode: true });
      await _downloadExcelReport(`/api/collections/${COLLECTION_JOB_ID}/export.xlsx?${params.toString()}`, `keyword-collection-${COLLECTION_JOB_ID}-filtered.xlsx`);
      toast("当前筛选结果已导出", "ok");
    } catch (e) { toast("导出失败：" + e.message, "err"); }
  });
}
function hideCollectionComments() {
  const modal = $("collection-comments-modal");
  ++COLLECTION_COMMENT_REQUEST;
  clearTimeout(COLLECTION_COMMENT_TIMER);
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
    try { await api(`/api/collections/${jobId}`, { method: "DELETE" }); if (COLLECTION_RENDERED_JOB_ID === Number(jobId)) { COLLECTION_RENDERED_JOB_ID = 0; COLLECTION_RENDERED_SIG = ""; } if (COLLECTION_JOB_ID === jobId) closeCollectionResults(); toast("任务记录已删除，本地文件已保留", "ok"); await refreshCollections(); }
    catch (e) { toast("删除失败：" + e.message, "err"); }
  });
}
function exportCollection(jobId) {
  const link = document.createElement("a");
  link.href = `/api/collections/${jobId}/export.xlsx`;
  link.download = `keyword-collection-${jobId}.xlsx`;
  document.body.appendChild(link); link.click(); link.remove();
}

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

// 15-publish-repost.js — 多平台图文/视频发布编辑器、已发布作品管理与跨平台转发。
// ─── 多平台发布 ───
const BILI_PARTITION_CACHE = new Map();
function onBiliCopyrightChange() {
  const wrap = $("pub-bili-source-wrap");
  if (wrap) wrap.style.display = $("pub-bili-copyright")?.value === "2" ? "" : "none";
}
async function loadBiliPartitions(accountId) {
  const sel = $("pub-bili-tid");
  if (!sel || !accountId || PLATFORM !== "bilibili") return;
  const current = sel.value || "21";
  let data = BILI_PARTITION_CACHE.get(Number(accountId));
  if (!data) {
    try {
      data = await api("/api/accounts/" + Number(accountId) + "/bili/partitions");
      if (data && Array.isArray(data.partitions) && data.partitions.length) {
        BILI_PARTITION_CACHE.set(Number(accountId), data);
      }
    } catch (_) { return; }
  }
  const groups = Array.isArray(data?.partitions) ? data.partitions : [];
  const opts = [];
  for (const g of groups) {
    const pName = String(g.name || "");
    const children = Array.isArray(g.children) && g.children.length ? g.children : [g];
    for (const c of children) {
      const tid = Number(c.tid || c.id || 0);
      if (!tid) continue;
      const cName = String(c.name || "");
      opts.push({ tid: String(tid), label: pName && pName !== cName ? `${pName} · ${cName}` : cName });
    }
  }
  if (!opts.length) return;
  sel.innerHTML = opts.map(o => `<option value="${esc(o.tid)}">${esc(o.label)}</option>`).join("");
  sel.value = opts.some(o => o.tid === String(current)) ? String(current) : opts[0].tid;
  sel._csSync?.();
}
function populatePubAcc() {
  const sel = $("pub-acc"); if (!sel) return;
  // 小红书发布需创作者号;其余平台有登录态即可(走浏览器自动化)
  const list = PLATFORM === "xhs" ? ACCOUNTS.filter(a => a.has_creator) : ACCOUNTS;
  const ph = list.length ? "选择发布账号"
    : (PLATFORM === "kuaishou" ? "请先完成「快手扫码登录」"
      : PLATFORM === "douyin" ? "请先完成「抖音扫码/创作者登录」"
      : PLATFORM === "tiktok" ? "请先完成「TikTok 登录」"
      : PLATFORM === "bilibili" ? "请先完成「哔哩哔哩扫码登录」"
      : PLATFORM === "instagram" ? "请先完成「Instagram 登录」"
      : PLATFORM === "twitter" ? "请先完成「Twitter 登录」"
      : PLATFORM === "rednote" ? "请先完成「海外小红书创作者登录」"
      : "请先完成「小红书创作者登录」");
  sel.innerHTML = accOptions(list, ph);
  if (list.length) sel.value = String(list[0].id);
  if (!sel._biliBound) {
    sel._biliBound = true;
    sel.addEventListener("change", () => { if (PLATFORM === "bilibili" && sel.value) loadBiliPartitions(sel.value); });
  }
  if (PLATFORM === "bilibili" && sel.value) loadBiliPartitions(sel.value);
}
let pubFilesDT = new DataTransfer();
function onPubType() {
  const v = $("pub-type").value, inp = $("pub-files"), lbl = $("pub-files-label");
  if (!inp) return;
  const ig = PLATFORM === "instagram", tw = PLATFORM === "twitter";
  if (v === "video") {
    inp.accept = ig ? "video/*,image/*" : "video/*";
    inp.multiple = ig;
    lbl.textContent = ig ? "选择视频文件（单个 Reels/Story，或多选最多 10 项混合轮播）" : tw ? "选择视频或 GIF（单个，留空可发纯文本推文）" : "选择视频文件(单个)";
  } else {
    inp.accept = ig ? "image/*,video/*" : "image/*";
    inp.multiple = true;
    lbl.textContent = ig ? "选择图片/视频（可多选，最多 10 项轮播）" : tw ? "选择图片（可多选，最多 4 张；留空可发纯文本推文）" : "选择图片(可多选,最多 18 张)";
  }
  pubFilesClear();
}
function pubFilesClear() { pubFilesDT = new DataTransfer(); _pubSync(); }
function _pubSync() { const inp = $("pub-files"); if (inp) inp.files = pubFilesDT.files; renderPubFiles(); }
function pubAddFiles(files) {
  const isVideo = $("pub-type").value === "video";
  const ig = PLATFORM === "instagram", tw = PLATFORM === "twitter";
  const maxItems = tw ? 4 : ig ? 10 : 18;
  for (const f of files) {
    if (isVideo && !ig) { pubFilesDT = new DataTransfer(); pubFilesDT.items.add(f); break; }
    if ([...pubFilesDT.files].some(x => x.name === f.name && x.size === f.size)) continue;
    if (pubFilesDT.files.length >= maxItems) break;
    pubFilesDT.items.add(f);
  }
  _pubSync();
}
function pubRemoveFile(i) {
  const dt = new DataTransfer();
  [...pubFilesDT.files].forEach((f, idx) => { if (idx !== i) dt.items.add(f); });
  pubFilesDT = dt; _pubSync();
}
let PUB_THUMB_URLS = [];
function renderPubFiles() {
  const box = $("pub-filelist"); if (!box) return;
  PUB_THUMB_URLS.forEach(url => URL.revokeObjectURL(url)); PUB_THUMB_URLS = [];
  box.innerHTML = [...pubFilesDT.files].map((f, i) => {
    const url = f.type.startsWith("image/") ? URL.createObjectURL(f) : "";
    if (url) PUB_THUMB_URLS.push(url);
    const thumb = f.type.startsWith("image/")
      ? `<img src="${url}" alt="">`
      : `<span class="fp-ph">${ic("i-play")}</span>`;
    return `<span class="fp-chip">${thumb}<span title="${esc(f.name)}">${esc(f.name)}</span><button type="button" onclick="pubRemoveFile(${i})" aria-label="移除">${ic("i-x")}</button></span>`;
  }).join("");
  globalThis.CreatorHubWorkbench?.previewUpdated?.();
}
function bindPubFilePicker() {
  const inp = $("pub-files"), zone = $("pub-drop");
  if (!inp || !zone) return;
  inp.addEventListener("change", e => { pubAddFiles(e.target.files); });
  ["dragenter", "dragover"].forEach(ev => zone.addEventListener(ev, e => { e.preventDefault(); zone.classList.add("drag"); }));
  ["dragleave", "drop"].forEach(ev => zone.addEventListener(ev, e => { e.preventDefault(); if (ev === "dragleave" && zone.contains(e.relatedTarget)) return; zone.classList.remove("drag"); }));
  zone.addEventListener("drop", e => { if (e.dataTransfer && e.dataTransfer.files.length) pubAddFiles(e.dataTransfer.files); });
}
let PUB_SUBMITTING = false, PUB_UPLOAD_CACHE = null;
function publishFormPayload() {
  const tiktok = PLATFORM === "tiktok", bili = PLATFORM === "bilibili", ig = PLATFORM === "instagram", tw = PLATFORM === "twitter";
  const mediaType = $("pub-type").value;
  let biliOptions, igOptions, twOptions;
  if (bili) {
    const copyright = +($("pub-bili-copyright")?.value || 1);
    const source = ($("pub-bili-source")?.value || "").trim();
    if (mediaType === "video" && copyright === 2 && !source) {
      throw new Error("转载稿件请填写转载来源");
    }
    const stmtVal = +($("pub-bili-statement")?.value || -1);
    biliOptions = {
      tid: +($("pub-bili-tid")?.value || 21),
      copyright,
      source,
      creation_statement_id: stmtVal,
      declaration_index: stmtVal,
      recreate: +($("pub-bili-recreate")?.value || -1),
      up_close_danmu: $("pub-bili-close-danmu")?.value === "1",
      up_close_reply: $("pub-bili-close-reply")?.value === "1",
      up_selection_reply: $("pub-bili-selection-reply")?.value === "1",
      is_only_self: $("pub-bili-visibility")?.value === "private" ? 1 : 0,
    };
  }
  if (tw) {
    twOptions = {
      reply_permission: $("pub-tw-reply-perm")?.value || "everyone",
      long_text: $("pub-tw-long")?.value === "1",
      first_comment: ($("pub-tw-first-comment")?.value || "").trim(),
    };
  }
  if (ig) {
    igOptions = {
      post_kind: $("pub-ig-kind")?.value || "auto",
      share_to_feed: $("pub-ig-share-feed")?.value !== "0",
    };
  }
  return { account_id: +$("pub-acc").value, media_type: mediaType,
    title: $("pub-title").value.trim(), desc: $("pub-desc").value,
    topics: $("pub-topics").value.trim(), scheduled_at: scheduleToApi($("pub-when").value || null),
    location: $("pub-location") ? $("pub-location").value.trim() : "",
    visibility: bili ? ($("pub-bili-visibility")?.value || "public") : (tiktok ? $("pub-tk-visibility").value : $("pub-visibility") ? $("pub-visibility").value : "public"),
    allow_save: $("pub-allowsave") ? $("pub-allowsave").value !== "0" : true,
    allow_comment: bili ? ($("pub-bili-close-reply")?.value !== "1") : ig ? ($("pub-ig-comment")?.value !== "0") : tw ? (($("pub-tw-reply-perm")?.value || "everyone") !== "mentioned") : (tiktok ? $("pub-tk-comment").value !== "0" : true),
    allow_duet: tiktok ? $("pub-tk-duet").value !== "0" : true,
    allow_stitch: tiktok ? $("pub-tk-stitch").value !== "0" : true,
    ...(bili ? { bili_options: biliOptions } : {}),
    ...(ig ? { ig_options: igOptions } : {}),
    ...(tw ? { tw_options: twOptions } : {}) };
}
async function addPublish() {
  if (PUB_SUBMITTING) return;
  const acc = $("pub-acc").value;
  if (!acc) { toast("请选择" + (PF_NAME[PLATFORM] || "发布") + "账号", "err"); return; }
  const files = Array.from($("pub-files").files);
  if (!files.length && PLATFORM !== "twitter") { toast("请先选择要发布的文件", "err"); return; }
  if (!files.length && PLATFORM === "twitter" && !($("pub-title").value.trim() || $("pub-desc").value.trim() || $("pub-topics").value.trim())) { toast("请填写推文文案或选择媒体文件", "err"); return; }
  let body;
  try { body = publishFormPayload(); }
  catch (e) { $("pub-msg").textContent = e.message; toast(e.message, "err"); return; }
  const btn = evtBtn();
  PUB_SUBMITTING = true;
  $("pub-msg").textContent = "上传中…";
  try { await withBusy(btn, "提交中", async () => {
    try {
      if (!files.length) {
        PUB_UPLOAD_CACHE = { files: [], paths: [] };
      } else if (!PUB_UPLOAD_CACHE || PUB_UPLOAD_CACHE.files.length !== files.length ||
          !PUB_UPLOAD_CACHE.files.every((file, index) => file === files[index])) {
        const fd = new FormData(); for (const f of files) fd.append("files", f);
        const up = await api("/api/publish/upload", { method: "POST", body: fd });
        PUB_UPLOAD_CACHE = { files, paths: (up.files || []).map(f => f.path) };
      }
      const paths = PUB_UPLOAD_CACHE.paths;
      await api("/api/publish", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ...body, media_paths: paths }),
      });
      // A late response must not erase edits made during a slow upload.
      let unchanged = false;
      try { unchanged = JSON.stringify(publishFormPayload()) === JSON.stringify(body) &&
          Array.from($("pub-files").files).length === files.length &&
          Array.from($("pub-files").files).every((file, index) => file === files[index]); } catch (_) {}
      if (unchanged) {
        pubFilesClear(); $("pub-title").value = ""; $("pub-desc").value = ""; $("pub-topics").value = ""; $("pub-when").value = ""; if ($("pub-location")) $("pub-location").value = ""; if ($("pub-tw-first-comment")) $("pub-tw-first-comment").value = ""; dtSyncAll();
        PUB_UPLOAD_CACHE = null;
      }
      $("pub-msg").textContent = body.scheduled_at ? "已加入定时队列 ✓" : "已加入发布队列 ✓";
      toast("已加入发布队列", "ok");
    } catch (e) { $("pub-msg").textContent = "提交未确认: " + e.message + "；重试相同内容会沿用原提交编号。"; toast("提交未确认:" + e.message, "err"); }
  }); } finally { PUB_SUBMITTING = false; }
  refreshPublish();
}
const PUB_ST = { draft: "待确认预约", pending: "排队中", publishing: "发布中", uncertain: "结果待确认", done: "已发布", failed: "失败", canceled: "已取消" };
const PUB_PILL = { draft: "pending", pending: "pending", publishing: "downloading", uncertain: "downloading", done: "done", failed: "failed", canceled: "invalid" };
async function editPublish(id) {
  const task = PUBLISH_TASKS.find(x => x.id === id); if (!task) return;
  const accounts = (ALL_ACCOUNTS.length ? ALL_ACCOUNTS : ACCOUNTS).filter(a => a.platform === task.platform);
  const accountOptions = accounts.map(a =>
    `<option value="${a.id}">${esc(a.nickname)}${a.has_creator ? " · 创作号" : ""}</option>`
  ).join("");
  const value = await new Promise(res => {
    _uiResolve = res; _uiCancelVal = null;
    const curBili = task.bili_options || {};
    _uiGetVal = () => ({
      account_id: +$("ep-account").value,
      title: $("ep-title").value.trim(),
      desc: $("ep-desc").value,
      topics: $("ep-topics").value.trim(),
      scheduled_at: $("ep-when").value || null,
      location: $("ep-location") ? $("ep-location").value.trim() : "",
      visibility: $("ep-visibility") ? $("ep-visibility").value : "public",
      allow_save: $("ep-allowsave") ? $("ep-allowsave").value !== "0" : true,
      allow_comment: $("ep-tk-comment") ? $("ep-tk-comment").value !== "0" : true,
      allow_duet: $("ep-tk-duet") ? $("ep-tk-duet").value !== "0" : true,
      allow_stitch: $("ep-tk-stitch") ? $("ep-tk-stitch").value !== "0" : true,
      ...(task.platform === "bilibili" ? {
        bili_options: {
          ...curBili,
          tid: +($("ep-bili-tid")?.value || curBili.tid || 21),
          copyright: +($("ep-bili-copyright")?.value || curBili.copyright || 1),
          source: ($("ep-bili-source")?.value ?? curBili.source ?? "").trim(),
          is_only_self: ($("ep-visibility")?.value || "public") === "private" ? 1 : 0,
        },
      } : {}),
    });
    const editCap = task.platform === "twitter" ? (task.tw_options?.long_text ? 4000 : 280) : task.platform === "instagram" ? 2200 : task.platform === "bilibili" ? 80 : task.platform === "rednote" ? 40 : task.platform === "douyin" ? 30 : task.platform === "shipinhao" ? 16 : 20;
    const showVisOpts = ["douyin", "xhs", "rednote", "kuaishou"].includes(task.platform);
    $("ui-body").innerHTML = `
      <div><label class="field" for="ep-account">发布账号</label>
        <select id="ep-account">${accountOptions}</select></div>
      <div><label class="field" for="ep-title">标题（≤${editCap} 字）</label>
        <input id="ep-title" maxlength="${editCap}" value="${esc(task.title || "")}"></div>
      <div><label class="field" for="ep-desc">正文</label>
        <textarea id="ep-desc" rows="4">${esc(task.desc || "")}</textarea></div>
      <div><label class="field" for="ep-topics">话题</label>
        <input id="ep-topics" value="${esc(task.topics || "")}" placeholder="逗号分隔，不用带 #"></div>
      <div><label class="field" for="ep-when">定时发布</label>
        <input type="datetime-local" id="ep-when" aria-label="定时发布（留空=尽快发）"></div>
      ${task.platform === "shipinhao" ? `<div><label class="field" for="ep-location">位置</label>
        <input id="ep-location" value="${esc(task.location || "")}" placeholder="城市或地点名"></div>` : ""}
      ${showVisOpts ? `<fieldset class="publish-permissions">
        <legend>互动与权限</legend>
        <div class="publish-permission"><label class="field" for="ep-visibility">谁可以看</label>
          <select id="ep-visibility"><option value="public">公开</option><option value="friends">好友可见</option><option value="private">仅自己可见</option></select></div>
        ${task.platform === "douyin" ? `<div class="publish-permission"><label class="field" for="ep-allowsave">保存权限</label>
          <select id="ep-allowsave"><option value="1">允许他人保存</option><option value="0">不允许</option></select></div>` : ""}
      </fieldset>` : ""}
      ${task.platform === "tiktok" ? `<fieldset class="publish-permissions">
        <legend>TikTok 可见范围与互动</legend>
        <div class="publish-permission"><label class="field" for="ep-visibility">谁可以看</label>
          <select id="ep-visibility"><option value="public">所有人</option><option value="friends">好友</option><option value="private">仅自己</option></select></div>
        <div class="publish-permission"><label class="field" for="ep-tk-comment">评论</label>
          <select id="ep-tk-comment"><option value="1">允许</option><option value="0">关闭</option></select></div>
        <div class="publish-permission"><label class="field" for="ep-tk-duet">Duet</label>
          <select id="ep-tk-duet"><option value="1">允许</option><option value="0">关闭</option></select></div>
        <div class="publish-permission"><label class="field" for="ep-tk-stitch">Stitch</label>
          <select id="ep-tk-stitch"><option value="1">允许</option><option value="0">关闭</option></select></div>
      </fieldset>` : ""}
      ${task.platform === "bilibili" ? `<fieldset class="publish-permissions">
        <legend>哔哩哔哩投稿设置</legend>
        <div class="publish-permission"><label class="field" for="ep-bili-tid">分区 tid</label>
          <input id="ep-bili-tid" type="number" min="1" value="${Number(curBili.tid || 21)}"></div>
        <div class="publish-permission"><label class="field" for="ep-bili-copyright">稿件类型</label>
          <select id="ep-bili-copyright"><option value="1">自制</option><option value="2">转载</option></select></div>
        <div class="publish-permission"><label class="field" for="ep-bili-source">转载来源</label>
          <input id="ep-bili-source" value="${esc(curBili.source || "")}" placeholder="转载时填写出处"></div>
        <div class="publish-permission"><label class="field" for="ep-visibility">可见范围</label>
          <select id="ep-visibility"><option value="public">公开可见</option><option value="private">仅自己可见</option></select></div>
      </fieldset>` : ""}`;
    $("ep-account").value = task.account_id ? String(task.account_id) : "";
    $("ep-when").value = localDateTimeValue(task.scheduled_at);
    if ($("ep-visibility")) $("ep-visibility").value = task.visibility || "public";
    if ($("ep-allowsave")) $("ep-allowsave").value = task.allow_save === false ? "0" : "1";
    if ($("ep-tk-comment")) $("ep-tk-comment").value = task.allow_comment === false ? "0" : "1";
    if ($("ep-tk-duet")) $("ep-tk-duet").value = task.allow_duet === false ? "0" : "1";
    if ($("ep-tk-stitch")) $("ep-tk-stitch").value = task.allow_stitch === false ? "0" : "1";
    if ($("ep-bili-copyright")) $("ep-bili-copyright").value = String(curBili.copyright || 1);
    ["ep-account", "ep-visibility", "ep-allowsave", "ep-tk-comment", "ep-tk-duet", "ep-tk-stitch", "ep-bili-copyright"].forEach(key => { const el = $(key); if (el) enhanceSelect(el); });
    enhanceDateTime($("ep-when"));
    _uiOpen("编辑发布任务", `${task.media_count} 个附件保持不变。调整文案、账号、预约时间与权限。`, {
      okText: "保存修改", wide: true, icon: "i-send",
      submit: value => {
        if (!value.account_id) uiEditorError("请选择发布账号", "ep-account");
        const scheduled_at = scheduleToApi(value.scheduled_at);
        return api("/api/publish/" + id, {
          method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ...value, scheduled_at }),
        });
      },
    });
  });
  if (value === null) return;
  toast("发布任务已更新", "ok"); refreshPublish();
}
async function refreshPublish() {
  const isCurrent = beginViewRequest("publish");
  if (!$("pub-table")) return;
  const rows = await api("/api/publish" + (GLOBAL_MODE ? "" : "?platform=" + (pfHasPublish(PLATFORM) ? PLATFORM : "xhs")));
  if (!isCurrent()) return;
  PUBLISH_TASKS = rows;
  if ($("tb-pub")) $("tb-pub").textContent = rows.length;
  $("pub-table").innerHTML = rows.map(t => {
    const accObj = (typeof findAnyAccount === "function" && findAnyAccount(t.account_id)) || ACCOUNTS.find(a => a.id === t.account_id);
    const pubAcc = accObj ? (accObj.alias || accObj.nickname || `账号 #${t.account_id}`) : (t.account_id ? `账号 #${t.account_id}` : "—");
    const pfBadge = GLOBAL_MODE ? `<span class="mx-mini-pf inline" style="margin-right:6px">${esc(PF_NAME[t.platform] || t.platform)}</span>` : "";
    return `<tr>
    <td class="wrap" style="max-width:240px"><b>${esc(t.title || "(无标题)")}</b></td>
    <td>${pfBadge}${esc(pubAcc)}</td>
    <td>${t.platform === "twitter" && !(t.media_count || 0) ? "纯文本" : `${t.media_type === "video" ? "视频" : "图文"} · <span class="mut num">${t.media_count || 0}</span>`}</td>
    <td>${t.source_platform ? esc(PF_NAME[t.source_platform] || t.source_platform) + " 转发" : "手动"}</td>
    <td class="mut num">${t.scheduled_at ? new Date(t.scheduled_at).toLocaleString() : "尽快"}${t.next_allowed_at ? `<div class="mut" title="预约保持不变，同时等待风控间隔结束">风控最早 ${esc(riskTime(t.next_allowed_at))}</div>` : ""}</td>
    <td><span class="pill ${PUB_PILL[t.status] || "pending"}">${PUB_ST[t.status] || t.status}</span>${t.error ? ` <span class="warn-ic" title="${esc(t.error)}">${ic("i-info")}</span>` : ""}${t.result_url ? ((["kuaishou", "shipinhao", "tiktok"].includes(t.platform) || (t.platform === "bilibili" && String(t.result_url).includes("member.bilibili.com"))) ? ` <a href="javascript:void(0)" onclick="openPubInBrowser(${Number(t.account_id)}, ${jsArg(t.result_url)}, ${jsArg(t.platform)})">查看</a>` : ` <a href="${esc(safeHttpUrl(t.result_url))}" target="_blank" rel="noopener noreferrer">查看</a>`) : ""}${t.status === "publishing" ? `<div class="mut" id="pub-stage-${t.id}" style="margin-top:4px;white-space:normal;max-width:200px">${esc(t.live_stage || "执行中，请等待结果")}</div>` : ""}</td>
    <td class="acttd">
      ${["pending", "failed"].includes(t.status) ? `<button class="ghost sm" onclick="runPublish(${t.id})">立即发布</button>` : ""}
      ${["draft", "pending", "failed", "canceled"].includes(t.status) ? `<button class="ghost sm" onclick="editPublish(${t.id})">${t.schedule_needs_confirmation ? "确认预约时间" : "编辑"}</button>` : ""}
      ${t.status === "uncertain" ? `<button class="ghost sm" onclick="resolveTaskResult('publishes',${t.id})">核对结果</button>` : t.status === "publishing" ? "" : `<button class="ghost sm danger" onclick="delPublish(${t.id})">${ic("i-trash")}删除</button>`}
    </td></tr>`;
  }).join("") || empty(7, "暂无发布任务", "i-send",
      PLATFORM === "kuaishou" ? "上传图集/视频加入队列(发布到快手创作平台)"
      : PLATFORM === "douyin" ? "上传图集/视频加入队列(发布到抖音创作平台)"
      : PLATFORM === "tiktok" ? "上传单个视频加入队列(发布到 TikTok Studio)"
      : PLATFORM === "bilibili" ? "上传视频稿件或图文动态加入队列(发布到哔哩哔哩创作中心)"
      : PLATFORM === "instagram" ? "上传单图 / 轮播 / Reels / Story 加入队列(发布到 Instagram)"
      : PLATFORM === "twitter" ? "编写纯文本推文，或上传最多 4 图 / 单个视频加入队列(发布到 X / Twitter)"
      : "上传图集/视频加入队列,或在抖音作品上点「发小红书」转发过来");
}
// 创作平台管理页必须复用账号自己的登录态，不能交给系统浏览器。
async function openPubInBrowser(accountId, url, platform = "shipinhao") {
  if (!accountId) { toast("缺少账号信息", "err"); return; }
  const pageName = platform === "kuaishou" ? "快手作品管理页"
    : platform === "tiktok" ? "TikTok Studio 内容页"
    : platform === "bilibili" ? "哔哩哔哩稿件管理页" : "视频号管理页";
  toast("正在用该账号浏览器打开" + pageName + "…", "info", 5000);
  try {
    await api("/api/accounts/" + accountId + "/open-browser?url=" + encodeURIComponent(url || ""), { method: "POST" });
  } catch (e) { toast("打开失败:" + e.message, "err"); }
}
async function runPublish(id) {
  const btn = evtBtn();
  toast("发布中…会弹出浏览器窗口完成发布", "info", 8000);
  let lastStage = "";
  const stageTimer = setInterval(async () => {
    try {
      const resp = await fetch("/api/publish?platform=" + encodeURIComponent(pfHasPublish(PLATFORM) ? PLATFORM : "xhs"));
      if (!resp.ok) return;
      const rows = await resp.json();
      const row = Array.isArray(rows) ? rows.find(item => item.id === id) : null;
      const stage = String(row?.live_stage || "").trim();
      if (!stage) return;
      _busyHint = stage.includes("安全验证") ? "请在弹出窗口完成验证" : stage.slice(0, 24);
      const rowEl = $(`pub-stage-${id}`);
      if (rowEl) rowEl.textContent = stage;
      if (stage !== lastStage) {
        lastStage = stage;
        if (stage.includes("安全验证") || stage.includes("验证码") || stage.includes("扫码")) {
          toast(stage, "info", 12000);
        }
      }
    } catch (_) {}
  }, 2000);
  await withBusy(btn, "发布中", async () => {
    try { const r = await api("/api/publish/" + id + "/run-now", { method: "POST" }); toast(r.ok ? "发布成功 ✓" : "发布未成功:" + (r.error || ""), r.ok ? "ok" : "err", 6000); }
    catch (e) { toast("发布失败:" + e.message, "err"); }
    finally { clearInterval(stageTimer); _busyHint = ""; }
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
  if (!acc) { toast("请先选择" + (PF_NAME[PLATFORM] || "小红书") + "账号", "err"); return; }
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
        ${n.cover ? `<img class="ncard-cover" src="${esc(safeMediaUrl(n.cover))}" referrerpolicy="no-referrer" loading="lazy" alt="" onclick="pubPreview(${i})">` : `<div class="ncard-cover ph" onclick="pubPreview(${i})">${ic("i-image")}</div>`}
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
const repostKuaishou = (id) => openRepost(id, "kuaishou");
const repostTiktok = (id) => openRepost(id, "tiktok");
const repostBilibili = (id) => openRepost(id, "bilibili");
const repostRednote = (id) => openRepost(id, "rednote");
const repostInstagram = (id) => openRepost(id, "instagram");
const repostTwitter = (id) => openRepost(id, "twitter");
async function pickRepostTarget(id) {
  const rec = CONTENTS.find(r => r.id === id);
  const options = [
    { value: "xhs", label: "小红书" },
    { value: "rednote", label: "海外小红书" },
    { value: "douyin", label: "抖音" },
    { value: "kuaishou", label: "快手" },
    { value: "shipinhao", label: "视频号" },
    { value: "bilibili", label: "哔哩哔哩" },
    { value: "instagram", label: "Instagram" },
    { value: "twitter", label: "Twitter" },
  ];
  if (!rec || rec.media_type === "video") options.push({ value: "tiktok", label: "TikTok" });
  const target = await uiSelect({
    title: "转发作品",
    hint: "选择要发布到的平台，下一步可以继续编辑标题、文案和发布时间。",
    options,
    value: "shipinhao",
  });
  if (target === null) return;
  openRepost(id, target);
}
async function openRepost(id, target) {
  const rec = CONTENTS.find(r => r.id === id);
  // 拉取目标平台可发布账号:小红书需创作号;其余平台需任一登录态
  const all = await api("/api/accounts?platform=" + target);
  const accs = target === "xhs"
    ? all.filter(a => a.has_creator)
    : all.filter(a => a.has_storage || a.has_creator);
  if (!accs.length) {
    const loginHint = target === "xhs"
      ? "请先在小红书账号页完成「创作者登录」(发布用)"
      : target === "rednote"
        ? "请先在海外小红书账号页完成登录"
        : target === "shipinhao"
        ? "请先在视频号账号页完成「视频号登录」"
        : target === "kuaishou"
          ? "请先在快手账号页完成登录"
          : target === "tiktok"
            ? "请先在 TikTok 账号页完成登录"
            : target === "bilibili"
              ? "请先在哔哩哔哩账号页完成登录"


              : target === "instagram" ? "请先在 Instagram 账号页完成登录"
                : target === "twitter"
                  ? "请先在 Twitter 账号页完成登录"
                : "请先在抖音账号页完成登录(扫码/创作者/Cookie)";
    toast(loginHint, "err");
    return;
  }
  REPOST_ID = id; REPOST_TARGET = target;
  const isDy = target === "douyin";
  const isChannels = target === "shipinhao";
  const isKs = target === "kuaishou";
  const isTk = target === "tiktok";
  const isBili = target === "bilibili";
  const isRn = target === "rednote";
  const isIg = target === "instagram";
  const isTw = target === "twitter";
  const cap = isTw ? 280 : isIg ? 2200 : isBili ? 80 : isRn ? 40 : isDy ? 30 : isChannels ? 16 : 20;
  const pname = isDy ? "抖音" : isChannels ? "视频号" : isKs ? "快手" : isTk ? "TikTok" : isBili ? "哔哩哔哩" : isIg ? "Instagram" : isTw ? "Twitter" : isRn ? "海外小红书" : "小红书";
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
  $("rp-msg").textContent = "确认后加入发布队列";
  $("rp-msg").dataset.tone = "neutral";
  $("rp-msg").setAttribute("role", "status");
  $("rp-src").textContent = rec ? `来源:${rec.media_type === "images" ? "图集" : "视频"} · ${esc((rec.desc || "(无描述)").slice(0, 30))}` : "";
  // 抖音发布设置(可见性 / 保存权限)仅目标为抖音时显示
  if ($("rp-dy-opts")) $("rp-dy-opts").style.display = isDy ? "flex" : "none";
  if (isDy) { if ($("rp-visibility")) $("rp-visibility").value = "public"; if ($("rp-allowsave")) $("rp-allowsave").value = "1"; }
  renderRepostThumbs(id);   // 异步拉媒体缩略图,不阻塞弹窗
  $("rp-submit").disabled = false;
  $("repost").style.display = "flex";
  $("rp-body").scrollTop = 0;
  modalOpened($("repost"));
  $("rp-head").focus({ preventScroll: true });
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
      <img src="${esc(safeMediaUrl(m.url))}" referrerpolicy="no-referrer" draggable="false" alt="" title="点击看大图" onclick="openPreview(${REPOST_ID},${m.idx})">
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
  if ($("rp-submit").disabled) return;
  $("repost").style.display = "none"; REPOST_ID = null;
  modalClosed($("repost"));
}
async function submitRepost() {
  if (REPOST_ID === null || $("rp-submit").disabled) return;
  const accId = +$("rp-acc").value;
  if (!accId) { toast("请选择发布账号", "err"); return; }
  const btn = $("rp-submit"), modal = $("repost");
  let success = false;
  $("rp-body").inert = true; modal.setAttribute("aria-busy", "true");
  modal.querySelectorAll(".pv-close,#rp-actions .ghost").forEach(button => { button.disabled = true; });
  $("rp-msg").dataset.tone = "neutral"; $("rp-msg").setAttribute("role", "status");
  $("rp-msg").textContent = "提交中…";
  $("rp-msg").focus({ preventScroll: true });
  const body = {
    account_id: accId,
    title: $("rp-title").value.trim(),
    desc: $("rp-desc").value,
    topics: $("rp-topics").value.trim(),
    scheduled_at: $("rp-when").value,
    visibility: $("rp-visibility") ? $("rp-visibility").value : "public",
    allow_save: $("rp-allowsave") ? $("rp-allowsave").value !== "0" : true,
    media_order: rpMediaOrder(),
  };
  const pname = REPOST_TARGET === "douyin" ? "抖音"
    : REPOST_TARGET === "shipinhao" ? "视频号"
    : REPOST_TARGET === "kuaishou" ? "快手"
    : REPOST_TARGET === "tiktok" ? "TikTok"
    : REPOST_TARGET === "bilibili" ? "哔哩哔哩"
    : REPOST_TARGET === "instagram" ? "Instagram"
    : REPOST_TARGET === "twitter" ? "Twitter"
    : REPOST_TARGET === "rednote" ? "海外小红书" : "小红书";
  try {
    body.scheduled_at = scheduleToApi(body.scheduled_at);
    const r = await withBusy(btn, "提交中", () => api("/api/contents/" + REPOST_ID + "/repost-" + REPOST_TARGET, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    }));
    toast((body.scheduled_at ? "已加入定时发布队列" : `已加入${pname}发布队列`) + "(任务 #" + r.task_id + ")", "ok");
    success = true;
  } catch (e) {
    $("rp-msg").textContent = "提交未完成：" + e.message;
    $("rp-msg").dataset.tone = "error"; $("rp-msg").setAttribute("role", "alert");
    $("rp-msg").focus({ preventScroll: true });
  } finally {
    $("rp-body").inert = false; modal.removeAttribute("aria-busy");
    modal.querySelectorAll(".pv-close,#rp-actions .ghost").forEach(button => { button.disabled = false; });
  }
  if (success) { hideRepost(); if (typeof refreshPublish === "function") refreshPublish(); }
}

// 16-autocomment-queue-boot.js — 自动评论规则与审核、统一任务队列、轮询主循环及 CreatorHubBridge。
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
    if (["xhs", "bilibili", "instagram", "twitter"].includes(PLATFORM)) html += '<option value="keyword">搜索关键词</option>';
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
  const mode = $("ac-mode").value, kind = $("ac-kind").value, xhs = PLATFORM === "xhs", bili = PLATFORM === "bilibili", ig = PLATFORM === "instagram", tw = PLATFORM === "twitter";
  let show = true, label = "目标", ph = "";
  if (mode === "auto_reply") {
    if (kind === "self") show = false;
    else { label = xhs ? "笔记链接 / id" : tw ? "推文链接 / status_id" : "作品链接 / id"; ph = xhs ? "explore 链接 / xhslink / note_id" : bili ? "视频链接 / BV或av号 / opus链接" : ig ? "instagram.com/p/... 或 /reel/... 链接" : tw ? "x.com/.../status/... 链接或数字推文 ID" : "作品链接 / 短链 / 数字 id"; }
  } else {
    if (kind === "keyword") { label = "搜索关键词"; ph = tw ? "例如:AI agent / #buildinpublic" : ig ? "例如:#travel / skincare" : "例如:露营装备 / 口红试色"; }
    else { label = xhs ? "博主主页 / id" : bili ? "UP 主空间主页 / mid" : (ig || tw) ? "创作者主页 / @username" : "博主主页 / sec_uid"; ph = xhs ? "主页链接 / xhslink / user_id" : bili ? "space.bilibili.com 链接 / mid" : ig ? "instagram.com/<username> 或 @username" : tw ? "x.com/<username> 或 @username" : "主页链接 / 短链 / sec_uid"; }
  }
  $("ac-target-wrap").style.display = show ? "" : "none";
  $("ac-target-label").textContent = label; $("ac-target").placeholder = ph;
  $("ac-reply-filter").style.display = mode === "auto_reply" ? "" : "none";
  csSyncAll();
}
function populateAcAccount() {
  const sel = $("ac-acc"); if (!sel) return;
  sel.innerHTML = accOptions(ACCOUNTS, `请选择${PF_NAME[PLATFORM] || "当前平台"}账号(必选)`);
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
  await withBusy(evtBtn(), "创建中", async () => {
  try {
    await api("/api/comment-rules", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    $("ac-templates").value = ""; $("ac-target").value = "";
    $("ac-msg").textContent = "规则已创建(默认关闭),可在下方「试跑」预览文案 ✓";
    toast("规则已创建", "ok"); refreshCommentRules();
    globalThis.CreatorHubWorkbench?.completed?.("autocomment");
  } catch (e) { $("ac-msg").textContent = "失败: " + e.message; toast("创建失败:" + e.message, "err"); }
  });
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
  const accOpts = accOptions(ACCOUNTS.filter(a => a.platform === EM_PF), `请选择${PF_NAME[EM_PF] || "对应平台"}账号`);
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
      <fieldset class="ui-form-group"><legend>规则与对象</legend>
        <div><label class="field" for="em-name">规则名称</label><input id="em-name" maxlength="100" placeholder="便于识别这条规则"></div>
        <div class="form-grid">
          <div><label class="field" for="em-mode">互动方式</label><select id="em-mode" onchange="emOnMode()"><option value="auto_reply">回复自己作品的评论</option><option value="auto_comment">评论其他人的作品</option></select></div>
          <div><label class="field" for="em-kind">目标范围</label><select id="em-kind" onchange="emOnKind()"></select></div>
        </div>
        <div><label class="field" for="em-acc">使用账号</label><select id="em-acc">${accOpts}</select></div>
        <div id="em-target-wrap"><label class="field" id="em-target-label" for="em-target">目标</label><input id="em-target"></div>
      </fieldset>
      <fieldset class="ui-form-group"><legend>文案与审核</legend>
        <div><label class="field" for="em-templates">文案模板</label><textarea id="em-templates" rows="4" aria-describedby="em-templates-hint"></textarea>
          <p class="field-help" id="em-templates-hint">每行一条，支持 {nick}、{kw} 和随机词组 {好|不错|赞}。</p></div>
        <label class="ui-toggle"><span><b>AI 生成文案</b><small>生成失败时使用上面的模板</small></span><input type="checkbox" role="switch" id="em-use-ai"></label>
        <label class="ui-toggle"><span><b>发布前审核</b><small>只生成草稿，由你确认后再发送</small></span><input type="checkbox" role="switch" id="em-review"></label>
        <div class="form-grid" id="em-filter-wrap">
          <div><label class="field" for="em-reply-filter">只回复包含关键词的评论</label><input id="em-reply-filter" placeholder="留空不限制"></div>
          <div><label class="field" for="em-skip">跳过关键词</label><input id="em-skip" placeholder="多个词用逗号分隔"></div>
        </div>
      </fieldset>
      <fieldset class="ui-form-group"><legend>执行频率</legend>
        <div class="form-grid">
          <div><label class="field" for="em-cap">每日上限（条）</label><input type="number" id="em-cap" min="0" inputmode="numeric"></div>
          <div><label class="field" for="em-gap">最小间隔（秒）</label><input type="number" id="em-gap" min="1" inputmode="numeric"></div>
          <div><label class="field" for="em-max">每轮上限（条）</label><input type="number" id="em-max" min="1" inputmode="numeric"></div>
          <div><label class="field" for="em-interval">检查频率</label><select id="em-interval">${numericSelectOptions(r.interval_seconds || 1800, [[900, "每 15 分钟"], [1800, "每 30 分钟"], [3600, "每小时"]], " 秒")}</select></div>
        </div>
      </fieldset>`;
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
    _uiOpen("编辑规则 #" + id, "调整互动对象、文案和执行频率。修改目标或关键词后会重新解析。", {
      okText: "保存修改", wide: true, icon: "i-automation",
      submit: val => {
        if (!val.templates.length) uiEditorError("请至少写一条文案模板", "em-templates");
        if (!val.account_id) uiEditorError("请选择与规则平台一致的账号", "em-acc");
        return api("/api/comment-rules/" + id, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(val) });
      },
    });
    ["em-mode", "em-kind", "em-acc", "em-interval"].forEach(idd => { const el = $(idd); if (el) enhanceSelect(el); });
  }).then(async val => {
    if (!val) return;   // 取消
    toast("规则已更新", "ok"); refreshCommentRules();
  });
}
async function refreshCommentRules() {
  const isCurrent = beginViewRequest("comment-rules");
  if (!$("ac-rule-table")) return;
  const rows = await api("/api/comment-rules?platform=" + PLATFORM);
  if (!isCurrent()) return;
  if ($("tb-ac")) $("tb-ac").textContent = rows.length;
  AC_RULES = rows;
  $("ac-rule-table").innerHTML = rows.map(r => {
    const tgt = r.mode === "auto_comment"
      ? (r.target_kind === "keyword" ? "#" + esc(r.keyword) : esc((r.sec_uid || "").slice(0, 14)))
      : (r.target_kind === "work" ? esc(r.aweme_id) : "");
    const acc = (ACCOUNTS.find(a => a.id === r.account_id) || {}).nickname || ("#" + r.account_id);
    const tags = [r.use_ai ? "AI文案" : "", r.require_review ? "草稿审核" : ""].filter(Boolean)
      .map(x => `<span class="pill downloading" style="margin-left:4px;font-size:10px">${x}</span>`).join("");
    return `<tr>
      <td><b>${esc(r.name)}</b>${tags}</td>
      <td>${AC_MODE_T[r.mode] || r.mode}</td>
      <td class="wrap" style="max-width:180px">${AC_KIND_T[r.target_kind] || r.target_kind}${tgt ? `<div class="mut">${tgt}</div>` : ""}</td>
      <td>${esc(acc)}</td>
      <td class="mut num">${r.daily_cap}/日 · ${Math.round(r.interval_seconds / 60)}分</td>
      <td class="mut num">${r.last_run_at ? new Date(r.last_run_at + "Z").toLocaleString() : "—"}${r.last_error ? ` <span class="warn-ic" title="${esc(r.last_error)}">${ic("i-info")}</span>` : ""}${autoRunHint(r.next_auto_run_at)}</td>
      <td><span class="pill ${r.enabled ? "done" : "paused"}">${r.enabled ? "运行中" : "已停用"}</span></td>
      <td class="acttd">
        <button class="ghost sm" onclick="runRule(${r.id})">试跑</button>
        <button class="ghost sm" onclick="editRule(${r.id})">编辑</button>
        <button class="ghost sm" onclick="toggleRule(${r.id}, ${r.enabled ? "false" : "true"})">${r.enabled ? "停用" : "启用"}</button>
        <button class="ghost sm danger" onclick="delRule(${r.id})">${ic("i-trash")}删除</button>
      </td></tr>`;
  }).join("") || empty(8, "暂无评论规则", "i-msg", "创建规则后可先试跑，确认内容再启用。", "autocomment");
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
  const isCurrent = beginViewRequest("comment-tasks");
  if (!$("ac-task-table")) return;
  const st = $("ac-task-filter") ? $("ac-task-filter").value : "";
  const rows = await api("/api/comment-tasks?platform=" + PLATFORM + (st ? "&status=" + st : ""));
  if (!isCurrent()) return;
  AC_TASKS = rows;
  const drafts = rows.filter(t => t.status === "draft");
  if ($("ac-draft-bar")) {
    $("ac-draft-bar").style.display = drafts.length ? "flex" : "none";
    if (drafts.length) $("ac-draft-count").textContent = `有 ${drafts.length} 条草稿待审核——逐条「通过/编辑」,或一键全部通过后由引擎按节流发出`;
  }
  renderCommentTaskRows();
}
function renderCommentTaskRows() {
  if (!$("ac-task-table")) return;
  const query = (($("ac-task-search") && $("ac-task-search").value) || "").trim().toLocaleLowerCase();
  const filtered = AC_TASKS.filter(t => {
    if (!query) return true;
    const accName = (ACCOUNTS.find(a => a.id === t.account_id) || {}).nickname || "";
    return [t.content, t.aweme_id, t.target_nick, accName].filter(Boolean).join(" ").toLocaleLowerCase().includes(query);
  });
  if ($("ac-task-count")) $("ac-task-count").textContent = AC_TASKS.length ? `显示 ${filtered.length} / ${AC_TASKS.length}` : "";
  $("ac-task-table").innerHTML = filtered.map(t => {
    const isDraft = t.status === "draft", canSend = t.status === "pending" || t.status === "failed";
    const accName = (ACCOUNTS.find(a => a.id === t.account_id) || {}).nickname || (t.account_id ? `账号 #${t.account_id}` : "");
    return `<tr>
    <td class="wrap" style="max-width:260px">${esc(t.content)}</td>
    <td><code title="${esc(t.aweme_id || "")}">${esc((t.aweme_id || "—").slice(0, 18))}</code>${accName ? `<div class="mut">${esc(accName)}</div>` : ""}</td>
    <td>${t.target_comment_id ? "回复 " + esc(t.target_nick || "") : "顶层评论"}</td>
    <td class="mut num">${t.scheduled_at ? new Date(t.scheduled_at + "Z").toLocaleString() : "尽快"}${t.next_allowed_at ? `<div class="mut" title="预约保持不变，同时等待风控间隔结束">风控最早 ${esc(riskTime(t.next_allowed_at))}</div>` : ""}</td>
    <td class="mut">${t.method === "browser" ? "浏览器页面" : t.method === "api" ? "API 兼容模式" : t.method === "manual" ? "人工草稿" : "—"}</td>
    <td><span class="pill ${AC_TASK_PILL[t.status] || "pending"}">${AC_TASK_ST[t.status] || t.status}</span>${t.error ? ` <span class="warn-ic" title="${esc(t.error)}">${ic("i-info")}</span>` : ""}</td>
    <td class="acttd">
      ${isDraft ? `<button class="sm" onclick="approveTask(${t.id})">通过</button>` : ""}
      ${(isDraft || canSend) ? `<button class="ghost sm" onclick="editTaskContent(${t.id})">编辑</button>` : ""}
      ${canSend ? `<button class="ghost sm" onclick="runTask(${t.id})">立即发</button>` : ""}
      ${(isDraft || canSend) ? `<button class="ghost sm" onclick="cancelTask(${t.id})">${isDraft ? "弃用" : "取消"}</button>` : ""}
      ${t.status === "uncertain" ? `<button class="ghost sm" onclick="resolveTaskResult('comments',${t.id})">核对结果</button>` : t.status !== "doing" ? `<button class="ghost sm danger" onclick="delTask(${t.id})">${ic("i-trash")}删除</button>` : ""}
    </td></tr>`;
  }).join("") || empty(7, query ? "没有匹配的评论任务" : "暂无评论任务", "i-msg", query ? "调整搜索词或清除筛选" : "启用规则或点「试跑」后,这里会出现待发评论");
}
async function approveTask(id) {
  try { await api("/api/comment-tasks/" + id + "/approve", { method: "POST" }); toast("已通过,转入待发队列", "ok"); refreshCommentTasks(); }
  catch (e) { toast("操作失败:" + e.message, "err"); }
}
async function approveAllDrafts() {
  const platform = PLATFORM;
  const ids = AC_TASKS.filter(t => t.status === "draft").map(t => t.id);
  if (!ids.length) return;
  if (!await uiConfirm({ title: "审核当前列表草稿", message: `通过当前列表中 ${PF_NAME[platform] || platform} 的 ${ids.length} 条草稿？通过后引擎按每账号每日上限和最小间隔陆续发出。`, okText: "通过这些草稿" })) return;
  try { const r = await api("/api/comment-tasks/batch-approve", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ids, platform }) }); toast(`已通过 ${r.approved} 条`, "ok"); refreshCommentTasks(); }
  catch (e) { toast("操作失败:" + e.message, "err"); }
}
async function editTaskContent(id) {
  const t = AC_TASKS.find(x => x.id === id); if (!t) return;
  const v = await uiPrompt({ title: "编辑评论文案", hint: "保存只更新文案，不会发送评论。", value: t.content || "", multiline: true, rows: 3,
    submit: value => {
      if (!value.trim()) uiEditorError("文案不能为空", "ui-inp");
      return api("/api/comment-tasks/" + id, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ content: value.trim() }) });
    },
  });
  if (v === null) return;
  toast("文案已更新", "ok"); refreshCommentTasks();
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
  if (!await uiConfirm({ title: "删除评论任务", message: "确认删除这条评论任务或记录吗？", okText: "删除", danger: true })) return;
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
const TASK_QUEUE_SELECTED = new Set();

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
  const account = item.account_name || (item.account_id ? `账号 #${item.account_id}` : item.platform === "shipinhao" ? "本机微信客户端" : "未绑定账号");
  const scheduled = item.scheduled_at ? `<b>计划 ${taskQueueDate(item.scheduled_at)}${item.schedule_needs_confirmation ? "（待确认时区）" : ""}</b>` : "";
  const created = item.created_at ? `<small>创建 ${taskQueueDate(item.created_at)}</small>` : "";
  const nextAllowed = item.next_allowed_at ? `<small>最早继续：${taskQueueDate(item.next_allowed_at)}</small>` : "";
  const reason = item.blocked_reason || item.error || "—";
  const reasonClass = item.error && !item.blocked_reason ? " has-error" : "";
  const signal = item.blocked_signal ? `<small>信号：${esc(item.blocked_signal)}</small>` : "";
  const stopKey = `${item.queue_type}:${Number(item.id)}`;
  const selectable = ["collections", "publishes", "comments", "actions"].includes(item.queue_type)
    && !["done", "canceled", "skipped", "uncertain"].includes(item.status);
  return `<tr>
    <td><input type="checkbox" class="queue-select" data-queue-type="${esc(item.queue_type)}" data-task-id="${Number(item.id)}" aria-label="选择任务 ${Number(item.id)}" onchange="toggleTaskQueueItem(this)"${selectable ? "" : " disabled"}${TASK_QUEUE_SELECTED.has(stopKey) ? " checked" : ""}></td>
    <td><span class="pill q bare">${esc(item.queue_label)}</span><small class="mut" style="display:block;margin-top:5px">${esc(PF_NAME[item.platform] || item.platform || "—")}</small></td>
    <td><div class="queue-copy"><b title="${esc(item.title)}">${esc(item.title)}</b>${item.detail ? `<small>${esc(item.detail)}</small>` : ""}</div></td>
    <td><div class="queue-account"><b>${esc(account)}</b>${item.account_id ? `<small>ID ${Number(item.account_id)}</small>` : ""}</div></td>
    <td><div class="queue-time">${scheduled || "尽快执行"}${created}</div></td>
    <td><span class="pill ${stateMeta[1]}">${esc(stateMeta[0])}</span><small class="mut" style="display:block;margin-top:5px">${esc(rawStatus)}</small></td>
    <td><div class="queue-reason${reasonClass}">${esc(reason)}${signal}${nextAllowed}</div></td>
    <td class="acttd">${item.status === "uncertain" ? `<button type="button" class="ghost sm" onclick="resolveTaskResult(${jsArg(item.queue_type)},${Number(item.id)})">核对结果</button>` : ""}<button type="button" class="ghost sm" onclick="openTaskQueueSource(${jsArg(item.source_tab)})">${esc(taskQueueSourceLabel(item.source_tab))}</button></td>
  </tr>`;
}

function updateTaskQueueSelection() {
  const button = $("queue-batch-stop");
  if (button) {
    button.disabled = TASK_QUEUE_SELECTED.size === 0;
    button.lastChild.textContent = TASK_QUEUE_SELECTED.size ? `停止所选（${TASK_QUEUE_SELECTED.size}）` : "停止所选";
  }
}
function toggleTaskQueueItem(input) {
  const key = `${input.dataset.queueType}:${Number(input.dataset.taskId)}`;
  if (input.checked) TASK_QUEUE_SELECTED.add(key); else TASK_QUEUE_SELECTED.delete(key);
  updateTaskQueueSelection();
}
function toggleTaskQueuePage(checked) {
  document.querySelectorAll("#queue-table .queue-select:not(:disabled)").forEach(input => {
    input.checked = checked; toggleTaskQueueItem(input);
  });
}
async function batchStopSelectedTasks() {
  if (!TASK_QUEUE_SELECTED.size) return;
  if (!await uiConfirm({ title: "批量停止任务", message: `将安全停止 ${TASK_QUEUE_SELECTED.size} 个所选任务。执行中的发布、评论和私信不会被强制中断。`, okText: "停止所选", danger: true })) return;
  const items = [...TASK_QUEUE_SELECTED].map(value => {
    const split = value.lastIndexOf(":");
    return { queue_type: value.slice(0, split), id: Number(value.slice(split + 1)) };
  });
  try {
    const result = await api("/api/task-queue/batch-stop", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ items }),
    });
    (result.stopped || []).forEach(item => TASK_QUEUE_SELECTED.delete(`${item.queue_type}:${item.id}`));
    toast(`已停止 ${result.stopped_count || 0} 个任务${result.skipped_count ? `，跳过 ${result.skipped_count} 个执行中任务` : ""}`, result.skipped_count ? "info" : "ok");
    updateTaskQueueSelection();
    await refreshTaskQueue();
  } catch (e) { toast("批量停止失败：" + e.message, "err"); }
}

async function resolveTaskResult(queueType, id) {
  const outcome = await uiSelect({
    title: `核对任务 #${id} 的平台结果`,
    hint: "请先到平台检查是否已生效。本操作只记录核对结果，不会发送或重试。尚未查清请取消。",
    value: "",
    options: [{ value: "", label: "请选择核对结论", disabled: true },
      { value: "done", label: "已核实：平台操作已成功" },
      { value: "canceled", label: "取消后续处理，不再重试" }],
  });
  if (!["done", "canceled"].includes(outcome)) return;
  const note = await uiPrompt({ title: "填写核对说明", hint: "例如平台作品链接、核对时间和结果；请勿填写账号凭据。", placeholder: "必填，最多 500 字" });
  if (note === null) return;
  if (!note.trim() || note.trim().length > 500) { toast("核对说明需填写 1–500 字", "err"); return; }
  try {
    await api(`/api/task-queue/${encodeURIComponent(queueType)}/${Number(id)}/resolve`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ outcome, note: note.trim() }),
    });
    toast("核对结果已保存，任务不会自动重试", "ok");
    if (CURRENT_TAB === "publish") await refreshPublish();
    else if (CURRENT_TAB === "autocomment") await refreshCommentTasks();
    else if (CURRENT_TAB === "queue") await refreshTaskQueue();
    await refreshTaskQueueBadge();
  } catch (e) { toast("保存核对结果失败：" + e.message, "err"); }
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
  pager.hidden = Number(data.total || 0) <= TASK_QUEUE_PAGE_SIZE && TASK_QUEUE_PAGES <= 1;
}

function renderTaskQueueSummary(summary = {}) {
  if (taskQueuePlatform() === PLATFORM) globalThis.CreatorHubWorkbench?.queueSummary?.(summary);
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
  if (TASK_QUEUE_LOADING) { VIEW_REQUESTS.delete("task-queue"); TASK_QUEUE_REFRESH_PENDING = true; return; }
  const isCurrent = beginViewRequest("task-queue");
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
    if (!isCurrent()) return;
    body.innerHTML = data.items?.length
      ? data.items.map(taskQueueRow).join("")
      : empty(8, "当前筛选范围内没有任务", "i-inbox", "切换状态或平台范围后再查看");
    updateTaskQueueSelection();
    renderTaskQueueSummary(data.summary || {});
    renderTaskQueuePager(data);
  } catch (e) {
    if (!isCurrent()) return;
    body.innerHTML = empty(8, "任务队列加载失败", "i-info", e.message || "请稍后重试");
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
  const isCurrent = beginViewRequest("task-queue-badge");
  TASK_QUEUE_BADGE_LOADING = true;
  try {
    const params = new URLSearchParams({ platform: GLOBAL_MODE ? "all" : PLATFORM, state: "active", page_size: "1" });
    const data = await api("/api/task-queue?" + params.toString());
    if (!isCurrent() || CURRENT_TAB === "queue") return;
    globalThis.CreatorHubWorkbench?.queueSummary?.(data.summary || {});
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

async function refreshOverviewSummary() {
  const isCurrent = beginViewRequest("overview-summary");
  const data = await api("/api/overview/summary?platform=" + PLATFORM);
  if (!isCurrent()) return;
  for (const [key, id] of Object.entries({ accounts: "stat-acc", monitors: "stat-mon", downloaded: "stat-dl", comments: "stat-cmt" })) {
    if ($(id)) $(id).textContent = fmtNum(data[key]);
  }
}
let POLL_RUNNING = false, POLL_TIMER = null, POLL_DELAY = 8000;
async function loop() {
  if (POLL_RUNNING) return;
  clearTimeout(POLL_TIMER);
  if (INFLIGHT > 0 || document.hidden || globalThis.CreatorHubWorkbench?.isInteracting?.()) {
    POLL_TIMER = setTimeout(loop, POLL_DELAY);
    return;
  }
  POLL_RUNNING = true;
  const tab = CURRENT_TAB, platform = PLATFORM;
  const failuresBefore = _apiFailures;
  const focused = document.activeElement;
  const editing = focused && focused.matches("input, textarea, select, .cs-trg, .dt-trg, [contenteditable='true']");
  const refreshers = {
    overview: [refreshOverviewSummary, refreshOverviewChart],
    accounts: [refreshAccounts],
    monitors: [refreshMonitors, refreshContents],
    comments: [refreshWatches, refreshComments],
    danmaku: [refreshDanmakuWatches, refreshDanmaku],
    autocomment: [refreshCommentRules, refreshCommentTasks],
    publish: [refreshPublish],
    collections: [refreshCollections],
    "risk-control": [refreshRiskCenter],
    queue: [refreshTaskQueue],
    hub: [refreshHubSummary],
  };
  const jobs = editing ? [] : (refreshers[tab] || []);
  if (tab !== "queue") jobs.push(refreshTaskQueueBadge);
  try {
    const results = await Promise.allSettled(jobs.map(fn => Promise.resolve().then(() => fn())));
    const failed = _apiFailures > failuresBefore || results.some(result => result.status === "rejected");
    POLL_DELAY = failed ? Math.min(POLL_DELAY * 2, 60000) : 8000;
    const panel = document.querySelector(`[data-panel="${tab}"]`);
    if (panel && CURRENT_TAB === tab && PLATFORM === platform) {
      panel.dataset.refreshState = failed ? "stale" : "ready";
      if (failed) panel.setAttribute("aria-label", "刷新暂时失败，显示上次结果，将自动重试");
      else panel.removeAttribute("aria-label");
    }
  } finally {
    POLL_RUNNING = false;
    POLL_TIMER = setTimeout(loop, CURRENT_TAB !== tab || PLATFORM !== platform ? 0 : POLL_DELAY);
  }
}

// initial skeletons while data loads
$("mon-table").innerHTML = skeleton(8);
$("content-table").innerHTML = skeleton(8);
$("sd-history-body").innerHTML = skeleton(8);
$("watch-table").innerHTML = skeleton(8);
$("comment-table").innerHTML = skeleton(6);
$("danmaku-watch-table").innerHTML = skeleton(8);
$("danmaku-table").innerHTML = skeleton(6);
$("collection-job-table").innerHTML = collectionTaskSkeleton(3);
$("collection-content-list").innerHTML = collectionResultSkeleton(4);
$("queue-table").innerHTML = skeleton(8);

// restore last-selected platform & global mode BEFORE restoring tab so global-mode tab guards apply cleanly
PLATFORM = (() => { try { const p = localStorage.getItem("dym-pf"); return ["xhs", "rednote", "douyin", "kuaishou", "shipinhao", "tiktok", "bilibili", "instagram", "twitter"].includes(p) ? p : "douyin"; } catch (e) { return "douyin"; } })();
GLOBAL_MODE = (() => { try { const g = localStorage.getItem("dym-global"); return g === null ? true : g === "1"; } catch (e) { return true; } })();
if ($("queue-platform") && GLOBAL_MODE) $("queue-platform").value = "all";

// restore last-selected section (default: 总览);旧版四个独立页已并入「账号管理」
const VALID_TABS = ["overview", "media-hub", "accounts", "risk-control", "queue", "collections", "monitors", "comments", "danmaku", "hub", "tiktok-tools", "publish", "autocomment", "share-download", "notifications", "settings"];
const LEGACY_HUB_TABS = ["myworks", "following", "fans", "dm"];
switchTab((() => {
  try {
    const hashTab = decodeURIComponent(location.hash.replace(/^#/, ""));
    if (hashTab === "agent" || (!hashTab && localStorage.getItem("dym-tab") === "agent")) {
      window.CreatorHubOpenAssistant = true;
      return "overview";
    }
    const candidate = VALID_TABS.includes(hashTab)
      ? hashTab
      : (() => {
          const t = localStorage.getItem("dym-tab");
          if (LEGACY_HUB_TABS.includes(t)) { HUB_TAB = t; return "hub"; }
          return VALID_TABS.includes(t) ? t : "overview";
        })();
    if (GLOBAL_MODE && SINGLE_PF_ONLY_TABS.includes(candidate)) return "overview";
    return candidate;
  } catch (e) { return "overview"; }
})());
switchHubTab(HUB_TAB);   // 恢复上次停留的子标签(我的作品/关注/粉丝/私信)
applyPlatformUI();
updateTaskQueuePlatformLabel();

setupMonitorInterval("t-interval", 300);
setupMonitorInterval("w-interval", 600);
setupMonitorInterval("d-w-interval", 0, true);
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
  if (tab === "agent") {
    history.replaceState(null, "", "#" + CURRENT_TAB);
    window.dispatchEvent(new Event("creatorhub:assistant:open"));
    return;
  }
  if (VALID_TABS.includes(tab) && tab !== CURRENT_TAB) {
    switchTab(tab);
    globalThis.CreatorHubWorkbench?.restoreScroll?.();
  }
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
    const visible = window.scrollY > 520, button = $("backtop");
    button.classList.toggle("show", visible);
    button.tabIndex = visible ? 0 : -1;
    button.setAttribute("aria-hidden", String(!visible));
    _backTopTick = false;
  });
}, { passive: true });
document.addEventListener("visibilitychange", () => { if (!document.hidden) loop(); });

function launchPlatformLogin(pf, mode = "main") {
  if (pf && pf !== "all" && PLATFORM !== pf) {
    PLATFORM = pf;
    ACCOUNTS = ALL_ACCOUNTS.filter(a => a.platform === PLATFORM);
    applyPlatformUI();
  }
  if (mode === "cookie") {
    toggleCookie();
    requestAnimationFrame(() => $("ck-val")?.focus());
    return;
  }
  const creator = mode === "creator";
  if (pf === "xhs") return creator ? startXhsCreatorLogin() : startXhsLogin();
  if (pf === "rednote") return creator ? startRednoteCreatorLogin() : startRednoteLogin();
  if (pf === "kuaishou") return creator ? startKsCreatorLogin() : startKsLogin();
  if (pf === "shipinhao") return startChannelsLogin();
  if (pf === "tiktok") return startTikTokLogin();
  if (pf === "bilibili") return startBilibiliLogin();
  if (pf === "instagram") return startInstagramLogin();
  if (pf === "twitter") return startTwitterLogin();
  return creator ? startCreatorLogin() : startLogin();
}

// Narrow bridge for React islands; business mutations remain in their original handlers.
window.CreatorHubBridge = {
  selectPlatform: switchPlatform,
  getContext: () => ({ tab: CURRENT_TAB, platform: PLATFORM, scope: GLOBAL_MODE ? "all" : PLATFORM, globalMode: GLOBAL_MODE }),
  getAgentContext() {
    const job = CURRENT_TAB === "collections" && COLLECTION_JOB_ID
      ? COLLECTION_JOBS.find(item => item.id === COLLECTION_JOB_ID && item.platform === PLATFORM) : null;
    return { tab: CURRENT_TAB, platform: GLOBAL_MODE ? "all" : PLATFORM, title: (PAGE_META[CURRENT_TAB] || PAGE_META.overview).title,
      task: job ? { task_type: "collections", id: job.id, platform: job.platform, status: job.status } : null };
  },
  navigate: switchTab,
  openAccount: openAccountHub,
  openAggregatedDm,
  launchPlatformLogin,
  accountActions: {
    openBrowser: id => openAccountBrowser(Number(id)),
    relogin: (id, scope = "auto") => relogin(Number(id), scope),
    refreshProfile: id => refreshProfile(Number(id)),
    setProxy: id => setProxy(Number(id)),
    testProxy: id => testProxy(Number(id)),
    setBrowserBackend: id => setBrowserBackend(Number(id)),
    manageFingerprint: id => manageFingerprint(Number(id)),
    checkEnvironment: id => checkBrowserEnvironment(Number(id)),
    delAccount: id => delAccount(Number(id)),
    refreshAll: () => { refreshAccounts(); refreshProxies(); },
  },
  openQueue(state, allPlatforms = GLOBAL_MODE) {
    if ($("queue-platform")) { $("queue-platform").value = allPlatforms ? "all" : "current"; $("queue-platform")._csSync?.(); }
    $("queue-state").value = state; $("queue-state")._csSync?.();
    $("queue-query").value = ""; $("queue-type").value = ""; $("queue-type")._csSync?.();
    TASK_QUEUE_PAGE = 1;
    switchTab("queue", true);
  },
  async retry() {
    const refreshers = {
      overview: [refreshOverviewSummary, refreshOverviewChart, refreshTaskQueueBadge, () => window.dispatchEvent(new Event("creatorhub:matrix:refresh"))],
      "media-hub": [() => window.dispatchEvent(new Event("creatorhub:matrix:refresh"))],
      accounts: [refreshAccounts, refreshBrowserRuntimes, refreshProxies, () => window.dispatchEvent(new Event("creatorhub:matrix:refresh"))],
      monitors: [refreshMonitors, refreshContents], comments: [refreshWatches, refreshComments],
      danmaku: [refreshDanmakuWatches, refreshDanmaku], collections: [refreshCollections],
      queue: [refreshTaskQueue], publish: [refreshPublish, () => window.dispatchEvent(new Event("creatorhub:matrix:refresh"))],
      autocomment: [refreshCommentRules, refreshCommentTasks],
      notifications: [refreshChannels, refreshWebhooks], settings: [loadSettings],
      "risk-control": [refreshRiskCenter], hub: [refreshHubSummary, refreshHubPanel, () => window.dispatchEvent(new Event("creatorhub:matrix:refresh"))],
      "share-download": [refreshShareHistory],
    };
    await Promise.allSettled((refreshers[CURRENT_TAB] || []).map(fn => Promise.resolve().then(() => fn())));
  },
};
