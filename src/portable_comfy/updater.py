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
GENERATION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,191}$")
ARCHIVE_ROOT = re.compile(r"^Portable-Comfy-environment-[A-Za-z0-9._+-]+$")
MAX_MEMBERS = 500_000
MAX_UNPACKED_BYTES = 32 * 1024**3
TRANSACTION_MARKER = "environment-update-transaction.json"
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
    """An environment update could not be completed safely."""


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
                "an interrupted environment update left neither an active generation "
                f"nor its rollback; inspect {transaction}"
            )
        marker.unlink(missing_ok=True)
        cls._fsync_directory(marker.parent)
        shutil.rmtree(transaction, ignore_errors=True)
        return restored

    def install_bundle(self, archive: str | os.PathLike[str]) -> UpdateResult:
        """Validate, preflight with candidate Python, swap, and health-check."""

        source = Path(archive).expanduser().resolve()
        if not source.is_file():
            raise UpdateError(f"environment bundle does not exist: {source}")
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
                bundle = self._extract_and_validate(source, stage)
                self._check_node_overlay_abi(bundle.manifest["runtime"])
                self._preflight(bundle)
                return self._activate(bundle)
            finally:
                shutil.rmtree(stage, ignore_errors=True)

    def _check_node_overlay_abi(self, candidate: dict[str, Any]) -> None:
        """Do not silently carry compiled node packages across runtime ABIs."""

        overlay_entries = (
            path
            for path in self.paths.custom_node_runtime.rglob("*")
            if path.name != ".portable-comfy-root"
            and (path.is_file() or path.is_symlink())
        )
        if next(overlay_entries, None) is None:
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
                f"({', '.join(changed)}), but custom_node_runtime contains packages; "
                "move or rebuild that overlay before installing this environment"
            )

    def _extract_and_validate(self, archive: Path, stage: Path) -> ValidatedBundle:
        try:
            opened = tarfile.open(archive, mode="r:gz")
        except (OSError, tarfile.TarError) as error:
            raise BundleValidationError(
                f"cannot open gzip environment bundle: {error}"
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
                    "bundle root must be Portable-Comfy-environment-<version>"
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
        self._validate_manifest(bundle_root, manifest, checksum_text)
        return ValidatedBundle(
            root=bundle_root, manifest=manifest, checksums=checksum_text
        )

    @staticmethod
    def _validate_manifest(root: Path, manifest: Any, checksum_text: str) -> None:
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

    def _preflight(self, bundle: ValidatedBundle) -> None:
        candidate = bundle.environment
        candidate_prefix = candidate / "runtime" / "python"
        scratch = bundle.root / "preflight"
        scratch.mkdir()
        self.paths.repair_runtime_metadata(candidate_prefix)
        python = self.paths.python_executable(prefix=candidate_prefix)
        environment = self.paths.server_environment(
            python_prefix=candidate_prefix,
            comfyui_path=candidate,
            include_node_overlay=False,
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
                entries.append(
                    {"path": path_text, "type": "symlink", "target": target}
                )
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
        manifest, checksums = cls._seal_environment(
            paths.comfyui, delivery_manifest
        )
        cls._atomic_write(paths.environment_checksums, checksums)
        cls._atomic_write(
            paths.environment_manifest,
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        )

    def _activate(self, bundle: ValidatedBundle) -> UpdateResult:
        was_running = self.supervisor.is_running
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
            "had_environment_manifest": old_manifest is not None,
            "had_environment_checksums": old_checksums is not None,
        }
        self._write_journal(journal)
        self.supervisor.stop()
        activated = False
        try:
            if not self.paths.comfyui.is_dir():
                raise UpdateError(
                    f"installed environment directory is missing: {self.paths.comfyui}"
                )
            self.paths.comfyui.replace(backup)
            self._fsync_directory(self.paths.root)
            self._fsync_directory(backup.parent)
            journal["phase"] = "old_moved"
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
            self._atomic_write(
                self.paths.environment_checksums, installed_checksums
            )
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
            detail = f"environment update failed and was rolled back: {error}"
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
