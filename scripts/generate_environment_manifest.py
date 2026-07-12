#!/usr/bin/env python3
"""Generate the deterministic manifest for one atomic ComfyUI environment."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
from datetime import datetime, timezone
from pathlib import Path


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def created_at() -> str:
    epoch = int(os.environ.get("SOURCE_DATE_EPOCH", "0"))
    return (
        datetime.fromtimestamp(epoch, timezone.utc).isoformat().replace("+00:00", "Z")
    )


def contained_symlink(root: Path, path: Path) -> tuple[str, Path]:
    target = os.readlink(path)
    if not target or "\\" in target or "\x00" in target or os.path.isabs(target):
        raise SystemExit(f"unsafe links are forbidden in environment bundles: {path}")
    resolved = (path.parent / target).resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError as error:
        raise SystemExit(
            f"link escapes environment payload: {path} -> {target}"
        ) from error
    if not resolved.exists():
        raise SystemExit(
            f"link target is missing in environment payload: {path} -> {target}"
        )
    return target, resolved


def payload_entries(root: Path) -> list[dict[str, object]]:
    comfyui = root / "ComfyUI"
    if not comfyui.is_dir():
        raise SystemExit(f"missing environment payload: {comfyui}")
    resolved_root = comfyui.resolve()
    result: list[dict[str, object]] = []
    for path in sorted(comfyui.rglob("*")):
        mode = path.lstat().st_mode
        relative = path.relative_to(root).as_posix()
        if "\\" in relative or "\x00" in relative:
            raise SystemExit(f"unsafe environment payload path: {path}")
        if stat.S_ISLNK(mode):
            target, _ = contained_symlink(resolved_root, path)
            result.append({"path": relative, "type": "symlink", "target": target})
        elif stat.S_ISREG(mode):
            result.append(
                {
                    "path": relative,
                    "type": "file",
                    "sha256": digest(path),
                    "size": path.stat().st_size,
                }
            )
        elif not stat.S_ISDIR(mode):
            raise SystemExit(
                f"special files are forbidden in environment bundles: {path}"
            )
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("--output", default="manifest/environment.json")
    parser.add_argument("--checksums", default="manifest/environment-checksums.sha256")
    parser.add_argument("--generation-id", required=True)
    parser.add_argument("--core-version", required=True)
    parser.add_argument("--core-tag", required=True)
    parser.add_argument("--core-commit", required=True)
    parser.add_argument("--frontend-version", required=True)
    parser.add_argument("--frontend-commit", required=True)
    parser.add_argument("--python", required=True)
    parser.add_argument("--torch", required=True)
    parser.add_argument("--torchvision", required=True)
    parser.add_argument("--torchaudio", required=True)
    parser.add_argument("--cuda", required=True)
    parser.add_argument("--platform", default="linux-x86_64")
    parser.add_argument(
        "--requirements-lock-path", default="ComfyUI/runtime/requirements.lock"
    )
    parser.add_argument("--requirements-lock-sha256", required=True)
    args = parser.parse_args()

    root = args.root.resolve()
    files = payload_entries(root)
    manifest = {
        "schema_version": 2,
        "bundle_type": "environment",
        "app_id": "portable-comfy",
        "generation_id": args.generation_id,
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
            "torchvision": args.torchvision,
            "torchaudio": args.torchaudio,
            "cuda": args.cuda,
            "platform": args.platform,
            "requirements_lock_path": args.requirements_lock_path,
            "requirements_lock_sha256": args.requirements_lock_sha256,
        },
        "files": files,
    }
    output = root / args.output
    checksums = root / args.checksums
    output.parent.mkdir(parents=True, exist_ok=True)
    checksums.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    checksums.write_text(
        "".join(
            f"{item['sha256']}  {item['path']}\n"
            for item in files
            if item["type"] == "file"
        ),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
