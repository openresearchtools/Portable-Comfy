#!/usr/bin/env python3
"""Inventory every source recorded in a PyInstaller COLLECT TOC.

The resulting ledgers make the provenance of the frozen launcher auditable.
Absolute paths must belong to one of the build's known source trees or to an
installed Debian package.  Debian-owned inputs bring their exact package
version and copyright notice into the AppImage.
"""

from __future__ import annotations

import argparse
import ast
import csv
import os
import shutil
import subprocess
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path


PROVENANCE_FIELDS = (
    "origin",
    "destination",
    "typecode",
    "source",
    "resolved_source",
    "classification",
    "debian_package",
    "version",
    "license_reference",
)
PACKAGE_FIELDS = (
    "debian_package",
    "version",
    "copyright",
    "frozen_source_count",
)


@dataclass(frozen=True)
class DebianOwner:
    """Installed Debian package which owns one frozen source file."""

    package: str
    version: str
    copyright: Path


@dataclass(frozen=True)
class SourceRoots:
    """Known non-host source trees used to create the launcher."""

    launcher_venv: Path
    portable_python: Path
    pyinstaller_work: Path
    build_root: Path
    repository: Path


@dataclass(frozen=True)
class FrozenSource:
    """One input copied or referenced by the frozen launcher."""

    origin: str
    destination: str
    typecode: str
    source: str


def _resolved(path: Path) -> Path:
    return path.expanduser().resolve(strict=True)


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def load_collect_toc(
    toc_path: Path, *, contents_directory: str = "_internal"
) -> list[FrozenSource]:
    """Load PyInstaller's literal COLLECT TOC and map final destinations."""

    try:
        saved = ast.literal_eval(toc_path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, ValueError) as error:
        raise RuntimeError(
            f"cannot read PyInstaller COLLECT TOC: {toc_path}"
        ) from error
    if (
        not isinstance(saved, tuple)
        or len(saved) != 1
        or not isinstance(saved[0], list)
    ):
        raise RuntimeError(f"unexpected PyInstaller COLLECT TOC structure: {toc_path}")

    sources: list[FrozenSource] = []
    for item in saved[0]:
        if (
            not isinstance(item, tuple)
            or len(item) != 3
            or not all(isinstance(value, str) for value in item)
        ):
            raise RuntimeError(f"invalid PyInstaller COLLECT TOC entry: {item!r}")
        destination, source, typecode = item
        if os.path.isabs(destination) or ".." in Path(destination).parts:
            raise RuntimeError(
                f"unsafe PyInstaller destination in COLLECT TOC: {destination}"
            )
        if typecode in {"EXECUTABLE", "PKG"}:
            bundle_destination = destination
        else:
            bundle_destination = f"{contents_directory}/{destination}"
        sources.append(
            FrozenSource(
                origin="pyinstaller",
                destination=bundle_destination,
                typecode=typecode,
                source=source,
            )
        )
    return sources


