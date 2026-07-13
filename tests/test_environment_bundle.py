from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tarfile
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[1]
CONSTRAINTS = REPO / "packaging/runtime-constraints.txt"
LOCK_SHA256 = hashlib.sha256(CONSTRAINTS.read_bytes()).hexdigest()


def pinned_versions() -> dict[str, str]:
    completed = subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; env',
            "portable-comfy-test",
            str(REPO / "packaging/versions.env"),
        ],
        check=True,
        text=True,
        capture_output=True,
    )
    wanted = {
        "COMFY_VERSION",
        "COMFY_TAG",
        "COMFY_COMMIT",
        "FRONTEND_VERSION",
        "FRONTEND_COMMIT",
        "PYTHON_VERSION",
        "TORCH_VERSION",
        "TORCHVISION_VERSION",
        "TORCHAUDIO_VERSION",
        "CUDA_VERSION",
    }
    result: dict[str, str] = {}
    for line in completed.stdout.splitlines():
        key, separator, value = line.partition("=")
        if separator and key in wanted:
            result[key] = value
    assert set(result) == wanted
    return result


PINNED = pinned_versions()
PINNED_GENERATION = (
    f"comfyui-v{PINNED['COMFY_VERSION']}-{PINNED['COMFY_COMMIT'][:12]}-"
    f"frontend-{PINNED['FRONTEND_VERSION']}-{PINNED['FRONTEND_COMMIT'][:12]}-"
    f"python-{PINNED['PYTHON_VERSION']}-cu{PINNED['CUDA_VERSION'].replace('.', '')}-"
    f"lock-{LOCK_SHA256}"
)


def make_environment(root: Path, *, frozen_requirements: str | None = None) -> Path:
    comfyui = root / "ComfyUI"
    (comfyui / "frontend").mkdir(parents=True)
    (comfyui / "runtime/python/bin").mkdir(parents=True)
    (comfyui / "main.py").write_text("# test Core\n", encoding="utf-8")
    (comfyui / "LICENSE").write_text("Core license\n", encoding="utf-8")
    (comfyui / "frontend/index.html").write_text("<!doctype html>\n", encoding="utf-8")
    (comfyui / "frontend/LICENSE").write_text("Frontend license\n", encoding="utf-8")
    (comfyui / "frontend/THIRD_PARTY_NOTICES.md").write_text(
        "Frontend notices\n", encoding="utf-8"
    )
    frontend_notices = comfyui / "frontend/LICENSES/npm/example-1.0.0"
    frontend_notices.mkdir(parents=True)
    (frontend_notices / "LICENSE").write_text(
        "Example frontend dependency license\n", encoding="utf-8"
    )
    frontend_metadata = {
        "name": "example-frontend-dependency",
        "version": "1.0.0",
        "license": "MIT",
        "notice_files": ["example-1.0.0/LICENSE"],
    }
    (frontend_notices / "PACKAGE-METADATA.json").write_text(
        json.dumps(frontend_metadata), encoding="utf-8"
    )
    (comfyui / "frontend/LICENSES/npm/packages.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "frontend": {
                    "version": PINNED["FRONTEND_VERSION"],
                    "commit": PINNED["FRONTEND_COMMIT"],
                },
                "packages": [
                    {
                        **frontend_metadata,
                        "metadata_file": "example-1.0.0/PACKAGE-METADATA.json",
                    }
                ],
                "summary": {
                    "distributions": 1,
                    "with_notice_files": 1,
                    "metadata_only": 0,
                },
            }
        ),
        encoding="utf-8",
    )
    (
        comfyui
        / f"frontend/SOURCE-ComfyUI-frontend-{PINNED['FRONTEND_VERSION']}.tar.gz"
    ).write_bytes(b"pinned frontend source\n")
    (comfyui / "runtime/requirements.lock").write_bytes(CONSTRAINTS.read_bytes())
    (comfyui / "runtime/python/LICENSE.txt").write_text(
        "CPython license\n", encoding="utf-8"
    )
    license_root = comfyui / "runtime/LICENSES/python-packages"
    packages = []
    for package_name in (
        "comfyui-frontend-package",
        "torch",
        "torchvision",
        "torchaudio",
        "nvidia-cublas",
        "nvidia-cuda-runtime",
        "nvidia-cudnn-cu13",
    ):
        relative = f"{package_name}/LICENSE"
        notice = license_root / relative
        notice.parent.mkdir(parents=True, exist_ok=True)
        notice.write_text(f"{package_name} license\n", encoding="utf-8")
        packages.append(
            {"name": package_name, "version": "1.0", "license_files": [relative]}
        )
    (license_root / "packages.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "packages": packages,
                "summary": {
                    "distributions": len(packages),
                    "with_license_files": len(packages),
                    "metadata_only": 0,
                    "unidentified": 0,
                },
            }
        ),
        encoding="utf-8",
    )
    default_freeze = "".join(
        f"{package['name'].replace('-', '_')}=={package['version']}\n"
        for package in packages
    )
    (comfyui / "runtime/installed-requirements.txt").write_text(
        default_freeze if frozen_requirements is None else frozen_requirements,
        encoding="utf-8",
    )
    for name in ("python-portable", "repair-portable-entrypoints"):
        path = comfyui / "runtime/python/bin" / name
        path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        path.chmod(0o755)
    (comfyui / "main-link.py").symlink_to("main.py")
    return comfyui


