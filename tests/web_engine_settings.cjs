// Exercise the shipped configuration controller without a browser or network.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('app/web/engine-settings.js', 'utf8');
const html = fs.readFileSync('app/web/index.html', 'utf8').split('id="engine-settings-form"')[1].split('</form>')[0];
const defaultsSource = fs.readFileSync('preview/demo-api.js', 'utf8').match(/const engineDefaults = (\{[\s\S]*?\});/)[1];
const defaults = vm.runInNewContext('(' + defaultsSource + ')');
const clone = value => JSON.parse(JSON.stringify(value));
function deferred() { let resolve, reject; const promise = new Promise((a, b) => { resolve = a; reject = b; }); return { promise, resolve, reject }; }
function fixture(initial = {}) {
  const nodes = {}, events = {}, calls = [], fields = [];
  const document = { activeElement: null, getElementById: id => nodes[id] };
  function node(props = {}) {
    return Object.assign({ dataset: {}, attrs: {}, children: [], handlers: {}, textContent: '', hidden: false,
      classList: { toggle() {} }, labels: [{ textContent: props.name || 'field' }],
      addEventListener(name, fn) { this.handlers[name] = fn; },
      append(...children) { this.children.push(...children); }, replaceChildren() { this.children = []; },
      focus() { document.activeElement = this; }, closest() { return null; },
    }, props);
  }
  document.createElement = tagName => node({ tagName });
  for (const match of html.matchAll(/<(input|select)\b([^>]+)>/g)) {
    const attrs = Object.fromEntries([...match[2].matchAll(/([\w-]+)="([^"]*)"/g)].map(m => [m[1], m[2]]));
    if (!attrs.name) continue;
    const el = node({ ...attrs, tagName: match[1].toUpperCase(), value: '', checked: false, disabled: true });
    let value = '';
    Object.defineProperty(el, 'value', { get: () => value, set: v => { value = String(v); } });
    if (attrs['data-scale']) el.dataset.scale = attrs['data-scale'];
    if (el.tagName === 'SELECT') {
      const options = html.slice(match.index + match[0].length).split('</select>')[0];
      el.options = [...options.matchAll(/<option value="([^"]*)"/g)].map(m => ({ value: m[1] }));
    }
    const detail = node({ open: false, parentElement: { closest: () => null } });
    el.detail = detail;
    el.closest = () => detail;
    el.parentElement = { querySelector: () => null };
    nodes[el.id] = el; fields.push(el);
  }
  for (const id of ['form', 'status', 'errors', 'save', 'defaults', 'retry']) nodes['engine-settings-' + id] = node();
  const form = nodes['engine-settings-form']; form.querySelectorAll = () => fields;
  const status = nodes['engine-settings-status'], summary = nodes['engine-settings-errors'];
  const state = { values: { ...clone(defaults), ...initial }, defaults: clone(defaults) };
  let handler = async (path, init = {}) => {
    if (init.method === 'PUT') Object.assign(state.values, JSON.parse(init.body));
    return clone(state);
  };
  const window = { addEventListener: (event, fn) => { events[event] = fn; } };
  vm.runInNewContext(source, { window, document, csSyncAll() {},
    setFieldError(el, message) {
      el.error = message;
      if (message) nodes[el.id + '-error'] = node({ textContent: message });
      else delete nodes[el.id + '-error'];
      return !message;
    },
    api: (path, init) => { calls.push({ path, init }); return handler(path, init); },
  });
  return { api: window.CreatorHubEngineSettings, document, state, calls, fields, status, summary, nodes,
    byName: Object.fromEntries(fields.map(el => [el.name, el])),
    handler: fn => { handler = fn; },
    edit(key, value) {
      const el = this.byName[key]; if (el.type === 'checkbox') el.checked = value; else el.value = value;
      form.handlers.input();
    },
    writes: () => calls.filter(call => call.init?.method === 'PUT'),
    protectedUnload() {
      let prevented = false; events.beforeunload({ preventDefault() { prevented = true; } }); return prevented;
    },
  };
}

(async () => {
  const f = fixture();
  assert.equal(f.fields.length, 17);
  assert(f.fields.every(el => el.disabled));
  await f.api.save(); assert.equal(f.calls.length, 0);
  await f.api.load();
  assert(f.fields.every(el => !el.disabled));
  assert.equal(f.byName.work_health_interval_seconds.value, '60');
  assert.equal(f.protectedUnload(), false);
  await f.api.save(); assert.equal(f.writes().length, 0);
  f.edit('xhs_read_mode', 'api'); f.edit('work_health_interval_seconds', '90');
  f.edit('xhs_comment_review_before_publish', false);
  assert(f.api.isDirty() && f.protectedUnload());
  await f.api.load(); assert.equal(f.byName.xhs_read_mode.value, 'api', 'background refresh must preserve draft');
  await f.api.save();
  assert.deepEqual(JSON.parse(f.writes()[0].init.body), {
    xhs_read_mode: 'api', work_health_interval_seconds: 5400, xhs_comment_review_before_publish: false,
  });
  assert(!f.api.isDirty() && !f.protectedUnload());
  f.api.defaults();
  assert.equal(f.writes().length, 1, 'recommended values only fill the draft');
  assert(f.api.isDirty());
  await f.api.save(); assert.equal(f.state.values.xhs_read_mode, 'browser');

  for (const [key, value] of [['comment_recent_days', ''], ['comment_recent_days', '0'],
    ['comment_recent_days', '1.5'], ['xhs_request_jitter', '1.1'], ['request_timeout_seconds', 'NaN'],
    ['xhs_read_mode', 'invalid']]) {
    const invalid = fixture(); await invalid.api.load(); invalid.edit(key, value); await invalid.api.save();
    assert.equal(invalid.writes().length, 0, key);
    assert(invalid.byName[key].error && !invalid.summary.hidden && invalid.api.isDirty(), key);
    assert.equal(invalid.document.activeElement, invalid.summary);
    invalid.summary.children[1].children[0].children[0].handlers.click({ preventDefault() {} });
    assert(invalid.byName[key].detail.open);
    assert.equal(invalid.document.activeElement, invalid.byName[key]);
  }

  const broken = fixture(); broken.handler(async () => { throw Error('fixture outage'); });
  await broken.api.load();
  assert(broken.fields.every(el => el.disabled));
  assert(!broken.nodes['engine-settings-retry'].hidden);
  broken.handler(async () => clone(broken.state)); await broken.api.load();
  assert(broken.fields.every(el => !el.disabled));
  broken.edit('comment_recent_works', '9');
  broken.handler(async () => { throw Error('fixture save error'); }); await broken.api.save();
  assert.equal(broken.byName.comment_recent_works.value, '9');
  assert(broken.api.isDirty() && broken.protectedUnload());
  assert(!broken.nodes['engine-settings-save'].disabled);
  broken.handler(async () => { const error = Error('field error'); error.detail = [{loc: ['body', 'comment_recent_works']}]; throw error; });
  await broken.api.save(); assert(broken.byName.comment_recent_works.error && !broken.summary.hidden);

  const malformed = fixture(); malformed.handler(async () => ({ values: {}, defaults: {} }));
  await malformed.api.load(); assert(malformed.fields.every(el => el.disabled));
  assert.match(malformed.status.textContent, /不完整/);

  const raced = fixture(); await raced.api.load();
  const oldRead = deferred(), newerRead = deferred();
  raced.handler(() => oldRead.promise); const a = raced.api.load();
  raced.handler(() => newerRead.promise); const b = raced.api.load();
  newerRead.resolve({ ...clone(raced.state), values: { ...raced.state.values, comment_recent_works: 8 } }); await b;
  oldRead.resolve(clone(raced.state)); await a;
  assert.equal(raced.byName.comment_recent_works.value, '8', 'older reads cannot replace newer reads');
  const stale = deferred(); raced.handler(() => stale.promise); const staleRead = raced.api.load();
  raced.edit('comment_recent_works', 11);
  const writing = deferred(); raced.handler(() => writing.promise);
  const save = raced.api.save(); await raced.api.save(); await raced.api.load();
  assert.equal(raced.writes().length, 1, 'double-submit and refresh are locked during a write');
  assert(raced.fields.every(el => el.disabled) && raced.protectedUnload());
  writing.resolve({ ...clone(raced.state), values: { ...raced.state.values, comment_recent_works: 11 } }); await save;
  stale.resolve(clone(raced.state)); await staleRead;
  assert.equal(raced.byName.comment_recent_works.value, '11', 'pre-save read cannot undo a successful save');

  const manual = fixture({ comment_recent_works: 1000, work_health_interval_seconds: 962 }); await manual.api.load();
  manual.edit('xhs_read_mode', 'api'); await manual.api.save();
  assert.deepEqual(JSON.parse(manual.writes()[0].init.body), { xhs_read_mode: 'api' }, 'untouched file settings stay untouched');
  console.log('Engine settings: load, partial save, defaults draft, validation, failure retention, races, busy lock and unload protection passed.');
})().catch(error => { console.error(error); process.exitCode = 1; });
