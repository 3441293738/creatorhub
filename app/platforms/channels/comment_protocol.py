"""微信视频号 (WeChat Channels / Finder) 原生评论协议分页、归属校验与去重模块。

基于真实微信 PC 页面 `feedTransferService.finderGetCommentList` 协议还原:
  feedTransferService.finderGetCommentList(payload)
    -> jsonToUnderline / JSON.stringify
    -> WeixinJSBridge.invoke("finderH5ExtTransfer", params, callback)
    -> /cgi-bin/micromsg-bin/pc_findergetcommentlist (PC cgi_cmdid = 10753, scope = finderInteraction)
    -> jsapi_resp.resp_json -> jsonToHump -> commentInfo / lastBuffer / downContinueFlag

核心不变量:
  1. `objectId` / `commentId` / `rootCommentId` / `replyCommentId` 全程保持精确十进制字符串，禁止 float/bool。
  2. `lastBuffer` 为不透明字符串游标，原样回传，不自行 base64 解码或重建。
  3. 主评论分页与每个根评论的二级回复线程各自维护独立游标，绝不混用。
  4. 仅当 `downContinueFlag == 0` 才判该分页链明确耗尽；缺标志、游标停滞/循环或协议错误均单独标记。
  5. 递归剔除任何认证会话字段与 `reportJson`，同时分别保留 `parent_comment_id` 与 `root_comment_id`。
"""
from __future__ import annotations

import asyncio
import copy
import datetime
import json
import queue
import re
import subprocess
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Awaitable, Callable

MAIN_ENDPOINT = "/cgi-bin/micromsg-bin/pc_findergetcommentlist"
ASYNC_INFO_ENDPOINT = "/cgi-bin/micromsg-bin/pc_finderasyncgetcommentinfo"
API_METHOD = "WXU.API.finderGetCommentList"
BRIDGE_METHOD = "finderH5ExtTransfer"
BRIDGE_CMDID = 10753
BRIDGE_SCOPE = "finderInteraction"

ERR_UNKNOWN = -70000
ERR_LOGIN_FAILED = -70001
ERR_TRANSFER_FAILED = -70002
ERR_JSONPARSE_FAILED = -70003
ERR_TIMEOUT = -70004

PUBLIC_FIELDS = frozenset({
    "username", "headUrl", "contentType", "replyContent",
    "imageInfo", "imageInfos", "images", "mentionedUserInfo",
    "authorContact", "interactionLabelList", "authorReactionInfo",
    "displayFlag", "extFlag", "likeFlag", "dislikeCount", "searchKeywordInfo",
})
_SENSITIVE_SUBSTRINGS = (
    "cookie", "session", "token", "ticket", "secret",
    "password", "signature", "authorization", "credential", "reportjson",
)
_ROW_SCALAR_FIELDS = frozenset({
    "commentId", "replyCommentId", "nickname", "content", "createtime",
    "likeCount", "expandCommentCount", "lastBuffer", "continueFlag",
    "upContinueFlag", "contentType", "replyContent", "username",
    "headUrl", "displayFlag", "extFlag", "likeFlag", "dislikeCount",
})


class ProtocolError(ValueError):
    """评论协议结构或字段不合法。"""


class UnknownProtocol(ProtocolError):
    """未识别的评论协议方法。"""


class AttributionError(ProtocolError):
    """请求/响应关联或目标作品归属不一致。"""


def hump_to_underline(key: str) -> str:
    """微信前端精确 `jsonToUnderline`: 大写字母开头的键(如 BaseResponse/Ret)保持原样。"""
    if not key or ("A" <= key[0] <= "Z"):
        return key
    return re.sub(r"([A-Z])", lambda m: "_" + m.group(1).lower(), key)


def underline_to_hump(key: str) -> str:
    """微信前端精确 `jsonToHump`: `_x` 转大写 `X`。"""
    return re.sub(r"_([A-Za-z0-9])", lambda m: m.group(1).upper(), key)


def _transform_keys(value: Any, fn: Callable[[str], str]) -> Any:
    if isinstance(value, list):
        return [_transform_keys(x, fn) for x in value]
    if isinstance(value, dict):
        return {fn(str(k)): _transform_keys(v, fn) for k, v in value.items()}
    return value


def to_snake_keys(value: Any) -> Any:
    return _transform_keys(value, hump_to_underline)


def to_camel_keys(value: Any) -> Any:
    return _transform_keys(value, underline_to_hump)


def server_id(value: Any) -> str:
    """校验并返回精确十进制服务端 ID，拒绝 bool/float/None。"""
    if isinstance(value, (bool, float)) or value is None:
        raise ProtocolError("ID 必须是精确十进制字符串或整数，禁止 float/bool")
    text = str(value)
    if not text.isdecimal():
        raise ProtocolError(f"非法的十进制服务端 ID: {text!r}")
    return text


