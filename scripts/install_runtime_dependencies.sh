#!/usr/bin/env bash
# Install the pinned CUDA/ComfyUI runtime into the bundled interpreter.

set -Eeuo pipefail
IFS=$'\n\t'
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"

portable_root="${1:-}"
[[ -n "$portable_root" ]] || die "usage: $0 PORTABLE_ROOT"
portable_root="$(absolute_path "$portable_root")"
prefix="$portable_root/runtime/python"
python="$prefix/bin/python-portable"
[[ -x "$python" && -f "$portable_root/ComfyUI/requirements.txt" ]] \
  || die "portable Python or ComfyUI requirements are missing"

mkdir -p -- "$portable_root/manifest" "$portable_root/LICENSES/python-packages"
constraints="$portable_root/manifest/runtime-constraints.txt"
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
  -r "$portable_root/ComfyUI/requirements.txt" \
  -r "$portable_root/ComfyUI/manager_requirements.txt" \
  "pygit2==$PYGIT2_VERSION"
"$python" -m pip check
"$python" -m pip freeze --all | LC_ALL=C sort >"$portable_root/manifest/runtime-requirements.lock"
"$python" "$SCRIPT_DIR/collect_licenses.py" "$portable_root/LICENSES/python-packages"

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

# pip has just generated console scripts, so perform relocation repair last.
"$SCRIPT_DIR/repair_python_runtime.sh" "$prefix"
log "installed and validated ComfyUI/CUDA runtime dependencies"
