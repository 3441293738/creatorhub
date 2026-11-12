import json
import unittest
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, parse_qsl, urlencode, urlsplit

from app.platforms.douyin.client import DouyinClient
from app.platforms.douyin.signing import ABogus


class DouyinClientSearchTests(unittest.IsolatedAsyncioTestCase):
    async def test_resolve_visible_numeric_douyin_id_to_im_uid(self):
        client = DouyinClient("sid_tt=x", "Mozilla/5.0 Chrome/130.0.0.0")
        client._get_json = AsyncMock(return_value={
            "user_list": [{
                "user_info": {
                    "uid": "3928976331901290",
                    "sec_uid": "MS4wLjABtarget",
                    "unique_id": "66790575681",
                    "short_id": "",
                    "nickname": "HP惠普暗影精灵(直播版)",
                },
            }],
        })

        user, error = await client.resolve_user_identifier("66790575681")

        self.assertEqual(error, "")
        self.assertEqual(user, {
            "uid": "3928976331901290",
            "sec_uid": "MS4wLjABtarget",
            "unique_id": "66790575681",
            "short_id": "",
            "nickname": "HP惠普暗影精灵(直播版)",
        })
        params = client._get_json.await_args.args[1]
        self.assertEqual(params["search_channel"], "aweme_user_web")
        self.assertEqual(params["keyword"], "66790575681")

    async def test_resolve_douyin_id_requires_exact_visible_id_match(self):
        client = DouyinClient("sid_tt=x", "Mozilla/5.0 Chrome/130.0.0.0")
        client._get_json = AsyncMock(return_value={
            "user_list": [{
                "user_info": {
                    "uid": "66790575681",
                    "sec_uid": "MS4wLjABwrong",
                    "unique_id": "different-id",
                    "short_id": "12345",
                },
            }],
        })

        user, error = await client.resolve_user_identifier("66790575681")

        self.assertIsNone(user)
        self.assertEqual(error, "未找到完全匹配的抖音号")

    async def test_fetch_dm_conversations_posts_init_protocol_and_hydrates(self):
        client = DouyinClient("sid_tt=x", "Mozilla/5.0 Chrome/130.0.0.0")

        class Response:
            status_code = 200
            content = b"protobuf-response"

        fake_session = AsyncMock()
        fake_session.post = AsyncMock(return_value=Response())
        fake_session.close = AsyncMock()
        client.fetch_im_user_profiles = AsyncMock(return_value={
            "sec-peer": {"sec_uid": "sec-peer", "nickname": "对端", "avatar": "avatar"},
        })
        parsed = [{
            "conv_id": "0:1:self:peer", "conv_short_id": "42",
            "peer_uid": "peer", "peer_sec_uid": "sec-peer", "ticket": "ticket",
            "last_text": "hello", "last_time": 123, "last_msg_type": 7,
            "last_sender_uid": "peer", "self_uid": "self",
        }]

        with patch("app.platforms.douyin.client.AsyncSession", return_value=fake_session), \
                patch("app.browser.douyin_im_pb.build_init_request",
                      return_value=b"protobuf-request") as build, \
                patch("app.browser.douyin_im_pb.parse_send_response",
                      return_value={"ok": True, "cmd": 2043}), \
                patch("app.browser.douyin_im_pb.parse_conversations",
                      return_value=parsed):
            async with client.session_scope():
                rows = await client.fetch_dm_conversations()

        self.assertEqual(rows[0]["peer_nickname"], "对端")
        self.assertEqual(rows[0]["conv_short_id"], "42")
        self.assertGreater(build.call_args.args[0], 1_000_000_000_000_000)
        request = fake_session.post.await_args
        self.assertIn("get_message_by_init", request.args[0])
        self.assertEqual(request.kwargs["data"], b"protobuf-request")
        client.fetch_im_user_profiles.assert_awaited_once_with(["sec-peer"])

    async def test_fetch_self_profile_uses_current_cookie_endpoint(self):
        client = DouyinClient("sid_tt=x", "Mozilla/5.0 Chrome/130.0.0.0")
        client._get_json = AsyncMock(return_value={
            "status_code": 0,
            "user": {"sec_uid": "sec-self", "nickname": "本人"},
        })

        profile = await client.fetch_self_profile()

        self.assertEqual(profile["sec_uid"], "sec-self")
        client._get_json.assert_awaited_once_with(
            "/aweme/v1/web/user/profile/self/", {},
            referer="https://www.douyin.com/user/self")

    async def test_follower_list_resolves_internal_uid_before_paging(self):
        client = DouyinClient("sid_tt=x", "Mozilla/5.0 Chrome/130.0.0.0")
        client.fetch_profile = AsyncMock(return_value={"uid": "uid-self"})
        client._follow_page = AsyncMock(return_value={
            "followers": [{"uid": "fan-1"}], "has_more": 0,
        })

        with patch("app.platforms.douyin.client.time.time",
                   return_value=1_700_000_000):
            rows = await client.fetch_all_follows("", "sec-self", "fan")

        self.assertEqual([row["uid"] for row in rows], ["fan-1"])
        client.fetch_profile.assert_awaited_once_with("sec-self")
        client._follow_page.assert_awaited_once_with(
            "/aweme/v1/web/user/follower/list/", "uid-self", "sec-self",
            0, 1_700_000_000, 20, source_type=1)

    async def test_large_follow_list_streams_beyond_old_page_limit(self):
        client = DouyinClient("sid_tt=x", "Mozilla/5.0 Chrome/130.0.0.0")
        client.fetch_profile = AsyncMock(return_value={"uid": "uid-self"})
        client._follow_page = AsyncMock(side_effect=[
            {
                "followers": [{"uid": f"fan-{page}"}],
                "has_more": int(page < 29),
                "offset": page + 1,
                "max_time": 1_700_000_000 - page,
            }
            for page in range(30)
        ])
        received = []

        def on_page(rows, meta):
            received.extend(row["uid"] for row in rows)

        rows = await client.fetch_all_follows(
            "", "sec-self", "fan", max_pages=40, page_delay=0,
            on_page=on_page, collect=False)

        self.assertEqual(rows, [])
        self.assertEqual(len(received), 30)
        self.assertEqual(client._follow_page.await_count, 30)
        self.assertTrue(client.last_follow_meta["complete"])
        self.assertEqual(client.last_follow_meta["fetched"], 30)

    async def test_follow_page_limit_is_an_explicit_partial_failure(self):
        client = DouyinClient("sid_tt=x", "Mozilla/5.0 Chrome/130.0.0.0")
        client.fetch_profile = AsyncMock(return_value={"uid": "uid-self"})
        client._follow_page = AsyncMock(side_effect=[
            {"followers": [{"uid": f"fan-{page}"}], "has_more": 1,
             "offset": page + 1, "max_time": 1_700_000_000 - page}
            for page in range(3)
        ])

        rows = await client.fetch_all_follows(
            "", "sec-self", "fan", max_pages=3, page_delay=0)

        self.assertEqual(len(rows), 3)
        self.assertEqual(client.last_error, "page_limit:3")
        self.assertFalse(client.last_follow_meta["complete"])
        self.assertEqual(client.last_follow_meta["stop_reason"], "page_limit")

    async def test_current_abogus_wire_format_is_used(self):
        signature = ABogus(
            user_agent="Mozilla/5.0 Chrome/152.0.0.0",
            fp="1366|768|1390|848|0|0|0|0|1366|768|1366|768|1366|768|24|24|Win32",
        ).get_value("aid=6383&count=20")

        self.assertEqual(len(signature), 164)

    async def test_client_signer_uses_account_viewport_fingerprint(self):
        client = DouyinClient(
            "sid_tt=x", "Mozilla/5.0 Chrome/152.0.0.0",
            screen_width=1366, screen_height=768)

        query = parse_qs(urlsplit(client._build_url("/fixture", {})).query)

        self.assertEqual(len(query["a_bogus"][0]), 164)
        self.assertIn("1366|768|1390|848", client._abogus_fingerprint())

    async def test_direct_request_parameters_follow_account_environment(self):
        client = DouyinClient(
            "sid_tt=x", "Mozilla/5.0 Chrome/152.0.0.0",
            locale="zh-TW", accept_language="zh-TW,zh;q=0.8",
            screen_width=1440, screen_height=900)

        url = client._build_url("/fixture", {})
        headers = client._headers()

        self.assertIn("browser_language=zh-TW", url)
        self.assertIn("screen_width=1440", url)
        self.assertIn("screen_height=900", url)
        self.assertIn("browser_version=152.0.0.0", url)
        self.assertEqual(headers["Accept-Language"], "zh-TW,zh;q=0.8")

    async def test_stale_screen_cookies_do_not_override_account_viewport(self):
        state = json.dumps({
            "cookies": [
                {"name": "dy_swidth", "value": "800",
                 "domain": ".douyin.com", "path": "/"},
                {"name": "dy_sheight", "value": "600",
                 "domain": ".douyin.com", "path": "/"},
            ],
            "origins": [],
        })
        client = DouyinClient(
            "", "Mozilla/5.0 Chrome/152.0.0.0", storage_state=state,
            screen_width=1366, screen_height=768)

        self.assertEqual((client.screen_width, client.screen_height), (1366, 768))

    async def test_storage_state_identity_is_added_to_api_queries(self):
        state = json.dumps({
            "cookies": [{
                "name": "s_v_web_id", "value": "verify-fixture",
                "domain": ".douyin.com", "path": "/",
            }],
            "origins": [{
                "origin": "https://www.douyin.com",
                "localStorage": [{
                    "name": "__tea_cache_tokens_6383",
                    "value": json.dumps({
                        "web_id": "111", "user_unique_id": "222",
                    }),
                }],
            }],
        })
        client = DouyinClient(
            "", "Mozilla/5.0 Chrome/152.0.0.0", storage_state=state)

        query = parse_qs(urlsplit(client._build_url("/fixture", {})).query)

        self.assertEqual(query["webid"], ["222"])
        self.assertEqual(query["verifyFp"], ["verify-fixture"])
        self.assertEqual(query["fp"], ["verify-fixture"])

    async def test_secure_writer_uses_current_first_party_origin_and_params(self):
        client = DouyinClient(
            "sid_tt=x", "Mozilla/5.0 Chrome/152.0.0.0")

        url = client._build_url(
            "/aweme/v1/web/commit/follow/user/", {}, body="type=1&user_id=0")
        query = parse_qs(urlsplit(url).query)

        self.assertEqual(urlsplit(url).netloc, "www-hj.douyin.com")
        self.assertEqual(query["pc_libra_divert"], ["Windows"])
        self.assertEqual(query["update_version_code"], ["170400"])
        self.assertEqual(query["support_h265"], ["0"])
        self.assertEqual(query["support_dash"], ["0"])

    async def test_search_page_extracts_items_and_pagination(self):
        client = DouyinClient("sid_tt=x", "Mozilla/5.0 Chrome/130.0.0.0")
        payload = {
            "data": [
                {"aweme_info": {
                    "aweme_id": "a1", "desc": "one",
                    "video": {"play_addr": {"url_list": ["https://m/a.mp4"]}},
                }},
                {"aweme_info": {
                    "aweme_id": "a2", "images": [{"url_list": ["https://m/b.jpg"]}],
                }},
            ],
            "has_more": 1,
            "offset": 20,
        }
        client._get_payloads = AsyncMock(return_value=[payload])
        page = await client.search_awemes_page("测试", count=20)
        self.assertEqual([x["aweme_id"] for x in page["items"]], ["a1", "a2"])
        self.assertTrue(page["has_more"])
        self.assertEqual(page["offset"], 20)
        self.assertEqual(client._get_payloads.await_args.args[0],
                         "/aweme/v1/web/general/search/single/")
        params = client._get_payloads.await_args.args[1]
        self.assertEqual(params["search_channel"], "aweme_general")
        self.assertEqual(params["search_source"], "tab_search")
        self.assertEqual(params["list_type"], "multi")
        self.assertEqual(params["search_id"], "")
        self.assertNotIn("filter_selected", params)

    async def test_search_page_merges_stream_payloads(self):
        client = DouyinClient("sid_tt=x", "Mozilla/5.0 Chrome/130.0.0.0")
        first = {"data": [{"aweme_info": {
            "aweme_id": "stream-1", "video": {"play_addr": {"url_list": ["u"]}}
        }}], "has_more": 1, "offset": 20}
        second = {"data": [{"aweme_info": {
            "aweme_id": "stream-2", "video": {"play_addr": {"url_list": ["v"]}}
        }}], "has_more": 0, "offset": 40}
        client._get_payloads = AsyncMock(return_value=[first, second])
        page = await client.search_awemes_page("显卡", count=20)
        self.assertEqual([row["aweme_id"] for row in page["items"]],
                         ["stream-1", "stream-2"])
        self.assertTrue(page["has_more"])
        self.assertEqual(page["offset"], 40)

    async def test_search_page_chains_response_logid(self):
        client = DouyinClient("sid_tt=x", "Mozilla/5.0 Chrome/130.0.0.0")
        client._get_payloads = AsyncMock(side_effect=[[{
            "data": [], "has_more": 1, "offset": 15,
            "extra": {"logid": "search-log-1"},
        }], [{"data": [], "has_more": 0, "offset": 30}]])

        await client.search_awemes_page("显卡", offset=0, count=15)
        await client.search_awemes_page("显卡", offset=15, count=15)

        second_params = client._get_payloads.await_args_list[1].args[1]
        self.assertEqual(second_params["search_id"], "search-log-1")

    async def test_new_keyword_resets_search_id_chain(self):
        client = DouyinClient("sid_tt=x", "Mozilla/5.0 Chrome/130.0.0.0")
        client._search_id = "previous-keyword-log"
        client.search_awemes_page = AsyncMock(return_value={
            "items": [], "has_more": False, "offset": 0, "error": "",
        })

        await client.search_awemes("新关键词", max_pages=1)

        self.assertEqual(client._search_id, "")

    async def test_search_url_omits_a_bogus_and_uses_xmst(self):
        xmst = "x" * 184
        state = json.dumps({
            "cookies": [],
            "origins": [{
                "origin": "https://www.douyin.com",
                "localStorage": [{"name": "xmst", "value": xmst}],
            }],
        })
        client = DouyinClient(
            "", "Mozilla/5.0 Chrome/130.0.0.0", storage_state=state)

        with patch("app.platforms.douyin.client.gen_real_ms_token",
                   new=AsyncMock(side_effect=AssertionError("xmst should win"))):
            await client._prepare_ms_token(
                "https://www.douyin.com/aweme/v1/web/general/search/single/")
        url = client._build_url(
            "/aweme/v1/web/general/search/single/", {"keyword": "显卡"})
        query = parse_qs(urlsplit(url).query)

        self.assertEqual(query["msToken"], [xmst])
        self.assertNotIn("a_bogus", query)
        self.assertEqual(client._token_source, "account_local_storage_xmst")

    def test_replace_storage_state_refreshes_api_cookie_context(self):
        client = DouyinClient("sid_tt=old", "Mozilla/5.0 Chrome/130.0.0.0")
        client._ms_token = "stale"
        client.replace_storage_state({"cookies": [{
            "name": "sid_tt", "value": "new", "domain": ".douyin.com", "path": "/"
        }], "origins": []})
        self.assertIn("sid_tt=new", client.cookie)
        self.assertIsNone(client._ms_token)

    async def test_search_reports_verification_for_api_only_mode(self):
        client = DouyinClient("sid_tt=x", "Mozilla/5.0 Chrome/130.0.0.0")
        client._get_payloads = AsyncMock(return_value=[{
            "data": [],
            "search_nil_info": {"search_nil_type": "verify_check"},
        }])
        items, error = await client.search_awemes("测试", max_pages=1)
        self.assertEqual(items, [])
        self.assertEqual(error, "verification_required")

    async def test_search_business_login_rejection_is_not_an_empty_result(self):
        client = DouyinClient("sid_tt=x", "Mozilla/5.0 Chrome/130.0.0.0")
        client._get_payloads = AsyncMock(return_value=[{
            "status_code": 2483,
            "status_msg": "请先登录再继续使用",
        }])
        items, error = await client.search_awemes("测试", max_pages=1)
        self.assertEqual(items, [])
        self.assertEqual(error, "logged_out")
        self.assertEqual(client.last_error, "logged_out")

    async def test_search_unknown_business_rejection_keeps_status_code(self):
        client = DouyinClient("sid_tt=x", "Mozilla/5.0 Chrome/130.0.0.0")
        client._get_payloads = AsyncMock(return_value=[{
            "status_code": 12345,
            "status_msg": "request rejected",
        }])
        items, error = await client.search_awemes("测试", max_pages=1)
        self.assertEqual(items, [])
        self.assertEqual(error, "api_rejected:status_code=12345")

    async def test_session_scope_reuses_ms_token(self):
        client = DouyinClient("sid_tt=x", "Mozilla/5.0 Chrome/130.0.0.0")

        class Response:
            status_code = 200
            content = b"{}"

            def json(self):
                return {}

        fake_session = AsyncMock()
        fake_session.get = AsyncMock(return_value=Response())
        fake_session.close = AsyncMock()
        with patch("app.platforms.douyin.client.AsyncSession", return_value=fake_session), \
                patch("app.platforms.douyin.client.gen_real_ms_token",
                      new=AsyncMock(return_value="stable-token")) as token:
            async with client.session_scope():
                await client._get_json("/one", {})
                await client._get_json("/two", {})
        self.assertEqual(token.await_count, 1)
        self.assertEqual(fake_session.get.await_count, 2)
        first_url = fake_session.get.await_args_list[0].args[0]
        second_url = fake_session.get.await_args_list[1].args[0]
        self.assertIn("msToken=stable-token", first_url)
        self.assertIn("msToken=stable-token", second_url)

    async def test_fetch_all_danmaku_extracts_nested_rows_and_deduplicates(self):
        client = DouyinClient("sid_tt=x", "Mozilla/5.0 Chrome/130.0.0.0")
        client.fetch_danmaku_page = AsyncMock(return_value={
            "data": {
                "danmaku_list": [
                    {"danmaku_id": "d1", "content": "hello"},
                    {"danmaku_id": "d1", "content": "hello"},
                ],
                "meta": {"items": [{"cid": "d2", "text": "world"}]},
            }
        })
        rows = await client.fetch_all_danmaku("aweme-1")
        self.assertEqual([row.get("danmaku_id") or row.get("cid")
                          for row in rows], ["d1", "d2"])
        client.fetch_danmaku_page.assert_awaited_once()

    async def test_post_comment_success_builds_reply_form(self):
        client = DouyinClient("sid_tt=x", "Mozilla/5.0 Chrome/130.0.0.0")

        class Response:
            status_code = 200
            content = b'{"status_code":0,"comment":{"cid":"c1"}}'
            headers = {"content-type": "application/json"}
            cookies = None

            def json(self):
                return {"status_code": 0, "comment": {"cid": "c1"}}

        fake_session = AsyncMock()
        fake_session.post = AsyncMock(return_value=Response())
        fake_session.close = AsyncMock()
        with patch("app.platforms.douyin.client.AsyncSession", return_value=fake_session), \
                patch("app.platforms.douyin.client.gen_real_ms_token",
                      new=AsyncMock(return_value="stable-token")):
            async with client.session_scope():
                ok, cid, error = await client.post_comment(
                    "aweme-1", "收到", reply_comment_id="parent-1")
        self.assertTrue(ok)
        self.assertEqual(cid, "c1")
        self.assertEqual(error, "")
        request = fake_session.post.await_args
        form = dict(parse_qsl(request.kwargs["data"], keep_blank_values=True))
        self.assertEqual(form["reply_id"], "parent-1")
        self.assertEqual(form["reply_comment_id"], "parent-1")
        self.assertEqual(request.kwargs["headers"]["Content-Type"],
                         "application/x-www-form-urlencoded;charset=UTF-8")

    async def test_post_signature_binds_exact_form_body_sent_on_wire(self):
        client = DouyinClient("sid_tt=x", "Mozilla/5.0 Chrome/130.0.0.0")
        captured = {}

        class Response:
            status_code = 200
            content = b'{"status_code":0}'
            headers = {"content-type": "application/json"}
            cookies = None

            def json(self):
                return {"status_code": 0}

        fake_session = AsyncMock()
        fake_session.post = AsyncMock(return_value=Response())
        fake_session.close = AsyncMock()

        def signer(query, user_agent, *, body="", fp=""):
            captured.update(query=query, user_agent=user_agent, body=body, fp=fp)
            return query + "&a_bogus=SIGNED"

        with patch("app.platforms.douyin.client.AsyncSession", return_value=fake_session), \
                patch("app.platforms.douyin.client.gen_real_ms_token",
                      new=AsyncMock(return_value="stable-token")), \
                patch("app.platforms.douyin.client.sign_url", side_effect=signer):
            async with client.session_scope():
                data = await client._post_json(
                    "/aweme/v1/web/comment/publish/",
                    {"aweme_id": "aweme-1", "item_type": 0},
                    {"aweme_id": "aweme-1", "text": "收到 & ok",
                     "reply_id": "", "item_type": 0})

        expected = urlencode({
            "aweme_id": "aweme-1", "text": "收到 & ok",
            "reply_id": "", "item_type": "0",
        })
        request = fake_session.post.await_args
        self.assertEqual(data, {"status_code": 0})
        self.assertEqual(captured["body"], expected)
        self.assertEqual(request.kwargs["data"], expected)
        self.assertEqual(parse_qs(urlsplit(request.args[0]).query)["a_bogus"],
                         ["SIGNED"])
        self.assertTrue(client.last_response["a_bogus_body_bound"])
        self.assertEqual(client.last_response["business_code"], 0)

    async def test_post_attaches_account_bound_security_headers_and_classifies_403(self):
        state = json.dumps({
            "cookies": [
                {"name": "UIFID", "value": "device-fixture",
                 "domain": ".douyin.com", "path": "/"},
                {"name": "passport_csrf_token", "value": "csrf-fixture",
                 "domain": ".douyin.com", "path": "/"},
                {"name": "bd_ticket_guard_client_data", "value": "ticket-fixture",
                 "domain": ".douyin.com", "path": "/"},
            ],
        })
        client = DouyinClient(
            "", "Mozilla/5.0 Chrome/130.0.0.0", storage_state=state)

        class Response:
            status_code = 403
            content = b""
            headers = {
                "content-type": "application/json",
                "bd-ticket-guard-result": "1101",
            }
            cookies = None

        fake_session = AsyncMock()
        fake_session.post = AsyncMock(return_value=Response())
        fake_session.close = AsyncMock()
        with patch("app.platforms.douyin.client.AsyncSession", return_value=fake_session), \
                patch("app.platforms.douyin.client.gen_real_ms_token",
                      new=AsyncMock(return_value="stable-token")):
            async with client.session_scope():
                data = await client._post_json(
                    "/aweme/v1/web/commit/follow/user/", {},
                    {"user_id": "0", "type": 1})

        request = fake_session.post.await_args
        headers = request.kwargs["headers"]
        self.assertIsNone(data)
        self.assertEqual(headers["uifid"], "device-fixture")
        self.assertEqual(headers["x-secsdk-csrf-token"], "csrf-fixture")
        self.assertEqual(
            headers["bd-ticket-guard-client-data"], "ticket-fixture")
        self.assertEqual(client.last_error,
                         "api_requires_browser_security:http_403")
        self.assertFalse(client.last_write_uncertain)
        self.assertEqual(client.last_response["http_status"], 403)
        self.assertEqual(client.last_response["ticket_guard_result"], "1101")
        self.assertTrue(client.last_response["a_bogus_body_bound"])
        self.assertTrue(client.last_response["device_header_attached"])
        self.assertTrue(client.last_response["csrf_header_attached"])
        self.assertTrue(client.last_response["ticket_guard_client_attached"])
        self.assertFalse(client.last_response["dtrait_header_attached"])
        self.assertEqual(client.last_response["rejection_reason"],
                         "browser_security_context_required")

    async def test_post_comment_business_rejection_is_retryable_for_hybrid(self):
        client = DouyinClient("sid_tt=x", "Mozilla/5.0 Chrome/130.0.0.0")
        client._post_json = AsyncMock(return_value={
            "status_code": 8, "status_msg": "频繁操作",
        })
        ok, cid, error = await client.post_comment("aweme-1", "收到")
        self.assertFalse(ok)
        self.assertEqual(cid, "")
        self.assertTrue(error.startswith("api_rejected:status_code=8"))
        self.assertFalse(client.last_write_uncertain)

    async def test_protected_write_login_message_requires_dtrait_before_auth_loss(self):
        client = DouyinClient("sid_tt=x", "Mozilla/5.0 Chrome/130.0.0.0")
        client.last_response = {
            "browser_security_context_required": True,
            "dtrait_header_attached": False,
        }
        ok, error = client._write_result(
            {"status_code": 8, "status_msg": "用户未登录"}, "no_response")
        self.assertFalse(ok)
        self.assertTrue(error.startswith("api_requires_browser_security:"))

        client.last_response["dtrait_header_attached"] = True
        ok, error = client._write_result(
            {"status_code": 8, "status_msg": "用户未登录"}, "no_response")
        self.assertFalse(ok)
        self.assertTrue(error.startswith("logged_out:"))

    async def test_post_comment_invalid_success_body_is_uncertain(self):
        client = DouyinClient("sid_tt=x", "Mozilla/5.0 Chrome/130.0.0.0")
        client._post_json = AsyncMock(return_value={"comment": {}})
        ok, _, error = await client.post_comment("aweme-1", "收到")
        self.assertFalse(ok)
        self.assertEqual(error, "write_uncertain:invalid_response")
        self.assertTrue(client.last_write_uncertain)

    async def test_follow_and_unfollow_send_expected_type(self):
        client = DouyinClient("sid_tt=x", "Mozilla/5.0 Chrome/130.0.0.0")
        client._post_json = AsyncMock(return_value={"status_code": 0})
        self.assertEqual(await client.set_follow_state("uid-1"), (True, ""))
        self.assertEqual(await client.set_follow_state("uid-1", unfollow=True), (True, ""))
        forms = [call.args[2] for call in client._post_json.await_args_list]
        self.assertEqual([form["type"] for form in forms], [1, 2])
        self.assertEqual([call.args[1] for call in client._post_json.await_args_list],
                         [{}, {}])
        self.assertEqual([set(form) for form in forms],
                         [{"user_id", "type"}, {"user_id", "type"}])

    async def test_post_timeout_is_marked_uncertain(self):
        client = DouyinClient("sid_tt=x", "Mozilla/5.0 Chrome/130.0.0.0")
        client._post_json = AsyncMock(return_value=None)
        client.last_error = "network:TimeoutError"
        client.last_write_uncertain = True
        ok, _, error = await client.post_comment("aweme-1", "收到")
        self.assertFalse(ok)
        self.assertTrue(error.startswith("write_uncertain:network:TimeoutError"))

    async def test_send_dm_requires_existing_conversation_ticket(self):
        client = DouyinClient("sid_tt=x", "Mozilla/5.0 Chrome/130.0.0.0")
        ok, error = await client.send_dm("conv", "", "ticket", "hello")
        self.assertFalse(ok)
        self.assertIn("conv_id/short_id/ticket", error)

    async def test_create_dm_conversation_posts_current_protocol(self):
        client = DouyinClient("sid_tt=x", "Mozilla/5.0 Chrome/130.0.0.0")

        class Response:
            status_code = 200
            content = b"protobuf-create-response"

        expected = {
            "conv_id": "conv-new", "conv_short_id": "42",
            "conv_type": 1, "ticket": "ticket-new",
        }
        fake_session = AsyncMock()
        fake_session.post = AsyncMock(return_value=Response())
        fake_session.close = AsyncMock()
        with patch("app.platforms.douyin.client.AsyncSession", return_value=fake_session), \
                patch("app.browser.douyin_im_pb.build_create_conversation_request",
                      return_value=b"protobuf-create-request") as build, \
                patch("app.browser.douyin_im_pb.parse_create_conversation_response",
                      return_value={"ok": True, "conversation": expected}) as parse:
            async with client.session_scope():
                conversation, error = await client.create_dm_conversation(
                    "123456", "987654", target_sec_uid="MS4wLjABAAAA")

        self.assertEqual((conversation, error), (expected, ""))
        build.assert_called_once()
        parse.assert_called_once_with(b"protobuf-create-response")
        request = fake_session.post.await_args
        self.assertEqual(request.args[0],
                         "https://imapi.douyin.com/v2/conversation/create")
        self.assertEqual(request.kwargs["data"], b"protobuf-create-request")
        self.assertEqual(request.kwargs["headers"]["Content-Type"],
                         "application/x-protobuf")
        self.assertEqual(request.kwargs["headers"]["Referer"],
                         "https://www.douyin.com/user/MS4wLjABAAAA")

    async def test_create_dm_timeout_is_write_uncertain(self):
        client = DouyinClient("sid_tt=x", "Mozilla/5.0 Chrome/130.0.0.0")
        fake_session = AsyncMock()
        fake_session.post = AsyncMock(side_effect=TimeoutError())
        fake_session.close = AsyncMock()
        with patch("app.platforms.douyin.client.AsyncSession", return_value=fake_session):
            conversation, error = await client.create_dm_conversation(
                "123456", "987654")
        self.assertIsNone(conversation)
        self.assertEqual(error, "write_uncertain:network:TimeoutError")
        self.assertTrue(client.last_write_uncertain)

    async def test_send_dm_posts_existing_conversation_protocol(self):
        client = DouyinClient("sid_tt=x", "Mozilla/5.0.0 Chrome/130.0.0.0")

        class Response:
            status_code = 200
            content = b"protobuf-response"

        fake_session = AsyncMock()
        fake_session.post = AsyncMock(return_value=Response())
        fake_session.close = AsyncMock()
        with patch("app.platforms.douyin.client.AsyncSession", return_value=fake_session), \
                patch("app.browser.douyin_im_pb.build_send_request",
                      return_value=b"protobuf-request") as build, \
                patch("app.browser.douyin_im_pb.parse_send_response",
                      return_value={"ok": True, "cmd": 100}) as parse:
            async with client.session_scope():
                ok, error = await client.send_dm(
                    "conv-1", "42", "ticket-1", "hello")

        self.assertEqual((ok, error), (True, ""))
        build.assert_called_once()
        parse.assert_called_once_with(b"protobuf-response")
        request = fake_session.post.await_args
        self.assertEqual(request.kwargs["data"], b"protobuf-request")
        self.assertEqual(request.kwargs["headers"]["Accept"],
                         "application/x-protobuf")
        self.assertEqual(request.kwargs["headers"]["Content-Type"],
                         "application/x-protobuf")

    async def test_send_dm_invalid_success_body_is_uncertain(self):
        client = DouyinClient("sid_tt=x", "Mozilla/5.0 Chrome/130.0.0.0")

        class Response:
            status_code = 200
            content = b"malformed-protobuf"

        fake_session = AsyncMock()
        fake_session.post = AsyncMock(return_value=Response())
        fake_session.close = AsyncMock()
        with patch("app.platforms.douyin.client.AsyncSession", return_value=fake_session), \
                patch("app.browser.douyin_im_pb.parse_send_response",
                      return_value={"ok": False, "cmd": 0, "error_code": 0, "msg": ""}):
            async with client.session_scope():
                ok, error = await client.send_dm("conv-1", "42", "ticket-1", "hello")

        self.assertFalse(ok)
        self.assertEqual(error, "write_uncertain:invalid_response")
        self.assertTrue(client.last_write_uncertain)


if __name__ == "__main__":
    unittest.main()


# These platform contract tests exercise an authorized running client.
# License-denial behavior is covered by test_device_business_gate.py.
import pytest as _license_pytest

@_license_pytest.fixture(autouse=True)
def _authorized_platform_test(monkeypatch):
    monkeypatch.setattr("app.licensing.is_active", lambda: True)
