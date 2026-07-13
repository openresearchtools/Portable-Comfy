#!/usr/bin/env python3
"""Create a self-verifying source and relinking bundle for the AppImage runtime."""

from __future__ import annotations

import argparse
import csv
import hashlib
import re
import shutil
import tarfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


STATIC_LIBRARY_PATHS = {
    "/usr/lib/libc.a",
    "/usr/lib/libfuse3.a",
    "/usr/local/lib/libsquashfuse.a",
    "/usr/local/lib/libsquashfuse_ll.a",
    "/usr/lib/libzstd.a",
    "/usr/lib/libz.a",
    "/usr/lib/libmimalloc.a",
}
CRT_OBJECT_PATHS = {"/usr/lib/rcrt1.o", "/usr/lib/crti.o", "/usr/lib/crtn.o"}
STATIC_LIBRARY_LINE = re.compile(r"^([0-9a-f]{64})  (/.+)$")


@dataclass(frozen=True)
class Component:
    """One statically linked runtime component and its source material."""

    name: str
    version: str
    role: str
    license_expression: str
    source_archive: Path
    source_root: str
    license_members: tuple[tuple[str, str], ...]
    packaging_archive: Path | None = None
    modifications: str = "none"


@dataclass(frozen=True)
class BundleInputs:
    """All build outputs and source inputs copied into the bundle."""

    destination: Path
    alpine_version: str
    alpine_digest: str
    components: tuple[Component, ...]
    fallback_patch: Path
    dependencies_patch: Path
    runtime_object: Path
    apk_packages: Path
    all_build_packages: Path
    static_libraries: Path
    crt_objects: Path
    link_map: Path
    link_trace: Path
    dynamic_section: Path


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _safe_archive(archive_path: Path) -> None:
    with tarfile.open(archive_path, "r:*") as archive:
        for member in archive.getmembers():
            path = PurePosixPath(member.name)
            if path.is_absolute() or ".." in path.parts:
                raise RuntimeError(
                    f"source archive contains an unsafe path: {archive_path}: "
                    f"{member.name}"
                )


def _copy_notice(archive_path: Path, member_name: str, target: Path) -> None:
    with tarfile.open(archive_path, "r:*") as archive:
        try:
            member = archive.getmember(member_name)
        except KeyError as error:
            raise RuntimeError(
                f"source archive has no exact notice {member_name}: {archive_path}"
            ) from error
        if not member.isfile():
            raise RuntimeError(
                f"source notice is not a regular file: {archive_path}: {member_name}"
            )
        stream = archive.extractfile(member)
        if stream is None:
            raise RuntimeError(
                f"cannot read source notice: {archive_path}: {member_name}"
            )
        contents = stream.read()
    if not contents:
        raise RuntimeError(f"source notice is empty: {archive_path}: {member_name}")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(contents)


def _expected_apk_packages(components: tuple[Component, ...]) -> set[str]:
    versions = {component.name: component.version for component in components}
    package_groups = {
        "musl": ("musl", "musl-dev"),
        "zstd": ("zstd", "zstd-libs", "zstd-dev", "zstd-static"),
        "zlib": ("zlib", "zlib-dev", "zlib-static"),
        "mimalloc": (
            "mimalloc2",
            "mimalloc2-dev",
            "mimalloc2-debug",
            "mimalloc2-insecure",
        ),
    }
    expected: set[str] = set()
    for component_name, package_names in package_groups.items():
        try:
            version = versions[component_name]
        except KeyError as error:
            raise RuntimeError(
                f"runtime component inventory is missing {component_name}"
            ) from error
        expected.update(f"{package}-{version}" for package in package_names)
    return expected


