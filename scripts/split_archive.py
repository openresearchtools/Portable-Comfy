#!/usr/bin/env python3
"""Split a complete Core archive into independently verifiable release files."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path


FORMAT = "portable-comfy-core-multipart"
SCHEMA_VERSION = 1
DEFAULT_PART_SIZE = 1_900_000_000
MAX_PART_SIZE = 1_900_000_000
MAX_ARCHIVE_SIZE = 10_000_000_000
MAX_PARTS = 64
BUFFER_SIZE = 8 * 1024 * 1024
CORE_ARCHIVE = re.compile(
    r"^Portable-Comfy-core-v[0-9]+(?:\.[0-9]+){2}"
    r"(?:[-+._]?[0-9A-Za-z][0-9A-Za-z._+-]{0,63})?\.tar\.gz$"
)


def fail(message: str) -> None:
    raise SystemExit(f"cannot split complete Core archive: {message}")


def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(BUFFER_SIZE), b""):
            result.update(block)
    return result.hexdigest()


def split_archive(
    archive: Path,
    output_dir: Path,
    *,
    part_size: int = DEFAULT_PART_SIZE,
    keep_archive: bool = False,
) -> Path:
    archive = archive.expanduser().resolve()
    output_dir = output_dir.expanduser().resolve()
    if not archive.is_file():
        fail(f"archive does not exist: {archive}")
    if not CORE_ARCHIVE.fullmatch(archive.name):
        fail("archive name must be Portable-Comfy-core-v<semantic-version>.tar.gz")
    archive_stat = archive.stat()
    if not 1 <= archive_stat.st_size <= MAX_ARCHIVE_SIZE:
        fail(f"archive size must be between 1 and {MAX_ARCHIVE_SIZE} bytes")
    if not isinstance(part_size, int) or isinstance(part_size, bool):
        fail("part size must be an integer")
    if not 1 <= part_size <= MAX_PART_SIZE:
        fail(f"part size must be between 1 and {MAX_PART_SIZE} bytes")
    required_parts = (archive_stat.st_size + part_size - 1) // part_size
    if required_parts > MAX_PARTS:
        fail(f"archive requires more than {MAX_PARTS} parts")

    output_dir.mkdir(parents=True, exist_ok=True)
    descriptor = output_dir / f"{archive.name}.parts.json"
    part_prefix = f"{archive.name}.part"
    temporary_paths: list[Path] = []
    final_paths: list[Path] = []
    parts: list[dict[str, object]] = []
    archive_hash = hashlib.sha256()
    archive_size = 0

    # Never allow a rerun with a shorter archive to leave a stale trailing part.
    for candidate in output_dir.iterdir():
        if (
            candidate.name.startswith(part_prefix)
            and candidate.name[len(part_prefix) :].isdigit()
        ):
            candidate.unlink()
    descriptor.unlink(missing_ok=True)
    descriptor_tmp = descriptor.with_name(f".{descriptor.name}.tmp-{os.getpid()}")
    descriptor_tmp.unlink(missing_ok=True)

    try:
        with archive.open("rb") as source:
            number = 1
            while True:
                filename = f"{archive.name}.part{number:04d}"
                final_path = output_dir / filename
                temporary = output_dir / f".{filename}.tmp-{os.getpid()}"
                temporary.unlink(missing_ok=True)
                part_hash = hashlib.sha256()
                written = 0
                with temporary.open("xb") as output:
                    while written < part_size:
                        block = source.read(min(BUFFER_SIZE, part_size - written))
                        if not block:
                            break
                        output.write(block)
                        part_hash.update(block)
                        archive_hash.update(block)
                        written += len(block)
                        archive_size += len(block)
                    output.flush()
                    os.fsync(output.fileno())
                if written == 0:
                    temporary.unlink()
                    break
                temporary_paths.append(temporary)
                final_paths.append(final_path)
                parts.append(
                    {
                        "number": number,
                        "filename": filename,
                        "size": written,
                        "sha256": part_hash.hexdigest(),
                    }
                )
                number += 1
        if not parts:
            fail("archive is empty")

        document = {
            "schema_version": SCHEMA_VERSION,
            "format": FORMAT,
            "archive": {
                "filename": archive.name,
                "size": archive_size,
                "sha256": archive_hash.hexdigest(),
            },
            "part_size": part_size,
            "parts": parts,
        }
        descriptor_tmp.write_text(
            json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        with descriptor_tmp.open("rb") as stream:
            os.fsync(stream.fileno())

        # Publish complete part files first and the descriptor last. A consumer
        # therefore never sees a descriptor for a partially published set.
        for temporary, final_path, expected in zip(
            temporary_paths, final_paths, parts, strict=True
        ):
            os.replace(temporary, final_path)
            if (
                final_path.stat().st_size != expected["size"]
                or digest(final_path) != expected["sha256"]
            ):
                fail(f"published part failed verification: {final_path.name}")
        os.replace(descriptor_tmp, descriptor)

        if not keep_archive:
            archive.unlink()
            archive.with_name(f"{archive.name}.sha256").unlink(missing_ok=True)
        return descriptor
    except BaseException:
        descriptor_tmp.unlink(missing_ok=True)
        for temporary in temporary_paths:
            temporary.unlink(missing_ok=True)
        raise


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="output directory (defaults to the archive directory)",
    )
    parser.add_argument(
        "--part-size",
        type=int,
        default=DEFAULT_PART_SIZE,
        help=f"maximum bytes per part (default and maximum: {MAX_PART_SIZE})",
    )
    parser.add_argument(
        "--keep-archive",
        action="store_true",
        help="retain the reconstructed .tar.gz beside its parts",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    output_dir = args.output_dir or args.archive.parent
    descriptor = split_archive(
        args.archive,
        output_dir,
        part_size=args.part_size,
        keep_archive=args.keep_archive,
    )
    print(descriptor)
    return 0


if __name__ == "__main__":
    sys.exit(main())
