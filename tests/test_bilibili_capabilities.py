"""Offline unit and integration tests for Bilibili ('bilibili') platform capabilities."""
from __future__ import annotations

import asyncio
import json
import time
import zlib
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

import app.db as db
import app.engine.downloader as downloader_mod
import app.main as main
from app.config import Config
from app.engine.downloader import Downloader
from app.engine.monitor import MonitorEngine
from app.models import CommentRecord, DouyinAccount
from app.platforms.bilibili import (
    BILI_WS_DEFAULT_SUBSCRIBE_PATH,
    BILI_WS_METHODS,
    CREATION_STATEMENT_OPTIONS,
    FALLBACK_PARTITIONS,
    VC_COMMON_PARAMS,
    BilibiliClient,
    av_to_bv,
    bili_commit_request,
    bili_part_receipt,
    bili_part_request,
    bili_post_create_request,
    bili_req_upload_id_request,
    bili_upload_url,
    build_bili_form_from_task,
    bv_to_av,
    common_to_bili,
    cookie_map_from_state,
    create_uuid_v1_factory,
    decode_rpc_frame,
    encode_rpc_frame,
    encode_type,
    extract_refresh_csrf,
    flatten_bili_comments,
    format_bili_form,
    get_correspond_path,
    has_bili_login_cookies,
    looks_like_bili_work,
    mixin_key_from_wbi,
    parse_bili_aweme,
    parse_bili_comment,
    parse_bili_creator_work,
    parse_bili_danmaku_xml,
    parse_bili_dm_messages,
    parse_bili_dm_sessions,
    play_source_percent,
    resolve_bili_mid,
    resolve_bili_work_ref,
    sign_from_wbi,
    sign_with_key,
    validate_bili_form,
)
from app.profiles import DOMESTIC_PLATFORMS
from app.transport_matrix import resolve_transport
from test_project_optimizations import local_project


WBI_FIXTURE = {
    "img_url": "https://i0.hdslb.com/bfs/wbi/7cd084941338484aae1ad9425b84077c.png",
    "sub_url": "https://i0.hdslb.com/bfs/wbi/4932caff0ff746eab6f01bf08b70ac45.png",
}
EXPECTED_MIXIN_KEY = "ea1db124af3c7062474693fa704f4ff8"


# ═══════════ 1. WBI Signing & RSA-OAEP Cookie Refresh ═══════════


def test_wbi_signing_deterministic_and_strips_unsafe_chars():
    key = mixin_key_from_wbi(WBI_FIXTURE)
    assert len(key) == 32
    assert key == EXPECTED_MIXIN_KEY

    wbi_spec_2 = {
        "img_url": "https://i0.hdslb.com/bfs/wbi/653657f524a547ac981ded72ea172057.png",
        "sub_url": "https://i0.hdslb.com/bfs/wbi/6e4909c702f846728e64f6007736a338.png",
    }
    assert mixin_key_from_wbi(wbi_spec_2) == "72136226c6a73669787ee4fd02a74c27"

    signed = sign_from_wbi(
        WBI_FIXTURE,
        {"foo": "114", "bar": "514", "baz": "1919810"},
        wts=1702204169,
    )
    assert signed["wts"] == 1702204169
    assert signed["w_rid"] == "6149fdadf571698ca7e6a567265cd0ee"
    assert "bar=514&baz=1919810&foo=114&wts=1702204169" == signed["query"]

    filtered = sign_with_key(key, {"msg": "hi!'()*there"}, wts=1710000000)
    for bad in ("!", "'", "(", ")", "*"):
        assert bad not in filtered["query"]
    assert "hithere" in filtered["query"]
    assert VC_COMMON_PARAMS["mobi_app"] == "web"


def test_correspond_path_rsa_oaep_and_refresh_csrf_extraction():
    cipher_hex = get_correspond_path(1710000000123)
    assert len(cipher_hex) == 256
    assert all(c in "0123456789abcdef" for c in cipher_hex)

    sample_html = '<html><body><div id="1-name">9f8e7d6c5b4a3210</div></body></html>'
    assert extract_refresh_csrf(sample_html) == "9f8e7d6c5b4a3210"
    assert extract_refresh_csrf("<html><body>empty</body></html>") == ""


# ═══════════ 2. AV <-> BV Bijection & URL Resolution ═══════════


@pytest.mark.parametrize(
    ("aid", "bvid"),
    [
        (170001, "BV17x411w7KC"),
        (100001, "BV1dx411c7Dj"),
        (112919465726904, "BV1iGaSeAE9o"),
    ],
)
def test_av_bv_bijection_roundtrips(aid, bvid):
    assert av_to_bv(aid) == bvid
    assert bv_to_av(bvid) == aid


