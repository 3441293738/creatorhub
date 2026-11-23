// Shared helpers, platform metadata, and API client for CreatorHub Matrix React Islands.
export const PLATFORMS = [
  { id: "douyin", name: "抖音", short: "抖音", color: "#fe2c55", hasCreatorLogin: true, hasCookie: true, titleCap: 30 },
  { id: "xhs", name: "小红书", short: "小红书", color: "#ff2442", hasCreatorLogin: true, hasCookie: true, titleCap: 20 },
  { id: "rednote", name: "海外小红书", short: "Rednote", color: "#e11d48", hasCreatorLogin: true, hasCookie: true, titleCap: 40 },
  { id: "kuaishou", name: "快手", short: "快手", color: "#ff7902", hasCreatorLogin: true, hasCookie: true, titleCap: 20 },
  { id: "shipinhao", name: "视频号", short: "视频号", color: "#07c160", hasCreatorLogin: false, hasCookie: false, titleCap: 16 },
  { id: "tiktok", name: "TikTok", short: "TikTok", color: "#00b8b2", hasCreatorLogin: false, hasCookie: false, titleCap: 80 },
  { id: "bilibili", name: "哔哩哔哩", short: "B站", color: "#00aeec", hasCreatorLogin: false, hasCookie: true, titleCap: 80 },
  { id: "instagram", name: "Instagram", short: "IG", color: "#e1306c", hasCreatorLogin: false, hasCookie: true, titleCap: 2200 },
  { id: "twitter", name: "Twitter", short: "X", color: "#1d9bf0", hasCreatorLogin: false, hasCookie: true, titleCap: 280 },
];

export const PF_MAP = Object.fromEntries(PLATFORMS.map(p => [p.id, p]));

export const ROLE_META = {
  publish: { label: "创作主号", icon: "film", hint: "核心发布与私信运营账号，避免用于高频爬虫", tone: "publish" },
  crawler: { label: "采集小号", icon: "target", hint: "专用于监控轮询、关键词与评论抓取的工具号", tone: "crawler" },
  hybrid: { label: "综合通用", icon: "refresh", hint: "可同时用于内容发布与日常采集监控", tone: "hybrid" },
};

export const GROUP_COLOR_META = {
  blue: { label: "海蓝", dot: "#3b82f6" },
  rose: { label: "玫红", dot: "#f43f5e" },
  emerald: { label: "翠绿", dot: "#10b981" },
  amber: { label: "琥珀", dot: "#f59e0b" },
  violet: { label: "紫罗兰", dot: "#8b5cf6" },
  cyan: { label: "青碧", dot: "#06b6d4" },
  slate: { label: "岩灰", dot: "#64748b" },
};

export async function matrixApi(path, opts) {
  if (typeof window.api === "function") return window.api(path, opts);
  const res = await fetch(path, { credentials: "same-origin", ...(opts || {}) });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `请求失败 (${res.status})`);
  }
  return res.json();
}

export function matrixToast(msg, type = "info", ms = 3600) {
  if (typeof window.toast === "function") window.toast(msg, type, ms);
}

export function fmtCount(n) {
  const v = Number(n || 0);
  if (!Number.isFinite(v)) return "0";
  const abs = Math.abs(v);
  if (abs >= 100000000) return (v / 100000000).toFixed(1).replace(/\.0$/, "") + "亿";
  if (abs >= 10000) return (v / 10000).toFixed(1).replace(/\.0$/, "") + "w";
  return v.toLocaleString();
}

export function fmtDelta(n) {
  const v = Number(n || 0);
  if (!v) return "";
  return (v > 0 ? "+" : "−") + fmtCount(Math.abs(v));
}

