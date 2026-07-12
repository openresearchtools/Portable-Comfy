"""Portable-root discovery, layout creation, and ComfyUI launch settings."""

from __future__ import annotations

import os
import sys
import configparser
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence


class LayoutError(RuntimeError):
    """The portable tree is missing, unsafe, or internally inconsistent."""


PERSISTENT_DIRECTORIES = (
    "custom_nodes",
    "custom_node_runtime/site-packages",
    "custom_node_runtime/bin",
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
        return self.custom_node_runtime / "site-packages"

    @property
    def custom_node_bin(self) -> Path:
        return self.custom_node_runtime / "bin"

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
        self._ensure_node_overlay_manager_config()
        if not self.extra_model_paths.exists():
            self.extra_model_paths.write_text(
                "# Secondary source-tree configs; user models remain at the portable root.\n"
                "portable_comfy_core:\n"
                "  base_path: ../ComfyUI\n"
                "  configs: models/configs\n",
                encoding="utf-8",
            )

    def _ensure_node_overlay_manager_config(self) -> None:
        """Make Manager's default installer honour pip's persistent target.

        Manager's uv mode explicitly selects the active interpreter and would
        therefore write inside the replaceable environment. Normal pip honours
        ``PIP_TARGET``. Existing user configuration is never overwritten.
        """

        self.manager_config.parent.mkdir(parents=True, exist_ok=True)
        if not self.manager_config.exists():
            self.manager_config.write_text(
                "# Portable Comfy keeps custom-node packages outside ComfyUI/.\n"
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
        try:
            previous = Path(stamp.read_text(encoding="utf-8").strip()).resolve()
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
        if prefix == self.python_prefix.resolve():
            changed += self._repair_node_overlay_metadata()
        return changed

    def _repair_node_overlay_metadata(self) -> int:
        """Relocate node-installed entry points and editable path records."""

        self.custom_node_runtime.mkdir(parents=True, exist_ok=True)
        stamp = self.custom_node_runtime / ".portable-comfy-root"
        try:
            previous = Path(stamp.read_text(encoding="utf-8").strip()).resolve()
        except (FileNotFoundError, OSError, ValueError):
            previous = self.root
        changed = 0
        bin_locations = (
            (self.custom_node_bin, "../.."),
            (self.custom_node_site_packages / "bin", "../../.."),
        )
        for bin_dir, root_relative in bin_locations:
            if not bin_dir.is_dir():
                continue
            header = (
                b"#!/bin/sh\n"
                + b"'''exec' \"$(CDPATH= cd -- \"$(dirname -- \"$0\")/"
                + root_relative.encode()
                + b'" && pwd -P)/ComfyUI/runtime/python/bin/python3" -s "$0" "$@"\n'
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
        temporary = stamp.with_suffix(".tmp")
        temporary.write_text(str(self.root) + "\n", encoding="utf-8")
        temporary.replace(stamp)
        return changed

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
        command = [
            str(self.python_executable(prefix=selected_prefix, require=validate)),
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
        include_node_overlay: bool = True,
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
                "PYTHONHOME": str(selected_prefix),
                "PYTHONNOUSERSITE": "1",
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
        if include_node_overlay:
            node_paths = (self.custom_node_bin, self.custom_node_site_packages / "bin")
            env["PATH"] = (
                os.pathsep.join(map(str, node_paths)) + os.pathsep + env["PATH"]
            )
            # pip honours PIP_TARGET for Manager's normal `python -m pip`
            # installs. PYTHONPATH makes that target importable ahead of the
            # replaceable base packages without modifying the base generation.
            env["PYTHONPATH"] = str(self.custom_node_site_packages)
            env["PYTHONUSERBASE"] = str(self.custom_node_runtime)
            env["PIP_TARGET"] = str(self.custom_node_site_packages)
            env["PORTABLE_COMFY_NODE_SITE_PACKAGES"] = str(
                self.custom_node_site_packages
            )
        return env

    def portable_environment(
        self, base: Mapping[str, str] | None = None
    ) -> dict[str, str]:
        return self.server_environment(base)
