from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tarfile
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
CONSTRAINTS = REPO / "packaging/runtime-constraints.txt"
LOCK_SHA256 = hashlib.sha256(CONSTRAINTS.read_bytes()).hexdigest()


def make_environment(root: Path) -> Path:
    comfyui = root / "ComfyUI"
    (comfyui / "frontend").mkdir(parents=True)
    (comfyui / "runtime/python/bin").mkdir(parents=True)
    (comfyui / "main.py").write_text("# test Core\n", encoding="utf-8")
    (comfyui / "frontend/index.html").write_text("<!doctype html>\n", encoding="utf-8")
    (comfyui / "runtime/requirements.lock").write_bytes(CONSTRAINTS.read_bytes())
    for name in ("python-portable", "repair-portable-entrypoints"):
        path = comfyui / "runtime/python/bin" / name
        path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        path.chmod(0o755)
    (comfyui / "main-link.py").symlink_to("main.py")
    return comfyui


def generate(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "python3",
            str(REPO / "scripts/generate_environment_manifest.py"),
            str(root),
            "--generation-id",
            "test-environment-1",
            "--core-version",
            "0.27.0",
            "--core-tag",
            "v0.27.0",
            "--core-commit",
            "a" * 40,
            "--frontend-version",
            "1.45.20",
            "--frontend-commit",
            "b" * 40,
            "--python",
            "3.13.12",
            "--torch",
            "2.12.0+cu130",
            "--torchvision",
            "0.27.0+cu130",
            "--torchaudio",
            "2.11.0+cu130",
            "--cuda",
            "13.0",
            "--requirements-lock-sha256",
            LOCK_SHA256,
        ],
        check=False,
        text=True,
        capture_output=True,
        env={**os.environ, "SOURCE_DATE_EPOCH": "1782855362"},
    )


def verify(root: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "python3",
            str(REPO / "scripts/verify_environment_bundle.py"),
            str(root),
            *extra,
        ],
        check=False,
        text=True,
        capture_output=True,
    )


def test_manifest_covers_core_runtime_and_relative_links(tmp_path: Path) -> None:
    root = tmp_path / "environment"
    make_environment(root)
    assert generate(root).returncode == 0
    checked = verify(root)
    assert checked.returncode == 0, checked.stderr

    manifest = json.loads((root / "manifest/environment.json").read_text())
    assert manifest["schema_version"] == 2
    assert manifest["bundle_type"] == "environment"
    assert manifest["runtime"]["requirements_lock_path"] == (
        "ComfyUI/runtime/requirements.lock"
    )
    entries = {item["path"]: item for item in manifest["files"]}
    assert entries["ComfyUI/main.py"]["type"] == "file"
    assert entries["ComfyUI/runtime/python/bin/python-portable"]["type"] == "file"
    assert entries["ComfyUI/main-link.py"] == {
        "path": "ComfyUI/main-link.py",
        "type": "symlink",
        "target": "main.py",
    }
    checksums = (root / "manifest/environment-checksums.sha256").read_text()
    assert "ComfyUI/main.py" in checksums
    assert "ComfyUI/main-link.py" not in checksums


def test_verifier_rejects_tampering_and_persistent_payload(tmp_path: Path) -> None:
    root = tmp_path / "environment"
    make_environment(root)
    assert generate(root).returncode == 0
    (root / "ComfyUI/main.py").write_text("tampered\n", encoding="utf-8")
    checked = verify(root)
    assert checked.returncode != 0
    assert "checksum or size mismatch" in checked.stderr

    (root / "ComfyUI/main.py").write_text("# test Core\n", encoding="utf-8")
    (root / "models").mkdir()
    checked = verify(root)
    assert checked.returncode != 0
    assert "unexpected top-level payload: models" in checked.stderr


def test_generator_rejects_escaping_link(tmp_path: Path) -> None:
    root = tmp_path / "environment"
    comfyui = make_environment(root)
    (comfyui / "escape").symlink_to("../../outside")
    generated = generate(root)
    assert generated.returncode != 0
    assert "link escapes environment payload" in generated.stderr


def test_structural_builder_archives_only_atomic_environment(tmp_path: Path) -> None:
    source = tmp_path / "source/Portable-Comfy"
    make_environment(source)
    for persistent in ("models", "custom_nodes", "workflows", "user", "output"):
        directory = source / persistent
        directory.mkdir(parents=True)
        (directory / "must-not-ship.txt").write_text("persistent\n", encoding="utf-8")
    output = tmp_path / "artifacts"
    work = tmp_path / "work"
    built = subprocess.run(
        [
            str(REPO / "scripts/build_environment_bundle.sh"),
            "--source-root",
            str(source),
            "--output-dir",
            str(output),
            "--work-dir",
            str(work),
            "--structural",
        ],
        check=False,
        text=True,
        capture_output=True,
    )
    assert built.returncode == 0, built.stderr
    archive = output / "Portable-Comfy-environment-v0.27.0.tar.gz"
    assert archive.is_file()
    with tarfile.open(archive, "r:gz") as stream:
        names = stream.getnames()
        assert names[0] in {
            "Portable-Comfy-environment-v0.27.0",
            "Portable-Comfy-environment-v0.27.0/",
        }
        assert any(
            name.endswith("/ComfyUI/runtime/requirements.lock") for name in names
        )
        assert any(name.endswith("/manifest/environment.json") for name in names)
        assert not any(
            f"/{persistent}/" in f"/{name}/"
            for name in names
            for persistent in ("models", "custom_nodes", "workflows", "user", "output")
        )
        extracted = tmp_path / "extracted"
        stream.extractall(extracted, filter="data")
    root = extracted / "Portable-Comfy-environment-v0.27.0"
    checked = verify(root, "--structural")
    assert checked.returncode == 0, checked.stderr
    assert set(path.name for path in root.iterdir()) == {"ComfyUI", "manifest"}
