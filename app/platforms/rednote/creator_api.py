"""Rednote (海外小红书) experimental pure-protocol publisher (`rednote_publish_mode: "api"`).

Combines the recovered ``XYS_`` + ``b1`` + ``//webapi.rednote.com`` ``x-rap-param``
signing pipeline with Rednote-isolated media upload (supporting both ``ros-upload``
direct PUT and ``24352``–``24363`` 20 MiB Tencent COS multipart upload) and single-submit
``POST https://webapi.rednote.com/web_api/sns/v2/note`` safety boundaries.

Strict domain boundary:
  - Never contacts ``*.xiaohongshu.com`` for permits, uploads, topics, transcode, or submit.
  - Enforces a strict single-submit ``on_submit`` checkpoint before ``/web_api/sns/v2/note``;
    any post-submit ambiguity surfaces ``write_uncertain:`` rather than retrying.
"""
from __future__ import annotations

import json
import re
import threading
import time
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote, urlsplit

from curl_cffi import CurlOpt
from curl_cffi.curl import CURL_READFUNC_ABORT
from curl_cffi.requests import Session

from ...media_tools import video_cover_and_meta
from ...netfp import impersonate_for_ua
from ..xhs import creator_sign as xhs_cos_sign
from ..xhs.creator_api import (
    MAX_IMAGE_PIXELS,
    TRANSCODE_MAX_RETRIES,
    TRANSCODE_RETRY_DELAY,
    _image_info,
    _image_note_data,
    _video_cover_and_meta,
    _video_note_data,
)
from ..xhs.deadline import remaining_seconds
from ..xhs.media import (
    COPY_CHUNK_BYTES,
    MediaFile,
    media_size,
    validate_media_count,
    validate_media_size,
)
from ..xhs.responses import XhsApiError, check_http_status, parse_response
from .domains import CREATOR_URL, WEBAPI_URL, is_rednote_url
from .formatter import (
    MAX_DESC_WEIGHT,
    MAX_IMAGES,
    MAX_IMAGE_DESC_UTF16,
    MAX_TITLE_WEIGHT,
    MAX_TOPICS,
    utf16_code_units,
    weighted_truncate,
)
from .resolve import build_rednote_note_url
from .signer import (
    RednoteSignContext,
    RednoteSigner,
    canonical_request,
    parse_cookie_header,
    resolve_rednote_sign_context,
)

DEFAULT_REDNOTE_UPLOAD_URL = "https://ros-upload.rednote.com"
VIDEO_MULTIPART_CHUNK_BYTES = 20 * 1024 * 1024  # 20 MiB (literal 24363: 20971520)
_DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36"
)
_ALLOWED_UPLOAD_SUFFIXES = (".rednote.com", ".xhscdn.com", ".myqcloud.com")
_HOST_LABEL_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
_SEGMENT_RE = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9._-]*")


class RednotePublishError(XhsApiError):
    """Structured error raised during Rednote pure-API publishing."""


def _normalize_rednote_upload_origin(address: object) -> Tuple[str, bool]:
    """Validate upload host, rewrite ``ap-shanghai`` -> ``accelerate`` (`24352`), and reject ``xiaohongshu.com``.

    Returns ``(https_origin, is_cos_bucket)``.
    """
    if address is None or address == "":
        return DEFAULT_REDNOTE_UPLOAD_URL, False
    err_msg = "海外小红书上传地址须为受信任 HTTPS 域名（rednote.com / xhscdn.com / myqcloud.com），严禁使用 xiaohongshu.com"
    if (
        not isinstance(address, str)
        or address.startswith("//")
        or any(ord(ch) <= 0x20 or ord(ch) >= 0x7F or ch in "\\?#" for ch in address)
    ):
        raise RednotePublishError(err_msg, signal="invalid_upload_destination")
    rewritten = address.replace("ap-shanghai", "accelerate")
    try:
        parts = urlsplit(rewritten if "://" in rewritten else f"https://{rewritten}")
        host = (parts.hostname or "").lower()
        if (
            parts.scheme != "https"
            or parts.username is not None
            or parts.password is not None
            or parts.port not in (None, 443)
            or parts.path not in ("", "/")
            or not host
            or len(host) > 253
            or host == "xiaohongshu.com"
            or host.endswith(".xiaohongshu.com")
            or not any(host == suf.lstrip(".") or host.endswith(suf) for suf in _ALLOWED_UPLOAD_SUFFIXES)
            or not all(_HOST_LABEL_RE.fullmatch(lbl) for lbl in host.split("."))
        ):
            raise ValueError("disallowed upload host")
    except ValueError as exc:
        raise RednotePublishError(err_msg, signal="invalid_upload_destination") from exc
    is_cos_bucket = host.endswith(".myqcloud.com")
    return f"https://{host}", is_cos_bucket


