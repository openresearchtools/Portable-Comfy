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
require_command curl python3 setsid

# The spaces are intentional: this is the relocation/path-safety test.
temporary="$(mktemp -d "${TMPDIR:-/tmp}/Portable Comfy smoke.XXXXXX")"
server_pid=""
desktop_pid=""
xvfb_pid=""
cleanup() {
  if [[ -n "$desktop_pid" ]]; then
    kill -TERM -- "-$desktop_pid" 2>/dev/null || true
    sleep 0.5
    kill -KILL -- "-$desktop_pid" 2>/dev/null || true
  fi
  if [[ -n "$server_pid" ]]; then
    kill -TERM -- "-$server_pid" 2>/dev/null || true
    for _ in {1..50}; do
      kill -0 "$server_pid" 2>/dev/null || break
      sleep 0.1
    done
    kill -KILL -- "-$server_pid" 2>/dev/null || true
  fi
  if [[ -n "$xvfb_pid" ]]; then
    kill -TERM "$xvfb_pid" 2>/dev/null || true
    wait "$xvfb_pid" 2>/dev/null || true
  fi
  rm -rf -- "$temporary"
}
trap cleanup EXIT INT TERM
assert_safe_archive_paths "$target"
tar -xzf "$target" -C "$temporary" --no-same-owner --no-same-permissions
root="$temporary/Portable-Comfy"
[[ -d "$root" ]] || die "archive top-level directory is not Portable-Comfy"
"$SCRIPT_DIR/preflight_portable.sh" "$root"

python="$root/ComfyUI/runtime/python/bin/python-portable"
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
  PYTHONNOUSERSITE=1 XDG_CACHE_HOME="$root/ComfyUI/runtime/cache" XDG_CONFIG_HOME="$root/config" \
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
  require_command Xvfb
  display_file="$temporary/xvfb-display"
  Xvfb -displayfd 3 -screen 0 1920x1200x24 -nolisten tcp \
    3>"$display_file" >"$temporary/xvfb.log" 2>&1 &
  xvfb_pid=$!
  display_deadline=$((SECONDS + 15))
  until [[ -s "$display_file" ]]; do
    if ! kill -0 "$xvfb_pid" 2>/dev/null; then
      sed -n '1,160p' "$temporary/xvfb.log" >&2
      die "Xvfb exited before publishing a display"
    fi
    ((SECONDS < display_deadline)) || die "Xvfb did not become ready"
    sleep 0.1
  done
  DISPLAY=":$(<"$display_file")"
  export DISPLAY
fi

require_command xwininfo
if ! xwininfo -root >/dev/null 2>&1; then
  die "desktop smoke cannot inspect the X11 root window on DISPLAY=$DISPLAY"
fi
before_windows="$temporary/windows-before"
LC_ALL=C xwininfo -root -tree 2>/dev/null \
  | awk '/"Portable Comfy"/ && /"portable-comfy" "portable-comfy"/ {print $1}' \
  | sort -u >"$before_windows"

debug_port="$("$python" - <<'PY'
import socket
with socket.socket() as value:
    value.bind(("127.0.0.1", 0))
    print(value.getsockname()[1])
PY
)"
frontend_ready="$temporary/frontend-loaded"
surface_ack="$temporary/surface-validated"
surface_png="$temporary/webengine-surface.png"
rm -f -- "$frontend_ready" "$surface_ack" "$surface_png"

log "starting the actual AppImage desktop smoke on XCB with loopback DevTools"
setsid env \
  -u QT_QPA_PLATFORM \
  -u QT_XCB_GL_INTEGRATION \
  -u QT_QUICK_BACKEND \
  -u QTWEBENGINE_CHROMIUM_FLAGS \
  APPIMAGE_EXTRACT_AND_RUN=1 \
  PORTABLE_COMFY_ROOT="$root" \
  PORTABLE_COMFY_DESKTOP_SMOKE_READY="$frontend_ready" \
  PORTABLE_COMFY_DESKTOP_SMOKE_ACK="$surface_ack" \
  QTWEBENGINE_REMOTE_DEBUGGING="127.0.0.1:$debug_port" \
  "${desktop_command[@]}" >"$desktop_log" 2>&1 &
