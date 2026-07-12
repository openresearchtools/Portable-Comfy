#!/usr/bin/env python3
"""Verify that a ComfyUI source snapshot matches the selected version set."""

from __future__ import annotations

import argparse
import ast
from email.parser import Parser
import re
import subprocess
import time
import zipfile
from pathlib import Path
from pathlib import PurePosixPath


FRONTEND_NAME = "comfyui-frontend-package"
FRONTEND_METADATA_NAME = "comfyui_frontend_package"
CORE_REPOSITORY = "https://github.com/Comfy-Org/ComfyUI.git"
FRONTEND_REPOSITORY = "https://github.com/Comfy-Org/ComfyUI_frontend.git"
COMMIT = re.compile(r"^[0-9a-f]{40}$")


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


def verify_frontend_wheel(path: Path, expected_version: str) -> None:
    """Require the wheel's authoritative Core Metadata to match the pin."""

    expected_path = f"{FRONTEND_METADATA_NAME}-{expected_version}.dist-info/METADATA"
    try:
        with zipfile.ZipFile(path) as wheel:
            names = [entry.filename for entry in wheel.infolist()]
            if len(names) != len(set(names)):
                raise SystemExit("frontend wheel contains duplicate archive paths")
            metadata_paths = [
                name
                for name in names
                if PurePosixPath(name).name == "METADATA"
                and PurePosixPath(name).parent.name.endswith(".dist-info")
            ]
            if metadata_paths != [expected_path]:
                raise SystemExit(
                    "frontend wheel identity mismatch: expected exactly "
                    f"{expected_path!r}, found {metadata_paths!r}"
                )
            metadata_text = wheel.read(expected_path).decode("utf-8", errors="strict")
    except (OSError, UnicodeError, KeyError, zipfile.BadZipFile) as error:
        raise SystemExit(f"cannot read frontend wheel metadata: {error}") from error

    metadata = Parser().parsestr(metadata_text)
    names = metadata.get_all("Name", [])
    versions = metadata.get_all("Version", [])
    if names != [FRONTEND_METADATA_NAME] or versions != [expected_version]:
        raise SystemExit(
            "frontend wheel identity mismatch: "
            f"expected {FRONTEND_METADATA_NAME}=={expected_version}, "
            f"found Name={names!r}, Version={versions!r}"
        )


def resolve_remote_tag(output: str, tag: str) -> str:
    """Resolve either a lightweight tag or an annotated tag's peeled commit."""

    direct_ref = f"refs/tags/{tag}"
    peeled_ref = f"{direct_ref}^{{}}"
    references: dict[str, str] = {}
    for line in output.splitlines():
        commit, separator, reference = line.partition("\t")
        if (
            separator != "\t"
            or reference not in {direct_ref, peeled_ref}
            or not COMMIT.fullmatch(commit)
            or reference in references
        ):
            raise ValueError(f"malformed upstream tag response for {tag}: {line!r}")
        references[reference] = commit
    resolved = references.get(peeled_ref) or references.get(direct_ref)
    if resolved is None:
        raise ValueError(f"upstream tag is missing: {tag}")
    return resolved


def verify_upstream_tag(repository: str, tag: str, expected_commit: str) -> None:
    if not COMMIT.fullmatch(expected_commit):
        raise SystemExit(f"malformed pinned commit for {tag}: {expected_commit!r}")
    command = [
        "git",
        "ls-remote",
        "--tags",
        repository,
        f"refs/tags/{tag}",
        f"refs/tags/{tag}^{{}}",
    ]
    completed: subprocess.CompletedProcess[str] | None = None
    for attempt in range(3):
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if completed.returncode == 0:
            break
        if attempt < 2:
            time.sleep(attempt + 1)
    assert completed is not None
    if completed.returncode != 0:
        detail = completed.stderr.strip() or f"git exited {completed.returncode}"
        raise SystemExit(f"cannot resolve official upstream tag {tag}: {detail}")
    try:
        resolved = resolve_remote_tag(completed.stdout, tag)
    except ValueError as error:
        raise SystemExit(str(error)) from error
    if resolved != expected_commit:
        raise SystemExit(
            f"official upstream tag {tag} resolves to {resolved}, "
            f"not pinned commit {expected_commit}"
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("core_root", type=Path)
    parser.add_argument("core_version")
    parser.add_argument("frontend_version")
    parser.add_argument("--frontend-wheel", type=Path)
    parser.add_argument("--core-commit")
    parser.add_argument("--frontend-commit")
    parser.add_argument("--verify-upstream-tags", action="store_true")
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

    if args.frontend_wheel is not None:
        verify_frontend_wheel(args.frontend_wheel, args.frontend_version)

    if args.verify_upstream_tags:
        if args.core_commit is None or args.frontend_commit is None:
            raise SystemExit(
                "--verify-upstream-tags requires --core-commit and --frontend-commit"
            )
        verify_upstream_tag(CORE_REPOSITORY, f"v{args.core_version}", args.core_commit)
        verify_upstream_tag(
            FRONTEND_REPOSITORY,
            f"v{args.frontend_version}",
            args.frontend_commit,
        )

    print(f"verified ComfyUI Core {source_version} with frontend pin {expected}")


if __name__ == "__main__":
    main()
