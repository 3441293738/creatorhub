import React, { useEffect, useRef, useState } from "react";
import { Sheet } from "./ui/primitives";

const labels = { bound: "已绑定其他设备", revoked: "设备绑定已撤销", inactive: "未激活", active: "授权有效", expired: "授权已到期", invalid: "授权无效", disabled: "授权已禁用", unbound: "设备已解绑", device_mismatch: "设备不匹配", offline: "无法连接授权服务", grace: "断网宽限中", device_error: "设备标识读取失败", revalidation_required: "需要联网校验" };
export function LicenseControl() {
  const [license, setLicense] = useState(null);
  const [open, setOpen] = useState(false);
  const [code, setCode] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const trigger = useRef(null);
  const refresh = async () => {
    try {
      const response = await fetch("/api/license", { credentials: "same-origin", cache: "no-store" });
      if (!response.ok) throw new Error("授权状态读取失败，请重试");
      setLicense(await response.json());
    } catch (e) { setLicense(null); setError(e.message); }
  };
  useEffect(() => {
    refresh();
    const timer = setInterval(refresh, 5000);
    const onFocus = () => refresh();
    window.addEventListener("focus", onFocus);
    return () => { clearInterval(timer); window.removeEventListener("focus", onFocus); };
  }, []);
  const activate = async event => {
    event.preventDefault();
    if (busy) return;
    setBusy(true); setError(""); setMessage("");
    try {
      const response = await fetch("/api/license/activate", {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify({ code: code.trim() }),
      });
      const result = await response.json();
      if (!response.ok) throw new Error(typeof result.detail === "string" ? result.detail : "激活失败，请检查激活码");
      setLicense(result); setCode(""); setMessage("授权已保存，业务功能已恢复。");
    } catch (e) { setError(e.message || "激活失败，请重试"); }
    finally { setBusy(false); }
  };
  const revalidate = async () => {
    if (busy) return;
    setBusy(true); setError(""); setMessage("");
    try {
      const response = await fetch("/api/license/revalidate", { method: "POST", credentials: "same-origin" });
      const result = await response.json();
      if (!response.ok) throw new Error(typeof result.detail === "string" ? result.detail : "重新校验失败");
      setLicense(result);
      setMessage(result.valid ? "联网校验成功。" : "校验完成，请查看授权状态。");
    } catch (e) { setError(e.message); await refresh(); }
    finally { setBusy(false); }
  };
  const copyDevice = async () => {
    try { await navigator.clipboard.writeText(license.device_digest); setMessage("设备标识已复制。"); }
    catch { setError("无法访问剪贴板，请手动选择并复制设备标识。"); }
  };
  const status = license ? labels[license.status] || "需要重新校验" : "授权状态未读取";
  return <>
    <button type="button" className="ghost" ref={trigger} onClick={() => { setOpen(true); refresh(); }} aria-label="授权与激活">{status} · {license?.valid ? "续期" : "激活"}</button>
    <Sheet open={open} onOpenChange={setOpen} title="授权与激活" description="一个授权码绑定一台设备；续期后重新校验即可" busy={busy} onReturnFocus={() => trigger.current?.focus()}>
      <div className="wb-sheet-scroll" style={{ padding: "24px" }}>
        <p role="status"><strong>{status}</strong></p>
        {license?.customer && <p>授权对象：{license.customer}</p>}
        <p>到期时间：{license?.expires_at ? new Date(license.expires_at).toLocaleString("zh-CN", { timeZoneName: "short" }) : "—"}</p>
        <p style={{ overflowWrap: "anywhere" }}>设备标识：{license?.device_digest || "未读取"}</p>
        {license?.reason && <p role="status">{license.reason}</p>}
        <button type="button" disabled={!license?.device_digest} onClick={copyDevice}>复制设备标识</button>
        <button type="button" disabled={busy} onClick={revalidate}>手动重新校验</button>
        {!license?.valid && <p>激活后可启动业务任务。已有数据仍可查看和导出。</p>}
        <form onSubmit={activate} className="stack">
          <label htmlFor="license-code">授权码</label>
          <textarea id="license-code" rows={6} value={code} onChange={event => setCode(event.target.value)} required autoComplete="off" spellCheck={false} disabled={busy} style={{ width: "100%", overflowWrap: "anywhere" }} />
          {error && <p role="alert">{error}</p>}
          {message && <p role="status">{message}</p>}
          <button type="submit" disabled={busy || !code.trim()}>{busy ? "正在验证…" : "验证并保存授权"}</button>
        </form>
      </div>
    </Sheet>
  </>;
}
