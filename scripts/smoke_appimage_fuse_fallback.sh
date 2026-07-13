#!/usr/bin/env bash
# Prove that a built AppImage automatically falls back when FUSE is unavailable.

set -Eeuo pipefail
IFS=$'\n\t'
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"

usage() {
  cat <<'EOF'
Usage: smoke_appimage_fuse_fallback.sh APPIMAGE

Runs APPIMAGE --version in a pinned Ubuntu 22.04 container with no network or
/dev/fuse. The smoke succeeds only if the custom runtime reports its automatic
extraction fallback, the frozen launcher prints the repository version, and the
runtime removes its temporary extraction directory.
EOF
}

if (($# == 1)) && [[ "$1" == -h || "$1" == --help ]]; then
  usage
  exit 0
fi
[[ $# -eq 1 ]] || {
  usage >&2
  exit 2
}

require_command docker python3 realpath
appimage="$(realpath -e -- "$1")"
[[ -f "$appimage" && -x "$appimage" ]] \
  || die "AppImage is missing or not executable: $appimage"

expected_version="$(python3 - "$REPO_ROOT/src/portable_comfy/__init__.py" <<'PY'
import ast
import pathlib
import sys

source = pathlib.Path(sys.argv[1])
tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
for statement in tree.body:
    if not isinstance(statement, ast.Assign):
        continue
    if not any(
        isinstance(target, ast.Name) and target.id == "__version__"
        for target in statement.targets
    ):
        continue
    if isinstance(statement.value, ast.Constant) and isinstance(
        statement.value.value, str
    ):
        print(f"Portable Comfy {statement.value.value}")
        break
else:
    raise SystemExit("launcher __version__ is not a literal string")
PY
)"
[[ -n "$expected_version" ]] || die "could not determine launcher version"

# Pin the amd64 root filesystem used by the x86-64 AppImage smoke. Pulling the
# image happens before the isolated test container starts; the container itself
# always has Docker's `none` network and receives no host devices.
container_image="${PORTABLE_COMFY_SMOKE_CONTAINER_IMAGE:-docker.io/library/ubuntu:22.04@sha256:0d779ea97881505f5ef0039336ee85edba27519bdba968c284c86ee066a973c8}"
if ! docker image inspect "$container_image" >/dev/null 2>&1; then
  docker pull "$container_image"
fi

log "running automatic no-FUSE fallback smoke in $container_image"
docker run --rm --interactive \
  --pull never \
  --platform linux/amd64 \
  --network none \
  --read-only \
  --cap-drop ALL \
  --security-opt no-new-privileges \
  --pids-limit 128 \
  --user 65534:65534 \
  --env HOME=/tmp/home \
  --env TMPDIR=/tmp \
  --tmpfs /tmp:rw,noexec,nosuid,nodev,size=16777216,mode=1777 \
  --tmpfs /portable:rw,exec,nosuid,nodev,size=2147483648,mode=0700,uid=65534,gid=65534 \
  --mount "type=bind,source=$appimage,target=/portable/Portable-Comfy.AppImage,readonly" \
  "$container_image" /bin/bash -s -- "$expected_version" <<'CONTAINER'
set -Eeuo pipefail
IFS=$'\n\t'

appimage=/portable/Portable-Comfy.AppImage
expected_version="$1"

[[ ! -e /dev/fuse ]] || {
  printf 'unexpected /dev/fuse device in isolated smoke container\n' >&2
  exit 1
}
[[ -z "${APPIMAGE_EXTRACT_AND_RUN+x}" ]] || {
  printf 'APPIMAGE_EXTRACT_AND_RUN was set before invoking the AppImage\n' >&2
  exit 1
}
[[ -z "${NO_CLEANUP+x}" ]] || {
  printf 'NO_CLEANUP was set before invoking the AppImage\n' >&2
  exit 1
}
mkdir -p -- "$HOME"

set +e
launcher_output="$("$appimage" --version 2>&1)"
launcher_status=$?
set -e
printf '%s\n' "$launcher_output"

shopt -s nullglob
leftovers=(
  /portable/.mount_*
  /portable/.portable-comfy-appimage-*
  /tmp/.mount_*
  /tmp/appimage_extracted_*
  /tmp/.portable-comfy-appimage-*
)
if ((${#leftovers[@]})); then
  printf 'AppImage extraction directory was not removed:\n' >&2
  printf '  %s\n' "${leftovers[@]}" >&2
  exit 1
fi
if ((launcher_status != 0)); then
  printf 'AppImage --version exited with status %d\n' "$launcher_status" >&2
  exit 1
fi
grep -Fq -- 'FUSE mount unavailable (' <<<"$launcher_output" || {
  printf 'automatic no-FUSE fallback diagnostic was not emitted\n' >&2
  exit 1
}
grep -Fqx -- "$expected_version" <<<"$launcher_output" || {
  printf 'frozen launcher did not print expected version: %s\n' \
    "$expected_version" >&2
  exit 1
}
CONTAINER
log "automatic no-FUSE fallback reached $expected_version and cleaned its extraction tree"