def _extract_upload_permit(payload: dict, xt: str) -> dict[str, Any]:
    """Validate single-file upload permit (`creator/permit` or `web/permit` `24352`)."""
    try:
        permits = (payload.get("data") or {}).get("uploadTempPermits")
        if not isinstance(permits, list) or not permits or not isinstance(permits[0], dict):
            raise ValueError("missing uploadTempPermits")
        permit = permits[0]
        file_ids = permit.get("fileIds")
        if not isinstance(file_ids, list) or not file_ids or not isinstance(file_ids[0], str):
            raise ValueError("missing fileIds")
        raw_prefix = file_ids[0].strip("/")
        segments = raw_prefix.split("/")
        if not segments or not all(_SEGMENT_RE.fullmatch(part) for part in segments):
            raise ValueError("invalid fileId segments")
        token = permit.get("token")
        if not isinstance(token, str) or not token or any(not 0x21 <= ord(c) <= 0x7E for c in token):
            raise ValueError("invalid upload token")
        expire_raw = permit.get("expireTime")
        sign_time = ""
        if expire_raw is not None:
            times = []
            for value in (xt, expire_raw):
                if isinstance(value, bool) or not isinstance(value, (str, int)):
                    raise ValueError("invalid permit timestamp")
                text = str(value)
                if not re.fullmatch(r"[0-9]{1,16}", text) or int(text) <= 0:
                    raise ValueError("invalid permit timestamp")
                times.append(text[:10])
            sign_time = ";".join(times)
    except (KeyError, TypeError, ValueError, IndexError) as exc:
        raise RednotePublishError(
            "海外小红书上传凭证结构异常，本次未上传媒体",
            category="risk",
            signal="ambiguous_response",
        ) from exc

    origin, is_cos_bucket = _normalize_rednote_upload_origin(permit.get("uploadAddr"))
    return {
        "origin": origin,
        "host": urlsplit(origin).hostname or "",
        "is_cos_bucket": is_cos_bucket,
        "prefix": raw_prefix,
        "file_id": segments[-1],
        "token": token,
        "sign_time": sign_time,
    }


def _parse_initiate_upload_id(xml_text: str) -> str:
    """Safely extract ``<UploadId>`` from COS ``InitiateMultipartUploadResult`` XML (`24355`)."""
    raw = str(xml_text or "").strip()
    if not raw or "<!DOCTYPE" in raw.upper() or "<!ENTITY" in raw.upper():
        raise RednotePublishError("COS 分片初始化响应异常", category="risk", signal="ambiguous_response")
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise RednotePublishError("COS 分片初始化 XML 解析失败", category="risk", signal="ambiguous_response") from exc
    for elem in root.iter():
        tag = elem.tag.rsplit("}", 1)[-1]
        if tag == "UploadId" and elem.text and elem.text.strip():
            return elem.text.strip()
    raise RednotePublishError("COS 分片初始化未返回 UploadId", category="risk", signal="ambiguous_response")


def _build_complete_multipart_xml(parts: List[Tuple[int, str]]) -> bytes:
    """Serialize ``CompleteMultipartUpload`` XML (`24363`)."""
    root = ET.Element("CompleteMultipartUpload")
    for part_no, etag in parts:
        part_el = ET.SubElement(root, "Part")
        num_el = ET.SubElement(part_el, "PartNumber")
        num_el.text = str(int(part_no))
        etag_el = ET.SubElement(part_el, "ETag")
        etag_el.text = str(etag)
    return ET.tostring(root, encoding="utf-8", xml_declaration=False)


