// Local DOM/HTTP doubles: no browser process and no network connection.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('app/web/app.js', 'utf8');

function fn(name) {
  const match = new RegExp(`^(?:async )?function ${name}\\(`, 'm').exec(source);
  assert.ok(match, `missing function ${name}`);
  const afterFirstLine = source.indexOf('\n', match.index) + 1;
  const firstLine = source.slice(match.index, afterFirstLine);
  if (firstLine.trimEnd().endsWith('}')) return firstLine;
  const end = /^}\r?$/m.exec(source.slice(afterFirstLine));
  assert.ok(end, `missing closing brace for ${name}`);
  return source.slice(match.index, afterFirstLine + end.index + 1);
}

function fixture() {
  const nodes = new Map(), pending = [], clicks = [], timers = [];
  const $ = id => {
    if (!nodes.has(id)) nodes.set(id, {
      innerHTML: '', textContent: '', value: '', style: {}, dataset: {},
      classList: { add() {}, remove() {} },
      setAttribute() {}, removeAttribute() {},
    });
    return nodes.get(id);
  };
  const context = vm.createContext({
    $, URL, URLSearchParams, Promise, console,
    document: { hidden: false, activeElement: null, querySelector: () => null, querySelectorAll: () => [] },
    api: path => new Promise((resolve, reject) => pending.push({ path, resolve, reject })),
    ic: () => '', fmtNum: n => String(n || 0), fmtTime: () => '',
    openWork: (...args) => clicks.push(['work', ...args]),
    openWorkComments: (...args) => clicks.push(['comments', ...args]),
    monitorOwnWorkDanmaku: (...args) => clicks.push(['danmaku', ...args]),
    hubGridEmpty: value => value, empty: (_cols, text) => text,
    noteCard: row => row.desc, monitorById: () => null,
    contentTimeCell: () => '', contentStatusLabel: value => value, contentPathCell: () => '',
    contentSourceMarkup: () => '', contentCapturedTime: () => '', populateContentSrc() {},
    updateContentSelBar() {}, renderContentPager() {},
    setTimeout: (callback, delay) => { timers.push({ callback, delay }); return timers.length; },
    clearTimeout() {},
  });
  vm.runInContext(`
    let PLATFORM = 'douyin', HUB_ACC = '1', DM_CONV = null;
    let CURRENT_TAB = 'overview', INFLIGHT = 0, _apiFailures = 0;
    const VIEW_REQUESTS = new Map(); let VIEW_SERIAL = 0;
    let CONTENT_PAGE = 1, CONTENT_PAGE_SIZE = 20;
    let CONTENT_SRC = '', CONTENT_GROUP = '', CONTENT_TAG = '', CONTENTS = [];
    const CONTENT_SOURCE_CACHE = new Map();
    let CONTENT_RENDER_SCOPE = '';
    const selContent = new Set();
  `, context);
  for (const name of ['beginViewRequest', 'apiErrorMessage', 'scheduleToApi', 'localDateTimeValue',
    'safeMediaUrl', 'jsArg', 'esc', 'workCard', 'refreshMyWorks', 'contentCaptureBounds', 'refreshContents', 'openDmConv']) {
    vm.runInContext(fn(name), context, { filename: `app.js:${name}` });
  }
  return { context, $, pending, clicks, timers, run: code => vm.runInContext(code, context) };
}

