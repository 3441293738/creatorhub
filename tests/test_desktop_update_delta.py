"""Synthetic release trees; no real installs, user data, registry or network."""
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import Mock, patch
import zipfile

from desktop.build_update import build_update
from desktop.update_manifest import (file_path, manifest_name, parse_manifest,
                                    verify_tree, changes, hash_file)
from desktop.update_delta import apply_delta, read_journal, rollback, atomic_record
from desktop.update_helper import read_request, atomic_json, apply_update
from desktop.updates import UpdateChecker, release_info, RELEASES_URL
from test_desktop_update_download import Response


def bundle(root, version):
    (root / "_internal/app/web").mkdir(parents=True)
    (root / "CreatorHub.exe").write_bytes(b"MZ-fixture-not-an-executable")
    (root / "_internal/desktop-version.txt").write_text(version, encoding="utf-8")
    (root / "_internal/build-runtime.json").write_text('{"python":"fixture","pyinstaller":"fixture"}')
    (root / "_internal/app/web/app.js").write_bytes(b"old-ui")
    (root / "_internal/dependency.dll").write_bytes(b"unchanged dependency" * 2000)
    (root / "_internal/removed.txt").write_text("obsolete")


def fixture(root):
    old, new, release = root / "app", root / "build", root / "release"
    bundle(old, "0.2.0")
    base = build_update(old, root / "baseline", "0.2.0")
    shutil.copytree(old, new)
    (new / "_internal/desktop-version.txt").write_text("0.2.1")
    (new / "CreatorHub.exe").write_bytes(b"MZ-fixture-updated-python-code")
    (new / "_internal/app/web/app.js").write_bytes(b"new-ui")
    (new / "_internal/added.py").write_text("NEW_CODE = True")
    (new / "_internal/removed.txt").unlink()
    manifest = build_update(new, release, "0.2.1", base)
    home = root / "data-home"
    stage = home / "runtime/updates" / ("update-" + "c" * 32)
    stage.mkdir(parents=True)
    (home / "logs").mkdir()
    (home / "account-sentinel.txt").write_text("unchanged account fixture")
    package = stage / manifest["delta"]["asset_name"]
    shutil.copyfile(release / package.name, package)
    meta = stage / manifest_name("0.2.1")
    shutil.copyfile(release / meta.name, meta)
    request = stage / "request.json"
    atomic_json(request, {"schema": 1, "home": str(home), "install_dir": str(old),
        "parent_pid": os.getpid(), "installer": str(package), "kind": "delta",
        "version": "0.2.1", "from_version": "0.2.0", "size": package.stat().st_size,
        "sha256": hash_file(package), "manifest": str(meta), "manifest_sha256": hash_file(meta)})
    return home, old, new, release, manifest, request


class DeltaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.home, self.app, self.new, self.release, self.manifest, self.request = fixture(self.root)
        self.data = read_request(self.request)

    def tearDown(self):
        self.assertEqual((self.home / "account-sentinel.txt").read_text(), "unchanged account fixture")
        self.temp.cleanup()

    def test_delta_contains_changed_code_and_new_files_not_unchanged_dependency(self):
        with zipfile.ZipFile(self.data["installer"]) as archive:
            self.assertEqual(set(archive.namelist()), {"CreatorHub.exe", "_internal/desktop-version.txt",
                "_internal/app/web/app.js", "_internal/added.py"})
        changed, removed = changes(self.manifest["delta"]["base_files"], self.manifest["files"])
        self.assertEqual(removed, ["_internal/removed.txt"])
        self.assertLess(self.manifest["delta"]["size"], sum(f["size"] for f in self.manifest["files"]))
        self.assertEqual(len(changed), 4)

    def test_initial_release_and_runtime_change_have_inventory_but_no_delta(self):
        base = parse_manifest((self.root / "baseline" / manifest_name("0.2.0")).read_bytes())
        self.assertNotIn("delta", base)
        (self.new / "_internal/build-runtime.json").write_text("different runtime")
        result = build_update(self.new, self.root / "different", "0.2.1", base)
        self.assertNotIn("delta", result)

    def test_malformed_windows_paths_and_manifest_versions_are_rejected(self):
        for path in ("../config.yaml", "_internal/../../file", "C:/file", "_internal\\file", "_internal/NUL.txt",
                     "_internal/a:stream", "_internal/a.", "_internal/a ", "_internal//x", "data/config.yaml"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                file_path(path)
        for transform in (lambda m: m.update(schema=99), lambda m: m.update(version="0.2.2"),
                          lambda m: m["files"].append(m["files"][0]),
                          lambda m: m["delta"].update(asset_name="other.zip")):
            value = copy.deepcopy(self.manifest)
            transform(value)
            with self.assertRaises(ValueError):
                parse_manifest(json.dumps(value).encode(), "0.2.1")

    def test_success_switches_complete_tree_preserves_previous_and_uninstaller(self):
        (self.app / "unins000.exe").write_bytes(b"uninstaller-fixture")
        (self.app / "personal-note.txt").write_text("preserved in previous tree")
        with patch("desktop.update_delta.health_check") as health, patch("desktop.update_delta.update_display_version"):
            apply_delta(self.data, {})
        self.assertEqual(health.call_count, 2)
        verify_tree(self.app, self.manifest["files"])
        self.assertFalse((self.app / "_internal/removed.txt").exists())
        self.assertTrue((self.app / "unins000.exe").is_file())
        journal = read_journal(self.home)
        self.assertEqual(journal["phase"], "committed")
        self.assertEqual((journal["previous"] / "personal-note.txt").read_text(), "preserved in previous tree")
        verify_tree(journal["previous"], self.manifest["delta"]["base_files"])

    def test_failed_preflight_or_post_switch_health_keeps_old_version(self):
        for scenario in ("before", "after"):
            with tempfile.TemporaryDirectory(dir=self.root) as temp:
                home, app, _, _, manifest, request = fixture(Path(temp))
                data = read_request(request)
                failures = [RuntimeError("bad startup")] if scenario == "before" else [None, RuntimeError("bad startup")]
                with patch("desktop.update_delta.health_check", side_effect=failures), self.assertRaises(RuntimeError):
                    apply_delta(data, {})
                verify_tree(app, manifest["delta"]["base_files"])
                if scenario == "after":
                    self.assertEqual(read_journal(home)["phase"], "rolled_back")

    def test_second_rename_failure_rolls_back(self):
        rename = Path.rename
        def fail_candidate(path, target):
            if path.name.startswith(".CreatorHub-next-"):
                raise PermissionError("Windows file lock fixture")
            return rename(path, target)
        with patch("desktop.update_delta.health_check"), patch.object(Path, "rename", fail_candidate), self.assertRaises(PermissionError):
            apply_delta(self.data, {})
        verify_tree(self.app, self.manifest["delta"]["base_files"])
        self.assertEqual(read_journal(self.home)["phase"], "rolled_back")

    def test_changed_baseline_and_unexpected_archive_files_prevent_switch(self):
        (self.app / "_internal/dependency.dll").write_bytes(b"modified")
        with patch("desktop.update_delta.health_check") as health, self.assertRaises(ValueError):
            apply_delta(self.data, {})
        health.assert_not_called()
        self.assertFalse((self.home / "runtime/update-transaction.json").exists())
        shutil.copyfile(self.new / "_internal/dependency.dll", self.app / "_internal/dependency.dll")
        with zipfile.ZipFile(self.data["installer"], "a") as archive:
            archive.writestr("../outside.txt", b"rejected")
        with self.assertRaises(ValueError):
            apply_delta(self.data, {})
        self.assertFalse((self.root / "outside.txt").exists())
        verify_tree(self.app, self.manifest["delta"]["base_files"])

    def test_recovery_after_old_directory_saved(self):
        attempt = self.data["attempt"]
        previous = self.app.parent / (".CreatorHub-previous-" + attempt)
        candidate = self.app.parent / (".CreatorHub-next-" + attempt)
        self.app.rename(previous)
        atomic_record(self.home / "runtime/update-transaction.json", {"schema": 1, "attempt": attempt,
            "phase": "old_saved", "install_dir": str(self.app), "previous": str(previous),
            "candidate": str(candidate), "version": "0.2.1"})
        rollback(self.home, read_journal(self.home))
        verify_tree(self.app, self.manifest["delta"]["base_files"])

    def test_helper_delta_route_uses_locks_and_never_runs_inno(self):
        with patch("desktop.update_helper.ParentProcess", return_value=Mock()), \
             patch("desktop.update_delta.health_check"), patch("desktop.update_delta.update_display_version"), \
             patch("desktop.update_helper.run_installer") as installer, patch("desktop.update_helper.restart_app"):
            self.assertEqual(apply_update(self.request), 0)
        installer.assert_not_called()
        result = json.loads((self.home / "runtime/update-result.json").read_text())
        self.assertEqual(result["status"], "installed")
        verify_tree(self.app, self.manifest["files"])

    def release_info(self):
        name = "CreatorHub-Setup-0.2.1-windows-x64.exe"
        files = {p.name: p.read_bytes() for p in self.release.iterdir()}
        files[name] = b"fixture full installer" * 1000
        raw = {"tag_name": "v0.2.1", "assets": []}
        for filename, payload in files.items():
            if filename == "SHA256.txt":
                continue  # Test GitHub digest-only trust path for all assets.
            raw["assets"].append({"name": filename, "size": len(payload), "state": "uploaded",
                "digest": "sha256:" + hashlib.sha256(payload).hexdigest(),
                "browser_download_url": f"{RELEASES_URL}/download/v0.2.1/{filename}"})
        return release_info(raw, "0.2.0"), files

    def test_client_selects_smaller_delta_and_downloads_only_changed_files(self):
        info, files = self.release_info()
        checker = UpdateChecker("0.2.0", self.home / "runtime/updates", self.app)
        urls = []
        def source(url, **kwargs):
            urls.append(url)
            return Response(files[url.rsplit("/", 1)[-1]])
        with patch("desktop.updates.fetch_release", return_value=info), patch("desktop.updates.open_asset", side_effect=source):
            checker.run()
            self.assertEqual(checker.state()["download_kind"], "delta")
            checker.download("v0.2.1")
            checker.worker.join(5)
        self.assertEqual(checker.state()["status"], "downloaded")
        self.assertEqual(checker.artifact["kind"], "delta")
        self.assertFalse(any("Setup-" in url for url in urls))

    def test_client_falls_back_when_local_code_changed(self):
        info, files = self.release_info()
        (self.app / "CreatorHub.exe").write_bytes(b"locally edited Python bundle")
        checker = UpdateChecker("0.2.0", self.home / "runtime/updates", self.app)
        with patch("desktop.updates.fetch_release", return_value=info), \
             patch("desktop.updates.open_asset", side_effect=lambda url: Response(files[url.rsplit('/', 1)[-1]])):
            checker.run()
            self.assertEqual(checker.state()["download_kind"], "full")
            self.assertIn("fallback_reason", checker.state())

    def test_delta_corruption_after_check_downloads_verified_full_installer(self):
        info, files = self.release_info()
        checker = UpdateChecker("0.2.0", self.home / "runtime/updates", self.app)
        corrupt = False
        def source(url, **kwargs):
            name = url.rsplit('/', 1)[-1]
            body = files[name]
            return Response(b'x' * len(body) if corrupt and name.endswith('.zip') else body)
        with patch("desktop.updates.fetch_release", return_value=info), patch("desktop.updates.open_asset", side_effect=source):
            checker.run()
            self.assertEqual(checker.state()["download_kind"], "delta")
            corrupt = True
            checker.download("v0.2.1")
            checker.worker.join(5)
        self.assertEqual(checker.state()["status"], "downloaded")
        self.assertEqual(checker.artifact["kind"], "full")
        self.assertIn("fallback_reason", checker.state())

    def test_v010_without_manifest_establishes_new_baseline_without_delta(self):
        from desktop.fetch_update_base import fetch_base
        from test_desktop_updates import release
        output = self.root / "stale-base.json"
        output.write_text("previous attempt must not be reused")
        info = release_info(release("v0.1.0"), "0.2.0")
        with patch("desktop.fetch_update_base.fetch_release", return_value=info), \
             patch("desktop.fetch_update_base.fetch_manifest") as fetch:
            self.assertFalse(fetch_base("0.2.0", output))
        self.assertFalse(output.exists())
        fetch.assert_not_called()

    def test_failed_delta_helper_prefers_full_without_touching_old_files(self):
        with patch("desktop.update_helper.ParentProcess", return_value=Mock()), \
             patch("desktop.update_delta.health_check", side_effect=RuntimeError("fixture failure")), \
             patch("desktop.update_helper.restart_app"):
            self.assertEqual(apply_update(self.request), 1)
        result = json.loads((self.home / "runtime/update-result.json").read_text())
        self.assertTrue(result["prefer_full"])
        verify_tree(self.app, self.manifest["delta"]["base_files"])


if __name__ == "__main__":
    unittest.main()
