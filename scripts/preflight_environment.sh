#!/usr/bin/env bash
# Verify a full Core bundle: ComfyUI source/frontend/Python/Torch/CUDA.

set -Eeuo pipefail
IFS=$'\n\t'
export PYTHONDONTWRITEBYTECODE=1
unset PIP_TARGET PYTHONPATH PYTHONHOME VIRTUAL_ENV
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"

target="${1:-}"
shift || true
structural=0
while (($#)); do
  case "$1" in
    --structural) structural=1; shift ;;
    *) die "unknown argument: $1" ;;
  esac
done
[[ -n "$target" ]] || die "usage: $0 CORE_BUNDLE_ROOT_OR_TARBALL [--structural]"
target="$(absolute_path "$target")"
temporary=""
extension_temp=""
cleanup() {
  [[ -z "$temporary" ]] || rm -rf -- "$temporary"
  [[ -z "$extension_temp" ]] || rm -rf -- "$extension_temp"
}
trap cleanup EXIT

if [[ -f "$target" ]]; then
  assert_safe_archive_paths "$target"
  temporary="$(mktemp -d "${TMPDIR:-/tmp}/Portable Comfy core.XXXXXX")"
  tar -xzf "$target" -C "$temporary" --no-same-owner --no-same-permissions
  mapfile -t top_level < <(find "$temporary" -mindepth 1 -maxdepth 1 -print)
  ((${#top_level[@]} == 1)) || die "Core bundle must contain one outer directory"
  [[ -d "${top_level[0]}" && ! -L "${top_level[0]}" ]] \
    || die "Core bundle outer entry must be a directory"
  root="${top_level[0]}"
elif [[ -d "$target" ]]; then
  root="$target"
else
  die "target does not exist: $target"
fi

verify_args=("$root")
if ((structural)); then
  verify_args+=(--structural)
fi
python3 "$SCRIPT_DIR/verify_environment_bundle.py" "${verify_args[@]}"

[[ -s "$root/ComfyUI/LICENSE" \
   && -s "$root/ComfyUI/frontend/LICENSE" \
   && -s "$root/ComfyUI/frontend/THIRD_PARTY_NOTICES.md" \
   && -s "$root/ComfyUI/frontend/LICENSES/npm/packages.json" ]] \
  || die "ComfyUI Core/frontend redistribution notices are incomplete"
frontend_source="$root/ComfyUI/frontend/SOURCE-ComfyUI-frontend-${FRONTEND_VERSION}.tar.gz"
[[ -s "$frontend_source" ]] || die "pinned frontend source snapshot is missing"
if ((structural == 0)); then
  printf '%s  %s\n' "$FRONTEND_SOURCE_SHA256" "$frontend_source" | sha256sum -c -
fi

python3 - "$root/manifest/environment.json" <<PY
import json, sys
value = json.load(open(sys.argv[1], encoding="utf-8"))["runtime"]
expected = {
    "python": "$PYTHON_VERSION",
    "torch": "$TORCH_VERSION",
    "torchvision": "$TORCHVISION_VERSION",
    "torchaudio": "$TORCHAUDIO_VERSION",
    "cuda": "$CUDA_VERSION",
    "platform": "linux-x86_64",
    "requirements_lock_path": "ComfyUI/runtime/requirements.lock",
    "requirements_lock_sha256": "$RUNTIME_LOCK_SHA256",
}
assert value == expected, (value, expected)
PY

if ((structural == 0)); then
  prefix="$root/ComfyUI/runtime/python"
  python="$prefix/bin/python-portable"
  repair="$prefix/bin/repair-portable-entrypoints"
  [[ -x "$python" && -x "$repair" ]] || die "portable environment runtime is incomplete"
  [[ -s "$prefix/LICENSE.txt" \
     && -s "$root/ComfyUI/runtime/LICENSES/python-packages/packages.json" \
     && -s "$root/ComfyUI/runtime/LICENSES/python-native/packages.json" \
     && -s "$root/ComfyUI/runtime/LICENSES/runtime-exclusions/nvshmem-plugin-exclusions.json" ]] \
    || die "CPython or runtime-package redistribution notices are missing"
  "$repair"
  [[ "$(cat "$prefix/.portable-comfy-prefix")" == "$(realpath "$prefix")" ]] \
    || die "environment runtime prefix stamp was not repaired"
  require_command cc ldd readelf
  python3 "$SCRIPT_DIR/prune_runtime_plugins.py" verify "$prefix" \
    --manifest-root "$root/ComfyUI/runtime/LICENSES/runtime-exclusions"
  python3 "$SCRIPT_DIR/python_native_closure.py" audit "$prefix" \
    --license-root "$root/ComfyUI/runtime/LICENSES/python-native"
  "$python" - "$root" <<PY
import importlib.util, pathlib, platform, sys, sysconfig
import torch, torchvision, torchaudio
root = pathlib.Path(sys.argv[1]).resolve()
prefix = root / "ComfyUI/runtime/python"
assert platform.python_version() == "$PYTHON_VERSION"
assert torch.__version__ == "$TORCH_VERSION"
assert torchvision.__version__ == "$TORCHVISION_VERSION"
assert torchaudio.__version__ == "$TORCHAUDIO_VERSION"
assert torch.version.cuda == "$CUDA_VERSION"
assert pathlib.Path(sys.prefix).resolve() == prefix
assert pathlib.Path(sysconfig.get_path("purelib")).resolve().is_relative_to(prefix)
for name, value in sysconfig.get_paths().items():
    if value and pathlib.Path(value).is_absolute():
        assert pathlib.Path(value).resolve().is_relative_to(prefix), (name, value)
for name in ("BINDIR", "LIBDIR", "INCLUDEPY", "CONFINCLUDEPY", "LIBPL"):
    value = sysconfig.get_config_var(name)
    if value:
        assert pathlib.Path(value).resolve().is_relative_to(prefix), (name, value)
assert importlib.util.find_spec("comfyui_manager")
assert importlib.util.find_spec("pygit2")
PY

  extension_temp="$(mktemp -d "${TMPDIR:-/tmp}/Portable Comfy environment extension.XXXXXX")"
  venv="$extension_temp/custom node runtime"
  "$python" -m venv --system-site-packages --without-pip "$venv"
  venv_python="$venv/bin/python"
  [[ -x "$venv_python" ]] || die "portable Python could not create a custom-node venv"
  env -u LD_LIBRARY_PATH -u PYTHONHOME -u PYTHONPATH -u VIRTUAL_ENV \
    "$venv_python" - "$root" "$venv" <<PY
import pathlib, platform, sys, sysconfig
import torch, torchvision, torchaudio

root = pathlib.Path(sys.argv[1]).resolve()
venv = pathlib.Path(sys.argv[2]).resolve()
base = root / "ComfyUI/runtime/python"
assert platform.python_version() == "$PYTHON_VERSION"
assert pathlib.Path(sys.prefix).resolve() == venv, (sys.prefix, venv)
assert pathlib.Path(sys.base_prefix).resolve() == base, (sys.base_prefix, base)
for name in ("purelib", "platlib", "scripts"):
    value = pathlib.Path(sysconfig.get_path(name)).resolve()
    assert value.is_relative_to(venv), (name, value, venv)
for name in ("include", "platinclude"):
    value = pathlib.Path(sysconfig.get_path(name)).resolve()
    assert value.is_relative_to(base), (name, value, base)
for name in ("LIBDIR", "INCLUDEPY", "CONFINCLUDEPY", "LIBPL"):
    value = sysconfig.get_config_var(name)
    if value:
        resolved = pathlib.Path(value).resolve()
        assert resolved.is_relative_to(base), (name, resolved, base)
assert torch.__version__ == "$TORCH_VERSION"
assert torchvision.__version__ == "$TORCHVISION_VERSION"
assert torchaudio.__version__ == "$TORCHAUDIO_VERSION"
PY
  cat >"$extension_temp/portable_environment_test.c" <<'C'
#define PY_SSIZE_T_CLEAN
#include <Python.h>
static PyObject *answer(PyObject *self, PyObject *args) { return PyLong_FromLong(42); }
static PyMethodDef methods[] = {{"answer", answer, METH_NOARGS, NULL}, {NULL, NULL, 0, NULL}};
static struct PyModuleDef module = {PyModuleDef_HEAD_INIT, "portable_environment_test", NULL, -1, methods};
PyMODINIT_FUNC PyInit_portable_environment_test(void) { return PyModule_Create(&module); }
C
  mapfile -t extension_config < <(env -u LD_LIBRARY_PATH -u PYTHONHOME -u PYTHONPATH -u VIRTUAL_ENV \
    "$venv_python" - <<'PY'
import sysconfig
print(sysconfig.get_config_var("EXT_SUFFIX"))
print(sysconfig.get_path("include"))
PY
  )
  [[ -n "${extension_config[0]:-}" && -d "${extension_config[1]:-}" ]] \
    || die "environment sysconfig cannot build native extensions"
  cc -shared -fPIC -I"${extension_config[1]}" "$extension_temp/portable_environment_test.c" \
    -o "$extension_temp/portable_environment_test${extension_config[0]}"
  env -u LD_LIBRARY_PATH -u PYTHONHOME -u VIRTUAL_ENV PYTHONPATH="$extension_temp" \
    "$venv_python" -c \
    'import portable_environment_test as value; assert value.answer() == 42'
fi
log "complete Core bundle preflight passed: $root"
