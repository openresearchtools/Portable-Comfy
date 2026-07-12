#!/usr/bin/env bash
# Verify archive safety, layout, relocation, pinned versions, and update hashes.

set -Eeuo pipefail
IFS=$'\n\t'
# A preflight must be observational: imports such as torch -> pickletools must
# never add bytecode to a payload after its complete-file manifest is sealed.
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
[[ -n "$target" ]] || die "usage: $0 PATH_TO_PORTABLE_ROOT_OR_TARBALL [--structural]"
target="$(absolute_path "$target")"
temporary=""
extension_temp=""
node_venv_test=""
cleanup() {
  [[ -z "$temporary" ]] || rm -rf -- "$temporary"
  [[ -z "$extension_temp" ]] || rm -rf -- "$extension_temp"
  [[ -z "$node_venv_test" ]] || rm -rf -- "$node_venv_test"
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

required_dirs=(
  ComfyUI ComfyUI/frontend ComfyUI/runtime custom_nodes
  custom_node_runtime
  models input output temp workflows user logs config manifest
)
for path in "${required_dirs[@]}"; do
  [[ -d "$root/$path" ]] || die "portable layout is missing directory: $path"
done
[[ ! -e "$root/runtime" && ! -L "$root/runtime" ]] \
  || die "legacy top-level runtime is outside the atomic ComfyUI environment"
[[ -s "$root/ComfyUI/main.py" && -s "$root/ComfyUI/frontend/index.html" ]] \
  || die "Core or frontend entrypoint is missing"
[[ -f "$root/manifest/environment.json" \
   && -f "$root/manifest/environment-checksums.sha256" ]] \
  || die "portable environment manifests are missing"
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
required_notices=(
  LICENSE
  LICENSES/Portable-Comfy-GPL-3.0.txt
  LICENSES/ComfyUI-GPL-3.0.txt
  LICENSES/ComfyUI-Frontend-GPL-3.0.txt
  LICENSES/ComfyUI-Frontend-THIRD-PARTY-NOTICES.md
  LICENSES/README.txt
  ComfyUI/LICENSE
  ComfyUI/frontend/LICENSE
  ComfyUI/frontend/THIRD_PARTY_NOTICES.md
)
for notice in "${required_notices[@]}"; do
  [[ -s "$root/$notice" ]] || die "redistribution notice is missing: $notice"
done
[[ -L "$root/user/default/workflows" ]] || die "user/default/workflows must be a relative symlink"
[[ "$(readlink "$root/user/default/workflows")" == ../../workflows ]] \
  || die "workflow symlink has the wrong target"
[[ "$(realpath -m "$root/user/default/workflows")" == "$(realpath -m "$root/workflows")" ]] \
  || die "workflow symlink escapes or resolves incorrectly"
manager_config="$root/user/__manager/config.ini"
if [[ -f "$manager_config" ]]; then
  python3 - "$manager_config" <<'PY'
import configparser, sys
value = configparser.ConfigParser()
value.read(sys.argv[1], encoding="utf-8")
assert not value.getboolean("default", "use_uv", fallback=True)
assert not value.getboolean("default", "use_unified_resolver", fallback=True)
PY
fi

environment_verify_args=("$root" --portable-root)
if ((structural)); then
  environment_verify_args+=(--structural)
fi
python3 "$SCRIPT_DIR/verify_environment_bundle.py" "${environment_verify_args[@]}"
python3 - "$root/manifest/environment.json" <<PY
import json, sys
value = json.load(open(sys.argv[1], encoding="utf-8"))
assert value["generation_id"] == "$(environment_generation_id)"
assert value["core"] == {
    "version": "$COMFY_VERSION", "tag": "$COMFY_TAG", "commit": "$COMFY_COMMIT"
}
assert value["frontend"] == {
    "version": "$FRONTEND_VERSION", "commit": "$FRONTEND_COMMIT"
}
assert value["runtime"] == {
    "python": "$PYTHON_VERSION",
    "torch": "$TORCH_VERSION",
    "torchvision": "$TORCHVISION_VERSION",
    "torchaudio": "$TORCHAUDIO_VERSION",
    "cuda": "$CUDA_VERSION",
    "platform": "linux-x86_64",
    "requirements_lock_path": "ComfyUI/runtime/requirements.lock",
    "requirements_lock_sha256": "$RUNTIME_LOCK_SHA256",
}
PY
python3 "$SCRIPT_DIR/verify_file_manifest.py" "$root"

while IFS= read -r -d '' link; do
  destination="$(readlink "$link")"
  [[ "$destination" != /* ]] || die "portable tree contains absolute symlink: ${link#"$root/"}"
  resolved="$(realpath -m "$link")"
  [[ "$resolved" == "$root" || "$resolved" == "$root/"* ]] \
    || die "portable tree contains escaping symlink: ${link#"$root/"}"
done < <(find "$root" -type l -print0)

if ((structural == 0)); then
  prefix="$root/ComfyUI/runtime/python"
  python="$prefix/bin/python-portable"
  appimage="$root/Portable-Comfy.AppImage"
  [[ -x "$python" ]] || die "portable Python launcher is missing"
  [[ -x "$appimage" ]] || die "AppImage is missing or not executable"
  [[ -x "$prefix/bin/repair-portable-entrypoints" ]] \
    || die "runtime relocation repair tool is missing"
  runtime_notices="$root/ComfyUI/runtime/LICENSES/python-packages/packages.json"
  launcher_notices="$root/LICENSES/launcher-python-packages/packages.json"
  native_notices="$root/LICENSES/launcher-native-packages/packages.tsv"
  [[ -s "$prefix/LICENSE.txt" \
     && -s "$root/LICENSES/CPython-PSF-2.0.txt" \
     && -s "$root/LICENSES/AppImage-runtime-MIT.txt" \
     && -s "$root/LICENSES/QtWebEngine-Chromium-BSD-3-Clause.txt" \
     && -s "$runtime_notices" \
     && -s "$launcher_notices" \
     && -s "$native_notices" ]] \
    || die "runtime/AppImage redistribution notices are incomplete"
  python3 - "$runtime_notices" "$launcher_notices" "$native_notices" <<'PY'
import csv
import json
import sys

runtime_path, launcher_path, native_path = sys.argv[1:]

def licensed(path):
    data = json.load(open(path, encoding="utf-8"))
    assert data["schema_version"] == 2
    return {
        package["name"].lower().replace("_", "-")
        for package in data["packages"]
        if package["license_files"]
    }

runtime = licensed(runtime_path)
assert {
    "torch", "torchvision", "torchaudio", "nvidia-cublas",
    "nvidia-cuda-runtime", "nvidia-cudnn-cu13", "comfyui-frontend-package",
} <= runtime
launcher = licensed(launcher_path)
assert {
    "portable-comfy", "pyinstaller", "pywebview", "proxy-tools",
    "pyqt6", "pyqt6-qt6",
    "pyqt6-webengine", "pyqt6-webengine-qt6", "pyqt6-sip", "qtpy",
} <= launcher
with open(native_path, encoding="utf-8", newline="") as stream:
    libraries = {row["library"] for row in csv.DictReader(stream, delimiter="\t")}
assert {
    "libpulse.so.0", "libxcb-cursor.so.0", "libxcb-icccm.so.4",
    "libxcb-image.so.0", "libxcb-keysyms.so.1", "libxcb-render-util.so.0",
    "libxcb-shape.so.0", "libxcb-util.so.1", "libxcb-xkb.so.1",
    "libxkbcommon-x11.so.0", "libwayland-client.so.0",
    "libwayland-cursor.so.0", "libwayland-egl.so.1",
    "libwayland-server.so.0",
} <= libraries
PY
  "$prefix/bin/repair-portable-entrypoints"
  [[ "$(cat "$prefix/.portable-comfy-prefix")" == "$(realpath "$prefix")" ]] \
    || die "runtime prefix stamp was not repaired after relocation"
  if ldd "$prefix/bin/python3" | grep -q 'not found'; then
    ldd "$prefix/bin/python3" >&2
    die "portable Python has unresolved libraries"
  fi
  "$python" - "$root" <<PY
import importlib.util, json, pathlib, platform, sys, sysconfig
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
print(json.dumps({"python": platform.python_version(), "torch": torch.__version__, "cuda": torch.version.cuda}))
PY
  node_venv_test="$(mktemp -d "${TMPDIR:-/tmp}/Portable Comfy node venv.XXXXXX")"
  rm -rf -- "$node_venv_test"
  "$python" -m venv --system-site-packages --without-pip "$node_venv_test"
  node_python="$node_venv_test/bin/python"
  "$node_python" - "$node_venv_test" "$prefix" <<'PY'
import pathlib, sys
import pip, torch

venv = pathlib.Path(sys.argv[1]).resolve()
base = pathlib.Path(sys.argv[2]).resolve()
assert pathlib.Path(sys.prefix).resolve() == venv
assert pathlib.Path(sys.base_prefix).resolve() == base
assert pathlib.Path(torch.__file__).resolve().is_relative_to(base)
assert pathlib.Path(pip.__file__).resolve().is_relative_to(base)
PY
  "$python" - "$node_venv_test" <<'PY'
from pathlib import Path
from zipfile import ZipFile
import sys

root = Path(sys.argv[1])
for version in ("1.0", "2.0"):
    dist = f"portable_venv_probe-{version}.dist-info"
    wheel = root / f"portable_venv_probe-{version}-py3-none-any.whl"
    with ZipFile(wheel, "w") as archive:
        archive.writestr("portable_venv_probe.py", f"VERSION = {version!r}\n")
        archive.writestr(
            f"{dist}/METADATA",
            "Metadata-Version: 2.1\nName: portable-venv-probe\n"
            f"Version: {version}\n",
        )
        archive.writestr(
            f"{dist}/WHEEL",
            "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        )
        archive.writestr(
            f"{dist}/RECORD",
            "portable_venv_probe.py,,\n"
            f"{dist}/METADATA,,\n{dist}/WHEEL,,\n{dist}/RECORD,,\n",
        )
PY
  "$node_python" -m pip install --disable-pip-version-check --no-deps \
    "$node_venv_test/portable_venv_probe-1.0-py3-none-any.whl"
  "$node_python" -m pip install --disable-pip-version-check --no-deps \
    "$node_venv_test/portable_venv_probe-2.0-py3-none-any.whl"
  "$node_python" - "$node_venv_test" <<'PY'
from pathlib import Path
import portable_venv_probe
import sys

site = next((Path(sys.argv[1]) / "lib").glob("python*/site-packages"))
assert portable_venv_probe.VERSION == "2.0"
assert [path.name for path in site.glob("portable_venv_probe-*.dist-info")] == [
    "portable_venv_probe-2.0.dist-info"
]
PY
  rm -rf -- "$node_venv_test"
  node_venv_test=""
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
