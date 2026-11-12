"""Exercise the native renderer without accounts or weakening the shell CSP."""
import json
import queue
import tempfile
import threading
import time
from pathlib import Path
from desktop.platform_support import webview_options


def renderer_snapshot(window, token, results):
    # evaluate_js wraps its input in eval, which WKWebView correctly rejects
    # under our script-src 'self' policy. run_js executes the fixed probe without
    # eval; its platform-dependent return value is deliberately not used.
    window.run_js("""(() => {
        const text = document.body.innerText;
        fetch('/api/action', {
            method: 'POST',
            headers: {'Content-Type': 'application/json', 'X-Desktop-Token': TOKEN},
            body: JSON.stringify({name: 'renderer_snapshot', data: {
                stopped: text.includes('启动本地服务'),
                theme: text.includes('跟随系统'),
                updates: text.includes('更新由本地构建提供'),
                mains: document.querySelectorAll('main').length
            }})
        });
    })()""".replace("TOKEN", json.dumps(token)))
    try:
        return results.get(timeout=5)
    except queue.Empty as exc:
        raise AssertionError("Native renderer did not return its DOM snapshot") from exc


def smoke():
    import webview
    from desktop.controller import Controller
    from desktop.launcher import prepare_home
    from desktop.web_shell import ShellServer

    failures = []
    results = queue.Queue()

    class ProbeController(Controller):
        # Exists only in this disposable smoke-test process. The production
        # controller's action allowlist and all token/origin checks stay intact.
        def action(self, name, data=None):
            if name == "renderer_snapshot":
                results.put(data)
                return {"ok": True}
            return super().action(name, data)

    with tempfile.TemporaryDirectory(prefix="creatorhub-webview-") as temp:
        home = Path(temp)
        prepare_home(home)
        server = ShellServer(ProbeController(home, install_browser=False))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        window = webview.create_window("CreatorHub renderer test", server.origin, hidden=True)

        def verify():
            try:
                if not window.events.loaded.wait(45):
                    raise AssertionError("Native renderer did not finish loading the shell page")
                deadline = time.monotonic() + 45
                while time.monotonic() < deadline:
                    result = renderer_snapshot(window, server.token, results)
                    if result["stopped"]:
                        break
                    time.sleep(.3)
                else:
                    raise AssertionError("Desktop renderer did not load real stopped state: " + repr(result))
                window.run_js("location.hash = '#/settings'")
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    result = renderer_snapshot(window, server.token, results)
                    if result["theme"] and result["updates"]:
                        break
                    time.sleep(.3)
                else:
                    raise AssertionError("Desktop settings did not render: " + repr(result))
                assert result["mains"] == 1
            except Exception as exc:
                failures.append(str(exc))
            finally:
                window.destroy()

        try:
            webview.start(verify, **webview_options())
        finally:
            server.shutdown()
            server.server_close()
    if failures:
        raise AssertionError("; ".join(failures))
    print("PASS native desktop renderer and navigation")
    return 0
