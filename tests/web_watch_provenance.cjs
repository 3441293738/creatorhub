const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('app/web/app.js','utf8');
function fn(name) {
  const match = new RegExp(`^(?:async )?function ${name}\\(`,'m').exec(source);
  assert.ok(match, name);
  const next = source.indexOf('\n',match.index)+1;
  if (source.slice(match.index,next).trimEnd().endsWith('}')) return source.slice(match.index,next);
  const end = /^}\r?$/m.exec(source.slice(next));
  return source.slice(match.index,next+end.index+1);
}
function fixture() {
  const nodes = new Map(), requests = [], calls = [];
  const $ = id => {
    if (!nodes.has(id)) nodes.set(id,{value:'',textContent:'',innerHTML:'',hidden:false,style:{},dataset:{},
      setAttribute(name,value){this[name]=value},removeAttribute(name){delete this[name]},focus:()=>calls.push(['focus',id])});
    return nodes.get(id);
  };
  const context = vm.createContext({$,URL,URLSearchParams,console,
    document:{querySelector:()=>null}, requestAnimationFrame:callback=>callback(),
    CreatorHubBridge:{navigate:(...args)=>calls.push(['navigate',...args])},
    CreatorHubWorkbench:{showSection:(...args)=>calls.push(['section',...args])},
    api:path=>new Promise((resolve,reject)=>requests.push({path,resolve,reject})),
    toast:(...args)=>calls.push(['toast',...args]),empty:(_cols,message)=>message,
    fmtTime:n=>n?`published:${n}`:'—',fmtNum:n=>n||0,ic:()=>'',
    updateCommentSelBar(){},renderCommentPager:meta=>calls.push(['comment-pager',meta]),
    renderDanmakuPager:meta=>calls.push(['danmaku-pager',meta]),
    refreshWatches:async()=>{},refreshDanmakuWatches:async()=>{},
    uiConfirm:async config=>{calls.push(['confirm',config]);return true;},
  });
  vm.runInContext(`let PLATFORM='douyin',WATCHES=[],DANMAKU_WATCHES=[],COMMENT_SRC='',COMMENT_GROUP='',COMMENT_TAG='',DANMAKU_SRC='';
    let COMMENT_PAGE=1,COMMENT_PAGE_SIZE=10,DANMAKU_PAGE=1,DANMAKU_PAGE_SIZE=10;
    const selComment=new Set(),WATCH_RECORD_STATE={comment:{cache:new Map(),scope:''},danmaku:{cache:new Map(),scope:''}};
    const VIEW_REQUESTS=new Map();let VIEW_SERIAL=0;`,context);
  for (const name of ['esc','beginViewRequest','watchBaseName','watchName','contentCaptureBounds','contentCapturedTime',
    'watchRecordConfig','watchRecordSource','updateWatchRecordScope','populateWatchRecordSource','showWatchRecords','backToWatchTasks',
    'watchRecordSourceMarkup','watchRecordTimeMarkup','prepareWatchRecordLoad','watchRecordLoadError','cacheWatchRecordSources',
    'danmakuTime','refreshComments','refreshDanmaku','_moduleReportParams','delWatch','delDanmakuWatch'])
    vm.runInContext(fn(name),context);
  return {context,$,requests,calls,run:code=>vm.runInContext(code,context)};
}
async function run() {
  for (const kind of ['comment','danmaku']) {
    const module=kind==='comment'?'comments':'danmaku', src=kind==='comment'?'COMMENT_SRC':'DANMAKU_SRC';
    const refresh=kind==='comment'?'refreshComments':'refreshDanmaku';
    const f=fixture();
    f.run(`WATCHES=[{id:11,platform:'douyin',alias:'Comment A',title:'work A'}];
      DANMAKU_WATCHES=[{id:11,platform:'douyin',alias:'Danmaku A',title:'work A'}];${src}='11';selComment.add(5)`);
    f.run(`showWatchRecords('${kind}',11)`);
    assert.equal(f.$(kind+'-src').value,'11');
    assert.equal(f.$(kind+'-sort').value,'captured_desc');
    assert.ok(f.calls.some(c=>c[0]==='section'&&c[1]===module&&c[2]==='records'));
    assert.equal(new URL(f.requests[0].path,'http://local').searchParams.get('watch_id'),'11');
    assert.equal(f.run('selComment.size'),kind==='comment'?0:1);
    f.requests[0].resolve({items:[],total:0});await new Promise(resolve=>setImmediate(resolve));

    f.run(`${src}='77';populateWatchRecordSource('${kind}')`);
    assert.equal(f.$(kind+'-src').value,'77'); // Missing tasks never reset to all records.
    assert.ok(f.$(kind+'-scope-name').textContent.includes('77'));
    f.run(`${src}='0';populateWatchRecordSource('${kind}')`);
    assert.equal(f.$(kind+'-src').value,'0');
    assert.equal(f.$(kind+'-show-all').hidden,false);

    f.context.row={id:1,watch_id:99,platform:'douyin',aweme_id:'<script>work</script>',source:'creator',create_time:123,
      captured_at:'2026-09-08T08:00:00.123+00:00',watch_source:{id:99,module,platform:'douyin',deleted:true,name:'<img onerror="attack()">'}};
    const html=f.run(`watchRecordSourceMarkup('${kind}',row)`);
    assert.ok(!html.includes('<img onerror=')&&!html.includes('<script>'));
    assert.ok(html.includes('原任务已删除')&&html.includes(`showWatchRecords('${kind}',99)`));
    const time=f.run(`watchRecordTimeMarkup('${kind}',row)`);
    assert.ok(time.includes('2026-09-08T08:00:00.123Z')&&time.includes('published:123'));
    f.context.row.captured_at=null;
    assert.ok(f.run(`watchRecordTimeMarkup('${kind}',row)`).includes('未记录'));

    f.$(kind+'-captured-from').value='2026-03-08';f.$(kind+'-captured-to').value='2026-03-08';
    const bounds=JSON.parse(f.run(`JSON.stringify(contentCaptureBounds('${kind}'))`));
    assert.equal((Date.parse(bounds.captured_before)-Date.parse(bounds.captured_from))/3600000,process.env.TZ==='America/New_York'?23:24);
    const params=new URLSearchParams(f.run(`_moduleReportParams('${module}',false).toString()`));
    assert.equal(params.get('captured_from'),bounds.captured_from);
    assert.equal(params.get('watch_id'),'0');
    assert.ok(!f.run(`_moduleReportParams('${module}',true).toString()`).includes('captured_'));

    const g=fixture();g.run(`${src}='11'`);
    const old=g.run(`${refresh}(true)`);g.run(`${src}='12'`);const current=g.run(`${refresh}(true)`);
    g.requests[1].resolve({items:[{id:2,watch_id:12,text:'CURRENT',source:'creator',create_time:100,captured_at:null,
      watch_source:{id:12,module,platform:'douyin',name:'Task B'}}],total:1});await current;
    g.requests[0].resolve({items:[{id:1,watch_id:11,text:'STALE'}],total:1});await old;
    assert.ok(g.$(kind+'-table').innerHTML.includes('CURRENT')&&!g.$(kind+'-table').innerHTML.includes('STALE'));
    assert.equal(g.$(kind+'-scope-name').textContent,'Task B');
    if(kind==='danmaku') assert.ok(g.$('danmaku-table').innerHTML.includes('获取渠道')&&g.$('danmaku-table').innerHTML.includes('创作中心'));
    g.run(`${src}='99'`);const failed=g.run(`${refresh}(true)`);
    assert.ok(!g.$(kind+'-table').innerHTML.includes('CURRENT'));g.requests[2].reject(new Error('offline'));await failed;
    assert.ok(g.$(kind+'-table').innerHTML.includes('记录加载失败'));
    g.$(kind+'-table').innerHTML='STALE';g.$(kind+'-captured-from').value='2026-09-09';g.$(kind+'-captured-to').value='2026-09-08';
    g.run(`${src}='15'`);await g.run(`${refresh}()`);
    assert.ok(!g.$(kind+'-table').innerHTML.includes('STALE'));
    assert.equal(g.$(kind+'-captured-from')['aria-invalid'],'true');assert.equal(g.requests.length,3);
    g.run(`${src}='15'`);g.run(`showWatchRecords('${kind}',15)`);
    assert.equal(g.$(kind+'-captured-from').value,'');assert.equal(g.$(kind+'-captured-from')['aria-invalid'],undefined);
    g.requests[3].resolve({items:[],total:0});await new Promise(resolve=>setImmediate(resolve));
    g.run(`PLATFORM='xhs';VIEW_REQUESTS.clear();populateWatchRecordSource('${kind}')`);
    assert.ok(!g.$(kind+'-src').innerHTML.includes('Task B')); // Module + platform isolation.

    const d=fixture();
    d.context[refresh]=async()=>{};
    const deletion=d.run(`${kind==='comment'?'delWatch':'delDanmakuWatch'}(11)`);
    await new Promise(resolve=>setImmediate(resolve));
    assert.ok(d.calls[0][1].message.includes('记录会保留'));
    assert.ok(d.requests[0].path.includes(kind==='comment'?'with_comments=false':'with_records=false'));
    d.requests[0].resolve({ok:true});await deletion;
  }
  console.log('watch provenance, scope isolation, dates, exports and non-destructive task deletion passed');
}
run().catch(e=>{console.error(e);process.exitCode=1;});
