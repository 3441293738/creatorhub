import React, { useState, useEffect, useCallback } from "react";
import { Icon, SelectBox } from "../ui/primitives";
import {
  PLATFORMS,
  PF_MAP,
  ROLE_META,
  GROUP_COLOR_META,
  matrixApi,
  matrixToast,
  fmtCount,
  fmtDelta,
  fmtShortTime,
  safeThumb,
  platformExternalWorkUrl,
} from "./constants";

export function HubMatrixDashboard({ ctx }) {
  const bridge = window.CreatorHubBridge;
  const [mode, setMode] = useState(() => {
    const preset = window.__chHubPresetMode || (ctx.globalMode ? "analytics" : "analytics");
    window.__chHubPresetMode = "";
    return preset === "works_abnormal" ? "works" : preset;
  });
  const [workFilter, setWorkFilter] = useState(() => (window.__chHubPresetMode === "works_abnormal" ? "abnormal" : "all"));
  const [pfFilter, setPfFilter] = useState("auto");
  const [groupFilter, setGroupFilter] = useState("");
  const [workSort, setWorkSort] = useState("latest");
  const [query, setQuery] = useState("");
  const [accSort, setAccSort] = useState("followers");
  const [unreadOnly, setUnreadOnly] = useState(false);

  const [analytics, setAnalytics] = useState({ kpi: {}, accounts: [], works: { items: [], total: 0, page: 1, pages: 1 } });
  const [inbox, setInbox] = useState({ items: [], unread: 0 });
  const [groups, setGroups] = useState([]);
  const [syncingAll, setSyncingAll] = useState(false);

  const effectivePf = pfFilter === "auto" ? (ctx.globalMode ? "all" : ctx.platform) : pfFilter;

  useEffect(() => {
    setPfFilter("auto");
    if (ctx.globalMode && mode === "single") setMode("analytics");
  }, [ctx.platform, ctx.globalMode]);

  // Toggle visibility of legacy #hub-single-account-card based on mode
  useEffect(() => {
    const singleCard = document.getElementById("hub-single-account-card");
    if (singleCard) {
      singleCard.style.display = (mode === "single" && !ctx.globalMode) ? "" : "none";
    }
  }, [mode, ctx.globalMode]);

  useEffect(() => {
    const onHubMode = e => {
      const m = e.detail?.mode;
      if (m === "works_abnormal") {
        setMode("works");
        setWorkFilter("abnormal");
      } else if (m) {
        setMode(m);
      }
    };
    window.addEventListener("creatorhub:hub-mode", onHubMode);
    return () => window.removeEventListener("creatorhub:hub-mode", onHubMode);
  }, []);

  const loadData = useCallback(async () => {
    if (ctx.tab !== "hub") return;
    try {
      const params = new URLSearchParams({
        platform: effectivePf,
        work_filter: workFilter,
        sort: workSort,
      });
      if (groupFilter) params.set("group_name", groupFilter);
      if (query.trim()) params.set("q", query.trim());

      const dmParams = new URLSearchParams({ limit: "200" });
      if (effectivePf && effectivePf !== "all") dmParams.set("platform", effectivePf);
      if (unreadOnly) dmParams.set("unread_only", "true");

      const [anaRes, dmRes, accRes] = await Promise.all([
        matrixApi(`/api/matrix/hub-analytics?${params.toString()}`),
        matrixApi(`/api/dm/inbox?${dmParams.toString()}`).catch(() => ({ items: [], unread: 0 })),
        matrixApi("/api/matrix/accounts").catch(() => ({ groups: [] })),
      ]);
      setAnalytics(anaRes);
      setInbox(dmRes);
      if (Array.isArray(accRes.groups)) setGroups(accRes.groups);
    } catch (_) {}
  }, [ctx.tab, effectivePf, groupFilter, workFilter, workSort, query, unreadOnly]);

  useEffect(() => {
    loadData();
    const onRefresh = () => loadData();
    window.addEventListener("creatorhub:matrix:refresh", onRefresh);
    return () => window.removeEventListener("creatorhub:matrix:refresh", onRefresh);
  }, [loadData]);

  const syncAllVisibleWorks = async () => {
    const valid = (analytics.accounts || []).filter(a => a.status !== "invalid");
    if (!valid.length || syncingAll) return;
    setSyncingAll(true);
    let ok = 0;
    for (const acc of valid.slice(0, 15)) {
      try {
        const r = await matrixApi(`/api/accounts/${acc.id}/works/sync`, { method: "POST" });
        if (!r.skipped) ok++;
      } catch (_) {}
    }
    setSyncingAll(false);
    matrixToast(`已完成 ${ok} 个账号作品同步刷新`, "ok");
    loadData();
  };

  const sortedAccounts = [...(analytics.accounts || [])].sort((a, b) => {
    if (accSort === "delta") return b.fans_delta - a.fans_delta;
    if (accSort === "plays") return b.plays_total - a.plays_total;
    if (accSort === "likes") return b.likes_total - a.likes_total;
    if (accSort === "works") return b.aweme_count - a.aweme_count;
    if (accSort === "abnormal") return b.abnormal_count - a.abnormal_count;
    return (Number(b.pinned) - Number(a.pinned)) || (b.follower_count - a.follower_count);
  });

  const kpi = analytics.kpi || {};

  return (
    <div className="mx-hub-wrap">
      {/* Top Mode Switcher */}
      <div className="mx-source-tabs" role="tablist" aria-label="矩阵运营视图切换">
        <button type="button" role="tab" aria-selected={mode === "analytics"} className={`mx-stab ${mode === "analytics" ? "active" : ""}`} onClick={() => setMode("analytics")}>
          <Icon name="overview" /><span>全矩阵账号对比</span><b>{kpi.accounts_count || 0}</b>
        </button>
        <button type="button" role="tab" aria-selected={mode === "works"} className={`mx-stab ${mode === "works" ? "active" : ""}`} onClick={() => setMode("works")}>
          <Icon name="film" /><span>全矩阵已发作品墙</span>
          {kpi.abnormal_works_total > 0 && <b className="warn">{kpi.abnormal_works_total} 异常</b>}
        </button>
        <button type="button" role="tab" aria-selected={mode === "inbox"} className={`mx-stab ${mode === "inbox" ? "active" : ""}`} onClick={() => setMode("inbox")}>
          <Icon name="msg" /><span>全平台聚合私信</span>
          {(inbox.unread || kpi.unread_dm_total) > 0 && <b className="accent">{inbox.unread || kpi.unread_dm_total}</b>}
        </button>
        {!ctx.globalMode && (
          <button type="button" role="tab" aria-selected={mode === "single"} className={`mx-stab ${mode === "single" ? "active" : ""}`} onClick={() => setMode("single")}>
            <Icon name="user" /><span>单账号精细管理</span>
          </button>
        )}
      </div>

      {mode !== "single" && (
        <>
          {/* Shared KPI Strip */}
          <div className="mx-kpi-strip" style={{ marginTop: "12px" }}>
            <div className="mx-kpi-box">
              <span className="mx-kpi-label">汇总粉丝规模</span>
              <div className="mx-kpi-val">
                <b>{fmtCount(kpi.followers_total || 0)}</b>
                {kpi.fans_delta_total ? <em className={kpi.fans_delta_total > 0 ? "up" : "down"}>{fmtDelta(kpi.fans_delta_total)}</em> : null}
              </div>
              <span className="mx-kpi-sub">覆盖 {kpi.accounts_count || 0} 个自有账号</span>
            </div>
            <div className="mx-kpi-box">
              <span className="mx-kpi-label">已同步总播放</span>
              <div className="mx-kpi-val"><b>{fmtCount(kpi.plays_total || 0)}</b></div>
              <span className="mx-kpi-sub">累计作品 {fmtCount(kpi.works_total || 0)} 篇</span>
            </div>
            <div className="mx-kpi-box">
              <span className="mx-kpi-label">累计获赞 / 评论</span>
              <div className="mx-kpi-val"><b>{fmtCount(kpi.likes_total || 0)}</b><small>评 {fmtCount(kpi.comments_total || 0)}</small></div>
              <span className="mx-kpi-sub">基于已同步作品汇总</span>
            </div>
            <div className="mx-kpi-box" onClick={() => { setMode("works"); setWorkFilter("abnormal"); }} role="button" tabIndex={0}>
              <span className="mx-kpi-label">0播 / 异常预警作品</span>
              <div className="mx-kpi-val">
                <b style={{ color: kpi.abnormal_works_total > 0 ? "var(--danger)" : undefined }}>
                  {kpi.abnormal_works_total || 0}
                </b>
              </div>
              <span className="mx-kpi-sub">未读私信 {kpi.unread_dm_total || 0} 条</span>
            </div>
          </div>

          {/* Filter Bar */}
          <div className="mx-acc-toolbar" style={{ marginTop: "12px" }}>
            <label className="mx-search-box">
              <Icon name="search" />
              <input
                type="search"
                value={query}
                onChange={e => setQuery(e.target.value)}
                placeholder="搜账号备注 / 昵称 / 分组…"
                aria-label="搜索矩阵账号与作品"
              />
            </label>
            <SelectBox
              value={effectivePf}
              onChange={setPfFilter}
              ariaLabel="按平台筛选"
              options={[
                { value: "all", label: "全部平台", icon: "globe" },
                ...PLATFORMS.map(p => ({ value: p.id, label: p.name, dot: p.color })),
              ]}
            />
            <SelectBox
              value={groupFilter}
              onChange={setGroupFilter}
              ariaLabel="按矩阵组筛选"
              options={[
                { value: "", label: "全部矩阵组", icon: "layers" },
                ...groups.map(g => ({
                  value: g.name,
                  label: g.name,
                  dot: GROUP_COLOR_META[g.color]?.dot || "#3b82f6",
                })),
                { value: "__ungrouped__", label: "未分组" },
              ]}
            />

            {mode === "analytics" && (
              <>
                <SelectBox
                  value={accSort}
                  onChange={setAccSort}
                  ariaLabel="账号排行排序"
                  options={[
                    { value: "followers", label: "按粉丝量排行" },
                    { value: "delta", label: "按涨粉净增排行" },
                    { value: "plays", label: "按总播放排行" },
                    { value: "likes", label: "按总点赞排行" },
                    { value: "works", label: "按作品数排行" },
                    { value: "abnormal", label: "异常预警优先" },
                  ]}
                />
                <button type="button" className="ghost sm" disabled={syncingAll} onClick={syncAllVisibleWorks}>
                  <Icon name="refresh" />{syncingAll ? "正在同步作品…" : "一键同步各号作品"}
                </button>
              </>
            )}

            {mode === "works" && (
              <>
                <SelectBox
                  value={workFilter}
                  onChange={setWorkFilter}
                  ariaLabel="作品健康状态筛选"
                  options={[
                    { value: "all", label: "全部已发作品" },
                    { value: "abnormal", label: "仅看 0播 / 状态异常", icon: "alert" },
                    { value: "zero_play", label: "仅看持续 0 播预警" },
                  ]}
                />
                <SelectBox
                  value={workSort}
                  onChange={setWorkSort}
                  ariaLabel="作品排序"
                  options={[
                    { value: "latest", label: "最新发布优先" },
                    { value: "plays_desc", label: "播放最高优先" },
                    { value: "likes_desc", label: "点赞最高优先" },
                    { value: "comments_desc", label: "评论最多优先" },
                  ]}
                />
              </>
            )}

            {mode === "inbox" && (
              <label className="mx-inline-check">
                <input type="checkbox" checked={unreadOnly} onChange={e => setUnreadOnly(e.target.checked)} />
                <span>仅看未读会话</span>
              </label>
            )}
          </div>
        </>
      )}

      {/* Cross-Account Comparison Table */}
      {mode === "analytics" && (
        <div className="mx-ov-card" style={{ marginTop: "12px" }}>
          <div className="table-wrap">
            <table className="mx-dense-table" aria-label="跨平台账号数据对比排行">
              <thead>
                <tr>
                  <th>账号 / 矩阵分组</th>
                  <th>平台 / 角色</th>
                  <th>粉丝数 (较前次)</th>
                  <th>累计作品 (近7日)</th>
                  <th>已同步总播放</th>
                  <th>已同步总获赞</th>
                  <th>作品健康度</th>
                  <th style={{ textAlign: "right" }}>操作</th>
                </tr>
              </thead>
              <tbody>
                {sortedAccounts.length === 0 ? (
                  <tr><td colSpan={8}><div className="mx-empty-compact">暂无匹配的已登录账号</div></td></tr>
                ) : (
                  sortedAccounts.map(acc => {
                    const pf = PF_MAP[acc.platform] || { name: acc.platform, short: acc.platform, color: "#64748b" };
                    const role = ROLE_META[acc.account_role] || ROLE_META.hybrid;
                    return (
                      <tr key={acc.id}>
                        <td>
                          <div className="mx-td-acc">
                            {acc.avatar ? (
                              <>
                                <img
                                  className="avatar sm"
                                  src={safeThumb(acc.avatar)}
                                  alt=""
                                  referrerPolicy="no-referrer"
                                  onError={e => {
                                    e.currentTarget.style.display = "none";
                                    if (e.currentTarget.nextElementSibling) e.currentTarget.nextElementSibling.style.display = "";
                                  }}
                                />
                                <span className="wb-avatar-fallback sm" style={{ display: "none" }}>{(acc.display_name || "号").slice(0, 1)}</span>
                              </>
                            ) : (
                              <span className="wb-avatar-fallback sm">{(acc.display_name || "号").slice(0, 1)}</span>
                            )}
                            <div>
                              <b>{acc.display_name}</b>
                              <div className="mut" style={{ fontSize: "11.5px" }}>{acc.group_name ? acc.group_name : "未分组"}</div>
                            </div>
                          </div>
                        </td>
                        <td>
                          <span className="mx-mini-pf inline" style={{ "--pf-c": pf.color }}>{pf.short}</span>
                          <span className={`mx-role-tag role-${acc.account_role}`} style={{ marginLeft: "6px" }}>{role.label}</span>
                        </td>
                        <td>
                          <b>{fmtCount(acc.follower_count)}</b>
                          {acc.fans_delta ? <em className={`mx-delta ${acc.fans_delta > 0 ? "up" : "down"}`}>{fmtDelta(acc.fans_delta)}</em> : null}
                        </td>
                        <td>
                          <b>{fmtCount(acc.aweme_count)}</b>
                          <span className="mut"> (7日 +{acc.recent_7d_works})</span>
                        </td>
                        <td><b>{fmtCount(acc.plays_total)}</b></td>
                        <td><b>{fmtCount(acc.likes_total)}</b> <span className="mut">· 评 {fmtCount(acc.comments_total)}</span></td>
                        <td>
                          {acc.abnormal_count > 0 ? (
                            <span className="mx-st-pill danger"><Icon name="alert" />{acc.abnormal_count} 篇异常</span>
                          ) : (
                            <span className="mx-st-pill ok">正常</span>
                          )}
                          {acc.unread_dm > 0 && <span className="mx-dm-chip" style={{ marginLeft: "6px" }}><Icon name="msg" />{acc.unread_dm}</span>}
                        </td>
                        <td className="acttd">
                          <button type="button" className="ghost sm" onClick={() => bridge?.openAccount?.(acc.id)}>
                            单号明细 / 作品<Icon name="next" />
                          </button>
                        </td>
                      </tr>
                    );
                  })
                )}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {/* Cross-Account Published Works Wall */}
      {mode === "works" && (
        <div className="mx-ov-card" style={{ marginTop: "12px" }}>
          {(analytics.works?.items || []).length === 0 ? (
            <div className="mx-empty-box">
              <Icon name="film" />
              <b>暂无匹配的自有已发作品</b>
              <span>点击「全矩阵账号对比 → 一键同步各号作品」，即可在此跨平台复盘爆款与 0 播作品。</span>
            </div>
          ) : (
            <div className="mx-gallery-grid">
              {analytics.works.items.map(w => {
                const pf = PF_MAP[w.platform] || { short: w.platform, color: "#64748b" };
                const ext = platformExternalWorkUrl(w.platform, w.item_id);
                const isTextPost = w.platform === "twitter" && !w.cover_url && w.media_type !== "video";
                const kindLabel = isTextPost ? "纯文本" : (w.media_type === "images" ? "图文" : "视频");
                return (
                  <article key={w.id} className="mx-gcard">
                    <div className="mx-gcard-cover" onClick={() => ext && window.open(ext, "_blank", "noopener")}>
                      {w.cover_url ? (
                        <>
                          <img
                            src={safeThumb(w.cover_url)}
                            alt=""
                            loading="lazy"
                            referrerPolicy="no-referrer"
                            onError={e => {
                              e.currentTarget.style.display = "none";
                              if (e.currentTarget.nextElementSibling) e.currentTarget.nextElementSibling.style.display = "";
                            }}
                          />
                          <div className="mx-mini-ph" style={{ display: "none" }}><Icon name={w.media_type === "images" ? "image" : "film"} /></div>
                        </>
                      ) : isTextPost ? (
                        <div className="mx-mini-ph" style={{ padding: "14px", textAlign: "left", alignItems: "flex-start" }}>
                          <span style={{ fontSize: "12.5px", lineHeight: 1.55, color: "var(--fg-2)", display: "-webkit-box", WebkitLineClamp: 5, WebkitBoxOrient: "vertical", overflow: "hidden" }}>
                            {w.desc}
                          </span>
                        </div>
                      ) : (
                        <div className="mx-mini-ph"><Icon name={w.media_type === "images" ? "image" : "film"} /></div>
                      )}
                      <span className="mx-mini-pf" style={{ "--pf-c": pf.color }}>{pf.short}</span>
                      <span className="mx-mini-kind">{kindLabel}</span>
                      {(w.zero_play_alerted || w.status_alerted) && (
                        <span className="mx-mini-local warn">{w.zero_play_alerted ? "0播预警" : w.status_alerted}</span>
                      )}
                    </div>
                    <div className="mx-gcard-body">
                      <p className="mx-gcard-title">{w.desc}</p>
                      <div className="mx-gcard-sub">
                        <b>{w.account_name}</b>
                        <span className="mut">{fmtShortTime(w.create_time)}</span>
                      </div>
                      <div className="mx-gcard-foot">
                        <span className="mx-icon-num"><Icon name="play" />{fmtCount(w.play_count)}</span>
                        <span className="mx-metrics-inline">
                          <b><Icon name="heart" />{fmtCount(w.like_count)}</b>
                          <span><Icon name="msg" />{fmtCount(w.comment_count)}</span>
                        </span>
                      </div>
                    </div>
                  </article>
                );
              })}
            </div>
          )}
        </div>
      )}

      {/* Cross-Platform Aggregated DM Inbox */}
      {mode === "inbox" && (
        <div className="mx-ov-card" style={{ marginTop: "12px" }}>
          <div className="table-wrap">
            <table className="mx-dense-table" aria-label="全平台聚合私信收件箱">
              <thead>
                <tr>
                  <th>接收账号</th>
                  <th>对话方</th>
                  <th>最近一条消息</th>
                  <th>时间</th>
                  <th style={{ textAlign: "right" }}>快速处理</th>
                </tr>
              </thead>
              <tbody>
                {(inbox.items || []).length === 0 ? (
                  <tr>
                    <td colSpan={5}>
                      <div className="mx-empty-compact">当前筛选范围内暂无私信会话（在单账号私信页同步后会自动汇聚到此）。</div>
                    </td>
                  </tr>
                ) : (
                  inbox.items.map(row => {
                    const pf = PF_MAP[row.platform] || { short: row.platform, color: "#64748b" };
                    return (
                      <tr key={row.id}>
                        <td>
                          <span className="mx-mini-pf inline" style={{ "--pf-c": pf.color }}>{pf.short}</span>
                          <b style={{ marginLeft: "6px" }}>{row.account_name || `账号 #${row.account_id}`}</b>
                        </td>
                        <td>
                          <b>{row.peer_nickname || row.peer_uid || "未知用户"}</b>
                          {row.unread_count > 0 && <span className="mx-dm-chip" style={{ marginLeft: "6px" }}>{row.unread_count} 未读</span>}
                        </td>
                        <td style={{ maxWidth: "360px" }}>
                          <div className="mx-clamp-2">{row.last_text || "（非文本消息）"}</div>
                        </td>
                        <td className="mut">{row.last_time ? fmtShortTime(row.last_time) : "—"}</td>
                        <td className="acttd">
                          <div className="mx-row-btns">
                            <button type="button" className="sm" onClick={() => bridge?.openAggregatedDm?.(row.account_id, row.conv_id)}>
                              <Icon name="send" />回复
                            </button>
                            <button type="button" className="ghost sm" onClick={() => bridge?.accountActions?.openBrowser?.(row.account_id)}>
                              浏览器
                            </button>
                          </div>
                        </td>
                      </tr>
                    );
                  })
                )}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  );
}
