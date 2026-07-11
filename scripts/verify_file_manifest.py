#!/usr/bin/env python3
"""Verify every regular file recorded in a complete portable payload."""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path, PurePosixPath


HEX = re.compile(r"^[0-9a-f]{64}$")


def fail(message: str) -> None:
    raise SystemExit(f"invalid full-file manifest: {message}")


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def main() -> None:
    if len(sys.argv) != 2:
        fail("usage: verify_file_manifest.py PORTABLE_ROOT")
    root = Path(sys.argv[1]).resolve()
    manifest_path = root / "manifest/files.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        fail(str(error))
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        fail("unsupported schema_version")
    entries = manifest.get("files")
    if not isinstance(entries, list) or not entries:
        fail("files list is missing or empty")
    expected: dict[str, tuple[str, int]] = {}
    for item in entries:
        if not isinstance(item, dict):
            fail("file entry is not an object")
        name, checksum, size = item.get("path"), item.get("sha256"), item.get("size")
        if (
            not isinstance(name, str)
            or not isinstance(checksum, str)
            or not isinstance(size, int)
        ):
            fail("file entry is malformed")
        path = PurePosixPath(name)
        if (
            path.is_absolute()
            or ".." in path.parts
            or not path.parts
            or name == "manifest/files.json"
            or name in expected
            or not HEX.fullmatch(checksum)
            or size < 0
        ):
            fail(f"unsafe, duplicate, or malformed entry: {name}")
        expected[name] = (checksum, size)

    actual: set[str] = set()
    for path in root.rglob("*"):
        if not path.is_file() or path.is_symlink() or path == manifest_path:
            continue
        name = path.relative_to(root).as_posix()
        actual.add(name)
        if name not in expected:
            fail(f"unlisted file: {name}")
        checksum, size = expected[name]
        if path.stat().st_size != size or digest(path) != checksum:
            fail(f"checksum or size mismatch: {name}")
    missing = set(expected) - actual
    if missing:
        fail(f"manifest references missing file: {sorted(missing)[0]}")
    print(f"verified {len(actual)} complete-payload files")


if __name__ == "__main__":
    main()
