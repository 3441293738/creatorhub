// Injected only by local record-provenance browser tests; no platform traffic.
(() => {
  const previous = window.fetch.bind(window), deleted = {comments:new Set([99]),danmaku:new Set([99])};
  const json = (body,status=200) => new Response(JSON.stringify(body),{status,headers:{'Content-Type':'application/json'}});
  const watches = (module,platform) => [
    {id:21,platform,alias:'任务 A',title:'日常作品反馈',kind:'user',group_name:'品牌 A',tags:['每日'],comment_count:2,danmaku_count:2},
    {id:22,platform,alias:'任务 B 长名称用于确认手机端来源信息完整显示并自动换行',title:'同一作品的独立观察',kind:'video',group_name:'品牌 B',tags:[],comment_count:1,danmaku_count:1},
  ].filter(w=>!deleted[module].has(w.id)).map(w=>({...w,enabled:true,interval_seconds:3600,mode:'public',account_id:1}));
  const task = (module,platform,id) => {
    const w=watches(module,platform).find(w=>w.id===Number(id)),label=module==='comments'?'评论':'弹幕';
    return {id:Number(id||0),module,platform,deleted:Number(id)>0&&!w,unassigned:!id,kind:w?.kind||'',
      name:w?`${w.alias} · ${w.title}`:id?`已删除${label}任务 #${id}`:`未关联${label}监控`};
  };
  window.fetch = async (input,init={}) => {
    const url=new URL(typeof input==='string'?input:input.url,location.href),method=(init.method||'GET').toUpperCase();
    const flags=document.documentElement.dataset;
    if (!flags.fixtureWatchRecords) return previous(input,init);
    const module=url.pathname.startsWith('/api/comment')?'comments':url.pathname.startsWith('/api/danmaku')?'danmaku':null;
    if (!module) return previous(input,init);
    const prefix=module==='comments'?'/api/comment-watches':'/api/danmaku-watches';
    const platform=url.searchParams.get('platform')||'douyin';
    if(method==='DELETE'&&url.pathname.startsWith(prefix+'/')) {
      flags.fixtureWatchDelete=url.pathname+url.search;
      deleted[module].add(Number(url.pathname.split('/').pop()));return json({ok:true,records_deleted:0});
    }
    if(method==='GET'&&url.pathname===prefix) return json(watches(module,platform));
    if(method!=='GET'||url.pathname!==`/api/${module}`) return previous(input,init);
    flags.fixtureWatchQuery=url.search;
    if(flags.fixtureWatchFail==='true') return json({detail:'测试：读取失败'},503);
    const q=url.searchParams;
    let rows=[
      {id:101,watch_id:21,text:'任务 A 本次抓取的内容',captured_at:'2026-09-08T08:10:00.123Z'},
      {id:102,watch_id:21,text:'任务 A 昨天抓取的内容',captured_at:'2026-09-07T08:00:00Z'},
      {id:103,watch_id:22,text:'任务 B 独立记录同一内容',captured_at:'2026-09-08T09:00:00Z'},
      {id:104,watch_id:99,text:'已删除任务的历史内容',captured_at:null},
      {id:105,watch_id:0,text:'未关联监控的手动记录',captured_at:'2026-09-08T10:00:00Z'},
      {id:106,watch_id:null,text:'未关联监控的旧记录',captured_at:'2026-09-08T11:00:00Z'},
    ].map(row=>({...row,platform,aweme_id:'fixture-shared-work',comment_id:'shared',danmaku_id:'shared',source:'creator',
      create_time:1756684800,video_time_ms:12340,user_nickname:'示例用户',like_count:12,watch_source:task(module,platform,row.watch_id)}));
    if(q.has('watch_id')) rows=rows.filter(row=>String(row.watch_id||0)===q.get('watch_id'));
    if(q.get('captured_from')) rows=rows.filter(row=>row.captured_at&&Date.parse(row.captured_at)>=Date.parse(q.get('captured_from')));
    if(q.get('captured_before')) rows=rows.filter(row=>row.captured_at&&Date.parse(row.captured_at)<Date.parse(q.get('captured_before')));
    if(q.get('q')) rows=rows.filter(row=>row.text.includes(q.get('q')));
    if(q.get('sort')==='captured_desc'||q.get('sort')==='captured_asc') rows.sort((a,b)=>
      (Date.parse(a.captured_at||0)-Date.parse(b.captured_at||0))*(q.get('sort')==='captured_desc'?-1:1));
    const total=rows.length,page=Number(q.get('page')||1),size=Number(q.get('page_size')||10);
    return json({items:rows.slice((page-1)*size,page*size),total,page,page_size:size,pages:Math.max(1,Math.ceil(total/size)),
      watch_source:q.has('watch_id')?task(module,platform,Number(q.get('watch_id'))):null});
  };
  document.addEventListener('fixture-watch-record-refresh',async () => {
    await Promise.all([refreshWatches(),refreshComments(true),refreshDanmakuWatches(),refreshDanmaku(true)]);
    document.documentElement.dataset.fixtureWatchReady='true';
  });
})();
