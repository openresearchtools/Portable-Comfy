from __future__ import annotations

from pathlib import Path

import pytest

from scripts.find_wayland_desktop_process import (
    ProcessInspectionError,
    find_wayland_desktop_process,
    main,
)


MARKER = "/tmp/Portable Comfy smoke/frontend-loaded"


def _process(
    proc_root: Path,
    pid: int,
    parent_pid: int,
    environment: list[str],
    *,
    state: str = "S",
) -> None:
    process = proc_root / str(pid)
    process.mkdir(parents=True)
    (process / "status").write_text(
        f"Name:\ttest-process\nState:\t{state} (test)\nPPid:\t{parent_pid}\n",
        encoding="utf-8",
    )
    (process / "environ").write_bytes(
        b"\0".join(value.encode() for value in environment) + b"\0"
    )


def test_reports_live_matching_wayland_descendant(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    proc_root = tmp_path / "proc"
    marker = f"PORTABLE_COMFY_DESKTOP_SMOKE_READY={MARKER}"
    _process(proc_root, 100, 1, [marker])
    _process(proc_root, 101, 100, [marker])
    _process(proc_root, 102, 101, [marker, "QT_QPA_PLATFORM=wayland"])
    _process(proc_root, 200, 1, [marker, "QT_QPA_PLATFORM=wayland"])

    assert main(["100", MARKER, "--proc-root", str(proc_root)]) == 0
    assert capsys.readouterr().out == "102\n"


def test_rejects_matching_descendant_without_wayland_qpa(tmp_path: Path) -> None:
    proc_root = tmp_path / "proc"
    marker = f"PORTABLE_COMFY_DESKTOP_SMOKE_READY={MARKER}"
    _process(proc_root, 100, 1, [])
    _process(proc_root, 101, 100, [marker, "QT_QPA_PLATFORM=xcb"])

    with pytest.raises(ProcessInspectionError, match="QT_QPA_PLATFORM=wayland"):
        find_wayland_desktop_process(100, MARKER, proc_root=proc_root)


def test_rejects_matching_wayland_descendant_with_display(tmp_path: Path) -> None:
    proc_root = tmp_path / "proc"
    marker = f"PORTABLE_COMFY_DESKTOP_SMOKE_READY={MARKER}"
    _process(proc_root, 100, 1, [])
    _process(
        proc_root,
        101,
        100,
        [marker, "QT_QPA_PLATFORM=wayland", "DISPLAY=:0"],
    )

    with pytest.raises(ProcessInspectionError, match="retained DISPLAY"):
        find_wayland_desktop_process(100, MARKER, proc_root=proc_root)
