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

require_command curl git python3 sha256sum tar unzip
mkdir -p -- "$cache_dir"
source_archive="$cache_dir/ComfyUI-${COMFY_COMMIT}.tar.gz"
frontend_wheel="$cache_dir/comfyui_frontend_package-${FRONTEND_VERSION}-py3-none-any.whl"
frontend_license="$cache_dir/ComfyUI-frontend-${FRONTEND_COMMIT}-LICENSE"
frontend_notices="$cache_dir/ComfyUI-frontend-${FRONTEND_COMMIT}-THIRD_PARTY_NOTICES.md"
download_verified "$COMFY_ARCHIVE_URL" "$source_archive" "$COMFY_ARCHIVE_SHA256"
download_verified "$FRONTEND_WHEEL_URL" "$frontend_wheel" "$FRONTEND_WHEEL_SHA256"
download_verified "$FRONTEND_LICENSE_URL" "$frontend_license" "$FRONTEND_LICENSE_SHA256"
download_verified "$FRONTEND_NOTICES_URL" "$frontend_notices" "$FRONTEND_NOTICES_SHA256"

safe_rm_tree "$destination"
mkdir -p -- "$destination"
temporary="$(mktemp -d "${destination}.extract.XXXXXX")"
trap 'rm -rf -- "$temporary"' EXIT
tar -xzf "$source_archive" -C "$temporary" --no-same-owner --no-same-permissions
extracted="$(find "$temporary" -mindepth 1 -maxdepth 1 -type d -print -quit)"
[[ -n "$extracted" && -f "$extracted/main.py" ]] || die "ComfyUI archive has unexpected layout"
python3 "$SCRIPT_DIR/verify_core_identity.py" \
  "$extracted" "$COMFY_VERSION" "$FRONTEND_VERSION" \
  --frontend-wheel "$frontend_wheel" \
  --core-commit "$COMFY_COMMIT" --frontend-commit "$FRONTEND_COMMIT" \
  --verify-upstream-tags
cp -a -- "$extracted/." "$destination/"
rm -rf -- "$destination/.github" "$destination/.git"

mkdir -p -- "$destination/frontend"
unzip -q "$frontend_wheel" 'comfyui_frontend_package/static/*' -d "$temporary/frontend-wheel"
cp -a -- "$temporary/frontend-wheel/comfyui_frontend_package/static/." "$destination/frontend/"
[[ -s "$destination/frontend/index.html" ]] || die "frontend wheel has unexpected layout"
# The frontend wheel contains only compiled static assets and no dist-info
# license metadata. Bind the notices from the exact pinned source commit into
# the same replaceable ComfyUI generation as those assets.
install -m 0644 -- "$frontend_license" "$destination/frontend/LICENSE"
install -m 0644 -- "$frontend_notices" "$destination/frontend/THIRD_PARTY_NOTICES.md"

if find "$destination" -type l -print -quit | grep -q .; then
  die "ComfyUI update content contains symbolic links"
fi
find "$destination" -type d -exec chmod 0755 {} +
find "$destination" -type f -exec chmod 0644 {} +
chmod 0755 "$destination/main.py"
log "prepared ComfyUI $COMFY_TAG with frontend $FRONTEND_VERSION at $destination"
