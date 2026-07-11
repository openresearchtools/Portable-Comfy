from __future__ import annotations

from pathlib import Path

import pytest

from portable_comfy.locking import AlreadyRunningError, InstanceLock


def test_instance_lock_is_exclusive_and_releasable(tmp_path: Path) -> None:
    first = InstanceLock(tmp_path / "state/launcher.lock")
    second = InstanceLock(tmp_path / "state/launcher.lock")
    first.acquire()
    try:
        with pytest.raises(AlreadyRunningError):
            second.acquire()
    finally:
        first.release()
    second.acquire()
    second.release()
