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
from portable_comfy.updater import EnvironmentUpdater, UpdateError


LOGGER = logging.getLogger("portable_comfy")
DESKTOP_SMOKE_READY_ENV = "PORTABLE_COMFY_DESKTOP_SMOKE_READY"
DESKTOP_SMOKE_ACK_ENV = "PORTABLE_COMFY_DESKTOP_SMOKE_ACK"

QT_DARK_STYLESHEET = """
QMenuBar {
    background-color: #191c24;
    color: #eaf0ff;
    border-bottom: 1px solid #303746;
}
QMenuBar::item {
    background-color: transparent;
    color: #eaf0ff;
    padding: 6px 10px;
}
QMenuBar::item:selected {
    background-color: #303746;
    color: #ffffff;
}
QMenuBar::item:pressed {
    background-color: #3a465a;
    color: #ffffff;
}
QMenu {
    background-color: #191c24;
    color: #eaf0ff;
    border: 1px solid #303746;
    padding: 4px;
}
QMenu::item {
    background-color: transparent;
    color: #eaf0ff;
    padding: 6px 24px 6px 10px;
}
QMenu::item:selected {
    background-color: #31505e;
    color: #ffffff;
}
QMenu::item:disabled {
    color: #747e92;
}
QMenu::separator {
    height: 1px;
    background-color: #303746;
    margin: 4px 8px;
}
QToolTip {
    background-color: #252a35;
    color: #eaf0ff;
    border: 1px solid #465065;
}
"""


def _apply_qt_dark_theme() -> Any:
    """Create Qt's application object and give all native chrome a dark palette."""

    from qtpy.QtCore import Qt
    from qtpy.QtGui import QColor, QPalette
    from qtpy.QtWidgets import QApplication

    # The application is intentionally created before pywebview initializes its
    # backend so its menus inherit our palette. Qt WebEngine requires this
    # attribute when its widgets are imported after QApplication is constructed.
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
    application = QApplication.instance() or QApplication(sys.argv)
    application.setStyle("Fusion")

    palette = QPalette()
    role = QPalette.ColorRole
    colors = {
        role.Window: "#111319",
        role.WindowText: "#eaf0ff",
        role.Base: "#111319",
        role.AlternateBase: "#191c24",
        role.ToolTipBase: "#252a35",
        role.ToolTipText: "#eaf0ff",
        role.Text: "#eaf0ff",
        role.Button: "#191c24",
        role.ButtonText: "#eaf0ff",
        role.BrightText: "#ffffff",
        role.Link: "#69e0ff",
        role.Highlight: "#31505e",
        role.HighlightedText: "#ffffff",
        role.PlaceholderText: "#8f9bb2",
    }
    for color_role, color in colors.items():
        palette.setColor(color_role, QColor(color))

    disabled = QPalette.ColorGroup.Disabled
    for color_role in (role.WindowText, role.Text, role.ButtonText):
        palette.setColor(disabled, color_role, QColor("#747e92"))

    application.setPalette(palette)
    application.setStyleSheet(QT_DARK_STYLESHEET)
    return application


