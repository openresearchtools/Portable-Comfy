from __future__ import annotations

import subprocess
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
TAESD_MODELS = (
    "taef1_decoder.safetensors",
    "taef1_encoder.safetensors",
    "taesd3_decoder.safetensors",
    "taesd3_encoder.safetensors",
    "taesd_decoder.safetensors",
    "taesd_encoder.safetensors",
    "taesdxl_decoder.safetensors",
    "taesdxl_encoder.safetensors",
)


def make_bootstrap(root: Path) -> None:
    for relative in (
        "custom_nodes",
        "custom_node_runtime",
        "models/vae_approx",
        "input",
        "output",
        "temp",
        "workflows",
        "user/default",
        "logs",
        "config",
        "manifest",
        "state",
        "cache",
        "LICENSES",
    ):
        (root / relative).mkdir(parents=True, exist_ok=True)
    (root / "user/default/workflows").symlink_to("../../workflows")
    (root / "LICENSE").write_text("project license\n", encoding="utf-8")
    (root / "LICENSES/Portable-Comfy-GPL-3.0.txt").write_text(
        "project license\n", encoding="utf-8"
    )
    (root / "LICENSES/README.txt").write_text("notice index\n", encoding="utf-8")
    (root / "LICENSES/TAESD-MIT.txt").write_text("model license\n", encoding="utf-8")
    (root / "manifest/builtin-models.json").write_text("{}\n", encoding="utf-8")
    (root / "config/extra_model_paths.yaml").write_text("{}\n", encoding="utf-8")
    for model in TAESD_MODELS:
        (root / "models/vae_approx" / model).write_bytes(b"model")
    subprocess.run(
        ["python3", str(REPO / "scripts/generate_file_manifest.py"), str(root)],
        check=True,
    )


def preflight(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(REPO / "scripts/preflight_portable.sh"), str(root), "--structural"],
        check=False,
        text=True,
        capture_output=True,
    )


def test_bootstrap_preflight_requires_core_free_launcher_layout(tmp_path: Path) -> None:
    root = tmp_path / "Portable-Comfy"
    make_bootstrap(root)

    checked = preflight(root)
    assert checked.returncode == 0, checked.stderr

    (root / "ComfyUI").mkdir()
    checked = preflight(root)
    assert checked.returncode != 0
    assert "must not contain a ComfyUI environment" in checked.stderr


def test_bootstrap_preflight_rejects_environment_manifest(tmp_path: Path) -> None:
    root = tmp_path / "Portable-Comfy"
    make_bootstrap(root)
    (root / "manifest/environment.json").write_text("{}\n", encoding="utf-8")

    checked = preflight(root)
    assert checked.returncode != 0
    assert "must not claim an installed environment" in checked.stderr


def test_workflow_delivers_bootstrap_and_only_multipart_core_files() -> None:
    workflow = (REPO / ".github/workflows/build-artifacts.yml").read_text(
        encoding="utf-8"
    )

    assert (
        '--source-root "$RUNNER_TEMP/portable-build/'
        'environment-source/Portable-Comfy"' in workflow
    )
    assert "python3 scripts/split_archive.py" in workflow
    assert "--part-size 1900000000" in workflow
    assert "artifacts/${{ steps.versions.outputs.core_archive }}.parts.json" in workflow
    assert "artifacts/${{ steps.versions.outputs.core_archive }}.part0*" in workflow
    assert not any(
        line.strip() == "path: artifacts/${{ steps.versions.outputs.core_archive }}"
        for line in workflow.splitlines()
    )
