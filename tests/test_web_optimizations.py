"""Run the actual UI functions with Node VM, never with a real browser."""
import os
from pathlib import Path
import shutil
import subprocess

import pytest


@pytest.mark.parametrize("zone", ["Asia/Shanghai", "America/New_York"])
@pytest.mark.parametrize("script", ["web_optimizations.cjs", "web_submissions.cjs", "web_appearance.cjs", "web_engine_settings.cjs", "web_content_provenance.cjs", "web_watch_provenance.cjs", "web_preview.cjs"])
def test_ui_behavior_offline(zone, script):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is not installed")
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [node, str(root / "tests" / script)],
        cwd=root, env={**os.environ, "TZ": zone}, capture_output=True,
        text=True, encoding="utf-8", timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
