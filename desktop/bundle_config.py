"""Shared, explicit packaging inventory for Windows and macOS."""
from pathlib import Path

# Optional imports in Pillow, pywebview and download libraries must not pull
# unrelated toolkits/scientific stacks out of a developer's build environment.
EXCLUDED_MODULES = (
    "cv2", "numpy", "scipy", "pandas", "matplotlib", "IPython", "pytest",
    "PyQt5", "PyQt6", "PySide2", "PySide6", "qtpy", "cefpython3",
)


def dependency_options(target):
    options = []
    for package in EXCLUDED_MODULES:
        options += ["--exclude-module", package]
    for package in ("app", "xhshow", "uvicorn"):
        options += ["--collect-submodules", package]
    for package in ("app", "patchright", "xhshow", "imageio_ffmpeg", "tzdata", "webview"):
        options += ["--collect-data", package]
    # collect-all used to copy distribution metadata implicitly. Preserve
    # version information and upstream license files when collecting precisely.
    for distribution in ("patchright", "xhshow", "imageio-ffmpeg", "tzdata", "pywebview",
                         "Pillow", "pystray", "PyExecJS"):
        options += ["--copy-metadata", distribution]
    # CFFI/.NET loaders use native libraries resolved at runtime.
    for package in (("curl_cffi", "clr_loader", "pythonnet") if target == "windows" else ("curl_cffi",)):
        options += ["--collect-all", package]
    if target == "windows":
        options += ["--hidden-import", "pystray._win32", "--hidden-import", "webview.platforms.edgechromium"]
    else:
        options += ["--hidden-import", "pystray._darwin", "--hidden-import", "webview.platforms.cocoa",
                    "--exclude-module", "pythonnet", "--exclude-module", "clr_loader"]
    return options


def node_module_data(root):
    source = Path(root) / "node_modules" / "crypto-js"
    if not (source / "package.json").is_file():
        raise ValueError("Run npm ci before packaging the shared Node runtime dependencies")
    return source, "node_modules/crypto-js"
