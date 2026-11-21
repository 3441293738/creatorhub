/*
 * Load into the existing WeChat Finder page MAIN execution world via CDP Runtime.evaluate
 * or same-origin local proxy script injection.
 * Does NOT navigate, patch UI, read cookies, configure a proxy, or send network/JSBridge requests on load.
 *
 * Exposes:
 *   __finderProtocolDiscover()
 *   await __finderProtocolLoad(optionalExactModuleURL)
 *   globalThis.__finderCommentsProtocol.fetchPage(...)
 *   globalThis.__finderProtocol.{fetchCommentsPage, fetchFeedDetail, fetchUserPage, search, fetchFeedShareUrl, initFinder}
 */
(() => {
  'use strict';
  const validID = (x, name) => {
    if (typeof x !== 'string' || !/^\d+$/.test(x)) {
      throw Error(name + ' must be a decimal STRING, never a Number');
    }
    return x;
  };
  const validCursor = (x, name) => {
    if (x !== undefined && x !== '' && typeof x !== 'string') {
      throw Error(name + ' must remain an opaque string');
    }
    return x;
  };

  const hump2Underline = (k) =>
    /^[A-Z]/.test(k) ? k : k.replace(/([A-Z])/g, '_$1').toLowerCase();
  const underline2Hump = (k) =>
    k.replace(/_([A-Za-z0-9])/g, (_, c) => c.toUpperCase());

  const transformKeys = (value, keyFn) => {
    if (Array.isArray(value)) {
      return value.map((item) => transformKeys(item, keyFn));
    }
    if (value !== null && typeof value === 'object') {
      return Object.fromEntries(
        Object.entries(value).map(([k, v]) => [keyFn(k), transformKeys(v, keyFn)])
      );
    }
    return value;
  };
  const toSnake = (v) => transformKeys(v, hump2Underline);
  const toCamel = (v) => transformKeys(v, underline2Hump);

  const generateRequestId = () =>
    String(Date.now()) + String(Math.floor(Math.random() * 1e6)).padStart(6, '0');

  const safeParseRespJson = (rawText) => {
    const src = String(rawText || '');
    // Preserve >15-digit integer literals and non-JSON NaN/Infinity outside string literals.
    let out = '';
    let inStr = false;
    let esc = false;
    let i = 0;
    while (i < src.length) {
      const ch = src[i];
      if (inStr) {
        out += ch;
        if (esc) {
          esc = false;
        } else if (ch === '\\') {
          esc = true;
        } else if (ch === '"') {
          inStr = false;
        }
        i += 1;
        continue;
      }
      if (ch === '"') {
        inStr = true;
        out += ch;
        i += 1;
        continue;
      }
      if (ch === '-' || (ch >= '0' && ch <= '9')) {
        let j = i + (ch === '-' ? 1 : 0);
        while (j < src.length && src[j] >= '0' && src[j] <= '9') j += 1;
        const isFloat =
          j < src.length && (src[j] === '.' || src[j] === 'e' || src[j] === 'E');
        const digitsOnly = src.slice(ch === '-' ? i + 1 : i, j);
        if (!isFloat && digitsOnly.length >= 16) {
          out += '"' + src.slice(i, j) + '"';
          i = j;
          continue;
        }
      }
      if (src.startsWith('NaN', i)) {
        out += 'null';
        i += 3;
        continue;
      }
      if (src.startsWith('-Infinity', i)) {
        out += 'null';
        i += 9;
        continue;
      }
      if (src.startsWith('Infinity', i)) {
        out += 'null';
        i += 8;
        continue;
      }
      out += ch;
      i += 1;
    }
    return JSON.parse(out);
  };

  const NATIVE_CGI_MAP = Object.freeze({
    Init: {
      scope: 'finderLive',
      pc: { url: '/cgi-bin/micromsg-bin/pc_finderinit', cmdid: 5831 },
      ilink: { url: '/cgi-bin/micromsg-bin/ilink_finderinit', cmdid: 6869 },
    },
    FinderGetCommentList: {
      scope: 'finderInteraction',
      pc: { url: '/cgi-bin/micromsg-bin/pc_findergetcommentlist', cmdid: 10753 },
      ilink: { url: '/cgi-bin/micromsg-bin/ilink_findergetcommentlist', cmdid: 9019 },
    },
    FinderGetCommentDetail: {
      scope: 'finderLive',
      pc: { url: '/cgi-bin/micromsg-bin/pc_findergetcommentdetail', cmdid: 5259 },
      ilink: { url: '/cgi-bin/micromsg-bin/ilink_findergetcommentdetail', cmdid: 6225 },
    },
    FinderUserPage: {
      scope: 'finderLive',
      pc: { url: '/cgi-bin/micromsg-bin/pc_finderuserpage', cmdid: 6624 },
      ilink: { url: '/cgi-bin/micromsg-bin/ilink_finderuserpage', cmdid: 4146 },
    },
    FinderSearch: {
      scope: 'finderLive',
      pc: { url: '/cgi-bin/micromsg-bin/pc_findersearch', cmdid: 4287 },
      ilink: { url: '/cgi-bin/micromsg-bin/ilink_finderpcsearch', cmdid: 9294 },
    },
    FinderPCSearch: {
      scope: 'finderLive',
      pc: { url: '/cgi-bin/micromsg-bin/pc_finderpcsearch', cmdid: 6901 },
      ilink: { url: '/cgi-bin/micromsg-bin/ilink_finderpcsearch', cmdid: 9294 },
    },
    FinderGetFeedH5Url: {
      scope: 'finderInteraction',
      pc: { url: '/cgi-bin/micromsg-bin/pc_findergetfeedh5url', cmdid: 22299 },
      ilink: { url: '/cgi-bin/micromsg-bin/pc_findergetfeedh5url', cmdid: 22299 },
    },
  });

  const getNativeBridgeKind = () => {
    if (typeof globalThis.WeixinJSBridge?.invoke === 'function') {
      return 'weixin';
    }
    if (typeof globalThis.IlinkJSBridge?.invoke === 'function') {
      return 'ilink';
    }
    return '';
  };

  const createNativeBridgeDriver = () => {
    let cachedH5AuthToken = '';

    const invokeRaw = (bridgeKind, method, params, timeoutMs = 15000) =>
      new Promise((resolve) => {
        let settled = false;
        const finish = (val) => {
          if (settled) return;
          settled = true;
          clearTimeout(timer);
          resolve(val);
        };
        const timer = setTimeout(
          () => finish({ err_msg: method + ':timeout', jsapi_resp: { ret: -70004 } }),
          timeoutMs
        );
        try {
          const bridgeObj =
            bridgeKind === 'ilink' ? globalThis.IlinkJSBridge : globalThis.WeixinJSBridge;
          if (!bridgeObj || typeof bridgeObj.invoke !== 'function') {
            finish({ err_msg: method + ':missing_bridge' });
            return;
          }
          bridgeObj.invoke(method, params, (resp) => finish(resp || {}));
        } catch (err) {
          finish({ err_msg: String((err && err.message) || err || 'invoke_failed') });
        }
      });

    const requestH5Auth = async () => {
      if (typeof globalThis.WeixinJSBridge?.invoke !== 'function') {
        return '';
      }
      const res = await invokeRaw(
        'weixin',
        'finderH5Auth',
        { h5Version: 3774873601, scope: 'finderLive' },
        10000
      );
      const tok = typeof res?.h5AuthToken === 'string' ? res.h5AuthToken : '';
      if (tok) {
        cachedH5AuthToken = tok;
      }
      return tok;
    };

    const callOnce = async (bridgeKind, spec, snakeData, { token = '', includeScope = true } = {}) => {
      const target = bridgeKind === 'ilink' ? spec.ilink || spec.pc : spec.pc;
      const params = {
        req_json: JSON.stringify(snakeData),
        url: target.url,
        cgi_cmdid: target.cmdid,
        is_security_check: false,
      };
      if (bridgeKind !== 'ilink') {
        params.h5AuthToken = token || '';
      }
      if (includeScope) {
        params.scope = spec.scope || 'finderLive';
      }
      const raw = await invokeRaw(bridgeKind, 'finderH5ExtTransfer', params);
      const errMsg = String(raw?.err_msg || '');
      const jsRet = raw?.jsapi_resp?.ret;
      const baseRet = raw?.base_resp?.ret;
      if (typeof raw?.jsapi_resp?.resp_json === 'string' && raw.jsapi_resp.resp_json) {
        try {
          const parsed = toCamel(safeParseRespJson(raw.jsapi_resp.resp_json));
          return { errCode: 0, errMsg: 'ok', data: parsed, _rawRet: jsRet };
        } catch (parseErr) {
          return {
            errCode: -70003,
            errMsg: 'JSAPI_JSONPARSE_FAILED: ' + String((parseErr && parseErr.message) || parseErr),
            data: {},
            _rawRet: jsRet,
          };
        }
      }
      return {
        errCode: -70002,
        errMsg: errMsg || `JSAPI_TRANSFER_FAILED (ret=${jsRet ?? baseRet ?? 'unknown'})`,
        data: {},
        _rawRet: jsRet,
        _baseRet: baseRet,
        _errMsg: errMsg,
      };
    };

    const callCgi = async (apiName, payloadData = {}) => {
      const bridgeKind = getNativeBridgeKind();
      if (!bridgeKind) {
        throw Error('WeixinJSBridge is not available in current page');
      }
      const spec = NATIVE_CGI_MAP[apiName];
      if (!spec) {
        throw Error('Unsupported native Finder CGI: ' + apiName);
      }
      const enriched = {
        finderBasereq: {
          requestId: generateRequestId(),
          ...(payloadData.finderBasereq || {}),
        },
        ...payloadData,
      };
      if (payloadData.finderBasereq) {
        enriched.finderBasereq = {
          requestId: generateRequestId(),
          ...payloadData.finderBasereq,
        };
      }
      const snakeData = toSnake(enriched);

      // First attempt: standard PC CLIENT_AUTH (or cached h5AuthToken if already acquired).
      let res = await callOnce(bridgeKind, spec, snakeData, {
        token: cachedH5AuthToken,
        includeScope: !cachedH5AuthToken,
      });
      if (res.errCode === 0 || bridgeKind === 'ilink') {
        return { errCode: res.errCode, errMsg: res.errMsg, data: res.data };
      }

      // Second attempt (WeChat 4.2+ general H5 WebView fallback): acquire h5AuthToken via finderH5Auth and retry.
      const freshToken = await requestH5Auth();
      if (freshToken) {
        res = await callOnce(bridgeKind, spec, snakeData, {
          token: freshToken,
          includeScope: false,
        });
        if (res.errCode !== 0) {
          res = await callOnce(bridgeKind, spec, snakeData, {
            token: freshToken,
            includeScope: true,
          });
        }
      }
      return { errCode: res.errCode, errMsg: res.errMsg, data: res.data };
    };

    return {
      finderInit: () => callCgi('Init', {}),
      finderGetCommentList: (data) => callCgi('FinderGetCommentList', data),
      finderGetCommentDetail: (data) => callCgi('FinderGetCommentDetail', data),
      finderUserPage: (data) => callCgi('FinderUserPage', data),
      finderGetFeedH5Url: (data) => callCgi('FinderGetFeedH5Url', data),
      finderSearch: async (data) => {
        const primary = await callCgi('FinderSearch', data);
        const baseRet =
          primary?.data?.BaseResponse?.Ret ?? primary?.data?.baseResponse?.ret ?? 0;
        if (primary.errCode === 0 && Number(baseRet) === 0) {
          return primary;
        }
        const secondary = await callCgi('FinderPCSearch', data);
        return secondary.errCode === 0 ? secondary : primary;
      },
    };
  };

  const discover = () => {
    const urls = new Map();
    const add = (value, source) => {
      try {
        const u = new URL(value, location.href);
        if (
          u.protocol !== 'https:' ||
          u.hostname !== 'res.wx.qq.com' ||
          !u.pathname.includes('virtual_svg-icons-register.publish') ||
          !u.pathname.endsWith('.js')
        ) {
          return;
        }
        const item = urls.get(u.href) || { url: u.href, sources: [] };
        if (!item.sources.includes(source)) item.sources.push(source);
        urls.set(u.href, item);
      } catch {}
    };
    for (const e of globalThis.performance?.getEntriesByType('resource') || []) {
      add(e.name, 'performance.resource');
    }
    for (const e of Array.from(document.scripts || [])) {
      if (e.src) add(e.src, 'document.script');
    }
    for (const e of document.querySelectorAll('link[rel="modulepreload"][href]')) {
      add(e.href, 'document.modulepreload');
    }
    for (const e of document.querySelectorAll('script[type="importmap"]')) {
      try {
        const map = JSON.parse(e.textContent || '{}');
        for (const value of Object.values(map.imports || {})) {
          const u = new URL(value, location.href).href;
          if (urls.has(u)) add(u, 'document.importmap-confirmed');
        }
      } catch {}
    }
    return [...urls.values()];
  };

  globalThis.__finderProtocolDiscover = discover;

  globalThis.__finderProtocolLoad = async function (moduleURL) {
    const candidates = discover();
    if (moduleURL !== undefined) {
      const exact = new URL(moduleURL, location.href).href;
      if (!candidates.some((x) => x.url === exact)) {
        throw Error('Explicit module URL is not in current page resource/script/modulepreload set');
      }
      moduleURL = exact;
    }

    const nativeDriver = createNativeBridgeDriver();
    const nativeKind = getNativeBridgeKind();
    let feedService = globalThis.WXU?.API;
    let searchService = globalThis.WXU?.API2;
    let extraMethods = {};
    let origin = 'existing WXU.API';
    let usingPureNative = false;

    if (!feedService || typeof feedService.finderGetCommentList !== 'function') {
      if (moduleURL !== undefined || candidates.length === 1) {
        const targetModuleURL = moduleURL !== undefined ? moduleURL : candidates[0].url;
        const u = new URL(targetModuleURL, location.href);
        const ns = await import(u.href);
        const entries = Object.entries(ns).filter(([, v]) => v && typeof v === 'object');
        const commentMatches = entries.filter(([, v]) => typeof v.finderGetCommentList === 'function');
        if (commentMatches.length === 1) {
          feedService = commentMatches[0][1];
          origin = 'module export ' + commentMatches[0][0];

          const searchMatches = entries.filter(([, v]) => typeof v.finderSearch === 'function');
          if (searchMatches.length === 1) {
            searchService = searchMatches[0][1];
          }
          for (const [, svc] of entries) {
            for (const methodName of [
              'finderGetCommentDetail',
              'finderUserPage',
              'finderGetFeedH5Url',
              'finderInit',
              'finderLiveUserPage',
            ]) {
              if (typeof svc[methodName] === 'function' && !extraMethods[methodName]) {
                extraMethods[methodName] = svc[methodName].bind(svc);
              }
            }
          }
        } else if (moduleURL !== undefined || !nativeKind) {
          return {
            ready: false,
            reason: 'Expected one exported service with finderGetCommentList',
            matches: commentMatches.map(([name]) => name),
            exports: Object.keys(ns),
          };
        }
      }

      if (!feedService || typeof feedService.finderGetCommentList !== 'function') {
        if (nativeKind && moduleURL === undefined) {
          usingPureNative = true;
          feedService = nativeDriver;
          searchService = nativeDriver;
          origin = nativeKind === 'ilink' ? 'native IlinkJSBridge' : 'native WeixinJSBridge';
          for (const methodName of [
            'finderGetCommentDetail',
            'finderUserPage',
            'finderGetFeedH5Url',
            'finderInit',
          ]) {
            extraMethods[methodName] = nativeDriver[methodName];
          }
        } else {
          return {
            ready: false,
            reason: 'Expected exactly one current-page service module URL',
            candidates,
          };
        }
      }
    } else {
      for (const methodName of [
        'finderGetCommentDetail',
        'finderUserPage',
        'finderGetFeedH5Url',
        'finderInit',
      ]) {
        if (typeof feedService[methodName] === 'function') {
          extraMethods[methodName] = feedService[methodName].bind(feedService);
        } else if (typeof globalThis.WXU?.API4?.[methodName] === 'function') {
          extraMethods[methodName] = globalThis.WXU.API4[methodName];
        }
      }
    }

    const withNativeFallback = (primaryFn, nativeMethodName) => {
      if (usingPureNative) {
        return nativeDriver[nativeMethodName];
      }
      if (typeof primaryFn !== 'function') {
        return getNativeBridgeKind() ? nativeDriver[nativeMethodName] : null;
      }
      return async (arg) => {
        try {
          const res = await primaryFn(arg);
          if (
            res &&
            typeof res === 'object' &&
            res.errCode !== undefined &&
            Number(res.errCode) !== 0 &&
            getNativeBridgeKind()
          ) {
            const fallbackRes = await nativeDriver[nativeMethodName](arg);
            if (fallbackRes && Number(fallbackRes.errCode) === 0) {
              return fallbackRes;
            }
          }
          return res;
        } catch (err) {
          if (getNativeBridgeKind()) {
            return await nativeDriver[nativeMethodName](arg);
          }
          throw err;
        }
      };
    };

    const callCommentList = withNativeFallback(
      feedService.finderGetCommentList.bind(feedService),
      'finderGetCommentList'
    );
    const callSearch = withNativeFallback(
      searchService && typeof searchService.finderSearch === 'function'
        ? searchService.finderSearch.bind(searchService)
        : null,
      'finderSearch'
    );
    for (const methodName of [
      'finderGetCommentDetail',
      'finderUserPage',
      'finderGetFeedH5Url',
      'finderInit',
    ]) {
      const bound = withNativeFallback(extraMethods[methodName], methodName);
      if (bound) {
        extraMethods[methodName] = bound;
      }
    }

    const capabilities = ['finderGetCommentList'];
    for (const k of Object.keys(extraMethods)) capabilities.push(k);
    if (callSearch) capabilities.push('finderSearch');

    async function fetchCommentsPage({
      objectId,
      objectNonceId,
      rootCommentId,
      lastBuffer,
      enterSessionId,
      finderBasereq,
    } = {}) {
      validID(objectId, 'objectId');
      validCursor(lastBuffer, 'lastBuffer');
      let payload;
      if (rootCommentId !== undefined) {
        validID(rootCommentId, 'rootCommentId');
        payload = {
          direction: 2,
          identityScene: 2,
          objectId,
          lastBuffer: lastBuffer || undefined,
          rootCommentId,
        };
      } else {
        if (typeof objectNonceId !== 'string' || !objectNonceId) {
          throw Error('objectNonceId is required for root comments');
        }
        payload = {
          finderBasereq: finderBasereq || {
            scene: 140,
            ctxInfo: { clientReportBuff: '{"entranceId":"1002"}' },
            objectBaseInfos: [],
          },
          objectId,
          direction: 2,
          objectNonceId,
          identityScene: 2,
          lastBuffer: lastBuffer || undefined,
          enterSessionId: enterSessionId || String(Date.now()),
        };
      }
      return await callCommentList(payload);
    }

    async function fetchFeedDetail({
      objectId,
      objectNonceId,
      encryptedObjectId,
      scene,
      pullScene = 6,
    } = {}) {
      if (!extraMethods.finderGetCommentDetail) {
        throw Error('finderGetCommentDetail is not exported in current module');
      }
      const hasEid = typeof encryptedObjectId === 'string' && encryptedObjectId.length > 0;
      if (!hasEid) {
        validID(objectId, 'objectId');
      }
      const payload = {
        needObject: 1,
        lastBuffer: '',
        scene: scene !== undefined ? Number(scene) : (hasEid ? 141 : 146),
        direction: 2,
        identityScene: 2,
        pullScene: Number(pullScene),
        objectid: hasEid ? undefined : objectId,
        objectNonceId: hasEid ? undefined : (objectNonceId || undefined),
        encrypted_objectid: hasEid ? encryptedObjectId : '',
      };
      return await extraMethods.finderGetCommentDetail(payload);
    }

    async function fetchUserPage({
      username,
      finderUsername = '',
      lastBuffer = '',
      needFansCount = 0,
      objectId = '0',
    } = {}) {
      if (!extraMethods.finderUserPage) {
        throw Error('finderUserPage is not exported in current module');
      }
      if (typeof username !== 'string' || !username.trim()) {
        throw Error('username is required');
      }
      validCursor(lastBuffer, 'lastBuffer');
      let selfUser = finderUsername || globalThis.__wx_username || '';
      if (!selfUser && extraMethods.finderInit) {
        try {
          const initRes = await extraMethods.finderInit();
          selfUser = initRes?.data?.mainFinderUsername || '';
          if (selfUser) globalThis.__wx_username = selfUser;
        } catch {}
      }
      const cleanUsername = username.trim();
      const payload = {
        username: cleanUsername,
        finderUsername: selfUser || cleanUsername,
        lastBuffer: lastBuffer || '',
        needFansCount: Number(needFansCount) || 0,
        objectId: validID(String(objectId || '0'), 'objectId'),
      };
      return await extraMethods.finderUserPage(payload);
    }

    async function search({
      query,
      scene = 13,
      lastBuff = '',
      requestId,
    } = {}) {
      if (!callSearch) {
        throw Error('finderSearch is not exported in current module');
      }
      if (typeof query !== 'string' || !query.trim()) {
        throw Error('query is required');
      }
      validCursor(lastBuff, 'lastBuff');
      const payload = {
        query: query.trim(),
        scene: Number(scene) || 13,
        lastBuff: lastBuff || '',
        requestId: requestId ? String(requestId) : String(Date.now()),
      };
      return await callSearch(payload);
    }

    async function fetchFeedShareUrl({ objectId } = {}) {
      if (!extraMethods.finderGetFeedH5Url) {
        throw Error('finderGetFeedH5Url is not exported in current module');
      }
      validID(objectId, 'objectId');
      return await extraMethods.finderGetFeedH5Url({ objectId });
    }

    async function initFinder() {
      if (!extraMethods.finderInit) {
        throw Error('finderInit is not exported in current module');
      }
      return await extraMethods.finderInit();
    }

    globalThis.__finderCommentsProtocol = {
      origin,
      fetchPage: fetchCommentsPage,
    };

    globalThis.__finderProtocol = {
      origin,
      capabilities,
      fetchCommentsPage,
      fetchFeedDetail,
      fetchUserPage,
      search,
      fetchFeedShareUrl,
      initFinder,
    };

    return {
      ready: true,
      origin,
      method: 'finderGetCommentList',
      capabilities,
      networkRequestsSent: 0,
    };
  };

  return { loaderInstalled: true, networkRequestsSent: 0 };
})();
