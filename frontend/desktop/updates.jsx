import React, { useState } from "react";
import "./updates.css";

const busyStatuses = ["checking", "downloading", "verifying", "preparing", "installing"];
const downloadStatuses = ["available", "development", "download_error", "cancelled", "install_error"];
const megabytes = bytes => `${((Number(bytes) || 0) / 1024 / 1024).toFixed(1)} MB`;

export default function Updates({ state, disabled, act, Modal, Button, Icon, showSettings = true }) {
  const [dialog, setDialog] = useState(null), [selectedTag, setSelectedTag] = useState(null);
  const update = state.updates || { status: "idle", message: "手动检查正式版更新。" };
  const checking = update.status === "checking";
  const downloading = ["downloading", "verifying"].includes(update.status);
  const installing = ["preparing", "installing"].includes(update.status);
  const busy = busyStatuses.includes(update.status);
  const ready = update.status === "downloaded" || (update.status === "install_error" && update.progress === 100);
  const downloadable = !!update.download_url && downloadStatuses.includes(update.status);
  const nativeUpdate = state.update_install_supported && update.verified_download;
  const progress = Math.max(0, Math.min(100, Number(update.progress) || 0));
  const delta = update.download_kind === "delta";
  const downloadSize = update.download_size ?? update.size;
  const show = mode => { setSelectedTag(update.tag); setDialog(mode); };
  const close = () => setDialog(null);
  const prepare = async () => { if (await act("prepare_update", { confirmed: true, tag: update.tag })) close(); };
  const stale = selectedTag !== update.tag;
  const updateButton = () => ready
    ? <Button variant="primary" disabled={disabled || !state.update_install_supported} icon="refresh-cw" onClick={() => show("install")}>安装并重启</Button>
    : downloadable && nativeUpdate
      ? <Button variant="primary" disabled={disabled || busy} icon="download" onClick={prepare}>{["download_error", "cancelled", "install_error"].includes(update.status) ? "重试下载" : "一键更新"}</Button>
      : null;
  return <>
    {!showSettings && update.notification && <section className="update-banner" aria-label="版本更新提醒">
      <Icon name={ready ? "shield-check" : "download"} size={22} />
      <div className="update-banner-copy">
        <b role="status">{ready ? `CreatorHub ${update.version} 已准备好` : downloading ? "正在下载新版，当前任务继续运行" : installing ? "正在安装新版" : `发现 CreatorHub ${update.version}`}</b>
        <p>{ready ? "方便时安装并重启，账号与配置会保留。" : downloading ? `${megabytes(update.downloaded_bytes)} / ${megabytes(downloadSize)} · 支持断点续传` : update.status === "available" ? "可先查看更新说明，再决定是否更新。" : update.message}</p>
        {downloading && <progress aria-label="安装包下载进度" max="100" value={progress} />}
        <div className="update-actions">
          {updateButton()}
          <Button variant="ghost" disabled={installing} icon="file-text" onClick={() => show("notes")}>更新说明</Button>
          {downloading ? <Button variant="ghost" disabled={disabled} onClick={() => act("cancel_update")}>取消下载</Button> : !installing && <Button variant="ghost" disabled={disabled} onClick={() => act("defer_update", { tag: update.tag })}>稍后提醒</Button>}
        </div>
      </div>
    </section>}
    {showSettings && <section className="settings-group desktop-updates" aria-labelledby="updates-title">
    <h2 id="updates-title">版本与更新</h2>
    <div className="setting-row"><div><b>CreatorHub {state.version}</b><span>下载不中断任务，安装前再次确认。</span></div><Button disabled={disabled || busy || ready} icon={checking ? "loader-circle" : "refresh-cw"} onClick={() => act("check_updates")}>{checking ? "正在检查…" : "检查更新"}</Button></div>
    <div className="setting-row"><label htmlFor="auto-check-updates"><b>自动检查新版本</b><span>安装版启动后在后台检查，每天最多一次。只提醒，不自动下载。</span></label><input id="auto-check-updates" className="switch" type="checkbox" role="switch" checked={update.auto_check !== false} disabled={disabled} onChange={e => act("update_preferences", { auto_check: e.target.checked })} /></div>
    <div className={`update-status ${["error", "download_error", "install_error"].includes(update.status) ? "update-error" : ""}`} role="status" aria-live="polite" aria-busy={checking}>
      <span>{update.message}</span>{update.checked_at && <small>上次检查：{new Date(update.checked_at * 1000).toLocaleString("zh-CN", { hour12: false })}</small>}
    </div>
    {(downloadable || downloading || ready || installing) && downloadSize > 0 && <p className="update-help update-plan">
      <b>{delta ? "文件增量更新" : "完整安装包更新"}</b> · 本次下载 {megabytes(downloadSize)}
      {delta && <>，比完整包少下载 {megabytes(Math.max(0, update.size - downloadSize))}；未变化的文件继续复用。</>}
      {!delta && <>。支持断点续传，下载完成后验证完整性。</>}
    </p>}
    {update.fallback_reason && <p className="update-help">{update.fallback_reason}</p>}
    {(downloading || ready || installing) && <div className="update-progress">
      <div className="update-progress-caption"><b>{ready ? "已通过 SHA-256 校验" : installing ? "正在准备安装" : update.status === "verifying" ? "正在校验更新文件" : delta ? "正在下载变化文件" : "正在下载安装包"}</b><span>{downloading ? `${megabytes(update.downloaded_bytes)} / ${megabytes(downloadSize)}` : "下载 → 校验 → 备份 → 安装"}</span></div>
      <progress aria-label="安装包下载进度" max="100" value={progress} />
      {downloading && <div className="update-actions"><span className="subtle">{update.resumed_bytes > 0 ? `已复用 ${megabytes(update.resumed_bytes)} 下载缓存。` : "下载期间可继续使用工作台。"}取消后保留下载进度。</span><Button disabled={disabled} onClick={() => act("cancel_update")}>取消下载</Button></div>}
      {installing && <p className="subtle">请等待安装交接完成。完成后重新打开启动中心，任务由你手动启动。</p>}
    </div>}
    <div className="update-actions">
      {updateButton()}
      {update.version && <Button disabled={installing} onClick={() => show("notes")} icon="file-text">查看 {update.version} 更新说明</Button>}
      {update.status === "available" && <Button variant="ghost" disabled={disabled} onClick={() => act("skip_update", { tag: update.tag })}>忽略此版本</Button>}
      {downloadable && !nativeUpdate && <Button disabled={disabled || busy} icon="download" onClick={() => show("manual")}>下载新版</Button>}
      <Button variant="ghost" disabled={disabled || installing} icon="arrow-up-right" onClick={() => act("open_releases")}>前往发布页</Button>
    </div>
    {downloadable && !update.verified_download && <p className="update-help">此版本尚未提供完整的校验信息，一键更新将在发布信息补齐后开放。</p>}
    {ready && !state.update_install_supported && <p className="update-help">一键安装在打包后的 Windows 桌面客户端中使用；源码预览不执行安装。</p>}
    </section>}
    <Modal open={!!dialog} onOpenChange={value => { if (!value) close(); }} title={dialog === "install" ? "安装新版并重启？" : dialog === "manual" ? "下载前，请留意" : `CreatorHub ${update.version || ""}`} description={dialog === "install" ? "先备份，再更新程序；账号资料和媒体文件原位保留。" : dialog === "manual" ? "下载由默认浏览器处理，当前任务不会被中断。" : "GitHub 正式版更新说明"}>
      {dialog === "install" ? <div className="update-detail">
        <div className="update-warning"><Icon name="triangle-alert" /><p>{state.can_stop ? "本地服务正在运行。继续会停止服务，并中断正在进行的采集、发布等任务。" : "启动中心将退出以替换程序文件，安装完成后重新打开。"}</p></div>
        <ol className="update-steps"><li>再次校验安装包，停止服务。</li><li>备份配置与数据库，保留账号和媒体。</li><li>安装新版并重新打开启动中心，不自动重放任务。</li></ol>
        {delta && <p className="subtle">增量安装先组装并检查完整新版，再切换程序目录；切换或启动检查失败时恢复旧版。需要额外磁盘空间保留旧版。</p>}
        <p className="subtle">安装期间请保持电源连接。重要资料可提前完整备份。</p>
        <div className="update-actions"><Button disabled={disabled} onClick={close}>暂不安装</Button><Button variant="primary" disabled={disabled || !ready || stale || !state.update_install_supported} icon="refresh-cw" onClick={async () => { if (await act("install_update", { confirmed: true, tag: selectedTag })) close(); }}>确认安装并重启</Button></div>
      </div> : dialog === "manual" ? <div className="update-detail">
        <p>下载完成后，请先在旧版点击“停止并退出”，再运行安装包。不要同时运行两个版本。</p><p>账号与配置保留在原用户目录；如有重要资料，建议先完整备份。</p><code>{update.asset_name}</code>
        <div className="update-actions"><Button onClick={() => setDialog("notes")}>返回更新说明</Button><Button variant="primary" disabled={disabled || stale || !downloadable} icon="download" onClick={async () => { if (await act("download_update", { confirmed: true, tag: selectedTag })) setDialog("notes"); }}>确认并下载</Button></div>
      </div> : <div className="update-detail">
        {update.published_at && <p className="subtle">发布时间：{update.published_at.slice(0, 10)}</p>}
        <div className="release-notes" tabIndex={0} aria-label="更新说明内容">{update.notes || "暂无更新说明。"}</div>
        <p className="subtle">{update.asset_name ? `Windows x64 · ${megabytes(update.size)}` : "本次发布尚未提供 Windows x64 安装包。"}</p>
        <div className="update-actions"><Button variant="ghost" onClick={close}>返回设置</Button>{updateButton()}{downloadable && <Button disabled={disabled || busy} icon="download" onClick={() => show("manual")}>下载新版</Button>}</div>
      </div>}
    </Modal>
  </>;
}
