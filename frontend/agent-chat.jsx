import React, { memo, useEffect, useRef, useState } from "react";
import { Icon } from "./ui/primitives";

function CopyText({ text, label = "复制回复" }) {
  const [result, setResult] = useState("");
  const timer = useRef(null);
  useEffect(() => () => clearTimeout(timer.current), []);
  const finish = value => {
    setResult(value);
    clearTimeout(timer.current);
    timer.current = setTimeout(() => setResult(""), 2000);
  };
  return <button type="button" className="ghost agent-copy" data-copied={result === "已复制" || undefined} aria-label={label} onClick={async () => {
    try { await navigator.clipboard.writeText(text); finish("已复制"); }
    catch { finish("请手动选择复制"); }
  }}><Icon name={result === "已复制" ? "check" : "copy"} /><span>{result || label}</span></button>;
}

const inline = text => text.split(/(`[^`\n]+`|\*\*[^*\n]+\*\*)/g).map((part, index) =>
  part.startsWith("**") && part.endsWith("**") ? <strong key={index}>{part.slice(2, -2)}</strong>
    : part.startsWith("`") && part.endsWith("`") ? <code key={index}>{part.slice(1, -1)}</code> : part);

const number = value => value == null ? "—" : value.toLocaleString("zh-CN");
const pending = run => ["queued", "running"].includes(run.status);
const usageFor = run => run.usage || {
  input_tokens: run.input_tokens || null, output_tokens: run.output_tokens || null,
  total_tokens: run.input_tokens || run.output_tokens ? (run.input_tokens || 0) + (run.output_tokens || 0) : null,
  status: run.input_tokens || run.output_tokens ? "legacy" : "unavailable", costs: [],
};

function Reasoning({ run }) {
  const records = run.reasoning || [], visible = records.some(item => item.text);
  if (!records.length) return <p className="agent-reasoning-empty">{pending(run)
    ? "流式连接中，等待上游返回推理内容或答复"
    : "上游未返回可展示的推理内容；可检查模型能力与高级推理设置。"}</p>;
  return <details className="agent-reasoning" open={pending(run)}><summary><span>思考 / 推理内容</span><small>{visible ? pending(run) ? "正在接收" : "上游返回" : "仅受保护内容"}</small></summary>
    <p className="field-help">仅展示模型接口返回的摘要或推理文本，不等同于完整内部思考；与下方工具执行记录分别显示。</p>
    <div className="agent-reasoning-body">{records.map((item, index) => <section key={index}>
      <h4>第 {item.round} 次模型调用 · {item.kind === "summary" ? "推理摘要" : "上游推理文本"}</h4>
      {item.text ? <p>{item.text}</p> : <p>此轮仅返回受保护内容，没有可展示的文本。</p>}
      {item.truncated && <p className="field-help">内容较长，仅保留前段。</p>}
    </section>)}</div>
  </details>;
}

function TokenUsage({ run }) {
  const usage = usageFor(run), costs = usage.costs || [];
  const hint = usage.status === "legacy" ? "旧记录用量，未记录完整明细"
    : usage.status === "partial" ? `仅已返回部分 · ${usage.reported_requests}/${usage.requests} 次调用用量完整`
    : usage.status === "reported" ? `${usage.requests} 次模型调用合计`
    : pending(run) ? "等待上游 usage，收到统计事件后更新" : "上游未返回完整用量，不代表消耗为 0";
  return <div className="agent-token-usage" aria-label="本条消息 token 用量" aria-live="off">
    <dl><div><dt>输入 token</dt><dd>{number(usage.input_tokens)}</dd></div><div><dt>输出 token</dt><dd>{number(usage.output_tokens)}</dd></div><div><dt>总消耗 token</dt><dd>{number(usage.total_tokens)}</dd></div></dl>
    <p className="agent-token-breakdown">{usage.reasoning_tokens != null && <span>推理 {number(usage.reasoning_tokens)}（已含在输出中）</span>}
      {usage.cached_input_tokens != null && <span>缓存命中 {number(usage.cached_input_tokens)}（已含在输入中）</span>}
      {usage.cache_write_input_tokens != null && <span>缓存写入 {number(usage.cache_write_input_tokens)}（已含在输入中）</span>}</p>
    <p className="agent-usage-hint">{hint} · {((run.duration_ms || 0) / 1000).toFixed(1)} 秒</p>
    <p className="agent-usage-cost">费用：{costs.length ? `${usage.cost_complete ? "" : "已返回部分 "}${costs.map(cost => `${cost.currency} ${cost.amount}`).join(" + ")}（上游回报，账单为准）`
      : pending(run) ? "等待上游回报" : "上游未返回可核对金额，请以上游账单为准"}</p>
  </div>;
}

export function ConversationUsage({ turns }) {
  if (!turns.length) return null;
  const usages = turns.map(usageFor), totals = usages.filter(item => item.total_tokens != null);
  return <p className="agent-conversation-usage">当前显示 {turns.length} 轮 · 已知消耗合计 {totals.length ? number(totals.reduce((sum, item) => sum + item.total_tokens, 0)) : "—"} token
    {usages.some(item => item.status !== "reported") ? "（含用量缺失 / 不完整记录）" : "（含工具往返的模型调用）"}</p>;
}

// A deliberately small Markdown subset. Every upstream byte stays React text:
// no raw HTML, external image loading, dangerous link schemes or code execution.
export const MessageBody = memo(function MessageBody({ text }) {
  const lines = text.split("\n"), blocks = [];
  for (let i = 0; i < lines.length;) {
    if (lines[i].startsWith("```")) {
      const language = lines[i++].slice(3).trim().slice(0, 30), code = [];
      while (i < lines.length && !lines[i].startsWith("```")) code.push(lines[i++]);
      if (i < lines.length) i++;
      const value = code.join("\n");
      blocks.push(<div className="agent-code-block" key={i}><div><span>{language || "代码"}</span><CopyText text={value} label="复制代码" /></div><pre><code>{value}</code></pre></div>);
    } else if (/^#{1,3}\s/.test(lines[i])) {
      blocks.push(<h4 key={i}>{inline(lines[i++].replace(/^#{1,3}\s+/, ""))}</h4>);
    } else if (/^\s*(?:[-*]|\d+\.)\s/.test(lines[i])) {
      const ordered = /^\s*\d+\./.test(lines[i]), items = [], matcher = ordered ? /^\s*\d+\.\s+/ : /^\s*[-*]\s+/;
      while (i < lines.length && matcher.test(lines[i])) items.push(<li key={i}>{inline(lines[i++].replace(matcher, ""))}</li>);
      blocks.push(ordered ? <ol key={i}>{items}</ol> : <ul key={i}>{items}</ul>);
    } else if (!lines[i].trim()) i++;
    else {
      const paragraph = [lines[i++]];
      while (i < lines.length && lines[i].trim() && !/^(?:```|#{1,3}\s|\s*(?:[-*]|\d+\.)\s)/.test(lines[i])) paragraph.push(lines[i++]);
      blocks.push(<p key={i}>{inline(paragraph.join("\n"))}</p>);
    }
  }
  return <div className="agent-answer">{blocks}</div>;
});

export function ChatMessages({ turns, busy, statusText, platformText, onRetry, renderWorkflow }) {
  return <div className="agent-transcript" role="log" aria-label="对话消息" aria-live="polite" aria-relevant="additions text">
    {turns.map((run, index) => <section className="agent-result agent-turn" key={run.id} data-run-id={run.id} data-status={run.status} aria-label={`第 ${index + 1} 轮对话`}>
      <div className="agent-user-message"><span>你</span><p className="agent-result-question">{run.prompt}</p></div>
      <div className="agent-assistant-message">
        <div className="agent-message-top">
          <div className="agent-message-heading"><span className="agent-message-avatar"><Icon name="sparkles" /></span><strong>AI 助手</strong><span className="agent-model-chip">{run.model_name}</span></div>
          <h3 className="agent-message-status">{statusText[run.status] || run.status}</h3>
        </div>
        {pending(run) && <p className="agent-working"><span className="agent-working-dot" />{run.partial_answer ? "正在流式输出…" : "正在读取证据或接收模型事件…"}</p>}
        {run.error && <p className="agent-error">{run.error}</p>}
        <Reasoning run={run} />
        {run.answer && <MessageBody text={run.answer} />}
        {!run.answer && run.partial_answer && <div className="agent-stream-preview"><p className="agent-partial-label">{pending(run) ? "生成中 · 内容尚未完成" : "已接收的部分内容 · 本次未完成"}</p><MessageBody text={run.partial_answer} /></div>}
        {(run.workflows || []).map(workflow => <details className="agent-chat-plan" open key={workflow.id}><summary>工作流卡片 · {statusText[workflow.status] || workflow.status}</summary>{renderWorkflow(workflow, run.id)}</details>)}
        <div className="agent-message-meta">本次范围：{platformText[run.platform] || "全平台"}{run.account_id ? ` · 账号 #${run.account_id}` : ""}{run.channel_name ? ` · ${run.channel_name}` : ""}</div>
        <TokenUsage run={run} />
        <details className="agent-details agent-message-evidence"><summary>执行记录与证据（{run.evidence?.length || 0}）</summary>
          <p className="field-help">证据编号仅对应本轮消息。输入包含系统说明、上下文与工具结果；输出包含上游计入的推理 token，不按回复字数估算。</p>
          {!!run.model_calls?.length && <ul className="agent-call-usage">{run.model_calls.map(call => <li key={call.round}>模型调用 {call.round} · 输入 {number(call.usage?.input_tokens)} / 输出 {number(call.usage?.output_tokens)} / 合计 {number(call.usage?.total_tokens)} token · {call.status === "completed" ? "已返回" : call.status === "running" ? "等待返回" : "未完成"}</li>)}</ul>}
          <ol>{(run.steps || []).map((step, n) => <li key={n}>{step.tool} · {step.status}{step.evidence_id && ` · ${step.evidence_id}`}{step.error && ` · ${step.error}`}</li>)}</ol>
          {(run.evidence || []).map(item => <details key={item.id}><summary>{item.id} · {item.tool}</summary><pre>{JSON.stringify(item.data, null, 2)}</pre></details>)}
        </details>
        <div className="agent-message-actions">{run.answer && <CopyText text={run.answer} />}
          {["failed", "canceled", "interrupted"].includes(run.status) && <button type="button" className="ghost" disabled={busy} onClick={() => onRetry(run.prompt)}><Icon name="edit" />编辑后重试</button>}</div>
      </div>
    </section>)}
  </div>;
}
