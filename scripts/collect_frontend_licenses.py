#!/usr/bin/env python3
"""Materialize a relocatable notice inventory from ``pnpm licenses`` JSON."""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import re
import shutil
from pathlib import Path, PurePosixPath
from typing import Any


NOTICE_NAME = re.compile(
    r"^(?:licen[cs]e|copying|notice|copyright|authors?)(?:[._-].*)?$",
    re.IGNORECASE,
)
MAX_NOTICE_BYTES = 16 * 1024 * 1024
SHA256 = re.compile(r"^[0-9a-f]{64}$")
SPECIAL_NOTICE_NAMES = {
    "@iconify/json": {"collections.json", "collections.md", "readme.md"},
    "@iconify-json/lucide": {"info.json"},
}


def fail(message: str) -> None:
    raise SystemExit(f"invalid frontend dependency notices: {message}")


def load_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        fail(f"cannot read {path}: {error}")
    if not isinstance(value, dict):
        fail(f"{path} is not a JSON object")
    return value


def text_metadata(value: object) -> str | list[str] | dict[str, object] | None:
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return value
    if isinstance(value, dict) and all(
        isinstance(key, str)
        and (item is None or isinstance(item, (str, bool, int, float)))
        for key, item in value.items()
    ):
        return value
    return str(value)


def package_directory(name: str, version: str) -> str:
    label = re.sub(r"[^A-Za-z0-9._-]+", "_", f"{name}-{version}").strip("._-")
    suffix = hashlib.sha256(f"{name}\0{version}".encode()).hexdigest()[:10]
    return f"{label[:100] or 'package'}-{suffix}"


