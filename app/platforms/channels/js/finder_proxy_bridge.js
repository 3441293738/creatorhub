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

  function hasNativeJsBridge() {
    return (
      typeof globalThis.WeixinJSBridge !== 'undefined' ||
      typeof globalThis.IlinkJSBridge !== 'undefined'
    );
  }

  function updateBridgePageUi() {
    if (typeof document?.getElementById !== 'function') {
      return;
    }
    const badgeEl = document.getElementById('creatorhub-bridge-badge');
    const detailEl = document.getElementById('creatorhub-bridge-detail');
    if (!badgeEl && !detailEl) {
      return;
    }
    const wsConnected = Boolean(socket && socket.readyState === WebSocket.OPEN);
    const hasBridge = hasNativeJsBridge();
    if (wsConnected && loadState.ready) {
      if (badgeEl) {
        badgeEl.textContent = '✓ 已连接 CreatorHub · 协议就绪';
        badgeEl.dataset.state = 'ready';
      }
      if (detailEl) {
        detailEl.textContent =
          '当前微信内置页面已完成原生通道桥接（' +
          (loadState.origin || 'WeixinJSBridge') +
          '）。请保持此窗口在后台开启（可最小化），现在可回到 CreatorHub 使用搜索、主页采集、二级评论与视频解密。';
      }
    } else if (!hasBridge) {
      if (badgeEl) {
        badgeEl.textContent = '等待微信 WeixinJSBridge 环境…';
        badgeEl.dataset.state = 'warn';
      }
      if (detailEl) {
        detailEl.textContent =
          '若您是在 Chrome / Edge 等外部浏览器打开了本页，请复制上方链接发送到电脑微信「文件传输助手」，并在微信聊天窗口内点击打开。';
      }
    } else {
      if (badgeEl) {
        badgeEl.textContent = wsConnected ? '正在加载视频号协议服务…' : '正在连接本机 CreatorHub 代理…';
        badgeEl.dataset.state = 'pending';
      }
      if (detailEl && loadState.reason && loadState.reason !== 'initializing') {
        detailEl.textContent = '状态：' + loadState.reason;
      }
    }
  }

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
      updateBridgePageUi();
      return loadState;
    }
    if (typeof globalThis.__finderProtocolLoad !== 'function') {
      loadState = {
        ready: false,
        origin: '',
        capabilities: {},
        reason: 'bootstrap_missing',
      };
      updateBridgePageUi();
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
          updateBridgePageUi();
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
      hasWeixinJSBridge: hasNativeJsBridge(),
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
    const delayMs = Math.min(3500, 900 * Math.max(1, reconnectAttempts));
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
      updateBridgePageUi();
    });
    socket.addEventListener('message', (event) => {
      try {
        const msg = JSON.parse(String(event.data || '{}'));
        void handleRpcMessage(msg);
      } catch {}
    });
    socket.addEventListener('close', () => {
      socket = null;
      updateBridgePageUi();
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

  const onJsBridgeReady = () => {
    void ensureProtocolLoaded().then(() => {
      sendJson({ type: 'hello', ...snapshotMeta() });
      updateBridgePageUi();
    });
  };
  if (typeof document?.addEventListener === 'function') {
    document.addEventListener('WeixinJSBridgeReady', onJsBridgeReady);
    document.addEventListener('IlinkJSBridgeReady', onJsBridgeReady);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => {
      updateBridgePageUi();
      startDiscoveryLoop();
    }, { once: true });
  } else {
    updateBridgePageUi();
    startDiscoveryLoop();
  }
  globalThis.addEventListener('load', () => {
    void ensureProtocolLoaded().then((state) => {
      if (state.ready) {
        sendJson({ type: 'hello', ...snapshotMeta() });
      }
      updateBridgePageUi();
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
