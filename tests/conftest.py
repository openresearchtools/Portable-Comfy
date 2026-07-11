from __future__ import annotations

import json
import os
import shlex
from pathlib import Path

import pytest

from portable_comfy.paths import PortablePaths


@pytest.fixture
def portable_root(tmp_path: Path) -> PortablePaths:
    paths = PortablePaths(tmp_path / "Portable Root With Spaces")
    paths.create_layout()
    (paths.python_prefix / "bin").mkdir(parents=True)
    python = paths.python_prefix / "bin" / "python-portable"
    # The production executable belongs to this prefix. A test symlink to the
    # host interpreter would receive the intentionally isolated PYTHONHOME and
    # fail before running our fixture server, so use a tiny environment bridge.
    python.write_text(
        "#!/bin/sh\nunset PYTHONHOME\nexec "
        + shlex.quote(os.sys.executable)
        + ' "$@"\n',
        encoding="utf-8",
    )
    python.chmod(0o755)
    (paths.comfyui / "frontend").mkdir(parents=True)
    (paths.comfyui / "main.py").write_text("# test core\n", encoding="utf-8")
    (paths.frontend / "index.html").write_text("<!doctype html>\n", encoding="utf-8")
    paths.runtime_manifest.write_text(
        json.dumps(
            {
                "python": "3.13.12",
                "torch": "2.12.0+cu130",
                "cuda": "13.0",
                "platform": "linux-x86_64",
                "requirements_lock_sha256": "a" * 64,
            }
        ),
        encoding="utf-8",
    )
    return paths
