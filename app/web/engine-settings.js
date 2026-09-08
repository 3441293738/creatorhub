/* Local configuration editor: explicit saves, partial patches, preserved drafts. */
(() => {
  "use strict";
  const form = document.getElementById("engine-settings-form");
  if (!form) return;
  const fields = [...form.querySelectorAll("input[name],select[name]")];
  const byName = Object.fromEntries(fields.map(el => [el.name, el]));
  const status = document.getElementById("engine-settings-status");
  const summary = document.getElementById("engine-settings-errors");
  const saveButton = document.getElementById("engine-settings-save");
  const defaultsButton = document.getElementById("engine-settings-defaults");
  const retryButton = document.getElementById("engine-settings-retry");
  const saveLabel = saveButton.innerHTML;
  let baseline = null, recommended = null, busy = false, dirty = false, sequence = 0;

  const value = el => {
    if (el.type === "checkbox") return el.checked;
    if (el.type !== "number") return el.value;
    if (!el.value.trim()) return NaN;
    const scaled = Number(el.value) * Number(el.dataset.scale || 1);
    // The minutes control stores whole seconds. Preserve file values such as
    // 962 seconds across a display round-trip, without inventing an edited field.
    return el.dataset.scale ? Math.round(scaled) : scaled;
  };
  const values = () => Object.fromEntries(fields.map(el => [el.name, value(el)]));
  const changedFields = () => baseline ? fields.filter(el => !Object.is(value(el), baseline[el.name])) : [];
  function message(text, state = "") { status.textContent = text; status.dataset.state = state; }
  function syncFieldA11y(el) {
    if (el.tagName !== "SELECT") return;
    const target = el.parentElement.querySelector(".cs-trg");
    if (!target) return;
    for (const name of ["aria-describedby", "aria-invalid"]) {
      const attribute = el.getAttribute(name);
      if (attribute) target.setAttribute(name, attribute);
      else target.removeAttribute(name);
    }
  }
  function fieldError(el, error) {
    const valid = setFieldError(el, error);
    syncFieldA11y(el);
    return valid;
  }
  function controls() {
    fields.forEach(el => { el.disabled = busy || !baseline; });
    saveButton.disabled = defaultsButton.disabled = busy || !baseline;
    retryButton.disabled = busy;
    saveButton.innerHTML = busy ? "保存中…" : saveLabel;
    saveButton.classList.toggle("busy", busy);
    if (typeof csSyncAll === "function") csSyncAll();
    fields.forEach(syncFieldA11y);
  }
  function validResponse(data) {
    if (!data?.values || !data?.defaults || fields.some(el =>
        !(el.name in data.values) || !(el.name in data.defaults))) {
      throw new Error("返回的配置不完整，请重新读取");
    }
    return data;
  }
  function render(data) {
    fields.forEach(el => {
      if (el.type === "checkbox") el.checked = data[el.name];
      else el.value = el.type === "number" ? data[el.name] / Number(el.dataset.scale || 1) : data[el.name];
      el.dataset.wbEdited = "false";
      fieldError(el, "");
    });
    summary.hidden = true; summary.replaceChildren();
    controls();
  }
  function changed() {
    if (busy || !baseline) return;
    dirty = changedFields().length > 0;
    message(dirty ? "有未保存的修改" : "与已读取的设置一致", dirty ? "warning" : "");
  }
  function validate(el) {
    let error = "";
    if (el.type === "number") {
      const number = Number(el.value);
      if (!el.value.trim() || !Number.isFinite(number)) error = "请输入有效数字";
      else if (number < Number(el.min) || number > Number(el.max)) error = `请填写 ${el.min}–${el.max} 之间的数值`;
      else if (el.step === "1" && !Number.isInteger(number)) error = "请填写整数";
    } else if (el.tagName === "SELECT" && ![...el.options].some(option => option.value === el.value)) {
      error = "请选择有效选项";
    }
    return fieldError(el, error);
  }
  function focusField(el) {
    for (let details = el.closest("details"); details; details = details.parentElement.closest("details")) details.open = true;
    const target = el.tagName === "SELECT" ? el.parentElement.querySelector(".cs-trg") || el : el;
    target.focus();
  }
  function showErrors(invalid) {
    summary.replaceChildren();
    const title = document.createElement("b"); title.textContent = "请先检查以下配置，修改内容已保留：";
    const list = document.createElement("ul");
    invalid.forEach(el => {
      const li = document.createElement("li"), link = document.createElement("a");
      link.href = "#" + el.id;
      link.textContent = (el.labels?.[0]?.textContent.trim() || el.name) + "：" + document.getElementById(el.id + "-error").textContent;
      link.addEventListener("click", event => { event.preventDefault(); focusField(el); });
      li.append(link); list.append(li);
    });
    summary.append(title, list); summary.hidden = false; summary.focus();
    message("尚未保存，请修正标出的配置项", "error");
  }
  async function load() {
    if (busy) return;
    const current = ++sequence;
    try {
      const data = validResponse(await api("/api/settings/engine"));
      if (current !== sequence || busy) return;
      recommended = { ...data.defaults };
      if (!dirty) { baseline = { ...data.values }; render(baseline); message("已读取当前设置"); }
      else message("已保留未保存的修改，请确认后保存", "warning");
      retryButton.hidden = true;
    } catch (error) {
      if (current !== sequence || busy) return;
      message("读取失败：" + error.message + (dirty ? "；修改已保留" : ""), "error");
      retryButton.hidden = false;
    }
    controls();
  }
  function defaults() {
    if (busy || !recommended || !baseline) return;
    render(recommended); dirty = changedFields().length > 0;
    message(dirty ? "推荐值已填入，点击保存后生效" : "当前已是推荐值", dirty ? "warning" : "");
  }
  async function save() {
    if (busy || !baseline) return;
    const edited = changedFields();
    const invalid = edited.filter(el => !validate(el));
    if (invalid.length) { showErrors(invalid); return; }
    if (!edited.length) { message("当前没有待保存的修改"); return; }
    const draft = values();
    const patch = Object.fromEntries(edited.map(el => [el.name, draft[el.name]]));
    busy = true; ++sequence; controls(); message("正在保存…"); summary.hidden = true;
    try {
      const data = validResponse(await api("/api/settings/engine", {
        method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(patch),
      }));
      baseline = { ...data.values }; recommended = { ...data.defaults }; dirty = false;
      render(baseline); retryButton.hidden = true;
      message("已保存 · 后续任务生效，重启后保留", "success");
    } catch (error) {
      dirty = true;
      const invalid = [];
      if (Array.isArray(error.detail)) for (const item of error.detail) {
        const el = byName[item.loc?.at(-1)];
        if (el) { fieldError(el, "配置值不符合要求，请检查取值范围与类型"); invalid.push(el); }
      }
      if (invalid.length) showErrors(invalid);
      else message("保存尚未确认：" + error.message + "；修改已保留", "error");
    } finally { busy = false; controls(); }
  }
  form.addEventListener("input", changed);
  form.addEventListener("change", changed);
  fields.forEach(el => el.addEventListener("blur", () => { if (baseline && !busy) validate(el); }));
  window.addEventListener("beforeunload", event => {
    if (dirty || busy) { event.preventDefault(); event.returnValue = ""; }
  });
  window.CreatorHubEngineSettings = { load, defaults, save, validate, isDirty: () => dirty };
})();
