"""Exercise the packaged executable without touching user accounts or data."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid


def main():
    executable = Path(sys.argv[1]).resolve()
    source = Path(__file__).resolve().parents[1]
    # Check both renderers and the offline guide, not just importability. A stale
    # local build must not silently ship the previous release's desktop styles.
    for name in ("index.html", "app.js", "app.css", "icons.svg", "THIRD-PARTY-NOTICES.txt"):
        bundled = executable.parent / "_internal" / "desktop" / "web" / name
        assert bundled.read_bytes() == (source / "desktop" / "web" / name).read_bytes(), \
            f"Frozen desktop asset differs from the current build: {name}"
    with tempfile.TemporaryDirectory(prefix="creatorhub-smoke-") as temp:
        home = Path(temp)
        env = {**os.environ, "CREATORHUB_DESKTOP_HOME": temp, "PYTHONUTF8": "1"}
        try:
            subprocess.run([str(executable), "--smoke-test"], env=env, check=True, timeout=120)
            subprocess.run([str(executable), "--shell-smoke-test"], env=env, check=True, timeout=120)
        except Exception:
            for log in (home / "logs").glob("*.log"):
                print(log.read_text(encoding="utf-8", errors="replace")[-12000:])
            raise
        # Force a port conflict without stopping another service.
        occupied = socket.socket()
        try:
            occupied.bind(("127.0.0.1", 8000))
            occupied.listen()
        except OSError:
            occupied.close()
            occupied = None
        session = uuid.uuid4().hex
        child = subprocess.Popen([str(executable), "--serve", "--session", session, "--skip-browser-install"], env=env)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            deadline = time.monotonic() + 120
            ready = False
            while time.monotonic() < deadline:
                if child.poll() is not None:
                    raise RuntimeError(f"Service exited early: {child.returncode}")
                path = home / "runtime" / f"{session}.json"
                if path.exists():
                    port = json.loads(path.read_text(encoding="utf-8"))["port"]
                    assert port != 8000, "Port collision fallback did not occur"
                    url = f"http://127.0.0.1:{port}"
                    try:
                        with opener.open(url + "/_desktop/ready", timeout=1) as response:
                            ready = json.load(response) == {"session": session}
                    except OSError:
                        pass
                    if ready:
                        break
                time.sleep(.25)
            assert ready, "Service did not become ready"
            duplicate = subprocess.run([str(executable), "--serve", "--session", uuid.uuid4().hex,
                                        "--skip-browser-install"], env=env, timeout=15)
            assert duplicate.returncode != 0, "A duplicate frozen service used the same data directory"
            web = source / "app" / "web"
            assets = ("app.js", "engine-settings.js", "workbench.js", "workbench.css",
                      "appearance.js", "appearance.css", "douyin-dm.js", "submissions.js")
            for path in ("/", "/health", "/guide/", "/guide/xhs/", "/guide/douyin/", "/guide/guide.css",
                         *(f"/static/{name}" for name in assets)):
                with opener.open(url + path, timeout=5) as response:
                    assert response.status == 200, path
                    if path == "/":
                        assert b'data-guide-base="/guide/"' in response.read()
                    elif path == "/guide/guide.css":
                        assert response.read() == (source / "guide" / "guide.css").read_bytes(), \
                            "Frozen guide stylesheet differs from the current build"
                    elif path.startswith("/static/"):
                        assert response.read() == (web / path.rsplit("/", 1)[1]).read_bytes(), \
                            f"Frozen workbench asset differs from the current build: {path}"
            with opener.open(url + "/openapi.json", timeout=5) as response:
                paths = json.load(response)["paths"]
                assert "/api/douyin/dm/preview" in paths
                assert "/api/douyin/dm/batches" in paths
            with opener.open(url + "/api/license", timeout=5) as response:
                assert not json.load(response)["valid"], "Empty test data must require activation"
            (home / "runtime" / f"{session}.stop").touch()
            assert child.wait(timeout=35) == 0, "Service shutdown failed"
            assert (home / "data" / "creatorhub.db").is_file()
            print("PASS: frozen imports, bundled resources, isolated database, port fallback, HTTP routes and graceful stop")
        except Exception:
            for log in (home / "logs").glob("*.log"):
                print(log.read_text(encoding="utf-8", errors="replace")[-12000:])
            raise
        finally:
            if child.poll() is None:
                subprocess.run(["taskkill", "/PID", str(child.pid), "/T", "/F"], capture_output=True)
                child.wait(timeout=20)
            if occupied:
                occupied.close()


if __name__ == "__main__":
    main()
