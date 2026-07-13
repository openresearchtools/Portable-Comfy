from __future__ import annotations

import csv
import hashlib
import importlib.util
import json
import subprocess
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
verify_inventory = MODULE.verify_inventory


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


def _python_native_inventory(
    tmp_path: Path, library: Path, *, soname: str = "libssl.so.3"
) -> Path:
    license_root = tmp_path / "python-native"
    notice = _file(
        license_root / "packages/libssl3_3.0.2_amd64/01-copyright",
        "OpenSSL terms: /usr/share/common-licenses/Apache-2.0\n",
    )
    common = _file(
        license_root / "common-licenses/Apache-2.0",
        "Apache License 2.0\n",
    )
    readme = _file(license_root / "README.md", "Native dependency notices\n")

    def bound_file(path: Path) -> dict[str, object]:
        return {
            "path": path.relative_to(license_root).as_posix(),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "size": path.stat().st_size,
            "source_path": f"/builder/{path.name}",
        }

    library_digest = hashlib.sha256(library.read_bytes()).hexdigest()
    inventory = {
        "schema_version": 1,
        "platform": "linux-x86_64",
        "native_directory": "lib/portable-native",
        "host_abi": {"driver": [], "glibc_kernel": [], "interpreters": []},
        "packages": [
            {
                "architecture": "amd64",
                "package": "libssl3:amd64",
                "source_package": "openssl",
                "source_version": "3.0.2-0ubuntu1.25",
                "version": "3.0.2-0ubuntu1.25",
                "notices": [bound_file(notice)],
            }
        ],
        "common_licenses": [
            {
                "name": "Apache-2.0",
                **bound_file(common),
            }
        ],
        "libraries": [
            {
                "debian_package": "libssl3:amd64",
                "path": f"lib/portable-native/{soname}",
                "sha256": library_digest,
                "size": library.stat().st_size,
                "soname": soname,
                "source_path": f"/usr/lib/x86_64-linux-gnu/{soname}",
                "source_sha256": library_digest,
                "source_size": library.stat().st_size,
            }
        ],
        "readme": bound_file(readme),
        "summary": {"common_licenses": 1, "libraries": 1, "packages": 1},
    }
    (license_root / "packages.json").write_text(
        json.dumps(inventory, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return license_root


def test_inventories_every_toc_and_manual_source(tmp_path: Path) -> None:
    roots = _roots(tmp_path)
    generated = _file(roots.pyinstaller_work / "portable-comfy/portable-comfy")
    wheel_data = _file(roots.launcher_venv / "lib/site-packages/webview/js/api.js")
    stdlib = _file(roots.portable_python / "lib/python3.13/os.py")
    project = _file(roots.repository / "src/portable_comfy/app.py")
    host_library = _file(tmp_path / "host/libpulse.so.0")
    manual_library = _file(tmp_path / "host/libwayland-client.so.0")
    copyright_file = _file(
        tmp_path / "debian/libexample/copyright",
        "Terms\n"
        "See /usr/share/common-licenses/GPL-2.\n"
        "Alias /usr/share/common-licenses/GPL-3.0”.\n"
        "Repeated /usr/share/common-licenses/GPL-2\n",
    )
    common_licenses = tmp_path / "common-licenses"
    gpl2 = _file(common_licenses / "GPL-2", "GPL version 2\n")
    gpl3 = _file(common_licenses / "GPL-3", "GPL version 3\n")
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
        common_license_directory=common_licenses,
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
    assert (destination / package_rows[0]["copyright"]).read_bytes() == (
        copyright_file.read_bytes()
    )

    with (destination / "common-licenses.tsv").open(
        encoding="utf-8", newline=""
    ) as stream:
        common_rows = list(csv.DictReader(stream, delimiter="\t"))
    assert common_rows == [
        {
            "debian_package": "libexample1:amd64",
            "referenced_name": "GPL-2",
            "resolved_name": "GPL-2",
            "license_text": "common-licenses/GPL-2",
            "sha256": hashlib.sha256(gpl2.read_bytes()).hexdigest(),
            "size": str(gpl2.stat().st_size),
        },
        {
            "debian_package": "libexample1:amd64",
            "referenced_name": "GPL-3.0",
            "resolved_name": "GPL-3",
            "license_text": "common-licenses/GPL-3.0",
            "sha256": hashlib.sha256(gpl3.read_bytes()).hexdigest(),
            "size": str(gpl3.stat().st_size),
        },
    ]
    assert (destination / "common-licenses/GPL-2").read_bytes() == gpl2.read_bytes()
    assert (destination / "common-licenses/GPL-3.0").read_bytes() == gpl3.read_bytes()
    manifest_paths = {
        line.split("  ", 1)[1]
        for line in (destination / "SHA256SUMS").read_text().splitlines()
    }
    assert manifest_paths == {
        path.relative_to(destination).as_posix()
        for path in destination.rglob("*")
        if path.is_file() and path.name != "SHA256SUMS"
    }
    verify_inventory(destination)


def test_portable_native_source_uses_exact_python_native_inventory(
    tmp_path: Path,
) -> None:
    roots = _roots(tmp_path)
    library = _file(
        roots.portable_python / "lib/portable-native/libssl.so.3",
        "portable libssl payload\n",
    )
    python_native = _python_native_inventory(tmp_path, library)
    toc = _toc(
        tmp_path / "COLLECT-00.toc",
        [("libssl.so.3", str(library), "BINARY")],
    )

    destination = tmp_path / "launcher-native"
    provenance_path, _ = write_inventory(
        toc_path=toc,
        destination=destination,
        roots=roots,
        owner_lookup=lambda path: pytest.fail(
            f"portable-native must not use host owner lookup: {path}"
        ),
        python_native_license_root=python_native,
    )

    with provenance_path.open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream, delimiter="\t"))
    assert rows == [
        {
            "origin": "pyinstaller",
            "destination": "_internal/libssl.so.3",
            "typecode": "BINARY",
            "source": str(library),
            "resolved_source": str(library.resolve()),
            "classification": "portable-python-native",
            "debian_package": "libssl3:amd64",
            "version": "3.0.2-0ubuntu1.25",
            "license_reference": (
                "../python-native/packages.json#lib/portable-native/libssl.so.3"
            ),
        }
    ]
    verify_inventory(destination, python_native)
    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--verify",
            str(destination),
            "--python-native-license-root",
            str(python_native),
        ],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr

    notice = next((python_native / "packages").rglob("*-copyright"))
    notice.write_text("tampered terms\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="notice checksum mismatch"):
        verify_inventory(destination, python_native)


def test_rejects_unlisted_or_modified_portable_native_source(tmp_path: Path) -> None:
    roots = _roots(tmp_path)
    listed = _file(
        roots.portable_python / "lib/portable-native/libssl.so.3",
        "portable libssl payload\n",
    )
    python_native = _python_native_inventory(tmp_path, listed)
    unlisted = _file(
        roots.portable_python / "lib/portable-native/libcrypto.so.3",
        "unlisted portable crypto\n",
    )
    unlisted_toc = _toc(
        tmp_path / "unlisted-COLLECT-00.toc",
        [("libcrypto.so.3", str(unlisted), "BINARY")],
    )

    with pytest.raises(RuntimeError, match="unlisted portable Python native source"):
        write_inventory(
            toc_path=unlisted_toc,
            destination=tmp_path / "unlisted-notices",
            roots=roots,
            python_native_license_root=python_native,
        )

    listed.write_text("modified after inventory\n", encoding="utf-8")
    modified_toc = _toc(
        tmp_path / "modified-COLLECT-00.toc",
        [("libssl.so.3", str(listed), "BINARY")],
    )
    with pytest.raises(RuntimeError, match="source checksum mismatch"):
        write_inventory(
            toc_path=modified_toc,
            destination=tmp_path / "modified-notices",
            roots=roots,
            python_native_license_root=python_native,
        )


def test_rejects_portable_native_source_without_inventory(tmp_path: Path) -> None:
    roots = _roots(tmp_path)
    library = _file(
        roots.portable_python / "lib/portable-native/libssl.so.3",
        "portable libssl payload\n",
    )
    toc = _toc(
        tmp_path / "COLLECT-00.toc",
        [("libssl.so.3", str(library), "BINARY")],
    )

    with pytest.raises(RuntimeError, match="has no license inventory"):
        write_inventory(
            toc_path=toc,
            destination=tmp_path / "notices",
            roots=roots,
        )


def test_verifier_rejects_modified_common_license(tmp_path: Path) -> None:
    roots = _roots(tmp_path)
    host_library = _file(tmp_path / "host/libexample.so")
    copyright_file = _file(
        tmp_path / "debian/libexample/copyright",
        "License text: /usr/share/common-licenses/Apache-2.0\n",
    )
    common_licenses = tmp_path / "common-licenses"
    _file(common_licenses / "Apache-2.0", "Apache terms\n")
    toc = _toc(
        tmp_path / "COLLECT-00.toc",
        [("libexample.so", str(host_library), "BINARY")],
    )

    destination = tmp_path / "notices"
    write_inventory(
        toc_path=toc,
        destination=destination,
        roots=roots,
        owner_lookup=lambda _path: DebianOwner(
            package="libexample1:amd64",
            version="1.0-1",
            copyright=copyright_file,
        ),
        common_license_directory=common_licenses,
    )
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), "--verify", str(destination)],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    (destination / "common-licenses/Apache-2.0").write_text("changed\n")

    with pytest.raises(RuntimeError, match="common-license metadata mismatch"):
        verify_inventory(destination)
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), "--verify", str(destination)],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert completed.returncode != 0
    assert "common-license metadata mismatch" in completed.stderr