def _validate_build_metadata(inputs: BundleInputs) -> None:
    package_lines = inputs.apk_packages.read_text(encoding="utf-8").splitlines()
    if package_lines != sorted(set(package_lines)) or not all(package_lines):
        raise RuntimeError("runtime APK inventory is not sorted and unique")
    packages = set(package_lines)
    expected_packages = _expected_apk_packages(inputs.components)
    if packages != expected_packages:
        missing = sorted(expected_packages - packages)
        extra = sorted(packages - expected_packages)
        raise RuntimeError(
            f"runtime APK inventory differs from pins; missing={missing}, extra={extra}"
        )
    all_build_package_lines = inputs.all_build_packages.read_text(
        encoding="utf-8"
    ).splitlines()
    if all_build_package_lines != sorted(set(all_build_package_lines)) or not all(
        all_build_package_lines
    ):
        raise RuntimeError("full build-package ledger is not sorted and unique")
    all_build_packages = set(all_build_package_lines)
    if not packages <= all_build_packages:
        raise RuntimeError(
            "linked APK inventory is absent from full build-package ledger"
        )

    def checksum_paths(path: Path) -> set[str]:
        result: set[str] = set()
        lines = path.read_text(encoding="utf-8").splitlines()
        for line in lines:
            match = STATIC_LIBRARY_LINE.fullmatch(line)
            if not match:
                raise RuntimeError(f"invalid link-input checksum record: {line!r}")
            result.add(match.group(2))
        if len(result) != len(lines):
            raise RuntimeError("link-input checksum inventory contains duplicates")
        return result

    library_paths = checksum_paths(inputs.static_libraries)
    if library_paths != STATIC_LIBRARY_PATHS:
        raise RuntimeError(
            "static-library checksum inventory is incomplete or contains unknown paths"
        )
    crt_paths = checksum_paths(inputs.crt_objects)
    if crt_paths != CRT_OBJECT_PATHS:
        raise RuntimeError(
            "musl CRT checksum inventory is incomplete or contains unknown paths"
        )

    trace_paths = {
        line.strip()
        for line in inputs.link_trace.read_text(encoding="utf-8").splitlines()
        if line.strip()
    }
    expected_trace_paths = STATIC_LIBRARY_PATHS | CRT_OBJECT_PATHS | {"runtime.o"}
    if trace_paths != expected_trace_paths:
        raise RuntimeError(
            "linker trace contains an unclassified input or omits an expected input; "
            f"expected={sorted(expected_trace_paths)}, actual={sorted(trace_paths)}"
        )
    map_text = inputs.link_map.read_text(encoding="utf-8", errors="strict")
    if "/usr/lib/gcc/" in map_text or "libgcc" in map_text:
        raise RuntimeError("linker map contains an unclassified GCC runtime input")
    dynamic_text = inputs.dynamic_section.read_text(encoding="utf-8")
    if "(NEEDED)" in dynamic_text:
        raise RuntimeError("static runtime contains a DT_NEEDED dependency")

    object_bytes = inputs.runtime_object.read_bytes()
    if (
        len(object_bytes) < 20
        or object_bytes[:4] != b"\x7fELF"
        or object_bytes[4:6] != b"\x02\x01"
        or object_bytes[16:18] != b"\x01\x00"
        or object_bytes[18:20] != b"\x3e\x00"
    ):
        raise RuntimeError(
            "runtime relink object is not a little-endian x86-64 ELF object"
        )


def _write_components(destination: Path, components: tuple[Component, ...]) -> None:
    path = destination / "COMPONENTS.tsv"
    fields = (
        "component",
        "version",
        "role",
        "license",
        "source_archive",
        "source_sha256",
        "packaging_archive",
        "packaging_sha256",
        "modifications",
    )
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, delimiter="\t")
        writer.writeheader()
        for component in components:
            source_target = Path("sources") / component.source_archive.name
            packaging_target = (
                Path("alpine-packaging") / component.packaging_archive.name
                if component.packaging_archive
                else None
            )
            writer.writerow(
                {
                    "component": component.name,
                    "version": component.version,
                    "role": component.role,
                    "license": component.license_expression,
                    "source_archive": source_target.as_posix(),
                    "source_sha256": sha256(component.source_archive),
                    "packaging_archive": (
                        packaging_target.as_posix() if packaging_target else ""
                    ),
                    "packaging_sha256": (
                        sha256(component.packaging_archive)
                        if component.packaging_archive
                        else ""
                    ),
                    "modifications": component.modifications,
                }
            )


