import React, { useState, useEffect, useLayoutEffect, useRef } from "react";
import { createRoot } from "react-dom/client";
import { createPortal, flushSync } from "react-dom";
import { Sheet, ActionMenu, Tabs, Icon } from "./ui/primitives";
import { installMotion, reveal, selectionMarker } from "./ui/motion";

const $ = id => document.getElementById(id);
const bridge = window.CreatorHubBridge;
const events = new EventTarget();
const emit = (name, detail) => events.dispatchEvent(new CustomEvent(name, { detail }));
const context = () => bridge.getContext();
const parking = document.createElement("div");
parking.hidden = true;
parking.id = "wb-form-parking";
document.body.append(parking);

// Reunite split sections before organizing their existing nodes into local views.
const panels = new Map();
document.querySelectorAll("main > [data-panel]").forEach(panel => {
  const first = panels.get(panel.dataset.panel);
  if (first) { first.append(...panel.childNodes); panel.remove(); }
  else panels.set(panel.dataset.panel, panel);
});

const composerSpecs = {
  monitors: ["t-url", "新建作品监控", "粘贴目标，选择账号与采集策略。"],
  collections: ["col-keywords", "新建关键词采集", "设置关键词与采集上限，提交后在列表查看进度。"],
  comments: ["w-url", "新建评论监控", "选择作品或账号，持续收集评论。"],
  danmaku: ["d-w-url", "新建弹幕监控", "保留弹幕时间点，分别设置来源与采集范围。"],
  autocomment: ["ac-templates", "新建评论规则", "规则默认关闭，先试跑、检查文案，再手动启用。"],
  notifications: ["n-name", "添加通知渠道", "配置推送渠道，添加后可发送测试通知。"],
};
// Notifications previously mixed configuration and saved objects in one card.
const notificationCard = $("n-name").closest(".card");
const notificationForm = document.createElement("div");
notificationForm.className = "card";
while (notificationCard.firstElementChild && !notificationCard.firstElementChild.classList.contains("section-divider")) {
  notificationForm.append(notificationCard.firstElementChild);
}
notificationCard.prepend(notificationForm);
const composers = Object.fromEntries(Object.entries(composerSpecs).map(([key, [field, title, description]]) => {
  const node = $(field).closest(".card");
  node.dataset.composer = key;
  parking.append(node);
  return [key, { key, field, title, description, node }];
}));
// Ask for target/account/execution first; optional filing metadata comes later.
for (const key of ["monitors", "comments", "danmaku"]) {
  const node = composers[key].node, stack = node.querySelector(".stack");
  const optional = [node.querySelector(".meta-fields")].filter(Boolean);
  if (key === "monitors") optional.unshift($("t-dir").closest(".form-field") || $("t-dir").parentElement);
  if (!stack || !optional.length) continue;
  const details = document.createElement("details"); details.className = "collection-advanced wb-optional";
  const summary = document.createElement("summary"); summary.textContent = key === "monitors" ? "备注、分组与存储位置（可选）" : "备注、分组与标签（可选）";
  const body = document.createElement("div"); body.className = "collection-advanced-body stack";
  body.append(...optional); details.append(summary, body);
  const before = [...stack.children].find(el => el.matches("details,.form-actions"));
  stack.insertBefore(details, before || null);
}

// Configuration is progressively disclosed; the search/record tools stay first.
const groupSpecs = {
  accounts: [["accounts", "账号列表"], ["runtime", "浏览器内核"], ["proxies", "代理池"]],
  settings: [["appearance", "外观与体验"], ["downloads", "下载设置"], ["ai", "AI 文案"], ["engine", "采集与运行"]],
  monitors: [["targets", "监控目标"], ["records", "作品记录"]],
  comments: [["targets", "监控目标"], ["records", "评论记录"]],
  danmaku: [["targets", "监控目标"], ["records", "弹幕记录"]],
  autocomment: [["rules", "评论规则"], ["tasks", "任务与审核"]],
  publish: [["editor", "撰写内容"], ["records", "发布记录"], ["published", "已发布作品", "xhs"]],
  "risk-control": [["status", "账号状态"], ["rules", "风控规则"]],
};
const groups = Object.entries(groupSpecs).map(([key, spec]) => {
  const panel = panels.get(key);
  const nodes = [...panel.children].filter(node => node.classList.contains("card"));
  const target = document.createElement("div");
  target.className = "wb-section-tabs";
  panel.prepend(target);
  const items = spec.map(([value, label, platform], i) => ({ value, label, platform, node: nodes[i] })).filter(x => x.node);
  items.forEach(item => parking.append(item.node));
  // Risk counts belong with status, not above every configuration view.
  if (key === "risk-control") items[0].node.prepend(panel.querySelector(".risk-stats"));
  return { key, target, items };
});
const accountPanel = groups.find(group => group.key === "accounts").items[0].node;
const loginChoices = accountPanel.querySelector(":scope > .row");
accountPanel.querySelector(":scope > .card-head").hidden = true;
parking.append(loginChoices);

