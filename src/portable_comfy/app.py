"""CLI and pywebview desktop shell for Portable Comfy."""

from __future__ import annotations

import argparse
import atexit
import html
import json
import logging
import logging.handlers
import os
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Sequence

from portable_comfy import __version__
from portable_comfy.locking import AlreadyRunningError, InstanceLock
from portable_comfy.paths import LayoutError, PortablePaths
from portable_comfy.supervisor import ServerError, ServerSupervisor
from portable_comfy.updater import CoreUpdater, UpdateError


LOGGER = logging.getLogger("portable_comfy")


def _page(title: str, message: str, *, error: bool = False, busy: bool = False) -> str:
    accent = "#ff647c" if error else "#69e0ff"
    spinner = '<div class="spinner"></div>' if busy else ""
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="color-scheme" content="dark">
<style>
html,body{{height:100%;margin:0;background:#111319;color:#eaf0ff;font-family:Inter,system-ui,sans-serif}}
body{{display:grid;place-items:center}} .card{{width:min(680px,80vw);padding:42px;border:1px solid #303746;
border-radius:20px;background:#191c24;box-shadow:0 24px 80px #0008}} h1{{margin:0 0 14px;font-size:28px}}
p{{line-height:1.55;color:#b9c3d8;white-space:pre-wrap;overflow-wrap:anywhere}} .mark{{color:{accent}}}
.spinner{{width:24px;height:24px;margin-top:24px;border:3px solid #384052;border-top-color:{accent};
border-radius:50%;animation:spin .8s linear infinite}} @keyframes spin{{to{{transform:rotate(360deg)}}}}
</style></head><body><main class="card"><h1><span class="mark">●</span> {html.escape(title)}</h1>
<p>{html.escape(message)}</p>{spinner}</main></body></html>"""


class DesktopController:
    """Threaded native-menu callbacks; no API is exposed to ComfyUI JavaScript."""

    def __init__(
        self,
        window: Any,
        paths: PortablePaths,
        supervisor: ServerSupervisor,
        updater: CoreUpdater,
        *,
        auto_start: bool,
        desktop_smoke: bool = False,
    ) -> None:
        self.window = window
        self.paths = paths
        self.supervisor = supervisor
        self.updater = updater
        self.auto_start = auto_start
        self.desktop_smoke = desktop_smoke
        self.smoke_success: bool | None = None
        self._operation_lock = threading.Lock()
        self._closing = threading.Event()

    def launch(self) -> None:
        if self.auto_start:
            self.start_server()
        else:
            self.window.load_html(
                _page(
                    "Server stopped",
                    "Use Server → Start to launch the bundled ComfyUI Core.",
                )
            )

    def start_server(self) -> None:
        self._background("Starting ComfyUI", self._start)

    def stop_server(self) -> None:
        self._background("Stopping ComfyUI", self._stop)

    def restart_server(self) -> None:
        self._background("Restarting ComfyUI", self._restart)

    def reload(self) -> None:
        if self.supervisor.is_running and self.supervisor.url:
            self.window.load_url(self.supervisor.url)
        else:
            self.window.load_html(
                _page(
                    "Server stopped",
                    "Use Server → Start before reloading the interface.",
                )
            )

    def install_bundle(self) -> None:
        if self._closing.is_set():
            return
        selected = self.window.create_file_dialog(
            10,
            directory=str(self.paths.root),
            allow_multiple=False,
            file_types=(
                "Portable Comfy Core (*.tar.gz;*.tgz)",
                "All files (*.*)",
            ),
        )
        if not selected:
            return
        archive = Path(selected[0])
        if not self.window.create_confirmation_dialog(
            "Install Core bundle",
            f"Validate and install {archive.name}?\n\n"
            "Models, custom nodes, workflows, user data and their Python packages "
            "will not be replaced.",
        ):
            return

        def operation() -> None:
            result = self.updater.install_bundle(archive)
            if self._closing.is_set():
                return
            if self.supervisor.is_running and self.supervisor.url:
                self.window.load_url(self.supervisor.url)
            else:
                self.window.load_html(
                    _page(
                        "Core update installed",
                        f"ComfyUI {result.version} is installed. Use Server → Start when ready.",
                    )
                )

        self._background("Installing Core update", operation)

    def about(self) -> None:
        self.window.create_confirmation_dialog(
            "About Portable Comfy",
            f"Portable Comfy {__version__}\n\n"
            "ComfyUI Core, custom nodes, models, workflows and the Python/CUDA "
            "runtime stay in the extracted portable directory.",
        )

    def closing(self) -> bool:
        self._closing.set()
        try:
            self.supervisor.stop()
        except Exception:
            LOGGER.exception("failed to stop ComfyUI while closing")
        # An updater may be between its atomic swap and health check. Let it
        # finish or roll back, then stop once more so it cannot leave a server
        # behind after the native window has gone away.
        acquired = self._operation_lock.acquire(timeout=330)
        if acquired:
            self._operation_lock.release()
        try:
            self.supervisor.stop()
        except Exception:
            LOGGER.exception("failed final ComfyUI stop while closing")
        return True

    def _start(self) -> None:
        url = self.supervisor.start()
        if self.desktop_smoke:
            self.window.events.loaded.clear()
        self.window.load_url(url)
        if self.desktop_smoke:
            self._schedule_smoke_close(True)

    def _stop(self) -> None:
        self.supervisor.stop()
        self.window.load_html(
            _page("Server stopped", "ComfyUI and its child process group have exited.")
        )

    def _restart(self) -> None:
        url = self.supervisor.restart()
        self.window.load_url(url)

    def _background(self, label: str, operation: Callable[[], None]) -> None:
        if self._closing.is_set():
            return
        if not self._operation_lock.acquire(blocking=False):
            self.window.create_confirmation_dialog(
                "Portable Comfy is busy",
                "Wait for the current server operation to finish.",
            )
            return
        self.window.load_html(
            _page(label, "This can take a moment on first launch.", busy=True)
        )

        def worker() -> None:
            try:
                operation()
            except (LayoutError, ServerError, UpdateError, OSError) as error:
                LOGGER.exception("%s failed", label)
                if not self._closing.is_set():
                    detail = str(error)
                    if isinstance(error, ServerError):
                        detail += "\n\nRecent server log:\n" + self.supervisor.tail_log(
                            35
                        )
                    self.window.load_html(_page(f"{label} failed", detail, error=True))
                if self.desktop_smoke:
                    self._schedule_smoke_close(False)
            except Exception as error:  # GUI must surface unexpected failures too.
                LOGGER.exception("unexpected %s failure", label)
                if not self._closing.is_set():
                    self.window.load_html(
                        _page(f"{label} failed", str(error), error=True)
                    )
                if self.desktop_smoke:
                    self._schedule_smoke_close(False)
            finally:
                self._operation_lock.release()

        threading.Thread(
            target=worker,
            name=f"portable-comfy-{label.lower().replace(' ', '-')}",
            daemon=True,
        ).start()

    def _schedule_smoke_close(self, success: bool) -> None:
        if self.smoke_success is not None:
            return

        def close_when_rendered() -> None:
            rendered = self.window.events.loaded.wait(30) if success else True
            self.smoke_success = bool(success and rendered)
            # Give QtWebEngine one event-loop turn after its loaded signal.
            time.sleep(0.5)
            try:
                self.window.destroy()
            except Exception:
                LOGGER.exception("could not close desktop smoke-test window")

        threading.Thread(
            target=close_when_rendered,
            name="portable-comfy-desktop-smoke-close",
            daemon=True,
        ).start()


def _menu(controller: DesktopController) -> list[Any]:
    from webview.menu import Menu, MenuAction

    return [
        Menu(
            "Server",
            [
                MenuAction("Start", controller.start_server),
                MenuAction("Stop", controller.stop_server),
                MenuAction("Restart", controller.restart_server),
            ],
        ),
        Menu("View", [MenuAction("Reload", controller.reload)]),
        Menu("Core", [MenuAction("Install bundle…", controller.install_bundle)]),
        Menu("Help", [MenuAction("About Portable Comfy", controller.about)]),
    ]


def configure_logging(paths: PortablePaths) -> None:
    paths.logs.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        paths.logs / "launcher.log",
        maxBytes=2 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    LOGGER.setLevel(logging.INFO)
    LOGGER.handlers.clear()
    LOGGER.addHandler(handler)
    if not getattr(sys, "frozen", False):
        console = logging.StreamHandler()
        console.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
        LOGGER.addHandler(console)


def self_test(paths: PortablePaths) -> dict[str, object]:
    paths.create_layout()
    command = paths.comfy_command(8188, validate=False)
    workflow_target = paths.workflow_link.resolve(strict=False)
    if workflow_target != paths.workflows.resolve(strict=False):
        raise LayoutError("workflow link does not resolve inside the portable root")
    if "--base-directory" not in command or "--front-end-root" not in command:
        raise LayoutError("ComfyUI command is missing portable path arguments")
    return {
        "ok": True,
        "root": str(paths.root),
        "workflow_link": os.readlink(paths.workflow_link),
        "database_url": command[command.index("--database-url") + 1],
        "command": command,
    }


def _run_headless(
    supervisor: ServerSupervisor,
    *,
    smoke_test: bool,
    no_auto_start: bool,
) -> int:
    if no_auto_start:
        return 0
    url = supervisor.start()
    print(
        json.dumps({"ok": True, "url": url, "pid": supervisor.status().pid}), flush=True
    )
    if smoke_test:
        supervisor.stop(interrupt_timeout=3.0, terminate_timeout=2.0)
        return 0
    stopped = threading.Event()

    def handle_signal(_signum: int, _frame: object) -> None:
        stopped.set()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)
    try:
        while not stopped.wait(0.5) and supervisor.is_running:
            pass
        return supervisor.status().returncode or 0
    finally:
        supervisor.stop()


def _run_desktop(
    paths: PortablePaths,
    supervisor: ServerSupervisor,
    *,
    no_auto_start: bool,
    desktop_smoke: bool = False,
) -> int:
    import webview

    window = webview.create_window(
        "Portable Comfy",
        html=_page("Portable Comfy", "Preparing the local ComfyUI server…", busy=True),
        width=1440,
        height=920,
        min_size=(900, 620),
        background_color="#111319",
        text_select=True,
        zoomable=True,
    )
    if window is None:
        raise RuntimeError("pywebview did not create the application window")
    updater = CoreUpdater(paths, supervisor)
    controller = DesktopController(
        window,
        paths,
        supervisor,
        updater,
        auto_start=not no_auto_start,
        desktop_smoke=desktop_smoke,
    )
    window.events.closing += controller.closing

    def terminate(_signum: int, _frame: object) -> None:
        controller.closing()
        try:
            window.destroy()
        except Exception:
            pass

    signal.signal(signal.SIGINT, terminate)
    signal.signal(signal.SIGTERM, terminate)
    storage = paths.root / "user" / "webview"
    storage.mkdir(parents=True, exist_ok=True)
    if desktop_smoke:

        def watchdog() -> None:
            time.sleep(150)
            if controller.smoke_success is None:
                controller.smoke_success = False
                try:
                    window.destroy()
                except Exception:
                    pass

        threading.Thread(
            target=watchdog, name="portable-comfy-smoke-watchdog", daemon=True
        ).start()
    webview.start(
        controller.launch,
        gui="qt",
        debug=False,
        private_mode=False,
        storage_path=str(storage),
        menu=_menu(controller),
    )
    supervisor.stop()
    return 0 if not desktop_smoke or controller.smoke_success is True else 2


def build_parser() -> argparse.ArgumentParser:
    def port_number(value: str) -> int:
        parsed = int(value)
        if not 1 <= parsed <= 65535:
            raise argparse.ArgumentTypeError("port must be between 1 and 65535")
        return parsed

    def positive_seconds(value: str) -> float:
        parsed = float(value)
        if parsed <= 0:
            raise argparse.ArgumentTypeError("timeout must be positive")
        return parsed

    parser = argparse.ArgumentParser(
        description="Portable ComfyUI Linux desktop launcher"
    )
    parser.add_argument(
        "--root", type=Path, help="portable root (normally discovered beside AppImage)"
    )
    parser.add_argument(
        "--port",
        type=port_number,
        help="fixed loopback port (default: choose a free port)",
    )
    parser.add_argument("--start-timeout", type=positive_seconds, default=120.0)
    parser.add_argument("--cpu", action="store_true", help="force CPU mode")
    parser.add_argument("--disable-custom-nodes", action="store_true")
    parser.add_argument("--no-auto-start", action="store_true")
    parser.add_argument(
        "--no-webview", action="store_true", help="run only the managed server"
    )
    parser.add_argument(
        "--smoke-test", action="store_true", help="start, health-check, and stop"
    )
    parser.add_argument(
        "--desktop-smoke-test",
        action="store_true",
        help="open Qt, load the healthy frontend, then close automatically",
    )
    parser.add_argument(
        "--self-test", action="store_true", help="validate layout without a runtime"
    )
    parser.add_argument(
        "--version", action="version", version=f"Portable Comfy {__version__}"
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.no_auto_start and (args.smoke_test or args.desktop_smoke_test):
        parser.error("smoke tests require automatic server startup")
    if args.smoke_test and args.desktop_smoke_test:
        parser.error("choose either --smoke-test or --desktop-smoke-test")
    if args.desktop_smoke_test and args.no_webview:
        parser.error("--desktop-smoke-test cannot be combined with --no-webview")
    try:
        paths = PortablePaths.discover(args.root)
        if args.self_test:
            paths.create_layout()
            configure_logging(paths)
            print(json.dumps(self_test(paths), indent=2))
            return 0
        # Lock before any layout repair, runtime rewrite, or update recovery.
        # Creating the lock parent is itself idempotent and race-safe.
        paths.state.mkdir(parents=True, exist_ok=True)
        lock = InstanceLock(paths.state / "launcher.lock")
        lock.acquire()
        try:
            paths.create_layout()
            configure_logging(paths)
            CoreUpdater.recover_interrupted_update(paths)
            paths.validate_runtime()
            repaired = paths.repair_runtime_metadata()
            if repaired:
                LOGGER.info("repaired %d portable Python metadata files", repaired)
            supervisor = ServerSupervisor(
                paths,
                port=args.port,
                start_timeout=args.start_timeout,
                cpu=args.cpu,
                disable_custom_nodes=args.disable_custom_nodes,
            )
            atexit.register(supervisor.stop)
            try:
                if args.no_webview or args.smoke_test:
                    return _run_headless(
                        supervisor,
                        smoke_test=args.smoke_test,
                        no_auto_start=args.no_auto_start,
                    )
                return _run_desktop(
                    paths,
                    supervisor,
                    no_auto_start=args.no_auto_start,
                    desktop_smoke=args.desktop_smoke_test,
                )
            finally:
                supervisor.stop()
        finally:
            lock.release()
    except (
        LayoutError,
        AlreadyRunningError,
        ServerError,
        UpdateError,
        OSError,
    ) as error:
        LOGGER.error("%s", error)
        print(f"portable-comfy: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