def query_debian_owner(source: Path) -> DebianOwner | None:
    """Return the installed Debian owner of *source*, if one exists."""

    candidates = [source.resolve(strict=True)]
    if source != candidates[0]:
        candidates.append(source)
    owner = ""
    for candidate in candidates:
        completed = subprocess.run(
            ["dpkg-query", "--search", os.fspath(candidate)],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        if completed.returncode == 0 and completed.stdout.strip():
            owner = completed.stdout.splitlines()[0].split(": ", 1)[0]
            break
    if not owner:
        return None

    version = subprocess.run(
        ["dpkg-query", "--show", "--showformat=${Version}", owner],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    ).stdout
    package_name = owner.split(":", 1)[0]
    copyright_path = Path("/usr/share/doc") / package_name / "copyright"
    if not copyright_path.is_file() or copyright_path.stat().st_size == 0:
        raise RuntimeError(
            f"Debian copyright notice is missing for frozen package: {owner}"
        )
    return DebianOwner(
        package=owner,
        version=version,
        copyright=copyright_path.resolve(strict=True),
    )


def _classification(source: Path, roots: SourceRoots) -> tuple[str, str] | None:
    lexical = Path(os.path.normpath(os.fspath(source)))
    resolved = _resolved(source)
    candidates = (
        (
            "launcher-venv",
            roots.launcher_venv,
            "../python-packages/packages.json",
        ),
        ("portable-python", roots.portable_python, "../cpython/LICENSE.txt"),
        (
            "pyinstaller-build",
            roots.pyinstaller_work,
            "../portable-comfy/LICENSE;../cpython/LICENSE.txt;"
            "../python-packages/packages.json",
        ),
        (
            "build-generated",
            roots.build_root,
            "../portable-comfy/LICENSE;../cpython/LICENSE.txt;"
            "../python-packages/packages.json",
        ),
        ("project-source", roots.repository, "../portable-comfy/LICENSE"),
    )
    for classification, root, notice in candidates:
        normalized_root = root.resolve(strict=True)
        if _is_within(lexical, normalized_root) or _is_within(
            resolved, normalized_root
        ):
            return classification, notice
    return None


def _safe_package_directory(package: str) -> str:
    value = package.replace(":", "_").replace("/", "_")
    if not value or value in {".", ".."}:
        raise RuntimeError(f"unsafe Debian package name: {package!r}")
    return value


def write_inventory(
    *,
    toc_path: Path,
    destination: Path,
    roots: SourceRoots,
    manual_sources: Iterable[FrozenSource] = (),
    owner_lookup: Callable[[Path], DebianOwner | None] = query_debian_owner,
) -> tuple[Path, Path]:
    """Write complete provenance and Debian package ledgers."""

    sources = [*load_collect_toc(toc_path), *manual_sources]
    if not sources:
        raise RuntimeError("PyInstaller COLLECT TOC contains no sources")

    normalized_roots = SourceRoots(
        **{name: _resolved(path) for name, path in vars(roots).items()}
    )
    rows: list[dict[str, str]] = []
    packages: dict[str, tuple[DebianOwner, int]] = {}
    for entry in sources:
        source_path = Path(entry.source)
        row = {
            "origin": entry.origin,
            "destination": entry.destination,
            "typecode": entry.typecode,
            "source": entry.source,
            "resolved_source": "",
            "classification": "relative-reference",
            "debian_package": "",
            "version": "",
            "license_reference": "not-applicable",
        }
        if source_path.is_absolute():
            resolved_source = _resolved(source_path)
            row["resolved_source"] = os.fspath(resolved_source)
            classified = _classification(source_path, normalized_roots)
            if classified is not None:
                row["classification"], row["license_reference"] = classified
            else:
                owner = owner_lookup(source_path)
                if owner is None:
                    raise RuntimeError(
                        "unclassified absolute PyInstaller source: "
                        f"{entry.source} -> {resolved_source}"
                    )
                package_dir = _safe_package_directory(owner.package)
                copyright_target = f"{package_dir}/copyright"
                row.update(
                    {
                        "classification": "debian-host-package",
                        "debian_package": owner.package,
                        "version": owner.version,
                        "license_reference": copyright_target,
                    }
                )
                existing = packages.get(owner.package)
                if existing is not None and existing[0] != owner:
                    raise RuntimeError(
                        f"conflicting Debian ownership metadata for {owner.package}"
                    )
                packages[owner.package] = (owner, (existing[1] if existing else 0) + 1)
        rows.append(row)

    destination.mkdir(parents=True, exist_ok=True)
    provenance_path = destination / "provenance.tsv"
    with provenance_path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=PROVENANCE_FIELDS, delimiter="\t")
        writer.writeheader()
        writer.writerows(
            sorted(
                rows,
                key=lambda row: (
                    row["destination"],
                    row["origin"],
                    row["source"],
                ),
            )
        )

    package_rows: list[dict[str, str | int]] = []
    for package, (owner, count) in sorted(packages.items()):
        target = destination / _safe_package_directory(package) / "copyright"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(owner.copyright, target)
        package_rows.append(
            {
                "debian_package": package,
                "version": owner.version,
                "copyright": target.relative_to(destination).as_posix(),
                "frozen_source_count": count,
            }
        )

    package_path = destination / "packages.tsv"
    with package_path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=PACKAGE_FIELDS, delimiter="\t")
        writer.writeheader()
        writer.writerows(package_rows)
    return provenance_path, package_path


def _manual_source(value: str) -> FrozenSource:
    try:
        destination, source = value.split("=", 1)
    except ValueError as error:
        raise argparse.ArgumentTypeError(
            "manual source must be DESTINATION=SOURCE"
        ) from error
    if not destination or not source or not os.path.isabs(source):
        raise argparse.ArgumentTypeError(
            "manual source must have a non-empty destination and absolute source"
        )
    return FrozenSource(
        origin="manual",
        destination=destination,
        typecode="BINARY",
        source=source,
    )


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--toc", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--launcher-venv", type=Path, required=True)
    parser.add_argument("--portable-python", type=Path, required=True)
    parser.add_argument("--pyinstaller-work", type=Path, required=True)
    parser.add_argument("--build-root", type=Path, required=True)
    parser.add_argument("--repository", type=Path, required=True)
    parser.add_argument(
        "--manual-source",
        type=_manual_source,
        action="append",
        default=[],
        help="additional frozen input as DESTINATION=ABSOLUTE_SOURCE",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    write_inventory(
        toc_path=args.toc,
        destination=args.destination,
        roots=SourceRoots(
            launcher_venv=args.launcher_venv,
            portable_python=args.portable_python,
            pyinstaller_work=args.pyinstaller_work,
            build_root=args.build_root,
            repository=args.repository,
        ),
        manual_sources=args.manual_source,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