const actionTarget = document.createElement("div");
actionTarget.id = "workbench-actions";
$("page-context").append(actionTarget);
const connectionTarget = document.createElement("div");
connectionTarget.id = "workbench-connection";
$("page-context").after(connectionTarget);
const overviewTarget = document.createElement("div");
overviewTarget.id = "workbench-attention";
panels.get("overview").prepend(overviewTarget);
const previewTarget = document.createElement("aside");
previewTarget.className = "wb-publish-preview";
const publishNode = groups.find(g => g.key === "publish").items[0].node;
const editorLayout = document.createElement("div");
editorLayout.className = "wb-publish-layout";
editorLayout.append(publishNode.querySelector(".stack"), previewTarget);
publishNode.append(editorLayout);

// Existing account actions stay authoritative, including platform conditions.
const accountToolbar = document.createElement("div");
accountToolbar.className = "wb-list-tools";
accountToolbar.innerHTML = `<label class="wb-search-field"><svg aria-hidden="true"><use href="#i-logo"/></svg><input type="search" id="wb-account-search" placeholder="搜索账号、平台号或登录状态" aria-label="搜索账号"></label><span id="wb-account-count" class="mut" role="status"></span>`;
$("acc-table").closest(".table-wrap").before(accountToolbar);
const accountEmpty = document.createElement("div");
accountEmpty.className = "empty";
accountEmpty.hidden = true;
accountEmpty.innerHTML = `<b>没有匹配的账号</b><span>换个昵称、平台号或清除搜索条件。</span><button type="button" class="ghost sm">清除搜索</button>`;
$("acc-table").after(accountEmpty);
function filterAccounts() {
  const query = $("wb-account-search").value.trim().toLocaleLowerCase();
  const rows = [...document.querySelectorAll("#acc-table tr[data-account-id]")];
  rows.forEach(row => { row.hidden = !!query && !row.textContent.toLocaleLowerCase().includes(query); });
  const visible = rows.filter(row => !row.hidden).length;
  $("wb-account-count").textContent = query ? `${visible} / ${rows.length} 个账号` : `${rows.length} 个账号`;
  accountEmpty.hidden = !rows.length || visible > 0;
}
$("wb-account-search").addEventListener("input", filterAccounts);
accountEmpty.querySelector("button").onclick = () => { $("wb-account-search").value = ""; filterAccounts(); $("wb-account-search").focus(); };

// Local lookup explicitly searches loaded records, not an implied API-wide search.
for (const [id, key, label, selector] of [
  ["collection-job-table", "collection", "关键词、任务状态", ".collection-task"],
  ["pub-table", "publish", "发布标题、状态", "tr:not(:has([colspan]))"],
  ["ac-rule-table", "rule", "评论规则、目标", "tr:not(:has([colspan]))"],
  ["n-table", "channel", "通知渠道、类型", "tbody tr:not(:has([colspan]))"],
]) {
  const container = $(id);
  const target = container.closest(".table-wrap") || container;
  const toolbar = document.createElement("div"); toolbar.className = "wb-list-tools";
  toolbar.innerHTML = `<label class="wb-search-field"><svg aria-hidden="true"><use href="#i-logo"/></svg><input type="search" id="wb-${key}-search" aria-label="搜索已加载的${label}" placeholder="搜索${label}"></label><span class="mut" role="status"></span>`;
  const notice = document.createElement("div"); notice.className = "empty"; notice.hidden = true;
  notice.innerHTML = `<b>没有匹配的记录</b><span>试试其他关键词，或清除搜索条件。</span><button type="button" class="ghost sm">清除搜索</button>`;
  target.before(toolbar); target.after(notice);
  const input = toolbar.querySelector("input"), count = toolbar.querySelector("[role=status]");
  const filter = () => {
    const query = input.value.trim().toLocaleLowerCase();
    const items = [...container.querySelectorAll(selector)].filter(el => !el.querySelector(".sk"));
    items.forEach(item => { item.hidden = !!query && !item.textContent.toLocaleLowerCase().includes(query); });
    const visible = items.filter(item => !item.hidden).length;
    count.textContent = query ? `${visible} / ${items.length} 条已加载记录` : "";
    notice.hidden = !items.length || visible > 0;
  };
  input.addEventListener("input", filter);
  notice.querySelector("button").onclick = () => { input.value = ""; filter(); input.focus(); };
  new MutationObserver(filter).observe(container, { childList: true, subtree: true }); filter();
}

