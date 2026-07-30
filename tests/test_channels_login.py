import unittest
from unittest.mock import AsyncMock, patch

import app.main as main
from app.browser.login import (
    _channels_auth_response_ok,
    _channels_login_ready,
)


class ChannelsLoginSignalTests(unittest.TestCase):
    def test_wxuin_alone_is_not_a_completed_login(self):
        self.assertFalse(
            _channels_login_ready({"wxuin"}, auth_verified=False, on_platform=True)
        )

    def test_strong_cookie_completes_login_on_platform(self):
        self.assertTrue(
            _channels_login_ready(
                {"wxuin", "sessionid"}, auth_verified=False, on_platform=True
            )
        )
        self.assertTrue(
            _channels_login_ready(
                {"_finder_auth"}, auth_verified=False, on_platform=True
            )
        )

    def test_successful_auth_response_completes_login_without_cookie_guess(self):
        self.assertTrue(
            _channels_login_ready(
                {"wxuin"}, auth_verified=True, on_platform=True
            )
        )
        self.assertFalse(
            _channels_login_ready(
                {"sessionid"}, auth_verified=True, on_platform=False
            )
        )

    def test_auth_response_requires_explicit_business_success(self):
        self.assertTrue(_channels_auth_response_ok({"errCode": 0, "data": {}}))
        self.assertTrue(_channels_auth_response_ok(
            {"baseResponse": {"retCode": "0"}}
        ))
        self.assertFalse(_channels_auth_response_ok({"errCode": -1}))
        self.assertFalse(_channels_auth_response_ok({"data": {"nickname": "游客"}}))


class ChannelsProfileRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_transient_logged_out_result_is_retried(self):
        fetch = AsyncMock(side_effect=[
            ({}, "logged_out"),
            ({"nickname": "视频号账号"}, ""),
        ])
        with (
            patch.object(main, "browser", object()),
            patch.object(main, "fetch_channels_self_profile", fetch),
            patch.object(main.asyncio, "sleep", AsyncMock()) as sleep,
        ):
            profile, error = await main._fetch_channels_profile_with_retry(object())

        self.assertEqual(profile["nickname"], "视频号账号")
        self.assertEqual(error, "")
        self.assertEqual(fetch.await_count, 2)
        sleep.assert_awaited_once_with(1.5)

    async def test_non_login_error_is_not_retried(self):
        fetch = AsyncMock(return_value=({}, "no_profile_data"))
        with (
            patch.object(main, "browser", object()),
            patch.object(main, "fetch_channels_self_profile", fetch),
        ):
            profile, error = await main._fetch_channels_profile_with_retry(object())

        self.assertEqual(profile, {})
        self.assertEqual(error, "no_profile_data")
        self.assertEqual(fetch.await_count, 1)


if __name__ == "__main__":
    unittest.main()
