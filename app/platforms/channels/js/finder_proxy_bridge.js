/*
 * Loaded into top-level https://channels.weixin.qq.com windows via CreatorHub's
 * selective loopback proxy (after finder_protocol_browser_bootstrap.js).
 * Opens a same-origin WebSocket back to /__creatorhub_ws and dispatches
 * read-only Finder protocol RPC calls without auto-executing business requests.
 */
(() => {
  'use strict';

  if (globalThis.top !== globalThis.self) {
    return;
  }
  if (globalThis.location?.hostname !== 'channels.weixin.qq.com') {
    return;
  }
  if (globalThis.__creatorhubProxyBridgeActive) {
    return;
  }
  globalThis.__creatorhubProxyBridgeActive = true;

  const CFG = (/*__CREATORHUB_PROXY_CONFIG__*/ null) || {};
  const WS_PATH = typeof CFG.wsPath === 'string' && CFG.wsPath ? CFG.wsPath : '/__creatorhub_ws';
  const TOKEN = typeof CFG.token === 'string' ? CFG.token : '';
  const ALLOWED_METHODS = new Set([
    'fetchCommentsPage',
    'fetchFeedDetail',
    'fetchUserPage',
    'search',
    'fetchFeedShareUrl',
    'initFinder',
  ]);

  const pageId =
    (globalThis.crypto && typeof globalThis.crypto.randomUUID === 'function'
      ? globalThis.crypto.randomUUID()
      : 'page-' + Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 8));

  let socket = null;
  let closed = false;
  let reconnectTimer = null;
  let pollTimer = null;
  let reconnectAttempts = 0;
  let loadState = {
    ready: false,
    origin: '',
    capabilities: {},
    reason: 'initializing',
  };
  let loadingPromise = null;

  function normalizeCapabilities(raw) {
    if (Array.isArray(raw)) {
      const mapped = {};
      for (const item of raw) {
        const key = String(item || '').trim();
        if (key) {
          mapped[key] = true;
        }
      }
      return mapped;
    }
    if (raw && typeof raw === 'object') {
      const mapped = {};
      for (const [key, val] of Object.entries(raw)) {
        const clean = String(key || '').trim();
        if (clean) {
          mapped[clean] = Boolean(val);
        }
      }
      return mapped;
    }
    return {};
  }

  async function ensureProtocolLoaded() {
    if (globalThis.__finderProtocol && typeof globalThis.__finderProtocol.fetchCommentsPage === 'function') {
      loadState = {
        ready: true,
        origin: globalThis.__finderProtocol.origin || 'loaded',
        capabilities: normalizeCapabilities(globalThis.__finderProtocol.capabilities),
        reason: '',
      };
      return loadState;
    }
    if (typeof globalThis.__finderProtocolLoad !== 'function') {
      loadState = {
        ready: false,
        origin: '',
        capabilities: {},
        reason: 'bootstrap_missing',
      };
      return loadState;
    }
    if (!loadingPromise) {
      loadingPromise = (async () => {
        try {
          const res = await globalThis.__finderProtocolLoad();
          if (res && res.ready) {
            loadState = {
              ready: true,
              origin: String(res.origin || ''),
              capabilities: normalizeCapabilities(res.capabilities),
              reason: '',
            };
          } else {
            loadState = {
              ready: false,
              origin: '',
              capabilities: {},
              reason: String((res && res.reason) || 'module_not_ready'),
            };
          }
        } catch (err) {
          loadState = {
            ready: false,
            origin: '',
            capabilities: {},
            reason: err && err.message ? String(err.message) : String(err),
          };
        } finally {
          loadingPromise = null;
        }
        return loadState;
      })();
    }
    return loadingPromise;
  }

  function snapshotMeta() {
    return {
      pageId,
      url: String(globalThis.location?.href || ''),
      title: String(document?.title || ''),
      ready: Boolean(loadState.ready),
      origin: String(loadState.origin || ''),
      reason: String(loadState.reason || ''),
      capabilities: loadState.capabilities || {},
      hasWeixinJSBridge: typeof globalThis.WeixinJSBridge !== 'undefined',
    };
  }

  function sendJson(obj) {
    if (!socket || socket.readyState !== WebSocket.OPEN) {
      return false;
    }
    try {
      socket.send(
        JSON.stringify(obj, (_key, val) => (typeof val === 'bigint' ? val.toString() : val))
      );
      return true;
    } catch {
      return false;
    }
  }

  function buildWsUrl() {
    const query = TOKEN ? '?token=' + encodeURIComponent(TOKEN) : '';
    return 'wss://channels.weixin.qq.com' + WS_PATH + query;
  }

  function scheduleReconnect() {
    if (closed || reconnectTimer !== null) {
      return;
    }
    const delayMs = Math.min(8000, 1200 * Math.max(1, reconnectAttempts));
    reconnectTimer = setTimeout(() => {
      reconnectTimer = null;
      connectSocket();
    }, delayMs);
  }

  async function handleRpcMessage(msg) {
    if (!msg || typeof msg !== 'object') {
      return;
    }
    if (msg.type === 'ping') {
      await ensureProtocolLoaded();
      sendJson({ type: 'pong', ...snapshotMeta() });
      return;
    }
    if (msg.type === 'probe') {
      const state = await ensureProtocolLoaded();
      sendJson({
        type: 'result',
        id: msg.id,
        ok: true,
        result: {
          ...snapshotMeta(),
          ready: Boolean(state.ready),
          origin: state.origin,
          capabilities: state.capabilities,
          reason: state.reason,
        },
      });
      return;
    }
    if (msg.type !== 'call') {
      return;
    }
    const reqId = msg.id;
    const method = String(msg.method || '');
    if (!ALLOWED_METHODS.has(method)) {
      sendJson({ type: 'result', id: reqId, ok: false, error: 'Unsupported Finder RPC method: ' + method });
      return;
    }
    try {
      const state = await ensureProtocolLoaded();
      if (!state.ready || !globalThis.__finderProtocol || typeof globalThis.__finderProtocol[method] !== 'function') {
        throw Error(state.reason || 'Finder protocol runtime is not ready on current page');
      }
      const result = await globalThis.__finderProtocol[method](msg.payload || {});
      sendJson({ type: 'result', id: reqId, ok: true, result });
    } catch (err) {
      sendJson({
        type: 'result',
        id: reqId,
        ok: false,
        error: err && err.message ? String(err.message) : String(err),
      });
    }
  }

  function connectSocket() {
    if (closed || !TOKEN) {
      return;
    }
    if (socket && (socket.readyState === WebSocket.CONNECTING || socket.readyState === WebSocket.OPEN)) {
      return;
    }
    try {
      socket = new WebSocket(buildWsUrl());
    } catch {
      reconnectAttempts += 1;
      scheduleReconnect();
      return;
    }
    socket.addEventListener('open', async () => {
      reconnectAttempts = 0;
      await ensureProtocolLoaded();
      sendJson({ type: 'hello', ...snapshotMeta() });
    });
    socket.addEventListener('message', (event) => {
      try {
        const msg = JSON.parse(String(event.data || '{}'));
        void handleRpcMessage(msg);
      } catch {}
    });
    socket.addEventListener('close', () => {
      socket = null;
      if (!closed) {
        reconnectAttempts += 1;
        scheduleReconnect();
      }
    });
    socket.addEventListener('error', () => {
      // close event handles reconnect scheduling
    });
  }

  function startDiscoveryLoop() {
    let attempts = 0;
    const tick = async () => {
      if (closed) {
        return;
      }
      attempts += 1;
      const beforeReady = loadState.ready;
      const state = await ensureProtocolLoaded();
      if (state.ready && !beforeReady) {
        sendJson({ type: 'hello', ...snapshotMeta() });
      }
      if (!state.ready && attempts < 60) {
        pollTimer = setTimeout(tick, attempts < 10 ? 500 : 1200);
      } else {
        pollTimer = null;
      }
    };
    void tick();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => {
      startDiscoveryLoop();
    }, { once: true });
  } else {
    startDiscoveryLoop();
  }
  globalThis.addEventListener('load', () => {
    void ensureProtocolLoaded().then((state) => {
      if (state.ready) {
        sendJson({ type: 'hello', ...snapshotMeta() });
      }
    });
  }, { once: true });

  const shutdown = () => {
    closed = true;
    if (reconnectTimer !== null) {
      clearTimeout(reconnectTimer);
      reconnectTimer = null;
    }
    if (pollTimer !== null) {
      clearTimeout(pollTimer);
      pollTimer = null;
    }
    if (socket && socket.readyState === WebSocket.OPEN) {
      try {
        sendJson({ type: 'bye', pageId });
        socket.close(1000, 'page unload');
      } catch {}
    }
  };
  globalThis.addEventListener('beforeunload', shutdown);
  globalThis.addEventListener('pagehide', shutdown);

  connectSocket();
})();
