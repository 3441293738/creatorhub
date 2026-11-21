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
