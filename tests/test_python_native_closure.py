from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/python_native_closure.py"
SPEC = importlib.util.spec_from_file_location("python_native_closure", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
closure = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = closure
SPEC.loader.exec_module(closure)


def test_host_native_abi_is_intentionally_narrow() -> None:
    assert closure.DRIVER_HOST_ABI == {"libcuda.so.1", "libnvidia-ml.so.1"}
    assert "libstdc++.so.6" not in closure.GLIBC_HOST_ABI
    assert "libgcc_s.so.1" not in closure.GLIBC_HOST_ABI
    assert "libssl.so.3" not in closure.GLIBC_HOST_ABI


def test_origin_entry_is_relative_to_each_consumer(tmp_path: Path) -> None:
    binary = tmp_path / "lib/python3.13/lib-dynload/_ssl.so"
    native = tmp_path / "lib/portable-native"
    assert closure.origin_entry(binary, native) == "$ORIGIN/../../portable-native"
    assert closure.is_origin_rpath("$ORIGIN/../../portable-native")
    assert not closure.is_origin_rpath("/usr/lib/x86_64-linux-gnu")
    assert not closure.is_origin_rpath("relative/to/current-directory")


def test_package_query_paths_include_both_usrmerge_spellings() -> None:
    canonical = Path("/usr/lib/x86_64-linux-gnu/libbz2.so.1.0.4")

    candidates = closure.package_query_paths(canonical)

    assert canonical in candidates
    assert Path("/lib/x86_64-linux-gnu/libbz2.so.1.0.4") in candidates


def test_package_owner_accepts_legacy_usrmerge_database_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canonical = Path("/usr/lib/x86_64-linux-gnu/libbz2.so.1.0.4")

    def completed(
        command: list[str], *, check: bool = True, env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        del check, env
        if command[:2] == ["dpkg-query", "-S"]:
            if command[2] == "/lib/x86_64-linux-gnu/libbz2.so.1.0.4":
                return subprocess.CompletedProcess(
                    command,
                    0,
                    "libbz2-1.0:amd64: /lib/x86_64-linux-gnu/libbz2.so.1.0.4\n",
                    "",
                )
            return subprocess.CompletedProcess(command, 1, "", "not found")
        assert command[:2] == ["dpkg-query", "-W"]
        return subprocess.CompletedProcess(
            command,
            0,
            "libbz2-1.0:amd64\t1.0.8-5build1\tamd64\tbzip2\t1.0.8-5build1\n",
            "",
        )

    monkeypatch.setattr(closure, "run", completed)

    owner = closure.package_owner(canonical)

    assert owner.package == "libbz2-1.0:amd64"
    assert owner.source_package == "bzip2"
    assert owner.version == "1.0.8-5build1"


def test_ldd_parser_preserves_paths_with_spaces(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def completed(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=["ldd"],
            returncode=0,
            stdout=(
                "libpython3.13.so.1.0 => "
                "/tmp/Portable Comfy/runtime/python/lib/libpython3.13.so.1.0 "
                "(0x00007f00)\n"
            ),
            stderr="",
        )

    monkeypatch.setattr(closure, "run", completed)
    resolved, _ = closure.ldd_resolutions(Path("/unused/python"))
    assert resolved["libpython3.13.so.1.0"] == Path(
        "/tmp/Portable Comfy/runtime/python/lib/libpython3.13.so.1.0"
    )


def test_debian_common_license_references_include_full_text(tmp_path: Path) -> None:
    notice = tmp_path / "copyright"
    notice.write_text(
        "See /usr/share/common-licenses/GPL-3. Also common-licenses/Apache-2.0. "
        "The alternatives are /usr/share/common-licenses/"
        "{MPL-1.1,GPL-2, LGPL-2.1}.\n",
        encoding="utf-8",
    )
    expected = {"Apache-2.0", "GPL-2", "GPL-3", "LGPL-2.1", "MPL-1.1"}
    assert closure.referenced_common_licenses({notice}) == expected
    license_root = tmp_path / "licenses"
    license_root.mkdir()
    records = closure.copy_common_licenses(license_root, {notice})
    assert {record["name"] for record in records} == expected
    assert all(
        (license_root / str(record["path"])).stat().st_size > 0 for record in records
    )


def test_empty_native_inventory_is_bound_and_extra_files_are_rejected(
    tmp_path: Path,
) -> None:
    prefix = tmp_path / "python"
    native = prefix / closure.NATIVE_SUBDIRECTORY
    native.mkdir(parents=True)
    license_root = tmp_path / "licenses"
    license_root.mkdir()
    readme = license_root / "README.md"
    readme.write_text("No copied libraries in fixture.\n", encoding="utf-8")
    inventory = {
        "schema_version": closure.SCHEMA_VERSION,
        "platform": "linux-x86_64",
        "native_directory": closure.NATIVE_SUBDIRECTORY.as_posix(),
        "host_abi": {
            "driver": sorted(closure.DRIVER_HOST_ABI),
            "glibc_kernel": sorted(closure.GLIBC_HOST_ABI),
            "interpreters": sorted(closure.ALLOWED_INTERPRETERS),
        },
        "common_licenses": [],
        "libraries": [],
        "packages": [],
        "readme": {
            "path": "README.md",
            "sha256": closure.sha256(readme),
            "size": readme.stat().st_size,
        },
        "summary": {"common_licenses": 0, "libraries": 0, "packages": 0},
    }
    (license_root / "packages.json").write_text(json.dumps(inventory), encoding="utf-8")
    closure.verify_inventory(prefix, license_root)

    (native / "unlisted.so").write_bytes(b"not inventoried")
    with pytest.raises(closure.ClosureError, match="inventory disagrees"):
        closure.verify_inventory(prefix, license_root)


def test_index_includes_safe_symlink_aliases_and_dt_sonames(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prefix = tmp_path / "python"
    library = prefix / "wheel/lib/libimplementation.so.12.0"
    library.parent.mkdir(parents=True)
    library.write_bytes(b"\x7fELFfixture")
    (library.parent / "libalias.so.12").symlink_to(library.name)

    monkeypatch.setattr(
        closure,
        "dynamic_info",
        lambda path: closure.DynamicInfo((), (), None, "libsoname.so.12"),
    )
    indexed = closure.index_prefix_libraries(prefix.resolve())
    assert set(indexed) == {
        "libalias.so.12",
        "libimplementation.so.12.0",
        "libsoname.so.12",
    }
    assert all(
        candidates
        == [
            closure.InternalCandidate(
                target=library.resolve(), directory=library.parent.resolve()
            )
        ]
        for candidates in indexed.values()
    )


def test_bundle_prefers_pinned_soname_over_runner_cuda(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prefix = (tmp_path / "python").resolve()
    binary = prefix / "wheel/lib/libconsumer.so"
    candidate = prefix / "wheel/cuda/libpinned-cusolver.so"
    binary.parent.mkdir(parents=True)
    candidate.parent.mkdir(parents=True)
    binary.write_bytes(b"\x7fELFconsumer")
    candidate.write_bytes(b"\x7fELFpinned")
    soname = "libcusolverMg.so.12"
    patched: set[Path] = set()

    def info(path: Path) -> object:
        if path.resolve() == binary:
            return closure.DynamicInfo((soname,), (), None, None)
        return closure.DynamicInfo((), (), None, soname)

    def patch(path: Path, directories: set[Path]) -> None:
        if path.resolve() == binary:
            patched.update(directory.resolve() for directory in directories)

    def resolutions(path: Path) -> tuple[dict[str, Path | None], str]:
        if path.resolve() != binary:
            return {}, ""
        selected = (
            candidate.resolve()
            if candidate.parent.resolve() in patched
            else Path("/usr/local/cuda-13.3/lib64/libcusolverMg.so.12")
        )
        return {soname: selected}, str(selected)

    monkeypatch.setattr(closure, "dynamic_info", info)
    monkeypatch.setattr(closure, "set_relative_rpaths", patch)
    monkeypatch.setattr(closure, "ldd_resolutions", resolutions)
    monkeypatch.setattr(closure, "write_inventory", lambda *args: None)
    monkeypatch.setattr(closure, "audit", lambda *args: None)
    monkeypatch.setattr(
        closure,
        "package_owner",
        lambda path: pytest.fail(f"runner CUDA must not be inventoried: {path}"),
    )

    closure.bundle(prefix, tmp_path / "licenses")
    alias = candidate.parent / soname
    assert alias.is_symlink()
    assert alias.resolve() == candidate
    assert candidate.parent.resolve() in patched


def test_same_internal_payload_through_multiple_alias_directories_is_not_ambiguous(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prefix = (tmp_path / "python").resolve()
    binary = prefix / "consumer/libconsumer.so"
    target = prefix / "implementation/libcuda-component.so.12.0"
    alias_directory = prefix / "wheel/lib"
    binary.parent.mkdir(parents=True)
    target.parent.mkdir(parents=True)
    alias_directory.mkdir(parents=True)
    binary.write_bytes(b"\x7fELFconsumer")
    target.write_bytes(b"\x7fELFimplementation")
    soname = "libcuda-component.so.12"
    (alias_directory / soname).symlink_to(Path("../..") / target.relative_to(prefix))
    patched: set[Path] = set()

    monkeypatch.setattr(
        closure,
        "set_relative_rpaths",
        lambda path, directories: patched.update(directories),
    )
    monkeypatch.setattr(
        closure,
        "ldd_resolutions",
        lambda path: ({soname: target.resolve()}, "resolved inside prefix"),
    )

    selected = closure.prefer_internal_dependency(
        binary,
        soname,
        [
            closure.InternalCandidate(target.resolve(), target.parent.resolve()),
            closure.InternalCandidate(target.resolve(), alias_directory.resolve()),
        ],
        prefix,
    )
    assert selected == target.resolve()
    assert patched == {alias_directory.resolve()}
