import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import app.db as db
import app.main as main
from app.browser.ip_fingerprint import (
    derive_ip_fingerprint,
    locale_for_country,
    normalize_country,
    timezone_for_geo,
)
from app.models import DouyinAccount


class IpFingerprintDerivationTests(unittest.TestCase):
    def test_same_account_and_ip_is_deterministic(self):
        first = derive_ip_fingerprint(
            7, "203.0.113.8", country="US", timezone_id="America/Los_Angeles",
            latitude=34.0522, longitude=-118.2437)
        second = derive_ip_fingerprint(
            7, "203.0.113.8", country="US", timezone_id="America/Los_Angeles",
            latitude=34.0522, longitude=-118.2437)

        self.assertEqual(first, second)
        self.assertEqual(first["timezone_id"], "America/Los_Angeles")
        self.assertEqual(first["locale"], "en-US")
        self.assertNotEqual(first["geo_lat"], 34.0522)

    def test_accounts_sharing_one_ip_do_not_share_one_device(self):
        first = derive_ip_fingerprint(1, "2001:db8::1", country="CN")
        second = derive_ip_fingerprint(2, "2001:db8::1", country="CN")

        self.assertNotEqual(first["fp_seed"], second["fp_seed"])
        self.assertEqual(first["source_ip"], "2001:db8::1")

    def test_country_aliases_drive_locale_and_timezone(self):
        self.assertEqual(normalize_country("中国"), "CN")
        self.assertEqual(locale_for_country("台湾"), "zh-TW")
        self.assertEqual(timezone_for_geo("JP"), "Asia/Tokyo")
        self.assertEqual(
            timezone_for_geo("US", "America/Chicago"), "America/Chicago")

    def test_invalid_ip_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "出口 IP 无效"):
            derive_ip_fingerprint(1, "not-an-ip")

    def test_geo_parsers_keep_timezone_and_coordinates(self):
        ipinfo = main._parse_ipinfo({
            "ip": "203.0.113.9", "country": "US", "region": "California",
            "city": "Los Angeles", "loc": "34.0522,-118.2437",
            "timezone": "America/Los_Angeles", "org": "AS64500 Fixture",
        })
        ipapi = main._parse_ipapi({
            "status": "success", "query": "198.51.100.4",
            "country": "日本", "countryCode": "JP", "regionName": "Tokyo",
            "city": "Tokyo", "lat": 35.68, "lon": 139.76,
            "timezone": "Asia/Tokyo", "isp": "Fixture ISP",
        })

        self.assertEqual(ipinfo["timezone"], "America/Los_Angeles")
        self.assertEqual(ipinfo["lat"], 34.0522)
        self.assertEqual(ipapi["country"], "JP")
        self.assertEqual(ipapi["lon"], 139.76)


