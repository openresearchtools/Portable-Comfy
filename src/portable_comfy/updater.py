"""Validated, transactional replacement of the ComfyUI Core/frontend tree."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
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


SHA256 = re.compile(r"^[0-9a-f]{64}$")
MAX_MEMBERS = 200_000
MAX_UNPACKED_BYTES = 4 * 1024**3
TRANSACTION_MARKER = "core-update-transaction.json"
RUNTIME_COMPATIBILITY_KEYS = (
    "python",
    "torch",
    "cuda",
    "platform",
    "requirements_lock_sha256",
)


class UpdateError(RuntimeError):
    """The Core update could not be completed safely."""


class BundleValidationError(UpdateError):
    pass


class CompatibilityError(UpdateError):
    pass


@dataclass(frozen=True, slots=True)
class ValidatedBundle:
    stage: Path
    manifest: dict[str, Any]
    checksums: str

    @property
    def core(self) -> Path:
        return self.stage / "ComfyUI"


@dataclass(frozen=True, slots=True)
class UpdateResult:
    version: str
    commit: str
    restarted: bool


def _digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def _safe_member_name(name: str) -> PurePosixPath:
    while name.startswith("./"):
        name = name[2:]
    if name in {"", "."}:
        return PurePosixPath()
    if "\\" in name or "\x00" in name:
        raise BundleValidationError(f"unsafe archive path: {name!r}")
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts:
        raise BundleValidationError(f"unsafe archive path: {name!r}")
    if path.parts[0] not in {
        "ComfyUI",
        "LICENSES",
        "manifest.json",
        "checksums.sha256",
    }:
        raise BundleValidationError(f"unexpected archive path: {name}")
    return path


class CoreUpdater:
    """Install local Core bundles without touching models, nodes, or user data."""

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
        """Restore the previous Core if power was lost during a swap.

        The journal is written and fsynced before either rename. Any surviving
        backup therefore wins over an uncommitted active candidate. Persistent
        data and the Python runtime are never involved in recovery.
        """

        marker = paths.state / TRANSACTION_MARKER
        if not marker.exists():
            return False
        try:
            journal = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise UpdateError(
                f"cannot read interrupted-update journal: {error}"
            ) from error
        if not isinstance(journal, dict) or journal.get("schema_version") != 1:
            raise UpdateError("interrupted-update journal has an unsupported schema")

        backup = cls._journal_path(paths, journal, "backup", paths.state / "rollback")
        transaction = cls._journal_path(
            paths, journal, "transaction", paths.state / "transactions"
        )
        restored = False
        if backup.is_dir():
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
        elif not paths.comfyui.is_dir():
            raise UpdateError(
                "an interrupted Core update left neither an active Core nor its rollback; "
                f"inspect {transaction}"
            )
        marker.unlink(missing_ok=True)
        cls._fsync_directory(marker.parent)
        shutil.rmtree(transaction, ignore_errors=True)
        return restored

    def install_bundle(self, archive: str | os.PathLike[str]) -> UpdateResult:
        """Validate, stage, preflight, atomically swap, and health-check Core."""

        source = Path(archive).expanduser().resolve()
        if not source.is_file():
            raise UpdateError(f"Core bundle does not exist: {source}")
        self.paths.create_layout()
        with self._thread_lock, InstanceLock(self.paths.state / "core-update.lock"):
            update_root = self.paths.state / "updates"
            update_root.mkdir(parents=True, exist_ok=True)
            stage = update_root / f"core-stage-{uuid.uuid4().hex}"
            stage.mkdir()
            try:
                bundle = self._extract_and_validate(source, stage)
                self._check_runtime(bundle.manifest["runtime"])
                self._preflight(bundle.core)
                return self._activate(bundle)
            finally:
                shutil.rmtree(stage, ignore_errors=True)

    def _extract_and_validate(self, archive: Path, stage: Path) -> ValidatedBundle:
        try:
            opened = tarfile.open(archive, mode="r:gz")
        except (OSError, tarfile.TarError) as error:
            raise BundleValidationError(
                f"cannot open gzip Core bundle: {error}"
            ) from error
        seen: set[str] = set()
        total_size = 0
        with opened:
            members = opened.getmembers()
            if len(members) > MAX_MEMBERS:
                raise BundleValidationError("bundle contains too many archive members")
            for member in members:
                relative = _safe_member_name(member.name)
                if not relative.parts:
                    continue
                normalized = relative.as_posix()
                if normalized in seen:
                    raise BundleValidationError(f"duplicate archive path: {normalized}")
                seen.add(normalized)
                if not (member.isfile() or member.isdir()):
                    raise BundleValidationError(
                        f"links and special files are forbidden: {normalized}"
                    )
                if member.size < 0:
                    raise BundleValidationError(f"invalid size for {normalized}")
                total_size += member.size
                if total_size > MAX_UNPACKED_BYTES:
                    raise BundleValidationError(
                        "bundle exceeds the unpacked-size limit"
                    )
            for member in members:
                relative = _safe_member_name(member.name)
                if not relative.parts:
                    continue
                target = stage.joinpath(*relative.parts)
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                source = opened.extractfile(member)
                if source is None:
                    raise BundleValidationError(f"could not read {relative}")
                with source, target.open("xb") as output:
                    shutil.copyfileobj(source, output, length=1024 * 1024)
                target.chmod(
                    0o755 if relative.as_posix() == "ComfyUI/main.py" else 0o644
                )

        manifest_path = stage / "manifest.json"
        checksum_path = stage / "checksums.sha256"
        if not manifest_path.is_file() or not checksum_path.is_file():
            raise BundleValidationError("manifest.json or checksums.sha256 is missing")
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise BundleValidationError(f"invalid manifest.json: {error}") from error
        checksum_text = checksum_path.read_text(encoding="utf-8")
        self._validate_manifest(stage, manifest, checksum_text)
        return ValidatedBundle(stage=stage, manifest=manifest, checksums=checksum_text)

    @staticmethod
    def _validate_manifest(stage: Path, manifest: Any, checksum_text: str) -> None:
        if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
            raise BundleValidationError("unsupported Core manifest schema_version")
        if (
            manifest.get("bundle_type") != "core"
            or manifest.get("app_id") != "portable-comfy"
        ):
            raise BundleValidationError("bundle identity is not Portable Comfy Core")
        for key in ("core", "frontend", "runtime"):
            if not isinstance(manifest.get(key), dict):
                raise BundleValidationError(f"manifest is missing {key}")
        required_runtime = set(RUNTIME_COMPATIBILITY_KEYS)
        if not required_runtime <= manifest["runtime"].keys():
            raise BundleValidationError("runtime compatibility fields are incomplete")
        lock_digest = manifest["runtime"]["requirements_lock_sha256"]
        if not isinstance(lock_digest, str) or not SHA256.fullmatch(lock_digest):
            raise BundleValidationError("runtime requirements lock digest is malformed")
        files = manifest.get("files")
        if not isinstance(files, list) or not files:
            raise BundleValidationError("manifest has no files")

        expected: dict[str, tuple[str, int]] = {}
        for item in files:
            if not isinstance(item, dict):
                raise BundleValidationError("manifest file entry is not an object")
            path_text, digest, size = (
                item.get("path"),
                item.get("sha256"),
                item.get("size"),
            )
            if (
                not isinstance(path_text, str)
                or not isinstance(digest, str)
                or not isinstance(size, int)
            ):
                raise BundleValidationError("malformed manifest file entry")
            path = _safe_member_name(path_text)
            if not path.parts or path.parts[0] != "ComfyUI" or path_text in expected:
                raise BundleValidationError(
                    f"invalid or duplicate manifest path: {path_text}"
                )
            if not SHA256.fullmatch(digest) or size < 0:
                raise BundleValidationError(f"invalid digest or size for {path_text}")
            expected[path_text] = (digest, size)

        actual: set[str] = set()
        for path in (stage / "ComfyUI").rglob("*"):
            if path.is_symlink():
                raise BundleValidationError(f"links are forbidden: {path}")
            if not path.is_file():
                continue
            relative = path.relative_to(stage).as_posix()
            actual.add(relative)
            if relative not in expected:
                raise BundleValidationError(f"unlisted Core file: {relative}")
            digest, size = expected[relative]
            if path.stat().st_size != size or _digest(path) != digest:
                raise BundleValidationError(f"checksum or size mismatch: {relative}")
        if actual != set(expected):
            raise BundleValidationError(
                "manifest references files absent from the archive"
            )

        checksum_entries: dict[str, str] = {}
        for line in checksum_text.splitlines():
            digest, separator, path_text = line.partition("  ")
            if (
                separator != "  "
                or not SHA256.fullmatch(digest)
                or path_text in checksum_entries
            ):
                raise BundleValidationError("checksums.sha256 is malformed")
            checksum_entries[path_text] = digest
        wanted = {path: value[0] for path, value in expected.items()}
        if checksum_entries != wanted:
            raise BundleValidationError("checksums.sha256 disagrees with manifest.json")
        if not (stage / "ComfyUI" / "main.py").is_file():
            raise BundleValidationError("ComfyUI/main.py is missing")
        if not (stage / "ComfyUI" / "frontend" / "index.html").is_file():
            raise BundleValidationError("compiled frontend index.html is missing")

    def _check_runtime(self, required: dict[str, Any]) -> None:
        try:
            installed = json.loads(
                self.paths.runtime_manifest.read_text(encoding="utf-8")
            )
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise CompatibilityError(
                f"cannot read installed runtime manifest: {error}"
            ) from error
        for key in RUNTIME_COMPATIBILITY_KEYS:
            if installed.get(key) != required.get(key):
                raise CompatibilityError(
                    f"Core bundle requires {key}={required.get(key)!r}, "
                    f"but the portable runtime provides {installed.get(key)!r}"
                )

    def _preflight(self, core: Path) -> None:
        command = self.paths.comfy_command(
            8188,
            cpu=True,
            disable_custom_nodes=True,
            quick_test=True,
            main_path=core / "main.py",
            frontend_path=core / "frontend",
            database_url="sqlite:///:memory:",
        )
        try:
            completed = self._run(
                command,
                cwd=core,
                env=self.paths.server_environment(),
                text=True,
                capture_output=True,
                timeout=300,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise UpdateError(f"could not preflight staged Core: {error}") from error
        if completed.returncode:
            detail = (
                completed.stderr or completed.stdout or "unknown import failure"
            ).strip()
            raise UpdateError(f"staged Core import preflight failed: {detail}")

    def _activate(self, bundle: ValidatedBundle) -> UpdateResult:
        was_running = self.supervisor.is_running
        rollback_root = self.paths.state / "rollback"
        rollback_root.mkdir(parents=True, exist_ok=True)
        transaction_id = uuid.uuid4().hex
        transaction = self.paths.state / "transactions" / transaction_id
        transaction.mkdir(parents=True, exist_ok=False)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup = rollback_root / f"ComfyUI-{stamp}-{transaction_id[:8]}"
        failed = bundle.stage / "failed-ComfyUI"
        old_manifest = (
            self.paths.core_manifest.read_bytes()
            if self.paths.core_manifest.exists()
            else None
        )
        old_checksums_path = self.paths.manifest / "core-checksums.sha256"
        old_checksums = (
            old_checksums_path.read_bytes() if old_checksums_path.exists() else None
        )
        if old_manifest is not None:
            self._atomic_bytes(transaction / "core.json", old_manifest)
        if old_checksums is not None:
            self._atomic_bytes(transaction / "core-checksums.sha256", old_checksums)
        journal: dict[str, Any] = {
            "schema_version": 1,
            "transaction_id": transaction_id,
            "phase": "prepared",
            "backup": backup.relative_to(self.paths.root).as_posix(),
            "transaction": transaction.relative_to(self.paths.root).as_posix(),
            "had_core_manifest": old_manifest is not None,
            "had_core_checksums": old_checksums is not None,
        }
        self._write_journal(journal)
        self.supervisor.stop()
        activated = False
        try:
            if not self.paths.comfyui.is_dir():
                raise UpdateError(
                    f"installed Core directory is missing: {self.paths.comfyui}"
                )
            self.paths.comfyui.replace(backup)
            self._fsync_directory(self.paths.root)
            self._fsync_directory(backup.parent)
            journal["phase"] = "old_moved"
            self._write_journal(journal)
            bundle.core.replace(self.paths.comfyui)
            self._fsync_directory(self.paths.root)
            self._fsync_directory(bundle.stage)
            activated = True
            journal["phase"] = "candidate_active"
            self._write_journal(journal)
            self._atomic_write(
                self.paths.core_manifest,
                json.dumps(bundle.manifest, indent=2, sort_keys=True) + "\n",
            )
            self._atomic_write(old_checksums_path, bundle.checksums)
            self.supervisor.start()
            if not was_running:
                self.supervisor.stop()
            core = bundle.manifest["core"]
            self._prune_rollbacks(keep=2)
            self._clear_transaction(transaction)
            return UpdateResult(
                version=str(core.get("version") or core.get("tag") or "unknown"),
                commit=str(core.get("commit") or "unknown"),
                restarted=was_running,
            )
        except Exception as error:
            rollback_errors: list[str] = []
            try:
                self.supervisor.stop()
            except Exception as stop_error:
                rollback_errors.append(f"stop failed: {stop_error}")
            try:
                if activated and self.paths.comfyui.exists():
                    self.paths.comfyui.replace(failed)
                if backup.exists():
                    backup.replace(self.paths.comfyui)
                    self._fsync_directory(self.paths.root)
                    self._fsync_directory(backup.parent)
                self._restore_file(self.paths.core_manifest, old_manifest)
                self._restore_file(old_checksums_path, old_checksums)
                if was_running:
                    self.supervisor.start()
                self._clear_transaction(transaction)
            except Exception as rollback_error:
                rollback_errors.append(f"rollback failed: {rollback_error}")
            detail = f"Core update failed and was rolled back: {error}"
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
                "had_core_manifest",
                transaction / "core.json",
                paths.core_manifest,
            ),
            (
                "had_core_checksums",
                transaction / "core-checksums.sha256",
                paths.manifest / "core-checksums.sha256",
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

    @classmethod
    def _restore_file(cls, path: Path, content: bytes | None) -> None:
        if content is None:
            path.unlink(missing_ok=True)
            return
        cls._atomic_bytes(path, content)