def test_bili_work_and_mid_resolution_offline():
    ref_bv = asyncio.run(resolve_bili_work_ref("【测试分享】https://www.bilibili.com/video/BV17x411w7KC?p=1", "UA"))
    assert ref_bv is not None
    assert ref_bv.work_id == "BV17x411w7KC"
    assert ref_bv.bvid == "BV17x411w7KC"
    assert ref_bv.aid == 170001

    ref_av = asyncio.run(resolve_bili_work_ref("av170001", "UA"))
    assert ref_av is not None and ref_av.bvid == "BV17x411w7KC" and ref_av.aid == 170001

    ref_opus = asyncio.run(resolve_bili_work_ref("https://www.bilibili.com/opus/987654321012345678", "UA"))
    assert ref_opus is not None and ref_opus.opus_id == "987654321012345678" and ref_opus.work_id == "987654321012345678"

    assert looks_like_bili_work("BV17x411w7KC")
    assert looks_like_bili_work("https://t.bilibili.com/987654321012345678")
    assert not looks_like_bili_work("https://space.bilibili.com/546195")

    mid = asyncio.run(resolve_bili_mid("https://space.bilibili.com/546195/video", "UA"))
    assert mid == "546195"
    assert asyncio.run(resolve_bili_mid("546195", "UA")) == "546195"


# ═══════════ 3. Formatter, Validation, Play Sources & UPOS ═══════════


def test_bili_form_formatting_and_validation():
    schedule_dt = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(hours=4)
    raw_self = build_bili_form_from_task(
        title="B站离线投稿协议测试",
        desc="正文简介说明",
        tags=["数码", "评测"],
        schedule_at=schedule_dt,
        bili_options={
            "tid": 95,
            "copyright": 1,
            "recreate": False,
            "up_close_danmu": True,
            "up_close_reply": True,
            "up_selection_reply": True,
            "creation_statement_id": 3,
            "topic": {"topic_id": 1088, "topic_name": "科技猎手", "mission_id": 5001, "source": "web"},
        },
    )
    validate_bili_form(raw_self)
    formatted = format_bili_form(raw_self)
    fd = formatted["formData"]
    assert fd["human_type2"] == 95
    assert fd["copyright"] == 1
    assert fd["recreate"] == -1
    assert fd["no_reprint"] == 1
    assert fd["tag"].split(",")[0] == "科技猎手"
    assert fd["topic_id"] == 1088 and fd["mission_id"] == 5001
    # up_close_reply overrides up_selection_reply
    assert fd["up_close_danmu"] is True
    assert fd["up_close_reply"] is True
    assert fd["up_selection_reply"] is False
    assert fd["dtime"] == int(schedule_dt.timestamp())
    assert len(CREATION_STATEMENT_OPTIONS) >= 6
    assert len(FALLBACK_PARTITIONS) >= 5

    # Repost requires source
    bad_repost = build_bili_form_from_task(
        title="转载无来源",
        tags=["测试"],
        bili_options={"copyright": 2, "source": ""},
    )
    with pytest.raises(ValueError, match="转载来源"):
        validate_bili_form(bad_repost)

    # common_to_bili truncates to <=80 while validate_bili_form rejects raw >80
    mapped = common_to_bili({"title": "x" * 85, "tags": ["测试"]})
    assert len(mapped["title"]) == 80
    with pytest.raises(ValueError, match="80"):
        validate_bili_form({"title": "x" * 85, "bili_tags": ["测试"]})

    # _merge_add_v3_payload handles human_type2 >= 1000 without KeyError('tid'), preserves copyright=3 + creation_statement, and maps recreate=-1
    from app.platforms.bilibili.publish import _merge_add_v3_payload, _resolve_creation_statement_id

    assert _resolve_creation_statement_id({"declaration_index": 0}, 1) == -1
    assert _resolve_creation_statement_id({"creation_statement_id": 4}, 1) == 4
    assert _resolve_creation_statement_id({"declaration_index": 0}, 2) == -2

    form_neg_rec = format_bili_form(
        build_bili_form_from_task(
            title="B站音乐分区投稿测试",
            tags=["音乐", "测试"],
            bili_options={"tid": 1001, "copyright": 1, "recreate": -1},
        )
    )["formData"]
    assert form_neg_rec["recreate"] == -1

    form_1001 = format_bili_form(
        build_bili_form_from_task(
            title="B站音乐分区投稿测试",
            tags=["音乐", "编程"],
            bili_options={"tid": 1001, "copyright": 1, "recreate": 1},
        )
    )["formData"]
    form_1001["is_only_self"] = 1
    merged_json = _merge_add_v3_payload(
        json.dumps(
            {
                "tid": 28,
                "copyright": 3,
                "creation_statement": {"id": -1},
                "cover": "",
                "videos": [{"filename": "upos_1", "title": "old_hash"}],
            }
        ),
        form_1001,
        explicit_tid=True,
        cover_url="https://archive.biliimg.com/bfs/archive/auto_cover.jpg",
    )
    merged_obj = json.loads(merged_json)
    assert merged_obj["human_type2"] == 1001
    assert merged_obj["tid"] == 28
    assert merged_obj["copyright"] == 3
    assert merged_obj["creation_statement"] == {"id": -1}
    assert merged_obj["recreate"] == 1
    assert merged_obj["videos"][0]["title"] == "B站音乐分区投稿测试"
    assert merged_obj["cover"] == "https://archive.biliimg.com/bfs/archive/auto_cover.jpg"
    assert merged_obj["is_only_self"] == 1


