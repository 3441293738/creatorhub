"""Exercise the actual WebView2 renderer without accounts or service startup."""
import tempfile
import threading
import time
from pathlib import Path


def smoke():
    import webview
    from desktop.controller import Controller
    from desktop.launcher import prepare_home
    from desktop.web_shell import ShellServer

    failures = []
    with tempfile.TemporaryDirectory(prefix="creatorhub-webview-") as temp:
        home = Path(temp)
        prepare_home(home)
        server = ShellServer(Controller(home, install_browser=False))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        window = webview.create_window("CreatorHub renderer test", server.origin, hidden=True)

        def verify():
            try:
                deadline = time.monotonic() + 45
                while time.monotonic() < deadline:
                    result = window.evaluate_js("document.body.innerText") or ""
                    if "启动本地服务" in result:
                        break
                    time.sleep(.3)
                else:
                    raise AssertionError("Desktop renderer did not load real stopped state: " + result)
                window.evaluate_js("location.hash = '#/settings'")
                time.sleep(1)
                assert "跟随系统" in window.evaluate_js("document.body.innerText")
                assert "检查更新" in window.evaluate_js("document.body.innerText")
                assert window.evaluate_js("document.querySelectorAll('main').length") == 1
            except Exception as exc:
                failures.append(str(exc))
            finally:
                window.destroy()

        try:
            webview.start(verify, gui="edgechromium")
        finally:
            server.shutdown()
            server.server_close()
    if failures:
        raise AssertionError("; ".join(failures))
    print("PASS native WebView2 desktop renderer and navigation")
    return 0
