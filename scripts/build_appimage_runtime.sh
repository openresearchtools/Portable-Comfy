#!/usr/bin/env bash
# Build the pinned AppImage runtime with an automatic no-FUSE extraction fallback.

set -Eeuo pipefail
IFS=$'\n\t'
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"

output="${1:-}"
shift || true
work_dir="$REPO_ROOT/build/appimage-runtime"
source_copy=""
source_bundle=""
while (($#)); do
  case "$1" in
    --work-dir) work_dir="$2"; shift 2 ;;
    --source-copy) source_copy="$2"; shift 2 ;;
    --source-bundle) source_bundle="$2"; shift 2 ;;
    -h|--help)
      printf 'Usage: %s OUTPUT [--work-dir DIR] [--source-copy PATH] [--source-bundle DIR]\n' "$0"
      exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done
[[ -n "$output" ]] || die "an output runtime path is required"
output="$(absolute_path "$output")"
work_dir="$(absolute_path "$work_dir")"
if [[ -n "$source_copy" ]]; then
  source_copy="$(absolute_path "$source_copy")"
fi
if [[ -n "$source_bundle" ]]; then
  source_bundle="$(absolute_path "$source_bundle")"
fi

require_command curl docker file patch python3 sha256sum tar
source_archive="$work_dir/type2-runtime-${APPIMAGE_RUNTIME_COMMIT}.tar.gz"
source_cache="$work_dir/compliance-source-cache"
source_parent="$work_dir/source"
docker_output="$work_dir/docker-output"
download_verified "$APPIMAGE_RUNTIME_SOURCE_URL" "$source_archive" \
  "$APPIMAGE_RUNTIME_SOURCE_SHA256"
musl_archive="$source_cache/musl-${APPIMAGE_RUNTIME_MUSL_UPSTREAM_VERSION}.tar.gz"
libfuse_archive="$source_cache/fuse-${APPIMAGE_RUNTIME_LIBFUSE_VERSION}.tar.xz"
squashfuse_archive="$source_cache/squashfuse-${APPIMAGE_RUNTIME_SQUASHFUSE_VERSION}.tar.gz"
zstd_archive="$source_cache/zstd-${APPIMAGE_RUNTIME_ZSTD_UPSTREAM_VERSION}.tar.gz"
zlib_archive="$source_cache/zlib-${APPIMAGE_RUNTIME_ZLIB_UPSTREAM_VERSION}.tar.gz"
mimalloc_archive="$source_cache/mimalloc-${APPIMAGE_RUNTIME_MIMALLOC_UPSTREAM_VERSION}.tar.gz"
musl_packaging="$source_cache/aports-musl-${APPIMAGE_RUNTIME_MUSL_APORTS_COMMIT}.tar.gz"
zstd_packaging="$source_cache/aports-zstd-${APPIMAGE_RUNTIME_ZSTD_APORTS_COMMIT}.tar.gz"
zlib_packaging="$source_cache/aports-zlib-${APPIMAGE_RUNTIME_ZLIB_APORTS_COMMIT}.tar.gz"
mimalloc_packaging="$source_cache/aports-mimalloc2-${APPIMAGE_RUNTIME_MIMALLOC_APORTS_COMMIT}.tar.gz"
download_verified "$APPIMAGE_RUNTIME_MUSL_SOURCE_URL" "$musl_archive" \
  "$APPIMAGE_RUNTIME_MUSL_SOURCE_SHA256"
download_verified "$APPIMAGE_RUNTIME_LIBFUSE_SOURCE_URL" "$libfuse_archive" \
  "$APPIMAGE_RUNTIME_LIBFUSE_SOURCE_SHA256"
download_verified "$APPIMAGE_RUNTIME_SQUASHFUSE_SOURCE_URL" "$squashfuse_archive" \
  "$APPIMAGE_RUNTIME_SQUASHFUSE_SOURCE_SHA256"
download_verified "$APPIMAGE_RUNTIME_ZSTD_SOURCE_URL" "$zstd_archive" \
  "$APPIMAGE_RUNTIME_ZSTD_SOURCE_SHA256"
download_verified "$APPIMAGE_RUNTIME_ZLIB_SOURCE_URL" "$zlib_archive" \
  "$APPIMAGE_RUNTIME_ZLIB_SOURCE_SHA256"
download_verified "$APPIMAGE_RUNTIME_MIMALLOC_SOURCE_URL" "$mimalloc_archive" \
  "$APPIMAGE_RUNTIME_MIMALLOC_SOURCE_SHA256"
download_verified "$APPIMAGE_RUNTIME_MUSL_APORTS_URL" "$musl_packaging" \
  "$APPIMAGE_RUNTIME_MUSL_APORTS_SHA256"
download_verified "$APPIMAGE_RUNTIME_ZSTD_APORTS_URL" "$zstd_packaging" \
  "$APPIMAGE_RUNTIME_ZSTD_APORTS_SHA256"
download_verified "$APPIMAGE_RUNTIME_ZLIB_APORTS_URL" "$zlib_packaging" \
  "$APPIMAGE_RUNTIME_ZLIB_APORTS_SHA256"
download_verified "$APPIMAGE_RUNTIME_MIMALLOC_APORTS_URL" "$mimalloc_packaging" \
  "$APPIMAGE_RUNTIME_MIMALLOC_APORTS_SHA256"
assert_safe_archive_paths "$source_archive"
safe_rm_tree "$source_parent"
safe_rm_tree "$docker_output"
mkdir -p -- "$source_parent" "$docker_output" "$(dirname -- "$output")"
tar -xzf "$source_archive" -C "$source_parent" \
  --no-same-owner --no-same-permissions