function NodeHost({ node, className = "" }) {
  const ref = useRef(null);
  useLayoutEffect(() => {
    ref.current.append(node);
    return () => parking.append(node);
  }, [node]);
  return <div className={className} ref={ref} />;
}
const tabSetters = new Map();
function SectionTabs({ group, platform }) {
  const [value, setValue] = useState(group.items[0].value);
  const root = useRef(null), list = useRef(null), previous = useRef(value);
  const visible = group.items.filter(item => !item.platform || item.platform === platform);
  useLayoutEffect(() => {
    tabSetters.set(group.key, setValue);
    return () => tabSetters.delete(group.key);
  }, [group]);
  useEffect(() => { if (!visible.some(item => item.value === value)) setValue(visible[0].value); }, [platform]);
  useLayoutEffect(() => selectionMarker(list.current, '[data-state="active"]', "line"), []);
  useLayoutEffect(() => {
    if (previous.current !== value) {
      const direction = group.items.findIndex(item => item.value === value) > group.items.findIndex(item => item.value === previous.current) ? 1 : -1;
      reveal(root.current?.querySelector('.wb-tabpanel:not([hidden]) > div'), "x", direction);
      previous.current = value;
    }
  }, [value]);
  return <Tabs.Root ref={root} value={value} onValueChange={setValue} className="wb-tabs">
    <Tabs.List ref={list} className="wb-tablist" aria-label={group.items.map(item => item.label).join("、")}>
      {visible.map(item => <Tabs.Trigger key={item.value} value={item.value} className="wb-tab">{item.label}</Tabs.Trigger>)}
    </Tabs.List>
    {group.items.map(item => <Tabs.Content key={item.value} value={item.value} forceMount hidden={item.value !== value} className="wb-tabpanel">
      <NodeHost node={item.node} />
    </Tabs.Content>)}
  </Tabs.Root>;
}

function Attention({ summary, platform }) {
  const openQueue = state => bridge.openQueue(state);
  const amount = key => summary ? Number(summary[key] || 0).toLocaleString() : "—";
  return <section className="wb-attention" aria-labelledby="wb-attention-title">
    <div className="wb-attention-intro"><span className="wb-eyebrow">现在，先处理这些</span><h2 id="wb-attention-title">工作在这里继续。</h2>
      <p>{summary ? Number(summary.failed || 0) > 0 ? "有任务需要检查，打开明细查看原因与下一步。" : "查看运行中的任务，或开始一次新的内容采集。" : "正在读取当前平台任务状态…"}</p>
      <button className="ghost" onClick={() => bridge.navigate("queue", true)}>打开任务队列<Icon name="next" /></button>
    </div>
    <div className="wb-attention-list" aria-label="当前平台任务状态">
      {[["failed", "需要处理", "检查失败原因，不自动重试", "alert", "danger"], ["blocked", "等待恢复", "查看受阻原因与恢复条件", "shield", "warn"], ["active", "正在推进", "运行中与等待执行的任务", "clock", "info"]].map(([key, label, hint, icon, tone]) =>
        <button key={key} type="button" className="wb-attention-row" data-tone={tone} onClick={() => openQueue(key)}>
          <span className="wb-status-icon"><Icon name={icon} /></span><span className="wb-attention-copy"><b>{label}</b><small>{hint}</small></span>
          <strong data-overview-state={key}>{amount(key)}</strong><Icon name="next" />
        </button>)}
    </div>
  </section>;
}

