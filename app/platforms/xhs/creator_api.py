"""小红书创作平台「API 直发」客户端(参考 Spider_XHS-master/apis/xhs_creator_apis.py)。
流程:获取上传凭证 -> PUT 文件到 ros-upload(COS 签名)-> (视频)轮询转码 -> POST 发布笔记。
同步实现(curl_cffi.Session + execjs 签名 + cv2 取图像尺寸/视频封面),由引擎用线程调度。
走 curl_cffi 的 impersonate 复刻 Chrome TLS 指纹(发布是写操作,风控更严)。
"""
from __future__ import annotations

import json
import io
import os
import re
import tempfile
import threading
import time
from typing import List, Optional, Tuple
from urllib.parse import urlsplit

from curl_cffi import CurlOpt
from curl_cffi.curl import CURL_READFUNC_ABORT
from curl_cffi.requests import Session

from . import creator_sign as sign
from .deadline import remaining_seconds
from .media import (COPY_CHUNK_BYTES, MediaFile, media_size, validate_media_count,
                    validate_media_size)
from .responses import XhsApiError, check_http_status, parse_response
from ...netfp import impersonate_for_ua

CREATOR_URL = "https://creator.xiaohongshu.com"
UPLOAD_URL = "https://ros-upload.xiaohongshu.com"
EDITH_URL = "https://edith.xiaohongshu.com"
WEB_URL = "https://www.xiaohongshu.com"
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36 Edg/138.0.0.0")
TRANSCODE_MAX_RETRIES = 20
TRANSCODE_RETRY_DELAY = 3
MAX_IMAGE_PIXELS = 50_000_000


class XhsPublishError(XhsApiError):
    pass


def _upload_origin(address) -> str:
    """Accept only HTTPS XHS origins, never credentials, redirects or URL paths."""
    if address is None or address == "":
        return UPLOAD_URL
    error = "上传地址须为 HTTPS 小红书站点域名，且不含凭据、路径或查询参数"
    if (not isinstance(address, str) or address.startswith("//")
            or any(ord(char) <= 0x20 or ord(char) >= 0x7f or char in "\\?#" for char in address)):
        raise XhsPublishError(error, signal="invalid_upload_destination")
    try:
        parts = urlsplit(address if "://" in address else "https://" + address)
        host = parts.hostname or ""
        if (parts.scheme != "https" or parts.username is not None or parts.password is not None
                or parts.port not in (None, 443) or parts.path not in ("", "/")
                or not host.endswith(".xiaohongshu.com") or len(host) > 253
                or not all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                           for label in host.split("."))):
            raise ValueError("unexpected upload origin")
    except ValueError as exc:
        raise XhsPublishError(error, signal="invalid_upload_destination") from exc
    return "https://" + host


def _upload_fields(payload, xt) -> tuple[str, str, str, str]:
    """Validate the single-file permit before signing or sending any media."""
    try:
        permits = payload["data"]["uploadTempPermits"]
        if not isinstance(permits, list) or len(permits) != 1 or not isinstance(permits[0], dict):
            raise ValueError("unexpected permits")
        permit = permits[0]
        file_ids = permit["fileIds"]
        if not isinstance(file_ids, list) or len(file_ids) != 1 or not isinstance(file_ids[0], str):
            raise ValueError("unexpected file IDs")
        segments = file_ids[0].split("/")
        if not all(re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9._-]*", part) for part in segments):
            raise ValueError("unexpected file ID")
        token = permit["token"]
        if not isinstance(token, str) or not token or any(not 0x21 <= ord(c) <= 0x7e for c in token):
            raise ValueError("unexpected upload token")
        times = []
        for value in (xt, permit["expireTime"]):
            if isinstance(value, bool) or not isinstance(value, (str, int)):
                raise ValueError("unexpected permit timestamp")
            text = str(value)
            if not re.fullmatch(r"[0-9]{1,16}", text) or int(text) <= 0:
                raise ValueError("unexpected permit timestamp")
            times.append(text[:10])
    except (KeyError, TypeError, ValueError, IndexError) as exc:
        raise XhsPublishError("上传凭证结构异常，未上传媒体",
                              category="risk", signal="ambiguous_response") from exc
    origin = _upload_origin(permit.get("uploadAddr"))
    return origin, segments[-1], token, ";".join(times)


