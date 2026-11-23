import React, { useState, useEffect, useMemo, useCallback, useRef } from "react";
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
  safeThumb,
} from "./constants";

const HEALTH_FILTERS = [
  { key: "all", label: "全部账号", tone: "neutral" },
  { key: "invalid", label: "登录失效", tone: "danger" },
  { key: "proxy_issue", label: "代理故障", tone: "danger" },
  { key: "shared_proxy", label: "共用代理提醒", tone: "warn" },
  { key: "no_proxy", label: "直连风险提醒", tone: "warn" },
  { key: "risk", label: "风控冷却", tone: "warn" },
  { key: "unread_dm", label: "有未读私信", tone: "accent" },
  { key: "healthy", label: "健康可用", tone: "ok" },
];

const HEALTH_BADGE = {
  healthy: { text: "健康正常", cls: "ok" },
  invalid: { text: "登录失效", cls: "danger" },
  proxy_issue: { text: "代理故障", cls: "danger" },
  shared_proxy: { text: "共用代理提醒", cls: "warn" },
  risk: { text: "风控/冷却", cls: "warn" },
  no_proxy: { text: "本机直连提醒", cls: "warn" },
};

export function AccountMatrixCenter({ ctx }) {
  const bridge = window.CreatorHubBridge;
  const actions = bridge?.accountActions;

  const [payload, setPayload] = useState({ items: [], groups: [], all_tags: [] });
  const [loading, setLoading] = useState(true);
  const [viewMode, setViewMode] = useState(() => {
    try { return localStorage.getItem("dym-acc-view") || "groups"; } catch { return "groups"; }
  });
  const [pfFilter, setPfFilter] = useState("auto"); // auto follows ctx, or explicit "all" / pf
  const [healthFilter, setHealthFilter] = useState(() => {
    const preset = window.__chAccountPresetFilter || "all";
    window.__chAccountPresetFilter = "";
    return preset;
  });
  const [groupFilter, setGroupFilter] = useState("");
  const [roleFilter, setRoleFilter] = useState("");
  const [query, setQuery] = useState("");
  const [sortBy, setSortBy] = useState("default");
  const [selectedIds, setSelectedIds] = useState(() => new Set());
  const [collapsedGroups, setCollapsedGroups] = useState(() => new Set());

  // Quick Add Account bar platform selector
  const [loginPf, setLoginPf] = useState(ctx.platform || "douyin");

  // Modals / Inline Wizards
  const [editingMeta, setEditingMeta] = useState(null); // single account meta editor
  const [groupModal, setGroupModal] = useState(null); // { mode: 'create'|'edit', name, rename_to, color, description, preferred_country }
  const [batchProgress, setBatchProgress] = useState(null); // { label, done, total }
  const batchCancelRef = useRef(false);

  // Sequential Relogin Queue
  const [reloginQueue, setReloginQueue] = useState(null); // { list: [...], index: 0, statusText: '', running: false }
  const reloginPollRef = useRef(null);

  useEffect(() => {
    setPfFilter("auto");
    if (!ctx.globalMode && ctx.platform) setLoginPf(ctx.platform);
  }, [ctx.globalMode, ctx.platform]);

  const effectivePf = pfFilter === "auto" ? (ctx.globalMode ? "all" : ctx.platform) : pfFilter;

  const load = useCallback(async () => {
    try {
      const res = await matrixApi("/api/matrix/accounts");
      setPayload({
        items: Array.isArray(res.items) ? res.items : [],
        groups: Array.isArray(res.groups) ? res.groups : [],
        all_tags: Array.isArray(res.all_tags) ? res.all_tags : [],
      });
    } catch (_) {
      // keep existing items on transient failure
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
    const onRefresh = () => load();
    const onPreset = e => {
      if (e.detail) {
        setHealthFilter(e.detail);
        setViewMode("table");
      }
    };
    window.addEventListener("creatorhub:matrix:refresh", onRefresh);
    window.addEventListener("creatorhub:account-filter", onPreset);
    const timer = setInterval(() => {
      if (!document.hidden && ctx.tab === "accounts" && !editingMeta && !groupModal) load();
    }, 9000);
    return () => {
      clearInterval(timer);
      window.removeEventListener("creatorhub:matrix:refresh", onRefresh);
      window.removeEventListener("creatorhub:account-filter", onPreset);
      clearTimeout(reloginPollRef.current);
    };
  }, [load, ctx.tab, editingMeta, groupModal]);

  // Hide legacy 3-column #acc-table UI while keeping DOM nodes intact
  useEffect(() => {
    const card = document.querySelector('[data-panel="accounts"] [data-section="accounts"]');
    if (!card) return;
    card.classList.add("mx-accounts-enhanced");
    return () => card.classList.remove("mx-accounts-enhanced");
  }, []);

  const switchView = mode => {
    setViewMode(mode);
    try { localStorage.setItem("dym-acc-view", mode); } catch {}
  };

  // Scope accounts by platform first (for accurate health pill counts)
  const pfScopedItems = useMemo(() => {
    if (!effectivePf || effectivePf === "all") return payload.items;
    return payload.items.filter(a => a.platform === effectivePf);
  }, [payload.items, effectivePf]);

  const healthCounts = useMemo(() => {
    const c = { all: pfScopedItems.length, invalid: 0, proxy_issue: 0, shared_proxy: 0, risk: 0, no_proxy: 0, unread_dm: 0, healthy: 0 };
    pfScopedItems.forEach(a => {
      if (c[a.health_state] !== undefined) c[a.health_state]++;
      if (a.unread_dm > 0) c.unread_dm++;
    });
    return c;
  }, [pfScopedItems]);

  const filteredItems = useMemo(() => {
    const needle = query.trim().toLowerCase();
    const list = pfScopedItems.filter(a => {
      if (healthFilter !== "all") {
        if (healthFilter === "unread_dm") {
          if (!(a.unread_dm > 0)) return false;
        } else if (a.health_state !== healthFilter) {
          return false;
        }
      }
      if (groupFilter === "__ungrouped__" && a.group_name) return false;
      if (groupFilter && groupFilter !== "__ungrouped__" && a.group_name !== groupFilter) return false;
      if (roleFilter && a.account_role !== roleFilter) return false;
      if (!needle) return true;
      const hay = [
        a.display_name, a.nickname, a.alias, a.douyin_id, a.sec_uid,
        a.group_name, a.exit_ip, a.fp_country, a.fp_city, PF_MAP[a.platform]?.name,
        ...(a.tags || []),
      ].join(" ").toLowerCase();
      return hay.includes(needle);
    });

    const sorted = [...list];
    if (sortBy === "followers") sorted.sort((a, b) => b.follower_count - a.follower_count);
    else if (sortBy === "delta") sorted.sort((a, b) => b.fans_delta - a.fans_delta);
    else if (sortBy === "unread") sorted.sort((a, b) => b.unread_dm - a.unread_dm);
    else if (sortBy === "tasks") sorted.sort((a, b) => (b.tasks?.total || 0) - (a.tasks?.total || 0));
    else sorted.sort((a, b) => (Number(b.pinned) - Number(a.pinned)) || (a.sort_order - b.sort_order) || (a.id - b.id));
    return sorted;
  }, [pfScopedItems, healthFilter, groupFilter, roleFilter, query, sortBy]);

  // Group buckets for Group Matrix View
  const groupedBuckets = useMemo(() => {
    const groupMetaByName = new Map(payload.groups.map(g => [g.name, g]));
    const map = new Map();
    payload.groups.forEach(g => {
      map.set(g.name, { meta: g, accounts: [] });
    });
    const ungrouped = [];
    filteredItems.forEach(acc => {
      if (!acc.group_name) {
        ungrouped.push(acc);
      } else {
        if (!map.has(acc.group_name)) {
          map.set(acc.group_name, {
            meta: groupMetaByName.get(acc.group_name) || { name: acc.group_name, color: "blue", description: "" },
            accounts: [],
          });
        }
        map.get(acc.group_name).accounts.push(acc);
      }
    });
    const out = [...map.values()].filter(b => b.accounts.length > 0 || (!query && healthFilter === "all" && !roleFilter && (!groupFilter || groupFilter === b.meta.name)));
    if (ungrouped.length > 0 || out.length === 0) {
      out.push({
        meta: { name: "", label: "未分组账号", color: "slate", description: "建议按跨平台 IP 人设或业务团队归入矩阵组，便于一键群发与批量运维" },
        accounts: ungrouped,
        isUngrouped: true,
      });
    }
    return out;
  }, [filteredItems, payload.groups, query, healthFilter, roleFilter, groupFilter]);

  const toggleOne = id => {
    setSelectedIds(prev => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });
  };

  const toggleGroupSelection = accounts => {
    const ids = accounts.map(a => a.id);
    setSelectedIds(prev => {
      const next = new Set(prev);
      const allSelected = ids.length > 0 && ids.every(id => next.has(id));
      ids.forEach(id => { if (allSelected) next.delete(id); else next.add(id); });
      return next;
    });
  };

  const toggleSelectAllVisible = () => {
    const ids = filteredItems.map(a => a.id);
    const allIn = ids.length > 0 && ids.every(id => selectedIds.has(id));
    if (allIn) {
      setSelectedIds(prev => {
        const next = new Set(prev);
        ids.forEach(id => next.delete(id));
        return next;
      });
    } else {
      setSelectedIds(prev => new Set([...prev, ...ids]));
    }
  };

  const refreshEverything = async () => {
    await load();
    actions?.refreshAll?.();
  };

  // Single account pin quick toggle
  const togglePin = async acc => {
    try {
      await matrixApi(`/api/accounts/${acc.id}/meta`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ pinned: !acc.pinned }),
      });
      load();
    } catch (e) {
      matrixToast("置顶更新失败：" + e.message, "err");
    }
  };

  // Batch operations
  const runBatchApi = async (body, okMsg) => {
    const ids = [...selectedIds];
    if (!ids.length) return;
    try {
      const res = await matrixApi("/api/matrix/accounts/batch", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ account_ids: ids, ...body }),
      });
      matrixToast(typeof okMsg === "function" ? okMsg(res) : okMsg, "ok");
      if (res && res.warning) {
        setTimeout(() => matrixToast("⚠️ " + res.warning, "info", 6500), 280);
      }
      await refreshEverything();
    } catch (e) {
      matrixToast("批量操作失败：" + e.message, "err");
    }
  };

  const handleBatchGroup = async () => {
    const options = [
      ...payload.groups.map(g => ({ value: g.name, label: g.name })),
      { value: "__new__", label: "输入新建矩阵分组…" },
      { value: "", label: "移出分组（设为未分组）" },
    ];
    const picked = await window.uiSelect?.({
      title: `批量分配矩阵分组（已选 ${selectedIds.size} 个账号）`,
      hint: "跨平台账号可归入同一个 IP 人设组或业务组，方便聚合对比与一键群发。",
      options,
      value: payload.groups[0]?.name || "__new__",
    });
    if (picked === null || picked === undefined) return;
    let groupName = picked;
    if (picked === "__new__") {
      const custom = await window.uiPrompt?.({
        title: "新建矩阵分组并移入所选账号",
        hint: "例如：户外露营-主IP、美妆矩阵A组、纯采集工具号池",
        placeholder: "输入矩阵分组名称",
      });
      if (!custom || !custom.trim()) return;
      groupName = custom.trim();
      await matrixApi("/api/matrix/groups", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: groupName, color: "blue" }),
      }).catch(() => {});
    }
    await runBatchApi({ action: "set_group", group_name: groupName }, groupName ? `已移入「${groupName}」` : "已移出分组");
  };

  const handleBatchRole = async () => {
    const picked = await window.uiSelect?.({
      title: `批量设置账号职能角色（已选 ${selectedIds.size} 个账号）`,
      hint: "将核心大号标为「创作主号」可避免误用于高频爬虫；工具小号标为「采集小号」。",
      options: [
        { value: "publish", label: "创作主号（专注发布 / 私信，避免高频抓取）" },
        { value: "crawler", label: "采集小号（专供监控 / 关键词与评论采集）" },
        { value: "hybrid", label: "综合通用（发布与采集通用）" },
      ],
      value: "publish",
    });
    if (!picked) return;
    await runBatchApi({ action: "set_role", account_role: picked }, `已批量设为「${ROLE_META[picked]?.label || picked}」`);
  };

  const handleBatchTags = async () => {
    const raw = await window.uiPrompt?.({
      title: `批量追加标签（已选 ${selectedIds.size} 个账号）`,
      hint: "多个标签用逗号分隔；输入 CLEAR 可清空所选账号标签。",
      placeholder: "例如：主力变现, 日更, 美区",
    });
    if (raw === null || raw === undefined || !raw.trim()) return;
    if (raw.trim().toUpperCase() === "CLEAR") {
      await runBatchApi({ action: "clear_tags" }, "已清空所选账号标签");
    } else {
      await runBatchApi({ action: "add_tags", tags: raw }, "已批量追加标签");
    }
  };

  const handleBatchRuntime = async () => {
    let catalog;
    try {
      catalog = await matrixApi("/api/browser-backends");
    } catch (e) {
      matrixToast("读取内核列表失败：" + e.message, "err");
      return;
    }
    const opts = typeof window.browserChoiceOptions === "function"
      ? window.browserChoiceOptions(catalog, { localOnly: false })
      : [{ value: "default", label: "跟随全局默认" }, { value: "local", label: "系统 Chrome / Patchright" }];
    const selected = await window.uiSelect?.({
      title: `批量切换浏览器内核（已选 ${selectedIds.size} 个账号）`,
      hint: "小红书账号若包含在内会自动保持系统 Chrome 原生环境，其余平台切换至所选内核。",
      options: opts,
      value: "default",
    });
    if (!selected) return;
    const [backend, runtimeId = ""] = String(selected).split("::", 2);
    await runBatchApi(
      { action: "set_browser_backend", browser_backend: backend || "default", browser_runtime_id: runtimeId },
      res => `已切换 ${res.updated} 个账号内核${res.skipped_xhs ? `（已自动跳过 ${res.skipped_xhs} 个小红书原生账号）` : ""}`
    );
  };

  const handleBatchSequentialCheck = async kind => {
    const ids = [...selectedIds];
    if (!ids.length || batchProgress) return;
    batchCancelRef.current = false;
    const title = kind === "proxy" ? "批量检测代理与出口" : "批量刷新资料与登录态";
    setBatchProgress({ label: title, done: 0, total: ids.length });
    let okCount = 0;
    let failCount = 0;
    for (let i = 0; i < ids.length; i++) {
      if (batchCancelRef.current) break;
      setBatchProgress({ label: title, done: i, total: ids.length });
      try {
        if (kind === "proxy") {
          const r = await matrixApi(`/api/accounts/${ids[i]}/test-proxy`, { method: "POST" });
          if (r.ok) okCount++; else failCount++;
        } else {
          const r = await matrixApi(`/api/accounts/${ids[i]}/refresh-profile`, { method: "POST" });
          if (r.ok && !r.skipped) okCount++; else failCount++;
        }
      } catch (_) {
        failCount++;
      }
    }
    setBatchProgress(null);
    matrixToast(`${title}完成：成功 ${okCount} 个${failCount ? `，未通过/跳过 ${failCount} 个` : ""}`, failCount ? "info" : "ok", 5500);
    await refreshEverything();
  };

  // Sequential Relogin Wizard for invalid accounts
  const startSequentialRelogin = list => {
    const invalidList = list.filter(a => a.health_state === "invalid" || a.status === "invalid");
    if (!invalidList.length) {
      matrixToast("当前没有需要重新登录的失效账号", "info");
      return;
    }
    const queueObj = { list: invalidList, index: 0, statusText: "准备唤起第 1 个账号登录窗口…", running: true };
    setReloginQueue(queueObj);
    triggerReloginStep(invalidList, 0);
  };

  const triggerReloginStep = async (list, idx) => {
    clearTimeout(reloginPollRef.current);
    if (idx >= list.length) {
      setReloginQueue(null);
      matrixToast("连续扫码补登队列已全部完成 ✓", "ok", 5000);
      refreshEverything();
      return;
    }
    const acc = list[idx];
    setReloginQueue({ list, index: idx, statusText: `正在打开「${acc.display_name}」(${PF_MAP[acc.platform]?.name}) 登录窗口…`, running: true });
    try {
      const res = await matrixApi(`/api/accounts/${acc.id}/relogin/start?scope=auto`, { method: "POST" });
      setReloginQueue({ list, index: idx, statusText: "请在弹出的独立浏览器窗口扫码登录，成功后将自动切换下一个账号…", running: true });
      const poll = async () => {
        try {
          const st = await matrixApi(`/api/login/browser/poll?task_id=${encodeURIComponent(res.task_id)}`);
          if (st.status === "confirmed") {
            matrixToast(`「${acc.display_name}」补登成功`, "ok");
            refreshEverything();
            triggerReloginStep(list, idx + 1);
            return;
          }
          if (st.status === "expired" || st.status === "error") {
            setReloginQueue({ list, index: idx, statusText: `本次扫码未完成 (${st.error || st.status})，可点重试或跳到下一个`, running: false });
            return;
          }
          reloginPollRef.current = setTimeout(poll, 800);
        } catch (_) {
          setReloginQueue({ list, index: idx, statusText: "状态轮询中断，可手动重试或进入下一个", running: false });
        }
      };
      poll();
    } catch (e) {
      setReloginQueue({ list, index: idx, statusText: `启动失败：${e.message}`, running: false });
    }
  };

  const saveAccountMetaModal = async e => {
    e.preventDefault();
    if (!editingMeta) return;
    try {
      await matrixApi(`/api/accounts/${editingMeta.id}/meta`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          alias: editingMeta.alias,
          group_name: editingMeta.group_name,
          account_role: editingMeta.account_role,
          tags: editingMeta.tagsText,
          pinned: editingMeta.pinned,
        }),
      });
      matrixToast("账号矩阵归类信息已保存", "ok");
      setEditingMeta(null);
      refreshEverything();
    } catch (err) {
      matrixToast("保存失败：" + err.message, "err");
    }
  };

  const saveGroupModal = async e => {
    e.preventDefault();
    if (!groupModal) return;
    try {
      await matrixApi("/api/matrix/groups", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          name: groupModal.mode === "edit" ? groupModal.originalName : groupModal.name,
          rename_to: groupModal.name,
          color: groupModal.color,
          description: groupModal.description,
          preferred_country: groupModal.preferred_country,
        }),
      });
      matrixToast("矩阵分组已保存", "ok");
      setGroupModal(null);
      load();
    } catch (err) {
      matrixToast("分组保存失败：" + err.message, "err");
    }
  };

  const deleteGroup = async name => {
    const ok = await window.uiConfirm?.({
      title: `解散矩阵组「${name}」`,
      message: "仅移除该分组归类（组内账号将变为未分组，不会删除任何账号或登录态）。确认继续？",
      okText: "移除分组",
      danger: true,
    });
    if (!ok) return;
    try {
      await matrixApi(`/api/matrix/groups/${encodeURIComponent(name)}`, { method: "DELETE" });
      matrixToast(`已移除分组「${name}」`, "ok");
      setGroupModal(null);
      if (groupFilter === name) setGroupFilter("");
      load();
    } catch (err) {
      matrixToast("删除分组失败：" + err.message, "err");
    }
  };

  const jumpGroupPublish = accounts => {
    const validIds = accounts.filter(a => a.status !== "invalid").map(a => a.id);
    window.__chPublishPresetAccounts = validIds;
    window.dispatchEvent(new CustomEvent("creatorhub:publish-preset", { detail: { accountIds: validIds } }));
    bridge?.navigate?.("publish", true);
    setTimeout(() => window.CreatorHubWorkbench?.showSection?.("publish", "matrix"), 0);
  };

  const loginPfMeta = PF_MAP[loginPf] || PLATFORMS[0];
  const openCount = pfScopedItems.filter(a => a.browser_open).length;
  const invalidInScope = pfScopedItems.filter(a => a.health_state === "invalid");

  return (
    <div className="mx-acc-wrap">
      {/* Cross-Platform Add Account & Fast Actions Ribbon */}
      <div className="mx-add-acc-bar">
        <div className="mx-add-acc-left">
          <span className="mx-bar-kicker"><Icon name="plus" />快速接入新账号</span>
          <SelectBox
            value={loginPf}
            onChange={setLoginPf}
            ariaLabel="选择要添加账号的目标平台"
            options={PLATFORMS.map(p => ({ value: p.id, label: p.name, dot: p.color }))}
          />
          <button type="button" className="sm" onClick={() => bridge?.launchPlatformLogin?.(loginPf, "main")}>
            <Icon name="qr" /><span>{loginPfMeta.name}扫码 / 网页登录</span>
          </button>
          {loginPfMeta.hasCreatorLogin && (
            <button type="button" className="ghost sm" onClick={() => bridge?.launchPlatformLogin?.(loginPf, "creator")}>
              <Icon name="send" /><span>创作者中心登录</span>
            </button>
          )}
          {loginPfMeta.hasCookie && (
            <button type="button" className="ghost sm" onClick={() => bridge?.launchPlatformLogin?.(loginPf, "cookie")}>
              <Icon name="cookie" /><span>Cookie / Token 导入</span>
            </button>
          )}
        </div>
        <div className="mx-add-acc-right">
          {invalidInScope.length > 0 && (
            <button
              type="button"
              className="sm warning-action"
              onClick={() => startSequentialRelogin(invalidInScope)}
            >
              <Icon name="scan" /><span>连续补登 {invalidInScope.length} 个失效号</span>
            </button>
          )}
          <button
            type="button"
            className="ghost sm"
            onClick={() => setGroupModal({ mode: "create", name: "", originalName: "", color: "blue", description: "", preferred_country: "" })}
          >
            <Icon name="folder" /><span>新建矩阵组</span>
          </button>
        </div>
      </div>

      {/* Sequential Relogin Active Banner */}
      {reloginQueue && (
        <div className="mx-wizard-bar" role="region" aria-label="连续扫码补登向导">
          <div className="mx-wizard-info">
            <span className="mx-st-pill warn">补登进度 {reloginQueue.index + 1} / {reloginQueue.list.length}</span>
            <b>{reloginQueue.list[reloginQueue.index]?.display_name}</b>
            <span className="mut">({PF_MAP[reloginQueue.list[reloginQueue.index]?.platform]?.name})</span>
            <span className="mx-wizard-status">{reloginQueue.statusText}</span>
          </div>
          <div className="mx-wizard-actions">
            <button type="button" className="ghost sm" onClick={() => triggerReloginStep(reloginQueue.list, reloginQueue.index)}>
              重试当前号
            </button>
            <button type="button" className="ghost sm" onClick={() => triggerReloginStep(reloginQueue.list, reloginQueue.index + 1)}>
              跳过，下一个
            </button>
            <button type="button" className="ghost sm danger" onClick={() => { clearTimeout(reloginPollRef.current); setReloginQueue(null); }}>
              结束向导
            </button>
          </div>
        </div>
      )}

      {/* Batch Sequential Check Progress Banner */}
      {batchProgress && (
        <div className="mx-wizard-bar info" role="status">
          <div className="mx-wizard-info">
            <b>{batchProgress.label}</b>
            <span>正在处理 {batchProgress.done + 1} / {batchProgress.total}…</span>
          </div>
          <button type="button" className="ghost sm danger" onClick={() => { batchCancelRef.current = true; }}>
            停止批量任务
          </button>
        </div>
      )}

      {/* Health Status Quick-Filter Pills */}
      <div className="mx-health-pills" role="tablist" aria-label="按账号健康状态快速筛选">
        {HEALTH_FILTERS.map(f => {
          const cnt = healthCounts[f.key] || 0;
          const active = healthFilter === f.key;
          return (
            <button
              key={f.key}
              type="button"
              role="tab"
              aria-selected={active}
              className={`mx-hpill tone-${f.tone} ${active ? "active" : ""} ${cnt === 0 && f.key !== "all" ? "zero" : ""}`}
              onClick={() => setHealthFilter(f.key)}
            >
              <span>{f.label}</span>
              <b>{cnt}</b>
            </button>
          );
        })}
      </div>

      {/* Multi-Dimensional Filter Toolbar + View Switcher */}
      <div className="mx-acc-toolbar">
        <label className="mx-search-box">
          <Icon name="search" />
          <input
            type="search"
            value={query}
            onChange={e => setQuery(e.target.value)}
            placeholder="搜备注名 / 昵称 / 平台号 / 分组 / 标签 / 出口 IP…"
            aria-label="搜索矩阵账号"
          />
        </label>

        <SelectBox
          value={effectivePf}
          onChange={setPfFilter}
          ariaLabel="平台筛选"
          options={[
            { value: "all", label: "全部平台", icon: "globe", count: payload.items.length },
            ...PLATFORMS.map(p => ({
              value: p.id,
              label: p.name,
              dot: p.color,
              count: payload.items.filter(x => x.platform === p.id).length,
            })),
          ]}
        />

        <SelectBox
          value={groupFilter}
          onChange={setGroupFilter}
          ariaLabel="矩阵分组筛选"
          options={[
            { value: "", label: "全部矩阵组", icon: "layers" },
            ...payload.groups.map(g => ({
              value: g.name,
              label: g.name,
              dot: GROUP_COLOR_META[g.color]?.dot || "#3b82f6",
            })),
            { value: "__ungrouped__", label: "未分组账号" },
          ]}
        />

        <SelectBox
          value={roleFilter}
          onChange={setRoleFilter}
          ariaLabel="职能角色筛选"
          options={[
            { value: "", label: "全部角色" },
            { value: "publish", label: "创作主号", icon: "film" },
            { value: "crawler", label: "采集小号", icon: "target" },
            { value: "hybrid", label: "综合通用", icon: "refresh" },
          ]}
        />

        <SelectBox
          value={sortBy}
          onChange={setSortBy}
          ariaLabel="账号排序"
          options={[
            { value: "default", label: "排序：置顶优先" },
            { value: "followers", label: "排序：粉丝从高到低" },
            { value: "delta", label: "排序：近期涨粉最快" },
            { value: "unread", label: "排序：未读私信优先" },
            { value: "tasks", label: "排序：任务负载优先" },
          ]}
        />

        <div className="mx-view-seg" role="group" aria-label="账号视图切换">
          <button type="button" className={viewMode === "groups" ? "active" : ""} onClick={() => switchView("groups")} title="按跨平台人设或业务分组折叠展示">
            <Icon name="layers" /><span>矩阵分组</span>
          </button>
          <button type="button" className={viewMode === "table" ? "active" : ""} onClick={() => switchView("table")} title="高密度百号巡检表格">
            <Icon name="list" /><span>高密表格</span>
          </button>
          <button type="button" className={viewMode === "browsers" ? "active" : ""} onClick={() => switchView("browsers")} title="实时浏览器窗口与工作会话网格">
            <Icon name="monitor" /><span>浏览器墙{openCount > 0 ? ` (${openCount})` : ""}</span>
          </button>
        </div>
      </div>

      {/* Floating / Sticky Batch Operations Bar */}
      {selectedIds.size > 0 && (
        <div className="mx-batch-bar" role="region" aria-label="账号批量运维工具栏">
          <div className="mx-batch-count">
            <b>已勾选 {selectedIds.size} 个账号</b>
            <button type="button" className="ghost sm" onClick={toggleSelectAllVisible}>
              {filteredItems.every(a => selectedIds.has(a.id)) ? "取消本页全选" : `全选筛选结果 (${filteredItems.length})`}
            </button>
            <button type="button" className="ghost sm" onClick={() => setSelectedIds(new Set())}>清空选择</button>
          </div>
          <div className="mx-batch-actions">
            <button type="button" className="ghost sm" onClick={handleBatchGroup}><Icon name="folder" />设矩阵组</button>
            <button type="button" className="ghost sm" onClick={handleBatchRole}><Icon name="shield" />设职能角色</button>
            <button type="button" className="ghost sm" onClick={handleBatchTags}><Icon name="hash" />打标签</button>
            <button
              type="button"
              className="ghost sm"
              onClick={() => runBatchApi(
                { action: "assign_proxies", overwrite_existing_proxy: false },
                r => `已为 ${r.assigned} 个无代理账号分配池节点${r.shared_assigned ? `（含复用 ${r.shared_assigned} 个）` : ""}${r.unassigned ? `，未分配 ${r.unassigned} 个` : ""}`
              )}
            >
              <Icon name="globe" />自动配代理
            </button>
            <button
              type="button"
              className="ghost sm"
              onClick={async () => {
                const ok = await window.uiConfirm?.({
                  title: `批量切换为本机直连（已选 ${selectedIds.size} 个账号）`,
                  message: "清除所选账号代理后将改走本机直连（不拦截任务，但暴露真实 IP，多账号同平台或海外平台存在关联风控风险）。确认清除？",
                  okText: "清除代理改直连",
                });
                if (!ok) return;
                await runBatchApi({ action: "clear_proxy" }, r => `已清除 ${r.updated} 个账号代理（当前改走本机直连）`);
              }}
            >
              <Icon name="x" />清除代理(直连)
            </button>
            <button type="button" className="ghost sm" onClick={handleBatchRuntime}><Icon name="settings" />切内核</button>
            <button type="button" className="ghost sm" disabled={!!batchProgress} onClick={() => handleBatchSequentialCheck("proxy")}>
              <Icon name="network" />批量测代理
            </button>
            <button type="button" className="ghost sm" disabled={!!batchProgress} onClick={() => handleBatchSequentialCheck("profile")}>
              <Icon name="refresh" />批量刷资料
            </button>
            <button
              type="button"
              className="sm"
              onClick={() => jumpGroupPublish(payload.items.filter(a => selectedIds.has(a.id)))}
            >
              <Icon name="send" />去多号群发
            </button>
          </div>
        </div>
      )}

      {/* Cross-Platform Matrix Group View */}
      {viewMode === "groups" && (
        <div className="mx-groups-stack">
          {loading && !payload.items.length ? (
            <div className="mx-empty-box"><span>正在读取多平台账号矩阵…</span></div>
          ) : filteredItems.length === 0 && payload.items.length === 0 ? (
            <div className="mx-empty-box">
              <Icon name="card" />
              <b>尚未接入任何平台账号</b>
              <span>在上方选择目标平台点击「扫码 / 网页登录」，建议将跨平台同一人设的账号归入同一矩阵组。</span>
            </div>
          ) : (
            groupedBuckets.map(bucket => {
              const gKey = bucket.meta.name || "__ungrouped__";
              const collapsed = collapsedGroups.has(gKey);
              const colorDot = GROUP_COLOR_META[bucket.meta.color]?.dot || "#3b82f6";
              const followersSum = bucket.accounts.reduce((s, a) => s + (a.follower_count || 0), 0);
              const healthyCount = bucket.accounts.filter(a => a.health_state === "healthy").length;
              const allGroupChecked = bucket.accounts.length > 0 && bucket.accounts.every(a => selectedIds.has(a.id));

              return (
                <section key={gKey} className="mx-group-card">
                  <header className="mx-group-head">
                    <div
                      className="mx-group-title"
                      onClick={() => setCollapsedGroups(prev => {
                        const next = new Set(prev);
                        if (next.has(gKey)) next.delete(gKey); else next.add(gKey);
                        return next;
                      })}
                      role="button"
                      tabIndex={0}
                    >
                      <span className="mx-group-dot" style={{ background: colorDot }} />
                      <h3>{bucket.meta.label || bucket.meta.name}</h3>
                      <span className="mx-group-badge">{bucket.accounts.length} 个账号</span>
                      <span className="mut" style={{ fontSize: "12px" }}>
                        {healthyCount}/{bucket.accounts.length} 健康 · 共 {fmtCount(followersSum)} 粉丝
                      </span>
                      {bucket.meta.description && <span className="mx-group-desc">· {bucket.meta.description}</span>}
                    </div>
                    <div className="mx-group-actions">
                      {bucket.accounts.length > 0 && (
                        <>
                          <button type="button" className="ghost sm" onClick={() => toggleGroupSelection(bucket.accounts)}>
                            {allGroupChecked ? "取消本组" : "全选本组"}
                          </button>
                          <button type="button" className="ghost sm" onClick={() => jumpGroupPublish(bucket.accounts)}>
                            <Icon name="send" />本组群发
                          </button>
                        </>
                      )}
                      {!bucket.isUngrouped && (
                        <button
                          type="button"
                          className="ghost sm"
                          onClick={() => setGroupModal({
                            mode: "edit",
                            originalName: bucket.meta.name,
                            name: bucket.meta.name,
                            color: bucket.meta.color || "blue",
                            description: bucket.meta.description || "",
                            preferred_country: bucket.meta.preferred_country || "",
                          })}
                        >
                          <Icon name="edit" />设置
                        </button>
                      )}
                    </div>
                  </header>

                  {!collapsed && (
                    bucket.accounts.length > 0 ? (
                      <div className="mx-acc-cards">
                        {bucket.accounts.map(acc => {
                          const pf = PF_MAP[acc.platform] || { short: acc.platform, name: acc.platform, color: "#64748b" };
                          const hb = HEALTH_BADGE[acc.health_state] || HEALTH_BADGE.healthy;
                          const role = ROLE_META[acc.account_role] || ROLE_META.hybrid;
                          const checked = selectedIds.has(acc.id);
                          return (
                            <div key={acc.id} className={`mx-acc-card ${checked ? "is-selected" : ""} health-${acc.health_state}`}>
                              <div className="mx-acc-card-top">
                                <input
                                  type="checkbox"
                                  checked={checked}
                                  onChange={() => toggleOne(acc.id)}
                                  aria-label={`勾选 ${acc.display_name}`}
                                />
                                <span className="mx-mini-pf inline" style={{ "--pf-c": pf.color }}>{pf.name}</span>
                                <span className={`mx-role-tag role-${acc.account_role}`} title={role.hint}>{role.label}</span>
                                <span className={`mx-st-pill ${hb.cls}`} style={{ marginLeft: "auto" }}>{hb.text}</span>
                              </div>

                              <div className="mx-acc-identity">
                                {acc.avatar ? (
                                  <>
                                    <img
                                      className="avatar"
                                      src={safeThumb(acc.avatar)}
                                      alt=""
                                      referrerPolicy="no-referrer"
                                      onError={e => {
                                        e.currentTarget.style.display = "none";
                                        if (e.currentTarget.nextElementSibling) e.currentTarget.nextElementSibling.style.display = "";
                                      }}
                                    />
                                    <span className="wb-avatar-fallback" style={{ display: "none" }}>{(acc.display_name || "号").slice(0, 1)}</span>
                                  </>
                                ) : (
                                  <span className="wb-avatar-fallback">{(acc.display_name || "号").slice(0, 1)}</span>
                                )}
                                <div className="mx-acc-names">
                                    <div className="mx-acc-title-row">
                                      <b title={acc.display_name}>{acc.display_name}</b>
                                      <button
                                        type="button"
                                        className={`mx-pin-btn ${acc.pinned ? "pinned" : ""}`}
                                        onClick={() => togglePin(acc)}
                                        title={acc.pinned ? "取消置顶" : "置顶核心账号"}
                                        aria-label={acc.pinned ? "取消置顶" : "置顶核心账号"}
                                      >
                                        <Icon name="star" />
                                      </button>
                                    </div>
                                    <small>
                                      {acc.alias && acc.nickname && acc.alias !== acc.nickname ? `原名:${acc.nickname} · ` : ""}
                                      {acc.douyin_id || `#${acc.id}`}
                                    </small>
                                  </div>
                                </div>

                                <div className="mx-acc-stats-line">
                                  <span>粉丝 <b>{fmtCount(acc.follower_count)}</b>{acc.fans_delta ? <em className={acc.fans_delta > 0 ? "up" : "down"}>{fmtDelta(acc.fans_delta)}</em> : null}</span>
                                  <span>作品 <b>{fmtCount(acc.aweme_count)}</b></span>
                                  {acc.unread_dm > 0 && <span className="mx-dm-chip"><Icon name="msg" />{acc.unread_dm} 未读</span>}
                                </div>

                              <div className="mx-acc-env-line">
                                <span title={(acc.proxy_warnings && acc.proxy_warnings.length) ? acc.proxy_warnings.join("；") : (acc.proxy || "当前走本机直连")}>
                                  <Icon name="globe" />
                                  {acc.has_proxy ? (acc.exit_country || acc.fp_city || acc.proxy) : "⚠️ 本机直连"}
                                </span>
                                {acc.shared_proxy && (
                                  <span className="mx-mini-tag warn" title={(acc.proxy_warnings || []).join("；") || "同平台多账号正共用该代理或出口 IP，建议尽量一号一代理"}>
                                    ⚠️ 共用({acc.shared_proxy_count || 2}号)
                                  </span>
                                )}
                                {!acc.shared_proxy && acc.cross_platform_shared && (
                                  <span className="mx-mini-tag info" title={(acc.proxy_warnings || []).join("；") || "跨平台复用同一代理"}>跨平台复用</span>
                                )}
                                {acc.browser_open && <span className="mx-open-chip">窗口已开</span>}
                                {(acc.tags || []).slice(0, 2).map(t => (
                                  <span key={t} className="mx-tag-chip">#{t}</span>
                                ))}
                              </div>

                              <div className="mx-acc-card-foot">
                                {acc.health_state === "invalid" ? (
                                  <button type="button" className="sm warning-action" onClick={() => actions?.relogin?.(acc.id)}>重新扫码</button>
                                ) : (
                                  <button type="button" className="ghost sm" onClick={() => actions?.openBrowser?.(acc.id)}>开浏览器</button>
                                )}
                                <button type="button" className="ghost sm" onClick={() => bridge?.openAccount?.(acc.id)}>数据/私信</button>
                                <button
                                  type="button"
                                  className="ghost sm"
                                  onClick={() => setEditingMeta({
                                    id: acc.id,
                                    nickname: acc.nickname,
                                    platform: acc.platform,
                                    alias: acc.alias || "",
                                    group_name: acc.group_name || "",
                                    account_role: acc.account_role || "hybrid",
                                    tagsText: (acc.tags || []).join(", "),
                                    pinned: !!acc.pinned,
                                  })}
                                  title="编辑备注别名、所属矩阵组、角色与标签"
                                >
                                  归类
                                </button>
                                <button type="button" className="ghost sm" onClick={() => actions?.setProxy?.(acc.id)} title="配置独立代理">代理</button>
                              </div>
                            </div>
                          );
                        })}
                      </div>
                    ) : (
                      <div className="mx-empty-compact">本分组当前筛选条件下暂无账号，勾选其他账号点「设矩阵组」即可移入。</div>
                    )
                  )}
                </section>
              );
            })
          )}
        </div>
      )}

      {/* High-Density Operations DataGrid */}
      {viewMode === "table" && (
        <div className="table-wrap mx-dense-table-wrap">
          <table className="mx-dense-table" aria-label="多平台账号高密度运维表">
            <thead>
              <tr>
                <th style={{ width: "38px" }}>
                  <input
                    type="checkbox"
                    checked={filteredItems.length > 0 && filteredItems.every(a => selectedIds.has(a.id))}
                    onChange={toggleSelectAllVisible}
                    aria-label="全选当前列表账号"
                  />
                </th>
                <th>平台 / 账号身份</th>
                <th>矩阵分组 / 角色</th>
                <th>健康与风控状态</th>
                <th>代理与网络环境</th>
                <th>粉丝 / 负载 / 私信</th>
                <th style={{ textAlign: "right" }}>快捷运维</th>
              </tr>
            </thead>
            <tbody>
              {filteredItems.length === 0 ? (
                <tr>
                  <td colSpan={7}>
                    <div className="mx-empty-compact">没有符合当前筛选条件的账号，可点击上方「全部账号」重置筛选。</div>
                  </td>
                </tr>
              ) : (
                filteredItems.map(acc => {
                  const pf = PF_MAP[acc.platform] || { name: acc.platform, short: acc.platform, color: "#64748b" };
                  const hb = HEALTH_BADGE[acc.health_state] || HEALTH_BADGE.healthy;
                  const role = ROLE_META[acc.account_role] || ROLE_META.hybrid;
                  const checked = selectedIds.has(acc.id);
                  return (
                    <tr key={acc.id} className={checked ? "is-selected" : ""}>
                      <td>
                        <input type="checkbox" checked={checked} onChange={() => toggleOne(acc.id)} aria-label={`勾选 ${acc.display_name}`} />
                      </td>
                      <td>
                        <div className="mx-td-acc">
                          <button type="button" className={`mx-pin-btn ${acc.pinned ? "pinned" : ""}`} onClick={() => togglePin(acc)} title="置顶" aria-label="置顶"><Icon name="star" /></button>
                          <span className="mx-mini-pf inline" style={{ "--pf-c": pf.color }}>{pf.short}</span>
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
                            <div className="mx-td-name">
                              <b>{acc.display_name}</b>
                              {acc.alias && acc.nickname && acc.alias !== acc.nickname && <span className="mut">({acc.nickname})</span>}
                            </div>
                            <div className="mut" style={{ fontSize: "11.5px" }}>{acc.douyin_id || acc.sec_uid?.slice(0, 16) || `ID #${acc.id}`}</div>
                          </div>
                        </div>
                      </td>
                      <td>
                        <div className="mx-td-meta">
                          {acc.group_name ? <span className="mx-grp-chip"><Icon name="folder" />{acc.group_name}</span> : <span className="mut">未分组</span>}
                          <span className={`mx-role-tag role-${acc.account_role}`}>{role.label}</span>
                          {(acc.tags || []).map(t => <span key={t} className="mx-tag-chip">#{t}</span>)}
                        </div>
                      </td>
                      <td>
                        <span className={`mx-st-pill ${hb.cls}`}>{hb.text}</span>
                        {acc.risk_level > 0 && <span className="mx-mini-tag warn" style={{ marginLeft: "4px" }}>L{acc.risk_level}</span>}
                        {acc.risk_reason && <div className="mut" style={{ fontSize: "11px", marginTop: "3px" }} title={acc.risk_reason}>{acc.risk_reason.slice(0, 28)}</div>}
                      </td>
                      <td>
                        {acc.has_proxy ? (
                          <div title={(acc.proxy_warnings || []).join("；")}>
                            <code style={{ fontSize: "11.5px" }}>{acc.proxy}</code>
                            <div className="mut" style={{ fontSize: "11px", display: "flex", alignItems: "center", gap: "4px", flexWrap: "wrap" }}>
                              <span>{acc.proxy_status}{acc.exit_ip ? ` · ${acc.exit_ip}` : ""}</span>
                              {acc.shared_proxy && <span className="mx-mini-tag warn">⚠️ 共用({acc.shared_proxy_count || 2}号)</span>}
                              {!acc.shared_proxy && acc.cross_platform_shared && <span className="mx-mini-tag info">跨平台复用</span>}
                              {!acc.ip_matches_exit && <span className="mx-mini-tag warn">出口漂移</span>}
                            </div>
                          </div>
                        ) : (
                          <div title={(acc.proxy_warnings || []).join("；") || "当前走本机直连（不拦截任务，多号或海外平台建议配置代理）"}>
                            <span className="mx-mini-tag warn">⚠️ 本机直连</span>
                          </div>
                        )}
                      </td>
                      <td>
                        <div style={{ fontSize: "12.5px" }}>
                          <b>{fmtCount(acc.follower_count)}</b> 粉
                          {acc.fans_delta ? <em className={`mx-delta ${acc.fans_delta > 0 ? "up" : "down"}`}>{fmtDelta(acc.fans_delta)}</em> : null}
                          <span className="mut"> · {acc.monitor_count} 监控</span>
                        </div>
                        <div className="mut" style={{ fontSize: "11px", display: "flex", alignItems: "center", gap: "6px", flexWrap: "wrap" }}>
                          <span>队列 {acc.tasks?.running || 0} 运行 / {acc.tasks?.pending || 0} 排队</span>
                          {acc.unread_dm > 0 && <span className="mx-dm-chip"><Icon name="msg" />{acc.unread_dm}</span>}
                        </div>
                      </td>
                      <td className="acttd">
                        <div className="mx-row-btns">
                          {acc.health_state === "invalid" ? (
                            <button type="button" className="sm warning-action" onClick={() => actions?.relogin?.(acc.id)}>补登</button>
                          ) : (
                            <button type="button" className="ghost sm" onClick={() => actions?.openBrowser?.(acc.id)}>浏览器</button>
                          )}
                          <button type="button" className="ghost sm" onClick={() => bridge?.openAccount?.(acc.id)}>数据</button>
                          <button
                            type="button"
                            className="ghost sm"
                            onClick={() => setEditingMeta({
                              id: acc.id, nickname: acc.nickname, platform: acc.platform,
                              alias: acc.alias || "", group_name: acc.group_name || "",
                              account_role: acc.account_role || "hybrid",
                              tagsText: (acc.tags || []).join(", "), pinned: !!acc.pinned,
                            })}
                          >
                            归类
                          </button>
                          <button type="button" className="ghost sm" onClick={() => actions?.setProxy?.(acc.id)}>代理</button>
                          <button type="button" className="ghost sm" onClick={() => actions?.manageFingerprint?.(acc.id)}>指纹</button>
                          <button type="button" className="ghost sm" onClick={() => actions?.setBrowserBackend?.(acc.id)}>内核</button>
                          <button type="button" className="ghost sm danger" onClick={() => actions?.delAccount?.(acc.id)} aria-label="删除"><Icon name="trash" /></button>
                        </div>
                      </td>
                    </tr>
                  );
                })
              )}
            </tbody>
          </table>
        </div>
      )}

      {/* Live Browser Session Grid Wall */}
      {viewMode === "browsers" && (
        <div>
          <div className="mx-browser-wall-head">
            <span className="mut">
              实时显示全部独立 Profile 浏览器状态：<b>{openCount}</b> 个手动窗口开启中。手动窗口开启时后台自动采集会自动礼让暂停，关窗自动持久化最新 Cookie。
            </span>
            {openCount > 0 && (
              <button
                type="button"
                className="ghost sm danger"
                onClick={async () => {
                  const openIds = pfScopedItems.filter(a => a.browser_open).map(a => a.id);
                  if (!openIds.length) return;
                  await matrixApi("/api/matrix/accounts/batch", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ account_ids: openIds, action: "close_browsers" }),
                  });
                  matrixToast(`已安全关闭并保存 ${openIds.length} 个浏览器会话`, "ok");
                  load();
                }}
              >
                <Icon name="x" />一键回收全部已开窗口 ({openCount})
              </button>
            )}
          </div>
          <div className="mx-browser-grid">
            {filteredItems.map(acc => {
              const pf = PF_MAP[acc.platform] || { name: acc.platform, color: "#64748b" };
              const state = acc.browser_open ? "open" : acc.worker_running ? "worker" : "idle";
              return (
                <div key={acc.id} className={`mx-bcard state-${state}`}>
                  <div className="mx-bcard-head">
                    <span className="mx-mini-pf inline" style={{ "--pf-c": pf.color }}>{pf.name}</span>
                    <span className={`mx-st-pill ${state === "open" ? "ok" : state === "worker" ? "info" : "muted"}`}>
                      <span className="mx-st-dot" />
                      {state === "open" ? "窗口已打开" : state === "worker" ? "后台执行中" : "空闲待命"}
                    </span>
                  </div>
                  <div className="mx-bcard-main">
                    <b>{acc.display_name}</b>
                    <small>{acc.group_name ? `${acc.group_name} · ` : ""}{acc.has_proxy ? acc.proxy : "本机网络"}</small>
                  </div>
                  <div className="mx-bcard-actions">
                    <button type="button" className="ghost sm" onClick={() => actions?.openBrowser?.(acc.id)}>
                      <Icon name="external" />{acc.browser_open ? "前置 / 重开窗口" : "唤起独立浏览器"}
                    </button>
                    {acc.browser_open && (
                      <button
                        type="button"
                        className="ghost sm"
                        onClick={async () => {
                          await matrixApi("/api/matrix/accounts/batch", {
                            method: "POST",
                            headers: { "Content-Type": "application/json" },
                            body: JSON.stringify({ account_ids: [acc.id], action: "close_browsers" }),
                          });
                          matrixToast("已关闭该账号浏览器窗口", "ok");
                          load();
                        }}
                      >
                        关闭
                      </button>
                    )}
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      )}

      {/* Single Account Matrix Meta Editor */}
      {editingMeta && (
        <div className="mx-modal-backdrop" onClick={e => { if (e.target === e.currentTarget) setEditingMeta(null); }}>
          <form className="mx-modal-card" onSubmit={saveAccountMetaModal}>
            <div className="mx-modal-head">
              <h3>账号矩阵归类与角色设置</h3>
              <button type="button" className="ghost wb-icon-button" onClick={() => setEditingMeta(null)} aria-label="关闭"><Icon name="x" /></button>
            </div>
            <p className="mut" style={{ margin: "-4px 0 12px", fontSize: "12.5px" }}>
              平台：{PF_MAP[editingMeta.platform]?.name} · 原始昵称：{editingMeta.nickname || `#${editingMeta.id}`}
            </p>
            <div className="stack">
              <div>
                <label className="field">内部备注别名（优先展示，便于在几十个号里一眼识别）</label>
                <input
                  value={editingMeta.alias}
                  onChange={e => setEditingMeta({ ...editingMeta, alias: e.target.value })}
                  placeholder="例如：美妆主号-小雅-抖音01"
                  maxLength={60}
                  autoFocus
                />
              </div>
              <div className="form-grid">
                <div className="form-field">
                  <label>所属跨平台矩阵组</label>
                  <input
                    list="mx-group-datalist"
                    value={editingMeta.group_name}
                    onChange={e => setEditingMeta({ ...editingMeta, group_name: e.target.value })}
                    placeholder="选择已有或输入新组名"
                    maxLength={40}
                  />
                  <datalist id="mx-group-datalist">
                    {payload.groups.map(g => <option key={g.name} value={g.name} />)}
                  </datalist>
                </div>
                <div className="form-field">
                  <label>账号职能角色</label>
                  <SelectBox
                    full
                    value={editingMeta.account_role}
                    onChange={val => setEditingMeta({ ...editingMeta, account_role: val })}
                    ariaLabel="账号职能角色"
                    options={[
                      { value: "publish", label: "创作主号（核心发布/私信）", icon: "film" },
                      { value: "crawler", label: "采集小号（监控/批量抓取）", icon: "target" },
                      { value: "hybrid", label: "综合通用", icon: "refresh" },
                    ]}
                  />
                </div>
              </div>
              <div>
                <label className="field">多维标签（逗号分隔）</label>
                <input
                  value={editingMeta.tagsText}
                  onChange={e => setEditingMeta({ ...editingMeta, tagsText: e.target.value })}
                  placeholder="例如：主力变现, 日更, 广东IP"
                />
              </div>
              <label className="check-row">
                <input
                  type="checkbox"
                  checked={editingMeta.pinned}
                  onChange={e => setEditingMeta({ ...editingMeta, pinned: e.target.checked })}
                />
                <span>置顶此账号（在矩阵列表与数据大盘中优先排在最前）</span>
              </label>
            </div>
            <div className="mx-modal-foot">
              <button type="button" className="ghost" onClick={() => setEditingMeta(null)}>取消</button>
              <button type="submit"><Icon name="check" />保存设置</button>
            </div>
          </form>
        </div>
      )}

      {/* Create / Edit Matrix Group */}
      {groupModal && (
        <div className="mx-modal-backdrop" onClick={e => { if (e.target === e.currentTarget) setGroupModal(null); }}>
          <form className="mx-modal-card" onSubmit={saveGroupModal}>
            <div className="mx-modal-head">
              <h3>{groupModal.mode === "edit" ? `编辑矩阵组「${groupModal.originalName}」` : "新建跨平台矩阵分组"}</h3>
              <button type="button" className="ghost wb-icon-button" onClick={() => setGroupModal(null)} aria-label="关闭"><Icon name="x" /></button>
            </div>
            <div className="stack">
              <div>
                <label className="field">分组名称（如同一 IP 人设、业务线或采集号池）</label>
                <input
                  required
                  value={groupModal.name}
                  onChange={e => setGroupModal({ ...groupModal, name: e.target.value })}
                  placeholder="例如：户外穿搭-主IP组"
                  maxLength={40}
                  autoFocus
                />
              </div>
              <div className="form-grid">
                <div className="form-field">
                  <label>视觉色标</label>
                  <SelectBox
                    full
                    value={groupModal.color}
                    onChange={val => setGroupModal({ ...groupModal, color: val })}
                    ariaLabel="视觉色标"
                    options={Object.entries(GROUP_COLOR_META).map(([k, v]) => ({
                      value: k,
                      label: v.label,
                      dot: v.dot,
                    }))}
                  />
                </div>
                <div className="form-field">
                  <label>期望代理地区代码（可选）</label>
                  <input
                    value={groupModal.preferred_country}
                    onChange={e => setGroupModal({ ...groupModal, preferred_country: e.target.value })}
                    placeholder="如 CN / US / JP"
                    maxLength={8}
                  />
                </div>
              </div>
              <div>
                <label className="field">运营备注 / 人设说明（可选）</label>
                <input
                  value={groupModal.description}
                  onChange={e => setGroupModal({ ...groupModal, description: e.target.value })}
                  placeholder="例如：负责抖音+小红书+视频号同步日更"
                  maxLength={160}
                />
              </div>
            </div>
            <div className="mx-modal-foot">
              {groupModal.mode === "edit" && (
                <button type="button" className="ghost danger" style={{ marginRight: "auto" }} onClick={() => deleteGroup(groupModal.originalName)}>
                  解散此分组
                </button>
              )}
              <button type="button" className="ghost" onClick={() => setGroupModal(null)}>取消</button>
              <button type="submit"><Icon name="check" />保存分组</button>
            </div>
          </form>
        </div>
      )}
    </div>
  );
}
