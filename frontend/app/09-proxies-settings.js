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
  const btn = evtBtn();
  await withBusy(btn, "测试中", async () => {
    try {
      const r = await api("/api/accounts/" + id + "/test-proxy", { method: "POST" });
      toast((r.ok ? "代理可用 ✓ " : "代理不可用 ✗ ") + (r.detail || ""), r.ok ? "ok" : "err");
    } catch (e) { toast("测试失败:" + e.message, "err"); }
  });
  refreshAccounts();
  window.dispatchEvent(new Event("creatorhub:matrix:refresh"));
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
        window.dispatchEvent(new Event("creatorhub:matrix:refresh"));
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
        refreshAccounts();
        window.dispatchEvent(new Event("creatorhub:matrix:refresh"));
        return;
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
    window.dispatchEvent(new Event("creatorhub:matrix:refresh"));
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
  const platformNames = { douyin: "抖音", xhs: "小红书", rednote: "海外小红书", kuaishou: "快手", shipinhao: "视频号", tiktok: "TikTok", bilibili: "哔哩哔哩", instagram: "Instagram", twitter: "Twitter", facebook: "Facebook" };
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
    globalThis.CreatorHubEngineSettings?.syncPlatforms?.(data?.accounts);
    if (status) status.textContent = `已核对 ${data.rows?.length || 0} 项能力、${data.accounts?.length || 0} 个账号环境`;
    return data;
  } catch (error) {
    if (status) status.textContent = "兼容矩阵读取失败：" + error.message;
    return null;
  }
}
globalThis.CreatorHubTransportMatrix = { load: refreshTransportMatrix, route: transportRoute };

// ─── 下载与统一 AI 文案设置 ───
let AI_CONFIG_STATE = null;
let AI_CHANNELS_STATE = { active_channel_id: "default", channels: [] };
let AI_CUSTOM_PRESETS = [];
let AI_ALL_PRESETS = [];

const AI_PROTO_LABELS = {
  openai_compatible: "Chat Completions",
  openai_responses: "OpenAI Responses",
  anthropic: "Anthropic Messages",
};

