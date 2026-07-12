from __future__ import annotations

import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from portable_comfy.paths import LayoutError, PortablePaths


def _write_probe_wheel(directory: Path, version: str) -> Path:
    wheel = directory / f"portable_base_probe-{version}-py3-none-any.whl"
    dist_info = f"portable_base_probe-{version}.dist-info"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("portable_base_probe.py", f"VERSION = {version!r}\n")
        archive.writestr(
            f"{dist_info}/METADATA",
            f"Metadata-Version: 2.1\nName: portable-base-probe\nVersion: {version}\n",
        )
        archive.writestr(
            f"{dist_info}/WHEEL",
            "Wheel-Version: 1.0\n"
            "Generator: portable-comfy-test\n"
            "Root-Is-Purelib: true\n"
            "Tag: py3-none-any\n",
        )
        archive.writestr(
            f"{dist_info}/RECORD",
            "portable_base_probe.py,,\n"
            f"{dist_info}/METADATA,,\n"
            f"{dist_info}/WHEEL,,\n"
            f"{dist_info}/RECORD,,\n",
        )
    return wheel


def test_discover_priority_and_appimage(tmp_path: Path) -> None:
    explicit = PortablePaths.discover(
        tmp_path / "explicit", environ={"APPIMAGE": "/x/y"}
    )
    assert explicit.root == (tmp_path / "explicit").resolve()
    env = PortablePaths.discover(environ={"PORTABLE_COMFY_ROOT": str(tmp_path / "env")})
    assert env.root == (tmp_path / "env").resolve()
    image = PortablePaths.discover(
        environ={"APPIMAGE": str(tmp_path / "bundle/App.AppImage")}
    )
    assert image.root == (tmp_path / "bundle").resolve()
    source = PortablePaths.discover(environ={}, cwd=tmp_path / "source")
    assert source.root == (tmp_path / "source").resolve()


