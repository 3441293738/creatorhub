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
