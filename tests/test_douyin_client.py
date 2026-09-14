import unittest
from unittest.mock import AsyncMock, patch

from app.platforms.douyin.client import DouyinClient


class DouyinClientSearchTests(unittest.IsolatedAsyncioTestCase):
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
        client._get_json = AsyncMock(return_value=payload)
        page = await client.search_awemes_page("测试", count=20)
        self.assertEqual([x["aweme_id"] for x in page["items"]], ["a1", "a2"])
        self.assertTrue(page["has_more"])
        self.assertEqual(page["offset"], 20)

    async def test_search_reports_verification_for_api_only_mode(self):
        client = DouyinClient("sid_tt=x", "Mozilla/5.0 Chrome/130.0.0.0")
        client._get_json = AsyncMock(return_value={
            "data": [],
            "search_nil_info": {"search_nil_type": "verify_check"},
        })
        items, error = await client.search_awemes("测试", max_pages=1)
        self.assertEqual(items, [])
        self.assertEqual(error, "verification_required")

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
        form = fake_session.post.await_args.kwargs["data"]
        self.assertEqual(form["reply_id"], "parent-1")
        self.assertEqual(form["reply_comment_id"], "parent-1")

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
                      return_value={"ok": True}) as parse:
            async with client.session_scope():
                ok, error = await client.send_dm(
                    "conv-1", "42", "ticket-1", "hello")

        self.assertEqual((ok, error), (True, ""))
        build.assert_called_once()
        parse.assert_called_once_with(b"protobuf-response")
        request = fake_session.post.await_args
        self.assertEqual(request.kwargs["data"], b"protobuf-request")
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
