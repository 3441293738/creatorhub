import React, { useState, useEffect, useCallback } from "react";
import { Icon, SelectBox } from "../ui/primitives";
import {
  PLATFORMS,
  PF_MAP,
  matrixApi,
  matrixToast,
  fmtCount,
  fmtShortTime,
  safeThumb,
  platformExternalWorkUrl,
} from "./constants";

const SOURCE_TABS = [
  { key: "all", label: "全部内容" },
  { key: "monitor", label: "作品监控" },
  { key: "collection", label: "批量采集" },
  { key: "own", label: "自有已发" },
  { key: "share", label: "链接下载" },
];

export function MediaHubGallery({ ctx }) {
  const bridge = window.CreatorHubBridge;
  const [sourceKind, setSourceKind] = useState("all");
  const [pfFilter, setPfFilter] = useState("auto");
  const [mediaType, setMediaType] = useState("");
  const [localOnly, setLocalOnly] = useState(false);
  const [minLikes, setMinLikes] = useState("");
  const [query, setQuery] = useState("");
  const [sort, setSort] = useState("latest");
  const [layout, setLayout] = useState("grid"); // grid | table
  const [page, setPage] = useState(1);

  const [data, setData] = useState({ items: [], total: 0, page: 1, pages: 1, source_counts: {} });
  const [loading, setLoading] = useState(true);
  const [selected, setSelected] = useState(() => {
    const p = window.__chMediaPreset || null;
    window.__chMediaPreset = null;
    return p;
  });
  const [detail, setDetail] = useState(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [slideIdx, setSlideIdx] = useState(0);

  const effectivePf = pfFilter === "auto" ? (ctx.globalMode ? "all" : ctx.platform) : pfFilter;

  useEffect(() => {
    setPfFilter("auto");
    setPage(1);
  }, [ctx.platform, ctx.globalMode]);

  const loadList = useCallback(async () => {
    setLoading(true);
    try {
      const params = new URLSearchParams({
        platform: effectivePf,
        source_kind: sourceKind,
        sort,
        page: String(page),
        page_size: "24",
      });
      if (mediaType) params.set("media_type", mediaType);
      if (localOnly) params.set("local_only", "true");
      if (Number(minLikes) > 0) params.set("min_likes", String(Math.floor(Number(minLikes))));
      if (query.trim()) params.set("q", query.trim());
      const res = await matrixApi(`/api/matrix/media-hub?${params.toString()}`);
      setData(res);
      if (res.items?.length && !selected) {
        setSelected({ uid: res.items[0].uid, source_kind: res.items[0].source_kind, id: res.items[0].id });
      }
    } catch (e) {
      matrixToast("加载汇总预览库失败：" + e.message, "err");
    } finally {
      setLoading(false);
    }
  }, [effectivePf, sourceKind, mediaType, localOnly, minLikes, query, sort, page]);

  useEffect(() => {
    if (ctx.tab === "media-hub") loadList();
  }, [loadList, ctx.tab]);

  useEffect(() => {
    const onRefresh = () => { if (ctx.tab === "media-hub") loadList(); };
    const onSelect = e => {
      if (e.detail?.source_kind && e.detail?.id) {
        setSourceKind("all");
        setPfFilter("all");
        setSelected(e.detail);
      }
    };
    window.addEventListener("creatorhub:matrix:refresh", onRefresh);
    window.addEventListener("creatorhub:media-select", onSelect);
    return () => {
      window.removeEventListener("creatorhub:matrix:refresh", onRefresh);
      window.removeEventListener("creatorhub:media-select", onSelect);
    };
  }, [loadList, ctx.tab]);

  // Load Quick-Look Detail whenever selected item changes
  useEffect(() => {
    if (!selected?.source_kind || !selected?.id || ctx.tab !== "media-hub") return;
    let alive = true;
    setDetailLoading(true);
    setSlideIdx(0);
    matrixApi(`/api/matrix/media-hub/detail?source_kind=${encodeURIComponent(selected.source_kind)}&id=${encodeURIComponent(selected.id)}`)
      .then(res => { if (alive) setDetail(res); })
      .catch(() => { if (alive) setDetail(null); })
      .finally(() => { if (alive) setDetailLoading(false); });
    return () => { alive = false; };
  }, [selected?.uid, selected?.source_kind, selected?.id, ctx.tab]);

  // Keyboard J / K quick stepping in Media Hub
  useEffect(() => {
    if (ctx.tab !== "media-hub") return;
    const onKey = e => {
      if (e.target.matches("input,textarea,select,[contenteditable='true']")) return;
      const list = data.items || [];
      if (!list.length) return;
      const idx = list.findIndex(x => x.uid === selected?.uid);
      if (e.key === "j" || e.key === "ArrowDown") {
        e.preventDefault();
        const next = list[Math.min(list.length - 1, idx + 1)] || list[0];
        setSelected({ uid: next.uid, source_kind: next.source_kind, id: next.id });
      } else if (e.key === "k" || e.key === "ArrowUp") {
        e.preventDefault();
        const prev = list[Math.max(0, idx - 1)] || list[0];
        setSelected({ uid: prev.uid, source_kind: prev.source_kind, id: prev.id });
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [ctx.tab, data.items, selected?.uid]);

  const copyCaption = () => {
    if (!detail?.desc) return;
    if (typeof window.copyTextValue === "function") {
      window.copyTextValue(detail.desc, "作品文案已复制");
    } else {
      navigator.clipboard?.writeText(detail.desc);
      matrixToast("作品文案已复制", "ok");
    }
  };

  const revealLocal = async () => {
    if (!detail) return;
    try {
      await matrixApi("/api/matrix/media-hub/reveal", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CreatorHub-Local-Action": "reveal" },
        body: JSON.stringify({ source_kind: detail.source_kind, id: detail.id }),
      });
      matrixToast("已在系统文件夹中定位该文件", "ok");
    } catch (e) {
      matrixToast("打开本地目录失败：" + e.message, "err");
    }
  };

  const sendToMatrixPublish = () => {
    if (!detail?.local_file_paths?.length) {
      matrixToast("请先下载该素材到本地后再转入矩阵群发", "info");
      return;
    }
    const preset = {
      media_type: detail.media_type === "images" ? "images" : "video",
      media_paths: detail.local_file_paths,
      title: (detail.desc || "").split(/\r?\n/)[0].slice(0, 30),
      desc: detail.desc || "",
      source_platform: detail.platform || "",
      source_content_id: detail.source_kind === "monitor" ? detail.id : null,
    };
    window.__chPublishMediaPreset = preset;
    window.dispatchEvent(new CustomEvent("creatorhub:publish-preset", { detail: preset }));
    bridge?.navigate?.("publish", true);
    setTimeout(() => window.CreatorHubWorkbench?.showSection?.("publish", "matrix"), 0);
    matrixToast("已将本地素材与文案载入「矩阵多号群发」编辑器", "ok");
  };

  // Assemble playable / viewable slides for Quick-Look right pane
  const mediaSlides = (() => {
    if (!detail) return [];
    if (detail.local_medias?.length) return detail.local_medias;
    if (detail.remote_medias?.length) return detail.remote_medias;
    if (detail.cover_url) return [{ kind: "image", url: detail.cover_url }];
    return [];
  })();

  const activeSlide = mediaSlides[Math.min(slideIdx, Math.max(0, mediaSlides.length - 1))];
  const counts = data.source_counts || {};
  const extUrl = detail ? (detail.source_url || platformExternalWorkUrl(detail.platform, detail.aweme_id)) : "";

  return (
    <div className="mx-media-hub">
      {/* Source Tabs */}
      <div className="mx-source-tabs" role="tablist" aria-label="内容来源切换">
        {SOURCE_TABS.map(tab => (
          <button
            key={tab.key}
            type="button"
            role="tab"
            aria-selected={sourceKind === tab.key}
            className={`mx-stab ${sourceKind === tab.key ? "active" : ""}`}
            onClick={() => { setSourceKind(tab.key); setPage(1); }}
          >
            <span>{tab.label}</span>
            <b>{fmtCount(counts[tab.key] || 0)}</b>
          </button>
        ))}
        <span className="mx-kbd-hint">快捷键 <kbd>J</kbd> / <kbd>K</kbd> 连续切换右侧预览</span>
      </div>

      {/* Filter & View Bar */}
      <div className="mx-acc-toolbar">
        <label className="mx-search-box">
          <Icon name="search" />
          <input
            type="search"
            value={query}
            onChange={e => { setQuery(e.target.value); setPage(1); }}
            placeholder="搜文案关键词 / 作者 / 监控备注 / 作品 ID…"
            aria-label="搜索汇总素材"
          />
        </label>

        <SelectBox
          value={effectivePf}
          onChange={val => { setPfFilter(val); setPage(1); }}
          ariaLabel="按平台筛选"
          options={[
            { value: "all", label: "全部平台", icon: "globe" },
            ...PLATFORMS.map(p => ({ value: p.id, label: p.name, dot: p.color })),
          ]}
        />

        <SelectBox
          value={mediaType}
          onChange={val => { setMediaType(val); setPage(1); }}
          ariaLabel="按媒体类型筛选"
          options={[
            { value: "", label: "全部载体" },
            { value: "video", label: "视频", icon: "film" },
            { value: "images", label: "图文 / 图集", icon: "image" },
          ]}
        />

        <input
          type="number"
          min="0"
          className="mx-num-input"
          value={minLikes}
          onChange={e => { setMinLikes(e.target.value); setPage(1); }}
          placeholder="最低赞数"
          aria-label="最低点赞数"
        />

        <label className="mx-inline-check">
          <input
            type="checkbox"
            checked={localOnly}
            onChange={e => { setLocalOnly(e.target.checked); setPage(1); }}
          />
          <span>仅已下载本地</span>
        </label>

        <SelectBox
          value={sort}
          onChange={val => { setSort(val); setPage(1); }}
          ariaLabel="排序方式"
          options={[
            { value: "latest", label: "最新抓取入库" },
            { value: "publish_desc", label: "最新发布时间" },
            { value: "likes_desc", label: "点赞从高到低" },
            { value: "comments_desc", label: "评论从高到低" },
          ]}
        />

        <div className="mx-view-seg" role="group" aria-label="布局切换">
          <button type="button" className={layout === "grid" ? "active" : ""} onClick={() => setLayout("grid")}>
            <Icon name="grid" /><span>画廊</span>
          </button>
          <button type="button" className={layout === "table" ? "active" : ""} onClick={() => setLayout("table")}>
            <Icon name="list" /><span>表格</span>
          </button>
        </div>
      </div>

      {/* Split Master-Detail Body */}
      <div className="mx-split-layout">
        <div className="mx-split-master">
          {loading && !data.items.length ? (
            <div className="mx-empty-box"><span>正在汇聚多平台作品库…</span></div>
          ) : data.items.length === 0 ? (
            <div className="mx-empty-box">
              <Icon name="image" />
              <b>没有匹配的内容记录</b>
              <span>调整筛选条件，或前往「作品监控 / 关键词采集 / 链接下载」捕获新内容。</span>
            </div>
          ) : layout === "grid" ? (
            <div className="mx-gallery-grid">
              {data.items.map(item => {
                const pf = PF_MAP[item.platform] || { short: item.platform, color: "#64748b" };
                const active = selected?.uid === item.uid;
                return (
                  <article
                    key={item.uid}
                    className={`mx-gcard ${active ? "is-active" : ""}`}
                    onClick={() => setSelected({ uid: item.uid, source_kind: item.source_kind, id: item.id })}
                    role="button"
                    tabIndex={0}
                    onKeyDown={e => { if (e.key === "Enter") setSelected({ uid: item.uid, source_kind: item.source_kind, id: item.id }); }}
                  >
                    <div className="mx-gcard-cover">
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
                          <div className="mx-mini-ph" style={{ display: "none" }}><Icon name={item.media_type === "images" ? "image" : "film"} /></div>
                        </>
                      ) : item.platform === "twitter" && !item.local_ready && item.media_type !== "video" ? (
                        <div className="mx-mini-ph" style={{ padding: "14px", textAlign: "left", alignItems: "flex-start" }}>
                          <span style={{ fontSize: "12.5px", lineHeight: 1.55, color: "var(--fg-2)", display: "-webkit-box", WebkitLineClamp: 5, WebkitBoxOrient: "vertical", overflow: "hidden" }}>
                            {item.desc}
                          </span>
                        </div>
                      ) : (
                        <div className="mx-mini-ph"><Icon name={item.media_type === "images" ? "image" : "film"} /></div>
                      )}
                      <span className="mx-mini-pf" style={{ "--pf-c": pf.color }}>{pf.short}</span>
                      <span className="mx-mini-kind">{item.platform === "twitter" && !item.cover_url && !item.local_ready && item.media_type !== "video" ? "纯文本" : (item.media_type === "images" ? "图集" : "视频")}</span>
                      {item.local_ready && <span className="mx-mini-local">已存本地</span>}
                    </div>
                    <div className="mx-gcard-body">
                      <p className="mx-gcard-title">{item.desc}</p>
                      <div className="mx-gcard-sub">
                        <span className="mx-src-pill">{item.source_label}</span>
                        <span className="mut" title={item.author_name}>{item.author_name}</span>
                      </div>
                      <div className="mx-gcard-foot">
                        <span>{fmtShortTime(item.create_time || item.captured_at)}</span>
                        <span className="mx-metrics-inline">
                          <b><Icon name="heart" />{fmtCount(item.like_count)}</b>
                          <span><Icon name="msg" />{fmtCount(item.comment_count)}</span>
                        </span>
                      </div>
                    </div>
                  </article>
                );
              })}
            </div>
          ) : (
            <div className="table-wrap">
              <table className="mx-dense-table">
                <thead>
                  <tr>
                    <th>平台 / 来源</th>
                    <th>作品内容</th>
                    <th>作者 / 任务</th>
                    <th>互动指标</th>
                    <th>状态</th>
                  </tr>
                </thead>
                <tbody>
                  {data.items.map(item => {
                    const pf = PF_MAP[item.platform] || { short: item.platform, color: "#64748b" };
                    const active = selected?.uid === item.uid;
                    return (
                      <tr
                        key={item.uid}
                        className={active ? "is-selected" : ""}
                        style={{ cursor: "pointer" }}
                        onClick={() => setSelected({ uid: item.uid, source_kind: item.source_kind, id: item.id })}
                      >
                        <td>
                          <span className="mx-mini-pf inline" style={{ "--pf-c": pf.color }}>{pf.short}</span>
                          <div className="mut" style={{ fontSize: "11px", marginTop: "2px" }}>{item.source_label}</div>
                        </td>
                        <td style={{ maxWidth: "280px" }}>
                          <div className="mx-clamp-2">{item.desc}</div>
                        </td>
                        <td>{item.author_name}</td>
                        <td>
                          <span className="mx-metrics-inline">
                            <b><Icon name="heart" />{fmtCount(item.like_count)}</b>
                            <span><Icon name="msg" />{fmtCount(item.comment_count)}</span>
                          </span>
                        </td>
                        <td>
                          <span className={`mx-st-pill ${item.local_ready ? "ok" : "muted"}`}>
                            {item.local_ready ? "已存本地" : (item.download_status === "own" ? "自有作品" : "仅元数据")}
                          </span>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}

          {/* Pagination */}
          {data.pages > 1 && (
            <div className="mx-pager">
              <button type="button" className="ghost sm" disabled={page <= 1} onClick={() => setPage(p => Math.max(1, p - 1))}>
                <Icon name="prev" />上一页
              </button>
              <span className="mut">第 {data.page} / {data.pages} 页 · 共 {fmtCount(data.total)} 条</span>
              <button type="button" className="ghost sm" disabled={page >= data.pages} onClick={() => setPage(p => Math.min(data.pages, p + 1))}>
                下一页<Icon name="next" />
              </button>
            </div>
          )}
        </div>

        {/* Right Sticky Split-Pane Quick-Look Inspector */}
        <aside className="mx-split-detail" aria-label="右侧分栏快览">
          {!selected ? (
            <div className="mx-detail-empty">
              <Icon name="eye" />
              <b>点击左侧任意卡片预览</b>
              <span>支持本地视频直放、多图轮播、文案复制、高赞评论同屏查看与一键二创转发。</span>
            </div>
          ) : detailLoading && !detail ? (
            <div className="mx-detail-empty"><span>正在载入媒体与评论详情…</span></div>
          ) : detail ? (
            <div className="mx-detail-inner">
              <div className="mx-detail-player">
                {activeSlide ? (
                  activeSlide.kind === "video" ? (
                    <video
                      key={activeSlide.url}
                      src={activeSlide.url}
                      poster={safeThumb(detail.cover_url)}
                      controls
                      playsInline
                      preload="metadata"
                    />
                  ) : (
                    <img
                      key={activeSlide.url}
                      src={activeSlide.url.startsWith("/api/") ? activeSlide.url : safeThumb(activeSlide.url)}
                      alt=""
                      referrerPolicy="no-referrer"
                    />
                  )
                ) : (
                  <div className="mx-detail-empty"><Icon name="image" /><span>暂无可播放媒体流</span></div>
                )}

                {mediaSlides.length > 1 && (
                  <div className="mx-slide-bar">
                    <button type="button" className="ghost sm" disabled={slideIdx <= 0} onClick={() => setSlideIdx(i => Math.max(0, i - 1))}>
                      <Icon name="prev" />
                    </button>
                    <span>{slideIdx + 1} / {mediaSlides.length}</span>
                    <button type="button" className="ghost sm" disabled={slideIdx >= mediaSlides.length - 1} onClick={() => setSlideIdx(i => Math.min(mediaSlides.length - 1, i + 1))}>
                      <Icon name="next" />
                    </button>
                  </div>
                )}
              </div>

              <div className="mx-detail-meta">
                <div className="mx-detail-badges">
                  <span className="mx-mini-pf inline" style={{ "--pf-c": PF_MAP[detail.platform]?.color || "#64748b" }}>
                    {PF_MAP[detail.platform]?.name || detail.platform}
                  </span>
                  <span className="mx-metrics-inline">
                    <b><Icon name="heart" />{fmtCount(detail.like_count)}</b>
                    <span><Icon name="msg" />{fmtCount(detail.comment_count)}</span>
                  </span>
                  {detail.local_file_paths?.length > 0 && <span className="mx-st-pill ok">本地文件就绪 ({detail.local_file_paths.length})</span>}
                </div>

                <div className="mx-detail-caption">{detail.desc || "（无正文文案）"}</div>

                <div className="mx-detail-actions">
                  <button type="button" className="ghost sm" onClick={copyCaption}>
                    <Icon name="copy" />复制文案
                  </button>
                  {detail.local_file_paths?.length > 0 && (
                    <button type="button" className="ghost sm" onClick={revealLocal}>
                      <Icon name="folder" />定位文件
                    </button>
                  )}
                  {extUrl && (
                    <a className="mx-btn-link ghost sm" href={extUrl} target="_blank" rel="noopener noreferrer">
                      <Icon name="external" />原网页
                    </a>
                  )}
                  {detail.can_repost && (
                    <button type="button" className="sm" onClick={sendToMatrixPublish}>
                      <Icon name="send" />转入矩阵群发
                    </button>
                  )}
                </div>
              </div>

              <div className="mx-detail-comments">
                <div className="mx-sec-head">
                  <h4><Icon name="msg" />同屏高赞评论快览 ({detail.comments?.length || 0})</h4>
                </div>
                {detail.comments?.length > 0 ? (
                  <div className="mx-cmt-list">
                    {detail.comments.map(c => (
                      <div key={c.id} className="mx-cmt-row">
                        <div className="mx-cmt-top">
                          <b>{c.user_nickname}</b>
                          {c.ip_label && <span className="mut">· {c.ip_label}</span>}
                          <span className="mx-cmt-like"><Icon name="heart" />{fmtCount(c.like_count)}</span>
                        </div>
                        <p>{c.is_reply ? "↳ " : ""}{c.text}</p>
                      </div>
                    ))}
                  </div>
                ) : (
                  <p className="mut" style={{ fontSize: "12px", margin: "6px 0" }}>
                    本地暂未抓取该作品的评论明细（开启评论监控或批量采集评论后会自动关联合并展示）。
                  </p>
                )}
              </div>
            </div>
          ) : null}
        </aside>
      </div>
    </div>
  );
}
