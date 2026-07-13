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

require_command curl git pnpm python3 sha256sum tar unzip
mkdir -p -- "$cache_dir"
source_archive="$cache_dir/ComfyUI-${COMFY_COMMIT}.tar.gz"
frontend_source="$cache_dir/ComfyUI-frontend-${FRONTEND_COMMIT}.tar.gz"
frontend_wheel="$cache_dir/comfyui_frontend_package-${FRONTEND_VERSION}-py3-none-any.whl"
frontend_license="$cache_dir/ComfyUI-frontend-${FRONTEND_COMMIT}-LICENSE"
frontend_notices="$cache_dir/ComfyUI-frontend-${FRONTEND_COMMIT}-THIRD_PARTY_NOTICES.md"
frontend_notice_cache="$cache_dir/frontend-dependency-notices"
mkdir -p -- "$frontend_notice_cache"
algolia_license="$frontend_notice_cache/algolia-LICENSE"
posthog_license="$frontend_notice_cache/posthog-LICENSE"
tiptap_license="$frontend_notice_cache/tiptap-LICENSE.md"
vue_devtools_license="$frontend_notice_cache/vue-devtools-LICENSE"
xterm_license="$frontend_notice_cache/xterm-LICENSE"
firebase_license="$frontend_notice_cache/firebase-LICENSE"
boolbase_license="$frontend_notice_cache/boolbase-LICENSE"
lucide_license="$frontend_notice_cache/lucide-LICENSE"
uuid_license="$frontend_notice_cache/uuid-LICENSE.md"
inter_license="$frontend_notice_cache/inter-OFL-1.1.txt"
mdi_license="$frontend_notice_cache/material-design-icons-LICENSE.txt"
desktop_bridge_license="$frontend_notice_cache/comfyui-desktop-bridge-MIT.txt"
download_verified "$COMFY_ARCHIVE_URL" "$source_archive" "$COMFY_ARCHIVE_SHA256"
download_verified "$FRONTEND_SOURCE_URL" "$frontend_source" "$FRONTEND_SOURCE_SHA256"
download_verified "$FRONTEND_WHEEL_URL" "$frontend_wheel" "$FRONTEND_WHEEL_SHA256"
download_verified "$FRONTEND_LICENSE_URL" "$frontend_license" "$FRONTEND_LICENSE_SHA256"
download_verified "$FRONTEND_NOTICES_URL" "$frontend_notices" "$FRONTEND_NOTICES_SHA256"
download_verified "$FRONTEND_ALGOLIA_LICENSE_URL" "$algolia_license" \
  "$FRONTEND_ALGOLIA_LICENSE_SHA256"
download_verified "$FRONTEND_POSTHOG_LICENSE_URL" "$posthog_license" \
  "$FRONTEND_POSTHOG_LICENSE_SHA256"
download_verified "$FRONTEND_TIPTAP_LICENSE_URL" "$tiptap_license" \
  "$FRONTEND_TIPTAP_LICENSE_SHA256"
download_verified "$FRONTEND_VUE_DEVTOOLS_LICENSE_URL" "$vue_devtools_license" \
  "$FRONTEND_VUE_DEVTOOLS_LICENSE_SHA256"
download_verified "$FRONTEND_XTERM_LICENSE_URL" "$xterm_license" \
  "$FRONTEND_XTERM_LICENSE_SHA256"
download_verified "$FRONTEND_FIREBASE_LICENSE_URL" "$firebase_license" \
  "$FRONTEND_FIREBASE_LICENSE_SHA256"
download_verified "$FRONTEND_BOOLBASE_LICENSE_URL" "$boolbase_license" \
  "$FRONTEND_BOOLBASE_LICENSE_SHA256"
download_verified "$FRONTEND_LUCIDE_LICENSE_URL" "$lucide_license" \
  "$FRONTEND_LUCIDE_LICENSE_SHA256"
download_verified "$FRONTEND_UUID_LICENSE_URL" "$uuid_license" \
  "$FRONTEND_UUID_LICENSE_SHA256"
download_verified "$FRONTEND_INTER_LICENSE_URL" "$inter_license" \
  "$FRONTEND_INTER_LICENSE_SHA256"
download_verified "$FRONTEND_MDI_LICENSE_URL" "$mdi_license" \
  "$FRONTEND_MDI_LICENSE_SHA256"
