#!/usr/bin/env python3
"""Validate pinned runtime versions and materialize donor license notices."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import re
import shutil
from collections.abc import Iterable, Mapping
from pathlib import Path, PurePosixPath


WORKFLOW_RECIPIENTS = (
    "comfyui-workflow-templates-core",
    "comfyui-workflow-templates-json",
    "comfyui-workflow-templates-media-api",
    "comfyui-workflow-templates-media-assets-01",
    "comfyui-workflow-templates-media-image",
    "comfyui-workflow-templates-media-other",
    "comfyui-workflow-templates-media-video",
)
WORKFLOW_DONOR = "comfyui-workflow-templates"
CUDA_RECIPIENT = "cuda-toolkit"
CUDA_DONOR = "nvidia-cuda-runtime"
EXTERNAL_NOTICE_PACKAGES = (
    "pyopengl",
    "sentencepiece",
    "spandrel",
    "tokenizers",
    "trampoline",
)
EXACT_PIN = re.compile(r"^([A-Za-z0-9_.-]+)==([^;\s]+)(?:\s*;.*)?$")


def normalized_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def read_exact_pins(path: Path) -> dict[str, str]:
    pins: dict[str, str] = {}
    for line_number, raw_line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = EXACT_PIN.fullmatch(line)
        if not match:
            raise RuntimeError(
                f"runtime lock line {line_number} is not an exact package pin: {line}"
            )
        name, version = match.groups()
        normalized = normalized_name(name)
        previous = pins.setdefault(normalized, version)
        if previous != version:
            raise RuntimeError(f"runtime lock has conflicting pins for {name}")
    return pins


def distribution_map(
    distributions: Iterable[importlib.metadata.Distribution],
) -> dict[str, importlib.metadata.Distribution]:
    result: dict[str, importlib.metadata.Distribution] = {}
    for distribution in distributions:
        name = distribution.metadata.get("Name")
        if not name:
            continue
        normalized = normalized_name(name)
        if normalized in result:
            raise RuntimeError(f"multiple installed distributions match {name}")
        result[normalized] = distribution
    return result


def require_exact_version(
    name: str,
    expected: str,
    pins: Mapping[str, str],
    installed: Mapping[str, importlib.metadata.Distribution],
) -> importlib.metadata.Distribution:
    normalized = normalized_name(name)
    locked = pins.get(normalized)
    if locked != expected:
        raise RuntimeError(
            f"runtime lock version for {name} is {locked!r}, expected {expected!r}"
        )
    distribution = installed.get(normalized)
    if distribution is None:
        raise RuntimeError(f"required notice distribution is not installed: {name}")
    if distribution.version != expected:
        raise RuntimeError(
            f"installed version for {name} is {distribution.version!r}, "
            f"expected {expected!r}"
        )
    return distribution


def locate_dist_info_notice(
    distribution: importlib.metadata.Distribution,
) -> Path:
    candidates: list[Path] = []
    for file in distribution.files or ():
        path = PurePosixPath(file.as_posix())
        parts = tuple(part.lower() for part in path.parts)
        if not any(part.endswith(".dist-info") for part in parts):
            continue
        if len(parts) < 2 or parts[-2] not in {"license", "licenses"}:
            continue
        if path.name.lower() not in {"license", "license.txt"}:
            continue
        source = Path(distribution.locate_file(file)).resolve()
        if source.is_file():
            candidates.append(source)
    if len(candidates) != 1:
        name = distribution.metadata.get("Name") or "unknown"
        raise RuntimeError(
            f"expected exactly one installed donor notice for {name}, "
            f"found {len(candidates)}"
        )
    return candidates[0]


def prepare(
    destination: Path,
    requirements_lock: Path,
    workflow_reference: Path,
    notice_versions: Mapping[str, str],
    distributions: Iterable[importlib.metadata.Distribution],
) -> dict[str, Path]:
    pins = read_exact_pins(requirements_lock)
    installed = distribution_map(distributions)
    expected_names = {
        WORKFLOW_DONOR,
        CUDA_RECIPIENT,
        *EXTERNAL_NOTICE_PACKAGES,
    }
    normalized_versions = {
        normalized_name(name): version for name, version in notice_versions.items()
    }
    if set(normalized_versions) != {normalized_name(name) for name in expected_names}:
        raise RuntimeError(
            "notice version set is incomplete or contains unknown packages"
        )

    for name in (*WORKFLOW_RECIPIENTS, CUDA_DONOR):
        normalized = normalized_name(name)
        expected = pins.get(normalized)
        if expected is None:
            raise RuntimeError(f"runtime lock is missing notice package: {name}")
        require_exact_version(name, expected, pins, installed)
    for name in expected_names:
        require_exact_version(
            name,
            normalized_versions[normalized_name(name)],
            pins,
            installed,
        )

    workflow_distribution = installed[normalized_name(WORKFLOW_DONOR)]
    workflow_source = locate_dist_info_notice(workflow_distribution)
    if not workflow_reference.is_file():
        raise RuntimeError(f"pinned workflow license is missing: {workflow_reference}")
    if workflow_source.read_bytes() != workflow_reference.read_bytes():
        raise RuntimeError(
            "installed comfyui_workflow_templates license does not match its "
            "pinned upstream notice"
        )

    cuda_distribution = installed[normalized_name(CUDA_DONOR)]
    cuda_source = locate_dist_info_notice(cuda_distribution)
    if not cuda_source.read_bytes():
        raise RuntimeError("installed NVIDIA CUDA runtime license is empty")

    destination.mkdir(parents=True, exist_ok=True)
    workflow_target = destination / "workflow-templates-donor-LICENSE.txt"
    cuda_target = destination / "nvidia-cuda-runtime-donor-LICENSE.txt"
    shutil.copyfile(workflow_source, workflow_target)
    shutil.copyfile(cuda_source, cuda_target)
    return {"workflow": workflow_target, "cuda": cuda_target}


def parse_notice_versions(values: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for value in values:
        name, separator, version = value.partition("=")
        if not separator or not name or not version:
            raise ValueError("notice versions must use DISTRIBUTION=VERSION")
        normalized = normalized_name(name)
        if normalized in result:
            raise ValueError(f"duplicate notice version: {name}")
        result[normalized] = version
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("destination", type=Path)
    parser.add_argument("requirements_lock", type=Path)
    parser.add_argument("workflow_reference", type=Path)
    parser.add_argument(
        "--notice-version",
        action="append",
        default=[],
        metavar="DISTRIBUTION=VERSION",
    )
    args = parser.parse_args()
    try:
        notice_versions = parse_notice_versions(args.notice_version)
    except ValueError as error:
        parser.error(str(error))
    prepared = prepare(
        args.destination,
        args.requirements_lock,
        args.workflow_reference,
        notice_versions,
        importlib.metadata.distributions(),
    )
    for name, path in sorted(prepared.items()):
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        print(f"{name}\t{path}\t{digest}")


if __name__ == "__main__":
    main()
