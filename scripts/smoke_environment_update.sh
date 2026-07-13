#!/usr/bin/env bash
# Install the downloaded multipart environment into a standalone launcher tree.

set -Eeuo pipefail
IFS=$'\n\t'
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"

launcher_archive="${1:-}"
environment_source="${2:-}"
shift 2 || true
timeout_seconds=120
while (($#)); do
  case "$1" in
    --timeout) timeout_seconds="$2"; shift 2 ;;
    *) die "unknown argument: $1" ;;
  esac
done
[[ -f "$launcher_archive" && -f "$environment_source" ]] \
  || die "usage: $0 LAUNCHER_ARCHIVE ENVIRONMENT_DESCRIPTOR_OR_PART [--timeout SECONDS]"
launcher_archive="$(absolute_path "$launcher_archive")"
environment_source="$(absolute_path "$environment_source")"
require_command python3 tar
assert_safe_archive_paths "$launcher_archive"

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

tar -xzf "$launcher_archive" -C "$temporary" --no-same-owner --no-same-permissions
mapfile -t roots < <(find "$temporary" -mindepth 1 -maxdepth 1 -type d -print)
((${#roots[@]} == 1)) || die "launcher archive must contain one top-level directory"
root="${roots[0]}"
python3 "$SCRIPT_DIR/smoke_environment_update.py" \
  "$root" "$environment_source" --timeout "$timeout_seconds"
log "transactional environment install/update smoke passed"
