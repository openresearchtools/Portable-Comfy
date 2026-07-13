#!/usr/bin/env python3
"""Inventory every source recorded in a PyInstaller COLLECT TOC.

The resulting ledgers make the provenance of the frozen launcher auditable.
Absolute paths must belong to one of the build's known source trees or to an
installed Debian package.  Debian-owned inputs bring their exact package
version, copyright notice and every referenced Debian common-license text into
the AppImage. Inputs from portable Python's managed native closure additionally
must match its checksum/package inventory and bring that complete notice tree.
"""

from __future__ import annotations

import argparse
import ast
import csv
import hashlib
import json
import os
import re
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
COMMON_LICENSE_FIELDS = (
    "debian_package",
    "referenced_name",
    "resolved_name",
    "license_text",
    "sha256",
    "size",
)
FORMAT_VERSION = "portable-comfy-launcher-native-license-inventory-v1"
COMMON_LICENSE_PREFIX = "/usr/share/common-licenses/"
COMMON_LICENSE_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9.+_-]*")
COMMON_LICENSE_TERMINATORS = frozenset(".,;:!?)]}>\"'`’”")
COMMON_LICENSE_ALIASES = {
    "GPL-1.0": "GPL-1",
    "GPL-2.0": "GPL-2",
    "GPL-3.0": "GPL-3",
    "LGPL-2.0": "LGPL-2",
    "LGPL-3.0": "LGPL-3",
}
PYTHON_NATIVE_SCHEMA_VERSION = 1
PYTHON_NATIVE_DIRECTORY = Path("lib/portable-native")
PYTHON_NATIVE_LICENSE_REFERENCE = "../python-native/packages.json"
PYTHON_NATIVE_SONAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+~-]*")
SHA256_RE = re.compile(r"[0-9a-f]{64}")
PYTHON_NATIVE_COMMON_LICENSE_RE = re.compile(
    rb"(?:/usr/share/)?common-licenses/([A-Za-z0-9][A-Za-z0-9.+_-]*)"
)
PYTHON_NATIVE_COMMON_LICENSE_BRACE_RE = re.compile(
    rb"(?:/usr/share/)?common-licenses/\{([^}\r\n]+)\}"
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


@dataclass(frozen=True)
class CommonLicense:
    """One Debian common-license reference and its resolved source text."""

    referenced_name: str
    resolved_name: str
    source: Path


@dataclass(frozen=True)
class PythonNativeLibrary:
    """One checksum-bound shared library in the portable Python closure."""

    path: str
    sha256: str
    size: int
    soname: str
    package: str
    version: str


@dataclass(frozen=True)
class PythonNativeInventory:
    """Validated Python-native notices and their indexed library records."""

    root: Path
    libraries: dict[str, PythonNativeLibrary]


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
    if not re.fullmatch(
        r"[a-z0-9][a-z0-9+.-]*(?::[A-Za-z0-9][A-Za-z0-9_-]*)?", package
    ):
        raise RuntimeError(f"unsafe Debian package name: {package!r}")
    value = package.replace(":", "_").replace("/", "_")
    if not value or value in {".", ".."}:
        raise RuntimeError(f"unsafe Debian package name: {package!r}")
    return value


def _safe_common_license_name(name: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.+_-]*", name):
        raise RuntimeError(f"unsafe Debian common-license name: {name!r}")
    return name


def _common_license_tokens(copyright_path: Path) -> tuple[str, ...]:
    """Return every syntactically safe common-license reference token."""

    try:
        text = copyright_path.read_text(encoding="utf-8", errors="strict")
    except (OSError, UnicodeError) as error:
        raise RuntimeError(
            f"cannot read Debian copyright as UTF-8: {copyright_path}"
        ) from error

    tokens: set[str] = set()
    offset = 0
    while True:
        offset = text.find(COMMON_LICENSE_PREFIX, offset)
        if offset < 0:
            break
        reference_start = offset + len(COMMON_LICENSE_PREFIX)
        if reference_start < len(text) and text[reference_start] == "{":
            reference_end = text.find("}", reference_start + 1)
            if reference_end < 0:
                raise RuntimeError(
                    "malformed Debian common-license brace reference in "
                    f"{copyright_path}"
                )
            names = text[reference_start + 1 : reference_end].split(",")
            if not names or any(
                not COMMON_LICENSE_NAME_RE.fullmatch(name) for name in names
            ):
                raise RuntimeError(
                    f"unsafe Debian common-license brace reference in {copyright_path}"
                )
            reference_finish = reference_end + 1
        else:
            match = COMMON_LICENSE_NAME_RE.match(text, reference_start)
            if match is None:
                raise RuntimeError(
                    f"malformed Debian common-license reference in {copyright_path}"
                )
            names = [match.group(0)]
            reference_finish = match.end()
        if reference_finish < len(text):
            following = text[reference_finish]
            if not following.isspace() and following not in COMMON_LICENSE_TERMINATORS:
                raise RuntimeError(
                    f"unsafe Debian common-license reference in {copyright_path}"
                )
        tokens.update(_safe_common_license_name(name) for name in names)
        offset = reference_finish
    return tuple(sorted(tokens))


def _resolve_common_license(token: str, directory: Path) -> CommonLicense:
    """Resolve punctuation and Debian's historical ``-N.0`` aliases safely."""

    root = directory.resolve(strict=True)
    if not root.is_dir():
        raise RuntimeError(f"Debian common-license directory is missing: {directory}")

    candidates = [token]
    without_sentence_period = token.rstrip(".")
    if without_sentence_period and without_sentence_period != token:
        candidates.append(without_sentence_period)

    referenced_name = ""
    source: Path | None = None
    for candidate in candidates:
        candidate = _safe_common_license_name(candidate)
        candidate_path = root / candidate
        if candidate_path.exists():
            referenced_name = candidate
            source = candidate_path
            break
    if not referenced_name:
        referenced_name = _safe_common_license_name(without_sentence_period)
        resolved_alias = COMMON_LICENSE_ALIASES.get(referenced_name)
        if resolved_alias:
            source = root / resolved_alias
    if source is None or not source.exists():
        raise RuntimeError(
            "referenced Debian common license is unavailable: "
            f"{COMMON_LICENSE_PREFIX}{referenced_name or token}"
        )

    resolved_source = source.resolve(strict=True)
    if resolved_source.parent != root or not resolved_source.is_file():
        raise RuntimeError(
            "Debian common-license reference escapes its source directory: "
            f"{source} -> {resolved_source}"
        )
    if resolved_source.stat().st_size == 0:
        raise RuntimeError(f"Debian common-license text is empty: {resolved_source}")
    return CommonLicense(
        referenced_name=referenced_name,
        resolved_name=_safe_common_license_name(resolved_source.name),
        source=resolved_source,
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _python_native_file(root: Path, value: object) -> Path:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise RuntimeError("Python-native inventory contains an unsafe path")
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts or relative == Path("."):
        raise RuntimeError("Python-native inventory contains an unsafe path")
    path = root / relative
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise RuntimeError(
            f"Python-native inventory file is missing: {value}"
        ) from error
    if path.is_symlink() or not resolved.is_file() or not _is_within(resolved, root):
        raise RuntimeError(f"Python-native inventory file is unsafe: {value}")
    return resolved


def _validate_python_native_bound_file(
    root: Path, record: object, expected_files: set[Path]
) -> Path:
    if not isinstance(record, dict):
        raise RuntimeError("Python-native inventory contains a malformed file record")
    path = _python_native_file(root, record.get("path"))
    if path in expected_files:
        raise RuntimeError(f"duplicate Python-native inventory path: {path}")
    if (
        type(record.get("size")) is not int
        or record["size"] <= 0
        or record["size"] != path.stat().st_size
        or not isinstance(record.get("sha256"), str)
        or not SHA256_RE.fullmatch(record["sha256"])
        or record["sha256"] != _sha256(path)
    ):
        raise RuntimeError(f"Python-native notice checksum mismatch: {path}")
    expected_files.add(path)
    return path


def _python_native_referenced_common_licenses(
    notice_paths: set[Path], available_names: set[str]
) -> set[str]:
    referenced: set[str] = set()

    def add(raw_name: bytes) -> None:
        try:
            name = raw_name.decode("ascii").strip()
        except UnicodeError as error:
            raise RuntimeError(
                "Python-native notice has a malformed common-license reference"
            ) from error
        while name not in available_names and name and name[-1] in ".,;:+":
            name = name[:-1]
        if not PYTHON_NATIVE_SONAME_RE.fullmatch(name) or name not in available_names:
            raise RuntimeError(
                f"Python-native notice has an unmapped common license: {name}"
            )
        referenced.add(name)

    for path in notice_paths:
        content = path.read_bytes()
        for raw_name in PYTHON_NATIVE_COMMON_LICENSE_RE.findall(content):
            add(raw_name)
        for brace_list in PYTHON_NATIVE_COMMON_LICENSE_BRACE_RE.findall(content):
            for raw_name in brace_list.split(b","):
                add(raw_name)
    return referenced


def load_python_native_inventory(license_root: Path) -> PythonNativeInventory:
    """Validate and index the complete Python-native notice inventory."""

    try:
        root = license_root.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise RuntimeError(
            f"Python-native license root is missing: {license_root}"
        ) from error
    if not root.is_dir():
        raise RuntimeError(f"Python-native license root is not a directory: {root}")
    for path in root.rglob("*"):
        if path.is_symlink():
            raise RuntimeError(f"Python-native license tree contains a symlink: {path}")
        if not path.is_file() and not path.is_dir():
            raise RuntimeError(
                f"Python-native license tree contains a special file: {path}"
            )

    inventory_path = root / "packages.json"
    try:
        value = json.loads(inventory_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise RuntimeError("invalid Python-native dependency inventory") from error
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != PYTHON_NATIVE_SCHEMA_VERSION
        or value.get("platform") != "linux-x86_64"
        or value.get("native_directory") != PYTHON_NATIVE_DIRECTORY.as_posix()
        or not isinstance(value.get("host_abi"), dict)
        or not isinstance(value.get("packages"), list)
        or not isinstance(value.get("common_licenses"), list)
        or not isinstance(value.get("libraries"), list)
    ):
        raise RuntimeError("unsupported Python-native dependency inventory schema")
    host_abi = value["host_abi"]
    if set(host_abi) != {"driver", "glibc_kernel", "interpreters"} or any(
        not isinstance(entries, list)
        or any(not isinstance(entry, str) or not entry for entry in entries)
        or entries != sorted(set(entries))
        for entries in host_abi.values()
    ):
        raise RuntimeError("Python-native host ABI inventory is malformed")

    expected_files: set[Path] = {inventory_path.resolve(strict=True)}
    packages: dict[str, str] = {}
    package_order: list[str] = []
    for record in value["packages"]:
        if not isinstance(record, dict):
            raise RuntimeError("Python-native package inventory is malformed")
        package = record.get("package")
        version = record.get("version")
        if not isinstance(package, str):
            raise RuntimeError("Python-native package name is malformed")
        _safe_package_directory(package)
        if (
            package in packages
            or not isinstance(version, str)
            or not version
            or any(character in version for character in "\t\r\n")
            or not all(
                isinstance(record.get(field), str) and record[field]
                for field in ("architecture", "source_package", "source_version")
            )
            or not isinstance(record.get("notices"), list)
            or not record["notices"]
        ):
            raise RuntimeError(
                f"Python-native package identity is malformed: {package}"
            )
        packages[package] = version
        package_order.append(package)
        notice_paths: list[str] = []
        for notice in record["notices"]:
            path = _validate_python_native_bound_file(root, notice, expected_files)
            relative = path.relative_to(root).as_posix()
            if not relative.startswith("packages/"):
                raise RuntimeError(
                    f"Python-native package notice has the wrong path: {relative}"
                )
            notice_paths.append(relative)
        if notice_paths != sorted(notice_paths):
            raise RuntimeError(f"Python-native notices are not sorted: {package}")
    if package_order != sorted(package_order):
        raise RuntimeError("Python-native package inventory is not sorted")

    common_names: list[str] = []
    for record in value["common_licenses"]:
        if not isinstance(record, dict):
            raise RuntimeError("Python-native common-license inventory is malformed")
        name = record.get("name")
        if (
            not isinstance(name, str)
            or not PYTHON_NATIVE_SONAME_RE.fullmatch(name)
            or name in common_names
            or record.get("path") != f"common-licenses/{name}"
        ):
            raise RuntimeError("Python-native common-license identity is malformed")
        _validate_python_native_bound_file(root, record, expected_files)
        common_names.append(name)
    if common_names != sorted(common_names):
        raise RuntimeError("Python-native common-license inventory is not sorted")
    referenced_common = _python_native_referenced_common_licenses(
        {path for path in expected_files if _is_within(path, root / "packages")},
        set(common_names),
    )
    if referenced_common != set(common_names):
        raise RuntimeError("Python-native common-license coverage is incomplete")

    libraries: dict[str, PythonNativeLibrary] = {}
    soname_order: list[str] = []
    for record in value["libraries"]:
        if not isinstance(record, dict):
            raise RuntimeError("Python-native library inventory is malformed")
        soname = record.get("soname")
        package = record.get("debian_package")
        path = record.get("path")
        digest = record.get("sha256")
        size = record.get("size")
        if (
            not isinstance(soname, str)
            or not PYTHON_NATIVE_SONAME_RE.fullmatch(soname)
            or not isinstance(package, str)
            or package not in packages
            or path != (PYTHON_NATIVE_DIRECTORY / soname).as_posix()
            or path in libraries
            or not isinstance(digest, str)
            or not SHA256_RE.fullmatch(digest)
            or type(size) is not int
            or size <= 0
            or not isinstance(record.get("source_path"), str)
            or not str(record["source_path"]).startswith("/")
            or not isinstance(record.get("source_sha256"), str)
            or not SHA256_RE.fullmatch(str(record["source_sha256"]))
            or type(record.get("source_size")) is not int
            or int(record["source_size"]) <= 0
        ):
            raise RuntimeError(f"Python-native library record is malformed: {soname}")
        libraries[path] = PythonNativeLibrary(
            path=path,
            sha256=digest,
            size=size,
            soname=soname,
            package=package,
            version=packages[package],
        )
        soname_order.append(soname)
    if soname_order != sorted(soname_order):
        raise RuntimeError("Python-native library inventory is not sorted")

    readme = value.get("readme")
    readme_path = _validate_python_native_bound_file(root, readme, expected_files)
    if readme_path.relative_to(root).as_posix() != "README.md":
        raise RuntimeError("Python-native README path is malformed")
    if value.get("summary") != {
        "common_licenses": len(common_names),
        "libraries": len(libraries),
        "packages": len(packages),
    }:
        raise RuntimeError("Python-native dependency inventory summary is inconsistent")

    actual_files = {
        path.resolve(strict=True) for path in root.rglob("*") if path.is_file()
    }
    if actual_files != expected_files:
        raise RuntimeError("Python-native dependency notice coverage is incomplete")
    return PythonNativeInventory(root=root, libraries=libraries)


def _portable_python_native_library(
    source: Path,
    portable_python: Path,
    inventory: PythonNativeInventory | None,
) -> PythonNativeLibrary | None:
    """Match a portable-native source exactly, failing closed on ambiguity."""

    portable_root = portable_python.resolve(strict=True)
    native_root = portable_root / PYTHON_NATIVE_DIRECTORY
    lexical = Path(os.path.normpath(os.fspath(source)))
    resolved = _resolved(source)
    lexical_inside = _is_within(lexical, native_root)
    resolved_inside = _is_within(resolved, native_root)
    if not lexical_inside and not resolved_inside:
        return None
    if not lexical_inside or not resolved_inside:
        raise RuntimeError(
            f"portable Python native source crosses its managed root: {source}"
        )
    lexical_relative = lexical.relative_to(portable_root)
    resolved_relative = resolved.relative_to(portable_root)
    if lexical_relative != resolved_relative or source.is_symlink():
        raise RuntimeError(
            f"portable Python native source is not a regular file: {source}"
        )
    if inventory is None:
        raise RuntimeError(
            "portable Python native source has no license inventory: "
            f"{lexical_relative.as_posix()}"
        )
    relative = lexical_relative.as_posix()
    library = inventory.libraries.get(relative)
    if library is None:
        raise RuntimeError(f"unlisted portable Python native source: {relative}")
    if source.stat().st_size != library.size or _sha256(source) != library.sha256:
        raise RuntimeError(
            f"portable Python native source checksum mismatch: {relative}"
        )
    return library


def _write_checksum_manifest(destination: Path) -> Path:
    manifest = destination / "SHA256SUMS"
    paths = sorted(
        (
            path
            for path in destination.rglob("*")
            if path.is_file() and path != manifest
        ),
        key=lambda path: path.relative_to(destination).as_posix(),
    )
    manifest.write_text(
        "".join(
            f"{_sha256(path)}  {path.relative_to(destination).as_posix()}\n"
            for path in paths
        ),
        encoding="utf-8",
    )
    return manifest


def _read_tsv(path: Path, expected_fields: tuple[str, ...]) -> list[dict[str, str]]:
    try:
        with path.open(encoding="utf-8", newline="") as stream:
            reader = csv.DictReader(stream, delimiter="\t")
            if tuple(reader.fieldnames or ()) != expected_fields:
                raise RuntimeError(f"unexpected fields in {path}")
            rows = list(reader)
            if any(
                None in row or any(value is None for value in row.values())
                for row in rows
            ):
                raise RuntimeError(f"malformed row in {path}")
            return rows
    except OSError as error:
        raise RuntimeError(f"cannot read native license inventory: {path}") from error


def _path_has_portable_native_component(value: str) -> bool:
    parts = Path(value).parts
    marker = PYTHON_NATIVE_DIRECTORY.parts
    return any(
        parts[index : index + len(marker)] == marker
        for index in range(len(parts) - len(marker) + 1)
    )


def _path_ends_with(value: str, relative: str) -> bool:
    parts = Path(value).parts
    expected = Path(relative).parts
    return len(parts) >= len(expected) and parts[-len(expected) :] == expected


def verify_inventory(
    destination: Path, python_native_license_root: Path | None = None
) -> None:
    """Verify inventory structure, common-license coverage and every checksum."""

    root = destination.resolve(strict=True)
    if not root.is_dir():
        raise RuntimeError(f"native license inventory is not a directory: {root}")
    for path in root.rglob("*"):
        if path.is_symlink():
            raise RuntimeError(f"native license inventory contains a symlink: {path}")
        if not path.is_file() and not path.is_dir():
            raise RuntimeError(f"native license inventory has a special file: {path}")

    format_path = root / "FORMAT"
    if format_path.read_text(encoding="utf-8") != f"{FORMAT_VERSION}\n":
        raise RuntimeError("unsupported launcher native license inventory format")

    provenance_rows = _read_tsv(root / "provenance.tsv", PROVENANCE_FIELDS)
    package_rows = _read_tsv(root / "packages.tsv", PACKAGE_FIELDS)
    common_rows = _read_tsv(root / "common-licenses.tsv", COMMON_LICENSE_FIELDS)
    python_native_inventory = (
        load_python_native_inventory(python_native_license_root)
        if python_native_license_root is not None
        else None
    )
    if provenance_rows != sorted(
        provenance_rows,
        key=lambda row: (row["destination"], row["origin"], row["source"]),
    ):
        raise RuntimeError("frozen source provenance inventory is not sorted")
    if package_rows != sorted(package_rows, key=lambda row: row["debian_package"]):
        raise RuntimeError("Debian package inventory is not sorted")
    if common_rows != sorted(
        common_rows,
        key=lambda row: (row["debian_package"], row["referenced_name"]),
    ):
        raise RuntimeError("Debian common-license inventory is not sorted")

    for row in provenance_rows:
        classification = row["classification"]
        if classification == "portable-python-native":
            if python_native_inventory is None:
                raise RuntimeError(
                    "portable Python native provenance has no license inventory"
                )
            reference_prefix = f"{PYTHON_NATIVE_LICENSE_REFERENCE}#"
            license_reference = row["license_reference"]
            if not license_reference.startswith(reference_prefix):
                raise RuntimeError(
                    "portable Python native provenance has an invalid license reference"
                )
            library_path = license_reference.removeprefix(reference_prefix)
            library = python_native_inventory.libraries.get(library_path)
            if library is None:
                raise RuntimeError(
                    f"portable Python native provenance is unlisted: {library_path}"
                )
            if (
                row["debian_package"] != library.package
                or row["version"] != library.version
                or not _path_ends_with(row["source"], library.path)
                or not _path_ends_with(row["resolved_source"], library.path)
            ):
                raise RuntimeError(
                    f"portable Python native provenance mismatch: {library_path}"
                )
        elif classification == "portable-python" and (
            _path_has_portable_native_component(row["source"])
            or _path_has_portable_native_component(row["resolved_source"])
        ):
            raise RuntimeError(
                "portable Python native source was classified as CPython-only"
            )

    packages: dict[str, dict[str, str]] = {}
    for row in package_rows:
        package = row["debian_package"]
        package_dir = _safe_package_directory(package)
        if package in packages:
            raise RuntimeError(f"duplicate Debian package inventory row: {package}")
        expected_copyright = f"{package_dir}/copyright"
        if row["copyright"] != expected_copyright:
            raise RuntimeError(f"unsafe Debian copyright path for {package}")
        copyright_path = root / expected_copyright
        if not copyright_path.is_file() or copyright_path.stat().st_size == 0:
            raise RuntimeError(f"missing Debian copyright text for {package}")
        try:
            if int(row["frozen_source_count"]) <= 0:
                raise ValueError
        except ValueError as error:
            raise RuntimeError(f"invalid frozen source count for {package}") from error
        if not row["version"] or any(c in row["version"] for c in "\t\r\n"):
            raise RuntimeError(f"invalid Debian package version for {package}")
        packages[package] = row

    provenance_counts: dict[str, int] = {}
    for row in provenance_rows:
        if row["classification"] != "debian-host-package":
            continue
        package = row["debian_package"]
        if package not in packages:
            raise RuntimeError(
                f"provenance references unknown Debian package: {package}"
            )
        if row["version"] != packages[package]["version"]:
            raise RuntimeError(f"Debian version mismatch in provenance: {package}")
        if row["license_reference"] != packages[package]["copyright"]:
            raise RuntimeError(f"Debian copyright mismatch in provenance: {package}")
        provenance_counts[package] = provenance_counts.get(package, 0) + 1
    for package, row in packages.items():
        if provenance_counts.get(package) != int(row["frozen_source_count"]):
            raise RuntimeError(f"frozen source count mismatch for {package}")

    ledger_references: dict[str, set[str]] = {package: set() for package in packages}
    common_files: set[str] = set()
    common_metadata: dict[str, tuple[str, str, str]] = {}
    for row in common_rows:
        package = row["debian_package"]
        if package not in packages:
            raise RuntimeError(
                f"common-license ledger references unknown package: {package}"
            )
        referenced_name = _safe_common_license_name(row["referenced_name"])
        resolved_name = _safe_common_license_name(row["resolved_name"])
        expected_path = f"common-licenses/{referenced_name}"
        if row["license_text"] != expected_path:
            raise RuntimeError(f"unsafe common-license path: {row['license_text']}")
        if referenced_name in ledger_references[package]:
            raise RuntimeError(
                f"duplicate common-license mapping: {package}/{referenced_name}"
            )
        license_path = root / expected_path
        if not license_path.is_file() or license_path.stat().st_size == 0:
            raise RuntimeError(f"missing common-license text: {expected_path}")
        digest = _sha256(license_path)
        size = str(license_path.stat().st_size)
        if row["sha256"] != digest or row["size"] != size:
            raise RuntimeError(f"common-license metadata mismatch: {expected_path}")
        metadata = (resolved_name, digest, size)
        expected_alias = COMMON_LICENSE_ALIASES.get(referenced_name)
        if expected_alias is not None and resolved_name != expected_alias:
            raise RuntimeError(
                f"incorrect Debian common-license alias: {referenced_name}"
            )
        if referenced_name in common_metadata:
            if common_metadata[referenced_name] != metadata:
                raise RuntimeError(
                    f"conflicting common-license metadata: {referenced_name}"
                )
        else:
            common_metadata[referenced_name] = metadata
        ledger_references[package].add(referenced_name)
        common_files.add(expected_path)

    for package, row in packages.items():
        copyright_path = root / row["copyright"]
        expected: set[str] = set()
        known = ledger_references[package]
        for token in _common_license_tokens(copyright_path):
            if token in known:
                expected.add(token)
            elif token.rstrip(".") in known:
                expected.add(token.rstrip("."))
            else:
                raise RuntimeError(
                    f"unmapped common-license reference for {package}: {token}"
                )
        if expected != known:
            raise RuntimeError(f"common-license coverage mismatch for {package}")

    actual_common_files = (
        {
            path.relative_to(root).as_posix()
            for path in (root / "common-licenses").glob("*")
            if path.is_file()
        }
        if (root / "common-licenses").is_dir()
        else set()
    )
    if actual_common_files != common_files:
        raise RuntimeError("untracked file in common-license mirror")

    fixed_files = {
        "FORMAT",
        "README.txt",
        "provenance.tsv",
        "packages.tsv",
        "common-licenses.tsv",
    }
    expected_files = {
        *fixed_files,
        *(row["copyright"] for row in package_rows),
        *common_files,
    }
    actual_without_manifest = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and path != root / "SHA256SUMS"
    }
    if actual_without_manifest != expected_files:
        raise RuntimeError("unexpected file in native license inventory")

    manifest_path = root / "SHA256SUMS"
    manifest_lines = manifest_path.read_text(encoding="utf-8").splitlines()
    manifest: dict[str, str] = {}
    for line in manifest_lines:
        digest, separator, relative = line.partition("  ")
        if not separator or not re.fullmatch(r"[0-9a-f]{64}", digest) or not relative:
            raise RuntimeError("invalid native license checksum manifest entry")
        relative_path = Path(relative)
        if relative_path.is_absolute() or ".." in relative_path.parts:
            raise RuntimeError("unsafe native license checksum manifest path")
        if relative in manifest:
            raise RuntimeError("duplicate native license checksum manifest path")
        manifest[relative] = digest
    if list(manifest) != sorted(manifest):
        raise RuntimeError("native license checksum manifest is not sorted")
    if set(manifest) != actual_without_manifest:
        raise RuntimeError("native license checksum manifest coverage mismatch")
    for relative, digest in manifest.items():
        if _sha256(root / relative) != digest:
            raise RuntimeError(f"native license checksum mismatch: {relative}")


def write_inventory(
    *,
    toc_path: Path,
    destination: Path,
    roots: SourceRoots,
    manual_sources: Iterable[FrozenSource] = (),
    owner_lookup: Callable[[Path], DebianOwner | None] = query_debian_owner,
    common_license_directory: Path = Path("/usr/share/common-licenses"),
    python_native_license_root: Path | None = None,
) -> tuple[Path, Path]:
    """Write complete provenance and Debian package ledgers."""

    sources = [*load_collect_toc(toc_path), *manual_sources]
    if not sources:
        raise RuntimeError("PyInstaller COLLECT TOC contains no sources")

    normalized_roots = SourceRoots(
        **{name: _resolved(path) for name, path in vars(roots).items()}
    )
    python_native_inventory = (
        load_python_native_inventory(python_native_license_root)
        if python_native_license_root is not None
        else None
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
            python_native_library = _portable_python_native_library(
                source_path,
                normalized_roots.portable_python,
                python_native_inventory,
            )
            if python_native_library is not None:
                row.update(
                    {
                        "classification": "portable-python-native",
                        "debian_package": python_native_library.package,
                        "version": python_native_library.version,
                        "license_reference": (
                            f"{PYTHON_NATIVE_LICENSE_REFERENCE}#"
                            f"{python_native_library.path}"
                        ),
                    }
                )
            elif (
                classified := _classification(source_path, normalized_roots)
            ) is not None:
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

    if destination.exists() and any(destination.iterdir()):
        raise RuntimeError(
            f"native license inventory destination is not empty: {destination}"
        )
    destination.mkdir(parents=True, exist_ok=True)
    (destination / "FORMAT").write_text(f"{FORMAT_VERSION}\n", encoding="utf-8")
    (destination / "README.txt").write_text(
        "Portable Comfy launcher native dependency notices\n"
        "==================================================\n\n"
        "Debian package copyright files are copied byte-for-byte into each\n"
        "package directory. Any /usr/share/common-licenses reference in those\n"
        "files is mirrored under common-licenses/ and mapped by\n"
        "common-licenses.tsv, including Debian's historical -N.0 aliases.\n"
        "SHA256SUMS covers every other file in this directory.\n",
        encoding="utf-8",
    )
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
    common_rows: list[dict[str, str | int]] = []
    copied_common_licenses: dict[str, CommonLicense] = {}
    for package, (owner, count) in sorted(packages.items()):
        target = destination / _safe_package_directory(package) / "copyright"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(owner.copyright, target)
        package_common_licenses: dict[str, CommonLicense] = {}
        for token in _common_license_tokens(target):
            common_license = _resolve_common_license(token, common_license_directory)
            existing_license = copied_common_licenses.get(
                common_license.referenced_name
            )
            if existing_license is not None and existing_license != common_license:
                raise RuntimeError(
                    "conflicting Debian common-license resolution: "
                    f"{common_license.referenced_name}"
                )
            common_target = (
                destination / "common-licenses" / common_license.referenced_name
            )
            if existing_license is None:
                common_target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(common_license.source, common_target)
                copied_common_licenses[common_license.referenced_name] = common_license
            existing_package_license = package_common_licenses.get(
                common_license.referenced_name
            )
            if (
                existing_package_license is not None
                and existing_package_license != common_license
            ):
                raise RuntimeError(
                    "conflicting package common-license resolution: "
                    f"{package}/{common_license.referenced_name}"
                )
            package_common_licenses[common_license.referenced_name] = common_license
        for common_license in package_common_licenses.values():
            common_target = (
                destination / "common-licenses" / common_license.referenced_name
            )
            common_rows.append(
                {
                    "debian_package": package,
                    "referenced_name": common_license.referenced_name,
                    "resolved_name": common_license.resolved_name,
                    "license_text": common_target.relative_to(destination).as_posix(),
                    "sha256": _sha256(common_target),
                    "size": common_target.stat().st_size,
                }
            )
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
    common_path = destination / "common-licenses.tsv"
    with common_path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(
            stream, fieldnames=COMMON_LICENSE_FIELDS, delimiter="\t"
        )
        writer.writeheader()
        writer.writerows(
            sorted(
                common_rows,
                key=lambda row: (row["debian_package"], row["referenced_name"]),
            )
        )
    _write_checksum_manifest(destination)
    verify_inventory(destination, python_native_license_root)
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
    parser.add_argument("--verify", type=Path)
    parser.add_argument("--toc", type=Path)
    parser.add_argument("--destination", type=Path)
    parser.add_argument("--launcher-venv", type=Path)
    parser.add_argument("--portable-python", type=Path)
    parser.add_argument("--pyinstaller-work", type=Path)
    parser.add_argument("--build-root", type=Path)
    parser.add_argument("--repository", type=Path)
    parser.add_argument("--python-native-license-root", type=Path)
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
    if args.verify is not None:
        build_values = (
            args.toc,
            args.destination,
            args.launcher_venv,
            args.portable_python,
            args.pyinstaller_work,
            args.build_root,
            args.repository,
        )
        if any(value is not None for value in build_values) or args.manual_source:
            raise SystemExit("--verify cannot be combined with inventory build options")
        verify_inventory(args.verify, args.python_native_license_root)
        return 0
    required = {
        "--toc": args.toc,
        "--destination": args.destination,
        "--launcher-venv": args.launcher_venv,
        "--portable-python": args.portable_python,
        "--pyinstaller-work": args.pyinstaller_work,
        "--build-root": args.build_root,
        "--repository": args.repository,
    }
    missing = [flag for flag, value in required.items() if value is None]
    if missing:
        raise SystemExit(f"missing required inventory options: {', '.join(missing)}")
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
        python_native_license_root=args.python_native_license_root,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
