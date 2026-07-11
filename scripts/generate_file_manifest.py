#!/usr/bin/env python3
"""Hash every regular file in a complete Portable Comfy directory."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: generate_file_manifest.py PORTABLE_ROOT")
    root = Path(sys.argv[1]).resolve()
    output = root / "manifest" / "files.json"
    records = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink() or path == output:
            continue
        value = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                value.update(block)
        records.append(
            {
                "path": path.relative_to(root).as_posix(),
                "sha256": value.hexdigest(),
                "size": path.stat().st_size,
            }
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps({"schema_version": 1, "files": records}, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