@pytest.mark.parametrize(
    "reference, message",
    [
        ("/usr/share/common-licenses/../GPL-2", "malformed"),
        ("/usr/share/common-licenses/GPL-2/escape", "unsafe"),
        ("/usr/share/common-licenses/GPL-*", "unsafe"),
    ],
)
def test_rejects_unsafe_common_license_reference(
    tmp_path: Path, reference: str, message: str
) -> None:
    copyright_file = _file(tmp_path / "copyright", f"Terms: {reference}\n")

    with pytest.raises(RuntimeError, match=message):
        MODULE._common_license_tokens(copyright_file)


def test_rejects_missing_and_escaping_common_license(tmp_path: Path) -> None:
    common_licenses = tmp_path / "common-licenses"
    common_licenses.mkdir()

    with pytest.raises(RuntimeError, match="is unavailable"):
        MODULE._resolve_common_license("GPL-2", common_licenses)

    outside = _file(tmp_path / "outside/GPL-2", "terms\n")
    (common_licenses / "GPL-2").symlink_to(outside)
    with pytest.raises(RuntimeError, match="escapes its source directory"):
        MODULE._resolve_common_license("GPL-2", common_licenses)


def test_parses_debian_common_license_brace_list(tmp_path: Path) -> None:
    copyright_file = _file(
        tmp_path / "copyright",
        "Terms are in /usr/share/common-licenses/{MPL-1.1,GPL-2,LGPL-2.1}.\n",
    )

    assert MODULE._common_license_tokens(copyright_file) == (
        "GPL-2",
        "LGPL-2.1",
        "MPL-1.1",
    )