def test_play_source_percent_and_upos_descriptors():
    pct = play_source_percent({
        "pc": 25,
        "android": 30,
        "ios": 20,
        "h5": 20,
        "tv": 5,
    })
    by_name = {item["name"]: item["value"] for item in pct}
    assert by_name == {
        "PC": 25.0,
        "移动端": 50.0,
        "H5及小程序": 20.0,
        "云视听小电视": 5.0,
        "其它": 0.0,
    }
    assert play_source_percent({}) == []

    meta = {
        "endpoint": "//upos-sz-upcdnbda2.bilivideo.com",
        "upos_uri": "upos://ugcboss/n261118xyz.mp4",
        "auth": "ak=1&sign=2",
        "chunk_size": 1048576,
        "upload_id": "u1",
    }
    upload_url = bili_upload_url(meta)
    assert upload_url == "https://upos-sz-upcdnbda2.bilivideo.com/ugcboss/n261118xyz.mp4"
    node = {**meta, "uploadUrl": upload_url}
    req_id = bili_req_upload_id_request(node, {"filesize": 1048576})
    assert req_id["options"]["method"] == "post" and "uploads" in req_id["options"]["params"]
    part = bili_part_request(node=node, part_number=1, data=b"abc", total=3)
    assert part["options"]["method"] == "PUT" and part["options"]["params"]["partNumber"] == 1
    assert bili_part_receipt(1) == {"partNumber": 1, "eTag": "etag"}
    commit = bili_commit_request(node, {"name": "demo.mp4"}, {"parts": [bili_part_receipt(1)]})
    assert commit["options"]["method"] == "POST"
    assert commit["options"]["data"] == {"parts": [{"partNumber": 1, "eTag": "etag"}]}
    create_req = bili_post_create_request({"title": "demo"}, csrf="csrf_tok", now_ms=1710000000123)
    assert create_req["options"]["params"]["csrf"] == "csrf_tok"


# ═══════════ 4. Broadcast Protobuf Codec & UUIDv1 ═══════════


def test_broadcast_protobuf_wire_roundtrip_and_unknown_any_preservation():
    assert BILI_WS_DEFAULT_SUBSCRIBE_PATH == "/bilibili.broadcast.message.im.Notify/WatchNotify"
    assert BILI_WS_METHODS["HEARTBEAT"].endswith("/Heartbeat")

    encoded = encode_rpc_frame(
        BILI_WS_METHODS["AUTH"],
        "bilibili.broadcast.v1.AuthReq",
        {"token": "uuid-token-1"},
        request_id=7,
    )
    decoded = decode_rpc_frame(encoded["buffer"])
    assert decoded["requestId"] == 7
    assert decoded["method"] == BILI_WS_METHODS["AUTH"]
    assert decoded["payload"]["decoded"]["token"] == "uuid-token-1"

    # Unknown inner Any payload inside WatchNotify must be preserved raw without failing
    raw_im_payload = b"\x08\x96\x01\x12\x05hello"
    raw_frame = encode_type(
        "bilibili.broadcast.v1.RpcFrame",
        {
            "meta": {"id": 42},
            "method": BILI_WS_DEFAULT_SUBSCRIBE_PATH,
            "payload": {
                "type_url": "type.googleapis.com/bilibili.broadcast.message.im.Notify",
                "value": raw_im_payload,
            },
        },
    )
    pushed = decode_rpc_frame(raw_frame)
    assert pushed["requestId"] == 42
    assert pushed["payload"]["value"] == raw_im_payload
    assert pushed["payload"]["decoded"] is None

    fake_clock = iter([1710000000000, 1710000000000, 1709999999000])
    gen = create_uuid_v1_factory(
        now_ms_fn=lambda: next(fake_clock),
        random_bytes_fn=lambda n: b"\x12\x34" if n == 2 else b"\x01\x23\x45\x67\x89\xab",
    )
    u1, u2, u3 = gen(), gen(), gen()
    assert len(u1) == 36 and u1[14] == "1"
    assert u1 != u2 and u2 != u3


# ═══════════ 5. Data Extractors (Aweme, Comments, Danmaku XML, DM) ═══════════


