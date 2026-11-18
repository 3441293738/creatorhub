"""微信视频号 (WeChat Channels / Finder) 本地回环 CDP 原生服务客户端。

连接已登录桌面微信自身的 Finder 页面 main-world 执行上下文(支持本地 CDP HTTP 端点、
`ws://127.0.0.1:...` 浏览器根会话或 `wmpf_cdp.py` 生成的本地控制口令文件):
  - 严格限定回环地址 `127.0.0.1` / `localhost` / `[::1]`，拒绝非本机或附带 URL 凭据的端点;
  - 校验原生环境(`location.hostname === "channels.weixin.qq.com"`、`WeixinJSBridge.invoke` 存在、
    `MicroMessenger|WindowsWechat` UA)，绝不把普通 Chrome 误作微信原生上下文;
  - 复用页面已加载的 Finder ES 模块服务导出(`finderGetCommentList`、`finderGetCommentDetail`、
    `finderUserPage`、`finderSearch`、`finderGetFeedH5Url`)，不导出任何账号令牌，不改系统代理。
"""
from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlsplit

import httpx

from .comment_protocol import (
    Exchange,
    ProtocolSchema,
    Request,
    Target,
    collect_comments_async,
    protocol_record_to_comment_dict,
    safe_response,
    server_id,
)
from .extract import parse_channels_feed
from .resolve import (
    fetch_finder_preview_info,
    normalize_finder_username,
    parse_channels_url,
)

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_BOOTSTRAP_JS_PATH = Path(__file__).resolve().parent / "js" / "finder_protocol_browser_bootstrap.js"
_TOKEN_REDACT_RE = re.compile(
    r'((?:h5AuthToken|pass_ticket|token|exportkey|session_key)["\']?\s*[:=]\s*["\']?)[^\s,;}"\'&]+',
    re.IGNORECASE,
)
_URL_REDACT_RE = re.compile(r"(?:https?|wss?)://[^\s]+", re.IGNORECASE)


class ChannelsProtocolTransportError(RuntimeError):
    """本地微信 Finder 原生 CDP 通道不可用或调用失败。"""


def sanitize_bridge_error(exc: Any) -> str:
    """脱敏桥接错误文本，避免把会话参数或控制口令 URL 写进任务日志。"""
    text = str(getattr(exc, "message", None) or exc or "")
    text = _URL_REDACT_RE.sub("[URL omitted]", text)
    text = _TOKEN_REDACT_RE.sub(r"\1[omitted]", text)
    return text


def normalize_capabilities(raw: Any) -> dict[str, bool]:
    """Normalize JS bridge capabilities from either list[str] or dict[str, Any]."""
    if isinstance(raw, dict):
        return {str(k).strip(): bool(v) for k, v in raw.items() if str(k).strip()}
    if isinstance(raw, (list, tuple, set)):
        return {str(item).strip(): True for item in raw if str(item).strip()}
    return {}


def validate_loopback_url(url: str, *, schemes: tuple[str, ...]) -> str:
    raw = (url or "").strip()
    if not raw:
        raise ChannelsProtocolTransportError("未配置本地微信 Finder CDP 端点")
    parsed = urlsplit(raw)
    scheme = (parsed.scheme or "").lower()
    host = (parsed.hostname or "").lower()
    if scheme not in schemes or host not in _LOOPBACK_HOSTS:
        raise ChannelsProtocolTransportError("CDP 端点必须是本机回环地址 (127.0.0.1 / localhost / ::1)")
    if parsed.username or parsed.password:
        raise ChannelsProtocolTransportError("CDP 端点不允许携带 URL 用户名或密码")
    return raw


def is_finder_page_target(target: dict[str, Any]) -> bool:
    if not isinstance(target, dict) or target.get("type") != "page":
        return False
    try:
        u = urlsplit(str(target.get("url") or ""))
    except ValueError:
        return False
    return (u.hostname or "").lower() == "channels.weixin.qq.com" and (u.path or "").startswith("/web/pages/")


def choose_finder_target(targets: list[dict[str, Any]], target_id: str = "") -> dict[str, Any]:
    if not isinstance(targets, list):
        raise ChannelsProtocolTransportError("CDP 目标列表格式无效")
    normalized = [
        {**t, "id": str(t.get("id") or t.get("targetId") or "")}
        for t in targets
        if is_finder_page_target(t)
    ]
    chosen = None
    if target_id:
        chosen = next((t for t in normalized if t["id"] == target_id), None)
    elif len(normalized) == 1:
        chosen = normalized[0]
    elif len(normalized) > 1:
        # 优先选择具体 feed/profile 页，其次 home 页
        non_home = [
            t for t in normalized
            if not urlsplit(str(t.get("url") or "")).path.endswith("/home")
        ]
        chosen = non_home[0] if non_home else normalized[0]
    if not chosen:
        raise ChannelsProtocolTransportError(
            "未发现已打开的微信视频号原生页面(/web/pages/*)；请先在桌面微信中打开视频号页面"
        )
    return chosen


