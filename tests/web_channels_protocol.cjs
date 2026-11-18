// Exercise the shipped WeChat Channels visual protocol UI controller without a browser.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const appSource = fs.readFileSync('app/web/app.js', 'utf8');
const startIdx = appSource.indexOf('// ─── 视频号本机微信通道可视化配置 ───');
const endIdx = appSource.indexOf('const QMAP =', startIdx);
const section = appSource.slice(startIdx, endIdx);

function makeNode(id) {
  return {
    id,
    value: '',
    checked: false,
    hidden: false,
    disabled: false,
    placeholder: '',
    textContent: '',
    innerHTML: '',
    dataset: {},
    focused: false,
    focus() { this.focused = true; },
  };
}

function fixture() {
  const ids = [
    'channels-protocol-card', 'ch-proto-badge', 'ch-proto-summary-text',
    'ch-proto-caps', 'ch-proto-probe-box', 'ch-proto-read-mode', 'ch-proto-mode',
    'ch-proto-proxy-wrap', 'ch-proto-proxy-port', 'channels-proxy-panel',
    'ch-proto-endpoint-wrap', 'ch-proto-endpoint', 'ch-proto-discovered',
    'ch-proto-file-wrap', 'ch-proto-endpoint-file', 'ch-proto-file-help',
    'ch-proto-msg',
    'ch-proto-scan-btn', 'ch-proto-probe-btn', 'ch-proto-reset-btn', 'ch-proto-save-btn',
    'ch-proxy-badge', 'ch-proxy-status-text', 'ch-proxy-ca-badge', 'ch-proxy-ca-text',
    'ch-proxy-ca-install-btn', 'ch-proxy-ca-download-btn', 'ch-proxy-ca-uninstall-btn',
    'ch-proxy-pages', 'ch-proxy-manage-sysproxy', 'ch-proxy-start-btn', 'ch-proxy-probe-btn', 'ch-proxy-stop-btn',
    'channels-bridge-panel', 'ch-bridge-badge', 'ch-bridge-status-text', 'ch-bridge-procs',
    'ch-bridge-debug-port', 'ch-bridge-cdp-port', 'ch-bridge-pids',
    'ch-bridge-start-btn', 'ch-bridge-stop-btn',
  ];
  const nodes = Object.fromEntries(ids.map(id => [id, makeNode(id)]));
  nodes['ch-proto-proxy-port'].value = '2023';
  nodes['ch-proxy-manage-sysproxy'].checked = true;
  nodes['ch-bridge-debug-port'].value = '9421';
  nodes['ch-bridge-cdp-port'].value = '9422';

  const calls = [], toasts = [];
  let engineLoads = 0, matrixLoads = 0, switchedTab = '', shownSection = null;
  const routes = new Map();

  const context = vm.createContext({
    $: id => nodes[id] || null,
    esc: s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])),
    jsArg: v => String(JSON.stringify(String(v ?? ''))).replace(/"/g, '&quot;'),
    csSyncAll() {},
    switchTab(tab) { switchedTab = tab; },
    toast(message, type) { toasts.push({ message, type }); },
    withBusy: async (btn, _label, fn) => {
      if (btn) btn.disabled = true;
      try { return await fn(); } finally { if (btn) btn.disabled = false; }
    },
    api: async (path, init) => {
      const body = init?.body ? JSON.parse(init.body) : null;
      const method = (init?.method || 'GET').toUpperCase();
      calls.push({ method, path, body });
      const handler = routes.get(`${method} ${path}`) || routes.get(path);
      if (!handler) throw new Error(`unexpected ${method} ${path}`);
      return handler(body);
    },
    globalThis: {
      CreatorHubWorkbench: {
        isDirty: () => false,
        showSection: (group, sec) => { shownSection = { group, sec }; },
      },
      CreatorHubEngineSettings: { load() { engineLoads++; } },
      CreatorHubTransportMatrix: { load() { matrixLoads++; } },
    },
    navigator: {
      clipboard: { writeText: async text => { context.__copiedText = text; } },
    },
  });
  context.globalThis.CreatorHubChannelsProtocol = undefined;
  vm.runInContext(section, context);
  return {
    ctrl: context.globalThis.CreatorHubChannelsProtocol,
    open: context.openChannelsProtocolSettings,
    nodes, calls, toasts, routes,
    stats: () => ({ engineLoads, matrixLoads, switchedTab, shownSection }),
  };
}

