"""Transactional installation of complete, self-contained ComfyUI environments."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tarfile
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from .locking import InstanceLock
from .paths import PortablePaths
from .supervisor import ServerSupervisor


SCHEMA_VERSION = 2
SHA256 = re.compile(r"^[0-9a-f]{64}$")
COMMIT = re.compile(r"^[0-9a-f]{40}$")
VERSION = re.compile(
    r"^[0-9]+(?:\.[0-9]+){2}(?:[-+._]?[0-9A-Za-z][0-9A-Za-z._+-]{0,63})?$"
)
FROZEN_REQUIREMENT = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^=\s]+)$")
DISTRIBUTION_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
GENERATION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,191}$")
UPDATE_STAGE_NAME = re.compile(r"^environment-stage-[0-9a-f]{32}$")
# "Core bundle" is the public artifact name. It always means the complete,
# self-contained ComfyUI/ generation: Core source, matching frontend, private
# Python, Torch/CUDA and every locked Core dependency.
ARCHIVE_ROOT = re.compile(r"^Portable-Comfy-core-[A-Za-z0-9._+-]+$")
CORE_ARCHIVE_NAME = re.compile(
    r"^Portable-Comfy-core-v[0-9]+(?:\.[0-9]+){2}"
    r"(?:[-+._]?[0-9A-Za-z][0-9A-Za-z._+-]{0,63})?\.tar\.gz$"
)
MULTIPART_PART_NAME = re.compile(
    r"^(Portable-Comfy-core-v[0-9]+(?:\.[0-9]+){2}"
    r"(?:[-+._]?[0-9A-Za-z][0-9A-Za-z._+-]{0,63})?\.tar\.gz)"
    r"\.part([0-9]{4})$"
)
MULTIPART_FORMAT = "portable-comfy-core-multipart"
MULTIPART_SCHEMA_VERSION = 1
MAX_MULTIPART_PART_BYTES = 1_900_000_000
MAX_MULTIPART_PARTS = 64
MAX_MULTIPART_DESCRIPTOR_BYTES = 1024 * 1024
MAX_ARCHIVE_BYTES = 10_000_000_000
MAX_MEMBERS = 500_000
MAX_UNPACKED_BYTES = 32 * 1024**3
TRANSACTION_MARKER = "environment-update-transaction.json"
IDENTITY_NAME = "PORTABLE-COMFY-IDENTITY.json"
RUNTIME_LICENSE_INVENTORY = "ComfyUI/runtime/LICENSES/python-packages/packages.json"
NATIVE_LICENSE_INVENTORY = "ComfyUI/runtime/LICENSES/python-native/packages.json"
RUNTIME_EXCLUSIONS_MANIFEST = (
    "ComfyUI/runtime/LICENSES/runtime-exclusions/nvshmem-plugin-exclusions.json"
)
RUNTIME_EXCLUSIONS_README = "ComfyUI/runtime/LICENSES/runtime-exclusions/README.md"
RUNTIME_INSTALLED_REQUIREMENTS = "ComfyUI/runtime/installed-requirements.txt"
FRONTEND_LICENSE_INVENTORY = "ComfyUI/frontend/LICENSES/npm/packages.json"
REQUIRED_LICENSE_FILES = (
    "ComfyUI/LICENSE",
    "ComfyUI/frontend/LICENSE",
    "ComfyUI/frontend/THIRD_PARTY_NOTICES.md",
    "ComfyUI/runtime/python/LICENSE.txt",
    RUNTIME_LICENSE_INVENTORY,
    NATIVE_LICENSE_INVENTORY,
    RUNTIME_EXCLUSIONS_MANIFEST,
    RUNTIME_EXCLUSIONS_README,
)
REQUIRED_RUNTIME_LICENSE_PACKAGES = frozenset(
    {
        "comfyui-frontend-package",
        "torch",
        "torchvision",
        "torchaudio",
        "nvidia-cublas",
        "nvidia-cuda-runtime",
        "nvidia-cudnn-cu13",
    }
)
RUNTIME_FIELDS = (
    "python",
    "torch",
    "torchvision",
    "torchaudio",
    "cuda",
    "platform",
    "requirements_lock_path",
    "requirements_lock_sha256",
)
NODE_EXTENSION_ABI_FIELDS = (
    "python",
    "torch",
    "torchvision",
    "torchaudio",
    "cuda",
    "platform",
)


class UpdateError(RuntimeError):
    """A complete Core/environment update could not be completed safely."""


class BundleValidationError(UpdateError):
    pass


class CompatibilityError(UpdateError):
    """Retained for API compatibility with the former source-only updater."""


@dataclass(frozen=True, slots=True)
class ValidatedBundle:
    root: Path
    manifest: dict[str, Any]
    checksums: str

    @property
    def environment(self) -> Path:
        return self.root / "ComfyUI"

    @property
    def core(self) -> Path:
        """Compatibility name for callers written for the source-only bundle."""

        return self.environment


@dataclass(frozen=True, slots=True)
class UpdateResult:
    version: str
    commit: str
    generation_id: str
    restarted: bool


def _digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def _strict_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON field: {key}")
        result[key] = value
    return result


def _archive_path(name: str) -> PurePosixPath:
    while name.startswith("./"):
        name = name[2:]
    if name in {"", "."}:
        return PurePosixPath()
    if "\\" in name or "\x00" in name:
        raise BundleValidationError(f"unsafe archive path: {name!r}")
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts:
        raise BundleValidationError(f"unsafe archive path: {name!r}")
    return path


def _payload_path(path_text: str) -> PurePosixPath:
    path = _archive_path(path_text)
    if not path.parts or path.parts[0] != "ComfyUI" or path.as_posix() != path_text:
        raise BundleValidationError(f"invalid environment payload path: {path_text}")
    return path


def _safe_link_target(link_path: PurePosixPath, target_text: str) -> None:
    if not target_text or "\\" in target_text or "\x00" in target_text:
        raise BundleValidationError(
            f"unsafe symlink target for {link_path}: {target_text!r}"
        )
    target = PurePosixPath(target_text)
    if target.is_absolute():
        raise BundleValidationError(f"absolute symlink target for {link_path}")
    resolved: list[str] = list(link_path.parent.parts)
    for part in target.parts:
        if part in {"", "."}:
            continue
        if part == "..":
            if not resolved:
                raise BundleValidationError(f"escaping symlink target for {link_path}")
            resolved.pop()
        else:
            resolved.append(part)
    if not resolved or resolved[0] != "ComfyUI":
        raise BundleValidationError(f"symlink escapes the environment: {link_path}")


class EnvironmentUpdater:
    """Replace one complete ComfyUI generation and nothing persistent."""

    def __init__(
        self,
        paths: PortablePaths,
        supervisor: ServerSupervisor,
        *,
        command_runner: Callable[
            ..., subprocess.CompletedProcess[str]
        ] = subprocess.run,
    ) -> None:
        self.paths = paths
        self.supervisor = supervisor
        self._run = command_runner
        self._thread_lock = threading.Lock()

    @classmethod
    def recover_interrupted_update(cls, paths: PortablePaths) -> bool:
        """Restore the previous complete generation after an interrupted swap."""

        cls._cleanup_stale_update_stages(paths)
        marker = paths.state / TRANSACTION_MARKER
        if not marker.exists():
            return False
        try:
            journal = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise UpdateError(
                f"cannot read interrupted-update journal: {error}"
            ) from error
        if not isinstance(journal, dict) or journal.get("schema_version") != 2:
            raise UpdateError("interrupted-update journal has an unsupported schema")

        backup = cls._journal_path(paths, journal, "backup", paths.state / "rollback")
        transaction = cls._journal_path(
            paths, journal, "transaction", paths.state / "transactions"
        )
        phase = journal.get("phase")
        if phase not in {
            "prepared",
            "old_absent",
            "old_moved",
            "candidate_active",
            "rollback_started",
        }:
            raise UpdateError("interrupted-update journal has an invalid phase")
        had_active_environment = journal.get("had_active_environment")
        if had_active_environment is None:
            # Journals written before standalone bootstrap support always had an
            # active environment and can be identified by their saved manifest.
            had_active_environment = journal.get("had_environment_manifest")
        if not isinstance(had_active_environment, bool):
            raise UpdateError(
                "interrupted-update journal is missing active-environment state"
            )
        restored = False
        if backup.is_dir():
            if not had_active_environment:
                raise UpdateError(
                    "first-install journal unexpectedly contains an environment backup"
                )
            # Recovery is itself a transaction. Persist rollback intent before
            # consuming the only backup so a second startup can trust the old
            # generation after backup -> ComfyUI even if metadata restoration
            # or journal cleanup is interrupted.
            journal["phase"] = "rollback_started"
            cls._atomic_bytes(
                marker,
                (json.dumps(journal, indent=2, sort_keys=True) + "\n").encode("utf-8"),
            )
            recovered_root = paths.state / "recovered"
            recovered_root.mkdir(parents=True, exist_ok=True)
            if paths.comfyui.exists():
                failed = recovered_root / f"uncommitted-ComfyUI-{uuid.uuid4().hex[:12]}"
                paths.comfyui.replace(failed)
            backup.replace(paths.comfyui)
            cls._fsync_directory(paths.root)
            cls._fsync_directory(backup.parent)
            cls._restore_journal_metadata(paths, transaction, journal)
            restored = True
        elif not had_active_environment:
            # A failed or interrupted first installation rolls back to the valid
            # bootstrap state: no active ComfyUI generation. Preserve any
            # uncommitted candidate for diagnosis instead of silently trusting it.
            if paths.comfyui.exists():
                recovered_root = paths.state / "recovered"
                recovered_root.mkdir(parents=True, exist_ok=True)
                failed = recovered_root / f"uncommitted-ComfyUI-{uuid.uuid4().hex[:12]}"
                paths.comfyui.replace(failed)
                cls._fsync_directory(paths.root)
            cls._restore_journal_metadata(paths, transaction, journal)
            restored = True
        elif phase in {"prepared", "rollback_started"} and paths.comfyui.is_dir():
            # Either the old environment had not moved, or rollback had already
            # moved it back before power was lost. In both cases the active tree
            # is the trusted old generation described by transaction metadata.
            cls._restore_journal_metadata(paths, transaction, journal)
            restored = phase == "rollback_started"
        else:
            raise UpdateError(
                "an interrupted full-Core update left neither an active generation "
                f"nor its rollback; inspect {transaction}"
            )
        marker.unlink(missing_ok=True)
        cls._fsync_directory(marker.parent)
        shutil.rmtree(transaction, ignore_errors=True)
        return restored

    @classmethod
    def _cleanup_stale_update_stages(cls, paths: PortablePaths) -> None:
        """Remove app-owned preflight trees left before a journal was created."""

        update_root = paths.state / "updates"
        if not update_root.is_dir() or update_root.is_symlink():
            return
        changed = False
        try:
            for candidate in update_root.iterdir():
                if not UPDATE_STAGE_NAME.fullmatch(candidate.name):
                    continue
                if candidate.is_symlink() or not candidate.is_dir():
                    candidate.unlink(missing_ok=True)
                else:
                    shutil.rmtree(candidate)
                changed = True
        except OSError as error:
            raise UpdateError(
                f"cannot remove stale environment update stage: {error}"
            ) from error
        if changed:
            cls._fsync_directory(update_root)

    def install_bundle(self, archive: str | os.PathLike[str]) -> UpdateResult:
        """Validate, preflight with candidate Python, swap, and health-check.

        ``archive`` may be the complete ``.tar.gz``, its multipart descriptor,
        or any one of the descriptor's sibling ``.partNNNN`` files.
        """

        source = Path(archive).expanduser().resolve()
        if not source.is_file():
            raise UpdateError(f"Core bundle does not exist: {source}")
        self.paths.create_layout()
        with (
            self._thread_lock,
            InstanceLock(self.paths.state / "environment-update.lock"),
        ):
            update_root = self.paths.state / "updates"
            update_root.mkdir(parents=True, exist_ok=True)
            stage = update_root / f"environment-stage-{uuid.uuid4().hex}"
            stage.mkdir()
            try:
                materialized = self._materialize_archive(source, stage)
                bundle = self._extract_and_validate(materialized, stage)
                self._check_node_runtime_abi(bundle.manifest["runtime"])
                self._preflight(bundle)
                return self._activate(bundle)
            finally:
                shutil.rmtree(stage, ignore_errors=True)

    @staticmethod
    def _materialize_archive(source: Path, stage: Path) -> Path:
        """Return a complete archive, reconstructing a verified part set."""

        part_match = MULTIPART_PART_NAME.fullmatch(source.name)
        if source.name.endswith(".parts.json"):
            descriptor = source
            selected_part: str | None = None
        elif part_match is not None:
            archive_name = part_match.group(1)
            descriptor = source.with_name(f"{archive_name}.parts.json")
            selected_part = source.name
        else:
            return source

        if not descriptor.is_file():
            raise BundleValidationError(
                f"multipart descriptor is missing: {descriptor.name}"
            )
        try:
            flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            descriptor_fd = os.open(descriptor, flags)
            with os.fdopen(descriptor_fd, "rb") as stream:
                descriptor_stat = os.fstat(stream.fileno())
                if not stat.S_ISREG(descriptor_stat.st_mode):
                    raise BundleValidationError(
                        "multipart descriptor is not a regular file"
                    )
                descriptor_size = descriptor_stat.st_size
                descriptor_bytes = stream.read(MAX_MULTIPART_DESCRIPTOR_BYTES + 1)
            if not 1 <= descriptor_size <= MAX_MULTIPART_DESCRIPTOR_BYTES:
                raise BundleValidationError(
                    "multipart descriptor is empty or exceeds its size limit"
                )
            document = json.loads(
                descriptor_bytes.decode("utf-8"),
                object_pairs_hook=_strict_json_object,
            )
        except BundleValidationError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
            raise BundleValidationError(
                f"cannot read multipart descriptor: {error}"
            ) from error

        archive_name, archive_size, archive_sha256, parts = (
            EnvironmentUpdater._validate_multipart_descriptor(document, descriptor.name)
        )
        expected_names = {part["filename"] for part in parts}
        if selected_part is not None and selected_part not in expected_names:
            raise BundleValidationError(
                "selected part is not listed by its multipart descriptor"
            )

        prefix = f"{archive_name}.part"
        try:
            actual_names = {
                candidate.name
                for candidate in descriptor.parent.iterdir()
                if candidate.name.startswith(prefix)
                and candidate.name != descriptor.name
            }
        except OSError as error:
            raise BundleValidationError(
                f"cannot inspect multipart bundle directory: {error}"
            ) from error
        if actual_names != expected_names:
            missing = sorted(expected_names - actual_names)
            unexpected = sorted(actual_names - expected_names)
            details: list[str] = []
            if missing:
                details.append(f"missing: {', '.join(missing)}")
            if unexpected:
                details.append(f"unexpected: {', '.join(unexpected)}")
            raise BundleValidationError(
                "multipart part set does not exactly match the descriptor"
                + (f" ({'; '.join(details)})" if details else "")
            )

        reconstructed_dir = stage / "reconstructed"
        reconstructed_dir.mkdir()
        reconstructed = reconstructed_dir / archive_name
        full_digest = hashlib.sha256()
        total_written = 0
        try:
            with reconstructed.open("xb") as output:
                for part in parts:
                    part_path = descriptor.parent / str(part["filename"])
                    part_digest = hashlib.sha256()
                    part_written = 0
                    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
                    flags |= getattr(os, "O_NOFOLLOW", 0)
                    part_fd = os.open(part_path, flags)
                    with os.fdopen(part_fd, "rb") as stream:
                        file_stat = os.fstat(stream.fileno())
                        if not stat.S_ISREG(file_stat.st_mode):
                            raise BundleValidationError(
                                "multipart part is not a regular file: "
                                f"{part_path.name}"
                            )
                        if file_stat.st_size != part["size"]:
                            raise BundleValidationError(
                                f"multipart part size mismatch: {part_path.name}"
                            )
                        remaining = int(part["size"])
                        while remaining:
                            block = stream.read(min(8 * 1024 * 1024, remaining))
                            if not block:
                                break
                            output.write(block)
                            part_digest.update(block)
                            full_digest.update(block)
                            part_written += len(block)
                            total_written += len(block)
                            remaining -= len(block)
                        if stream.read(1):
                            raise BundleValidationError(
                                f"multipart part grew while reading: {part_path.name}"
                            )
                    if (
                        part_written != part["size"]
                        or part_digest.hexdigest() != part["sha256"]
                    ):
                        raise BundleValidationError(
                            f"multipart part checksum mismatch: {part_path.name}"
                        )
                output.flush()
                os.fsync(output.fileno())
        except BundleValidationError:
            reconstructed.unlink(missing_ok=True)
            raise
        except OSError as error:
            reconstructed.unlink(missing_ok=True)
            raise BundleValidationError(
                f"cannot reconstruct multipart Core archive: {error}"
            ) from error

        if total_written != archive_size or full_digest.hexdigest() != archive_sha256:
            reconstructed.unlink(missing_ok=True)
            raise BundleValidationError(
                "reconstructed Core archive size or checksum does not match descriptor"
            )
        return reconstructed

    @staticmethod
    def _validate_multipart_descriptor(
        document: Any, descriptor_name: str
    ) -> tuple[str, int, str, list[dict[str, Any]]]:
        """Validate descriptor identity, deterministic order, names, and sizes."""

        if not isinstance(document, dict) or set(document) != {
            "schema_version",
            "format",
            "archive",
            "part_size",
            "parts",
        }:
            raise BundleValidationError("multipart descriptor fields are malformed")
        if (
            document.get("schema_version") != MULTIPART_SCHEMA_VERSION
            or document.get("format") != MULTIPART_FORMAT
        ):
            raise BundleValidationError("unsupported multipart descriptor format")

        archive = document.get("archive")
        if not isinstance(archive, dict) or set(archive) != {
            "filename",
            "size",
            "sha256",
        }:
            raise BundleValidationError("multipart archive identity is malformed")
        archive_name = archive.get("filename")
        archive_size = archive.get("size")
        archive_sha256 = archive.get("sha256")
        if (
            not isinstance(archive_name, str)
            or not CORE_ARCHIVE_NAME.fullmatch(archive_name)
            or descriptor_name != f"{archive_name}.parts.json"
            or not isinstance(archive_size, int)
            or isinstance(archive_size, bool)
            or not 1 <= archive_size <= MAX_ARCHIVE_BYTES
            or not isinstance(archive_sha256, str)
            or not SHA256.fullmatch(archive_sha256)
        ):
            raise BundleValidationError(
                "multipart archive filename, size, or checksum is malformed"
            )

        part_size = document.get("part_size")
        parts = document.get("parts")
        if (
            not isinstance(part_size, int)
            or isinstance(part_size, bool)
            or not 1 <= part_size <= MAX_MULTIPART_PART_BYTES
            or not isinstance(parts, list)
            or not 1 <= len(parts) <= MAX_MULTIPART_PARTS
        ):
            raise BundleValidationError("multipart part size or count is malformed")

        total_size = 0
        checked_parts: list[dict[str, Any]] = []
        for expected_number, part in enumerate(parts, start=1):
            expected_name = f"{archive_name}.part{expected_number:04d}"
            if not isinstance(part, dict) or set(part) != {
                "number",
                "filename",
                "size",
                "sha256",
            }:
                raise BundleValidationError("multipart part entry is malformed")
            size = part.get("size")
            checksum = part.get("sha256")
            if (
                part.get("number") != expected_number
                or part.get("filename") != expected_name
                or not isinstance(size, int)
                or isinstance(size, bool)
                or not 1 <= size <= part_size
                or (expected_number < len(parts) and size != part_size)
                or not isinstance(checksum, str)
                or not SHA256.fullmatch(checksum)
            ):
                raise BundleValidationError(
                    f"multipart part {expected_number} has invalid order or metadata"
                )
            total_size += size
            checked_parts.append(part)
        if total_size != archive_size:
            raise BundleValidationError(
                "multipart part sizes do not equal the declared archive size"
            )
        return archive_name, archive_size, archive_sha256, checked_parts

    def _check_node_runtime_abi(self, candidate: dict[str, Any]) -> None:
        """Do not silently carry compiled node packages across runtime ABIs."""

        if not self.paths.node_runtime_has_packages():
            return
        try:
            active = json.loads(
                self.paths.environment_manifest.read_text(encoding="utf-8")
            )["runtime"]
        except (
            OSError,
            UnicodeError,
            json.JSONDecodeError,
            KeyError,
            TypeError,
        ) as error:
            raise CompatibilityError(
                "cannot verify the ABI of persistent custom-node packages; "
                f"the active environment manifest is incomplete: {error}"
            ) from error
        changed = [
            field
            for field in NODE_EXTENSION_ABI_FIELDS
            if active.get(field) != candidate.get(field)
        ]
        if changed:
            raise CompatibilityError(
                "the candidate changes the custom-node extension ABI "
                f"({', '.join(changed)}), but the persistent custom-node venv "
                "contains packages; install a compatible environment or explicitly "
                "rebuild that venv"
            )

    def _extract_and_validate(self, archive: Path, stage: Path) -> ValidatedBundle:
        try:
            opened = tarfile.open(archive, mode="r:gz")
        except (OSError, tarfile.TarError) as error:
            raise BundleValidationError(
                f"cannot open gzip Core bundle: {error}"
            ) from error

        with opened:
            members = opened.getmembers()
            if len(members) > MAX_MEMBERS:
                raise BundleValidationError("bundle contains too many archive members")
            parsed = [(member, _archive_path(member.name)) for member in members]
            top_levels = {path.parts[0] for _, path in parsed if path.parts}
            if len(top_levels) != 1:
                raise BundleValidationError(
                    "bundle must contain exactly one top-level directory"
                )
            outer = next(iter(top_levels))
            if not ARCHIVE_ROOT.fullmatch(outer):
                raise BundleValidationError(
                    "bundle root must be Portable-Comfy-core-<version>"
                )

            seen: set[str] = set()
            total_size = 0
            normalized: list[tuple[tarfile.TarInfo, PurePosixPath]] = []
            links: list[tuple[tarfile.TarInfo, PurePosixPath]] = []
            for member, archive_path in parsed:
                if not archive_path.parts or archive_path.parts == (outer,):
                    continue
                relative = PurePosixPath(*archive_path.parts[1:])
                if not relative.parts or relative.parts[0] not in {
                    "ComfyUI",
                    "manifest",
                    "LICENSES",
                }:
                    raise BundleValidationError(
                        f"unexpected archive path: {member.name}"
                    )
                path_text = relative.as_posix()
                if path_text in seen:
                    raise BundleValidationError(f"duplicate archive path: {path_text}")
                seen.add(path_text)
                if not (member.isfile() or member.isdir() or member.issym()):
                    raise BundleValidationError(
                        f"hardlinks and special files are forbidden: {path_text}"
                    )
                if member.issym():
                    if relative.parts[0] != "ComfyUI":
                        raise BundleValidationError(
                            f"metadata symlinks are forbidden: {path_text}"
                        )
                    _safe_link_target(relative, member.linkname)
                    links.append((member, relative))
                else:
                    normalized.append((member, relative))
                if member.size < 0:
                    raise BundleValidationError(f"invalid size for {path_text}")
                total_size += member.size
                if total_size > MAX_UNPACKED_BYTES:
                    raise BundleValidationError(
                        "bundle exceeds the unpacked-size limit"
                    )

            bundle_root = stage / "bundle"
            bundle_root.mkdir()
            for member, relative in normalized:
                target = bundle_root.joinpath(*relative.parts)
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    target.chmod(0o755)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                source = opened.extractfile(member)
                if source is None:
                    raise BundleValidationError(f"could not read {relative}")
                with source, target.open("xb") as output:
                    shutil.copyfileobj(source, output, length=1024 * 1024)
                mode = member.mode & 0o777
                target.chmod(mode or 0o644)
            # Create links last, so no archive member can traverse through one.
            for member, relative in links:
                target = bundle_root.joinpath(*relative.parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.symlink_to(member.linkname)

        manifest_path = bundle_root / "manifest" / "environment.json"
        checksum_path = bundle_root / "manifest" / "environment-checksums.sha256"
        if not manifest_path.is_file() or not checksum_path.is_file():
            raise BundleValidationError(
                "manifest/environment.json or environment-checksums.sha256 is missing"
            )
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            checksum_text = checksum_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise BundleValidationError(
                f"invalid environment metadata: {error}"
            ) from error
        self._validate_manifest(
            bundle_root, manifest, checksum_text, archive_root=outer
        )
        return ValidatedBundle(
            root=bundle_root, manifest=manifest, checksums=checksum_text
        )

    @staticmethod
    def _validate_manifest(
        root: Path,
        manifest: Any,
        checksum_text: str,
        *,
        archive_root: str,
    ) -> None:
        if (
            not isinstance(manifest, dict)
            or manifest.get("schema_version") != SCHEMA_VERSION
        ):
            raise BundleValidationError(
                "unsupported environment manifest schema_version"
            )
        if (
            manifest.get("bundle_type") != "environment"
            or manifest.get("app_id") != "portable-comfy"
        ):
            raise BundleValidationError(
                "bundle identity is not a Portable Comfy environment"
            )
        generation_id = manifest.get("generation_id")
        if not isinstance(generation_id, str) or not GENERATION_ID.fullmatch(
            generation_id
        ):
            raise BundleValidationError("generation_id is missing or malformed")
        for key in ("core", "frontend", "runtime"):
            if not isinstance(manifest.get(key), dict):
                raise BundleValidationError(f"manifest is missing {key}")
        core = manifest["core"]
        frontend = manifest["frontend"]
        if set(core) != {"version", "tag", "commit"}:
            raise BundleValidationError(
                "Core identity fields are missing or unexpected"
            )
        if (
            not isinstance(core.get("version"), str)
            or not VERSION.fullmatch(core["version"])
            or core.get("tag") != f"v{core['version']}"
            or not isinstance(core.get("commit"), str)
            or not COMMIT.fullmatch(core["commit"])
        ):
            raise BundleValidationError("Core version, tag, or commit is malformed")
        if set(frontend) != {"version", "commit"}:
            raise BundleValidationError(
                "frontend identity fields are missing or unexpected"
            )
        if (
            not isinstance(frontend.get("version"), str)
            or not VERSION.fullmatch(frontend["version"])
            or not isinstance(frontend.get("commit"), str)
            or not COMMIT.fullmatch(frontend["commit"])
        ):
            raise BundleValidationError("frontend version or commit is malformed")
        expected_archive_root = f"Portable-Comfy-core-v{core['version']}"
        if archive_root != expected_archive_root:
            raise BundleValidationError(
                "Core bundle root does not match manifest core.version: "
                f"expected {expected_archive_root}, found {archive_root}"
            )
        runtime = manifest["runtime"]
        for field in RUNTIME_FIELDS:
            if not isinstance(runtime.get(field), str) or not runtime[field]:
                raise BundleValidationError(f"runtime field {field} is missing")
        if not SHA256.fullmatch(runtime["requirements_lock_sha256"]):
            raise BundleValidationError("runtime requirements lock digest is malformed")

        files = manifest.get("files")
        if not isinstance(files, list) or not files:
            raise BundleValidationError("manifest has no payload files")
        regular: dict[str, tuple[str, int]] = {}
        symlinks: dict[str, str] = {}
        for item in files:
            if not isinstance(item, dict):
                raise BundleValidationError("manifest file entry is not an object")
            path_text = item.get("path")
            kind = item.get("type")
            if not isinstance(path_text, str):
                raise BundleValidationError("manifest file path is malformed")
            path = _payload_path(path_text)
            if path_text in regular or path_text in symlinks:
                raise BundleValidationError(f"duplicate manifest path: {path_text}")
            if kind == "file":
                digest, size = item.get("sha256"), item.get("size")
                if (
                    not isinstance(digest, str)
                    or not SHA256.fullmatch(digest)
                    or not isinstance(size, int)
                    or isinstance(size, bool)
                    or size < 0
                    or set(item) != {"path", "type", "sha256", "size"}
                ):
                    raise BundleValidationError(
                        f"invalid digest or size for {path_text}"
                    )
                regular[path_text] = (digest, size)
            elif kind == "symlink":
                target = item.get("target")
                if not isinstance(target, str) or set(item) != {
                    "path",
                    "type",
                    "target",
                }:
                    raise BundleValidationError(
                        f"invalid symlink target for {path_text}"
                    )
                _safe_link_target(path, target)
                symlinks[path_text] = target
            else:
                raise BundleValidationError(f"invalid payload type for {path_text}")

        actual_files: set[str] = set()
        actual_links: set[str] = set()
        for path in (root / "ComfyUI").rglob("*"):
            relative = path.relative_to(root).as_posix()
            if path.is_symlink():
                actual_links.add(relative)
                if relative not in symlinks or os.readlink(path) != symlinks[relative]:
                    raise BundleValidationError(
                        f"unlisted or changed symlink: {relative}"
                    )
                try:
                    path.resolve(strict=True).relative_to((root / "ComfyUI").resolve())
                except (FileNotFoundError, RuntimeError, ValueError) as error:
                    raise BundleValidationError(
                        f"dangling, cyclic, or escaping symlink: {relative}"
                    ) from error
            elif path.is_file():
                actual_files.add(relative)
                if relative not in regular:
                    raise BundleValidationError(
                        f"unlisted environment file: {relative}"
                    )
                digest, size = regular[relative]
                if path.stat().st_size != size or _digest(path) != digest:
                    raise BundleValidationError(
                        f"checksum or size mismatch: {relative}"
                    )
        if actual_files != set(regular) or actual_links != set(symlinks):
            raise BundleValidationError(
                "manifest references payload entries absent from archive"
            )

        checksum_entries: dict[str, str] = {}
        for line in checksum_text.splitlines():
            digest, separator, path_text = line.partition("  ")
            if (
                separator != "  "
                or not SHA256.fullmatch(digest)
                or path_text in checksum_entries
            ):
                raise BundleValidationError("environment-checksums.sha256 is malformed")
            checksum_entries[path_text] = digest
        if checksum_entries != {path: value[0] for path, value in regular.items()}:
            raise BundleValidationError(
                "environment-checksums.sha256 disagrees with environment.json"
            )

        for path_text in REQUIRED_LICENSE_FILES:
            entry = regular.get(path_text)
            if entry is None or entry[1] <= 0:
                raise BundleValidationError(
                    f"required redistribution notice is missing or empty: {path_text}"
                )
        installed_entry = regular.get(RUNTIME_INSTALLED_REQUIREMENTS)
        if installed_entry is None or installed_entry[1] <= 0:
            raise BundleValidationError(
                "installed runtime requirements freeze is missing or empty"
            )
        EnvironmentUpdater._validate_frontend_license_inventory(root, regular, frontend)
        EnvironmentUpdater._validate_runtime_license_inventory(root, regular)

        identity_path = root / "ComfyUI" / IDENTITY_NAME
        if f"ComfyUI/{IDENTITY_NAME}" not in regular:
            raise BundleValidationError(
                f"{IDENTITY_NAME} is absent from the payload manifest"
            )
        try:
            identity = json.loads(identity_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise BundleValidationError(f"invalid {IDENTITY_NAME}: {error}") from error
        wanted_identity = {
            "schema_version": 1,
            "app_id": manifest["app_id"],
            "generation_id": manifest["generation_id"],
            "core": manifest["core"],
            "frontend": manifest["frontend"],
            "runtime": manifest["runtime"],
        }
        if identity != wanted_identity:
            raise BundleValidationError(
                f"{IDENTITY_NAME} disagrees with environment.json"
            )

        required = (
            root / "ComfyUI" / "main.py",
            root / "ComfyUI" / "frontend" / "index.html",
            root / "ComfyUI" / "runtime" / "python" / "bin" / "python-portable",
        )
        if not all(path.is_file() for path in required):
            raise BundleValidationError(
                "environment lacks Core, compiled frontend, or portable Python"
            )
        python = required[-1]
        if not stat.S_IMODE(python.stat().st_mode) & 0o111:
            raise BundleValidationError("candidate python-portable is not executable")
        lock_path_text = runtime["requirements_lock_path"]
        lock_path = _payload_path(lock_path_text)
        lock_file = root.joinpath(*lock_path.parts)
        if (
            lock_path_text not in regular
            or not lock_file.is_file()
            or _digest(lock_file) != runtime["requirements_lock_sha256"]
        ):
            raise BundleValidationError(
                "runtime requirements lock does not match manifest"
            )

    @staticmethod
    def _validate_frontend_license_inventory(
        root: Path,
        regular: dict[str, tuple[str, int]],
        frontend: dict[str, Any],
    ) -> None:
        inventory_entry = regular.get(FRONTEND_LICENSE_INVENTORY)
        if inventory_entry is None or inventory_entry[1] <= 0:
            raise BundleValidationError(
                "frontend dependency license inventory is missing or empty"
            )
        inventory_path = root / FRONTEND_LICENSE_INVENTORY
        try:
            inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise BundleValidationError(
                f"invalid frontend dependency license inventory: {error}"
            ) from error
        packages = inventory.get("packages") if isinstance(inventory, dict) else None
        if (
            not isinstance(inventory, dict)
            or inventory.get("schema_version") != 1
            or inventory.get("frontend") != frontend
            or not isinstance(packages, list)
            or not packages
        ):
            raise BundleValidationError(
                "frontend dependency license inventory has an unsupported schema"
            )

        with_notices = sum(
            isinstance(package, dict) and bool(package.get("notice_files"))
            for package in packages
        )
        summary = {
            "distributions": len(packages),
            "with_notice_files": with_notices,
            "metadata_only": len(packages) - with_notices,
        }
        if inventory.get("summary") != summary or with_notices != len(packages):
            raise BundleValidationError(
                "frontend dependency license inventory is not comprehensive"
            )

        inventory_parent = PurePosixPath(FRONTEND_LICENSE_INVENTORY).parent
        seen: set[tuple[str, str]] = set()
        for package in packages:
            if not isinstance(package, dict):
                raise BundleValidationError(
                    "frontend dependency license inventory contains a malformed package"
                )
            name = package.get("name")
            version = package.get("version")
            expression = package.get("license")
            metadata_file = package.get("metadata_file")
            notice_files = package.get("notice_files")
            if (
                not isinstance(name, str)
                or not name
                or "\\" in name
                or "\x00" in name
                or not isinstance(version, str)
                or not version
                or any(character.isspace() for character in version)
                or not isinstance(expression, str)
                or not expression.strip()
                or not isinstance(metadata_file, str)
                or not isinstance(notice_files, list)
            ):
                raise BundleValidationError(
                    "frontend dependency license inventory contains malformed fields"
                )
            identity = (name, version)
            if identity in seen:
                raise BundleValidationError(
                    f"frontend dependency license inventory has a duplicate: {name}"
                )
            seen.add(identity)
            for relative_text in [metadata_file, *notice_files]:
                if not isinstance(relative_text, str):
                    raise BundleValidationError(
                        "frontend dependency license inventory has a malformed path"
                    )
                relative = PurePosixPath(relative_text)
                if (
                    relative.is_absolute()
                    or not relative.parts
                    or ".." in relative.parts
                    or "\\" in relative_text
                    or "\x00" in relative_text
                    or relative.as_posix() != relative_text
                ):
                    raise BundleValidationError(
                        "frontend dependency license inventory has an unsafe path"
                    )
                payload_path = (inventory_parent / relative).as_posix()
                entry = regular.get(payload_path)
                if entry is None or entry[1] <= 0:
                    raise BundleValidationError(
                        "frontend dependency notice is missing or empty: "
                        f"{payload_path}"
                    )

            bundled_assets = package.get("bundled_assets")
            if bundled_assets is not None:
                if not isinstance(bundled_assets, list) or not bundled_assets:
                    raise BundleValidationError(
                        "frontend bundled-asset inventory is malformed"
                    )
                for asset in bundled_assets:
                    if not isinstance(asset, dict) or set(asset) != {
                        "path",
                        "sha256",
                        "size",
                    }:
                        raise BundleValidationError(
                            "frontend bundled-asset inventory is malformed"
                        )
                    asset_path = asset.get("path")
                    asset_sha256 = asset.get("sha256")
                    asset_size = asset.get("size")
                    if not isinstance(asset_path, str):
                        raise BundleValidationError(
                            "frontend bundled-asset path is malformed"
                        )
                    relative = PurePosixPath(asset_path)
                    if (
                        relative.is_absolute()
                        or not relative.parts
                        or ".." in relative.parts
                        or "\\" in asset_path
                        or "\x00" in asset_path
                        or relative.as_posix() != asset_path
                        or not isinstance(asset_sha256, str)
                        or not SHA256.fullmatch(asset_sha256)
                        or not isinstance(asset_size, int)
                        or isinstance(asset_size, bool)
                        or asset_size <= 0
                    ):
                        raise BundleValidationError(
                            "frontend bundled-asset metadata is malformed"
                        )
                    payload_path = (
                        PurePosixPath("ComfyUI/frontend") / relative
                    ).as_posix()
                    entry = regular.get(payload_path)
                    if (
                        entry is None
                        or entry[0] != asset_sha256
                        or entry[1] != asset_size
                    ):
                        raise BundleValidationError(
                            "frontend bundled asset disagrees with the payload: "
                            f"{payload_path}"
                        )

        source_path = (
            f"ComfyUI/frontend/SOURCE-ComfyUI-frontend-{frontend.get('version')}.tar.gz"
        )
        source_entry = regular.get(source_path)
        if source_entry is None or source_entry[1] <= 0:
            raise BundleValidationError(
                "pinned frontend source snapshot is missing or empty"
            )

    @staticmethod
    def _validate_runtime_license_inventory(
        root: Path, regular: dict[str, tuple[str, int]]
    ) -> None:
        inventory_path = root / RUNTIME_LICENSE_INVENTORY
        try:
            inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise BundleValidationError(
                f"invalid runtime package license inventory: {error}"
            ) from error
        if (
            not isinstance(inventory, dict)
            or inventory.get("schema_version") != 2
            or not isinstance(inventory.get("packages"), list)
            or not inventory["packages"]
        ):
            raise BundleValidationError(
                "runtime package license inventory has an unsupported schema"
            )
        package_count = len(inventory["packages"])
        if inventory.get("summary") != {
            "distributions": package_count,
            "with_license_files": package_count,
            "metadata_only": 0,
            "unidentified": 0,
        }:
            raise BundleValidationError(
                "runtime package license inventory is not comprehensive"
            )

        licensed: set[str] = set()
        inventory_versions: dict[str, str] = {}
        inventory_parent = PurePosixPath(RUNTIME_LICENSE_INVENTORY).parent
        for package in inventory["packages"]:
            if not isinstance(package, dict):
                raise BundleValidationError(
                    "runtime package license inventory contains a malformed package"
                )
            name = package.get("name")
            version = package.get("version")
            files = package.get("license_files")
            if (
                not isinstance(name, str)
                or not DISTRIBUTION_NAME.fullmatch(name)
                or not isinstance(version, str)
                or not version
                or "=" in version
                or any(character.isspace() for character in version)
                or not isinstance(files, list)
            ):
                raise BundleValidationError(
                    "runtime package license inventory contains malformed fields"
                )
            normalized_name = re.sub(r"[-_.]+", "-", name).lower()
            if normalized_name in inventory_versions:
                raise BundleValidationError(
                    "runtime package license inventory contains duplicate names: "
                    f"{normalized_name}"
                )
            inventory_versions[normalized_name] = version
            if not files:
                raise BundleValidationError(
                    f"runtime package has no bundled license file: {normalized_name}"
                )
            licensed.add(normalized_name)
            for relative_text in files:
                if not isinstance(relative_text, str):
                    raise BundleValidationError(
                        "runtime package license inventory has a malformed path"
                    )
                relative = PurePosixPath(relative_text)
                if (
                    relative.is_absolute()
                    or not relative.parts
                    or ".." in relative.parts
                    or "\\" in relative_text
                    or "\x00" in relative_text
                    or relative.as_posix() != relative_text
                ):
                    raise BundleValidationError(
                        "runtime package license inventory has an unsafe path"
                    )
                payload_path = (inventory_parent / relative).as_posix()
                entry = regular.get(payload_path)
                if entry is None or entry[1] <= 0:
                    raise BundleValidationError(
                        "runtime package license file is missing or empty: "
                        f"{payload_path}"
                    )
        missing = sorted(REQUIRED_RUNTIME_LICENSE_PACKAGES - licensed)
        if missing:
            raise BundleValidationError(
                "runtime package has no bundled license file: " + missing[0]
            )
        frozen = EnvironmentUpdater._read_frozen_requirements(
            root / RUNTIME_INSTALLED_REQUIREMENTS
        )
        if frozen != inventory_versions:
            raise BundleValidationError(
                "installed runtime requirements and package license inventory disagree"
            )

    @staticmethod
    def _read_frozen_requirements(path: Path) -> dict[str, str]:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError) as error:
            raise BundleValidationError(
                f"cannot read installed runtime requirements: {error}"
            ) from error
        if not lines:
            raise BundleValidationError("installed runtime requirements are empty")
        frozen: dict[str, str] = {}
        for line_number, line in enumerate(lines, start=1):
            match = FROZEN_REQUIREMENT.fullmatch(line)
            if match is None:
                raise BundleValidationError(
                    "installed runtime requirements line is not exact "
                    f"NAME==VERSION at line {line_number}"
                )
            name, version = match.groups()
            normalized_name = re.sub(r"[-_.]+", "-", name).lower()
            if normalized_name in frozen:
                raise BundleValidationError(
                    "installed runtime requirements contain a duplicate package: "
                    f"{normalized_name}"
                )
            frozen[normalized_name] = version
        return frozen

    def _preflight(self, bundle: ValidatedBundle) -> None:
        candidate = bundle.environment
        candidate_prefix = candidate / "runtime" / "python"
        scratch = bundle.root / "preflight"
        scratch.mkdir()
        for directory in (
            "cache",
            "data/custom_nodes",
            "data/input",
            "data/models",
            "data/output",
            "data/temp",
            "temp",
            "user",
        ):
            (scratch / directory).mkdir(parents=True, exist_ok=True)
        self.paths.repair_runtime_metadata(candidate_prefix)
        python = self.paths.python_executable(prefix=candidate_prefix)
        environment = self.paths.server_environment(
            python_prefix=candidate_prefix,
            comfyui_path=candidate,
            include_node_runtime=False,
            cache_root=scratch / "cache",
        )
        runtime = bundle.manifest["runtime"]
        version_script = (
            "import platform,sys,torch,torchvision,torchaudio; "
            "expected=__import__('json').loads(sys.argv[1]); "
            "actual={'python':platform.python_version(),'torch':torch.__version__,"
            "'torchvision':torchvision.__version__,'torchaudio':torchaudio.__version__,"
            "'cuda':torch.version.cuda}; "
            "assert actual == {k:expected[k] for k in actual}, (actual,expected)"
        )
        commands = (
            [str(python), "-s", "-c", version_script, json.dumps(runtime)],
            [str(python), "-s", "-m", "pip", "check"],
            self.paths.comfy_command(
                8188,
                cpu=True,
                disable_custom_nodes=True,
                quick_test=True,
                comfyui_path=candidate,
                python_prefix=candidate_prefix,
                database_url="sqlite:///:memory:",
                include_extra_model_paths=False,
                base_directory=scratch / "data",
                user_directory=scratch / "user",
                temp_directory=scratch / "temp",
                use_node_runtime=False,
            ),
        )
        for command in commands:
            try:
                completed = self._run(
                    command,
                    cwd=candidate,
                    env=environment,
                    text=True,
                    capture_output=True,
                    timeout=300,
                    check=False,
                )
            except (OSError, subprocess.SubprocessError) as error:
                raise UpdateError(
                    f"could not preflight candidate environment: {error}"
                ) from error
            if completed.returncode:
                detail = (
                    completed.stderr or completed.stdout or "unknown preflight failure"
                ).strip()
                raise UpdateError(f"candidate environment preflight failed: {detail}")

    @classmethod
    def _seal_environment(
        cls, environment: Path, delivery_manifest: dict[str, Any]
    ) -> tuple[dict[str, Any], str]:
        """Describe the installed bytes after relocation repair.

        Delivery checksums authenticate the archive before it executes. The
        installed manifest is deliberately resealed because the relocatable
        runtime updates text metadata and its prefix stamp at the final path.
        """

        payload_root = environment.parent
        entries: list[dict[str, object]] = []
        for path in sorted(environment.rglob("*")):
            relative = path.relative_to(payload_root)
            path_text = relative.as_posix()
            mode = path.lstat().st_mode
            if stat.S_ISDIR(mode):
                continue
            if stat.S_ISLNK(mode):
                target = os.readlink(path)
                _safe_link_target(PurePosixPath(path_text), target)
                try:
                    path.resolve(strict=True).relative_to(environment.resolve())
                except (FileNotFoundError, RuntimeError, ValueError) as error:
                    raise UpdateError(
                        f"cannot seal dangling, cyclic, or escaping link: {path_text}"
                    ) from error
                entries.append({"path": path_text, "type": "symlink", "target": target})
            elif stat.S_ISREG(mode):
                entries.append(
                    {
                        "path": path_text,
                        "type": "file",
                        "sha256": _digest(path),
                        "size": path.stat().st_size,
                    }
                )
            else:
                raise UpdateError(f"cannot seal special environment file: {path_text}")
        manifest = copy.deepcopy(delivery_manifest)
        manifest["files"] = entries
        checksums = "".join(
            f"{item['sha256']}  {item['path']}\n"
            for item in entries
            if item["type"] == "file"
        )
        return manifest, checksums

    @classmethod
    def reseal_active_environment(cls, paths: PortablePaths) -> None:
        """Refresh installed integrity metadata after an explicit relocation."""

        try:
            delivery_manifest = json.loads(
                paths.environment_manifest.read_text(encoding="utf-8")
            )
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise UpdateError(
                f"cannot read active environment manifest: {error}"
            ) from error
        if not isinstance(delivery_manifest, dict):
            raise UpdateError("active environment manifest is not an object")
        manifest, checksums = cls._seal_environment(paths.comfyui, delivery_manifest)
        cls._atomic_write(paths.environment_checksums, checksums)
        cls._atomic_write(
            paths.environment_manifest,
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        )

    def _activate(self, bundle: ValidatedBundle) -> UpdateResult:
        was_running = self.supervisor.is_running
        had_active_environment = self.paths.comfyui.is_dir()
        if self.paths.comfyui.exists() and not had_active_environment:
            raise UpdateError(
                f"installed environment path is not a directory: {self.paths.comfyui}"
            )
        rollback_root = self.paths.state / "rollback"
        rollback_root.mkdir(parents=True, exist_ok=True)
        transaction_id = uuid.uuid4().hex
        transaction = self.paths.state / "transactions" / transaction_id
        transaction.mkdir(parents=True, exist_ok=False)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup = rollback_root / f"ComfyUI-{stamp}-{transaction_id[:8]}"
        failed = bundle.root / "failed-ComfyUI"
        old_manifest = (
            self.paths.environment_manifest.read_bytes()
            if self.paths.environment_manifest.exists()
            else None
        )
        old_checksums = (
            self.paths.environment_checksums.read_bytes()
            if self.paths.environment_checksums.exists()
            else None
        )
        if old_manifest is not None:
            self._atomic_bytes(transaction / "environment.json", old_manifest)
        if old_checksums is not None:
            self._atomic_bytes(
                transaction / "environment-checksums.sha256", old_checksums
            )
        journal: dict[str, Any] = {
            "schema_version": 2,
            "transaction_id": transaction_id,
            "phase": "prepared",
            "backup": backup.relative_to(self.paths.root).as_posix(),
            "transaction": transaction.relative_to(self.paths.root).as_posix(),
            "had_active_environment": had_active_environment,
            "had_environment_manifest": old_manifest is not None,
            "had_environment_checksums": old_checksums is not None,
        }
        self._write_journal(journal)
        self.supervisor.stop()
        activated = False
        try:
            if had_active_environment:
                self.paths.comfyui.replace(backup)
                self._fsync_directory(self.paths.root)
                self._fsync_directory(backup.parent)
                journal["phase"] = "old_moved"
            else:
                journal["phase"] = "old_absent"
            self._write_journal(journal)
            bundle.environment.replace(self.paths.comfyui)
            self._fsync_directory(self.paths.root)
            self._fsync_directory(bundle.root)
            activated = True
            journal["phase"] = "candidate_active"
            self._write_journal(journal)
            # The candidate moved once more after staged preflight. Repair only
            # relocatable text metadata; persistent node packages remain outside.
            self.paths.repair_runtime_metadata()
            installed_manifest, installed_checksums = self._seal_environment(
                self.paths.comfyui, bundle.manifest
            )
            # Checksums go first; the manifest is the commit record for the
            # resealed active generation. The transaction journal remains live
            # until the candidate passes its HTTP health check.
            self._atomic_write(self.paths.environment_checksums, installed_checksums)
            self._atomic_write(
                self.paths.environment_manifest,
                json.dumps(installed_manifest, indent=2, sort_keys=True) + "\n",
            )
            self.supervisor.start()
            if not was_running:
                self.supervisor.stop()
            core = bundle.manifest["core"]
            self._prune_rollbacks(keep=2)
            self._clear_transaction(transaction)
            return UpdateResult(
                version=str(core.get("version") or core.get("tag") or "unknown"),
                commit=str(core.get("commit") or "unknown"),
                generation_id=bundle.manifest["generation_id"],
                restarted=was_running,
            )
        except Exception as error:
            rollback_errors: list[str] = []
            try:
                self.supervisor.stop()
            except Exception as stop_error:
                rollback_errors.append(f"stop failed: {stop_error}")
            try:
                # Persist rollback intent before moving either generation. If
                # power is lost after backup -> ComfyUI but before journal
                # cleanup, startup can distinguish the restored old tree from
                # an uncommitted candidate whose backup has disappeared.
                journal["phase"] = "rollback_started"
                self._write_journal(journal)
                if activated and self.paths.comfyui.exists():
                    self.paths.comfyui.replace(failed)
                if backup.exists():
                    backup.replace(self.paths.comfyui)
                    self._fsync_directory(self.paths.root)
                    self._fsync_directory(backup.parent)
                self._restore_file(self.paths.environment_manifest, old_manifest)
                self._restore_file(self.paths.environment_checksums, old_checksums)
                if was_running:
                    self.supervisor.start()
                self._clear_transaction(transaction)
            except Exception as rollback_error:
                rollback_errors.append(f"rollback failed: {rollback_error}")
            detail = f"full-Core update failed and was rolled back: {error}"
            if rollback_errors:
                detail += "; " + "; ".join(rollback_errors)
            raise UpdateError(detail) from error

    def _prune_rollbacks(self, keep: int) -> None:
        rollback_root = self.paths.state / "rollback"
        backups = sorted(
            (path for path in rollback_root.glob("ComfyUI-*") if path.is_dir()),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        for old in backups[keep:]:
            shutil.rmtree(old, ignore_errors=True)

    @staticmethod
    def _journal_path(
        paths: PortablePaths,
        journal: dict[str, Any],
        key: str,
        allowed_root: Path,
    ) -> Path:
        value = journal.get(key)
        if not isinstance(value, str):
            raise UpdateError(f"interrupted-update journal is missing {key}")
        relative = PurePosixPath(value)
        if relative.is_absolute() or ".." in relative.parts or not relative.parts:
            raise UpdateError(f"interrupted-update journal has unsafe {key} path")
        resolved = paths.root.joinpath(*relative.parts).resolve(strict=False)
        if not resolved.is_relative_to(allowed_root.resolve(strict=False)):
            raise UpdateError(f"interrupted-update journal {key} escapes managed state")
        return resolved

    @classmethod
    def _restore_journal_metadata(
        cls,
        paths: PortablePaths,
        transaction: Path,
        journal: dict[str, Any],
    ) -> None:
        targets = (
            (
                "had_environment_manifest",
                transaction / "environment.json",
                paths.environment_manifest,
            ),
            (
                "had_environment_checksums",
                transaction / "environment-checksums.sha256",
                paths.environment_checksums,
            ),
        )
        for key, saved, target in targets:
            existed = journal.get(key)
            if not isinstance(existed, bool):
                raise UpdateError(f"interrupted-update journal is missing {key}")
            if existed:
                try:
                    content = saved.read_bytes()
                except OSError as error:
                    raise UpdateError(
                        f"cannot restore saved update metadata: {error}"
                    ) from error
                cls._atomic_bytes(target, content)
            else:
                target.unlink(missing_ok=True)

    def _write_journal(self, journal: dict[str, Any]) -> None:
        marker = self.paths.state / TRANSACTION_MARKER
        self._atomic_bytes(
            marker,
            (json.dumps(journal, indent=2, sort_keys=True) + "\n").encode("utf-8"),
        )

    def _clear_transaction(self, transaction: Path) -> None:
        marker = self.paths.state / TRANSACTION_MARKER
        marker.unlink(missing_ok=True)
        self._fsync_directory(marker.parent)
        shutil.rmtree(transaction, ignore_errors=True)

    @classmethod
    def _atomic_write(cls, path: Path, text: str) -> None:
        cls._atomic_bytes(path, text.encode("utf-8"))

    @classmethod
    def _atomic_bytes(cls, path: Path, content: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        with temporary.open("wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        cls._fsync_directory(path.parent)

    @staticmethod
    def _restore_file(path: Path, content: bytes | None) -> None:
        if content is None:
            path.unlink(missing_ok=True)
            return
        EnvironmentUpdater._atomic_bytes(path, content)

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        try:
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        except OSError:
            return
        try:
            os.fsync(descriptor)
        except OSError:
            pass
        finally:
            os.close(descriptor)


# Keep the old import name for the UI/plugin boundary while changing semantics.
CoreUpdater = EnvironmentUpdater