class RednoteCreatorApi:
    """Synchronous ``curl_cffi`` + ``RednoteSigner`` (`XYS_` + ``b1``) publisher for Rednote."""

    def __init__(
        self,
        cookie_str: str,
        *,
        storage_state: str = "",
        sign_context: RednoteSignContext | None = None,
        user_agent: str = _DEFAULT_UA,
        locale: str = "en-US",
        timezone: str = "UTC",
        timeout: float = 30.0,
        proxy: str = "",
        cancel_event: threading.Event | None = None,
        deadline: float | None = None,
    ):
        self._cancel_event = cancel_event
        self._deadline = deadline
        self.cookie_str = cookie_str or ""
        self.cookies = parse_cookie_header(self.cookie_str)
        if not self.cookies.get("a1"):
            raise RednotePublishError(
                "海外小红书登录态缺少 rednote.com 的 a1 Cookie，请先完成扫码或 Cookie 登录",
                category="auth",
                signal="auth_expired",
            )
        self.a1 = self.cookies["a1"]
        self.user_agent = str(user_agent or _DEFAULT_UA).strip()
        self._signer = RednoteSigner()
        self.sign_context = resolve_rednote_sign_context(
            self.cookie_str,
            storage_state=storage_state,
            explicit_context=sign_context,
            user_agent=self.user_agent,
            locale=locale,
            timezone=timezone,
        )
        from ...browser.manager import normalize_proxy

        norm_proxy = normalize_proxy(proxy) or None
        self._session_cls = Session
        self._session_options = {
            "timeout": timeout,
            "impersonate": impersonate_for_ua(self.user_agent),
            "proxies": {"http": norm_proxy, "https": norm_proxy} if norm_proxy else None,
            "discard_cookies": True,
            "allow_redirects": False,
        }
        self._upload_cli: Session | None = None
        self.cli = self._session_cls(**self._session_options)

    def close(self) -> None:
        for client in (self._upload_cli, self.cli):
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass

    def _check_cancelled(self):
        return remaining_seconds(self._deadline, self._cancel_event)

    def _base_headers(self) -> Dict[str, str]:
        return {
            "user-agent": self.user_agent,
            "accept": "application/json, text/plain, */*",
            "content-type": "application/json;charset=UTF-8",
            "origin": CREATOR_URL,
            "referer": f"{CREATOR_URL}/",
            "sec-fetch-site": "same-site",
            "sec-fetch-mode": "cors",
            "sec-fetch-dest": "empty",
            "accept-language": "en-US,en;q=0.9,zh-CN;q=0.8",
        }

    def _send(self, client: Session, method: str, url: str, **kwargs):
        remaining = self._check_cancelled()
        if remaining is not None:
            kwargs["timeout"] = min(self._session_options["timeout"], remaining)
        try:
            response = getattr(client, method)(url, **kwargs)
        except Exception:
            self._check_cancelled()
            raise
        self._check_cancelled()
        return response

    def _api_request(self, method: str, url: str, **kwargs):
        if not is_rednote_url(url):
            raise RednotePublishError(
                f"拒绝向非 rednote.com 域名发送创作者请求: {url}",
                category="risk",
                signal="cross_domain_blocked",
            )
        return self._send(self.cli, method, url, **kwargs)

    def _ensure_upload_cli(self) -> Session:
        if self._upload_cli is None:
            self._upload_cli = self._session_cls(**self._session_options)
        return self._upload_cli

    def _signed_get(self, base_url: str, uri: str, params: Dict[str, Any] | None = None) -> Tuple[dict, str]:
        query = params or {}
        sign = self._signer.sign_headers(
            "GET",
            uri,
            self.cookie_str,
            params=query,
            sign_format="xys",
            b1=self.sign_context.b1,
            dsl_pair=self.sign_context.dsl_pair,
            sign_count=self.sign_context.next_sign_count(),
        )
        headers = {**self._base_headers(), **sign}
        full_url = base_url + canonical_request("GET", uri, query)
        resp = self._api_request("get", full_url, headers=headers, cookies=self.cookies)
        return parse_response(resp), sign["x-t"]

    def _signed_post(
        self,
        base_url: str,
        uri: str,
        payload: Dict[str, Any],
        *,
        x_rap: bool | None = None,
    ) -> dict:
        sign = self._signer.sign_headers(
            "POST",
            uri,
            self.cookie_str,
            payload=payload,
            sign_format="xys",
            x_rap=x_rap,
            b1=self.sign_context.b1,
            dsl_pair=self.sign_context.dsl_pair,
            sign_count=self.sign_context.next_sign_count(),
        )
        headers = {**self._base_headers(), **sign}
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        resp = self._api_request(
            "post",
            base_url + uri,
            headers=headers,
            data=body.encode("utf-8"),
            cookies=self.cookies,
        )
        return parse_response(resp)

    def _upload_put_stream(self, url: str, source: bytes | bytearray | MediaFile, headers: Dict[str, str]):
        self._check_cancelled()
        upload_cli = self._ensure_upload_cli()
        if not isinstance(source, MediaFile):
            return self._send(upload_cli, "put", url, headers=headers, data=bytes(source))
        with source.path.open("rb") as stream:
            read_error = None
            sent = 0

            def read_chunk(max_size: int):
                nonlocal read_error, sent
                try:
                    self._check_cancelled()
                    chunk = stream.read(min(max_size, COPY_CHUNK_BYTES, source.size - sent))
                    if not chunk and sent < source.size:
                        raise ValueError("媒体快照在上传期间变短")
                    sent += len(chunk)
                    return chunk
                except Exception as exc:
                    read_error = exc
                    return CURL_READFUNC_ABORT

            previous = upload_cli.curl_options
            upload_cli.curl_options = {
                **previous,
                CurlOpt.UPLOAD: 1,
                CurlOpt.INFILESIZE_LARGE: source.size,
                CurlOpt.READFUNCTION: read_chunk,
            }
            try:
                try:
                    response = self._send(upload_cli, "put", url, headers=headers)
                except Exception:
                    if read_error is not None:
                        raise read_error
                    raise
                if read_error is not None:
                    raise read_error
                if 200 <= response.status_code < 300 and sent != source.size:
                    raise RednotePublishError("海外小红书媒体上传未传完，本次未提交发布")
                return response
            finally:
                upload_cli.curl_options = previous

    def get_upload_permit(self, media_type: str) -> dict[str, Any]:
        """Request upload permit from ``creator.rednote.com`` or fallback ``webapi.rednote.com`` (`24352`)."""
        params = {
            "biz_name": "spectrum",
            "scene": media_type,
            "file_count": "1",
            "version": "1",
            "source": "web",
        }
        last_exc: Exception | None = None
        for base_url, api_path in (
            (CREATOR_URL, "/api/media/v1/upload/creator/permit"),
            (WEBAPI_URL, "/api/media/v1/upload/web/permit"),
        ):
            try:
                payload, xt = self._signed_get(base_url, api_path, params)
                return _extract_upload_permit(payload, xt)
            except XhsApiError as exc:
                last_exc = exc
                if exc.category == "auth":
                    raise
                continue
        assert last_exc is not None
        raise last_exc

    def _upload_cos_multipart_video(
        self, permit: dict[str, Any], file_source: bytes | bytearray | MediaFile, file_size: int
    ) -> str:
        """Execute ``24355`` -> ``24359`` -> ``24363`` 20 MiB COS multipart upload."""
        upload_cli = self._ensure_upload_cli()
        origin = permit["origin"]
        prefix = permit["prefix"]
        object_path = prefix if prefix.startswith("spectrum/") else f"spectrum/{prefix}"
        object_url = f"{origin}/{object_path}"
        cos_headers = {
            "user-agent": self.user_agent,
            "origin": CREATOR_URL,
            "referer": f"{CREATOR_URL}/",
            "x-cos-security-token": permit["token"],
        }
        # 1) Preflight & initiate multipart upload (24355)
        q_prefix = quote(prefix, safe="/")
        self._send(upload_cli, "get", f"{origin}?uploads=&prefix={q_prefix}", headers=cos_headers)
        init_resp = self._send(upload_cli, "post", f"{object_url}?uploads", headers=cos_headers)
        check_http_status(init_resp.status_code)
        upload_id = _parse_initiate_upload_id(init_resp.text)

        # 2) Serial 20 MiB part uploads (24359 & 24363)
        parts: List[Tuple[int, str]] = []
        q_uid = quote(upload_id, safe="")
        if isinstance(file_source, MediaFile):
            with file_source.path.open("rb") as fp:
                part_no = 1
                while True:
                    self._check_cancelled()
                    chunk = fp.read(VIDEO_MULTIPART_CHUNK_BYTES)
                    if not chunk:
                        break
                    part_resp = self._send(
                        upload_cli,
                        "put",
                        f"{object_url}?partNumber={part_no}&uploadId={q_uid}",
                        headers=cos_headers,
                        data=chunk,
                    )
                    check_http_status(part_resp.status_code)
                    etag = part_resp.headers.get("ETag") or part_resp.headers.get("etag") or ""
                    if not etag:
                        raise RednotePublishError(f"COS 分片 #{part_no} 缺少 ETag")
                    parts.append((part_no, etag))
                    part_no += 1
        else:
            raw_bytes = bytes(file_source)
            for idx in range(0, max(1, file_size), VIDEO_MULTIPART_CHUNK_BYTES):
                part_no = (idx // VIDEO_MULTIPART_CHUNK_BYTES) + 1
                chunk = raw_bytes[idx : idx + VIDEO_MULTIPART_CHUNK_BYTES]
                part_resp = self._send(
                    upload_cli,
                    "put",
                    f"{object_url}?partNumber={part_no}&uploadId={q_uid}",
                    headers=cos_headers,
                    data=chunk,
                )
                check_http_status(part_resp.status_code)
                etag = part_resp.headers.get("ETag") or part_resp.headers.get("etag") or ""
                if not etag:
                    raise RednotePublishError(f"COS 分片 #{part_no} 缺少 ETag")
                parts.append((part_no, etag))

        # 3) CompleteMultipartUpload XML (24363)
        complete_xml = _build_complete_multipart_xml(parts)
        comp_resp = self._send(
            upload_cli,
            "post",
            f"{object_url}?uploadId={q_uid}",
            headers={**cos_headers, "content-type": "application/xml"},
            data=complete_xml,
        )
        check_http_status(comp_resp.status_code)
        return (
            comp_resp.headers.get("X-Ros-Video-Id")
            or comp_resp.headers.get("x-ros-video-id")
            or ""
        )

    def upload_media(self, file_source: bytes | bytearray | MediaFile, media_type: str) -> dict[str, Any]:
        try:
            if media_type not in ("image", "video"):
                raise ValueError("上传媒体类型无效")
            file_size = media_size(file_source)
            validate_media_size(media_type, file_size, file_size)
        except (OSError, ValueError) as exc:
            raise RednotePublishError(str(exc)) from exc

        permit = self.get_upload_permit(media_type)
        file_id = permit["file_id"]
        res: dict[str, Any] = {
            "fileIds": file_id,
            "width": "",
            "height": "",
            "video_id": "",
            "file_size": file_size,
        }
        if media_type == "image":
            w, h, checked_size, mime_type = _image_info(file_source)
            res.update(width=w, height=h, file_size=checked_size, mime_type=mime_type)
            file_size = checked_size

        if permit["is_cos_bucket"] and media_type == "video":
            vid = self._upload_cos_multipart_video(permit, file_source, file_size)
            res["video_id"] = vid
            return res

        origin = permit["origin"]
        host = permit["host"]
        prefix = permit["prefix"]
        object_path = prefix if prefix.startswith("spectrum/") else f"spectrum/{file_id}"
        headers = {
            "accept": "*/*",
            "origin": CREATOR_URL,
            "referer": f"{CREATOR_URL}/",
            "user-agent": self.user_agent,
            "content-type": "",
            "x-cos-security-token": permit["token"],
        }
        if permit["sign_time"] and not permit["is_cos_bucket"]:
            msg = permit["sign_time"]
            sig = xhs_cos_sign.cos_signature(msg, file_id, file_size, host)
            headers["authorization"] = (
                f"q-sign-algorithm=sha1&q-ak=null&q-sign-time={msg}"
                f"&q-key-time={msg}&q-header-list=content-length;host"
                f"&q-url-param-list=&q-signature={sig}"
            )
        resp = self._upload_put_stream(f"{origin}/{object_path}", file_source, headers)
        check_http_status(resp.status_code)
        if media_type == "video":
            vid = resp.headers.get("X-Ros-Video-Id") or resp.headers.get("x-ros-video-id") or ""
            if not vid and not permit["is_cos_bucket"]:
                raise RednotePublishError("海外小红书视频上传响应缺少 X-Ros-Video-Id")
            res["video_id"] = vid
        return res

    def query_transcode(self, video_id: str) -> dict:
        params = {"video_id": str(video_id), "need_transcode": "false", "resource_type": "0"}
        payload, _ = self._signed_get(WEBAPI_URL, "/web_api/sns/capa/postgw/query_transcode", params)
        return payload

    def _poll_delay(self) -> None:
        remaining = self._check_cancelled()
        delay = TRANSCODE_RETRY_DELAY if remaining is None else min(TRANSCODE_RETRY_DELAY, remaining)
        if self._cancel_event is None:
            time.sleep(delay)
        else:
            self._cancel_event.wait(delay)
        self._check_cancelled()

    def resolve_topic(self, keyword: str, title: str = "") -> Optional[dict]:
        """Resolve topic via ``24397`` search -> exact match -> ``24402`` custom topic."""
        clean = str(keyword or "").strip().lstrip("#").strip()[:50]
        if not clean:
            return None
        search_payload = {
            "keyword": clean[:20],
            "suggest_topic_request": {"title": title, "desc": f"#{clean[:20]}"},
            "page": {"page_size": 20, "page": 1},
        }
        try:
            res = self._signed_post(WEBAPI_URL, "/web_api/sns/v1/search/topic", search_payload)
            dtos = ((res.get("data") or {}).get("topic_info_dtos")) or []
            if isinstance(dtos, list):
                for row in dtos:
                    if isinstance(row, dict) and str(row.get("name") or "").strip() == clean:
                        return row
                if dtos and isinstance(dtos[0], dict):
                    return dtos[0]
        except XhsApiError:
            pass

        try:
            custom = self._signed_post(
                WEBAPI_URL,
                "/web_api/sns/capa/postgw/topic/batch_customized",
                {"topic_names": [clean]},
            )
            infos = ((custom.get("data") or {}).get("topic_infos")) or []
            if isinstance(infos, list) and infos and isinstance(infos[0], dict):
                return infos[0]
        except XhsApiError:
            pass
        return None

    def post_note(
        self,
        *,
        media_type: str,
        title: str,
        desc: str,
        image_files: Optional[List[bytes | MediaFile]] = None,
        video_file: Optional[bytes | MediaFile] = None,
        topics: Optional[List[str]] = None,
        post_time_ms: Optional[int] = None,
        privacy_type: int = 0,
        on_submit=None,
    ) -> Tuple[bool, str, dict]:
        """Upload media, mark single-submit ``on_submit`` checkpoint, and POST ``/web_api/sns/v2/note``."""
        self._check_cancelled()
        norm_title = weighted_truncate(str(title or "").strip(), MAX_TITLE_WEIGHT)
        norm_desc = weighted_truncate(str(desc or ""), MAX_DESC_WEIGHT)
        if media_type != "video" and utf16_code_units(norm_desc) > MAX_IMAGE_DESC_UTF16:
            raise RednotePublishError(
                f"海外小红书图文正文不能超过 {MAX_IMAGE_DESC_UTF16} 个 UTF-16 编码单元"
            )

        try:
            if media_type == "video":
                validate_media_count("video", 1 if video_file else 0)
                blobs: List[bytes | MediaFile] = [video_file]  # type: ignore[list-item]
            else:
                if not isinstance(image_files, (list, tuple)):
                    raise ValueError("图片须为媒体内容列表")
                clipped = list(image_files)[:MAX_IMAGES]
                validate_media_count("image", len(clipped))
                blobs = clipped
            total_bytes = 0
            for blob in blobs:
                sz = media_size(blob)
                total_bytes += sz
                validate_media_size("video" if media_type == "video" else "image", sz, total_bytes)
        except (OSError, ValueError) as exc:
            raise RednotePublishError(str(exc)) from exc

        post_loc: dict = {}
        if media_type == "video":
            if not video_file:
                raise RednotePublishError("缺少视频文件")
            cover, metadata = _video_cover_and_meta(video_file)
            file_info = self.upload_media(video_file, "video")
            cover_info = self.upload_media(cover, "image")
            if file_info.get("video_id"):
                ready = False
                for attempt in range(TRANSCODE_MAX_RETRIES):
                    res = self.query_transcode(file_info["video_id"])
                    d = res.get("data") or {}
                    if not d:
                        raise RednotePublishError(
                            "海外小红书视频转码响应缺少状态，本次未提交发布",
                            category="risk",
                            signal="ambiguous_response",
                        )
                    status = d.get("status")
                    if isinstance(status, str) and status.lower() in {"failed", "failure", "error"}:
                        raise RednotePublishError("海外小红书视频转码失败，本次未提交发布")
                    frame_id = d.get("firstFrameFileId") or d.get("first_frame_file_id")
                    if (
                        d.get("hasFirstFrame") is True
                        or d.get("has_first_frame") is True
                        or (isinstance(frame_id, str) and frame_id.strip())
                        or status in (2, "success", "SUCCESS")
                    ):
                        ready = True
                        break
                    if attempt + 1 < TRANSCODE_MAX_RETRIES:
                        self._poll_delay()
                if not ready:
                    raise RednotePublishError("海外小红书视频转码等待超时，本次未提交发布")
            data = _video_note_data(
                norm_title, norm_desc, post_time_ms, post_loc, privacy_type, file_info, cover_info, metadata
            )
        else:
            infos = [self.upload_media(b, "image") for b in blobs]
            data = _image_note_data(norm_title, norm_desc, post_time_ms, post_loc, privacy_type, infos)

        for raw_topic in list(topics or [])[:MAX_TOPICS]:
            clean = str(raw_topic or "").strip().lstrip("#").strip()[:50]
            if not clean:
                continue
            resolved = self.resolve_topic(clean, title=norm_title)
            if resolved and resolved.get("id") and resolved.get("name"):
                data["common"]["hash_tag"].append({
                    "id": resolved["id"],
                    "link": str(resolved.get("link") or ""),
                    "name": resolved["name"],
                    "type": "topic",
                })
                data["common"]["desc"] = f"{data['common']['desc']} #{resolved['name']}[话题]# ".strip()
            elif f"#{clean}" not in data["common"]["desc"]:
                data["common"]["desc"] = f"{data['common']['desc']} #{clean}".strip()

        post_api = "/web_api/sns/v2/note"
        sign = self._signer.sign_headers(
            "POST",
            post_api,
            self.cookie_str,
            payload=data,
            sign_format="xys",
            x_rap=True,
            b1=self.sign_context.b1,
            dsl_pair=self.sign_context.dsl_pair,
            sign_count=self.sign_context.next_sign_count(),
        )
        headers = {
            **self._base_headers(),
            "content-type": "application/json",
            **sign,
        }
        body = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
        self._check_cancelled()
        if on_submit is not None:
            on_submit()
        resp = self._api_request(
            "post",
            WEBAPI_URL + post_api,
            headers=headers,
            data=body.encode("utf-8"),
            cookies=self.cookies,
        )
        parsed = parse_response(resp)
        result = parsed.get("data") or {}
        note_id = result.get("id") or result.get("note_id")
        if not isinstance(note_id, str) or not note_id.strip():
            raise RednotePublishError(
                "海外小红书发布响应缺少笔记 ID，结果需到创作者中心核对",
                signal="ambiguous_response",
            )
        return True, str(parsed.get("msg") or ""), parsed


def build_published_note_url(response_json: dict) -> str:
    data = (response_json or {}).get("data") or {}
    note_id = str(data.get("id") or data.get("note_id") or "").strip()
    return build_rednote_note_url(note_id) if note_id else ""


__all__ = [
    "DEFAULT_REDNOTE_UPLOAD_URL",
    "RednoteCreatorApi",
    "RednotePublishError",
    "VIDEO_MULTIPART_CHUNK_BYTES",
    "_build_complete_multipart_xml",
    "_extract_upload_permit",
    "_normalize_rednote_upload_origin",
    "_parse_initiate_upload_id",
    "build_published_note_url",
]