class _AsyncCdpSession:
    """轻量级异步本地 CDP WebSocket 会话(支持扁平化 Target sessionId 路由)。"""

    def __init__(self, ws: Any, timeout: float = 30.0) -> None:
        self.ws = ws
        self.timeout = max(2.0, float(timeout))
        self.serial = 0
        self.session_id: str | None = None
        self._pending: dict[int, tuple[str | None, asyncio.Future[Any]]] = {}
        self._reader_task = asyncio.create_task(self._reader_loop())

    @classmethod
    async def open(cls, ws_url: str, timeout: float = 30.0) -> "_AsyncCdpSession":
        from websockets.asyncio.client import connect

        validated = validate_loopback_url(ws_url, schemes=("ws", "wss"))
        try:
            ws = await asyncio.wait_for(
                connect(validated, max_size=16 * 1024 * 1024, compression=None),
                timeout=timeout,
            )
        except Exception as exc:
            status_code = getattr(getattr(exc, "response", None), "status_code", None)
            detail = sanitize_bridge_error(exc)
            if status_code == 503 or "HTTP 503" in detail:
                raise ChannelsProtocolTransportError(
                    "WMPF 回环桥接已在监听，但本机微信尚未接入调试端口（HTTP 503）；"
                    "标准零售版桌面微信默认不会主动连接 9421，请改用「内置本机微信代理桥接」模式"
                ) from exc
            if status_code == 409 or "HTTP 409" in detail:
                raise ChannelsProtocolTransportError(
                    "本地微信 WMPF 调试通道已被另一会话占用（HTTP 409），请稍后重试或重启桥接"
                ) from exc
            if status_code == 403 or "HTTP 403" in detail:
                raise ChannelsProtocolTransportError(
                    "本地微信 WMPF 控制令牌校验失败（HTTP 403），请重新启动桥接并保存配置"
                ) from exc
            raise ChannelsProtocolTransportError(
                f"无法连接本地微信 CDP WebSocket: {detail}"
            ) from exc
        return cls(ws, timeout=timeout)

    async def _reader_loop(self) -> None:
        try:
            async for raw in self.ws:
                if not isinstance(raw, str):
                    continue
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(msg, dict) or "id" not in msg:
                    continue
                msg_id = msg.get("id")
                if not isinstance(msg_id, int):
                    continue
                entry = self._pending.get(msg_id)
                if entry is None:
                    continue
                expected_session, fut = entry
                if expected_session and msg.get("sessionId") != expected_session:
                    continue
                self._pending.pop(msg_id, None)
                if not fut.done():
                    if "error" in msg and isinstance(msg["error"], dict):
                        fut.set_exception(
                            ChannelsProtocolTransportError(
                                str(msg["error"].get("message") or "CDP error")
                            )
                        )
                    else:
                        fut.set_result(msg.get("result") or {})
        except Exception as exc:
            err = ChannelsProtocolTransportError(
                f"CDP 目标连接已断开: {sanitize_bridge_error(exc)}"
            )
            for _, fut in list(self._pending.values()):
                if not fut.done():
                    fut.set_exception(err)
            self._pending.clear()

    async def call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        use_session: bool = True,
    ) -> dict[str, Any]:
        self.serial += 1
        req_id = self.serial
        sid = self.session_id if use_session else None
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[Any] = loop.create_future()
        self._pending[req_id] = (sid, fut)
        envelope: dict[str, Any] = {"id": req_id, "method": method, "params": params or {}}
        if sid:
            envelope["sessionId"] = sid
        try:
            await self.ws.send(json.dumps(envelope, ensure_ascii=False))
            return await asyncio.wait_for(fut, timeout=self.timeout)
        except asyncio.TimeoutError as exc:
            self._pending.pop(req_id, None)
            raise ChannelsProtocolTransportError(f"CDP {method} 调用超时") from exc
        except Exception:
            self._pending.pop(req_id, None)
            raise

    async def evaluate(self, expression: str) -> Any:
        res = await self.call(
            "Runtime.evaluate",
            {
                "expression": expression,
                "awaitPromise": True,
                "returnByValue": True,
                "userGesture": False,
            },
        )
        if res.get("exceptionDetails"):
            details = res["exceptionDetails"]
            desc = ""
            if isinstance(details.get("exception"), dict):
                desc = str(details["exception"].get("description") or "").splitlines()[0]
            raise ChannelsProtocolTransportError(
                f"微信页面执行异常: {sanitize_bridge_error(desc or details.get('text') or 'error')}"
            )
        result_obj = res.get("result") if isinstance(res.get("result"), dict) else {}
        if result_obj.get("subtype") == "error":
            raise ChannelsProtocolTransportError("微信页面返回错误对象")
        if "value" not in result_obj:
            raise ChannelsProtocolTransportError("微信页面返回值不可 JSON 序列化")
        return result_obj["value"]

    async def close(self) -> None:
        if self.session_id:
            sid = self.session_id
            self.session_id = None
            try:
                await asyncio.wait_for(
                    self.call("Target.detachFromTarget", {"sessionId": sid}, use_session=False),
                    timeout=1.5,
                )
            except Exception:
                pass
        try:
            await self.ws.close()
        except Exception:
            pass
        if not self._reader_task.done():
            self._reader_task.cancel()
            await asyncio.gather(self._reader_task, return_exceptions=True)


