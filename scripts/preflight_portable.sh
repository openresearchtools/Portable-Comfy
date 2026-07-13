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
  native_provenance="$root/LICENSES/launcher-native-packages/provenance.tsv"
  native_packages="$root/LICENSES/launcher-native-packages/packages.tsv"
  qtwebengine_notices="$root/LICENSES/Qt-${QT_RUNTIME_VERSION}-attributions"
  [[ -s "$root/LICENSES/CPython-PSF-2.0.txt" \
     && -s "$root/LICENSES/AppImage-runtime-MIT.txt" \
     && -s "$root/LICENSES/QtWebEngine-Chromium-BSD-3-Clause.txt" \
     && -s "$qtwebengine_notices/MODULE-LICENSES/GFDL-1.3-no-invariants-only.txt" \
     && -s "$launcher_notices" \
     && -s "$native_provenance" \
     && -s "$native_packages" ]] \
    || die "AppImage redistribution notices are incomplete"
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
  APPIMAGE_EXTRACT_AND_RUN=1 "$appimage" --appimage-version >/dev/null
fi
log "standalone launcher preflight passed: $root"
