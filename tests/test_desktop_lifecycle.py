"""Regression coverage for independent service locking and launcher loss."""
import inspect
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import urllib.request
from desktop.launcher import ROOT, prepare_home, InstanceLock, child_command, main
from desktop.web_shell import run_desktop


class DesktopLifecycleTests(unittest.TestCase):
    def test_default_entry_is_manual(self):
        self.assertFalse(inspect.signature(run_desktop).parameters["autostart"].default)
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {"CREATORHUB_DESKTOP_HOME": temp}), patch.object(sys, "argv", ["launcher.py"]), patch("desktop.web_shell.run_desktop") as show:
            self.assertEqual(main(), 0)
            self.assertNotIn("autostart", show.call_args.kwargs)
            self.assertFalse((Path(temp) / "browsers").exists())
            self.assertFalse((Path(temp) / "data" / "creatorhub.db").exists())

    def test_service_lock_is_independent_of_window_lock(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp); prepare_home(home)
            first = InstanceLock(home, "service.lock")
            window = InstanceLock(home)
            try:
                with self.assertRaises((RuntimeError, OSError)):
                    InstanceLock(home, "service.lock")
            finally:
                first.close(); window.close()

    def test_parent_loss_stops_child_and_duplicate_service_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp); prepare_home(home)
            env = {**os.environ, "CREATORHUB_DESKTOP_HOME": temp}
            # Own inert parent fixture; no user process is terminated.
            parent = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(120)"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            session = "a" * 32
            child = subprocess.Popen(child_command("--serve", "--session", session, "--parent-pid", str(parent.pid), "--skip-browser-install"), cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                info = home / "runtime" / f"{session}.json"
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                deadline = time.monotonic() + 45
                while time.monotonic() < deadline:
                    if child.poll() is not None: self.fail("Service exited before readiness")
                    if info.exists():
                        url = "http://127.0.0.1:" + str(json.loads(info.read_text())["port"])
                        try:
                            with opener.open(url + "/_desktop/ready", timeout=1): break
                        except OSError: pass
                    time.sleep(.1)
                else: self.fail("Service startup timed out")
                with opener.open(url, timeout=3) as response:
                    self.assertIn(b'data-guide-base="https://3441293738.github.io/creatorhub/guide/"', response.read())
                duplicate = subprocess.run(child_command("--serve", "--session", "b" * 32, "--skip-browser-install"), cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=12)
                self.assertNotEqual(duplicate.returncode, 0)
                self.assertFalse((home / "runtime" / ("b" * 32 + ".json")).exists())
                parent.terminate(); parent.wait(5)
                self.assertEqual(child.wait(timeout=40), 0)
                self.assertFalse(info.exists())
                reopened = InstanceLock(home, "service.lock"); reopened.close()
            finally:
                if parent.poll() is None: parent.terminate(); parent.wait(5)
                if child.poll() is None:
                    (home / "runtime" / f"{session}.stop").touch()
                    try: child.wait(40)
                    except subprocess.TimeoutExpired: child.kill(); child.wait(5)

    @unittest.skipUnless(os.name == "nt", "Windows process-tree fallback")
    def test_parent_loss_during_stalled_startup_is_bounded(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp); prepare_home(home)
            parent = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(120)"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            code = "import sys,time;from pathlib import Path;import desktop.launcher as l;l._serve=lambda *args:time.sleep(120);l.serve(Path(sys.argv[1]),'c'*32,False,int(sys.argv[2]))"
            child = subprocess.Popen([sys.executable, "-c", code, temp, str(parent.pid)], cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                time.sleep(1)
                parent.terminate(); parent.wait(5)
                self.assertIsNotNone(child.wait(timeout=40))
                self.assertTrue((home / "runtime" / ("c" * 32 + ".stop")).exists())
                reopened = InstanceLock(home, "service.lock"); reopened.close()
            finally:
                if parent.poll() is None: parent.terminate(); parent.wait(5)
                if child.poll() is None: child.kill(); child.wait(5)


if __name__ == "__main__": unittest.main()