def _common_headers() -> dict:
    return {
        "user-agent": _UA, "accept": "application/json, text/plain, */*",
        "content-type": "application/json;charset=UTF-8",
        "origin": CREATOR_URL, "referer": f"{CREATOR_URL}/",
        "sec-fetch-site": "same-site", "sec-fetch-mode": "cors", "sec-fetch-dest": "empty",
        "accept-language": "zh-CN,zh;q=0.9",
    }


class XhsCreatorApi:
    def __init__(self, cookie_str: str, timeout: float = 30.0, proxy: str = "", *,
                 cancel_event: threading.Event | None = None, deadline: float | None = None):
        self._cancel_event = cancel_event
        self._deadline = deadline
        self.cookies = sign.trans_cookies(cookie_str)
        if not self.cookies.get("a1"):
            raise XhsPublishError("登录态缺少 a1,请重新扫码登录该小红书账号",
                                  category="auth", signal="auth_expired")
        self.a1 = self.cookies["a1"]
        from ...browser.manager import normalize_proxy
        p = normalize_proxy(proxy) or None
        self._session_cls = Session
        self._session_options = {
            "timeout": timeout, "impersonate": impersonate_for_ua(_UA),
            "proxies": {"http": p, "https": p} if p else None,
            "discard_cookies": True, "allow_redirects": False,
        }
        self._upload_cli = None
        self.cli = self._session_cls(**self._session_options)

    def close(self):
        for client in (self._upload_cli, self.cli):
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass

    def _check_cancelled(self):
        return remaining_seconds(self._deadline, self._cancel_event)

    def _send(self, client, method: str, url: str, **kwargs):
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

    def _request(self, method: str, url: str, **kwargs):
        return self._send(self.cli, method, url, **kwargs)

    def _upload_request(self, url: str, **kwargs):
        self._check_cancelled()
        if self._upload_cli is None:
            # This transport never receives the login Cookie jar. Only the
            # per-file COS credentials belong on an upload request.
            self._upload_cli = self._session_cls(**self._session_options)
        source = kwargs.pop("media_file", None)
        if source is None:
            return self._send(self._upload_cli, "put", url, **kwargs)
        # requests' data= reads BytesIO in full and rejects ordinary files in
        # curl_cffi 0.15. Use the supported libcurl upload callback instead.
        # This Session is private to one sequential publishing worker.
        with source.path.open("rb") as stream:
            read_error = None
            sent = 0

            def read_chunk(max_size):
                nonlocal read_error, sent
                try:
                    self._check_cancelled()
                    chunk = stream.read(min(max_size, COPY_CHUNK_BYTES, source.size - sent))
                    if not chunk and sent < source.size:
                        raise ValueError("媒体快照在上传期间变短")
                    sent += len(chunk)
                    return chunk
                except Exception as exc:
                    # C callbacks must return ABORT, not leak a Python exception
                    # that older curl_cffi versions can mistake for EOF.
                    read_error = exc
                    return CURL_READFUNC_ABORT

            previous = self._upload_cli.curl_options
            self._upload_cli.curl_options = {
                **previous, CurlOpt.UPLOAD: 1, CurlOpt.INFILESIZE_LARGE: source.size,
                CurlOpt.READFUNCTION: read_chunk,
            }
            try:
                try:
                    response = self._send(self._upload_cli, "put", url, **kwargs)
                except Exception:
                    if read_error is not None:
                        raise read_error
                    raise
                if read_error is not None:
                    raise read_error
                if 200 <= response.status_code < 300 and sent != source.size:
                    raise XhsPublishError("媒体上传未传完，本次未提交发布")
                return response
            finally:
                self._upload_cli.curl_options = previous

    def _poll_delay(self):
        remaining = self._check_cancelled()
        delay = (TRANSCODE_RETRY_DELAY if remaining is None
                 else min(TRANSCODE_RETRY_DELAY, remaining))
        if self._cancel_event is None:
            time.sleep(delay)
        else:
            self._cancel_event.wait(delay)
        self._check_cancelled()

    # ── 上传凭证 ──
    def get_file_ids(self, media_type: str) -> Tuple[dict, str]:
        api = "/api/media/v1/upload/creator/permit"
        params = {"biz_name": "spectrum", "scene": media_type, "file_count": "1",
                  "version": "1", "source": "web"}
        spliced = sign.splice_str(api, params)
        h = _common_headers()
        h.update(sign.generate_xsc(self.a1, spliced))
        r = self._request("get", CREATOR_URL + spliced, headers=h, cookies=self.cookies)
        j = parse_response(r)
        return j, h["x-t"]

    def upload_media(self, file_bytes: bytes | bytearray | MediaFile, media_type: str) -> dict:
        try:
            if media_type not in ("image", "video"):
                raise ValueError("上传媒体类型无效")
            file_size = media_size(file_bytes)
            validate_media_size(media_type, file_size, file_size)
        except (OSError, ValueError) as exc:
            raise XhsPublishError(str(exc)) from exc
        res = {"fileIds": "", "width": "", "height": "", "video_id": "", "file_size": 0}
        j, xt = self.get_file_ids(media_type)
        upload_url, file_id, token, message = _upload_fields(j, xt)
        res["fileIds"] = file_id

        if media_type == "image":
            w, h, file_size, mime_type = _image_info(file_bytes)
            res.update(width=w, height=h, file_size=file_size, mime_type=mime_type)
        else:
            res["file_size"] = file_size
        host_only = urlsplit(upload_url).hostname
        signature = sign.cos_signature(message, file_id, file_size, host_only)
        headers = {
            "accept": "*/*", "origin": CREATOR_URL, "referer": f"{CREATOR_URL}/",
            "user-agent": _UA, "content-type": "",
            "authorization": (f"q-sign-algorithm=sha1&q-ak=null&q-sign-time={message}"
                              f"&q-key-time={message}&q-header-list=content-length;host"
                              f"&q-url-param-list=&q-signature={signature}"),
            "x-cos-security-token": token,
        }
        source = ({"media_file": file_bytes} if isinstance(file_bytes, MediaFile)
                  else {"data": bytes(file_bytes)})
        r = self._upload_request(f"{upload_url}/spectrum/{file_id}", headers=headers, **source)
        check_http_status(r.status_code)
        if media_type == "video":
            vid = r.headers.get("X-Ros-Video-Id")
            if not vid:
                raise XhsPublishError("视频上传响应缺少 X-Ros-Video-Id")
            res["video_id"] = vid
        return res

    def query_transcode(self, video_id: str) -> dict:
        api = "/web_api/sns/capa/postgw/query_transcode"
        params = {"video_id": str(video_id), "need_transcode": "false", "resource_type": "0"}
        spliced = sign.splice_str(api, params)
        h = _common_headers()
        h.update(sign.generate_xsc(self.a1, spliced))
        r = self._request("get", EDITH_URL + spliced, headers=h, cookies=self.cookies)
        return parse_response(r)

    def get_topic(self, keyword: str) -> Optional[dict]:
        api = "/web_api/sns/v1/search/topic"
        data = {"keyword": keyword,
                "suggest_topic_request": {"title": "", "desc": f"#{keyword}"},
                "page": {"page_size": 20, "page": 1}}
        h = _common_headers()
        h.update(sign.generate_xsc(self.a1, api, data))
        body = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
        r = self._request("post", EDITH_URL + api, headers=h, data=body.encode("utf-8"),
                          cookies=self.cookies)
        j = parse_response(r)
        dtos = ((j.get("data") or {}).get("topic_info_dtos")) or []
        if not isinstance(dtos, list) or any(not isinstance(row, dict) for row in dtos):
            raise XhsPublishError("话题列表响应结构异常", category="risk",
                                  signal="ambiguous_response")
        return dtos[0] if dtos else None

    # ── 发布 ──
    def post_note(self, *, media_type: str, title: str, desc: str,
                  image_files: Optional[List[bytes | MediaFile]] = None,
                  video_file: Optional[bytes | MediaFile] = None,
                  topics: Optional[List[str]] = None,
                  post_time_ms: Optional[int] = None,
                  privacy_type: int = 0, on_submit=None) -> Tuple[bool, str, dict]:
        """Prepare media, then mark the no-retry boundary before one final POST.

        ``on_submit`` is synchronous here; the async entry bridges callbacks to
        its event loop. Any callback failure stops before publishing the note.
        """
        self._check_cancelled()
        try:
            if media_type == "video":
                validate_media_count(media_type, 1 if video_file else 0)
                blobs = [video_file]
            else:
                if not isinstance(image_files, (list, tuple)):
                    raise ValueError("图片须为媒体内容列表")
                validate_media_count(media_type, len(image_files))
                blobs = image_files
            total = 0
            for blob in blobs:
                size = media_size(blob)
                total += size
                validate_media_size(media_type, size, total)
        except (OSError, ValueError) as exc:
            raise XhsPublishError(str(exc)) from exc
        post_api = "/web_api/sns/v2/note"
        post_loc: dict = {}
        if media_type == "video":
            if not video_file:
                raise XhsPublishError("缺少视频文件")
            cover, metadata = _video_cover_and_meta(video_file)
            file_info = self.upload_media(video_file, "video")
            cover_info = self.upload_media(cover, "image")
            ready = False
            for attempt in range(TRANSCODE_MAX_RETRIES):
                res = self.query_transcode(file_info["video_id"])
                d = res.get("data") or {}
                if not d:
                    raise XhsPublishError("视频转码响应缺少状态，未提交发布",
                                          category="risk", signal="ambiguous_response")
                status = d.get("status")
                if isinstance(status, str) and status.lower() in {"failed", "failure", "error"}:
                    raise XhsPublishError("视频转码失败，未提交发布")
                frame_id = d.get("firstFrameFileId") or d.get("first_frame_file_id")
                if (d.get("hasFirstFrame") is True or d.get("has_first_frame") is True
                        or (isinstance(frame_id, str) and frame_id.strip())
                        or status in (2, "success", "SUCCESS")):
                    ready = True
                    break
                if attempt + 1 < TRANSCODE_MAX_RETRIES:
                    self._poll_delay()
            if not ready:
                raise XhsPublishError("视频转码超时")
            data = _video_note_data(title, desc, post_time_ms, post_loc,
                                    privacy_type, file_info, cover_info, metadata)
        else:
            if not image_files:
                raise XhsPublishError("缺少图片文件")
            infos = [self.upload_media(b, "image") for b in image_files]
            data = _image_note_data(title, desc, post_time_ms, post_loc,
                                    privacy_type, infos)

        for topic in (topics or []):
            t = self.get_topic(topic)
            if t:
                data["common"]["hash_tag"].append(
                    {"id": t["id"], "link": t["link"], "name": t["name"], "type": "topic"})
                data["common"]["desc"] += f" #{t['name']}[话题]# "

        body = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
        xs, xt, xs_common = sign.generate_xs_xs_common(self.a1, post_api, body)
        h = {
            "user-agent": _UA, "accept": "application/json, text/plain, */*",
            "content-type": "application/json", "origin": CREATOR_URL,
            "referer": f"{CREATOR_URL}/", "sec-fetch-site": "same-site",
            "sec-fetch-mode": "cors", "sec-fetch-dest": "empty",
            "x-s": xs, "x-t": str(xt), "x-s-common": xs_common,
            "x-b3-traceid": sign.gen_b3_traceid(), "x-xray-traceid": sign.gen_xray_traceid(),
            "x-rap-param": sign.generate_x_rap_param(post_api, body),
        }
        self._check_cancelled()
        if on_submit is not None:
            on_submit()
        r = self._request("post", EDITH_URL + post_api, headers=h, data=body.encode("utf-8"),
                          cookies=self.cookies)
        j = parse_response(r)
        result = j.get("data") or {}
        note_id = result.get("id") or result.get("note_id")
        if not isinstance(note_id, str) or not note_id.strip():
            raise XhsPublishError("发布响应缺少笔记 ID，结果需到平台核对",
                                  signal="ambiguous_response")
        return True, str(j.get("msg") or ""), j

    def my_info(self, *, detailed: bool = False):
        """创作平台「我的信息」；详细模式额外返回未裁剪的响应。"""
        api = "/api/galaxy/user/info"
        h = _common_headers()
        h["sec-fetch-site"] = "same-origin"
        h.update(sign.generate_xsc(self.a1, api))
        r = self._request("get", CREATOR_URL + api, headers=h, cookies=self.cookies)
        j = parse_response(r)
        result = True, (j.get("data") or {})
        return (*result, j) if detailed else result

    def ping(self, *, detailed: bool = False):
        """轻量校验创作者登录态；详细模式额外返回未裁剪的响应。"""
        api = "/api/galaxy/creator/note/user/posted"
        spliced = sign.splice_str(api, {"tab": "0"})
        h = _common_headers()
        h.update(sign.generate_xsc(self.a1, spliced))
        r = self._request("get", CREATOR_URL + spliced, headers=h, cookies=self.cookies)
        j = parse_response(r)
        result = True, (j.get("msg") or "")
        return (*result, j) if detailed else result

    # ── 已发布列表 ──
    def published_notes(self) -> Tuple[bool, str, list]:
        notes, page = [], None
        api = "/api/galaxy/creator/note/user/posted"
        for i in range(30):
            params = {"tab": "0"}
            if page:
                params["page"] = str(page)
            spliced = sign.splice_str(api, params)
            h = _common_headers()
            h.update(sign.generate_xsc(self.a1, spliced))
            r = self._request("get", CREATOR_URL + spliced, headers=h, cookies=self.cookies)
            j = parse_response(r)
            d = j.get("data") or {}
            page_notes = d.get("notes") or d.get("note_infos") or d.get("noteList") or []
            notes += page_notes
            page = d.get("page")
            if page in (-1, None) or not page_notes:
                break
        return True, "ok", notes