mapfile -t roots < <(find "$source_parent" -mindepth 1 -maxdepth 1 -type d -print)
((${#roots[@]} == 1)) || die "AppImage runtime source has an unexpected layout"
source_root="${roots[0]}"
patch --batch --forward --no-backup-if-mismatch -p1 -d "$source_root" \
  < "$REPO_ROOT/packaging/appimage-runtime-dependencies.patch"
patch --batch --forward --no-backup-if-mismatch -p1 -d "$source_root" \
  < "$REPO_ROOT/packaging/appimage-runtime-fuse-fallback.patch"
mkdir -p -- "$source_root/compliance-sources"
install -m 0644 -- "$libfuse_archive" \
  "$source_root/compliance-sources/fuse-${APPIMAGE_RUNTIME_LIBFUSE_VERSION}.tar.xz"
install -m 0644 -- "$squashfuse_archive" \
  "$source_root/compliance-sources/squashfuse-${APPIMAGE_RUNTIME_SQUASHFUSE_VERSION}.tar.gz"

# Upstream's pinned Docker recipe builds a static PIE runtime and checks the
# downloaded libfuse/squashfuse source hashes. GitHub-hosted runners provide
# Docker; no privileged container is used.
(
  cd -- "$docker_output"
  ARCH=x86_64 "$source_root/scripts/docker/build-with-docker.sh"
)
built="$docker_output/runtime-x86_64"
[[ -x "$built" ]] || die "patched AppImage runtime build produced no executable"
file -Lb "$built" | grep -q 'ELF 64-bit.*x86-64' \
  || die "patched AppImage runtime is not an x86-64 ELF"
runtime_object="$docker_output/runtime.o"
apk_packages="$docker_output/runtime-apk-packages.txt"
all_build_packages="$docker_output/runtime-all-build-packages.txt"
static_libraries="$docker_output/runtime-static-libraries.sha256"
crt_objects="$docker_output/runtime-crt-objects.sha256"
link_map="$docker_output/runtime-link.map"
link_trace="$docker_output/runtime-link.trace"
dynamic_section="$docker_output/runtime-dynamic-section.txt"
file -Lb "$runtime_object" | grep -q 'ELF 64-bit.*relocatable.*x86-64' \
  || die "AppImage runtime relink object is missing or invalid"
[[ -s "$apk_packages" && -s "$all_build_packages" \
   && -s "$static_libraries" && -s "$crt_objects" \
   && -s "$link_map" && -s "$link_trace" && -s "$dynamic_section" ]] \
  || die "AppImage runtime build-input metadata is incomplete"
! grep -q '(NEEDED)' "$dynamic_section" \
  || die "AppImage runtime unexpectedly has a dynamic dependency"
version="$($built --appimage-version 2>&1)"
grep -Fq 'portable-comfy-private-extract-v2' <<<"$version" \
  || die "patched AppImage runtime has the wrong identity"
install -m 0755 -- "$built" "$output"
if [[ -n "$source_copy" ]]; then
  mkdir -p -- "$(dirname -- "$source_copy")"
  install -m 0644 -- "$source_archive" "$source_copy"
fi
if [[ -n "$source_bundle" ]]; then
  safe_rm_tree "$source_bundle"
  python3 "$SCRIPT_DIR/package_appimage_runtime_sources.py" "$source_bundle" \
    --alpine-version "$APPIMAGE_RUNTIME_ALPINE_VERSION" \
    --alpine-digest "$APPIMAGE_RUNTIME_ALPINE_AMD64_DIGEST" \
    --runtime-version "$APPIMAGE_RUNTIME_COMMIT" \
    --runtime-archive "$source_archive" \
    --musl-version "$APPIMAGE_RUNTIME_MUSL_VERSION" \
    --musl-upstream-version "$APPIMAGE_RUNTIME_MUSL_UPSTREAM_VERSION" \
    --musl-archive "$musl_archive" --musl-packaging "$musl_packaging" \
    --libfuse-version "$APPIMAGE_RUNTIME_LIBFUSE_VERSION" \
    --libfuse-archive "$libfuse_archive" \
    --squashfuse-version "$APPIMAGE_RUNTIME_SQUASHFUSE_VERSION" \
    --squashfuse-archive "$squashfuse_archive" \
    --zstd-version "$APPIMAGE_RUNTIME_ZSTD_VERSION" \
    --zstd-upstream-version "$APPIMAGE_RUNTIME_ZSTD_UPSTREAM_VERSION" \
    --zstd-archive "$zstd_archive" --zstd-packaging "$zstd_packaging" \
    --zlib-version "$APPIMAGE_RUNTIME_ZLIB_VERSION" \
    --zlib-upstream-version "$APPIMAGE_RUNTIME_ZLIB_UPSTREAM_VERSION" \
    --zlib-archive "$zlib_archive" --zlib-packaging "$zlib_packaging" \
    --mimalloc-version "$APPIMAGE_RUNTIME_MIMALLOC_VERSION" \
    --mimalloc-upstream-version "$APPIMAGE_RUNTIME_MIMALLOC_UPSTREAM_VERSION" \
    --mimalloc-archive "$mimalloc_archive" \
    --mimalloc-packaging "$mimalloc_packaging" \
    --fallback-patch "$REPO_ROOT/packaging/appimage-runtime-fuse-fallback.patch" \
    --dependencies-patch "$REPO_ROOT/packaging/appimage-runtime-dependencies.patch" \
    --runtime-object "$runtime_object" --apk-packages "$apk_packages" \
    --all-build-packages "$all_build_packages" \
    --static-libraries "$static_libraries" --crt-objects "$crt_objects" \
    --link-map "$link_map" --link-trace "$link_trace" \
    --dynamic-section "$dynamic_section"
  (cd -- "$source_bundle" && sha256sum --check --strict SHA256SUMS)
fi
log "built patched AppImage runtime at $output"
