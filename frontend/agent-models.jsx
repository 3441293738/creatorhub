import React, { useEffect, useMemo, useRef, useState } from "react";
import * as Dialog from "@radix-ui/react-dialog";
import { Icon } from "./ui/primitives";

const MAX_MODELS = 1000;

// Selection is a local draft. Neither fetching nor closing this dialog changes
// the provider's configured directory; only its explicit apply action does.
export function ModelPicker({ open, available, configured, onOpenChange, onApply, returnFocus }) {
  const [query, setQuery] = useState(""), [selected, setSelected] = useState(new Set());
  const [page, setPage] = useState(0), [error, setError] = useState("");
  const search = useRef(null);
  const rows = useMemo(() => {
    const existing = new Map(configured.map(item => [item.id, item]));
    const ids = new Set(available.map(item => item.id));
    return [...available.map(item => ({ ...(existing.get(item.id) || item), local: false })),
      ...configured.filter(item => !ids.has(item.id)).map(item => ({ ...item, local: true }))];
  }, [available, configured]);
  useEffect(() => {
    if (!open) return;
    setQuery(""); setPage(0); setError("");
    setSelected(new Set((configured.length ? configured : available).map(item => item.id)));
  }, [open, available, configured]);
  const filtered = useMemo(() => {
    const needle = query.trim().toLocaleLowerCase();
    return rows.filter(item => `${item.id} ${item.name}`.toLocaleLowerCase().includes(needle));
  }, [rows, query]);
  const pages = Math.max(1, Math.ceil(filtered.length / 100));
  const select = (ids, checked) => {
    const next = new Set(selected);
    ids.forEach(id => checked ? next.add(id) : next.delete(id));
    if (next.size > MAX_MODELS) { setError("最多配置 1000 个模型，请减少勾选数量。"); return; }
    setSelected(next); setError("");
  };
  const apply = () => {
    onApply(rows.filter(item => selected.has(item.id)).map(({ id, name }) => ({ id, name })));
    onOpenChange(false);
  };
  return <Dialog.Root open={open} onOpenChange={onOpenChange}>
    <Dialog.Portal>
      <Dialog.Overlay className="agent-picker-overlay" />
      <Dialog.Content className="agent-model-picker agent-global agent-workspace" data-agent-model-picker
        onOpenAutoFocus={event => { event.preventDefault(); search.current?.focus(); }}
        onCloseAutoFocus={event => { event.preventDefault(); returnFocus.current?.focus(); }}
        onEscapeKeyDown={event => event.stopPropagation()}>
        <div className="agent-picker-heading"><Dialog.Title>选择模型</Dialog.Title>
          <Dialog.Close className="ghost agent-icon-button" aria-label="关闭模型选择"><Icon name="x" /></Dialog.Close></div>
        <Dialog.Description className="field-help agent-picker-description">勾选需要加入目录的模型，再点击「更新目录」。已有的显示名称会保留；此操作尚未保存配置。</Dialog.Description>
        <div className="agent-picker-toolbar"><label htmlFor="agent-model-search">搜索模型<input id="agent-model-search" ref={search} type="search" maxLength={200} value={query}
          placeholder="搜索模型 ID 或显示名称" onChange={event => { setQuery(event.target.value); setPage(0); }} /></label>
          <div className="agent-catalog-actions"><button type="button" className="ghost" onClick={() => select(filtered.map(item => item.id), true)} disabled={!filtered.length}>{query ? "全选搜索结果" : "全选"}</button>
            <button type="button" className="ghost" onClick={() => { setSelected(new Set()); setError(""); }} disabled={!selected.size}>取消全选</button></div>
        </div>
        <p className="field-help agent-picker-count" role="status">找到 {filtered.length} 个模型 · 已选 {selected.size} 个</p>
        {error && <p className="agent-error agent-picker-error" role="alert">{error}</p>}
        <div className="agent-picker-results" role="group" aria-label="可选择模型">
          {!filtered.length ? <p className="agent-catalog-empty">未找到匹配模型，请尝试其他关键词或清空搜索。</p>
            : filtered.slice(page * 100, (page + 1) * 100).map(item => <label key={item.id} className="agent-picker-option">
              <input type="checkbox" checked={selected.has(item.id)} onChange={event => select([item.id], event.target.checked)} aria-label={`选择模型 ${item.id}`} />
              <span><strong>{item.id}</strong>{item.name && item.name !== item.id && <small>{item.name}</small>}{item.local && <small>已在目录 · 本次上游未返回</small>}</span>
            </label>)}
        </div>
        {pages > 1 && <div className="agent-model-pagination"><button type="button" className="ghost" disabled={!page} onClick={() => setPage(value => value - 1)} aria-label="上一页可选模型">上一页</button>
          <span>{page + 1} / {pages} 页</span><button type="button" className="ghost" disabled={page + 1 >= pages} onClick={() => setPage(value => value + 1)} aria-label="下一页可选模型">下一页</button></div>}
        <div className="agent-picker-footer"><Dialog.Close className="ghost">取消</Dialog.Close><button type="button" onClick={apply}>更新目录</button></div>
      </Dialog.Content>
    </Dialog.Portal>
  </Dialog.Root>;
}

export function ModelDirectory({ models, currentModel, busy, onRename, onEdit, onRemove }) {
  const [page, setPage] = useState(0);
  const pages = Math.max(1, Math.ceil(models.length / 20));
  useEffect(() => setPage(value => Math.min(value, pages - 1)), [pages]);
  return <div className="agent-directory">
    <ul className="agent-directory-list" aria-label="已配置模型">{models.slice(page * 20, (page + 1) * 20).map(item => <li key={item.id} data-model-id={item.id} data-current={currentModel === item.id}>
      <div className="agent-directory-fields"><label>模型 ID{currentModel === item.id && <span className="agent-current-model">当前使用</span>}<input value={item.id} readOnly aria-label={`模型 ${item.id} 的 ID`} /></label>
        <label>显示名称<input value={item.name} maxLength={200} placeholder={item.id} disabled={busy} aria-label={`模型 ${item.id} 的显示名称`} onChange={event => onRename(item.id, event.target.value)} /></label></div>
      <div className="agent-directory-actions"><button type="button" className="ghost agent-icon-button" disabled={busy} aria-label={`配置模型 ${item.id}`} title="编辑模型 ID 与显示名称" onClick={() => onEdit(item)}><Icon name="edit" /></button>
        <button type="button" className="ghost agent-icon-button agent-directory-remove" disabled={busy} aria-label={`移除模型 ${item.id}`} title="从目录移除" onClick={() => onRemove(item.id)}><Icon name="trash" /></button></div>
    </li>)}</ul>
    {pages > 1 && <div className="agent-model-pagination"><button type="button" className="ghost" disabled={!page} onClick={() => setPage(value => value - 1)} aria-label="上一页已配置模型">上一页</button>
      <span>{page + 1} / {pages} 页</span><button type="button" className="ghost" disabled={page + 1 >= pages} onClick={() => setPage(value => value + 1)} aria-label="下一页已配置模型">下一页</button></div>}
  </div>;
}
