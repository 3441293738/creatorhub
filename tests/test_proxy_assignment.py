import tempfile
import unittest
from pathlib import Path

import app.db as db
from app.config import Config
from app.main import _proxy_probe_status, _proxy_status_ok
from app.models import DouyinAccount, ProxyPool
from app.profiles import (
    assign_proxy_from_pool,
    is_mainland_geo,
    release_proxy_reservation,
    reserve_proxy_from_pool,
)


class ProxyAssignmentTests(unittest.TestCase):
    def setUp(self):
        self.previous_engine = db._engine
        self.tmp = tempfile.TemporaryDirectory()
        db.init_db(str(Path(self.tmp.name) / "proxy.db"))
        self.cfg = Config()

    def tearDown(self):
        if db._engine is not None:
            db._engine.dispose()
        db._engine = self.previous_engine
        self.tmp.cleanup()

    def test_auto_assignment_never_reuses_an_occupied_proxy(self):
        with db.get_session() as session:
            session.add(ProxyPool(url="http://proxy-a.example:8000", status="ok"))
            session.add(ProxyPool(url="http://proxy-b.example:8000", status="unknown"))
            session.add(DouyinAccount(
                nickname="a", proxy="proxy-a.example:8000"))
            session.add(DouyinAccount(
                nickname="b", proxy="http://proxy-b.example:8000"))
            session.commit()

            self.assertEqual(assign_proxy_from_pool(session, self.cfg), "")

    def test_auto_assignment_skips_unusable_pool_entries(self):
        usable = "http://usable.example:8000"
        with db.get_session() as session:
            session.add(ProxyPool(
                url="http://disabled.example:8000", enabled=False, status="ok"))
            session.add(ProxyPool(url="http://bad.example:8000", status="bad"))
            session.add(ProxyPool(
                url="http://auth.example:8000", status="auth_error"))
            session.add(ProxyPool(
                url="http://blocked.example:8000", status="blocked"))
            session.add(ProxyPool(url=usable, status="unknown"))
            session.commit()

            self.assertEqual(assign_proxy_from_pool(session, self.cfg), usable)

    def test_config_pool_urls_are_normalized_and_unique(self):
        self.cfg.proxies = ["proxy-a.example:8000", "http://proxy-a.example:8000"]
        with db.get_session() as session:
            self.assertEqual(
                assign_proxy_from_pool(session, self.cfg),
                "http://proxy-a.example:8000",
            )

    def test_concurrent_login_reservations_are_unique_until_released(self):
        first = "http://proxy-a.example:8000"
        second = "http://proxy-b.example:8000"
        self.cfg.proxies = [first, second]
        try:
            with db.get_session() as session:
                self.assertEqual(
                    reserve_proxy_from_pool(session, self.cfg, "login-a"), first)
                self.assertEqual(
                    reserve_proxy_from_pool(session, self.cfg, "login-b"), second)
                self.assertEqual(
                    reserve_proxy_from_pool(session, self.cfg, "login-a"), first)
            release_proxy_reservation("login-a")
            with db.get_session() as session:
                self.assertEqual(
                    reserve_proxy_from_pool(session, self.cfg, "login-c"), first)
        finally:
            for key in ("login-a", "login-b", "login-c"):
                release_proxy_reservation(key)

    def test_proxy_http_status_is_strictly_classified(self):
        for status in (200, 204, 301, 399):
            self.assertTrue(_proxy_status_ok(status))
            self.assertEqual(_proxy_probe_status(status), "ok")

        self.assertFalse(_proxy_status_ok(407))
        self.assertEqual(_proxy_probe_status(407), "auth_error")
        self.assertEqual(_proxy_probe_status(403), "blocked")
        self.assertEqual(_proxy_probe_status(429), "blocked")
        self.assertEqual(_proxy_probe_status(500), "bad")

    def test_domestic_auto_assignment_skips_verified_overseas_proxy(self):
        overseas = "socks5://user:pass@us-proxy.example:1080"
        with db.get_session() as session:
            session.add(ProxyPool(
                url=overseas,
                status="ok",
                enabled=True,
                exit_ip="203.0.113.10",
                country="US",
                region="California",
                city="Los Angeles",
            ))
            session.commit()

            self.assertEqual(
                assign_proxy_from_pool(session, self.cfg, platform="xhs"),
                "",
            )
            self.assertEqual(
                reserve_proxy_from_pool(
                    session, self.cfg, "login-xhs", platform="xhs"),
                "",
            )
            self.assertEqual(
                assign_proxy_from_pool(session, self.cfg, platform="tiktok"),
                overseas,
            )

    def test_is_mainland_geo_distinguishes_overseas_and_special_regions(self):
        self.assertTrue(is_mainland_geo("CN", "Guangdong", "Shenzhen"))
        self.assertTrue(is_mainland_geo("中国", "浙江", "杭州"))
        self.assertFalse(is_mainland_geo("CN", "Hong Kong", "Hong Kong"))
        self.assertFalse(is_mainland_geo("中国", "台湾", "台北"))
        self.assertFalse(is_mainland_geo("US", "California", "Los Angeles"))


if __name__ == "__main__":
    unittest.main()
