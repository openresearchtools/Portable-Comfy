from __future__ import annotations

import email
import importlib.util
from importlib.metadata import PackagePath
from pathlib import Path

import pytest


SCRIPT = Path(__file__).parents[1] / "scripts" / "prepare_runtime_license_extras.py"
SPEC = importlib.util.spec_from_file_location("prepare_runtime_license_extras", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


VERSIONS = {
    "comfyui-workflow-templates": "0.11.1",
    "comfyui-workflow-templates-core": "0.3.266",
    "comfyui-workflow-templates-json": "0.1.1",
    "comfyui-workflow-templates-media-api": "0.3.84",
    "comfyui-workflow-templates-media-assets-01": "0.1.0",
    "comfyui-workflow-templates-media-image": "0.3.160",
    "comfyui-workflow-templates-media-other": "0.3.229",
    "comfyui-workflow-templates-media-video": "0.3.101",
    "cuda-toolkit": "13.0.2",
    "nvidia-cuda-runtime": "13.0.96",
    "pyopengl": "3.1.10",
    "sentencepiece": "0.2.1",
    "spandrel": "0.4.2",
    "tokenizers": "0.22.2",
    "trampoline": "0.1.2",
}
NOTICE_VERSIONS = {
    name: VERSIONS[name]
    for name in (
        "comfyui-workflow-templates",
        "cuda-toolkit",
        "pyopengl",
        "sentencepiece",
        "spandrel",
        "tokenizers",
        "trampoline",
    )
}


class FakeDistribution:
    def __init__(
        self,
        root: Path,
        *,
        name: str,
        version: str,
        notice: bytes | None = None,
    ) -> None:
        self.root = root
        self.version = version
        self.metadata = email.message_from_string(
            f"Metadata-Version: 2.4\nName: {name}\nVersion: {version}\n"
        )
        self.files: list[PackagePath] = []
        if notice is not None:
            relative = PackagePath(
                f"{name.replace('-', '_')}-{version}.dist-info/licenses/LICENSE"
            )
            target = root / relative
            target.parent.mkdir(parents=True)
            target.write_bytes(notice)
            self.files.append(relative)

    def locate_file(self, path: PackagePath) -> Path:
        return self.root / path


def fixture_inputs(tmp_path: Path):
    workflow_text = b"Pinned workflow templates license\n"
    cuda_text = b"NVIDIA CUDA runtime license\n"
    distributions = []
    for name, version in VERSIONS.items():
        notice = None
        if name == "comfyui-workflow-templates":
            notice = workflow_text
        elif name == "nvidia-cuda-runtime":
            notice = cuda_text
        distributions.append(
            FakeDistribution(
                tmp_path / "site" / name,
                name=name,
                version=version,
                notice=notice,
            )
        )
    lock = tmp_path / "requirements.lock"
    lock.write_text(
        "# exact test lock\n"
        + "".join(f"{name}=={version}\n" for name, version in VERSIONS.items()),
        encoding="utf-8",
    )
    reference = tmp_path / "workflow-LICENSE"
    reference.write_bytes(workflow_text)
    return distributions, lock, reference


def test_prepares_exact_installed_workflow_and_cuda_donor_notices(tmp_path: Path):
    distributions, lock, reference = fixture_inputs(tmp_path)

    prepared = MODULE.prepare(
        tmp_path / "notices",
        lock,
        reference,
        NOTICE_VERSIONS,
        distributions,
    )

    assert prepared["workflow"].read_bytes() == reference.read_bytes()
    assert prepared["cuda"].read_bytes() == b"NVIDIA CUDA runtime license\n"


def test_rejects_installed_recipient_version_different_from_lock(tmp_path: Path):
    distributions, lock, reference = fixture_inputs(tmp_path)
    for distribution in distributions:
        if distribution.metadata["Name"] == "cuda-toolkit":
            distribution.version = "13.0.1"
            break

    with pytest.raises(RuntimeError, match="installed version for cuda-toolkit"):
        MODULE.prepare(
            tmp_path / "notices",
            lock,
            reference,
            NOTICE_VERSIONS,
            distributions,
        )


def test_rejects_donor_notice_different_from_pinned_upstream(tmp_path: Path):
    distributions, lock, reference = fixture_inputs(tmp_path)
    reference.write_bytes(b"Different notice\n")

    with pytest.raises(RuntimeError, match="does not match its pinned upstream"):
        MODULE.prepare(
            tmp_path / "notices",
            lock,
            reference,
            NOTICE_VERSIONS,
            distributions,
        )
