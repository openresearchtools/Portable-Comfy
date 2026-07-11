#!/usr/bin/env bash
# Freeze the small desktop launcher and wrap it in an AppImage.

set -Eeuo pipefail
IFS=$'\n\t'
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"

portable_root="${1:-}"
shift || true
work_dir="$REPO_ROOT/build/appimage"
build_python="${BUILD_PYTHON:-python3}"
while (($#)); do
  case "$1" in
    --work-dir) work_dir="$2"; shift 2 ;;
    --build-python) build_python="$2"; shift 2 ;;
    *) die "unknown argument: $1" ;;
  esac
done
[[ -n "$portable_root" ]] || die "usage: $0 PORTABLE_ROOT [--work-dir DIR] [--build-python PYTHON]"
portable_root="$(absolute_path "$portable_root")"
work_dir="$(absolute_path "$work_dir")"
appdir="$work_dir/Portable-Comfy.AppDir"
venv="$work_dir/launcher-venv"
tool="$work_dir/appimagetool-x86_64.AppImage"
runtime_file="$work_dir/runtime-x86_64"
output="$portable_root/Portable-Comfy.AppImage"

require_command "$build_python" curl sha256sum
[[ -f "$REPO_ROOT/src/portable_comfy/__main__.py" ]] || die "launcher entrypoint is missing"
safe_rm_tree "$appdir"
safe_rm_tree "$venv"
mkdir -p -- "$work_dir" "$appdir/usr/lib/portable-comfy" "$appdir/usr/bin" \
  "$appdir/usr/share/applications" "$appdir/usr/share/icons/hicolor/scalable/apps"

"$build_python" -m venv "$venv"
"$venv/bin/python" -m pip install --disable-pip-version-check --upgrade pip setuptools wheel
"$venv/bin/python" -m pip install --disable-pip-version-check \
  -r "$REPO_ROOT/packaging/launcher-requirements.txt"
"$venv/bin/python" -m pip install --disable-pip-version-check --no-deps "$REPO_ROOT"

log "freezing launcher with PyInstaller"
"$venv/bin/pyinstaller" --noconfirm --clean --noupx --onedir --windowed \
  --name portable-comfy \
  --distpath "$work_dir/pyinstaller-dist" --workpath "$work_dir/pyinstaller-work" \
  --specpath "$work_dir" --paths "$REPO_ROOT/src" \
  --collect-data webview \
  --hidden-import webview.platforms.qt \
  --hidden-import qtpy.QtCore --hidden-import qtpy.QtGui --hidden-import qtpy.QtWidgets \
  --hidden-import qtpy.QtNetwork --hidden-import qtpy.QtWebChannel \
  --hidden-import qtpy.QtWebEngineCore --hidden-import qtpy.QtWebEngineWidgets \
  "$REPO_ROOT/src/portable_comfy/__main__.py"
cp -a -- "$work_dir/pyinstaller-dist/portable-comfy/." "$appdir/usr/lib/portable-comfy/"

cat >"$appdir/usr/bin/portable-comfy" <<'EOF'
#!/bin/sh
set -eu
APPDIR=${APPDIR:-$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd -P)}
exec "$APPDIR/usr/lib/portable-comfy/portable-comfy" "$@"
EOF
chmod 0755 "$appdir/usr/bin/portable-comfy"
ln -s usr/bin/portable-comfy "$appdir/AppRun"
cp -- "$REPO_ROOT/packaging/Portable-Comfy.desktop" \
  "$appdir/org.openresearchtools.PortableComfy.desktop"
cp -- "$REPO_ROOT/packaging/Portable-Comfy.desktop" \
  "$appdir/usr/share/applications/org.openresearchtools.PortableComfy.desktop"
cp -- "$REPO_ROOT/assets/icons/portable-comfy.svg" "$appdir/portable-comfy.svg"
cp -- "$REPO_ROOT/assets/icons/portable-comfy.svg" \
  "$appdir/usr/share/icons/hicolor/scalable/apps/portable-comfy.svg"
mkdir -p -- "$appdir/usr/share/metainfo"
cp -- "$REPO_ROOT/packaging/org.openresearchtools.PortableComfy.appdata.xml" \
  "$appdir/usr/share/metainfo/org.openresearchtools.PortableComfy.appdata.xml"
mkdir -p -- "$appdir/usr/share/licenses/portable-comfy"
cp -- "$REPO_ROOT/LICENSE" "$appdir/usr/share/licenses/portable-comfy/LICENSE"
"$venv/bin/python" "$SCRIPT_DIR/collect_licenses.py" "$appdir/usr/share/licenses/python-packages"

download_verified "$APPIMAGETOOL_URL" "$tool" "$APPIMAGETOOL_SHA256"
download_verified "$APPIMAGE_RUNTIME_URL" "$runtime_file" "$APPIMAGE_RUNTIME_SHA256"
chmod 0755 "$tool"
rm -f -- "$output"
# appimagetool itself is an AppImage. Extraction mode works on GitHub runners
# and containers where FUSE is intentionally unavailable.
ARCH=x86_64 APPIMAGE_EXTRACT_AND_RUN=1 SOURCE_DATE_EPOCH="$SOURCE_DATE_EPOCH" \
  "$tool" --runtime-file "$runtime_file" "$appdir" "$output"
chmod 0755 "$output"
[[ -s "$output" ]] || die "appimagetool did not create $output"
log "created launcher AppImage at $output"
