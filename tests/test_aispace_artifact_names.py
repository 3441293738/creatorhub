from pathlib import Path
from unittest.mock import patch

import pytest

from desktop.update_manifest import installed_executable, file_path, validate_files, manifest_name, delta_name
from desktop.update_helper import restart_app
from desktop.updates import UpdateChecker


def test_new_artifact_names_and_legacy_recovery(tmp_path):
    legacy = tmp_path / "CreatorHub.exe"
    legacy.touch()
    assert installed_executable(tmp_path) == legacy
    current = tmp_path / "aispace.exe"
    current.touch()
    assert installed_executable(tmp_path) == current
    with patch("desktop.update_helper.subprocess.Popen") as launch:
        restart_app({"home": tmp_path, "executable": legacy})
    assert launch.call_args.args[0] == [str(current), "--no-autostart"]
    assert manifest_name("0.2.7.2") == "aispace-Update-0.2.7.2-windows-x64.json"
    assert delta_name("0.2.7.1", "0.2.7.2").startswith("aispace-Delta-")


def test_manifest_supports_legacy_baseline_but_rejects_two_entrypoints():
    def entry(name):
        return {"path": name, "size": 1, "sha256": "0" * 64}
    for executable in ("CreatorHub.exe", "aispace.exe"):
        validate_files([entry(executable), entry("_internal/desktop-version.txt")])
    with pytest.raises(ValueError):
        validate_files([entry("CreatorHub.exe"), entry("aispace.exe"), entry("_internal/desktop-version.txt")])
    with pytest.raises(ValueError):
        file_path("unrelated.exe")


def test_commercial_online_updates_stay_disabled(tmp_path):
    checker = UpdateChecker("0.2.7.2", tmp_path)
    with pytest.raises(ValueError, match="本地商业验证版"):
        checker.check()


def test_rename_upgrade_requires_full_installer(tmp_path):
    from desktop.build_update import build_update
    import json
    bundles = []
    for executable, version in (("CreatorHub.exe", "0.2.7.1"), ("aispace.exe", "0.2.7.2")):
        root = tmp_path / version
        (root / "_internal").mkdir(parents=True)
        (root / executable).write_bytes(b"mock-executable")
        (root / "_internal/desktop-version.txt").write_text(version)
        (root / "_internal/build-runtime.json").write_text('{}')
        bundles.append(root)
    output = tmp_path / "output"
    build_update(bundles[0], output, "0.2.7.1")
    build_update(bundles[1], output, "0.2.7.2", output / manifest_name("0.2.7.1"))
    manifest = json.loads((output / manifest_name("0.2.7.2")).read_text())
    assert "delta" not in manifest
