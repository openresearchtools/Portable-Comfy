from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from portable_comfy.app import (
    DESKTOP_SMOKE_ACK_ENV,
    DESKTOP_SMOKE_READY_ENV,
    DesktopController,
    _confirm_desktop_smoke_surface,
    _menu,
    main,
    self_test,
)
from portable_comfy.locking import AlreadyRunningError, InstanceLock
from portable_comfy.paths import PortablePaths
from portable_comfy.updater import EnvironmentUpdater


def test_self_test_needs_no_bundled_runtime(tmp_path: Path) -> None:
    result = self_test(PortablePaths(tmp_path / "empty portable"))
    assert result["ok"] is True
    assert result["workflow_link"] == "../../workflows"
    assert str(tmp_path / "empty portable") in str(result["database_url"])


def test_cli_self_test(tmp_path: Path, capsys: object) -> None:
    assert main(["--root", str(tmp_path / "CLI Root"), "--self-test"]) == 0
    # pytest's fixture is intentionally left loosely typed for Python 3.13.
    captured = capsys.readouterr()  # type: ignore[attr-defined]
    value = json.loads(captured.out)
    assert value["ok"] is True
    assert Path(value["root"]).name == "CLI Root"


class _Window:
    def __init__(self) -> None:
        self.urls: list[str] = []
        self.pages: list[str] = []
        self.confirmations: list[tuple[str, str]] = []

    def load_url(self, value: str) -> None:
        self.urls.append(value)

    def load_html(self, value: str) -> None:
        self.pages.append(value)

    def create_confirmation_dialog(self, title: str, message: str) -> bool:
        self.confirmations.append((title, message))
        return True


class _Supervisor:
    def __init__(self) -> None:
        self.running = False
        self.stops = 0

    @property
    def is_running(self) -> bool:
        return self.running

    @property
    def url(self) -> str:
        return "http://127.0.0.1:8188/"

    def start(self) -> str:
        self.running = True
        return self.url

    def stop(self, **_kwargs: float) -> None:
        self.running = False
        self.stops += 1

    def restart(self) -> str:
        self.stop()
        return self.start()


def test_native_menu_exposes_lifecycle_and_updater(tmp_path: Path) -> None:
    window = _Window()
    supervisor = _Supervisor()
    controller = DesktopController(
        window,
        PortablePaths(tmp_path),
        supervisor,  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        auto_start=False,
    )
    menus = _menu(controller)
    assert [menu.title for menu in menus] == [
        "Server",
        "View",
        "Core",
        "Help",
    ]
    assert [item.title for item in menus[0].items] == ["Start", "Stop", "Restart"]
    assert [item.title for item in menus[2].items] == ["Install complete bundle…"]
    controller._start()
    assert window.urls == [supervisor.url]
    controller._restart()
    assert window.urls[-1] == supervisor.url
    controller._stop()
    assert "Server stopped" in window.pages[-1]
    assert controller.closing() is True and supervisor.stops >= 3


def test_about_shows_installed_complete_core_identity(tmp_path: Path) -> None:
    paths = PortablePaths(tmp_path)
    paths.comfyui.mkdir(parents=True)
    identity = {
        "schema_version": 1,
        "app_id": "portable-comfy",
        "generation_id": "comfyui-v0.27.0-exact-generation",
        "core": {"version": "0.27.0", "tag": "v0.27.0", "commit": "core-sha"},
        "frontend": {"version": "1.45.20", "commit": "frontend-sha"},
        "runtime": {
            "python": "3.13.12",
            "torch": "2.12.0+cu130",
            "torchvision": "0.27.0+cu130",
            "torchaudio": "2.11.0+cu130",
            "cuda": "13.0",
        },
    }
    (paths.comfyui / "PORTABLE-COMFY-IDENTITY.json").write_text(
        json.dumps(identity), encoding="utf-8"
    )
    window = _Window()
    controller = DesktopController(
        window,
        paths,
        _Supervisor(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        auto_start=False,
    )

    controller.about()

    title, message = window.confirmations[-1]
    assert title == "About Portable Comfy"
    for expected in (
        "Installed generation: comfyui-v0.27.0-exact-generation",
        "Core: 0.27.0 (v0.27.0)",
        "Core commit: core-sha",
        "Frontend: 1.45.20",
        "Frontend commit: frontend-sha",
        "Python: 3.13.12",
        "Torch: 2.12.0+cu130",
        "CUDA: 13.0",
    ):
        assert expected in message


def test_about_gracefully_handles_missing_identity(tmp_path: Path) -> None:
    window = _Window()
    controller = DesktopController(
        window,
        PortablePaths(tmp_path),
        _Supervisor(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        auto_start=False,
    )

    controller.about()

    assert "Installed Core identity: unavailable" in window.confirmations[-1][1]


def test_main_locks_before_runtime_repair(
    portable_root: PortablePaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    observed = False
    resealed = False

    def repair(paths: PortablePaths) -> int:
        nonlocal observed
        competitor = InstanceLock(paths.state / "launcher.lock")
        with pytest.raises(AlreadyRunningError):
            competitor.acquire()
        observed = True
        return 1

    def reseal(_class: type[EnvironmentUpdater], paths: PortablePaths) -> None:
        nonlocal resealed
        competitor = InstanceLock(paths.state / "launcher.lock")
        with pytest.raises(AlreadyRunningError):
            competitor.acquire()
        resealed = True

    monkeypatch.setattr(PortablePaths, "repair_runtime_metadata", repair)
    monkeypatch.setattr(
        EnvironmentUpdater, "reseal_active_environment", classmethod(reseal)
    )
    monkeypatch.setattr("portable_comfy.app._run_headless", lambda *_args, **_kwargs: 0)
    assert (
        main(
            [
                "--root",
                str(portable_root.root),
                "--no-webview",
                "--no-auto-start",
            ]
        )
        == 0
    )
    assert observed and resealed


def test_smoke_rejects_disabled_autostart() -> None:
    with pytest.raises(SystemExit):
        main(["--smoke-test", "--no-auto-start"])


def test_desktop_smoke_waits_for_external_surface_confirmation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ready = tmp_path / "frontend.ready"
    acknowledgement = tmp_path / "pixels.valid"
    monkeypatch.setenv(DESKTOP_SMOKE_READY_ENV, str(ready))
    monkeypatch.setenv(DESKTOP_SMOKE_ACK_ENV, str(acknowledgement))

    def pixel_probe() -> None:
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if (
                ready.exists()
                and ready.read_text(encoding="utf-8") == "frontend-loaded\n"
            ):
                acknowledgement.touch()
                return
            time.sleep(0.01)
        raise AssertionError("desktop smoke ready marker was not written")

    worker = threading.Thread(target=pixel_probe)
    worker.start()
    assert _confirm_desktop_smoke_surface(threading.Event(), timeout=2)
    worker.join()
    assert not ready.exists()
    assert not acknowledgement.exists()


def test_desktop_smoke_cannot_pass_without_external_validator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(DESKTOP_SMOKE_READY_ENV, raising=False)
    monkeypatch.delenv(DESKTOP_SMOKE_ACK_ENV, raising=False)
    assert not _confirm_desktop_smoke_surface(threading.Event(), timeout=0.01)
