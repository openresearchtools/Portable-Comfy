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
runtime_license="$work_dir/AppImage-runtime-LICENSE"
chromium_license="$work_dir/QtWebEngine-Chromium-LICENSE"
proxy_tools_license="$work_dir/proxy-tools-LICENSE.txt"
output="$portable_root/Portable-Comfy.AppImage"

require_command "$build_python" awk cmp curl dpkg-query ldconfig sha256sum
[[ -f "$REPO_ROOT/src/portable_comfy/__main__.py" ]] || die "launcher entrypoint is missing"

# actions/setup-python exports its own lib directory through LD_LIBRARY_PATH.
# The launcher build must instead resolve and freeze the libpython belonging to
# the selected build interpreter (normally runtime/python/python-portable).
mapfile -t build_runtime < <("$build_python" - <<'PY'
import pathlib
import sys
import sysconfig

print(pathlib.Path(sys.prefix).resolve())
print(pathlib.Path(sysconfig.get_config_var("LIBDIR")).resolve())
print(sysconfig.get_config_var("LDLIBRARY"))
PY
)
((${#build_runtime[@]} == 3)) || die "could not inspect the launcher build interpreter"
build_prefix="${build_runtime[0]}"
build_library_dir="${build_runtime[1]}"
build_libpython="$build_library_dir/${build_runtime[2]}"
[[ -d "$build_prefix" && -f "$build_libpython" ]] \
  || die "selected build interpreter has no shared libpython: $build_libpython"
build_libpython="$(realpath -- "$build_libpython")"
unset PYTHONHOME PYTHONPATH VIRTUAL_ENV
export LD_LIBRARY_PATH="$build_library_dir"
export PYTHONDONTWRITEBYTECODE=1
export PYTHONNOUSERSITE=1

safe_rm_tree "$appdir"
safe_rm_tree "$venv"
mkdir -p -- "$work_dir" "$appdir/usr/lib/portable-comfy" "$appdir/usr/bin" \
  "$appdir/usr/share/applications" "$appdir/usr/share/icons/hicolor/scalable/apps" \
  "$appdir/usr/share/licenses/native-packages"

"$build_python" -m venv "$venv"
"$venv/bin/python" -m pip install --disable-pip-version-check --upgrade \
  "pip==26.1.2" "setuptools==83.0.0" "wheel==0.47.0" "packaging==26.2"
"$venv/bin/python" -m pip install --disable-pip-version-check \
  -r "$REPO_ROOT/packaging/launcher-requirements.txt"
"$venv/bin/python" -m pip install --disable-pip-version-check \
  --no-build-isolation --no-deps "$REPO_ROOT"
"$venv/bin/python" -m pip check

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
frozen_root="$work_dir/pyinstaller-dist/portable-comfy/_internal"
frozen_libpython="$frozen_root/$(basename -- "$build_libpython")"
[[ -f "$frozen_libpython" ]] || die "PyInstaller did not collect the selected libpython"
cmp --silent "$build_libpython" "$frozen_libpython" \
  || die "PyInstaller collected a host libpython instead of $build_libpython"

# QtWebEngine loads Wayland support during import even when the active display
# is X11/Xvfb. PyInstaller does not discover these dlopen-time dependencies, so
# copy the target runtime libraries explicitly instead of relying on the host.
wayland_libraries=(
  libwayland-client.so.0 libwayland-cursor.so.0
  libwayland-egl.so.1 libwayland-server.so.0
)
declare -A native_library_sources=()
for library in "${wayland_libraries[@]}"; do
  source_library="$(
    ldconfig -p | awk -v name="$library" \
      '$1 == name && /x86-64/ && $NF ~ /^\// && !found { print $NF; found = 1 }'
  )"
  [[ -n "$source_library" && -f "$source_library" ]] \
    || die "required Wayland runtime library is unavailable: $library"
  cp -L -- "$source_library" "$frozen_root/$library"
  native_library_sources["$library"]="$source_library"
done
required_qt_libraries=(
  libpulse.so.0 libxcb-cursor.so.0 libxcb-icccm.so.4 libxcb-image.so.0
  libxcb-keysyms.so.1 libxcb-render-util.so.0 libxcb-shape.so.0
  libxcb-util.so.1 libxcb-xkb.so.1 libxkbcommon-x11.so.0
  "${wayland_libraries[@]}"
)
for library in "${required_qt_libraries[@]}"; do
  [[ -f "$frozen_root/$library" ]] \
    || die "PyInstaller did not collect required Qt runtime library: $library"
  if [[ -z "${native_library_sources[$library]:-}" ]]; then
    source_library="$(
      ldconfig -p | awk -v name="$library" \
        '$1 == name && /x86-64/ && $NF ~ /^\// && !found { print $NF; found = 1 }'
    )"
    [[ -n "$source_library" && -f "$source_library" ]] \
      || die "cannot identify build-host source for native library: $library"
    native_library_sources["$library"]="$source_library"
  fi
done

# These libraries are copied from the Ubuntu build host, not from wheels. Bind
# each binary to its exact Debian package/version and ship that package's
# copyright file. This list is deliberately the same list asserted above.
native_inventory="$appdir/usr/share/licenses/native-packages/packages.tsv"
printf 'library\tsource\tdebian_package\tversion\tcopyright\n' >"$native_inventory"
declare -A copied_native_notices=()
for library in "${required_qt_libraries[@]}"; do
  source_library="${native_library_sources[$library]}"
  real_library="$(realpath -- "$source_library")"
  owner_lines="$(dpkg-query --search "$real_library" 2>/dev/null || true)"
  if [[ -z "$owner_lines" ]]; then
    owner_lines="$(dpkg-query --search "$source_library" 2>/dev/null || true)"
  fi
  owner_line="${owner_lines%%$'\n'*}"
  [[ -n "$owner_line" ]] \
    || die "cannot identify Debian package for bundled native library: $real_library"
  owner_with_arch="${owner_line%%: *}"
  package="${owner_with_arch%%:*}"
  package_version="$(dpkg-query --show --showformat='${Version}' "$owner_with_arch")"
  copyright="/usr/share/doc/$package/copyright"
  [[ -s "$copyright" ]] \
    || die "Debian copyright notice is missing for bundled package: $package"
  target="native-packages/$package/copyright"
  if [[ -z "${copied_native_notices[$package]:-}" ]]; then
    mkdir -p -- "$appdir/usr/share/licenses/native-packages/$package"
    cp -L -- "$copyright" "$appdir/usr/share/licenses/$target"
    copied_native_notices["$package"]=1
  fi
  printf '%s\t%s\t%s\t%s\t%s\n' \
    "$library" "$real_library" "$owner_with_arch" "$package_version" "$target" \
    >>"$native_inventory"
done
log "verified frozen launcher libpython and Qt runtime libraries"
cp -a -- "$work_dir/pyinstaller-dist/portable-comfy/." "$appdir/usr/lib/portable-comfy/"

cp -- "$REPO_ROOT/packaging/appimage-launcher.sh" "$appdir/usr/bin/portable-comfy"
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
mkdir -p -- "$appdir/usr/share/licenses/cpython"
cp -- "$portable_root/ComfyUI/runtime/python/LICENSE.txt" \
  "$appdir/usr/share/licenses/cpython/LICENSE.txt"
download_verified "$PROXY_TOOLS_LICENSE_URL" "$proxy_tools_license" \
  "$PROXY_TOOLS_LICENSE_SHA256"
"$venv/bin/python" "$SCRIPT_DIR/collect_licenses.py" \
  "$appdir/usr/share/licenses/python-packages" \
  --require-license-file portable-comfy \
  --require-license-file pyinstaller \
  --require-license-file pywebview \
  --require-license-file proxy-tools \
  --require-license-file PyQt6 \
  --require-license-file PyQt6-Qt6 \
  --require-license-file PyQt6-WebEngine \
  --require-license-file PyQt6-WebEngine-Qt6 \
  --require-license-file PyQt6-sip \
  --require-license-file QtPy \
  --extra-license-file "proxy-tools=$proxy_tools_license"

download_verified "$APPIMAGETOOL_URL" "$tool" "$APPIMAGETOOL_SHA256"
download_verified "$APPIMAGE_RUNTIME_URL" "$runtime_file" "$APPIMAGE_RUNTIME_SHA256"
download_verified "$APPIMAGE_RUNTIME_LICENSE_URL" "$runtime_license" \
  "$APPIMAGE_RUNTIME_LICENSE_SHA256"
download_verified "$QTWEBENGINE_CHROMIUM_LICENSE_URL" "$chromium_license" \
  "$QTWEBENGINE_CHROMIUM_LICENSE_SHA256"
cp -- "$runtime_license" "$appdir/usr/share/licenses/AppImage-runtime-MIT.txt"
cp -- "$chromium_license" \
  "$appdir/usr/share/licenses/QtWebEngine-Chromium-BSD-3-Clause.txt"

# Keep launcher notices inspectable beside the AppImage as well as inside it.
# The duplicate text compresses well and avoids requiring AppImage extraction
# just to read redistribution terms.
rm -rf -- "$portable_root/LICENSES/launcher-python-packages" \
  "$portable_root/LICENSES/launcher-native-packages"
cp -a -- "$appdir/usr/share/licenses/python-packages" \
  "$portable_root/LICENSES/launcher-python-packages"
cp -a -- "$appdir/usr/share/licenses/native-packages" \
  "$portable_root/LICENSES/launcher-native-packages"
cp -- "$runtime_license" "$portable_root/LICENSES/AppImage-runtime-MIT.txt"
cp -- "$chromium_license" \
  "$portable_root/LICENSES/QtWebEngine-Chromium-BSD-3-Clause.txt"
chmod 0755 "$tool"
rm -f -- "$output"
# appimagetool itself is an AppImage. Extraction mode works on GitHub runners
# and containers where FUSE is intentionally unavailable.
ARCH=x86_64 APPIMAGE_EXTRACT_AND_RUN=1 SOURCE_DATE_EPOCH="$SOURCE_DATE_EPOCH" \
  "$tool" --runtime-file "$runtime_file" "$appdir" "$output"
chmod 0755 "$output"
[[ -s "$output" ]] || die "appimagetool did not create $output"
log "created launcher AppImage at $output"
