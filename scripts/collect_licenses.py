#!/usr/bin/env python3
"""Collect wheel license files and compact package metadata for redistribution."""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import shutil
from pathlib import Path


def safe_name(value: str) -> str:
    return "".join(
        character if character.isalnum() or character in ".-_" else "_"
        for character in value
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    destination = args.destination.resolve()
    destination.mkdir(parents=True, exist_ok=True)
    notices: list[dict[str, object]] = []
    for distribution in sorted(
        importlib.metadata.distributions(),
        key=lambda item: item.metadata["Name"].lower(),
    ):
        name = distribution.metadata["Name"] or "unknown"
        version = distribution.version
        entry = {
            "name": name,
            "version": version,
            "license": distribution.metadata.get("License"),
            "home_page": distribution.metadata.get("Home-page"),
            "license_files": [],
        }
        package_dir = destination / f"{safe_name(name)}-{safe_name(version)}"
        for file in distribution.files or []:
            parts = tuple(part.lower() for part in file.parts)
            basename = file.name.lower()
            if not (
                "licenses" in parts
                or basename.startswith(("license", "copying", "notice", "authors"))
            ):
                continue
            source = Path(distribution.locate_file(file))
            if not source.is_file():
                continue
            package_dir.mkdir(parents=True, exist_ok=True)
            target = package_dir / safe_name("-".join(file.parts[-2:]))
            shutil.copyfile(source, target)
            entry["license_files"].append(target.relative_to(destination).as_posix())
        notices.append(entry)
    (destination / "packages.json").write_text(
        json.dumps(notices, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    main()
