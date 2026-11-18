const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const source = fs.readFileSync('app/web/app.js', 'utf8');

function fn(name) {
  const match = new RegExp(`^(?:async )?function ${name}\\(`, 'm').exec(source);
  assert.ok(match, `missing function ${name}`);
  const after = source.indexOf('\n', match.index) + 1;
  const end = /^}\r?$/m.exec(source.slice(after));
  return end ? source.slice(match.index, after + end.index + 1) : source.slice(match.index);
}

function fixture() {
  const nodes = new Map();
  const apiCalls = [];
  const toasts = [];
  const $ = id => {
    if (!nodes.has(id)) {
      nodes.set(id, {
        value: '',
        textContent: '',
        className: '',
        disabled: false,
        required: false,
        innerHTML: '',
        isConnected: true,
        style: {},
        attrs: {},
        options: [],
        setAttribute(k, v) { this.attrs[k] = v; },
        removeAttribute(k) { delete this.attrs[k]; },
      });
    }
    return nodes.get(id);
  };

  const routes = new Map();
  const context = vm.createContext({
    $,
    Promise,
    console,
    Date,
    document: { querySelector: () => null },
    PF_NAME: { shipinhao: '视频号', douyin: '抖音', xhs: '小红书', kuaishou: '快手', tiktok: 'TikTok' },
    QMAP: { highest: '最高画质' },
    ACCOUNTS: [{ id: 1, platform: 'douyin', nickname: '测试抖音号', status: 'active', has_storage: true, has_creator: false }],
    esc: s => String(s ?? ''),
    ic: name => `[${name}]`,
    metaChips: () => '',
    monitorIntervalText: () => '5 分钟',
    monitorStrategySummary: () => '深度 10',
    safeMediaUrl: u => u || '',
    autoRunHint: () => '',
    setFieldError: () => {},
    rebuildCollectionSources: () => {},
    toast: (msg, type) => toasts.push({ msg, type }),
    api: async (path, init) => {
      const body = init?.body ? JSON.parse(init.body) : null;
      apiCalls.push({ path, body });
      const handler = routes.get(path);
      if (!handler) throw new Error(`unexpected api ${path}`);
      return handler(body);
    },
  });
  vm.runInContext(`
    let PLATFORM = 'shipinhao';
    let _LAST_MONITOR_PLATFORM = '';
    const SMART_TARGET_PARSE_SEQ = Object.create(null);
  `, context);
  for (const name of [
    'smartTargetPlatformName', 'setSmartTargetFeedback', 'resetSmartTargetState',
    'isChannelsWorkUrl', 'isChannelsCreatorId', 'normalizePlatformShareTarget',
    'resetMonitorTargetState', 'normalizeMonitorTarget', 'applyMonitorForm',
    'resetCollectionCreatorState', 'normalizeCollectionCreatorTarget',
    'resetWatchTargetState', 'normalizeWatchTarget',
    'accOptions', 'populateAccountSelect',
    'collectionSourceValue', 'collectionRequiresAccount',
    'populateCollectionAccount', 'applyCollectionForm',
    'populateWatchAccount', 'monRow',
  ]) {
    vm.runInContext(fn(name), context);
  }
  return { context, $, apiCalls, toasts, routes, run: code => vm.runInContext(code, context) };
}

function testShipinhaoDefaultsAndNoAccountUi(f) {
  // Monitor form: shipinhao hides #t-acc-wrap, shows #t-backfill-wrap with default "5"
  f.run("PLATFORM = 'shipinhao'; _LAST_MONITOR_PLATFORM = '';");
  f.run('applyMonitorForm(); populateAccountSelect(); populateWatchAccount();');
  assert.equal(f.$('t-acc-wrap').style.display, 'none');
  assert.equal(f.$('t-acc').value, '');
  assert.equal(f.$('w-acc-wrap').style.display, 'none');
  assert.equal(f.$('w-acc').value, '');
  assert.equal(f.$('t-backfill-wrap').style.display, '');
  assert.equal(f.$('t-backfill').value, '5');

  // Collection form: keyword & creator hide #col-account-wrap; account_works shows #col-account-wrap
  f.$('col-source').value = 'keyword';
  f.run('applyCollectionForm();');
  assert.equal(f.run("collectionRequiresAccount('shipinhao', 'keyword')"), false);
  assert.equal(f.$('col-account-wrap').style.display, 'none');
  assert.equal(f.$('col-account').required, false);

  f.$('col-source').value = 'creator';
  f.run('applyCollectionForm();');
  assert.equal(f.run("collectionRequiresAccount('shipinhao', 'creator')"), false);
  assert.equal(f.$('col-account-wrap').style.display, 'none');

  f.$('col-source').value = 'account_works';
  f.run('applyCollectionForm();');
  assert.equal(f.run("collectionRequiresAccount('shipinhao', 'account_works')"), true);
  assert.equal(f.$('col-account-wrap').style.display, '');
  assert.equal(f.$('col-account').required, true);

  // Monitor row for shipinhao renders local WeChat channel label instead of red "未绑定账号"
  const sphRow = f.run("monRow({ id: 8, platform: 'shipinhao', nickname: '老郑说AI前瞻', account_id: null, interval_seconds: 300, max_scrolls: 12, max_items_per_scan: 5, video_quality: 'highest', enabled: true })");
  assert.match(sphRow, /通道：本机微信客户端/);
  assert.doesNotMatch(sphRow, /未绑定账号/);

  // Non-shipinhao (douyin) still restores #t-acc-wrap and resets #t-backfill default to ""
  f.run("PLATFORM = 'douyin'; applyMonitorForm(); populateAccountSelect(); populateWatchAccount();");
  assert.equal(f.$('t-acc-wrap').style.display, '');
  assert.equal(f.$('t-acc').value, '1');
  assert.equal(f.$('w-acc-wrap').style.display, '');
  assert.equal(f.$('t-backfill').value, '');
  const dyRow = f.run("monRow({ id: 9, platform: 'douyin', nickname: '抖音测试', account_id: null, interval_seconds: 300, max_scrolls: 10, max_items_per_scan: 5, video_quality: 'highest', enabled: true })");
  assert.match(dyRow, /未绑定账号/);
}