def public_value(value: Any) -> Any:
    """递归剥离认证凭据/会话令牌与 reportJson。"""
    if isinstance(value, dict):
        return {
            k: public_value(v)
            for k, v in value.items()
            if not any(s in str(k).lower().replace("_", "") for s in _SENSITIVE_SUBSTRINGS)
        }
    if isinstance(value, list):
        return [public_value(x) for x in value]
    return copy.deepcopy(value)


def nonnegative_count(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, str) and value.isdecimal():
        return int(value)
    return None


def decode_json_layers(value: Any, max_layers: int = 5) -> dict[str, Any]:
    for _ in range(max_layers + 1):
        if isinstance(value, dict):
            return value
        if isinstance(value, (bytes, bytearray)):
            value = value.decode("utf-8", errors="strict")
        if not isinstance(value, str):
            raise ProtocolError("响应不是 JSON 对象或字符串")
        try:
            value = json.loads(value)
        except (ValueError, UnicodeError) as exc:
            raise ProtocolError("JSON 响应截断或格式损坏") from exc
    raise ProtocolError("JSON 嵌套层数超限")


def _at_path(value: Any, path: tuple[str, ...]) -> Any:
    for key in path:
        if not isinstance(value, dict) or key not in value:
            raise ProtocolError(f"响应缺少关键路径字段: {key}")
        value = value[key]
    return value


def build_native_bridge_params(payload: dict[str, Any], state: dict[str, Any] | None = None) -> dict[str, Any]:
    """还原 PC 微信 `ExtTransferService.computeParams` 生成 `finderH5ExtTransfer` 参数。"""
    st = dict(state or {})
    base_req = {
        **copy.deepcopy(st.get("finderBasereq") or {}),
        **copy.deepcopy(payload.get("finderBasereq") or {}),
    }
    data: dict[str, Any] = {
        "finderBasereq": base_req,
        "pcDeviceType": st.get("deviceType"),
        **copy.deepcopy(payload),
    }
    if not isinstance(data.get("finderBasereq"), dict):
        data["finderBasereq"] = {}
    if st.get("routeScene") is not None:
        data["finderBasereq"]["scene"] = st["routeScene"]
    if st.get("contextId"):
        data["finderBasereq"]["ctxInfo"] = {"contextId": st["contextId"]}
    if st.get("entranceId"):
        report: dict[str, Any] = {"entranceId": st["entranceId"]}
        if st.get("subEntranceid"):
            report["subEntranceid"] = st["subEntranceid"]
        ctx_info = dict(data["finderBasereq"].get("ctxInfo") or {})
        ctx_info["clientReportBuff"] = json.dumps(report, separators=(",", ":"))
        data["finderBasereq"]["ctxInfo"] = ctx_info

    snake_data = to_snake_keys(data)
    params: dict[str, Any] = {
        "req_json": json.dumps(snake_data, ensure_ascii=False, separators=(",", ":")),
        "url": MAIN_ENDPOINT,
        "cgi_cmdid": BRIDGE_CMDID,
        "h5AuthToken": "" if st.get("h5AuthToken") is None else st["h5AuthToken"],
        "is_security_check": False,
    }
    if not st.get("needLogin"):
        params["scope"] = BRIDGE_SCOPE
    return params


def parse_native_bridge_response(
    raw: Any, *, need_login: bool = False
) -> dict[str, Any]:
    """解析 `WeixinJSBridge.invoke('finderH5ExtTransfer', ...)` 原始回调。"""
    if not isinstance(raw, dict):
        return {"errCode": ERR_TRANSFER_FAILED, "errMsg": "Invalid bridge response", "data": None, "ok": False}
    jsapi_resp = raw.get("jsapi_resp") if isinstance(raw.get("jsapi_resp"), dict) else {}
    base_resp = raw.get("base_resp") if isinstance(raw.get("base_resp"), dict) else {}
    native_ret = jsapi_resp.get("ret")
    base_ret = base_resp.get("ret")
    err_msg = raw.get("err_msg")
    if isinstance(err_msg, str) and "access_denied" in err_msg:
        return {
            "errCode": ERR_TRANSFER_FAILED,
            "errMsg": "Bridge access denied",
            "data": None,
            "ok": False,
            "nativeRet": native_ret,
            "baseRet": base_ret,
        }
    if native_ret == -1:
        return {
            "errCode": ERR_TRANSFER_FAILED,
            "errMsg": "Native transfer system error",
            "data": None,
            "ok": False,
            "nativeRet": native_ret,
            "baseRet": base_ret,
        }
    if need_login and native_ret in (-13, -77):
        return {
            "errCode": ERR_LOGIN_FAILED,
            "errMsg": "Service authentication/refresh required",
            "data": None,
            "ok": False,
            "nativeRet": native_ret,
            "baseRet": base_ret,
            "requiresServiceAuth": True,
        }
    resp_json = jsapi_resp.get("resp_json")
    if not isinstance(resp_json, str) or not resp_json:
        return {
            "errCode": ERR_JSONPARSE_FAILED,
            "errMsg": "Missing resp_json",
            "data": None,
            "ok": False,
            "nativeRet": native_ret,
            "baseRet": base_ret,
        }
    try:
        parsed = json.loads(resp_json)
    except ValueError:
        return {
            "errCode": ERR_JSONPARSE_FAILED,
            "errMsg": "Malformed JSON",
            "data": None,
            "ok": False,
            "nativeRet": native_ret,
            "baseRet": base_ret,
        }
    data = to_camel_keys(parsed)
    biz_base = (
        data.get("baseResponse")
        or data.get("BaseResponse")
        or data.get("baseresponse")
        or {}
    ) if isinstance(data, dict) else {}
    biz_ret = biz_base.get("Ret", biz_base.get("ret")) if isinstance(biz_base, dict) else None
    return {
        "errCode": 0,
        "errMsg": "ok",
        "data": data,
        "ok": not (biz_ret is not None and biz_ret != 0),
        "businessRet": biz_ret,
        "nativeRet": native_ret,
        "baseRet": base_ret,
    }