function PublishPreview({ revision, contextVersion }) {
  const [index, setIndex] = useState(0);
  const [media, setMedia] = useState([]);
  const [copy, setCopy] = useState({});
  const preview = useRef(null);
  useLayoutEffect(() => { reveal(preview.current?.firstElementChild); }, [index, media]);
  useEffect(() => {
    const sync = () => setCopy({ title: $("pub-title").value, desc: $("pub-desc").value, topics: $("pub-topics").value,
      account: $("pub-acc").selectedOptions[0]?.textContent, when: $("pub-when").value });
    document.addEventListener("input", sync); document.addEventListener("change", sync); sync();
    return () => { document.removeEventListener("input", sync); document.removeEventListener("change", sync); };
  }, [contextVersion]);
  useEffect(() => {
    const files = [...$("pub-files").files];
    const items = files.map(file => ({ src: URL.createObjectURL(file), type: file.type, name: file.name }));
    setMedia(items); setIndex(0);
    return () => items.forEach(item => URL.revokeObjectURL(item.src));
  }, [revision]);
  const item = media[index];
  return <><div className="wb-preview-heading"><b>内容预览</b><span>仅本地预览</span></div>
    <div className="wb-preview-media" ref={preview}>{item ? item.type.startsWith("video/")
      ? <video key={item.src} src={item.src} controls preload="metadata" aria-label={item.name} />
      : <img key={item.src} src={item.src} alt={item.name} />
      : <div className="wb-preview-placeholder"><Icon name="image" /><span>选择素材后在这里预览</span></div>}</div>
    {media.length > 1 && <div className="wb-preview-pagination"><button className="ghost sm" disabled={index === 0} onClick={() => setIndex(index - 1)} aria-label="上一张素材"><Icon name="prev" /></button>
      <span>{index + 1} / {media.length}</span><button className="ghost sm" disabled={index >= media.length - 1} onClick={() => setIndex(index + 1)} aria-label="下一张素材"><Icon name="next" /></button></div>}
    <div className="wb-preview-copy"><b>{copy.title || "作品标题"}</b><p>{copy.desc || "正文会随输入同步显示。"}</p>
      {copy.topics && <span className="wb-topics">{copy.topics.split(/[,，]/).filter(Boolean).map(t => `#${t.trim()}`).join(" ")}</span>}</div>
    <div className="wb-preview-foot"><span>{copy.account}</span><span>{copy.when ? copy.when.replace("T", " ") : "提交后尽快发布"}</span></div>
    <p className="field-help">素材和文案预览；实际排版及发布状态以平台为准。</p>
  </>;
}

const readFailures = new Map();
const latestReads = new Map();
let readSerial = 0;
const prefixes = { overview: ["/api/overview", "/api/stats", "/api/task-queue"], accounts: ["/api/accounts", "/api/browser-runtimes", "/api/proxies"],
  monitors: ["/api/monitors", "/api/contents"], comments: ["/api/comment-watches", "/api/comments"], danmaku: ["/api/danmaku"],
  collections: ["/api/collections"], queue: ["/api/task-queue"], publish: ["/api/publish"], autocomment: ["/api/comment-rules", "/api/comment-tasks"],
  settings: ["/api/settings"], notifications: ["/api/notifications"], "risk-control": ["/api/risk"], hub: ["/api/accounts", "/api/hub"] };
