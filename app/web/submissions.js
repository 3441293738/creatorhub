// One UUID per unconfirmed logical submission; only hashes/UUIDs enter storage.
(() => {
  const pending = new Map(), keys = new Map();
  function supports(path, options) {
    return (options.method || "GET").toUpperCase() === "POST" && typeof options.body === "string" &&
      (/^\/api\/(publish|account-actions|comment-rules|collections|dm\/auto-reply-rules)$/.test(path) ||
       /^\/api\/contents\/\d+\/repost-(xhs|douyin|shipinhao)$/.test(path));
  }
  async function fingerprint(value) {
    const bytes = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(value));
    return Array.from(new Uint8Array(bytes), b => b.toString(16).padStart(2, "0")).join("");
  }
  function newKey() {
    return Array.from(crypto.getRandomValues(new Uint8Array(24)), b => b.toString(16).padStart(2, "0")).join("");
  }
  window.CreatorHubSubmissions = {
    async run(path, options, send) {
      if (!supports(path, options)) return send(options);
      const signature = path + "\n" + options.body;
      if (pending.has(signature)) return pending.get(signature);
      const task = (async () => {
        const storageKey = "creatorhub-submission:" + await fingerprint(signature);
        let key = keys.get(storageKey);
        try { key = key || sessionStorage.getItem(storageKey); } catch (_) {}
        if (!key) key = newKey();
        keys.set(storageKey, key);
        try { sessionStorage.setItem(storageKey, key); } catch (_) {}
        const headers = new Headers(options.headers || {});
        if (!headers.has("Idempotency-Key")) headers.set("Idempotency-Key", key);
        const clear = () => {
          keys.delete(storageKey);
          try { sessionStorage.removeItem(storageKey); } catch (_) {}
        };
        try {
          // The caller parses JSON before success is confirmed. Truncated/lost
          // responses retain the key, just like network failures and HTTP 5xx.
          const result = await send({ ...options, headers });
          clear();
          return result;
        } catch (e) {
          if ([400, 404, 410, 413, 422].includes(e.status)) clear();
          throw e;
        }
      })();
      pending.set(signature, task);
      try { return await task; }
      finally { pending.delete(signature); }
    },
  };
})();
