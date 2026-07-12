#!/usr/bin/env python3
"""Collect installed-distribution license files and a redistribution inventory."""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import shutil
from collections.abc import Iterable
from pathlib import Path, PurePosixPath


LICENSE_BASENAME_PREFIXES = ("license", "copying", "notice", "authors")


def safe_name(value: str) -> str:
    return "".join(
        character if character.isalnum() or character in ".-_" else "_"
        for character in value
    )


def metadata_values(
    distribution: importlib.metadata.Distribution, key: str
) -> list[str]:
    return [value for value in distribution.metadata.get_all(key, []) if value]


def license_metadata(
    distribution: importlib.metadata.Distribution,
) -> dict[str, object]:
    classifiers = [
        value
        for value in metadata_values(distribution, "Classifier")
        if value.startswith("License ::")
    ]
    project_urls: dict[str, str] = {}
    for value in metadata_values(distribution, "Project-URL"):
        label, separator, url = value.partition(",")
        if separator and label.strip() and url.strip():
            project_urls[label.strip()] = url.strip()
    return {
        "license_expression": distribution.metadata.get("License-Expression"),
        "license": distribution.metadata.get("License"),
        "license_classifiers": classifiers,
        "license_file_headers": metadata_values(distribution, "License-File"),
        "home_page": distribution.metadata.get("Home-page"),
        "project_urls": project_urls,
    }


def is_license_path(path: PurePosixPath) -> bool:
    """Return true only for notice-like files, not Python license helper modules."""
    parts = tuple(part.lower() for part in path.parts)
    basename = path.name.lower()
    if basename.startswith(LICENSE_BASENAME_PREFIXES):
        return True
    # PEP 639 wheels conventionally place arbitrary-named notices beneath the
    # distribution metadata's licenses directory. Do not match package source
    # trees such as packaging/licenses/_spdx.py.
    return any(
        parts[index].endswith((".dist-info", ".egg-info"))
        and index + 1 < len(parts)
        and parts[index + 1] in {"license", "licenses"}
        for index in range(len(parts))
    )


def _metadata_license_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    # Older wheels sometimes put the complete notice in License instead of a
    # License-File. A short identifier is inventory data, not a license text.
    value = value.strip()
    if "\n" in value and len(value) >= 200:
        return value + "\n"
    return None


def collect(
    destination: Path,
    distributions: Iterable[importlib.metadata.Distribution],
    *,
    required_license_files: Iterable[str] = (),
    extra_license_files: Iterable[tuple[str, Path]] = (),
) -> list[dict[str, object]]:
    destination = destination.resolve()
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True)

    required = {name.lower().replace("_", "-") for name in required_license_files}
    extras: dict[str, list[Path]] = {}
    for name, path in extra_license_files:
        extras.setdefault(name.lower().replace("_", "-"), []).append(path.resolve())
    found_names: set[str] = set()
    notices: list[dict[str, object]] = []
    ordered = sorted(
        distributions,
        key=lambda item: (item.metadata.get("Name") or "unknown").lower(),
    )
    for distribution in ordered:
        name = distribution.metadata.get("Name") or "unknown"
        normalized_name = name.lower().replace("_", "-")
        found_names.add(normalized_name)
        version = distribution.version
        metadata = license_metadata(distribution)
        entry: dict[str, object] = {
            "name": name,
            "version": version,
            **metadata,
            "license_files": [],
        }
        package_dir = destination / f"{safe_name(name)}-{safe_name(version)}"
        copied_sources: set[Path] = set()
        for file in distribution.files or []:
            package_path = PurePosixPath(file.as_posix())
            if not is_license_path(package_path):
                continue
            source = Path(distribution.locate_file(file)).resolve()
            if not source.is_file() or source in copied_sources:
                continue
            copied_sources.add(source)
            package_dir.mkdir(parents=True, exist_ok=True)
            target = package_dir / safe_name(package_path.as_posix())
            # A malformed wheel can list colliding paths after sanitization.
            if target.exists() and target.read_bytes() != source.read_bytes():
                raise RuntimeError(
                    f"colliding license paths for {name}: {package_path}"
                )
            shutil.copyfile(source, target)
            entry["license_files"].append(target.relative_to(destination).as_posix())

        for index, source in enumerate(extras.get(normalized_name, []), start=1):
            if not source.is_file():
                raise RuntimeError(f"external license file is missing: {source}")
            package_dir.mkdir(parents=True, exist_ok=True)
            target = package_dir / f"EXTERNAL-LICENSE-{index}-{safe_name(source.name)}"
            shutil.copyfile(source, target)
            entry["license_files"].append(target.relative_to(destination).as_posix())
            entry.setdefault("external_license_sources", []).append(source.name)

        if not entry["license_files"]:
            text = _metadata_license_text(metadata["license"])
            if text:
                package_dir.mkdir(parents=True, exist_ok=True)
                target = package_dir / "LICENSE-from-package-metadata.txt"
                target.write_text(text, encoding="utf-8")
                entry["license_files"].append(
                    target.relative_to(destination).as_posix()
                )
                entry["license_source"] = "core-metadata License field"

        has_identity = bool(
            entry["license_files"]
            or metadata["license_expression"]
            or metadata["license"]
            or metadata["license_classifiers"]
        )
        entry["status"] = (
            "license-files"
            if entry["license_files"]
            else "metadata-only"
            if has_identity
            else "unidentified"
        )
        if normalized_name in required and not entry["license_files"]:
            raise RuntimeError(
                f"required distribution has no redistributable license file: {name}=={version}"
            )
        notices.append(entry)

    missing = sorted(required - found_names)
    if missing:
        raise RuntimeError(f"required distribution is not installed: {missing[0]}")
    unused_extras = sorted(set(extras) - found_names)
    if unused_extras:
        raise RuntimeError(
            f"external license distribution is not installed: {unused_extras[0]}"
        )
    summary = {
        "schema_version": 2,
        "packages": notices,
        "summary": {
            "distributions": len(notices),
            "with_license_files": sum(
                bool(entry["license_files"]) for entry in notices
            ),
            "metadata_only": sum(
                entry["status"] == "metadata-only" for entry in notices
            ),
            "unidentified": sum(entry["status"] == "unidentified" for entry in notices),
        },
    }
    (destination / "packages.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return notices


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("destination", type=Path)
    parser.add_argument(
        "--require-license-file",
        action="append",
        default=[],
        metavar="DISTRIBUTION",
        help="fail unless this installed distribution contributes a license file",
    )
    parser.add_argument(
        "--extra-license-file",
        action="append",
        default=[],
        metavar="DISTRIBUTION=PATH",
        help="copy a pinned upstream notice omitted by an installed wheel",
    )
    args = parser.parse_args()
    extras: list[tuple[str, Path]] = []
    for value in args.extra_license_file:
        name, separator, path = value.partition("=")
        if not separator or not name or not path:
            parser.error("--extra-license-file must be DISTRIBUTION=PATH")
        extras.append((name, Path(path)))
    collect(
        args.destination,
        importlib.metadata.distributions(),
        required_license_files=args.require_license_file,
        extra_license_files=extras,
    )


if __name__ == "__main__":
    main()
