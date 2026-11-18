"""微信视频号 (WeChat Channels / Finder) 全链路能力单元与集成测试。

覆盖:
  1. WxIsaac64 128KiB 逆序密钥流位级一致性、MP4 Box 结构核验、Downloader 自动流式解密;
  2. 分享短链 / feed URL / Base64 uint64 编解码 / 公开预览接口 / ChannelsObject 与评论解析;
  3. pc_findergetcommentlist (cmdid 10753) 主评论与独立二级回复线程分页、去重、归属核验与脱敏;
  4. Node.js 实跑核验 finder_native_protocol.js 与 finder_protocol_browser_bootstrap.js 零请求加载与调用契约;
  5. WMPF Protobuf-Zlib 编解码、防解压炸弹、回环地址门禁与 ChannelsProtocolClient 会话调度;
  6. /api/share-download、KeywordCollector (account_works / keyword / creator)、MonitorEngine 作品与评论监控集成。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import struct
import subprocess
import tempfile
import time
import unittest
import zlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from sqlmodel import select

import app.db as db
import app.main as main_mod
from app.agent.workflows import WorkflowStep
from app.config import Config
from app.engine.collection import KeywordCollector
from app.engine.downloader import Downloader
from app.engine.monitor import MonitorEngine
from app.engine_settings import EngineSettingsPatch
from app.models import (
    CommentRecord,
    CommentWatch,
    ContentRecord,
    DouyinAccount,
    KeywordCollectionComment,
    KeywordCollectionContent,
    KeywordCollectionJob,
    MonitorTarget,
)
from app.platforms.channels import (
    CDP_RESULT,
    KEYSTREAM_SIZE,
    AttributionError,
    ChannelsCreatorResolution,
    ChannelsDecryptError,
    ChannelsPreviewError,
    ChannelsProtocolClient,
    ChannelsProtocolTransportError,
    CodecError,
    Envelope,
    Exchange,
    ProtocolSchema,
    Request,
    Target,
    build_finder_feed_url,
    build_native_bridge_params,
    build_request,
    collect_comments_async,
    decode_base64_to_uint64,
    decode_cdp,
    decrypt_channels_video_file,
    encode_cdp,
    encode_envelope,
    encode_uint64_base64,
    fetch_finder_preview_info,
    flatten_channels_comments,
    is_channels_share_url,
    normalize_finder_username,
    parse_channels_comment,
    parse_channels_feed,
    parse_channels_url,
    parse_exchange,
    resolve_channels_creator,
    resolve_channels_user_id,
    safe_response,
    to_camel_keys,
    to_snake_keys,
    unwrap_cdp_result,
    validate_decode_key,
    validate_loopback_url,
    validate_mp4_structure,
    wx_isaac64_keystream,
)


def _mp4_box(box_type: bytes, payload: bytes) -> bytes:
    assert len(box_type) == 4
    return struct.pack(">I4s", 8 + len(payload), box_type) + payload


def _build_synthetic_mp4(mdat_payload_size: int = 200_000) -> bytes:
    """构造具备标准 ftyp + moov(mvhd + trak) + mdat 结构的测试 MP4(超过 128KiB 以覆盖加密前缀与明文尾部)。"""
    ftyp = _mp4_box(b"ftyp", b"isom\x00\x00\x02\x00isomiso2mp41")
    mvhd = _mp4_box(b"mvhd", b"\x00" * 32)
    tkhd = _mp4_box(b"tkhd", b"\x00" * 24)
    trak = _mp4_box(b"trak", tkhd)
    moov = _mp4_box(b"moov", mvhd + trak)
    pattern = bytes((i * 37 + 13) & 0xFF for i in range(mdat_payload_size))
    mdat = _mp4_box(b"mdat", pattern)
    return ftyp + moov + mdat


def _encrypt_channels_mp4(plain_mp4: bytes, decode_key: str) -> bytes:
    ks = wx_isaac64_keystream(decode_key, KEYSTREAM_SIZE)
    prefix_len = min(len(plain_mp4), KEYSTREAM_SIZE)
    enc_prefix = bytes(a ^ b for a, b in zip(plain_mp4[:prefix_len], ks[:prefix_len]))
    return enc_prefix + plain_mp4[prefix_len:]


class ChannelsDecryptAndMp4Tests(unittest.IsolatedAsyncioTestCase):
    def test_wx_isaac64_keystream_matches_reference_wasm_sha256(self):
        ks = wx_isaac64_keystream("1707439823", KEYSTREAM_SIZE)
        self.assertEqual(len(ks), 131072)
        self.assertEqual(
            hashlib.sha256(ks).hexdigest(),
            "9596769dc29ccabdbf8e94dbb95671e85824d5e85db70bb6dbb11c47e95b6d9d",
        )

    def test_validate_decode_key_rejects_ambiguous_types(self):
        self.assertEqual(validate_decode_key("1707439823"), "1707439823")
        self.assertEqual(validate_decode_key(2**64 - 1), str(2**64 - 1))
        for bad in (True, False, 1.5, -1, 2**64, "", "0x1234", "abc"):
            with self.subTest(bad=bad), self.assertRaises(ChannelsDecryptError):
                validate_decode_key(bad)

    def test_decrypt_file_restores_exact_mp4_and_rejects_wrong_key(self):
        plain = _build_synthetic_mp4(210_000)
        encrypted = _encrypt_channels_mp4(plain, "1707439823")
        with self.assertRaises(ChannelsDecryptError):
            validate_mp4_structure(encrypted)

        with tempfile.TemporaryDirectory() as tmp:
            enc_path = Path(tmp) / "sample.mp4.enc"
            out_path = Path(tmp) / "sample.mp4"
            bad_out = Path(tmp) / "wrong.mp4"
            enc_path.write_bytes(encrypted)

            report = decrypt_channels_video_file(
                enc_path, out_path, "1707439823", validate_mp4=True
            )
            self.assertTrue(report["structure"]["valid"])
            self.assertEqual(out_path.read_bytes(), plain)

            with self.assertRaises(ChannelsDecryptError):
                decrypt_channels_video_file(
                    enc_path, bad_out, "1707439824", validate_mp4=True
                )
            self.assertFalse(bad_out.exists())

    async def test_downloader_decrypts_channels_video_and_sets_channels_referer(self):
        plain = _build_synthetic_mp4(160_000)
        encrypted = _encrypt_channels_mp4(plain, "1707439823")
        seen_headers: dict[str, str] = {}

        def _handler(request: httpx.Request) -> httpx.Response:
            seen_headers.update(dict(request.headers))
            return httpx.Response(200, content=encrypted, headers={"Content-Type": "video/mp4"})

        real_async_client = httpx.AsyncClient

        def _patched_client(*args, **kwargs):
            kwargs.pop("proxy", None)
            kwargs["transport"] = httpx.MockTransport(_handler)
            return real_async_client(*args, **kwargs)

        raw_feed = {
            "id": "15048048986165157126",
            "objectNonceId": "9876543210_nonce",
            "createtime": 1775229591,
            "contact": {"username": "v2_060000231003@finder", "nickname": "测试作者"},
            "objectDesc": {
                "description": "加密视频测试",
                "mediaType": 4,
                "media": [
                    {
                        "mediaType": 4,
                        "url": "https://finder.video.qq.com/251/20302/stodownload",
                        "urlToken": "?token=abc123",
                        "decodeKey": "1707439823",
                        "thumbUrl": "https://finder.video.qq.com/thumb.jpg",
                        "spec": [
                            {"fileFormat": "xWT111", "bitRate": 900000, "width": 720, "height": 1280},
                            {"fileFormat": "xWT112", "bitRate": 2100000, "width": 1080, "height": 1920},
                        ],
                    }
                ],
            },
        }
        aweme = parse_channels_feed(raw_feed, quality="highest")
        self.assertIsNotNone(aweme)
        self.assertEqual(aweme.medias[0].decode_key, "1707439823")
        self.assertIn("X-snsvideoflag=xWT112", aweme.medias[0].url)

        with tempfile.TemporaryDirectory() as tmp:
            dl = Downloader(tmp, "Mozilla/5.0 Test", timeout=20.0)
            with patch("app.engine.downloader.httpx.AsyncClient", side_effect=_patched_client):
                ok, saved_path, err = await dl.download_aweme(aweme, base_dir=tmp)
            self.assertTrue(ok, err)
            self.assertEqual(err, "")
            self.assertEqual(seen_headers.get("referer"), "https://channels.weixin.qq.com/")
            saved = Path(saved_path)
            self.assertTrue(saved.is_file())
            self.assertFalse(saved.with_suffix(".mp4.enc").exists())
            self.assertEqual(saved.read_bytes(), plain)


class ChannelsResolveAndExtractTests(unittest.IsolatedAsyncioTestCase):
    def test_uint64_base64_roundtrip_preserves_64bit_integer(self):
        oid = "15048048986165157126"
        nid = "1293847561029384756"
        b64_oid = encode_uint64_base64(oid)
        self.assertEqual(decode_base64_to_uint64(b64_oid), oid)
        feed_url = build_finder_feed_url(oid, nid)
        parsed = parse_channels_url(feed_url)
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.object_id, oid)
        self.assertEqual(parsed.object_nonce_id, nid)

    def test_share_url_recognition_and_non_channels_exclusion(self):
        share_copy = "来看看这个视频号作品吧！https://weixin.qq.com/sph/Amv88hJDUj 真的太精彩了"
        ref = parse_channels_url(share_copy)
        self.assertIsNotNone(ref)
        self.assertEqual(ref.kind, "share")
        self.assertEqual(ref.share_id, "Amv88hJDUj")
        self.assertTrue(is_channels_share_url("https://weixin.qq.com/sph/Amv88hJDUj"))
        self.assertTrue(is_channels_share_url("https://channels.weixin.qq.com/web/pages/feed?oid=abc"))
        self.assertFalse(is_channels_share_url("https://v.qq.com/x/cover/mzc00200.html"))
        self.assertEqual(
            normalize_finder_username("v2_060000231003b20faec8@finder"),
            "v2_060000231003b20faec8@finder",
        )

    async def test_resolve_channels_creator_supports_direct_and_video_share_reverse_lookup(self):
        # 1) Direct creator identifiers & profile URLs resolve without network
        self.assertEqual(
            await resolve_channels_user_id("v2_060000231003b20faec8"),
            "v2_060000231003b20faec8@finder",
        )
        self.assertEqual(
            await resolve_channels_user_id(
                "https://channels.weixin.qq.com/web/pages/profile?username=v2_060000231003b20faec8@finder"
            ),
            "v2_060000231003b20faec8@finder",
        )
        self.assertEqual(
            await resolve_channels_user_id(
                "https://channels.weixin.qq.com/web/pages/author?finderUsername=v2_060000231003b20faec8@finder"
            ),
            "v2_060000231003b20faec8@finder",
        )
        self.assertEqual(await resolve_channels_user_id("sphAccountId1"), "sphAccountId1")

        # 2) Video share URL when preview returns author username directly
        preview_with_user = {
            "export_id": "export/UzFfAgtgek",
            "author": "直接预览作者",
            "author_username": "v2_preview_creator@finder",
            "author_avatar": "https://img/p.jpg",
        }
        with patch(
            "app.platforms.channels.resolve.fetch_finder_preview_info",
            new=AsyncMock(return_value=preview_with_user),
        ):
            res_direct = await resolve_channels_creator("https://weixin.qq.com/sph/Amv88hJDUj")
        self.assertEqual(res_direct.username, "v2_preview_creator@finder")
        self.assertEqual(res_direct.nickname, "直接预览作者")

        # 3) Video share URL when preview lacks username -> falls back to local WeChat CDP fetch_feed_detail
        preview_no_user = {
            "export_id": "export/UzFfAgtgek",
            "author": "仅昵称作者",
            "author_username": "",
            "author_avatar": "https://img/a.jpg",
        }
        cfg = Config()
        cfg.engine.channels_cdp_endpoint = "http://127.0.0.1:9222"
        detail_obj = {
            "id": "15048048986165157126",
            "contact": {
                "username": "v2_reversed_creator@finder",
                "nickname": "反查创作者",
                "headUrl": "https://img/rev.jpg",
            },
        }
        with (
            patch(
                "app.platforms.channels.resolve.fetch_finder_preview_info",
                new=AsyncMock(return_value=preview_no_user),
            ),
            patch.object(
                ChannelsProtocolClient,
                "fetch_feed_detail",
                new=AsyncMock(return_value=(detail_obj, "")),
            ) as mock_detail,
            patch.object(ChannelsProtocolClient, "close", new=AsyncMock()),
        ):
            res_proto = await resolve_channels_creator(
                "https://weixin.qq.com/sph/Amv88hJDUj", cfg=cfg
            )
        mock_detail.assert_awaited_once()
        self.assertEqual(res_proto.username, "v2_reversed_creator@finder")
        self.assertEqual(res_proto.nickname, "反查创作者")
        self.assertEqual(res_proto.avatar, "https://img/rev.jpg")

        # 4) Video share URL without local WeChat channel explains clearly what is needed
        cfg_none = Config()
        cfg_none.engine.channels_cdp_endpoint = ""
        with patch(
            "app.platforms.channels.resolve.fetch_finder_preview_info",
            new=AsyncMock(return_value=preview_no_user),
        ):
            res_missing = await resolve_channels_creator(
                "https://weixin.qq.com/sph/Amv88hJDUj", cfg=cfg_none
            )
        self.assertEqual(res_missing.username, "")
        self.assertIn("仅昵称作者", res_missing.error)
        self.assertIn("视频号微信通道", res_missing.error)

    async def test_fetch_finder_preview_info_parses_metadata_without_fake_media(self):
        def _handler(request: httpx.Request) -> httpx.Response:
            body_text = request.content.decode("utf-8", errors="ignore")
            if "bad" in body_text or "bad" in str(request.url):
                return httpx.Response(401, json={"errCode": 401})
            return httpx.Response(
                200,
                json={
                    "errCode": 0,
                    "data": {
                        "feedInfo": {
                            "description": "公开预览标题",
                            "coverUrl": "https://wx.qlogo.cn/preview_cover.jpg",
                            "createTime": 1775229591,
                            "likeCount": 42,
                        },
                        "authorInfo": {
                            "nickname": "预览创作者",
                        },
                        "sceneInfo": {
                            "dynamicExportId": "export/UzFfAgtgekIEAQAAAAAA",
                        },
                    },
                },
            )

        real_async_client = httpx.AsyncClient

        def _patched_client(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(_handler)
            return real_async_client(*args, **kwargs)

        with patch("app.platforms.channels.resolve.httpx.AsyncClient", side_effect=_patched_client):
            info = await fetch_finder_preview_info(share_id="Amv88hJDUj")
            self.assertEqual(info["export_id"], "export/UzFfAgtgekIEAQAAAAAA")
            self.assertEqual(info["author"], "预览创作者")
            aweme = parse_channels_feed(info)
            self.assertIsNotNone(aweme)
            self.assertEqual(aweme.medias, [])
            self.assertEqual(aweme.cover, "https://wx.qlogo.cn/preview_cover.jpg")

            with self.assertRaises(ChannelsPreviewError):
                await fetch_finder_preview_info(share_id="bad")

    def test_parse_and_flatten_channels_comments(self):
        raw_comments = [
            {
                "commentId": "900000000000000001",
                "content": "主评论内容",
                "nickname": "用户甲",
                "username": "v2_user_a@finder",
                "likeCount": 12,
                "createtime": 1775229600,
                "replyCommentId": "0",
                "ipRegionInfo": {"regionText": "广东"},
                "levelTwoComment": [
                    {
                        "commentId": "900000000000000002",
                        "content": "二级回复内容",
                        "nickname": "用户乙",
                        "username": "v2_user_b@finder",
                        "likeCount": 3,
                        "createtime": 1775229650,
                        "replyCommentId": "0",
                        "ipRegionInfo": {"regionText": "浙江"},
                    }
                ],
            }
        ]
        flat = [parse_channels_comment(x) for x in flatten_channels_comments(raw_comments)]
        self.assertEqual(len(flat), 2)
        self.assertEqual(flat[0]["reply_to"], "")
        self.assertEqual(flat[0]["ip_label"], "广东")
        self.assertEqual(flat[1]["reply_to"], "900000000000000001")
        self.assertEqual(flat[1]["ip_label"], "浙江")


class ChannelsCommentProtocolAndJsTests(unittest.IsolatedAsyncioTestCase):
    def test_key_case_mapping_and_redaction(self):
        camel = to_camel_keys({"object_id": "123", "root_comment_id": "0", "BaseResponse": {"Ret": 0}})
        self.assertEqual(camel["objectId"], "123")
        self.assertEqual(camel["BaseResponse"]["Ret"], 0)
        snake = to_snake_keys(camel)
        self.assertEqual(snake["object_id"], "123")
        self.assertEqual(snake["BaseResponse"]["Ret"], 0)

        bridge = build_native_bridge_params({"objectId": "123", "scene": 145})
        self.assertEqual(bridge["cgi_cmdid"], 10753)
        self.assertEqual(bridge["url"], "/cgi-bin/micromsg-bin/pc_findergetcommentlist")
        self.assertEqual(json.loads(bridge["req_json"])["object_id"], "123")

        sanitized = safe_response(
            {
                "errCode": 0,
                "h5AuthToken": "secret-auth-token",
                "data": {
                    "BaseResponse": {"Ret": 0},
                    "reportJson": '{"session":"sensitive"}',
                    "commentInfo": [{"commentId": "1", "content": "公开评论", "cookie": "drop-me"}],
                },
            }
        )
        dumped = json.dumps(sanitized, ensure_ascii=False)
        self.assertNotIn("secret-auth-token", dumped)
        self.assertNotIn("sensitive", dumped)
        self.assertNotIn("drop-me", dumped)
        self.assertIn("公开评论", dumped)

    async def test_collect_comments_async_pages_main_and_reply_threads_with_dedupe(self):
        oid = "15048048986165157126"
        nid = "nonce_token_1"
        target = Target(object_id=oid, object_nonce_id=nid)

        async def _fake_transport(req: Request) -> Exchange:
            p = req.payload
            root_id = str(p.get("rootCommentId") or "0")
            buf = str(p.get("lastBuffer") or "")
            if root_id == "0" and buf == "":
                resp = {
                    "errCode": 0,
                    "data": {
                        "BaseResponse": {"Ret": 0},
                        "commentCount": 4,
                        "lastBuffer": "MAIN_CUR_1",
                        "downContinueFlag": 1,
                        "commentInfo": [
                            {
                                "commentId": "101",
                                "content": "第一条根评论",
                                "nickname": "甲",
                                "username": "u_a",
                                "createtime": 1700000001,
                                "likeCount": 5,
                                "expandCommentCount": 2,
                                "levelTwoComment": [
                                    {
                                        "commentId": "201",
                                        "content": "内联预览二级回复",
                                        "nickname": "乙",
                                        "username": "u_b",
                                        "createtime": 1700000002,
                                    }
                                ],
                            }
                        ],
                    },
                }
            elif root_id == "0" and buf == "MAIN_CUR_1":
                resp = {
                    "errCode": 0,
                    "data": {
                        "BaseResponse": {"Ret": 0},
                        "commentCount": 4,
                        "lastBuffer": "MAIN_CUR_2",
                        "downContinueFlag": 0,
                        "commentInfo": [
                            {
                                "commentId": "102",
                                "content": "第二条根评论",
                                "nickname": "丙",
                                "username": "u_c",
                                "createtime": 1700000003,
                                "expandCommentCount": 0,
                            }
                        ],
                    },
                }
            elif root_id == "101":
                resp = {
                    "errCode": 0,
                    "data": {
                        "BaseResponse": {"Ret": 0},
                        "lastBuffer": "REPLY_CUR_END",
                        "downContinueFlag": 0,
                        "commentInfo": [
                            {
                                "commentId": "201",
                                "content": "内联预览二级回复",
                                "nickname": "乙",
                                "username": "u_b",
                                "createtime": 1700000002,
                            },
                            {
                                "commentId": "202",
                                "content": "独立分页拉出的第二条二级回复",
                                "nickname": "丁",
                                "username": "u_d",
                                "createtime": 1700000004,
                            },
                        ],
                    },
                }
            else:
                raise AssertionError(f"Unexpected request: {p}")
            return Exchange(request_id=req.request_id, response=resp, evidence_ref="mem://test")

        audit = await collect_comments_async(
            target, _fake_transport, schema=ProtocolSchema(), include_replies=True, delay=0.0
        )
        self.assertEqual(
            audit["main_pagination"]["pagination_stop_reason"],
            "explicit_downContinueFlag_zero",
        )
        self.assertTrue(audit["all_accessible_pages_exhausted"])
        self.assertEqual(audit["record_count"], 4)
        self.assertEqual(len(audit["reply_pagination"]), 1)
        ids = [r["comment_id"] for r in audit["records"]]
        self.assertEqual(ids, ["101", "201", "102", "202"])

    async def test_attribution_error_blocks_cross_object_response(self):
        target = Target(object_id="15048048986165157126", object_nonce_id="nonce", enter_session_id="1000")
        req = build_request(target, ProtocolSchema())
        bad_ex = Exchange(
            request_id=req.request_id,
            response={
                "errCode": 0,
                "data": {
                    "BaseResponse": {"Ret": 0},
                    "objectId": "9999999999999999999",
                    "downContinueFlag": 0,
                    "commentInfo": [],
                },
            },
            evidence_ref="mem://cross-object",
        )
        with self.assertRaises(AttributionError):
            parse_exchange(req, bad_ex)

        async def _wrong_object_transport(r: Request) -> Exchange:
            return Exchange(
                request_id=r.request_id,
                response=bad_ex.response,
                evidence_ref="mem://cross-object",
            )

        audit = await collect_comments_async(target, _wrong_object_transport, delay=0.0)
        self.assertEqual(audit["record_count"], 0)
        self.assertTrue(
            audit["main_pagination"]["pagination_stop_reason"].startswith("protocol_error:")
        )

    def test_js_protocol_and_bootstrap_scripts_execute_cleanly_in_node(self):
        node_bin = shutil.which("node") or r"C:\Program Files\nodejs\node.exe"
        if not Path(node_bin).exists():
            self.skipTest("Node.js runtime not installed")

        js_dir = Path(__file__).resolve().parents[1] / "app" / "platforms" / "channels" / "js"
        native_js = (js_dir / "finder_native_protocol.js").as_posix()
        boot_js = (js_dir / "finder_protocol_browser_bootstrap.js").as_posix()

        script = f"""
