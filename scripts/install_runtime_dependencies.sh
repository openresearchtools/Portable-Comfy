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
"$python" "$SCRIPT_DIR/collect_licenses.py" "$comfyui_root/runtime/LICENSES/python-packages"

# Node-only packages live outside the replaceable environment. A .pth import
# hook uses addsitedir (rather than plain PYTHONPATH semantics) so nested .pth
# files installed in that persistent overlay are processed as well. Candidate
# update preflight omits the environment variable and therefore stays isolated.
"$python" - <<'PY'
from pathlib import Path
import sysconfig

hook = Path(sysconfig.get_path("purelib")) / "portable_comfy_node_overlay.pth"
hook.write_text(
    "import os,site; p=os.environ.get('PORTABLE_COMFY_NODE_SITE_PACKAGES'); "
    "p and site.addsitedir(p)\n",
    encoding="utf-8",
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

# pip has just generated console scripts, so perform relocation repair last.
"$SCRIPT_DIR/repair_python_runtime.sh" "$prefix"
log "installed and validated ComfyUI/CUDA runtime dependencies"
