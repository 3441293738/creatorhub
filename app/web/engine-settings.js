/* Local configuration editor: beginner-friendly presets, plain-language controls,
   explicit saves, partial patches, platform prioritization and JSON backup/restore. */
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
  const exportButton = document.getElementById("eng-export-btn");
  const importButton = document.getElementById("eng-import-btn");
  const importInput = document.getElementById("eng-import-file");
  const stickyBar = document.getElementById("eng-sticky-bar");
  const saveLabel = saveButton.innerHTML;

  // Compound human-friendly controls (backed by real hidden fields).
  const scanPresetEl = document.getElementById("eng-scan-preset");
  const scanCustomWrap = document.getElementById("eng-scan-custom-wrap");
  const scanCustomValEl = document.getElementById("eng-scan-custom-val");
  const scanCustomUnitEl = document.getElementById("eng-scan-custom-unit");
  const backfillModeEl = document.getElementById("eng-backfill-mode");
  const backfillCountWrap = document.getElementById("eng-backfill-count-wrap");
  const backfillCountEl = document.getElementById("eng-backfill-count");
  const helperControls = [
    scanPresetEl, scanCustomValEl, scanCustomUnitEl,
    backfillModeEl, backfillCountEl,
  ].filter(Boolean);

  const SCAN_PRESET_SECONDS = [60, 300, 900, 1800, 3600];
  const LEGACY_LIMIT_FIELDS = [
    ["comment_daily_cap_per_account", "每日自动评论上限"],
    ["comment_hourly_cap_per_account", "每小时自动评论上限"],
    ["comment_min_gap_seconds", "自动评论最小间隔"],
    ["action_daily_cap_per_account", "每日互动动作上限"],
    ["action_hourly_cap_per_account", "每小时互动动作上限"],
    ["action_min_gap_seconds", "互动动作最小间隔"],
  ];
  const PERF_TIERS = {
    lite: {
      label: "省资源",
      values: {
        worker_pool_size: 1,
        scan_concurrency: 1,
        active_accounts: 2,
        max_live_contexts: 2,
        resident_browser_sessions: false,
        browser_session_idle_seconds: 600,
      },
    },
    standard: {
      label: "标准均衡",
      values: {
        worker_pool_size: 2,
        scan_concurrency: 2,
        active_accounts: 3,
        max_live_contexts: 6,
        resident_browser_sessions: true,
        browser_session_idle_seconds: 1800,
      },
    },
    high: {
      label: "高性能",
      values: {
        worker_pool_size: 4,
        scan_concurrency: 4,
        active_accounts: 6,
        max_live_contexts: 10,
        resident_browser_sessions: true,
        browser_session_idle_seconds: 3600,
      },
    },
  };
  const RUN_PRESETS = {
    recommended: {
      title: "当前方案：省心推荐",
      values: {
        scan_interval_seconds: 300,
        worker_pool_size: 2,
        scan_concurrency: 2,
        active_accounts: 3,
        max_live_contexts: 6,
        resident_browser_sessions: true,
        browser_session_idle_seconds: 1800,
        block_media_resources: false,
        route_download_via_proxy: true,
        comment_browser_headed: true,
        xhs_comment_review_before_publish: true,
        request_timeout_seconds: 20,
        download_timeout_seconds: 120,
        douyin_keyword_gap_seconds: 8,
        xhs_keyword_gap_seconds: 10,
        xhs_item_gap_seconds: 2.5,
        xhs_request_jitter: 0.35,
        xhs_read_mode: "api",
        rednote_read_mode: "hybrid",
        douyin_read_mode: "hybrid",
        tiktok_read_mode: "native",
        channels_read_mode: "auto",
        bilibili_read_mode: "hybrid",
        instagram_read_mode: "hybrid",
        twitter_read_mode: "hybrid",
        facebook_read_mode: "hybrid",
        youtube_read_mode: "hybrid",
        douyin_profile_mode: "hybrid",
        douyin_followers_mode: "hybrid",
        douyin_dm_sync_mode: "hybrid",
        douyin_creator_danmaku_mode: "hybrid",
        douyin_write_mode: "browser",
        xhs_publish_mode: "browser",
        rednote_publish_mode: "browser",
        xhs_comment_write_mode: "browser",
        bilibili_write_mode: "hybrid",
        instagram_write_mode: "hybrid",
      },
    },
    lite: {
      title: "当前方案：轻量省内存",
      values: {
        scan_interval_seconds: 600,
        worker_pool_size: 1,
        scan_concurrency: 1,
        active_accounts: 2,
        max_live_contexts: 2,
        resident_browser_sessions: false,
        browser_session_idle_seconds: 600,
        block_media_resources: false,
        route_download_via_proxy: true,
        comment_browser_headed: true,
        xhs_comment_review_before_publish: true,
        request_timeout_seconds: 20,
        download_timeout_seconds: 120,
        douyin_keyword_gap_seconds: 8,
        xhs_keyword_gap_seconds: 10,
        xhs_item_gap_seconds: 2.5,
        xhs_request_jitter: 0.35,
        xhs_read_mode: "api",
        rednote_read_mode: "hybrid",
        douyin_read_mode: "hybrid",
        tiktok_read_mode: "native",
        channels_read_mode: "auto",
        bilibili_read_mode: "hybrid",
        instagram_read_mode: "hybrid",
        twitter_read_mode: "hybrid",
        facebook_read_mode: "hybrid",
        youtube_read_mode: "hybrid",
        douyin_profile_mode: "hybrid",
        douyin_followers_mode: "hybrid",
        douyin_dm_sync_mode: "hybrid",
        douyin_creator_danmaku_mode: "hybrid",
        douyin_write_mode: "browser",
        xhs_publish_mode: "browser",
        rednote_publish_mode: "browser",
        xhs_comment_write_mode: "browser",
        bilibili_write_mode: "hybrid",
        instagram_write_mode: "hybrid",
      },
    },
    compat: {
      title: "当前方案：稳妥兼容",
      values: {
        scan_interval_seconds: 600,
        worker_pool_size: 2,
        scan_concurrency: 1,
        active_accounts: 2,
        max_live_contexts: 4,
        resident_browser_sessions: true,
        browser_session_idle_seconds: 1800,
        block_media_resources: false,
        route_download_via_proxy: true,
        comment_browser_headed: true,
        xhs_comment_review_before_publish: true,
        request_timeout_seconds: 35,
        download_timeout_seconds: 240,
        douyin_keyword_gap_seconds: 15,
        xhs_keyword_gap_seconds: 18,
        xhs_item_gap_seconds: 4,
        xhs_request_jitter: 0.5,
        xhs_read_mode: "api",
        rednote_read_mode: "hybrid",
        douyin_read_mode: "hybrid",
        tiktok_read_mode: "auto",
        channels_read_mode: "auto",
        bilibili_read_mode: "hybrid",
        instagram_read_mode: "hybrid",
        twitter_read_mode: "hybrid",
        facebook_read_mode: "hybrid",
        youtube_read_mode: "hybrid",
        douyin_profile_mode: "hybrid",
        douyin_followers_mode: "hybrid",
        douyin_dm_sync_mode: "hybrid",
        douyin_creator_danmaku_mode: "hybrid",
        douyin_write_mode: "browser",
        xhs_publish_mode: "browser",
        rednote_publish_mode: "browser",
        xhs_comment_write_mode: "browser",
        bilibili_write_mode: "hybrid",
        instagram_write_mode: "hybrid",
      },
    },
  };
  const DEFAULT_PRIMARY_PLATFORMS = ["douyin", "xhs", "bilibili", "shipinhao"];
  const PLATFORM_ORDER = [
    "douyin", "xhs", "bilibili", "shipinhao",
    "rednote", "tiktok", "instagram", "twitter", "facebook", "youtube",
  ];

  let baseline = null;
  let recommended = null;
  let restartRequiredSet = new Set(["worker_pool_size", "scan_concurrency", "active_accounts"]);
  let busy = false;
  let dirty = false;
  let sequence = 0;
  let knownAccounts = [];
  // Track which scaled inputs were explicitly touched by the user so non-aligned
  // config.yaml values (e.g. 7201s) are never falsely marked dirty on round-trip.
  const touchedScaled = new Set();

  function cleanNumber(num) {
    return Math.round(num * 10000) / 10000;
  }

  function sameValue(a, b) {
    if (typeof a === "number" && typeof b === "number") {
      return Math.abs(a - b) < 1e-9;
    }
    return Object.is(a, b);
  }

  function value(el) {
    if (el.type === "checkbox") return el.checked;
    if (el.type !== "number") return el.value;
    if (!el.value.trim()) return NaN;
    const scaleAttr = el.dataset.scale;
    if (!scaleAttr) return cleanNumber(Number(el.value));
    if (baseline && !touchedScaled.has(el.name)) {
      const displayOfBaseline = formatScaledDisplay(baseline[el.name], Number(scaleAttr));
      if (String(displayOfBaseline) === el.value.trim()) {
        return baseline[el.name];
      }
    }
    const scale = Number(scaleAttr);
    const scaled = Number(el.value) * scale;
    return scale >= 1 ? Math.round(scaled) : cleanNumber(scaled);
  }

  function formatScaledDisplay(raw, scale) {
    if (raw == null || !Number.isFinite(Number(raw))) return "";
    const num = Number(raw) / scale;
    return scale >= 1 ? Math.round(num * 10) / 10 : Math.round(num * 100) / 100;
  }

  const values = () => Object.fromEntries(fields.map(el => [el.name, value(el)]));
  const changedFields = () => baseline ? fields.filter(el => !sameValue(value(el), baseline[el.name])) : [];

  function message(text, state = "") {
    status.textContent = text;
    status.dataset.state = state;
  }

  function syncFieldA11y(el) {
    if (el.tagName !== "SELECT") return;
    const target = el.parentElement?.querySelector(".cs-trg");
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

  function formatDurationShort(sec) {
    const n = Number(sec);
    if (!Number.isFinite(n) || n <= 0) return "未设置";
    if (n % 3600 === 0) return `每 ${n / 3600} 小时`;
    if (n % 60 === 0) return `每 ${n / 60} 分钟`;
    return `每 ${n} 秒`;
  }

  // ── Compound controls sync ──
  function syncScanUiFromHidden() {
    const hiddenEl = byName.scan_interval_seconds;
    if (!hiddenEl || !scanPresetEl) return;
    const sec = Number(hiddenEl.value);
    if (SCAN_PRESET_SECONDS.includes(sec)) {
      scanPresetEl.value = String(sec);
      if (scanCustomWrap) scanCustomWrap.hidden = true;
    } else {
      scanPresetEl.value = "custom";
      if (scanCustomWrap) scanCustomWrap.hidden = false;
      if (Number.isFinite(sec) && sec > 0) {
        if (sec >= 60 && sec % 60 === 0) {
          if (scanCustomUnitEl) scanCustomUnitEl.value = "min";
          if (scanCustomValEl) scanCustomValEl.value = String(sec / 60);
        } else {
          if (scanCustomUnitEl) scanCustomUnitEl.value = "sec";
          if (scanCustomValEl) scanCustomValEl.value = String(sec);
        }
      } else {
        if (scanCustomUnitEl) scanCustomUnitEl.value = "min";
        if (scanCustomValEl) scanCustomValEl.value = "";
      }
    }
  }

  function syncScanHiddenFromUi(fromPresetChange = false) {
    const hiddenEl = byName.scan_interval_seconds;
    if (!hiddenEl || !scanPresetEl) return;
    if (scanPresetEl.value !== "custom") {
      if (scanCustomWrap) scanCustomWrap.hidden = true;
      hiddenEl.value = scanPresetEl.value;
    } else {
      if (scanCustomWrap) scanCustomWrap.hidden = false;
      if (fromPresetChange && scanCustomValEl && !scanCustomValEl.value.trim()) {
        const cur = Number(hiddenEl.value) || 300;
        if (scanCustomUnitEl) scanCustomUnitEl.value = "min";
        scanCustomValEl.value = String(Math.max(1, Math.round(cur / 60)));
      }
      const rawText = (scanCustomValEl?.value || "").trim();
      const raw = Number(rawText);
      const mult = scanCustomUnitEl?.value === "min" ? 60 : 1;
      hiddenEl.value = rawText && Number.isFinite(raw) ? String(Math.round(raw * mult)) : "";
    }
    if (typeof csSyncAll === "function") csSyncAll();
    validate(hiddenEl);
    changed();
  }

  function syncBackfillUiFromHidden() {
    const hiddenEl = byName.monitor_initial_backfill_count;
    if (!hiddenEl || !backfillModeEl) return;
    const count = Number(hiddenEl.value);
    if (count === 0) {
      backfillModeEl.value = "0";
      if (backfillCountWrap) backfillCountWrap.hidden = true;
    } else if (count === -1) {
      backfillModeEl.value = "-1";
      if (backfillCountWrap) backfillCountWrap.hidden = true;
    } else {
      backfillModeEl.value = "recent";
      if (backfillCountWrap) backfillCountWrap.hidden = false;
      if (backfillCountEl) backfillCountEl.value = Number.isFinite(count) && count > 0 ? String(count) : "20";
    }
  }

  function syncBackfillHiddenFromUi(fromModeChange = false) {
    const hiddenEl = byName.monitor_initial_backfill_count;
    if (!hiddenEl || !backfillModeEl) return;
    if (backfillModeEl.value === "0" || backfillModeEl.value === "-1") {
      if (backfillCountWrap) backfillCountWrap.hidden = true;
      hiddenEl.value = backfillModeEl.value;
    } else {
      if (backfillCountWrap) backfillCountWrap.hidden = false;
      if (fromModeChange && backfillCountEl && !backfillCountEl.value.trim()) {
        backfillCountEl.value = "20";
      }
      const rawText = (backfillCountEl?.value || "").trim();
      const n = Number(rawText);
      hiddenEl.value = rawText && Number.isFinite(n) ? String(n) : "";
    }
    if (typeof csSyncAll === "function") csSyncAll();
    validate(hiddenEl);
    changed();
  }

  // ── Visual summaries, preset states & legacy banner ──
  function matchesMap(draft, map) {
    return Object.entries(map).every(([k, expected]) => !(k in draft) || sameValue(draft[k], expected));
  }

  function detectPreset(draft) {
    for (const [key, spec] of Object.entries(RUN_PRESETS)) {
      if (matchesMap(draft, spec.values)) return key;
    }
    return "custom";
  }

  function detectPerfTier(draft) {
    for (const [key, spec] of Object.entries(PERF_TIERS)) {
      if (matchesMap(draft, spec.values)) return key;
    }
    return "custom";
  }

  function renderLegacyBanner() {
    const banner = document.getElementById("eng-legacy-limits-banner");
    const detail = document.getElementById("eng-legacy-limits-detail");
    if (!banner || !baseline) return;
    const active = LEGACY_LIMIT_FIELDS
      .filter(([key]) => Number(baseline[key] || 0) > 0)
      .map(([key, label]) => `${label} ${baseline[key]}`);
    banner.hidden = active.length === 0;
    if (active.length && detail) {
      detail.textContent = `当前非零旧限额：${active.join("、")}。建议一键清零后统一在「风控中心 → 风控规则」管理。`;
    }
  }

  function updateVisualSummaries() {
    if (!baseline || !recommended) return;
    const draft = values();

    // 1. Preset & Perf tier active buttons
    const activePreset = detectPreset(draft);
    form.querySelectorAll("[data-eng-preset]").forEach(btn => {
      btn.classList.toggle("active", btn.dataset.engPreset === activePreset);
    });
    const activeTier = detectPerfTier(draft);
    form.querySelectorAll("[data-perf-tier]").forEach(btn => {
      btn.classList.toggle("active", btn.dataset.perfTier === activeTier);
    });

    // 2. Count customized fields vs recommended defaults
    const customized = fields.filter(el => !sameValue(draft[el.name], recommended[el.name]));
    const hero = document.getElementById("eng-hero");
    const heroTitle = document.getElementById("eng-hero-title");
    const heroPill = document.getElementById("eng-hero-pill");
    const heroChips = document.getElementById("eng-hero-chips");
    if (hero) hero.dataset.state = customized.length ? "custom" : "default";
    if (heroTitle) {
      heroTitle.textContent = activePreset === "custom"
        ? "当前方案：自定义调整"
        : RUN_PRESETS[activePreset].title;
    }
    if (heroPill) {
      heroPill.textContent = customized.length ? `已自定义 ${customized.length} 项` : "全部为推荐值";
    }
    if (heroChips) {
      const chips = [];
      chips.push({ text: `巡检：${formatDurationShort(draft.scan_interval_seconds)}` });
      const bf = Number(draft.monitor_initial_backfill_count);
      chips.push({
        text: bf === 0 ? "抖音新监控：仅看新增" : bf === -1 ? "抖音新监控：拉全部历史" : `抖音新监控：补拉 ${bf} 条`,
      });
      const tierLabel = PERF_TIERS[activeTier]?.label || "自定义";
      chips.push({
        text: `负载：${tierLabel} (${draft.worker_pool_size}下载/${draft.scan_concurrency}采集/${draft.max_live_contexts}浏览器)`,
      });
      chips.push({
        text: draft.xhs_comment_review_before_publish ? "小红书评论：先审后发" : "小红书评论：免审直发",
        tone: draft.xhs_comment_review_before_publish ? "is-on" : "is-warn",
      });
      if (draft.xhs_dm_monitor_enabled) {
        chips.push({ text: draft.xhs_dm_auto_reply_enabled ? "小红书私信：监控+自动回复" : "小红书私信：监控已开启", tone: "is-on" });
      }
      if (draft.work_health_enabled) {
        chips.push({ text: "作品 0 播健康检查：已开启", tone: "is-on" });
      }
      heroChips.replaceChildren(...chips.map(c => {
        const span = document.createElement("span");
        span.className = "eng-chip" + (c.tone ? " " + c.tone : "");
        span.textContent = c.text;
        return span;
      }));
    }

    // 3. Per-card customization badges & dimming
    form.querySelectorAll(".eng-module[data-eng-card]").forEach(card => {
      const key = card.dataset.engCard;
      const cardFields = [...card.querySelectorAll("input[name],select[name]")];
      const diff = cardFields.filter(el => !sameValue(draft[el.name], recommended[el.name])).length;
      card.classList.toggle("is-custom", diff > 0);
      const badge = document.getElementById(`eng-badge-${key}`);
      if (badge) {
        if (key === "perf") {
          badge.textContent = PERF_TIERS[activeTier]?.label || `已调整 ${diff} 项`;
        } else {
          badge.textContent = diff > 0 ? `已调整 ${diff} 项` : "推荐值";
        }
      }
    });

    // Live contextual summaries inside cards
    const perfSummary = document.getElementById("eng-perf-summary");
    if (perfSummary) {
      perfSummary.textContent = `同时下载 ${draft.worker_pool_size} 个 · 同时采集 ${draft.scan_concurrency} 个 · 活跃账号上限 ${draft.active_accounts} · 常驻浏览器上限 ${draft.max_live_contexts} 个`;
    }
    const dmMeta = document.getElementById("eng-dm-meta");
    if (dmMeta) {
      dmMeta.textContent = draft.xhs_dm_monitor_enabled
        ? (draft.xhs_dm_auto_reply_enabled ? `已开启 · 自动回复` : `已开启 · ${formatDurationShort(draft.xhs_dm_poll_interval_seconds)}`)
        : "未启用";
    }
    const dmBody = document.getElementById("eng-xhs-dm-body");
    if (dmBody) dmBody.classList.toggle("eng-dimmed", !draft.xhs_dm_monitor_enabled);

    const healthMeta = document.getElementById("eng-health-meta");
    if (healthMeta) {
      const mins = Math.round(Number(draft.work_health_interval_seconds || 3600) / 60);
      healthMeta.textContent = draft.work_health_enabled ? `每 ${mins} 分钟检查` : "0 播检查未开启";
    }

    // Platform drawer & expert drawer badges
    const pfDrawer = document.getElementById("engine-settings-platforms");
    const pfSummary = document.getElementById("eng-pf-summary");
    if (pfDrawer && pfSummary) {
      const pfFields = [...pfDrawer.querySelectorAll("input[name],select[name]")];
      const pfDiff = pfFields.filter(el => !sameValue(draft[el.name], recommended[el.name])).length;
      pfSummary.textContent = pfDiff > 0 ? `已自定义 ${pfDiff} 项` : "默认已选用推荐通道 · 通常无需修改";
    }
    const expertDrawer = document.getElementById("engine-settings-expert");
    const expertSummary = document.getElementById("eng-expert-summary");
    if (expertDrawer && expertSummary) {
      const exFields = [...expertDrawer.querySelectorAll("input[name],select[name]")];
      const exDiff = exFields.filter(el => !sameValue(draft[el.name], recommended[el.name])).length;
      expertSummary.textContent = exDiff > 0 ? `已自定义 ${exDiff} 项` : "专家参数 · 默认即可";
    }

    // Per-platform custom highlight
    form.querySelectorAll(".eng-pf-card[data-platform]").forEach(card => {
      const cardFields = [...card.querySelectorAll("input[name],select[name]")];
      const hasCustom = cardFields.some(el => !sameValue(draft[el.name], recommended[el.name]));
      card.classList.toggle("is-custom", hasCustom);
    });

    renderLegacyBanner();
  }

  // ── Smart platform prioritization based on user's connected accounts ──
  function syncPlatforms(accounts) {
    if (Array.isArray(accounts)) knownAccounts = accounts;
    else if (Array.isArray(window.TRANSPORT_MATRIX?.accounts)) knownAccounts = window.TRANSPORT_MATRIX.accounts;
    else if (Array.isArray(window.ALL_ACCOUNTS)) knownAccounts = window.ALL_ACCOUNTS;

    const activeGrid = document.getElementById("eng-pf-active");
    const inactiveGrid = document.getElementById("eng-pf-inactive");
    const othersDetails = document.getElementById("eng-pf-others");
    const othersSummary = document.getElementById("eng-pf-others-summary");
    if (!activeGrid || !inactiveGrid || !othersDetails) return;

    const counts = new Map();
    for (const acc of knownAccounts) {
      const pf = String(acc?.platform || "").trim().toLowerCase();
      if (!pf) continue;
      counts.set(pf, (counts.get(pf) || 0) + 1);
    }
    const hasAnyAccount = counts.size > 0;
    const draft = baseline ? values() : null;
    const cards = [...form.querySelectorAll(".eng-pf-card[data-platform]")];

    const primary = [];
    const secondary = [];
    for (const card of cards) {
      const pf = card.dataset.platform;
      const count = counts.get(pf) || 0;
      const hasCustom = Boolean(
        draft && recommended &&
        [...card.querySelectorAll("input[name],select[name]")]
          .some(el => !sameValue(draft[el.name], recommended[el.name]))
      );
      const tag = card.querySelector(`[data-pf-badge="${pf}"]`);
      if (tag) {
        if (count > 0) {
          tag.textContent = `已绑 ${count} 号${hasCustom ? " · 已调" : ""}`;
          tag.classList.add("is-bound");
        } else if (hasCustom) {
          tag.textContent = "已自定义";
          tag.classList.remove("is-bound");
        } else {
          tag.textContent = "";
          tag.classList.remove("is-bound");
        }
      }
      const isPrimary = hasAnyAccount
        ? (count > 0 || hasCustom)
        : (DEFAULT_PRIMARY_PLATFORMS.includes(pf) || hasCustom);
      (isPrimary ? primary : secondary).push({ card, pf, count });
    }

    const sortCards = (a, b) => {
      if (b.count !== a.count) return b.count - a.count;
      return PLATFORM_ORDER.indexOf(a.pf) - PLATFORM_ORDER.indexOf(b.pf);
    };
    primary.sort(sortCards);
    secondary.sort(sortCards);

    // Fallback: never leave activeGrid empty
    if (!primary.length && secondary.length) {
      primary.push(...secondary.splice(0, 4));
    }

    primary.forEach(item => activeGrid.appendChild(item.card));
    secondary.forEach(item => inactiveGrid.appendChild(item.card));
    othersDetails.hidden = secondary.length === 0;
    if (othersSummary && secondary.length) {
      othersSummary.textContent = hasAnyAccount
        ? `展开其余 ${secondary.length} 个暂未添加账号的平台`
        : `展开其余 ${secondary.length} 个海外与扩展平台`;
    }
  }

  function controls() {
    const lock = busy || !baseline;
    fields.forEach(el => { el.disabled = lock; });
    helperControls.forEach(el => { el.disabled = lock; });
    saveButton.disabled = defaultsButton.disabled = lock;
    if (exportButton) exportButton.disabled = lock;
    if (importButton) importButton.disabled = lock;
    form.querySelectorAll("[data-eng-preset],[data-perf-tier]").forEach(btn => { btn.disabled = lock; });
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

  function render(data, { resetTouched = true } = {}) {
    if (resetTouched) touchedScaled.clear();
    fields.forEach(el => {
      if (el.type === "checkbox") {
        el.checked = Boolean(data[el.name]);
      } else if (el.type === "number" && el.dataset.scale) {
        el.value = formatScaledDisplay(data[el.name], Number(el.dataset.scale));
      } else {
        el.value = data[el.name];
      }
      el.dataset.wbEdited = "false";
      fieldError(el, "");
    });
    syncScanUiFromHidden();
    syncBackfillUiFromHidden();
    summary.hidden = true;
    summary.replaceChildren();
    controls();
    updateVisualSummaries();
    syncPlatforms();
  }

  function changed() {
    if (busy || !baseline) return;
    const edited = changedFields();
    dirty = edited.length > 0;
    if (stickyBar) stickyBar.classList.toggle("is-dirty", dirty);
    if (dirty) {
      const needsRestart = edited.some(el => restartRequiredSet.has(el.name));
      message(
        `有 ${edited.length} 项未保存修改${needsRestart ? "（含需重启生效的并发项）" : ""}`,
        "warning"
      );
    } else {
      message("当前配置已同步，重启软件依然保留");
    }
    updateVisualSummaries();
  }

  function validate(el) {
    let error = "";
    if (el.type === "number") {
      const rawText = el.value.trim();
      const number = Number(rawText);
      const min = el.min !== "" ? Number(el.min) : -Infinity;
      const max = el.max !== "" ? Number(el.max) : Infinity;
      if (!rawText || !Number.isFinite(number)) {
        error = "请输入有效数字";
      } else if (el.name === "monitor_initial_backfill_count" && backfillModeEl?.value === "recent" && (number < 1 || number > 1000)) {
        error = "补拉条数需在 1–1000 之间";
      } else if (number < min || number > max) {
        error = el.name === "scan_interval_seconds"
          ? "巡检间隔需在 1 秒至 24 小时（86400 秒）之间"
          : `请填写 ${el.min}–${el.max} 之间的数值`;
      } else if (el.step === "1" && !Number.isInteger(number)) {
        error = "请填写整数";
      }
    } else if (el.tagName === "SELECT" && ![...el.options].some(option => option.value === el.value)) {
      error = "请选择有效选项";
    }
    return fieldError(el, error);
  }

  function focusField(el) {
    for (let details = el.closest("details"); details; details = details.parentElement?.closest("details")) {
      details.open = true;
    }
    if (el.name === "scan_interval_seconds") {
      if (scanCustomWrap && !scanCustomWrap.hidden && scanCustomValEl) { scanCustomValEl.focus(); return; }
      const trg = scanPresetEl?.parentElement?.querySelector(".cs-trg") || scanPresetEl;
      trg?.focus();
      return;
    }
    if (el.name === "monitor_initial_backfill_count") {
      if (backfillCountWrap && !backfillCountWrap.hidden && backfillCountEl) { backfillCountEl.focus(); return; }
      const trg = backfillModeEl?.parentElement?.querySelector(".cs-trg") || backfillModeEl;
      trg?.focus();
      return;
    }
    const target = el.tagName === "SELECT" ? el.parentElement?.querySelector(".cs-trg") || el : el;
    target.focus();
  }

  function fieldLabel(el) {
    if (el.labels?.[0]) return el.labels[0].textContent.trim();
    const fieldWrap = el.closest(".form-field");
    const lbl = fieldWrap?.querySelector("label");
    return lbl ? lbl.textContent.trim() : el.name;
  }

  function showErrors(invalid) {
    summary.replaceChildren();
    const title = document.createElement("b");
    title.textContent = "请先检查以下配置，修改内容已保留：";
    const list = document.createElement("ul");
    invalid.forEach(el => {
      const li = document.createElement("li");
      const link = document.createElement("a");
      link.href = "#" + el.id;
      const errEl = document.getElementById(el.id + "-error");
      link.textContent = fieldLabel(el) + "：" + (errEl ? errEl.textContent : "取值无效");
      link.addEventListener("click", event => { event.preventDefault(); focusField(el); });
      li.append(link);
      list.append(li);
    });
    summary.append(title, list);
    summary.hidden = false;
    summary.focus();
    message("尚未保存，请修正标出的配置项", "error");
  }

  async function load() {
    if (busy) return;
    const current = ++sequence;
    try {
      const data = validResponse(await api("/api/settings/engine"));
      if (current !== sequence || busy) return;
      recommended = { ...data.defaults };
      if (Array.isArray(data.restart_required_fields)) {
        restartRequiredSet = new Set(data.restart_required_fields);
      }
      if (!dirty || !baseline) {
        baseline = { ...data.values };
        render(baseline);
        if (stickyBar) stickyBar.classList.remove("is-dirty");
        message("当前配置已同步，重启软件依然保留");
      } else {
        updateVisualSummaries();
        message("已保留未保存的修改，请确认后保存", "warning");
      }
      retryButton.hidden = true;
    } catch (error) {
      if (current !== sequence || busy) return;
      message("读取失败：" + error.message + (dirty ? "；修改已保留" : ""), "error");
      retryButton.hidden = false;
    }
    controls();
  }

  function applyFieldMap(patchMap, toastLabel = "") {
    if (busy || !baseline) return;
    Object.entries(patchMap).forEach(([key, val]) => {
      const el = byName[key];
      if (!el) return;
      if (el.type === "checkbox") {
        el.checked = Boolean(val);
      } else if (el.type === "number" && el.dataset.scale) {
        touchedScaled.add(el.name);
        el.value = formatScaledDisplay(val, Number(el.dataset.scale));
      } else {
        el.value = val;
      }
      fieldError(el, "");
    });
    syncScanUiFromHidden();
    syncBackfillUiFromHidden();
    if (typeof csSyncAll === "function") csSyncAll();
    changed();
    if (toastLabel && typeof toast === "function") {
      toast(dirty ? `${toastLabel}已填入，点击「保存采集设置」生效` : `当前已是${toastLabel}`, "info", 3200);
    }
  }

  function applyPreset(name) {
    const spec = RUN_PRESETS[name];
    if (!spec) return;
    applyFieldMap(spec.values, spec.title.replace(/^当前方案：/, "「") + "」方案");
  }

  function applyPerfTier(tier) {
    const spec = PERF_TIERS[tier];
    if (!spec) return;
    applyFieldMap(spec.values, `「${spec.label}」负载档位`);
  }

  function defaults() {
    if (busy || !recommended || !baseline) return;
    touchedScaled.clear();
    fields.forEach(el => { if (el.dataset.scale) touchedScaled.add(el.name); });
    render(recommended, { resetTouched: false });
    changed();
    if (!dirty) message("当前已是全部推荐值");
  }

  async function clearLegacyLimits() {
    if (busy || !baseline) return;
    const btn = document.getElementById("eng-clear-legacy-btn");
    const patch = Object.fromEntries(LEGACY_LIMIT_FIELDS.map(([key]) => [key, 0]));
    busy = true;
    if (btn) btn.disabled = true;
    controls();
    message("正在清零旧版写操作限制…");
    try {
      const data = validResponse(await api("/api/settings/engine", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(patch),
      }));
      baseline = { ...data.values };
      recommended = { ...data.defaults };
      renderLegacyBanner();
      changed();
      if (!dirty) message("已清零旧版写操作限制，请统一在「风控规则」管理", "success");
      if (typeof toast === "function") toast("旧版写操作基础限制已全部清零", "ok");
    } catch (error) {
      message("清零失败：" + error.message, "error");
    } finally {
      busy = false;
      if (btn) btn.disabled = false;
      controls();
    }
  }

  function exportBackup() {
    if (!baseline) return;
    const payload = {
      format: "creatorhub.engine_settings.v1",
      exported_at: new Date().toISOString(),
      note: "仅包含采集与运行参数，不含账号登录态、代理密码或 API Key。",
      values: values(),
    };
    const stamp = new Date().toISOString().slice(0, 10).replace(/-/g, "");
    const blob = new Blob([JSON.stringify(payload, null, 2)], { type: "application/json;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `creatorhub-engine-settings-${stamp}.json`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    if (typeof toast === "function") toast("已导出运行配置备份（不含敏感凭据）", "ok");
  }

  function triggerImport() {
    if (!importInput || busy || !baseline) return;
    importInput.value = "";
    importInput.click();
  }

  async function handleImportFile(event) {
    const file = event.target?.files?.[0];
    if (!file || !baseline) return;
    try {
      const text = await file.text();
      const parsed = JSON.parse(text);
      const source = (parsed && typeof parsed === "object" && parsed.values && typeof parsed.values === "object")
        ? parsed.values
        : parsed;
      if (!source || typeof source !== "object" || Array.isArray(source)) {
        throw new Error("文件内容不是有效的配置对象");
      }
      const patch = {};
      let applied = 0;
      for (const el of fields) {
        if (!(el.name in source)) continue;
        const v = source[el.name];
        if (el.type === "checkbox" && typeof v === "boolean") { patch[el.name] = v; applied++; }
        else if (el.type === "number" && typeof v === "number" && Number.isFinite(v)) { patch[el.name] = v; applied++; }
        else if (el.tagName === "SELECT" && typeof v === "string" && [...el.options].some(o => o.value === v)) {
          patch[el.name] = v; applied++;
        }
      }
      if (!applied) {
        throw new Error("未在文件中找到可导入的运行配置项");
      }
      applyFieldMap(patch);
      const invalid = changedFields().filter(el => !validate(el));
      if (invalid.length) {
        showErrors(invalid);
      } else if (typeof toast === "function") {
        toast(`已载入 ${applied} 项配置，请确认无误后点击「保存采集设置」`, "info", 4500);
      }
    } catch (error) {
      message("导入失败：" + error.message, "error");
      if (typeof toast === "function") toast("导入失败：" + error.message, "err");
    }
  }

  async function save() {
    if (busy || !baseline) return;
    const edited = changedFields();
    const invalid = edited.filter(el => !validate(el));
    if (invalid.length) { showErrors(invalid); return; }
    if (!edited.length) { message("当前没有待保存的修改"); return; }
    const draft = values();
    const patch = Object.fromEntries(edited.map(el => [el.name, draft[el.name]]));
    const touchedRestart = edited.some(el => restartRequiredSet.has(el.name));
    busy = true;
    ++sequence;
    controls();
    message("正在保存…");
    summary.hidden = true;
    try {
      const data = validResponse(await api("/api/settings/engine", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(patch),
      }));
      baseline = { ...data.values };
      recommended = { ...data.defaults };
      dirty = false;
      render(baseline);
      if (stickyBar) stickyBar.classList.remove("is-dirty");
      retryButton.hidden = true;
      const needsRestart = Boolean(data.restart_required || touchedRestart);
      const okMsg = needsRestart
        ? "已保存 · 常规项已生效，并发参数重启软件后生效"
        : "已保存 · 后续任务生效，重启后保留";
      message(okMsg, "success");
      if (typeof toast === "function") toast(okMsg, "ok", needsRestart ? 5200 : 3200);
      globalThis.CreatorHubTransportMatrix?.load?.();
    } catch (error) {
      dirty = true;
      if (stickyBar) stickyBar.classList.add("is-dirty");
      const bad = [];
      if (Array.isArray(error.detail)) {
        for (const item of error.detail) {
          const el = byName[item.loc?.at(-1)];
          if (el) { fieldError(el, "配置值不符合要求，请检查取值范围与类型"); bad.push(el); }
        }
      }
      if (bad.length) showErrors(bad);
      else message("保存尚未确认：" + error.message + "；修改已保留", "error");
    } finally {
      busy = false;
      controls();
    }
  }

  // Event bindings
  if (scanPresetEl) scanPresetEl.addEventListener("change", () => syncScanHiddenFromUi(true));
  if (scanCustomValEl) scanCustomValEl.addEventListener("input", () => syncScanHiddenFromUi(false));
  if (scanCustomUnitEl) scanCustomUnitEl.addEventListener("change", () => syncScanHiddenFromUi(false));
  if (backfillModeEl) backfillModeEl.addEventListener("change", () => syncBackfillHiddenFromUi(true));
  if (backfillCountEl) backfillCountEl.addEventListener("input", () => syncBackfillHiddenFromUi(false));
  if (importInput) importInput.addEventListener("change", handleImportFile);

  form.addEventListener("input", event => {
    const el = event.target;
    if (el?.name && el.dataset?.scale) touchedScaled.add(el.name);
    if (!helperControls.includes(el)) changed();
  });
  form.addEventListener("change", event => {
    const el = event.target;
    if (el?.name && el.dataset?.scale) touchedScaled.add(el.name);
    if (!helperControls.includes(el)) changed();
  });
  fields.forEach(el => {
    el.addEventListener("blur", () => { if (baseline && !busy) validate(el); });
  });
  window.addEventListener("beforeunload", event => {
    if (dirty || busy) { event.preventDefault(); event.returnValue = ""; }
  });

  window.CreatorHubEngineSettings = {
    load,
    defaults,
    save,
    validate,
    isDirty: () => dirty,
    applyPreset,
    applyPerfTier,
    clearLegacyLimits,
    exportBackup,
    triggerImport,
    syncPlatforms,
  };
})();