def _write_readme(inputs: BundleInputs) -> None:
    destination = inputs.destination
    component_lines = "\n".join(
        f"- `{component.name}` {component.version}: "
        f"{component.license_expression} ({component.role})."
        for component in inputs.components
    )
    (destination / "README.md").write_text(
        f"""# AppImage runtime source and compliance bundle

This directory accompanies the patched, statically linked type-2 AppImage
runtime. `COMPONENTS.tsv` binds every linked component to the exact source
archive used by the build. For Alpine-provided archives it also includes the
exact aports recipe and patches for the installed package release.

The build base is Alpine {inputs.alpine_version} for linux/amd64, pinned as
`alpine:{inputs.alpine_version}@{inputs.alpine_digest}`.
`build-inputs/runtime-apk-packages.txt` records the installed APK closure for
the Alpine-provided linked components, while
`build-inputs/runtime-all-build-packages.txt` captures every installed package
in the build container. The file
`build-inputs/runtime-static-libraries.sha256` records every static archive
presented to the final link. The linker map and trace enumerate the archive
members and top-level inputs; `runtime-crt-objects.sha256` binds the three
non-archive musl inputs. The link deliberately uses `-nostdlib`, so no GCC
or compiler-rt object is incorporated, and the captured dynamic section has no
`DT_NEEDED` entry.

Components

{component_lines}

The upstream runtime `LICENSE` predates its mimalloc link and does not contain
the dependency license texts. The exact component notices in `licenses/` and
the full source archives here are the authoritative supplement. Zstandard is
used under its BSD option. Only libfuse's `include/`, `lib/`, and Meson build
files are linked; the GPL-licensed libfuse utilities are not linked, although
their source and GPL text remain in the unmodified full source archive.

Verify every copied file from this directory with:

```sh
sha256sum --check SHA256SUMS
```

See `RELINKING.md` for the libfuse LGPL v2.1 source, modification, and relinking
materials. This inventory is supplied for compliance and reproducibility; it
is not legal advice.
""",
        encoding="utf-8",
    )


def _write_relinking(inputs: BundleInputs) -> None:
    destination = inputs.destination
    runtime = next(
        component
        for component in inputs.components
        if component.name == "type2-runtime"
    )
    libfuse = next(
        component for component in inputs.components if component.name == "libfuse"
    )
    squashfuse = next(
        component for component in inputs.components if component.name == "squashfuse"
    )
    (destination / "RELINKING.md").write_text(
        f"""# libfuse LGPL v2.1 relinking materials

The runtime statically links libfuse {libfuse.version}. The applicable upstream
notice and full LGPL v2.1 text are `licenses/libfuse-LICENSE.txt` and
`licenses/libfuse-LGPL-2.1.txt`. You may modify libfuse and relink the runtime;
this distribution imposes no term prohibiting reverse engineering for
debugging such modifications.

This bundle provides both forms of the non-library side of the link:

- complete type2-runtime source at `sources/{runtime.source_archive.name}`;
- the exact relocatable `relink/runtime.o` produced before the final link.

It also provides the complete libfuse source, every other linked dependency's
source, the upstream libfuse modification, both Portable Comfy runtime patches,
the link script, and the build scripts. The AppImage project's libfuse change
is in `patches/upstream-libfuse-mount.c.diff`; the dependency patch makes the
modified `lib/mount.c` carry its change purpose and 2024-11-24 date.

To reconstruct the build tree, unpack
`sources/{runtime.source_archive.name}`, then from its root apply:

```sh
patch -p1 < ../patches/appimage-runtime-dependencies.patch
patch -p1 < ../patches/appimage-runtime-fuse-fallback.patch
mkdir -p compliance-sources
cp ../sources/{libfuse.source_archive.name} compliance-sources/fuse-{libfuse.version}.tar.xz
cp ../sources/{squashfuse.source_archive.name} compliance-sources/squashfuse-{squashfuse.version}.tar.gz
ARCH=x86_64 scripts/docker/build-with-docker.sh
```

Adjust the relative `..` paths if the source tree is unpacked elsewhere. The
Docker recipe is pinned to Alpine {inputs.alpine_version} amd64 and exact
linked APK releases. To relink a modified libfuse, apply the desired changes
to the bundled libfuse source before its Meson/Ninja build (updating the local
archive checksum in the build recipe), then run the same final link command
shown in `src/runtime/Makefile`. You may substitute `relink/runtime.o` for the
newly compiled object in that command. Preserve the AppImage magic-byte and
debug-link post-processing in `scripts/build-runtime.sh` for a deployable
runtime.

`build-inputs/runtime-static-libraries.sha256` identifies the original static
archives, while `runtime-crt-objects.sha256` identifies musl's startup and
termination objects. `runtime-link.trace`, `runtime-link.map`, and
`runtime-dynamic-section.txt` provide the final-link proof. The sources and
Alpine packaging recipes in this bundle allow those inputs to be rebuilt;
compiler and other build-only Alpine packages are not part of the final static
link. Their exact resolved versions are nevertheless recorded in
`build-inputs/runtime-all-build-packages.txt`.
""",
        encoding="utf-8",
    )


