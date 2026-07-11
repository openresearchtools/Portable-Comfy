#!/usr/bin/env bash
# Install the tiny, redistributable TAESD preview models ComfyUI expects.

set -Eeuo pipefail
IFS=$'\n\t'
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"

portable_root="${1:-}"
[[ -n "$portable_root" ]] || die "usage: $0 PORTABLE_ROOT"
portable_root="$(absolute_path "$portable_root")"
cache_dir="${CACHE_DIR:-$REPO_ROOT/.cache/portable-comfy}"
archive="$cache_dir/taesd-${TAESD_COMMIT}.tar.gz"
destination="$portable_root/models/vae_approx"
expected=(
  taef1_decoder.safetensors taef1_encoder.safetensors
  taesd3_decoder.safetensors taesd3_encoder.safetensors
  taesd_decoder.safetensors taesd_encoder.safetensors
  taesdxl_decoder.safetensors taesdxl_encoder.safetensors
)

require_command curl sha256sum tar python3
download_verified "$TAESD_ARCHIVE_URL" "$archive" "$TAESD_ARCHIVE_SHA256"
temporary="$(mktemp -d)"
trap 'rm -rf -- "$temporary"' EXIT
tar -xzf "$archive" -C "$temporary" --no-same-owner --no-same-permissions
source_dir="$(find "$temporary" -mindepth 1 -maxdepth 1 -type d -print -quit)"
[[ -n "$source_dir" && -f "$source_dir/LICENSE" ]] || die "TAESD archive has unexpected layout"
mkdir -p -- "$destination" "$portable_root/LICENSES" "$portable_root/manifest"
for name in "${expected[@]}"; do
  [[ -s "$source_dir/$name" ]] || die "TAESD archive is missing $name"
  install -m 0644 -- "$source_dir/$name" "$destination/$name"
done
cp -- "$source_dir/LICENSE" "$portable_root/LICENSES/TAESD-MIT.txt"

python3 - "$portable_root" "$TAESD_COMMIT" "${expected[@]}" <<'PY'
import hashlib
import json
from pathlib import Path
import sys

root, commit, *names = sys.argv[1:]
root = Path(root)
files = []
for name in names:
    path = root / "models" / "vae_approx" / name
    files.append(
        {
            "path": path.relative_to(root).as_posix(),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "size": path.stat().st_size,
        }
    )
(root / "manifest" / "builtin-models.json").write_text(
    json.dumps({"schema_version": 1, "taesd_commit": commit, "files": files}, indent=2, sort_keys=True) + "\n",
    encoding="utf-8",
)
PY
log "installed ${#expected[@]} pinned TAESD preview models"