class IpFingerprintApiTests(unittest.TestCase):
    def setUp(self):
        self.previous_engine = db._engine
        self.previous_browser = main.browser
        self.previous_open_browsers = dict(main.open_browsers)
        self.tmp = tempfile.TemporaryDirectory()
        db.init_db(str(Path(self.tmp.name) / "fingerprint.db"))
        main.open_browsers.clear()

    def tearDown(self):
        main.browser = self.previous_browser
        main.open_browsers.clear()
        main.open_browsers.update(self.previous_open_browsers)
        if db._engine is not None:
            db._engine.dispose()
        db._engine = self.previous_engine
        self.tmp.cleanup()

    def test_generate_from_current_ip_persists_and_closes_old_context(self):
        with db.get_session() as session:
            account = DouyinAccount(
                nickname="fixture", platform="douyin",
                proxy="http://proxy.fixture:8080",
                browser_backend="fingerprint_chromium",
                fp_seed="old-seed",
                fp_platform="macos",
                fp_brand="Edge",
                fp_hardware_concurrency=16,
                fp_disable_spoofing="canvas",
                fp_language_mode="custom",
                fp_timezone_mode="custom",
                fp_viewport_mode="custom",
                fp_location_mode="custom",
                fp_geolocation_permission="deny",
                fp_webrtc_mode="allow",
                fp_extra_args="--mute-audio",
            )
            session.add(account)
            session.commit()
            session.refresh(account)
            account_id = account.id

        class BrowserStub:
            def __init__(self):
                self.closed = []

            async def close_context(self, key):
                self.closed.append(key)

        browser = BrowserStub()
        main.browser = browser
        geo = {
            "ip": "203.0.113.20", "country": "JP", "region": "Tokyo",
            "city": "Tokyo", "timezone": "Asia/Tokyo",
            "lat": 35.6762, "lon": 139.6503,
        }
        with patch("app.main._proxy_geo", new=AsyncMock(return_value=geo)):
            result = asyncio.run(
                main.generate_account_fingerprint_from_ip(account_id))

        with db.get_session() as session:
            saved = session.get(DouyinAccount, account_id)
            first_seed = saved.fp_seed
            self.assertEqual(saved.fp_source_ip, "203.0.113.20")
            self.assertEqual(saved.fp_country, "JP")
            self.assertEqual(saved.timezone_id, "Asia/Tokyo")
            self.assertEqual(saved.locale, "ja-JP")
            self.assertIsNotNone(saved.fp_generated_at)
            self.assertEqual(saved.fp_platform, "")
            self.assertEqual(saved.fp_brand, "")
            self.assertEqual(saved.fp_hardware_concurrency, 0)
            self.assertEqual(saved.fp_disable_spoofing, "")
            self.assertEqual(saved.fp_language_mode, "auto")
            self.assertEqual(saved.fp_timezone_mode, "auto")
            self.assertEqual(saved.fp_viewport_mode, "auto")
            self.assertEqual(saved.fp_location_mode, "auto")
            self.assertEqual(saved.fp_geolocation_permission, "allow")
            self.assertEqual(saved.fp_webrtc_mode, "conceal")
            self.assertEqual(saved.fp_extra_args, "")
        self.assertTrue(result["ok"])
        self.assertEqual(result["fingerprint"]["source_ip"], "203.0.113.20")
        self.assertEqual(browser.closed, [account_id])

        # The same sticky exit must keep the same device seed.
        with patch("app.main._proxy_geo", new=AsyncMock(return_value=geo)):
            asyncio.run(main.generate_account_fingerprint_from_ip(account_id))
        with db.get_session() as session:
            self.assertEqual(session.get(DouyinAccount, account_id).fp_seed,
                             first_seed)

    def test_every_supported_fingerprint_field_can_be_edited(self):
        with db.get_session() as session:
            account = DouyinAccount(
                nickname="editable", platform="douyin",
                browser_backend="fingerprint_chromium",
                fp_seed="initial-seed")
            session.add(account)
            session.commit()
            session.refresh(account)
            account_id = account.id

        class BrowserStub:
            def __init__(self):
                self.closed = []

            async def close_context(self, key):
                self.closed.append(key)

        main.browser = BrowserStub()
        body = main.AccountFingerprintUpdateIn(
            seed="manual-seed",
            source_ip="2001:db8::2",
            country="US", region="California", city="Los Angeles",
            timezone="America/Los_Angeles", locale="en-US",
            accept_languages="en-US,en",
            viewport_w=1536, viewport_h=864,
            geo_lat=34.0522, geo_lon=-118.2437,
            platform="macos", platform_version="15.2.0",
            brand="Edge", brand_version="148.0.0.0",
            hardware_concurrency=12,
            gpu_vendor="Apple Inc.", gpu_renderer="Apple M3",
            disable_spoofing=["canvas", "audio"],
            language_mode="custom", timezone_mode="custom",
            viewport_mode="custom", location_mode="custom",
            geolocation_permission="ask", webrtc_mode="allow",
            extra_args="--mute-audio\n--disable-notifications",
        )

        result = asyncio.run(
            main.update_account_fingerprint(account_id, body))

        with db.get_session() as session:
            saved = session.get(DouyinAccount, account_id)
            self.assertEqual(saved.fp_seed, "manual-seed")
            self.assertEqual(saved.fp_source_ip, "2001:db8::2")
            self.assertEqual(saved.fp_platform, "macos")
            self.assertEqual(saved.fp_brand, "Edge")
            self.assertEqual(saved.fp_hardware_concurrency, 12)
            self.assertEqual(saved.fp_gpu_renderer, "Apple M3")
            self.assertEqual(saved.fp_disable_spoofing, "canvas,audio")
            self.assertEqual(saved.fp_language_mode, "custom")
            self.assertEqual(saved.fp_timezone_mode, "custom")
            self.assertEqual(saved.fp_viewport_mode, "custom")
            self.assertEqual(saved.fp_location_mode, "custom")
            self.assertEqual(saved.fp_geolocation_permission, "ask")
            self.assertEqual(saved.fp_webrtc_mode, "allow")
            self.assertEqual(
                saved.fp_extra_args,
                "--mute-audio\n--disable-notifications")
            self.assertEqual(saved.viewport_w, 1536)
            self.assertEqual(saved.timezone_id, "America/Los_Angeles")
        self.assertTrue(result["ok"])
        self.assertEqual(result["fingerprint"]["brand"], "Edge")
        self.assertEqual(main.browser.closed, [account_id])

    def test_invalid_manual_fingerprint_is_rejected(self):
        with self.assertRaisesRegex(main.HTTPException, "IANA 时区"):
            main._validate_fingerprint_update(main.AccountFingerprintUpdateIn(
                seed="fixture", timezone="not/a-zone"))

        with self.assertRaisesRegex(main.HTTPException, "账号环境冲突"):
            main._validate_fingerprint_update(main.AccountFingerprintUpdateIn(
                seed="fixture", extra_args="--user-data-dir=D:/fixture"))

        with self.assertRaisesRegex(main.HTTPException, "WebRTC 模式"):
            main._validate_fingerprint_update(main.AccountFingerprintUpdateIn(
                seed="fixture", webrtc_mode="invalid"))


if __name__ == "__main__":
    unittest.main()