def test_frozen_discovery_requires_anchor(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    with pytest.raises(LayoutError, match="APPIMAGE"):
        PortablePaths.discover(environ={})


def test_create_layout_and_workflow_link(tmp_path: Path) -> None:
    paths = PortablePaths(tmp_path / "portable")
    paths.create_layout()
    assert paths.workflows.is_dir()
    assert paths.workflow_link.is_symlink()
    assert os.readlink(paths.workflow_link) == "../../workflows"
    assert paths.workflow_link.resolve() == paths.workflows.resolve()
    config = paths.extra_model_paths.read_text(encoding="utf-8")
    assert "base_path: ../ComfyUI" in config
    manager = paths.manager_config.read_text(encoding="utf-8")
    assert "use_uv = false" in manager
    assert "use_unified_resolver = false" in manager
    paths.create_layout()  # idempotent


def test_layout_forces_manager_away_from_nonpersistent_uv_installs(
    tmp_path: Path,
) -> None:
    paths = PortablePaths(tmp_path / "portable")
    paths.manager_config.parent.mkdir(parents=True)
    paths.manager_config.write_text(
        "[default]\nuse_uv = true\nuse_unified_resolver = true\nchannel_url = keep-me\n",
        encoding="utf-8",
    )
    paths.create_layout()
    value = paths.manager_config.read_text(encoding="utf-8")
    assert "use_uv = false" in value
    assert "use_unified_resolver = false" in value
    assert "channel_url = keep-me" in value


def test_layout_refuses_wrong_link_or_real_directory(tmp_path: Path) -> None:
    wrong = PortablePaths(tmp_path / "wrong")
    (wrong.root / "user/default").mkdir(parents=True)
    (wrong.root / "workflows").mkdir()
    wrong.workflow_link.symlink_to("../../../outside", target_is_directory=True)
    with pytest.raises(LayoutError, match="outside"):
        wrong.create_layout()

    real = PortablePaths(tmp_path / "real")
    (real.root / "user/default/workflows").mkdir(parents=True)
    with pytest.raises(LayoutError, match="not the managed symlink"):
        real.create_layout()


def test_command_keeps_all_state_in_root(portable_root: PortablePaths) -> None:
    command = portable_root.comfy_command(45678, validate=True)
    assert command[:2] == [str(portable_root.custom_node_python), "-s"]
    assert command[command.index("--listen") + 1] == "127.0.0.1"
    assert command[command.index("--base-directory") + 1] == str(portable_root.root)
    assert command[command.index("--user-directory") + 1] == str(
        portable_root.root / "user"
    )
    assert command[command.index("--front-end-root") + 1] == str(portable_root.frontend)
    assert (
        command[command.index("--database-url") + 1]
        == f"sqlite:///{portable_root.database}"
    )
    assert "--temp-directory" not in command  # avoids ComfyUI's temp/temp behavior
    assert "--enable-manager" in command
    assert "--log-stdout" in command


def test_environment_discards_pyinstaller_and_virtualenv_paths(
    portable_root: PortablePaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    env = portable_root.server_environment(
        {
            "PATH": "/usr/bin",
            "PYTHONPATH": "/bad",
            "VIRTUAL_ENV": "/bad-venv",
            "LD_LIBRARY_PATH": "/pyinstaller",
            "LD_LIBRARY_PATH_ORIG": "/host/lib",
        }
    )
    assert "PYTHONPATH" not in env
    assert env["VIRTUAL_ENV"] == str(portable_root.custom_node_runtime)
    assert "PYTHONHOME" not in env
    assert env["LD_LIBRARY_PATH"] == f"{portable_root.python_prefix / 'lib'}:/host/lib"
    assert "/pyinstaller" not in env["LD_LIBRARY_PATH"]
    assert env["CM_USE_PYGIT2"] == "1"
    assert "PIP_TARGET" not in env
    assert str(portable_root.custom_node_bin) in env["PATH"]
    assert env["PYTHONDONTWRITEBYTECODE"] == "1"


def test_candidate_environment_can_exclude_persistent_node_runtime(
    portable_root: PortablePaths,
) -> None:
    candidate = portable_root.state / "candidate/ComfyUI"
    prefix = candidate / "runtime/python"
    env = portable_root.server_environment(
        python_prefix=prefix,
        comfyui_path=candidate,
        include_node_runtime=False,
    )
    assert "PYTHONPATH" not in env
    assert "PIP_TARGET" not in env
    assert env["PYTHONHOME"] == str(prefix)
    assert env["COMFYUI_PATH"] == str(candidate)


def test_server_environment_prevents_environment_bytecode_writes(
    portable_root: PortablePaths,
) -> None:
    module = portable_root.comfyui / "bytecode_probe.py"
    module.write_text("VALUE = 42\n", encoding="utf-8")
    completed = subprocess.run(
        [
            str(portable_root.custom_node_python),
            "-c",
            "import bytecode_probe; assert bytecode_probe.VALUE == 42",
        ],
        cwd=portable_root.comfyui,
        env=portable_root.server_environment(),
        check=False,
    )
    assert completed.returncode == 0
    assert not (portable_root.comfyui / "__pycache__").exists()


def test_node_runtime_is_unseeded_system_site_packages_venv(
    portable_root: PortablePaths,
) -> None:
    configuration = (portable_root.custom_node_runtime / "pyvenv.cfg").read_text(
        encoding="utf-8"
    )
    assert "include-system-site-packages = true" in configuration
    assert not list(portable_root.custom_node_site_packages.glob("pip-*.dist-info"))
    completed = subprocess.run(
        [
            str(portable_root.custom_node_python),
            "-s",
            "-c",
            "import pathlib,pip,sys; print(pathlib.Path(pip.__file__).resolve()); "
            "assert sys.prefix != sys.base_prefix",
        ],
        env=portable_root.server_environment(),
        text=True,
        capture_output=True,
        check=True,
    )
    assert not Path(completed.stdout.strip()).is_relative_to(
        portable_root.custom_node_runtime
    )


def test_legacy_target_directory_is_migrated_into_node_venv(
    tmp_path: Path,
) -> None:
    paths = PortablePaths(tmp_path / "legacy portable")
    paths.create_layout()
    (paths.python_prefix / "bin").mkdir(parents=True)
    (paths.python_prefix / "bin/python-portable").symlink_to(sys.executable)
    (paths.python_prefix / "bin/python3").symlink_to(sys.executable)
    (paths.python_prefix / "lib").symlink_to(Path(sys.base_prefix) / "lib")
    legacy_site = paths.custom_node_runtime / "site-packages"
    legacy_site.mkdir()
    (legacy_site / "legacy_node_dep.py").write_text("SENTINEL = 42\n", encoding="utf-8")
    legacy_tool = paths.custom_node_runtime / "bin/legacy-tool"
    legacy_tool.parent.mkdir(exist_ok=True)
    legacy_tool.write_text(
        "#!/old/portable/python\nprint('legacy tool')\n", encoding="utf-8"
    )
    legacy_tool.chmod(0o755)

    assert paths.ensure_node_runtime() > 0
    assert paths.custom_node_site_packages != legacy_site
    assert not legacy_site.exists()
    assert (paths.custom_node_site_packages / "legacy_node_dep.py").is_file()
    assert (
        (paths.custom_node_bin / "legacy-tool")
        .read_text()
        .startswith("#!/bin/sh\n'''exec'")
    )
    subprocess.run(
        [
            str(paths.custom_node_python),
            "-s",
            "-c",
            "import legacy_node_dep; assert legacy_node_dep.SENTINEL == 42",
        ],
        env=paths.server_environment(),
        check=True,
    )


def test_node_runtime_rebinds_after_complete_portable_root_move(
    portable_root: PortablePaths, tmp_path: Path
) -> None:
    source_node = portable_root.root / "custom_nodes/local_probe"
    source_node.mkdir(parents=True)
    (source_node / "relocated_probe.py").write_text(
        "VALUE = 'moved'\n", encoding="utf-8"
    )
    editable = portable_root.custom_node_site_packages / "relocated-probe.pth"
    editable.write_text(str(source_node) + "\n", encoding="utf-8")
    moved_root = tmp_path / "moved portable"
    shutil.copytree(portable_root.root, moved_root, symlinks=True)
    moved = PortablePaths(moved_root)

    assert moved.repair_runtime_metadata() > 0
    moved.ensure_node_runtime()
    assert str(moved.python_prefix / "bin") in (
        moved.custom_node_runtime / "pyvenv.cfg"
    ).read_text(encoding="utf-8")
    assert str(moved.root / "custom_nodes/local_probe") in (
        moved.custom_node_site_packages / "relocated-probe.pth"
    ).read_text(encoding="utf-8")
    subprocess.run(
        [
            str(moved.custom_node_python),
            "-s",
            "-c",
            "import relocated_probe; assert relocated_probe.VALUE == 'moved'",
        ],
        env=moved.server_environment(),
        check=True,
    )


def test_python_abi_change_rebuilds_empty_node_venv(
    portable_root: PortablePaths,
) -> None:
    configuration = portable_root.custom_node_runtime / "pyvenv.cfg"
    lines = configuration.read_text(encoding="utf-8").splitlines()
    configuration.write_text(
        "\n".join(
            "version = 2.7" if line.startswith("version = ") else line for line in lines
        )
        + "\n",
        encoding="utf-8",
    )
    assert portable_root.ensure_node_runtime() > 0
    assert "version = 2.7" not in configuration.read_text(encoding="utf-8")


def test_python_abi_change_refuses_nonempty_node_venv(
    portable_root: PortablePaths,
) -> None:
    (portable_root.custom_node_site_packages / "node_package.py").write_text(
        "VALUE = 1\n", encoding="utf-8"
    )
    configuration = portable_root.custom_node_runtime / "pyvenv.cfg"
    lines = configuration.read_text(encoding="utf-8").splitlines()
    configuration.write_text(
        "\n".join(
            "version = 2.7" if line.startswith("version = ") else line for line in lines
        )
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(LayoutError, match="baseline changed"):
        portable_root.ensure_node_runtime()


def test_node_venv_pip_shadows_base_then_upgrades_without_duplicate_metadata(
    portable_root: PortablePaths, tmp_path: Path
) -> None:
    base_site = tmp_path / "immutable-base-site"
    base_dist = base_site / "portable_base_probe-0.5.dist-info"
    base_dist.mkdir(parents=True)
    (base_site / "portable_base_probe.py").write_text(
        "VERSION = '0.5'\n", encoding="utf-8"
    )
    (base_dist / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: portable-base-probe\nVersion: 0.5\n",
        encoding="utf-8",
    )
    before = {
        path.relative_to(base_site): path.read_bytes()
        for path in base_site.rglob("*")
        if path.is_file()
    }
    (portable_root.custom_node_site_packages / "base-probe.pth").write_text(
        str(base_site) + "\n", encoding="utf-8"
    )
    environment = portable_root.server_environment()

    for version in ("1.0", "2.0"):
        wheel = _write_probe_wheel(tmp_path, version)
        completed = subprocess.run(
            [
                str(portable_root.custom_node_python),
                "-s",
                "-m",
                "pip",
                "install",
                "--no-deps",
                "--disable-pip-version-check",
                str(wheel),
            ],
            env=environment,
            text=True,
            capture_output=True,
            timeout=60,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr or completed.stdout
        observed = subprocess.run(
            [
                str(portable_root.custom_node_python),
                "-s",
                "-c",
                "import portable_base_probe as p; print(p.VERSION)",
            ],
            env=environment,
            text=True,
            capture_output=True,
            check=True,
        )
        assert observed.stdout.strip() == version

    assert before == {
        path.relative_to(base_site): path.read_bytes()
        for path in base_site.rglob("*")
        if path.is_file()
    }
    metadata = sorted(
        path.name
        for path in portable_root.custom_node_site_packages.glob(
            "portable_base_probe-*.dist-info"
        )
    )
    assert metadata == ["portable_base_probe-2.0.dist-info"]

    removed = subprocess.run(
        [
            str(portable_root.custom_node_python),
            "-s",
            "-m",
            "pip",
            "uninstall",
            "--yes",
            "portable-base-probe",
        ],
        env=environment,
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )
    assert removed.returncode == 0, removed.stderr or removed.stdout
    fallback = subprocess.run(
        [
            str(portable_root.custom_node_python),
            "-s",
            "-c",
            "import portable_base_probe as p; print(p.VERSION)",
        ],
        env=environment,
        text=True,
        capture_output=True,
        check=True,
    )
    assert fallback.stdout.strip() == "0.5"
    assert not list(
        portable_root.custom_node_site_packages.glob("portable_base_probe-*.dist-info")
    )
    assert before == {
        path.relative_to(base_site): path.read_bytes()
        for path in base_site.rglob("*")
        if path.is_file()
    }


def test_runtime_metadata_repair_handles_move_and_new_pip_scripts(
    tmp_path: Path,
) -> None:
    paths = PortablePaths(tmp_path / "New Root")
    bin_dir = paths.python_prefix / "bin"
    config_dir = paths.python_prefix / "lib/python3.13/config-test"
    bin_dir.mkdir(parents=True)
    config_dir.mkdir(parents=True)
    (bin_dir / "python3").write_text("binary placeholder", encoding="utf-8")
    script = bin_dir / "node-tool"
    script.write_text(
        "#!/old/root/ComfyUI/runtime/python/bin/python3\nprint('ok')\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    makefile = config_dir / "Makefile"
    makefile.write_text("prefix=/old/root/ComfyUI/runtime/python\n", encoding="utf-8")
    (paths.python_prefix / ".portable-comfy-prefix").write_text(
        "/old/root/ComfyUI/runtime/python\n", encoding="utf-8"
    )
    changed = paths.repair_runtime_metadata()
    assert changed == 3
    assert script.read_text(encoding="utf-8").startswith("#!/bin/sh\n'''exec'")
    assert str(paths.python_prefix) in makefile.read_text(encoding="utf-8")
    assert (paths.python_prefix / ".portable-comfy-prefix").read_text().strip() == str(
        paths.python_prefix
    )


def test_runtime_repair_relocates_persistent_node_entrypoints(
    portable_root: PortablePaths,
) -> None:
    paths = portable_root
    script = paths.custom_node_bin / "node-command"
    script.write_text(
        "#!/old/place/ComfyUI/runtime/python/bin/python3\nprint('node')\n",
        encoding="utf-8",
    )
    editable = paths.custom_node_site_packages / "local-node.pth"
    editable.write_text("/old/place/custom_nodes/local-node\n", encoding="utf-8")
    (paths.custom_node_runtime / ".portable-comfy-root").write_text(
        "/old/place\n", encoding="utf-8"
    )

    assert paths.repair_runtime_metadata() >= 3
    wrapper = script.read_text(encoding="utf-8")
    assert wrapper.startswith("#!/bin/sh\n'''exec'")
    assert ')/python" -s' in wrapper
    assert str(paths.root / "custom_nodes/local-node") in editable.read_text()