def create_bundle(inputs: BundleInputs) -> None:
    destination = inputs.destination.resolve()
    if destination.exists() and any(destination.iterdir()):
        raise RuntimeError(
            f"runtime source bundle destination is not empty: {destination}"
        )
    destination.mkdir(parents=True, exist_ok=True)

    required_files = (
        inputs.fallback_patch,
        inputs.dependencies_patch,
        inputs.runtime_object,
        inputs.apk_packages,
        inputs.all_build_packages,
        inputs.static_libraries,
        inputs.crt_objects,
        inputs.link_map,
        inputs.link_trace,
        inputs.dynamic_section,
        *(component.source_archive for component in inputs.components),
        *(
            component.packaging_archive
            for component in inputs.components
            if component.packaging_archive is not None
        ),
    )
    for path in required_files:
        if not path.is_file() or path.stat().st_size == 0:
            raise RuntimeError(f"runtime compliance input is missing or empty: {path}")
    for component in inputs.components:
        _safe_archive(component.source_archive)
        if component.packaging_archive:
            _safe_archive(component.packaging_archive)
    _validate_build_metadata(inputs)

    sources = destination / "sources"
    packaging = destination / "alpine-packaging"
    licenses = destination / "licenses"
    patches = destination / "patches"
    build_inputs = destination / "build-inputs"
    relink = destination / "relink"
    for directory in (sources, packaging, licenses, patches, build_inputs, relink):
        directory.mkdir(parents=True, exist_ok=True)

    for component in inputs.components:
        shutil.copyfile(
            component.source_archive, sources / component.source_archive.name
        )
        if component.packaging_archive:
            shutil.copyfile(
                component.packaging_archive,
                packaging / component.packaging_archive.name,
            )
        for member_name, notice_name in component.license_members:
            _copy_notice(
                component.source_archive,
                f"{component.source_root}/{member_name}",
                licenses / notice_name,
            )

    runtime_component = next(
        component
        for component in inputs.components
        if component.name == "type2-runtime"
    )
    _copy_notice(
        runtime_component.source_archive,
        f"{runtime_component.source_root}/patches/libfuse/mount.c.diff",
        patches / "upstream-libfuse-mount.c.diff",
    )
    shutil.copyfile(inputs.fallback_patch, patches / inputs.fallback_patch.name)
    shutil.copyfile(inputs.dependencies_patch, patches / inputs.dependencies_patch.name)
    shutil.copyfile(inputs.runtime_object, relink / "runtime.o")
    shutil.copyfile(inputs.apk_packages, build_inputs / "runtime-apk-packages.txt")
    shutil.copyfile(
        inputs.all_build_packages,
        build_inputs / "runtime-all-build-packages.txt",
    )
    shutil.copyfile(
        inputs.static_libraries,
        build_inputs / "runtime-static-libraries.sha256",
    )
    shutil.copyfile(inputs.crt_objects, build_inputs / "runtime-crt-objects.sha256")
    shutil.copyfile(inputs.link_map, build_inputs / "runtime-link.map")
    shutil.copyfile(inputs.link_trace, build_inputs / "runtime-link.trace")
    shutil.copyfile(
        inputs.dynamic_section, build_inputs / "runtime-dynamic-section.txt"
    )

    _write_components(destination, inputs.components)
    _write_readme(inputs)
    _write_relinking(inputs)

    manifest_lines = []
    for path in sorted(destination.rglob("*")):
        if path.is_file() and path != destination / "SHA256SUMS":
            relative = path.relative_to(destination).as_posix()
            manifest_lines.append(f"{sha256(path)}  {relative}\n")
    (destination / "SHA256SUMS").write_text("".join(manifest_lines), encoding="utf-8")