let menuOpen = false, sheetOpen = false;
let lastContext = context();
const drafts = new Map();
function snapshotDrafts() {
  return Object.fromEntries(Object.entries(composers).filter(([key]) => key !== "notifications").map(([key, { node }]) => [key,
    [...node.querySelectorAll("input[id],textarea[id],select[id]")].filter(el => !/(?:acc|account)$/.test(el.id)).map(el => [el.id, el.value, el.checked]) ]));
}
const defaultDrafts = snapshotDrafts();
document.addEventListener("input", event => {
  if (event.target.matches("input[id],textarea[id],select[id]")) event.target.dataset.wbEdited = "true";
});
document.addEventListener("change", event => {
  if (event.target.matches("input[id],textarea[id],select[id]")) event.target.dataset.wbEdited = "true";
});
const scrollPositions = new Map();
let savedReturnTarget = null;
window.CreatorHubWorkbench = {
  isDirty(id) { return $(id)?.dataset.wbEdited === "true"; },
  navigate() {
    const next = context();
    if (lastContext.platform !== next.platform) { readFailures.clear(); $("wb-account-search").value = ""; }
    lastContext = next; emit("context", next);
  },
  beforeNavigate() { scrollPositions.set(`${lastContext.platform}/${lastContext.tab}`, window.scrollY); },
  restoreScroll() { requestAnimationFrame(() => window.scrollTo({ top: scrollPositions.get(`${context().platform}/${context().tab}`) || 0, behavior: "instant" })); },
  platformChanging(platform) {
    if (platform === lastContext.platform) return;
    drafts.set(lastContext.platform, snapshotDrafts());
    const data = drafts.get(platform) || defaultDrafts;
    for (const fields of Object.values(data)) for (const [id, value, checked] of fields) {
      const el = $(id); el.value = value; if (el.type === "checkbox") el.checked = checked;
      el._csSync?.();
    }
  },
  requestStarted({ path, method }) {
    if (method !== "GET") return;
    const key = new URL(path, location.origin).pathname;
    const request = ++readSerial; latestReads.set(key, request); return request;
  },
  requestResult({ path, request, ok, method }) {
    if (method !== "GET") return;
    const url = new URL(path, location.origin);
    if (latestReads.has(url.pathname) && latestReads.get(url.pathname) !== request) return;
    const platform = url.searchParams.get("platform");
    if (platform && platform !== context().platform) return;
    if (ok) readFailures.delete(url.pathname); else readFailures.set(url.pathname, true);
    emit("connection");
  },
  queueSummary(summary) { window.CreatorHubQueueSummary = { platform: context().platform, summary }; emit("summary", summary); },
  accountsUpdated() { filterAccounts(); emit("accounts"); },
  isInteracting() { return menuOpen || sheetOpen || !!document.querySelector('.wb-sheet[data-state="closed"]'); },
  feedback(detail) {
    if (detail.type !== "err" || !document.querySelector('.wb-sheet[data-state="open"] [data-composer]')) return false;
    emit("feedback", detail); return true;
  },
  completed(key) { emit("completed", key); },
  previewUpdated() { emit("preview"); },
  showSection(key, value) { flushSync(() => tabSetters.get(key)?.(value)); },
  openComposer(key) { emit("composer", key); },
  collectionDetail(open) {
    const list = $("collection-job-table").closest(".card");
    if (open) savedReturnTarget = document.activeElement;
    list.hidden = open;
    if (!open) {
      const action = savedReturnTarget?.getAttribute("onclick");
      const target = savedReturnTarget?.isConnected ? savedReturnTarget : (action && list.querySelector(`[onclick="${CSS.escape(action)}"]`)) || list.querySelector("button");
      requestAnimationFrame(() => target?.focus({ preventScroll: true }));
      reveal(list, "x", -1);
    } else requestAnimationFrame(() => { const title = $("collection-results-title"); title.tabIndex = -1; title.focus({ preventScroll: true }); reveal(title.closest(".card"), "x"); });
  },
};

