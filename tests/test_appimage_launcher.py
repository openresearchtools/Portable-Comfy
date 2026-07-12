from __future__ import annotations

import os
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = REPO_ROOT / "packaging" / "appimage-launcher.sh"


def _launcher_environment(tmp_path: Path, overrides: dict[str, str]) -> dict[str, str]:
    appdir = tmp_path / "Portable-Comfy.AppDir"
    executable = appdir / "usr/lib/portable-comfy/portable-comfy"
    executable.parent.mkdir(parents=True)
    executable.symlink_to("/bin/sh")
    environment = {
        "PATH": os.environ["PATH"],
        "APPDIR": str(appdir),
        **overrides,
    }
    completed = subprocess.run(
        ["/bin/sh", str(LAUNCHER), "-c", "/usr/bin/env"],
        check=True,
        text=True,
        capture_output=True,
        env=environment,
    )
    return dict(line.split("=", 1) for line in completed.stdout.splitlines())


def test_appimage_launcher_uses_stable_rendering_defaults(tmp_path: Path) -> None:
    environment = _launcher_environment(
        tmp_path,
        {
            "GTK_PATH": "/snap/host/gtk",
            "GIO_EXTRA_MODULES": "/snap/host/gio",
            "GIO_MODULE_DIR": "/snap/host/gio-cache",
            "GI_TYPELIB_PATH": "/snap/host/types",
            "SNAP": "/snap/code/current",
            "SNAP_COOKIE": "host-sandbox-cookie",
            "SNAP_LIBRARY_PATH": "/snap/code/current/lib",
        },
    )

    assert environment["QT_QPA_PLATFORM"] == "xcb"
    assert environment["QT_XCB_GL_INTEGRATION"] == "none"
    assert environment["QT_QUICK_BACKEND"] == "software"
    assert environment["QTWEBENGINE_CHROMIUM_FLAGS"] == (
        "--disable-gpu --disable-gpu-compositing"
    )
    for name in (
        "GTK_PATH",
        "GIO_EXTRA_MODULES",
        "GIO_MODULE_DIR",
        "GI_TYPELIB_PATH",
        "SNAP",
        "SNAP_COOKIE",
        "SNAP_LIBRARY_PATH",
    ):
        assert name not in environment


def test_appimage_launcher_preserves_explicit_qt_overrides(tmp_path: Path) -> None:
    overrides = {
        "QT_QPA_PLATFORM": "wayland",
        "QT_XCB_GL_INTEGRATION": "xcb_glx",
        "QT_QUICK_BACKEND": "rhi",
        "QTWEBENGINE_CHROMIUM_FLAGS": "--enable-gpu",
    }
    environment = _launcher_environment(tmp_path, overrides)

    for name, value in overrides.items():
        assert environment[name] == value