async function run() {
  const f = fixture();
  const tricky = `Bob's \\video "quote" <img src=x onerror=alert(1)> &quot;\n第二行`;
  f.context.work = { id: 3, platform: 'douyin', account_id: 4, item_id: tricky, desc: tricky,
    cover_url: 'https://example.invalid/a.jpg?x=" onerror="alert(1)' };
  const card = f.run('workCard(work)');
  const decode = value => value.replace(/&(?:quot|amp|lt|gt);/g,
    entity => ({ '&quot;': '"', '&amp;': '&', '&lt;': '<', '&gt;': '>' })[entity]);
  for (const [, handler] of card.matchAll(/onclick="([^"]*)"/g)) {
    f.run(decode(handler)); // Attribute entities decode once before JS is parsed.
  }
  assert.deepEqual(f.clicks.find(row => row[0] === 'comments'), ['comments', 3, 'douyin', tricky]);
  assert.deepEqual(f.clicks.find(row => row[0] === 'danmaku'), ['danmaku', tricky, 4]);
  assert.ok(f.clicks.filter(row => row[0] === 'work').every(row => row[2] === tricky));
  assert.ok(!card.includes('<img src=x'));
  assert.ok(!card.includes(' onerror="alert(1)'));
  assert.equal(f.run('safeMediaUrl("javascript:alert(1)")'), '');
  assert.equal(f.run('safeMediaUrl("data:text/html,payload")'), '');
  assert.equal(f.run('safeMediaUrl("/fixture.jpg")'), 'http://localhost/fixture.jpg');
  assert.match(f.run('apiErrorMessage([{loc:["body","scheduled_at"],msg:"invalid"}],422)'), /scheduled_at: invalid/);

  const expected = process.env.TZ === 'Asia/Shanghai' ? '2026-09-07T10:30:00.000Z' : '2026-09-07T22:30:00.000Z';
  assert.equal(f.run('scheduleToApi("2026-09-07T18:30")'), expected);
  f.context.expected = expected;
  assert.equal(f.run('localDateTimeValue(expected)'), '2026-09-07T18:30');
  assert.equal(f.run('scheduleToApi("")'), null);
  assert.throws(() => f.run('scheduleToApi("not a date")'));
  assert.throws(() => f.run('scheduleToApi("2026-02-30T18:30")'));
  if (process.env.TZ === 'America/New_York') assert.throws(() => f.run('scheduleToApi("2026-03-08T02:30")'));

  // An old platform result must not replace the new platform's cards.
  const old = f.run('refreshContents()');
  f.run('PLATFORM = "xhs"; VIEW_REQUESTS.clear()');
  const current = f.run('refreshContents()');
  f.pending[1].resolve({ items: [{ desc: 'new XHS result' }], total: 1 });
  await current;
  f.pending[0].resolve({ items: [{ desc: 'stale DY result' }], total: 1 });
  await old;
  assert.equal(f.$('content-cards').innerHTML, 'new XHS result');
  assert.equal(f.$('content-table-wrap').style.display, 'none');

  // Repeated filters on the same platform are also ordered by request serial.
  const filterOld = f.run('refreshContents()'), filterNew = f.run('refreshContents()');
  f.pending[3].resolve({ items: [{ desc: 'latest filter' }], total: 1 });
  await filterNew;
  f.pending[2].resolve({ items: [{ desc: 'old filter' }], total: 1 });
  await filterOld;
  assert.equal(f.$('content-cards').innerHTML, 'latest filter');

  // A -> B -> A must not make an old request current again.
  f.run('globalThis.previous = beginViewRequest("fixture")');
  f.run('PLATFORM = "douyin"; VIEW_REQUESTS.clear(); PLATFORM = "xhs"; VIEW_REQUESTS.clear()');
  assert.equal(f.run('previous()'), false);

  const accountOld = f.run('refreshMyWorks()');
  f.run('HUB_ACC = "2"');
  const accountNew = f.run('refreshMyWorks()');
  f.pending[5].resolve([{ id: 9, desc: 'new account', platform: 'xhs' }]);
  await accountNew;
  f.pending[4].reject(new Error('stale account error'));
  await accountOld;
  assert.ok(f.$('mw-grid').innerHTML.includes('new account'));
  assert.ok(!f.$('mw-grid').innerHTML.includes('stale account'));

  // History returning late must not mark a conversation read on another account.
  let marked = false;
  f.context.markDmRead = () => { marked = true; };
  f.context.refreshDmMessages = async () => {};
  const conversation = f.run('openDmConv("fixture/conv")');
  assert.ok(f.pending[6].path.includes('fixture%2Fconv'));
  f.run('HUB_ACC = "3"; DM_CONV = null');
  f.pending[6].resolve({});
  await conversation;
  assert.equal(marked, false);

  const calls = [];
  const refreshers = ['refreshOverviewSummary', 'refreshOverviewChart', 'refreshAccounts',
    'refreshMonitors', 'refreshContents', 'refreshWatches', 'refreshComments',
    'refreshDanmakuWatches', 'refreshDanmaku', 'refreshCommentRules', 'refreshCommentTasks',
    'refreshPublish', 'refreshCollections', 'refreshRiskCenter', 'refreshTaskQueue',
    'refreshHubSummary', 'refreshTaskQueueBadge'];
  refreshers.forEach(name => { f.context[name] = async () => { calls.push(name); }; });
  f.run('let POLL_RUNNING = false, POLL_TIMER = null, POLL_DELAY = 8000;');
  f.run(fn('loop'));
  await f.run('loop()');
  assert.deepEqual(calls.sort(), ['refreshOverviewChart', 'refreshOverviewSummary', 'refreshTaskQueueBadge']);
  calls.length = 0;
  f.context.document.hidden = true;
  await f.run('loop()');
  assert.equal(calls.length, 0);
  f.context.document.hidden = false;
  f.context.document.activeElement = { matches: () => true };
  await f.run('loop()');
  assert.deepEqual(calls, ['refreshTaskQueueBadge']);
  calls.length = 0;
  f.context.document.activeElement = null;
  let complete;
  f.context.refreshOverviewSummary = () => new Promise(resolve => { complete = resolve; });
  const polling = f.run('loop()');
  await Promise.resolve();
  await f.run('loop()');
  assert.equal(calls.filter(name => name === 'refreshOverviewChart').length, 1);
  complete();
  await polling;
  f.context.refreshOverviewSummary = async () => { throw new Error('offline'); };
  await f.run('loop()');
  assert.equal(f.run('POLL_DELAY'), 16000);
  for (let i = 0; i < 4; i++) await f.run('loop()');
  assert.equal(f.run('POLL_DELAY'), 60000);

  // Normal pacing has a truthful deadline, not an abnormal account label.
  f.context.PF_NAME = { douyin: '抖音', xhs: '小红书' };
  for (const name of ['riskDate', 'riskTime', 'riskDuration', 'riskRemaining', 'riskPlatformLabel', 'autoRunHint', 'riskNumber', 'saveRiskConfig',
    'riskAccountWait', 'renderRiskAccounts', 'renderRiskSummary', 'fillRiskConfig', 'approveAllDrafts']) {
    f.run(fn(name));
  }
  const now = Date.now();
  assert.equal(f.run('riskDuration(8)'), '8秒');
  assert.equal(f.run('riskDuration(65)'), '1分钟5秒');
  assert.equal(f.run('autoRunHint(null)'), '');
  assert.match(f.run('autoRunHint("2000-01-01T00:00:00Z")'), /已到期，等待调度/);
  assert.match(f.run('autoRunHint("2099-01-01T00:00:00Z")'), /下次最早/);
  f.context.account = { account_id: 1, status: 'normal', status_label: '正常',
    status_tone: 'success', operation_not_before: new Date(now + 20000).toISOString(),
    session_rest_until: new Date(now + 240000).toISOString() };
  const wait = f.run('riskAccountWait(account)');
  assert.equal(wait.label, '连续操作休息');
  assert.equal(wait.until, f.context.account.session_rest_until);
  assert.equal(wait.waiting, true);
  f.run('let RISK_ACCOUNTS = [account]; renderRiskAccounts()');
  assert.match(f.$('risk-account-table').innerHTML, /连续操作休息/);
  assert.match(f.$('risk-account-table').innerHTML, /risk-status success/);
  assert.match(f.$('risk-account-table').innerHTML, /probeRiskAccount\(1\)" disabled/);
  f.context.account = { account_id: 1, status: 'verification_required', status_label: '待人工验证',
    status_tone: 'danger', manual_review_required: true };
  f.run('RISK_ACCOUNTS = [account]; renderRiskAccounts()');
  assert.match(f.$('risk-account-table').innerHTML, /等待人工验证/);
  assert.match(f.$('risk-account-table').innerHTML, /probeRiskAccount\(1\)" disabled/);
  assert.match(f.$('risk-account-table').innerHTML, /openAccountBrowser\(1\)/);
  assert.match(f.$('risk-account-table').innerHTML, /clearRiskAccount\(1\)/);
  assert.equal(f.run('riskAccountWait({status:"normal",operation_not_before:"2000-01-01T00:00:00Z"}).waiting'), false);
  f.run('renderRiskSummary({counts:{normal:2, verification_required:1, network_backoff:1}})');
  assert.equal(f.$('risk-stat-invalid').textContent, 1);
  assert.equal(f.$('risk-stat-cooldown').textContent, 1);
  f.run('fillRiskConfig({risk_control:{operation_gap_min_seconds:8, operation_gap_max_seconds:25, session_operation_limit:12, session_rest_min_seconds:180, session_rest_max_seconds:480}})');
  for (const [id, expected] of [['risk-operation-min', 8], ['risk-operation-max', 25],
    ['risk-session-limit', 12], ['risk-rest-min', 180], ['risk-rest-max', 480]]) {
    assert.equal(f.$(id).value, expected);
    assert.ok(fs.readFileSync('app/web/index.html', 'utf8').includes(`id="${id}"`));
  }

  // The form shows percentages but submits bounded ratios, never rounded to 0.
  const markup = fs.readFileSync('app/web/index.html', 'utf8');
  for (const [tag] of markup.matchAll(/<input\b[^>]*\bid="risk-[^"]+"[^>]*>/g)) {
    if (!tag.includes('type="number"')) continue;
    const field = f.$(/id="([^"]+)"/.exec(tag)[1]);
    field.min = /min="([^"]+)"/.exec(tag)?.[1] || '0';
    field.max = /max="([^"]+)"/.exec(tag)?.[1] || '';
    field.value = field.min;
  }
  const scanField = f.$('risk-scan-jitter');
  let focused = false, invalid;
  scanField.focus = () => { focused = true; };
  scanField.setAttribute = (key, value) => { if (key === 'aria-invalid') invalid = value; };
  scanField.removeAttribute = () => { invalid = null; };
  scanField.value = '';
  assert.throws(() => f.run('riskNumber("risk-scan-jitter")'));
  assert.equal(focused, true); assert.equal(invalid, 'true');
  scanField.value = '101';
  assert.throws(() => f.run('riskNumber("risk-scan-jitter")'));
  scanField.value = '25';
  assert.equal(f.run('riskNumber("risk-scan-jitter")'), 25);
  assert.equal(invalid, null);
  f.$('risk-comment-jitter').value = '40'; f.$('risk-dm-poll-jitter').value = '35';
  f.$('risk-initial-spread').value = '60'; f.$('risk-retry-jitter').value = '30';
  f.$('risk-cooldown-steps').value = '30, 120'; f.$('risk-mode').value = 'conservative';
  f.$('risk-active-start').value = '8'; f.$('risk-active-end').value = '24';
  f.context.btnLoading = () => () => {}; f.context.evtBtn = () => null;
  f.context._barSync = () => {}; f.context.toast = () => {};
  let submittedConfig;
  f.context.api = async (_path, options) => {
    submittedConfig = JSON.parse(options.body); return submittedConfig;
  };
  await f.run('saveRiskConfig()');
  assert.equal(submittedConfig.schedule.scan_jitter, 0.25);
  assert.equal(submittedConfig.schedule.comment_jitter, 0.4);
  assert.equal(submittedConfig.schedule.xhs_dm_poll_jitter, 0.35);
  assert.equal(submittedConfig.schedule.initial_scan_spread_seconds, 60);
  assert.equal(submittedConfig.risk_control.network_retry_jitter_seconds, 30);
  assert.equal(f.$('risk-scan-jitter').value, 25);

  // Approval captures explicit task IDs and platform before the modal awaits.
  let confirmApproval, approvedBody;
  f.context.uiConfirm = () => new Promise(resolve => { confirmApproval = resolve; });
  f.context.api = async (_path, options) => { approvedBody = JSON.parse(options.body); return { approved: 1 }; };
  f.context.toast = () => {};
  f.run('let AC_TASKS = [{id:5,status:"draft"}]; PLATFORM = "douyin";');
  const approving = f.run('approveAllDrafts()');
  f.run('PLATFORM = "xhs"; AC_TASKS = [{id:6,status:"draft"}]');
  confirmApproval(true);
  await approving;
  assert.deepEqual(approvedBody, { ids: [5], platform: 'douyin' });
  approvedBody = null;
  f.run('AC_TASKS = []');
  await f.run('approveAllDrafts()');
  assert.equal(approvedBody, null);
  // Real form handlers preserve uploads and typed edits across slow/failing requests.
  const g = fixture(), requests = [], messages = [];
  for (const name of ['publishFormPayload', 'addPublish', 'sendDm', 'actFollow']) {
    vm.runInContext(fn(name), g.context);
  }
  g.run('let PUB_SUBMITTING = false, PUB_UPLOAD_CACHE = null, HUB_TAB = "following", DM_CONVS = [];');
  g.context.PF_NAME = { douyin: '抖音' };
  g.context.evtBtn = () => null;
  g.context.withBusy = async (_btn, _text, callback) => callback();
  g.context.FormData = class { append() {} };
  g.context.dtSyncAll = () => {};
  g.context.refreshPublish = () => {};
  g.context.toast = (message, type) => messages.push({ message, type });
  let clearCount = 0;
  g.context.pubFilesClear = () => { clearCount++; g.$('pub-files').files = []; };
  g.$('pub-acc').value = '1'; g.$('pub-type').value = 'images';
  g.$('pub-title').value = 'original'; g.$('pub-desc').value = 'first draft';
  g.$('pub-visibility').value = 'public'; g.$('pub-allowsave').value = '1';
  const originalFile = { name: 'fixture.jpg' };
  g.$('pub-files').files = [originalFile];
  let fail = true;
  g.context.api = async (path, options) => {
    requests.push({ path, options });
    if (path.endsWith('/upload')) return { files: [{ path: 'uploaded-fixture.jpg' }] };
    if (fail) { fail = false; throw new Error('response lost'); }
    return { id: 1, replayed: true };
  };
  await Promise.all([g.run('addPublish()'), g.run('addPublish()')]);
  assert.equal(requests.length, 2, 'double click uploads/creates only once');
  assert.equal(clearCount, 0); assert.equal(g.$('pub-title').value, 'original');
  await g.run('addPublish()');
  assert.equal(requests.filter(row => row.path.endsWith('/upload')).length, 1, 'retry keeps the original upload paths');
  assert.equal(requests[1].options.body, requests[2].options.body);
  assert.equal(clearCount, 1);
  let uploaded;
  g.$('pub-files').files = [{ name: 'second.jpg' }];
  g.$('pub-title').value = 'snapshot';
  g.context.api = async (path, options) => {
    if (path.endsWith('/upload')) return new Promise(resolve => { uploaded = resolve; });
    requests.push({ path, options }); return { id: 2 };
  };
  const uploading = g.run('addPublish()');
  g.$('pub-title').value = 'typed during upload';
  uploaded({ files: [{ path: 'second-upload.jpg' }] });
  await uploading;
  assert.equal(JSON.parse(requests.at(-1).options.body).title, 'snapshot');
  assert.equal(g.$('pub-title').value, 'typed during upload');
  assert.equal(clearCount, 1, 'late result does not erase a newer draft');

  let finishDm, historyRefreshes = 0;
  g.run('HUB_ACC = "1"; DM_CONV = "first"; DM_CONVS = [{conv_id:"first",peer_uid:"peer"}];');
  g.$('dm-input').value = 'first message';
  g.context.api = async (path, options) => {
    requests.push({ path, options }); return new Promise(resolve => { finishDm = resolve; });
  };
  g.context.setTimeout = callback => callback();
  g.context.openDmConv = async () => { historyRefreshes++; };
  const dm = g.run('sendDm()');
  g.run('HUB_ACC = "2"; DM_CONV = "second";');
  g.$('dm-input').value = 'new conversation draft';
  finishDm({ id: 5, status: 'pending', ran: false, execution_error: 'waiting' });
  await dm;
  assert.equal(g.$('dm-input').value, 'new conversation draft');
  assert.equal(historyRefreshes, 0);
  assert.equal(JSON.parse(requests.at(-1).options.body).account_id, 1);
  assert.equal(messages.at(-1).type, 'info', 'queued is not shown as sent');

  let confirmFollow, writes = 0;
  g.run('HUB_ACC = "1";');
  g.context.api = async (path) => {
    if (path.startsWith('/api/follows?')) return [{ id: 8, uid: 'peer', nickname: 'fixture' }];
    writes++; return {};
  };
  g.context.uiConfirm = () => new Promise(resolve => { confirmFollow = resolve; });
  g.context.refreshFollows = () => {};
  const follow = g.run('actFollow("follow", 8)');
  await new Promise(setImmediate);
  g.run('HUB_ACC = "2";');
  confirmFollow(true);
  await follow;
  assert.equal(writes, 0, 'switching account while confirming cancels the old action');

  // Partial API reads and refreshed access hints must not appear as total
  // failure, nor imply that API mode opened a browser.
  const monitor = fixture(), monitorToasts = [];
  monitor.context.toast = (message, type) => monitorToasts.push({ message, type });
  monitor.context.evtBtn = () => null;
  monitor.context.withBusy = async (_button, _label, action) => action();
  monitor.context.refreshMonitors = monitor.context.refreshContents = () => {};
  vm.runInContext(fn('runNow'), monitor.context);
  for (const [result, tone, message] of [
    [{ partial: true, captured: 2, new: 3, failed: 1, error: 'code=-510000' }, 'info', '部分完成'],
    [{ partial: false, captured: 0, failed: 3, error: '连续 3 条笔记详情未返回' }, 'err', '抓取未成功'],
    [{ ok: true, skipped: true, reason: '本轮等待' }, 'info', '本轮等待'],
    [{ ok: true, new: 0, refreshed: 1 }, 'info', '手动重试'],
    [{ ok: true, new: 2, scanned: 3, filtered: 1 }, 'ok', '抓取完成'],
  ]) {
    monitor.context.api = async () => result;
    await monitor.run('runNow(11)');
    assert.equal(monitorToasts.at(-1).type, tone);
    assert.ok(monitorToasts.at(-1).message.includes(message));
    assert.ok(!monitorToasts.at(-2).message.includes('开浏览器'));
  }

  console.log(`UI regression checks passed (${process.env.TZ})`);
}

run().catch(error => { console.error(error); process.exitCode = 1; });
