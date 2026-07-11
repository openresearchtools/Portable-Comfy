#!/usr/bin/env bash
# Compile upstream PSF CPython into the portable root without MPL build tooling.

set -Eeuo pipefail
IFS=$'\n\t'
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"

portable_root="${1:-}"
shift || true
work_dir="$REPO_ROOT/build/python"
jobs="${JOBS:-$(getconf _NPROCESSORS_ONLN 2>/dev/null || printf '2')}"
while (($#)); do
  case "$1" in
    --work-dir) work_dir="$2"; shift 2 ;;
    --jobs) jobs="$2"; shift 2 ;;
    *) die "unknown argument: $1" ;;
  esac
done
[[ -n "$portable_root" ]] || die "usage: $0 PORTABLE_ROOT [--work-dir DIR] [--jobs N]"
portable_root="$(absolute_path "$portable_root")"
work_dir="$(absolute_path "$work_dir")"
prefix="$portable_root/runtime/python"
cache_dir="${CACHE_DIR:-$REPO_ROOT/.cache/portable-comfy}"
if [[ "$prefix$work_dir" =~ [[:space:]] ]]; then
  die "CPython's build system cannot install into whitespace-containing paths; build in a simple CI path (the finished runtime remains relocatable to paths with spaces)"
fi

require_command curl sha256sum tar make gcc file readelf
mkdir -p -- "$cache_dir" "$work_dir" "$portable_root/runtime"
archive="$cache_dir/Python-${PYTHON_VERSION}.tar.xz"
download_verified "$PYTHON_URL" "$archive" "$PYTHON_SHA256"
safe_rm_tree "$work_dir/source"
safe_rm_tree "$work_dir/build"
safe_rm_tree "$prefix"
mkdir -p -- "$work_dir/source" "$work_dir/build" "$prefix"
tar -xJf "$archive" -C "$work_dir/source" --strip-components=1 --no-same-owner --no-same-permissions

log "configuring upstream CPython $PYTHON_VERSION"
cd "$work_dir/build"
export CFLAGS="${CFLAGS:-} -O2 -fPIC -ffile-prefix-map=$work_dir=/usr/src/portable-comfy/python"
portable_rpath='-Wl,-rpath='\''$$ORIGIN/../lib'\'''
export LDFLAGS="${LDFLAGS:-} -Wl,--enable-new-dtags $portable_rpath"
"$work_dir/source/configure" \
  --prefix="$prefix" \
  --enable-shared \
  --with-ensurepip=install \
  --with-system-expat
make -j "$jobs"
make install
cp -- "$work_dir/source/LICENSE" "$prefix/LICENSE.txt"

PYTHONHOME="$prefix" LD_LIBRARY_PATH="$prefix/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}" \
  "$prefix/bin/python3" -s -m ensurepip --upgrade
"$SCRIPT_DIR/repair_python_runtime.sh" "$prefix"
[[ "$("$prefix/bin/python-portable" -c 'import platform; print(platform.python_version())')" == "$PYTHON_VERSION" ]] \
  || die "portable interpreter version mismatch"
log "built upstream CPython $PYTHON_VERSION at $prefix"