@dataclass(frozen=True)
class ProtocolSchema:
    api_method: str = API_METHOD
    endpoint: str = MAIN_ENDPOINT
    scope: str = BRIDGE_SCOPE
    response_path: tuple[str, ...] = ("data",)
    list_field: str = "commentInfo"
    request_cursor_field: str = "lastBuffer"
    response_cursor_field: str = "lastBuffer"
    continue_field: str = "downContinueFlag"
    parent_request_field: str = "rootCommentId"


@dataclass(frozen=True)
class Target:
    object_id: str
    object_nonce_id: str = ""
    identity_scene: int = 2
    direction: int = 2
    enter_session_id: str | None = None
    entrance_id: str = "1002"

    def __post_init__(self) -> None:
        server_id(self.object_id)


@dataclass(frozen=True)
class Request:
    request_id: str
    api_method: str
    endpoint: str
    scope: str
    payload: dict[str, Any]
    level: str
    parent_id: str | None = None


@dataclass(frozen=True)
class Exchange:
    request_id: str
    response: Any
    evidence_ref: str = "memory://live"
    response_object_id: str | None = None
    synthetic: bool = False


@dataclass
class ParsedPage:
    records: list[dict[str, Any]]
    cursor: str | None
    continue_flag: int | None
    source_ref: str
    request_id: str
    synthetic: bool = False
    count_info: dict[str, Any] = field(default_factory=dict)
    quarantined: list[dict[str, Any]] = field(default_factory=list)


