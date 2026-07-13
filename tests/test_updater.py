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
    identity_mismatch: bool = False,
    core_overrides: dict[str, str] | None = None,
    frontend_overrides: dict[str, str] | None = None,
    frontend_inventory_overrides: dict[str, object] | None = None,
    frontend_package_overrides: dict[str, object] | None = None,
    outer_version: str = "9.9.9",
    omitted_notice: str | None = None,
    unlicensed_package: str | None = None,
    frozen_requirements: str | None = None,
    frozen_version_overrides: dict[str, str] | None = None,
    omit_frozen_requirements: bool = False,
) -> Path:
    outer = tmp_path / f"Portable-Comfy-core-v{outer_version}"
    core = outer / "ComfyUI"
    prefix = core / "runtime/python"
    (core / "frontend").mkdir(parents=True)
    (prefix / "bin").mkdir(parents=True)
    (core / "runtime").mkdir(exist_ok=True)
    (outer / "manifest").mkdir()
    (core / "main.py").write_text("# new core\n", encoding="utf-8")
    (core / "LICENSE").write_text("Core license\n", encoding="utf-8")
    (core / "frontend/index.html").write_text("<title>new</title>\n", encoding="utf-8")
    (core / "frontend/LICENSE").write_text("Frontend license\n", encoding="utf-8")
    (core / "frontend/THIRD_PARTY_NOTICES.md").write_text(
        "Frontend notices\n", encoding="utf-8"
    )
    (core / "new.txt").write_text("new payload\n", encoding="utf-8")
    python = prefix / "bin/python-portable"
    python.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    python.chmod(0o755)
    (prefix / "LICENSE.txt").write_text("CPython license\n", encoding="utf-8")
    (prefix / "bin/python3").symlink_to("python-portable")
    lock = core / "runtime/requirements.lock"
    lock.write_text("torch==3.0.0+cu140\n", encoding="utf-8")
    if unsafe_link is not None:
        (core / "bad-link").symlink_to(unsafe_link)

    runtime = {
        **RUNTIME,
        "requirements_lock_sha256": _sha256(lock),
        **(runtime_overrides or {}),
    }
    core_identity = {
        "version": "9.9.9",
        "tag": "v9.9.9",
        "commit": "a" * 40,
        **(core_overrides or {}),
    }
    frontend_identity = {
        "version": "8.8.8",
        "commit": "b" * 40,
        **(frontend_overrides or {}),
    }
    frontend_notice_root = core / "frontend/LICENSES/npm/example-1.0.0"
    frontend_notice_root.mkdir(parents=True)
    (frontend_notice_root / "LICENSE").write_text(
        "Example frontend dependency license\n", encoding="utf-8"
    )
    frontend_metadata = {
        "name": "example-frontend-dependency",
        "version": "1.0.0",
        "license": "MIT",
        "notice_files": ["example-1.0.0/LICENSE"],
    }
    (frontend_notice_root / "PACKAGE-METADATA.json").write_text(
        json.dumps(frontend_metadata), encoding="utf-8"
    )
    frontend_package = {
        **frontend_metadata,
        "metadata_file": "example-1.0.0/PACKAGE-METADATA.json",
        **(frontend_package_overrides or {}),
    }
    frontend_inventory = {
        "schema_version": 1,
        "frontend": frontend_identity,
        "packages": [frontend_package],
        "summary": {
            "distributions": 1,
            "with_notice_files": 1,
            "metadata_only": 0,
        },
        **(frontend_inventory_overrides or {}),
    }
    (core / "frontend/LICENSES/npm/packages.json").write_text(
        json.dumps(frontend_inventory),
        encoding="utf-8",
    )
    (
        core / f"frontend/SOURCE-ComfyUI-frontend-{frontend_identity['version']}.tar.gz"
    ).write_bytes(b"pinned frontend source\n")
    license_root = core / "runtime/LICENSES/python-packages"
    packages = []
    for package_name in (
        "comfyui-frontend-package",
        "torch",
        "torchvision",
        "torchaudio",
        "nvidia-cublas",
        "nvidia-cuda-runtime",
        "nvidia-cudnn-cu13",
    ):
        relative = f"{package_name}/LICENSE"
        notice = license_root / relative
        notice.parent.mkdir(parents=True, exist_ok=True)
        notice.write_text(f"{package_name} license\n", encoding="utf-8")
        packages.append(
            {
                "name": package_name,
                "version": "1.0",
                "license_files": (
                    [] if package_name == unlicensed_package else [relative]
                ),
            }
        )
    (license_root / "packages.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "packages": packages,
                "summary": {
                    "distributions": len(packages),
                    "with_license_files": len(packages)
                    - int(unlicensed_package is not None),
                    "metadata_only": int(unlicensed_package is not None),
                    "unidentified": 0,
                },
            }
        ),
        encoding="utf-8",
    )
    native_license_root = core / "runtime/LICENSES/python-native"
    native_license_root.mkdir(parents=True)
    (native_license_root / "packages.json").write_text(
        json.dumps({"schema_version": 1, "test_fixture": True}), encoding="utf-8"
    )
    exclusions_root = core / "runtime/LICENSES/runtime-exclusions"
    exclusions_root.mkdir(parents=True)
    (exclusions_root / "README.md").write_text(
        "Fixture runtime exclusions.\n", encoding="utf-8"
    )
    (exclusions_root / "nvshmem-plugin-exclusions.json").write_text(
        json.dumps({"schema_version": 1, "test_fixture": True}), encoding="utf-8"
    )
    (exclusions_root / "cufile-plugin-exclusions.json").write_text(
        json.dumps({"schema_version": 1, "test_fixture": True}), encoding="utf-8"
    )
    if not omit_frozen_requirements:
        default_freeze = "".join(
            f"{package['name'].replace('-', '_')}=="
            f"{(frozen_version_overrides or {}).get(str(package['name']), str(package['version']))}\n"
            for package in packages
        )
        (core / "runtime/installed-requirements.txt").write_text(
            default_freeze if frozen_requirements is None else frozen_requirements,
            encoding="utf-8",
        )
    if omitted_notice is not None:
        (core / omitted_notice).unlink()
    identity = {
        "schema_version": 1,
        "app_id": "portable-comfy",
        "generation_id": "comfyui-v9.9.9-test-generation",
        "core": core_identity,
        "frontend": frontend_identity,
        "runtime": runtime,
    }
    if identity_mismatch:
        identity["core"] = {**core_identity, "version": "different"}
    (core / "PORTABLE-COMFY-IDENTITY.json").write_text(
        json.dumps(identity, sort_keys=True), encoding="utf-8"
    )

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
    manifest = {
        "schema_version": 2,
        "bundle_type": "environment",
        "app_id": "portable-comfy",
        "generation_id": "comfyui-v9.9.9-test-generation",
        "core": core_identity,
        "frontend": frontend_identity,
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


def split_bundle(tmp_path: Path, *, keep_archive: bool = False) -> Path:
    archive = make_bundle(tmp_path)
    canonical = tmp_path / "Portable-Comfy-core-v9.9.9.tar.gz"
    archive.replace(canonical)
    part_size = max(1, canonical.stat().st_size // 3)
    command = [
        "python3",
        str(Path(__file__).resolve().parents[1] / "scripts/split_archive.py"),
        str(canonical),
        "--part-size",
        str(part_size),
    ]
    if keep_archive:
        command.append("--keep-archive")
    result = subprocess.run(command, check=True, text=True, capture_output=True)
    descriptor = Path(result.stdout.strip())
    assert descriptor.is_file()
    return descriptor


@pytest.mark.parametrize("selection", ["descriptor", "first-part", "last-part"])
def test_multipart_environment_update_accepts_descriptor_or_any_part(
    portable_root: PortablePaths, tmp_path: Path, selection: str
) -> None:
    descriptor = split_bundle(tmp_path)
    metadata = json.loads(descriptor.read_text(encoding="utf-8"))
    assert metadata["format"] == "portable-comfy-core-multipart"
    assert metadata["archive"]["filename"] == ("Portable-Comfy-core-v9.9.9.tar.gz")
    assert len(metadata["parts"]) >= 3
    assert not (tmp_path / metadata["archive"]["filename"]).exists()
    reconstructed = b""
    for expected_number, part in enumerate(metadata["parts"], start=1):
        part_path = tmp_path / part["filename"]
        payload = part_path.read_bytes()
        assert part["number"] == expected_number
        assert part["filename"].endswith(f".part{expected_number:04d}")
        assert len(payload) == part["size"] <= metadata["part_size"]
        assert hashlib.sha256(payload).hexdigest() == part["sha256"]
        reconstructed += payload
    assert len(reconstructed) == metadata["archive"]["size"]
    assert hashlib.sha256(reconstructed).hexdigest() == metadata["archive"]["sha256"]
    selected = descriptor
    if selection == "first-part":
        selected = tmp_path / metadata["parts"][0]["filename"]
    elif selection == "last-part":
        selected = tmp_path / metadata["parts"][-1]["filename"]

    result = EnvironmentUpdater(
        portable_root,
        FakeSupervisor(running=False),
        command_runner=completed,  # type: ignore[arg-type]
    ).install_bundle(selected)

    assert result.version == "9.9.9"
    assert (portable_root.comfyui / "new.txt").read_text() == "new payload\n"


def test_multipart_rejects_missing_extra_and_corrupt_parts_before_stop(
    portable_root: PortablePaths, tmp_path: Path
) -> None:
    descriptor = split_bundle(tmp_path)
    metadata = json.loads(descriptor.read_text(encoding="utf-8"))
    first = tmp_path / metadata["parts"][0]["filename"]
    last = tmp_path / metadata["parts"][-1]["filename"]
    supervisor = FakeSupervisor()
    updater = EnvironmentUpdater(
        portable_root,
        supervisor,
        command_runner=completed,  # type: ignore[arg-type]
    )

    saved = last.read_bytes()
    last.unlink()
    with pytest.raises(BundleValidationError, match="missing"):
        updater.install_bundle(descriptor)
    last.write_bytes(saved)

    extra = tmp_path / f"{metadata['archive']['filename']}.part9999"
    extra.write_bytes(b"unexpected")
    with pytest.raises(BundleValidationError, match="unexpected"):
        updater.install_bundle(first)
    extra.unlink()

    first.write_bytes(first.read_bytes()[:-1] + b"X")
    with pytest.raises(BundleValidationError, match="checksum"):
        updater.install_bundle(descriptor)
    assert supervisor.stops == 0


def test_multipart_rejects_wrong_reconstructed_archive_digest(
    portable_root: PortablePaths, tmp_path: Path
) -> None:
    descriptor = split_bundle(tmp_path)
    metadata = json.loads(descriptor.read_text(encoding="utf-8"))
    first = tmp_path / metadata["parts"][0]["filename"]
    changed = first.read_bytes()[:-1] + b"X"
    first.write_bytes(changed)
    metadata["parts"][0]["sha256"] = hashlib.sha256(changed).hexdigest()
    descriptor.write_text(json.dumps(metadata), encoding="utf-8")

    with pytest.raises(BundleValidationError, match="reconstructed Core archive"):
        EnvironmentUpdater(
            portable_root,
            FakeSupervisor(),
            command_runner=completed,  # type: ignore[arg-type]
        ).install_bundle(descriptor)


def test_multipart_rejects_descriptor_tampering_before_writing_environment(
    portable_root: PortablePaths, tmp_path: Path
) -> None:
    descriptor = split_bundle(tmp_path)
    metadata = json.loads(descriptor.read_text(encoding="utf-8"))
    metadata["parts"][0]["filename"] = "../escape"
    descriptor.write_text(json.dumps(metadata), encoding="utf-8")

    with pytest.raises(BundleValidationError, match="invalid order or metadata"):
        EnvironmentUpdater(
            portable_root,
            FakeSupervisor(),
            command_runner=completed,  # type: ignore[arg-type]
        ).install_bundle(descriptor)
    assert not (tmp_path.parent / "escape").exists()


def test_first_environment_install_starts_from_standalone_bootstrap(
    tmp_path: Path,
) -> None:
    paths = PortablePaths(tmp_path / "standalone")
    paths.create_layout()
    persistent = paths.models_cache / "bootstrap-sentinel"
    persistent.parent.mkdir(parents=True, exist_ok=True)
    persistent.write_text("persistent\n", encoding="utf-8")
    supervisor = FakeSupervisor(running=False)

    result = EnvironmentUpdater(
        paths,
        supervisor,
        command_runner=completed,  # type: ignore[arg-type]
    ).install_bundle(make_bundle(tmp_path))

    assert result.version == "9.9.9" and not result.restarted
    assert (paths.comfyui / "new.txt").read_text() == "new payload\n"
    assert paths.environment_manifest.is_file()
    assert paths.environment_checksums.is_file()
    assert persistent.read_text() == "persistent\n"
    assert not list((paths.state / "rollback").glob("ComfyUI-*"))
    assert supervisor.starts == 1 and not supervisor.running
    assert not (paths.state / TRANSACTION_MARKER).exists()


def test_failed_first_environment_install_rolls_back_to_bootstrap(
    tmp_path: Path,
) -> None:
    paths = PortablePaths(tmp_path / "standalone")
    paths.create_layout()
    supervisor = FakeSupervisor(running=False, fail_starts=1)

    with pytest.raises(UpdateError, match="rolled back"):
        EnvironmentUpdater(
            paths,
            supervisor,
            command_runner=completed,  # type: ignore[arg-type]
        ).install_bundle(make_bundle(tmp_path))

    assert not paths.comfyui.exists()
    assert not paths.environment_manifest.exists()
    assert not paths.environment_checksums.exists()
    assert not list((paths.state / "rollback").glob("ComfyUI-*"))
    assert not (paths.state / TRANSACTION_MARKER).exists()


@pytest.mark.parametrize("candidate_active", [False, True])
def test_startup_recovers_interrupted_first_install_to_bootstrap(
    tmp_path: Path, candidate_active: bool
) -> None:
    paths = PortablePaths(tmp_path / "standalone")
    paths.create_layout()
    if candidate_active:
        paths.comfyui.mkdir()
        (paths.comfyui / "uncommitted.txt").write_text("candidate\n", encoding="utf-8")
        paths.environment_manifest.write_text("candidate\n", encoding="utf-8")
        paths.environment_checksums.write_text("candidate\n", encoding="utf-8")
    transaction = paths.state / "transactions/first-install"
    transaction.mkdir(parents=True)
    backup = paths.state / "rollback/ComfyUI-first-install"
    journal = {
        "schema_version": 2,
        "transaction_id": "first-install",
        "phase": "candidate_active" if candidate_active else "old_absent",
        "backup": backup.relative_to(paths.root).as_posix(),
        "transaction": transaction.relative_to(paths.root).as_posix(),
        "had_active_environment": False,
        "had_environment_manifest": False,
        "had_environment_checksums": False,
    }
    (paths.state / TRANSACTION_MARKER).write_text(json.dumps(journal), encoding="utf-8")

    assert EnvironmentUpdater.recover_interrupted_update(paths) is True
    assert not paths.comfyui.exists()
    assert not paths.environment_manifest.exists()
    assert not paths.environment_checksums.exists()
    assert not (paths.state / TRANSACTION_MARKER).exists()
    recovered = list((paths.state / "recovered").glob("uncommitted-ComfyUI-*"))
    assert bool(recovered) is candidate_active


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
                and (
                    Path(command[command.index("--base-directory") + 1])
                    / "custom_nodes"
                ).is_dir()
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


def test_visible_identity_must_match_top_manifest(
    portable_root: PortablePaths, tmp_path: Path
) -> None:
    supervisor = FakeSupervisor()
    updater = EnvironmentUpdater(
        portable_root,
        supervisor,
        command_runner=completed,  # type: ignore[arg-type]
    )
    with pytest.raises(
        BundleValidationError, match="PORTABLE-COMFY-IDENTITY.json disagrees"
    ):
        updater.install_bundle(make_bundle(tmp_path, identity_mismatch=True))
    assert supervisor.stops == 0


@pytest.mark.parametrize(
    ("core_overrides", "frontend_overrides"),
    [
        ({"version": "9"}, {}),
        ({"tag": "release-9.9.9"}, {}),
        ({"commit": "abc"}, {}),
        ({}, {"version": "8"}),
        ({}, {"commit": "def"}),
    ],
)
def test_exact_core_and_frontend_identity_is_required(
    portable_root: PortablePaths,
    tmp_path: Path,
    core_overrides: dict[str, str],
    frontend_overrides: dict[str, str],
) -> None:
    updater = EnvironmentUpdater(
        portable_root,
        FakeSupervisor(),
        command_runner=completed,  # type: ignore[arg-type]
    )
    with pytest.raises(BundleValidationError, match="version|tag|commit"):
        updater.install_bundle(
            make_bundle(
                tmp_path,
                core_overrides=core_overrides,
                frontend_overrides=frontend_overrides,
            )
        )


def test_archive_root_version_must_match_manifest(
    portable_root: PortablePaths, tmp_path: Path
) -> None:
    updater = EnvironmentUpdater(
        portable_root,
        FakeSupervisor(),
        command_runner=completed,  # type: ignore[arg-type]
    )
    with pytest.raises(BundleValidationError, match="root does not match"):
        updater.install_bundle(make_bundle(tmp_path, outer_version="9.9.8"))


@pytest.mark.parametrize(
    "notice",
    [
        "LICENSE",
        "frontend/LICENSE",
        "frontend/THIRD_PARTY_NOTICES.md",
        "runtime/python/LICENSE.txt",
        "runtime/LICENSES/python-packages/packages.json",
        "runtime/LICENSES/python-native/packages.json",
        "runtime/LICENSES/runtime-exclusions/nvshmem-plugin-exclusions.json",
        "runtime/LICENSES/runtime-exclusions/cufile-plugin-exclusions.json",
        "runtime/LICENSES/runtime-exclusions/README.md",
    ],
)
def test_complete_core_bundle_requires_redistribution_notices(
    portable_root: PortablePaths, tmp_path: Path, notice: str
) -> None:
    updater = EnvironmentUpdater(
        portable_root,
        FakeSupervisor(),
        command_runner=completed,  # type: ignore[arg-type]
    )
    with pytest.raises(BundleValidationError, match="redistribution notice"):
        updater.install_bundle(make_bundle(tmp_path, omitted_notice=notice))


@pytest.mark.parametrize(
    ("omitted_path", "message"),
    [
        (
            "frontend/LICENSES/npm/packages.json",
            "frontend dependency license inventory is missing or empty",
        ),
        (
            "frontend/SOURCE-ComfyUI-frontend-8.8.8.tar.gz",
            "pinned frontend source snapshot is missing or empty",
        ),
    ],
)
def test_complete_core_bundle_requires_frontend_inventory_and_source(
    portable_root: PortablePaths,
    tmp_path: Path,
    omitted_path: str,
    message: str,
) -> None:
    updater = EnvironmentUpdater(
        portable_root,
        FakeSupervisor(),
        command_runner=completed,  # type: ignore[arg-type]
    )
    with pytest.raises(BundleValidationError, match=message):
        updater.install_bundle(make_bundle(tmp_path, omitted_notice=omitted_path))


@pytest.mark.parametrize(
    ("inventory_overrides", "package_overrides", "message"),
    [
        (
            {"frontend": {"version": "8.8.7", "commit": "b" * 40}},
            {},
            "unsupported schema",
        ),
        (
            {
                "summary": {
                    "distributions": 1,
                    "with_notice_files": 0,
                    "metadata_only": 1,
                }
            },
            {},
            "not comprehensive",
        ),
        ({}, {"metadata_file": "../PACKAGE-METADATA.json"}, "unsafe path"),
        (
            {},
            {"notice_files": ["example-1.0.0/MISSING-LICENSE"]},
            "notice is missing or empty",
        ),
    ],
)
def test_frontend_dependency_inventory_is_deeply_validated(
    portable_root: PortablePaths,
    tmp_path: Path,
    inventory_overrides: dict[str, object],
    package_overrides: dict[str, object],
    message: str,
) -> None:
    updater = EnvironmentUpdater(
        portable_root,
        FakeSupervisor(),
        command_runner=completed,  # type: ignore[arg-type]
    )
    with pytest.raises(BundleValidationError, match=message):
        updater.install_bundle(
            make_bundle(
                tmp_path,
                frontend_inventory_overrides=inventory_overrides,
                frontend_package_overrides=package_overrides,
            )
        )


def test_every_runtime_package_requires_a_bundled_license_file(
    portable_root: PortablePaths, tmp_path: Path
) -> None:
    updater = EnvironmentUpdater(
        portable_root,
        FakeSupervisor(),
        command_runner=completed,  # type: ignore[arg-type]
    )
    with pytest.raises(BundleValidationError, match="not comprehensive"):
        updater.install_bundle(make_bundle(tmp_path, unlicensed_package="torchvision"))


@pytest.mark.parametrize(
    ("frozen_requirements", "message"),
    [
        ("torch==1.0\n", "disagree"),
        ("torch==1.0\nTorch==1.0\n", "duplicate package"),
        ("torch>=1.0\n", "not exact NAME==VERSION"),
    ],
)
def test_installed_runtime_freeze_must_exactly_match_license_inventory(
    portable_root: PortablePaths,
    tmp_path: Path,
    frozen_requirements: str,
    message: str,
) -> None:
    updater = EnvironmentUpdater(
        portable_root,
        FakeSupervisor(),
        command_runner=completed,  # type: ignore[arg-type]
    )
    with pytest.raises(BundleValidationError, match=message):
        updater.install_bundle(
            make_bundle(tmp_path, frozen_requirements=frozen_requirements)
        )


def test_complete_core_bundle_requires_installed_runtime_freeze(
    portable_root: PortablePaths, tmp_path: Path
) -> None:
    updater = EnvironmentUpdater(
        portable_root,
        FakeSupervisor(),
        command_runner=completed,  # type: ignore[arg-type]
    )
    with pytest.raises(BundleValidationError, match="freeze is missing"):
        updater.install_bundle(make_bundle(tmp_path, omit_frozen_requirements=True))


def test_installed_runtime_versions_must_match_license_inventory(
    portable_root: PortablePaths, tmp_path: Path
) -> None:
    updater = EnvironmentUpdater(
        portable_root,
        FakeSupervisor(),
        command_runner=completed,  # type: ignore[arg-type]
    )
    with pytest.raises(BundleValidationError, match="disagree"):
        updater.install_bundle(
            make_bundle(tmp_path, frozen_version_overrides={"torch": "2.0"})
        )


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
    outer = "Portable-Comfy-core-v1"
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


def test_startup_removes_only_owned_stale_preflight_stages(
    portable_root: PortablePaths,
) -> None:
    updates = portable_root.state / "updates"
    stale = updates / f"environment-stage-{'a' * 32}"
    stale.mkdir(parents=True)
    (stale / "reconstructed-core.tar.gz").write_bytes(b"large partial payload")
    unrelated = updates / "environment-stage-user-backup"
    unrelated.mkdir()
    (unrelated / "keep.txt").write_text("keep\n", encoding="utf-8")

    assert EnvironmentUpdater.recover_interrupted_update(portable_root) is False
    assert not stale.exists()
    assert (unrelated / "keep.txt").read_text(encoding="utf-8") == "keep\n"


def test_startup_recovers_power_loss_after_old_environment_was_restored(
    portable_root: PortablePaths,
) -> None:
    paths = portable_root
    (paths.comfyui / "old.txt").write_text("restored old\n", encoding="utf-8")
    transaction = paths.state / "transactions/rollback-interrupted"
    transaction.mkdir(parents=True)
    old_manifest = b'{"restored": true}\n'
    old_checksums = b"restored checksums\n"
    (transaction / "environment.json").write_bytes(old_manifest)
    (transaction / "environment-checksums.sha256").write_bytes(old_checksums)
    paths.environment_manifest.write_text('{"candidate": true}\n', encoding="utf-8")
    paths.environment_checksums.write_text("candidate checksums\n", encoding="utf-8")
    backup = paths.state / "rollback/ComfyUI-rollback-interrupted"
    journal = {
        "schema_version": 2,
        "transaction_id": "rollback-interrupted",
        "phase": "rollback_started",
        "backup": backup.relative_to(paths.root).as_posix(),
        "transaction": transaction.relative_to(paths.root).as_posix(),
        "had_active_environment": True,
        "had_environment_manifest": True,
        "had_environment_checksums": True,
    }
    marker = paths.state / TRANSACTION_MARKER
    marker.write_text(json.dumps(journal), encoding="utf-8")

    assert EnvironmentUpdater.recover_interrupted_update(paths) is True
    assert (paths.comfyui / "old.txt").read_text() == "restored old\n"
    assert paths.environment_manifest.read_bytes() == old_manifest
    assert paths.environment_checksums.read_bytes() == old_checksums
    assert not marker.exists()
    assert not transaction.exists()


def test_startup_recovery_is_repeatable_after_consuming_backup(
    portable_root: PortablePaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = portable_root
    (paths.comfyui / "candidate.txt").write_text("candidate\n", encoding="utf-8")
    backup = paths.state / "rollback/ComfyUI-repeatable-recovery"
    backup.parent.mkdir(parents=True, exist_ok=True)
    backup.mkdir()
    (backup / "old.txt").write_text("trusted old\n", encoding="utf-8")
    transaction = paths.state / "transactions/repeatable-recovery"
    transaction.mkdir(parents=True)
    old_manifest = b'{"trusted": true}\n'
    old_checksums = b"trusted checksums\n"
    (transaction / "environment.json").write_bytes(old_manifest)
    (transaction / "environment-checksums.sha256").write_bytes(old_checksums)
    journal = {
        "schema_version": 2,
        "transaction_id": "repeatable-recovery",
        "phase": "candidate_active",
        "backup": backup.relative_to(paths.root).as_posix(),
        "transaction": transaction.relative_to(paths.root).as_posix(),
        "had_active_environment": True,
        "had_environment_manifest": True,
        "had_environment_checksums": True,
    }
    marker = paths.state / TRANSACTION_MARKER
    marker.write_text(json.dumps(journal), encoding="utf-8")

    def interrupted_metadata_restore(
        _cls: type[EnvironmentUpdater],
        _paths: PortablePaths,
        _transaction: Path,
        _journal: dict[str, object],
    ) -> None:
        raise UpdateError("simulated power loss after backup activation")

    with monkeypatch.context() as patcher:
        patcher.setattr(
            EnvironmentUpdater,
            "_restore_journal_metadata",
            classmethod(interrupted_metadata_restore),
        )
        with pytest.raises(UpdateError, match="simulated power loss"):
            EnvironmentUpdater.recover_interrupted_update(paths)

    persisted = json.loads(marker.read_text(encoding="utf-8"))
    assert persisted["phase"] == "rollback_started"
    assert not backup.exists()
    assert (paths.comfyui / "old.txt").read_text() == "trusted old\n"

    assert EnvironmentUpdater.recover_interrupted_update(paths) is True
    assert paths.environment_manifest.read_bytes() == old_manifest
    assert paths.environment_checksums.read_bytes() == old_checksums
    assert not marker.exists()


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
