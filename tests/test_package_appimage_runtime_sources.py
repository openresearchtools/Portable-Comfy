from __future__ import annotations

import csv
import importlib.util
import io
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest


SCRIPT = Path(__file__).parents[1] / "scripts/package_appimage_runtime_sources.py"
SPEC = importlib.util.spec_from_file_location(
    "package_appimage_runtime_sources", SCRIPT
)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def _archive(path: Path, members: dict[str, bytes]) -> Path:
    mode = "w:xz" if path.suffix == ".xz" else "w:gz"
    with tarfile.open(path, mode) as archive:
        for name, contents in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(contents)
            archive.addfile(info, io.BytesIO(contents))
    return path


def _fixture(tmp_path: Path) -> MODULE.BundleInputs:
    sources = tmp_path / "input"
    sources.mkdir()
    runtime_version = "a" * 40
    runtime = _archive(
        sources / f"type2-runtime-{runtime_version}.tar.gz",
        {
            f"type2-runtime-{runtime_version}/LICENSE": b"Runtime MIT\n",
            f"type2-runtime-{runtime_version}/patches/libfuse/mount.c.diff": (
                b"upstream libfuse patch\n"
            ),
        },
    )
    musl = _archive(
        sources / "musl-1.2.5.tar.gz",
        {"musl-1.2.5/COPYRIGHT": b"musl notice\n"},
    )
    libfuse = _archive(
        sources / "fuse-3.15.0.tar.xz",
        {
            "fuse-3.15.0/LICENSE": b"fuse file map\n",
            "fuse-3.15.0/LGPL2.txt": b"LGPL 2.1\n",
            "fuse-3.15.0/GPL2.txt": b"GPL 2\n",
        },
    )
    squashfuse = _archive(
        sources / "squashfuse-0.5.2.tar.gz",
        {"squashfuse-0.5.2/LICENSE": b"squashfuse notice\n"},
    )
    zstd = _archive(
        sources / "zstd-1.5.6.tar.gz",
        {
            "zstd-1.5.6/LICENSE": b"zstd BSD\n",
            "zstd-1.5.6/COPYING": b"zstd GPL option\n",
        },
    )
    zlib = _archive(
        sources / "zlib-1.3.2.tar.gz",
        {"zlib-1.3.2/LICENSE": b"zlib notice\n"},
    )
    mimalloc = _archive(
        sources / "mimalloc-2.1.7.tar.gz",
        {"mimalloc-2.1.7/LICENSE": b"mimalloc MIT\n"},
    )

    packaging = {}
    for name in ("musl", "zstd", "zlib", "mimalloc2"):
        packaging[name] = _archive(
            sources / f"aports-{name}-{'b' * 40}.tar.gz",
            {f"aports/{name}/APKBUILD": f"pkgname={name}\n".encode()},
        )

    components = (
        MODULE.Component(
            "type2-runtime",
            runtime_version,
            "runtime application code",
            "MIT",
            runtime,
            f"type2-runtime-{runtime_version}",
            (("LICENSE", "type2-runtime-MIT.txt"),),
            modifications="two patches",
        ),
        MODULE.Component(
            "musl",
            "1.2.5-r11",
            "static libc",
            "MIT",
            musl,
            "musl-1.2.5",
            (("COPYRIGHT", "musl-COPYRIGHT.txt"),),
            packaging["musl"],
        ),
        MODULE.Component(
            "libfuse",
            "3.15.0",
            "static FUSE library",
            "LGPL-2.1-only",
            libfuse,
            "fuse-3.15.0",
            (
                ("LICENSE", "libfuse-LICENSE.txt"),
                ("LGPL2.txt", "libfuse-LGPL-2.1.txt"),
                ("GPL2.txt", "libfuse-GPL-2.0.txt"),
            ),
        ),
        MODULE.Component(
            "squashfuse",
            "0.5.2",
            "static SquashFS reader",
            "BSD-2-Clause",
            squashfuse,
            "squashfuse-0.5.2",
            (("LICENSE", "squashfuse-BSD-2-Clause.txt"),),
        ),
        MODULE.Component(
            "zstd",
            "1.5.6-r2",
            "static decompressor",
            "BSD-3-Clause",
            zstd,
            "zstd-1.5.6",
            (
                ("LICENSE", "zstd-BSD-3-Clause.txt"),
                ("COPYING", "zstd-GPL-2.0.txt"),
            ),
            packaging["zstd"],
        ),
        MODULE.Component(
            "zlib",
            "1.3.2-r0",
            "static decompressor",
            "Zlib",
            zlib,
            "zlib-1.3.2",
            (("LICENSE", "zlib.txt"),),
            packaging["zlib"],
        ),
        MODULE.Component(
            "mimalloc",
            "2.1.7-r0",
            "static allocator",
            "MIT",
            mimalloc,
            "mimalloc-2.1.7",
            (("LICENSE", "mimalloc-MIT.txt"),),
            packaging["mimalloc2"],
        ),
    )

    package_versions = {
        "musl": ("1.2.5-r11", ("musl", "musl-dev")),
        "zstd": (
            "1.5.6-r2",
            ("zstd", "zstd-libs", "zstd-dev", "zstd-static"),
        ),
        "zlib": ("1.3.2-r0", ("zlib", "zlib-dev", "zlib-static")),
        "mimalloc": (
            "2.1.7-r0",
            (
                "mimalloc2",
                "mimalloc2-dev",
                "mimalloc2-debug",
                "mimalloc2-insecure",
            ),
        ),
    }
    apk_packages = sources / "runtime-apk-packages.txt"
    apk_packages.write_text(
        "".join(
            f"{record}\n"
            for record in sorted(
                f"{package}-{version}"
                for version, packages in package_versions.values()
                for package in packages
            )
        ),
        encoding="utf-8",
    )
    all_build_packages = sources / "runtime-all-build-packages.txt"
    all_build_packages.write_text(
        "".join(
            f"{package}\n"
            for package in sorted(
                {
                    *apk_packages.read_text(encoding="utf-8").splitlines(),
                    "clang20-20.1.2-r0",
                }
            )
        ),
        encoding="utf-8",
    )
    static_libraries = sources / "runtime-static-libraries.sha256"
    static_libraries.write_text(
        "".join(
            f"{'0' * 64}  {path}\n" for path in sorted(MODULE.STATIC_LIBRARY_PATHS)
        ),
        encoding="utf-8",
    )
    crt_objects = sources / "runtime-crt-objects.sha256"
    crt_objects.write_text(
        "".join(f"{'1' * 64}  {path}\n" for path in sorted(MODULE.CRT_OBJECT_PATHS)),
        encoding="utf-8",
    )
    link_trace = sources / "runtime-link.trace"
    link_trace.write_text(
        "".join(
            f"{path}\n"
            for path in sorted(
                MODULE.STATIC_LIBRARY_PATHS | MODULE.CRT_OBJECT_PATHS | {"runtime.o"}
            )
        ),
        encoding="utf-8",
    )
    link_map = sources / "runtime-link.map"
    link_map.write_text("LOAD runtime.o\nLOAD /usr/lib/libc.a\n", encoding="utf-8")
    dynamic_section = sources / "runtime-dynamic-section.txt"
    dynamic_section.write_text(
        "Dynamic section has no dependencies\n", encoding="utf-8"
    )
    runtime_object = sources / "runtime.o"
    runtime_object.write_bytes(
        b"\x7fELF\x02\x01" + b"\0" * 10 + b"\x01\x00\x3e\x00" + b"\0" * 32
    )
    fallback_patch = sources / "appimage-runtime-fuse-fallback.patch"
    fallback_patch.write_text("fallback patch\n", encoding="utf-8")
    dependencies_patch = sources / "appimage-runtime-dependencies.patch"
    dependencies_patch.write_text("dependency patch\n", encoding="utf-8")
    return MODULE.BundleInputs(
        destination=tmp_path / "bundle",
        alpine_version="3.21.7",
        alpine_digest="sha256:" + "c" * 64,
        components=components,
        fallback_patch=fallback_patch,
        dependencies_patch=dependencies_patch,
        runtime_object=runtime_object,
        apk_packages=apk_packages,
        all_build_packages=all_build_packages,
        static_libraries=static_libraries,
        crt_objects=crt_objects,
        link_map=link_map,
        link_trace=link_trace,
        dynamic_section=dynamic_section,
    )


