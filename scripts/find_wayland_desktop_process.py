#!/usr/bin/env python3
"""Find the real native-Wayland desktop below an AppImage runtime process."""

from __future__ import annotations

import argparse
import os
import sys
from collections import defaultdict, deque
from pathlib import Path
from typing import Sequence


READY_ENV = b"PORTABLE_COMFY_DESKTOP_SMOKE_READY="
WAYLAND_ENV = b"QT_QPA_PLATFORM=wayland"
DISPLAY_ENV = b"DISPLAY="
DEAD_STATES = {"X", "Z"}


class ProcessInspectionError(RuntimeError):
    """The expected live native-Wayland process could not be verified."""


def _status(path: Path) -> tuple[int, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        name, separator, value = line.partition(":")
        if separator:
            values[name] = value.strip()
    try:
        parent_pid = int(values["PPid"].split()[0])
        state = values["State"].split()[0]
    except (KeyError, IndexError, ValueError) as error:
        raise OSError(f"malformed process status: {path}") from error
    return parent_pid, state


def _live(path: Path) -> bool:
    try:
        _parent_pid, state = _status(path / "status")
    except OSError:
        return False
    return state not in DEAD_STATES


def _descendants(root_pid: int, proc_root: Path) -> list[int]:
    children: dict[int, list[int]] = defaultdict(list)
    try:
        entries = list(proc_root.iterdir())
    except OSError as error:
        raise ProcessInspectionError(
            f"cannot inspect process tree at {proc_root}"
        ) from error

    for entry in entries:
        if not entry.name.isascii() or not entry.name.isdecimal():
            continue
        try:
            pid = int(entry.name)
            parent_pid, state = _status(entry / "status")
        except (OSError, ValueError):
            continue
        if state not in DEAD_STATES:
            children[parent_pid].append(pid)

    descendants: list[int] = []
    pending = deque([root_pid])
    visited = {root_pid}
    while pending:
        parent_pid = pending.popleft()
        for pid in sorted(children.get(parent_pid, ())):
            if pid in visited:
                continue
            visited.add(pid)
            descendants.append(pid)
            pending.append(pid)
    return descendants


def find_wayland_desktop_process(
    root_pid: int,
    ready_marker: str,
    *,
    proc_root: Path = Path("/proc"),
) -> int:
    """Return the live descendant proving the desktop uses native Wayland."""

    if root_pid <= 0:
        raise ProcessInspectionError("root PID must be positive")
    expected_marker = READY_ENV + os.fsencode(ready_marker)
    valid: list[int] = []
    missing_wayland: list[int] = []
    retained_display: list[int] = []

    for pid in _descendants(root_pid, proc_root):
        process_path = proc_root / str(pid)
        try:
            entries = set((process_path / "environ").read_bytes().split(b"\0"))
        except OSError:
            continue
        if expected_marker not in entries or not _live(process_path):
            continue
        if WAYLAND_ENV not in entries:
            missing_wayland.append(pid)
            continue
        if any(entry.startswith(DISPLAY_ENV) for entry in entries):
            retained_display.append(pid)
            continue
        valid.append(pid)

    if retained_display:
        values = ", ".join(str(pid) for pid in retained_display)
        raise ProcessInspectionError(
            f"native Wayland desktop process retained DISPLAY (PID {values})"
        )
    if valid:
        return valid[0]
    if missing_wayland:
        values = ", ".join(str(pid) for pid in missing_wayland)
        raise ProcessInspectionError(
            "desktop marker matched, but QT_QPA_PLATFORM=wayland was absent "
            f"(PID {values})"
        )
    raise ProcessInspectionError(
        "no live descendant had the exact desktop smoke readiness marker"
    )


def _arguments(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root_pid", type=int, help="AppImage runtime parent PID")
    parser.add_argument("ready_marker", help="exact readiness-marker environment value")
    parser.add_argument(
        "--proc-root",
        type=Path,
        default=Path("/proc"),
        help="process filesystem root (default: /proc)",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _arguments(argv)
    try:
        pid = find_wayland_desktop_process(
            arguments.root_pid,
            arguments.ready_marker,
            proc_root=arguments.proc_root,
        )
    except ProcessInspectionError as error:
        print(f"Wayland process validation failed: {error}", file=sys.stderr)
        return 1
    print(pid)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
