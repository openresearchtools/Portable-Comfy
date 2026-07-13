#!/usr/bin/env python3
"""Bundle and verify the ELF dependency closure of the portable Python tree.

The environment deliberately keeps the glibc/kernel loader ABI and the two
NVIDIA driver entry points as host interfaces.  Every other ELF dependency
must resolve below the portable Python prefix.  Build mode copies dependencies
owned by Debian packages into ``lib/portable-native``, records their exact
package provenance/notices, and gives each consumer an ORIGIN-relative
RUNPATH.  Audit mode is read-only and is suitable for an extracted artifact.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
from collections import deque
from dataclasses import dataclass
from pathlib import Path


SCHEMA_VERSION = 1
NATIVE_SUBDIRECTORY = Path("lib/portable-native")

# These are interfaces supplied by the minimum supported Linux userspace, not
# opportunistic libraries inherited from the build runner.  Keep this list
# intentionally explicit: extending it weakens the portable-runtime contract.
GLIBC_HOST_ABI = frozenset(
    {
        "ld-linux-x86-64.so.2",
        "libanl.so.1",
        "libBrokenLocale.so.1",
        "libc.so.6",
        "libdl.so.2",
        "libm.so.6",
        "libpthread.so.0",
        "libresolv.so.2",
        "librt.so.1",
        "libthread_db.so.1",
        "libutil.so.1",
        "linux-vdso.so.1",
    }
)
DRIVER_HOST_ABI = frozenset({"libcuda.so.1", "libnvidia-ml.so.1"})
ALLOWED_INTERPRETERS = frozenset(
    {
        "/lib64/ld-linux-x86-64.so.2",
        "/lib/x86_64-linux-gnu/ld-linux-x86-64.so.2",
    }
)
NEEDED_PATTERN = re.compile(r"Shared library: \[([^]]+)]")
SONAME_PATTERN = re.compile(r"Library soname: \[([^]]+)]")
RPATH_PATTERN = re.compile(r"Library (?:rpath|runpath): \[([^]]*)]")
INTERPRETER_PATTERN = re.compile(r"Requesting program interpreter: \[([^]]+)]")
SAFE_SONAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+~-]*$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
COMMON_LICENSE_PATTERN = re.compile(
    rb"(?:/usr/share/)?common-licenses/([A-Za-z0-9][A-Za-z0-9.+_-]*)"
)
COMMON_LICENSE_BRACE_PATTERN = re.compile(
    rb"(?:/usr/share/)?common-licenses/\{([^}\r\n]+)\}"
)


class ClosureError(RuntimeError):
    """Raised when the runtime violates its declared native closure."""


@dataclass(frozen=True)
class DynamicInfo:
    needed: tuple[str, ...]
    rpath: tuple[str, ...]
    interpreter: str | None
    soname: str | None


@dataclass(frozen=True)
class InternalCandidate:
    target: Path
    directory: Path


@dataclass(frozen=True)
class DebianOwner:
    package: str
    version: str
    architecture: str
    source_package: str
    source_version: str


def run(
    command: list[str],
    *,
    check: bool = True,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        check=check,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def is_elf(path: Path) -> bool:
    if path.is_symlink() or not path.is_file():
        return False
    try:
        with path.open("rb") as stream:
            return stream.read(4) == b"\x7fELF"
    except OSError:
        return False


def elf_files(prefix: Path) -> list[Path]:
    return sorted(path for path in prefix.rglob("*") if is_elf(path))


def dynamic_info(path: Path) -> DynamicInfo | None:
    dynamic = run(["readelf", "-dW", str(path)], check=False)
    if dynamic.returncode != 0 or "There is no dynamic section" in dynamic.stdout:
        return None
    needed = tuple(NEEDED_PATTERN.findall(dynamic.stdout))
    soname_matches = SONAME_PATTERN.findall(dynamic.stdout)
    soname = soname_matches[0] if soname_matches else None
    rpaths: list[str] = []
    for value in RPATH_PATTERN.findall(dynamic.stdout):
        rpaths.extend(value.split(":"))
    program = run(["readelf", "-lW", str(path)], check=False)
    interpreter_matches = INTERPRETER_PATTERN.findall(program.stdout)
    interpreter = interpreter_matches[0] if interpreter_matches else None
    return DynamicInfo(
        needed=needed,
        rpath=tuple(rpaths),
        interpreter=interpreter,
        soname=soname,
    )


def clean_loader_environment() -> dict[str, str]:
    environment = dict(os.environ)
    for name in (
        "LD_AUDIT",
        "LD_DEBUG",
        "LD_LIBRARY_PATH",
        "LD_PRELOAD",
        "PYTHONHOME",
        "PYTHONPATH",
        "VIRTUAL_ENV",
    ):
        environment.pop(name, None)
    return environment


def ldd_resolutions(path: Path) -> tuple[dict[str, Path | None], str]:
    completed = run(["ldd", str(path)], check=False, env=clean_loader_environment())
    output = completed.stdout + completed.stderr
    # A static PIE has a program interpreter but no NEEDED entries.  It does
    # not participate in the closure and ldd commonly returns non-zero for it.
    resolutions: dict[str, Path | None] = {}
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if " => " not in line:
            continue
        name, target = line.split(" => ", 1)
        name = name.strip()
        target = target.strip()
        if target.startswith("not found"):
            resolutions[name] = None
            continue
        # ldd does not shell-quote paths; split the trailing load address from
        # the right so portable roots containing spaces remain intact.
        resolved = target.rsplit(" (", 1)[0]
        if resolved.startswith("/"):
            resolutions[name] = Path(resolved).resolve()
    return resolutions, output


def origin_entry(binary: Path, directory: Path) -> str:
    relative = os.path.relpath(directory, binary.parent)
    return "$ORIGIN" if relative == "." else f"$ORIGIN/{relative}"


def is_origin_rpath(value: str) -> bool:
    return (
        value == "$ORIGIN"
        or value.startswith("$ORIGIN/")
        or value == "${ORIGIN}"
        or value.startswith("${ORIGIN}/")
    )


def set_relative_rpaths(binary: Path, extra_directories: set[Path]) -> None:
    info = dynamic_info(binary)
    if info is None:
        return
    retained = [value for value in info.rpath if value and is_origin_rpath(value)]
    for directory in sorted(extra_directories):
        entry = origin_entry(binary, directory)
        if entry not in retained:
            retained.append(entry)
    if tuple(retained) == info.rpath:
        return
    if retained:
        run(["patchelf", "--set-rpath", ":".join(retained), str(binary)])
    else:
        run(["patchelf", "--remove-rpath", str(binary)])


def package_owner(path: Path) -> DebianOwner:
    candidates = [path.resolve(), path]
    package = ""
    for candidate in candidates:
        completed = run(["dpkg-query", "-S", str(candidate)], check=False)
        if completed.returncode != 0:
            continue
        for line in completed.stdout.splitlines():
            owner, separator, listed_path = line.partition(": ")
            if separator and Path(listed_path).resolve() == path.resolve():
                package = owner
                break
        if package:
            break
    if not package:
        raise ClosureError(
            f"native dependency is not owned by a Debian package: {path}"
        )
    fields = (
        run(
            [
                "dpkg-query",
                "-W",
                "-f=${binary:Package}\\t${Version}\\t${Architecture}\\t${source:Package}\\t${source:Version}\\n",
                package,
            ]
        )
        .stdout.rstrip("\n")
        .split("\t")
    )
    if len(fields) != 5 or not all(fields[:3]):
        raise ClosureError(f"cannot determine exact Debian identity for {package}")
    binary_package, version, architecture, source_package, source_version = fields
    source_package = source_package or binary_package.split(":", 1)[0]
    source_version = source_version or version
    return DebianOwner(
        package=binary_package,
        version=version,
        architecture=architecture,
        source_package=source_package,
        source_version=source_version,
    )


def safe_package_slug(owner: DebianOwner) -> str:
    value = f"{owner.package}_{owner.version}_{owner.architecture}"
    return re.sub(r"[^A-Za-z0-9._+-]", "_", value)


def package_notice_paths(package: str) -> list[Path]:
    completed = run(["dpkg-query", "-L", package])
    result: list[Path] = []
    for line in completed.stdout.splitlines():
        path = Path(line)
        lowered = path.name.lower()
        if not str(path).startswith("/usr/share/doc/"):
            continue
        if (
            lowered == "copyright"
            or lowered.startswith("license")
            or lowered.startswith("notice")
        ):
            if path.exists() and path.is_file():
                result.append(path)
    # Debian commonly makes a binary package's entire documentation directory
    # a symlink to a sibling package from the same source (ncurses is one
    # example). dpkg-query lists the symlink but not the target's children.
    doc_root = Path("/usr/share/doc") / package.split(":", 1)[0]
    if doc_root.exists() and doc_root.is_dir():
        for path in doc_root.iterdir():
            lowered = path.name.lower()
            if (
                lowered == "copyright"
                or lowered.startswith("license")
                or lowered.startswith("notice")
            ) and path.is_file():
                result.append(path)
    unique: dict[Path, None] = {}
    for path in result:
        unique[path.resolve()] = None
    if not unique:
        raise ClosureError(
            f"Debian package has no installed copyright/license notice: {package}"
        )
    return sorted(unique)


def copy_package_notices(
    license_root: Path, owners: dict[str, DebianOwner]
) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for package in sorted(owners):
        owner = owners[package]
        package_root = license_root / "packages" / safe_package_slug(owner)
        package_root.mkdir(parents=True, exist_ok=True)
        notices: list[dict[str, object]] = []
        for index, source in enumerate(package_notice_paths(package), start=1):
            filename = f"{index:02d}-{source.name}"
            destination = package_root / filename
            shutil.copyfile(source, destination)
            relative = destination.relative_to(license_root).as_posix()
            notices.append(
                {
                    "path": relative,
                    "sha256": sha256(destination),
                    "size": destination.stat().st_size,
                    "source_path": str(source),
                }
            )
        records.append(
            {
                "architecture": owner.architecture,
                "package": owner.package,
                "source_package": owner.source_package,
                "source_version": owner.source_version,
                "version": owner.version,
                "notices": notices,
            }
        )
    return records


def referenced_common_licenses(paths: set[Path]) -> set[str]:
    result: set[str] = set()
    common_root = Path("/usr/share/common-licenses")

    def add(raw_name: bytes) -> None:
        name = raw_name.decode("ascii").strip()
        # A full stop or comma immediately after a prose reference is not
        # part of the filename. Prefer an exact installed basename and peel
        # only trailing punctuation when necessary.
        while name and not (common_root / name).is_file() and name[-1] in ".,;:+":
            name = name[:-1]
        if not SAFE_SONAME.fullmatch(name) or not (common_root / name).is_file():
            raise ClosureError(
                f"Debian notice references an unavailable common license: {raw_name!r}"
            )
        result.add(name)

    for path in paths:
        content = path.read_bytes()
        for raw_name in COMMON_LICENSE_PATTERN.findall(content):
            add(raw_name)
        for brace_list in COMMON_LICENSE_BRACE_PATTERN.findall(content):
            for raw_name in brace_list.split(b","):
                add(raw_name)
    return result


def copy_common_licenses(
    license_root: Path, notice_paths: set[Path]
) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    output_root = license_root / "common-licenses"
    for name in sorted(referenced_common_licenses(notice_paths)):
        source = Path("/usr/share/common-licenses") / name
        output_root.mkdir(parents=True, exist_ok=True)
        destination = output_root / name
        shutil.copyfile(source, destination)
        records.append(
            {
                "name": name,
                "path": destination.relative_to(license_root).as_posix(),
                "sha256": sha256(destination),
                "size": destination.stat().st_size,
                "source_path": str(source),
            }
        )
    return records


def write_inventory(
    prefix: Path,
    license_root: Path,
    libraries: dict[str, tuple[Path, DebianOwner]],
) -> None:
    owners = {owner.package: owner for _, owner in libraries.values()}
    package_records = copy_package_notices(license_root, owners)
    notice_paths = {
        license_root / str(notice["path"])
        for package in package_records
        for notice in package["notices"]  # type: ignore[union-attr]
    }
    common_license_records = copy_common_licenses(license_root, notice_paths)
    library_records: list[dict[str, object]] = []
    for soname in sorted(libraries):
        source, owner = libraries[soname]
        destination = prefix / NATIVE_SUBDIRECTORY / soname
        library_records.append(
            {
                "debian_package": owner.package,
                "path": destination.relative_to(prefix).as_posix(),
                "sha256": sha256(destination),
                "size": destination.stat().st_size,
                "soname": soname,
                "source_path": str(source),
                "source_sha256": sha256(source),
                "source_size": source.stat().st_size,
            }
        )
    readme = license_root / "README.md"
    readme.write_text(
        "# Portable Python native dependencies\n\n"
        "`packages.json` binds each separately replaceable shared library to the "
        "exact Debian binary/source package version used by the Ubuntu 22.04 "
        "builder. The package directories contain the installed Debian "
        "copyright, license and notice files; every referenced Debian common-"
        "license text is copied under `common-licenses/` and checksum-bound in "
        "the inventory. These records do not replace any "
        "corresponding-source duty a downstream distributor may have.\n\n"
        "The build intentionally omits CPython's Tk, readline and gdbm modules; "
        "ComfyUI does not use them, and they would add desktop or GPL-licensed "
        "runtime libraries to this server-focused interpreter.\n",
        encoding="utf-8",
    )
    inventory = {
        "schema_version": SCHEMA_VERSION,
        "platform": "linux-x86_64",
        "native_directory": NATIVE_SUBDIRECTORY.as_posix(),
        "host_abi": {
            "driver": sorted(DRIVER_HOST_ABI),
            "glibc_kernel": sorted(GLIBC_HOST_ABI),
            "interpreters": sorted(ALLOWED_INTERPRETERS),
        },
        "common_licenses": common_license_records,
        "libraries": library_records,
        "packages": package_records,
        "readme": {
            "path": "README.md",
            "sha256": sha256(readme),
            "size": readme.stat().st_size,
        },
        "summary": {
            "common_licenses": len(common_license_records),
            "libraries": len(library_records),
            "packages": len(package_records),
        },
    }
    (license_root / "packages.json").write_text(
        json.dumps(inventory, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def index_prefix_libraries(prefix: Path) -> dict[str, list[InternalCandidate]]:
    indexed: dict[str, set[InternalCandidate]] = {}

    def add(name: str, target: Path, directory: Path) -> None:
        if not SAFE_SONAME.fullmatch(name):
            return
        target = target.resolve()
        directory = directory.resolve()
        if not is_relative_to(target, prefix) or not is_relative_to(directory, prefix):
            return
        indexed.setdefault(name, set()).add(
            InternalCandidate(target=target, directory=directory)
        )

    for path in elf_files(prefix):
        add(path.name, path, path.parent)
        info = dynamic_info(path)
        if info is not None and info.soname is not None:
            add(info.soname, path, path.parent)
    # Wheel layouts frequently expose versioned objects through relative
    # symlink aliases. is_elf() intentionally skips symlinks, so index those
    # aliases explicitly after proving that they remain inside the prefix and
    # resolve to a regular ELF payload.
    for alias in prefix.rglob("*"):
        if not alias.is_symlink() or not SAFE_SONAME.fullmatch(alias.name):
            continue
        try:
            target = alias.resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        if is_relative_to(target, prefix) and is_elf(target):
            add(alias.name, target, alias.parent)
    return {
        name: sorted(
            candidates, key=lambda value: (str(value.directory), str(value.target))
        )
        for name, candidates in indexed.items()
    }


def prefer_internal_dependency(
    binary: Path,
    soname: str,
    candidates: list[InternalCandidate],
    prefix: Path,
) -> Path:
    candidates = [candidate for candidate in candidates if candidate.target != binary]
    targets = {candidate.target for candidate in candidates}
    if len(targets) != 1:
        raise ClosureError(
            f"ambiguous in-prefix dependency {soname} in {binary}: "
            f"{len(targets)} payload candidates"
        )
    target = targets.pop()
    existing: list[InternalCandidate] = []
    creatable: list[InternalCandidate] = []
    for candidate in candidates:
        alias = candidate.directory / soname
        if not (alias.exists() or alias.is_symlink()):
            creatable.append(candidate)
            continue
        try:
            alias_target = alias.resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        if alias_target == target:
            existing.append(candidate)
    usable = existing or creatable
    if not usable:
        raise ClosureError(
            f"all in-prefix alias locations conflict for {soname} in {binary}"
        )
    candidate = sorted(
        usable, key=lambda value: (str(value.directory), str(value.target))
    )[0]
    alias = candidate.directory / soname
    if not (alias.exists() or alias.is_symlink()):
        relative_target = os.path.relpath(candidate.target, candidate.directory)
        alias.symlink_to(relative_target)
    set_relative_rpaths(binary, {candidate.directory})
    resolutions, output = ldd_resolutions(binary)
    resolved = resolutions.get(soname)
    if (
        resolved is None
        or not is_relative_to(resolved, prefix)
        or resolved != candidate.target
    ):
        raise ClosureError(
            f"in-prefix dependency did not win loader resolution for {soname} "
            f"in {binary}\n{output}"
        )
    return resolved


def bundle(prefix: Path, license_root: Path) -> None:
    native = prefix / NATIVE_SUBDIRECTORY
    if native.exists():
        shutil.rmtree(native)
    if license_root.exists():
        shutil.rmtree(license_root)
    native.mkdir(parents=True)
    license_root.mkdir(parents=True)

    seeds = elf_files(prefix)
    for binary in seeds:
        # Remove build-directory and other host-specific search entries before
        # resolving the closure.  Valid wheel-provided ORIGIN paths survive.
        set_relative_rpaths(binary, set())
    internal = index_prefix_libraries(prefix)
    queue: deque[Path] = deque(seeds)
    visited: set[Path] = set()
    additions: dict[Path, set[Path]] = {}
    libraries: dict[str, tuple[Path, DebianOwner]] = {}

    while queue:
        binary = queue.popleft().resolve()
        if binary in visited:
            continue
        visited.add(binary)
        info = dynamic_info(binary)
        if info is None:
            continue
        if (
            info.interpreter is not None
            and info.interpreter not in ALLOWED_INTERPRETERS
        ):
            raise ClosureError(
                f"unsupported ELF interpreter in {binary}: {info.interpreter}"
            )
        resolutions, output = ldd_resolutions(binary)
        for soname in info.needed:
            if soname in GLIBC_HOST_ABI:
                continue
            resolved = resolutions.get(soname)
            if resolved is not None and is_relative_to(resolved, prefix):
                continue
            if soname in DRIVER_HOST_ABI:
                continue
            candidates = [
                candidate
                for candidate in internal.get(soname, [])
                if candidate.target != binary
            ]
            if candidates:
                # A build runner may expose a newer CUDA under /usr/local and
                # ldd will happily choose it ahead of a pinned wheel object.
                # The environment's unique SONAME/alias candidate always wins;
                # patch and re-resolve before considering any host library.
                prefer_internal_dependency(binary, soname, candidates, prefix)
                continue
            if resolved is None:
                raise ClosureError(
                    f"unresolved native dependency {soname} in {binary}\n{output}"
                )
            if not SAFE_SONAME.fullmatch(soname):
                raise ClosureError(
                    f"unsafe ELF dependency name in {binary}: {soname!r}"
                )
            source = resolved.resolve()
            owner = package_owner(source)
            previous = libraries.get(soname)
            if previous is not None and sha256(previous[0]) != sha256(source):
                raise ClosureError(
                    f"dependency name resolves to different files: {soname}: "
                    f"{previous[0]} and {source}"
                )
            destination = native / soname
            if previous is None:
                shutil.copy2(source, destination)
                libraries[soname] = (source, owner)
                internal.setdefault(soname, []).append(
                    InternalCandidate(
                        target=destination.resolve(), directory=native.resolve()
                    )
                )
                queue.append(destination)
            additions.setdefault(binary, set()).add(native)

    for binary, directories in additions.items():
        set_relative_rpaths(binary, directories)
    for soname in libraries:
        set_relative_rpaths(native / soname, {native})
    write_inventory(prefix, license_root, libraries)
    audit(prefix, license_root)


def safe_inventory_path(root: Path, value: object) -> Path:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise ClosureError("native inventory contains an unsafe path")
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts:
        raise ClosureError("native inventory contains an unsafe path")
    path = root / relative
    if not path.is_file() or path.is_symlink():
        raise ClosureError(f"native inventory file is missing or unsafe: {value}")
    return path


def verify_inventory(prefix: Path, license_root: Path) -> None:
    inventory_path = license_root / "packages.json"
    try:
        value = json.loads(inventory_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ClosureError(
            f"invalid Python native dependency inventory: {error}"
        ) from error
    expected_host = {
        "driver": sorted(DRIVER_HOST_ABI),
        "glibc_kernel": sorted(GLIBC_HOST_ABI),
        "interpreters": sorted(ALLOWED_INTERPRETERS),
    }
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != SCHEMA_VERSION
        or value.get("platform") != "linux-x86_64"
        or value.get("native_directory") != NATIVE_SUBDIRECTORY.as_posix()
        or value.get("host_abi") != expected_host
        or not isinstance(value.get("common_licenses"), list)
        or not isinstance(value.get("libraries"), list)
        or not isinstance(value.get("packages"), list)
    ):
        raise ClosureError(
            "Python native dependency inventory has an unsupported schema"
        )
    packages: dict[str, dict[str, object]] = {}
    expected_notice_files: set[Path] = set()
    for record in value["packages"]:
        if not isinstance(record, dict):
            raise ClosureError("native package inventory contains a malformed record")
        package = record.get("package")
        if (
            not all(
                isinstance(record.get(field), str) and record[field]
                for field in (
                    "package",
                    "version",
                    "architecture",
                    "source_package",
                    "source_version",
                )
            )
            or not isinstance(record.get("notices"), list)
            or not record["notices"]
        ):
            raise ClosureError(
                "native package inventory contains malformed identity fields"
            )
        if package in packages:
            raise ClosureError(f"duplicate native package inventory entry: {package}")
        packages[str(package)] = record
        for notice in record["notices"]:
            if not isinstance(notice, dict):
                raise ClosureError(
                    "native package inventory contains a malformed notice"
                )
            path = safe_inventory_path(license_root, notice.get("path"))
            if path in expected_notice_files:
                raise ClosureError(f"duplicate native notice inventory path: {path}")
            expected_notice_files.add(path)
            if notice.get("size") != path.stat().st_size or notice.get(
                "sha256"
            ) != sha256(path):
                raise ClosureError(f"native package notice checksum mismatch: {path}")
    common_licenses: dict[str, Path] = {}
    for record in value["common_licenses"]:
        if not isinstance(record, dict):
            raise ClosureError("native inventory contains a malformed common license")
        name = record.get("name")
        if not isinstance(name, str) or not SAFE_SONAME.fullmatch(name):
            raise ClosureError(
                "native inventory contains an unsafe common-license name"
            )
        if name in common_licenses or record.get("path") != f"common-licenses/{name}":
            raise ClosureError(f"duplicate or mismatched common-license entry: {name}")
        path = safe_inventory_path(license_root, record.get("path"))
        if record.get("size") != path.stat().st_size or record.get("sha256") != sha256(
            path
        ):
            raise ClosureError(f"common-license checksum mismatch: {name}")
        common_licenses[name] = path
    referenced = referenced_common_licenses(expected_notice_files)
    if set(common_licenses) != referenced:
        difference = sorted(set(common_licenses) ^ referenced)
        raise ClosureError(
            f"common-license inventory coverage mismatch: {difference[0]}"
        )
    native = prefix / NATIVE_SUBDIRECTORY
    expected_libraries: set[Path] = set()
    for record in value["libraries"]:
        if not isinstance(record, dict):
            raise ClosureError("native library inventory contains a malformed record")
        soname = record.get("soname")
        package = record.get("debian_package")
        if not isinstance(soname, str) or not SAFE_SONAME.fullmatch(soname):
            raise ClosureError("native library inventory contains an unsafe soname")
        if package not in packages:
            raise ClosureError(
                f"native library refers to an unknown Debian package: {package}"
            )
        if (
            not isinstance(record.get("source_path"), str)
            or not str(record["source_path"]).startswith("/")
            or not isinstance(record.get("source_sha256"), str)
            or not SHA256_PATTERN.fullmatch(str(record["source_sha256"]))
            or not isinstance(record.get("source_size"), int)
            or int(record["source_size"]) <= 0
        ):
            raise ClosureError(
                f"native library has incomplete Debian source provenance: {soname}"
            )
        expected_path = NATIVE_SUBDIRECTORY / soname
        if record.get("path") != expected_path.as_posix():
            raise ClosureError(f"native library path disagrees with soname: {soname}")
        path = prefix / expected_path
        if not path.is_file() or path.is_symlink():
            raise ClosureError(f"bundled native library is missing or unsafe: {soname}")
        expected_libraries.add(path)
        if record.get("size") != path.stat().st_size or record.get("sha256") != sha256(
            path
        ):
            raise ClosureError(f"bundled native library checksum mismatch: {soname}")
    actual_libraries = (
        {path for path in native.iterdir() if path.is_file()}
        if native.is_dir()
        else set()
    )
    if actual_libraries != expected_libraries:
        difference = sorted(str(path) for path in actual_libraries ^ expected_libraries)
        raise ClosureError(
            f"native library inventory disagrees with payload: {difference[0]}"
        )
    readme = value.get("readme")
    if not isinstance(readme, dict):
        raise ClosureError("native dependency README inventory is missing")
    readme_path = safe_inventory_path(license_root, readme.get("path"))
    if readme.get("size") != readme_path.stat().st_size or readme.get(
        "sha256"
    ) != sha256(readme_path):
        raise ClosureError("native dependency README checksum mismatch")
    actual_notice_files = (
        {
            path
            for path in (license_root / "packages").rglob("*")
            if path.is_file() and not path.is_symlink()
        }
        if (license_root / "packages").is_dir()
        else set()
    )
    if actual_notice_files != expected_notice_files:
        difference = sorted(
            str(path) for path in actual_notice_files ^ expected_notice_files
        )
        raise ClosureError(
            f"native notice inventory disagrees with payload: {difference[0]}"
        )
    common_root = license_root / "common-licenses"
    actual_common_licenses = (
        {
            path
            for path in common_root.iterdir()
            if path.is_file() and not path.is_symlink()
        }
        if common_root.is_dir()
        else set()
    )
    if actual_common_licenses != set(common_licenses.values()):
        difference = sorted(
            str(path) for path in actual_common_licenses ^ set(common_licenses.values())
        )
        raise ClosureError(
            f"common-license inventory disagrees with payload: {difference[0]}"
        )
    if value.get("summary") != {
        "common_licenses": len(common_licenses),
        "libraries": len(expected_libraries),
        "packages": len(packages),
    }:
        raise ClosureError("native dependency inventory summary is inconsistent")


def audit(prefix: Path, license_root: Path) -> None:
    verify_inventory(prefix, license_root)
    binaries = elf_files(prefix)
    if not binaries:
        raise ClosureError(f"portable Python contains no ELF files: {prefix}")
    for binary in binaries:
        info = dynamic_info(binary)
        if info is None:
            continue
        invalid_rpaths = [
            entry for entry in info.rpath if entry and not is_origin_rpath(entry)
        ]
        if invalid_rpaths:
            raise ClosureError(
                f"non-relative ELF search path in {binary}: {invalid_rpaths[0]}"
            )
        if (
            info.interpreter is not None
            and info.interpreter not in ALLOWED_INTERPRETERS
        ):
            raise ClosureError(
                f"unsupported ELF interpreter in {binary}: {info.interpreter}"
            )
        resolutions, output = ldd_resolutions(binary)
        for soname in info.needed:
            resolved = resolutions.get(soname)
            if soname in GLIBC_HOST_ABI:
                if resolved is not None and is_relative_to(resolved, prefix):
                    raise ClosureError(
                        f"host glibc ABI was unexpectedly bundled: {soname}"
                    )
                continue
            if soname in DRIVER_HOST_ABI:
                continue
            if resolved is None:
                raise ClosureError(
                    f"unresolved native dependency {soname} in {binary}\n{output}"
                )
            if not is_relative_to(resolved, prefix):
                raise ClosureError(
                    f"out-of-bundle native dependency {soname} in {binary}: {resolved}"
                )
    print(
        json.dumps(
            {
                "elf_files": len(binaries),
                "host_driver_abi": sorted(DRIVER_HOST_ABI),
                "host_glibc_kernel_abi": sorted(GLIBC_HOST_ABI),
                "prefix": str(prefix),
            },
            sort_keys=True,
        )
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("bundle", "audit"))
    parser.add_argument("prefix", type=Path)
    parser.add_argument("--license-root", required=True, type=Path)
    return parser.parse_args()


def main() -> None:
    arguments = parse_args()
    prefix = arguments.prefix.resolve()
    license_root = arguments.license_root.resolve()
    if not (prefix / "bin").is_dir() or not (prefix / "lib").is_dir():
        raise SystemExit(f"not a Python prefix: {prefix}")
    for command in ("ldd", "readelf"):
        if shutil.which(command) is None:
            raise SystemExit(f"required command is unavailable: {command}")
    try:
        if arguments.mode == "bundle":
            for command in ("dpkg-query", "patchelf"):
                if shutil.which(command) is None:
                    raise ClosureError(
                        f"required build command is unavailable: {command}"
                    )
            bundle(prefix, license_root)
        else:
            audit(prefix, license_root)
    except ClosureError as error:
        raise SystemExit(f"Python native closure error: {error}") from error


if __name__ == "__main__":
    main()