function App() {
  const [ctx, setContext] = useState(context);
  const [composer, setComposer] = useState(null);
  const [account, setAccount] = useState(null);
  const [accountsVersion, setAccountsVersion] = useState(0);
  const [previewVersion, setPreviewVersion] = useState(0);
  const [summary, setSummary] = useState(window.CreatorHubQueueSummary?.platform === ctx.platform ? window.CreatorHubQueueSummary.summary : null);
  const [online, setOnline] = useState(navigator.onLine);
  const [, setConnectionVersion] = useState(0);
  const [retrying, setRetrying] = useState(false);
  const [busy, setBusy] = useState(false);
  const [feedback, setFeedback] = useState(null);
  const [refreshFeedback, setRefreshFeedback] = useState("");
  const refreshing = useRef(false), feedbackTimer = useRef(null), inspector = useRef(null), previousAccount = useRef(null);
  const trigger = useRef(null);
  const oldPlatform = useRef(ctx.platform);
  const openComposer = key => { if (!composers[key]) return; trigger.current = document.activeElement; setFeedback(null); setComposer(key); setAccount(null); };
  useEffect(() => {
    const subscriptions = {
      context: e => { setContext(e.detail); setComposer(null); setAccount(null); },
      composer: e => openComposer(e.detail),
      completed: e => setComposer(current => current === e.detail ? null : current),
      accounts: () => setAccountsVersion(n => n + 1),
      summary: e => setSummary(e.detail),
      preview: () => setPreviewVersion(n => n + 1),
      connection: () => setConnectionVersion(n => n + 1),
      feedback: e => setFeedback(e.detail),
    };
    Object.entries(subscriptions).forEach(([name, fn]) => events.addEventListener(name, fn));
    const connection = () => { setOnline(navigator.onLine); if (navigator.onLine) bridge.retry(); };
    window.addEventListener("offline", connection); window.addEventListener("online", connection);
    const click = e => {
      const button = e.target.closest("[data-account-detail],[data-open-composer]");
      if (button?.dataset.accountDetail) { trigger.current = button; setAccount(button.dataset.accountDetail); setComposer(null); }
      if (button?.dataset.openComposer) openComposer(button.dataset.openComposer);
    };
    document.addEventListener("click", click);
    const observer = new MutationObserver(() => setBusy(!!document.querySelector('.wb-sheet [aria-busy="true"]')));
    observer.observe(document.body, { attributes: true, subtree: true, attributeFilter: ["aria-busy"] });
    return () => {
      Object.entries(subscriptions).forEach(([name, fn]) => events.removeEventListener(name, fn));
      window.removeEventListener("offline", connection); window.removeEventListener("online", connection);
      document.removeEventListener("click", click); observer.disconnect();
    };
  }, []);
  useLayoutEffect(() => { sheetOpen = !!(composer || account); return () => { sheetOpen = false; }; }, [composer, account]);
  useEffect(() => { if (oldPlatform.current !== ctx.platform) { setSummary(null); oldPlatform.current = ctx.platform; } }, [ctx.platform]);
  useLayoutEffect(() => {
    if (account && previousAccount.current && account !== previousAccount.current) reveal(inspector.current, "x", Number(account) > Number(previousAccount.current) ? 1 : -1);
    previousAccount.current = account;
  }, [account]);
  useEffect(() => () => clearTimeout(feedbackTimer.current), []);
  useEffect(() => { clearTimeout(feedbackTimer.current); setRefreshFeedback(""); }, [ctx.tab, ctx.platform]);
  useEffect(() => { if (busy) setFeedback(null); }, [busy]);
  const failed = [...readFailures.keys()].some(path => (prefixes[ctx.tab] || []).some(prefix => path.startsWith(prefix)));
  const showConnection = !online || failed;
  useEffect(() => {
    const engine = $("engine-status");
    engine.querySelector(".engine-label").textContent = !online ? "当前离线" : failed ? "连接待恢复" : "服务已连接";
    engine.dataset.connection = !online || failed ? "error" : "ready";
    engine.title = !online ? "网络离线，已保留本页输入" : failed ? "读取失败，显示已加载的结果" : "页面与本地服务连接正常";
  }, [online, failed]);
  const retry = async () => {
    if (refreshing.current) return;
    refreshing.current = true; setRetrying(true); setRefreshFeedback(""); clearTimeout(feedbackTimer.current);
    const requested = context();
    try {
      await bridge.retry();
      const current = context();
      if (requested.tab === current.tab && requested.platform === current.platform) {
        const failedNow = !navigator.onLine || [...readFailures.keys()].some(path => (prefixes[current.tab] || []).some(prefix => path.startsWith(prefix)));
        setRefreshFeedback(failedNow ? "刷新未完成" : "已刷新");
        feedbackTimer.current = setTimeout(() => setRefreshFeedback(""), 2600);
      }
    } finally { refreshing.current = false; setRetrying(false); }
  };
  const rows = [...document.querySelectorAll("#acc-table tr[data-account-id]")];
  const accountRows = rows.filter(row => !row.hidden);
  const index = accountRows.findIndex(row => row.dataset.accountId === account);
  const selected = rows.find(row => row.dataset.accountId === account);
  const currentComposer = composers[composer];
  const returnFocus = () => requestAnimationFrame(() => {
    if (document.querySelector('.wb-sheet[data-state="open"],.pv-overlay:not([data-ui-closing])[style*="display: flex"]')) return;
    if (trigger.current?.isConnected && trigger.current.getClientRects().length) trigger.current.focus({ preventScroll: true });
    else if (trigger.current?.dataset.accountDetail) document.querySelector(`[data-account-detail="${CSS.escape(trigger.current.dataset.accountDetail)}"]`)?.focus({ preventScroll: true });
    else document.querySelector('#workbench-actions button')?.focus({ preventScroll: true });
  });
  return <>
    {groups.map(group => createPortal(<SectionTabs group={group} platform={ctx.platform} />, group.target, group.key))}
    {createPortal(<div className="wb-page-actions">
      <span className="wb-refresh-control">
        <button type="button" className="ghost wb-icon-button" aria-label="刷新当前页面" title="刷新当前页面" aria-busy={retrying} data-feedback={refreshFeedback === "已刷新" ? "success" : undefined} onClick={retry} disabled={retrying || !online}><Icon name={refreshFeedback === "已刷新" ? "check" : "refresh"} /></button>
        <span className="wb-refresh-feedback" role="status">{retrying ? "正在刷新…" : refreshFeedback}</span>
      </span>
      {ctx.tab === "accounts" && <ActionMenu label="添加平台账号" triggerLabel="添加账号" heading="选择登录方式" icon="plus"
        buttons={[...loginChoices.querySelectorAll("button")].filter(button => !button.classList.contains("hidden"))} onOpenChange={open => { menuOpen = open; }} />}
      {composers[ctx.tab] && <button type="button" id="wb-create" onClick={() => openComposer(ctx.tab)}><Icon name="plus" />{composerSpecs[ctx.tab][1]}</button>}
      {ctx.tab === "overview" && <button type="button" onClick={() => bridge.navigate("publish", true)}><Icon name="plus" />创作内容</button>}
    </div>, actionTarget)}
    {createPortal(showConnection && <div className="wb-connection" role="status"><Icon name={!online ? "offline" : "alert"} /><span><b>{!online ? "当前离线" : "部分数据刷新失败"}</b> · 已保留输入与已加载的记录；提交前请确认连接恢复。</span>
      <button className="ghost sm" onClick={retry} disabled={!online || retrying}>{retrying ? "正在重试…" : "重新加载"}</button></div>, connectionTarget)}
    {createPortal(<Attention summary={summary} platform={ctx.platform} />, overviewTarget)}
    {createPortal(<PublishPreview revision={previewVersion} contextVersion={`${ctx.platform}/${accountsVersion}`} />, previewTarget)}
    {rows.map(row => { const slot = row.querySelector("[data-account-menu]");
      return slot && createPortal(<ActionMenu label={`${row.querySelector('[data-account-detail]')?.textContent.trim()}的更多操作`}
        buttons={[...row.querySelectorAll("[data-account-actions] button")]} onOpenChange={open => { menuOpen = open; }} />, slot, row.dataset.accountId);
    })}
    <Sheet open={!!currentComposer} title={currentComposer?.title || "新建任务"}
      description={currentComposer?.description || ""} onOpenChange={() => setComposer(null)} onReturnFocus={returnFocus} busy={busy}>
      {currentComposer && <><div className="wb-sheet-scroll"><NodeHost node={currentComposer.node} /></div>
        <div className="wb-sheet-footer wb-composer-footer"><span className="wb-sheet-status" data-tone={feedback?.type} role={feedback ? "alert" : undefined} tabIndex={feedback ? 0 : undefined}>{feedback?.message || "关闭后保留本次未提交内容"}</span><button className="ghost" disabled={busy} onClick={() => setComposer(null)}>返回列表</button></div></>}
    </Sheet>
    <Sheet open={!!account} title={selected?.querySelector('[data-account-detail]')?.textContent || "账号详情"}
      description="登录、网络与浏览器环境" onOpenChange={() => setAccount(null)} onReturnFocus={returnFocus}>
      <div className="wb-sheet-scroll wb-account-inspector" ref={inspector}>
        {selected ? <div dangerouslySetInnerHTML={{ __html: selected.querySelector("[data-account-info]")?.innerHTML || "" }} /> : <p>这个账号已被移除，请返回列表。</p>}
        {selected && <button className="ghost" onClick={() => { setAccount(null); bridge.openAccount(Number(account)); }}>查看作品与私信<Icon name="next" /></button>}
      </div>
      <div className="wb-sheet-footer"><button className="ghost" onClick={() => setAccount(null)}>返回账号列表</button>
        <div className="wb-detail-nav"><button className="ghost wb-icon-button" disabled={index <= 0} onClick={() => setAccount(accountRows[index - 1].dataset.accountId)} aria-label="上一个账号"><Icon name="prev" /></button>
          <span>{index + 1} / {accountRows.length}</span><button className="ghost wb-icon-button" disabled={index < 0 || index >= accountRows.length - 1} onClick={() => setAccount(accountRows[index + 1].dataset.accountId)} aria-label="下一个账号"><Icon name="next" /></button></div>
      </div>
    </Sheet>
  </>;
}

