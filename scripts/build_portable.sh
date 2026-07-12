#!/usr/bin/env bash
# Assemble the complete one-click portable directory and deterministic archive.

set -Eeuo pipefail
IFS=$'\n\t'
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"

output_dir="$REPO_ROOT/dist"
work_dir="$REPO_ROOT/build"
skip_runtime=0
skip_appimage=0
while (($#)); do
  case "$1" in
    --output-dir) output_dir="$2"; shift 2 ;;
    --work-dir) work_dir="$2"; shift 2 ;;
    --skip-runtime) skip_runtime=1; shift ;;
    --skip-appimage) skip_appimage=1; shift ;;
    -h|--help)
      printf 'Usage: %s [--output-dir DIR] [--work-dir DIR] [--skip-runtime] [--skip-appimage]\n' "$0"
      exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done
output_dir="$(absolute_path "$output_dir")"
work_dir="$(absolute_path "$work_dir")"
portable_root="$work_dir/Portable-Comfy"
archive="$output_dir/Portable-Comfy-linux-x86_64.tar.gz"

require_command python3 tar gzip sha256sum
verify_runtime_constraints
safe_rm_tree "$portable_root"
mkdir -p -- "$portable_root" "$output_dir"
"$SCRIPT_DIR/prepare_core.sh" "$portable_root/ComfyUI"

# Persistent data is deliberately outside the atomically replaceable ComfyUI/.
data_dirs=(
  custom_nodes custom_node_runtime/bin custom_node_runtime/site-packages
  input output temp workflows
  logs config manifest user/default
  models/checkpoints models/configs models/loras models/vae models/text_encoders
  models/clip models/unet models/diffusion_models models/clip_vision models/style_models
  models/embeddings models/diffusers models/vae_approx models/controlnet models/t2i_adapter
  models/gligen models/upscale_models models/latent_upscale_models models/hypernetworks
  models/photomaker models/classifiers models/model_patches models/audio_encoders
  models/background_removal models/frame_interpolation models/geometry_estimation
  models/optical_flow models/detection
)
for path in "${data_dirs[@]}"; do
  mkdir -p -- "$portable_root/$path"
done
ln -s ../../workflows "$portable_root/user/default/workflows"
cat >"$portable_root/config/extra_model_paths.yaml" <<'EOF'
# Optional model paths rooted in the replaceable environment tree. User-managed models
# live in the top-level models/ directory selected by --base-directory.
portable_comfy_core:
  base_path: ../ComfyUI
  configs: models/configs
EOF
cp -- "$REPO_ROOT/LICENSE" "$portable_root/LICENSE"
mkdir -p -- "$portable_root/LICENSES"
cp -- "$REPO_ROOT/LICENSE" "$portable_root/LICENSES/Portable-Comfy-GPL-3.0.txt"
cp -- "$portable_root/ComfyUI/LICENSE" "$portable_root/LICENSES/ComfyUI-GPL-3.0.txt"
"$SCRIPT_DIR/install_builtin_models.sh" "$portable_root"

if ((skip_runtime == 0)); then
  "$SCRIPT_DIR/build_python_runtime.sh" "$portable_root/ComfyUI" --work-dir "$work_dir/python"
  cp -- "$portable_root/ComfyUI/runtime/python/LICENSE.txt" \
    "$portable_root/LICENSES/CPython-PSF-2.0.txt"
  "$SCRIPT_DIR/install_runtime_dependencies.sh" "$portable_root/ComfyUI"
else
  mkdir -p -- "$portable_root/ComfyUI/runtime"
  cp -- "$REPO_ROOT/packaging/runtime-constraints.txt" \
    "$portable_root/ComfyUI/runtime/requirements.lock"
  log "WARNING: --skip-runtime creates a structural-only staging tree"
fi

if ((skip_appimage == 0)); then
  launcher_python="$portable_root/ComfyUI/runtime/python/bin/python-portable"
  if ((skip_runtime)); then
    launcher_python="${BUILD_PYTHON:-python3}"
  fi
  "$SCRIPT_DIR/build_appimage.sh" "$portable_root" --work-dir "$work_dir/appimage" \
    --build-python "$launcher_python"
  if ((skip_runtime == 0)); then
    # Keep the shipped interpreter free of bytecode tied to the CI build path.
    "$SCRIPT_DIR/repair_python_runtime.sh" "$portable_root/ComfyUI/runtime/python"
  fi
else
  log "WARNING: --skip-appimage creates a structural-only staging tree"
fi

SOURCE_DATE_EPOCH="$SOURCE_DATE_EPOCH" python3 \
  "$SCRIPT_DIR/generate_environment_manifest.py" "$portable_root" \
  --generation-id "$(environment_generation_id)" \
  --core-version "$COMFY_VERSION" --core-tag "$COMFY_TAG" --core-commit "$COMFY_COMMIT" \
  --frontend-version "$FRONTEND_VERSION" --frontend-commit "$FRONTEND_COMMIT" \
  --python "$PYTHON_VERSION" --torch "$TORCH_VERSION" \
  --torchvision "$TORCHVISION_VERSION" --torchaudio "$TORCHAUDIO_VERSION" \
  --cuda "$CUDA_VERSION" --requirements-lock-sha256 "$RUNTIME_LOCK_SHA256"
python3 "$SCRIPT_DIR/generate_file_manifest.py" "$portable_root"

if ((skip_runtime || skip_appimage)); then
  "$SCRIPT_DIR/preflight_portable.sh" "$portable_root" --structural
  log "structural staging tree retained at $portable_root; no incomplete archive was emitted"
  exit 0
fi

"$SCRIPT_DIR/preflight_portable.sh" "$portable_root"
# The first manifest lets preflight verify the assembled tree. Seal it again
# afterwards so any legitimate relocation repair performed by preflight is
# represented in the archive. PYTHONDONTWRITEBYTECODE keeps imports read-only.
python3 "$SCRIPT_DIR/generate_file_manifest.py" "$portable_root"
python3 "$SCRIPT_DIR/verify_file_manifest.py" "$portable_root"
create_deterministic_tar_gz "$portable_root" "$archive"
assert_safe_archive_paths "$archive"
"$SCRIPT_DIR/preflight_portable.sh" "$archive"
log "created $archive ($(du -h "$archive" | cut -f1))"
