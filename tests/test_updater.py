from __future__ import annotations

import hashlib
import io
import json
import subprocess
import tarfile
from pathlib import Path

import pytest

from portable_comfy.paths import PortablePaths
from portable_comfy.updater import (
    BundleValidationError,
    CompatibilityError,
    CoreUpdater,
    TRANSACTION_MARKER,
    UpdateError,
)


RUNTIME = {
    "python": "3.13.12",
    "torch": "2.12.0+cu130",
    "cuda": "13.0",
    "platform": "linux-x86_64",
    "requirements_lock_sha256": "a" * 64,
}


class FakeSupervisor:
    def __init__(self, *, running: bool = True, fail_starts: int = 0) -> None:
        self.running = running
        self.fail_starts = fail_starts
        self.starts = 0
        self.stops = 0

    @property
    def is_running(self) -> bool:
        return self.running

    def stop(self, **_kwargs: float) -> None:
        self.stops += 1
        self.running = False

    def start(self) -> str:
        self.starts += 1
        if self.fail_starts:
            self.fail_starts -= 1
            raise RuntimeError("candidate health failure")
        self.running = True
        return "http://127.0.0.1:8188/"


def completed(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], 0, "preflight ok", "")


def make_bundle(
    tmp_path: Path,
    *,
    runtime: dict[str, str] | None = None,
    tamper: bool = False,
) -> Path:
    stage = tmp_path / "bundle-stage"
    core = stage / "ComfyUI"
    (core / "frontend").mkdir(parents=True)
    (core / "main.py").write_text("# new core\n", encoding="utf-8")
    (core / "frontend/index.html").write_text("<title>new</title>\n", encoding="utf-8")
    (core / "new.txt").write_text("new payload\n", encoding="utf-8")
    files = []
    for path in sorted(core.rglob("*")):
        if path.is_file():
            data = path.read_bytes()
            files.append(
                {
                    "path": path.relative_to(stage).as_posix(),
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "size": len(data),
                }
            )
    manifest = {
        "schema_version": 1,
        "bundle_type": "core",
        "app_id": "portable-comfy",
        "core": {"version": "9.9.9", "tag": "v9.9.9", "commit": "abc"},
        "frontend": {"version": "8.8.8", "commit": "def"},
        "runtime": RUNTIME if runtime is None else runtime,
        "files": files,
    }
    (stage / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (stage / "checksums.sha256").write_text(
        "".join(f"{item['sha256']}  {item['path']}\n" for item in files),
        encoding="utf-8",
    )
    if tamper:
        (core / "new.txt").write_text("tampered\n", encoding="utf-8")
    archive = tmp_path / "core.tar.gz"
    with tarfile.open(archive, "w:gz") as output:
        output.add(stage, arcname=".")
    return archive


def test_valid_update_preserves_data_and_keeps_rollback(
    portable_root: PortablePaths, tmp_path: Path
) -> None:
    (portable_root.comfyui / "old.txt").write_text("old payload\n", encoding="utf-8")
    node = portable_root.root / "custom_nodes/my_node.py"
    node.write_text("persistent\n", encoding="utf-8")
    supervisor = FakeSupervisor(running=True)
    updater = CoreUpdater(portable_root, supervisor, command_runner=completed)  # type: ignore[arg-type]
    result = updater.install_bundle(make_bundle(tmp_path))
    assert result.version == "9.9.9" and result.restarted
    assert (portable_root.comfyui / "new.txt").read_text() == "new payload\n"
    assert node.read_text() == "persistent\n"
    backups = list((portable_root.state / "rollback").glob("ComfyUI-*"))
    assert len(backups) == 1
    assert (backups[0] / "old.txt").read_text() == "old payload\n"
    assert supervisor.running and supervisor.starts == 1
    assert not (portable_root.state / TRANSACTION_MARKER).exists()
    assert not any((portable_root.state / "transactions").iterdir())


def test_update_health_checks_then_restores_stopped_state(
    portable_root: PortablePaths, tmp_path: Path
) -> None:
    supervisor = FakeSupervisor(running=False)
    updater = CoreUpdater(portable_root, supervisor, command_runner=completed)  # type: ignore[arg-type]
    result = updater.install_bundle(make_bundle(tmp_path))
    assert not result.restarted
    assert supervisor.starts == 1 and not supervisor.running


def test_incompatible_runtime_is_rejected_before_stop(
    portable_root: PortablePaths, tmp_path: Path
) -> None:
    required = {**RUNTIME, "python": "3.14.0"}
    supervisor = FakeSupervisor()
    updater = CoreUpdater(portable_root, supervisor, command_runner=completed)  # type: ignore[arg-type]
    with pytest.raises(CompatibilityError, match="python"):
        updater.install_bundle(make_bundle(tmp_path, runtime=required))
    assert supervisor.stops == 0


def test_checksum_tampering_is_rejected(
    portable_root: PortablePaths, tmp_path: Path
) -> None:
    updater = CoreUpdater(portable_root, FakeSupervisor(), command_runner=completed)  # type: ignore[arg-type]
    with pytest.raises(BundleValidationError, match="checksum"):
        updater.install_bundle(make_bundle(tmp_path, tamper=True))


@pytest.mark.parametrize("kind", ["traversal", "symlink"])
def test_unsafe_archive_members_are_rejected(
    portable_root: PortablePaths, tmp_path: Path, kind: str
) -> None:
    archive = tmp_path / f"{kind}.tar.gz"
    with tarfile.open(archive, "w:gz") as output:
        if kind == "traversal":
            item = tarfile.TarInfo("../escape")
            data = b"bad"
            item.size = len(data)
            output.addfile(item, io.BytesIO(data))
        else:
            item = tarfile.TarInfo("ComfyUI/link")
            item.type = tarfile.SYMTYPE
            item.linkname = "/etc/passwd"
            output.addfile(item)
    updater = CoreUpdater(portable_root, FakeSupervisor(), command_runner=completed)  # type: ignore[arg-type]
    with pytest.raises(BundleValidationError):
        updater.install_bundle(archive)
    assert not (tmp_path / "escape").exists()


def test_failed_candidate_start_rolls_back_active_core(
    portable_root: PortablePaths, tmp_path: Path
) -> None:
    (portable_root.comfyui / "old.txt").write_text("still old\n", encoding="utf-8")
    supervisor = FakeSupervisor(running=True, fail_starts=1)
    updater = CoreUpdater(portable_root, supervisor, command_runner=completed)  # type: ignore[arg-type]
    with pytest.raises(UpdateError, match="rolled back"):
        updater.install_bundle(make_bundle(tmp_path))
    assert (portable_root.comfyui / "old.txt").read_text() == "still old\n"
    assert not (portable_root.comfyui / "new.txt").exists()
    assert supervisor.running and supervisor.starts == 2
    assert not (portable_root.state / TRANSACTION_MARKER).exists()


def test_startup_recovers_power_loss_between_core_renames(
    portable_root: PortablePaths,
) -> None:
    paths = portable_root
    # The top-level Core is an uncommitted candidate; the old Core survived in
    # the journaled rollback location.
    (paths.comfyui / "candidate.txt").write_text("candidate\n", encoding="utf-8")
    backup = paths.state / "rollback/ComfyUI-interrupted"
    backup.parent.mkdir(parents=True, exist_ok=True)
    backup.mkdir()
    (backup / "frontend").mkdir()
    (backup / "main.py").write_text("# restored core\n", encoding="utf-8")
    (backup / "frontend/index.html").write_text("restored\n", encoding="utf-8")
    (backup / "old.txt").write_text("old survives\n", encoding="utf-8")
    transaction = paths.state / "transactions/interrupted"
    transaction.mkdir(parents=True)
    old_manifest = b'{"old": true}\n'
    old_checksums = b"old checksums\n"
    (transaction / "core.json").write_bytes(old_manifest)
    (transaction / "core-checksums.sha256").write_bytes(old_checksums)
    paths.core_manifest.write_text('{"candidate": true}\n', encoding="utf-8")
    (paths.manifest / "core-checksums.sha256").write_text(
        "candidate checksums\n", encoding="utf-8"
    )
    journal = {
        "schema_version": 1,
        "transaction_id": "interrupted",
        "phase": "candidate_active",
        "backup": backup.relative_to(paths.root).as_posix(),
        "transaction": transaction.relative_to(paths.root).as_posix(),
        "had_core_manifest": True,
        "had_core_checksums": True,
    }
    (paths.state / TRANSACTION_MARKER).write_text(json.dumps(journal), encoding="utf-8")

    assert CoreUpdater.recover_interrupted_update(paths) is True
    assert (paths.comfyui / "old.txt").read_text() == "old survives\n"
    assert not (paths.comfyui / "candidate.txt").exists()
    assert paths.core_manifest.read_bytes() == old_manifest
    assert (paths.manifest / "core-checksums.sha256").read_bytes() == old_checksums
    assert list((paths.state / "recovered").glob("uncommitted-ComfyUI-*"))
    assert not (paths.state / TRANSACTION_MARKER).exists()
    assert not transaction.exists()


def test_startup_recovery_rejects_journal_path_escape(
    portable_root: PortablePaths,
) -> None:
    marker = portable_root.state / TRANSACTION_MARKER
    marker.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "backup": "../../outside",
                "transaction": "state/transactions/test",
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(UpdateError, match="unsafe backup"):
        CoreUpdater.recover_interrupted_update(portable_root)