def generate(
    root: Path, *, core_commit: str | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "python3",
            str(REPO / "scripts/generate_environment_manifest.py"),
            str(root),
            "--generation-id",
            PINNED_GENERATION,
            "--core-version",
            PINNED["COMFY_VERSION"],
            "--core-tag",
            PINNED["COMFY_TAG"],
            "--core-commit",
            core_commit or PINNED["COMFY_COMMIT"],
            "--frontend-version",
            PINNED["FRONTEND_VERSION"],
            "--frontend-commit",
            PINNED["FRONTEND_COMMIT"],
            "--python",
            PINNED["PYTHON_VERSION"],
            "--torch",
            PINNED["TORCH_VERSION"],
            "--torchvision",
            PINNED["TORCHVISION_VERSION"],
            "--torchaudio",
            PINNED["TORCHAUDIO_VERSION"],
            "--cuda",
            PINNED["CUDA_VERSION"],
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
    root = tmp_path / f"Portable-Comfy-core-v{PINNED['COMFY_VERSION']}"
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
    identity_path = root / "ComfyUI/PORTABLE-COMFY-IDENTITY.json"
    identity = json.loads(identity_path.read_text(encoding="utf-8"))
    assert identity == {
        "schema_version": 1,
        "app_id": manifest["app_id"],
        "generation_id": manifest["generation_id"],
        "core": manifest["core"],
        "frontend": manifest["frontend"],
        "runtime": manifest["runtime"],
    }
    entries = {item["path"]: item for item in manifest["files"]}
    assert entries["ComfyUI/main.py"]["type"] == "file"
    assert entries["ComfyUI/PORTABLE-COMFY-IDENTITY.json"]["type"] == "file"
    assert entries["ComfyUI/runtime/python/bin/python-portable"]["type"] == "file"
    assert entries["ComfyUI/main-link.py"] == {
        "path": "ComfyUI/main-link.py",
        "type": "symlink",
        "target": "main.py",
    }
    checksums = (root / "manifest/environment-checksums.sha256").read_text()
    assert "ComfyUI/main.py" in checksums
    assert "ComfyUI/main-link.py" not in checksums


def test_verifier_binds_compiled_only_dependency_assets(tmp_path: Path) -> None:
    root = tmp_path / f"Portable-Comfy-core-v{PINNED['COMFY_VERSION']}"
    comfyui = make_environment(root)
    asset = comfyui / "frontend/fonts/example.woff2"
    asset.parent.mkdir(parents=True)
    asset.write_bytes(b"compiled font bytes")
    inventory_path = comfyui / "frontend/LICENSES/npm/packages.json"
    inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    inventory["packages"][0]["bundled_assets"] = [
        {
            "path": "fonts/example.woff2",
            "sha256": hashlib.sha256(asset.read_bytes()).hexdigest(),
            "size": asset.stat().st_size,
        }
    ]
    inventory_path.write_text(json.dumps(inventory), encoding="utf-8")
    assert generate(root).returncode == 0
    checked = verify(root)
    assert checked.returncode == 0, checked.stderr

    inventory["packages"][0]["bundled_assets"][0]["sha256"] = "0" * 64
    inventory_path.write_text(json.dumps(inventory), encoding="utf-8")
    assert generate(root).returncode == 0
    checked = verify(root)
    assert checked.returncode != 0
    assert "bundled asset disagrees" in checked.stderr


def test_verifier_rejects_tampering_and_persistent_payload(tmp_path: Path) -> None:
    root = tmp_path / f"Portable-Comfy-core-v{PINNED['COMFY_VERSION']}"
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
    root = tmp_path / f"Portable-Comfy-core-v{PINNED['COMFY_VERSION']}"
    comfyui = make_environment(root)
    (comfyui / "escape").symlink_to("../../outside")
    generated = generate(root)
    assert generated.returncode != 0
    assert "link escapes environment payload" in generated.stderr


def test_verifier_requires_exact_core_root_identity_and_notices(
    tmp_path: Path,
) -> None:
    root = tmp_path / f"Portable-Comfy-core-v{PINNED['COMFY_VERSION']}"
    make_environment(root)
    assert generate(root).returncode == 0

    renamed = tmp_path / "Portable-Comfy-core-v0.0.0"
    root.rename(renamed)
    checked = verify(renamed)
    assert checked.returncode != 0
    assert "root does not match manifest core.version" in checked.stderr

    renamed.rename(root)
    (root / "ComfyUI/frontend/THIRD_PARTY_NOTICES.md").unlink()
    assert generate(root).returncode == 0
    checked = verify(root)
    assert checked.returncode != 0
    assert "required redistribution notice" in checked.stderr


def test_verifier_rejects_non_exact_upstream_commit_identity(tmp_path: Path) -> None:
    root = tmp_path / f"Portable-Comfy-core-v{PINNED['COMFY_VERSION']}"
    make_environment(root)
    assert generate(root, core_commit="abc").returncode == 0
    checked = verify(root)
    assert checked.returncode != 0
    assert "Core version, tag, or commit is malformed" in checked.stderr


@pytest.mark.parametrize(
    ("frozen_requirements", "message"),
    [
        ("torch==1.0\n", "disagree"),
        ("torch==1.0\nTorch==1.0\n", "duplicate package"),
        ("torch @ file:///tmp/torch\n", "not exact NAME==VERSION"),
    ],
)
def test_verifier_binds_license_inventory_to_installed_runtime_freeze(
    tmp_path: Path, frozen_requirements: str, message: str
) -> None:
    root = tmp_path / f"Portable-Comfy-core-v{PINNED['COMFY_VERSION']}"
    make_environment(root, frozen_requirements=frozen_requirements)
    assert generate(root).returncode == 0
    checked = verify(root)
    assert checked.returncode != 0
    assert message in checked.stderr


def test_verifier_binds_installed_versions_to_license_inventory(tmp_path: Path) -> None:
    root = tmp_path / f"Portable-Comfy-core-v{PINNED['COMFY_VERSION']}"
    make_environment(root)
    freeze = root / "ComfyUI/runtime/installed-requirements.txt"
    freeze.write_text(
        freeze.read_text(encoding="utf-8").replace("torch==1.0", "torch==2.0"),
        encoding="utf-8",
    )
    assert generate(root).returncode == 0
    checked = verify(root)
    assert checked.returncode != 0
    assert "package license inventory disagree" in checked.stderr


def test_structural_builder_archives_only_atomic_environment(tmp_path: Path) -> None:
    source = tmp_path / "source/Portable-Comfy"
    make_environment(source)
    generated = generate(source)
    assert generated.returncode == 0, generated.stderr
    source_identity = (source / "ComfyUI/PORTABLE-COMFY-IDENTITY.json").read_bytes()
    source_manifest_bytes = (source / "manifest/environment.json").read_bytes()
    source_checksums = (source / "manifest/environment-checksums.sha256").read_bytes()
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
    archive = output / "Portable-Comfy-core-v0.27.0.tar.gz"
    assert archive.is_file()
    with tarfile.open(archive, "r:gz") as stream:
        names = stream.getnames()
        assert names[0] in {
            "Portable-Comfy-core-v0.27.0",
            "Portable-Comfy-core-v0.27.0/",
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
    root = extracted / "Portable-Comfy-core-v0.27.0"
    checked = verify(root, "--structural")
    assert checked.returncode == 0, checked.stderr
    assert set(path.name for path in root.iterdir()) == {"ComfyUI", "manifest"}
    assert (root / "ComfyUI/PORTABLE-COMFY-IDENTITY.json").read_bytes() == (
        source_identity
    )
    assert (root / "manifest/environment.json").read_bytes() == source_manifest_bytes
    assert (
        root / "manifest/environment-checksums.sha256"
    ).read_bytes() == source_checksums