def build_request(
    target: Target,
    schema: ProtocolSchema | None = None,
    *,
    cursor: str | None = None,
    parent_id: str | None = None,
    now_ms: int | None = None,
) -> Request:
    schema = schema or ProtocolSchema()
    if schema.api_method != API_METHOD:
        raise UnknownProtocol("不支持的评论协议方法")
    if cursor is not None and not isinstance(cursor, str):
        raise ProtocolError("游标 lastBuffer 必须是不透明字符串")

    payload: dict[str, Any] = {
        "direction": target.direction,
        "identityScene": target.identity_scene,
        "objectId": server_id(target.object_id),
    }
    if parent_id is not None:
        payload[schema.parent_request_field] = server_id(parent_id)
    else:
        if not target.object_nonce_id:
            raise ProtocolError("主评论请求必须提供 objectNonceId")
        session_id = (
            target.enter_session_id
            if target.enter_session_id is not None
            else str(now_ms if now_ms is not None else time.time_ns() // 1_000_000)
        )
        payload.update(
            finderBasereq={
                "scene": 140,
                "ctxInfo": {
                    "clientReportBuff": json.dumps(
                        {"entranceId": target.entrance_id}, separators=(",", ":")
                    )
                },
                "objectBaseInfos": [],
            },
            objectNonceId=target.object_nonce_id,
            enterSessionId=session_id,
        )
    if cursor not in (None, ""):
        payload[schema.request_cursor_field] = cursor
    return Request(
        request_id=str(uuid.uuid4()),
        api_method=schema.api_method,
        endpoint=schema.endpoint,
        scope=schema.scope,
        payload=payload,
        level="reply" if parent_id else "main",
        parent_id=parent_id,
    )


def parse_exchange(
    request: Request,
    exchange: Exchange,
    schema: ProtocolSchema | None = None,
) -> ParsedPage:
    schema = schema or ProtocolSchema()
    if exchange.request_id != request.request_id:
        raise AttributionError("评论请求与响应 request_id 不匹配")
    if not exchange.evidence_ref:
        raise AttributionError("缺少响应证据引用标识")

    oid = request.payload["objectId"]
    if exchange.response_object_id is not None and server_id(exchange.response_object_id) != oid:
        raise AttributionError("响应归属另一个视频 objectId")

    root = decode_json_layers(exchange.response)
    body = decode_json_layers(_at_path(root, schema.response_path))
    for obj in (root, body, body.get("BaseResponse", {}), body.get("baseResponse", {})):
        if not isinstance(obj, dict):
            continue
        for key in ("Ret", "ret", "errCode", "err_code"):
            if key in obj and obj[key] not in (0, "0", None):
                err_detail = obj.get("errMsg") or obj.get("ErrMsg") or obj[key]
                raise ProtocolError(f"评论接口返回非零错误码: {key}={obj[key]} ({err_detail})")

    for ctx in (body, root.get("payload", {}), body.get("payload", {})):
        if isinstance(ctx, dict) and "objectId" in ctx and server_id(ctx["objectId"]) != oid:
            raise AttributionError("响应 payload.objectId 与请求不一致")

    if schema.list_field not in body or not isinstance(body[schema.list_field], list):
        raise ProtocolError("响应缺少 commentInfo 数组")

    flag = body.get(schema.continue_field)
    if flag is not None:
        if isinstance(flag, bool):
            flag = int(flag)
        if flag not in (0, 1, "0", "1"):
            raise ProtocolError(f"非法 downContinueFlag: {flag!r}")
        flag = int(flag)

    cursor = body.get(schema.response_cursor_field)
    if cursor is not None and not isinstance(cursor, str):
        raise ProtocolError("lastBuffer 不是字符串类型")

    records: list[dict[str, Any]] = []
    quarantined: list[dict[str, Any]] = []

    def visit(row: Any, pointer: str, container_parent: str | None = None) -> None:
        if not isinstance(row, dict):
            quarantined.append({"pointer": pointer, "reason": "not_object"})
            return
        try:
            if "objectId" in row and server_id(row["objectId"]) != oid:
                raise AttributionError("record_objectId_mismatch")
            cid = server_id(row.get("commentId"))
            content = row.get("content")
            if not isinstance(content, str):
                raise ProtocolError("content_not_text")
            if any(ord(c) < 32 and c not in "\n\r\t" for c in content):
                raise ProtocolError("literal_control_character")
            explicit = (
                server_id(row["replyCommentId"])
                if row.get("replyCommentId") not in (None, "")
                else None
            )
            root_parent = container_parent
            kind = (
                "reply"
                if root_parent or request.level == "reply" or explicit not in (None, "0")
                else "main"
            )
            nested = row.get("levelTwoComment") or []
            if not isinstance(nested, list):
                raise ProtocolError("levelTwoComment_not_list")

            ip_info = row.get("ipRegionInfo")
            ip_region = ip_info.get("regionText") if isinstance(ip_info, dict) else None
            rec: dict[str, Any] = {
                "object_id": oid,
                "comment_id": cid,
                "parent_comment_id": explicit if explicit != "0" else None,
                "root_comment_id": root_parent,
                "parent_source": "explicit_replyCommentId" if explicit is not None else "unknown",
                "source_kind": kind,
                "nickname": row.get("nickname") if isinstance(row.get("nickname"), str) else None,
                "content": content,
                "create_time_raw": row.get("createtime"),
                "like_count": nonnegative_count(row.get("likeCount")) or 0,
                "ip_region": ip_region if isinstance(ip_region, str) else None,
                "expand_comment_count": row.get("expandCommentCount"),
                "reply_continue_flag": row.get("continueFlag"),
                "reply_last_buffer": row.get("lastBuffer"),
                "embedded_reply_count": len(nested),
                "public_fields": {k: public_value(row[k]) for k in PUBLIC_FIELDS if k in row},
                "evidence": [
                    {
                        "response_ref": exchange.evidence_ref,
                        "json_pointer": pointer,
                        "request_id": request.request_id,
                    }
                ],
                "synthetic": exchange.synthetic,
            }
            records.append(rec)
            for idx, child in enumerate(nested):
                visit(child, f"{pointer}/levelTwoComment/{idx}", root_parent or cid)
        except ProtocolError as exc:
            quarantined.append({"pointer": pointer, "reason": str(exc)})

    prefix = "".join("/" + p for p in schema.response_path) + "/" + schema.list_field
    for idx, row in enumerate(body[schema.list_field]):
        visit(row, f"{prefix}/{idx}", request.parent_id)

    count_info = body.get("countInfo") if isinstance(body.get("countInfo"), dict) else {}
    return ParsedPage(
        records=records,
        cursor=cursor,
        continue_flag=flag,
        source_ref=exchange.evidence_ref,
        request_id=request.request_id,
        synthetic=exchange.synthetic,
        count_info=count_info,
        quarantined=quarantined,
    )


def safe_record(row: Any) -> dict[str, Any] | None:
    if not isinstance(row, dict):
        return None
    out: dict[str, Any] = {
        k: v
        for k, v in row.items()
        if k in _ROW_SCALAR_FIELDS and isinstance(v, (str, int, float, bool, type(None)))
    }
    if isinstance(row.get("ipRegionInfo"), dict):
        out["ipRegionInfo"] = {"regionText": row["ipRegionInfo"].get("regionText")}
    if isinstance(row.get("levelTwoComment"), list):
        out["levelTwoComment"] = [safe_record(c) for c in row["levelTwoComment"]]
    if "objectId" in row:
        out["objectId"] = row["objectId"]
    for key in PUBLIC_FIELDS:
        if key in row:
            out[key] = public_value(row[key])
    return out


def safe_response(raw: Any) -> dict[str, Any]:
    """生成不含任何账号凭据的白名单评论响应快照。"""
    root = decode_json_layers(raw)
    d = root.get("data", {})
    if not isinstance(d, dict):
        d = decode_json_layers(d)
    result: dict[str, Any] = {"errCode": root.get("errCode"), "data": {}}
    if isinstance(d.get("BaseResponse"), dict):
        result["data"]["BaseResponse"] = {"Ret": d["BaseResponse"].get("Ret")}
    for key in ("lastBuffer", "downContinueFlag", "upContinueFlag", "requestId", "objectId"):
        if key in d:
            result["data"][key] = d[key]
    if isinstance(d.get("commentInfo"), list):
        result["data"]["commentInfo"] = [safe_record(r) for r in d["commentInfo"]]
    raw_count_info = d.get("countInfo") if isinstance(d.get("countInfo"), dict) else {}
    mono = d.get("monotonicData") if isinstance(d.get("monotonicData"), dict) else {}
    mono_count_info = mono.get("countInfo") if isinstance(mono.get("countInfo"), dict) else {}
    if raw_count_info or mono_count_info:
        merged_counts: dict[str, Any] = {}
        for k in ("commentCount", "likeCount", "forwardCount", "favCount"):
            v = raw_count_info.get(k)
            if v in (None, "", 0, "0"):
                mv = mono_count_info.get(k)
                if mv not in (None, ""):
                    v = mv
            if isinstance(v, (int, str)) and not isinstance(v, bool):
                merged_counts[k] = v
        if merged_counts:
            result["data"]["countInfo"] = merged_counts
    if isinstance(root.get("payload"), dict) and "objectId" in root["payload"]:
        result["payload"] = {"objectId": root["payload"]["objectId"]}
    if isinstance(d.get("payload"), dict) and "objectId" in d["payload"]:
        result["data"]["payload"] = {"objectId": d["payload"]["objectId"]}
    return result


def protocol_record_to_comment_dict(rec: dict[str, Any]) -> dict[str, Any]:
    """将协议层唯一评论记录转换为 CreatorHub 统一入库格式。"""
    pub = rec.get("public_fields") if isinstance(rec.get("public_fields"), dict) else {}
    author_contact = pub.get("authorContact") if isinstance(pub.get("authorContact"), dict) else {}
    sec_uid = str(
        pub.get("username")
        or author_contact.get("username")
        or ""
    )
    raw_ts = rec.get("create_time_raw")
    ts = 0
    if isinstance(raw_ts, (int, float)):
        ts = int(raw_ts)
    elif isinstance(raw_ts, str) and raw_ts.strip().isdigit():
        ts = int(raw_ts.strip())
    if ts > 10_000_000_000:
        ts //= 1000

    parent_id = str(rec.get("parent_comment_id") or "")
    root_id = str(rec.get("root_comment_id") or "")
    if parent_id == "0":
        parent_id = ""
    reply_to = parent_id or (root_id if rec.get("source_kind") == "reply" else "")
    ip_region = str(rec.get("ip_region") or "").strip()
    return {
        "comment_id": str(rec.get("comment_id") or ""),
        "text": str(rec.get("content") or "").strip(),
        "user_nickname": str(rec.get("nickname") or author_contact.get("nickname") or ""),
        "user_sec_uid": sec_uid,
        "ip_label": ip_region,
        "like_count": int(rec.get("like_count") or 0),
        "create_time": ts,
        "reply_to": reply_to,
    }


def _needs_reply_thread(root_rec: dict[str, Any]) -> tuple[bool, str | None]:
    count = nonnegative_count(root_rec.get("expand_comment_count"))
    flag = root_rec.get("reply_continue_flag")
    unresolved = None
    if root_rec.get("expand_comment_count") is not None and count is None:
        unresolved = "invalid_expandCommentCount"
    elif count is None and flag not in (0, "0", 1, "1", None):
        unresolved = "reply_scope_unknown"
    should_query = bool(
        (count is not None and count > 0)
        or flag in (1, "1")
        or int(root_rec.get("embedded_reply_count") or 0) > 0
    )
    return should_query, unresolved


class CommentPager:
    """同步注入式评论分页器(用于自定义传输层与 CLI JSONL 子进程传输)。"""

    def __init__(
        self,
        target: Target,
        schema: ProtocolSchema,
        transport: Callable[[Request], Exchange],
        max_pages: int = 200,
    ) -> None:
        if max_pages < 1:
            raise ValueError("max_pages 必须大于 0")
        self.target = (
            target
            if target.enter_session_id is not None
            else replace(target, enter_session_id=str(time.time_ns() // 1_000_000))
        )
        self.schema = schema
        self.transport = transport
        self.max_pages = max_pages

    def collect(
        self,
        *,
        parent_id: str | None = None,
        start_cursor: str | None = None,
        max_records: int = 0,
    ) -> dict[str, Any]:
        cursor = start_cursor
        seen_cursors: set[str] = set()
        records: dict[tuple[str, str], dict[str, Any]] = {}
        pages: list[dict[str, Any]] = []
        quarantined: list[dict[str, Any]] = []
        reason = "page_limit"
        exhausted = False

        for _ in range(self.max_pages):
            req = build_request(self.target, self.schema, cursor=cursor, parent_id=parent_id)
            try:
                page = parse_exchange(req, self.transport(req), self.schema)
            except ProtocolError as exc:
                reason = f"protocol_error:{exc}"
                break
            pages.append({
                "request_id": req.request_id,
                "api_method": req.api_method,
                "evidence_ref": page.source_ref,
                "request_cursor": cursor,
                "response_cursor": page.cursor,
                "down_continue_flag": page.continue_flag,
                "records": len(page.records),
                "count_info": page.count_info,
                "synthetic": page.synthetic,
            })
            quarantined.extend(page.quarantined)
            for row in page.records:
                key = (row["object_id"], row["comment_id"])
                if key not in records:
                    records[key] = copy.deepcopy(row)
                    records[key]["content_variants"] = []
                old = records[key]
                if old["content"] != row["content"]:
                    old["content_variants"].append(
                        {"content": row["content"], "evidence": row["evidence"]}
                    )
                    old["content_conflict"] = True
                old["evidence"] = list(
                    {json.dumps(e, sort_keys=True): e for e in old["evidence"] + row["evidence"]}.values()
                )
            if page.continue_flag == 0:
                exhausted = True
                reason = "explicit_downContinueFlag_zero"
                break
            if max_records > 0 and len(records) >= max_records:
                reason = "record_limit"
                break
            if page.continue_flag is None:
                reason = "downContinueFlag_missing_not_exhaustion"
                break
            if not page.cursor:
                reason = "continue_without_cursor"
                break
            if page.cursor == cursor or page.cursor in seen_cursors:
                reason = "cursor_stalled_or_cycle"
                break
            seen_cursors.add(page.cursor)
            cursor = page.cursor

        return {
            "object_id": self.target.object_id,
            "schema": asdict(self.schema),
            "records": list(records.values()),
            "pages": pages,
            "quarantined": quarantined,
            "record_count": len(records),
            "pagination_exhausted": exhausted,
            "page_chain_exhausted": exhausted
            and not quarantined
            and not any(r.get("content_conflict") for r in records.values()),
            "pagination_stop_reason": reason,
            "complete": False,
            "synthetic": any(p["synthetic"] for p in pages),
        }


def collect_main_and_replies(
    target: Target,
    schema: ProtocolSchema,
    transport: Callable[[Request], Exchange],
    max_pages: int = 200,
    *,
    max_records: int = 0,
) -> dict[str, Any]:
    """同步采集主评论链与每个可见根评论的独立二级回复链。"""
    main = CommentPager(target, schema, transport, max_pages).collect(max_records=max_records)
    reply_runs: list[dict[str, Any]] = []
    unresolved_roots: list[dict[str, str]] = []
    roots = {r["comment_id"]: r for r in main["records"] if r["source_kind"] == "main"}

    for cid, root_rec in roots.items():
        if max_records > 0 and len(main["records"]) + sum(
            x["result"]["record_count"] for x in reply_runs
        ) >= max_records:
            break
        should_query, unresolved = _needs_reply_thread(root_rec)
        if unresolved:
            unresolved_roots.append({"root_comment_id": cid, "reason": unresolved})
        if should_query:
            reply_runs.append({
                "root_comment_id": cid,
                "result": CommentPager(target, schema, transport, max_pages).collect(parent_id=cid),
            })

    return _merge_comment_runs(target.object_id, main, reply_runs, unresolved_roots)


async def collect_comments_async(
    target: Target,
    transport: Callable[[Request], Awaitable[Exchange]],
    *,
    schema: ProtocolSchema | None = None,
    include_replies: bool = True,
    max_pages: int = 50,
    max_records: int = 0,
    delay: float = 0.35,
) -> dict[str, Any]:
    """异步采集主评论(及可选独立二级回复线程)，供 CreatorHub 异步引擎直接调用。"""
    schema = schema or ProtocolSchema()
    bound_target = (
        target
        if target.enter_session_id is not None
        else replace(target, enter_session_id=str(time.time_ns() // 1_000_000))
    )

    async def _collect_chain(parent_id: str | None = None, chain_limit: int = 0) -> dict[str, Any]:
        cursor: str | None = None
        seen_cursors: set[str] = set()
        records: dict[tuple[str, str], dict[str, Any]] = {}
        pages: list[dict[str, Any]] = []
        quarantined: list[dict[str, Any]] = []
        reason = "page_limit"
        exhausted = False

        for page_idx in range(max(1, max_pages)):
            if page_idx > 0 and delay > 0:
                await asyncio.sleep(delay)
            req = build_request(bound_target, schema, cursor=cursor, parent_id=parent_id)
            try:
                ex = await transport(req)
                page = parse_exchange(req, ex, schema)
            except ProtocolError as exc:
                reason = f"protocol_error:{exc}"
                break
            pages.append({
                "request_id": req.request_id,
                "api_method": req.api_method,
                "evidence_ref": page.source_ref,
                "request_cursor": cursor,
                "response_cursor": page.cursor,
                "down_continue_flag": page.continue_flag,
                "records": len(page.records),
                "count_info": page.count_info,
                "synthetic": page.synthetic,
            })
            quarantined.extend(page.quarantined)
            for row in page.records:
                key = (row["object_id"], row["comment_id"])
                if key not in records:
                    records[key] = copy.deepcopy(row)
                    records[key]["content_variants"] = []
                old = records[key]
                if old["content"] != row["content"]:
                    old["content_variants"].append(
                        {"content": row["content"], "evidence": row["evidence"]}
                    )
                    old["content_conflict"] = True
                old["evidence"] = list(
                    {json.dumps(e, sort_keys=True): e for e in old["evidence"] + row["evidence"]}.values()
                )
            if page.continue_flag == 0:
                exhausted = True
                reason = "explicit_downContinueFlag_zero"
                break
            if chain_limit > 0 and len(records) >= chain_limit:
                reason = "record_limit"
                break
            if page.continue_flag is None:
                reason = "downContinueFlag_missing_not_exhaustion"
                break
            if not page.cursor:
                reason = "continue_without_cursor"
                break
            if page.cursor == cursor or page.cursor in seen_cursors:
                reason = "cursor_stalled_or_cycle"
                break
            seen_cursors.add(page.cursor)
            cursor = page.cursor

        return {
            "object_id": bound_target.object_id,
            "schema": asdict(schema),
            "records": list(records.values()),
            "pages": pages,
            "quarantined": quarantined,
            "record_count": len(records),
            "pagination_exhausted": exhausted,
            "page_chain_exhausted": exhausted
            and not quarantined
            and not any(r.get("content_conflict") for r in records.values()),
            "pagination_stop_reason": reason,
            "complete": False,
            "synthetic": any(p["synthetic"] for p in pages),
        }

    main = await _collect_chain(parent_id=None, chain_limit=max_records)
    if not include_replies:
        main["all_accessible_pages_exhausted"] = False
        main["page_chains_exhausted"] = False
        return main

    reply_runs: list[dict[str, Any]] = []
    unresolved_roots: list[dict[str, str]] = []
    roots = {r["comment_id"]: r for r in main["records"] if r["source_kind"] == "main"}
    collected_unique = {(r["object_id"], r["comment_id"]) for r in main["records"]}

    for cid, root_rec in roots.items():
        if max_records > 0 and len(collected_unique) >= max_records:
            break
        should_query, unresolved = _needs_reply_thread(root_rec)
        if unresolved:
            unresolved_roots.append({"root_comment_id": cid, "reason": unresolved})
        if should_query:
            if delay > 0:
                await asyncio.sleep(delay)
            remaining = max(0, max_records - len(collected_unique)) if max_records > 0 else 0
            sub_res = await _collect_chain(parent_id=cid, chain_limit=remaining)
            for r in sub_res["records"]:
                collected_unique.add((r["object_id"], r["comment_id"]))
            reply_runs.append({"root_comment_id": cid, "result": sub_res})

    return _merge_comment_runs(bound_target.object_id, main, reply_runs, unresolved_roots)


def _merge_comment_runs(
    object_id: str,
    main: dict[str, Any],
    reply_runs: list[dict[str, Any]],
    unresolved_roots: list[dict[str, str]],
) -> dict[str, Any]:
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    conflicts: list[dict[str, Any]] = []
    runs = [main] + [x["result"] for x in reply_runs]
    for run in runs:
        for r in run["records"]:
            key = (r["object_id"], r["comment_id"])
            if key not in merged:
                merged[key] = copy.deepcopy(r)
            else:
                old = merged[key]
                if old["content"] != r["content"]:
                    conflicts.append({
                        "comment_id": r["comment_id"],
                        "contents": [old["content"], r["content"]],
                    })
                # 如果主列表里是内嵌二级回复，而独立回复链补齐了 root_comment_id，合并保留
                if not old.get("root_comment_id") and r.get("root_comment_id"):
                    old["root_comment_id"] = r["root_comment_id"]
                old["evidence"] = list(
                    {json.dumps(e, sort_keys=True): e for e in old["evidence"] + r["evidence"]}.values()
                )
    exhausted = (
        all(
            x["pagination_exhausted"]
            and not x["quarantined"]
            and not any(r.get("content_conflict") for r in x["records"])
            for x in runs
        )
        and not conflicts
        and not unresolved_roots
    )
    return {
        "object_id": object_id,
        "records": list(merged.values()),
        "record_count": len(merged),
        "main_pagination": main,
        "reply_pagination": reply_runs,
        "content_conflicts": conflicts,
        "unresolved_reply_roots": unresolved_roots,
        "all_accessible_pages_exhausted": exhausted,
        "page_chains_exhausted": exhausted,
        "complete": False,
        "synthetic": main["synthetic"] or any(x["result"]["synthetic"] for x in reply_runs),
    }


class JsonlBridgeTransport:
    """通过 JSONL 标准输入输出子进程调用已连接微信页面的传输适配器。"""

    def __init__(
        self,
        argv: list[str],
        evidence_dir: Path | None = None,
        *,
        timeout: float = 60.0,
        ready_timeout: float = 30.0,
        delay: float = 0.8,
    ) -> None:
        if not argv or not all(isinstance(x, str) for x in argv):
            raise ValueError("transport_command 必须是字符串参数列表")
        if delay < 0:
            raise ValueError("delay 不能为负数")
        self.delay = delay
        self.last_sent: float | None = None
        self.directory = evidence_dir
        if self.directory is not None:
            self.directory.mkdir(exist_ok=True, parents=True)
        self.timeout = timeout
        self.sequence = 0
        self.lines: queue.Queue[str | None] = queue.Queue()
        self.proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=None,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )

        def _reader() -> None:
            try:
                assert self.proc.stdout is not None
                for line in self.proc.stdout:
                    self.lines.put(line)
            finally:
                self.lines.put(None)

        self.thread = threading.Thread(target=_reader, daemon=True)
        self.thread.start()
        try:
            ready = self._next(ready_timeout)
            if (
                ready.get("id") is not None
                or ready.get("type") != "ready"
                or ready.get("ready") is not True
            ):
                raise ProtocolError("Bridge 未返回有效 ready 握手")
            self.ready = {
                "ready": True,
                "transport": ready.get("transport"),
                "origin": str(ready.get("origin", "")).split("?", 1)[0],
            }
        except Exception:
            self.close()
            raise

    def _next(self, seconds: float) -> dict[str, Any]:
        try:
            line = self.lines.get(timeout=seconds)
        except queue.Empty as exc:
            raise ProtocolError("Bridge 响应超时") from exc
        if line is None:
            raise ProtocolError("Bridge 子进程提前退出")
        try:
            obj = json.loads(line)
        except ValueError as exc:
            raise ProtocolError("Bridge 输出了非 JSON 内容") from exc
        if not isinstance(obj, dict):
            raise ProtocolError("Bridge 输出必须是 JSON 对象")
        return obj

    def __call__(self, request: Request) -> Exchange:
        if self.proc.poll() is not None:
            raise ProtocolError("Bridge 子进程已退出")
        if self.last_sent is not None:
            remaining = self.delay - (time.monotonic() - self.last_sent)
            if remaining > 0:
                time.sleep(remaining)
        self.last_sent = time.monotonic()
        assert self.proc.stdin is not None
        message = {"id": request.request_id, "payload": request.payload}
        self.proc.stdin.write(json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n")
        self.proc.stdin.flush()
        response = self._next(self.timeout)
        if response.get("id") != request.request_id:
            raise AttributionError("JSONL bridge 响应 ID 与请求不匹配")
        if response.get("ok") is not True:
            raise ProtocolError(f"Bridge 调用失败: {response.get('error') or 'unknown'}")
        if "result" not in response:
            raise ProtocolError("Bridge 成功响应缺少 result 字段")
        self.sequence += 1
        sanitized = safe_response(response["result"])
        ref = f"jsonl://exchange/{self.sequence:04d}"
        if self.directory is not None:
            path = self.directory / f"exchange_{self.sequence:04d}.json"
            path.write_text(
                json.dumps(
                    {
                        "captured_at": datetime.datetime.now().astimezone().isoformat(),
                        "request": {
                            "request_id": request.request_id,
                            "api_method": request.api_method,
                            "level": request.level,
                            "parent_id": request.parent_id,
                            "payload": request.payload,
                        },
                        "response": sanitized,
                        "synthetic": False,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            ref = str(path.resolve()) + "#/response"
        return Exchange(request.request_id, sanitized, ref, synthetic=False)

    def close(self) -> None:
        proc = getattr(self, "proc", None)
        if proc is None:
            return
        try:
            if proc.stdin:
                proc.stdin.close()
        except (OSError, BrokenPipeError):
            pass
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=3)
        if proc.stdout:
            proc.stdout.close()

    def __enter__(self) -> "JsonlBridgeTransport":
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()
