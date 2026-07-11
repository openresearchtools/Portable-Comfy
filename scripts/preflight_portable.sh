#!/usr/bin/env bash
# Verify archive safety, layout, relocation, pinned versions, and update hashes.

set -Eeuo pipefail
IFS=$'\n\t'
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
[[ -n "$target" ]] || die "usage: $0 PATH_TO_PORTABLE_ROOT_OR_TARBALL [--structural]"
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
  temporary="$(mktemp -d "${TMPDIR:-/tmp}/Portable Comfy preflight.XXXXXX")"
  tar -xzf "$target" -C "$temporary" --no-same-owner --no-same-permissions
  mapfile -t roots < <(find "$temporary" -mindepth 1 -maxdepth 1 -type d -print)
  ((${#roots[@]} == 1)) || die "full archive must contain one top-level directory"
  root="${roots[0]}"
elif [[ -d "$target" ]]; then
  root="$target"
else
  die "target does not exist: $target"
fi

required_dirs=(ComfyUI ComfyUI/frontend custom_nodes models input output temp workflows user logs config manifest runtime)
for path in "${required_dirs[@]}"; do
  [[ -d "$root/$path" ]] || die "portable layout is missing directory: $path"
done
[[ -s "$root/ComfyUI/main.py" && -s "$root/ComfyUI/frontend/index.html" ]] \
  || die "Core or frontend entrypoint is missing"
[[ -f "$root/manifest/runtime.json" && -f "$root/manifest/core.json" ]] \
  || die "portable manifests are missing"
taesd_models=(
  taef1_decoder.safetensors taef1_encoder.safetensors
  taesd3_decoder.safetensors taesd3_encoder.safetensors
  taesd_decoder.safetensors taesd_encoder.safetensors
  taesdxl_decoder.safetensors taesdxl_encoder.safetensors
)
for model in "${taesd_models[@]}"; do
  [[ -s "$root/models/vae_approx/$model" ]] || die "bundled TAESD model is missing: $model"
done
[[ -s "$root/manifest/builtin-models.json" && -s "$root/LICENSES/TAESD-MIT.txt" ]] \
  || die "TAESD manifest or MIT license is missing"
[[ -L "$root/user/default/workflows" ]] || die "user/default/workflows must be a relative symlink"
[[ "$(readlink "$root/user/default/workflows")" == ../../workflows ]] \
  || die "workflow symlink has the wrong target"
[[ "$(realpath -m "$root/user/default/workflows")" == "$(realpath -m "$root/workflows")" ]] \
  || die "workflow symlink escapes or resolves incorrectly"

python3 - "$root/manifest/runtime.json" <<PY
import json, sys
value = json.load(open(sys.argv[1], encoding="utf-8"))
expected = {"python": "$PYTHON_VERSION", "torch": "$TORCH_VERSION", "cuda": "$CUDA_VERSION", "platform": "linux-x86_64", "requirements_lock_sha256": "$RUNTIME_LOCK_SHA256"}
assert value == expected, (value, expected)
PY
python3 "$SCRIPT_DIR/verify_core_bundle.py" "$root" \
  --manifest manifest/core.json --checksums manifest/core-checksums.sha256
python3 "$SCRIPT_DIR/verify_file_manifest.py" "$root"

while IFS= read -r -d '' link; do
  destination="$(readlink "$link")"
  [[ "$destination" != /* ]] || die "portable tree contains absolute symlink: ${link#"$root/"}"
  resolved="$(realpath -m "$link")"
  [[ "$resolved" == "$root" || "$resolved" == "$root/"* ]] \
    || die "portable tree contains escaping symlink: ${link#"$root/"}"
done < <(find "$root" -type l -print0)

if ((structural == 0)); then
  python="$root/runtime/python/bin/python-portable"
  appimage="$root/Portable-Comfy.AppImage"
  [[ -x "$python" ]] || die "portable Python launcher is missing"
  [[ -x "$appimage" ]] || die "AppImage is missing or not executable"
  [[ -x "$root/runtime/python/bin/repair-portable-entrypoints" ]] \
    || die "runtime relocation repair tool is missing"
  "$root/runtime/python/bin/repair-portable-entrypoints"
  [[ "$(cat "$root/runtime/python/.portable-comfy-prefix")" == "$(realpath "$root/runtime/python")" ]] \
    || die "runtime prefix stamp was not repaired after relocation"
  if ldd "$root/runtime/python/bin/python3" | grep -q 'not found'; then
    ldd "$root/runtime/python/bin/python3" >&2
    die "portable Python has unresolved libraries"
  fi
  "$python" - "$root" <<PY
import importlib.util, json, pathlib, platform, sys, sysconfig
import torch, torchvision, torchaudio
root = pathlib.Path(sys.argv[1]).resolve()
assert platform.python_version() == "$PYTHON_VERSION"
assert torch.__version__ == "$TORCH_VERSION"
assert torchvision.__version__ == "$TORCHVISION_VERSION"
assert torchaudio.__version__ == "$TORCHAUDIO_VERSION"
assert torch.version.cuda == "$CUDA_VERSION"
assert pathlib.Path(sys.prefix).resolve() == root / "runtime/python"
assert pathlib.Path(sysconfig.get_path("purelib")).resolve().is_relative_to(root / "runtime/python")
for name, value in sysconfig.get_paths().items():
    if value and pathlib.Path(value).is_absolute():
        assert pathlib.Path(value).resolve().is_relative_to(root / "runtime/python"), (name, value)
for name in ("BINDIR", "LIBDIR", "INCLUDEPY", "CONFINCLUDEPY", "LIBPL"):
    value = sysconfig.get_config_var(name)
    if value:
        assert pathlib.Path(value).resolve().is_relative_to(root / "runtime/python"), (name, value)
assert importlib.util.find_spec("comfyui_manager")
assert importlib.util.find_spec("pygit2")
print(json.dumps({"python": platform.python_version(), "torch": torch.__version__, "cuda": torch.version.cuda}))
PY
  require_command cc
  extension_temp="$(mktemp -d "${TMPDIR:-/tmp}/Portable Comfy extension.XXXXXX")"
  extension_dir="$extension_temp/native-extension"
  mkdir -p -- "$extension_dir"
  cat >"$extension_dir/portable_relocation_test.c" <<'C'
#define PY_SSIZE_T_CLEAN
#include <Python.h>

static PyObject *answer(PyObject *self, PyObject *args) {
    return PyLong_FromLong(42);
}

static PyMethodDef methods[] = {
    {"answer", answer, METH_NOARGS, "Return the relocation test sentinel."},
    {NULL, NULL, 0, NULL}
};

static struct PyModuleDef module = {
    PyModuleDef_HEAD_INIT, "portable_relocation_test", NULL, -1, methods
};

PyMODINIT_FUNC PyInit_portable_relocation_test(void) {
    return PyModule_Create(&module);
}
C
  mapfile -t extension_config < <("$python" - <<'PY'
import sysconfig
print(sysconfig.get_config_var("EXT_SUFFIX"))
print(sysconfig.get_path("include"))
PY
  )
  [[ -n "${extension_config[0]:-}" && -d "${extension_config[1]:-}" ]] \
    || die "portable sysconfig cannot build native extensions"
  cc -shared -fPIC -I"${extension_config[1]}" "$extension_dir/portable_relocation_test.c" \
    -o "$extension_dir/portable_relocation_test${extension_config[0]}"
  PYTHONPATH="$extension_dir" "$python" -c \
    'import portable_relocation_test as value; assert value.answer() == 42'
  file -Lb "$appimage" | grep -q 'ELF 64-bit.*x86-64' || die "AppImage is not an x86-64 ELF"
  APPIMAGE_EXTRACT_AND_RUN=1 "$appimage" --appimage-version >/dev/null
fi
log "portable preflight passed: $root"
