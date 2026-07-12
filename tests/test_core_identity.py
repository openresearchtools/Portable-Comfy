from __future__ import annotations

import importlib.util
import subprocess
import zipfile
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/verify_core_identity.py"
SPEC = importlib.util.spec_from_file_location("verify_core_identity", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


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


@pytest.mark.parametrize(
    ("metadata_name", "metadata_version", "passes"),
    [
        ("comfyui_frontend_package", "1.45.20", True),
        ("ComfyUI-Frontend-Package", "1.45.20", False),
        ("comfyui_frontend_package", "1.45.19", False),
    ],
)
def test_frontend_wheel_requires_exact_metadata_identity(
    tmp_path: Path,
    metadata_name: str,
    metadata_version: str,
    passes: bool,
) -> None:
    (tmp_path / "comfyui_version.py").write_text(
        '__version__ = "0.27.0"\n', encoding="utf-8"
    )
    (tmp_path / "requirements.txt").write_text(
        "comfyui-frontend-package==1.45.20\n", encoding="utf-8"
    )
    wheel = tmp_path / "comfyui_frontend_package-1.45.20-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as output:
        output.writestr(
            "comfyui_frontend_package-1.45.20.dist-info/METADATA",
            "Metadata-Version: 2.4\n"
            f"Name: {metadata_name}\n"
            f"Version: {metadata_version}\n",
        )
        output.writestr("comfyui_frontend_package/static/index.html", "frontend")

    completed = subprocess.run(
        [
            "python3",
            str(SCRIPT),
            str(tmp_path),
            "0.27.0",
            "1.45.20",
            "--frontend-wheel",
            str(wheel),
        ],
        check=False,
        text=True,
        capture_output=True,
    )

    assert (completed.returncode == 0) is passes
    if not passes:
        assert "frontend wheel identity mismatch" in completed.stderr


def test_tag_resolution_prefers_peeled_commit_and_accepts_lightweight() -> None:
    direct = "a" * 40
    peeled = "b" * 40
    assert (
        MODULE.resolve_remote_tag(f"{direct}\trefs/tags/v0.27.0\n", "v0.27.0") == direct
    )
    assert (
        MODULE.resolve_remote_tag(
            f"{direct}\trefs/tags/v0.27.0\n{peeled}\trefs/tags/v0.27.0^{{}}\n",
            "v0.27.0",
        )
        == peeled
    )
