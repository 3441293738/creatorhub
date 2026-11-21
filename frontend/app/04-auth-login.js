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
async function choosePreLoginBrowserBackend({ platform = "" } = {}) {
  let catalog;
  try {
    catalog = await api("/api/browser-backends");
    preLoginBrowserCatalog = catalog;
  } catch (e) {
    toast("读取登录环境失败：" + e.message, "err");
    return null;
  }
  const localOnly = platform === "xhs";
  const options = browserChoiceOptions(catalog, { localOnly });
  const selected = await uiSelect({
    title: "选择扫码登录环境",
    hint: localOnly
      ? "小红书固定使用系统 Chrome/CDP 原生环境；扫码、Cookie 和后续任务共用同一持久 Profile。"
      : "扫码、Cookie 落地和后续账号任务将使用同一浏览器内核。新的指纹环境会同时打开 BrowserScan 体检标签。",
    options,
    value: localOnly ? "local" : preLoginBrowserBackend,
  });
  if (selected !== null) preLoginBrowserBackend = selected;
  return selected;
}
function formatProxyOptionLabel(p, platform = PLATFORM) {
  const domestic = !["tiktok", "rednote", "instagram", "twitter"].includes(platform || "douyin");
  const geo = p.geo_loc ? ` · ${p.geo_loc}` : "";
  const overseasWarn = domestic && p.geo_checked && !p.is_mainland ? " · ⚠️非大陆(慢/高风控)" : (!domestic && p.geo_checked && p.is_mainland ? " · ⚠️大陆节点(海外平台高风控)" : "");
  return `${p.label}${geo}${overseasWarn} · ${p.status} · 占用${p.used_by} · ${p.masked}${p.enabled ? "" : " · 已停用"}`;
}
// 登录前选代理:返回 "" (不用) | "auto" | 具体url | null(取消)
async function choosePreLoginProxy({ platform = PLATFORM } = {}) {
  let opts = [];
  try { opts = await api("/api/proxies/options"); } catch (e) { }
  const domestic = !["tiktok", "rednote", "instagram", "twitter"].includes(platform || "douyin");
  const usablePool = opts.filter(p => (
    p.enabled && !["bad", "auth_error", "blocked"].includes(p.status)
    && Number(p.used_by || 0) === 0 && (domestic ? (!p.geo_checked || p.is_mainland) : (!p.geo_checked || !p.is_mainland))
  ));
  const preferDirect = opts.length > 0 && usablePool.length === 0;
  const autoLabel = !opts.length
    ? "自动分配（池为空时不用代理）"
    : preferDirect
      ? (domestic ? "自动分配（仅大陆/未标注节点；当前池内无可用节点）" : "自动分配（仅海外/未标注节点；当前池内无可用节点）")
      : "自动分配（占用最少）";
  const options = [
    { value: "auto", label: autoLabel },
    ...opts.map(p => ({ value: p.url, label: formatProxyOptionLabel(p, platform) })),
    { value: "__custom__", label: "✎ 手动输入指定代理…" },
    { value: "", label: "不用代理（使用本机网络）" },
  ];
  const v = await uiSelect({
    title: "选择本次登录使用的代理",
    hint: preferDirect
      ? (domestic ? "当前代理池无可用大陆节点；默认改用本机网络，避免境外代理导致国内平台加载慢或白屏。" : "当前代理池仅有大陆节点；请改选境外代理或手动输入，避免大陆节点导致海外平台连接失败或风控。")
      : "整个登录/扫码过程都走它,从一开始就绑定这条 IP(最稳)。",
    options, value: preferDirect ? "" : "auto",
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
  const proxy = await choosePreLoginProxy({ platform: "douyin" });
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
  const proxy = await choosePreLoginProxy({ platform: "douyin" });
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
  const browserBackend = await choosePreLoginBrowserBackend({ platform: "xhs" });
  if (browserBackend === null) return;
  const proxy = await choosePreLoginProxy({ platform: "xhs" });
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
      $("qrstatus").innerHTML = `${ic("i-eye")} <b>小红书官网首页已用系统 Chrome 打开</b>，请在窗口中点击「登录」并使用小红书 App 扫码。<br>如出现平台安全验证，自动任务会暂停，请只在当前窗口按提示完成。<br>主站登录成功后会保存读取登录态并自动关闭窗口。<br>如需发布，请随后单独点击「创作者登录」。`;
    }
    pollLogin(res.task_id);
  } catch (e) { $("qrstatus").textContent = "启动失败: " + e.message; toast("小红书登录启动失败:" + e.message, "err"); }
}

