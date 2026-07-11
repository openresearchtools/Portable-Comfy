#!/usr/bin/env bash
# Build the replaceable, checksummed ComfyUI Core + frontend update bundle.

set -Eeuo pipefail
IFS=$'\n\t'
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"

output_dir="$REPO_ROOT/dist"
work_dir="$REPO_ROOT/build"
keep_stage=0
while (($#)); do
  case "$1" in
    --output-dir) output_dir="$2"; shift 2 ;;
    --work-dir) work_dir="$2"; shift 2 ;;
    --keep-stage) keep_stage=1; shift ;;
    -h|--help)
      printf 'Usage: %s [--output-dir DIR] [--work-dir DIR] [--keep-stage]\n' "$0"
      exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done
output_dir="$(absolute_path "$output_dir")"
work_dir="$(absolute_path "$work_dir")"
stage="$work_dir/core-bundle"
archive="$output_dir/Portable-Comfy-core-v${COMFY_VERSION}.tar.gz"

require_command python3 tar gzip sha256sum
verify_runtime_constraints
safe_rm_tree "$stage"
mkdir -p -- "$stage/ComfyUI" "$stage/LICENSES" "$output_dir"
"$SCRIPT_DIR/prepare_core.sh" "$stage/ComfyUI"
cp -- "$stage/ComfyUI/LICENSE" "$stage/LICENSES/ComfyUI-GPL-3.0.txt"
cp -- "$REPO_ROOT/LICENSE" "$stage/LICENSES/Portable-Comfy-GPL-3.0.txt"

SOURCE_DATE_EPOCH="$SOURCE_DATE_EPOCH" python3 "$SCRIPT_DIR/generate_manifest.py" "$stage" \
  --core-version "$COMFY_VERSION" --core-tag "$COMFY_TAG" --core-commit "$COMFY_COMMIT" \
  --frontend-version "$FRONTEND_VERSION" --frontend-commit "$FRONTEND_COMMIT" \
  --python "$PYTHON_VERSION" --torch "$TORCH_VERSION" --cuda "$CUDA_VERSION" \
  --requirements-lock-sha256 "$RUNTIME_LOCK_SHA256"
python3 "$SCRIPT_DIR/verify_core_bundle.py" "$stage"
create_deterministic_contents_tar_gz "$stage" "$archive"
assert_safe_archive_paths "$archive"

verify_dir="$(mktemp -d "$work_dir/core-verify.XXXXXX")"
trap 'rm -rf -- "$verify_dir"' EXIT
tar -xzf "$archive" -C "$verify_dir" --no-same-owner --no-same-permissions
python3 "$SCRIPT_DIR/verify_core_bundle.py" "$verify_dir"
log "created $archive ($(du -h "$archive" | cut -f1))"
if ((keep_stage)); then
  log "kept staged bundle at $stage"
else
  safe_rm_tree "$stage"
fi
