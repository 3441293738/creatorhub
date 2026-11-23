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
