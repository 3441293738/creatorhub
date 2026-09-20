// Offline DOM/HTTP doubles for the first-message Douyin DM flow.
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

async function run() {
  const nodes = new Map();
  const opened = [];
  const requests = [];
  const toasts = [];
  const $ = id => {
    if (!nodes.has(id)) nodes.set(id, {
      innerHTML: '', value: '', placeholder: '', dataset: {}, style: {},
      focus() {},
      classList: { add() {}, remove() {}, toggle() {} },
    });
    return nodes.get(id);
  };
  const context = vm.createContext({
    $, URL, Promise, console,
    document: { querySelectorAll: () => [] },
    ic: () => '', esc: value => String(value), evtBtn: () => null,
    withBusy: async (_button, _label, action) => action(),
    uiPrompt: async () => null,
    toast: (...args) => toasts.push(args),
    setTimeout: callback => { callback(); return 1; },
    api: async (_path, options) => {
      requests.push(JSON.parse(options.body));
      return { id: 91, ran: true };
    },
    refreshDmConvs: async () => {
      vm.runInContext(`DM_CONVS = [{
        conv_id: 'conv-created', peer_uid: '123456', peer_sec_uid: 'MS4wLjAB'
      }]`, context);
    },
    openDmConv: async convId => opened.push(convId),
  });
  vm.runInContext(`
    let PLATFORM = 'douyin', HUB_ACC = '7', DM_CONV = null;
    let DM_CONVS = [], DM_NEW_TARGET = '';
  `, context);
  for (const name of ['normalizeDmTarget', 'startNewDm', 'sendDm']) {
    vm.runInContext(fn(name), context, { filename: `app.js:${name}` });
  }

  assert.equal(context.normalizeDmTarget(' @123456 '), '123456');
  assert.equal(context.normalizeDmTarget('https://www.douyin.com/user/MS4wLjAB?from_tab_name=main'), 'MS4wLjAB');
  assert.equal(context.normalizeDmTarget('https://evil.example/user/MS4wLjAB'), '');
  assert.equal(context.normalizeDmTarget('https://www.douyin.com/video/1'), '');

  context.uiPrompt = async () => 'https://www.douyin.com/user/123456?previous_page=app_code_link';
  await context.startNewDm();
  assert.equal(vm.runInContext('DM_NEW_TARGET', context), '123456');
  assert.match($('dm-thread').innerHTML, /目标 123456/);
  assert.equal($('dm-input').placeholder, '输入第一条私信…');

  $('dm-input').value = '你好';
  await context.sendDm();
  assert.deepEqual(requests[0], {
    account_id: 7, action: 'send_dm', target_uid: '123456',
    target_sec_uid: '', target_nick: '', conv_id: '',
    content: '你好', run_now: true,
  });
  assert.deepEqual(opened, ['conv-created']);
  assert.equal(vm.runInContext('DM_NEW_TARGET', context), '');
  assert.equal($('dm-input').value, '');

  vm.runInContext(`DM_CONV = null; DM_NEW_TARGET = 'MS4wLjAB'; DM_CONVS = []`, context);
  context.refreshDmConvs = async () => {};
  $('dm-input').value = 'sec uid message';
  await context.sendDm();
  assert.equal(requests[1].target_uid, '');
  assert.equal(requests[1].target_sec_uid, 'MS4wLjAB');
  assert.equal(vm.runInContext('DM_NEW_TARGET', context), 'MS4wLjAB');
  assert.equal($('dm-input').placeholder, '输入私信内容…');

  const html = fs.readFileSync('app/web/index.html', 'utf8');
  assert.match(html, /onclick="startNewDm\(\)"/);
  console.log('douyin dm ui: target normalization, payload, and conversation transition passed');
}

run().catch(error => { console.error(error); process.exitCode = 1; });
