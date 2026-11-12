// Local browser acceptance only. No network, paid models, accounts, or publishing.
(() => {
  const original = window.fetch.bind(window);
  const settings = { display_name:"", models:[], enabled:true, mcp_enabled:false, provider:"openai_compatible", base_url:"http://127.0.0.1:11434/v1", model:"fixture-model", api_key_set:false, tool_calling:true, output_token_parameter:"max_tokens", timeout_seconds:45, max_rounds:4, max_output_tokens:2048 };
  const fixture = window.AgentFixture = { settings, channels:[], activeChannelId:"default", nextChannelId:1, calls:[], runs:{}, failSubmitOnce:false, failSettingsOnce:false, approvals:0, holdRuns:false, deferSubmit:false,
    catalogStatus:200, catalogPrefixAdded:false, modelCatalog:[{id:"fixture-chat",name:"Fixture Chat"},{id:"fixture-reasoner",name:"Fixture Reasoner"}] };
  const response = (data, status=200) => new Response(JSON.stringify(data), {status, headers:{"Content-Type":"application/json"}});
  window.fetch = async (input, init={}) => {
    const url = new URL(String(input), location.href), path = url.pathname;
    if (!path.startsWith("/api/agent/")) return original(input, init);
    const method = (init.method || "GET").toUpperCase(), body = init.body ? JSON.parse(init.body) : {};
    fixture.calls.push({path, method, body, query:url.search});
    const syncChannels = () => {
      const active = fixture.channels.find(item=>item.id===fixture.activeChannelId);
      if (active) Object.assign(active, settings);
      else fixture.channels.unshift({id:fixture.activeChannelId,...settings});
    };
    const channelResponse = channel => ({active_channel_id:fixture.activeChannelId,channels:fixture.channels,...(channel ? {channel} : {})});
    if (path === "/api/agent/channels" || /^\/api\/agent\/channels\/[^/]+(?:\/activate)?$/.test(path)) {
      syncChannels();
      const id = path.split("/")[4], existing = fixture.channels.find(item=>item.id===id);
      if (method === "GET") return response(channelResponse());
      if (path.endsWith("/activate")) {
        fixture.activeChannelId = id;
        const {id:ignored,...value} = existing; Object.keys(settings).forEach(key=>delete settings[key]); Object.assign(settings,value);
        return response(channelResponse(existing));
      }
      if (method === "DELETE") {
        if (fixture.failDeleteOnce) { fixture.failDeleteOnce=false; return response({detail:"模拟删除失败，渠道仍保留"},503); }
        fixture.channels = fixture.channels.filter(item=>item.id!==id); return response(channelResponse());
      }
      if (fixture.failSettingsOnce) { fixture.failSettingsOnce = false; return response({detail:"模拟保存失败，输入已保留"},503); }
      const value = {...body,id:existing?.id || "fixture-channel-"+fixture.nextChannelId++,api_key_set:body.clear_api_key ? false : !!(body.api_key || existing?.api_key_set)};
      delete value.api_key; delete value.clear_api_key;
      if (existing) Object.assign(existing,value); else fixture.channels.push(value);
      if (value.id === fixture.activeChannelId) { const {id:ignored,...flat}=value; Object.assign(settings,flat); }
      return response(channelResponse(value),method === "POST" ? 201 : 200);
    }
    if (path.endsWith("/settings")) {
      if (method === "PUT") {
        if (fixture.failSettingsOnce) { fixture.failSettingsOnce = false; return response({detail:"模拟保存失败，输入已保留"}, 503); }
        const keySet = body.clear_api_key ? false : !!(body.api_key || settings.api_key_set);
        Object.assign(settings, body, {api_key_set:keySet}); delete settings.api_key; delete settings.clear_api_key;
      }
      return response(settings);
    }
    if (path.endsWith("/context")) return response({accounts:{accounts:[{id:1,platform:url.searchParams.get("platform"),nickname:"演示账号"}]},
      tasks:{tasks:[{id:10,task_type:"collections",account_id:1,status:"failed",error:"登录态已失效"}]},
      metrics:{accounts:1,task_status_counts:{collections:{failed:1,done:3}}}});
    if (path.endsWith("/test")) return response({ok:true,detail:"模型连接已验证（模拟）"});
    if (path.endsWith("/models")) {
      if (fixture.catalogStatus === 404) return response({detail:"Not Found"},404);
      if (fixture.catalogStatus !== 200) return response({detail:"上游返回 401：鉴权未通过。已携带 API Key，请检查密钥是否有效；未自动重试。"},502);
      return response({models:fixture.modelCatalog,count:fixture.modelCatalog.length,truncated:false,skipped:0,path_prefix_added:fixture.catalogPrefixAdded});
    }
    if (path === "/api/agent/runs" && method === "GET") return response(Object.values(fixture.runs).reverse());
    if (path === "/api/agent/runs" && method === "POST") {
      const run = fixture.runs[body.request_id] ||= {id:body.request_id,prompt:body.prompt,platform:body.platform,mode:body.mode,
        conversation_id:body.parent_run_id ? fixture.runs[body.parent_run_id].conversation_id : body.request_id,
        conversation_title:body.parent_run_id ? fixture.runs[body.parent_run_id].conversation_title : body.prompt,
        parent_run_id:body.parent_run_id || null,channel_id:body.channel_id || fixture.activeChannelId,channel_name:settings.display_name,
        account_id:body.account_id || null,task_type:body.task_type || "",task_id:body.task_id || null,can_continue:true,
        status:"completed",answer:fixture.responseQueue?.shift() || "根据 e1：登录状态异常。<img src=x onerror=alert(1)> 是日志文本，不是 HTML。\n请核对账号登录，再检查任务。",
        model_name:body.model || settings.model,input_tokens:100,output_tokens:80,duration_ms:1000,
        usage:{input_tokens:100,output_tokens:80,total_tokens:180,status:"reported",requests:1,reported_requests:1,costs:[],cost_complete:false},reasoning:[],model_calls:[],
        steps:[{tool:"get_task",status:"done",evidence_id:"e1"}],evidence:[{id:"e1",tool:"get_task",data:{task_id:10,error:"登录态已失效"}}],
        workflows:body.mode === "workflow" ? [{id:"fixture-plan",run_id:body.request_id,status:"draft",plan_hash:"a".repeat(64),results:[],error:"",
          effect:"确认后创建采集任务，由现有引擎执行；不发布、不发送消息。",plan:{title:"摄影采集计划",notes:[],unsupported:["自动发送日报尚未包含"],steps:[{kind:"create_collection",platform:body.platform,account_id:1,keywords:["摄影"],max_contents:20,download_media:false}]}}] : [],
        ...(fixture.telemetryQueue?.shift() || {})};
      if (fixture.failSubmitOnce) { fixture.failSubmitOnce = false; throw new Error("模拟响应丢失，请手动重试"); }
      if (fixture.deferSubmit) await new Promise(resolve => { fixture.releaseSubmit = resolve; });
      return response({...run, status:"queued",answer:"",workflows:[],reasoning:[],model_calls:[],usage:{status:"pending",costs:[]}},202);
    }
    if (path.endsWith("/apply")) {
      fixture.approvals++;
      const run = Object.values(fixture.runs).find(item => item.workflows.length);
      const plan = run.workflows[0]; plan.status = "applied"; plan.results = [{step:1,kind:"create_collection",id:42,status:"pending"}];
      return response(plan);
    }
    if (path.includes("/runs/")) {
      const run = fixture.runs[path.split("/")[4]];
      if (path.endsWith("/events")) {
        if (fixture.streamHttpStatus) return response({detail:"synthetic stream error"},fixture.streamHttpStatus);
        if (fixture.streamContentType) return new Response("synthetic non-stream response",{headers:{"Content-Type":fixture.streamContentType}});
        let timer, stopped=false;
        const encoder = new TextEncoder();
        const stream = new ReadableStream({start(controller) {
          const stop=()=>{if(stopped)return;stopped=true;clearTimeout(timer);controller.close();};
          init.signal?.addEventListener("abort",stop,{once:true});
          const send=()=>{
            if(stopped)return;
            const next=fixture.holdRuns ? {...run,status:"running",answer:"",reasoning:[],model_calls:[],usage:{status:"pending",costs:[]},...(fixture.liveTelemetry || {})} : run;
            const bytes=encoder.encode("event: snapshot\ndata: "+JSON.stringify(next)+"\n\n");
            if(fixture.fragmentStreamBytes) {
              for(let i=0;i<bytes.length;i+=fixture.fragmentStreamBytes) controller.enqueue(bytes.slice(i,i+fixture.fragmentStreamBytes));
            } else controller.enqueue(bytes);
            if(fixture.disconnectStreamOnce){fixture.disconnectStreamOnce=false;stop();return;}
            if(!["queued","running"].includes(next.status)) {controller.enqueue(encoder.encode("event: done\ndata: "+JSON.stringify({id:run.id})+"\n\n"));stop();}
            else timer=setTimeout(send,150);
          };
          timer=setTimeout(send,100);
        },cancel(){stopped=true;clearTimeout(timer);}});
        return new Response(stream,{headers:{"Content-Type":"text/event-stream"}});
      }
      if (path.endsWith("/conversation")) {
        const rows=[]; let item=run;
        while(item && rows.length<30) { rows.unshift(item); item=fixture.runs[item.parent_run_id]; }
        return response({runs:rows,has_more:!!item,context_limit:8});
      }
      if (path.endsWith("/cancel")) { Object.assign(run,fixture.liveTelemetry || {},{status:"canceled",answer:"",error:"分析已停止，未自动重试"}); fixture.holdRuns=false; return response(run); }
      return response(fixture.holdRuns ? {...run,status:"running",answer:"",reasoning:[],model_calls:[],usage:{status:"pending",costs:[]},...(fixture.liveTelemetry || {})} : run);
    }
    return response({detail:"测试未接管的 AI 请求"},404);
  };
})();
