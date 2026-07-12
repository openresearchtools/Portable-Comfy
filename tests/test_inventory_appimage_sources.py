from __future__ import annotations

import csv
import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts/inventory_appimage_sources.py"
SPEC = importlib.util.spec_from_file_location("inventory_appimage_sources", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)
DebianOwner = MODULE.DebianOwner
FrozenSource = MODULE.FrozenSource
SourceRoots = MODULE.SourceRoots
write_inventory = MODULE.write_inventory


def _file(path: Path, text: str = "input\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _roots(tmp_path: Path) -> SourceRoots:
    roots = SourceRoots(
        launcher_venv=tmp_path / "launcher-venv",
        portable_python=tmp_path / "portable-python",
        pyinstaller_work=tmp_path / "build/pyinstaller-work",
        build_root=tmp_path / "build",
        repository=tmp_path / "repository",
    )
    for root in vars(roots).values():
        root.mkdir(parents=True, exist_ok=True)
    return roots


def _toc(path: Path, rows: list[tuple[str, str, str]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(repr((rows,)), encoding="utf-8")
    return path


def test_inventories_every_toc_and_manual_source(tmp_path: Path) -> None:
    roots = _roots(tmp_path)
    generated = _file(roots.pyinstaller_work / "portable-comfy/portable-comfy")
    wheel_data = _file(roots.launcher_venv / "lib/site-packages/webview/js/api.js")
    stdlib = _file(roots.portable_python / "lib/python3.13/os.py")
    project = _file(roots.repository / "src/portable_comfy/app.py")
    host_library = _file(tmp_path / "host/libpulse.so.0")
    manual_library = _file(tmp_path / "host/libwayland-client.so.0")
    copyright_file = _file(tmp_path / "debian/libexample/copyright", "Terms\n")
    toc = _toc(
        tmp_path / "COLLECT-00.toc",
        [
            ("portable-comfy", str(generated), "EXECUTABLE"),
            ("webview/js/api.js", str(wheel_data), "DATA"),
            ("python3.13/os.py", str(stdlib), "DATA"),
            ("portable_comfy/app.py", str(project), "DATA"),
            ("libpulse.so.0", str(host_library), "BINARY"),
            ("libalias.so", "libtarget.so", "SYMLINK"),
        ],
    )

    def owner_lookup(path: Path) -> DebianOwner | None:
        if path in {host_library, manual_library}:
            return DebianOwner(
                package="libexample1:amd64",
                version="1.2.3-4ubuntu5",
                copyright=copyright_file,
            )
        return None

    destination = tmp_path / "notices"
    provenance_path, packages_path = write_inventory(
        toc_path=toc,
        destination=destination,
        roots=roots,
        manual_sources=[
            FrozenSource(
                origin="manual",
                destination="_internal/libwayland-client.so.0",
                typecode="BINARY",
                source=str(manual_library),
            )
        ],
        owner_lookup=owner_lookup,
    )

    with provenance_path.open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream, delimiter="\t"))
    assert len(rows) == 7
    by_destination = {row["destination"]: row for row in rows}
    assert by_destination["portable-comfy"]["classification"] == "pyinstaller-build"
    assert (
        by_destination["_internal/webview/js/api.js"]["classification"]
        == "launcher-venv"
    )
    assert by_destination["_internal/python3.13/os.py"]["classification"] == (
        "portable-python"
    )
    assert by_destination["_internal/portable_comfy/app.py"]["classification"] == (
        "project-source"
    )
    assert by_destination["_internal/libalias.so"]["classification"] == (
        "relative-reference"
    )
    assert by_destination["_internal/libwayland-client.so.0"]["origin"] == "manual"

    with packages_path.open(encoding="utf-8", newline="") as stream:
        package_rows = list(csv.DictReader(stream, delimiter="\t"))
    assert package_rows == [
        {
            "debian_package": "libexample1:amd64",
            "version": "1.2.3-4ubuntu5",
            "copyright": "libexample1_amd64/copyright",
            "frozen_source_count": "2",
        }
    ]
    assert (destination / package_rows[0]["copyright"]).read_text() == "Terms\n"


def test_rejects_unclassified_absolute_source(tmp_path: Path) -> None:
    roots = _roots(tmp_path)
    unknown = _file(tmp_path / "unowned/libmystery.so")
    toc = _toc(
        tmp_path / "COLLECT-00.toc",
        [("libmystery.so", str(unknown), "BINARY")],
    )

    with pytest.raises(RuntimeError, match="unclassified absolute PyInstaller source"):
        write_inventory(
            toc_path=toc,
            destination=tmp_path / "notices",
            roots=roots,
            owner_lookup=lambda _path: None,
        )


def test_qt_only_freeze_uses_pywebview_hook_without_cross_platform_data() -> None:
    build_script = (REPO_ROOT / "scripts/build_appimage.sh").read_text(encoding="utf-8")

    assert "--collect-data webview" not in build_script
    for module in (
        "webview.platforms.gtk",
        "gi",
        "webview.platforms.android",
        "webview.platforms.cocoa",
        "webview.platforms.winforms",
    ):
        assert f"--exclude-module {module}" in build_script
    assert '"$frozen_root/webview/js/$webview_js"' in build_script
    assert '"$frozen_root/webview/lib"' in build_script
