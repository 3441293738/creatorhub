import React, { useState } from "react";

export default function Updates({ state, disabled, act, Modal, Button, Icon }) {
  const [open, setOpen] = useState(false), [confirm, setConfirm] = useState(false);
  const update = state.updates || { status: "idle", message: "手动检查正式版更新。" };
  const checking = update.status === "checking";
  const downloadable = !!update.download_url && ["available", "development"].includes(update.status);
  const check = () => act("check_updates");
  return <section className="settings-group desktop-updates" aria-labelledby="updates-title">
    <h2 id="updates-title">版本与更新</h2>
    <div className="setting-row"><div><b>CreatorHub {state.version}</b><span>只在你检查时连接 GitHub，不会自动安装。</span></div><Button disabled={disabled || checking} icon={checking ? "loader-circle" : "refresh-cw"} onClick={check}>{checking ? "正在检查…" : "检查更新"}</Button></div>
    <div className={`update-status ${update.status === "error" ? "update-error" : ""}`} role="status" aria-live="polite" aria-busy={checking}>
      <span>{update.message}</span>{update.checked_at && <small>上次检查：{new Date(update.checked_at * 1000).toLocaleString("zh-CN", { hour12: false })}</small>}
    </div>
    <div className="update-actions">{update.version && <Button onClick={() => { setConfirm(false); setOpen(true); }} icon="file-text">查看 {update.version} 更新说明</Button>}<Button variant="ghost" disabled={disabled} icon="arrow-up-right" onClick={() => act("open_releases")}>前往发布页</Button></div>
    <Modal open={open} onOpenChange={value => { setOpen(value); if (!value) setConfirm(false); }} title={confirm ? "下载前，请留意" : `CreatorHub ${update.version || ""}`} description={confirm ? "下载由默认浏览器处理，当前任务不会被中断。" : "GitHub 正式版更新说明"}>
      {confirm ? <div className="update-detail"><p>下载完成后，请先在旧版点击“停止并退出”，再运行安装包。不要同时运行两个版本。</p><p>账号与配置保留在原用户目录；如有重要资料，建议先完整备份。不会自动安装或重启。</p><code>{update.asset_name}</code><div className="update-actions"><Button onClick={() => setConfirm(false)}>返回更新说明</Button><Button variant="primary" disabled={disabled || !downloadable} icon="download" onClick={async () => { if (await act("download_update", { confirmed: true, tag: update.tag })) setConfirm(false); }}>确认并下载</Button></div></div> : <div className="update-detail">
        {update.published_at && <p className="subtle">发布时间：{update.published_at.slice(0, 10)}</p>}
        <div className="release-notes" tabIndex={0} aria-label="更新说明内容">{update.notes || "暂无更新说明。"}</div>
        <p className="subtle">{update.asset_name ? `Windows x64 · ${Math.ceil(update.size / 1024 / 1024)} MB` : "本次发布尚未提供 Windows x64 安装包。"}</p>
        <div className="update-actions"><Button variant="ghost" onClick={() => setOpen(false)}>返回设置</Button>{downloadable && <Button variant="primary" disabled={disabled} icon="download" onClick={() => setConfirm(true)}>下载新版</Button>}</div>
      </div>}
    </Modal>
  </section>;
}
