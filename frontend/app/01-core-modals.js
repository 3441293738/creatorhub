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
function safeMediaUrl(value) {
  const raw = String(value || "").trim();
  if (!raw) return "";
  try {
    const url = new URL(raw, typeof location === "undefined" ? "http://localhost" : location.href);
    if (!["http:", "https:"].includes(url.protocol)) return "";
    const host = (url.hostname || "").toLowerCase();
    const isPreview = typeof document !== "undefined" && document.documentElement?.dataset?.preview === "true";
    if (!isPreview && url.protocol === "https:" && (host === "twimg.com" || host.endsWith(".twimg.com"))) {
      return `/api/tw-media-thumb?url=${encodeURIComponent(url.href)}`;
    }
    if (!isPreview && url.protocol === "https:" && (host.endsWith(".cdninstagram.com") || host.endsWith(".fbcdn.net"))) {
      return `/api/ig-media-thumb?url=${encodeURIComponent(url.href)}`;
    }
    return url.href;
  } catch (_) { return ""; }
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
let _uiResolve = null, _uiGetVal = null, _uiCancelVal = null, _uiSubmit = null, _uiBusy = false;
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
    uiEditorNotice("正在保存，请稍候…");
    $("ui-feedback").focus({ preventScroll: true });
    success = await withBusy($("ui-ok"), "保存中", () => _uiSubmit(value)) !== false;
  } catch (error) {
    uiEditorNotice(error.fieldId ? error.message : "保存未完成：" + error.message, true);
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
function _uiOpen(title, hint, { okText = "确定", danger = false, wide = false, submit = null, icon = "i-edit" } = {}) {
  const previousExtraAction = $("ui-extra-action");
  if (previousExtraAction) previousExtraAction.remove();
  $("ui-title").textContent = title || "";
  $("ui-hint").textContent = hint || "";
  $("ui-hint").hidden = !hint;
  $("ui-icon").setAttribute("href", "#" + (danger ? "i-trash" : icon));
  _uiSubmit = submit; _uiBusy = false;
  uiEditorNotice(submit ? "更改仅在保存后生效" : "");
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
