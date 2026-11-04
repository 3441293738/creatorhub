// Exercise the shipped proxy controls without a browser or live credentials.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('app/web/app.js', 'utf8');
const section = source.slice(source.indexOf('// ─── 代理池 ───'), source.indexOf('async function testProxy(id)'));
const html = fs.readFileSync('app/web/index.html', 'utf8');
const formats = ['auto', 'host_colon', 'host_csv', 'host_at', 'auth_colon', 'auth_at', 'host_hash'];
const select = html.split('id="px-format"')[1].split('</select>')[0];
assert.deepEqual([...select.matchAll(/<option value="([^"]+)"/g)].map(m => m[1]), formats);
assert(html.includes('for="px-format"') && select.includes('aria-describedby="px-input-hint"'));
const addressInput = html.match(/<input\b[^>]*id="px-url"[^>]*>/)[0];
assert(addressInput.includes('placeholder="例：用户名:密码@主机:端口"'));
assert(!addressInput.includes('上述'));
assert(addressInput.includes('aria-describedby="px-input-hint"'));
const examples = html.split('id="px-format-examples"')[1].split('</ul>')[0];
const labels = [...select.matchAll(/<option value="[^"]+">([^<]+)<\/option>/g)].map(m => m[1]).slice(1);
assert.deepEqual([...examples.matchAll(/<code>([^<]+)<\/code>/g)].map(m => m[1]), labels);
const helpTag = html.match(/<details\b[^>]*id="px-format-help"[^>]*>/)[0];
assert(!/\bopen(?:\s|=|>)/.test(helpTag), 'help must start collapsed, not as a permanent instruction block');
assert(helpTag.includes('ontoggle="syncProxyFormatHelp(this)"'));
assert(html.includes('<summary id="px-format-toggle" aria-controls="px-format-panel">格式示例</summary>'));
assert(!html.includes('<div class="proxy-format-help">'));
const addressField = html.split('<div class="form-field proxy-address-field">')[1].split('</details>')[0];
assert(addressField.includes('for="px-url"') && addressField.includes('id="px-format-help"'), 'keep the help trigger beside the address label');
assert(html.includes('class="form-grid proxy-input-grid"'));
assert(html.includes('minmax(280px,2fr)'), 'the address gets more width than short configuration fields');
assert(html.includes('.form-grid.proxy-input-grid { grid-template-columns:minmax(0,1fr); }'), 'narrow screens stack fields');

function fixture() {
  const nodes = Object.fromEntries(['px-url', 'px-proto', 'px-format', 'px-label'].map(id => [id, { value: '' }]));
  const listeners = new Map();
  nodes['px-format-toggle'] = { focused: false, focus() { this.focused = true; } };
  nodes['px-format-panel'] = {};
  nodes['px-format-help'] = { open: false, contains(target) {
    return [this, nodes['px-format-toggle'], nodes['px-format-panel']].includes(target);
  } };
  nodes['px-proto'].value = 'socks5'; nodes['px-format'].value = 'auto';
  const calls = [], messages = [], button = { disabled: false, textContent: '测试' };
  let result = {}, prompt = null, promptResult = '';
  const context = vm.createContext({
    document: {
      addEventListener(type, handler, capture) { assert.equal(capture, true); listeners.set(type, handler); },
      removeEventListener(type, handler, capture) { assert.equal(capture, true); if (listeners.get(type) === handler) listeners.delete(type); },
    },
    $: id => nodes[id], event: { target: { closest: () => button } },
    api: async (path, init) => { calls.push({ path, body: init?.body ? JSON.parse(init.body) : null }); return result; },
    toast: (message, type) => messages.push({ message, type }),
    uiPrompt: async options => { prompt = options; return promptResult; },
  });
  vm.runInContext(section, context);
  return { nodes, calls, messages, button, context, listeners,
    dispatch(type, data = {}) {
      const event = { type, target: {}, defaultPrevented: false, propagationStopped: false,
        preventDefault() { this.defaultPrevented = true; }, stopPropagation() { this.propagationStopped = true; }, ...data };
      listeners.get(type)?.(event);
      return event;
    },
    run: script => vm.runInContext(script, context),
    result: value => { result = value; },
    promptResult: value => { promptResult = value; },
    prompt: () => prompt,
  };
}

(async () => {
  const f = fixture();
  const help = f.nodes['px-format-help'];
  assert.equal(help.open, false);
  help.open = true;
  f.run('syncProxyFormatHelp($("px-format-help"))');
  assert.equal(f.listeners.size, 3);
  f.dispatch('pointerdown', { target: f.nodes['px-format-panel'] });
  assert(help.open, 'reading examples should not close help');
  f.dispatch('keydown', { key: 'Enter' });
  f.dispatch('keydown', { key: 'Escape', isComposing: true });
  assert(help.open);
  const escape = f.dispatch('keydown', { key: 'Escape' });
  assert(!help.open && escape.defaultPrevented && escape.propagationStopped);
  assert(f.nodes['px-format-toggle'].focused, 'Escape returns focus to the trigger');
  f.run('syncProxyFormatHelp($("px-format-help"))');
  assert.equal(f.listeners.size, 0, 'closed help must release global listeners');
  for (const type of ['pointerdown', 'focusin']) {
    help.open = true;
    f.run('syncProxyFormatHelp($("px-format-help"))');
    f.dispatch(type, { target: f.nodes['px-url'] });
    assert(!help.open, 'clicking or tabbing back to the form dismisses examples');
    f.run('syncProxyFormatHelp($("px-format-help"))');
  }
  const raw = 'proxy.example:8080@USER:1234';
  f.nodes['px-url'].value = raw; f.nodes['px-format'].value = 'host_at';
  await f.run('addProxy()');
  assert.deepEqual(f.calls[0].body, { url: raw, label: '', geo: null,
    default_scheme: 'socks5', input_format: 'host_at' });
  assert.equal(f.nodes['px-url'].value, '');

  f.nodes['px-url'].value = raw;
  const canonical = 'socks5://USER:1234@proxy.example:8080';
  f.result({ ok: true, scheme: 'socks5', auth: 'required', browser_ok: true,
    recommend: canonical, geo: { country: 'US' }, geo_text: '' });
  await f.run('detectProxy()');
  assert.equal(f.calls.at(-1).body.input_format, 'host_at');
  assert.equal(f.nodes['px-url'].value, canonical);
  assert.equal(f.nodes['px-format'].value, 'auto', 'canonical URL must not keep an export-only format');
  await f.run('addProxy()');
  assert.equal(f.calls.at(-1).body.url, canonical);
  assert.equal(f.calls.at(-1).body.input_format, 'auto');
  assert.deepEqual(f.calls.at(-1).body.geo, { country: 'US' });
  assert.equal(f.button.disabled, false);

  f.nodes['px-format'].value = 'host_csv';
  f.promptResult('proxy.example,8080,USER,TOKEN');
  f.result({ added: 1, skipped: 0, invalid: 0 });
  await f.run('importProxies()');
  assert.deepEqual(f.calls.at(-1).body, { text: 'proxy.example,8080,USER,TOKEN',
    default_scheme: 'socks5', input_format: 'host_csv' });
  assert(f.prompt().hint.includes('SOCKS5'));

  f.result({ ok: false, detail: '两个检测站点均超时' });
  await f.run('testPoolProxy(1)');
  assert(f.messages.at(-1).message.startsWith('通用检测未通过'));
  assert.equal(f.button.disabled, false);

  f.result({ added: 0, skipped: 0, invalid: 1, invalid_samples: ['第 1 行：代理格式有歧义'] });
  await f.run('importProxies()');
  assert(f.messages.at(-1).message.includes('第 1 行'));
  console.log('proxy input UI regressions passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
