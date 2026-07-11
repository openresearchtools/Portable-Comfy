#!/usr/bin/env bash
# Remove build-prefix assumptions from a source-built CPython prefix.

set -Eeuo pipefail
IFS=$'\n\t'
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"

prefix="${1:-}"
[[ -n "$prefix" ]] || die "usage: $0 PYTHON_PREFIX"
prefix="$(absolute_path "$prefix")"
[[ -x "$prefix/bin/python3" ]] || die "not a Python prefix: $prefix"
require_command file python3 readelf

repair_rpath() {
  local binary="$1" wanted="$2"
  if command -v patchelf >/dev/null 2>&1; then
    patchelf --set-rpath "$wanted" "$binary"
  elif ! readelf -d "$binary" 2>/dev/null | grep -Fq '$ORIGIN'; then
    die "patchelf is unavailable and $binary lacks an ORIGIN-relative rpath"
  fi
}

# The executable finds libpython relative to itself after the tree is moved.
while IFS= read -r -d '' binary; do
  if file -Lb "$binary" | grep -q '^ELF '; then
    repair_rpath "$binary" '$ORIGIN/../lib'
  fi
done < <(find "$prefix/bin" -maxdepth 1 -type f -name 'python3*' -print0)
while IFS= read -r -d '' library; do
  if file -Lb "$library" | grep -q '^ELF '; then
    repair_rpath "$library" '$ORIGIN'
  fi
done < <(find "$prefix/lib" -maxdepth 1 -type f -name 'libpython*.so*' -print0)

# pkg-config files are text and otherwise preserve the build machine prefix.
while IFS= read -r -d '' pc_file; do
  python3 - "$pc_file" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
lines = path.read_text(encoding="utf-8").splitlines()
for index, line in enumerate(lines):
    if line.startswith("prefix="):
        lines[index] = "prefix=${pcfiledir}/../.."
    elif line.startswith("exec_prefix="):
        lines[index] = "exec_prefix=${prefix}"
path.write_text("\n".join(lines) + "\n", encoding="utf-8")
PY
done < <(find "$prefix/lib/pkgconfig" -type f -name '*.pc' -print0 2>/dev/null || true)

# Python's generated config helper is also made relative to its own bin/
# directory. Third-party native extensions often call this script directly.
while IFS= read -r -d '' config_script; do
  python3 - "$config_script" <<'PY'
from pathlib import Path
import re
import sys

path = Path(sys.argv[1])
text = path.read_text(encoding="utf-8")
text = re.sub(
    r"^prefix=.*$",
    'prefix=${prefix_real}',
    text,
    count=1,
    flags=re.MULTILINE,
)
text = re.sub(r"^exec_prefix=.*$", 'exec_prefix=${prefix}', text, count=1, flags=re.MULTILINE)
text = text.replace(
    'prefix_real=$(installed_prefix "$0")',
    'prefix_real=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)',
)
path.write_text(text, encoding="utf-8")
PY
done < <(find "$prefix/bin" -maxdepth 1 -type f -name 'python*-config' -print0)

# sysconfig's generated build dictionary retains configure-time paths. This
# hook rewrites those values in memory from their stale prefix to the current
# executable-derived prefix. It contains no build path itself and therefore
# continues to work after any number of moves.
site_packages="$prefix/lib/python${PYTHON_VERSION%.*}/site-packages"
mkdir -p -- "$site_packages"
cat >"$site_packages/sitecustomize.py" <<'PY'
"""Portable Comfy CPython relocation fixups."""
from __future__ import annotations

import os
import sys
import sysconfig


def _rewrite(value: str, old: str, new: str) -> str:
    if value == old:
        return new
    prefix = old.rstrip(os.sep) + os.sep
    if value.startswith(prefix):
        return new.rstrip(os.sep) + os.sep + value[len(prefix) :]
    return value.replace(prefix, new.rstrip(os.sep) + os.sep)


_current_prefix = os.path.realpath(sys.prefix)
_variables = sysconfig.get_config_vars()
_stale_prefixes = {
    os.path.realpath(value)
    for key in ("prefix", "exec_prefix", "base", "platbase", "installed_base", "installed_platbase")
    if isinstance((value := _variables.get(key)), str) and os.path.isabs(value)
}
for _key, _value in tuple(_variables.items()):
    if not isinstance(_value, str):
        continue
    for _old_prefix in _stale_prefixes:
        if _old_prefix != _current_prefix:
            _value = _rewrite(_value, _old_prefix, _current_prefix)
    _variables[_key] = _value