function openAiChannelsManager() {
  window.dispatchEvent(new Event("creatorhub:assistant:open-settings"));
}
function openAiSettingsSection() {
  if (typeof switchTab === "function") switchTab("settings", true);
  globalThis.CreatorHubWorkbench?.showSection?.("settings", "ai");
}
function _aiSelectedChannel() {
  const cid = $("ai-channel-id")?.value || AI_CHANNELS_STATE.active_channel_id || "default";
  return (AI_CHANNELS_STATE.channels || []).find(c => c.id === cid) || null;
}
function populateAiChannelSelectors(selectedId = "") {
  const sel = $("ai-channel-id");
  if (!sel) return;
  const channels = AI_CHANNELS_STATE.channels || [];
  const active = channels.find(c => c.id === AI_CHANNELS_STATE.active_channel_id);
  const activeHint = active
    ? `${active.display_name}${active.model ? `（${active.model}）` : "（未配模型）"}`
    : "默认渠道";
  const opts = [`<option value="">跟随 AI 助手当前渠道 · ${esc(activeHint)}</option>`];
  channels.forEach(c => {
    const status = c.configured ? (AI_PROTO_LABELS[c.provider] || c.provider) : "未完成配置";
    opts.push(`<option value="${esc(c.id)}">${esc(c.display_name)} · ${esc(c.model || "未选模型")}（${esc(status)}）</option>`);
  });
  sel.innerHTML = opts.join("");
  if (selectedId && [...sel.options].some(o => o.value === selectedId)) sel.value = selectedId;
  else sel.value = "";
  onAiChannelChange();
}
function onAiChannelChange() {
  const ch = _aiSelectedChannel();
  const dl = $("ai-channel-models-datalist");
  if (dl) {
    const models = ch?.models || [];
    dl.innerHTML = models.map(m => `<option value="${esc(m.id)}">${esc(m.name && m.name !== m.id ? `${m.name} (${m.id})` : m.id)}</option>`).join("");
  }
  const help = $("ai-channel-help");
  if (help) {
    if (!ch) {
      help.textContent = "暂无可用共享渠道，请点击右上角「添加/编辑渠道」在 AI 助手中先配置一个模型渠道。";
    } else if (!ch.configured) {
      help.textContent = `当前渠道「${ch.display_name}」尚未配置接口地址或模型 ID，请先到 AI 助手模型设置中补全保存。`;
    } else {
      help.textContent = `当前指向：${ch.display_name} · ${AI_PROTO_LABELS[ch.provider] || ch.provider} · 默认模型 ${ch.model}。`;
    }
  }
  syncAiStatusBanner();
  csSyncAll();
}
function populateAiPresetSelect(selectedId = "") {
  const sel = $("ai-default-preset");
  if (!sel) return;
  sel.innerHTML = (AI_ALL_PRESETS || []).map(p =>
    `<option value="${esc(p.id)}">${esc(p.name)}${p.builtin ? "" : "（自定义）"}</option>`
  ).join("");
  if (selectedId && [...sel.options].some(o => o.value === selectedId)) sel.value = selectedId;
  onAiPresetChange();
}
function onAiPresetChange() {
  const pid = $("ai-default-preset")?.value || "natural_cn";
  const preset = (AI_ALL_PRESETS || []).find(p => p.id === pid);
  const desc = $("ai-preset-desc");
  if (desc && preset) {
    desc.textContent = `${preset.description || preset.style_prompt.slice(0, 80)}（建议 ≤${preset.max_chars} 字；单条规则可单独覆盖）`;
  }
}
function onAiSourceChange() {
  const isCustom = Boolean($("ai-source-custom")?.checked);
  if ($("ai-panel-channel")) $("ai-panel-channel").hidden = isCustom;
  if ($("ai-panel-custom")) $("ai-panel-custom").hidden = !isCustom;
  syncAiStatusBanner();
  csSyncAll();
}
function onAiProxyModeChange() {
  const mode = $("ai-proxy-mode")?.value || "direct";
  const wrap = $("ai-proxy-url-wrap");
  if (wrap) wrap.hidden = mode !== "custom";
  if (mode !== "custom") setFieldError($("ai-proxy-url"), "");
}
function onAiClearKeyToggle() {
  const keyEl = $("ai-key");
  const clr = Boolean($("ai-clear-key")?.checked);
  if (keyEl) {
    keyEl.disabled = clr;
    if (clr) keyEl.value = "";
  }
}
function syncAiStatusBanner() {
  const strip = $("ai-status-strip");
  const title = $("ai-status-title");
  const desc = $("ai-status-desc");
  if (!strip || !title || !desc) return;
  const enabled = Boolean($("ai-enabled")?.checked);
  const isCustom = Boolean($("ai-source-custom")?.checked);
  let ready = false, routeText = "", reason = "";
  if (isCustom) {
    const base = ($("ai-base")?.value || "").trim();
    const model = ($("ai-model")?.value || "").trim();
    const proto = AI_PROTO_LABELS[$("ai-provider")?.value || "openai_compatible"] || "OpenAI";
    ready = Boolean(base && model);
    routeText = ready ? `独立接口 · ${proto} · ${model}` : "独立接口尚未填完地址或模型";
    reason = ready ? "" : "请填写接口基础地址与模型名称";
  } else {
    const ch = _aiSelectedChannel();
    const ovModel = ($("ai-channel-model-override")?.value || "").trim();
    const model = ovModel || ch?.model || "";
    ready = Boolean(ch?.base_url && model);
    const proto = AI_PROTO_LABELS[ch?.provider || "openai_compatible"] || "";
    routeText = ch
      ? `复用共享渠道「${ch.display_name}」· ${proto} · ${model || "未选模型"}`
      : "尚未配置共享模型渠道";
    reason = ready ? "" : "请先在「管理共享模型渠道」中配置接口地址与模型";
  }
  if (!enabled) {
    strip.dataset.state = "off";
    title.textContent = "AI 文案生成当前未启用";
    desc.textContent = `当前路由：${routeText}。开启下方总开关并保存后，勾选 AI 的自动评论规则即可生效。`;
  } else if (ready) {
    strip.dataset.state = "ready";
    title.textContent = `AI 文案已就绪 · ${routeText}`;
    desc.textContent = "自动评论规则可直接使用此配置，也可按单条规则覆盖人设预设或指定模型。";
  } else {
    strip.dataset.state = "warn";
    title.textContent = "AI 文案已开启但模型未就绪";
    desc.textContent = `${routeText} — ${reason}。`;
  }
}
function buildAiConfigDraft() {
  const isCustom = Boolean($("ai-source-custom")?.checked);
  const rawTemp = ($("ai-temp")?.value ?? "").trim();
  const tempVal = rawTemp === "" ? null : Number(rawTemp);
  return {
    enabled: Boolean($("ai-enabled")?.checked),
    source: isCustom ? "custom" : "channel",
    channel_id: $("ai-channel-id")?.value || "",
    model_override: ($("ai-channel-model-override")?.value || "").trim(),
    provider: $("ai-provider")?.value || "openai_compatible",
    base_url: ($("ai-base")?.value || "").trim(),
    api_key: ($("ai-key")?.value || "").trim(),
    clear_api_key: Boolean($("ai-clear-key")?.checked),
    api_path_mode: $("ai-path-mode")?.value || "auto",
    model: ($("ai-model")?.value || "").trim(),
    output_token_parameter: $("ai-token-param")?.value || "max_tokens",
    temperature: Number.isFinite(tempVal) ? tempVal : null,
    max_output_tokens: Math.max(64, Math.min(2048, Number($("ai-max-output-tokens")?.value) || 256)),
    timeout_seconds: Math.max(5, Math.min(90, Number($("ai-timeout")?.value) || 25)),
    max_chars: Math.max(15, Math.min(300, Number($("ai-max-chars")?.value) || 60)),
    language_mode: $("ai-language-mode")?.value || "auto",
    disable_thinking: Boolean($("ai-disable-thinking")?.checked),
    proxy_mode: $("ai-proxy-mode")?.value || "direct",
    proxy_url: ($("ai-proxy-url")?.value || "").trim(),
    default_preset_id: $("ai-default-preset")?.value || "natural_cn",
    custom_prompt: $("ai-prompt")?.value || "",
    custom_presets: AI_CUSTOM_PRESETS || [],
  };
}
function applyAiSettingsData(s, { force = false } = {}) {
  if (!s) return;
  AI_CHANNELS_STATE = s.ai_channels || AI_CHANNELS_STATE;
  const cfg = s.ai_config || {};
  AI_CONFIG_STATE = cfg;
  AI_ALL_PRESETS = Array.isArray(cfg.presets) ? cfg.presets : [];
  AI_CUSTOM_PRESETS = Array.isArray(cfg.custom_presets) ? cfg.custom_presets : [];
  if (!$("ai-enabled")) return;
  const assign = (id, prop, val) => {
    const el = $(id);
    if (!el) return;
    if (force) {
      delete el.dataset.wbEdited;
      el[prop] = val;
    } else if (!globalThis.CreatorHubWorkbench?.isDirty?.(id)) {
      el[prop] = val;
    }
  };
  assign("ai-enabled", "checked", !!cfg.enabled);
  const isCustom = cfg.source === "custom";
  if ($("ai-source-channel")) $("ai-source-channel").checked = !isCustom;
  if ($("ai-source-custom")) $("ai-source-custom").checked = isCustom;
  populateAiChannelSelectors(cfg.channel_id || "");
  assign("ai-channel-model-override", "value", cfg.model_override || "");
  assign("ai-provider", "value", cfg.provider || "openai_compatible");
  assign("ai-path-mode", "value", cfg.api_path_mode || "auto");
  assign("ai-base", "value", cfg.base_url || "");
  assign("ai-model", "value", cfg.model || "");
  assign("ai-token-param", "value", cfg.output_token_parameter || "max_tokens");
  assign("ai-temp", "value", cfg.temperature === null || cfg.temperature === undefined ? "" : String(cfg.temperature));
  assign("ai-max-chars", "value", String(cfg.max_chars || 60));
  assign("ai-language-mode", "value", cfg.language_mode || "auto");
  assign("ai-max-output-tokens", "value", String(cfg.max_output_tokens || 256));
  assign("ai-timeout", "value", String(cfg.timeout_seconds || 25));
  assign("ai-proxy-mode", "value", cfg.proxy_mode || "direct");
  assign("ai-proxy-url", "value", cfg.proxy_url || "");
  assign("ai-disable-thinking", "checked", cfg.disable_thinking !== false);
  assign("ai-prompt", "value", cfg.custom_prompt || "");
  populateAiPresetSelect(cfg.default_preset_id || "natural_cn");
  if ($("ai-key")) {
    $("ai-key").value = "";
    $("ai-key").disabled = false;
    $("ai-key").placeholder = cfg.api_key_set ? "已保存（留空保留原密钥）" : "输入 API Key；本地无鉴权服务可留空";
  }
  if ($("ai-clear-key")) $("ai-clear-key").checked = false;
  if ($("ai-clear-key-wrap")) $("ai-clear-key-wrap").style.display = cfg.api_key_set ? "flex" : "none";
  if ($("ai-migrate-btn")) $("ai-migrate-btn").hidden = !(isCustom && cfg.base_url && cfg.model);
  onAiProxyModeChange();
  onAiSourceChange();
  csSyncAll();
}
async function refreshAiSharedChannels() {
  try {
    const s = await api("/api/settings");
    AI_CHANNELS_STATE = s.ai_channels || AI_CHANNELS_STATE;
    populateAiChannelSelectors($("ai-channel-id")?.value || "");
    toast("已刷新共享模型渠道列表", "ok", 2200);
  } catch (e) { toast("刷新渠道失败：" + e.message, "err"); }
}
async function loadSettings() {
  globalThis.CreatorHubEngineSettings?.load();
  globalThis.CreatorHubChannelsProtocol?.load?.();
  globalThis.CreatorHubDiagnostics?.load?.();
  refreshTransportMatrix();
  try {
    const s = await api("/api/settings");
    const assign = (id, property, value) => {
      if ($(id) && !globalThis.CreatorHubWorkbench?.isDirty?.(id)) $(id)[property] = value;
    };
    assign("dl-dir", "value", s.download_dir || "");
    assign("dl-quality", "value", s.video_quality || "highest");
    applyAiSettingsData(s);
    csSyncAll();
  } catch (e) {}
}
async function saveAiSettings() {
  if (!validateAiSettings(false)) return;
  $("ai-msg").textContent = "保存中…";
  const ai_config = buildAiConfigDraft();
  await withBusy(evtBtn(), "保存中", async () => {
    try {
      const s = await api("/api/settings", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ai_config }),
      });
      applyAiSettingsData(s, { force: true });
      $("ai-msg").textContent = "已保存 ✓ " + (s.ai_config?.enabled ? "（已生效于自动评论规则）" : "（当前总开关未启用）");
      toast("AI 文案设置已保存", "ok");
      window.dispatchEvent(new Event("creatorhub:ai-config:updated"));
    } catch (e) {
      $("ai-msg").textContent = "保存失败：" + e.message;
      toast("保存失败：" + e.message, "err", 7000);
    }
  });
}
async function migrateAiToChannel() {
  const name = await uiPrompt({
    title: "转存为共享模型渠道",
    hint: "将把当前独立接口的地址、协议、密钥和模型保存为一个共享渠道（与 AI 助手互通），并自动切换为「复用共享渠道」模式。",
    value: "AI 文案渠道",
    placeholder: "输入渠道显示名称",
  });
  if (name === null) return;
  try {
    const res = await api("/api/settings/ai-migrate-to-channel", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ display_name: name.trim() || "AI 文案渠道" }),
    });
    applyAiSettingsData({ ai_config: res.ai_config, ai_channels: res.ai_channels }, { force: true });
    toast("已转存为共享渠道并自动切换 ✓", "ok", 5000);
    window.dispatchEvent(new Event("creatorhub:ai-config:updated"));
  } catch (e) {
    toast("转存失败：" + e.message, "err", 7000);
  }
}
async function testAi() {
  const btn = evtBtn();
  if (!validateAiSettings(true)) return;
  if ($("ai-test-drawer")) $("ai-test-drawer").open = true;
  $("ai-msg").textContent = "实景测试生成中…";
  const out = $("ai-test-output");
  if (out) { out.hidden = false; out.removeAttribute("data-tone"); out.textContent = "正在调用模型生成样例文案…"; }
  const payload = {
    ai_config: buildAiConfigDraft(),
    preset_id: $("ai-default-preset")?.value || "natural_cn",
    sample_platform: $("ai-test-platform")?.value || "douyin",
    sample_mode: $("ai-test-mode")?.value || "auto_reply",
    sample_work_desc: $("ai-test-work")?.value || "",
    sample_source_text: $("ai-test-source")?.value || "",
  };
  await withBusy(btn, "测试中", async () => {
    try {
      const r = await api("/api/settings/ai-test", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (r.ok) {
        const meta = [r.source_label, r.model, r.preset_name, r.elapsed_ms ? `${r.elapsed_ms}ms` : ""].filter(Boolean).join(" · ");
        $("ai-msg").textContent = `测试成功 ✓（${r.elapsed_ms || 0}ms）`;
        if (out) {
          out.dataset.tone = "ok";
          out.innerHTML = `<div class="mut" style="font-size:11.5px;margin-bottom:4px">${esc(meta)}</div><b>${esc(r.sample || "")}</b>`;
        }
        toast("AI 实景生成正常 ✓", "ok", 5000);
      } else {
        $("ai-msg").textContent = "生成失败：" + (r.error || "");
        if (out) { out.dataset.tone = "err"; out.textContent = "测试失败：" + (r.error || ""); }
        toast("AI 测试失败：" + (r.error || ""), "err", 8000);
      }
    } catch (e) {
      $("ai-msg").textContent = "失败：" + e.message;
      if (out) { out.dataset.tone = "err"; out.textContent = "测试失败：" + e.message; }
      toast("测试失败：" + e.message, "err", 8000);
    }
  });
}
async function manageAiPresets() {
  let working = (AI_CUSTOM_PRESETS || []).map(x => ({ ...x }));
  const builtins = (AI_ALL_PRESETS || []).filter(p => p.builtin);
  let editingIndex = -1;

  const renderModalBody = () => {
    const body = $("ui-body");
    if (!body) return;
    body.innerHTML = `
      <div class="stack" style="gap:12px">
        <div class="ac-section-box">
          <div class="ac-section-title"><span>${editingIndex >= 0 ? "编辑自定义人设" : "新建自定义人设"}</span></div>
          <div class="form-grid">
            <div class="form-field"><label for="ap-name">人设名称</label><input id="ap-name" maxlength="60" placeholder="例如：美妆博主闺蜜口吻 / 跨境独立站客服"></div>
            <div class="form-field"><label for="ap-lang">语种</label><select id="ap-lang"><option value="auto">自动匹配</option><option value="zh">中文</option><option value="en">英文</option></select></div>
            <div class="form-field"><label for="ap-chars">建议字数上限</label><input id="ap-chars" type="number" min="15" max="300" value="60"></div>
            <div class="form-field"><label for="ap-desc">简短说明</label><input id="ap-desc" maxlength="200" placeholder="说明适用场景（可选）"></div>
          </div>
          <div class="form-field"><label for="ap-style">风格与人设要求</label><textarea id="ap-style" rows="3" maxlength="2000" placeholder="描述语气、口吻、禁忌词与互动策略…"></textarea></div>
          <div style="display:flex;gap:8px;justify-content:flex-end">
            ${editingIndex >= 0 ? `<button type="button" class="ghost sm" id="ap-cancel-edit">取消编辑</button>` : ""}
            <button type="button" class="sm" id="ap-add-btn">${editingIndex >= 0 ? "更新此人设" : "添加到自定义人设"}</button>
          </div>
        </div>
        <div class="ac-section-box">
          <div class="ac-section-title"><span>已保存的自定义人设（${working.length} / 20）</span></div>
          ${working.length ? `<ul class="ai-preset-list">${working.map((p, idx) => `
            <li class="ai-preset-item">
              <div style="min-width:0;flex:1"><b>${esc(p.name)}</b> <span class="mut" style="font-size:11px">≤${p.max_chars}字 · ${p.language_mode}</span><p>${esc(p.style_prompt || p.description || "")}</p></div>
              <div style="display:flex;gap:6px;flex:0 0 auto">
                <button type="button" class="ghost sm" data-ap-edit="${idx}">编辑</button>
                <button type="button" class="ghost sm danger" data-ap-del="${idx}">删除</button>
              </div>
            </li>`).join("")}</ul>` : `<div class="mut" style="font-size:12px">还没有自定义人设；内置的 ${builtins.length} 套常用预设可直接使用。</div>`}
        </div>
      </div>`;
    if (editingIndex >= 0 && working[editingIndex]) {
      const cur = working[editingIndex];
      $("ap-name").value = cur.name || "";
      $("ap-lang").value = cur.language_mode || "auto";
      $("ap-chars").value = String(cur.max_chars || 60);
      $("ap-desc").value = cur.description || "";
      $("ap-style").value = cur.style_prompt || "";
    }
    enhanceSelect($("ap-lang"));
    if ($("ap-cancel-edit")) $("ap-cancel-edit").onclick = () => { editingIndex = -1; renderModalBody(); };
    $("ap-add-btn").onclick = () => {
      const name = ($("ap-name").value || "").trim();
      const style = ($("ap-style").value || "").trim();
      if (!name) { toast("请填写人设名称", "err"); $("ap-name").focus(); return; }
      if (!style) { toast("请填写风格与人设要求", "err"); $("ap-style").focus(); return; }
      const item = {
        id: editingIndex >= 0 ? working[editingIndex].id : `custom_${Date.now().toString(36)}`,
        name: name.slice(0, 60),
        description: ($("ap-desc").value || "").trim().slice(0, 200),
        style_prompt: style.slice(0, 2000),
        max_chars: Math.max(15, Math.min(300, Number($("ap-chars").value) || 60)),
        temperature: 0.85,
        language_mode: $("ap-lang").value || "auto",
        builtin: false,
      };
      if (editingIndex >= 0) { working[editingIndex] = item; editingIndex = -1; }
      else {
        if (working.length >= 20) { toast("最多创建 20 个自定义人设", "err"); return; }
        working.push(item);
      }
      renderModalBody();
    };
    body.querySelectorAll("[data-ap-edit]").forEach(btn => {
      btn.onclick = () => { editingIndex = Number(btn.dataset.apEdit); renderModalBody(); };
    });
    body.querySelectorAll("[data-ap-del]").forEach(btn => {
      btn.onclick = () => {
        const idx = Number(btn.dataset.apDel);
        working.splice(idx, 1);
        if (editingIndex === idx) editingIndex = -1;
        renderModalBody();
      };
    });
  };

  const saved = await new Promise(resolve => {
    _uiResolve = resolve; _uiCancelVal = null;
    _uiGetVal = () => working;
    renderModalBody();
    _uiOpen("管理自定义文案人设", "保存后会立即同步到 AI 文案设置与所有自动评论规则的人设下拉列表。", {
      okText: "保存人设列表",
      wide: true,
      icon: "i-sparkles",
      submit: async list => {
        AI_CUSTOM_PRESETS = list;
        const draft = buildAiConfigDraft();
        draft.custom_presets = list;
        const s = await api("/api/settings", {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ ai_config: draft }),
        });
        applyAiSettingsData(s, { force: true });
      },
    });
  });
  if (saved) {
    toast("自定义人设已保存 ✓", "ok");
    window.dispatchEvent(new Event("creatorhub:ai-config:updated"));
  }
}
window.addEventListener("creatorhub:ai-channels:updated", () => {
  api("/api/settings").then(s => {
    AI_CHANNELS_STATE = s.ai_channels || AI_CHANNELS_STATE;
    populateAiChannelSelectors($("ai-channel-id")?.value || "");
  }).catch(() => {});
});
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