desktop_pid=$!
desktop_deadline=$((SECONDS + timeout_seconds))
owned_server_pid=""
window_id=""
window_width=""
window_height=""

while [[ ! -f "$frontend_ready" || -z "$window_id" ]]; do
  if ! kill -0 "$desktop_pid" 2>/dev/null; then
    wait "$desktop_pid" || true
    desktop_pid=""
    sed -n '1,240p' "$desktop_log" >&2
    die "AppImage exited before presenting a loaded desktop surface"
  fi
  if [[ -z "$owned_server_pid" && -f "$root/state/server.json" ]]; then
    owned_server_pid="$(python3 - "$root/state/server.json" <<'PY'
import json
import sys
try:
    print(int(json.load(open(sys.argv[1], encoding="utf-8"))["pid"]))
except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
    pass
PY
)"
  fi
  if [[ -f "$frontend_ready" ]]; then
    while IFS= read -r candidate; do
      [[ -n "$candidate" ]] || continue
      grep -Fqx -- "$candidate" "$before_windows" && continue
      info="$(LC_ALL=C xwininfo -id "$candidate" 2>/dev/null || true)"
      grep -Fq 'Map State: IsViewable' <<<"$info" || continue
      candidate_width="$(awk -F: '/^[[:space:]]*Width:/ {gsub(/[[:space:]]/, "", $2); print $2; exit}' <<<"$info")"
      candidate_height="$(awk -F: '/^[[:space:]]*Height:/ {gsub(/[[:space:]]/, "", $2); print $2; exit}' <<<"$info")"
      if [[ "$candidate_width" =~ ^[0-9]+$ && "$candidate_height" =~ ^[0-9]+$ \
        && "$candidate_width" -ge 800 && "$candidate_height" -ge 600 ]]; then
        window_id="$candidate"
        window_width="$candidate_width"
        window_height="$candidate_height"
        break
      fi
    done < <(LC_ALL=C xwininfo -root -tree 2>/dev/null \
      | awk '/"Portable Comfy"/ && /"portable-comfy" "portable-comfy"/ {print $1}' \
      | sort -u)
  fi
  if ((SECONDS >= desktop_deadline)); then
    sed -n '1,240p' "$desktop_log" >&2
    die "AppImage did not present a loaded, mapped desktop window within ${timeout_seconds}s"
  fi
  sleep 0.1
done

if ! python3 "$SCRIPT_DIR/verify_webengine_surface.py" \
  "http://127.0.0.1:$debug_port" "$surface_png" --timeout 20; then
  sed -n '1,240p' "$desktop_log" >&2
  die "AppImage WebEngine viewport did not render usable pixels"
fi
touch -- "$surface_ack"
log "validated mapped desktop window $window_id (${window_width}x${window_height}) and rendered WebEngine pixels"

while kill -0 "$desktop_pid" 2>/dev/null; do
  if ((SECONDS >= desktop_deadline)); then
    sed -n '1,240p' "$desktop_log" >&2
    die "AppImage did not close after desktop surface validation"
  fi
  sleep 0.1
done
set +e
wait "$desktop_pid"
desktop_status=$?
set -e
desktop_pid=""
if ((desktop_status != 0)); then
  sed -n '1,240p' "$desktop_log" >&2
  die "AppImage desktop smoke exited with status $desktop_status"
fi
[[ ! -e "$root/state/server.json" ]] \
  || die "desktop window closed but left ComfyUI server state behind"
if [[ -n "$owned_server_pid" ]] && kill -0 "$owned_server_pid" 2>/dev/null; then
  die "desktop window closed but owned ComfyUI server PID $owned_server_pid is still alive"
fi
log "artifact smoke passed: API, node registry, compiled frontend, mapped rendered AppImage window, and owned-server shutdown"
