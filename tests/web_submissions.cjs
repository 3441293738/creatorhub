// Execute the real access/submission scripts with DOM, storage and fetch doubles.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const { webcrypto } = require('node:crypto');
const { setImmediate: tick } = require('node:timers/promises');
const submissionSource = fs.readFileSync('app/web/submissions.js', 'utf8');

function storageFixture(data = new Map()) {
  return { data, getItem: key => data.get(key) || null,
    setItem: (key, value) => data.set(key, value), removeItem: key => data.delete(key) };
}
function submissions(storage = storageFixture()) {
  const window = {};
  const context = vm.createContext({ window, crypto: webcrypto, TextEncoder, Uint8Array,
    Headers, Map, Promise, sessionStorage: storage });
  vm.runInContext(submissionSource, context);
  return { run: window.CreatorHubSubmissions.run, storage };
}
async function testSubmissions() {
  const f = submissions(), keys = [];
  const options = { method: 'POST', body: JSON.stringify({ account_id: 1, content: 'PRIVATE_TEXT' }) };
  let release;
  const sending = f.run('/api/account-actions', options, async opts => {
    keys.push(opts.headers.get('Idempotency-Key'));
    await new Promise(resolve => { release = resolve; });
    return { id: 7 };
  });
  const duplicate = f.run('/api/account-actions', options, () => { throw new Error('duplicate dispatch'); });
  while (!release) await tick();
  assert.equal(f.storage.data.size, 1);
  assert.ok(!JSON.stringify([...f.storage.data]).includes('PRIVATE_TEXT'));
  release();
  assert.equal((await sending).id, (await duplicate).id);
  assert.equal(keys.length, 1); assert.equal(f.storage.data.size, 0);
  await f.run('/api/account-actions', options, async opts => { keys.push(opts.headers.get('Idempotency-Key')); return {}; });
  assert.notEqual(keys[0], keys[1], 'intentional new submission has a new key');

  let originalKey;
  await assert.rejects(f.run('/api/publish', options, async opts => {
    originalKey = opts.headers.get('Idempotency-Key'); throw new Error('response lost');
  }), /response lost/);
  const restarted = submissions(f.storage);
  await assert.rejects(restarted.run('/api/publish', options, async opts => {
    assert.equal(opts.headers.get('Idempotency-Key'), originalKey);
    throw new SyntaxError('truncated JSON');
  }), /truncated JSON/);
  await restarted.run('/api/publish', options, async opts => {
    assert.equal(opts.headers.get('Idempotency-Key'), originalKey); return { id: 8, replayed: true };
  });
  assert.equal(f.storage.data.size, 0);

  for (const status of [400, 404, 410, 413, 422]) {
    await assert.rejects(f.run('/api/publish', options, async () => { throw Object.assign(new Error('invalid'), { status }); }));
    assert.equal(f.storage.data.size, 0);
  }
  for (const status of [401, 409, 429, 500, 503]) {
    await assert.rejects(f.run('/api/publish', options, async () => { throw Object.assign(new Error('not confirmed'), { status }); }));
    assert.equal(f.storage.data.size, 1);
  }
  await f.run('/api/publish', options, async () => ({}));
  await f.run('/api/contents/4/repost-douyin', options, async opts => { assert.ok(opts.headers.get('Idempotency-Key')); return {}; });
  for (const path of ['/api/comment-rules', '/api/dm/auto-reply-rules', '/api/collections']) {
    await f.run(path, options, async opts => { assert.ok(opts.headers.get('Idempotency-Key')); return {}; });
  }
  await f.run('/api/publish/upload', { method: 'POST', body: {} }, async opts => { assert.equal(opts.headers, undefined); return {}; });
  await f.run('/api/publish/4/run-now', { method: 'POST' }, async opts => { assert.equal(opts.headers, undefined); return {}; });
  await f.run('/api/publish', { ...options, headers: { 'Idempotency-Key': 'explicit-fixture-key' } }, async opts => {
    assert.equal(opts.headers.get('Idempotency-Key'), 'explicit-fixture-key'); return {};
  });
  const brokenStorage = { getItem() { throw new Error('disabled'); }, setItem() { throw new Error('disabled'); }, removeItem() {} };
  const memory = submissions(brokenStorage);
  await assert.rejects(memory.run('/api/publish', options, async opts => { originalKey = opts.headers.get('Idempotency-Key'); throw new Error('offline'); }));
  await memory.run('/api/publish', options, async opts => { assert.equal(opts.headers.get('Idempotency-Key'), originalKey); return {}; });
}

testSubmissions().then(() => console.log('Submission UI regressions passed (offline)')).catch(error => { console.error(error); process.exitCode = 1; });
