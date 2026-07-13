#!/usr/bin/env bash
# Verify the standalone launcher archive, persistent skeleton, and notices.

set -Eeuo pipefail
IFS=$'\n\t'
export PYTHONDONTWRITEBYTECODE=1
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"

target="${1:-}"
shift || true
structural=0
while (($#)); do
  case "$1" in
    --structural) structural=1; shift ;;
    *) die "unknown argument: $1" ;;
  esac
done
[[ -n "$target" ]] || die "usage: $0 PATH_TO_PORTABLE_ROOT_OR_TARBALL [--structural]"
target="$(absolute_path "$target")"
temporary=""
cleanup() {
  [[ -z "$temporary" ]] || rm -rf -- "$temporary"
}
trap cleanup EXIT

if [[ -f "$target" ]]; then
  assert_safe_archive_paths "$target"
  temporary="$(mktemp -d "${TMPDIR:-/tmp}/Portable Comfy launcher preflight.XXXXXX")"
  tar -xzf "$target" -C "$temporary" --no-same-owner --no-same-permissions
  mapfile -t roots < <(find "$temporary" -mindepth 1 -maxdepth 1 -type d -print)
  ((${#roots[@]} == 1)) || die "launcher archive must contain one top-level directory"
  root="${roots[0]}"
else
  [[ -d "$target" ]] || die "target does not exist: $target"
  root="$target"
fi
[[ "$(basename -- "$root")" == Portable-Comfy ]] \
  || die "launcher archive root must be named Portable-Comfy"

required_dirs=(
  custom_nodes custom_node_runtime models input output temp workflows
  user logs config manifest state cache LICENSES
)
for path in "${required_dirs[@]}"; do
  [[ -d "$root/$path" ]] || die "portable launcher layout is missing directory: $path"
done

# The launcher download is deliberately independent from every Core generation.
[[ ! -e "$root/ComfyUI" && ! -L "$root/ComfyUI" ]] \
  || die "standalone launcher archive must not contain a ComfyUI environment"
[[ ! -e "$root/runtime" && ! -L "$root/runtime" ]] \
  || die "standalone launcher archive must not contain a legacy runtime"
[[ ! -e "$root/manifest/environment.json" \
   && ! -e "$root/manifest/environment-checksums.sha256" ]] \
  || die "standalone launcher must not claim an installed environment"

taesd_models=(
  taef1_decoder.safetensors taef1_encoder.safetensors
  taesd3_decoder.safetensors taesd3_encoder.safetensors
  taesd_decoder.safetensors taesd_encoder.safetensors
  taesdxl_decoder.safetensors taesdxl_encoder.safetensors
)
for model in "${taesd_models[@]}"; do
  [[ -s "$root/models/vae_approx/$model" ]] \
    || die "bundled TAESD model is missing: $model"
done
[[ -s "$root/manifest/builtin-models.json" && -s "$root/LICENSES/TAESD-MIT.txt" ]] \
  || die "TAESD manifest or MIT license is missing"

required_notices=(
  LICENSE
  LICENSES/Portable-Comfy-GPL-3.0.txt
  LICENSES/README.txt
)
for notice in "${required_notices[@]}"; do
  [[ -s "$root/$notice" ]] || die "launcher redistribution notice is missing: $notice"
done
[[ -L "$root/user/default/workflows" ]] \
  || die "user/default/workflows must be a relative symlink"
[[ "$(readlink "$root/user/default/workflows")" == ../../workflows ]] \
  || die "workflow symlink has the wrong target"
[[ "$(realpath -m "$root/user/default/workflows")" == "$(realpath -m "$root/workflows")" ]] \
  || die "workflow symlink escapes or resolves incorrectly"

python3 "$SCRIPT_DIR/verify_file_manifest.py" "$root"
while IFS= read -r -d '' link; do
  destination="$(readlink "$link")"
  [[ "$destination" != /* ]] \
    || die "portable tree contains absolute symlink: ${link#"$root/"}"
  resolved="$(realpath -m "$link")"
  [[ "$resolved" == "$root" || "$resolved" == "$root/"* ]] \
    || die "portable tree contains escaping symlink: ${link#"$root/"}"
done < <(find "$root" -type l -print0)

if ((structural == 0)); then
  appimage="$root/Portable-Comfy.AppImage"
  [[ -x "$appimage" ]] || die "AppImage is missing or not executable"
  launcher_notices="$root/LICENSES/launcher-python-packages/packages.json"
  native_notices="$root/LICENSES/launcher-native-packages"
  native_provenance="$native_notices/provenance.tsv"
  native_packages="$native_notices/packages.tsv"
  native_common_licenses="$native_notices/common-licenses.tsv"
  native_checksums="$native_notices/SHA256SUMS"
  python_native_notices="$root/LICENSES/python-native"
  qtwebengine_notices="$root/LICENSES/Qt-${QT_RUNTIME_VERSION}-attributions"
  runtime_bundle="$root/LICENSES/AppImage-runtime-source"
  runtime_source="$runtime_bundle/sources/type2-runtime-${APPIMAGE_RUNTIME_COMMIT}.tar.gz"
  runtime_patch="$runtime_bundle/patches/appimage-runtime-fuse-fallback.patch"
  runtime_dependencies_patch="$runtime_bundle/patches/appimage-runtime-dependencies.patch"
  [[ -s "$root/LICENSES/CPython-PSF-2.0.txt" \
     && -s "$root/LICENSES/AppImage-runtime-MIT.txt" \
     && -s "$runtime_source" \
     && -s "$runtime_patch" \
     && -s "$runtime_dependencies_patch" \
     && -s "$runtime_bundle/README.md" \
     && -s "$runtime_bundle/RELINKING.md" \
     && -s "$runtime_bundle/COMPONENTS.tsv" \
     && -s "$runtime_bundle/SHA256SUMS" \
     && -s "$runtime_bundle/relink/runtime.o" \
     && -s "$root/LICENSES/QtWebEngine-Chromium-BSD-3-Clause.txt" \
     && -s "$qtwebengine_notices/MODULE-LICENSES/GFDL-1.3-no-invariants-only.txt" \
     && -s "$launcher_notices" \
     && -s "$native_provenance" \
     && -s "$native_packages" \
     && -s "$native_common_licenses" \
     && -s "$native_checksums" \
     && -s "$native_notices/FORMAT" \
     && -s "$native_notices/README.txt" \
     && -s "$python_native_notices/packages.json" \
     && -s "$python_native_notices/README.md" ]] \
    || die "AppImage redistribution notices are incomplete"
  python3 "$SCRIPT_DIR/inventory_appimage_sources.py" \
    --verify "$native_notices" \
    --python-native-license-root "$python_native_notices" \
    || die "launcher native dependency notice verification failed"
  printf '%s  %s\n' "$APPIMAGE_RUNTIME_SOURCE_SHA256" "$runtime_source" \
    | sha256sum -c -
  (cd -- "$runtime_bundle" && sha256sum --check --strict SHA256SUMS)
  cmp -- "$REPO_ROOT/packaging/appimage-runtime-fuse-fallback.patch" \
    "$runtime_patch" \
    || die "shipped AppImage runtime patch differs from the project source"
  cmp -- "$REPO_ROOT/packaging/appimage-runtime-dependencies.patch" \
    "$runtime_dependencies_patch" \
    || die "shipped AppImage dependency patch differs from the project source"
  python3 - "$runtime_bundle" <<'PY'
import csv
import os
import pathlib
import re
import sys

root = pathlib.Path(sys.argv[1]).resolve()
manifest_path = root / "SHA256SUMS"
manifest_files = set()
for line in manifest_path.read_text(encoding="utf-8").splitlines():
    digest, separator, relative = line.partition("  ")
    assert separator and re.fullmatch(r"[0-9a-f]{64}", digest)
    path = pathlib.PurePosixPath(relative)
    assert not path.is_absolute() and ".." not in path.parts
    manifest_files.add(path.as_posix())
actual_files = {
    path.relative_to(root).as_posix()
    for path in root.rglob("*")
    if path.is_file() and path != manifest_path
}
assert manifest_files == actual_files

with (root / "COMPONENTS.tsv").open(encoding="utf-8", newline="") as stream:
    rows = list(csv.DictReader(stream, delimiter="\t"))
expected_versions = {
    "type2-runtime": os.environ["APPIMAGE_RUNTIME_COMMIT"],
    "musl": os.environ["APPIMAGE_RUNTIME_MUSL_VERSION"],
    "libfuse": os.environ["APPIMAGE_RUNTIME_LIBFUSE_VERSION"],
    "squashfuse": os.environ["APPIMAGE_RUNTIME_SQUASHFUSE_VERSION"],
    "zstd": os.environ["APPIMAGE_RUNTIME_ZSTD_VERSION"],
    "zlib": os.environ["APPIMAGE_RUNTIME_ZLIB_VERSION"],
    "mimalloc": os.environ["APPIMAGE_RUNTIME_MIMALLOC_VERSION"],
}
assert {row["component"]: row["version"] for row in rows} == expected_versions
assert next(row for row in rows if row["component"] == "libfuse")["license"] == "LGPL-2.1-only"

expected_packages = {
    *(
        f"{name}-{os.environ['APPIMAGE_RUNTIME_MUSL_VERSION']}"
        for name in ("musl", "musl-dev")
    ),
    *(
        f"{name}-{os.environ['APPIMAGE_RUNTIME_ZSTD_VERSION']}"
        for name in ("zstd", "zstd-libs", "zstd-dev", "zstd-static")
    ),
    *(
        f"{name}-{os.environ['APPIMAGE_RUNTIME_ZLIB_VERSION']}"
        for name in ("zlib", "zlib-dev", "zlib-static")
    ),
    *(
        f"{name}-{os.environ['APPIMAGE_RUNTIME_MIMALLOC_VERSION']}"
        for name in (
            "mimalloc2", "mimalloc2-dev", "mimalloc2-debug",
            "mimalloc2-insecure",
        )
    ),
}
actual_packages = set(
    (root / "build-inputs/runtime-apk-packages.txt")
    .read_text(encoding="utf-8")
    .splitlines()
)
assert actual_packages == expected_packages
all_build_packages = set(
    (root / "build-inputs/runtime-all-build-packages.txt")
    .read_text(encoding="utf-8")
    .splitlines()
)
assert actual_packages <= all_build_packages

static_paths = set()
for line in (root / "build-inputs/runtime-static-libraries.sha256").read_text(
    encoding="utf-8"
).splitlines():
    match = re.fullmatch(r"[0-9a-f]{64}  (/.+)", line)
    assert match
    static_paths.add(match.group(1))
assert static_paths == {
    "/usr/lib/libc.a", "/usr/lib/libfuse3.a",
    "/usr/local/lib/libsquashfuse.a",
    "/usr/local/lib/libsquashfuse_ll.a", "/usr/lib/libzstd.a",
    "/usr/lib/libz.a", "/usr/lib/libmimalloc.a",
}
crt_paths = set()
for line in (root / "build-inputs/runtime-crt-objects.sha256").read_text(
    encoding="utf-8"
).splitlines():
    match = re.fullmatch(r"[0-9a-f]{64}  (/.+)", line)
    assert match
    crt_paths.add(match.group(1))
assert crt_paths == {"/usr/lib/rcrt1.o", "/usr/lib/crti.o", "/usr/lib/crtn.o"}
trace_paths = set(
    (root / "build-inputs/runtime-link.trace").read_text(encoding="utf-8").splitlines()
)
assert trace_paths == static_paths | crt_paths | {"runtime.o"}
link_map = (root / "build-inputs/runtime-link.map").read_text(encoding="utf-8")
assert "/usr/lib/gcc/" not in link_map and "libgcc" not in link_map
dynamic_section = (root / "build-inputs/runtime-dynamic-section.txt").read_text(
    encoding="utf-8"
)
assert "(NEEDED)" not in dynamic_section
runtime_object = (root / "relink/runtime.o").read_bytes()
assert runtime_object[:6] == b"\x7fELF\x02\x01"
assert runtime_object[16:20] == b"\x01\x00\x3e\x00"

required_notices = {
    "type2-runtime-MIT.txt", "musl-COPYRIGHT.txt",
    "libfuse-LICENSE.txt", "libfuse-LGPL-2.1.txt",
    "libfuse-GPL-2.0.txt", "squashfuse-BSD-2-Clause.txt",
    "zstd-BSD-3-Clause.txt", "zstd-GPL-2.0.txt", "zlib.txt",
    "mimalloc-MIT.txt",
}
assert required_notices <= {path.name for path in (root / "licenses").iterdir()}
PY
  python3 "$SCRIPT_DIR/qtwebengine_notices.py" "$qtwebengine_notices" \
    --url "$QT_LICENSES_USED_URL" \
    --webengine-url "$QTWEBENGINE_NOTICES_URL" \
    --main-sha256 "$QT_LICENSES_USED_SHA256" \
    --webengine-sha256 "$QTWEBENGINE_NOTICES_MAIN_SHA256" \
    --manifest-sha256 "$QTWEBENGINE_NOTICES_MANIFEST_SHA256" \
    --module-license-manifest-sha256 \
      "$QTWEBENGINE_MODULE_LICENSES_MANIFEST_SHA256" \
    --expected-linked-pages "$QTWEBENGINE_NOTICES_LINKED_PAGES" \
    --qt-version "$QT_RUNTIME_VERSION" \
    --qtwebengine-commit "$QTWEBENGINE_SOURCE_COMMIT" \
    --chromium-commit "$QTWEBENGINE_CHROMIUM_COMMIT" \
    --verify-only
  python3 - "$launcher_notices" "$native_provenance" "$native_packages" <<'PY'
import collections
import csv
import json
import pathlib
import sys

launcher_path, provenance_path, packages_path = sys.argv[1:]
data = json.load(open(launcher_path, encoding="utf-8"))
package_count = len(data["packages"])
assert data["schema_version"] == 2
assert data["summary"] == {
    "distributions": package_count,
    "with_license_files": package_count,
    "metadata_only": 0,
    "unidentified": 0,
}
launcher = {
    package["name"].lower().replace("_", "-")
    for package in data["packages"]
    if package["license_files"]
}
assert {
    "portable-comfy", "pyinstaller", "pywebview", "proxy-tools",
    "pyqt6", "pyqt6-qt6", "pyqt6-webengine", "pyqt6-webengine-qt6",
    "pyqt6-sip", "qtpy",
} <= launcher

with open(provenance_path, encoding="utf-8", newline="") as stream:
    provenance = list(csv.DictReader(stream, delimiter="\t"))
assert provenance
assert set(provenance[0]) == {
    "origin", "destination", "typecode", "source", "resolved_source",
    "classification", "debian_package", "version", "license_reference",
}
allowed = {
    "relative-reference", "launcher-venv", "portable-python",
    "pyinstaller-build", "build-generated", "project-source",
    "debian-host-package",
}
assert all(row["classification"] in allowed for row in provenance)
assert all(
    row["classification"] != "relative-reference"
    for row in provenance
    if pathlib.Path(row["source"]).is_absolute()
)

with open(packages_path, encoding="utf-8", newline="") as stream:
    package_rows = list(csv.DictReader(stream, delimiter="\t"))
assert package_rows
assert set(package_rows[0]) == {
    "debian_package", "version", "copyright", "frozen_source_count",
}
packages = {row["debian_package"]: row for row in package_rows}
assert len(packages) == len(package_rows)
native_root = pathlib.Path(packages_path).resolve().parent
for row in package_rows:
    copyright_path = (native_root / row["copyright"]).resolve()
    assert copyright_path.is_relative_to(native_root)
    assert copyright_path.is_file() and copyright_path.stat().st_size
counts = collections.Counter(
    row["debian_package"]
    for row in provenance
    if row["classification"] == "debian-host-package"
)
assert set(counts) == set(packages)
for package, count in counts.items():
    package_row = packages[package]
    assert int(package_row["frozen_source_count"]) == count
    for source_row in provenance:
        if source_row["debian_package"] == package:
            assert source_row["version"] == package_row["version"]
            assert source_row["license_reference"] == package_row["copyright"]

libraries = {pathlib.PurePosixPath(row["destination"]).name for row in provenance}
assert {
    "libpulse.so.0", "libxcb-cursor.so.0", "libxcb-icccm.so.4",
    "libxcb-image.so.0", "libxcb-keysyms.so.1", "libxcb-render-util.so.0",
    "libxcb-shape.so.0", "libxcb-util.so.1", "libxcb-xkb.so.1",
    "libxkbcommon-x11.so.0", "libwayland-client.so.0",
    "libwayland-cursor.so.0", "libwayland-egl.so.1",
    "libwayland-server.so.0",
} <= libraries
manual_wayland = {
    pathlib.PurePosixPath(row["destination"]).name
    for row in provenance
    if row["origin"] == "manual"
}
assert {
    "libwayland-client.so.0", "libwayland-cursor.so.0",
    "libwayland-egl.so.1", "libwayland-server.so.0",
} == manual_wayland
destinations = {row["destination"] for row in provenance}
assert {
    "_internal/webview/js/api.js", "_internal/webview/js/customize.js",
    "_internal/webview/js/finish.js", "_internal/webview/js/state.js",
    "_internal/webview/js/lib/dom_json.js",
    "_internal/webview/js/lib/polyfill.js",
} <= destinations
assert not any(
    destination.startswith("_internal/webview/lib/")
    for destination in destinations
)
PY
  file -Lb "$appimage" | grep -q 'ELF 64-bit.*x86-64' \
    || die "AppImage is not an x86-64 ELF"
  runtime_identity="$("$appimage" --appimage-version 2>&1)"
  grep -Fq 'portable-comfy-private-extract-v2' <<<"$runtime_identity" \
    || die "AppImage does not contain the patched no-FUSE runtime"
fi
log "standalone launcher preflight passed: $root"
