#!/usr/bin/env python3
"""Strictly validate a staged Portable Comfy core-update bundle."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path, PurePosixPath


HEX = re.compile(r"^[0-9a-f]{64}$")


def sha256(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def fail(message: str) -> None:
    raise SystemExit(f"invalid core bundle: {message}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("--manifest", default="manifest.json")
    parser.add_argument("--checksums", default="checksums.sha256")
    args = parser.parse_args()
    root = args.root.resolve()
    manifest_path = root / args.manifest
    checksums_path = root / args.checksums
    if not manifest_path.is_file() or not checksums_path.is_file():
        fail("manifest.json or checksums.sha256 is missing")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != 1:
        fail("unsupported schema_version")
    if (
        manifest.get("bundle_type") != "core"
        or manifest.get("app_id") != "portable-comfy"
    ):
        fail("wrong bundle identity")
    for group in ("core", "frontend", "runtime"):
        if not isinstance(manifest.get(group), dict):
            fail(f"missing {group} object")
    expected: dict[str, tuple[str, int]] = {}
    for item in manifest.get("files", []):
        if not isinstance(item, dict):
            fail("non-object files entry")
        path_text = item.get("path")
        digest = item.get("sha256")
        size = item.get("size")
        if (
            not isinstance(path_text, str)
            or not isinstance(digest, str)
            or not isinstance(size, int)
        ):
            fail("malformed files entry")
        posix = PurePosixPath(path_text)
        if (
            posix.is_absolute()
            or ".." in posix.parts
            or not posix.parts
            or posix.parts[0] != "ComfyUI"
        ):
            fail(f"unsafe update path: {path_text}")
        if path_text in expected or not HEX.fullmatch(digest):
            fail(f"duplicate path or malformed digest: {path_text}")
        expected[path_text] = (digest, size)
    actual: set[str] = set()
    for path in (root / "ComfyUI").rglob("*"):
        if path.is_symlink():
            fail(f"links are forbidden: {path.relative_to(root)}")
        if path.is_file():
            relative = path.relative_to(root).as_posix()
            actual.add(relative)
            if relative not in expected:
                fail(f"unlisted file: {relative}")
            wanted_hash, wanted_size = expected[relative]
            if path.stat().st_size != wanted_size or sha256(path) != wanted_hash:
                fail(f"checksum or size mismatch: {relative}")
    if actual != set(expected):
        fail(f"manifest references missing files: {sorted(set(expected) - actual)[:3]}")
    checksum_entries: dict[str, str] = {}
    for line in checksums_path.read_text(encoding="utf-8").splitlines():
        digest, separator, path_text = line.partition("  ")
        if (
            separator != "  "
            or not HEX.fullmatch(digest)
            or path_text in checksum_entries
        ):
            fail("malformed checksums.sha256")
        checksum_entries[path_text] = digest
    if checksum_entries != {path: value[0] for path, value in expected.items()}:
        fail("checksums.sha256 disagrees with manifest.json")
    if (
        not (root / "ComfyUI/main.py").is_file()
        or not (root / "ComfyUI/frontend/index.html").is_file()
    ):
        fail("required Core/frontend entrypoints are missing")
    print(f"verified {len(actual)} ComfyUI files")


if __name__ == "__main__":
    main()
