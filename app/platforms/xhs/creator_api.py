"""小红书创作平台「API 直发」客户端(参考 Spider_XHS-master/apis/xhs_creator_apis.py)。
流程:获取上传凭证 -> PUT 文件到 ros-upload(COS 签名)-> (视频)轮询转码 -> POST 发布笔记。
同步实现(curl_cffi.Session + execjs 签名 + cv2 取图像尺寸/视频封面),由引擎用线程调度。
走 curl_cffi 的 impersonate 复刻 Chrome TLS 指纹(发布是写操作,风控更严)。
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from typing import List, Optional, Tuple

from curl_cffi.requests import Session

from . import creator_sign as sign
from ...netfp import impersonate_for_ua

CREATOR_URL = "https://creator.xiaohongshu.com"
UPLOAD_URL = "https://ros-upload.xiaohongshu.com"
EDITH_URL = "https://edith.xiaohongshu.com"
WEB_URL = "https://www.xiaohongshu.com"
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36 Edg/138.0.0.0")
TRANSCODE_MAX_RETRIES = 20
TRANSCODE_RETRY_DELAY = 3


class XhsPublishError(Exception):
    pass


def _common_headers() -> dict:
    return {
        "user-agent": _UA, "accept": "application/json, text/plain, */*",
        "content-type": "application/json;charset=UTF-8",
        "origin": CREATOR_URL, "referer": f"{CREATOR_URL}/",
        "sec-fetch-site": "same-site", "sec-fetch-mode": "cors", "sec-fetch-dest": "empty",
        "accept-language": "zh-CN,zh;q=0.9",
    }


class XhsCreatorApi:
    def __init__(self, cookie_str: str, timeout: float = 30.0, proxy: str = ""):
        self.cookies = sign.trans_cookies(cookie_str)
        if "a1" not in self.cookies:
            raise XhsPublishError("登录态缺少 a1,请重新扫码登录该小红书账号")
        self.a1 = self.cookies["a1"]
        from ...browser.manager import normalize_proxy
        p = normalize_proxy(proxy) or None
        self.cli = Session(
            timeout=timeout,
            impersonate=impersonate_for_ua(_UA),
            proxies={"http": p, "https": p} if p else None,
        )

    def close(self):
        try:
            self.cli.close()
        except Exception:
            pass

    # ── 上传凭证 ──
    def get_file_ids(self, media_type: str) -> Tuple[dict, str]:
        api = "/api/media/v1/upload/creator/permit"
        params = {"biz_name": "spectrum", "scene": media_type, "file_count": "1",
                  "version": "1", "source": "web"}
        spliced = sign.splice_str(api, params)
        h = _common_headers()
        h.update(sign.generate_xsc(self.a1, spliced))
        r = self.cli.get(CREATOR_URL + spliced, headers=h, cookies=self.cookies)
        j = r.json()
        if not j.get("success"):
            raise XhsPublishError(f"获取上传凭证失败: {j.get('msg') or j}")
        return j, h["x-t"]

    def upload_media(self, file_bytes: bytes, media_type: str) -> dict:
        res = {"fileIds": "", "width": "", "height": "", "video_id": "", "file_size": 0}
        j, xt = self.get_file_ids(media_type)
        data = j["data"]["uploadTempPermits"][0]
        upload_host = data.get("uploadAddr") or UPLOAD_URL.replace("https://", "")
        upload_url = upload_host if upload_host.startswith("http") else f"https://{upload_host}"
        file_id = data["fileIds"][0].split("/")[-1]
        token, expire = data["token"], data["expireTime"]
        res["fileIds"] = file_id
        message = f"{str(xt)[:10]};{str(expire)[:10]}"

        if media_type == "image":
            w, h, file_size = _image_info(file_bytes)
            res.update(width=w, height=h, file_size=file_size, mime_type="image/png")
        else:
            file_size = len(file_bytes)
            res["file_size"] = file_size
        host_only = upload_url.replace("https://", "").replace("http://", "")
        signature = sign.cos_signature(message, file_id, file_size, host_only)
        headers = {
            "accept": "*/*", "origin": CREATOR_URL, "referer": f"{CREATOR_URL}/",
            "user-agent": _UA, "content-type": "",
            "authorization": (f"q-sign-algorithm=sha1&q-ak=null&q-sign-time={message}"
                              f"&q-key-time={message}&q-header-list=content-length;host"
                              f"&q-url-param-list=&q-signature={signature}"),
            "x-cos-security-token": token,
        }
        r = self.cli.put(f"{upload_url}/spectrum/{file_id}", headers=headers,
                         data=file_bytes, cookies=self.cookies)
        r.raise_for_status()
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
        r = self.cli.get(EDITH_URL + spliced, headers=h, cookies=self.cookies)
        return r.json()

    def get_topic(self, keyword: str) -> Optional[dict]:
        api = "/web_api/sns/v1/search/topic"
        data = {"keyword": keyword,
                "suggest_topic_request": {"title": "", "desc": f"#{keyword}"},
                "page": {"page_size": 20, "page": 1}}
        h = _common_headers()
        h.update(sign.generate_xsc(self.a1, api, data))
        body = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
        r = self.cli.post(EDITH_URL + api, headers=h, data=body.encode("utf-8"),
                         cookies=self.cookies)
        j = r.json()
        dtos = ((j.get("data") or {}).get("topic_info_dtos")) or []
        return dtos[0] if dtos else None

    # ── 发布 ──
    def post_note(self, *, media_type: str, title: str, desc: str,
                  image_files: Optional[List[bytes]] = None,
                  video_file: Optional[bytes] = None,
                  topics: Optional[List[str]] = None,
                  post_time_ms: Optional[int] = None,
                  privacy_type: int = 0) -> Tuple[bool, str, dict]:
        post_api = "/web_api/sns/v2/note"
        post_loc: dict = {}
        if media_type == "video":
            if not video_file:
                raise XhsPublishError("缺少视频文件")
            cover, metadata = _video_cover_and_meta(video_file)
            file_info = self.upload_media(video_file, "video")
            cover_info = self.upload_media(cover, "image")
            ready = False
            for _ in range(TRANSCODE_MAX_RETRIES):
                res = self.query_transcode(file_info["video_id"])
                d = res.get("data") or {}
                if (d.get("hasFirstFrame") or d.get("has_first_frame")
                        or d.get("firstFrameFileId") or d.get("first_frame_file_id")
                        or d.get("status") in (2, "success", "SUCCESS") or not d):
                    ready = True
                    break
                time.sleep(TRANSCODE_RETRY_DELAY)
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
        r = self.cli.post(EDITH_URL + post_api, headers=h, data=body.encode("utf-8"),
                         cookies=self.cookies)
        j = r.json()
        return bool(j.get("success")), j.get("msg", ""), j

    def my_info(self, *, detailed: bool = False):
        """创作平台「我的信息」；详细模式额外返回未裁剪的响应。"""
        api = "/api/galaxy/user/info"
        h = _common_headers()
        h["sec-fetch-site"] = "same-origin"
        h.update(sign.generate_xsc(self.a1, api))
        r = self.cli.get(CREATOR_URL + api, headers=h, cookies=self.cookies)
        j = r.json()
        result = bool(j.get("success")), (j.get("data") or {})
        return (*result, j) if detailed else result

    def ping(self, *, detailed: bool = False):
        """轻量校验创作者登录态；详细模式额外返回未裁剪的响应。"""
        api = "/api/galaxy/creator/note/user/posted"
        spliced = sign.splice_str(api, {"tab": "0"})
        h = _common_headers()
        h.update(sign.generate_xsc(self.a1, spliced))
        r = self.cli.get(CREATOR_URL + spliced, headers=h, cookies=self.cookies)
        j = r.json()
        result = bool(j.get("success")), (j.get("msg") or "")
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
            r = self.cli.get(CREATOR_URL + spliced, headers=h, cookies=self.cookies)
            j = r.json()
            d = j.get("data") or {}
            page_notes = d.get("notes") or d.get("note_infos") or d.get("noteList") or []
            if not j.get("success"):
                return False, j.get("msg", "获取失败"), notes
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


# ── 媒体信息(cv2)──
def _image_info(file_bytes: bytes):
    import cv2
    import numpy as np
    img = cv2.imdecode(np.frombuffer(file_bytes, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        raise XhsPublishError("图片解码失败")
    h, w = img.shape[0], img.shape[1]
    if w > 2 * h:
        h = int(w / 2)
    return w, h, len(file_bytes)


def _video_cover_and_meta(video: bytes):
    import cv2
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".mp4") as f:
            f.write(video); tmp = f.name
        cap = cv2.VideoCapture(tmp)
        if not cap.isOpened():
            raise XhsPublishError("视频解码失败")
        fps = cap.get(cv2.CAP_PROP_FPS) or 0
        frames = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0
        w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
        h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
        dur = int(frames / fps * 1000) if fps else 0
        ok, frame = cap.read(); cap.release()
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
                   "ats": [], "hash_tag": [], "post_loc": post_loc,
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
                   "ats": [], "hash_tag": [], "post_loc": post_loc,
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
