from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from portable_comfy.paths import LayoutError, PortablePaths


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
    assert command[:2] == [str(portable_root.python), "-s"]
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
    assert env["PYTHONPATH"] == str(portable_root.custom_node_site_packages)
    assert "VIRTUAL_ENV" not in env
    assert env["PYTHONHOME"] == str(portable_root.python_prefix)
    assert env["LD_LIBRARY_PATH"] == f"{portable_root.python_prefix / 'lib'}:/host/lib"
    assert "/pyinstaller" not in env["LD_LIBRARY_PATH"]
    assert env["CM_USE_PYGIT2"] == "1"
    assert env["PIP_TARGET"] == str(portable_root.custom_node_site_packages)
    assert str(portable_root.custom_node_bin) in env["PATH"]


def test_candidate_environment_can_exclude_persistent_node_overlay(
    portable_root: PortablePaths,
) -> None:
    candidate = portable_root.state / "candidate/ComfyUI"
    prefix = candidate / "runtime/python"
    env = portable_root.server_environment(
        python_prefix=prefix,
        comfyui_path=candidate,
        include_node_overlay=False,
    )
    assert "PYTHONPATH" not in env
    assert "PIP_TARGET" not in env
    assert env["PYTHONHOME"] == str(prefix)
    assert env["COMFYUI_PATH"] == str(candidate)


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
    assert changed == 2
    assert script.read_text(encoding="utf-8").startswith("#!/bin/sh\n'''exec'")
    assert str(paths.python_prefix) in makefile.read_text(encoding="utf-8")
    assert (paths.python_prefix / ".portable-comfy-prefix").read_text().strip() == str(
        paths.python_prefix
    )


def test_runtime_repair_relocates_persistent_node_entrypoints(tmp_path: Path) -> None:
    paths = PortablePaths(tmp_path / "Moved Portable")
    paths.create_layout()
    (paths.python_prefix / "bin").mkdir(parents=True)
    (paths.python_prefix / "bin/python3").write_text("placeholder")
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

    assert paths.repair_runtime_metadata() == 2
    wrapper = script.read_text(encoding="utf-8")
    assert wrapper.startswith("#!/bin/sh\n'''exec'")
    assert "ComfyUI/runtime/python/bin/python3" in wrapper
    assert str(paths.root / "custom_nodes/local-node") in editable.read_text()
