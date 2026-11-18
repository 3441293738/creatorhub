/*
 * Load into the existing WeChat Finder page MAIN execution world via CDP Runtime.evaluate.
 * Does NOT navigate, patch UI, read cookies, configure a proxy, or send network requests on load.
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

  globalThis.__finderProtocolLoad = async function(moduleURL) {
    const candidates = discover();
    if (moduleURL !== undefined) {
      const exact = new URL(moduleURL, location.href).href;
      if (!candidates.some(x => x.url === exact)) {
        throw Error('Explicit module URL is not in current page resource/script/modulepreload set');
      }
      moduleURL = exact;
    }

    let feedService = globalThis.WXU?.API;
    let searchService = globalThis.WXU?.API2;
    let extraMethods = {};
    let origin = 'existing WXU.API';

    if (!feedService || typeof feedService.finderGetCommentList !== 'function') {
      if (moduleURL === undefined) {
        if (candidates.length !== 1) {
          return {
            ready: false,
            reason: 'Expected exactly one current-page service module URL',
            candidates,
          };
        }
        moduleURL = candidates[0].url;
      }
      const u = new URL(moduleURL, location.href);
      const ns = await import(u.href);
      const entries = Object.entries(ns).filter(([, v]) => v && typeof v === 'object');
      const commentMatches = entries.filter(([, v]) => typeof v.finderGetCommentList === 'function');
      if (commentMatches.length !== 1) {
        return {
          ready: false,
          reason: 'Expected one exported service with finderGetCommentList',
          matches: commentMatches.map(([name]) => name),
          exports: Object.keys(ns),
        };
      }
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

    const callCommentList = feedService.finderGetCommentList.bind(feedService);
    const callSearch = searchService && typeof searchService.finderSearch === 'function'
      ? searchService.finderSearch.bind(searchService)
      : null;

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
