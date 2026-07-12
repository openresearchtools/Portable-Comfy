#!/usr/bin/env bash
# Build one atomic full-ComfyUI Core bundle, including Python/Torch/CUDA.

set -Eeuo pipefail
IFS=$'\n\t'
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"

output_dir="$REPO_ROOT/dist"
work_dir="$REPO_ROOT/build/environment"
source_root=""
keep_stage=0
structural=0
while (($#)); do
  case "$1" in
    --output-dir) output_dir="$2"; shift 2 ;;
    --work-dir) work_dir="$2"; shift 2 ;;
    --source-root) source_root="$2"; shift 2 ;;
    --keep-stage) keep_stage=1; shift ;;
    --structural) structural=1; shift ;;
    -h|--help)
      printf 'Usage: %s [--output-dir DIR] [--work-dir DIR] [--source-root PORTABLE_ROOT] [--keep-stage] [--structural]\n' "$0"
      exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done
output_dir="$(absolute_path "$output_dir")"
work_dir="$(absolute_path "$work_dir")"
if [[ -n "$source_root" ]]; then
  source_root="$(absolute_path "$source_root")"
fi
bundle_basename="$(core_bundle_basename)"
stage="$work_dir/$bundle_basename"
archive="$output_dir/$bundle_basename.tar.gz"
generation_id="$(environment_generation_id)"

require_command python3 tar gzip sha256sum cp
verify_runtime_constraints
safe_rm_tree "$stage"
mkdir -p -- "$work_dir" "$stage" "$output_dir"

if [[ -n "$source_root" ]]; then
  [[ -f "$source_root/ComfyUI/main.py" && -f "$source_root/ComfyUI/frontend/index.html" ]] \
    || die "source root does not contain a prepared ComfyUI environment: $source_root"
  if ((structural == 0)); then
    [[ -x "$source_root/ComfyUI/runtime/python/bin/python-portable" ]] \
      || die "source root does not contain a complete portable environment runtime"
  fi
  # The completed first-install staging tree is on the same runner filesystem.
  # Hard links avoid duplicating its multi-gigabyte CUDA environment; fall back
  # to a normal copy when the caller places source and work trees on different
  # filesystems. The staged payload is never modified after this copy.
  if ! cp -al -- "$source_root/ComfyUI" "$stage/ComfyUI" 2>/dev/null; then
    safe_rm_tree "$stage/ComfyUI"
    cp -a --reflink=auto -- "$source_root/ComfyUI" "$stage/ComfyUI"
  fi
else
  "$SCRIPT_DIR/prepare_core.sh" "$stage/ComfyUI"
  if ((structural)); then
    mkdir -p -- "$stage/ComfyUI/runtime"
    cp -- "$REPO_ROOT/packaging/runtime-constraints.txt" \
      "$stage/ComfyUI/runtime/requirements.lock"
  else
    "$SCRIPT_DIR/build_python_runtime.sh" "$stage/ComfyUI" --work-dir "$work_dir/python"
    "$SCRIPT_DIR/install_runtime_dependencies.sh" "$stage/ComfyUI"
  fi
fi

SOURCE_DATE_EPOCH="$SOURCE_DATE_EPOCH" python3 \
  "$SCRIPT_DIR/generate_environment_manifest.py" "$stage" \
  --generation-id "$generation_id" \
  --core-version "$COMFY_VERSION" --core-tag "$COMFY_TAG" --core-commit "$COMFY_COMMIT" \
  --frontend-version "$FRONTEND_VERSION" --frontend-commit "$FRONTEND_COMMIT" \
  --python "$PYTHON_VERSION" --torch "$TORCH_VERSION" \
  --torchvision "$TORCHVISION_VERSION" --torchaudio "$TORCHAUDIO_VERSION" \
  --cuda "$CUDA_VERSION" --requirements-lock-sha256 "$RUNTIME_LOCK_SHA256"

verify_args=("$stage")
if ((structural)); then
  verify_args+=(--structural)
fi
python3 "$SCRIPT_DIR/verify_environment_bundle.py" "${verify_args[@]}"
create_deterministic_tar_gz "$stage" "$archive"
assert_safe_archive_paths "$archive"
preflight_args=("$archive")
if ((structural)); then
  preflight_args+=(--structural)
fi
"$SCRIPT_DIR/preflight_environment.sh" "${preflight_args[@]}"
log "created $archive ($(du -h "$archive" | cut -f1))"
if ((keep_stage)); then
  log "kept staged environment at $stage"
else
  safe_rm_tree "$stage"
fi
