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
function fixture() {
  const nodes = new Map(), calls = [], requests = [];
  const $ = id => {
    if (!nodes.has(id)) nodes.set(id, {value:'', innerHTML:'', textContent:'', hidden:false,
      style:{}, dataset:{}, setAttribute(){}, removeAttribute(){}, focus:()=>calls.push(['focus',id])});
    return nodes.get(id);
  };
  const context = vm.createContext({$, URL, URLSearchParams, console,
    document:{querySelector:()=>null}, requestAnimationFrame:callback=>callback(),
    CreatorHubBridge:{navigate:(...args)=>calls.push(['navigate',...args])},
    CreatorHubWorkbench:{showSection:(...args)=>calls.push(['section',...args])},
    refreshContents:reset=>calls.push(['refresh',reset]),
    api:path=>new Promise((resolve,reject)=>requests.push({path,resolve,reject})),
    toast:(...args)=>calls.push(['toast',...args]),
    empty:(_cols,message)=>message, ic:()=>'', fmtNum:String, fmtTime:value=>`published:${value}`,
    contentStatusLabel:value=>value, contentPathCell:()=>'', renderContentPager:()=>{}, updateContentSelBar:()=>{},
  });
  vm.runInContext(`let PLATFORM='xhs',MONITORS=[],CONTENTS=[],CONTENT_SRC='',CONTENT_GROUP='',CONTENT_TAG='';
    const CONTENT_SOURCE_CACHE=new Map(),selContent=new Set(),VIEW_REQUESTS=new Map();
    let VIEW_SERIAL=0,CONTENT_PAGE=1,CONTENT_PAGE_SIZE=10,CONTENT_RENDER_SCOPE='';`, context);
  for (const name of ['esc','safeMediaUrl','beginViewRequest','monitorBaseName','monitorName','contentSource',
    'contentSourceMarkup','updateContentScope','populateContentSrc','showMonitorRecords','backToMonitorTasks',
    'contentCaptureBounds','contentCapturedTime','srcOf','noteCard','_moduleReportParams'])
    vm.runInContext(fn(name),context);
  return {context,$,calls,requests,run:code=>vm.runInContext(code,context)};
}
async function run() {
  const f=fixture();
  f.context.row={id:1,target_id:12,captured_at:'2026-09-08T08:00:00+00:00',create_time:99,
    desc:'sample',download_status:'done',media_type:'images',source:{id:12,platform:'xhs',name:'任务 <img onerror="alert(1)">',target_kind:'keyword'}};
  const html=f.run('noteCard(row)');
  assert.ok(html.includes('来源任务') && html.includes('抓取入库') && html.includes('发布 · published:99'));
  assert.ok(html.includes('任务 #12 · 关键词监控'));
  assert.ok(!html.includes('<img onerror='));
  assert.ok(html.includes('showMonitorRecords(12)'));
  assert.ok(html.includes('2026-09-08T08:00:00.000Z'));
  assert.ok(f.run('contentCapturedTime(null)').includes('未记录'));
  assert.ok(f.run('contentCapturedTime("invalid")').includes('未记录'));
  assert.equal(f.run('contentCapturedTime("2026-09-08T08:00:00")'), f.run('contentCapturedTime("2026-09-08T08:00:00Z")'));

  f.run(`CONTENT_SRC='99'; CONTENT_SOURCE_CACHE.set('99',{id:99,name:'已删除任务 #99',platform:'xhs',deleted:true}); populateContentSrc()`);
  assert.equal(f.$('content-src').value,'99');
  assert.ok(f.$('content-src').innerHTML.includes('已删除任务 #99'));
  assert.ok(f.$('content-scope-hint').textContent.includes('原任务已删除'));
  f.run(`CONTENT_SRC='77';populateContentSrc()`);
  assert.equal(f.$('content-src').value,'77'); // No silent widening to all tasks.

  f.run(`MONITORS=[{id:12,platform:'xhs',target_kind:'keyword',keyword:'城市',alias:'A'},
    {id:13,platform:'douyin',target_kind:'creator',nickname:'other'}]; CONTENT_PAGE=7;selContent.add(9);CONTENT_GROUP='old'`);
  for (const id of ['content-group','content-tag','content-search','content-captured-from','content-captured-to']) f.$(id).value='old';
  f.run('showMonitorRecords(12)');
  assert.equal(f.run('CONTENT_SRC'),'12');
  assert.equal(f.run('CONTENT_GROUP'),'');
  assert.equal(f.run('selContent.size'),0);
  assert.equal(f.$('content-search').value,'');
  assert.equal(f.$('content-captured-from').value,'');
  assert.equal(f.$('content-sort').value,'captured_desc');
  assert.deepEqual(f.calls.find(row=>row[0]==='section'),['section','monitors','records']);
  assert.deepEqual(f.calls.find(row=>row[0]==='refresh'),['refresh',true]);
  assert.ok(!f.$('content-src').innerHTML.includes('other'));
  f.run('showMonitorRecords(null)');
  assert.equal(f.$('content-scope-name').textContent,'全部监控任务');
  assert.equal(f.$('content-show-all').hidden,true);

  f.$('content-captured-from').value='2026-03-08';f.$('content-captured-to').value='2026-03-08';
  const bounds=JSON.parse(f.run('JSON.stringify(contentCaptureBounds())'));
  const hours=(Date.parse(bounds.captured_before)-Date.parse(bounds.captured_from))/3600000;
  assert.equal(hours,process.env.TZ==='America/New_York'?23:24);
  const params=f.run('_moduleReportParams("contents",false).toString()');
  assert.equal(new URLSearchParams(params).get('captured_from'),bounds.captured_from);
  assert.ok(!f.run('_moduleReportParams("contents",true).toString()').includes('captured_'));
  f.$('content-captured-from').value='2026-03-09';
  assert.throws(()=>f.run('contentCaptureBounds()'));

  const g=fixture();vm.runInContext(fn('refreshContents'),g.context);
  g.run(`CONTENT_SRC='11'`);const old=g.run('refreshContents(true)');
  g.run(`CONTENT_SRC='12'`);const current=g.run('refreshContents(true)');
  assert.ok(g.$('content-cards').innerHTML.includes('正在读取'));
  assert.equal(new URL(g.requests[1].path,'http://local').searchParams.get('target_id'),'12');
  g.requests[1].resolve({items:[{id:2,target_id:12,platform:'xhs',desc:'current task',source:{id:12,platform:'xhs',name:'Task B'}}],total:1});
  await current;
  g.requests[0].resolve({items:[{id:1,target_id:11,platform:'xhs',desc:'OLD TASK'}],total:1});
  await old;
  assert.ok(g.$('content-cards').innerHTML.includes('current task'));
  assert.ok(!g.$('content-cards').innerHTML.includes('OLD TASK'));
  assert.equal(g.$('content-scope-name').textContent,'Task B');
  g.run(`CONTENT_SRC='99'`);const failed=g.run('refreshContents(true)');
  assert.ok(!g.$('content-cards').innerHTML.includes('current task'));
  g.requests[2].reject(new Error('offline'));await failed;
  assert.ok(g.$('content-cards').innerHTML.includes('记录加载失败'));
  g.$('content-cards').innerHTML='STALE ROW';
  g.$('content-captured-from').value='2026-09-09';g.$('content-captured-to').value='2026-09-08';
  g.run(`CONTENT_SRC='15'`);await g.run('refreshContents()');
  assert.ok(!g.$('content-cards').innerHTML.includes('STALE ROW'));
  assert.equal(g.$('content-capture-help').dataset.error,'true');
  assert.equal(g.requests.length,3);
  console.log('content provenance, scope isolation, timezones and export filters passed');
}
run().catch(error=>{console.error(error);process.exitCode=1;});