download_verified "$FRONTEND_DESKTOP_BRIDGE_LICENSE_URL" \
  "$desktop_bridge_license" "$FRONTEND_DESKTOP_BRIDGE_LICENSE_SHA256"

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

# The compiled wheel omits its JavaScript dependency licenses. Resolve the
# production graph from the exact source lock with scripts disabled, then copy
# its packed notices into a relocatable inventory. Keep that pinned source
# snapshot beside the compiled assets as well.
assert_safe_archive_paths "$frontend_source"
mkdir -p -- "$temporary/frontend-source"
tar -xzf "$frontend_source" -C "$temporary/frontend-source" \
  --no-same-owner --no-same-permissions
frontend_source_root="$(find "$temporary/frontend-source" -mindepth 1 -maxdepth 1 -type d -print -quit)"
[[ -n "$frontend_source_root" && -f "$frontend_source_root/pnpm-lock.yaml" ]] \
  || die "frontend source archive has unexpected layout"
(
  cd -- "$frontend_source_root"
  CI=1 pnpm install --prod --frozen-lockfile --ignore-scripts \
    --filter '@comfyorg/comfyui-frontend...'
  pnpm --filter '@comfyorg/comfyui-frontend...' \
    licenses list --prod --json > "$temporary/frontend-licenses.json"
)
python3 "$SCRIPT_DIR/collect_frontend_licenses.py" \
  "$temporary/frontend-licenses.json" "$frontend_source_root" \
  "$destination/frontend/LICENSES/npm" \
  --frontend-version "$FRONTEND_VERSION" --frontend-commit "$FRONTEND_COMMIT" \
  --compiled-root "$destination/frontend" \
  --workspace-license "$frontend_license" \
  --fallback-license "@algolia/*=$algolia_license" \
  --fallback-license "@posthog/core=$posthog_license" \
  --fallback-license "@tiptap/*=$tiptap_license" \
  --fallback-license "@vue/devtools-api=$vue_devtools_license" \
  --fallback-license "@xterm/addon-serialize=$xterm_license" \
  --fallback-license "@firebase/*=$firebase_license" \
  --fallback-license "firebase=$firebase_license" \
  --fallback-license "@comfyorg/comfyui-electron-types=$frontend_license" \
  --fallback-license \
    "@comfyorg/comfyui-desktop-bridge-types=$desktop_bridge_license" \
  --fallback-license "boolbase=$boolbase_license" \
  --fallback-license "@iconify-json/lucide=$lucide_license" \
  --additional-package uuid 11.1.0 MIT "$uuid_license" \
  --additional-asset uuid 11.1.0 assets/vendor-other-ifjiCTHW.js \
    "$FRONTEND_UUID_BUNDLE_SHA256" \
  --additional-asset uuid 11.1.0 assets/vendor-other-ifjiCTHW.js.map \
    "$FRONTEND_UUID_SOURCEMAP_SHA256" \
  --additional-package @fontsource-variable/inter 5.2.7 OFL-1.1 \
    "$inter_license" \
  --additional-asset @fontsource-variable/inter 5.2.7 \
    fonts/inter-latin-normal.woff2 "$FRONTEND_INTER_NORMAL_SHA256" \
  --additional-asset @fontsource-variable/inter 5.2.7 \
    fonts/inter-latin-italic.woff2 "$FRONTEND_INTER_ITALIC_SHA256" \
  --additional-package @mdi/font 7.4.47 Apache-2.0 "$mdi_license" \
  --additional-asset @mdi/font 7.4.47 \
    fonts/materialdesignicons-webfont.woff2 "$FRONTEND_MDI_FONT_SHA256"
install -m 0644 -- "$frontend_source" \
  "$destination/frontend/SOURCE-ComfyUI-frontend-${FRONTEND_VERSION}.tar.gz"

if find "$destination" -type l -print -quit | grep -q .; then
  die "ComfyUI update content contains symbolic links"
fi
find "$destination" -type d -exec chmod 0755 {} +
find "$destination" -type f -exec chmod 0644 {} +
chmod 0755 "$destination/main.py"
log "prepared ComfyUI $COMFY_TAG with frontend $FRONTEND_VERSION at $destination"