class ChannelsProtocolClient:
    """微信视频号原生协议客户端(复用已登录微信页面服务层完成搜索、主页、详情与评论分页)。"""

    def __init__(
        self,
        *,
        endpoint: str = "",
        endpoint_file: str = "",
        target_id: str = "",
        module_url: str = "",
        use_proxy: bool = False,
        proxy_manager: Any = None,
        timeout: float = 30.0,
    ) -> None:
        self.endpoint = (endpoint or "").strip()
        self.endpoint_file = (endpoint_file or "").strip()
        self.target_id = (target_id or "").strip()
        self.module_url = (module_url or "").strip()
        self.use_proxy = bool(use_proxy)
        self._proxy_manager = proxy_manager
        self.timeout = max(5.0, float(timeout))
        self._session: _AsyncCdpSession | None = None
        self.ready_info: dict[str, Any] = {}

    @classmethod
    def from_config(cls, cfg: Any) -> "ChannelsProtocolClient | None":
        engine_cfg = getattr(cfg, "engine", cfg)
        use_proxy = bool(getattr(engine_cfg, "channels_proxy_enabled", False))
        endpoint = str(getattr(engine_cfg, "channels_cdp_endpoint", "") or "").strip()
        endpoint_file = str(getattr(engine_cfg, "channels_cdp_endpoint_file", "") or "").strip()
        if not use_proxy and not endpoint and not endpoint_file:
            return None
        timeout = float(getattr(engine_cfg, "request_timeout_seconds", 30.0) or 30.0)
        return cls(
            endpoint=endpoint,
            endpoint_file=endpoint_file,
            use_proxy=use_proxy,
            timeout=max(15.0, timeout),
        )

    @property
    def configured(self) -> bool:
        return bool(self.use_proxy or self.endpoint or self.endpoint_file)

    def _resolve_proxy_manager(self) -> Any:
        mgr = self._proxy_manager
        if mgr is None:
            from app.platforms.channels.proxy_bridge import get_active_proxy_manager

            mgr = get_active_proxy_manager()
        if mgr is None:
            raise ChannelsProtocolTransportError("内置视频号代理管理器尚未初始化")
        return mgr

    def _resolve_ws_or_http(self) -> tuple[str, str]:
        """返回 (browser_ws_url, http_endpoint_url) 二选一。"""
        if self.endpoint_file:
            fpath = Path(self.endpoint_file).expanduser()
            if not fpath.is_file():
                raise ChannelsProtocolTransportError(f"本地 CDP 端点描述文件不存在: {fpath.name}")
            desc = json.loads(fpath.read_text("utf-8"))
            if (
                not isinstance(desc, dict)
                or desc.get("bind_host") != "127.0.0.1"
                or desc.get("local_debug_control_secret_not_account_token") is not True
            ):
                raise ChannelsProtocolTransportError("端点描述文件未通过本机回环与非凭据校验")
            ws_url = validate_loopback_url(
                str(desc.get("browserWebSocketUrl") or ""), schemes=("ws", "wss")
            )
            return ws_url, ""

        if not self.endpoint:
            raise ChannelsProtocolTransportError(
                "尚未配置视频号原生 CDP 端点(channels_cdp_endpoint)"
            )
        scheme = (urlsplit(self.endpoint).scheme or "").lower()
        if scheme in ("ws", "wss"):
            return validate_loopback_url(self.endpoint, schemes=("ws", "wss")), ""
        return "", validate_loopback_url(self.endpoint, schemes=("http", "https"))

    async def connect(self) -> dict[str, Any]:
        if self.ready_info.get("ready") and (self.use_proxy or self._session is not None):
            return self.ready_info

        if self.use_proxy:
            mgr = self._resolve_proxy_manager()
            res = await mgr.probe(timeout=min(10.0, self.timeout))
            if not res.get("ready"):
                raise ChannelsProtocolTransportError(
                    str(res.get("error") or "内置视频号代理尚未就绪")
                )
            self.ready_info = {
                "ready": True,
                "transport": "channels-local-proxy",
                "url": str(res.get("url") or ""),
                "title": str(res.get("title") or ""),
                "capabilities": normalize_capabilities(res.get("capabilities")),
                "service": res,
            }
            return self.ready_info

        browser_ws, http_endpoint = self._resolve_ws_or_http()
        session: _AsyncCdpSession | None = None
        try:
            if browser_ws:
                session = await _AsyncCdpSession.open(browser_ws, timeout=self.timeout)
                targets_res = await session.call("Target.getTargets", {}, use_session=False)
                target = choose_finder_target(
                    targets_res.get("targetInfos") or [], self.target_id
                )
                attached = await session.call(
                    "Target.attachToTarget",
                    {"targetId": target["id"], "flatten": True},
                    use_session=False,
                )
                sid = str(attached.get("sessionId") or "")
                if not sid:
                    raise ChannelsProtocolTransportError("附加微信 Finder 页面未返回有效 sessionId")
                session.session_id = sid
            else:
                list_url = http_endpoint.rstrip("/") + "/json/list"
                try:
                    async with httpx.AsyncClient(timeout=min(6.0, self.timeout), trust_env=False) as cli:
                        resp = await cli.get(list_url)
                    if resp.status_code != 200:
                        raise ChannelsProtocolTransportError(f"CDP /json/list HTTP {resp.status_code}")
                    targets = resp.json()
                except ChannelsProtocolTransportError:
                    raise
                except Exception as exc:
                    raise ChannelsProtocolTransportError(
                        f"本地微信 CDP 端点不可达({sanitize_bridge_error(exc)})；桌面微信需开启调试转发通道"
                    ) from exc
                target = choose_finder_target(targets, self.target_id)
                ws_debugger_url = str(target.get("webSocketDebuggerUrl") or "")
                session = await _AsyncCdpSession.open(ws_debugger_url, timeout=self.timeout)

            await session.call("Runtime.enable")
            env = await session.evaluate(
                "(()=>({host:location.hostname,path:location.pathname,"
                "nativeBridge:typeof globalThis.WeixinJSBridge?.invoke==='function',"
                "wechatUA:/MicroMessenger|WindowsWechat/i.test(navigator.userAgent)}))()"
            )
            if (
                not isinstance(env, dict)
                or env.get("host") != "channels.weixin.qq.com"
                or not env.get("nativeBridge")
                or not env.get("wechatUA")
            ):
                raise ChannelsProtocolTransportError(
                    "目标页面不是微信原生视频号环境(缺少 WeixinJSBridge 或微信 UA；普通 Chrome 无法替代桌面微信)"
                )

            bootstrap_source = _BOOTSTRAP_JS_PATH.read_text("utf-8")
            await session.evaluate(bootstrap_source)
            mod_arg = "undefined" if not self.module_url else json.dumps(self.module_url)
            ready = await session.evaluate(f"__finderProtocolLoad({mod_arg})")
            if not isinstance(ready, dict) or not ready.get("ready"):
                raise ChannelsProtocolTransportError(
                    f"视频号页面服务模块未就绪: {ready.get('reason') if isinstance(ready, dict) else ready}"
                )

            self._session = session
            self.ready_info = {
                "ready": True,
                "transport": "wechat-main-world-cdp",
                "targetId": target["id"],
                "flattenedTargetSession": bool(session.session_id),
                "environment": env,
                "service": ready,
            }
            return self.ready_info
        except BaseException:
            if session is not None:
                await session.close()
            raise

    async def close(self) -> None:
        self.ready_info = {}
        if self._session is not None:
            session = self._session
            self._session = None
            await session.close()

    async def __aenter__(self) -> "ChannelsProtocolClient":
        await self.connect()
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.close()

    async def _eval_call(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        if self.use_proxy:
            mgr = self._resolve_proxy_manager()
            raw = await mgr.call_method(method, payload, timeout=self.timeout)
            if not isinstance(raw, dict):
                raise ChannelsProtocolTransportError(f"{method} 返回了非对象结果")
            return raw
        await self.connect()
        assert self._session is not None
        arg_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        raw = await self._session.evaluate(f"__finderProtocol.{method}({arg_json})")
        if not isinstance(raw, dict):
            raise ChannelsProtocolTransportError(f"{method} 返回了非对象结果")
        return raw

    async def probe(self) -> dict[str, Any]:
        if self.use_proxy:
            try:
                mgr = self._resolve_proxy_manager()
                return await mgr.probe(timeout=min(10.0, self.timeout))
            except Exception as exc:
                return {
                    "ok": False,
                    "ready": False,
                    "transport": "channels-local-proxy",
                    "error": sanitize_bridge_error(exc),
                }
            finally:
                await self.close()
        try:
            info = await self.connect()
            return {"ok": True, **info}
        except Exception as exc:
            return {
                "ok": False,
                "ready": False,
                "transport": "wechat-main-world-cdp",
                "error": sanitize_bridge_error(exc),
            }
        finally:
            await self.close()

    async def fetch_feed_detail(
        self,
        *,
        object_id: str = "",
        object_nonce_id: str = "",
        export_id: str = "",
        share_url: str = "",
    ) -> tuple[dict[str, Any], str]:
        """获取单条视频号作品完整 `ChannelsObject`(含加密 CDN 直链、`urlToken`、`decodeKey`、`objectNonceId`)。"""
        oid = (object_id or "").strip()
        nid = (object_nonce_id or "").strip()
        eid = (export_id or "").strip()

        if share_url and not (oid or eid):
            ref = parse_channels_url(share_url)
            if ref is not None:
                oid = ref.object_id or oid
                nid = ref.object_nonce_id or nid
                eid = ref.export_id or eid
                if ref.share_id and not eid:
                    try:
                        preview = await fetch_finder_preview_info(share_id=ref.share_id)
                        eid = str(preview.get("export_id") or "")
                    except Exception:
                        eid = ref.share_id

        if oid.startswith("sph:") and not eid:
            sid = oid[4:]
            try:
                preview = await fetch_finder_preview_info(share_id=sid)
                eid = str(preview.get("export_id") or sid)
            except Exception:
                eid = sid
            oid = ""

        if not oid and not eid:
            return {}, "缺少视频号作品 objectId 或 exportId"

        try:
            req_payload: dict[str, Any] = {}
            if eid and not oid:
                req_payload["encryptedObjectId"] = eid
            else:
                req_payload["objectId"] = server_id(oid)
                if nid:
                    req_payload["objectNonceId"] = nid
            resp = await self._eval_call("fetchFeedDetail", req_payload)
            err_code = resp.get("errCode", 0)
            data = resp.get("data") if isinstance(resp.get("data"), dict) else {}
            base_ret = (data.get("BaseResponse") or data.get("baseResponse") or {}).get("Ret", 0)
            if err_code not in (0, "0", None) or base_ret not in (0, "0", None):
                return {}, f"视频号详情接口返回错误(errCode={err_code}, Ret={base_ret})"
            obj = data.get("object") if isinstance(data.get("object"), dict) else {}
            if not obj:
                return {}, "视频号详情响应缺少 object 数据"
            if data.get("commentCount") not in (None, "") and not obj.get("commentCount"):
                obj = {**obj, "commentCount": data.get("commentCount")}
            if isinstance(data.get("countInfo"), dict) and not isinstance(obj.get("countInfo"), dict):
                obj = {**obj, "countInfo": data["countInfo"]}
            if isinstance(data.get("monotonicData"), dict) and not isinstance(obj.get("monotonicData"), dict):
                obj = {**obj, "monotonicData": data["monotonicData"]}
            return obj, ""
        except Exception as exc:
            return {}, f"视频号详情协议读取失败: {sanitize_bridge_error(exc)}"

    async def fetch_user_feeds(
        self,
        username: str,
        *,
        max_results: int = 20,
        max_pages: int = 10,
        stop_before: int = 0,
    ) -> tuple[list[dict[str, Any]], Optional[dict[str, Any]], str]:
        """分页读取任意视频号创作者(`v2_...@finder`)的主页作品列表与作者资料。"""
        clean_user = normalize_finder_username(username) or (username or "").strip()
        if not clean_user:
            return [], None, "缺少创作者 finderUsername"
        if not clean_user.endswith("@finder") and clean_user.startswith("v2_"):
            clean_user += "@finder"

        found: dict[str, dict[str, Any]] = {}
        author_contact: Optional[dict[str, Any]] = None
        cursor = ""
        seen_cursors: set[str] = set()
        pages = max(1, min(40, int(max_pages or 1)))
        limit = max(1, int(max_results or 20))

        try:
            for page_idx in range(pages):
                if page_idx > 0:
                    await asyncio.sleep(0.45)
                resp = await self._eval_call(
                    "fetchUserPage",
                    {"username": clean_user, "lastBuffer": cursor, "objectId": "0"},
                )
                err_code = resp.get("errCode", 0)
                data = resp.get("data") if isinstance(resp.get("data"), dict) else {}
                base_ret = (data.get("BaseResponse") or data.get("baseResponse") or {}).get("Ret", 0)
                if err_code not in (0, "0", None) or base_ret not in (0, "0", None):
                    return (
                        list(found.values())[:limit],
                        author_contact,
                        f"视频号主页接口返回错误(errCode={err_code}, Ret={base_ret})",
                    )
                if author_contact is None and isinstance(data.get("contact"), dict):
                    author_contact = dict(data["contact"])
                objects = (
                    data.get("object")
                    if isinstance(data.get("object"), list)
                    else data.get("objectList")
                    if isinstance(data.get("objectList"), list)
                    else data.get("objects")
                    if isinstance(data.get("objects"), list)
                    else []
                )
                timestamped = 0
                older_than_cutoff = 0
                for obj in objects:
                    if not isinstance(obj, dict):
                        continue
                    oid = str(obj.get("id") or obj.get("objectId") or "")
                    if not oid:
                        continue
                    if author_contact and not isinstance(obj.get("contact"), dict):
                        obj = {**obj, "contact": author_contact}
                    found.setdefault(oid, obj)
                    ct = int(obj.get("createtime") or obj.get("createTime") or 0)
                    if stop_before and ct:
                        timestamped += 1
                        if ct < stop_before:
                            older_than_cutoff += 1
                    if len(found) >= limit:
                        break
                reached_cutoff = bool(stop_before and timestamped and older_than_cutoff == timestamped)
                if len(found) >= limit or reached_cutoff:
                    break
                continue_flag = data.get("continueFlag")
                next_cursor = str(data.get("lastBuffer") or "")
                if continue_flag in (0, "0", False, None) or not next_cursor or next_cursor in seen_cursors:
                    break
                seen_cursors.add(next_cursor)
                cursor = next_cursor
            return list(found.values())[:limit], author_contact, ""
        except Exception as exc:
            return (
                list(found.values())[:limit],
                author_contact,
                f"视频号创作者作品读取失败: {sanitize_bridge_error(exc)}",
            )

    @staticmethod
    def _extract_search_page(
        data: dict[str, Any],
    ) -> tuple[list[dict[str, Any]], list[tuple[str, dict[str, Any]]]]:
        candidates: list[dict[str, Any]] = []
        if isinstance(data.get("objectList"), list):
            candidates.extend(x for x in data["objectList"] if isinstance(x, dict))
        if isinstance(data.get("object"), dict):
            candidates.append(data["object"])
        if isinstance(data.get("multiFeedStream"), list):
            for block in data["multiFeedStream"]:
                if not isinstance(block, dict):
                    continue
                if isinstance(block.get("object"), dict):
                    candidates.append(block["object"])
                for key in ("objects", "objectList", "feedList", "items"):
                    if isinstance(block.get(key), list):
                        candidates.extend(x for x in block[key] if isinstance(x, dict))
                if (
                    (block.get("id") or block.get("objectId"))
                    and not isinstance(block.get("object"), dict)
                ):
                    candidates.append(block)

        contacts: list[tuple[str, dict[str, Any]]] = []
        if isinstance(data.get("infoList"), list):
            for info in data["infoList"]:
                if not isinstance(info, dict):
                    continue
                c = info.get("contact") if isinstance(info.get("contact"), dict) else info
                uname = str(c.get("username") or "").strip() if isinstance(c, dict) else ""
                if uname and isinstance(c, dict):
                    contacts.append((uname, c))
        return candidates, contacts

    @staticmethod
    def _creator_feed_rank(obj: dict[str, Any], keyword: str) -> tuple[int, int, int]:
        aw = parse_channels_feed(obj)
        if aw is None:
            return (0, 0, 0)
        haystack = f"{aw.desc} {aw.author_name}".lower()
        terms = [t for t in keyword.lower().split() if t]
        kw_hit = int(
            bool(keyword)
            and (
                keyword.lower() in haystack
                or (len(terms) > 1 and all(t in haystack for t in terms))
            )
        )
        has_comments = int(aw.comment_count > 0)
        engagement = aw.comment_count * 3 + aw.like_count
        return (kw_hit, has_comments, engagement)

    @staticmethod
    def _has_feed_engagement(obj: dict[str, Any]) -> tuple[int, int]:
        aw = parse_channels_feed(obj)
        if aw is None:
            return (0, 0)
        return (1 if aw.comment_count > 0 else 0, 1 if aw.like_count > 0 else 0)

    async def search_feeds(
        self,
        keyword: str,
        *,
        max_results: int = 20,
        max_pages: int = 10,
        prefer_engaged: bool = False,
    ) -> tuple[list[dict[str, Any]], str]:
        """通过微信原生 `finderSearch` 搜索关键词作品(直出不足或全为0互动短片时自动从匹配创作者主页补齐)。"""
        clean_kw = (keyword or "").strip()
        if not clean_kw:
            return [], "搜索关键词不能为空"

        found: dict[str, dict[str, Any]] = {}
        matched_contacts: list[str] = []
        contact_map: dict[str, dict[str, Any]] = {}
        cursor = ""
        seen_cursors: set[str] = set()
        pages = max(1, min(40, int(max_pages or 1)))
        limit = max(1, int(max_results or 20))
        stagnant_direct_pages = 0

        try:
            for page_idx in range(pages):
                if page_idx > 0:
                    await asyncio.sleep(0.5)
                resp = await self._eval_call(
                    "search",
                    {"query": clean_kw, "scene": 13, "lastBuff": cursor},
                )
                err_code = resp.get("errCode", 0)
                data = resp.get("data") if isinstance(resp.get("data"), dict) else {}
                base_ret = (data.get("BaseResponse") or data.get("baseResponse") or {}).get("Ret", 0)
                if err_code not in (0, "0", None) or base_ret not in (0, "0", None):
                    return (
                        list(found.values())[:limit],
                        f"视频号搜索接口返回错误(errCode={err_code}, Ret={base_ret})",
                    )

                candidates, contacts = self._extract_search_page(data)
                before_count = len(found)
                for obj in candidates:
                    oid = str(obj.get("id") or obj.get("objectId") or "")
                    if oid:
                        found.setdefault(oid, obj)
                    if len(found) >= limit:
                        break

                for uname, contact_obj in contacts:
                    if uname not in contact_map:
                        matched_contacts.append(uname)
                        contact_map[uname] = contact_obj

                if len(found) >= limit:
                    commented_so_far = sum(
                        1 for item in found.values() if self._has_feed_engagement(item)[0] > 0
                    )
                    if (
                        not prefer_engaged
                        or matched_contacts
                        or commented_so_far >= min(limit, max(3, limit // 2))
                        or page_idx >= 1
                    ):
                        break
                if len(found) == before_count:
                    stagnant_direct_pages += 1
                    if stagnant_direct_pages >= (2 if matched_contacts else 3):
                        break
                else:
                    stagnant_direct_pages = 0

                continue_flag = data.get("continueFlag")
                next_cursor = str(data.get("lastBuff") or data.get("lastBuffer") or "")
                if continue_flag in (0, "0", False, None) or not next_cursor or next_cursor in seen_cursors:
                    break
                seen_cursors.add(next_cursor)
                cursor = next_cursor

            commented_direct = sum(
                1 for item in found.values() if self._has_feed_engagement(item)[0] > 0
            )
            needs_creator_backfill = bool(
                matched_contacts
                and (
                    len(found) < limit
                    or (prefer_engaged and commented_direct < min(limit, max(3, limit // 2)))
                )
            )
            if needs_creator_backfill:
                max_creators = min(len(matched_contacts), max(6, limit))
                per_creator_fetch = min(10, max(6, limit))
                creator_batches: list[list[dict[str, Any]]] = []
                relevant_pool = 0
                extra_target = limit if prefer_engaged and len(found) >= limit else (limit - len(found))
                for idx, uname in enumerate(matched_contacts[:max_creators]):
                    if idx > 0:
                        await asyncio.sleep(0.25)
                    sub_items, author_contact, _ = await self.fetch_user_feeds(
                        uname, max_results=per_creator_fetch, max_pages=1
                    )
                    fallback_contact = author_contact or contact_map.get(uname)
                    batch: list[dict[str, Any]] = []
                    for obj in sub_items:
                        oid = str(obj.get("id") or obj.get("objectId") or "")
                        if not oid or oid in found:
                            continue
                        if fallback_contact and not isinstance(obj.get("contact"), dict):
                            obj = {**obj, "contact": fallback_contact}
                        batch.append(obj)
                    if batch:
                        batch.sort(
                            key=lambda item: self._creator_feed_rank(item, clean_kw),
                            reverse=True,
                        )
                        creator_batches.append(batch)
                        relevant_pool += sum(
                            1 for item in batch if self._creator_feed_rank(item, clean_kw)[0] > 0
                        )
                        if relevant_pool >= extra_target * 2 and len(creator_batches) >= 3:
                            break

                extra_found: dict[str, dict[str, Any]] = {}
                # 轮询各匹配创作者，避免单一创作者占满所有名额
                while len(extra_found) < extra_target and any(creator_batches):
                    next_batches: list[list[dict[str, Any]]] = []
                    for batch in creator_batches:
                        while batch:
                            candidate = batch.pop(0)
                            oid = str(candidate.get("id") or candidate.get("objectId") or "")
                            if oid and oid not in found and oid not in extra_found:
                                extra_found[oid] = candidate
                                break
                        if batch:
                            next_batches.append(batch)
                        if len(extra_found) >= extra_target:
                            break
                    creator_batches = next_batches

                merged = list(found.values()) + list(extra_found.values())
                if prefer_engaged:
                    merged.sort(key=self._has_feed_engagement, reverse=True)
                return merged[:limit], ""

            return list(found.values())[:limit], ""
        except Exception as exc:
            return (
                list(found.values())[:limit],
                f"视频号关键词搜索失败: {sanitize_bridge_error(exc)}",
            )

    async def collect_comments(
        self,
        object_id: str,
        object_nonce_id: str = "",
        *,
        limit: int = 200,
        include_replies: bool = True,
        max_pages: int = 50,
    ) -> tuple[list[dict[str, Any]], dict[str, Any], str]:
        """使用 `pc_findergetcommentlist`(cmdid 10753) 采集主评论与各根评论独立二级回复线程。"""
        if limit <= 0:
            return [], {}, ""

        oid = (object_id or "").strip()
        nid = (object_nonce_id or "").strip()

        # 若传入的是分享短链 / exportId 或缺少 objectNonceId，先通过详情协议补齐精确 oid + nid
        if not oid.isdecimal() or not nid:
            detail_obj, detail_err = await self.fetch_feed_detail(
                object_id=oid if oid.isdecimal() else "",
                export_id="" if oid.isdecimal() else oid,
            )
            if detail_obj:
                oid = str(detail_obj.get("id") or detail_obj.get("objectId") or oid)
                nid = str(detail_obj.get("objectNonceId") or nid)
            elif not oid.isdecimal():
                return [], {}, detail_err or "无法将视频号目标解析为十进制 objectId"
            elif not nid:
                return [], {}, detail_err or "视频号评论协议缺少 objectNonceId"

        try:
            target = Target(object_id=server_id(oid), object_nonce_id=nid, entrance_id="1019")
        except Exception as exc:
            return [], {}, f"视频号评论参数校验失败: {exc}"

        seq = 0

        async def _transport(req: Request) -> Exchange:
            nonlocal seq
            seq += 1
            raw = await self._eval_call("fetchCommentsPage", req.payload)
            sanitized = safe_response(raw)
            return Exchange(
                request_id=req.request_id,
                response=sanitized,
                evidence_ref=(
                    f"proxy://exchange/{seq:04d}"
                    if self.use_proxy
                    else f"cdp://exchange/{seq:04d}"
                ),
                synthetic=False,
            )

        try:
            audit = await collect_comments_async(
                target,
                _transport,
                schema=ProtocolSchema(),
                include_replies=include_replies,
                max_pages=max(1, max_pages),
                max_records=limit,
                delay=0.35,
            )
        except Exception as exc:
            return [], {}, f"视频号评论协议采集失败: {sanitize_bridge_error(exc)}"

        stop_reason = str(
            audit.get("pagination_stop_reason")
            or (audit.get("main_pagination") or {}).get("pagination_stop_reason")
            or ""
        )
        err = ""
        if stop_reason.startswith("protocol_error:"):
            err = f"视频号评论协议中断: {stop_reason.split(':', 1)[1]}"

        normalized = [
            protocol_record_to_comment_dict(rec)
            for rec in audit.get("records") or []
        ]
        if not include_replies:
            normalized = [c for c in normalized if not c.get("reply_to")]
        return normalized[:limit], audit, err
