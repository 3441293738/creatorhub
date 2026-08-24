import unittest
from unittest.mock import patch

import creatorhub


class CreatorHubLauncherTests(unittest.TestCase):
    def test_start_reuses_an_existing_creatorhub_service(self):
        with patch.object(creatorhub, "ensure_config"), \
                patch.object(creatorhub, "installation_is_current",
                             return_value=True), \
                patch.object(creatorhub, "venv_python",
                             return_value=creatorhub.Path(
                                 creatorhub.sys.executable)), \
                patch.object(creatorhub, "service_is_ready",
                             return_value=True), \
                patch.object(creatorhub.webbrowser, "open") as browser_open, \
                patch.object(creatorhub, "run") as run:
            creatorhub.start(
                host="0.0.0.0", port=8000, no_open=False, reload=False,
                skip_install=False, skip_browser=False, skip_node=False,
            )

        run.assert_not_called()
        browser_open.assert_called_once_with("http://127.0.0.1:8000")

    def test_start_reports_non_creatorhub_port_conflict_before_uvicorn(self):
        with patch.object(creatorhub, "ensure_config"), \
                patch.object(creatorhub, "installation_is_current",
                             return_value=True), \
                patch.object(creatorhub, "venv_python",
                             return_value=creatorhub.Path(
                                 creatorhub.sys.executable)), \
                patch.object(creatorhub, "service_is_ready",
                             return_value=False), \
                patch.object(creatorhub, "port_is_open", return_value=True), \
                patch.object(creatorhub, "run") as run:
            with self.assertRaisesRegex(RuntimeError, "端口 8000 已被其他程序占用"):
                creatorhub.start(
                    host="0.0.0.0", port=8000, no_open=True,
                    reload=False, skip_install=False,
                    skip_browser=False, skip_node=False,
                )

        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
