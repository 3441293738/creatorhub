// Actual pre-paint module, evaluated without a browser or network.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('app/web/appearance.js', 'utf8');
function fixture({ stored = null, dark = false, readError = false, writeError = false, matchMedia = true, legacy = false } = {}) {
  const listeners = {}, docListeners = {}, themeButtons = [], radios = [], densities = [];
  const node = (value, data = {}) => ({ value, dataset: data, attrs: {}, handlers: {}, checked: false,
    setAttribute(name, value) { this.attrs[name] = value; }, addEventListener(name, fn) { this.handlers[name] = fn; } });
  ['light', 'dark', 'system'].forEach(value => { themeButtons.push(node(value, { themeChoice: value })); radios.push(node(value)); });
  ['comfortable', 'compact'].forEach(value => densities.push(node(value)));
  const root = { dataset: {}, style: {} }, status = {}, motion = node(''), meta = {};
  let mediaListener, writes = 0;
  const media = { matches: dark };
  media[legacy ? 'addListener' : 'addEventListener'] = (...args) => { mediaListener = args.at(-1); };
  const storage = { value: stored, getItem() { if (readError) throw Error('disabled'); return this.value; },
    setItem(key, value) { if (writeError) throw Error('quota'); assert.equal(key, 'creatorhub-appearance'); writes++; this.value = value; } };
  const document = { documentElement: root, readyState: 'loading',
    querySelector: () => meta,
    querySelectorAll: selector => selector === '[data-theme-choice]' ? themeButtons : selector.includes('appearance-theme') ? radios : densities,
    getElementById: id => id === 'appearance-motion' ? motion : status,
    addEventListener: (name, fn) => { docListeners[name] = fn; },
  };
  const window = { addEventListener: (name, fn) => { listeners[name] = fn; }, dispatchEvent() {} };
  if (matchMedia) window.matchMedia = () => media;
  vm.runInNewContext(source, { window, document, localStorage: storage, CustomEvent: class { constructor(type, init) { this.type = type; this.detail = init.detail; } } });
  docListeners.DOMContentLoaded();
  return { window, root, status, motion, meta, radios, densities, themeButtons, storage,
    api: window.CreatorHubAppearance, writes: () => writes,
    system(value) { media.matches = value; mediaListener(); },
    storageEvent(key, value) { storage.value = value; listeners.storage({ key }); } };
}

const normal = fixture();
assert.equal(normal.root.dataset.theme, 'light');
assert.equal(normal.root.dataset.themeMode, 'system');
assert.equal(normal.writes(), 0, 'initialization does not overwrite preferences');
normal.system(true);
assert.equal(normal.root.dataset.theme, 'dark');
normal.themeButtons[0].handlers.click();
assert.equal(normal.root.dataset.theme, 'light');
assert.equal(normal.root.style.colorScheme, 'light');
assert.equal(normal.themeButtons[0].attrs['aria-pressed'], 'true');
assert.equal(normal.radios[0].checked, true);
normal.system(false); normal.system(true);
assert.equal(normal.root.dataset.theme, 'light', 'manual preference wins over OS');
normal.densities[1].checked = true; normal.densities[1].handlers.change();
normal.motion.checked = true; normal.motion.handlers.change();
assert.equal(normal.root.dataset.density, 'compact');
assert.equal(normal.root.dataset.motion, 'reduced');
const restored = fixture({ stored: normal.storage.value, dark: true });
assert.equal(restored.root.dataset.theme, 'light');
assert.equal(restored.root.dataset.density, 'compact');
assert.equal(restored.motion.checked, true);
const copy = restored.api.get(); copy.theme = 'dark';
assert.equal(restored.api.get().theme, 'light', 'API exposes a copy');
normal.storageEvent('unrelated-key', JSON.stringify({ theme: 'dark' }));
assert.equal(normal.root.dataset.theme, 'light');
normal.storageEvent('creatorhub-appearance', JSON.stringify({ theme: 'dark' }));
assert.equal(normal.root.dataset.theme, 'dark');
normal.storageEvent(null, null);
assert.equal(normal.root.dataset.themeMode, 'system');
assert.equal(normal.root.dataset.density, 'comfortable');
assert.equal(fixture({ stored: '{broken', dark: true }).root.dataset.theme, 'dark');
assert.equal(fixture({ stored: '"light"' }).root.dataset.themeMode, 'system');
assert.equal(fixture({ stored: '{"theme":"bad","density":"bad","motion":"bad"}' }).root.dataset.density, 'comfortable');
assert.equal(fixture({ readError: true }).root.dataset.theme, 'light');
assert.equal(fixture({ matchMedia: false }).root.dataset.theme, 'light');
const denied = fixture({ writeError: true });
denied.api.set({ theme: 'dark' });
assert.equal(denied.root.dataset.theme, 'dark');
assert.equal(denied.api.get().saved, false);
assert.match(denied.status.textContent, /未保存/);
const oldWebview = fixture({ legacy: true }); oldWebview.system(true);
assert.equal(oldWebview.root.dataset.theme, 'dark');
console.log('Appearance: pre-paint, persistence, system/manual, cross-tab, controls, density, motion and storage fallbacks passed.');