def _cnum(v) -> int:
    if isinstance(v, (int, float)):
        return int(v)
    s = str(v or "").strip().replace("+", "")
    try:
        if "万" in s:
            return int(float(s.replace("万", "")) * 10000)
        return int(float(s))
    except (ValueError, TypeError):
        return 0


def parse_creator_user(d: dict) -> dict:
    """把创作平台 user/info 归一成账号资料(字段名按多种可能兜底)。"""
    def f(*keys):
        for k in keys:
            v = d.get(k)
            if v not in (None, "", 0):
                return v
        return ""
    return {
        "nickname": f("userName", "nickname", "name", "nick_name") or "",
        "sec_uid": str(f("userId", "user_id", "id") or ""),
        "douyin_id": str(f("redId", "red_id", "redNumber") or ""),
        "avatar": f("userImage", "image", "images", "avatar", "userAvatar", "headPhoto") or "",
        "follower_count": _cnum(f("fansCount", "fans", "fansNum", "followerCount")),
        "aweme_count": _cnum(f("noteCount", "notesCount", "notes", "publishNoteCount")),
    }


# ── 媒体信息(图片仅读元信息，视频使用 cv2)──
def _image_info(file_bytes: bytes | bytearray | MediaFile):
    from PIL import Image
    try:
        size = media_size(file_bytes)
        validate_media_size("image", size, size)
        source = file_bytes.path if isinstance(file_bytes, MediaFile) else io.BytesIO(file_bytes)
        # Identify actual bytes, not the extension; never fabricate dimensions
        # or decode the whole pixel array just to read width/height.
        with Image.open(source, formats=("JPEG", "PNG", "WEBP", "GIF", "BMP", "TIFF")) as img:
            w, h = img.size
            if w <= 0 or h <= 0 or w * h > MAX_IMAGE_PIXELS:
                raise ValueError("图片像素数超过本项目资源上限")
            mime_type = Image.MIME[img.format]
            img.verify()
        return w, h, size, mime_type
    except (OSError, ValueError, SyntaxError, Image.DecompressionBombError) as exc:
        raise XhsPublishError(f"图片校验失败：{exc}") from exc


