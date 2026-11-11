from unittest.mock import MagicMock, patch

import pytest
from desktop.launcher import ensure_browser


@pytest.mark.parametrize('system_available,bundled_available,downloads', [
    (True, False, False), (True, True, False),
    (False, True, False), (False, False, True),
])
def test_browser_selection(system_available, bundled_available, downloads):
    runtime = MagicMock()
    runtime.chromium.executable_path = 'missing/chrome.exe'
    if not system_available:
        runtime.chromium.launch.side_effect = RuntimeError('Chrome unavailable')
    with patch('patchright.sync_api.sync_playwright') as factory, \
         patch('desktop.launcher.Path.is_file', return_value=bundled_available), \
         patch('desktop.launcher.subprocess.run') as install:
        factory.return_value.__enter__.return_value = runtime
        ensure_browser()
        runtime.chromium.launch.assert_called_once_with(channel='chrome', headless=True)
        assert install.called == downloads
        if system_available:
            runtime.chromium.launch.return_value.close.assert_called_once()
        if downloads:
            assert install.call_args.args[0][-2:] == ['install', 'chromium']
            assert install.call_args.kwargs['check'] is True
