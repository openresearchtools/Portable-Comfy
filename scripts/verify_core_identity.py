#!/usr/bin/env python3
"""Verify that a ComfyUI source snapshot matches the selected version set."""

from __future__ import annotations

import argparse
import ast
import re
from pathlib import Path


FRONTEND_NAME = "comfyui-frontend-package"


def requirement_name(requirement: str) -> str:
    candidate = re.split(r"[<>=!~;\[ @]", requirement, maxsplit=1)[0]
    return re.sub(r"[-_.]+", "-", candidate).lower()


def read_core_version(path: Path) -> str:
    try:
        module = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, UnicodeError, SyntaxError) as error:
        raise SystemExit(f"cannot read pinned Core version: {error}") from error
    versions: list[str] = []
    for statement in module.body:
        if not isinstance(statement, ast.Assign) or not isinstance(
            statement.value, ast.Constant
        ):
            continue
        if not isinstance(statement.value.value, str):
            continue
        if any(
            isinstance(target, ast.Name) and target.id == "__version__"
            for target in statement.targets
        ):
            versions.append(statement.value.value)
    if len(versions) != 1:
        raise SystemExit(
            "ComfyUI Core version mismatch: comfyui_version.py must define one "
            "literal __version__"
        )
    return versions[0]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("core_root", type=Path)
    parser.add_argument("core_version")
    parser.add_argument("frontend_version")
    args = parser.parse_args()

    source_version = read_core_version(args.core_root / "comfyui_version.py")
    if source_version != args.core_version:
        raise SystemExit(
            "ComfyUI Core version mismatch: "
            f"expected {args.core_version!r}, source reports {source_version!r}"
        )

    try:
        lines = (
            (args.core_root / "requirements.txt")
            .read_text(encoding="utf-8")
            .splitlines()
        )
    except (OSError, UnicodeError) as error:
        raise SystemExit(f"cannot read pinned Core requirements: {error}") from error

    matches: list[str] = []
    for raw in lines:
        requirement = raw.split("#", maxsplit=1)[0].strip()
        if requirement and requirement_name(requirement) == FRONTEND_NAME:
            matches.append(requirement)

    expected = f"{FRONTEND_NAME}=={args.frontend_version}"
    if matches != [expected]:
        found = ", ".join(matches) if matches else "no frontend requirement"
        raise SystemExit(
            "ComfyUI Core/frontend version mismatch: "
            f"expected exactly {expected!r}, found {found}"
        )

    print(f"verified ComfyUI Core {source_version} with frontend pin {expected}")


if __name__ == "__main__":
    main()
