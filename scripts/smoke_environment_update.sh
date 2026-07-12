#!/usr/bin/env bash
# Install the downloaded full Core bundle into a downloaded first-install tree.

set -Eeuo pipefail
IFS=$'\n\t'
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"

full_archive="${1:-}"
core_archive="${2:-}"
shift 2 || true
timeout_seconds=120
while (($#)); do
  case "$1" in
    --timeout) timeout_seconds="$2"; shift 2 ;;
    *) die "unknown argument: $1" ;;
  esac
done
[[ -f "$full_archive" && -f "$core_archive" ]] \
  || die "usage: $0 FULL_ARCHIVE CORE_BUNDLE [--timeout SECONDS]"
full_archive="$(absolute_path "$full_archive")"
core_archive="$(absolute_path "$core_archive")"
require_command python3 tar
assert_safe_archive_paths "$full_archive"

temporary="$(mktemp -d "${TMPDIR:-/tmp}/Portable Comfy update smoke.XXXXXX")"
root=""
cleanup() {
  status=$?
  if ((status != 0)) && [[ -n "$root" && -f "$root/logs/comfyui.log" ]]; then
    sed -n '1,240p' "$root/logs/comfyui.log" >&2 || true
  fi
  rm -rf -- "$temporary"
}
trap cleanup EXIT

tar -xzf "$full_archive" -C "$temporary" --no-same-owner --no-same-permissions
mapfile -t roots < <(find "$temporary" -mindepth 1 -maxdepth 1 -type d -print)
((${#roots[@]} == 1)) || die "complete archive must contain one top-level directory"
root="${roots[0]}"
python3 "$SCRIPT_DIR/smoke_environment_update.py" \
  "$root" "$core_archive" --timeout "$timeout_seconds"
log "transactional full-Core update smoke passed"