const fs = require("fs");
const vm = require("vm");
const assert = require("assert");

const nativeMod = require({json.dumps(native_js)});
const params = nativeMod.buildBridgeParams({{ objectId: "15048048986165157126" }});
assert.strictEqual(params.cgi_cmdid, 10753);
assert.strictEqual(JSON.parse(params.req_json).object_id, "15048048986165157126");

let invokeCount = 0;
const ctx = vm.createContext({{
  globalThis: null,
  location: {{ href: "https://channels.weixin.qq.com/web/pages/feed" }},
  document: {{ scripts: [], querySelectorAll: () => [] }},
  performance: {{ getEntriesByType: () => [] }},
  WeixinJSBridge: {{ invoke: () => {{ invokeCount += 1; }} }},
  WXU: {{
    API: {{
      finderGetCommentList: async (body) => ({{ errCode: 0, echoedObjectId: body.objectId, echoedDirection: body.direction }}),
      finderGetCommentDetail: async (body) => ({{ errCode: 0, data: {{ BaseResponse: {{ Ret: 0 }}, object: {{ id: body.objectid }} }} }}),
      finderUserPage: async (body) => ({{ errCode: 0, data: {{ BaseResponse: {{ Ret: 0 }}, object: [], username: body.username }} }}),
    }},
    API2: {{
      finderSearch: async (body) => ({{ errCode: 0, data: {{ BaseResponse: {{ Ret: 0 }}, objectList: [], query: body.query }} }}),
    }},
  }},
}});
ctx.globalThis = ctx;
vm.runInContext(fs.readFileSync({json.dumps(boot_js)}, "utf8"), ctx);
assert.strictEqual(invokeCount, 0, "bootstrap must fire zero requests on load");
(async () => {{
  const bindRes = await ctx.__finderProtocolLoad();
  assert.strictEqual(bindRes.ready, true);
  const page = await ctx.__finderCommentsProtocol.fetchPage({{
    objectId: "12345",
    objectNonceId: "nonce_1",
    enterSessionId: "999",
  }});
  assert.strictEqual(page.echoedObjectId, "12345");
  assert.strictEqual(page.echoedDirection, 2);
  const detail = await ctx.__finderProtocol.fetchFeedDetail({{ objectId: "12345" }});
  assert.strictEqual(detail.data.object.id, "12345");
  console.log("NODE_JS_BRIDGE_OK");
}})().catch(err => {{ console.error(err); process.exit(1); }});
"""
        proc = subprocess.run(
            [node_bin, "-e", script],
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("NODE_JS_BRIDGE_OK", proc.stdout)


class WmpfCodecAndProtocolClientTests(unittest.IsolatedAsyncioTestCase):
    def test_wmpf_protobuf_zlib_roundtrip_and_zip_bomb_guard(self):
        payload_str = json.dumps({"id": 7, "result": {"result": {"value": {"ok": True}}}})
        raw_frame = encode_cdp(payload_str, seq=42, category=CDP_RESULT, compress=True)
        msg = decode_cdp(raw_frame)
        self.assertEqual(msg.seq, 42)
        self.assertEqual(json.loads(unwrap_cdp_result(raw_frame))["result"]["result"]["value"]["ok"], True)

        # 构造超过上限的压缩载荷，确认流式解压立即阻断内存膨胀
        giant_compressed = zlib.compress(b"A" * 200_000)
        bomb_frame = encode_envelope(
            Envelope(
                seq=1,
                after=0,
                category=CDP_RESULT,
                data=giant_compressed,
                compress_algo=1,
                original_size=200_000,
            )
        )
        with self.assertRaises(CodecError):
            decode_cdp(bomb_frame, maximum=32_768)

    def test_loopback_url_guard_rejects_remote_and_credentials(self):
        self.assertEqual(
            validate_loopback_url("http://127.0.0.1:9222", schemes=("http", "https")),
            "http://127.0.0.1:9222",
        )
        for bad in (
            "http://192.168.1.50:9222",
            "http://example.com:9222",
            "http://admin:secret@127.0.0.1:9222",
            "ftp://127.0.0.1:9222",
        ):
            with self.subTest(bad=bad), self.assertRaises(ChannelsProtocolTransportError):
                validate_loopback_url(bad, schemes=("http", "https"))

    async def test_protocol_client_methods_dispatch_and_resolve_missing_nonce(self):
        client = ChannelsProtocolClient(endpoint="http://127.0.0.1:9222")
        calls: list[tuple[str, dict]] = []

        async def _mock_eval(method: str, payload: dict) -> dict:
            calls.append((method, payload))
            if method == "fetchFeedDetail":
                return {
                    "errCode": 0,
                    "data": {
                        "BaseResponse": {"Ret": 0},
                        "commentCount": 2,
                        "object": {
                            "id": "15048048986165157126",
                            "objectNonceId": "resolved_nonce_99",
                            "createtime": 1775229591,
                            "contact": {"username": "v2_author@finder", "nickname": "协议作者"},
                            "objectDesc": {"description": "详情标题", "mediaType": 4, "media": []},
                        },
                    },
                }
            if method == "fetchCommentsPage":
                return {
                    "errCode": 0,
                    "data": {
                        "BaseResponse": {"Ret": 0},
                        "downContinueFlag": 0,
                        "lastBuffer": "END",
                        "commentInfo": [
                            {
                                "commentId": "501",
                                "content": "来自原生协议的评论",
                                "nickname": "读者",
                                "username": "v2_reader@finder",
                                "createtime": 1775229700,
                                "likeCount": 9,
                                "ipRegionInfo": {"regionText": "上海"},
                            }
                        ],
                    },
                }
            if method == "search":
                return {
                    "errCode": 0,
                    "data": {
                        "BaseResponse": {"Ret": 0},
                        "continueFlag": 0,
                        "objectList": [
                            {
                                "id": "15048048986165157126",
                                "objectNonceId": "resolved_nonce_99",
                                "createtime": 1775229591,
                                "contact": {"username": "v2_author@finder", "nickname": "协议作者"},
                                "objectDesc": {"description": "露营搜索结果", "mediaType": 4, "media": []},
                            }
                        ],
                    },
                }
            raise AssertionError(f"Unexpected method: {method}")

        client._eval_call = AsyncMock(side_effect=_mock_eval)
        search_items, search_err = await client.search_feeds("露营", max_results=5, max_pages=1)
        self.assertEqual(search_err, "")
        self.assertEqual(len(search_items), 1)

        # 未提供 objectNonceId 时，collect_comments 应先调用 fetchFeedDetail 自动补齐
        comments, audit, c_err = await client.collect_comments(
            "15048048986165157126", "", limit=10, include_replies=True
        )
        self.assertEqual(c_err, "")
        self.assertEqual(len(comments), 1)
        self.assertEqual(comments[0]["text"], "来自原生协议的评论")
        self.assertEqual(comments[0]["ip_label"], "上海")
        self.assertEqual([m for m, _ in calls[-2:]], ["fetchFeedDetail", "fetchCommentsPage"])


class ChannelsEngineAndApiIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.prev_engine = db._engine
        self.prev_cfg = main_mod.cfg
        self.tmp = tempfile.TemporaryDirectory()
        db_path = Path(self.tmp.name) / "channels-integration.db"
        db.init_db(str(db_path))
        self.cfg = Config()
        self.cfg.engine.media_dir = str(Path(self.tmp.name) / "media")
        self.cfg.engine.channels_read_mode = "auto"
        self.cfg.engine.channels_cdp_endpoint = "http://127.0.0.1:9222"
        main_mod.cfg = self.cfg

        with db.get_session() as s:
            acc = DouyinAccount(
                platform="shipinhao",
                nickname="测试视频号",
                sec_uid="v2_owner@finder",
                status="active",
                storage_state='{"cookies":[{"name":"sessionid","value":"ok","domain":".weixin.qq.com"}]}',
            )
            s.add(acc)
            s.commit()
            s.refresh(acc)
            self.account_id = acc.id
            self.account_ns = SimpleNamespace(
                id=acc.id,
                platform="shipinhao",
                nickname=acc.nickname,
                sec_uid=acc.sec_uid,
                storage_state=acc.storage_state,
                proxy="",
            )

    async def asyncTearDown(self):
        main_mod.cfg = self.prev_cfg
        if db._engine is not None:
            db._engine.dispose()
        db._engine = self.prev_engine
        self.tmp.cleanup()

    def test_settings_patch_and_agent_workflow_accept_shipinhao(self):
        patch_obj = EngineSettingsPatch.model_validate({"channels_read_mode": "protocol"})
        self.assertEqual(patch_obj.channels_read_mode, "protocol")

        step_col = WorkflowStep(
            kind="create_collection",
            platform="shipinhao",
            account_id=self.account_id,
            keywords=["视频号测试"],
        )
        self.assertEqual(step_col.payload()["platform"], "shipinhao")

        step_mon = WorkflowStep(
            kind="create_monitor",
            platform="shipinhao",
            account_id=self.account_id,
            target="https://channels.weixin.qq.com/web/pages/profile?username=v2_abc@finder",
        )
        self.assertEqual(step_mon.payload()["platform"], "shipinhao")

    async def test_share_download_endpoint_decrypts_channels_video_and_falls_back_for_v_qq(self):
        plain_mp4 = _build_synthetic_mp4(150_000)
        enc_mp4 = _encrypt_channels_mp4(plain_mp4, "1707439823")

        def _cdn_handler(request: httpx.Request) -> httpx.Response:
            if "cover.jpg" in str(request.url):
                return httpx.Response(200, content=b"\xff\xd8\xff\xd9", headers={"Content-Type": "image/jpeg"})
            return httpx.Response(200, content=enc_mp4, headers={"Content-Type": "video/mp4"})

        real_async_client = httpx.AsyncClient

        def _patched_dl_client(*args, **kwargs):
            kwargs.pop("proxy", None)
            kwargs["transport"] = httpx.MockTransport(_cdn_handler)
            return real_async_client(*args, **kwargs)

        fake_detail = {
            "id": "15048048986165157126",
            "objectNonceId": "nonce_share_1",
            "createtime": 1775229591,
            "likeCount": 88,
            "commentCount": 12,
            "contact": {"username": "v2_share_author@finder", "nickname": "分享作者"},
            "objectDesc": {
                "description": "分享链加密视频下载测试",
                "mediaType": 4,
                "media": [
                    {
                        "mediaType": 4,
                        "url": "https://finder.video.qq.com/251/20302/stodownload",
                        "urlToken": "?token=xyz",
                        "decodeKey": "1707439823",
                        "thumbUrl": "https://finder.video.qq.com/cover.jpg",
                        "videoPlayLen": 15,
                    }
                ],
            },
        }

        out_dir = Path(self.tmp.name) / "share_out"
        with (
            patch.object(
                ChannelsProtocolClient,
                "connect",
                new=AsyncMock(return_value={"ready": True}),
            ),
            patch.object(ChannelsProtocolClient, "close", new=AsyncMock()),
            patch.object(
                ChannelsProtocolClient,
                "fetch_feed_detail",
                new=AsyncMock(return_value=(fake_detail, "")),
            ),
            patch("app.engine.downloader.httpx.AsyncClient", side_effect=_patched_dl_client),
            patch("app.platforms.channels.share.httpx.AsyncClient", side_effect=_patched_dl_client),
        ):
            res = await main_mod.share_download(
                main_mod.ShareDownloadIn(
                    share_text="微信分享 https://weixin.qq.com/sph/Amv88hJDUj 快看",
                    download=True,
                    output_dir=str(out_dir),
                )
            )

        self.assertTrue(res["ok"], res)
        item = res["results"][0]
        self.assertTrue(item["ok"])
        self.assertEqual(item["metadata"]["decrypted_with"], "WxIsaac64-128KiB")
        media_files = [f for f in item["files"] if f["role"] == "media"]
        self.assertEqual(len(media_files), 1)
        self.assertEqual(Path(media_files[0]["path"]).read_bytes(), plain_mp4)

        # 非视频号 qq.com 链接(如 v.qq.com)应自动走 ShareDownloader 通用提取器
        with patch.object(
            main_mod.ShareDownloader,
            "inspect",
            new=AsyncMock(return_value={"ok": True, "url": "https://v.qq.com/x/cover/abc.html", "metadata": {"title": "腾讯视频"}}),
        ) as generic_inspect:
            qq_res = await main_mod.share_download(
                main_mod.ShareDownloadIn(
                    share_text="https://v.qq.com/x/cover/abc.html",
                    download=False,
                )
            )
            self.assertTrue(qq_res["ok"])
            generic_inspect.assert_awaited_once()

    async def test_keyword_collection_and_monitor_pipelines_for_shipinhao(self):
        plain_mp4 = _build_synthetic_mp4(140_000)
        enc_mp4 = _encrypt_channels_mp4(plain_mp4, "1707439823")

        def _cdn_handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=enc_mp4, headers={"Content-Type": "video/mp4"})

        real_async_client = httpx.AsyncClient

        def _patched_dl_client(*args, **kwargs):
            kwargs.pop("proxy", None)
            kwargs["transport"] = httpx.MockTransport(_cdn_handler)
            return real_async_client(*args, **kwargs)

        now_ts = int(time.time())
        feed_item = {
            "id": "15048048986165157126",
            "objectNonceId": "nonce_col_1",
            "createtime": now_ts,
            "likeCount": 320,
            "commentCount": 2,
            "contact": {"username": "v2_creator_x@finder", "nickname": "户外达人"},
            "objectDesc": {
                "description": "露营篝火教程",
                "mediaType": 4,
                "media": [
                    {
                        "mediaType": 4,
                        "url": "https://finder.video.qq.com/251/20302/stodownload",
                        "urlToken": "?token=col",
                        "decodeKey": "1707439823",
                        "thumbUrl": "https://finder.video.qq.com/thumb.jpg",
                    }
                ],
            },
        }
        proto_comments = [
            {
                "comment_id": "701",
                "text": "求帐篷链接",
                "user_nickname": "路人甲",
                "user_sec_uid": "v2_u1@finder",
                "ip_label": "四川",
                "like_count": 6,
                "create_time": now_ts,
                "reply_to": "",
            },
            {
                "comment_id": "702",
                "text": "同求+1",
                "user_nickname": "路人乙",
                "user_sec_uid": "v2_u2@finder",
                "ip_label": "北京",
                "like_count": 2,
                "create_time": now_ts + 1,
                "reply_to": "701",
            },
        ]

        with db.get_session() as s:
            job = KeywordCollectionJob(
                platform="shipinhao",
                account_id=self.account_id,
                source_type="keyword",
                keywords='["露营"]',
                max_contents_per_keyword=5,
                max_pages_per_keyword=2,
                max_comments_per_content=10,
                include_replies=True,
                download_media=True,
                download_dir=str(Path(self.tmp.name) / "col_dl"),
            )
            s.add(job)
            s.commit()
            s.refresh(job)
            job_id = job.id

        browser = SimpleNamespace(
            identity_for=lambda _acc: SimpleNamespace(ua="Mozilla/5.0"),
            anon_identity=lambda: SimpleNamespace(ua="Mozilla/5.0"),
        )
        downloader = Downloader(self.cfg.engine.media_dir, "Mozilla/5.0", timeout=20.0)
        collector = KeywordCollector(self.cfg, browser, downloader)

        with (
            patch.object(
                ChannelsProtocolClient,
                "search_feeds",
                new=AsyncMock(return_value=([feed_item], "")),
            ),
            patch.object(
                ChannelsProtocolClient,
                "collect_comments",
                new=AsyncMock(return_value=(proto_comments, {"collected_total": 2}, "")),
            ),
            patch.object(ChannelsProtocolClient, "close", new=AsyncMock()),
            patch("app.engine.downloader.httpx.AsyncClient", side_effect=_patched_dl_client),
            patch("app.engine.collection.asyncio.sleep", new=AsyncMock()),
        ):
            col_res = await collector.run(job_id, self.account_ns)

        self.assertEqual(col_res["contents"], 1)
        self.assertEqual(col_res["comments"], 2)

        with db.get_session() as s:
            row = s.exec(
                select(KeywordCollectionContent).where(KeywordCollectionContent.job_id == job_id)
            ).one()
            self.assertEqual(row.download_status, "done")
            self.assertEqual(row.xsec_token, "nonce_col_1")
            self.assertEqual(row.author_id, "v2_creator_x@finder")
            self.assertEqual(Path(row.local_path).read_bytes(), plain_mp4)
            content_id = row.id

        media_preview = await main_mod.keyword_collection_content_media(job_id, content_id)
        self.assertTrue(media_preview["local_url"].endswith("/local-media/0"))

        # 验证作品监控与评论监控 API 创建 + 扫描入库
        mon_created = await main_mod.add_monitor(
            main_mod.TargetIn(
                platform="shipinhao",
                url_or_secuid="v2_creator_x@finder",
                account_id=self.account_id,
                initial_backfill_count=5,
                download_enabled=True,
                download_dir=str(Path(self.tmp.name) / "mon_dl"),
            )
        )
        self.assertEqual(mon_created["platform"], "shipinhao")
        self.assertEqual(mon_created["sec_uid"], "v2_creator_x@finder")

        watch_url = build_finder_feed_url("15048048986165157126", "nonce_col_1")
        watch_created = await main_mod.add_watch(
            main_mod.WatchIn(
                platform="shipinhao",
                url_or_id=watch_url,
                account_id=self.account_id,
                kind="auto",
            )
        )
        self.assertEqual(watch_created["platform"], "shipinhao")
        self.assertEqual(watch_created["kind"], "video")
        self.assertEqual(watch_created["aweme_id"], "15048048986165157126")

        engine = MonitorEngine(
            self.cfg,
            SimpleNamespace(
                anon_identity=lambda: SimpleNamespace(ua="Mozilla/5.0", proxy=""),
                identity_for=lambda _acc: SimpleNamespace(ua="Mozilla/5.0", proxy=""),
                lock_for=lambda _key: asyncio.Lock(),
            ),
        )
        engine.downloader = downloader
        with (
            patch.object(
                ChannelsProtocolClient,
                "fetch_user_feeds",
                new=AsyncMock(return_value=([feed_item], {"nickname": "户外达人", "headUrl": "https://img/a.jpg"}, "")),
            ),
            patch.object(
                ChannelsProtocolClient,
                "collect_comments",
                new=AsyncMock(return_value=(proto_comments, {"collected_total": 2}, "")),
            ),
            patch.object(ChannelsProtocolClient, "close", new=AsyncMock()),
            patch("app.engine.downloader.httpx.AsyncClient", side_effect=_patched_dl_client),
        ):
            scan_res = await engine._scan_channels_target_locked(mon_created["id"])
            watch_res = await engine._scan_comment_watch_locked(watch_created["id"])

        self.assertTrue(scan_res["ok"], scan_res)
        self.assertEqual(scan_res["new"], 1)
        self.assertTrue(watch_res["ok"], watch_res)
        self.assertEqual(watch_res["new_comments"], 2)

        with db.get_session() as s:
            rec = s.exec(
                select(ContentRecord).where(ContentRecord.target_id == mon_created["id"])
            ).one()
            self.assertEqual(rec.download_status, "done")
            self.assertEqual(Path(rec.local_path).read_bytes(), plain_mp4)
            rebuilt = engine._rebuild_aweme(rec, "户外达人")
            self.assertEqual(rebuilt.medias[0].decode_key, "1707439823")

            saved_comments = s.exec(
                select(CommentRecord).where(CommentRecord.watch_id == watch_created["id"])
            ).all()
            self.assertEqual(len(saved_comments), 2)

        status_payload = await main_mod.channels_protocol_status(probe=False)
        self.assertTrue(status_payload["configured"])
        self.assertTrue(status_payload["capabilities"]["keyword_search"])

        # 无创作者账号(account_id=0)下仍可执行本机微信关键词采集 + sph: 短链评论监控
        no_acc_job = await main_mod.create_keyword_collection(
            main_mod.KeywordCollectionIn(
                platform="shipinhao",
                account_id=None,
                source_type="keyword",
                keywords=["无人机"],
                max_contents_per_keyword=3,
                max_comments_per_content=0,
                download_media=False,
            )
        )
        self.assertEqual(no_acc_job["account_id"], 0)
        with (
            patch.object(
                ChannelsProtocolClient,
                "search_feeds",
                new=AsyncMock(return_value=([feed_item], "")),
            ),
            patch.object(ChannelsProtocolClient, "close", new=AsyncMock()),
        ):
            run_res = await engine.run_collection_job(no_acc_job["id"])
        self.assertTrue(run_res["ok"], run_res)
        self.assertEqual(run_res["status"], "done")

        with db.get_session() as s:
            sph_watch = CommentWatch(
                platform="shipinhao",
                kind="video",
                aweme_id="sph:Amv88hJDUj",
                sec_uid="",
                xsec_token="",
                mode="public",
                account_id=None,
                title="短链作品",
            )
            s.add(sph_watch)
            s.commit()
            s.refresh(sph_watch)
            sph_watch_id = sph_watch.id

        detail_calls: list[dict] = []

        async def _fake_fetch_detail(**kwargs):
            detail_calls.append(kwargs)
            return {
                "id": "15048048986165157126",
                "objectNonceId": "nonce_sph_1",
                "objectDesc": {"description": "短链详情标题"},
            }, ""

        with (
            patch.object(ChannelsProtocolClient, "fetch_feed_detail", side_effect=_fake_fetch_detail),
            patch.object(
                ChannelsProtocolClient,
                "collect_comments",
                new=AsyncMock(return_value=(proto_comments, {"collected_total": 2}, "")),
            ),
            patch.object(ChannelsProtocolClient, "close", new=AsyncMock()),
        ):
            sph_res = await engine._scan_comment_watch_locked(sph_watch_id)
        self.assertTrue(sph_res["ok"], sph_res)
        self.assertEqual(detail_calls[0]["object_id"], "sph:Amv88hJDUj")
        self.assertEqual(detail_calls[0]["export_id"], "")

    async def test_search_highlight_tags_nested_counts_and_zero_placeholder_comments(self):
        # 1) 搜索结果中的 <em class="highlight"> 标签清洗 + objectExtend.monotonicData.countInfo 统计提取
        raw_search_feed = {
            "id": "15056754843467647603",
            "objectNonceId": "12443322076426719460_0_140_4_64_0_test",
            "createtime": int(time.time()),
            "likeCount": 0,
            "commentCount": 0,
            "contact": {
                "username": "v2_gold@finder",
                "nickname": '<em class="highlight">黄金</em>旗舰店 &amp; 珠宝',
            },
            "objectExtend": {
                "monotonicData": {
                    "countInfo": {"likeCount": 88, "commentCount": 14, "favCount": 5},
                }
            },
            "objectDesc": {
                "description": '<em class="highlight">黄金</em>福利拉满，&lt;10秒&gt;低价买<em class="highlight">黄金</em>',
                "mediaType": 4,
                "media": [],
            },
        }
        # Note: `<10秒>` intentionally stays intact unless it is an HTML tag; verify highlight stripped
        aw = parse_channels_feed(raw_search_feed)
        self.assertIsNotNone(aw)
        self.assertEqual(aw.author_name, "黄金旗舰店 & 珠宝")
        self.assertEqual(aw.desc, "黄金福利拉满，<10秒>低价买黄金")
        self.assertEqual(aw.like_count, 88)
        self.assertEqual(aw.comment_count, 14)

        # 2) fetch_feed_detail 在 object.commentCount == 0 时回填 wrapper data.commentCount + monotonicData
        proto = ChannelsProtocolClient(endpoint="http://127.0.0.1:9222")

        async def _detail_eval(method: str, payload: dict) -> dict:
            self.assertEqual(method, "fetchFeedDetail")
            return {
                "errCode": 0,
                "data": {
                    "BaseResponse": {"Ret": 0},
                    "commentCount": 9,
                    "object": {
                        "id": "15056754843467647603",
                        "objectNonceId": "nonce_det",
                        "commentCount": 0,
                        "objectDesc": {"description": "详情标题", "mediaType": 4, "media": []},
                    },
                },
            }

        with patch.object(proto, "_eval_call", side_effect=_detail_eval):
            det_obj, det_err = await proto.fetch_feed_detail(object_id="15056754843467647603")
        self.assertEqual(det_err, "")
        self.assertEqual(parse_channels_feed(det_obj).comment_count, 9)

        # 3) safe_response 从 monotonicData.countInfo 保留白名单统计计数
        sanitized = safe_response(
            {
                "errCode": 0,
                "data": {
                    "BaseResponse": {"Ret": 0},
                    "commentInfo": [],
                    "monotonicData": {
                        "countInfo": {"likeCount": 52, "commentCount": 7, "secretToken": "drop-me"}
                    },
                },
            }
        )
        self.assertEqual(sanitized["data"]["countInfo"], {"commentCount": 7, "likeCount": 52})

        # 4) 即使 finderSearch 返回 explicit likeCount=0 / commentCount=0，接入本机微信协议时仍抓取评论并回填计数
        placeholder_search_item = {
            "id": "15056672651047537264",
            "objectNonceId": "nonce_zero_search",
            "createtime": int(time.time()),
            "likeCount": 0,
            "commentCount": 0,
            "contact": {"username": "v2_gold_2@finder", "nickname": "中国黄金店"},
            "objectDesc": {
                "description": '捡漏<em class="highlight">黄金</em>好时机',
                "mediaType": 4,
                "media": [{"mediaType": 4, "thumbUrl": "https://finder.video.qq.com/t.jpg"}],
            },
        }
        returned_comments = [
            {
                "comment_id": "901",
                "text": "这个克价多少",
                "user_nickname": "买家",
                "user_sec_uid": "v2_buyer@finder",
                "ip_label": "广东",
                "like_count": 3,
                "create_time": int(time.time()),
                "reply_to": "",
            }
        ]
        audit_payload = {
            "main_pagination": {
                "pages": [{"count_info": {"likeCount": 64, "commentCount": 5}}]
            }
        }
        with db.get_session() as s:
            job = KeywordCollectionJob(
                platform="shipinhao",
                account_id=0,
                source_type="keyword",
                keywords='["黄金"]',
                max_contents_per_keyword=3,
                max_pages_per_keyword=1,
                max_comments_per_content=5,
                include_replies=True,
                download_media=False,
            )
            s.add(job)
            s.commit()
            s.refresh(job)
            gold_job_id = job.id

        browser = SimpleNamespace(
            identity_for=lambda _acc: SimpleNamespace(ua="Mozilla/5.0"),
            anon_identity=lambda: SimpleNamespace(ua="Mozilla/5.0"),
        )
        collector = KeywordCollector(
            self.cfg,
            browser,
            Downloader(self.cfg.engine.media_dir, "Mozilla/5.0", timeout=20.0),
        )
        with (
            patch.object(
                ChannelsProtocolClient,
                "search_feeds",
                new=AsyncMock(return_value=([placeholder_search_item], "")),
            ),
            patch.object(
                ChannelsProtocolClient,
                "collect_comments",
                new=AsyncMock(return_value=(returned_comments, audit_payload, "")),
            ) as mock_collect_comments,
            patch.object(ChannelsProtocolClient, "close", new=AsyncMock()),
            patch("app.engine.collection.asyncio.sleep", new=AsyncMock()),
        ):
            res = await collector.run(gold_job_id, None)

        self.assertEqual(res["contents"], 1)
        self.assertEqual(res["comments"], 1)
        mock_collect_comments.assert_awaited_once()
        with db.get_session() as s:
            saved_content = s.exec(
                select(KeywordCollectionContent).where(KeywordCollectionContent.job_id == gold_job_id)
            ).one()
            self.assertEqual(saved_content.desc, "捡漏黄金好时机")
            self.assertEqual(saved_content.like_count, 64)
            self.assertEqual(saved_content.comment_count, 5)
            saved_cmts = s.exec(
                select(KeywordCollectionComment).where(KeywordCollectionComment.job_id == gold_job_id)
            ).all()
            self.assertEqual(len(saved_cmts), 1)
            self.assertEqual(saved_cmts[0].text, "这个克价多少")

    async def test_search_feeds_backfills_from_matched_contacts_and_bubbles_commented_works(self):
        client = ChannelsProtocolClient(endpoint="http://127.0.0.1:9222")
        search_calls: list[str] = []
        user_page_calls: list[str] = []

        async def _eval_call(method: str, payload: dict) -> dict:
            if method == "search":
                cursor = str(payload.get("lastBuff") or "")
                search_calls.append(cursor)
                if cursor == "":
                    return {
                        "errCode": 0,
                        "data": {
                            "BaseResponse": {"Ret": 0},
                            "continueFlag": 1,
                            "lastBuff": "cur_1",
                            "objectList": [
                                {
                                    "id": "1000",
                                    "objectNonceId": "n_1000",
                                    "likeCount": 0,
                                    "commentCount": 0,
                                    "contact": {"username": "v2_shop@finder", "nickname": "网咖店"},
                                    "objectDesc": {"description": "门店开业", "mediaType": 4, "media": []},
                                }
                            ],
                            "infoList": [
                                {"contact": {"username": "v2_gpu_a@finder", "nickname": "显卡测评A"}},
                                {"contact": {"username": "v2_gpu_b@finder", "nickname": "显卡装机B"}},
                            ],
                        },
                    }
                # 后续页仅有游标翻页但无新 objectList（验证停滞 2 页即止步，不空转 12 页）
                return {
                    "errCode": 0,
                    "data": {
                        "BaseResponse": {"Ret": 0},
                        "continueFlag": 1,
                        "lastBuff": f"cur_{len(search_calls)}",
                        "objectList": [],
                        "infoList": [],
                    },
                }

            if method == "fetchUserPage":
                uname = str(payload.get("username") or "")
                user_page_calls.append(uname)
                if uname == "v2_gpu_a@finder":
                    return {
                        "errCode": 0,
                        "data": {
                            "BaseResponse": {"Ret": 0},
                            "contact": {"username": uname, "nickname": "显卡测评A"},
                            "continueFlag": 0,
                            "object": [
                                {
                                    "id": "2001",
                                    "objectNonceId": "n_2001",
                                    "objectExtend": {"monotonicData": {"countInfo": {"likeCount": 10, "commentCount": 0}}},
                                    "objectDesc": {"description": "日常vlog", "mediaType": 4, "media": []},
                                },
                                {
                                    "id": "2002",
                                    "objectNonceId": "n_2002",
                                    "objectExtend": {"monotonicData": {"countInfo": {"likeCount": 210, "commentCount": 18}}},
                                    "objectDesc": {"description": "RTX 显卡实测横评", "mediaType": 4, "media": []},
                                },
                            ],
                        },
                    }
                if uname == "v2_gpu_b@finder":
                    return {
                        "errCode": 0,
                        "data": {
                            "BaseResponse": {"Ret": 0},
                            "contact": {"username": uname, "nickname": "显卡装机B"},
                            "continueFlag": 0,
                            "object": [
                                {
                                    "id": "3001",
                                    "objectNonceId": "n_3001",
                                    "objectExtend": {"monotonicData": {"countInfo": {"likeCount": 95, "commentCount": 7}}},
                                    "objectDesc": {"description": "2026 显卡性价比清单", "mediaType": 4, "media": []},
                                }
                            ],
                        },
                    }
            raise AssertionError(f"Unexpected method: {method}")

        client._eval_call = AsyncMock(side_effect=_eval_call)
        with patch("app.platforms.channels.client.asyncio.sleep", new=AsyncMock()):
            items, err = await client.search_feeds("显卡", max_results=5, max_pages=12)

        self.assertEqual(err, "")
        self.assertEqual(len(search_calls), 3)  # page 0 + 2 stagnant pages
        self.assertEqual(set(user_page_calls), {"v2_gpu_a@finder", "v2_gpu_b@finder"})
        self.assertEqual([x["id"] for x in items], ["1000", "2002", "3001", "2001"])

        # 在 KeywordCollector 中验证：有评论作品优先于 0 互动占位符，且空评论会刷新 nonce 重试一次
        with db.get_session() as s:
            job = KeywordCollectionJob(
                platform="shipinhao",
                account_id=0,
                source_type="keyword",
                keywords='["显卡"]',
                max_contents_per_keyword=2,
                max_pages_per_keyword=12,
                max_comments_per_content=5,
                include_replies=True,
                download_media=False,
            )
            s.add(job)
            s.commit()
            s.refresh(job)
            gpu_job_id = job.id

        browser = SimpleNamespace(
            identity_for=lambda _acc: SimpleNamespace(ua="Mozilla/5.0"),
            anon_identity=lambda: SimpleNamespace(ua="Mozilla/5.0"),
        )
        collector = KeywordCollector(
            self.cfg,
            browser,
            Downloader(self.cfg.engine.media_dir, "Mozilla/5.0", timeout=20.0),
        )
        nonce_history: list[tuple[str, str]] = []

        async def _fake_collect(oid: str, nid: str = "", **kwargs):
            nonce_history.append((oid, nid))
            if oid == "2002" and nid == "n_2002":
                return [], {}, ""
            if oid == "2002" and nid == "n_2002_refreshed":
                return (
                    [{
                        "comment_id": "c1",
                        "text": "这张卡噪音如何",
                        "user_nickname": "网友A",
                        "user_sec_uid": "u1",
                        "like_count": 2,
                        "create_time": int(time.time()),
                        "reply_to": "",
                    }],
                    {"main_pagination": {"pages": [{"count_info": {"likeCount": 210, "commentCount": 18}}]}},
                    "",
                )
            return (
                [{
                    "comment_id": "c2",
                    "text": "求配置单",
                    "user_nickname": "网友B",
                    "user_sec_uid": "u2",
                    "like_count": 1,
                    "create_time": int(time.time()),
                    "reply_to": "",
                }],
                {"main_pagination": {"pages": [{"count_info": {"likeCount": 95, "commentCount": 7}}]}},
                "",
            )

        with (
            patch.object(ChannelsProtocolClient, "search_feeds", new=AsyncMock(return_value=(items, ""))),
            patch.object(
                ChannelsProtocolClient,
                "fetch_feed_detail",
                new=AsyncMock(return_value=({"id": "2002", "objectNonceId": "n_2002_refreshed"}, "")),
            ),
            patch.object(ChannelsProtocolClient, "collect_comments", side_effect=_fake_collect),
            patch.object(ChannelsProtocolClient, "close", new=AsyncMock()),
            patch("app.engine.collection.asyncio.sleep", new=AsyncMock()),
        ):
            res = await collector.run(gpu_job_id, None)

        self.assertEqual(res["contents"], 2)
        self.assertEqual(res["comments"], 2)
        self.assertIn(("2002", "n_2002"), nonce_history)
        self.assertIn(("2002", "n_2002_refreshed"), nonce_history)
        with db.get_session() as s:
            rows = s.exec(
                select(KeywordCollectionContent)
                .where(KeywordCollectionContent.job_id == gpu_job_id)
                .order_by(KeywordCollectionContent.id)
            ).all()
            self.assertEqual([r.aweme_id for r in rows], ["2002", "3001"])

    async def test_search_feeds_prefer_engaged_backfills_when_direct_hits_have_zero_comments(self):
        client = ChannelsProtocolClient(endpoint="http://127.0.0.1:9222")

        async def _eval_call(method: str, payload: dict) -> dict:
            if method == "search":
                return {
                    "errCode": 0,
                    "data": {
                        "BaseResponse": {"Ret": 0},
                        "continueFlag": 0,
                        "objectList": [
                            {
                                "id": f"900{i}",
                                "objectNonceId": f"n_900{i}",
                                "likeCount": 0,
                                "commentCount": 0,
                                "contact": {"username": f"v2_live_{i}@finder", "nickname": "黄金直播间"},
                                "objectDesc": {"description": "黄金直播间开播啦", "mediaType": 4, "media": []},
                            }
                            for i in range(5)
                        ],
                        "infoList": [
                            {"contact": {"username": "v2_gold_creator@finder", "nickname": "黄金测评博主"}}
                        ],
                    },
                }
            if method == "fetchUserPage":
                return {
                    "errCode": 0,
                    "data": {
                        "BaseResponse": {"Ret": 0},
                        "contact": {"username": "v2_gold_creator@finder", "nickname": "黄金测评博主"},
                        "continueFlag": 0,
                        "object": [
                            {
                                "id": "9501",
                                "objectNonceId": "n_9501",
                                "objectExtend": {"monotonicData": {"countInfo": {"likeCount": 420, "commentCount": 36}}},
                                "objectDesc": {"description": "买黄金避坑指南", "mediaType": 4, "media": []},
                            },
                            {
                                "id": "9502",
                                "objectNonceId": "n_9502",
                                "objectExtend": {"monotonicData": {"countInfo": {"likeCount": 180, "commentCount": 12}}},
                                "objectDesc": {"description": "黄金工艺对比", "mediaType": 4, "media": []},
                            },
                        ],
                    },
                }
            raise AssertionError(f"Unexpected method: {method}")

        client._eval_call = AsyncMock(side_effect=_eval_call)
        with patch("app.platforms.channels.client.asyncio.sleep", new=AsyncMock()):
            res, err = await client.search_feeds("黄金", max_results=5, max_pages=3, prefer_engaged=True)

        self.assertEqual(err, "")
        self.assertEqual(len(res), 5)
        self.assertEqual([x["id"] for x in res[:2]], ["9501", "9502"])

    async def test_video_share_link_resolves_creator_across_resolve_collection_monitor_and_watch_apis(self):
        preview_no_user = {
            "export_id": "export/UzFfShareReverse",
            "author": "露营装备测评号",
            "author_username": "",
            "author_avatar": "https://img/camp.jpg",
        }
        detail_obj = {
            "id": "15048048986165157999",
            "contact": {
                "username": "v2_camp_creator_888@finder",
                "nickname": "露营装备测评号",
                "headUrl": "https://img/camp_hd.jpg",
            },
        }
        share_text = "来看看这个视频号作品！https://weixin.qq.com/sph/CampVideo01 推荐给你"

        with (
            patch(
                "app.platforms.channels.resolve.fetch_finder_preview_info",
                new=AsyncMock(return_value=preview_no_user),
            ),
            patch.object(
                ChannelsProtocolClient,
                "fetch_feed_detail",
                new=AsyncMock(return_value=(detail_obj, "")),
            ),
            patch.object(ChannelsProtocolClient, "close", new=AsyncMock()),
        ):
            resolved = await main_mod.resolve_channels_creator_endpoint(
                main_mod.ChannelsCreatorResolveIn(target=share_text)
            )
            self.assertTrue(resolved["ok"])
            self.assertEqual(resolved["username"], "v2_camp_creator_888@finder")
            self.assertEqual(resolved["nickname"], "露营装备测评号")

            job = await main_mod.create_keyword_collection(
                main_mod.KeywordCollectionIn(
                    platform="shipinhao",
                    source_type="creator",
                    source_ref=share_text,
                    source_name=f"创作者 {share_text}",
                    max_contents_per_keyword=10,
                    max_comments_per_content=0,
                    download_media=False,
                )
            )
            self.assertEqual(job["source_ref"], "v2_camp_creator_888@finder")
            self.assertIn("露营装备测评号", job["source_name"])
            self.assertIn("v2_camp_creator_888@finder", job["source_name"])

            mon = await main_mod.add_monitor(
                main_mod.TargetIn(
                    platform="shipinhao",
                    url_or_secuid=share_text,
                    account_id=self.account_id,
                )
            )
            self.assertEqual(mon["sec_uid"], "v2_camp_creator_888@finder")
            self.assertEqual(mon["nickname"], "露营装备测评号")
            self.assertEqual(mon["avatar"], "https://img/camp_hd.jpg")

            # kind="auto" keeps video watch semantics; kind="user" resolves creator
            watch_auto = await main_mod.add_watch(
                main_mod.WatchIn(
                    platform="shipinhao",
                    url_or_id=share_text,
                    kind="auto",
                )
            )
            self.assertEqual(watch_auto["kind"], "video")
            self.assertEqual(watch_auto["aweme_id"], "export/UzFfShareReverse")

            watch_user = await main_mod.add_watch(
                main_mod.WatchIn(
                    platform="shipinhao",
                    url_or_id=share_text,
                    kind="user",
                )
            )
            self.assertEqual(watch_user["kind"], "user")
            self.assertEqual(watch_user["sec_uid"], "v2_camp_creator_888@finder")
            self.assertEqual(watch_user["title"], "露营装备测评号")

        # When local WeChat channel is unconfigured, returns actionable 400 error
        self.cfg.engine.channels_cdp_endpoint = ""
        with patch(
            "app.platforms.channels.resolve.fetch_finder_preview_info",
            new=AsyncMock(return_value=preview_no_user),
        ):
            with self.assertRaises(main_mod.HTTPException) as ctx:
                await main_mod.resolve_channels_creator_endpoint(
                    main_mod.ChannelsCreatorResolveIn(target=share_text)
                )
            self.assertEqual(ctx.exception.status_code, 400)
            self.assertIn("视频号微信通道", str(ctx.exception.detail))

    async def test_channels_monitor_rescan_with_zero_known_and_pinned_feed_pagination(self):
        from datetime import datetime

        # 1) Pinned older feed on page 1 must not stop pagination while newer feeds exist on page 1;
        #    also covers objectList fallback when response uses objectList instead of object.
        client = ChannelsProtocolClient(endpoint="http://127.0.0.1:9222")
        eval_cursors: list[str] = []

        async def _eval_call(method: str, payload: dict) -> dict:
            assert method == "fetchUserPage"
            eval_cursors.append(str(payload.get("lastBuffer") or ""))
            if not payload.get("lastBuffer"):
                return {
                    "errCode": 0,
                    "data": {
                        "BaseResponse": {"Ret": 0},
                        "contact": {"username": "v2_pinned@finder", "nickname": "置顶博主"},
                        "continueFlag": 1,
                        "lastBuffer": "cursor_2",
                        "objectList": [
                            {"id": "101", "createtime": 1000, "objectDesc": {"description": "置顶旧帖"}},
                            {"id": "102", "createtime": 2500, "objectDesc": {"description": "最新作品"}},
                        ],
                    },
                }
            if payload.get("lastBuffer") == "cursor_2":
                return {
                    "errCode": 0,
                    "data": {
                        "BaseResponse": {"Ret": 0},
                        "continueFlag": 1,
                        "lastBuffer": "cursor_3",
                        "objectList": [
                            {"id": "103", "createtime": 1500, "objectDesc": {"description": "次页旧帖"}},
                        ],
                    },
                }
            raise AssertionError(f"Should have stopped before cursor {payload.get('lastBuffer')}")

        client._eval_call = AsyncMock(side_effect=_eval_call)
        with patch("app.platforms.channels.client.asyncio.sleep", new=AsyncMock()):
            items, contact, err = await client.fetch_user_feeds(
                "v2_pinned@finder", max_results=20, max_pages=5, stop_before=2000
            )
        self.assertEqual(err, "")
        self.assertEqual(contact["nickname"], "置顶博主")
        self.assertEqual(eval_cursors, ["", "cursor_2"])
        self.assertEqual([it["id"] for it in items], ["101", "102", "103"])

        # 2) MonitorTarget with account_id=None, last_scan_at already set from a 0-item scan,
        #    and initial_backfill_count=5 updated afterward should still backfill historical feeds
        #    (stop_before=0) without firing _notify_new.
        with db.get_session() as s:
            mon = MonitorTarget(
                platform="shipinhao",
                sec_uid="v2_backfill_recover@finder",
                nickname="老郑说AI前瞻",
                account_id=None,
                initial_backfill_count=5,
                download_enabled=False,
                last_scan_at=datetime.utcnow(),
            )
            s.add(mon)
            s.commit()
            s.refresh(mon)
            mon_id = mon.id

        engine = MonitorEngine(
            self.cfg,
            SimpleNamespace(
                anon_identity=lambda: SimpleNamespace(ua="Mozilla/5.0", proxy=""),
                identity_for=lambda _acc: SimpleNamespace(ua="Mozilla/5.0", proxy=""),
                lock_for=lambda _key: asyncio.Lock(),
            ),
        )
        old_ts = int(time.time()) - 7200
        hist_feed = {
            "id": "15048048986165999999",
            "objectNonceId": "nonce_hist_1",
            "createtime": old_ts,
            "likeCount": 12,
            "commentCount": 3,
            "contact": {"username": "v2_backfill_recover@finder", "nickname": "老郑说AI前瞻"},
            "objectDesc": {"description": "历史作品回填测试", "mediaType": 4, "media": []},
        }
        fetch_mock = AsyncMock(
            return_value=([hist_feed], {"nickname": "老郑说AI前瞻", "headUrl": "https://img/lz.jpg"}, "")
        )
        notify_mock = AsyncMock()
        with (
            patch.object(ChannelsProtocolClient, "fetch_user_feeds", new=fetch_mock),
            patch.object(ChannelsProtocolClient, "close", new=AsyncMock()),
            patch.object(engine, "_notify_new", new=notify_mock),
        ):
            res = await engine._scan_channels_target_locked(mon_id)

        self.assertTrue(res["ok"], res)
        self.assertEqual(res["scanned"], 1)
        self.assertEqual(res["new"], 1)
        self.assertEqual(res["backfill_count"], 5)
        self.assertEqual(fetch_mock.await_args.kwargs["stop_before"], 0)
        notify_mock.assert_not_awaited()
        with db.get_session() as s:
            saved = s.exec(select(ContentRecord).where(ContentRecord.target_id == mon_id)).all()
            self.assertEqual([r.aweme_id for r in saved], ["15048048986165999999"])
            refreshed = s.get(MonitorTarget, mon_id)
            self.assertEqual(refreshed.last_error, "")


if __name__ == "__main__":
    unittest.main()