@pytest.mark.parametrize(
    "reference",
    [
        "/usr/share/common-licenses/{GPL-2,../GPL-3}",
        "/usr/share/common-licenses/{GPL-2,GPL-3",
        "/usr/share/common-licenses/{}",
    ],
)
def test_rejects_unsafe_common_license_brace_list(
    tmp_path: Path, reference: str
) -> None:
    copyright_file = _file(tmp_path / "copyright", f"Terms: {reference}\n")

    with pytest.raises(RuntimeError, match="common-license.*brace reference"):
        MODULE._common_license_tokens(copyright_file)


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
    assert '--python-native-license-root "$python_native_notices"' in build_script
    assert '"$appdir/usr/share/licenses/python-native"' in build_script
    assert '"$portable_root/LICENSES/python-native"' in build_script
    portable_build = (REPO_ROOT / "scripts/build_portable.sh").read_text(
        encoding="utf-8"
    )
    assert '"$environment_root/LICENSES/python-native"' in portable_build
    assert '"$portable_root/LICENSES/python-native"' in portable_build


def test_portable_preflight_revalidates_complete_native_notice_inventory() -> None:
    preflight = (REPO_ROOT / "scripts/preflight_portable.sh").read_text(
        encoding="utf-8"
    )

    assert 'inventory_appimage_sources.py"' in preflight
    assert '--verify "$native_notices"' in preflight
    assert '--python-native-license-root "$python_native_notices"' in preflight
    for required in (
        "common-licenses.tsv",
        "SHA256SUMS",
        "native_notices/FORMAT",
        "native_notices/README.txt",
        "python_native_notices/packages.json",
        "python_native_notices/README.md",
    ):
        assert required in preflight
