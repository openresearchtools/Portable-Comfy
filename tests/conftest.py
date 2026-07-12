from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from portable_comfy.paths import PortablePaths


@pytest.fixture
def portable_root(tmp_path: Path) -> PortablePaths:
    paths = PortablePaths(tmp_path / "Portable Root With Spaces")
    paths.create_layout()
    (paths.python_prefix / "bin").mkdir(parents=True)
    python = paths.python_prefix / "bin" / "python-portable"
    python.symlink_to(sys.executable)
    (paths.python_prefix / "bin" / "python3").symlink_to(sys.executable)
    # Rebinding pyvenv.cfg makes the fake prefix the base prefix. Expose the
    # host standard library beneath it so the persistent venv remains real.
    (paths.python_prefix / "lib").symlink_to(Path(sys.base_prefix) / "lib")
    (paths.comfyui / "frontend").mkdir(parents=True)
    (paths.comfyui / "main.py").write_text("# test core\n", encoding="utf-8")
    (paths.frontend / "index.html").write_text("<!doctype html>\n", encoding="utf-8")
    paths.environment_manifest.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "bundle_type": "environment",
                "generation_id": "fixture-environment",
                "runtime": {
                    "python": "3.14.1",
                    "torch": "3.0.0+cu140",
                    "torchvision": "0.30.0+cu140",
                    "torchaudio": "3.0.0+cu140",
                    "cuda": "14.0",
                    "platform": "linux-x86_64",
                    "requirements_lock_path": "ComfyUI/runtime/requirements.lock",
                    "requirements_lock_sha256": "a" * 64,
                },
            }
        ),
        encoding="utf-8",
    )
    paths.ensure_node_runtime()
    return paths
