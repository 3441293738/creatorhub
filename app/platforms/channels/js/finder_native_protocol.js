/* Offline reconstruction of the PC WeChat comment bridge protocol.
 * No credential/cookie reads, logging, network, bridge discovery, or auto-auth.
 */
(function(root,factory){const api=factory();if(typeof module==='object'&&module.exports)module.exports=api;else root.FinderNativeProtocol=api;})(typeof globalThis!=='undefined'?globalThis:this,function(){
  'use strict';
  const CONFIG=Object.freeze({method:'finderH5ExtTransfer',name:'FinderGetCommentList',url:'/cgi-bin/micromsg-bin/pc_findergetcommentlist',cmdid:10753,scope:'finderInteraction'});
  const ERR=Object.freeze({UNKNOWN:-70000,LOGIN_FAILED:-70001,TRANSFER_FAILED:-70002,JSONPARSE_FAILED:-70003,TIMEOUT:-70004});
  const hump2Underline=k=>/^[A-Z]/.test(k)?k:k.replace(/([A-Z])/g,'_$1').toLowerCase();
  const underline2Hump=k=>k.replace(/_(\w)/g,(_,c)=>c.toUpperCase());
  function transform(value,keyFn){if(Array.isArray(value))return value.map(x=>transform(x,keyFn));if(value!==null&&typeof value==='object')return Object.fromEntries(Object.entries(value).map(([k,v])=>[keyFn(k),transform(v,keyFn)]));return value;}
  const toSnake=value=>transform(value,hump2Underline),toCamel=value=>transform(value,underline2Hump);
  function clone(v){return transform(v,k=>k);}
  function assertSafeNumbers(v){if(typeof v==='number'&&Number.isInteger(v)&&!Number.isSafeInteger(v))throw Error('Unsafe integer: original ID precision cannot be recovered');if(v&&typeof v==='object')for(const x of Object.values(v))assertSafeNumbers(x);}
  function id(value,name){if(typeof value!=='string'||!/^\d+$/.test(value))throw Error(name+' must be a decimal string');return value;}
  function commentPayload({objectId,objectNonceId,rootCommentId,lastBuffer,enterSessionId,finderBasereq}={}){
    id(objectId,'objectId');if(lastBuffer!==undefined&&typeof lastBuffer!=='string')throw Error('lastBuffer must be an opaque string');
    if(rootCommentId!==undefined)return{direction:2,identityScene:2,objectId,lastBuffer:lastBuffer||undefined,rootCommentId:id(rootCommentId,'rootCommentId')};
    if(typeof objectNonceId!=='string'||!objectNonceId)throw Error('objectNonceId is required');
    if(typeof enterSessionId!=='string'||!enterSessionId)throw Error('enterSessionId must be supplied explicitly');
    return{finderBasereq:clone(finderBasereq||{scene:140,ctxInfo:{clientReportBuff:'{"entranceId":"1002"}'},objectBaseInfos:[]}),objectId,direction:2,objectNonceId,identityScene:2,lastBuffer:lastBuffer||undefined,enterSessionId};
  }
  function buildBridgeParams(payload,state={}){
    assertSafeNumbers(payload);
    let data={finderBasereq:{...clone(state.finderBasereq||{}),...clone(payload.finderBasereq||{})},pcDeviceType:state.deviceType,...clone(payload)};
    if(!data.finderBasereq)data.finderBasereq={};
    if(state.routeScene)Object.assign(data.finderBasereq,{scene:state.routeScene});
    if(state.contextId)Object.assign(data.finderBasereq,{ctxInfo:{contextId:state.contextId}});
    if(state.entranceId){let report={entranceId:state.entranceId};if(state.subEntranceid)report.subEntranceid=state.subEntranceid;data.finderBasereq.ctxInfo={...(data.finderBasereq.ctxInfo||{}),clientReportBuff:JSON.stringify(report)};}
    data=toSnake(data);
    const params={req_json:JSON.stringify(data),url:CONFIG.url,cgi_cmdid:CONFIG.cmdid,h5AuthToken:state.h5AuthToken===undefined?'':state.h5AuthToken,is_security_check:false};
    if(!state.needLogin)params.scope=CONFIG.scope;
    return params;
  }
  function authParams(){return{method:'finderH5Auth',params:{h5Version:3774873601,scope:'finderLive'}};}
  function failure(code,message,extra={}){return{errCode:code,errMsg:message,data:null,ok:false,...extra};}
  function businessRet(data){const base=data&&(data.baseResponse||data.BaseResponse||data.baseresponse);return base&&(base.Ret!==undefined?base.Ret:base.ret);}
  function parseBridgeResponse(raw,{needLogin=false}={}){
    if(!raw||typeof raw!=='object')return failure(ERR.TRANSFER_FAILED,'Invalid bridge response');
    const nativeRet=raw.jsapi_resp?.ret,baseRet=raw.base_resp?.ret;
    if(typeof raw.err_msg==='string'&&raw.err_msg.includes('access_denied'))return failure(ERR.TRANSFER_FAILED,'Bridge access denied',{nativeRet,baseRet});
    if(nativeRet===-1)return failure(ERR.TRANSFER_FAILED,'Native transfer system error',{nativeRet,baseRet});
    if(needLogin&&(nativeRet===-13||nativeRet===-77))return failure(ERR.LOGIN_FAILED,'Service authentication/refresh required',{nativeRet,baseRet,requiresServiceAuth:true});
    if(typeof raw.jsapi_resp?.resp_json!=='string'||!raw.jsapi_resp.resp_json)return failure(ERR.JSONPARSE_FAILED,'Missing resp_json',{nativeRet,baseRet});
    try{const parsed=JSON.parse(raw.jsapi_resp.resp_json);assertSafeNumbers(parsed);const data=toCamel(parsed),ret=businessRet(data);return{errCode:0,errMsg:'ok',data,ok:!(ret!==undefined&&ret!==0),businessRet:ret,nativeRet,baseRet};}
    catch{return failure(ERR.JSONPARSE_FAILED,'Malformed JSON or unsafe numeric integer',{nativeRet,baseRet});}
  }
  function invokeBridge(invoke,params,{timeoutMs=10000,needLogin=false,setTimer=setTimeout,clearTimer=clearTimeout}={}){
    if(typeof invoke!=='function')return Promise.resolve(failure(ERR.TRANSFER_FAILED,'Missing injected invoke function'));
    if(!Number.isFinite(timeoutMs)||timeoutMs<=0)throw Error('timeoutMs must be positive');
    return new Promise(resolve=>{let done=false,timer;const settle=r=>{if(done)return;done=true;if(timer!==undefined)clearTimer(timer);resolve(r);};timer=setTimer(()=>settle(failure(ERR.TIMEOUT,'Bridge callback timeout')),timeoutMs);try{invoke(CONFIG.method,params,raw=>settle(parseBridgeResponse(raw,{needLogin})));}catch{settle(failure(ERR.TRANSFER_FAILED,'Injected bridge invoke failed'));}});
  }
  return Object.freeze({CONFIG,ERR,hump2Underline,underline2Hump,toSnake,toCamel,commentPayload,buildBridgeParams,authParams,parseBridgeResponse,invokeBridge});
});
