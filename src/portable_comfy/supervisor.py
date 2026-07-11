"""Own and health-check the ComfyUI server process group."""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import IO, Callable

from portable_comfy.paths import PortablePaths


class ServerError(RuntimeError):
    pass


class ServerStartError(ServerError):
    pass


@dataclass(frozen=True, slots=True)
class ServerStatus:
    state: str
    pid: int | None
    port: int | None
    url: str | None
    returncode: int | None = None
    detail: str | None = None


def choose_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as stream:
        stream.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        stream.bind(("127.0.0.1", 0))
        return int(stream.getsockname()[1])


def _http_ready(port: int, timeout: float = 2.0) -> bool:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for route in ("/system_stats", "/"):
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}{route}",
            headers={"Connection": "close", "User-Agent": "Portable-Comfy/0.1"},
        )
        try:
            with opener.open(request, timeout=timeout) as response:
                if response.status != 200:
                    return False
                response.read(1)
        except (OSError, urllib.error.URLError, TimeoutError):
            return False
    return True


class ServerSupervisor:
    """Lifecycle manager that never adopts or kills an unrelated process."""

    def __init__(
        self,
        paths: PortablePaths,
        *,
        port: int | None = None,
        start_timeout: float = 120.0,
        cpu: bool = False,
        disable_custom_nodes: bool = False,
        health_probe: Callable[[int, float], bool] = _http_ready,
    ) -> None:
        self.paths = paths
        self.port = port
        self.start_timeout = start_timeout
        self.cpu = cpu
        self.disable_custom_nodes = disable_custom_nodes
        self._health_probe = health_probe
        self._process: subprocess.Popen[bytes] | None = None
        self._log_stream: IO[bytes] | None = None
        self._state = "stopped"
        self._detail: str | None = None
        self._last_returncode: int | None = None
        self._lock = threading.RLock()

    @property
    def url(self) -> str | None:
        return None if self.port is None else f"http://127.0.0.1:{self.port}/"

    @property
    def is_running(self) -> bool:
        with self._lock:
            self._refresh_locked()
            return self._process is not None and self._process.poll() is None

    def status(self) -> ServerStatus:
        with self._lock:
            self._refresh_locked()
            return ServerStatus(
                state=self._state,
                pid=None if self._process is None else self._process.pid,
                port=self.port,
                url=self.url,
                returncode=self._last_returncode,
                detail=self._detail,
            )

    def start(self) -> str:
        self.paths.validate_runtime()
        self.paths.create_layout()
        with self._lock:
            self._refresh_locked()
            if self._process is not None:
                existing_port = self.port
            else:
                if self.port is None:
                    self.port = choose_loopback_port()
                existing_port = self.port
                self._rotate_logs()
                log_path = self.paths.logs / "comfyui.log"
                self._log_stream = log_path.open("ab", buffering=0)
                command = self.paths.comfy_command(
                    self.port,
                    cpu=self.cpu,
                    disable_custom_nodes=self.disable_custom_nodes,
                )
                try:
                    self._process = subprocess.Popen(
                        command,
                        cwd=self.paths.comfyui,
                        env=self.paths.server_environment(),
                        stdin=subprocess.DEVNULL,
                        stdout=self._log_stream,
                        stderr=subprocess.STDOUT,
                        start_new_session=True,
                        close_fds=True,
                    )
                except Exception:
                    self._close_log_locked()
                    self._process = None
                    self._state = "failed"
                    raise
                self._state = "starting"
                self._detail = None
                self._last_returncode = None
                self._write_state_locked(command)
        assert existing_port is not None
        deadline = time.monotonic() + self.start_timeout
        while time.monotonic() < deadline:
            with self._lock:
                process = self._process
                if process is None:
                    raise ServerStartError("ComfyUI startup was cancelled")
                returncode = process.poll()
            if returncode is not None:
                detail = self.tail_log()
                with self._lock:
                    self._last_returncode = returncode
                    self._state = "failed"
                    self._detail = f"server exited with status {returncode}"
                    self._finalize_process_locked()
                raise ServerStartError(
                    f"ComfyUI exited during startup with status {returncode}.\n{detail}"
                )
            if self._health_probe(
                existing_port, min(2.0, max(0.2, deadline - time.monotonic()))
            ):
                with self._lock:
                    if self._process is process and process.poll() is None:
                        self._state = "running"
                        self._detail = None
                        assert self.url is not None
                        return self.url
            time.sleep(0.25)
        detail = self.tail_log()
        self.stop(interrupt_timeout=2.0, terminate_timeout=2.0)
        with self._lock:
            self._state = "failed"
            self._detail = f"health check timed out after {self.start_timeout:g}s"
        raise ServerStartError(
            f"ComfyUI did not become healthy within {self.start_timeout:g} seconds.\n{detail}"
        )

    def stop(
        self,
        *,
        interrupt_timeout: float = 15.0,
        terminate_timeout: float = 5.0,
        kill_timeout: float = 3.0,
    ) -> None:
        with self._lock:
            self._refresh_locked()
            process = self._process
            if process is None:
                self._state = "stopped"
                self._remove_state_file()
                return
            self._state = "stopping"
        self._signal_group(process, signal.SIGINT)
        if not self._wait(process, interrupt_timeout):
            self._signal_group(process, signal.SIGTERM)
            if not self._wait(process, terminate_timeout):
                self._signal_group(process, signal.SIGKILL)
                self._wait(process, kill_timeout)
        with self._lock:
            if self._process is process:
                self._last_returncode = process.poll()
                self._finalize_process_locked()
            self._state = "stopped"
            self._detail = None

    def restart(self) -> str:
        self.stop()
        return self.start()

    def wait(self, poll_interval: float = 0.5) -> int:
        while True:
            with self._lock:
                self._refresh_locked()
                if self._process is None:
                    return self._last_returncode or 0
            time.sleep(poll_interval)

    def tail_log(self, lines: int = 60) -> str:
        path = self.paths.logs / "comfyui.log"
        if not path.is_file():
            return "(no server log was written)"
        try:
            with path.open("rb") as stream:
                stream.seek(0, os.SEEK_END)
                size = stream.tell()
                stream.seek(max(0, size - 128 * 1024))
                text = stream.read().decode("utf-8", errors="replace")
            return "\n".join(text.splitlines()[-lines:])
        except OSError as error:
            return f"(could not read server log: {error})"

    def _refresh_locked(self) -> None:
        if self._process is None:
            return
        returncode = self._process.poll()
        if returncode is None:
            return
        self._last_returncode = returncode
        if self._state not in {"stopping", "stopped"}:
            self._state = "failed"
            self._detail = f"server exited with status {returncode}"
        self._finalize_process_locked()

    def _finalize_process_locked(self) -> None:
        self._process = None
        self._close_log_locked()
        self._remove_state_file()

    def _close_log_locked(self) -> None:
        if self._log_stream is not None:
            self._log_stream.close()
            self._log_stream = None

    @staticmethod
    def _signal_group(process: subprocess.Popen[bytes], sig: signal.Signals) -> None:
        if process.poll() is not None:
            return
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            return

    @staticmethod
    def _wait(process: subprocess.Popen[bytes], timeout: float) -> bool:
        try:
            process.wait(timeout=max(0.0, timeout))
            return True
        except subprocess.TimeoutExpired:
            return False

    def _write_state_locked(self, command: list[str]) -> None:
        assert self._process is not None and self.port is not None
        self.paths.state.mkdir(parents=True, exist_ok=True)
        state_path = self.paths.state / "server.json"
        temporary = state_path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(
                {
                    "pid": self._process.pid,
                    "process_group": self._process.pid,
                    "port": self.port,
                    "started_at": datetime.now(timezone.utc).isoformat(),
                    "command": command,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        temporary.replace(state_path)

    def _remove_state_file(self) -> None:
        try:
            (self.paths.state / "server.json").unlink()
        except FileNotFoundError:
            pass

    def _rotate_logs(self, max_bytes: int = 5 * 1024 * 1024, backups: int = 3) -> None:
        self.paths.logs.mkdir(parents=True, exist_ok=True)
        current = self.paths.logs / "comfyui.log"
        if not current.exists() or current.stat().st_size < max_bytes:
            return
        oldest = current.with_name(f"{current.name}.{backups}")
        try:
            oldest.unlink()
        except FileNotFoundError:
            pass
        for index in range(backups - 1, 0, -1):
            source = current.with_name(f"{current.name}.{index}")
            if source.exists():
                source.replace(current.with_name(f"{current.name}.{index + 1}"))
        current.replace(current.with_name(f"{current.name}.1"))