for _key in ("prefix", "exec_prefix", "base", "platbase", "installed_base", "installed_platbase"):
    if _key in _variables:
        _variables[_key] = _current_prefix
PY

# pip console entrypoints contain an absolute shebang. Convert those files to a
# standard shell/Python polyglot that selects the adjacent portable interpreter.
python3 - "$prefix" <<'PY'
from pathlib import Path
import os
import sys

prefix = Path(sys.argv[1]).resolve()
bin_dir = prefix / "bin"
for path in sorted(bin_dir.iterdir()):
    if not path.is_file() or path.is_symlink() or path.name.startswith("python"):
        continue
    try:
        data = path.read_bytes()
    except OSError:
        continue
    first, separator, rest = data.partition(b"\n")
    if not separator or not first.startswith(b"#!") or b"python" not in first.lower():
        continue
    header = (
        b"#!/bin/sh\n"
        b"'''exec' \"$(CDPATH= cd -- \"$(dirname -- \"$0\")\" && pwd -P)/python3\" -s \"$0\" \"$@\"\n"
        b"' '''\n"
    )
    path.write_bytes(header + rest)
    path.chmod(path.stat().st_mode | 0o111)
PY

cat >"$prefix/bin/python-portable" <<'EOF'
#!/bin/sh
set -eu
bindir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
prefix=$(CDPATH= cd -- "$bindir/.." && pwd -P)
export PYTHONHOME="$prefix"
export PYTHONNOUSERSITE=1
export LD_LIBRARY_PATH="$prefix/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
exec "$bindir/python3" -s "$@"
EOF
cat >"$prefix/bin/pip-portable" <<'EOF'
#!/bin/sh
set -eu
bindir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
exec "$bindir/python-portable" -m pip "$@"
EOF
chmod 0755 "$prefix/bin/python-portable" "$prefix/bin/pip-portable"
cat >"$prefix/bin/repair-portable-entrypoints" <<'EOF'
#!/bin/sh
set -eu
bindir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
"$bindir/python-portable" - "$bindir" <<'PY'
from pathlib import Path
import sys

bin_dir = Path(sys.argv[1])
header = (
    b"#!/bin/sh\n"
    b"'''exec' \"$(CDPATH= cd -- \"$(dirname -- \"$0\")\" && pwd -P)/python3\" -s \"$0\" \"$@\"\n"
    b"' '''\n"
)
for path in sorted(bin_dir.iterdir()):
    if not path.is_file() or path.is_symlink() or path.name.startswith("python"):
        continue
    try:
        data = path.read_bytes()
    except OSError:
        continue
    first, separator, rest = data.partition(b"\n")
    if separator and first.startswith(b"#!") and b"python" in first.lower():
        path.write_bytes(header + rest)
        path.chmod(path.stat().st_mode | 0o111)
PY
prefix=$(CDPATH= cd -- "$bindir/.." && pwd -P)
printf '%s\n' "$prefix" >"$prefix/.portable-comfy-prefix"
EOF
chmod 0755 "$prefix/bin/repair-portable-entrypoints"
printf '%s\n' "$prefix" >"$prefix/.portable-comfy-prefix"

# Build-tree filenames in bytecode are both non-reproducible and misleading
# after relocation. Python may recreate caches in the writable portable tree.
find "$prefix" -type f \( -name '*.pyc' -o -name '*.pyo' \) -delete
find "$prefix" -depth -type d -name __pycache__ -empty -delete

"$prefix/bin/python-portable" - <<'PY'
import json
import pathlib
import sys
import sysconfig

prefix = pathlib.Path(sys.prefix).resolve()
purelib = pathlib.Path(sysconfig.get_path("purelib")).resolve()
assert purelib.is_relative_to(prefix), (prefix, purelib)
for key in ("BINDIR", "LIBDIR", "INCLUDEPY", "CONFINCLUDEPY", "LIBPL"):
    value = sysconfig.get_config_var(key)
    if value:
        assert pathlib.Path(value).resolve().is_relative_to(prefix), (key, value, prefix)
print(json.dumps({"executable": sys.executable, "prefix": str(prefix), "purelib": str(purelib)}))
PY
log "repaired relocatable Python metadata in $prefix"
