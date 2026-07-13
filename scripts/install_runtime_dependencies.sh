#!/usr/bin/env bash
# Install the pinned CUDA/ComfyUI runtime into the bundled interpreter.

set -Eeuo pipefail
IFS=$'\n\t'
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"

comfyui_root="${1:-}"
[[ -n "$comfyui_root" ]] || die "usage: $0 COMFYUI_ROOT"
comfyui_root="$(absolute_path "$comfyui_root")"
prefix="$comfyui_root/runtime/python"
python="$prefix/bin/python-portable"
[[ -x "$python" && -f "$comfyui_root/requirements.txt" ]] \
  || die "portable Python or ComfyUI requirements are missing"
require_command curl sha256sum tar

mkdir -p -- "$comfyui_root/runtime/LICENSES/python-packages"
constraints="$comfyui_root/runtime/requirements.lock"
cp -- "$REPO_ROOT/packaging/runtime-constraints.txt" "$constraints"
printf '%s  %s\n' "$RUNTIME_LOCK_SHA256" "$constraints" | sha256sum --check --status \
  || die "committed runtime constraints do not match RUNTIME_LOCK_SHA256"

export PIP_DISABLE_PIP_VERSION_CHECK=1
export PIP_NO_INPUT=1
export PIP_CACHE_DIR="${PIP_CACHE_DIR:-$REPO_ROOT/.cache/pip}"
"$python" -m pip install --upgrade --constraint "$constraints" pip setuptools wheel
"$python" -m pip install --extra-index-url "$PYTORCH_INDEX_URL" --constraint "$constraints" \
  "torch==$TORCH_VERSION" "torchvision==$TORCHVISION_VERSION" "torchaudio==$TORCHAUDIO_VERSION"
"$python" -m pip install --extra-index-url "$PYTORCH_INDEX_URL" --constraint "$constraints" \
  -r "$comfyui_root/requirements.txt" \
  -r "$comfyui_root/manager_requirements.txt" \
  "pygit2==$PYGIT2_VERSION"
"$python" -m pip check
"$python" -m pip freeze --all | LC_ALL=C sort >"$comfyui_root/runtime/installed-requirements.txt"

notice_cache="$REPO_ROOT/.cache/portable-comfy/runtime-license-notices"
notice_work="$(mktemp -d "$comfyui_root/runtime/.license-notices.XXXXXX")"
trap 'rm -rf -- "$notice_work"' EXIT

workflow_license="$notice_cache/workflow-templates-${WORKFLOW_TEMPLATES_LICENSE_COMMIT}-LICENSE"
pyopengl_sdist="$notice_cache/PyOpenGL-${PYOPENGL_NOTICE_VERSION}.tar.gz"
sentencepiece_sdist="$notice_cache/sentencepiece-${SENTENCEPIECE_NOTICE_VERSION}.tar.gz"
spandrel_license="$notice_cache/spandrel-${SPANDREL_LICENSE_COMMIT}-LICENSE"
tokenizers_sdist="$notice_cache/tokenizers-${TOKENIZERS_NOTICE_VERSION}.tar.gz"
trampoline_license="$notice_cache/trampoline-${TRAMPOLINE_LICENSE_COMMIT}-LICENSE"
download_verified "$WORKFLOW_TEMPLATES_LICENSE_URL" "$workflow_license" \
  "$WORKFLOW_TEMPLATES_LICENSE_SHA256"
download_verified "$PYOPENGL_SDIST_URL" "$pyopengl_sdist" \
  "$PYOPENGL_SDIST_SHA256"
download_verified "$SENTENCEPIECE_SDIST_URL" "$sentencepiece_sdist" \
  "$SENTENCEPIECE_SDIST_SHA256"
download_verified "$SPANDREL_LICENSE_URL" "$spandrel_license" \
  "$SPANDREL_LICENSE_SHA256"
download_verified "$TOKENIZERS_SDIST_URL" "$tokenizers_sdist" \
  "$TOKENIZERS_SDIST_SHA256"
download_verified "$TRAMPOLINE_LICENSE_URL" "$trampoline_license" \
  "$TRAMPOLINE_LICENSE_SHA256"

