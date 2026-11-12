"""Platform-independent checks; actual Cocoa/signature tests run on macOS CI."""
import json
from pathlib import Path
import plistlib
import queue
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import yaml

from desktop import build_macos, updates, webview_smoke
from desktop.package_report import package_report

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("arch", ["arm64", "x86_64"])
def test_macos_update_selects_matching_dmg_and_no_windows_delta(monkeypatch, arch):
    target = f"macos-{arch}"
    monkeypatch.setattr(updates, "platform_tag", lambda: target)
    names = [f"aispace-Setup-0.2.7-{target}.dmg", f"SHA256-{target}.txt",
             "aispace-Setup-0.2.7-windows-x64.exe"]
    data = {"tag_name": "v0.2.7", "assets": [
        {"name": name, "size": 1234, "state": "uploaded",
         "browser_download_url": f"{updates.RELEASES_URL}/download/v0.2.7/{name}"}
        for name in names]}
    info = updates.release_info(data, "0.2.6")
    assert info["asset_name"] == names[0]
    assert info["checksum_url"].endswith(names[1])
    assert info["status"] == "available"
    assert info["manifest_asset"] is None and info["delta_asset"] is None
    data["assets"] = data["assets"][2:]
    assert updates.release_info(data, "0.2.6")["status"] == "unavailable"


def test_package_report_distinguishes_compressed_and_expanded_bytes(tmp_path):
    bundle = tmp_path / "app"
    folder = bundle / "_internal" / "patchright"
    folder.mkdir(parents=True)
    (folder / "node").write_bytes(b"x" * 100)
    (bundle / "aispace.exe").write_bytes(b"y" * 50)
    installer = tmp_path / "setup.exe"
    installer.write_bytes(b"z" * 30)
    report = package_report(bundle, installer)
    assert report["bundle_bytes"] == 150
    assert report["installer_bytes"] == 30
    assert report["components"]["patchright"] == 100
    assert report["forbidden_files"] == []
    forbidden = bundle / "_internal" / "numpy"
    forbidden.mkdir()
    (forbidden / "library.pyd").touch()
    assert package_report(bundle)["forbidden_files"] == ["_internal/numpy/library.pyd"]


def test_workflow_builds_native_architectures_on_shared_tags_without_publishing():
    workflow = yaml.load((ROOT / ".github/workflows/macos-installer.yml").read_text(), Loader=yaml.BaseLoader)
    assert set(workflow["on"]) == {"push", "workflow_dispatch"}
    assert workflow["on"]["push"]["tags"] == ["v*"]
    assert workflow["concurrency"]["cancel-in-progress"] == "false"
    assert workflow["permissions"] == {"contents": "read"}
    job = workflow["jobs"]["macos"]
    matrix = job["strategy"]["matrix"]["include"]
    assert {entry["arch"] for entry in matrix} == {"arm64", "x86_64"}
    assert all("macos-" in entry["runner"] for entry in matrix)
    script = (ROOT / ".github/workflows/macos-installer.yml").read_text()
    assert "gh release" not in script
    assert "always() && env.SIGN_BUILD == 'true'" in script
    assert "vars.MACOS_SIGN_RELEASE == 'true'" in job["env"]["SIGN_BUILD"]
    assert "github.event_name == 'workflow_dispatch' && inputs.sign" in job["env"]["SIGN_BUILD"]
    steps = job["steps"]
    version_index = next(i for i, step in enumerate(steps) if step.get("name") == "Resolve version and prepare Chinese release notes")
    install_index = next(i for i, step in enumerate(steps) if step.get("name") == "Install build dependencies")
    assert version_index < install_index
    version_script = steps[version_index]["run"]
    assert "${GITHUB_REF_NAME#v}" in version_script
    assert 'desktop/release.py "$version" --notes-output' in version_script
    assert version_script.index("desktop/release.py") < version_script.index("GITHUB_ENV")
    signing = next(step for step in steps if step.get("name") == "Prepare isolated signing keychain")
    assert signing["if"] == "env.SIGN_BUILD == 'true'"
    upload = next(step for step in steps if step.get("uses", "").startswith("actions/upload-artifact@"))
    assert "RELEASE_NOTES.md" in upload["with"]["path"]


@pytest.mark.parametrize("status", ["Invalid", "In Progress"])
def test_notarization_must_be_accepted_not_just_exit_zero(monkeypatch, status):
    monkeypatch.setattr(build_macos.subprocess, "run", Mock(return_value=SimpleNamespace(stdout=json.dumps({"status": status}))))
    with pytest.raises(RuntimeError, match="not accept"):
        build_macos.notarize(Path("artifact.dmg"), "profile")


def test_macos_entitlements_only_allow_required_jit():
    with (ROOT / "desktop/entitlements.macos.plist").open("rb") as stream:
        assert plistlib.load(stream) == {"com.apple.security.cs.allow-jit": True}


def test_native_smoke_uses_csp_compatible_probe_without_return_value():
    results = queue.Queue()
    expected = {"stopped": True, "theme": False, "updates": False, "mains": 1}
    def run_js(script):
        assert "eval(" not in script
        assert '"sample-token"' in script
        assert "document.body.innerText" in script
        assert "X-Desktop-Token" in script
        assert "renderer_snapshot" in script
        results.put(expected)
        return None
    window = SimpleNamespace(run_js=run_js)
    assert webview_smoke.renderer_snapshot(window, "sample-token", results) == expected


def test_native_smoke_fails_if_dom_probe_never_returns():
    results = Mock()
    results.get.side_effect = queue.Empty
    with pytest.raises(AssertionError, match="DOM snapshot"):
        webview_smoke.renderer_snapshot(Mock(), "sample-token", results)
    results.get.assert_called_once_with(timeout=5)
