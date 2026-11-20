"""Offline unit and integration tests for Instagram ('instagram') platform capabilities."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlsplit

import httpx
import pytest

import app.db as db
import app.engine.downloader as downloader_mod
import app.main as main
from app.agent.tools import Scope as AgentScope
from app.config import Config
from app.engine.downloader import Downloader
from app.engine.monitor import MonitorEngine
from app.models import CommentTask
from app.platforms.instagram import (
    ANDROID_DEVICE_UAS,
    BASEAPI_URL,
    IOS_LEGACY_KEY_TEXT,
    MOBILE_APP_ID,
    RUPLOAD_PHOTO_URL,
    RUPLOAD_VIDEO_URL,
    WEB_APP_ID,
    HashtagRankTokenCache,
    IgHttpResponse,
    InstagramClient,
    InstagramSessionState,
    RankTokenCache,
    build_ig_caption,
    build_mobile_headers,
    build_web_headers,
    clean_ig_username,
    derive_device_id,
    flatten_ig_comments,
    full_mobile_user_agent,
    generate_upload_id,
    get_priority,
    ig_comment_breadcrumb,
    ig_configure_content,
    ig_legacy_session_id,
    ig_legacy_stream_buffer,
    ig_thumbnail_wait_ms,
    ig_video_duration_values,
    ios_signature,
    is_valid_android_device_ua,
    large_signed_content,
    looks_like_ig_work,
    media_id_to_shortcode,
    parse_android_device_ua,
    parse_ig_aweme,
    parse_ig_creator_work,
    parse_ig_dm_inbox,
    parse_ig_dm_messages,
    parse_ig_reels_chunked_response,
    password_service_envelope,
    resolve_ig_user_target,
    resolve_ig_work_ref,
    shortcode_to_media_id,
    signed_dict_content,
    totp_base32_decode_strict,
    totp_token_at,
    unsigned_content,
)
from app.profiles import DOMESTIC_PLATFORMS
from app.transport_matrix import resolve_transport
from test_project_optimizations import local_project


# ═══════════ 1. Protocol Primitives, Crypto, Encodings & Session State ═══════════


def test_totp_rfc_vector_and_strict_base32():
    # RFC 6238 SHA-1 secret "12345678901234567890" -> Base32 GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ -> 287082 at t=59
    rfc_secret = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"
    assert totp_base32_decode_strict(rfc_secret) == b"12345678901234567890"
    assert totp_token_at(rfc_secret, 59) == "287082"
    assert totp_token_at("GEZD GNBV GY3T QOJQ GEZD GNBV GY3T QOJQ", 59) == "287082"

    # Strict uppercase no-padding Base32 validation
    for invalid in ("INVALID1", "INVALID8", "INVALID9", "JBSWY3DP=", "gezdgnbv"):
        with pytest.raises(ValueError):
            totp_base32_decode_strict(invalid)


def test_utf16_surrogate_comment_breadcrumb_hmac():
    # Emoji '😀' (U+1F600) is 1 Python char, 4 UTF-8 bytes, and 2 UTF-16 code units!
    text = "A😀B"  # 1 + 2 + 1 = 4 UTF-16 code units
    ticks = 1700000000123 * 10000
    crumb = ig_comment_breadcrumb(text, 2, 950, 1, ticks)
    assert crumb.endswith("\n")
    sig_b64, data_b64 = crumb.rstrip("\n").split("\n")
    raw_data = base64.b64decode(data_b64).decode("utf-8")
    assert raw_data == "4 1900 1 1700000000123"
    expected_sig = base64.b64encode(
        hmac.new(b"iN4$aGr0m", raw_data.encode("ascii"), hashlib.sha256).digest()
    ).decode("ascii")
    assert sig_b64 == expected_sig


def test_device_identity_distinct_uuids_and_ua_parsing():
    st_a = InstagramSessionState(seed="account-1001")
    st_b = InstagramSessionState(seed="account-1002")

    distinct_fields = {
        st_a.unique_id,
        st_a.unique_id_ex,
        st_a.family_device_guid,
        st_a.pigeon_session_id,
        st_a.attribution_id,
        st_a.client_session_id,
    }
    assert len(distinct_fields) == 6
    assert st_a.unique_id != st_b.unique_id
    assert st_a.device_id == derive_device_id(st_a.unique_id)
    assert st_a.device_id.startswith("android-") and len(st_a.device_id) == 24

    for ua in ANDROID_DEVICE_UAS:
        assert is_valid_android_device_ua(ua)
    assert not is_valid_android_device_ua("invalid-ua")

    parsed_sam = parse_android_device_ua("28/9; 420dpi; 1080x2042; samsung; SM-G973F; beyond1; exynos9820; en_US")
    assert parsed_sam == {
        "manufacturer": "samsung",
        "model": "SM-G973F",
        "android_version": "28",
        "android_release": "9",
    }
    parsed_pix = parse_android_device_ua("28/9; 440dpi; 1080x2028; Google/google; Pixel 3; blueline; blueline; en_US")
    assert parsed_pix["model"] == "blueline"
    assert full_mobile_user_agent(ANDROID_DEVICE_UAS[0]).startswith("Instagram ")

    assert get_priority("https://i.instagram.com/api/v1/feed/timeline/") == "0"
    assert get_priority("https://i.instagram.com/api/v1/users/123/info/") == "3"
    assert get_priority("https://i.instagram.com/rupload_igvideo/abc") == "6, i"


def test_four_post_encodings_and_evidence_boundaries():
    # 1. signed_dict_content -> signed_body=SIGNATURE.<form_encoded_json> (spaces -> '+')
    s_body = signed_dict_content({"caption": "hello world", "flag": True}).decode("utf-8")
    assert s_body.startswith("signed_body=SIGNATURE.")
    assert "hello+world" in s_body
    decoded_json = json.loads(unquote(s_body.split("SIGNATURE.", 1)[1].replace("+", " ")))
    assert decoded_json == {"caption": "hello world", "flag": True}

    # 2. large_signed_content -> unescaped JSON concatenated after signed_body=SIGNATURE.
    raw_large = json.dumps({"big": "x " * 100}, separators=(",", ":"))
    l_body = large_signed_content(raw_large).decode("utf-8")
    assert l_body == "signed_body=SIGNATURE." + raw_large

    # 3. unsigned_content -> no SIGNATURE., capitalized bool True/False
    u_body = unsigned_content([("use_unified_inbox", True), ("btt_dual_send", False), ("text", "hi there")]).decode("utf-8")
    assert "SIGNATURE." not in u_body
    assert u_body == "use_unified_inbox=True&btt_dual_send=False&text=hi+there"

    # 4. ig_configure_content -> Uri.EscapeDataString (%20, no '+', no double-escape)
    raw_cfg = '{"caption":"hello world+100%","edits":{"crop_zoom":1.0}}'
    c_body = ig_configure_content(raw_cfg).decode("utf-8")
    assert c_body.startswith("signed_body=SIGNATURE.")
    escaped_part = c_body.split("SIGNATURE.", 1)[1]
    assert "hello%20world%2B100%25" in escaped_part
    assert "+" not in escaped_part
    assert unquote(escaped_part) == raw_cfg

    # iOS legacy signature uses 64-byte UTF-8 ASCII key text (not 32-byte hex decode)
    expected_ios_mac = hmac.new(IOS_LEGACY_KEY_TEXT.encode("utf-8"), b'{"a":1}', hashlib.sha256).hexdigest()
    assert ios_signature('{"a":1}') == expected_ios_mac

    # Outer Base64 password envelope helper
    env = password_service_envelope("myPass!", "A" * 30, "conn_k", "res_v")
    assert set(env) == {"ident", "connection", "res"}
    assert env["connection"] == "conn_k"


def test_video_duration_legacy_stream_and_thumbnail_helpers():
    durs = ig_video_duration_values(5.234)
    assert durs["rupload_upload_media_duration_ms"] == 5234
    durs_frac = ig_video_duration_values(5.2341)
    assert durs_frac["rupload_upload_media_duration_ms"] == 5235  # math.ceil
    assert durs_frac["metadata_DurationInMS"] == 5234  # int truncation

    assert ig_legacy_stream_buffer(8) == 2
    assert ig_legacy_stream_buffer(9) == 3

    sid = ig_legacy_session_id("upload123")
    assert sid.startswith("upload123-") and sid.split("-", 1)[1].isupper()
    assert len(generate_upload_id(use_new=False, time_ms=1700000000123)) == 13
    assert generate_upload_id(use_new=True, time_ms=1700000000123).isdigit()

    assert ig_thumbnail_wait_ms(1024) == 7000
    assert ig_thumbnail_wait_ms(10 * 1024 * 1024) == 20480


def test_dual_stack_headers_writeback_and_rank_token_caches():
    st = InstagramSessionState(seed="dual-stack")
    st.import_browser_cookies({
        "sessionid": "987654%3Asecret%3A27",
        "csrftoken": "csrf_tok_123",
        "ds_user_id": "987654",
        "rur": "EAG",
    })
    assert st.is_logged_in
    assert st.auth_ds_user_id == "987654"
    auth_cookie = st.authorization_cookie()
    assert auth_cookie is not None and auth_cookie.startswith("Bearer IGT:2:")

    mob_h = build_mobile_headers(st, f"{BASEAPI_URL}users/987654/info/")
    assert mob_h["Authorization"] == auth_cookie
    assert mob_h["X-Ig-App-Id"] == MOBILE_APP_ID
    assert "Cookie" not in mob_h

    web_h = build_web_headers(st, "https://www.instagram.com/api/v1/users/web_profile_info/?username=demo")
    assert "Cookie" in web_h and "sessionid=" in web_h["Cookie"]
    assert web_h["X-CSRFToken"] == "csrf_tok_123"
    assert web_h["X-Ig-App-Id"] == WEB_APP_ID
    assert "Authorization" not in web_h

    st.update_from_response_headers({
        "ig-set-authorization": "Bearer IGT:2:NEW_TOKEN",
        "x-ig-set-www-claim": "hmac.NEW_CLAIM",
        "ig-set-ig-u-rur": "RVA",
        "ig-set-ig-u-ds-user-id": "987654",
    })
    assert st.auth_bearer == "NEW_TOKEN"
    assert st.authorization_cookie() == "Bearer IGT:2:NEW_TOKEN"
    assert st.claims == "hmac.NEW_CLAIM"
    assert st.rur == "RVA"

    # RankTokenCache (>200 evicts 50) & HashtagRankTokenCache (>500 evicts 50)
    rc = RankTokenCache()
    t1 = rc.get("u1", "target1", None)
    assert rc.get("u1", "target1", "cursor_2") == t1
    for idx in range(205):
        rc.get("u1", f"t_{idx}", None)
    assert len(rc.tokens) <= 160

    hc = HashtagRankTokenCache()
    ht1 = hc.get("u1", "travel", None)
    assert hc.get("u1", "travel", "next_max") == ht1
    for idx in range(505):
        hc.get("u1", f"tag_{idx}", None)
    assert len(hc.tokens) <= 460


# ═══════════ 2. Shortcode <-> Media ID Bijection & URL Resolution ═══════════


@pytest.mark.parametrize(
    ("media_id", "shortcode"),
    [
        ("2053129211722681556", "Bx-LiPetUzU"),
        ("3141592653589793238", "CuZLd-iMknW"),
    ],
)
def test_shortcode_media_id_bijection(media_id, shortcode):
    computed_code = media_id_to_shortcode(f"{media_id}_987654")
    assert computed_code == shortcode
    assert shortcode_to_media_id(computed_code) == media_id


def test_work_and_user_resolution():
    ref = resolve_ig_work_ref("看看这条 https://www.instagram.com/reel/CuZLd-iMknW/?igsh=abc")
    assert ref is not None
    assert ref.shortcode == "CuZLd-iMknW"
    assert ref.kind == "reel"
    assert ref.media_id == "3141592653589793238"

    ref_story = resolve_ig_work_ref("https://www.instagram.com/stories/natgeo/3200000000000000001/")
    assert ref_story is not None and ref_story.kind == "story" and ref_story.media_id == "3200000000000000001"

    assert looks_like_ig_work("https://www.instagram.com/p/CuZLd-iMknW/")
    assert not looks_like_ig_work("https://www.instagram.com/natgeo/")

    assert clean_ig_username("https://www.instagram.com/natgeo/?hl=en") == "natgeo"
    assert clean_ig_username("@studio.visuals") == "studio.visuals"
    assert clean_ig_username("https://www.instagram.com/explore/") == ""
    assert resolve_ig_user_target("987654321") == "987654321"
    assert resolve_ig_user_target("https://www.instagram.com/natgeo/") == "natgeo"


# ═══════════ 3. Normalizers (Media, Carousel, NDJSON Reels, Comments, Direct v2) ═══════════


def test_extractors_carousel_reels_comments_and_dm():
    carousel_raw = {
        "pk": "3141592653589793238",
        "id": "3141592653589793238_100",
        "code": "CuZLd-iMknW",
        "media_type": 8,
        "taken_at": 1700000100,
        "like_count": 420,
        "comment_count": 19,
        "caption": {"text": "Carousel post #design"},
        "user": {"pk": "100", "username": "designer", "full_name": "Designer Studio"},
        "carousel_media": [
            {
                "pk": "1",
                "media_type": 1,
                "image_versions2": {"candidates": [{"url": "https://cdn.ig/img1.jpg", "width": 1080, "height": 1080}]},
            },
            {
                "pk": "2",
                "media_type": 2,
                "video_duration": 12.6,
                "image_versions2": {"candidates": [{"url": "https://cdn.ig/cov2.jpg", "width": 1080, "height": 1920}]},
                "video_versions": [{"url": "https://cdn.ig/vid2.mp4", "width": 1080, "height": 1920}],
            },
        ],
    }
    aw = parse_ig_aweme(carousel_raw)
    assert aw is not None
    assert aw.aweme_id == "CuZLd-iMknW"
    assert aw.media_type == "images"
    assert len(aw.medias) == 2
    assert aw.medias[0].kind == "image" and aw.medias[0].index == 0
    assert aw.medias[1].kind == "video" and aw.medias[1].index == 1

    cw = parse_ig_creator_work(carousel_raw)
    assert cw is not None and cw["item_id"] == "CuZLd-iMknW" and cw["like_count"] == 420

    # NDJSON chunked clips/user response
    ndjson_text = "\n".join([
        json.dumps({"items": [{"media": {"pk": "2053129211722681556", "code": "Bx-LiPetUzU", "media_type": 2, "play_count": 999, "video_versions": [{"url": "https://cdn.ig/r1.mp4"}]}}], "paging_info": {"max_id": "cur_1", "more_available": True}}),
        json.dumps({"items": [{"media": carousel_raw}], "paging_info": {"max_id": "cur_2", "more_available": False}}),
    ])
    clips_items, next_max, more = parse_ig_reels_chunked_response(ndjson_text)
    assert len(clips_items) == 2
    assert next_max == "cur_2" and more is False

    # Comments + child replies
    flat_c = flatten_ig_comments([
        {
            "pk": "c1",
            "text": "主评论",
            "created_at": 1700000200,
            "comment_like_count": 5,
            "user": {"pk": "u1", "username": "alice", "full_name": "Alice"},
            "preview_child_comments": [
                {
                    "pk": "c2",
                    "text": "子回复",
                    "created_at": 1700000250,
                    "parent_comment_id": "c1",
                    "user": {"pk": "u2", "username": "bob"},
                }
            ],
        }
    ], include_replies=True)
    assert [c["comment_id"] for c in flat_c] == ["c1", "c2"]
    assert flat_c[1]["reply_to"] == "c1"

    # Direct v2 microsecond timestamp normalization
    inbox_payload = {
        "seq_id": 88,
        "inbox": {
            "threads": [
                {
                    "thread_id": "3402823668417103009491281",
                    "thread_title": "Creator Peer",
                    "read_state": 1,
                    "users": [{"pk": "555", "username": "peer_ig", "full_name": "Creator Peer"}],
                    "items": [
                        {
                            "item_id": "m1",
                            "user_id": "555",
                            "item_type": "text",
                            "text": "Hello from IG!",
                            "timestamp": "1700000500123456",
                        }
                    ],
                }
            ]
        },
    }
    convs = parse_ig_dm_inbox(inbox_payload, self_uid="987654")
    assert len(convs) == 1
    assert convs[0]["conv_id"] == "3402823668417103009491281"
    assert convs[0]["peer_uid"] == "555"
    assert convs[0]["unread_count"] == 1
    assert convs[0]["last_time"] == 1700000500

    msgs = parse_ig_dm_messages({"thread": inbox_payload["inbox"]["threads"][0]}, self_uid="987654")
    assert len(msgs) == 1 and msgs[0]["create_time"] == 1700000500 and msgs[0]["direction"] == "in"


# ═══════════ 4. Dual-Stack InstagramClient Mock Transport ═══════════


def _logged_in_storage_state() -> Dict[str, Any]:
    return {
        "cookies": [
            {"name": "sessionid", "value": "987654%3AsecretToken%3A27", "domain": ".instagram.com"},
            {"name": "csrftoken", "value": "csrf_demo_token", "domain": ".instagram.com"},
            {"name": "ds_user_id", "value": "987654", "domain": ".instagram.com"},
            {"name": "mid", "value": "ZdemoMid123", "domain": ".instagram.com"},
        ]
    }


def test_client_560_retry_reads_comments_and_hashtag_sections():
    calls: List[Tuple[str, str, Dict[str, str], Optional[bytes]]] = []

    async def mock_transport(method: str, url: str, headers: Dict[str, str], body: Optional[bytes], _timeout: float) -> IgHttpResponse:
        calls.append((method, url, headers, body))
        # First call to users/987654/info/ returns HTTP 560 -> should retry once
        if "users/987654/info/" in url or "accounts/current_user/" in url:
            if sum(1 for _, u, _, _ in calls if ("users/987654/info/" in u or "accounts/current_user/" in u)) == 1:
                return IgHttpResponse(560, {}, "")
            return IgHttpResponse(
                200,
                {"ig-set-authorization": "Bearer IGT:2:REFRESHED_BEARER"},
                json.dumps({"status": "ok", "user": {"pk": "987654", "username": "ig_self", "full_name": "IG Self"}}),
            )
        if "clips/user/" in url:
            assert headers.get("X-Ig-Transfer-Encoding") == "chunked"
            ndjson = json.dumps({
                "items": [{"media": {"pk": "2053129211722681556", "code": "Bx-LiPetUzU", "media_type": 2, "video_versions": [{"url": "https://cdn.ig/1.mp4"}]}}],
                "paging_info": {"max_id": "", "more_available": False},
            })
            return IgHttpResponse(200, {}, ndjson)
        if "tags/photography/sections/" in url:
            form = parse_qs((body or b"").decode("utf-8"))
            if "max_id" not in form:
                return IgHttpResponse(
                    200,
                    {},
                    json.dumps({
                        "status": "ok",
                        "sections": [{"layout_content": {"medias": [{"media": {"pk": "111", "code": "C111", "media_type": 1}}]}}],
                        "next_max_id": "sec_cursor_2",
                        "next_page": 2,
                        "next_media_ids": ["111", "222"],
                        "more_available": True,
                    }),
                )
            assert form["next_media_ids"] == ["[111,222]"]
            return IgHttpResponse(
                200,
                {},
                json.dumps({"status": "ok", "sections": [], "more_available": False}),
            )
        if "stream_comments/" in url:
            q = parse_qs(urlsplit(url).query)
            assert "max_id" not in q
            if "min_id" not in q:
                # Real Instagram stream_comments returns multi-line NDJSON chunks!
                ndjson_stream = "\n".join([
                    json.dumps({
                        "comments": [
                            {
                                "pk": "c10",
                                "text": "Page 1 chunk A",
                                "created_at": 1700000001,
                                "child_comment_count": 1,
                                "preview_child_comments": [],
                                "user": {"pk": "1", "username": "u1"},
                            }
                        ],
                        "status": "ok",
                    }),
                    json.dumps({
                        "comments": [{"pk": "c10b", "text": "Page 1 chunk B", "created_at": 1700000001, "user": {"pk": "3", "username": "u3"}}],
                        "next_min_id": '{"server_cursor":"CUR2"}',
                        "status": "ok",
                    }),
                ])
                return IgHttpResponse(200, {}, ndjson_stream)
            return IgHttpResponse(
                200,
                {},
                json.dumps({
                    "status": "ok",
                    "comments": [{"pk": "c11", "text": "Page 2", "created_at": 1700000002, "user": {"pk": "2", "username": "u2"}}],
                    "next_min_id": "",
                }),
            )
        if "comments/c10/inline_child_comments/" in url:
            return IgHttpResponse(
                200,
                {},
                json.dumps({
                    "status": "ok",
                    "child_comments": [
                        {"pk": "c10_r1", "text": "Inline reply", "created_at": 1700000005, "user": {"pk": "9", "username": "u9"}}
                    ],
                }),
            )
        return IgHttpResponse(404, {}, json.dumps({"status": "fail", "message": "not found"}))

    async def scenario():
        cli = InstagramClient(_logged_in_storage_state(), transport=mock_transport)
        prof, err = await cli.fetch_self_profile()
        assert err == "" and prof["unique_id"] == "ig_self" and prof["nickname"] == "IG Self"
        assert cli.session.auth_bearer == "REFRESHED_BEARER"
        assert cli.session.authorization_cookie() == "Bearer IGT:2:REFRESHED_BEARER"

        reels, _, _, r_err = await cli.fetch_user_reels("987654")
        assert r_err == "" and len(reels) == 1 and reels[0]["code"] == "Bx-LiPetUzU"

        posts, s_err = await cli.search_posts("#photography", max_pages=2)
        assert s_err == "" and len(posts) == 1
        tag_calls = [b for _, u, _, b in calls if "tags/photography/sections/" in u]
        assert len(tag_calls) == 2
        rt1 = parse_qs(tag_calls[0].decode("utf-8"))["rank_token"][0]
        rt2 = parse_qs(tag_calls[1].decode("utf-8"))["rank_token"][0]
        assert rt1 == rt2

        cmts, c_err = await cli.fetch_comments("Bx-LiPetUzU", max_count=20)
        assert c_err == "" and [c["comment_id"] for c in cmts] == ["c10", "c10b", "c10_r1", "c11"]
        assert cmts[2]["reply_to"] == "c10"

    asyncio.run(scenario())


def test_client_writes_submit_guard_age_gate_dm_and_publishing():
    events: List[str] = []
    configure_video_attempts = 0
    video_upload_posts = 0
    captured_sidecar_json: Dict[str, Any] = {}

    async def mock_transport(method: str, url: str, headers: Dict[str, str], body: Optional[bytes], _timeout: float) -> IgHttpResponse:
        nonlocal configure_video_attempts, video_upload_posts, captured_sidecar_json
        events.append(f"{method}:{url}")

        # Bx-LiPetUzU -> media_id 2053129211722681556
        if "media/2053129211722681556/comment/" in url:
            raw_s = (body or b"").decode("utf-8").split("SIGNATURE.", 1)[1]
            data = json.loads(unquote(raw_s.replace("+", " ")))
            assert "\r" not in data["comment_text"]
            assert "\n" in data["user_breadcrumb"]
            if "TRIGGER_502" in data["comment_text"]:
                return IgHttpResponse(502, {}, "Bad Gateway")
            return IgHttpResponse(200, {}, json.dumps({"status": "ok", "comment": {"pk": "999001"}}))

        if "friendships/create/777/" in url:
            return IgHttpResponse(400, {}, json.dumps({"status": "fail", "age_gated_info": {"title": "Over 18"}}))
        if "friendships/mark_user_overage/777/follow/" in url:
            return IgHttpResponse(200, {}, json.dumps({"status": "ok", "friendship_status": {"following": True}}))
        if "friendships/destroy/888/" in url:
            return IgHttpResponse(404, {}, json.dumps({"status": "fail", "message": "User not found"}))

        if "direct_v2/threads/broadcast/text/" in url:
            form = parse_qs((body or b"").decode("utf-8"))
            assert "text" in form and "link_urls" not in form
            return IgHttpResponse(200, {}, json.dumps({"status": "ok", "payload": {"item_id": "dm_txt_1", "thread_id": "th_1"}}))
        if "direct_v2/threads/broadcast/link/" in url:
            form = parse_qs((body or b"").decode("utf-8"))
            assert "link_text" in form and json.loads(form["link_urls"][0]) == ["https://example.com/post"]
            return IgHttpResponse(200, {}, json.dumps({"status": "ok", "payload": {"item_id": "dm_lnk_1", "thread_id": "th_1"}}))
        if "/seen/" in url:
            raw_form = (body or b"").decode("utf-8")
            assert "use_unified_inbox=True" in raw_form
            return IgHttpResponse(200, {}, json.dumps({"status": "ok"}))

        if url.startswith(RUPLOAD_PHOTO_URL):
            assert headers["Offset"] == "0"
            p = json.loads(headers["X-Instagram-Rupload-Params"])
            return IgHttpResponse(200, {}, json.dumps({"status": "ok", "upload_id": p["upload_id"]}))
        if url.endswith("media/configure/"):
            raw = (body or b"").decode("utf-8")
            assert "+" not in raw.split("SIGNATURE.", 1)[1]
            return IgHttpResponse(200, {}, json.dumps({"status": "ok", "media": {"pk": "2053129211722681556", "code": "Bx-LiPetUzU"}}))

        if url.startswith(RUPLOAD_VIDEO_URL):
            if method == "GET":
                return IgHttpResponse(200, {}, json.dumps({"offset": 0}))
            video_upload_posts += 1
            assert headers["Offset"] == "0" and headers["Segment-Start-Offset"] == "0"
            return IgHttpResponse(200, {}, json.dumps({"status": "ok"}))
        if "media/configure_to_clips/?video=1" in url:
            configure_video_attempts += 1
            if configure_video_attempts == 1:
                return IgHttpResponse(202, {}, json.dumps({"status": "fail", "message": "Transcode not finished yet."}))
            return IgHttpResponse(200, {}, json.dumps({"status": "ok", "media": {"pk": "3141592653589793238", "code": "CuZLd-iMknW"}}))

        if "media/configure_sidecar/" in url:
            escaped = (body or b"").decode("utf-8").split("SIGNATURE.", 1)[1]
            captured_sidecar_json = json.loads(unquote(escaped))
            return IgHttpResponse(200, {}, json.dumps({"status": "ok", "media": {"pk": "3141592653589793238", "code": "CuZLd-iMknW"}}))

        return IgHttpResponse(404, {}, json.dumps({"status": "fail"}))

    async def no_sleep(_sec: float) -> None:
        return None

    async def scenario():
        cli = InstagramClient(_logged_in_storage_state(), transport=mock_transport)

        # 1. Comment success & write_uncertain on 502
        submitted = []
        ok_c, err_c, meta_c = await cli.post_comment("Bx-LiPetUzU", "Nice!\r\n😀", on_submit=lambda: submitted.append("c1"))
        assert ok_c and err_c == "" and meta_c["comment_id"] == "999001" and submitted == ["c1"]

        ok_u, err_u, _ = await cli.post_comment("Bx-LiPetUzU", "TRIGGER_502")
        assert not ok_u and err_u.startswith("write_uncertain:")

        # 2. Follow age-gate confirmation + Unfollow 404 idempotence
        ok_f, err_f = await cli.modify_follow("777", follow=True)
        assert ok_f and err_f == ""
        ok_uf, err_uf = await cli.modify_follow("888", follow=False)
        assert ok_uf and err_uf == ""

        # 3. Direct v2 text vs link + mark_dm_seen
        ok_dt, _, dm_t = await cli.send_dm("555", "Pure text message")
        assert ok_dt and dm_t["item_id"] == "dm_txt_1"
        ok_dl, _, dm_l = await cli.send_dm("555", "Check https://example.com/post now")
        assert ok_dl and dm_l["item_id"] == "dm_lnk_1"
        ok_seen, _ = await cli.mark_dm_seen("th_1", "dm_txt_1")
        assert ok_seen

        # 4. Photo two-stage upload + configure
        up_img, err_up = await cli.rupload_photo_bytes(b"\xff\xd8\xff\xe0jpegbytes", width=1080, height=1080)
        assert err_up == "" and up_img
        ok_p, url_p, err_p = await cli.configure_photo(up_img, width=1080, height=1080, caption="Photo #tag")
        assert ok_p and url_p == "https://www.instagram.com/p/Bx-LiPetUzU/" and err_p == ""

        # 5. Video single-POST upload + transcode_pending configure retry WITHOUT re-uploading bytes
        up_vid, err_vup = await cli.rupload_video_bytes(b"\x00\x00\x00\x18ftypmp42video", duration_sec=6.4, is_reel=True)
        assert err_vup == "" and up_vid
        assert video_upload_posts == 1
        ok_v, url_v, err_v = await cli.configure_video(
            up_vid, duration_sec=6.4, is_reel=True, caption="Reel test", transcode_sleep_fn=no_sleep
        )
        assert ok_v and url_v == "https://www.instagram.com/reel/CuZLd-iMknW/" and err_v == ""
        assert configure_video_attempts == 2
        assert video_upload_posts == 1  # Never re-uploaded!

        # 6. Sidecar configure: child metadata fields MUST be JSON strings & nav_chain is None
        ok_sc, url_sc, err_sc = await cli.configure_sidecar(
            [
                {"upload_id": up_img, "kind": "image", "width": 1080, "height": 1080},
                {"upload_id": up_vid, "kind": "video", "width": 720, "height": 1280, "duration_sec": 6.4},
            ],
            caption=build_ig_caption("Title", "Body", "art, reels"),
            transcode_sleep_fn=no_sleep,
        )
        assert ok_sc and url_sc == "https://www.instagram.com/p/CuZLd-iMknW/" and err_sc == ""
        ch0, ch1 = captured_sidecar_json["children_metadata"]
        assert isinstance(ch0["device"], str) and isinstance(ch0["edits"], str) and isinstance(ch0["extra"], str)
        assert ch0["nav_chain"] is None
        assert isinstance(ch1["clips"], str) and isinstance(ch1["edits"], str) and ch1["nav_chain"] is None
        assert "#art #reels" in captured_sidecar_json["caption"]

    asyncio.run(scenario())


# ═══════════ 5. System Integration (Downloader, Matrix, FastAPI & MonitorEngine) ═══════════


def test_instagram_system_routes_and_write_uncertainty(local_project, monkeypatch):
    assert AgentScope(platform="instagram").platform == "instagram"
    assert "instagram" not in DOMESTIC_PLATFORMS

    cfg = Config()
    assert cfg.engine.instagram_read_mode == "hybrid"
    assert cfg.engine.instagram_write_mode == "hybrid"
    route_pub = resolve_transport(cfg, "instagram", "publish")
    assert route_pub["effective_mode"] == "hybrid" and route_pub["api"] and route_pub["browser"]
    cfg.engine.instagram_read_mode = "api"
    route_api = resolve_transport(cfg, "instagram", "public_monitor")
    assert route_api["effective_mode"] == "api" and not route_api["opens_browser"]

    # Carousel multi-media downloader test (indexed filenames inside author folder + instagram referer)
    dl = Downloader(str(local_project.root / "media"), "TestUA/1.0")
    captured_headers: Dict[str, str] = {}
    real_async_client = httpx.AsyncClient

    def mock_client_factory(**kwargs):
        if "transport" not in kwargs:
            kwargs.pop("proxy", None)
            kwargs["transport"] = httpx.MockTransport(
                lambda req: (
                    captured_headers.update(dict(req.headers)),
                    httpx.Response(200, content=b"\xff\xd8\xff\xe0imgdata"),
                )[1]
            )
        return real_async_client(**kwargs)

    monkeypatch.setattr(downloader_mod.httpx, "AsyncClient", mock_client_factory)
    carousel_aw = parse_ig_aweme({
        "pk": "3141592653589793238",
        "code": "CuZLd-iMknW",
        "media_type": 8,
        "caption": {"text": "Carousel DL"},
        "user": {"pk": "100", "username": "studio"},
        "carousel_media": [
            {"pk": "1", "media_type": 1, "image_versions2": {"candidates": [{"url": "https://scontent.cdninstagram.com/1.jpg"}]}},
            {"pk": "2", "media_type": 1, "image_versions2": {"candidates": [{"url": "https://scontent.cdninstagram.com/2.jpg"}]}},
        ],
    })
    assert carousel_aw is not None and len(carousel_aw.medias) == 2
    ok_dl, saved_dir, err_dl = asyncio.run(dl.download_aweme(carousel_aw))
    assert ok_dl and err_dl == ""
    saved_files = sorted(p.name for p in Path(saved_dir).iterdir() if p.is_file())
    assert len(saved_files) == 2
    assert saved_files[0].endswith("_0.jpeg") and saved_files[1].endswith("_1.jpeg")
    assert captured_headers.get("referer") == "https://www.instagram.com/"

    # FastAPI & MonitorEngine end-to-end
    local_project.cfg.risk_control.operation_gap_min_seconds = 0
    local_project.cfg.risk_control.operation_gap_max_seconds = 0
    local_project.cfg.risk_control.read_light_gap_seconds = 0
    local_project.cfg.risk_control.read_heavy_gap_seconds = 0
    local_project.cfg.engine.native_write_gate_enabled = False
    engine = MonitorEngine(local_project.cfg, local_project.browser)
    monkeypatch.setattr(main, "engine", engine)

    from PIL import Image

    img_a = local_project.root / "slide1.jpg"
    img_b = local_project.root / "slide2.jpg"
    Image.new("RGB", (8, 8), (220, 40, 100)).save(img_a, format="JPEG")
    Image.new("RGB", (8, 8), (40, 100, 220)).save(img_b, format="JPEG")
    monkeypatch.setattr(main, "UPLOAD_DIR", local_project.root)

    async def _fake_self_profile(_self):
        return {
            "uid": "987654",
            "unique_id": "studio_ig",
            "douyin_id": "studio_ig",
            "nickname": "Studio IG",
            "follower_count": 1200,
            "aweme_count": 15,
        }, ""

    monkeypatch.setattr(InstagramClient, "fetch_self_profile", _fake_self_profile)

    async def call_api():
        transport = httpx.ASGITransport(app=main.app, client=("127.0.0.1", 1234), raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            # 1. Cookie login imports .instagram.com cookies + refresh-profile enriches profile
            ck_res = await client.post(
                "/api/login/cookie",
                json={
                    "platform": "instagram",
                    "nickname": "IG 初始名",
                    "cookie": "sessionid=987654%3AdemoSecret%3A27; csrftoken=csrf_123; ds_user_id=987654",
                },
            )
            assert ck_res.status_code == 200, ck_res.text
            account_id = ck_res.json()["account_id"]

            ref_res = await client.post(f"/api/accounts/{account_id}/refresh-profile")
            assert ref_res.status_code == 200, ref_res.text
            assert ref_res.json()["nickname"] == "Studio IG"
            assert ref_res.json()["douyin_id"] == "studio_ig"

            summary = await client.get("/api/overview/summary?platform=instagram")
            assert summary.status_code == 200 and summary.json()["accounts"] == 1

            # 1b. Share link inspect & full download (with thumbnail + metadata JSON)
            async def _fake_media_info(_self, _url):
                aw_reel = parse_ig_aweme({
                    "pk": "3141592653589793238",
                    "code": "DdL0sPNt5jn",
                    "media_type": 2,
                    "product_type": "clips",
                    "video_duration": 100.0,
                    "like_count": 312000,
                    "caption": {"text": "Custom solid 18K gold jewelry #18kgold"},
                    "user": {"pk": "200", "username": "lumiere_jewelry", "full_name": "Lumiere Jewelry"},
                    "image_versions2": {"candidates": [{"url": "https://scontent.cdninstagram.com/cover.jpg", "width": 1080, "height": 1920}]},
                    "video_versions": [{"url": "https://scontent.cdninstagram.com/reel.mp4", "width": 1080, "height": 1920}],
                })
                return aw_reel, {"code": "DdL0sPNt5jn"}, ""

            monkeypatch.setattr(InstagramClient, "fetch_media_info", _fake_media_info)
            monkeypatch.setattr(main, "httpx", downloader_mod.httpx, raising=False)

            insp_res = await client.post(
                "/api/share-download",
                json={
                    "share_text": "https://www.instagram.com/reel/DdL0sPNt5jn/",
                    "account_id": account_id,
                    "download": False,
                },
            )
            assert insp_res.status_code == 200, insp_res.text
            assert insp_res.json()["ok"] is True
            assert insp_res.json()["results"][0]["metadata"]["uploader"] == "Lumiere Jewelry"

            dl_res = await client.post(
                "/api/share-download",
                json={
                    "share_text": "https://www.instagram.com/reel/DdL0sPNt5jn/",
                    "account_id": account_id,
                    "download": True,
                    "save_thumbnail": True,
                    "save_metadata": True,
                    "output_dir": str(local_project.root / "share_out"),
                },
            )
            assert dl_res.status_code == 200, dl_res.text
            assert dl_res.json()["ok"] is True
            dl_item = dl_res.json()["results"][0]
            roles = {f["role"] for f in dl_item["files"]}
            assert "media" in roles and "thumbnail" in roles and "metadata" in roles

            hist_res = await client.get("/api/share-download/history?platform=instagram")
            assert hist_res.status_code == 200 and len(hist_res.json()) == 1

            matrix = await client.get("/api/settings/transport-matrix")
            assert matrix.status_code == 200
            ig_ops = {r["operation"] for r in matrix.json()["rows"] if r["platform"] == "instagram"}
            assert {"public_monitor", "account_works", "dm_sync", "comment_write", "dm_send", "publish"}.issubset(ig_ops)

            # 2. Keyword/Hashtag collection job
            col = await client.post(
                "/api/collections",
                json={
                    "platform": "instagram",
                    "source_type": "keyword",
                    "keywords": ["#visualart"],
                    "account_id": account_id,
                    "max_contents_per_keyword": 5,
                },
            )
            assert col.status_code == 200, col.text
            col_id = col.json()["id"]
            await client.post(f"/api/collections/{col_id}/cancel")

            # 3. Creator monitor + run-now
            async def _fake_resolve_user(_self, _ref):
                return "555000", "natgeo", "National Geographic", ""

            async def _fake_user_feed(_self, _uid, **_kw):
                return (
                    [
                        {
                            "pk": "2053129211722681556",
                            "code": "Bx-LiPetUzU",
                            "media_type": 1,
                            "taken_at": int(time.time()),
                            "caption": {"text": "New IG photo"},
                            "user": {"pk": "555000", "username": "natgeo", "full_name": "National Geographic"},
                            "image_versions2": {"candidates": [{"url": "https://cdn.ig/p1.jpg"}]},
                        }
                    ],
                    "",
                    False,
                    "",
                )

            monkeypatch.setattr(InstagramClient, "resolve_user_id", _fake_resolve_user)
            monkeypatch.setattr(InstagramClient, "fetch_user_feed", _fake_user_feed)
            mon = await client.post(
                "/api/monitors",
                json={
                    "platform": "instagram",
                    "url_or_secuid": "https://www.instagram.com/natgeo/",
                    "account_id": account_id,
                    "interval_seconds": 600,
                    "download_enabled": False,
                },
            )
            assert mon.status_code == 200, mon.text
            mon_id = mon.json()["id"]
            assert mon.json()["sec_uid"] == "natgeo"

            scan_res = await client.post(f"/api/monitors/{mon_id}/run-now")
            assert scan_res.status_code == 200, scan_res.text
            assert scan_res.json()["new"] == 1

            # 4. Comment watch + scan-now + thumb proxy SSRF guard
            async def _fake_watch_comments(_self, _work_id, **_kw):
                return [
                    {
                        "comment_id": "ig_cmt_1",
                        "text": "How much per gram?",
                        "user_nickname": "Buyer A",
                        "user_sec_uid": "buyer_a",
                        "ip_label": "",
                        "like_count": 12,
                        "create_time": int(time.time()),
                        "reply_to": "",
                    }
                ], ""

            monkeypatch.setattr(InstagramClient, "fetch_comments", _fake_watch_comments)
            cw = await client.post(
                "/api/comment-watches",
                json={
                    "platform": "instagram",
                    "url_or_id": "https://www.instagram.com/reel/DdL0sPNt5jn/",
                    "kind": "auto",
                    "account_id": account_id,
                    "interval_seconds": 600,
                },
            )
            assert cw.status_code == 200, cw.text
            cw_id = cw.json()["id"]
            assert cw.json()["aweme_id"] == "DdL0sPNt5jn"

            cw_scan = await client.post(f"/api/comment-watches/{cw_id}/scan-now")
            assert cw_scan.status_code == 200, cw_scan.text
            assert cw_scan.json()["new_comments"] == 1
            cw_rows = (await client.get("/api/comment-watches?platform=instagram")).json()
            assert "Lumiere Jewelry" in cw_rows[0]["title"]

            bad_thumb = await client.get("/api/ig-media-thumb?url=http://127.0.0.1:8000/secret")
            assert bad_thumb.status_code == 400
            ok_thumb = await client.get("/api/ig-media-thumb?url=https://scontent-lax3-2.cdninstagram.com/v/avatar.jpg")
            assert ok_thumb.status_code == 200 and ok_thumb.headers["content-type"].startswith("image/")

            # 5. Publish carousel validation (>10 rejected, 2 accepted with ig_options)
            too_many = await client.post(
                "/api/publish",
                json={
                    "account_id": account_id,
                    "media_type": "images",
                    "media_paths": [str(img_a)] * 11,
                    "title": "Too many",
                    "desc": "Too many",
                },
            )
            assert too_many.status_code in (400, 422)

            pub = await client.post(
                "/api/publish",
                json={
                    "account_id": account_id,
                    "media_type": "images",
                    "media_paths": [str(img_a), str(img_b)],
                    "title": "IG 轮播测试",
                    "desc": "正文",
                    "topics": "design,ig",
                    "ig_options": {"post_kind": "post", "share_to_feed": True, "first_comment": "首评测试"},
                },
            )
            assert pub.status_code == 200, pub.text
            pub_id = pub.json()["id"]
            pub_rows = (await client.get("/api/publish?platform=instagram")).json()
            assert len(pub_rows) == 1
            assert pub_rows[0]["ig_options"]["first_comment"] == "首评测试"

            # Native-identity Instagram account in hybrid mode MUST execute API publish first (not force browser!)
            api_pub_calls: List[str] = []
            first_cmt_calls: List[str] = []

            async def _fake_rupload_photo(_self, _b, **_kw):
                api_pub_calls.append("rupload_photo")
                return f"up_{len(api_pub_calls)}", ""

            async def _fake_cfg_sidecar(_self, _children, **kw):
                on_sub = kw.get("on_submit")
                if on_sub:
                    res = on_sub()
                    if asyncio.iscoroutine(res):
                        await res
                api_pub_calls.append("configure_sidecar")
                return True, "https://www.instagram.com/p/CuZLd-iMknW/", ""

            async def _fake_first_comment(_self, target_url, text, **_kw):
                first_cmt_calls.append(f"{target_url}|{text}")
                return True, "", {"comment_id": "fc_1"}

            monkeypatch.setattr(InstagramClient, "rupload_photo_bytes", _fake_rupload_photo)
            monkeypatch.setattr(InstagramClient, "configure_sidecar", _fake_cfg_sidecar)
            monkeypatch.setattr(InstagramClient, "post_comment", _fake_first_comment)

            pub_exec = await engine._publish_task_locked(pub_id)
            assert pub_exec["ok"] is True, pub_exec
            assert api_pub_calls == ["rupload_photo", "rupload_photo", "configure_sidecar"]
            assert first_cmt_calls == ["https://www.instagram.com/p/CuZLd-iMknW/|首评测试"]

            # 6. Hybrid comment task MUST mark uncertain and NEVER fallback to browser on write_uncertain:
            api_cmt_called = False
            browser_called = False

            async def _uncertain_api_comment(_self, *_a, **_kw):
                nonlocal api_cmt_called
                api_cmt_called = True
                on_sub = _kw.get("on_submit")
                if on_sub:
                    on_sub()
                return False, "write_uncertain:network timeout after submit", {}

            async def _forbidden_browser_fallback(*_a, **_kw):
                nonlocal browser_called
                browser_called = True
                return True, ""

            monkeypatch.setattr(InstagramClient, "post_comment", _uncertain_api_comment)
            import app.browser.ig_fetcher as ig_fetcher_mod
            monkeypatch.setattr(ig_fetcher_mod, "post_ig_comment", _forbidden_browser_fallback)
            engine.risk.policy.enabled = False

            with db.get_session() as s:
                ct = CommentTask(
                    platform="instagram",
                    account_id=account_id,
                    aweme_id="Bx-LiPetUzU",
                    content="Test uncertain guard",
                    status="pending",
                )
                s.add(ct)
                s.commit()
                s.refresh(ct)
                ct_id = ct.id

            await engine._execute_comment_task_locked(ct_id)
            assert api_cmt_called is True
            assert browser_called is False
            with db.get_session() as s:
                updated_ct = s.get(CommentTask, ct_id)
                assert updated_ct is not None
                assert updated_ct.status == "uncertain"

    asyncio.run(call_api())
