// Read-only live subscription. Reconnection is GET-only and never re-generates.
export async function watchRun(id, onSnapshot, signal) {
  const connection = new AbortController();
  const abort = () => connection.abort();
  signal.addEventListener("abort", abort, { once: true });
  if (signal.aborted) abort();
  let timeout, reader, timedOut = false;
  const arm = () => { clearTimeout(timeout); timeout = setTimeout(() => { timedOut = true; connection.abort(); }, 25000); };
  let finished = false, lastStatus = "", buffer = "";
  try {
    arm();
    const response = await fetch(`/api/agent/runs/${encodeURIComponent(id)}/events`, {
      credentials: "same-origin", cache: "no-store", headers: { Accept: "text/event-stream" }, signal: connection.signal,
    });
    if (!response.ok) {
      const error = new Error(response.status === 404 ? "本地后端缺少流式接口或记录，请重启 CreatorHub 后刷新页面。" : `流式订阅读取失败（${response.status}）`);
      error.retryable = ![401, 403, 404].includes(response.status);
      throw error;
    }
    if (!response.headers.get("Content-Type")?.includes("text/event-stream") || !response.body) {
      const error = new Error("本地接口未返回 SSE 流，请检查后端版本与代理配置。"); error.retryable = false; throw error;
    }
    reader = response.body.getReader();
    const decoder = new TextDecoder("utf-8", { fatal: true });
    while (!signal.aborted) {
      const { value, done } = await reader.read();
      if (done) break;
      arm(); buffer += decoder.decode(value, { stream: true });
      if (buffer.length > 2_000_000) throw new Error("本地流式消息超出大小限制。");
      let boundary;
      while ((boundary = /\r?\n\r?\n/.exec(buffer))) {
        const frame = buffer.slice(0, boundary.index); buffer = buffer.slice(boundary.index + boundary[0].length);
        let kind = "message"; const data = [];
        for (const line of frame.split(/\r?\n/)) {
          if (line.startsWith("event:")) kind = line.slice(6).trim();
          else if (line.startsWith("data:")) data.push(line.slice(5).replace(/^ /, ""));
        }
        if (!data.length) continue;
        const item = JSON.parse(data.join("\n"));
        if (item.id !== id) throw new Error("流式记录编号不匹配，已停止本次订阅。");
        if (kind === "snapshot") {
          if (typeof item.status !== "string") throw new Error("流式状态字段不完整。");
          lastStatus = item.status; onSnapshot(item);
        } else if (kind === "done") {
          if (!lastStatus || ["queued", "running"].includes(lastStatus)) throw new Error("流式连接提前结束。");
          finished = true; return;
        }
      }
    }
    if (!signal.aborted && !finished) throw new Error("流式连接中断，正在重新订阅已有记录；不会重新调用模型。");
  } catch (error) {
    if (timedOut && !signal.aborted) throw new Error("流式连接超时，正在重新订阅已有记录；不会重新调用模型。");
    throw error;
  } finally {
    clearTimeout(timeout); signal.removeEventListener("abort", abort); connection.abort();
    if (reader) { try { await reader.cancel(); } catch {} }
  }
}
