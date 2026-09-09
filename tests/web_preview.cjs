// Exercise the static preview's real fetch adapter without a browser or network.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const delegated = [];
const window = {
  location: { href: 'http://127.0.0.1/creatorhub/index.html' },
  fetch: async input => { delegated.push(input); return new Response('local asset'); },
};
vm.runInNewContext(fs.readFileSync('preview/demo-api.js', 'utf8'), {
  window, document: { addEventListener() {} }, URL, Response,
  setTimeout(resolve) { resolve(); },
});
const get = async path => (await window.fetch(path)).json();

(async () => {
  for (const platform of ['douyin', 'xhs', 'kuaishou', 'shipinhao']) {
    const query = `?platform=${platform}`;
    const summary = await get('/api/overview/summary' + query);
    assert.deepEqual(summary, {
      platform,
      accounts: (await get('/api/accounts' + query)).length,
      monitors: (await get('/api/monitors' + query)).filter(row => row.enabled).length,
      downloaded: (await get('/api/contents' + query)).filter(row => row.download_status === 'done').length,
      comments: (await get('/api/comments' + query)).length,
    });
    assert.equal(summary.accounts, 1);
    const queue = await get('/api/task-queue' + query);
    const tasks = await get('/api/publish' + query);
    assert.equal(queue.total, tasks.length);
    assert.equal(queue.summary.active, tasks.length);
    assert.equal(queue.summary.pending, tasks.length);
    assert.equal(queue.summary.failed, 0);
    assert.equal(queue.items[0].id, tasks[0].id);
    assert.equal(queue.items[0].platform, platform);
    assert.equal(queue.items[0].source_tab, 'publish');
    assert.equal(queue.items[0].account_name, (await get('/api/accounts' + query))[0].nickname);
  }

  const all = await get('/api/task-queue?platform=all&page_size=1&page=2');
  assert.equal(all.total, 4);
  assert.equal(all.summary.total, 4);
  assert.equal(all.pages, 4);
  assert.equal(all.page, 2);
  assert.equal(all.items.length, 1);
  assert.equal(all.items[0].platform, 'xhs');
  assert.equal((await get('/api/task-queue?platform=all&page_size=1&page=99')).page, 4);
  const failed = await get('/api/task-queue?platform=douyin&state=failed');
  assert.equal(failed.total, 0);
  assert.equal(failed.items.length, 0);
  assert.equal(failed.summary.active, 1, 'State filters must retain the platform summary');
  const wrongType = await get('/api/task-queue?platform=all&queue_type=collection');
  assert.equal(wrongType.total, 0);
  assert.equal(wrongType.summary.total, 0, 'Type filters must apply to the summary');
  const searched = await get('/api/task-queue?platform=all&q=' + encodeURIComponent('小红书示例号'));
  assert.equal(searched.total, 1);
  assert.equal(searched.summary.total, 1);
  assert.equal(searched.items[0].platform, 'xhs');
  assert.equal(delegated.length, 0, 'All API responses must stay in the local adapter');
  await window.fetch('./appearance.css');
  assert.deepEqual(delegated, ['./appearance.css']);
  console.log('Static preview summaries, queue filters and API isolation passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
