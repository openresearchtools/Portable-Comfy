#!/usr/bin/env python3
"""Generate deterministic Portable Comfy manifests and SHA-256 lists."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def regular_files(root: Path, subtree: str) -> list[dict[str, object]]:
    target = root / subtree
    if not target.is_dir():
        raise SystemExit(f"missing manifest subtree: {target}")
    result: list[dict[str, object]] = []
    for path in sorted(target.rglob("*")):
        if path.is_symlink():
            raise SystemExit(f"links are forbidden in update bundles: {path}")
        if path.is_file():
            relative = path.relative_to(root).as_posix()
            result.append(
                {"path": relative, "sha256": digest(path), "size": path.stat().st_size}
            )
    return result


def created_at() -> str:
    epoch = int(os.environ.get("SOURCE_DATE_EPOCH", "0"))
    return (
        datetime.fromtimestamp(epoch, timezone.utc).isoformat().replace("+00:00", "Z")
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("--subtree", default="ComfyUI")
    parser.add_argument("--output", default="manifest.json")
    parser.add_argument("--checksums", default="checksums.sha256")
    parser.add_argument("--core-version", required=True)
    parser.add_argument("--core-tag", required=True)
    parser.add_argument("--core-commit", required=True)
    parser.add_argument("--frontend-version", required=True)
    parser.add_argument("--frontend-commit", required=True)
    parser.add_argument("--python", required=True)
    parser.add_argument("--torch", required=True)
    parser.add_argument("--cuda", required=True)
    parser.add_argument("--platform", default="linux-x86_64")
    parser.add_argument("--requirements-lock-sha256", required=True)
    args = parser.parse_args()

    root = args.root.resolve()
    files = regular_files(root, args.subtree)
    manifest = {
        "schema_version": 1,
        "bundle_type": "core",
        "app_id": "portable-comfy",
        "created_at": created_at(),
        "core": {
            "version": args.core_version,
            "tag": args.core_tag,
            "commit": args.core_commit,
        },
        "frontend": {
            "version": args.frontend_version,
            "commit": args.frontend_commit,
        },
        "runtime": {
            "python": args.python,
            "torch": args.torch,
            "cuda": args.cuda,
            "platform": args.platform,
            "requirements_lock_sha256": args.requirements_lock_sha256,
        },
        "files": files,
    }
    output = root / args.output
    output.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    checksum_path = root / args.checksums
    checksum_path.write_text(
        "".join(f"{item['sha256']}  {item['path']}\n" for item in files),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