// Mobile filters collapse independently and always report non-default criteria.
document.querySelectorAll(".manage-toolbar").forEach((toolbar, i) => {
  const controls = [...toolbar.children].filter(el => !el.matches('input[type="search"],.filter-count'));
  if (!controls.length) return;
  const details = document.createElement("details"); details.className = "wb-filter-details";
  const summary = document.createElement("summary"); summary.innerHTML = `<svg aria-hidden="true"><use href="#i-filter"/></svg><span>筛选与导出</span><span class="wb-filter-total"></span>`;
  const body = document.createElement("div"); body.className = "wb-filter-body"; body.id = `wb-filters-${i}`;
  const clear = document.createElement("button"); clear.type = "button"; clear.className = "ghost sm"; clear.textContent = "重置筛选";
  const defaults = [...toolbar.querySelectorAll("select,input:not([type=search])")].map(el => [el, el.value]);
  const update = () => { const count = defaults.filter(([el, value]) => el.value !== value).length; summary.querySelector(".wb-filter-total").textContent = count ? `${count} 项` : ""; };
  clear.onclick = () => { defaults.forEach(([el, value]) => { if (el.value === value) return; el.value = value; el._csSync?.(); el.dispatchEvent(new Event("change", { bubbles: true })); }); update(); };
  body.append(...controls, clear); details.append(summary, body); toolbar.append(details);
  toolbar.addEventListener("change", update);
});