(async () => {
  const f = fixture();
  const baseStatus = {
    platform: 'shipinhao',
    read_mode: 'auto',
    configured: false,
    endpoint_configured: false,
    endpoint_file_configured: false,
    config: {
      read_mode: 'auto', connection_mode: 'none', cdp_endpoint: '', cdp_endpoint_file: '',
      configured: false, endpoint_file_info: null, saved_fields: [],
    },
    bridge: { running: false, wechat_processes: [{ pid: 4321, name: 'Weixin.exe' }] },
    capabilities: {
      account_works: true, keyword_search: false, creator_works: false,
      full_comment_threads: false, encrypted_video_decode: true, share_preview: true,
    },
  };
  f.routes.set('GET /api/channels/protocol/status', () => baseStatus);
  await f.ctrl.load();
  assert.equal(f.nodes['ch-proto-badge'].textContent, '未配置');
  assert.equal(f.nodes['ch-proto-endpoint-wrap'].hidden, true);
  assert.equal(f.nodes['ch-proto-file-wrap'].hidden, true);
  assert(f.nodes['ch-bridge-procs'].innerHTML.includes('Weixin.exe'));

  f.open();
  assert.equal(f.stats().switchedTab, 'settings');
  assert.deepEqual(f.stats().shownSection, { group: 'settings', sec: 'channels' });

  // Discover ports and apply discovered endpoint
  f.routes.set('GET /api/channels/protocol/discover', () => ({
    endpoints: [{
      endpoint: 'http://127.0.0.1:9222',
      reachable: true,
      total_pages: 2,
      has_finder_page: true,
      finder_targets: [{ id: 't1', title: '视频号<script>', path: '/web/pages/feed' }],
    }],
    wechat_processes: [{ pid: 4321, name: 'Weixin.exe' }],
    bridge: { running: false, wechat_processes: [{ pid: 4321, name: 'Weixin.exe' }] },
  }));
  await f.ctrl.discover();
  assert.equal(f.nodes['ch-proto-discovered'].hidden, false);
  assert(f.nodes['ch-proto-discovered'].innerHTML.includes('127.0.0.1:9222'));
  assert(!f.nodes['ch-proto-discovered'].innerHTML.includes('<script>'));

  f.ctrl.fillEndpoint('http://127.0.0.1:9222');
  assert.equal(f.nodes['ch-proto-mode'].value, 'endpoint');
  assert.equal(f.nodes['ch-proto-endpoint'].value, 'http://127.0.0.1:9222');
  assert.equal(f.nodes['ch-proto-endpoint-wrap'].hidden, false);

  // Probe draft before saving
  f.routes.set('POST /api/channels/protocol/probe', body => {
    assert.equal(body.connection_mode, 'endpoint');
    assert.equal(body.cdp_endpoint, 'http://127.0.0.1:9222');
    return {
      ok: true, ready: true, configured: true,
      probe: {
        ok: true, ready: true,
        environment: { host: 'channels.weixin.qq.com', path: '/web/pages/feed', nativeBridge: true, wechatUA: true },
        service: { capabilities: ['finderGetCommentList', 'finderSearch'] },
      },
    };
  });
  await f.ctrl.probe();
  assert.equal(f.nodes['ch-proto-probe-box'].hidden, false);
  assert.equal(f.nodes['ch-proto-probe-box'].dataset.state, 'ready');
  assert(f.nodes['ch-proto-probe-box'].innerHTML.includes('finderGetCommentList'));

  // Save config and ensure engine settings + transport matrix reload
  f.nodes['ch-proto-read-mode'].value = 'protocol';
  f.routes.set('PUT /api/channels/protocol/config', body => {
    assert.equal(body.channels_read_mode, 'protocol');
    assert.equal(body.connection_mode, 'endpoint');
    assert.equal(body.channels_cdp_endpoint, 'http://127.0.0.1:9222');
    return {
      ...baseStatus,
      read_mode: 'protocol',
      configured: true,
      endpoint_configured: true,
      config: { ...baseStatus.config, read_mode: 'protocol', connection_mode: 'endpoint', cdp_endpoint: 'http://127.0.0.1:9222', configured: true },
      capabilities: { ...baseStatus.capabilities, keyword_search: true, creator_works: true, full_comment_threads: true },
    };
  });
  await f.ctrl.save();
  assert.equal(f.nodes['ch-proto-msg'].dataset.state, 'success');
  assert.equal(f.stats().engineLoads, 1);
  assert.equal(f.stats().matrixLoads, 1);

  // Bridge start & stop
  f.nodes['ch-bridge-pids'].value = 'bad_pid';
  await f.ctrl.startBridge();
  assert.equal(f.nodes['ch-proto-msg'].dataset.state, 'error');

  f.nodes['ch-bridge-pids'].value = '4321';
  f.routes.set('POST /api/channels/protocol/bridge/start', body => {
    assert.deepEqual(body.allowed_pids, [4321]);
    return {
      ...baseStatus,
      configured: true,
      endpoint_file_configured: true,
      config: { ...baseStatus.config, connection_mode: 'endpoint_file', cdp_endpoint_file: './data/channels_wmpf_endpoint.json', configured: true },
      bridge: { running: true, debug_listening_port: 9421, cdp_listening_port: 9422, native_connected: false, commands_forwarded: 0, results_forwarded: 0, wechat_processes: [{ pid: 4321, name: 'Weixin.exe' }] },
      capabilities: { ...baseStatus.capabilities, keyword_search: true },
    };
  });
  await f.ctrl.startBridge();
  assert.equal(f.nodes['ch-bridge-badge'].textContent, '运行中');
  assert.equal(f.nodes['ch-bridge-start-btn'].disabled, true);
  assert.equal(f.nodes['ch-bridge-stop-btn'].disabled, false);
  assert.equal(f.nodes['ch-proto-mode'].value, 'endpoint_file');

  f.routes.set('POST /api/channels/protocol/bridge/stop', () => baseStatus);
  await f.ctrl.stopBridge();
  assert.equal(f.nodes['ch-bridge-badge'].textContent, '未启动');
  assert.equal(f.nodes['ch-bridge-stop-btn'].disabled, true);

  // Reset
  f.routes.set('DELETE /api/channels/protocol/config', () => baseStatus);
  await f.ctrl.reset();
  assert.equal(f.nodes['ch-proto-mode'].value, 'none');

  // Built-in local proxy workflow (CA install, proxy start/stop, connected pages rendering, uninstall)
  f.nodes['ch-proto-mode'].value = 'proxy';
  f.ctrl.onModeChange();
  assert.equal(f.nodes['ch-proto-proxy-wrap'].hidden, false);
  assert.equal(f.nodes['channels-proxy-panel'].open, true);

  const proxyInstalledStatus = {
    ...baseStatus,
    proxy: {
      running: false,
      state: 'stopped',
      proxy_port: 2023,
      ca: { exists: true, trusted: true, thumbprint: 'AABBCCDDEEFF', store: 'CurrentUser\\Root' },
      pages: [],
      connected_pages: 0,
      ready_pages: 0,
    },
  };
  f.routes.set('POST /api/channels/protocol/proxy/cert/install', () => ({
    ok: true,
    message: '本地 CA 根证书已安装并受信任',
    proxy: proxyInstalledStatus.proxy,
  }));
  await f.ctrl.installProxyCert();
  assert.equal(f.nodes['ch-proxy-ca-badge'].textContent, '根证书已受信任');
  assert.equal(f.nodes['ch-proxy-ca-install-btn'].disabled, true);
  assert.equal(f.nodes['ch-proxy-ca-uninstall-btn'].disabled, false);

  f.routes.set('POST /api/channels/protocol/proxy/start', body => {
    assert.equal(body.proxy_port, 2023);
    assert.equal(body.enable_system_proxy, true);
    return {
      ...baseStatus,
      configured: true,
      proxy_configured: true,
      config: { ...baseStatus.config, connection_mode: 'proxy', proxy_enabled: true, proxy_port: 2023, configured: true },
      proxy: {
        running: true,
        state: 'ready',
        proxy_port: 2023,
        system_proxy_managed: true,
        system_proxy: { supported: true, proxy_enable: true, proxy_server: '127.0.0.1:2023' },
        ca: proxyInstalledStatus.proxy.ca,
        connected_pages: 1,
        ready_pages: 1,
        pages: [{ page_id: 'p1', title: '微信视频号<script>', url: 'https://channels.weixin.qq.com/web/pages/feed', ready: true }],
        commands_forwarded: 3,
      },
      capabilities: { ...baseStatus.capabilities, keyword_search: true, creator_works: true, full_comment_threads: true },
    };
  });
  await f.ctrl.startProxy();
  assert.equal(f.nodes['ch-proto-mode'].value, 'proxy');
  assert.equal(f.nodes['ch-proxy-badge'].textContent, '运行中 · 页面就绪');
  assert.equal(f.nodes['ch-proxy-start-btn'].disabled, true);
  assert.equal(f.nodes['ch-proxy-stop-btn'].disabled, false);
  assert(f.nodes['ch-proxy-pages'].innerHTML.includes('channels.weixin.qq.com/web/pages/feed'));
  assert(f.nodes['ch-proxy-pages'].innerHTML.includes('copyPageUrl'));
  assert(!f.nodes['ch-proxy-pages'].innerHTML.includes('<script>'));
  assert.equal(await f.ctrl.copyPageUrl('https://channels.weixin.qq.com/web/pages/profile?username=v2_test@finder'), true);

  // One-click probe button always probes proxy mode explicitly
  f.nodes['ch-proto-mode'].value = 'endpoint_file';
  f.routes.set('POST /api/channels/protocol/probe', body => {
    assert.equal(body.connection_mode, 'proxy');
    return {
      ok: true, ready: true, configured: true,
      probe: { ok: true, ready: true, transport: 'channels-local-proxy', url: 'https://channels.weixin.qq.com/web/pages/feed', connected_pages: 1 },
    };
  });
  await f.ctrl.probe('proxy');
  assert.equal(f.nodes['ch-proto-probe-box'].dataset.state, 'ready');
  assert(f.nodes['ch-proto-probe-box'].innerHTML.includes('channels.weixin.qq.com/web/pages/feed'));

  f.routes.set('POST /api/channels/protocol/proxy/stop', () => proxyInstalledStatus);
  await f.ctrl.stopProxy();
  assert.equal(f.nodes['ch-proxy-badge'].textContent, '未启动');
  assert.equal(f.nodes['ch-proxy-start-btn'].disabled, false);
  assert.equal(f.nodes['ch-proxy-stop-btn'].disabled, true);

  f.routes.set('POST /api/channels/protocol/proxy/cert/uninstall', () => ({
    ok: true,
    message: '已从当前用户受信任根证书卸载',
    proxy: { running: false, ca: { exists: true, trusted: false, thumbprint: '', store: '' }, pages: [] },
  }));
  await f.ctrl.uninstallProxyCert();
  assert.equal(f.nodes['ch-proxy-ca-uninstall-btn'].disabled, true);
  assert.equal(f.nodes['ch-proxy-ca-install-btn'].disabled, false);

  // Verify finder_proxy_bridge.js top-window guard & allowlisted RPC dispatch
  const bridgeSrc = fs.readFileSync('app/platforms/channels/js/finder_proxy_bridge.js', 'utf8')
    .replace('/*__CREATORHUB_PROXY_CONFIG__*/ null', JSON.stringify({ wsPath: '/__creatorhub_ws', token: 'tok123' }));
  const sentMessages = [];
  let wsHandlers = {};
  class FakeWS {
    static CONNECTING = 0;
    static OPEN = 1;
    constructor(url) {
      this.url = url;
      this.readyState = FakeWS.OPEN;
    }
    addEventListener(ev, fn) { wsHandlers[ev] = wsHandlers[ev] || []; wsHandlers[ev].push(fn); }
    send(raw) { sentMessages.push(JSON.parse(raw)); }
    close() { this.readyState = 3; }
  }
  const bridgeCtx = vm.createContext({
    WebSocket: FakeWS,
    setTimeout, clearTimeout,
    document: { readyState: 'complete', title: '视频号首页', addEventListener() {} },
  });
  bridgeCtx.globalThis = bridgeCtx;
  bridgeCtx.self = bridgeCtx;
  bridgeCtx.top = bridgeCtx;
  bridgeCtx.location = { hostname: 'channels.weixin.qq.com', href: 'https://channels.weixin.qq.com/web/pages/feed' };
  bridgeCtx.addEventListener = () => {};
  bridgeCtx.WeixinJSBridge = { invoke() {} };
  bridgeCtx.__finderProtocolLoad = async () => {
    bridgeCtx.__finderProtocol = {
      origin: 'test',
      capabilities: { search: true },
      fetchCommentsPage: async () => ({}),
      search: async ({ query }) => ({ errCode: 0, query }),
    };
    return { ready: true, origin: 'test', capabilities: { search: true } };
  };
  vm.runInContext(bridgeSrc, bridgeCtx);
  for (const fn of wsHandlers.open || []) await fn();
  await new Promise(r => setTimeout(r, 10));
  assert.equal(sentMessages[0].type, 'hello');
  assert.equal(sentMessages[0].ready, true);
  for (const fn of wsHandlers.message || []) {
    await fn({ data: JSON.stringify({ type: 'call', id: 'c1', method: 'search', payload: { query: 'test' } }) });
    await fn({ data: JSON.stringify({ type: 'call', id: 'c2', method: 'evilMethod', payload: {} }) });
  }
  await new Promise(r => setTimeout(r, 10));
  const c1 = sentMessages.find(m => m.id === 'c1');
  const c2 = sentMessages.find(m => m.id === 'c2');
  assert.equal(c1.ok, true);
  assert.equal(c1.result.query, 'test');
  assert.equal(c2.ok, false);
  assert(c2.error.includes('Unsupported'));

  console.log('Channels protocol visual controller tests passed.');
})().catch(error => { console.error(error); process.exitCode = 1; });
