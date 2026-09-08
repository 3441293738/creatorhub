// Run before styles/first paint. Appearance is local to this browser, not an account.
(function () {
  "use strict";
  const root = document.documentElement;
  const key = "creatorhub-appearance";
  const defaults = { theme: "system", density: "comfortable", motion: "system" };
  const media = typeof window.matchMedia === "function" ? window.matchMedia("(prefers-color-scheme: dark)") : null;
  let saved = true;
  function normalize(value) {
    value = value && typeof value === "object" ? value : {};
    return {
      theme: ["light", "dark", "system"].includes(value.theme) ? value.theme : defaults.theme,
      density: ["comfortable", "compact"].includes(value.density) ? value.density : defaults.density,
      motion: ["system", "reduced"].includes(value.motion) ? value.motion : defaults.motion,
    };
  }
  function read() {
    try { return normalize(JSON.parse(localStorage.getItem(key))); }
    catch (_) { return { ...defaults }; }
  }
  let preference = read();
  function snapshot() {
    return { ...preference, resolvedTheme: preference.theme === "system" ? (media && media.matches ? "dark" : "light") : preference.theme, saved };
  }
  function syncControls() {
    const state = snapshot();
    document.querySelectorAll("[data-theme-choice]").forEach(button => {
      button.setAttribute("aria-pressed", String(button.dataset.themeChoice === state.theme));
    });
    document.querySelectorAll('input[name="appearance-theme"]').forEach(input => { input.checked = input.value === state.theme; });
    document.querySelectorAll('input[name="appearance-density"]').forEach(input => { input.checked = input.value === state.density; });
    const motion = document.getElementById("appearance-motion");
    if (motion) motion.checked = state.motion === "reduced";
    const status = document.getElementById("appearance-status");
    if (status) status.textContent = `${state.theme === "system" ? "跟随系统 · 当前" : "当前"}${state.resolvedTheme === "dark" ? "夜间" : "日间"}主题 · ${saved ? "偏好自动保存在此浏览器" : "已应用于当前页面，浏览器未保存偏好"}`;
  }
  function syncBrowserChrome() {
    const meta = document.querySelector('meta[name="theme-color"]');
    if (!meta) return;
    const background = document.body && window.getComputedStyle
      ? window.getComputedStyle(document.body).getPropertyValue("--bg").trim() : "";
    meta.content = background || (snapshot().resolvedTheme === "dark" ? "#090b10" : "#f5f5f7");
  }
  function apply() {
    const state = snapshot();
    root.dataset.theme = state.resolvedTheme;
    root.dataset.themeMode = state.theme;
    root.dataset.density = state.density;
    root.dataset.motion = state.motion;
    root.style.colorScheme = state.resolvedTheme;
    syncBrowserChrome();
    syncControls();
    window.dispatchEvent(new CustomEvent("creatorhub:appearance", { detail: state }));
  }
  function set(changes) {
    preference = normalize({ ...preference, ...changes });
    try { localStorage.setItem(key, JSON.stringify(preference)); saved = true; }
    catch (_) { saved = false; }
    apply();
  }
  window.CreatorHubAppearance = Object.freeze({ set, get: snapshot });
  apply();
  function bind() {
    syncControls();
    syncBrowserChrome();
    if (document.body && window.MutationObserver) {
      new window.MutationObserver(syncBrowserChrome).observe(document.body, { attributes: true, attributeFilter: ["class"] });
    }
    document.querySelectorAll("[data-theme-choice]").forEach(button => {
      button.addEventListener("click", () => set({ theme: button.dataset.themeChoice }));
    });
    document.querySelectorAll('input[name="appearance-theme"]').forEach(input => {
      input.addEventListener("change", () => { if (input.checked) set({ theme: input.value }); });
    });
    document.querySelectorAll('input[name="appearance-density"]').forEach(input => {
      input.addEventListener("change", () => { if (input.checked) set({ density: input.value }); });
    });
    const motion = document.getElementById("appearance-motion");
    if (motion) motion.addEventListener("change", () => set({ motion: motion.checked ? "reduced" : "system" }));
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", bind, { once: true });
  else bind();
  const onSystemChange = () => { if (preference.theme === "system") apply(); };
  if (media && media.addEventListener) media.addEventListener("change", onSystemChange);
  else if (media && media.addListener) media.addListener(onSystemChange);
  window.addEventListener("storage", event => {
    if (event.key !== key && event.key !== null) return;
    preference = read(); saved = true; apply();
  });
})();