def _video_cover_and_meta(video: bytes | MediaFile):
    import cv2
    tmp = None
    cap = None
    try:
        if isinstance(video, MediaFile):
            source = str(video.path)
        else:
            with tempfile.NamedTemporaryFile(delete=False, suffix=".mp4") as f:
                f.write(video); tmp = f.name
            source = tmp
        cap = cv2.VideoCapture(source)
        if not cap.isOpened():
            raise XhsPublishError("视频解码失败")
        fps = cap.get(cv2.CAP_PROP_FPS) or 0
        frames = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0
        w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
        h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
        dur = int(frames / fps * 1000) if fps else 0
        ok, frame = cap.read()
        if not ok:
            raise XhsPublishError("视频封面帧解码失败")
        ok, enc = cv2.imencode(".jpg", frame)
        if not ok:
            raise XhsPublishError("封面编码失败")
        meta = {
            "video": {"bitrate": None, "colour_primaries": "BT.709", "duration": dur,
                      "format": "AVC", "frame_rate": round(fps, 3) if fps else 0,
                      "height": h, "matrix_coefficients": "BT.709", "rotation": 0,
                      "transfer_characteristics": "BT.709", "width": w},
            "audio": {"bitrate": None, "channels": 2, "duration": dur,
                      "format": "AAC", "sampling_rate": 48000},
        }
        return enc.tobytes(), meta
    finally:
        if cap is not None:
            cap.release()
        if tmp and os.path.exists(tmp):
            os.remove(tmp)


