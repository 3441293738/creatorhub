import tempfile
import unittest
from pathlib import Path

import app.db as db
from app.config import Config
from app.main import _proxy_probe_status, _proxy_status_ok
from app.models import DouyinAccount, ProxyPool
from app.profiles import assign_proxy_from_pool


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

    def test_proxy_http_status_is_strictly_classified(self):
        for status in (200, 204, 301, 399):
            self.assertTrue(_proxy_status_ok(status))
            self.assertEqual(_proxy_probe_status(status), "ok")

        self.assertFalse(_proxy_status_ok(407))
        self.assertEqual(_proxy_probe_status(407), "auth_error")
        self.assertEqual(_proxy_probe_status(403), "blocked")
        self.assertEqual(_proxy_probe_status(429), "blocked")
        self.assertEqual(_proxy_probe_status(500), "bad")


if __name__ == "__main__":
    unittest.main()
