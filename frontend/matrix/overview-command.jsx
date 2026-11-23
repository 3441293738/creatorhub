import React, { useState, useEffect, useCallback } from "react";
import { Icon } from "../ui/primitives";
import {
  PF_MAP,
  matrixApi,
  fmtCount,
  fmtDelta,
  fmtShortTime,
  safeThumb,
  syncHeaderPlatformBadges,
} from "./constants";

const PUB_STATUS_LABELS = {
  draft: { text: "待确认预约", cls: "warn" },
  pending: { text: "排队待发", cls: "info" },
  publishing: { text: "正在发布", cls: "info" },
  uncertain: { text: "待人工核对", cls: "warn" },
  done: { text: "已发布", cls: "ok" },
  failed: { text: "发布失败", cls: "danger" },
  canceled: { text: "已取消", cls: "muted" },
};

export function MatrixOverviewCommand({ ctx }) {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [hideIdlePlatforms, setHideIdlePlatforms] = useState(() => {
    try { return localStorage.getItem("dym-hide-idle-pf") === "1"; } catch { return false; }
  });
  const bridge = window.CreatorHubBridge;
  const scope = ctx.globalMode ? "all" : ctx.platform;

  const load = useCallback(async () => {
    try {
      const res = await matrixApi(`/api/matrix/overview?platform=${encodeURIComponent(scope)}`);
      setData(res);
      syncHeaderPlatformBadges(res.platform_strip, hideIdlePlatforms, scope);
    } catch (_) {
      // Fall back gracefully if offline
    } finally {
      setLoading(false);
    }
  }, [scope, hideIdlePlatforms]);

  useEffect(() => {
    setLoading(true);
    load();
    const timer = setInterval(() => {
      if (!document.hidden && ctx.tab === "overview") load();
    }, 10000);
    const onRefresh = () => load();
    window.addEventListener("creatorhub:matrix:refresh", onRefresh);
    return () => {
      clearInterval(timer);
      window.removeEventListener("creatorhub:matrix:refresh", onRefresh);
    };
  }, [load, ctx.tab]);

  useEffect(() => {
    if (data?.platform_strip) {
      syncHeaderPlatformBadges(data.platform_strip, hideIdlePlatforms, scope);
    }
  }, [data, hideIdlePlatforms, scope]);

  // Hide superseded static stats/grid when Matrix Overview is mounted
  useEffect(() => {
    const panel = document.querySelector('[data-panel="overview"]');
    if (!panel) return;
    panel.classList.add("mx-overview-enhanced");
    return () => panel.classList.remove("mx-overview-enhanced");
  }, []);

  const toggleIdle = () => {
    const next = !hideIdlePlatforms;
    setHideIdlePlatforms(next);
    try { localStorage.setItem("dym-hide-idle-pf", next ? "1" : "0"); } catch {}
  };

  const jumpAccountFilter = filterKey => {
    window.__chAccountPresetFilter = filterKey;
    window.dispatchEvent(new CustomEvent("creatorhub:account-filter", { detail: filterKey }));
    bridge?.navigate?.("accounts", true);
  };

  const jumpMediaItem = item => {
    window.__chMediaPreset = { uid: item.uid, source_kind: item.source_kind, id: item.id };
    window.dispatchEvent(new CustomEvent("creatorhub:media-select", { detail: window.__chMediaPreset }));
    bridge?.navigate?.("media-hub", true);
  };

  const jumpHubMode = mode => {
    window.__chHubPresetMode = mode;
    window.dispatchEvent(new CustomEvent("creatorhub:hub-mode", { detail: { mode } }));
    bridge?.navigate?.("hub", true);
  };

  const jumpMatrixPublish = () => {
    bridge?.navigate?.("publish", true);
    setTimeout(() => window.CreatorHubWorkbench?.showSection?.("publish", "matrix"), 0);
  };

  const alerts = data?.alerts || {};
  const totals = data?.totals || {};
  const strip = data?.platform_strip || [];
  const visibleStrip = hideIdlePlatforms
    ? strip.filter(p => p.accounts_total > 0 || p.monitors_enabled > 0 || p.today_contents > 0 || p.platform === scope)
    : strip;

  const alertPills = [
    alerts.invalid_accounts > 0 && {
      key: "invalid", tone: "danger", icon: "alert",
      label: `${alerts.invalid_accounts} 个账号登录失效`, sub: "点击连续扫码补登",
      onClick: () => jumpAccountFilter("invalid"),
    },
    alerts.proxy_issues > 0 && {
      key: "proxy", tone: "danger", icon: "network",
      label: `${alerts.proxy_issues} 个代理连接异常 / 指纹漂移`, sub: "检查节点状态或切回直连",
      onClick: () => jumpAccountFilter("proxy_issue"),
    },
    alerts.shared_proxy_accounts > 0 && {
      key: "sharedproxy", tone: "warn", icon: "network",
      label: `${alerts.shared_proxy_accounts} 个同平台账号共用代理 / IP`, sub: "不强拦 · 建议尽量一号一代理防关联",
      onClick: () => jumpAccountFilter("shared_proxy"),
    },
    alerts.risk_accounts > 0 && {
      key: "risk", tone: "warn", icon: "shield",
      label: `${alerts.risk_accounts} 个账号处于风控冷却`, sub: "查看冷却倒计时",
      onClick: () => bridge?.navigate?.("risk-control", true),
    },
    alerts.no_proxy_accounts > 0 && {
      key: "noproxy", tone: "warn", icon: "globe",
      label: `${alerts.no_proxy_accounts} 个账号使用本机直连（无代理）`, sub: "不强拦 · 多号或海外建议配代理",
      onClick: () => jumpAccountFilter("no_proxy"),
    },
    alerts.unread_dm > 0 && {
      key: "dm", tone: "accent", icon: "msg",
      label: `${alerts.unread_dm} 条未读私信待处理`, sub: "打开跨平台收件箱",
      onClick: () => jumpHubMode("inbox"),
    },
    alerts.abnormal_works > 0 && {
      key: "abn", tone: "warn", icon: "eye",
      label: `${alerts.abnormal_works} 篇自有作品 0播 / 状态预警`, sub: "定位异常作品",
      onClick: () => jumpHubMode("works_abnormal"),
    },
    alerts.failed_tasks > 0 && {
      key: "failed", tone: "danger", icon: "x",
      label: `${alerts.failed_tasks} 个任务执行失败`, sub: "查看队列原因",
      onClick: () => bridge?.openQueue?.("failed", ctx.globalMode),
    },
    alerts.pending_reviews > 0 && {
      key: "review", tone: "info", icon: "check",
      label: `${alerts.pending_reviews} 条自动互动草稿待审`, sub: "人工确认后发送",
      onClick: () => bridge?.openQueue?.("pending", ctx.globalMode),
    },
  ].filter(Boolean);

  const series = data?.series_7d || { days: [], contents: [], comments: [], publishes: [] };
  const maxSeries = Math.max(1, ...(series.contents || []), ...(series.comments || []), ...(series.publishes || []));

  return (
    <div className="mx-overview wb-react-island">
      {/* Scope Bar & Global KPI Ribbon */}
      <div className="mx-ov-topbar">
        <div className="mx-ov-scope">
          <button
            type="button"
            className={`mx-chip-btn ${ctx.globalMode ? "active" : ""}`}
            onClick={() => bridge?.selectPlatform?.("all")}
          >
            <Icon name="globe" />
            <span>全平台全局大盘</span>
          </button>
          {!ctx.globalMode && (
            <span className="mx-scope-current">
              当前聚焦：<b>{PF_MAP[ctx.platform]?.name || ctx.platform}</b>
            </span>
          )}
          <button type="button" className="ghost sm mx-fold-toggle" onClick={toggleIdle}>
            <Icon name={hideIdlePlatforms ? "eye" : "eye-off"} />
            <span>{hideIdlePlatforms ? "显示全部 9 平台" : "收纳空置平台"}</span>
          </button>
        </div>
        <div className="mx-ov-quick-btns">
          <button type="button" className="ghost sm" onClick={() => bridge?.navigate?.("accounts", true)}>
            <Icon name="card" /><span>账号矩阵</span>
          </button>
          <button type="button" className="ghost sm" onClick={() => bridge?.navigate?.("media-hub", true)}>
            <Icon name="image" /><span>汇总预览库</span>
          </button>
          <button type="button" className="ghost sm" onClick={() => window.exportMonitorReport?.()}>
            <Icon name="download" /><span>导出监控报表</span>
          </button>
          <button type="button" className="sm" onClick={jumpMatrixPublish}>
            <Icon name="send" /><span>矩阵多号群发</span>
          </button>
        </div>
      </div>

      {/* KPI Metrics Strip */}
      <div className="mx-kpi-strip" aria-label="核心汇总指标">
        <div className="mx-kpi-box" onClick={() => bridge?.navigate?.("accounts", true)} role="button" tabIndex={0}>
          <span className="mx-kpi-label">在线 / 总账号</span>
          <div className="mx-kpi-val">
            <b>{loading && !data ? "—" : `${totals.active_accounts || 0} / ${totals.accounts || 0}`}</b>
          </div>
          <span className="mx-kpi-sub">跨 {strip.filter(x => x.accounts_total > 0).length} 个已接入平台</span>
        </div>
        <div className="mx-kpi-box" onClick={() => jumpHubMode("analytics")} role="button" tabIndex={0}>
          <span className="mx-kpi-label">矩阵总粉丝</span>
          <div className="mx-kpi-val">
            <b>{loading && !data ? "—" : fmtCount(totals.followers || 0)}</b>
            {totals.fans_delta ? (
              <em className={totals.fans_delta > 0 ? "up" : "down"}>{fmtDelta(totals.fans_delta)}</em>
            ) : null}
          </div>
          <span className="mx-kpi-sub">自有已发作品 {fmtCount(totals.own_works_total || 0)} 篇</span>
        </div>
        <div className="mx-kpi-box" onClick={() => bridge?.navigate?.("media-hub", true)} role="button" tabIndex={0}>
          <span className="mx-kpi-label">捕获作品 / 已下载</span>
          <div className="mx-kpi-val">
            <b>{loading && !data ? "—" : fmtCount(totals.contents_total || 0)}</b>
            <small>本地 {fmtCount(totals.downloaded_total || 0)}</small>
          </div>
          <span className="mx-kpi-sub">今日新抓 +{fmtCount(totals.today_contents || 0)} 条 · 运行监控 {totals.monitors || 0}</span>
        </div>
        <div className="mx-kpi-box" onClick={() => bridge?.openQueue?.("active", ctx.globalMode)} role="button" tabIndex={0}>
          <span className="mx-kpi-label">评论库 / 活跃任务</span>
          <div className="mx-kpi-val">
            <b>{loading && !data ? "—" : fmtCount(totals.comments_total || 0)}</b>
            <small>队列 {alerts.active_tasks || 0}</small>
          </div>
          <span className="mx-kpi-sub">今日新增评论 +{fmtCount(totals.today_comments || 0)} 条</span>
        </div>
      </div>

      {/* Actionable Health & Todo Alerts */}
      <div className="mx-alert-bar" role="region" aria-label="待处理事项">
        {alertPills.length > 0 ? (
          alertPills.map(item => (
            <button
              key={item.key}
              type="button"
              className={`mx-alert-pill tone-${item.tone}`}
              onClick={item.onClick}
            >
              <Icon name={item.icon} />
              <span className="mx-alert-text">
                <b>{item.label}</b>
                <small>{item.sub}</small>
              </span>
              <Icon name="next" />
            </button>
          ))
        ) : (
          <div className="mx-alert-ok">
            <Icon name="check" />
            <span>
              <b>当前范围运行平稳</b> · 无登录掉线、代理异常或失败阻塞任务。
            </span>
          </div>
        )}
      </div>

      {/* Nine-Platform Matrix Health Strip */}
      {ctx.globalMode && (
        <section className="mx-pf-section" aria-label="各平台矩阵健康态势">
          <div className="mx-sec-head">
            <h3><Icon name="overview" />各平台矩阵态势</h3>
            <span className="mut">点击任意平台卡片可聚焦该平台工作台，或直达其账号列表</span>
          </div>
          <div className="mx-pf-grid">
            {(visibleStrip.length ? visibleStrip : strip.slice(0, 5)).map(item => {
              const meta = PF_MAP[item.platform] || { name: item.platform, color: "#64748b" };
              const hasIssue = item.accounts_invalid > 0 || item.accounts_proxy_issue > 0;
              const hasWarn = !hasIssue && (item.accounts_risk > 0 || item.failed_tasks > 0);
              return (
                <div
                  key={item.platform}
                  className={`mx-pf-card ${hasIssue ? "is-danger" : hasWarn ? "is-warn" : item.accounts_total > 0 ? "is-ready" : "is-empty"}`}
                  onClick={() => bridge?.selectPlatform?.(item.platform)}
                  role="button"
                  tabIndex={0}
                  onKeyDown={e => { if (e.key === "Enter") bridge?.selectPlatform?.(item.platform); }}
                >
                  <div className="mx-pf-card-top">
                    <span className="mx-pf-badge" style={{ "--pf-c": meta.color }}>{meta.name}</span>
                    <span className={`mx-pf-health ${hasIssue ? "danger" : hasWarn ? "warn" : item.accounts_total > 0 ? "ok" : "idle"}`}>
                      {item.accounts_total > 0 ? `${item.accounts_active}/${item.accounts_total} 在线` : "未接入"}
                    </span>
                  </div>
                  <div className="mx-pf-metrics">
                    <div><span>粉丝</span><b>{fmtCount(item.followers_total)}</b></div>
                    <div><span>监控</span><b>{item.monitors_enabled}</b></div>
                    <div><span>今日新抓</span><b>+{item.today_contents}</b></div>
                  </div>
                  {(item.accounts_invalid > 0 || item.accounts_shared_proxy > 0 || item.accounts_no_proxy > 0 || item.unread_dm > 0 || item.active_tasks > 0) && (
                    <div className="mx-pf-tags">
                      {item.accounts_invalid > 0 && <span className="mx-mini-tag danger">{item.accounts_invalid} 失效</span>}
                      {item.accounts_shared_proxy > 0 && <span className="mx-mini-tag warn" title="同平台多号共用代理提醒（不阻断任务）">{item.accounts_shared_proxy} 共用代理</span>}
                      {item.accounts_no_proxy > 0 && <span className="mx-mini-tag warn" title="本机直连提醒（不阻断任务）">{item.accounts_no_proxy} 直连</span>}
                      {item.unread_dm > 0 && <span className="mx-mini-tag accent">{item.unread_dm} 私信</span>}
                      {item.active_tasks > 0 && <span className="mx-mini-tag info">{item.active_tasks} 运行中</span>}
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        </section>
      )}

      {/* Visual Preview: Hot Captured Feed + Today's Publish Schedule & 7D Trend */}
      <div className="mx-ov-columns">
        <section className="mx-ov-card mx-ov-feed">
          <div className="mx-sec-head">
            <h3><Icon name="film" />近期捕获与高赞内容快览</h3>
            <button type="button" className="ghost sm" onClick={() => bridge?.navigate?.("media-hub", true)}>
              打开汇总预览库<Icon name="next" />
            </button>
          </div>
          {(data?.hot_contents || []).length > 0 ? (
            <div className="mx-mini-gallery">
              {data.hot_contents.map(item => {
                const pf = PF_MAP[item.platform] || { short: item.platform, color: "#64748b" };
                return (
                  <article
                    key={item.uid}
                    className="mx-mini-media"
                    onClick={() => jumpMediaItem(item)}
                    role="button"
                    tabIndex={0}
                    onKeyDown={e => { if (e.key === "Enter") jumpMediaItem(item); }}
                  >
                    <div className="mx-mini-thumb">
                      {item.cover_url ? (
                        <>
                          <img
                            src={safeThumb(item.cover_url)}
                            alt=""
                            loading="lazy"
                            referrerPolicy="no-referrer"
                            onError={e => {
                              e.currentTarget.style.display = "none";
                              if (e.currentTarget.nextElementSibling) e.currentTarget.nextElementSibling.style.display = "";
                            }}
                          />
                          <div className="mx-mini-ph" style={{ display: "none" }}><Icon name={item.media_type === "images" ? "image" : "play"} /></div>
                        </>
                      ) : (
                        <div className="mx-mini-ph"><Icon name={item.media_type === "images" ? "image" : "play"} /></div>
                      )}
                      <span className="mx-mini-pf" style={{ "--pf-c": pf.color }}>{pf.short}</span>
                      <span className="mx-mini-kind">{item.platform === "twitter" && !item.cover_url && !item.local_ready && item.media_type !== "video" ? "纯文本" : (item.media_type === "images" ? "图文" : "视频")}</span>
                      {item.local_ready && <span className="mx-mini-local" title="已下载到本地">本地</span>}
                    </div>
                    <div className="mx-mini-body">
                      <p className="mx-mini-title">{item.desc}</p>
                      <div className="mx-mini-meta">
                        <span className="mx-mini-author" title={item.author_name}>{item.author_name}</span>
                        <span className="mx-mini-likes"><Icon name="heart" />{fmtCount(item.like_count)}</span>
                      </div>
                    </div>
                  </article>
                );
              })}
            </div>
          ) : (
            <div className="mx-empty-box">
              <Icon name="image" />
              <b>暂无捕获到的作品内容</b>
              <span>添加「作品监控」或发起「关键词采集」后，最新作品封面与互动数据会实时汇聚在此预览。</span>
              <div className="mx-empty-actions">
                <button type="button" className="ghost sm" onClick={() => bridge?.navigate?.("monitors", true)}>去添加作品监控</button>
                <button type="button" className="ghost sm" onClick={() => bridge?.navigate?.("collections", true)}>去新建批量采集</button>
              </div>
            </div>
          )}
        </section>

        <div className="mx-ov-side">
          <section className="mx-ov-card">
            <div className="mx-sec-head">
              <h3><Icon name="calendar" />发布排期与动态</h3>
              <button type="button" className="ghost sm" onClick={jumpMatrixPublish}>
                <Icon name="plus" />群发
              </button>
            </div>
            {(data?.schedule_items || []).length > 0 ? (
              <div className="mx-sched-list">
                {data.schedule_items.slice(0, 7).map(task => {
                  const pf = PF_MAP[task.platform] || { short: task.platform, color: "#64748b" };
                  const st = PUB_STATUS_LABELS[task.status] || { text: task.status, cls: "muted" };
                  return (
                    <div key={task.id} className="mx-sched-row" onClick={() => bridge?.navigate?.("publish", true)} role="button" tabIndex={0}>
                      <span className="mx-mini-pf inline" style={{ "--pf-c": pf.color }}>{pf.short}</span>
                      <div className="mx-sched-main">
                        <b>{task.title}</b>
                        <small>{task.account_name} · {task.scheduled_at ? fmtShortTime(task.scheduled_at) : "尽快发布"}</small>
                      </div>
                      <span className={`mx-st-pill ${st.cls}`}>{st.text}</span>
                    </div>
                  );
                })}
              </div>
            ) : (
              <div className="mx-empty-compact">
                <span>今日暂无待发排期，支持一套素材一键错峰分发至多平台账号。</span>
              </div>
            )}
          </section>

          <section className="mx-ov-card">
            <div className="mx-sec-head">
              <h3><Icon name="bolt" />近 7 日跨平台产出趋势</h3>
              <span className="mut" style={{ fontSize: "11.5px" }}>作品 / 评论 / 发布</span>
            </div>
            <div className="mx-mini-bars">
              {(series.days || []).map((day, idx) => {
                const cVal = series.contents?.[idx] || 0;
                const mVal = series.comments?.[idx] || 0;
                const pVal = series.publishes?.[idx] || 0;
                return (
                  <div key={day} className="mx-bar-col" title={`${day.slice(5)}：作品 ${cVal} · 评论 ${mVal} · 发布 ${pVal}`}>
                    <div className="mx-bar-pair">
                      <i className="b-content" style={{ height: `${Math.max(3, Math.round((cVal / maxSeries) * 68))}px` }} />
                      <i className="b-comment" style={{ height: `${Math.max(3, Math.round((mVal / maxSeries) * 68))}px` }} />
                    </div>
                    <span>{day.slice(5)}</span>
                  </div>
                );
              })}
            </div>
          </section>
        </div>
      </div>
    </div>
  );
}