export function fmtShortTime(tsOrIso) {
  if (!tsOrIso) return "—";
  const d = typeof tsOrIso === "number" ? new Date(tsOrIso * 1000) : new Date(tsOrIso);
  if (Number.isNaN(d.getTime())) return "—";
  const now = new Date();
  const sameDay = d.toDateString() === now.toDateString();
  const pad = x => String(x).padStart(2, "0");
  if (sameDay) return `今天 ${pad(d.getHours())}:${pad(d.getMinutes())}`;
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

export function safeThumb(url) {
  if (typeof window.safeMediaUrl === "function") return window.safeMediaUrl(url);
  return String(url || "").trim();
}

export function platformExternalWorkUrl(platform, itemId) {
  if (!itemId) return "";
  if (typeof window.workLink === "function") return window.workLink(platform, itemId);
  const enc = encodeURIComponent(itemId);
  if (platform === "xhs") return `https://www.xiaohongshu.com/explore/${enc}`;
  if (platform === "bilibili") return `https://www.bilibili.com/video/${enc}`;
  if (platform === "kuaishou") return `https://www.kuaishou.com/short-video/${enc}`;
  if (platform === "twitter") return `https://x.com/i/web/status/${enc}`;
  if (platform === "instagram") return `https://www.instagram.com/p/${enc}/`;
  return `https://www.douyin.com/video/${enc}`;
}

// Enhance top header .pswitch buttons with clean count pills (zero redundant dots or emojis).
export function syncHeaderPlatformBadges(platformStrip, hideEmpty = false, activePf = "all") {
  const pswitch = document.querySelector(".pswitch");
  if (!pswitch || !Array.isArray(platformStrip)) return;
  const byPf = new Map(platformStrip.map(item => [item.platform, item]));
  let totalAcc = 0;
  let totalDanger = 0;
  let totalWarn = 0;
  platformStrip.forEach(item => {
    totalAcc += Number(item.accounts_total || 0);
    totalDanger += Number(item.accounts_invalid || 0) + Number(item.accounts_proxy_issue || 0);
    totalWarn += Number(item.accounts_risk || 0) + Number(item.failed_tasks || 0);
  });

  pswitch.querySelectorAll("button[data-pf]").forEach(btn => {
    const pf = btn.dataset.pf;
    if (!btn.dataset.baseLabel) btn.dataset.baseLabel = btn.textContent.trim();
    const base = btn.dataset.baseLabel;

    if (pf === "all") {
      const toneCls = totalDanger > 0 ? " danger" : totalWarn > 0 ? " warn" : "";
      btn.innerHTML = `<svg class="ps-ic" aria-hidden="true"><use href="#i-globe"/></svg><span class="ps-lbl">${base}</span>${totalAcc > 0 ? `<span class="ps-cnt${toneCls}">${totalAcc}</span>` : ""}`;
      if (totalDanger + totalWarn > 0) {
        btn.setAttribute("data-tip", `全平台共 ${totalAcc} 个账号 · ${totalDanger + totalWarn} 项待关注状态`);
      } else {
        btn.setAttribute("data-tip", "跨 9 大平台全局汇总与矩阵管理");
      }
      return;
    }

    const info = byPf.get(pf);
    if (!info) return;
    const count = Number(info.accounts_total || 0);
    const hasActivity = count > 0 || Number(info.monitors_enabled || 0) > 0 || Number(info.today_contents || 0) > 0;
    const hasDanger = Number(info.accounts_invalid || 0) > 0 || Number(info.accounts_proxy_issue || 0) > 0;
    const hasWarn = !hasDanger && (Number(info.accounts_risk || 0) > 0 || Number(info.failed_tasks || 0) > 0);
    const toneCls = hasDanger ? " danger" : hasWarn ? " warn" : "";
    btn.innerHTML = `<span class="ps-lbl">${base}</span>${count > 0 ? `<span class="ps-cnt${toneCls}">${count}</span>` : ""}`;
    const tips = [];
    if (count > 0) tips.push(`${info.accounts_active}/${count} 在线`);
    if (info.accounts_invalid) tips.push(`${info.accounts_invalid} 登录失效`);
    if (info.accounts_proxy_issue) tips.push(`${info.accounts_proxy_issue} 代理异常`);
    if (info.accounts_risk) tips.push(`${info.accounts_risk} 风控冷却`);
    if (info.unread_dm) tips.push(`${info.unread_dm} 未读私信`);
    if (tips.length) btn.setAttribute("data-tip", `${base}：${tips.join(" · ")}`);

    // Collapse idle platforms when requested, unless currently selected
    const shouldHide = hideEmpty && !hasActivity && activePf !== pf;
    btn.classList.toggle("ps-idle-hidden", shouldHide);
  });

  requestAnimationFrame(() => window.dispatchEvent(new Event("creatorhub:layout")));
}
