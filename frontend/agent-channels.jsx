import React, { useRef, useState } from "react";
import * as Dialog from "@radix-ui/react-dialog";
import { Icon } from "./ui/primitives";

export const channelName = item => item.display_name || (item.id === "default" ? "默认渠道" : "未命名渠道");
const protocols = { openai_compatible: "Chat Completions", openai_responses: "Responses", anthropic: "Anthropic Messages" };

export function ChannelList({ channels, activeId, editingId, busy, dirty, onEdit, onAdd, onActivate, onDelete }) {
  const [removing, setRemoving] = useState(null);
  const [deleteError, setDeleteError] = useState("");
  const addButton = useRef(null), deleteButton = useRef(null);
  return <section className="agent-channels" aria-label="模型渠道管理">
    <div className="agent-section-head"><h3>模型渠道</h3><span className="field-help">已保存 {channels.length} / 20 个</span></div>
    <p className="field-help">每个渠道独立保存地址、密钥、模型及高级设置。切换仅影响后续分析，不代表连通性已验证。</p>
    <ul className="agent-channel-list">{channels.map(item => <li key={item.id} data-channel-id={item.id} data-editing={item.id === editingId} data-active={item.id === activeId || undefined}>
      <div className="agent-channel-info"><strong>{channelName(item)}</strong>
        <span className="agent-channel-badges">{item.id === activeId ? <b className="agent-channel-active">当前使用</b> : null}<i className="agent-channel-state" data-enabled={item.enabled || undefined}>{item.enabled ? "已启用" : "未启用"}</i></span>
        <small>{protocols[item.provider]} · {item.model || "尚未选择模型"}</small></div>
      <div className="agent-channel-actions">
        <button type="button" className="ghost" disabled={busy || dirty} aria-label={`编辑渠道 ${channelName(item)}`} onClick={() => onEdit(item)}>编辑</button>
        {item.id !== activeId && <><button type="button" className="ghost" disabled={busy || dirty || !item.base_url || !item.model} aria-label={`使用渠道 ${channelName(item)}`} onClick={() => onActivate(item.id)}>使用</button>
          <button type="button" className="ghost agent-channel-delete" disabled={busy || dirty} aria-label={`删除渠道 ${channelName(item)}`} onClick={event => { deleteButton.current = event.currentTarget; setDeleteError(""); setRemoving(item); }}>删除</button></>}
      </div>
    </li>)}</ul>
    <button type="button" className="ghost agent-add-channel" ref={addButton} disabled={busy || dirty || channels.length >= 20} onClick={onAdd}><Icon name="plus" />添加模型渠道</button>
    {dirty && <p className="field-help">先保存或取消当前修改，再切换、添加或删除渠道。</p>}
    <Dialog.Root open={!!removing} onOpenChange={value => { if (!busy && !value) setRemoving(null); }}>
      <Dialog.Portal><Dialog.Overlay className="agent-picker-overlay" />
        <Dialog.Content className="agent-model-picker agent-global agent-workspace agent-channel-confirm" data-agent-channel-dialog
          onEscapeKeyDown={event => event.stopPropagation()} onCloseAutoFocus={event => {
            event.preventDefault(); (deleteButton.current?.isConnected ? deleteButton.current : addButton.current)?.focus();
          }}>
          <Dialog.Title>删除模型渠道？</Dialog.Title>
          <Dialog.Description>将删除「{removing ? channelName(removing) : ""}」的本地配置、密钥和模型目录。不会删除上游账号或分析历史。</Dialog.Description>
          {deleteError && <p className="agent-error" role="alert">{deleteError}</p>}
          <div className="agent-picker-footer"><Dialog.Close className="ghost" disabled={busy}>保留渠道</Dialog.Close>
            <button type="button" className="danger" disabled={busy} onClick={async () => { if (await onDelete(removing.id)) setRemoving(null); else setDeleteError("删除未完成，渠道仍保留。可重试，或关闭弹窗查看详细错误。"); }}>确认删除渠道</button></div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  </section>;
}