def test_extractors_for_video_opus_comments_danmaku_and_dm():
    video_aw = parse_bili_aweme(
        {
            "bvid": "BV17x411w7KC",
            "aid": 170001,
            "cid": 279786,
            "title": '<em class="keyword">测试</em>视频标题',
            "desc": "视频简介",
            "pic": "//i0.hdslb.com/bfs/archive/sample.jpg",
            "pubdate": 1710000100,
            "duration": "02:15",
            "owner": {"mid": 546195, "name": "示例UP"},
            "stat": {"like": 1200, "reply": 88, "view": 56000},
        },
        play_url="https://upos-sz-mirrorcos.bilivideo.com/upgcxcode/sample.mp4",
    )
    assert video_aw is not None
    assert video_aw.aweme_id == "BV17x411w7KC"
    assert video_aw.desc.startswith("测试视频标题")
    assert video_aw.cover.startswith("https://")
    assert video_aw.duration == 135
    assert video_aw.medias[0].kind == "video"

    opus_aw = parse_bili_aweme({
        "id_str": "987654321012345678",
        "type": "DYNAMIC_TYPE_DRAW",
        "basic": {"comment_id_str": "31009988", "comment_type": 11},
        "modules": {
            "module_author": {"mid": 546195, "name": "示例UP", "pub_ts": 1710000200},
            "module_dynamic": {
                "major": {
                    "opus": {
                        "title": "图文动态标题",
                        "summary": {"text": "图文正文"},
                        "pics": [{"url": "//i0.hdslb.com/bfs/new_dyn/pic1.jpg"}, {"url": "https://i0.hdslb.com/bfs/new_dyn/pic2.png"}],
                    }
                }
            },
            "module_stat": {"like": {"count": 45}, "comment": {"count": 6}},
        },
    })
    assert opus_aw is not None
    assert opus_aw.media_type == "images" and len(opus_aw.medias) == 2

    cw = parse_bili_creator_work({
        "Archive": {"bvid": "BV17x411w7KC", "aid": 170001, "title": "我的投稿", "cover": "//i0.hdslb.com/c.jpg", "ptime": 1710000300, "state": 0, "state_desc": "已通过"},
        "Stat": {"view": 999, "like": 77, "reply": 12, "danmaku": 34, "coin": 20, "favorite": 15, "share": 5},
    })
    assert cw is not None and cw["item_id"] == "BV17x411w7KC" and cw["play_count"] == 999

    raw_replies = [{
        "rpid_str": "1001",
        "oid": 170001,
        "ctime": 1710000400,
        "like": 9,
        "member": {"mid": "8801", "uname": "热心观众", "avatar": "//i0.hdslb.com/a.jpg"},
        "content": {"message": "主楼评论"},
        "reply_control": {"location": "IP属地：上海"},
        "replies": [{
            "rpid_str": "1002",
            "oid": 170001,
            "root": 1001,
            "parent": 1001,
            "ctime": 1710000450,
            "like": 2,
            "member": {"mid": "8802", "uname": "二楼观众"},
            "content": {"message": "楼中楼回复"},
        }],
    }]
    flat = [parse_bili_comment(item) for item in flatten_bili_comments(raw_replies)]
    assert len(flat) == 2
    assert flat[0]["comment_id"] == "1001" and flat[0]["ip_label"] == "上海" and flat[0]["reply_to"] == ""
    assert flat[1]["comment_id"] == "1002" and flat[1]["reply_to"] == "1001"
    rec = CommentRecord(platform="bilibili", watch_id=1, aweme_id="BV17x411w7KC", **flat[0])
    assert rec.comment_id == "1001"

    xml_bytes = (
        '<?xml version="1.0" encoding="UTF-8"?><i>'
        '<d p="12.345,1,25,16777215,1710000500,0,9a8b7c6d,55443322">前方高能预警</d>'
        "</i>"
    ).encode("utf-8")
    compressor = zlib.compressobj(level=6, wbits=-zlib.MAX_WBITS)
    deflated = compressor.compress(xml_bytes) + compressor.flush()
    dm_rows = parse_bili_danmaku_xml(deflated)
    assert len(dm_rows) == 1
    assert dm_rows[0]["danmaku_id"] == "55443322"
    assert dm_rows[0]["video_time_ms"] == 12345
    assert dm_rows[0]["text"] == "前方高能预警"

    sessions = parse_bili_dm_sessions(
        {"data": {"session_list": [{"talker_id": 667788, "session_type": 1, "unread_count": 3, "last_msg": {"msg_type": 1, "content": '{"content":"你好UP主"}', "timestamp": 1710000600}}]}},
        {"667788": {"name": "私信小伙伴", "face": "//i0.hdslb.com/f.jpg"}},
    )
    assert len(sessions) == 1 and sessions[0]["peer_nickname"] == "私信小伙伴" and sessions[0]["last_text"] == "你好UP主"

    msgs = parse_bili_dm_messages(
        {"data": {"messages": [
            {"msg_key": 9002, "sender_uid": 546195, "receiver_id": 667788, "msg_type": 1, "msg_status": 0, "timestamp": 1710000620, "content": '{"content":"收到啦"}'},
            {"msg_key": 9001, "sender_uid": 667788, "receiver_id": 546195, "msg_type": 1, "msg_status": 0, "timestamp": 1710000600, "content": '{"content":"你好UP主"}'},
        ]}},
        peer_uid="667788",
        self_uid="546195",
    )
    assert len(msgs["messages"]) == 2
    assert msgs["messages"][0]["direction"] == "in"
    assert msgs["messages"][1]["direction"] == "out"