// Search affordances use the same SVG as navigation, not a hard-coded bitmap.
document.querySelectorAll('.manage-toolbar > input[type="search"],#command-search').forEach(input => {
  const label = document.createElement("label"); label.className = "wb-search-field";
  label.innerHTML = '<svg aria-hidden="true"><use href="#i-logo"/></svg>';
  input.before(label); label.append(input);
});

// Hide zero-only navigation noise without hiding the underlying accessible label.
const badgeObserver = new MutationObserver(records => records.forEach(record => {
  const badge = record.target.nodeType === 1 ? record.target.closest(".nav-badge") : record.target.parentElement?.closest(".nav-badge");
  if (badge) badge.classList.toggle("wb-zero", badge.textContent.trim() === "0");
}));
document.querySelectorAll(".nav-badge").forEach(badge => { badge.classList.toggle("wb-zero", badge.textContent.trim() === "0"); badgeObserver.observe(badge, { childList: true, characterData: true, subtree: true }); });
// Stacked record rows on narrow screens keep column context and every action.
for (const id of ["mon-table", "watch-table", "danmaku-watch-table", "queue-table", "pub-table", "ac-rule-table", "ac-task-table", "n-table", "risk-account-table"]) {
  const node = $(id), table = node.tagName === "TABLE" ? node : node.closest("table");
  table.dataset.objectTable = "";
  const labelRows = () => {
    const headers = [...table.querySelectorAll("thead th")].map(el => el.textContent.trim());
    table.querySelectorAll("tbody tr").forEach(row => [...row.children].forEach((cell, index) => {
      if (headers[index] && !cell.hasAttribute("colspan")) cell.dataset.label = headers[index];
    }));
  };
  new MutationObserver(labelRows).observe(table.querySelector("tbody"), { childList: true, subtree: true }); labelRows();
}
const root = document.createElement("div"); root.id = "workbench-root"; document.body.append(root);
flushSync(() => createRoot(root).render(<App />));
filterAccounts();
document.documentElement.classList.add("workbench-ready");
installMotion();
