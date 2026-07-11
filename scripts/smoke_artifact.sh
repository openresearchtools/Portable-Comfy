#!/usr/bin/env bash
# Start the packaged ComfyUI from a relocated artifact and probe API/frontend.

set -Eeuo pipefail
IFS=$'\n\t'
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
# shellcheck source=lib/common.sh
source "$SCRIPT_DIR/lib/common.sh"

target="${1:-}"
shift || true
timeout_seconds=180
allow_no_gpu=0
while (($#)); do
  case "$1" in
    --timeout) timeout_seconds="$2"; shift 2 ;;
    --allow-no-gpu) allow_no_gpu=1; shift ;;
    *) die "unknown argument: $1" ;;
  esac
done
[[ -n "$target" ]] || die "usage: $0 PATH_TO_TARBALL [--timeout N] [--allow-no-gpu]"
target="$(absolute_path "$target")"
[[ -f "$target" ]] || die "smoke test expects the complete tar.gz artifact"
require_command curl setsid timeout

# The spaces are intentional: this is the relocation/path-safety test.
temporary="$(mktemp -d "${TMPDIR:-/tmp}/Portable Comfy smoke.XXXXXX")"
server_pid=""
cleanup() {
  if [[ -n "$server_pid" ]]; then
    kill -TERM -- "-$server_pid" 2>/dev/null || true
    for _ in {1..50}; do
      kill -0 "$server_pid" 2>/dev/null || break
      sleep 0.1
    done
    kill -KILL -- "-$server_pid" 2>/dev/null || true
  fi
  rm -rf -- "$temporary"
}
trap cleanup EXIT INT TERM
assert_safe_archive_paths "$target"
tar -xzf "$target" -C "$temporary" --no-same-owner --no-same-permissions
root="$temporary/Portable-Comfy"
[[ -d "$root" ]] || die "archive top-level directory is not Portable-Comfy"
"$SCRIPT_DIR/preflight_portable.sh" "$root"

python="$root/runtime/python/bin/python-portable"
port="$("$python" - <<'PY'
import socket
with socket.socket() as value:
    value.bind(("127.0.0.1", 0))
    print(value.getsockname()[1])
PY
)"
log_file="$root/logs/smoke.log"
command=(
  "$python" "$root/ComfyUI/main.py"
  --base-directory "$root"
  --user-directory "$root/user"
  --database-url "sqlite:///$root/user/comfyui.db"
  --extra-model-paths-config "$root/config/extra_model_paths.yaml"
  --front-end-root "$root/ComfyUI/frontend"
  --listen 127.0.0.1 --port "$port"
  --disable-auto-launch --disable-all-custom-nodes --enable-manager --log-stdout
)
if ((allow_no_gpu)); then
  command+=(--cpu)
fi
log "starting relocated ComfyUI on port $port"
setsid env -u VIRTUAL_ENV -u PYTHONPATH -u PYTHONHOME -u LD_PRELOAD -u LD_LIBRARY_PATH \
  PYTHONNOUSERSITE=1 XDG_CACHE_HOME="$root/runtime/cache" XDG_CONFIG_HOME="$root/config" \
  HF_HOME="$root/models/.cache/huggingface" TORCH_HOME="$root/models/.cache/torch" \
  "${command[@]}" >"$log_file" 2>&1 &
server_pid=$!

deadline=$((SECONDS + timeout_seconds))
until curl -fsS --max-time 3 "http://127.0.0.1:$port/system_stats" >"$temporary/system_stats.json"; do
  if ! kill -0 "$server_pid" 2>/dev/null; then
    sed -n '1,240p' "$log_file" >&2
    die "ComfyUI exited before becoming healthy"
  fi
  if ((SECONDS >= deadline)); then
    sed -n '1,240p' "$log_file" >&2
    die "ComfyUI did not become healthy within ${timeout_seconds}s"
  fi
  sleep 1
done
curl -fsS --max-time 10 "http://127.0.0.1:$port/" >"$temporary/index.html"
curl -fsS --max-time 10 "http://127.0.0.1:$port/object_info" >"$temporary/object_info.json"
python3 - "$temporary/system_stats.json" "$temporary/object_info.json" "$temporary/index.html" <<'PY'
import json
import pathlib
import sys

stats = json.loads(pathlib.Path(sys.argv[1]).read_text())
objects = json.loads(pathlib.Path(sys.argv[2]).read_text())
html = pathlib.Path(sys.argv[3]).read_text(errors="replace").lower()
assert isinstance(stats.get("system"), dict), stats
assert isinstance(objects, dict) and objects, "object_info was empty"
assert "<html" in html and ("comfy" in html or "app" in html)
PY
kill -TERM -- "-$server_pid"
wait "$server_pid" || true
server_pid=""

appimage="$root/Portable-Comfy.AppImage"
desktop_log="$root/logs/desktop-smoke.log"
desktop_command=("$appimage" --desktop-smoke-test --disable-custom-nodes)
if ((allow_no_gpu)); then
  desktop_command+=(--cpu)
fi
if [[ -z "${DISPLAY:-}" ]]; then
  command -v xvfb-run >/dev/null 2>&1 \
    || die "desktop smoke requires DISPLAY or xvfb-run"
  desktop_command=(xvfb-run -a -s '-screen 0 1280x800x24' "${desktop_command[@]}")
fi
log "starting the actual AppImage desktop smoke"
if ! timeout --signal=TERM --kill-after=15 "${timeout_seconds}s" \
  env APPIMAGE_EXTRACT_AND_RUN=1 PORTABLE_COMFY_ROOT="$root" \
  "${desktop_command[@]}" >"$desktop_log" 2>&1; then
  sed -n '1,240p' "$desktop_log" >&2
  die "AppImage desktop smoke failed"
fi
log "artifact smoke passed: API, node registry, compiled frontend, and AppImage desktop"