// ─── 小红书创作者登录(发布用) ───
async function startXhsCreatorLogin() {
  const browserBackend = await choosePreLoginBrowserBackend({ platform: "xhs" });
  if (browserBackend === null) return;
  const proxy = await choosePreLoginProxy({ platform: "xhs" });
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
  const proxy = await choosePreLoginProxy({ platform: "kuaishou" });
  if (proxy === null) return;
  const fingerprint = await configurePreLoginFingerprint(browserBackend);
  if (fingerprint === null) return;
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
  const browserBackend = await choosePreLoginBrowserBackend();
  if (browserBackend === null) return;
  const proxy = await choosePreLoginProxy({ platform: "kuaishou" });
  if (proxy === null) return;
  const fingerprint = await configurePreLoginFingerprint(browserBackend);
  if (fingerprint === null) return;
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
  const browserBackend = await choosePreLoginBrowserBackend();
  if (browserBackend === null) return;
  const proxy = await choosePreLoginProxy({ platform: "shipinhao" });
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

// ─── TikTok Web 登录（读取、采集和写操作共用） ───
async function startTikTokLogin() {
  const browserBackend = await choosePreLoginBrowserBackend({ platform: "tiktok" });
  if (browserBackend === null) return;
  const proxy = await choosePreLoginProxy({ platform: "tiktok" });
  if (proxy === null) return;
  const fingerprint = await configurePreLoginFingerprint(browserBackend);
  if (fingerprint === null) return;
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
  const browserBackend = await choosePreLoginBrowserBackend({ platform: "bilibili" });
  if (browserBackend === null) return;
  const proxy = await choosePreLoginProxy({ platform: "bilibili" });
  if (proxy === null) return;
  const fingerprint = await configurePreLoginFingerprint(browserBackend);
  if (fingerprint === null) return;
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
  const browserBackend = await choosePreLoginBrowserBackend({ platform: "rednote" });
  if (browserBackend === null) return;
  const proxy = await choosePreLoginProxy({ platform: "rednote" });
  if (proxy === null) return;
  const fingerprint = await configurePreLoginFingerprint(browserBackend);
  if (fingerprint === null) return;
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
  const browserBackend = await choosePreLoginBrowserBackend({ platform: "rednote" });
  if (browserBackend === null) return;
  const proxy = await choosePreLoginProxy({ platform: "rednote" });
  if (proxy === null) return;
  const fingerprint = await configurePreLoginFingerprint(browserBackend);
  if (fingerprint === null) return;
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
  const browserBackend = await choosePreLoginBrowserBackend({ platform: "instagram" });
  if (browserBackend === null) return;
  const proxy = await choosePreLoginProxy({ platform: "instagram" });
  if (proxy === null) return;
  const fingerprint = await configurePreLoginFingerprint(browserBackend);
  if (fingerprint === null) return;
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
  const browserBackend = await choosePreLoginBrowserBackend({ platform: "twitter" });
  if (browserBackend === null) return;
  const proxy = await choosePreLoginProxy({ platform: "twitter" });
  if (proxy === null) return;
  const fingerprint = await configurePreLoginFingerprint(browserBackend);
  if (fingerprint === null) return;
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
    if (res && res.warning) toast(`账号已保存(${res.nickname || ""})，但状态异常：${res.warning}`, "err"); else toast(PLATFORM === "twitter" && res && res.nickname ? `Token / Cookie 已导入：${res.nickname}` : "Cookie 已保存", "ok"); refreshAccounts();
  } catch (e) { toast("保存失败:" + e.message, "err"); }
}
