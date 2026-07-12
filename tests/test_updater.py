from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import tarfile
from pathlib import Path

import pytest

from portable_comfy.paths import PortablePaths
from portable_comfy.updater import (
    BundleValidationError,
    CoreUpdater,
    EnvironmentUpdater,
    TRANSACTION_MARKER,
    UpdateError,
)


RUNTIME = {
    "python": "3.14.1",
    "torch": "3.0.0+cu140",
    "torchvision": "0.30.0+cu140",
    "torchaudio": "3.0.0+cu140",
    "cuda": "14.0",
    "platform": "linux-x86_64",
    "requirements_lock_path": "ComfyUI/runtime/requirements.lock",
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


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def make_bundle(
    tmp_path: Path,
    *,
    tamper: bool = False,
    unsafe_link: str | None = None,
    runtime_overrides: dict[str, str] | None = None,
) -> Path:
    outer = tmp_path / "Portable-Comfy-environment-v9.9.9"
    core = outer / "ComfyUI"
    prefix = core / "runtime/python"
    (core / "frontend").mkdir(parents=True)
    (prefix / "bin").mkdir(parents=True)
    (core / "runtime").mkdir(exist_ok=True)
    (outer / "manifest").mkdir()
    (core / "main.py").write_text("# new core\n", encoding="utf-8")
    (core / "frontend/index.html").write_text("<title>new</title>\n", encoding="utf-8")
    (core / "new.txt").write_text("new payload\n", encoding="utf-8")
    python = prefix / "bin/python-portable"
    python.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    python.chmod(0o755)
    (prefix / "bin/python3").symlink_to("python-portable")
    lock = core / "runtime/requirements.lock"
    lock.write_text("torch==3.0.0+cu140\n", encoding="utf-8")
    if unsafe_link is not None:
        (core / "bad-link").symlink_to(unsafe_link)

    files: list[dict[str, object]] = []
    for path in sorted(core.rglob("*")):
        relative = path.relative_to(outer).as_posix()
        if path.is_symlink():
            files.append(
                {"path": relative, "type": "symlink", "target": os.readlink(path)}
            )
        elif path.is_file():
            files.append(
                {
                    "path": relative,
                    "type": "file",
                    "sha256": _sha256(path),
                    "size": path.stat().st_size,
                }
            )
    runtime = {
        **RUNTIME,
        "requirements_lock_sha256": _sha256(lock),
        **(runtime_overrides or {}),
    }
    manifest = {
        "schema_version": 2,
        "bundle_type": "environment",
        "app_id": "portable-comfy",
        "generation_id": "comfyui-v9.9.9-test-generation",
        "core": {"version": "9.9.9", "tag": "v9.9.9", "commit": "abc"},
        "frontend": {"version": "8.8.8", "commit": "def"},
        "runtime": runtime,
        "files": files,
    }
    (outer / "manifest/environment.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    (outer / "manifest/environment-checksums.sha256").write_text(
        "".join(
            f"{item['sha256']}  {item['path']}\n"
            for item in files
            if item["type"] == "file"
        ),
        encoding="utf-8",
    )
    if tamper:
        (core / "new.txt").write_text("tampered\n", encoding="utf-8")

    archive = tmp_path / "environment.tar.gz"
    with tarfile.open(archive, "w:gz") as output:
        output.add(outer, arcname=outer.name, recursive=True)
    return archive


def test_complete_environment_update_preserves_every_persistent_area(
    portable_root: PortablePaths, tmp_path: Path
) -> None:
    (portable_root.comfyui / "old.txt").write_text("old payload\n", encoding="utf-8")
    node = portable_root.root / "custom_nodes/my_node.py"
    node.write_text("persistent\n", encoding="utf-8")
    node_package = portable_root.custom_node_site_packages / "node_dep.py"
    node_package.write_text("persistent package\n", encoding="utf-8")
    model = portable_root.root / "models/model.bin"
    model.write_bytes(b"model")
    supervisor = FakeSupervisor(running=True)
    updater = EnvironmentUpdater(portable_root, supervisor, command_runner=completed)  # type: ignore[arg-type]

    result = updater.install_bundle(make_bundle(tmp_path))

    assert result.version == "9.9.9" and result.restarted
    assert result.generation_id == "comfyui-v9.9.9-test-generation"
    assert (portable_root.comfyui / "new.txt").read_text() == "new payload\n"
    assert portable_root.python_prefix.is_relative_to(portable_root.comfyui)
    assert (portable_root.python_prefix / "bin/python-portable").is_file()
    assert node.read_text() == "persistent\n"
    assert node_package.read_text() == "persistent package\n"
    assert model.read_bytes() == b"model"
    backups = list((portable_root.state / "rollback").glob("ComfyUI-*"))
    assert len(backups) == 1
    assert (backups[0] / "old.txt").read_text() == "old payload\n"
    assert (backups[0] / "runtime/python/bin/python-portable").is_file()
    assert supervisor.running and supervisor.starts == 1
    assert not (portable_root.state / TRANSACTION_MARKER).exists()
    installed_manifest = json.loads(
        portable_root.environment_manifest.read_text(encoding="utf-8")
    )
    installed_files = {
        item["path"]: item
        for item in installed_manifest["files"]
        if item["type"] == "file"
    }
    stamp_path = "ComfyUI/runtime/python/.portable-comfy-prefix"
    assert stamp_path in installed_files
    assert installed_files[stamp_path]["sha256"] == _sha256(
        portable_root.python_prefix / ".portable-comfy-prefix"
    )
    checksum_entries = {
        line.split("  ", 1)[1]: line.split("  ", 1)[0]
        for line in portable_root.environment_checksums.read_text(
            encoding="utf-8"
        ).splitlines()
    }
    assert checksum_entries == {
        path: item["sha256"] for path, item in installed_files.items()
    }


def test_candidate_preflight_uses_candidate_python_without_node_runtime(
    portable_root: PortablePaths, tmp_path: Path
) -> None:
    calls: list[tuple[list[str], dict[str, str]]] = []
    scratch_ready: list[bool] = []

    def capture(
        command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        environment = kwargs["env"]  # type: ignore[assignment]
        calls.append((command, environment))  # type: ignore[arg-type]
        if "--quick-test-for-ci" in command:
            scratch_ready.append(
                all(
                    Path(command[command.index(option) + 1]).is_dir()
                    for option in (
                        "--base-directory",
                        "--user-directory",
                        "--temp-directory",
                    )
                )
                and Path(environment["XDG_CACHE_HOME"]).parent.is_dir()
            )
        return subprocess.CompletedProcess(command, 0, "", "")

    updater = EnvironmentUpdater(
        portable_root,
        FakeSupervisor(running=False),
        command_runner=capture,  # type: ignore[arg-type]
    )
    updater.install_bundle(make_bundle(tmp_path))
    assert len(calls) == 3
    for command, environment in calls:
        assert "environment-stage-" in command[0]
        assert command[0].endswith("ComfyUI/runtime/python/bin/python-portable")
        assert "PIP_TARGET" not in environment
        assert "PYTHONPATH" not in environment
        assert environment["PYTHONDONTWRITEBYTECODE"] == "1"
    assert scratch_ready == [True]


def test_environment_with_new_python_torch_cuda_is_accepted(
    portable_root: PortablePaths, tmp_path: Path
) -> None:
    supervisor = FakeSupervisor(running=False)
    result = CoreUpdater(  # Compatibility alias now has environment semantics.
        portable_root,
        supervisor,
        command_runner=completed,  # type: ignore[arg-type]
    ).install_bundle(make_bundle(tmp_path))
    assert result.generation_id.endswith("test-generation")
    assert supervisor.starts == 1 and not supervisor.running


def test_runtime_abi_change_with_node_packages_requires_explicit_rebuild(
    portable_root: PortablePaths, tmp_path: Path
) -> None:
    (portable_root.custom_node_site_packages / "native_extension.so").write_bytes(
        b"elf"
    )
    supervisor = FakeSupervisor()
    updater = EnvironmentUpdater(portable_root, supervisor, command_runner=completed)  # type: ignore[arg-type]
    with pytest.raises(UpdateError, match="custom-node extension ABI"):
        updater.install_bundle(
            make_bundle(tmp_path, runtime_overrides={"python": "3.15.0"})
        )
    assert supervisor.stops == 0
    assert (portable_root.custom_node_site_packages / "native_extension.so").exists()


def test_checksum_tampering_is_rejected_before_stop(
    portable_root: PortablePaths, tmp_path: Path
) -> None:
    supervisor = FakeSupervisor()
    updater = EnvironmentUpdater(portable_root, supervisor, command_runner=completed)  # type: ignore[arg-type]
    with pytest.raises(BundleValidationError, match="checksum"):
        updater.install_bundle(make_bundle(tmp_path, tamper=True))
    assert supervisor.stops == 0


def test_manifested_dangling_symlink_is_rejected(
    portable_root: PortablePaths, tmp_path: Path
) -> None:
    supervisor = FakeSupervisor()
    updater = EnvironmentUpdater(
        portable_root,
        supervisor,
        command_runner=completed,  # type: ignore[arg-type]
    )
    with pytest.raises(BundleValidationError, match="dangling"):
        updater.install_bundle(make_bundle(tmp_path, unsafe_link="missing-target"))
    assert supervisor.stops == 0


@pytest.mark.parametrize("kind", ["traversal", "absolute-symlink", "hardlink"])
def test_unsafe_archive_members_are_rejected(
    portable_root: PortablePaths, tmp_path: Path, kind: str
) -> None:
    archive = tmp_path / f"{kind}.tar.gz"
    outer = "Portable-Comfy-environment-v1"
    with tarfile.open(archive, "w:gz") as output:
        if kind == "traversal":
            item = tarfile.TarInfo(f"{outer}/../escape")
            data = b"bad"
            item.size = len(data)
            output.addfile(item, io.BytesIO(data))
        elif kind == "absolute-symlink":
            item = tarfile.TarInfo(f"{outer}/ComfyUI/link")
            item.type = tarfile.SYMTYPE
            item.linkname = "/etc/passwd"
            output.addfile(item)
        else:
            item = tarfile.TarInfo(f"{outer}/ComfyUI/link")
            item.type = tarfile.LNKTYPE
            item.linkname = f"{outer}/ComfyUI/main.py"
            output.addfile(item)
    updater = EnvironmentUpdater(
        portable_root, FakeSupervisor(), command_runner=completed
    )  # type: ignore[arg-type]
    with pytest.raises(BundleValidationError):
        updater.install_bundle(archive)
    assert not (tmp_path / "escape").exists()


def test_failed_candidate_health_rolls_back_whole_environment(
    portable_root: PortablePaths, tmp_path: Path
) -> None:
    (portable_root.comfyui / "old.txt").write_text("still old\n", encoding="utf-8")
    old_python = portable_root.python_prefix / "bin/python-portable"
    old_python_bytes = old_python.read_bytes()
    supervisor = FakeSupervisor(running=True, fail_starts=1)
    updater = EnvironmentUpdater(portable_root, supervisor, command_runner=completed)  # type: ignore[arg-type]
    with pytest.raises(UpdateError, match="rolled back"):
        updater.install_bundle(make_bundle(tmp_path))
    assert (portable_root.comfyui / "old.txt").read_text() == "still old\n"
    assert old_python.read_bytes() == old_python_bytes
    assert not (portable_root.comfyui / "new.txt").exists()
    assert supervisor.running and supervisor.starts == 2
    assert not (portable_root.state / TRANSACTION_MARKER).exists()


def test_startup_recovers_power_loss_during_generation_swap(
    portable_root: PortablePaths,
) -> None:
    paths = portable_root
    (paths.comfyui / "candidate.txt").write_text("candidate\n", encoding="utf-8")
    backup = paths.state / "rollback/ComfyUI-interrupted"
    backup.parent.mkdir(parents=True, exist_ok=True)
    backup.mkdir()
    (backup / "frontend").mkdir()
    (backup / "runtime/python/bin").mkdir(parents=True)
    (backup / "main.py").write_text("# restored core\n", encoding="utf-8")
    (backup / "frontend/index.html").write_text("restored\n", encoding="utf-8")
    (backup / "runtime/python/bin/python-portable").write_text("old python\n")
    (backup / "old.txt").write_text("old survives\n", encoding="utf-8")
    transaction = paths.state / "transactions/interrupted"
    transaction.mkdir(parents=True)
    old_manifest = b'{"old": true}\n'
    old_checksums = b"old checksums\n"
    (transaction / "environment.json").write_bytes(old_manifest)
    (transaction / "environment-checksums.sha256").write_bytes(old_checksums)
    paths.environment_manifest.write_text('{"candidate": true}\n', encoding="utf-8")
    paths.environment_checksums.write_text("candidate checksums\n", encoding="utf-8")
    journal = {
        "schema_version": 2,
        "transaction_id": "interrupted",
        "phase": "candidate_active",
        "backup": backup.relative_to(paths.root).as_posix(),
        "transaction": transaction.relative_to(paths.root).as_posix(),
        "had_environment_manifest": True,
        "had_environment_checksums": True,
    }
    (paths.state / TRANSACTION_MARKER).write_text(json.dumps(journal), encoding="utf-8")

    assert EnvironmentUpdater.recover_interrupted_update(paths) is True
    assert (paths.comfyui / "old.txt").read_text() == "old survives\n"
    assert (paths.python_prefix / "bin/python-portable").read_text() == "old python\n"
    assert not (paths.comfyui / "candidate.txt").exists()
    assert paths.environment_manifest.read_bytes() == old_manifest
    assert paths.environment_checksums.read_bytes() == old_checksums
    assert not (paths.state / TRANSACTION_MARKER).exists()


def test_startup_recovery_rejects_journal_path_escape(
    portable_root: PortablePaths,
) -> None:
    marker = portable_root.state / TRANSACTION_MARKER
    marker.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "backup": "../../outside",
                "transaction": "state/transactions/test",
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(UpdateError, match="unsafe backup"):
        EnvironmentUpdater.recover_interrupted_update(portable_root)
