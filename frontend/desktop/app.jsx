import React, { useState, useEffect, useRef } from "react";
import { createRoot } from "react-dom/client";
import * as Dialog from "@radix-ui/react-dialog";
import * as Menu from "@radix-ui/react-dropdown-menu";
import * as Tabs from "@radix-ui/react-tabs";
import platforms from "../../guide/platforms.json";
import "./style.css";
import Updates from "./updates.jsx";

const token = document.querySelector('meta[name="desktop-token"]')?.content;
const pages = { workspace: ["工作空间", "layout-dashboard"], activity: ["运行记录", "activity"], guides: ["上手指南", "book-open"], settings: ["偏好设置", "sliders-horizontal"] };
const phases = {
  stopped: ["已停止", "quiet", "准备好时，\n随时开始。", "启动本地服务"],
  starting: ["启动中", "warn", "正在准备，\n马上就好。", "正在启动…"],
  ready: ["运行中", "success", "工作台就绪，\n继续你的创作。", "打开工作台"],
  unreachable: ["连接中断", "warn", "稍等一下，\n正在重新连接。", "正在检查连接…"],
  stopping: ["停止中", "warn", "正在收尾，\n为下次留好位置。", "正在停止…"],
  error: ["需要处理", "danger", "遇到一点问题，\n一起处理一下。", "重新启动"],
};
const Icon = ({ name, size = 18, ...props }) => <svg width={size} height={size} aria-hidden="true" {...props}><use href={`icons.svg#${name}`} /></svg>;
function Button({ children, icon, variant = "secondary", className = "", ...props }) {
  return <button className={`button ${variant} ${className}`} {...props}>{icon && <Icon name={icon} />}{children}</button>;
}
function Pill({ phase }) { const [label, tone] = phases[phase] || phases.stopped; return <span className={`pill ${tone}`}><i />{label}</span>; }
function Modal({ open, onOpenChange, title, description, children, sheet = false }) {
  const previousFocus = useRef(null);
  return <Dialog.Root open={open} onOpenChange={onOpenChange}><Dialog.Portal><Dialog.Overlay className="overlay" /><Dialog.Content className={sheet ? "sheet" : "dialog"} onOpenAutoFocus={() => { previousFocus.current = document.activeElement; }} onCloseAutoFocus={event => { event.preventDefault(); if (previousFocus.current?.isConnected) previousFocus.current.focus(); }}>
    <div className="dialog-heading"><div><Dialog.Title>{title}</Dialog.Title><Dialog.Description>{description}</Dialog.Description></div><Dialog.Close asChild><Button variant="ghost" className="icon-button" aria-label="关闭"><Icon name="x" /></Button></Dialog.Close></div>{children}
  </Dialog.Content></Dialog.Portal></Dialog.Root>;
}
const timeLabel = (seconds) => new Date(seconds * 1000).toLocaleTimeString("zh-CN", { hour12: false });
const uptime = (seconds) => seconds >= 3600 ? `${Math.floor(seconds / 3600)} 小时 ${Math.floor(seconds % 3600 / 60)} 分钟` : seconds >= 60 ? `${Math.floor(seconds / 60)} 分钟` : `${seconds} 秒`;
function readRoute() { const [page, slug] = location.hash.replace(/^#\/?/, "").split("/"); return { page: pages[page] ? page : "workspace", slug: platforms[slug] ? slug : "" }; }

function App() {
  const [state, setState] = useState(null), [connected, setConnected] = useState(true);
  const [route, setRoute] = useState(readRoute), [drawer, setDrawer] = useState(null);
  const [pending, setPending] = useState(""), [notice, setNotice] = useState(null), [confirm, setConfirm] = useState(null);
  const [command, setCommand] = useState(false), [search, setSearch] = useState(""), [active, setActive] = useState(0);
  const [filter, setFilter] = useState("all"), [query, setQuery] = useState("");
  const [network, setNetwork] = useState(navigator.onLine);
  const titleRef = useRef(null), busy = useRef(false), mounted = useRef(true), noticeTimer = useRef(null), refreshSequence = useRef(0), latest = useRef(null), refreshing = useRef(false);
  const initialRoute = useRef(true), commandInput = useRef(null);
  function toast(message, tone = "success") { clearTimeout(noticeTimer.current); setNotice({ message, tone }); noticeTimer.current = setTimeout(() => setNotice(null), 5000); }
  async function refresh() {
    if (refreshing.current) return;
    refreshing.current = true;
    const request = ++refreshSequence.current;
    try {
      const response = await fetch("api/state", { headers: { "X-Desktop-Token": token }, signal: AbortSignal.timeout(4000) });
      if (!response.ok) throw new Error();
      const next = await response.json();
      if (mounted.current && request === refreshSequence.current) { latest.current = next; setState(next); setConnected(true); }
    } catch { if (mounted.current && request === refreshSequence.current) setConnected(false); }
    finally { refreshing.current = false; }
  }
  useEffect(() => {
    mounted.current = true; refresh();
    const timer = setInterval(refresh, 1200);
    const hash = () => setRoute(readRoute());
    const net = () => setNetwork(navigator.onLine);
    const key = e => { if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") { e.preventDefault(); setCommand(v => !v); } };
    window.addEventListener("hashchange", hash); window.addEventListener("keydown", key);
    window.addEventListener("online", net); window.addEventListener("offline", net);
    return () => { mounted.current = false; clearInterval(timer); clearTimeout(noticeTimer.current); window.removeEventListener("hashchange", hash); window.removeEventListener("keydown", key); window.removeEventListener("online", net); window.removeEventListener("offline", net); };
  }, []);
  useEffect(() => { if (initialRoute.current) { initialRoute.current = false; return; } titleRef.current?.focus({ preventScroll: true }); }, [route.page, route.slug]);
  useEffect(() => { if (state?.close_requested) setConfirm("exit"); }, [state?.close_requested]);
  const preference = state?.preferences.theme || "system";
  useEffect(() => {
    const media = matchMedia("(prefers-color-scheme: dark)");
    const apply = () => { document.documentElement.dataset.theme = preference === "system" ? media.matches ? "dark" : "light" : preference; };
    apply(); media.addEventListener("change", apply); return () => media.removeEventListener("change", apply);
  }, [preference]);
  async function act(name, data = {}, successMessage) {
    if (busy.current || !connected) return false;
    busy.current = true; setPending(name);
    try {
      const response = await fetch("api/action", { method: "POST", headers: { "Content-Type": "application/json", "X-Desktop-Token": token }, body: JSON.stringify({ name, data }) });
      const result = await response.json();
      if (!response.ok || !result.ok) throw new Error(result.error || "操作未完成，请稍后重试。");
      if (result.download) {
        const url = URL.createObjectURL(new Blob([JSON.stringify(result.download, null, 2)], { type: "application/json" }));
        const link = document.createElement("a"); link.href = url; link.download = "creatorhub-diagnostics.json"; link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
      }
      if (successMessage || result.message) toast(result.message || successMessage);
      await refresh(); return true;
    } catch (error) { toast(error.message === "Failed to fetch" ? "连接中断，请检查启动中心是否仍在运行。" : error.message, "danger"); return false; }
    finally { busy.current = false; setPending(""); }
  }
  async function copy(text) {
    try { await navigator.clipboard.writeText(text); toast("已复制"); }
    catch { toast("复制未完成，请选中文字手动复制。", "danger"); }
  }
  function navigate(page, slug = "") { location.hash = `/${page}${slug ? "/" + slug : ""}`; setDrawer(null); setCommand(false); }
  const phase = state?.phase || "stopped", info = phases[phase] || phases.stopped;
  const disabled = !!pending || !connected || !state;
  const startDisabled = disabled || ["starting", "stopping", "unreachable"].includes(phase);
  const guides = Object.entries(platforms);
  const commands = [
    ...Object.entries(pages).map(([id, [label, icon]]) => ({ id, label, icon, group: "页面", run: () => navigate(id) })),
    ...guides.map(([id, p]) => ({ id: "guide-" + id, label: p.name + "使用指南", icon: "book-open", group: "平台", run: () => navigate("guides", id) })),
    { id: "data", label: "数据目录与备份", icon: "folder", group: "工具", run: () => { setDrawer("data"); setCommand(false); } },
    { id: "diagnostics", label: "诊断与问题排查", icon: "stethoscope", group: "工具", run: () => { setDrawer("diagnostics"); setCommand(false); } },
  ].filter(item => item.label.toLowerCase().includes(search.trim().toLowerCase()));
  useEffect(() => { setActive(0); }, [search]);
  useEffect(() => { if (command) { setSearch(""); setActive(0); } }, [command]);
  const events = state?.events || [];
  const visibleEvents = events.filter(e => (filter === "all" || ["warning", "error"].includes(e.level)) && `${e.title} ${e.detail}`.includes(query.trim()));
  const guide = platforms[route.slug];
  const mainHeading = route.page === "workspace" ? "工作空间" : route.page === "guides" && guide ? guide.name + "上手指南" : pages[route.page][0];

  return <div className="shell">
    <a href="#content" className="skip" onClick={e => { e.preventDefault(); titleRef.current?.focus(); }}>跳到内容</a>
    <aside className="sidebar">
      <a className="brand" href="#/workspace" aria-label="CreatorHub 首页"><span className="brand-mark"><Icon name="brand" size={25} /></span><span>CreatorHub<small>DESKTOP WORKSPACE</small></span></a>
      <span className="nav-caption">你的工作台</span>
      <nav aria-label="主导航">{Object.entries(pages).filter(([id]) => id !== "settings").map(([id, [name, icon]]) => <a key={id} aria-label={name} href={`#/${id}`} className={route.page === id ? "active" : ""} aria-current={route.page === id ? "page" : undefined}><Icon name={icon} /><span>{name}</span>{route.page === id && <span className="nav-dot" />}</a>)}</nav>
      <div className="sidebar-bottom"><div className="local-status"><span className={`status-dot ${phase === "ready" && connected ? "success" : "quiet"}`} /><span>仅在本机运行<small>{state ? `桌面版 ${state.version}` : "正在连接启动中心"}</small></span><Icon name="shield-check" size={16} /></div><nav><a href="#/settings" aria-label="偏好设置" aria-current={route.page === "settings" ? "page" : undefined} className={route.page === "settings" ? "active" : ""}><Icon name="sliders-horizontal" /><span>偏好设置</span></a></nav></div>
    </aside>
    <div className="workspace">
      <header className="topbar"><div className="breadcrumb"><Icon name="monitor" size={16} /><span>本机</span><span className="slash">/</span><b>{pages[route.page][0]}</b></div><div className="top-actions"><button className="search-trigger" onClick={() => setCommand(true)}><Icon name="search" size={16} /><span>搜索入口与指南</span><kbd>Ctrl K</kbd></button><Menu.Root><Menu.Trigger asChild><Button variant="ghost" className="icon-button" aria-label="切换外观"><Icon name={preference === "dark" ? "moon" : "sun"} /></Button></Menu.Trigger><Menu.Portal><Menu.Content className="menu" align="end" sideOffset={8}><Menu.Label>外观</Menu.Label><Menu.RadioGroup value={preference} onValueChange={theme => act("preferences", { theme })}>{[["light", "浅色", "sun"], ["dark", "深色", "moon"], ["system", "跟随系统", "monitor"]].map(([value, label, icon]) => <Menu.RadioItem key={value} value={value} disabled={disabled}><Icon name={icon} />{label}<Menu.ItemIndicator><Icon name="check" size={16} /></Menu.ItemIndicator></Menu.RadioItem>)}</Menu.RadioGroup></Menu.Content></Menu.Portal></Menu.Root></div></header>
      <main id="content" className={`content page-${route.page}`}>
        {(!connected || !network) && <div className="connection-banner" role="alert"><Icon name="wifi-off" /><span>{!connected ? "启动中心连接中断。已保留页面内容，连接恢复前暂停操作。" : "当前网络离线。本地操作仍可使用，平台登录与下载需要网络。"}</span><Button variant="ghost" onClick={refresh} icon="refresh-cw">重新检查</Button></div>}
        <div className="page-heading"><div><span className="eyebrow">{route.page === "workspace" ? "YOUR CREATIVE SPACE" : route.page === "activity" ? "ACTIVITY" : route.page === "guides" ? "GET STARTED" : "PREFERENCES"}</span><h1 ref={titleRef} tabIndex={-1}>{mainHeading}</h1></div>{route.page === "workspace" && state && <Pill phase={phase} />}{route.page === "activity" && <span className="subtle">仅本次启动 · 最多保留 200 条</span>}</div>
        {!state ? <div className="loading" role="status">{connected ? <><div className="skeleton wide" /><div className="skeleton" /><p>正在连接本机启动中心…</p></> : <><Icon name="unplug" size={32} /><h2>暂时连接不上</h2><p>请确认桌面启动中心仍在运行，再重新检查。</p><Button onClick={refresh}>重新连接</Button></>}</div> : <>
        {route.page === "workspace" && <>
          <section className={`service-zone tone-${info[1]}`} aria-label="本地服务状态"><div className="service-copy"><h2>{info[2].split("\n").map((line, i) => <React.Fragment key={line}>{i > 0 && <br />}{line}</React.Fragment>)}</h2><p className="service-description" role="status">{state.detail}</p><div className="service-actions"><Button variant="primary" disabled={startDisabled} onClick={() => act(phase === "ready" ? "open_panel" : "start")} icon={["starting", "stopping"].includes(phase) ? "loader-circle" : phase === "ready" ? "arrow-up-right" : "play"}>{pending === "open_panel" ? "正在打开…" : info[3]}</Button>{phase === "error" ? <Button variant="ghost" onClick={() => setDrawer("diagnostics")} icon="stethoscope">排查问题</Button> : <Button variant="ghost" onClick={() => navigate("guides")} icon="book-open">从一个小任务开始</Button>}</div></div><div className={`signal-art ${phase === "starting" ? "preparing" : ""}`} aria-hidden="true"><div className="signal-orbit" /><div className="signal-orbit second" /><div className="signal-center"><Icon name="brand" size={55} /></div><span className="signal-node a"><Icon name="file-video" /></span><span className="signal-node b"><Icon name="image" /></span><span className="signal-node c"><Icon name="send" size={16} /></span></div></section>
          <div className="service-strip"><div><span className={`status-dot ${info[1]}`} /><b>本机服务</b><code>{state.url || (phase === "starting" ? "正在分配访问地址" : "尚未连接")}</code>{state.url && <button className="icon-button" aria-label="复制工作台地址" onClick={() => copy(state.url)}><Icon name="copy" size={15} /></button>}</div><div>{phase === "ready" && <span className="subtle">已运行 {uptime(state.uptime)}</span>}<Menu.Root><Menu.Trigger asChild><Button variant="ghost" className="icon-button" aria-label="服务操作"><Icon name="ellipsis" /></Button></Menu.Trigger><Menu.Portal><Menu.Content className="menu" align="end" sideOffset={6}><Menu.Item disabled={disabled || !state.native} onSelect={() => act("hide")}><Icon name="panel-bottom-close" />收起到系统托盘</Menu.Item><Menu.Separator /><Menu.Item className="destructive" disabled={disabled || !state.can_stop || phase === "stopping"} onSelect={() => setConfirm("stop")}><Icon name="square" size={15} />停止本地服务</Menu.Item></Menu.Content></Menu.Portal></Menu.Root></div></div>
          <section className="entry-section"><div className="section-title"><h2>常用入口</h2><span>少找一步，多做一点。</span></div><div className="object-list">{[
            ["book-open", "按平台上手", "从账号登录到第一个任务，跟着图文指南开始。", "查看指南", () => navigate("guides")],
            ["folder", "文件与数据", "找到下载内容、账号资料和升级备份。", "查看位置", () => setDrawer("data")],
            ["stethoscope", "诊断与帮助", "遇到问题时，先定位原因，再决定下一步。", "开始排查", () => setDrawer("diagnostics")],
          ].map(([icon, title, desc, action, click]) => <button key={title} className="object-row" onClick={click}><span className="object-icon"><Icon name={icon} size={22} /></span><span className="object-copy"><b>{title}</b><span>{desc}</span></span><span className="object-action">{action}<Icon name="arrow-up-right" size={16} /></span></button>)}</div></section>
          <div className="recent-line"><Icon name="activity" size={16} /><span>{events[0] ? events[0].title : "还没有运行记录"}</span>{events[0] && <time>{timeLabel(events[0].time)}</time>}<button onClick={() => navigate("activity")}>全部记录 <Icon name="arrow-right" size={14} /></button></div>
          </>}
        {route.page === "activity" && <><p className="page-intro">查看启动、连接与退出过程。这里不展示账号、私信或原始日志。</p><div className="list-toolbar"><Tabs.Root value={filter} onValueChange={setFilter}><Tabs.List aria-label="记录类型" className="tabs"><Tabs.Trigger value="all">全部记录 <span>{events.length}</span></Tabs.Trigger><Tabs.Trigger value="attention">需要关注 <span>{events.filter(e => ["warning", "error"].includes(e.level)).length}</span></Tabs.Trigger></Tabs.List></Tabs.Root><label className="inline-search"><Icon name="search" size={16} /><input aria-label="搜索运行记录" placeholder="搜索记录…" value={query} onChange={e => setQuery(e.target.value)} />{query && <button onClick={() => setQuery("")} aria-label="清除搜索"><Icon name="x" size={14} /></button>}</label></div>{visibleEvents.length ? <div className="activity-list">{visibleEvents.map(event => <button className="activity-row" key={event.id} onClick={() => setDrawer({ event })}><span className={`event-icon ${event.level}`}><Icon name={event.level === "error" ? "circle-alert" : event.level === "success" ? "check" : event.level === "warning" ? "triangle-alert" : "circle-dot"} size={17} /></span><span className="object-copy"><b>{event.title}</b><span>{event.detail}</span></span><time>{timeLabel(event.time)}</time><Icon name="chevron-right" size={16} /></button>)}</div> : <div className="empty"><Icon name={query ? "search-x" : "check-check"} size={32} /><h2>{query ? "没有找到匹配记录" : filter === "attention" ? "暂时没有需要关注的问题" : "还没有运行记录"}</h2><p>{query ? "试试更短的关键词，或清除筛选。" : "服务的启动、连接和退出会记录在这里。"}</p>{(query || filter !== "all") && <Button onClick={() => { setQuery(""); setFilter("all"); }}>查看全部记录</Button>}</div>}</>}
        {route.page === "guides" && <><p className="page-intro">选一个平台，先做好一件小事。各平台的登录方式与功能并不相同。</p><div className={`guide-layout ${guide ? "has-selection" : ""}`}><nav className="platform-list" aria-label="平台指南">{guides.map(([slug, p], index) => <a key={slug} href={`#/guides/${slug}`} aria-current={route.slug === slug ? "page" : undefined} className={route.slug === slug ? "selected" : ""}><span className={`platform-symbol pf-${slug}`}>{["抖", "红", "快", "视"][index]}</span><span><b>{p.name}</b><small>{p.features.length} 个功能教程</small></span><Icon name="chevron-right" size={16} /></a>)}</nav><div className="guide-detail" key={route.slug}>{guide ? <><button className="back-link" onClick={() => navigate("guides")}><Icon name="arrow-left" size={15} />返回平台列表</button><span className="eyebrow">{guide.name} / 快速开始</span><h2>先完成这两步</h2><div className="guide-step"><span>01</span><div><h3>登录你的账号</h3><p>{guide.login}</p></div></div><div className="guide-step"><span>02</span><div><h3>{route.slug === "shipinhao" ? "同步自己的作品" : "下载第一条内容"}</h3><p>{guide.first}</p></div></div><div className="guide-note"><Icon name="info" size={18} /><div><b>这个平台需要注意</b><ul>{guide.limits.map(text => <li key={text}>{text}</li>)}</ul></div></div><Button variant="primary" disabled={disabled} icon="arrow-up-right" onClick={() => act("open_guide", { platform: route.slug })}>阅读完整图文教程</Button></> : <div className="guide-welcome"><Icon name="book-open" size={36} /><h2>你想从哪个平台开始？</h2><p>选择左侧平台，查看登录方式、第一步操作和功能边界。</p><small>真实登录和采集在工作台完成，阅读教程不会执行任务。</small></div>}</div></div></>}
        {route.page === "settings" && <><p className="page-intro">让启动中心适合你的工作习惯。账号与采集配置仍在工作台中管理。</p><section className="settings-group"><h2>外观</h2><div className="theme-choices" role="group" aria-label="主题选择">{[["light", "浅色", "sun"], ["dark", "深色", "moon"], ["system", "跟随系统", "monitor"]].map(([value, label, icon]) => <button key={value} className={preference === value ? "chosen" : ""} aria-pressed={preference === value} disabled={disabled} onClick={() => act("preferences", { theme: value })}><span className={`theme-swatch swatch-${value}`}><i /><i /><i /></span><span><Icon name={icon} size={16} />{label}{preference === value && <Icon name="check" size={16} />}</span></button>)}</div></section><section className="settings-group"><h2>启动与窗口</h2><div className="setting-row"><label htmlFor="auto-open"><b>启动后自动打开工作台</b><span>服务就绪时，在默认浏览器中打开内容管理面板。</span></label><input id="auto-open" className="switch" type="checkbox" role="switch" checked={state.preferences.open_on_ready} disabled={disabled} onChange={e => act("preferences", { open_on_ready: e.target.checked })} /></div><div className="setting-row"><div><b>保留后台运行</b><span>收起启动中心后，本地服务继续运行；可从系统托盘返回。</span></div><Button disabled={disabled || !state.native} onClick={() => act("hide")}>收起到托盘</Button></div></section><section className="settings-group"><h2>本地与隐私</h2><div className="setting-row"><div><b>数据保存在你的电脑</b><span>卸载保留用户数据；升级前会备份配置与数据库。</span></div><Button onClick={() => setDrawer("data")}>查看数据位置</Button></div><div className="about-line"><span>CreatorHub {state.version}</span><span>仅监听 127.0.0.1</span><button onClick={() => setDrawer("diagnostics")}>诊断信息 <Icon name="arrow-up-right" size={14} /></button></div></section></>}
        {route.page === "settings" && <Updates state={state} disabled={disabled} act={act} Modal={Modal} Button={Button} Icon={Icon} />}
        </>}
        <footer className="page-footer"><span><Icon name="shield-check" size={14} />本地运行，数据由你掌握。</span><button disabled={!state || !connected || !!pending || phase === "stopping"} onClick={() => setConfirm("exit")}><Icon name="power" size={14} />停止并退出</button></footer>
      </main>
    </div>

    <Modal open={!!drawer} onOpenChange={open => { if (!open) setDrawer(null); }} sheet title={drawer === "data" ? "文件与数据" : drawer === "diagnostics" ? "诊断与帮助" : drawer?.event?.title || "运行详情"} description={drawer === "data" ? "找到本机文件，了解哪些内容需要保留。" : drawer === "diagnostics" ? "先看状态，再处理问题。" : "本次启动的运行事件"}>
      <div className="sheet-body">{drawer === "data" && <><h3>用户数据目录</h3><div className="path-block"><code>{state?.home}</code><button className="icon-button" aria-label="复制数据目录" onClick={() => copy(state.home)}><Icon name="copy" size={16} /></button></div><Button variant="primary" icon="folder-open" disabled={disabled} onClick={() => act("open_data")}>在资源管理器打开</Button><dl className="data-list"><dt>data/</dt><dd>数据库、下载内容与账号浏览器资料。</dd><dt>backups/</dt><dd>升级前的配置与数据库快照，不含媒体和账号 Profile。</dd><dt>logs/</dt><dd>本地错误日志。可能含私人信息，请勿直接公开。</dd><dt>config.yaml</dt><dd>服务与存储配置，可能包含密钥。</dd></dl><div className="guide-note"><Icon name="shield-check" /><p>完整迁移前先停止服务，再备份整个用户目录；如设置了其他存储路径，也需一起备份。</p></div></>}
      {drawer === "diagnostics" && <><div className="diagnostic-status"><Pill phase={phase} /><span>{connected ? "启动中心连接正常" : "启动中心连接中断"}</span></div><h3>按这个顺序检查</h3><ol className="troubleshoot"><li><b>先检查网络与服务状态</b><p>首次下载浏览器可能需要时间；本地服务正常不代表平台登录有效。</p></li><li><b>查看运行记录或本地日志</b><p>记录帮助判断发生了什么，详细错误保存在 logs 目录。</p><Button variant="ghost" icon="activity" onClick={() => navigate("activity")}>查看运行记录</Button><Button variant="ghost" icon="folder-open" disabled={disabled} onClick={() => act("open_logs")}>打开日志目录</Button></li><li><b>需要反馈时，导出诊断摘要</b><p>仅包含版本、系统、服务状态和退出码，不含配置、目录、账号或原始日志。</p><Button disabled={disabled} icon={pending === "export" ? "loader-circle" : "download"} onClick={() => act("export", {}, "诊断摘要已导出")}>{pending === "export" ? "正在导出…" : "导出诊断摘要"}</Button></li></ol></>}
      {drawer?.event && <><p className="event-time">{new Date(drawer.event.time * 1000).toLocaleString("zh-CN", { hour12: false })}</p><p>{drawer.event.detail || "此事件没有更多说明。"}</p>{["error", "warning"].includes(drawer.event.level) && <Button icon="stethoscope" onClick={() => setDrawer("diagnostics")}>查看排查步骤</Button>}</>}
      </div><div className="sheet-footer"><Button onClick={() => setDrawer(null)} icon="arrow-left">返回</Button></div>
    </Modal>
    <Modal open={!!confirm} onOpenChange={open => { if (!open && !pending) { setConfirm(null); if (state?.close_requested) act("dismiss_close"); } }} title={confirm === "exit" ? "停止服务并退出？" : "停止本地服务？"} description="正在进行的本地任务会中断，账号资料与历史数据将保留。">
      <p className="confirm-copy">{confirm === "exit" ? "如果只是暂时不用，可以收起到系统托盘，让任务继续运行。" : "停止后可以在这里重新启动。关闭浏览器页面不会停止服务。"}</p><div className="dialog-actions"><Button disabled={!!pending} onClick={() => { setConfirm(null); if (state?.close_requested) act("dismiss_close"); }}>继续使用</Button><Button variant="danger" disabled={disabled} icon="power" onClick={async () => { if (await act(confirm, { confirmed: true })) setConfirm(null); }}>{pending ? "正在处理…" : confirm === "exit" ? "停止并退出" : "停止服务"}</Button></div>
    </Modal>
    <Dialog.Root open={command} onOpenChange={setCommand}><Dialog.Portal><Dialog.Overlay className="overlay" /><Dialog.Content className="command-dialog" onOpenAutoFocus={e => { e.preventDefault(); commandInput.current?.focus(); }}><Dialog.Title className="sr-only">搜索入口与指南</Dialog.Title><Dialog.Description className="sr-only">输入关键词，使用上下方向键选择，回车打开，Escape 返回。</Dialog.Description><div className="command-input"><Icon name="search" size={21} /><input ref={commandInput} value={search} onChange={e => setSearch(e.target.value)} placeholder="你想做什么？" aria-label="搜索入口与指南" role="combobox" aria-expanded="true" aria-controls="command-results" aria-activedescendant={commands[active] ? `command-${commands[active].id}` : undefined} onKeyDown={e => { if (["ArrowDown", "ArrowUp"].includes(e.key)) { e.preventDefault(); const next = commands.length ? (active + (e.key === "ArrowDown" ? 1 : -1) + commands.length) % commands.length : 0; setActive(next); document.getElementById(`command-${commands[next]?.id}`)?.scrollIntoView({ block: "nearest" }); } if (e.key === "Enter" && commands[active]) { e.preventDefault(); commands[active].run(); } }} /><Dialog.Close asChild><button className="key-button" aria-label="关闭搜索">Esc</button></Dialog.Close></div><div id="command-results" role="listbox" aria-label="搜索结果" className="command-results">{commands.map((item, i) => <div id={`command-${item.id}`} role="option" aria-selected={active === i} key={item.id} className={active === i ? "selected" : ""} onMouseMove={() => setActive(i)} onClick={item.run}><Icon name={item.icon} /><span>{item.label}</span><small>{item.group}</small><Icon name="corner-down-left" size={14} /></div>)}{!commands.length && <div className="command-empty">没有找到相关入口，试试“指南”“数据”或平台名称。</div>}</div><div className="command-footer"><span>↑ ↓ 选择</span><span>↵ 打开</span><span>Esc 返回</span></div></Dialog.Content></Dialog.Portal></Dialog.Root>
    {notice && <div className={`toast ${notice.tone}`} role={notice.tone === "danger" ? "alert" : "status"}><Icon name={notice.tone === "danger" ? "circle-alert" : "check"} />{notice.message}<button aria-label="关闭提示" onClick={() => setNotice(null)}><Icon name="x" size={15} /></button></div>}
  </div>;
}
createRoot(document.getElementById("root")).render(<App />);