def test_creates_complete_self_verifying_runtime_source_bundle(tmp_path: Path) -> None:
    inputs = _fixture(tmp_path)

    MODULE.create_bundle(inputs)

    bundle = inputs.destination
    subprocess.run(
        ["sha256sum", "--check", "--strict", "SHA256SUMS"],
        cwd=bundle,
        check=True,
        stdout=subprocess.PIPE,
        text=True,
    )
    assert (bundle / "licenses/libfuse-LGPL-2.1.txt").read_bytes() == b"LGPL 2.1\n"
    assert (bundle / "licenses/mimalloc-MIT.txt").read_bytes() == b"mimalloc MIT\n"
    assert (
        bundle / "relink/runtime.o"
    ).read_bytes() == inputs.runtime_object.read_bytes()
    assert (bundle / "build-inputs/runtime-link.map").is_file()
    assert (bundle / "build-inputs/runtime-all-build-packages.txt").is_file()
    with (bundle / "COMPONENTS.tsv").open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream, delimiter="\t"))
    assert {row["component"] for row in rows} == {
        "type2-runtime",
        "musl",
        "libfuse",
        "squashfuse",
        "zstd",
        "zlib",
        "mimalloc",
    }
    assert "reverse engineering" in (bundle / "RELINKING.md").read_text(
        encoding="utf-8"
    )


def test_rejects_build_metadata_that_differs_from_package_pins(tmp_path: Path) -> None:
    inputs = _fixture(tmp_path)
    inputs.apk_packages.write_text("musl-1.2.5-r10\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="APK inventory differs from pins"):
        MODULE.create_bundle(inputs)


def test_rejects_full_build_ledger_missing_linked_packages(tmp_path: Path) -> None:
    inputs = _fixture(tmp_path)
    inputs.all_build_packages.write_text("clang20-20.1.2-r0\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="absent from full build-package ledger"):
        MODULE.create_bundle(inputs)
