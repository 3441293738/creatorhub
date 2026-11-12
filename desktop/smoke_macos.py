"""Isolated macOS bundle smoke test; no browser download or account login."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.processes import child_options, terminate_owned


def main():
    bundle = Path(sys.argv[1]).resolve(strict=True)
    executable = bundle / "Contents/MacOS/aispace"
    with tempfile.TemporaryDirectory(prefix="creatorhub-macos-smoke-") as directory:
        home = Path(directory)
        env = {**os.environ, "CREATORHUB_DESKTOP_HOME": str(home), "PYTHONUTF8": "1"}
        try:
            subprocess.run([str(executable), "--update-health-check"], env={**env, "PATH": ""}, check=True, timeout=120)
            subprocess.run([str(executable), "--shell-smoke-test"], env=env, check=True, timeout=120)
            session = uuid.uuid4().hex
            child = subprocess.Popen([str(executable), "--serve", "--session", session], env=env, **child_options())
            try:
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                deadline = time.monotonic() + 90
                while time.monotonic() < deadline:
                    if child.poll() is not None:
                        raise RuntimeError("macOS service exited before readiness")
                    try:
                        port = json.loads((home / "runtime" / f"{session}.json").read_text())["port"]
                        with opener.open(f"http://127.0.0.1:{port}/_desktop/ready", timeout=1) as response:
                            if json.load(response)["session"] == session:
                                break
                    except (OSError, ValueError, KeyError):
                        pass
                    time.sleep(.2)
                else:
                    raise RuntimeError("macOS service readiness timed out")
                assert not (home / "browsers").exists(), "Opening the workbench downloaded a browser"
                (home / "runtime" / f"{session}.stop").touch()
                assert child.wait(timeout=35) == 0
            finally:
                if child.poll() is None:
                    terminate_owned(child)
        except Exception:
            for log in (home / "logs").glob("*.log"):
                print(log.read_text(encoding="utf-8", errors="replace")[-12000:])
            raise


if __name__ == "__main__":
    main()