# ── 发布数据体(逐字段对齐 Spider_XHS)──
def _biz_binds(post_time_ms):
    if post_time_ms is None:
        return ('{"version":1,"noteId":0,"bizType":0,"noteOrderBind":{},"notePostTiming":{},'
                '"noteCollectionBind":{"id":""},"noteSketchCollectionBind":{"id":""},'
                '"coProduceBind":{"enable":true},"noteCopyBind":{"copyable":true},'
                '"interactionPermissionBind":{"commentPermission":0},"optionRelationList":[]}')
    return ('{"version":1,"noteId":0,"bizType":13,"noteOrderBind":{},"notePostTiming":'
            f'{{"postTime":"{post_time_ms}"}},"noteCollectionBind":{{"id":""}}}}')


_SOURCE = '{"type":"web","ids":"","extraInfo":"{\\"subType\\":\\"official\\",\\"systemId\\":\\"web\\"}"}'
_CTX = ('{"recommend_title":{"recommend_title_id":"","is_use":3,"used_index":-1},'
        '"recommendTitle":[],"recommend_topics":{"used":[]}}')


def _image_note_data(title, desc, post_time_ms, post_loc, privacy_type, file_infos):
    images = []
    for fi in file_infos:
        images.append({
            "file_id": f"spectrum/{fi['fileIds']}", "width": fi["width"], "height": fi["height"],
            "metadata": {"source": -1}, "stickers": {"version": 2, "floating": []},
            "extra_info_json": json.dumps(
                {"mimeType": fi.get("mime_type", "image/png"),
                 "image_metadata": {"bg_color": "", "origin_size": fi.get("file_size", 0) / 1024}},
                separators=(",", ":"), ensure_ascii=False),
        })
    return {
        "common": {"type": "normal", "title": title, "note_id": "", "desc": desc,
                   "source": _SOURCE, "business_binds": _biz_binds(post_time_ms),
                   "ats": [], "hash_tag": [],
                   **({"post_loc": post_loc} if post_loc else {}),
                   "privacy_info": {"op_type": 1, "type": privacy_type, "user_ids": []},
                   "goods_info": {}, "biz_relations": [],
                   "capa_trace_info": {"contextJson": _CTX}},
        "image_info": {"images": images}, "video_info": None,
    }