def _component(
    name: str,
    version: str,
    role: str,
    license_expression: str,
    source_archive: Path,
    source_root: str,
    license_members: tuple[tuple[str, str], ...],
    packaging_archive: Path | None,
    modifications: str,
) -> Component:
    return Component(
        name=name,
        version=version,
        role=role,
        license_expression=license_expression,
        source_archive=source_archive.resolve(),
        source_root=source_root,
        license_members=license_members,
        packaging_archive=(packaging_archive.resolve() if packaging_archive else None),
        modifications=modifications,
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("destination", type=Path)
    parser.add_argument("--alpine-version", required=True)
    parser.add_argument("--alpine-digest", required=True)
    parser.add_argument("--runtime-version", required=True)
    parser.add_argument("--runtime-archive", required=True, type=Path)
    parser.add_argument("--musl-version", required=True)
    parser.add_argument("--musl-upstream-version", required=True)
    parser.add_argument("--musl-archive", required=True, type=Path)
    parser.add_argument("--musl-packaging", required=True, type=Path)
    parser.add_argument("--libfuse-version", required=True)
    parser.add_argument("--libfuse-archive", required=True, type=Path)
    parser.add_argument("--squashfuse-version", required=True)
    parser.add_argument("--squashfuse-archive", required=True, type=Path)
    parser.add_argument("--zstd-version", required=True)
    parser.add_argument("--zstd-upstream-version", required=True)
    parser.add_argument("--zstd-archive", required=True, type=Path)
    parser.add_argument("--zstd-packaging", required=True, type=Path)
    parser.add_argument("--zlib-version", required=True)
    parser.add_argument("--zlib-upstream-version", required=True)
    parser.add_argument("--zlib-archive", required=True, type=Path)
    parser.add_argument("--zlib-packaging", required=True, type=Path)
    parser.add_argument("--mimalloc-version", required=True)
    parser.add_argument("--mimalloc-upstream-version", required=True)
    parser.add_argument("--mimalloc-archive", required=True, type=Path)
    parser.add_argument("--mimalloc-packaging", required=True, type=Path)
    parser.add_argument("--fallback-patch", required=True, type=Path)
    parser.add_argument("--dependencies-patch", required=True, type=Path)
    parser.add_argument("--runtime-object", required=True, type=Path)
    parser.add_argument("--apk-packages", required=True, type=Path)
    parser.add_argument("--all-build-packages", required=True, type=Path)
    parser.add_argument("--static-libraries", required=True, type=Path)
    parser.add_argument("--crt-objects", required=True, type=Path)
    parser.add_argument("--link-map", required=True, type=Path)
    parser.add_argument("--link-trace", required=True, type=Path)
    parser.add_argument("--dynamic-section", required=True, type=Path)
    args = parser.parse_args()

    components = (
        _component(
            "type2-runtime",
            args.runtime_version,
            "runtime application code",
            "MIT",
            args.runtime_archive,
            f"type2-runtime-{args.runtime_version}",
            (("LICENSE", "type2-runtime-MIT.txt"),),
            None,
            "patches/appimage-runtime-dependencies.patch;"
            "patches/appimage-runtime-fuse-fallback.patch",
        ),
        _component(
            "musl",
            args.musl_version,
            f"static libc (upstream {args.musl_upstream_version})",
            "MIT",
            args.musl_archive,
            f"musl-{args.musl_upstream_version}",
            (("COPYRIGHT", "musl-COPYRIGHT.txt"),),
            args.musl_packaging,
            "Alpine aports recipe and patches in alpine-packaging/",
        ),
        _component(
            "libfuse",
            args.libfuse_version,
            "static FUSE library",
            "LGPL-2.1-only",
            args.libfuse_archive,
            f"fuse-{args.libfuse_version}",
            (
                ("LICENSE", "libfuse-LICENSE.txt"),
                ("LGPL2.txt", "libfuse-LGPL-2.1.txt"),
                ("GPL2.txt", "libfuse-GPL-2.0.txt"),
            ),
            None,
            "patches/upstream-libfuse-mount.c.diff; change notice added by "
            "appimage-runtime-dependencies.patch",
        ),
        _component(
            "squashfuse",
            args.squashfuse_version,
            "static SquashFS reader",
            "BSD-2-Clause",
            args.squashfuse_archive,
            f"squashfuse-{args.squashfuse_version}",
            (("LICENSE", "squashfuse-BSD-2-Clause.txt"),),
            None,
            "none",
        ),
        _component(
            "zstd",
            args.zstd_version,
            f"static decompressor (upstream {args.zstd_upstream_version})",
            "BSD-3-Clause",
            args.zstd_archive,
            f"zstd-{args.zstd_upstream_version}",
            (
                ("LICENSE", "zstd-BSD-3-Clause.txt"),
                ("COPYING", "zstd-GPL-2.0.txt"),
            ),
            args.zstd_packaging,
            "Alpine aports recipe in alpine-packaging/",
        ),
        _component(
            "zlib",
            args.zlib_version,
            f"static decompressor (upstream {args.zlib_upstream_version})",
            "Zlib",
            args.zlib_archive,
            f"zlib-{args.zlib_upstream_version}",
            (("LICENSE", "zlib.txt"),),
            args.zlib_packaging,
            "Alpine aports recipe in alpine-packaging/",
        ),
        _component(
            "mimalloc",
            args.mimalloc_version,
            f"static allocator (upstream {args.mimalloc_upstream_version})",
            "MIT",
            args.mimalloc_archive,
            f"mimalloc-{args.mimalloc_upstream_version}",
            (("LICENSE", "mimalloc-MIT.txt"),),
            args.mimalloc_packaging,
            "Alpine aports recipe and patch in alpine-packaging/",
        ),
    )
    create_bundle(
        BundleInputs(
            destination=args.destination,
            alpine_version=args.alpine_version,
            alpine_digest=args.alpine_digest,
            components=components,
            fallback_patch=args.fallback_patch.resolve(),
            dependencies_patch=args.dependencies_patch.resolve(),
            runtime_object=args.runtime_object.resolve(),
            apk_packages=args.apk_packages.resolve(),
            all_build_packages=args.all_build_packages.resolve(),
            static_libraries=args.static_libraries.resolve(),
            crt_objects=args.crt_objects.resolve(),
            link_map=args.link_map.resolve(),
            link_trace=args.link_trace.resolve(),
            dynamic_section=args.dynamic_section.resolve(),
        )
    )


if __name__ == "__main__":
    main()
