"""Regression checks for the default no-browser pytest contract."""
import os
from pathlib import Path

import pytest


def test_main_uses_a_temporary_default_configuration():
    from app import main

    config_file = Path(os.environ["CREATORHUB_CONFIG_PATH"])
    assert config_file.parent.name.startswith("creatorhub-tests-")
    assert Path(main.cfg.db_path).parent == config_file.parent
    assert Path(main.cfg.engine.profiles_dir).parent == config_file.parent
    assert Path(main.cfg.engine.media_dir).parent == config_file.parent


@pytest.mark.parametrize("method", [
    "launch", "launch_persistent_context", "connect_over_cdp",
])
def test_patchright_browser_boundaries_fail_before_launch(method):
    from patchright.async_api import BrowserType

    with pytest.raises(pytest.fail.Exception, match="Real browser access"):
        getattr(BrowserType, method)(None)


def test_cdp_process_boundary_fails_before_launch():
    from app.browser.cdp import XhsCdpBackend

    with pytest.raises(pytest.fail.Exception, match="Real browser access"):
        XhsCdpBackend._spawn_process(None, ["fixture-chrome"])


def test_async_driver_is_not_started_by_default():
    from patchright.async_api import async_playwright

    with pytest.raises(pytest.fail.Exception, match="Real browser access"):
        async_playwright().start()
