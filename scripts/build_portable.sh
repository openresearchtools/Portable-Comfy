#!/usr/bin/env bash
# Build the standalone launcher archive and retain a complete environment source tree.

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
environment_root="$work_dir/environment-source/Portable-Comfy"
archive="$output_dir/Portable-Comfy-linux-x86_64.tar.gz"

require_command python3 tar gzip sha256sum
verify_runtime_constraints
if ((skip_runtime && skip_appimage == 0)); then
  die "--skip-runtime requires --skip-appimage because the AppImage is frozen with the portable interpreter"
fi
safe_rm_tree "$portable_root"
safe_rm_tree "$environment_root"
mkdir -p -- "$portable_root" "$environment_root/LICENSES" "$output_dir"
"$SCRIPT_DIR/prepare_core.sh" "$environment_root/ComfyUI"

# Persistent data is deliberately outside the atomically replaceable ComfyUI/.
data_dirs=(
  custom_nodes custom_node_runtime
  input output temp workflows
  logs config manifest user/default
  models/checkpoints models/configs models/loras models/vae models/text_encoders
  models/clip models/unet models/diffusion_models models/clip_vision models/style_models
  models/embeddings models/diffusers models/vae_approx models/controlnet models/t2i_adapter
  models/gligen models/upscale_models models/latent_upscale_models models/hypernetworks
  models/photomaker models/classifiers models/model_patches models/audio_encoders
  models/background_removal models/frame_interpolation models/geometry_estimation
  models/optical_flow models/detection
  state cache
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
cat >"$portable_root/LICENSES/README.txt" <<'EOF'
Portable Comfy redistribution notices
======================================

Portable-Comfy-GPL-3.0.txt applies to the launcher in this project.
TAESD-MIT.txt applies to the bundled preview encoder/decoder model weights.

CPython-PSF-2.0.txt applies to the CPython library frozen into the AppImage.

Notices for the frozen launcher packages (including PyWebView, PyInstaller,
PyQt6, Qt and Qt WebEngine) are under launcher-python-packages/. Notices and
a complete provenance.tsv ledger for every PyInstaller source are under
launcher-native-packages/. Its packages.tsv binds every Debian-owned host
input to the copied package copyright and exact version. Every Debian
/usr/share/common-licenses reference is mirrored under common-licenses/ and
mapped in common-licenses.tsv; SHA256SUMS authenticates the complete native
notice inventory. python-native/ contains the complete checksum-bound Debian
notice/common-license inventory for build-host libraries frozen from the
portable interpreter's lib/portable-native/ closure. The embedded AppImage
type-2 runtime notice is AppImage-runtime-MIT.txt. Qt-*-attributions/ mirrors
the version-pinned Qt and Qt WebEngine/Chromium third-party attribution pages.
AppImage-runtime-source/ is a self-verifying source/relink bundle for the
statically linked AppImage runtime. It contains exact source archives and
license texts for musl, libfuse, squashfuse, zstd, zlib and mimalloc; the
Alpine package recipes and applied patches; both project patches; build-input
metadata; and the runtime object needed to relink a modified LGPL libfuse.
EOF
"$SCRIPT_DIR/install_builtin_models.sh" "$portable_root"

if ((skip_runtime == 0)); then
  "$SCRIPT_DIR/build_python_runtime.sh" "$environment_root/ComfyUI" --work-dir "$work_dir/python"
  cp -- "$environment_root/ComfyUI/runtime/python/LICENSE.txt" \
    "$portable_root/LICENSES/CPython-PSF-2.0.txt"
  "$SCRIPT_DIR/install_runtime_dependencies.sh" "$environment_root/ComfyUI"
else
  mkdir -p -- "$environment_root/ComfyUI/runtime"
  cp -- "$REPO_ROOT/packaging/runtime-constraints.txt" \
    "$environment_root/ComfyUI/runtime/requirements.lock"
  log "WARNING: --skip-runtime creates a structural-only staging tree"
fi

if ((skip_appimage == 0)); then
  launcher_python="$environment_root/ComfyUI/runtime/python/bin/python-portable"
  if ((skip_runtime)); then
    launcher_python="${BUILD_PYTHON:-python3}"
  fi
  "$SCRIPT_DIR/build_appimage.sh" "$environment_root" --work-dir "$work_dir/appimage" \
    --build-python "$launcher_python"
  if ((skip_runtime == 0)); then
    # Keep the shipped interpreter free of bytecode tied to the CI build path.
    "$SCRIPT_DIR/repair_python_runtime.sh" "$environment_root/ComfyUI/runtime/python"
  fi
  cp -- "$environment_root/Portable-Comfy.AppImage" "$portable_root/Portable-Comfy.AppImage"
  chmod 0755 "$portable_root/Portable-Comfy.AppImage"
  cp -a -- "$environment_root/LICENSES/launcher-python-packages" \
    "$portable_root/LICENSES/launcher-python-packages"
  cp -a -- "$environment_root/LICENSES/launcher-native-packages" \
    "$portable_root/LICENSES/launcher-native-packages"
  cp -a -- "$environment_root/LICENSES/python-native" \
    "$portable_root/LICENSES/python-native"
  cp -- "$environment_root/LICENSES/AppImage-runtime-MIT.txt" \
    "$portable_root/LICENSES/AppImage-runtime-MIT.txt"
  cp -a -- "$environment_root/LICENSES/AppImage-runtime-source" \
    "$portable_root/LICENSES/AppImage-runtime-source"
  cp -- "$environment_root/LICENSES/QtWebEngine-Chromium-BSD-3-Clause.txt" \
    "$portable_root/LICENSES/QtWebEngine-Chromium-BSD-3-Clause.txt"
  cp -a -- "$environment_root/LICENSES/Qt-${QT_RUNTIME_VERSION}-attributions" \
    "$portable_root/LICENSES/Qt-${QT_RUNTIME_VERSION}-attributions"
else
  log "WARNING: --skip-appimage creates a structural-only staging tree"
fi

SOURCE_DATE_EPOCH="$SOURCE_DATE_EPOCH" python3 \
  "$SCRIPT_DIR/generate_environment_manifest.py" "$environment_root" \
  --generation-id "$(environment_generation_id)" \
  --core-version "$COMFY_VERSION" --core-tag "$COMFY_TAG" --core-commit "$COMFY_COMMIT" \
  --frontend-version "$FRONTEND_VERSION" --frontend-commit "$FRONTEND_COMMIT" \
  --python "$PYTHON_VERSION" --torch "$TORCH_VERSION" \
  --torchvision "$TORCHVISION_VERSION" --torchaudio "$TORCHAUDIO_VERSION" \
  --cuda "$CUDA_VERSION" --requirements-lock-sha256 "$RUNTIME_LOCK_SHA256"
environment_verify_args=("$environment_root" --portable-root)
if ((skip_runtime)); then
  environment_verify_args+=(--structural)
fi
python3 "$SCRIPT_DIR/verify_environment_bundle.py" "${environment_verify_args[@]}"
python3 "$SCRIPT_DIR/generate_file_manifest.py" "$portable_root"

if ((skip_runtime || skip_appimage)); then
  "$SCRIPT_DIR/preflight_portable.sh" "$portable_root" --structural
  log "structural launcher tree retained at $portable_root; no incomplete archive was emitted"
  log "structural environment source retained at $environment_root"
  exit 0
fi

"$SCRIPT_DIR/preflight_portable.sh" "$portable_root"
create_deterministic_tar_gz "$portable_root" "$archive"
assert_safe_archive_paths "$archive"
"$SCRIPT_DIR/preflight_portable.sh" "$archive"
log "created $archive ($(du -h "$archive" | cut -f1))"
log "retained complete environment source at $environment_root"