# ═══════════ 6. BilibiliClient Mock Session & Write Safety ═══════════


class _FakeResp:
    def __init__(self, status_code=200, payload=None, text="", cookies=None):
        self.status_code = status_code
        self._payload = payload
        self.text = text if text else (json.dumps(payload, ensure_ascii=False) if payload is not None else "")
        self.cookies = cookies or {}

    def json(self):
        if self._payload is not None:
            return self._payload
        return json.loads(self.text)


class _FakeSession:
    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    async def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return await self.handler(method, url, kwargs)


def test_bilibili_client_cookie_isolation_writes_and_uncertain_guard():
    now_ts = time.time()
    state = {
        "cookies": [
            {"name": "SESSDATA", "value": "sess_val", "domain": ".bilibili.com", "expires": now_ts + 3600},
            {"name": "bili_jct", "value": "csrf_123", "domain": ".bilibili.com", "expires": now_ts + 3600},
            {"name": "DedeUserID", "value": "546195", "domain": ".bilibili.com", "expires": now_ts + 3600},
            {"name": "expired_ck", "value": "old", "domain": ".bilibili.com", "expires": now_ts - 10},
            {"name": "other_site", "value": "leak", "domain": ".douyin.com", "expires": now_ts + 3600},
        ]
    }
    cmap = cookie_map_from_state(json.dumps(state))
    assert cmap == {"SESSDATA": "sess_val", "bili_jct": "csrf_123", "DedeUserID": "546195"}
    assert has_bili_login_cookies(cmap)

    async def handler(method, url, kwargs):
        if "/x/web-interface/nav" in url:
            return _FakeResp(200, {"code": 0, "data": {"isLogin": True, "mid": 546195, "uname": "测试UP", "face": "//i0.hdslb.com/u.jpg", "wbi_img": WBI_FIXTURE}})
        if "/x/relation/stat" in url:
            return _FakeResp(200, {"code": 0, "data": {"follower": 1024, "following": 32}})
        if "/x/member/web/account" in url:
            return _FakeResp(200, {"code": 0, "data": {"sign": "签名", "sex": "保密"}})
        if "/x/web/archives" in url:
            return _FakeResp(200, {"code": 0, "data": {"arc_audits": [], "page": {"count": 12}}})
        if "/x/v2/reply/add" in url:
            body = parse_qs(kwargs["data"])
            assert body["oid"] == ["170001"]
            assert body["type"] == ["1"]
            assert body["csrf"] == ["csrf_123"]
            assert body["root"] == ["1001"]
            assert body["parent"] == ["1001"]
            return _FakeResp(200, {"code": 0, "data": {"rpid_str": "2002"}})
        if "/web_im/v1/web_im/send_msg" in url:
            q = parse_qs(urlsplit(url).query)
            assert "w_rid" in q and "wts" in q and q["w_receiver_id"] == ["667788"]
            body = parse_qs(kwargs["data"])
            assert json.loads(body["msg[content]"][0]) == {"content": "你好B站"}
            return _FakeResp(200, {"code": 0, "data": {"msg_key": 778899}})
        if "/x/relation/modify" in url:
            return _FakeResp(502, text="Bad Gateway")
        raise AssertionError(f"Unexpected URL: {url}")

    client = BilibiliClient(state)
    fake_sess = _FakeSession(handler)
    client._session = fake_sess

    prof, err = asyncio.run(client.fetch_self_profile())
    assert err == "" and prof["nickname"] == "测试UP" and prof["follower_count"] == 1024 and prof["aweme_count"] == 12
    assert client._mixin_key == EXPECTED_MIXIN_KEY

    submitted = []
    ok_c, err_c, data_c = asyncio.run(
        client.post_comment(
            "BV17x411w7KC",
            "回复内容",
            reply_to="1001",
            root_id="1001",
            on_submit=lambda: submitted.append("comment"),
        )
    )
    assert ok_c and err_c == "" and data_c["rpid"] == "2002"
    assert submitted == ["comment"]

    ok_dm, err_dm, data_dm = asyncio.run(
        client.send_dm("667788", "你好B站", on_submit=lambda: submitted.append("dm"))
    )
    assert ok_dm and err_dm == "" and data_dm["msg_key"] == "778899"
    assert submitted == ["comment", "dm"]

    # HTTP 502 after submit must return write_uncertain: so MonitorEngine never auto-retries
    ok_f, err_f = asyncio.run(
        client.modify_follow("667788", follow=True, on_submit=lambda: submitted.append("follow"))
    )
    assert not ok_f and err_f.startswith("write_uncertain:")
    assert submitted == ["comment", "dm", "follow"]

    # SSR __playinfo__ DASH extraction + HTML5 fallback when WBI returns v_voucher
    async def dash_handler(_method, url, _kwargs):
        if "www.bilibili.com/video/BV13qsm6SEUU" in url:
            ssr_html = (
                "<html><script>window.__playinfo__="
                + json.dumps({
                    "code": 0,
                    "data": {
                        "dash": {
                            "video": [
                                {"id": 80, "codecid": 12, "bandwidth": 2000, "baseUrl": "https://cdn.example/hevc.m4s"},
                                {"id": 80, "codecid": 7, "bandwidth": 1800, "baseUrl": "https://cdn.example/avc.m4s"},
                            ],
                            "audio": [
                                {"id": 30280, "bandwidth": 192000, "baseUrl": "https://cdn.example/audio.m4s"},
                            ],
                        }
                    },
                })
                + "</script></html>"
            )
            return _FakeResp(200, text=ssr_html)
        if "www.bilibili.com/video/BV1dx411c7Dj" in url:
            return _FakeResp(200, text="<html></html>")
        if "/x/player/playurl" in url and "/wbi/" not in url:
            return _FakeResp(
                200,
                {"code": 0, "data": {"quality": 64, "durl": [{"url": "https://cdn.example/html5_720p.mp4"}]}},
            )
        raise AssertionError(f"Unexpected playurl: {url}")

    client._buvid_checked = True
    client._session = _FakeSession(dash_handler)
    v_url, a_url, qlabel, s_err = asyncio.run(client.fetch_video_streams("BV13qsm6SEUU", 9988, qn=80))
    assert s_err == "" and qlabel == "1080P"
    assert v_url == "https://cdn.example/avc.m4s"
    assert a_url == "https://cdn.example/audio.m4s"

    v2, a2, q2, e2 = asyncio.run(client.fetch_video_streams("BV1dx411c7Dj", 100001, qn=80))
    assert e2 == "" and q2 == "720P" and v2 == "https://cdn.example/html5_720p.mp4" and a2 == ""