def notice_candidates(package_root: Path, package_name: str) -> list[Path]:
    result = []
    for path in package_root.iterdir():
        if (
            path.is_file()
            and not path.is_symlink()
            and (
                NOTICE_NAME.fullmatch(path.name)
                or path.name.casefold() in SPECIAL_NOTICE_NAMES.get(package_name, set())
            )
        ):
            size = path.stat().st_size
            if not 0 < size <= MAX_NOTICE_BYTES:
                fail(f"notice has an invalid size: {path}")
            result.append(path)
    return sorted(result, key=lambda path: path.name.casefold())


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("inventory", type=Path)
    parser.add_argument("source_root", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--frontend-version", required=True)
    parser.add_argument("--frontend-commit", required=True)
    parser.add_argument(
        "--fallback-license",
        action="append",
        default=[],
        metavar="GLOB=PATH",
    )
    parser.add_argument("--workspace-license", type=Path)
    parser.add_argument("--compiled-root", type=Path)
    parser.add_argument(
        "--additional-package",
        action="append",
        nargs=4,
        default=[],
        metavar=("NAME", "VERSION", "LICENSE", "LICENSE_FILE"),
        help="record a dependency embedded in compiled output but absent from pnpm --prod",
    )
    parser.add_argument(
        "--additional-asset",
        action="append",
        nargs=4,
        default=[],
        metavar=("NAME", "VERSION", "RELATIVE_PATH", "SHA256"),
        help="bind an additional package to an exact compiled frontend asset",
    )
    args = parser.parse_args()

    source_root = args.source_root.resolve()
    inventory = load_object(args.inventory)
    packages: dict[tuple[str, str], dict[str, object]] = {}
    for license_expression, values in inventory.items():
        if not isinstance(license_expression, str) or not license_expression.strip():
            fail("inventory contains an empty license expression")
        if not isinstance(values, list) or not values:
            fail(f"license group is empty: {license_expression}")
        for value in values:
            if not isinstance(value, dict):
                fail(f"license group contains a non-object: {license_expression}")
            name = value.get("name")
            versions = value.get("versions")
            paths = value.get("paths")
            if (
                not isinstance(name, str)
                or not name
                or not isinstance(versions, list)
                or not versions
                or not all(isinstance(item, str) and item for item in versions)
                or not isinstance(paths, list)
                or not paths
                or not all(isinstance(item, str) and item for item in paths)
            ):
                fail(f"malformed package entry in {license_expression}")
            for raw_path in paths:
                package_root = Path(raw_path).resolve()
                if not package_root.is_relative_to(source_root):
                    fail(f"package path escapes the pinned frontend source: {raw_path}")
                metadata = load_object(package_root / "package.json")
                version = metadata.get("version")
                if metadata.get("name") != name or version not in versions:
                    fail(f"pnpm inventory disagrees with {package_root}/package.json")
                assert isinstance(version, str)
                key = (name, version)
                record = {
                    "name": name,
                    "version": version,
                    "license": license_expression,
                    "author": text_metadata(
                        metadata.get("author", value.get("author"))
                    ),
                    "homepage": text_metadata(
                        metadata.get("homepage", value.get("homepage"))
                    ),
                    "repository": text_metadata(metadata.get("repository")),
                    "description": text_metadata(
                        metadata.get("description", value.get("description"))
                    ),
                    "source_roots": [],
                }
                existing = packages.setdefault(key, record)
                if (
                    existing["license"] != license_expression
                    or existing["name"] != name
                    or existing["version"] != version
                ):
                    fail(f"conflicting package metadata: {name}=={version}")
                roots = existing["source_roots"]
                assert isinstance(roots, list)
                if package_root not in roots:
                    roots.append(package_root)

    workspace_license_expression: str | None = None
    if args.workspace_license is not None:
        workspace_license = args.workspace_license.resolve()
        if not workspace_license.is_file() or not workspace_license.stat().st_size:
            fail("workspace license is missing or empty")
        root_metadata = load_object(source_root / "package.json")
        root_license = root_metadata.get("license")
        if not isinstance(root_license, str) or not root_license:
            fail("pinned frontend source has no root license expression")
        workspace_license_expression = root_license
        workspace: dict[str, tuple[Path, dict[str, Any]]] = {}
        for metadata_path in sorted((source_root / "packages").glob("*/package.json")):
            metadata = load_object(metadata_path)
            name = metadata.get("name")
            if isinstance(name, str) and name:
                workspace[name] = (metadata_path.parent, metadata)
        dependencies = root_metadata.get("dependencies")
        if not isinstance(dependencies, dict):
            fail("pinned frontend source has no production dependencies")
        pending = [name for name in dependencies if name in workspace]
        included: set[str] = set()
        while pending:
            name = pending.pop()
            if name in included:
                continue
            included.add(name)
            package_root, metadata = workspace[name]
            version = metadata.get("version")
            if not isinstance(version, str) or not version:
                fail(f"workspace package has no version: {name}")
            expression = metadata.get("license") or root_license
            if not isinstance(expression, str) or not expression:
                fail(f"workspace package has no license expression: {name}")
            key = (name, version)
            record = {
                "name": name,
                "version": version,
                "license": expression,
                "author": text_metadata(metadata.get("author")),
                "homepage": text_metadata(metadata.get("homepage")),
                "repository": text_metadata(metadata.get("repository")),
                "description": text_metadata(metadata.get("description")),
                "source_roots": [package_root],
            }
            existing = packages.setdefault(key, record)
            roots = existing["source_roots"]
            assert isinstance(roots, list)
            if package_root not in roots:
                roots.append(package_root)
            nested = metadata.get("dependencies")
            if isinstance(nested, dict):
                pending.extend(item for item in nested if item in workspace)

    additional_keys: set[tuple[str, str]] = set()
    for name, version, expression, raw_license in args.additional_package:
        license_path = Path(raw_license).resolve()
        if (
            not name
            or not version
            or any(character.isspace() for character in version)
            or not expression.strip()
            or not license_path.is_file()
            or license_path.is_symlink()
            or not 0 < license_path.stat().st_size <= MAX_NOTICE_BYTES
        ):
            fail(f"invalid additional package: {name} {version}")
        key = (name, version)
        if key in packages or key in additional_keys:
            fail(f"duplicate additional package: {name}=={version}")
        additional_keys.add(key)
        packages[key] = {
            "name": name,
            "version": version,
            "license": expression,
            "author": None,
            "homepage": None,
            "repository": None,
            "description": "Dependency embedded in the pinned compiled frontend",
            "source_roots": [],
            "explicit_notices": [license_path],
            "bundled_assets": [],
        }

    compiled_root = (
        args.compiled_root.resolve() if args.compiled_root is not None else None
    )
    for name, version, relative_text, expected_digest in args.additional_asset:
        key = (name, version)
        if key not in additional_keys:
            fail(f"additional asset has no matching package: {name}=={version}")
        if compiled_root is None:
            fail("--compiled-root is required with --additional-asset")
        relative = PurePosixPath(relative_text)
        if (
            relative.is_absolute()
            or not relative.parts
            or ".." in relative.parts
            or "\\" in relative_text
            or "\x00" in relative_text
            or relative.as_posix() != relative_text
            or not SHA256.fullmatch(expected_digest)
        ):
            fail(f"invalid additional asset path or digest: {relative_text}")
        asset = compiled_root.joinpath(*relative.parts)
        if (
            not asset.is_file()
            or asset.is_symlink()
            or not asset.resolve().is_relative_to(compiled_root)
        ):
            fail(f"additional compiled asset is missing or unsafe: {relative_text}")
        contents = asset.read_bytes()
        if hashlib.sha256(contents).hexdigest() != expected_digest:
            fail(f"additional compiled asset checksum changed: {relative_text}")
        bundled_assets = packages[key]["bundled_assets"]
        assert isinstance(bundled_assets, list)
        if any(item["path"] == relative_text for item in bundled_assets):
            fail(f"duplicate additional compiled asset: {relative_text}")
        bundled_assets.append(
            {
                "path": relative_text,
                "sha256": expected_digest,
                "size": len(contents),
            }
        )
    for key in additional_keys:
        if not packages[key]["bundled_assets"]:
            fail(f"additional package has no bound compiled asset: {key[0]}=={key[1]}")

    fallbacks: list[tuple[str, Path]] = []
    for value in args.fallback_license:
        selector, separator, raw_path = value.partition("=")
        path = Path(raw_path).resolve()
        if (
            separator != "="
            or not selector
            or not raw_path
            or not path.is_file()
            or not path.stat().st_size
        ):
            fail(f"invalid fallback license: {value}")
        fallbacks.append((selector, path))
    workspace_fallback = (
        args.workspace_license.resolve() if args.workspace_license is not None else None
    )

    destination = args.destination.resolve()
    temporary = destination.with_name(f".{destination.name}.tmp")
    shutil.rmtree(temporary, ignore_errors=True)
    temporary.mkdir(parents=True)
    output_records = []
    try:
        for (name, version), value in sorted(packages.items()):
            directory = temporary / package_directory(name, version)
            directory.mkdir()
            roots = value.pop("source_roots")
            assert isinstance(roots, list)
            explicit_notices = value.pop("explicit_notices", [])
            assert isinstance(explicit_notices, list)
            notices: dict[str, Path] = {}
            for root in roots:
                assert isinstance(root, Path)
                for notice in notice_candidates(root, name):
                    digest = hashlib.sha256(notice.read_bytes()).hexdigest()
                    notices.setdefault(digest, notice)
            for notice in explicit_notices:
                assert isinstance(notice, Path)
                digest = hashlib.sha256(notice.read_bytes()).hexdigest()
                notices.setdefault(digest, notice)
            for selector, notice in fallbacks:
                if fnmatch.fnmatchcase(name, selector):
                    digest = hashlib.sha256(notice.read_bytes()).hexdigest()
                    notices.setdefault(digest, notice)
            if (
                not notices
                and workspace_fallback is not None
                and name.startswith("@comfyorg/")
                and value["license"] == workspace_license_expression
            ):
                digest = hashlib.sha256(workspace_fallback.read_bytes()).hexdigest()
                notices.setdefault(digest, workspace_fallback)
            notice_files = []
            for index, (digest, source) in enumerate(sorted(notices.items()), start=1):
                safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", source.name)
                target = directory / f"{index:02d}-{digest[:12]}-{safe_name}"
                shutil.copyfile(source, target)
                notice_files.append(target.relative_to(temporary).as_posix())
            metadata_path = directory / "PACKAGE-METADATA.json"
            metadata_value = {**value, "notice_files": notice_files}
            metadata_path.write_text(
                json.dumps(metadata_value, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            output_records.append(
                {
                    **value,
                    "metadata_file": metadata_path.relative_to(temporary).as_posix(),
                    "notice_files": notice_files,
                }
            )

        with_notices = sum(bool(item["notice_files"]) for item in output_records)
        index = {
            "schema_version": 1,
            "frontend": {
                "version": args.frontend_version,
                "commit": args.frontend_commit,
            },
            "packages": output_records,
            "summary": {
                "distributions": len(output_records),
                "with_notice_files": with_notices,
                "metadata_only": len(output_records) - with_notices,
            },
        }
        (temporary / "packages.json").write_text(
            json.dumps(index, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        shutil.rmtree(destination, ignore_errors=True)
        temporary.replace(destination)
    finally:
        shutil.rmtree(temporary, ignore_errors=True)

    print(
        f"collected {len(output_records)} frontend dependency records "
        f"({with_notices} with packaged notice files)"
    )


if __name__ == "__main__":
    main()