extract_notice() {
  local archive="$1" member="$2" destination="$3"
  if ! tar -xOf "$archive" "$member" >"$destination.part"; then
    rm -f -- "$destination.part"
    die "required notice is missing from $(basename -- "$archive"): $member"
  fi
  [[ -s "$destination.part" ]] || {
    rm -f -- "$destination.part"
    die "required notice is empty in $(basename -- "$archive"): $member"
  }
  mv -f -- "$destination.part" "$destination"
}

pyopengl_license="$notice_work/PyOpenGL-LICENSE.txt"
sentencepiece_license="$notice_work/sentencepiece-LICENSE.txt"
sentencepiece_absl_license="$notice_work/sentencepiece-third-party-absl-LICENSE.txt"
sentencepiece_darts_license="$notice_work/sentencepiece-third-party-darts-LICENSE.txt"
sentencepiece_esaxx_license="$notice_work/sentencepiece-third-party-esaxx-LICENSE.txt"
sentencepiece_protobuf_license="$notice_work/sentencepiece-third-party-protobuf-lite-LICENSE.txt"
tokenizers_license="$notice_work/tokenizers-LICENSE.txt"
extract_notice "$pyopengl_sdist" \
  "pyopengl-${PYOPENGL_NOTICE_VERSION}/license.txt" "$pyopengl_license"
extract_notice "$sentencepiece_sdist" \
  "sentencepiece-${SENTENCEPIECE_NOTICE_VERSION}/sentencepiece/LICENSE" \
  "$sentencepiece_license"
extract_notice "$sentencepiece_sdist" \
  "sentencepiece-${SENTENCEPIECE_NOTICE_VERSION}/sentencepiece/third_party/absl/LICENSE" \
  "$sentencepiece_absl_license"
extract_notice "$sentencepiece_sdist" \
  "sentencepiece-${SENTENCEPIECE_NOTICE_VERSION}/sentencepiece/third_party/darts_clone/LICENSE" \
  "$sentencepiece_darts_license"
extract_notice "$sentencepiece_sdist" \
  "sentencepiece-${SENTENCEPIECE_NOTICE_VERSION}/sentencepiece/third_party/esaxx/LICENSE" \
  "$sentencepiece_esaxx_license"
extract_notice "$sentencepiece_sdist" \
  "sentencepiece-${SENTENCEPIECE_NOTICE_VERSION}/sentencepiece/third_party/protobuf-lite/LICENSE" \
  "$sentencepiece_protobuf_license"
extract_notice "$tokenizers_sdist" \
  "tokenizers-${TOKENIZERS_NOTICE_VERSION}/tokenizers/LICENSE" \
  "$tokenizers_license"

"$python" "$SCRIPT_DIR/prepare_runtime_license_extras.py" \
  "$notice_work" "$constraints" "$workflow_license" \
  --notice-version "comfyui-workflow-templates=$WORKFLOW_TEMPLATES_VERSION" \
  --notice-version "cuda-toolkit=$CUDA_TOOLKIT_META_VERSION" \
  --notice-version "pyopengl=$PYOPENGL_NOTICE_VERSION" \
  --notice-version "sentencepiece=$SENTENCEPIECE_NOTICE_VERSION" \
  --notice-version "spandrel=$SPANDREL_NOTICE_VERSION" \
  --notice-version "tokenizers=$TOKENIZERS_NOTICE_VERSION" \
  --notice-version "trampoline=$TRAMPOLINE_NOTICE_VERSION"

