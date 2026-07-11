#!/usr/bin/env bash
# Write the exact runtime compatibility contract consumed by Core updates.

set -Eeuo pipefail
IFS=$'\n\t'
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"

portable_root="${1:-}"
[[ -n "$portable_root" ]] || die "usage: $0 PORTABLE_ROOT"
portable_root="$(absolute_path "$portable_root")"
mkdir -p -- "$portable_root/manifest"
cat >"$portable_root/manifest/runtime.json" <<EOF
{
  "python": "$PYTHON_VERSION",
  "torch": "$TORCH_VERSION",
  "cuda": "$CUDA_VERSION",
  "platform": "linux-x86_64",
  "requirements_lock_sha256": "$RUNTIME_LOCK_SHA256"
}
EOF