def _video_note_data(title, desc, post_time_ms, post_loc, privacy_type,
                     fi, cover, metadata):
    vm = metadata["video"]
    am = metadata["audio"]
    dur_s = round((vm.get("duration") or 0) / 1000, 3)
    vid_fid = f"spectrum/{fi['fileIds']}"
    cov_fid = f"spectrum/{cover['fileIds']}"
    return {
        "common": {"type": "video", "title": title, "note_id": "", "desc": desc,
                   "source": _SOURCE, "business_binds": _biz_binds(post_time_ms),
                   "ats": [], "hash_tag": [],
                   **({"post_loc": post_loc} if post_loc else {}),
                   "privacy_info": {"op_type": 1, "type": privacy_type, "user_ids": []},
                   "goods_info": {}, "biz_relations": [],
                   "capa_trace_info": {"contextJson": _CTX}},
        "image_info": None,
        "video_info": {
            "fileid": vid_fid, "file_id": vid_fid,
            "format_width": vm.get("width") or 0, "format_height": vm.get("height") or 0,
            "video_preview_type": "",
            "composite_metadata": {"video": vm, "audio": am}, "timelines": [],
            "cover": {"fileid": cov_fid, "file_id": cov_fid,
                      "height": cover.get("height") or vm.get("height") or 0,
                      "width": cover.get("width") or vm.get("width") or 0,
                      "frame": {"ts": 0, "is_user_select": False, "is_upload": False},
                      "stickers": {"version": 2, "neptune": []}, "fonts": [],
                      "extra_info_json": "{}"},
            "chapters": [], "chapter_sync_text": False,
            "segments": {"count": 1, "need_slice": False,
                         "items": [{"mute": 0, "speed": 1, "start": 0, "duration": dur_s,
                                    "transcoded": 0, "media_source": 1,
                                    "original_metadata": {"video": vm, "audio": am}}]},
            "entrance": "web"},
    }
