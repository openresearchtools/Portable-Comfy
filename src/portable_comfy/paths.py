"""Portable-root discovery, layout creation, and ComfyUI launch settings."""

from __future__ import annotations

import configparser
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence


class LayoutError(RuntimeError):
    """The portable tree is missing, unsafe, or internally inconsistent."""


PERSISTENT_DIRECTORIES = (
    "custom_nodes",
    "custom_node_runtime",
    "models",
    "input",
    "output",
    "temp",
    "workflows",
    "user",
    "logs",
    "config",
    "manifest",
    "state",
    "cache",
)


@dataclass(frozen=True, slots=True)
class PortablePaths:
    """Every filesystem path owned by one extracted Portable Comfy tree."""

    root: Path

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", self.root.expanduser().resolve())

    @classmethod
    def discover(
        cls,
        explicit: str | os.PathLike[str] | None = None,
        *,
        environ: Mapping[str, str] | None = None,
        cwd: str | os.PathLike[str] | None = None,
    ) -> "PortablePaths":
        """Find the root without depending on the process working directory."""

        env = os.environ if environ is None else environ
        if explicit is not None:
            return cls(Path(explicit))
        if env.get("PORTABLE_COMFY_ROOT"):
            return cls(Path(env["PORTABLE_COMFY_ROOT"]))
        # AppImage binaries execute from a temporary mount or extraction tree;
        # APPIMAGE retains the original file path beside the portable payload.
        if env.get("APPIMAGE"):
            return cls(Path(env["APPIMAGE"]).expanduser().resolve().parent)
        if getattr(sys, "frozen", False):
            raise LayoutError(
                "cannot locate the portable root: APPIMAGE and "
                "PORTABLE_COMFY_ROOT are both unset"
            )
        return cls(Path.cwd() if cwd is None else Path(cwd))

    @property
    def runtime(self) -> Path:
        """The runtime belonging to the active, replaceable ComfyUI generation."""

        return self.comfyui / "runtime"

    @property
    def python_prefix(self) -> Path:
        return self.runtime / "python"

    @property
    def custom_node_runtime(self) -> Path:
        """Persistent packages installed by custom nodes and their installers."""

        return self.root / "custom_node_runtime"

    @property
    def custom_node_site_packages(self) -> Path:
        candidates = sorted(self.custom_node_runtime.glob("lib/python*/site-packages"))
        if len(candidates) == 1:
            return candidates[0]
        # This is the location used by the old target-directory overlay.  It is
        # also a useful deterministic path before the venv has been created.
        return self.custom_node_runtime / "site-packages"

    @property
    def custom_node_bin(self) -> Path:
        return self.custom_node_runtime / "bin"

    @property
    def custom_node_python(self) -> Path:
        return self.custom_node_bin / "python"

    @property
    def python(self) -> Path:
        portable = self.python_prefix / "bin" / "python-portable"
        return portable if portable.exists() else self.python_prefix / "bin" / "python3"

    def python_executable(
        self, *, prefix: Path | None = None, require: bool = True
    ) -> Path:
        selected_prefix = self.python_prefix if prefix is None else prefix
        candidates = (
            selected_prefix / "bin" / "python-portable",
            selected_prefix / "bin" / "python3",
            selected_prefix / "bin" / "python3.13",
            selected_prefix / "bin" / "python",
        )
        for candidate in candidates:
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return candidate
        if require:
            raise LayoutError(
                "portable Python was not found; expected "
                f"{selected_prefix / 'bin/python-portable'}"
            )
        return candidates[0]

    @property
    def comfyui(self) -> Path:
        return self.root / "ComfyUI"

    @property
    def frontend(self) -> Path:
        return self.comfyui / "frontend"

    @property
    def logs(self) -> Path:
        return self.root / "logs"

    @property
    def state(self) -> Path:
        return self.root / "state"

    @property
    def config(self) -> Path:
        return self.root / "config"

    @property
    def manifest(self) -> Path:
        return self.root / "manifest"

    @property
    def workflows(self) -> Path:
        return self.root / "workflows"

    @property
    def workflow_link(self) -> Path:
        return self.root / "user" / "default" / "workflows"

    @property
    def extra_model_paths(self) -> Path:
        return self.config / "extra_model_paths.yaml"

    @property
    def database(self) -> Path:
        return self.root / "user" / "comfyui.db"

    @property
    def runtime_manifest(self) -> Path:
        """Compatibility alias for the active environment manifest."""

        return self.environment_manifest

    @property
    def environment_manifest(self) -> Path:
        return self.manifest / "environment.json"

    @property
    def environment_checksums(self) -> Path:
        return self.manifest / "environment-checksums.sha256"

    @property
    def core_manifest(self) -> Path:
        """Compatibility alias retained for older launcher integrations."""

        return self.environment_manifest

    @property
    def models_cache(self) -> Path:
        return self.root / "models" / ".cache"

    @property
    def manager_config(self) -> Path:
        return self.root / "user" / "__manager" / "config.ini"

    def create_layout(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        for relative in PERSISTENT_DIRECTORIES:
            (self.root / relative).mkdir(parents=True, exist_ok=True)
        (self.root / "user" / "default").mkdir(parents=True, exist_ok=True)
        self._ensure_workflow_link()
        self._ensure_node_runtime_manager_config()
        if not self.extra_model_paths.exists():
            self.extra_model_paths.write_text(
                "# Secondary source-tree configs; user models remain at the portable root.\n"
                "portable_comfy_core:\n"
                "  base_path: ../ComfyUI\n"
                "  configs: models/configs\n",
                encoding="utf-8",
            )

    def _ensure_node_runtime_manager_config(self) -> None:
        """Keep Manager installs in the interpreter that launched ComfyUI.

        The launcher uses the persistent custom-node venv. Manager's ordinary
        ``python -m pip`` operations therefore have normal install, upgrade and
        uninstall semantics there. Its alternate resolvers may select the
        replaceable base interpreter explicitly, so they remain disabled.
        Existing unrelated user configuration is preserved.
        """

        self.manager_config.parent.mkdir(parents=True, exist_ok=True)
        if not self.manager_config.exists():
            self.manager_config.write_text(
                "# Portable Comfy keeps custom-node packages in its persistent venv.\n"
                "[default]\n"
                "use_uv = false\n"
                "use_unified_resolver = false\n",
                encoding="utf-8",
            )
            return
        parser = configparser.ConfigParser(strict=False)
        try:
            parser.read(self.manager_config, encoding="utf-8")
            if not parser.has_section("default"):
                parser.add_section("default")
        except configparser.Error as error:
            raise LayoutError(
                f"cannot configure persistent Manager installs: {error}"
            ) from error
        needs_write = any(
            parser["default"].get(key, "").strip().lower() != "false"
            for key in ("use_uv", "use_unified_resolver")
        )
        if needs_write:
            parser["default"]["use_uv"] = "false"
            parser["default"]["use_unified_resolver"] = "false"
            temporary = self.manager_config.with_suffix(".tmp")
            with temporary.open("w", encoding="utf-8") as stream:
                parser.write(stream)
            temporary.replace(self.manager_config)

    def repair_runtime_metadata(self, python_prefix: Path | None = None) -> int:
        """Repair text metadata and pip scripts after the complete tree moves.

        CPython and libpython use relative RPATHs.  The remaining movable pieces
        are text: sysconfig/build metadata and console-script shebangs generated
        later by ComfyUI Manager or custom-node installers.
        """

        prefix = (
            self.python_prefix if python_prefix is None else python_prefix
        ).resolve()
        if not prefix.is_dir():
            return 0
        stamp = prefix / ".portable-comfy-prefix"
        stamp_text: str | None = None
        try:
            stamp_text = stamp.read_text(encoding="utf-8").strip()
            previous = Path(stamp_text).resolve()
        except (FileNotFoundError, OSError, ValueError):
            previous = prefix
        changed = 0
        bin_dir = prefix / "bin"
        if bin_dir.is_dir():
            for path in sorted(bin_dir.iterdir()):
                if (
                    not path.is_file()
                    or path.is_symlink()
                    or path.name.startswith("python")
                ):
                    continue
                try:
                    data = path.read_bytes()
                except OSError:
                    continue
                first, separator, rest = data.partition(b"\n")
                if (
                    not separator
                    or not first.startswith(b"#!")
                    or b"python" not in first.lower()
                ):
                    continue
                header = (
                    b"#!/bin/sh\n"
                    b'\'\'\'exec\' "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)/python3" '
                    b'-s "$0" "$@"\n'
                    b"' '''\n"
                )
                path.write_bytes(header + rest)
                path.chmod(path.stat().st_mode | 0o111)
                changed += 1
        if previous != prefix:
            old = os.fsencode(str(previous))
            new = os.fsencode(str(prefix))
            candidates = list((prefix / "lib" / "pkgconfig").glob("*.pc"))
            candidates.extend((prefix / "lib").glob("python*/config-*/Makefile"))
            candidates.extend((prefix / "lib").glob("python*/_sysconfigdata_*.py"))
            candidates.extend(bin_dir.glob("python*-config"))
            for path in candidates:
                if not path.is_file() or path.is_symlink():
                    continue
                try:
                    data = path.read_bytes()
                except OSError:
                    continue
                if old not in data:
                    continue
                path.write_bytes(data.replace(old, new))
                changed += 1
        temporary = stamp.with_suffix(".tmp")
        temporary.write_text(str(prefix) + "\n", encoding="utf-8")
        temporary.replace(stamp)
        if stamp_text != str(prefix):
            changed += 1
        if prefix == self.python_prefix.resolve():
            changed += self._repair_node_runtime_metadata()
        return changed

    def ensure_node_runtime(self, python_prefix: Path | None = None) -> int:
        """Create, rebind, and validate the persistent custom-node venv.

        Creation needs no network and deliberately omits a private pip copy.
        The venv inherits pip, Torch, and Core dependencies from the active
        replaceable runtime through ``--system-site-packages``. Packages later
        installed by nodes live in the venv and retain normal pip metadata.
        """

        prefix = (
            self.python_prefix if python_prefix is None else python_prefix
        ).resolve()
        base_python = self.python_executable(prefix=prefix)
        version = self._query_base_python_version(base_python, prefix)
        changed = 0
        configuration = self.custom_node_runtime / "pyvenv.cfg"
        if configuration.is_file():
            configured = self._read_pyvenv_configuration(configuration)
            configured_version = configured.get("version", "")
            if self._python_abi(configured_version) != self._python_abi(version):
                if self.node_runtime_has_packages():
                    raise LayoutError(
                        "the active Python baseline changed from "
                        f"{configured_version or 'unknown'} to {version}, but the "
                        "persistent custom-node venv contains packages; install a "
                        "compatible environment or explicitly rebuild that venv"
                    )
                self._replace_node_runtime(base_python, prefix)
                changed += 1
        else:
            venv_sites = sorted(
                self.custom_node_runtime.glob("lib/python*/site-packages")
            )
            if venv_sites and any(
                any(site_packages.iterdir()) for site_packages in venv_sites
            ):
                raise LayoutError(
                    "persistent custom-node venv metadata is missing, but its "
                    "site-packages contains data; refusing to rebuild it implicitly"
                )
            self._replace_node_runtime(base_python, prefix)
            changed += 1
        changed += self._repair_node_runtime_metadata(prefix, version=version)
        self._validate_node_runtime(prefix)
        return changed

    def node_runtime_has_packages(self) -> bool:
        """Return whether the persistent venv contains node-installed data."""

        site_packages = self.custom_node_site_packages
        if not site_packages.is_dir():
            return False
        ignored = {"__pycache__", ".DS_Store"}
        return any(path.name not in ignored for path in site_packages.iterdir())

    def _query_base_python_version(self, python: Path, prefix: Path) -> str:
        environment = self.server_environment(
            python_prefix=prefix, include_node_runtime=False
        )
        # python-portable establishes its own PYTHONHOME. Omitting the inherited
        # value also lets test/development runtimes use a symlinked interpreter.
        environment.pop("PYTHONHOME", None)
        try:
            completed = subprocess.run(
                [
                    str(python),
                    "-s",
                    "-c",
                    "import platform;print(platform.python_version())",
                ],
                env=environment,
                text=True,
                capture_output=True,
                timeout=60,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise LayoutError(f"cannot inspect portable Python: {error}") from error
        version = completed.stdout.strip().splitlines()
        if completed.returncode or not version:
            detail = (
                completed.stderr or completed.stdout or "no version output"
            ).strip()
            raise LayoutError(f"portable Python could not initialize: {detail}")
        return version[-1]

    @staticmethod
    def _python_abi(version: str) -> tuple[str, str]:
        parts = version.strip().split(".")
        return tuple((parts + [""])[:2])  # type: ignore[return-value]

    @staticmethod
    def _read_pyvenv_configuration(path: Path) -> dict[str, str]:
        result: dict[str, str] = {}
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                key, separator, value = line.partition("=")
                if separator:
                    result[key.strip().lower()] = value.strip()
        except (OSError, UnicodeError) as error:
            raise LayoutError(
                f"cannot read persistent venv metadata: {error}"
            ) from error
        return result

    def _replace_node_runtime(self, base_python: Path, prefix: Path) -> None:
        """Create a new venv and atomically migrate the former overlay."""

        self.root.mkdir(parents=True, exist_ok=True)
        temporary = Path(
            tempfile.mkdtemp(prefix=".custom-node-runtime-", dir=self.root)
        )
        backup: Path | None = None
        environment = self.server_environment(
            python_prefix=prefix, include_node_runtime=False
        )
        environment.pop("PYTHONHOME", None)
        try:
            completed = subprocess.run(
                [
                    str(base_python),
                    "-s",
                    "-m",
                    "venv",
                    "--system-site-packages",
                    "--without-pip",
                    str(temporary),
                ],
                env=environment,
                text=True,
                capture_output=True,
                timeout=180,
                check=False,
            )
            if completed.returncode:
                detail = (
                    completed.stderr or completed.stdout or "unknown venv failure"
                ).strip()
                raise LayoutError(
                    f"cannot create persistent custom-node venv: {detail}"
                )
            staged_sites = sorted(temporary.glob("lib/python*/site-packages"))
            if len(staged_sites) != 1:
                raise LayoutError(
                    "new custom-node venv did not contain one site-packages directory"
                )
            if self.custom_node_runtime.exists():
                self._copy_legacy_node_runtime(temporary, staged_sites[0])
                backup = Path(
                    tempfile.mkdtemp(prefix=".custom-node-runtime-old-", dir=self.root)
                )
                backup.rmdir()
                self.custom_node_runtime.replace(backup)
            temporary.replace(self.custom_node_runtime)
            if backup is not None:
                shutil.rmtree(backup, ignore_errors=True)
        except Exception:
            if (
                backup is not None
                and backup.exists()
                and not self.custom_node_runtime.exists()
            ):
                backup.replace(self.custom_node_runtime)
            raise
        finally:
            shutil.rmtree(temporary, ignore_errors=True)

    def _copy_legacy_node_runtime(
        self, staged_runtime: Path, staged_site_packages: Path
    ) -> None:
        old = self.custom_node_runtime
        old_site = old / "site-packages"
        if old_site.is_dir():
            for source in old_site.iterdir():
                self._copy_node_runtime_entry(
                    source, staged_site_packages / source.name
                )
        old_bin = old / "bin"
        if old_bin.is_dir():
            for source in old_bin.iterdir():
                if source.name.startswith("python") or source.name.lower().startswith(
                    "activate"
                ):
                    continue
                self._copy_node_runtime_entry(
                    source, staged_runtime / "bin" / source.name
                )
        known = {
            "site-packages",
            "bin",
            ".portable-comfy-root",
            ".gitignore",
            "pyvenv.cfg",
            "lib",
            "lib64",
            "include",
        }
        for source in old.iterdir():
            if source.name in known:
                continue
            self._copy_node_runtime_entry(source, staged_runtime / source.name)

    @staticmethod
    def _copy_node_runtime_entry(source: Path, destination: Path) -> None:
        if destination.exists() or destination.is_symlink():
            raise LayoutError(
                f"cannot migrate persistent node runtime; duplicate {destination.name}"
            )
        if source.is_symlink():
            destination.symlink_to(
                os.readlink(source), target_is_directory=source.is_dir()
            )
        elif source.is_dir():
            shutil.copytree(source, destination, symlinks=True)
        else:
            shutil.copy2(source, destination)

    def _repair_node_runtime_metadata(
        self, python_prefix: Path | None = None, *, version: str | None = None
    ) -> int:
        """Rebind venv metadata and entry points after the portable tree moves."""

        self.custom_node_runtime.mkdir(parents=True, exist_ok=True)
        configuration = self.custom_node_runtime / "pyvenv.cfg"
        if not configuration.is_file():
            return 0
        prefix = (
            self.python_prefix if python_prefix is None else python_prefix
        ).resolve()
        configured = self._read_pyvenv_configuration(configuration)
        selected_version = version or configured.get("version", "")
        if not selected_version:
            return 0
        stamp = self.custom_node_runtime / ".portable-comfy-root"
        try:
            previous = Path(stamp.read_text(encoding="utf-8").strip()).resolve()
        except (FileNotFoundError, OSError, ValueError):
            previous = self.root
        changed = 0
        base_binary = prefix / "bin" / "python3"
        if not base_binary.exists():
            base_binary = self.python_executable(prefix=prefix)
        for name in (
            "python",
            "python3",
            f"python{self._python_abi(selected_version)[0]}.{self._python_abi(selected_version)[1]}",
        ):
            path = self.custom_node_bin / name
            target = os.path.relpath(base_binary, self.custom_node_bin)
            if path.is_symlink() and os.readlink(path) == target:
                continue
            if path.exists() or path.is_symlink():
                path.unlink()
            path.symlink_to(target)
            changed += 1
        bin_locations = (
            (self.custom_node_bin, "python"),
            (self.custom_node_site_packages / "bin", "../../../../bin/python"),
        )
        for bin_dir, python_relative in bin_locations:
            if not bin_dir.is_dir():
                continue
            header = (
                b"#!/bin/sh\n"
                + b"""\'\'\'exec\' "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)/"""
                + python_relative.encode()
                + b"""" -s "$0" "$@"\n"""
                + b"' '''\n"
            )
            for path in sorted(bin_dir.iterdir()):
                if not path.is_file() or path.is_symlink():
                    continue
                try:
                    data = path.read_bytes()
                except OSError:
                    continue
                first, separator, rest = data.partition(b"\n")
                if (
                    not separator
                    or not first.startswith(b"#!")
                    or b"python" not in first.lower()
                ):
                    continue
                path.write_bytes(header + rest)
                path.chmod(path.stat().st_mode | 0o111)
                changed += 1
        if previous != self.root:
            old, new = os.fsencode(str(previous)), os.fsencode(str(self.root))
            candidates = list(self.custom_node_site_packages.glob("*.pth"))
            candidates.extend(self.custom_node_site_packages.glob("*.egg-link"))
            for path in candidates:
                if not path.is_file() or path.is_symlink():
                    continue
                try:
                    data = path.read_bytes()
                except OSError:
                    continue
                if old in data:
                    path.write_bytes(data.replace(old, new))
                    changed += 1
        cfg_text = (
            f"home = {prefix / 'bin'}\n"
            "include-system-site-packages = true\n"
            f"version = {selected_version}\n"
            f"executable = {base_binary}\n"
            f"command = {base_binary} -m venv --system-site-packages --without-pip {self.custom_node_runtime}\n"
        )
        if configuration.read_text(encoding="utf-8") != cfg_text:
            temporary_cfg = configuration.with_suffix(".tmp")
            temporary_cfg.write_text(cfg_text, encoding="utf-8")
            temporary_cfg.replace(configuration)
            changed += 1
        temporary = stamp.with_suffix(".tmp")
        temporary.write_text(str(self.root) + "\n", encoding="utf-8")
        temporary.replace(stamp)
        if previous != self.root:
            changed += 1
        return changed

    def _validate_node_runtime(self, prefix: Path) -> None:
        environment = self.server_environment(
            python_prefix=prefix, include_node_runtime=True
        )
        script = (
            "import pathlib,sys,pip; "
            "expected=pathlib.Path(sys.argv[1]).resolve(); "
            "actual=pathlib.Path(sys.prefix).resolve(); "
            "assert actual == expected,(actual,expected); "
            "assert sys.prefix != sys.base_prefix"
        )
        try:
            completed = subprocess.run(
                [
                    str(self.custom_node_python),
                    "-s",
                    "-c",
                    script,
                    str(self.custom_node_runtime),
                ],
                env=environment,
                text=True,
                capture_output=True,
                timeout=60,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise LayoutError(
                f"cannot start persistent custom-node venv: {error}"
            ) from error
        if completed.returncode:
            detail = (
                completed.stderr or completed.stdout or "unknown venv error"
            ).strip()
            raise LayoutError(f"persistent custom-node venv is unusable: {detail}")

    def _ensure_workflow_link(self) -> None:
        link = self.workflow_link
        expected_text = "../../workflows"
        if link.is_symlink():
            try:
                target = (link.parent / os.readlink(link)).resolve(strict=False)
            except OSError as error:
                raise LayoutError(f"cannot inspect workflow link: {error}") from error
            if target != self.workflows.resolve(strict=False):
                raise LayoutError(
                    f"{link} points outside the portable workflows directory; "
                    "refusing to replace it"
                )
            if os.readlink(link) != expected_text:
                link.unlink()
                link.symlink_to(expected_text, target_is_directory=True)
            return
        if link.exists():
            raise LayoutError(
                f"{link} already exists and is not the managed symlink; "
                "move its contents into the top-level workflows directory"
            )
        link.symlink_to(expected_text, target_is_directory=True)

    def validate_core(self) -> None:
        required = (self.comfyui / "main.py", self.frontend / "index.html")
        missing = [str(path) for path in required if not path.is_file()]
        if missing:
            raise LayoutError(
                "Core/frontend is incomplete; missing: " + ", ".join(missing)
            )

    def validate_runtime(self) -> None:
        self.python_executable()
        self.validate_core()
        if not self.environment_manifest.is_file():
            raise LayoutError(
                f"environment manifest is missing: {self.environment_manifest}"
            )

    def comfy_command(
        self,
        port: int = 8188,
        *,
        host: str = "127.0.0.1",
        cpu: bool = False,
        disable_custom_nodes: bool = False,
        quick_test: bool = False,
        comfyui_path: Path | None = None,
        python_prefix: Path | None = None,
        main_path: Path | None = None,
        frontend_path: Path | None = None,
        database_url: str | None = None,
        include_extra_model_paths: bool = True,
        base_directory: Path | None = None,
        user_directory: Path | None = None,
        temp_directory: Path | None = None,
        extra_args: Sequence[str] = (),
        use_node_runtime: bool = True,
        validate: bool = False,
    ) -> list[str]:
        """Build the shell-free command used for normal and staged Core runs."""

        if not 1 <= int(port) <= 65535:
            raise ValueError("port must be between 1 and 65535")
        if validate:
            self.validate_runtime()
        selected_comfyui = self.comfyui if comfyui_path is None else comfyui_path
        selected_prefix = (
            selected_comfyui / "runtime" / "python"
            if python_prefix is None and comfyui_path is not None
            else self.python_prefix
            if python_prefix is None
            else python_prefix
        )
        main = selected_comfyui / "main.py" if main_path is None else main_path
        frontend = (
            selected_comfyui / "frontend" if frontend_path is None else frontend_path
        )
        database = database_url or f"sqlite:///{self.database}"
        selected_base = self.root if base_directory is None else base_directory
        selected_user = self.root / "user" if user_directory is None else user_directory
        interpreter = (
            self.custom_node_python
            if use_node_runtime
            else self.python_executable(prefix=selected_prefix, require=validate)
        )
        if (
            validate
            and use_node_runtime
            and not (interpreter.is_file() and os.access(interpreter, os.X_OK))
        ):
            raise LayoutError(
                "persistent custom-node venv is missing; initialize it before launch"
            )
        command = [
            str(interpreter),
            "-s",
            str(main),
            "--listen",
            host,
            "--port",
            str(port),
            "--base-directory",
            str(selected_base),
            "--user-directory",
            str(selected_user),
            "--database-url",
            database,
            "--front-end-root",
            str(frontend),
            "--disable-auto-launch",
            "--enable-manager",
            "--log-stdout",
        ]
        if include_extra_model_paths:
            command.extend(["--extra-model-paths-config", str(self.extra_model_paths)])
        if temp_directory is not None:
            command.extend(["--temp-directory", str(temp_directory)])
        if cpu:
            command.append("--cpu")
        if disable_custom_nodes:
            command.append("--disable-all-custom-nodes")
        if quick_test:
            command.append("--quick-test-for-ci")
        command.extend(map(str, extra_args))
        return command

    def server_environment(
        self,
        inherited: Mapping[str, str] | None = None,
        *,
        python_prefix: Path | None = None,
        comfyui_path: Path | None = None,
        include_node_runtime: bool = True,
        cache_root: Path | None = None,
    ) -> dict[str, str]:
        """Return an isolated environment for the external portable interpreter."""

        source = os.environ if inherited is None else inherited
        env = dict(source)
        for key in (
            "PYTHONPATH",
            "PYTHONHOME",
            "PYTHONUSERBASE",
            "VIRTUAL_ENV",
            "CONDA_PREFIX",
            "CONDA_DEFAULT_ENV",
            "PIP_TARGET",
            "PIP_PREFIX",
            "PIP_USER",
            "PORTABLE_COMFY_NODE_SITE_PACKAGES",
            "UV_PROJECT_ENVIRONMENT",
            "UV_PYTHON",
        ):
            env.pop(key, None)
        original_ld = env.pop("LD_LIBRARY_PATH_ORIG", None)
        if original_ld is None and getattr(sys, "frozen", False):
            env.pop("LD_LIBRARY_PATH", None)
            original_ld = ""
        elif original_ld is None:
            original_ld = env.pop("LD_LIBRARY_PATH", "")
        else:
            env.pop("LD_LIBRARY_PATH", None)
        selected_comfyui = self.comfyui if comfyui_path is None else comfyui_path
        selected_prefix = (
            selected_comfyui / "runtime" / "python"
            if python_prefix is None and comfyui_path is not None
            else self.python_prefix
            if python_prefix is None
            else python_prefix
        )
        library_path = str(selected_prefix / "lib")
        if original_ld:
            library_path += os.pathsep + original_ld
        selected_cache = self.root / "cache" if cache_root is None else cache_root
        env.update(
            {
                "PYTHONNOUSERSITE": "1",
                "PYTHONDONTWRITEBYTECODE": "1",
                "PATH": f"{selected_prefix / 'bin'}{os.pathsep}{env.get('PATH', '')}",
                "LD_LIBRARY_PATH": library_path,
                "PORTABLE_COMFY_ROOT": str(self.root),
                "COMFYUI_PATH": str(selected_comfyui),
                "CM_USE_PYGIT2": "1",
                "XDG_CACHE_HOME": str(selected_cache / "xdg"),
                "XDG_CONFIG_HOME": str(self.config),
                "HF_HOME": str(
                    self.models_cache / "huggingface"
                    if cache_root is None
                    else selected_cache / "huggingface"
                ),
                "TORCH_HOME": str(
                    self.models_cache / "torch"
                    if cache_root is None
                    else selected_cache / "torch"
                ),
                "UV_CACHE_DIR": str(selected_cache / "uv"),
            }
        )
        if include_node_runtime:
            env["VIRTUAL_ENV"] = str(self.custom_node_runtime)
            env["PATH"] = f"{self.custom_node_bin}{os.pathsep}{env['PATH']}"
            # Manager's alternate resolvers are disabled because they may
            # explicitly select the replaceable base interpreter. Keep direct
            # uv calls pointed at the persistent venv as an additional guard.
            env["UV_PROJECT_ENVIRONMENT"] = str(self.custom_node_runtime)
            env["UV_PYTHON"] = str(self.custom_node_python)
        else:
            # A standalone base runtime needs PYTHONHOME. It must never leak
            # into normal venv launches, where it would bypass the venv prefix.
            env["PYTHONHOME"] = str(selected_prefix)
        return env

    def portable_environment(
        self, base: Mapping[str, str] | None = None
    ) -> dict[str, str]:
        return self.server_environment(base)
