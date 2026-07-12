from __future__ import annotations

from pathlib import Path


REPO = Path(__file__).resolve().parents[1]


def test_runtime_build_contains_no_legacy_overlay_hook() -> None:
    installer = (REPO / "scripts/install_runtime_dependencies.sh").read_text(
        encoding="utf-8"
    )
    verifier = (REPO / "scripts/verify_environment_bundle.py").read_text(
        encoding="utf-8"
    )

    assert "portable_comfy_node_overlay.pth" not in installer
    assert "PORTABLE_COMFY_NODE_SITE_PACKAGES" not in installer
    assert "portable_comfy_node_overlay.pth" not in verifier


def test_portable_preflight_exercises_unseeded_system_site_venv() -> None:
    preflight = (REPO / "scripts/preflight_portable.sh").read_text(encoding="utf-8")

    assert "--system-site-packages" in preflight
    assert "--without-pip" in preflight
    assert '"$node_python" -m pip install' in preflight
