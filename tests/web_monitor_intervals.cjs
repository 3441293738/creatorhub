// Exercise the shipped helpers with a minimal DOM, without a browser/network.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('app/web/app.js', 'utf8');
function fn(name) {
  const match = new RegExp(`^(?:async )?function ${name}\\(`, 'm').exec(source);
  assert.ok(match, name);
  const next = source.indexOf('\n', match.index) + 1;
  if (source.slice(match.index, next).trimEnd().endsWith('}')) return source.slice(match.index, next);
  const end = /^}\r?$/m.exec(source.slice(next));
  return source.slice(match.index, next + end.index + 1);
}
function fixture(seconds = 300, allowGlobal = false) {
  const nodes = new Map();
  const $ = id => {
    if (!nodes.has(id)) nodes.set(id, {
      id, dataset: {}, handlers: {}, hidden: false, disabled: false, error: '', _value: '',
      get value() { return this._value; }, set value(value) { this._value = String(value); },
      set innerHTML(html) {
        this.options = [...html.matchAll(/<option value="([^"]+)"( selected)?>([^<]+)<\/option>/g)]
          .map(([, value, selected, text]) => ({value, selected: !!selected, text}));
        this.value = (this.options.find(o => o.selected) || this.options[0])?.value || '';
      },
      insertAdjacentHTML(_where, html) {
        for (const [, child] of html.matchAll(/id="([^"]+)"/g)) $(child);
        $(id + '-unit').value = '1';
      },
      addEventListener(type, callback) { this.handlers[type] = callback; },
      change(value) { this.value = value; this.handlers.change?.(); },
      input(value) { this.value = value; this.handlers.input?.(); },
      focus() { this.focused = true; },
    });
    return nodes.get(id);
  };
  const context = vm.createContext({$, setFieldError: (node, message) => { node.error = message; }});
  for (const name of ['esc', 'uiEditorError', 'monitorIntervalText', 'monitorIntervalOptions',
    'setupMonitorInterval', 'monitorIntervalSeconds']) vm.runInContext(fn(name), context);
  context.setupMonitorInterval('interval', seconds, allowGlobal);
  return {context, $, select: $('interval'), amount: $('interval-amount'), unit: $('interval-unit'),
    custom: $('interval-custom'), seconds: () => context.monitorIntervalSeconds('interval')};
}

const f = fixture();
for (const [seconds, text] of [[1, '1 秒'], [30, '30 秒'], [60, '1 分钟'], [77, '1 分钟 17 秒'],
  [90, '1 分钟 30 秒'], [3605, '1 小时 5 秒'], [86400, '1 天']]) {
  assert.equal(f.context.monitorIntervalText(seconds), text);
}
for (const value of [0, -1, NaN, Infinity]) assert.equal(f.context.monitorIntervalText(value), '—');
for (const [seconds, global] of [[300, false], [600, false], [0, true]]) {
  const d = fixture(seconds, global);
  assert.equal(d.select.value, String(seconds));
  assert.equal(d.seconds(), seconds);
  assert.equal(d.custom.hidden, true);
  assert.equal(d.amount.disabled, true);
  assert.equal(d.unit.disabled, true);
}
assert.deepEqual(f.select.options.map(o => o.value),
  ['1','5','10','15','30','60','300','600','1800','3600','21600','86400','custom']);

// Loading old arbitrary intervals must preserve the exact stored seconds.
for (const seconds of [7, 77, 90, 120, 86399]) {
  const d = fixture(seconds);
  assert.equal(d.select.value, 'custom');
  assert.equal(d.custom.hidden, false);
  assert.equal(d.seconds(), seconds);
  d.unit.change('60');
  assert.equal(d.seconds(), seconds);
  d.unit.change('1');
  assert.equal(d.seconds(), seconds);
}
f.select.change('custom');
assert.equal(f.amount.disabled, false);
assert.equal(f.unit.disabled, false);
f.amount.input('1.5'); // Custom minutes; exact 90-second payload.
assert.equal(f.seconds(), 90);
f.unit.change('1');
assert.equal(f.amount.value, '90');
assert.equal(f.amount.step, '1');
f.unit.change('60');
assert.equal(f.amount.value, '1.5');
assert.equal(f.amount.max, '1440');
assert.equal(f.amount.step, 'any');
f.unit.change('1');
for (const value of ['', ' ', '0', '-1', '86401', '1.5', 'NaN', 'Infinity']) {
  f.amount.input(value);
  assert.throws(f.seconds, /1–86400 秒/);
  assert.equal(f.amount.focused, true);
  assert.ok(f.amount.error);
  f.amount.input('37');
  assert.equal(f.amount.error, '');
  assert.equal(f.seconds(), 37);
}
f.unit.change('60');
f.amount.input('0.01');
assert.throws(f.seconds, /整秒/);
f.amount.input(String(1 / 60));
assert.equal(f.seconds(), 1);
f.amount.input('1440');
assert.equal(f.seconds(), 86400);
f.amount.input('invalid');
f.select.change('5'); // Invalid hidden drafts must never block preset submission.
assert.equal(f.seconds(), 5);
assert.equal(f.amount.disabled, true);
assert.equal(f.amount.error, '');

const global = fixture(0, true);
global.select.change('custom'); global.amount.input('0');
assert.throws(global.seconds, /1–86400 秒/);
global.select.change('0'); assert.equal(global.seconds(), 0);
f.select.change('0'); assert.throws(f.seconds, /1–86400 秒/);

// Platform draft restoration assigns fields without dispatching change events.
f.select.value = 'custom'; f.unit.value = '1'; f.amount.value = '77';
f.select._monitorIntervalSync();
assert.equal(f.custom.hidden, false);
f.unit.change('60'); assert.equal(f.seconds(), 77);
for (const [name, id] of [['addMonitor','t-interval'], ['addWatch','w-interval'],
  ['addDanmakuWatch','d-w-interval'], ['editMonitor','em-interval'],
  ['editWatchMeta','ew-interval'], ['editDanmakuWatch','edw-interval']]) {
  assert.ok(fn(name).includes(`interval_seconds: monitorIntervalSeconds("${id}")`), name);
}
for (const name of ['monRow','watchRow','danmakuWatchRow']) {
  assert.ok(fn(name).includes('monitorIntervalText('), name);
  assert.ok(!fn(name).includes('interval_seconds / 60'), name);
}
console.log('monitor presets, custom input, units, exact seconds, validation and legacy values passed');