def test_bilibili_client_real_curl_cffi_session_quote_handling():
    """Verify real curl_cffi AsyncSession accepts default quote='' and WBI quote=False."""

    async def scenario():
        seen_paths: list[str] = []

        async def handle_conn(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
            try:
                head = await reader.readuntil(b"\r\n\r\n")
                req_line = head.split(b"\r\n", 1)[0].decode("latin1")
                path = req_line.split(" ")[1]
                seen_paths.append(path)
                body = json.dumps({"code": 0, "data": {"bvid": "BV17x411w7KC", "ok": True}}).encode("utf-8")
                resp = (
                    b"HTTP/1.1 200 OK\r\n"
                    b"Content-Type: application/json; charset=utf-8\r\n"
                    + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode("ascii")
                    + body
                )
                writer.write(resp)
                await writer.drain()
            finally:
                writer.close()

        server = await asyncio.start_server(handle_conn, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        base = f"http://127.0.0.1:{port}"
        try:
            cli = BilibiliClient({"SESSDATA": "s", "bili_jct": "c"})
            cli._buvid_checked = True
            cli._mixin_key = EXPECTED_MIXIN_KEY
            cli._wbi_fetched_at = time.time()
            async with cli.session_scope():
                # Default quote="" (normal GET with params)
                p1, e1 = await cli._get_json(f"{base}/x/web-interface/view", params={"bvid": "BV17x411w7KC"})
                assert e1 == "" and p1["data"]["bvid"] == "BV17x411w7KC"

                # Defensive coercion if a caller ever passes quote=True
                p2, e2 = await cli._get_json(f"{base}/x/web-interface/nav", params={"_t": "1"}, quote=True)
                assert e2 == "" and p2["code"] == 0

                # WBI signed GET (quote=False preserves exact signed query)
                p3, e3 = await cli._get_wbi_json(
                    f"{base}/x/web-interface/wbi/search/type",
                    {"search_type": "video", "keyword": "数码评测"},
                )
                assert e3 == "" and p3["code"] == 0
        finally:
            server.close()
            await server.wait_closed()

        assert len(seen_paths) == 3
        assert seen_paths[0] == "/x/web-interface/view?bvid=BV17x411w7KC"
        assert "w_rid=" in seen_paths[2] and "wts=" in seen_paths[2]

    asyncio.run(scenario())


# ═══════════ 7. System Integration (Downloader, Matrix, FastAPI Routes) ═══════════


def test_bilibili_system_and_api_integration(local_project, monkeypatch):
    assert "bilibili" in DOMESTIC_PLATFORMS

    cfg = Config()
    assert cfg.engine.bilibili_read_mode == "hybrid"
    assert cfg.engine.bilibili_write_mode == "hybrid"
    route = resolve_transport(cfg, "bilibili", "public_monitor")
    assert route["effective_mode"] == "hybrid" and route["api"] and route["browser"]
    cfg.engine.bilibili_read_mode = "api"
    api_only = resolve_transport(cfg, "bilibili", "public_monitor")
    assert api_only["effective_mode"] == "api" and not api_only["opens_browser"]

    dl = Downloader(str(local_project.root / "media"), "TestUA/1.0")
    captured_headers = {}
    real_async_client = httpx.AsyncClient

    def mock_client_factory(**kwargs):
        if "transport" not in kwargs:
            kwargs.pop("proxy", None)
            kwargs["transport"] = httpx.MockTransport(
                lambda req: (
                    captured_headers.update(dict(req.headers)),
                    httpx.Response(200, content=b"\x00\x00\x00\x18ftypmp42"),
                )[1]
            )
        return real_async_client(**kwargs)

    monkeypatch.setattr(downloader_mod.httpx, "AsyncClient", mock_client_factory)
    aw = parse_bili_aweme(
        {"bvid": "BV17x411w7KC", "aid": 170001, "title": "下载测试", "owner": {"mid": 1, "name": "UP"}},
        play_url="https://upos-sz-mirrorcos.bilivideo.com/sample.mp4",
    )
    ok_dl, saved_path, err_dl = asyncio.run(dl.download_aweme(aw))
    assert ok_dl and err_dl == "" and saved_path.endswith(".mp4")
    assert captured_headers.get("referer") == "https://www.bilibili.com/"

    local_project.cfg.risk_control.operation_gap_min_seconds = 0
    local_project.cfg.risk_control.operation_gap_max_seconds = 0
    local_project.cfg.risk_control.read_light_gap_seconds = 0
    local_project.cfg.risk_control.read_heavy_gap_seconds = 0
    engine = MonitorEngine(local_project.cfg, local_project.browser)
    monkeypatch.setattr(main, "engine", engine)

    with db.get_session() as s:
        acc = DouyinAccount(
            platform="bilibili",
            nickname="B站集成号",
            douyin_id="546195",
            sec_uid="546195",
            status="active",
            storage_state=json.dumps({
                "cookies": [
                    {"name": "SESSDATA", "value": "s1", "domain": ".bilibili.com"},
                    {"name": "bili_jct", "value": "j1", "domain": ".bilibili.com"},
                    {"name": "DedeUserID", "value": "546195", "domain": ".bilibili.com"},
                ]
            }),
        )
        s.add(acc)
        s.commit()
        s.refresh(acc)
        account_id = acc.id

    media_file = local_project.root / "demo.mp4"
    media_file.write_bytes(b"\x00\x00\x00\x18ftypmp42demo")
    monkeypatch.setattr(main, "UPLOAD_DIR", local_project.root)

    async def call_api():
        transport = httpx.ASGITransport(app=main.app, client=("127.0.0.1", 1234), raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            summary = await client.get("/api/overview/summary?platform=bilibili")
            assert summary.status_code == 200
            assert summary.json()["platform"] == "bilibili"
            assert summary.json()["accounts"] == 1

            local_project.cfg.engine.bilibili_read_mode = "browser"
            parts = await client.get(f"/api/accounts/{account_id}/bili/partitions")
            assert parts.status_code == 200, parts.text
            assert len(parts.json()["partitions"]) >= 5
            assert len(parts.json()["statements"]) >= 6
            local_project.cfg.engine.bilibili_read_mode = "hybrid"

            src = await client.post(
                "/api/collections",
                json={
                    "platform": "bilibili",
                    "source_type": "keyword",
                    "keywords": ["数码评测"],
                    "account_id": account_id,
                    "max_contents_per_keyword": 5,
                },
            )
            assert src.status_code == 200, src.text
            job_id = src.json()["id"]
            assert src.json()["platform"] == "bilibili"

            col_list = await client.get("/api/collections?platform=bilibili")
            assert col_list.status_code == 200
            assert [j["id"] for j in col_list.json()] == [job_id]
            col_other = await client.get("/api/collections?platform=douyin")
            assert col_other.status_code == 200 and col_other.json() == []

            # Simulate an in-flight stuck asyncio.Task and verify /cancel aborts it immediately
            with db.get_session() as s:
                jrow = s.get(main.KeywordCollectionJob, job_id)
                jrow.status = "running"
                jrow.current_step = "处理作品 1/37"
                s.add(jrow)
                s.commit()

            async def _slow_worker():
                await asyncio.sleep(60)

            engine._collection_tasks[job_id] = asyncio.create_task(_slow_worker())
            canceled = await client.post(f"/api/collections/{job_id}/cancel")
            assert canceled.status_code == 200, canceled.text
            assert canceled.json()["status"] == "canceled"
            assert canceled.json()["current_step"] == "已取消"
            assert job_id not in engine._collection_tasks

            mon = await client.post(
                "/api/monitors",
                json={
                    "platform": "bilibili",
                    "url_or_secuid": "https://space.bilibili.com/946974",
                    "account_id": account_id,
                    "interval_seconds": 600,
                    "download_enabled": False,
                },
            )
            assert mon.status_code == 200, mon.text
            mon_id = mon.json()["id"]
            assert mon.json()["platform"] == "bilibili" and mon.json()["sec_uid"] == "946974"

            async def _fake_user_videos(_self, _mid, **_kw):
                return (
                    [
                        {
                            "bvid": "BV17x411w7KC",
                            "aid": 170001,
                            "title": "监控扫描作品1",
                            "pic": "//i0.hdslb.com/bfs/archive/1.jpg",
                            "pubdate": int(time.time()),
                            "author": "示例UP",
                            "mid": 946974,
                        }
                    ],
                    False,
                    "",
                )

            monkeypatch.setattr(BilibiliClient, "fetch_user_videos", _fake_user_videos)
            scan_res = await client.post(f"/api/monitors/{mon_id}/run-now")
            assert scan_res.status_code == 200, scan_res.text
            scan_body = scan_res.json()
            assert scan_body["scanned"] == 1
            assert scan_body["filtered"] == 0
            assert scan_body["new"] == 1

            rec_dash = main.ContentRecord(
                platform="bilibili",
                target_id=mon_id,
                aweme_id="BV13qsm6SEUU",
                desc="DASH重试",
                media_type="video",
                create_time=1710000000,
                media_json=json.dumps([
                    {
                        "url": "https://cdn.example/v.m4s",
                        "audio_url": "https://cdn.example/a.m4s",
                        "kind": "video",
                        "ext": "mp4",
                        "index": 0,
                    }
                ]),
            )
            rebuilt = engine._rebuild_aweme(rec_dash, "示例UP")
            assert rebuilt.medias[0].audio_url == "https://cdn.example/a.m4s"

            async def _fake_empty_archives(_self, **_kw):
                return [], ""

            monkeypatch.setattr(BilibiliClient, "fetch_all_creator_archives", _fake_empty_archives)
            wsync = await client.post(f"/api/accounts/{account_id}/works/sync")
            assert wsync.status_code == 200, wsync.text
            assert wsync.json()["ok"] is True
            assert wsync.json()["fetched"] == 0 and wsync.json()["source"] == "api"

            async def _fake_follows_get_json(_self, url, **_kw):
                assert "/x/relation/followings" in url
                return (
                    {
                        "code": 0,
                        "data": {
                            "total": 3,
                            "list": [
                                {"mid": 101, "uname": "关注UP一", "face": "//i0.hdslb.com/1.jpg", "sign": "s1", "attribute": 2},
                                {"mid": 102, "uname": "关注UP二", "face": "//i0.hdslb.com/2.jpg", "sign": "s2", "attribute": 6},
                                {"mid": 103, "uname": "关注UP三", "face": "//i0.hdslb.com/3.jpg", "sign": "s3", "attribute": 2},
                            ],
                        },
                    },
                    "",
                )

            monkeypatch.setattr(BilibiliClient, "_get_json", _fake_follows_get_json)
            fsync = await client.post(f"/api/accounts/{account_id}/follows/sync?direction=following")
            assert fsync.status_code == 200, fsync.text
            assert fsync.json()["fetched"] == 3
            assert fsync.json()["stored"] == 3
            assert fsync.json()["added"] == 3
            assert fsync.json()["source"] == "api"

            flist = await client.get(f"/api/follows?account_id={account_id}&direction=following")
            assert flist.status_code == 200
            assert flist.json()["total"] == 3
            by_uid = {row["uid"]: row for row in flist.json()["items"]}
            assert by_uid["102"]["is_mutual"] is True and by_uid["102"]["is_following"] is True
            assert by_uid["101"]["is_mutual"] is False and by_uid["101"]["is_following"] is True

            cw = await client.post(
                "/api/comment-watches",
                json={
                    "platform": "bilibili",
                    "url_or_id": "BV17x411w7KC",
                    "kind": "auto",
                    "account_id": account_id,
                    "interval_seconds": 600,
                },
            )
            assert cw.status_code == 200, cw.text
            assert cw.json()["platform"] == "bilibili" and cw.json()["aweme_id"] == "BV17x411w7KC"

            dw = await client.post(
                "/api/danmaku-watches",
                json={
                    "url_or_id": "BV17x411w7KC",
                    "platform": "bilibili",
                    "kind": "video",
                    "mode": "public",
                    "account_id": account_id,
                },
            )
            assert dw.status_code == 200, dw.text
            assert dw.json()["platform"] == "bilibili"
            assert dw.json()["aweme_id"] == "BV17x411w7KC"

            pub = await client.post(
                "/api/publish",
                json={
                    "account_id": account_id,
                    "media_type": "video",
                    "media_paths": [str(media_file)],
                    "title": "B站API发布任务测试",
                    "desc": "简介",
                    "topics": "数码,科技",
                    "visibility": "private",
                    "bili_options": {
                        "tid": 95,
                        "copyright": 1,
                        "declaration_index": 3,
                        "recreate": -1,
                        "up_close_danmu": True,
                    },
                },
            )
            assert pub.status_code == 200, pub.text
            pub_list = await client.get("/api/publish?platform=bilibili")
            assert pub_list.status_code == 200
            rows = pub_list.json()
            assert len(rows) == 1
            assert rows[0]["platform"] == "bilibili"
            assert rows[0]["bili_options"]["tid"] == 95
            assert rows[0]["bili_options"]["declaration_index"] == 3

    asyncio.run(call_api())
