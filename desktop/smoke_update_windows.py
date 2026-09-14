"""Native updater handoff with disposable C# fixtures, no real installation.

Runs the frozen updater against a fake application and fake Setup executable.
The fixtures write only under build/desktop-update-native-qa; they have no UI,
registry entries, accounts, network access or task execution.
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from desktop.update_helper import atomic_json, clean_environment

APP_SOURCE = r'''
using System;
using System.IO;
using System.Threading;
class FixtureApp {
  static int Main(string[] args) {
    if (args.Length == 1 && args[0] == "--update-health-check") {
      string dir = Path.GetDirectoryName(System.Reflection.Assembly.GetExecutingAssembly().Location);
      return !Path.GetFileName(dir).StartsWith(".CreatorHub-next-") &&
        File.Exists(Path.Combine(dir, "_internal", "fail-installed-health.txt")) ? 3 : 0;
    }
    string runtime = Path.Combine(Environment.GetEnvironmentVariable("CREATORHUB_DESKTOP_HOME"), "runtime");
    if (args.Length == 1) {
      using (var file = new FileStream(Path.Combine(runtime, "desktop.lock"), FileMode.OpenOrCreate, FileAccess.ReadWrite, FileShare.ReadWrite)) {
        file.Lock(0, 1);
        File.WriteAllText(Path.Combine(runtime, "parent-started.txt"), "ready");
        var deadline = DateTime.UtcNow.AddSeconds(40);
        while (!File.Exists(args[0])) {
          if (DateTime.UtcNow > deadline) return 2;
          Thread.Sleep(50);
        }
        file.Unlock(0, 1);
      }
      return 0;
    }
    File.WriteAllText(Path.Combine(runtime, "restarted.txt"), "launcher-only");
    return 0;
  }
}
'''

SETUP_SOURCE = r'''
using System;
using System.IO;
using System.Reflection;
class FixtureSetup {
  static int Main(string[] args) {
    string stage = Path.GetDirectoryName(Assembly.GetExecutingAssembly().Location);
    try {
      string dir = Array.Find(args, a => a.StartsWith("/DIR=", StringComparison.Ordinal)).Substring(5);
      string log = Array.Find(args, a => a.StartsWith("/LOG=", StringComparison.Ordinal)).Substring(5);
      // Windows rejects this overwrite if the original app process is still alive.
      File.Copy(Path.Combine(stage, "replacement.exe"), Path.Combine(dir, "CreatorHub.exe"), true);
      File.WriteAllText(Path.Combine(dir, "_internal", "desktop-version.txt"), "0.3.0");
      File.WriteAllLines(log, args);
      return 0;
    } catch (Exception e) {
      File.WriteAllText(Path.Combine(stage, "fixture-error.txt"), e.ToString());
      return 5;
    }
  }
}
'''


def wait_file(path, seconds=30):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if path.is_file():
            return
        time.sleep(.1)
    raise RuntimeError(f"Fixture timeout: {path.name}")


def main():
    if os.name != "nt":
        raise SystemExit("Windows-only updater smoke test")
    updater = Path(sys.argv[1]).resolve(strict=True)
    if "--delta" in sys.argv[2:]:
        return delta_smoke(updater)
    compiler = Path(os.environ.get("WINDIR", "C:/Windows")) / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
    if not compiler.is_file():
        raise SystemExit(".NET Framework C# compiler required for native update fixtures")
    base = ROOT / "build" / "desktop-update-native-qa"
    base.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="case-", dir=base) as temp:
        work = Path(temp).resolve()
        assert work.is_relative_to(base.resolve())
        home, app = work / "用户 data [fixture]", work / "应用 folder [fixture]"
        stage = home / "runtime" / "updates" / ("update-" + "b" * 32)
        stage.mkdir(parents=True)
        (home / "logs").mkdir()
        (home / "data").mkdir()
        (home / "data" / "preserve.txt").write_text("unchanged", encoding="utf-8")
        (app / "_internal").mkdir(parents=True)
        (app / "_internal" / "desktop-version.txt").write_text("0.2.0", encoding="utf-8")
        installer = stage / "CreatorHub-Setup-0.3.0-windows-x64.exe"
        for name, source, target in (("App", APP_SOURCE, app / "CreatorHub.exe"),
                                     ("Setup", SETUP_SOURCE, installer)):
            file = work / (name + ".cs")
            file.write_text(source, encoding="utf-8-sig")
            subprocess.run([str(compiler), "/nologo", "/target:winexe", f"/out:{target}", str(file)],
                           check=True, capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
        shutil.copy2(app / "CreatorHub.exe", stage / "replacement.exe")
        copied = stage / "CreatorHubUpdater.exe"
        shutil.copy2(updater, copied)
        env = {**clean_environment(), "CREATORHUB_DESKTOP_HOME": str(home)}
        subprocess.run([str(copied), "--self-test"], env=env, check=True, timeout=30,
                       creationflags=subprocess.CREATE_NO_WINDOW)
        parent = subprocess.Popen([str(app / "CreatorHub.exe"), str(stage / "ready.json")],
                                  env=env, creationflags=subprocess.CREATE_NO_WINDOW)
        child = None
        try:
            wait_file(home / "runtime" / "parent-started.txt")
            request = stage / "request.json"
            atomic_json(request, {"schema": 1, "home": str(home), "install_dir": str(app),
                "parent_pid": parent.pid, "installer": str(installer), "version": "0.3.0",
                "size": installer.stat().st_size, "sha256": hashlib.sha256(installer.read_bytes()).hexdigest()})
            child = subprocess.Popen([str(copied), "--request", str(request)], cwd=stage,
                                     env=env, creationflags=subprocess.CREATE_NO_WINDOW)
            assert child.wait(timeout=45) == 0, "Frozen updater failed"
            assert parent.wait(timeout=5) == 0, "Parent did not exit after acknowledgment"
            wait_file(home / "runtime" / "restarted.txt", seconds=10)
            result = json.loads((home / "runtime" / "update-result.json").read_text(encoding="utf-8"))
            assert result["status"] == "installed", result
            assert (app / "_internal" / "desktop-version.txt").read_text() == "0.3.0"
            assert (home / "data" / "preserve.txt").read_text() == "unchanged"
            log = (home / "logs" / (stage.name + "-setup.log")).read_text(encoding="utf-8-sig")
            assert "/NORESTART" in log and "/NOCLOSEAPPLICATIONS" in log
            assert (home / "runtime" / "restarted.txt").read_text() == "launcher-only"
            print("PASS frozen updater: native parent handle, ready/exit handshake, exclusive locks, SHA-256, Unicode/spaced paths, literal installer args, file replacement and launcher restart; no real install or account data")
        except Exception:
            for path in (stage / "fixture-error.txt", stage / "failed.json", home / "runtime" / "update-result.json"):
                if path.is_file():
                    print(path.read_text(encoding="utf-8", errors="replace"))
            raise
        finally:
            (stage / "cancel").touch()
            for process in (child, parent):
                if process and process.poll() is None:
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        process.terminate()
                        process.wait(timeout=5)


def delta_smoke(updater):
    from desktop.build_update import build_update
    from desktop.update_manifest import manifest_name, hash_file, verify_tree
    compiler = Path(os.environ.get("WINDIR", "C:/Windows")) / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
    base = ROOT / "build/desktop-update-native-qa"
    base.mkdir(parents=True, exist_ok=True)
    for fail_health in (False, True):
        with tempfile.TemporaryDirectory(prefix="delta-", dir=base) as temp:
            root = Path(temp).resolve()
            assert root.is_relative_to(base.resolve())
            app, new, home = root / "应用 folder [fixture]", root / "new", root / "用户 data"
            (app / "_internal").mkdir(parents=True)
            (app / "_internal/desktop-version.txt").write_text("0.2.0")
            (app / "_internal/build-runtime.json").write_text('{"runtime":"C# native fixture"}')
            (app / "_internal/dependency.dat").write_bytes(b"unchanged" * 10000)
            source = root / "App.cs"
            source.write_text(APP_SOURCE, encoding="utf-8-sig")
            subprocess.run([str(compiler), "/nologo", "/target:winexe", f"/out:{app / 'CreatorHub.exe'}", str(source)],
                           check=True, capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
            baseline = build_update(app, root / "baseline", "0.2.0")
            shutil.copytree(app, new)
            # A legal PE overlay changes the executable digest without changing the fixture's behavior.
            with (new / "CreatorHub.exe").open("ab") as stream:
                stream.write(b"\nCreatorHub delta executable fixture\n")
            (new / "_internal/desktop-version.txt").write_text("0.2.1")
            (new / "_internal/new-code.js").write_text("// updated code fixture")
            if fail_health:
                (new / "_internal/fail-installed-health.txt").write_text("fixture")
            release = root / "release"
            manifest = build_update(new, release, "0.2.1", baseline)
            stage = home / "runtime/updates" / ("update-" + ("e" if fail_health else "d") * 32)
            stage.mkdir(parents=True)
            (home / "logs").mkdir()
            (home / "preserve.txt").write_text("user data unchanged")
            package = stage / manifest["delta"]["asset_name"]
            meta = stage / manifest_name("0.2.1")
            shutil.copy2(release / package.name, package)
            shutil.copy2(release / meta.name, meta)
            copied = stage / "CreatorHubUpdater.exe"
            shutil.copy2(updater, copied)
            env = {**clean_environment(), "CREATORHUB_DESKTOP_HOME": str(home)}
            parent = subprocess.Popen([str(app / "CreatorHub.exe"), str(stage / "ready.json")],
                                      env=env, creationflags=subprocess.CREATE_NO_WINDOW)
            child = None
            try:
                wait_file(home / "runtime/parent-started.txt")
                request = stage / "request.json"
                atomic_json(request, {"schema": 1, "home": str(home), "install_dir": str(app),
                    "parent_pid": parent.pid, "installer": str(package), "kind": "delta",
                    "version": "0.2.1", "from_version": "0.2.0", "size": package.stat().st_size,
                    "sha256": hash_file(package), "manifest": str(meta), "manifest_sha256": hash_file(meta)})
                child = subprocess.Popen([str(copied), "--request", str(request)], cwd=stage,
                                         env=env, creationflags=subprocess.CREATE_NO_WINDOW)
                code = child.wait(timeout=90)
                assert code == (1 if fail_health else 0), (code, list((home / "logs").glob("*")))
                assert parent.wait(timeout=5) == 0
                wait_file(home / "runtime/restarted.txt", seconds=10)
                verify_tree(app, baseline["files"] if fail_health else manifest["files"])
                result = json.loads((home / "runtime/update-result.json").read_text())
                assert result["status"] == ("install_error" if fail_health else "installed"), result
                assert (home / "preserve.txt").read_text() == "user data unchanged"
                print("PASS frozen file delta:", "failed health -> previous tree restored" if fail_health else
                      "changed executable/resources -> reuse dependency -> stage/swap/health/restart")
            except Exception:
                for path in (home / "logs").glob("*-helper.log"):
                    print(path.read_text(encoding="utf-8", errors="replace"))
                raise
            finally:
                (stage / "cancel").touch()
                for process in (child, parent):
                    if process and process.poll() is None:
                        process.terminate()
                        process.wait(timeout=10)


if __name__ == "__main__":
    main()
