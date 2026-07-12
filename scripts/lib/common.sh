#!/usr/bin/env bash
# shellcheck shell=bash

set -Eeuo pipefail
IFS=$'\n\t'

SCRIPT_LIB_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(cd -- "$SCRIPT_LIB_DIR/../.." && pwd -P)"
# shellcheck source=../../packaging/versions.env
source "$REPO_ROOT/packaging/versions.env"

log() {
  printf '[portable-comfy] %s\n' "$*" >&2
}

die() {
  log "ERROR: $*"
  exit 1
}

require_command() {
  local command_name
  for command_name in "$@"; do
    command -v "$command_name" >/dev/null 2>&1 || die "required command not found: $command_name"
  done
}

verify_runtime_constraints() {
  local constraints="$REPO_ROOT/packaging/runtime-constraints.txt"
  [[ -f "$constraints" ]] || die "runtime constraints file is missing: $constraints"
  printf '%s  %s\n' "$RUNTIME_LOCK_SHA256" "$constraints" \
    | sha256sum --check --status \
    || die "runtime constraints do not match RUNTIME_LOCK_SHA256"
}

environment_generation_id() {
  local cuda_slug="${CUDA_VERSION//./}"
  printf 'comfyui-v%s-%s-frontend-%s-%s-python-%s-cu%s-lock-%s\n' \
    "$COMFY_VERSION" "${COMFY_COMMIT:0:12}" \
    "$FRONTEND_VERSION" "${FRONTEND_COMMIT:0:12}" \
    "$PYTHON_VERSION" "$cuda_slug" "$RUNTIME_LOCK_SHA256"
}

core_bundle_basename() {
  printf 'Portable-Comfy-core-v%s\n' "$COMFY_VERSION"
}

absolute_path() {
  local path="$1"
  if [[ "$path" = /* ]]; then
    printf '%s\n' "$path"
  else
    printf '%s/%s\n' "$PWD" "$path"
  fi
}

download_verified() {
  local url="$1" destination="$2" expected_sha256="$3"
  mkdir -p -- "$(dirname -- "$destination")"
  if [[ -f "$destination" ]] && printf '%s  %s\n' "$expected_sha256" "$destination" | sha256sum --check --status; then
    log "using cached $(basename -- "$destination")"
    return 0
  fi
  rm -f -- "$destination.part"
  log "downloading $url"
  curl --fail --location --retry 5 --retry-all-errors --connect-timeout 30 \
    --output "$destination.part" "$url"
  printf '%s  %s\n' "$expected_sha256" "$destination.part" | sha256sum --check --status \
    || die "checksum mismatch for $url"
  mv -f -- "$destination.part" "$destination"
}

safe_rm_tree() {
  local path="$1"
  [[ -n "$path" && "$path" = /* && "$path" != / ]] || die "refusing unsafe removal: $path"
  rm -rf -- "$path"
}

create_deterministic_tar_gz() {
  local source_dir="$1" archive="$2"
  local parent name
  parent="$(dirname -- "$source_dir")"
  name="$(basename -- "$source_dir")"
  mkdir -p -- "$(dirname -- "$archive")"
  rm -f -- "$archive" "$archive.part"
  TZ=UTC tar --sort=name --format=posix --hard-dereference \
    --owner=0 --group=0 --numeric-owner \
    --mtime="@${SOURCE_DATE_EPOCH}" --pax-option=delete=atime,delete=ctime \
    -C "$parent" -cf - "$name" | gzip -n -9 >"$archive.part"
  mv -f -- "$archive.part" "$archive"
  sha256sum "$archive" >"$archive.sha256"
}

create_deterministic_contents_tar_gz() {
  local source_dir="$1" archive="$2"
  mkdir -p -- "$(dirname -- "$archive")"
  rm -f -- "$archive" "$archive.part"
  TZ=UTC tar --sort=name --format=posix --hard-dereference \
    --owner=0 --group=0 --numeric-owner \
    --mtime="@${SOURCE_DATE_EPOCH}" --pax-option=delete=atime,delete=ctime \
    -C "$source_dir" -cf - . | gzip -n -9 >"$archive.part"
  mv -f -- "$archive.part" "$archive"
  sha256sum "$archive" >"$archive.sha256"
}

assert_safe_archive_paths() {
  local archive="$1" entry
  while IFS= read -r entry; do
    entry="${entry#./}"
    [[ -z "$entry" ]] && continue
    [[ "$entry" != /* ]] || die "archive contains absolute path: $entry"
    [[ "/$entry/" != *"/../"* ]] || die "archive contains traversal path: $entry"
  done < <(tar -tzf "$archive")
}