def _confirm_desktop_smoke_surface(
    closing: threading.Event, *, timeout: float = 30.0
) -> bool:
    """Wait for external mapped-window and rendered-surface smoke validation."""

    ready_value = os.environ.get(DESKTOP_SMOKE_READY_ENV)
    ack_value = os.environ.get(DESKTOP_SMOKE_ACK_ENV)
    if not ready_value or not ack_value:
        LOGGER.error("desktop smoke requires both ready and acknowledgement paths")
        return False

    ready = Path(ready_value)
    acknowledgement = Path(ack_value)
    try:
        ready.parent.mkdir(parents=True, exist_ok=True)
        acknowledgement.unlink(missing_ok=True)
        ready.write_text("frontend-loaded\n", encoding="utf-8")
        deadline = time.monotonic() + timeout
        while not closing.is_set() and time.monotonic() < deadline:
            if acknowledgement.is_file():
                return True
            time.sleep(0.1)
        LOGGER.error("desktop smoke surface was not externally validated")
        return False
    except OSError:
        LOGGER.exception("desktop smoke surface handshake failed")
        return False
    finally:
        for marker in (ready, acknowledgement):
            try:
                marker.unlink(missing_ok=True)
            except OSError:
                LOGGER.exception("could not remove desktop smoke marker %s", marker)


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
        updater: EnvironmentUpdater,
        *,
        auto_start: bool,
        desktop_smoke: bool = False,
        bootstrap_smoke: bool = False,
    ) -> None:
        self.window = window
        self.paths = paths
        self.supervisor = supervisor
        self.updater = updater
        self.auto_start = auto_start
        self.desktop_smoke = desktop_smoke
        self.bootstrap_smoke = bootstrap_smoke
        self.smoke_success: bool | None = None
        self._operation_lock = threading.Lock()
        self._closing = threading.Event()

    def launch(self) -> None:
        problem = self._environment_problem()
        if problem is not None:
            if self.desktop_smoke:
                self.window.events.loaded.clear()
            self._show_environment_required(problem)
            if self.desktop_smoke:
                self._schedule_smoke_close(
                    self.bootstrap_smoke and self._environment_is_absent()
                )
            return
        if self.bootstrap_smoke:
            self.window.load_html(
                _page(
                    "Bootstrap smoke test failed",
                    "A ComfyUI environment is already installed.",
                    error=True,
                )
            )
            self._schedule_smoke_close(False)
            return
        if self.auto_start:
            self.start_server()
        else:
            self.window.load_html(
                _page(
                    "Server stopped",
                    "Use Server → Start to launch the bundled ComfyUI environment.",
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
            problem = self._environment_problem()
            if problem is not None:
                self._show_environment_required(problem)
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
                "Portable Comfy environment (*.parts.json;*.part????;*.tar.gz;*.tgz)",
                "All files (*.*)",
            ),
        )
        if not selected:
            return
        archive = Path(selected[0])
        if not self.window.create_confirmation_dialog(
            "Install complete ComfyUI environment",
            f"Validate and install {archive.name}?\n\n"
            "For a multipart download, keep its descriptor and every numbered "
            "part together in this directory.\n\n"
            "This replaces the complete ComfyUI folder, including its Core, "
            "frontend, private Python, Torch/CUDA and locked dependencies.\n\n"
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
                        "ComfyUI environment installed",
                        f"ComfyUI {result.version} is installed. Use Server → Start when ready.",
                    )
                )

        self._background("Installing ComfyUI environment", operation)

    def about(self) -> None:
        identity = self._installed_identity_summary()
        self.window.create_confirmation_dialog(
            "About Portable Comfy",
            f"Portable Comfy {__version__}\n\n"
            f"{identity}\n\n"
            "The complete ComfyUI/Python/CUDA environment is replaceable. Custom "
            "nodes, node packages, models, workflows and user data stay persistent.",
        )

    def _installed_identity_summary(self) -> str:
        path = self.paths.comfyui / "PORTABLE-COMFY-IDENTITY.json"
        try:
            if path.is_symlink() or not path.is_file() or path.stat().st_size > 131072:
                raise ValueError("identity file is missing, linked, or too large")
            identity = json.loads(path.read_text(encoding="utf-8"))
            if (
                not isinstance(identity, dict)
                or identity.get("schema_version") != 1
                or identity.get("app_id") != "portable-comfy"
            ):
                raise ValueError("identity schema is invalid")

            core = identity["core"]
            frontend = identity["frontend"]
            runtime = identity["runtime"]
            if not all(isinstance(group, dict) for group in (core, frontend, runtime)):
                raise ValueError("identity groups are invalid")

            def value(group: dict[str, object], key: str) -> str:
                result = group.get(key)
                if (
                    not isinstance(result, str)
                    or not result
                    or len(result) > 200
                    or any(character in result for character in "\r\n\x00")
                ):
                    raise ValueError(f"invalid identity value: {key}")
                return result

            generation = identity.get("generation_id")
            if (
                not isinstance(generation, str)
                or not generation
                or len(generation) > 200
                or any(character in generation for character in "\r\n\x00")
            ):
                raise ValueError("invalid generation ID")
            return "\n".join(
                (
                    f"Installed generation: {generation}",
                    f"Core: {value(core, 'version')} ({value(core, 'tag')})",
                    f"Core commit: {value(core, 'commit')}",
                    f"Frontend: {value(frontend, 'version')}",
                    f"Frontend commit: {value(frontend, 'commit')}",
                    f"Python: {value(runtime, 'python')}",
                    f"Torch: {value(runtime, 'torch')}",
                    f"torchvision: {value(runtime, 'torchvision')}",
                    f"torchaudio: {value(runtime, 'torchaudio')}",
                    f"CUDA: {value(runtime, 'cuda')}",
                )
            )
        except (
            KeyError,
            OSError,
            UnicodeError,
            ValueError,
            json.JSONDecodeError,
        ) as error:
            LOGGER.warning("installed Core identity is unavailable: %s", error)
            return "Installed ComfyUI environment: unavailable"

    def _environment_problem(self) -> str | None:
        try:
            self.paths.validate_runtime()
        except (LayoutError, OSError) as error:
            return str(error)
        return None

    def _environment_is_absent(self) -> bool:
        return not self.paths.comfyui.exists() and not self.paths.comfyui.is_symlink()

    def _show_environment_required(self, problem: str) -> None:
        missing = self._environment_is_absent()
        title = (
            "ComfyUI environment not installed"
            if missing
            else "ComfyUI environment unavailable"
        )
        instruction = (
            "Download the environment descriptor and every numbered part into "
            "one directory, then choose Environment → Install local environment… "
            "and select either the .parts.json descriptor or any .partNNNN file."
        )
        detail = instruction if missing else f"{problem}\n\n{instruction}"
        self.window.load_html(_page(title, detail, error=not missing))

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
            surface_confirmed = (
                _confirm_desktop_smoke_surface(self._closing)
                if success and rendered
                else False
            )
            self.smoke_success = bool(success and rendered and surface_confirmed)
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
        Menu(
            "Environment",
            [MenuAction("Install local environment…", controller.install_bundle)],
        ),
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
    bootstrap_smoke: bool = False,
) -> int:
    import webview

    # Construct the application before pywebview creates its native menu bar.
    # Host desktop palettes are not reliable inside an AppImage and can otherwise
    # produce black menu text over the launcher's dark window chrome.
    qt_application = _apply_qt_dark_theme()
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
    updater = EnvironmentUpdater(paths, supervisor)
    controller = DesktopController(
        window,
        paths,
        supervisor,
        updater,
        auto_start=not no_auto_start,
        desktop_smoke=desktop_smoke,
        bootstrap_smoke=bootstrap_smoke,
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
    # Keep the Python wrapper alive for the entire Qt event loop.
    del qt_application
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
        "--install-environment",
        type=Path,
        metavar="DESCRIPTOR_OR_PART",
        help=(
            "validate and install a local complete environment descriptor, "
            "numbered part, or legacy tar.gz, then exit"
        ),
    )
    parser.add_argument(
        "--no-webview", action="store_true", help="run only the managed server"
    )
    parser.add_argument(
        "--smoke-test", action="store_true", help="start, health-check, and stop"
    )
    parser.add_argument(
        "--desktop-smoke-test",
        action="store_true",
        help="run the externally validated Qt desktop smoke protocol",
    )
    parser.add_argument(
        "--desktop-bootstrap-smoke-test",
        action="store_true",
        help="externally validate the launcher-only setup screen and exit",
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
    if args.no_auto_start and (
        args.smoke_test or args.desktop_smoke_test or args.desktop_bootstrap_smoke_test
    ):
        parser.error("smoke tests require automatic server startup")
    if (
        sum(
            bool(value)
            for value in (
                args.smoke_test,
                args.desktop_smoke_test,
                args.desktop_bootstrap_smoke_test,
            )
        )
        > 1
    ):
        parser.error("choose only one smoke-test mode")
    if (
        args.desktop_smoke_test or args.desktop_bootstrap_smoke_test
    ) and args.no_webview:
        parser.error("desktop smoke tests cannot be combined with --no-webview")
    if args.install_environment and (
        args.smoke_test or args.desktop_smoke_test or args.desktop_bootstrap_smoke_test
    ):
        parser.error("--install-environment cannot be combined with a smoke test")
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
            EnvironmentUpdater.recover_interrupted_update(paths)
            supervisor = ServerSupervisor(
                paths,
                port=args.port,
                start_timeout=args.start_timeout,
                cpu=args.cpu,
                disable_custom_nodes=args.disable_custom_nodes,
            )
            atexit.register(supervisor.stop)
            try:
                if args.install_environment:
                    result = EnvironmentUpdater(paths, supervisor).install_bundle(
                        args.install_environment
                    )
                    print(
                        json.dumps(
                            {
                                "ok": True,
                                "version": result.version,
                                "commit": result.commit,
                                "generation_id": result.generation_id,
                            },
                            indent=2,
                        )
                    )
                    return 0

                runtime_problem: LayoutError | OSError | None = None
                try:
                    paths.validate_runtime()
                except (LayoutError, OSError) as error:
                    runtime_problem = error
                if runtime_problem is None:
                    repaired = paths.repair_runtime_metadata()
                    if repaired:
                        LOGGER.info(
                            "repaired %d portable Python metadata files", repaired
                        )
                        EnvironmentUpdater.reseal_active_environment(paths)
                    node_runtime_changes = paths.ensure_node_runtime()
                    if node_runtime_changes:
                        LOGGER.info(
                            "initialized or rebound persistent custom-node venv "
                            "(%d changes)",
                            node_runtime_changes,
                        )
                elif args.no_webview and not args.no_auto_start:
                    raise runtime_problem
                else:
                    LOGGER.info(
                        "starting launcher without an active ComfyUI environment: %s",
                        runtime_problem,
                    )

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
                    desktop_smoke=(
                        args.desktop_smoke_test or args.desktop_bootstrap_smoke_test
                    ),
                    bootstrap_smoke=args.desktop_bootstrap_smoke_test,
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