workflow_donor_license="$notice_work/workflow-templates-donor-LICENSE.txt"
cuda_donor_license="$notice_work/nvidia-cuda-runtime-donor-LICENSE.txt"
license_arguments=(
  --extra-license-file "comfyui_frontend_package=$comfyui_root/frontend/LICENSE"
  --extra-license-file "comfyui-workflow-templates-core=$workflow_donor_license"
  --extra-license-file "comfyui-workflow-templates-json=$workflow_donor_license"
  --extra-license-file "comfyui-workflow-templates-media-api=$workflow_donor_license"
  --extra-license-file "comfyui-workflow-templates-media-assets-01=$workflow_donor_license"
  --extra-license-file "comfyui-workflow-templates-media-image=$workflow_donor_license"
  --extra-license-file "comfyui-workflow-templates-media-other=$workflow_donor_license"
  --extra-license-file "comfyui-workflow-templates-media-video=$workflow_donor_license"
  --extra-license-file "cuda-toolkit=$cuda_donor_license"
  --extra-license-file "PyOpenGL=$pyopengl_license"
  --extra-license-file "sentencepiece=$sentencepiece_license"
  --extra-license-file "sentencepiece=$sentencepiece_absl_license"
  --extra-license-file "sentencepiece=$sentencepiece_darts_license"
  --extra-license-file "sentencepiece=$sentencepiece_esaxx_license"
  --extra-license-file "sentencepiece=$sentencepiece_protobuf_license"
  --extra-license-file "spandrel=$spandrel_license"
  --extra-license-file "tokenizers=$tokenizers_license"
  --extra-license-file "trampoline=$trampoline_license"
)
for required_notice in \
  comfyui_frontend_package \
  comfyui-workflow-templates-core \
  comfyui-workflow-templates-json \
  comfyui-workflow-templates-media-api \
  comfyui-workflow-templates-media-assets-01 \
  comfyui-workflow-templates-media-image \
  comfyui-workflow-templates-media-other \
  comfyui-workflow-templates-media-video \
  cuda-toolkit \
  PyOpenGL \
  sentencepiece \
  spandrel \
  tokenizers \
  trampoline \
  torch \
  torchvision \
  torchaudio \
  nvidia-cublas \
  nvidia-cuda-runtime \
  nvidia-cudnn-cu13; do
  license_arguments+=(--require-license-file "$required_notice")
done
"$python" "$SCRIPT_DIR/collect_licenses.py" \
  "$comfyui_root/runtime/LICENSES/python-packages" \
  "${license_arguments[@]}"

"$python" - "$comfyui_root/runtime/LICENSES/python-packages/packages.json" <<'PY'
import json
import pathlib
import sys

inventory = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
summary = inventory["summary"]
if summary["metadata_only"] or summary["unidentified"]:
    raise SystemExit(
        "runtime dependency notices are incomplete: "
        f"metadata_only={summary['metadata_only']}, "
        f"unidentified={summary['unidentified']}"
    )
PY

"$python" - <<PY
import importlib.util
import json
import pathlib
import platform
import torch
import torchvision
import torchaudio

assert platform.python_version() == "$PYTHON_VERSION"
assert torch.__version__ == "$TORCH_VERSION", torch.__version__
assert torchvision.__version__ == "$TORCHVISION_VERSION", torchvision.__version__
assert torchaudio.__version__ == "$TORCHAUDIO_VERSION", torchaudio.__version__
assert importlib.util.find_spec("comfyui_manager") is not None
assert importlib.util.find_spec("pygit2") is not None
print(json.dumps({"python": platform.python_version(), "torch": torch.__version__, "cuda": torch.version.cuda}))
PY

# The CUDA wheel includes optional NVSHMEM cluster plugins whose MPI/PMIx,
# OpenSHMEM, fabric, UCX and InfiniBand stacks are intentionally supplied by an
# HPC site rather than the wheel. Remove only the exact reviewed plugin bytes;
# retain core/device/UID/local functionality and record every exclusion.
runtime_exclusions="$comfyui_root/runtime/LICENSES/runtime-exclusions"
python3 "$SCRIPT_DIR/prune_runtime_plugins.py" prune "$prefix" \
  --manifest-root "$runtime_exclusions"

# Wheels can add ELF extensions and helper executables.  Recompute the complete
# closure after installation so no package can silently inherit a non-ABI
# library from the Ubuntu build runner.  This also refreshes exact Debian
# package/version/notices for every copied shared library.
python3 "$SCRIPT_DIR/python_native_closure.py" bundle "$prefix" \
  --license-root "$comfyui_root/runtime/LICENSES/python-native"
python3 "$SCRIPT_DIR/prune_runtime_plugins.py" finalize "$prefix" \
  --manifest-root "$runtime_exclusions"

# pip has just generated console scripts, so perform relocation repair last.
"$SCRIPT_DIR/repair_python_runtime.sh" "$prefix"
log "installed and validated ComfyUI/CUDA runtime dependencies"