async function run() {
  const f = fixture();
  f.routes.set('/api/share-download/links', body => {
    if (body.share_text.includes('weixin.qq.com/sph/')) {
      return { links: [{ platform: 'wechat', url: 'https://weixin.qq.com/sph/Amv88hJDUj' }] };
    }
    return { links: [] };
  });
  f.routes.set('/api/channels/resolve-creator', () => ({
    ok: true,
    username: 'v2_creator_999@finder',
    nickname: '户外测评君',
  }));

  // 1) Paste (quiet: true) on monitor target extracts URL locally without network reverse lookup
  f.$('t-url').value = '快来看视频！https://weixin.qq.com/sph/Amv88hJDUj 推荐';
  const pasted = await f.run('normalizeMonitorTarget(null, { quiet: true })');
  assert.equal(pasted, 'https://weixin.qq.com/sph/Amv88hJDUj');
  assert.equal(f.$('t-url').value, 'https://weixin.qq.com/sph/Amv88hJDUj');
  assert.equal(f.apiCalls.length, 1);
  assert.equal(f.apiCalls[0].path, '/api/share-download/links');
  assert.match(f.$('t-url-feedback').textContent, /反查作者/);

  // 2) Explicit click (quiet: false) on monitor target resolves v2_...@finder via backend
  const clicked = await f.run('normalizeMonitorTarget({ innerHTML: "识别", isConnected: true }, { quiet: false })');
  assert.equal(clicked, 'v2_creator_999@finder');
  assert.equal(f.$('t-url').value, 'v2_creator_999@finder');
  assert.equal(f.apiCalls[1].path, '/api/share-download/links');
  assert.equal(f.apiCalls[2].path, '/api/channels/resolve-creator');
  assert.match(f.$('t-url-feedback').textContent, /户外测评君.*v2_creator_999@finder/);

  // 3) Collection creator input (quiet: false) also resolves video share URL to creator ID
  f.$('col-source-ref').value = 'https://weixin.qq.com/sph/Amv88hJDUj';
  const colResolved = await f.run('normalizeCollectionCreatorTarget({ innerHTML: "识别", isConnected: true })');
  assert.equal(colResolved, 'v2_creator_999@finder');
  assert.equal(f.$('col-source-ref').value, 'v2_creator_999@finder');

  // 4) Comment watch: auto keeps video URL, user resolves creator ID
  f.$('w-kind').value = 'auto';
  f.$('w-url').value = 'https://weixin.qq.com/sph/Amv88hJDUj';
  const callsBeforeAuto = f.apiCalls.length;
  const watchAuto = await f.run('normalizeWatchTarget({ innerHTML: "识别", isConnected: true })');
  assert.equal(watchAuto, 'https://weixin.qq.com/sph/Amv88hJDUj');
  assert.equal(f.apiCalls.length, callsBeforeAuto + 1); // only /api/share-download/links

  f.$('w-kind').value = 'user';
  const watchUser = await f.run('normalizeWatchTarget({ innerHTML: "识别", isConnected: true })');
  assert.equal(watchUser, 'v2_creator_999@finder');
  assert.equal(f.$('w-url').value, 'v2_creator_999@finder');

  // 5) Direct v2_...@finder is recognized cleanly without link error toast
  f.$('col-source-ref').value = 'v2_060000231003b20faec8@finder';
  const direct = await f.run('normalizeCollectionCreatorTarget({ innerHTML: "识别", isConnected: true })');
  assert.equal(direct, 'v2_060000231003b20faec8@finder');
  assert.match(f.$('col-source-ref-feedback').textContent, /创作者标识已识别/);

  // 6) UI form & row behavior for shipinhao defaults & hidden account selectors
  testShipinhaoDefaultsAndNoAccountUi(f);

  console.log('Channels creator reverse-lookup and UI default tests passed.');
}

run().catch(error => { console.error(error); process.exitCode = 1; });
