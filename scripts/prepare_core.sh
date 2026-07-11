#!/usr/bin/env bash
# Fetch a pinned ComfyUI Core and materialize its matching compiled frontend.

set -Eeuo pipefail
IFS=$'\n\t'
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"

destination="${1:-}"
cache_dir="${CACHE_DIR:-$REPO_ROOT/.cache/portable-comfy}"
[[ -n "$destination" ]] || die "usage: $0 DESTINATION"
destination="$(absolute_path "$destination")"

require_command curl sha256sum tar unzip
mkdir -p -- "$cache_dir"
source_archive="$cache_dir/ComfyUI-${COMFY_COMMIT}.tar.gz"
frontend_wheel="$cache_dir/comfyui_frontend_package-${FRONTEND_VERSION}-py3-none-any.whl"
download_verified "$COMFY_ARCHIVE_URL" "$source_archive" "$COMFY_ARCHIVE_SHA256"
download_verified "$FRONTEND_WHEEL_URL" "$frontend_wheel" "$FRONTEND_WHEEL_SHA256"

safe_rm_tree "$destination"
mkdir -p -- "$destination"
temporary="$(mktemp -d "${destination}.extract.XXXXXX")"
trap 'rm -rf -- "$temporary"' EXIT
tar -xzf "$source_archive" -C "$temporary" --no-same-owner --no-same-permissions
extracted="$(find "$temporary" -mindepth 1 -maxdepth 1 -type d -print -quit)"
[[ -n "$extracted" && -f "$extracted/main.py" ]] || die "ComfyUI archive has unexpected layout"
cp -a -- "$extracted/." "$destination/"
rm -rf -- "$destination/.github" "$destination/.git"

mkdir -p -- "$destination/frontend"
unzip -q "$frontend_wheel" 'comfyui_frontend_package/static/*' -d "$temporary/frontend-wheel"
cp -a -- "$temporary/frontend-wheel/comfyui_frontend_package/static/." "$destination/frontend/"
[[ -s "$destination/frontend/index.html" ]] || die "frontend wheel has unexpected layout"

cat >"$destination/.portable-comfy.json" <<EOF
{
  "schema_version": 1,
  "core": {"version": "$COMFY_VERSION", "tag": "$COMFY_TAG", "commit": "$COMFY_COMMIT"},
  "frontend": {"version": "$FRONTEND_VERSION", "commit": "$FRONTEND_COMMIT"},
  "runtime": {"python": "$PYTHON_VERSION", "torch": "$TORCH_VERSION", "cuda": "$CUDA_VERSION", "platform": "linux-x86_64", "requirements_lock_sha256": "$RUNTIME_LOCK_SHA256"}
}
EOF

if find "$destination" -type l -print -quit | grep -q .; then
  die "ComfyUI update content contains symbolic links"
fi
find "$destination" -type d -exec chmod 0755 {} +
find "$destination" -type f -exec chmod 0644 {} +
chmod 0755 "$destination/main.py"
log "prepared ComfyUI $COMFY_TAG with frontend $FRONTEND_VERSION at $destination"
