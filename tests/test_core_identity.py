from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/verify_core_identity.py"


@pytest.mark.parametrize(
    ("source_version", "requirement", "passes"),
    [
        ("0.27.0", "comfyui-frontend-package==1.45.20", True),
        ("0.26.0", "comfyui-frontend-package==1.45.20", False),
        ("0.27.0", "comfyui_frontend_package==1.45.20", False),
        ("0.27.0", "comfyui-frontend-package==1.44.0", False),
        ("0.27.0", "comfyui-frontend-package>=1.45.20", False),
        ("0.27.0", "", False),
    ],
)
def test_core_source_requires_exact_version_and_frontend_pin(
    tmp_path: Path, source_version: str, requirement: str, passes: bool
) -> None:
    (tmp_path / "comfyui_version.py").write_text(
        f'__version__ = "{source_version}"\n', encoding="utf-8"
    )
    (tmp_path / "requirements.txt").write_text(
        f"torch\n{requirement}\naiohttp\n", encoding="utf-8"
    )

    completed = subprocess.run(
        ["python3", str(SCRIPT), str(tmp_path), "0.27.0", "1.45.20"],
        check=False,
        text=True,
        capture_output=True,
    )

    assert (completed.returncode == 0) is passes
    if not passes:
        assert "version mismatch" in completed.stderr
