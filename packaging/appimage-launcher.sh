#!/bin/sh
# Stable Qt WebEngine defaults for the frozen Linux desktop launcher.

set -eu

APPDIR=${APPDIR:-$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd -P)}

# Qt 6's Wayland/RHI auto-selection can produce a loaded WebEngine page without
# ever presenting a usable window on NVIDIA systems. Prefer the proven XCB path
# when XWayland is available, but remain usable on hardened Wayland sessions
# that intentionally do not export DISPLAY. Every explicit user value wins.
native_wayland_fallback=0
if [ -z "${QT_QPA_PLATFORM+x}" ]; then
  if [ -z "${DISPLAY:-}" ] && [ -n "${WAYLAND_DISPLAY:-}" ]; then
    QT_QPA_PLATFORM=wayland
    native_wayland_fallback=1
  else
    QT_QPA_PLATFORM=xcb
  fi
  export QT_QPA_PLATFORM
fi
if [ -z "${QT_XCB_GL_INTEGRATION+x}" ]; then
  QT_XCB_GL_INTEGRATION=none
  export QT_XCB_GL_INTEGRATION
fi
if [ -z "${QT_QUICK_BACKEND+x}" ]; then
  QT_QUICK_BACKEND=software
  export QT_QUICK_BACKEND
fi
if [ -z "${QTWEBENGINE_CHROMIUM_FLAGS+x}" ]; then
  QTWEBENGINE_CHROMIUM_FLAGS='--disable-gpu --disable-gpu-compositing'
  export QTWEBENGINE_CHROMIUM_FLAGS
fi

# The no-XWayland diagnostic path must not let a GTK platform theme inherited
# from a Snap/IDE try to reconnect through X11 or consume sandbox-private
# GSettings schemas. Fusion is fully bundled with Qt.
if [ "$native_wayland_fallback" -eq 1 ]; then
  unset GDK_BACKEND XDG_CURRENT_DESKTOP DESKTOP_SESSION GNOME_DESKTOP_SESSION_ID
  if [ -z "${QT_STYLE_OVERRIDE+x}" ]; then
    QT_STYLE_OVERRIDE=Fusion
    export QT_STYLE_OVERRIDE
  fi
fi

# Launching from a Snap-packaged IDE or terminal can leak GTK/GIO module paths
# from the host sandbox into the AppImage. Those modules are ABI-coupled to the
# host and are never inputs to this Qt-only shell. Keep session/display/DBus and
# user-selected Qt variables intact while removing only plugin-path pollution.
unset GTK_PATH GTK_EXE_PREFIX GTK_DATA_PREFIX GTK_MODULES GTK2_RC_FILES
unset GTK_IM_MODULE_FILE
unset GDK_PIXBUF_MODULEDIR GDK_PIXBUF_MODULE_FILE
unset GIO_EXTRA_MODULES GIO_MODULE_DIR GI_TYPELIB_PATH GSETTINGS_SCHEMA_DIR
unset SNAP SNAP_ARCH SNAP_COMMON SNAP_CONTEXT SNAP_DATA SNAP_EUID
unset SNAP_COOKIE SNAP_DESKTOP_RUNTIME SNAP_INSTANCE_KEY SNAP_INSTANCE_NAME
unset SNAP_LAUNCHER_ARCH_TRIPLET SNAP_LIBRARY_PATH SNAP_NAME
unset SNAP_REAL_HOME SNAP_REEXEC SNAP_REVISION SNAP_UID SNAP_USER_COMMON
unset SNAP_USER_DATA SNAP_VERSION

exec "$APPDIR/usr/lib/portable-comfy/portable-comfy" "$@"
