import React, { useState, useEffect, useCallback, useMemo, useRef } from "react";
import { Icon, SelectBox } from "../ui/primitives";
import {
  PLATFORMS,
  PF_MAP,
  ROLE_META,
  matrixApi,
  matrixToast,
} from "./constants";

export function MatrixBatchPublisher({ ctx }) {
  const [accounts, setAccounts] = useState([]);
  const [groups, setGroups] = useState([]);
  const [selectedAccIds, setSelectedAccIds] = useState(() => new Set(window.__chPublishPresetAccounts || []));
  const [mediaType, setMediaType] = useState("video");
  const [uploadedFiles, setUploadedFiles] = useState([]); // [{ path, name }]
  const [uploading, setUploading] = useState(false);
  const [uploadProgress, setUploadProgress] = useState(null); // { percent, loaded, total, speedBps, etaSeconds }
  const uploadAbortRef = useRef(null);
  const [submitting, setSubmitting] = useState(false);

  const [title, setTitle] = useState("");
  const [desc, setDesc] = useState("");
  const [topics, setTopics] = useState("");
  const [scheduledAt, setScheduledAt] = useState("");
  const [staggerMinutes, setStaggerMinutes] = useState("10");
  const [sourceMeta, setSourceMeta] = useState({ source_platform: "", source_content_id: null });
  const [overrides, setOverrides] = useState({}); // { [platform]: { title, visibility } }

  const loadAccounts = useCallback(async () => {
    try {
      const res = await matrixApi("/api/matrix/accounts");
      setAccounts(Array.isArray(res.items) ? res.items : []);
      setGroups(Array.isArray(res.groups) ? res.groups : []);
    } catch (_) {}
  }, []);

  useEffect(() => {
    loadAccounts();
    const applyPreset = preset => {
      if (!preset) return;
      if (Array.isArray(preset.accountIds)) {
        setSelectedAccIds(new Set(preset.accountIds));
      }
      if (preset.media_paths?.length) {
        setMediaType(preset.media_type || "video");
        setUploadedFiles(preset.media_paths.map(p => ({ path: p, name: String(p).split(/[\\/]/).pop() || p })));
        if (preset.title !== undefined) setTitle(preset.title);
        if (preset.desc !== undefined) setDesc(preset.desc);
        setSourceMeta({
          source_platform: preset.source_platform || "",
          source_content_id: preset.source_content_id || null,
        });
      }
    };
    if (window.__chPublishPresetAccounts) {
      applyPreset({ accountIds: window.__chPublishPresetAccounts });
      window.__chPublishPresetAccounts = null;
    }
    if (window.__chPublishMediaPreset) {
      applyPreset(window.__chPublishMediaPreset);
      window.__chPublishMediaPreset = null;
    }
    const onPreset = e => applyPreset(e.detail);
    const onRefresh = () => loadAccounts();
    window.addEventListener("creatorhub:publish-preset", onPreset);
    window.addEventListener("creatorhub:matrix:refresh", onRefresh);
    return () => {
      window.removeEventListener("creatorhub:publish-preset", onPreset);
      window.removeEventListener("creatorhub:matrix:refresh", onRefresh);
    };
  }, [loadAccounts]);

  const pfMeta = PF_MAP[ctx.platform] || PLATFORMS[0];
  const pfName = pfMeta.name;
  const videoOnlyPlatform = !ctx.globalMode && (ctx.platform === "tiktok" || ctx.platform === "youtube");

  const scopedAccounts = useMemo(
    () => (ctx.globalMode ? accounts : accounts.filter(a => a.platform === ctx.platform)),
    [accounts, ctx.globalMode, ctx.platform]
  );

  const scopedGroups = useMemo(() => {
    if (ctx.globalMode) return groups;
    const used = new Set(scopedAccounts.map(a => a.group_name).filter(Boolean));
    return groups.filter(g => used.has(g.name));
  }, [groups, scopedAccounts, ctx.globalMode]);

  useEffect(() => {
    if (videoOnlyPlatform && mediaType !== "video") {
      setMediaType("video");
      setUploadedFiles([]);
    }
  }, [videoOnlyPlatform, mediaType]);

  useEffect(() => {
    if (ctx.globalMode || !accounts.length) return;
    const allowedIds = new Set(scopedAccounts.map(a => a.id));
    setSelectedAccIds(prev => {
      if (!prev.size) return prev;
      let changed = false;
      const next = new Set();
      prev.forEach(id => {
        if (allowedIds.has(id)) next.add(id);
        else changed = true;
      });
      return changed ? next : prev;
    });
  }, [ctx.globalMode, ctx.platform, accounts.length, scopedAccounts]);

  const toggleAcc = id => {
    setSelectedAccIds(prev => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });
  };

  const selectByGroup = groupName => {
    const matching = scopedAccounts.filter(a => a.group_name === groupName && a.status !== "invalid").map(a => a.id);
    if (!matching.length) {
      matrixToast("该分组下暂无可用的在线账号", "info");
      return;
    }
    setSelectedAccIds(prev => {
      const next = new Set(prev);
      const allIn = matching.every(id => next.has(id));
      matching.forEach(id => { if (allIn) next.delete(id); else next.add(id); });
      return next;
    });
  };

  const selectAllPublishRoles = () => {
    const ids = scopedAccounts.filter(a => a.status !== "invalid" && a.account_role === "publish").map(a => a.id);
    if (!ids.length) {
      const fallback = scopedAccounts.filter(a => a.status !== "invalid" && a.account_role !== "crawler").map(a => a.id);
      setSelectedAccIds(new Set(fallback));
      return;
    }
    setSelectedAccIds(new Set(ids));
  };

  const handleFileChange = async e => {
    const rawFiles = Array.from(e.target.files || []);
    if (!rawFiles.length) return;
    setUploading(true);
    const totalBytes = rawFiles.reduce((s, f) => s + (f.size || 0), 0);
    setUploadProgress({ loaded: 0, total: totalBytes, percent: 0, speedBps: 0, etaSeconds: null });
    const controller = new AbortController();
    uploadAbortRef.current = controller;
    try {
      const fd = new FormData();
      rawFiles.forEach(f => fd.append("files", f));
      const res = typeof window.apiUploadWithProgress === "function"
        ? await window.apiUploadWithProgress("/api/publish/upload", fd, {
            signal: controller.signal,
            onProgress: prog => setUploadProgress(prog),
          })
        : await matrixApi("/api/publish/upload", { method: "POST", body: fd });
      const saved = Array.isArray(res.files) ? res.files : [];
      setUploadedFiles(prev => (mediaType === "video" ? saved.slice(0, 1) : [...prev, ...saved].slice(0, 18)));
      matrixToast(`已上传准备 ${saved.length} 个素材文件`, "ok");
    } catch (err) {
      matrixToast("素材上传失败：" + err.message, "err");
    } finally {
      uploadAbortRef.current = null;
      setUploading(false);
      setUploadProgress(null);
      e.target.value = "";
    }
  };

  const selectedAccounts = useMemo(
    () => scopedAccounts.filter(a => selectedAccIds.has(a.id)),
    [scopedAccounts, selectedAccIds]
  );

  const selectedPlatforms = useMemo(
    () => [...new Set(selectedAccounts.map(a => a.platform))],
    [selectedAccounts]
  );

  const crawlerSelectedCount = selectedAccounts.filter(a => a.account_role === "crawler").length;

  const submitBatch = async e => {
    e.preventDefault();
    if (!selectedAccounts.length) {
      matrixToast("请至少勾选 1 个目标发布账号", "err");
      return;
    }
    const requiresMedia = selectedAccounts.some(a => a.platform !== "twitter" && a.platform !== "facebook");
    if (requiresMedia && uploadedFiles.length === 0) {
      matrixToast("所选平台需要上传至少 1 个视频或图片素材", "err");
      return;
    }
    if (!requiresMedia && uploadedFiles.length === 0 && !(title.trim() || desc.trim())) {
      matrixToast("纯文本发布请填写标题或正文内容，或上传图片/视频素材", "err");
      return;
    }
    setSubmitting(true);
    try {
      let isoWhen = null;
      if (scheduledAt) {
        const dt = new Date(scheduledAt);
        if (Number.isNaN(dt.getTime())) throw new Error("预约时间格式无效");
        isoWhen = dt.toISOString();
      }
      const res = await matrixApi("/api/matrix/publish-batch", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          account_ids: selectedAccounts.map(a => a.id),
          media_type: mediaType,
          media_paths: uploadedFiles.map(f => f.path),
          title,
          desc,
          topics,
          scheduled_at: isoWhen,
          stagger_minutes: Number(staggerMinutes || 0),
          source_platform: sourceMeta.source_platform,
          source_content_id: sourceMeta.source_content_id,
          platform_overrides: overrides,
        }),
      });
      matrixToast(
        `已成功创建 ${res.created_count} 条跨账号发布任务${res.skipped_count ? `（跳过 ${res.skipped_count} 个不满足条件的账号）` : ""}`,
        "ok",
        6000
      );
      setTitle("");
      setDesc("");
      setTopics("");
      setUploadedFiles([]);
      setSourceMeta({ source_platform: "", source_content_id: null });
      if (typeof window.refreshPublish === "function") window.refreshPublish();
      window.CreatorHubWorkbench?.showSection?.("publish", "records");
    } catch (err) {
      matrixToast("矩阵群发提交失败：" + err.message, "err");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <form className="mx-pub-wrap" onSubmit={submitBatch}>
      <div className="mx-sec-head">
        <div>
          <h3><Icon name="send" />{ctx.globalMode ? "跨平台 / 多账号矩阵错峰群发" : `${pfName} · 同平台多账号错峰群发`}</h3>
          <p className="mut" style={{ margin: "4px 0 0", fontSize: "12.5px" }}>
            {ctx.globalMode
              ? "一次上传素材与主文案，按「跨平台矩阵组」一键勾选多号，自动适配各平台标题上限并按设定间隔错峰入队。"
              : `一次上传素材与主文案，勾选多个 ${pfName} 账号按设定间隔错峰入队。`}
          </p>
        </div>
      </div>

      {/* Account Matrix Picker */}
      <div className="mx-pub-step">
        <div className="mx-pub-step-head">
          <b>
            {ctx.globalMode
              ? `1. 选择分发目标账号（已选 ${selectedAccounts.length} 个账号，跨 ${selectedPlatforms.length} 个平台）`
              : `1. 选择 ${pfName} 发布账号（已选 ${selectedAccounts.length} 个账号）`}
          </b>
          <div className="mx-pub-quick-picks">
            <button type="button" className="ghost sm" onClick={selectAllPublishRoles}>一键勾选创作主号</button>
            {scopedGroups.map(g => (
              <button key={g.name} type="button" className="ghost sm" onClick={() => selectByGroup(g.name)}>
                <Icon name="folder" /><span>{g.name}</span>
              </button>
            ))}
            {selectedAccounts.length > 0 && (
              <button type="button" className="ghost sm" onClick={() => setSelectedAccIds(new Set())}>清空</button>
            )}
          </div>
        </div>

        {crawlerSelectedCount > 0 && (
          <div className="mx-inline-warn">
            <Icon name="alert" />
            <span>注意：当前勾选了 {crawlerSelectedCount} 个标记为「采集小号」的账号，确认是否也要用于公开发布。</span>
          </div>
        )}

        <div className="mx-pub-acc-grid">
          {scopedAccounts.length === 0 ? (
            <div className="mx-empty-compact">
              {ctx.globalMode
                ? "暂无已接入账号，请先前往「账号矩阵」扫码添加账号。"
                : `暂无已接入的 ${pfName} 账号，请先前往「账号矩阵」添加账号。`}
            </div>
          ) : (
            scopedAccounts.map(acc => {
              const pf = PF_MAP[acc.platform] || { short: acc.platform, color: "#64748b" };
              const checked = selectedAccIds.has(acc.id);
              const disabled = acc.status === "invalid"
                || (acc.platform === "xhs" && !acc.has_creator)
                || (mediaType === "images" && (acc.platform === "tiktok" || acc.platform === "youtube"));
              return (
                <label
                  key={acc.id}
                  className={`mx-pub-acc-item ${checked ? "checked" : ""} ${disabled ? "disabled" : ""}`}
                  title={disabled ? "登录失效、缺创作者授权或该平台仅支持视频" : acc.display_name}
                >
                  <input
                    type="checkbox"
                    disabled={disabled}
                    checked={checked && !disabled}
                    onChange={() => toggleAcc(acc.id)}
                  />
                  <span className="mx-mini-pf inline" style={{ "--pf-c": pf.color }}>{pf.short}</span>
                  <span className="mx-pub-acc-name">{acc.display_name}</span>
                  {acc.group_name && <small className="mut">({acc.group_name})</small>}
                  <span className={`mx-role-dot role-${acc.account_role}`} title={ROLE_META[acc.account_role]?.label} />
                </label>
              );
            })
          )}
        </div>
      </div>

      {/* Media & Master Copy */}
      <div className="mx-pub-columns">
        <div className="stack">
          <div className="form-grid">
            <div className="form-field">
              <label>素材载体类型</label>
              <SelectBox
                full
                value={mediaType}
                onChange={val => { setMediaType(val); setUploadedFiles([]); }}
                ariaLabel="素材载体类型"
                options={videoOnlyPlatform ? [
                  { value: "video", label: `视频（${pfName} 仅支持视频）`, icon: "film" },
                ] : [
                  { value: "video", label: ctx.globalMode ? "短视频（单视频全平台通用）" : "视频", icon: "film" },
                  { value: "images", label: ctx.globalMode ? "图文 / 图集（多图轮播，TikTok / YouTube 除外）" : "图文 / 图集（多图轮播）", icon: "image" },
                ]}
              />
            </div>
            <div className="form-field">
              <label>上传本地文件 {uploading ? "（上传中…）" : ""}</label>
              <label className="mx-file-drop">
                <input
                  type="file"
                  accept={mediaType === "video" ? "video/*" : "image/*"}
                  multiple={mediaType === "images"}
                  onChange={handleFileChange}
                />
                <Icon name="download" />
                <span>{uploading ? `正在上传素材${uploadProgress?.percent != null ? ` ${uploadProgress.percent}%` : ""}…` : (mediaType === "video" ? "点击选择本地视频文件" : "点击选择图片（支持多选）")}</span>
              </label>
              {uploading && uploadProgress && (() => {
                const P = window.CreatorHubProgress;
                const pct = uploadProgress.percent != null ? Math.round(uploadProgress.percent) : null;
                const meta = [
                  uploadProgress.total > 0 && P ? `${P.fmtBytes(uploadProgress.loaded)} / ${P.fmtBytes(uploadProgress.total)}` : "",
                  uploadProgress.speedBps > 0 && P ? P.fmtSpeed(uploadProgress.speedBps) : "",
                  uploadProgress.etaSeconds > 0 && P ? P.fmtEta(uploadProgress.etaSeconds) : "",
                ].filter(Boolean).join(" · ");
                return (
                  <div className="pub-upload-progress">
                    <div className="pub-upload-progress-head">
                      <span className="mut progress-num">{meta || "正在写入本地素材…"}</span>
                      <span style={{ display: "inline-flex", alignItems: "center", gap: "8px" }}>
                        <b className="progress-num">{pct != null ? `${pct}%` : "上传中"}</b>
                        <button type="button" className="ghost sm danger" style={{ minHeight: "22px", padding: "1px 7px" }} onClick={() => uploadAbortRef.current?.abort()}>取消</button>
                      </span>
                    </div>
                    <div className={`progress-track is-compact ${pct == null ? "is-indeterminate" : ""}`}>
                      <div className="progress-fill" style={pct != null ? { width: `${pct}%` } : undefined} />
                    </div>
                  </div>
                );
              })()}
            </div>
          </div>

          {uploadedFiles.length > 0 && (
            <div className="mx-uploaded-pills">
              {uploadedFiles.map((f, i) => (
                <span key={`${f.path}-${i}`} className="mx-file-pill">
                  <Icon name={mediaType === "video" ? "film" : "image"} />
                  <span>{f.name}</span>
                  <button type="button" onClick={() => setUploadedFiles(prev => prev.filter((_, idx) => idx !== i))} aria-label="移除"><Icon name="x" /></button>
                </span>
              ))}
            </div>
          )}

          <div>
            <label className="field">通用主标题（各平台超出上限将自动安全截断，也可在右侧单独定制）</label>
            <input
              value={title}
              onChange={e => setTitle(e.target.value)}
              placeholder="输入作品主标题…"
              maxLength={120}
            />
          </div>

          <div>
            <label className="field">通用正文描述</label>
            <textarea
              rows={4}
              value={desc}
              onChange={e => setDesc(e.target.value)}
              placeholder="输入各平台共用的正文描述…"
            />
          </div>

          <div className="form-grid">
            <div className="form-field">
              <label>话题标签（逗号分隔，免带 #）</label>
              <input value={topics} onChange={e => setTopics(e.target.value)} placeholder="例如：好物推荐, 户外露营" />
            </div>
            <div className="form-field">
              <label>矩阵自动错峰间隔（防同时间批量风控）</label>
              <SelectBox
                full
                value={staggerMinutes}
                onChange={setStaggerMinutes}
                ariaLabel="矩阵自动错峰间隔"
                options={[
                  { value: "0", label: "不错峰（同时进入队列）" },
                  { value: "5", label: "每账号顺延错开 5 分钟" },
                  { value: "10", label: "每账号顺延错开 10 分钟（推荐）" },
                  { value: "20", label: "每账号顺延错开 20 分钟" },
                  { value: "30", label: "每账号顺延错开 30 分钟" },
                ]}
              />
            </div>
          </div>

          <div className="form-field">
            <div className="mx-label-row">
              <label htmlFor="mx-pub-when">基准起始发布时间（留空则第 1 个号立即发，后续号按错峰顺延）</label>
              {scheduledAt && (
                <button type="button" className="ghost sm" onClick={() => setScheduledAt("")}>设为立即发布</button>
              )}
            </div>
            <input id="mx-pub-when" type="datetime-local" value={scheduledAt} onChange={e => setScheduledAt(e.target.value)} />
          </div>
        </div>

        {/* Right Preview & Per-Platform Title Cap / Override Inspector */}
        <aside className="mx-pub-side">
          <h4>{ctx.globalMode ? "各平台适配检查与微调" : `${pfName} 标题字数校验与微调`}</h4>
          {selectedPlatforms.length === 0 ? (
            <p className="mut" style={{ fontSize: "12.5px" }}>
              {ctx.globalMode
                ? "先在上方勾选账号，这里会实时显示各目标平台的字数校验与单独改写框。"
                : `先在上方勾选 ${pfName} 账号，这里会实时显示标题字数上限校验。`}
            </p>
          ) : (
            <div className="mx-pf-overrides">
              {selectedPlatforms.map(pfId => {
                const meta = PF_MAP[pfId] || PLATFORMS[0];
                const ov = overrides[pfId] || {};
                const effTitle = ov.title !== undefined ? ov.title : title;
                const overCap = effTitle.length > meta.titleCap;
                return (
                  <div key={pfId} className="mx-pf-ov-box">
                    <div className="mx-pf-ov-head">
                      <span className="mx-mini-pf inline" style={{ "--pf-c": meta.color }}>{meta.name}</span>
                      <span className={`mx-cap-cnt ${overCap ? "warn" : ""}`}>
                        标题 {effTitle.length} / {meta.titleCap}
                      </span>
                    </div>
                    <input
                      value={ov.title ?? ""}
                      onChange={e => setOverrides(prev => ({
                        ...prev,
                        [pfId]: { ...prev[pfId], title: e.target.value || undefined },
                      }))}
                      placeholder={`可选：单独覆盖${meta.name}标题（留空沿用主标题）`}
                    />
                  </div>
                );
              })}
            </div>
          )}

          <div className="mx-pub-submit-box">
            <button type="submit" disabled={submitting || uploading || selectedAccounts.length === 0}>
              <Icon name="send" />
              <span>{submitting ? "正在拆分入队…" : `一键入队分发 (${selectedAccounts.length} 个账号)`}</span>
            </button>
          </div>
        </aside>
      </div>
    </form>
  );
}
