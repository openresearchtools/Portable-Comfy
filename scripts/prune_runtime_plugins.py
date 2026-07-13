#!/usr/bin/env python3
"""Apply and verify the pinned local-workstation runtime exclusion policy."""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import json
import shutil
from email.parser import Parser
from pathlib import Path


SCHEMA_VERSION = 1
POLICY_ID = "portable-comfy-nvshmem-local-v1"
MANIFEST_NAME = "nvshmem-plugin-exclusions.json"
README_NAME = "README.md"
DIST_NAME = "nvidia-nvshmem-cu13"
DIST_VERSION = "3.4.5"
DIST_INFO_NAME = "nvidia_nvshmem_cu13-3.4.5.dist-info"
METADATA_SHA256 = "825ae32b105cfd50f7b375b9132e551f24dc32896c67c425349f6565d3b3fe46"
RECORD_SHA256 = "b997d9bbc91176e3141888d0741dbf38ed9a821b85e934dc9078cd047966f378"
METADATA_SIZE = 2094
RECORD_SIZE = 7173
LOST_CAPABILITIES = (
    "NVSHMEM multi-node execution",
    "MPI/PMI/PMIx/OpenSHMEM scheduler bootstrap",
    "InfiniBand/DevX/libfabric/UCX network transports",
)
RETAINED_CAPABILITIES = (
    "NVSHMEM core host/device libraries",
    "UID bootstrap",
    "local CUDA IPC/P2P paths",
)

# Relative to site-packages. These are optional NVSHMEM cluster bootstrap and
# network transport plugins. Ordinary ComfyUI is a local workstation process;
# shipping whatever MPI/PMIx/UCX/fabric stack happens to exist on the build
# runner would make the environment neither portable nor reproducible.
EXCLUDED: dict[str, tuple[str, int, str, tuple[str, ...]]] = {
    "nvidia/nvshmem/lib/nvshmem_bootstrap_mpi.so.3": (
        "3e74d9a3b5200a2ce2b07fb30e038d540837c0933de2ff236d249d8a67873a27",
        23952,
        "cluster-bootstrap",
        ("MPI (libmpi.so.40)",),
    ),
    "nvidia/nvshmem/lib/nvshmem_bootstrap_pmi.so.3": (
        "6a36f8359bafce06f795adc5a26f135fc605b0f8a7638123a0fe199ade7282de",
        39456,
        "cluster-bootstrap",
        ("PMI (runtime dlopen)",),
    ),
    "nvidia/nvshmem/lib/nvshmem_bootstrap_pmi2.so.3": (
        "d557779ea58feca4e841968ff2921e6836160b30bf249469061c72e25798233f",
        54112,
        "cluster-bootstrap",
        ("PMI-2 (runtime dlopen)",),
    ),
    "nvidia/nvshmem/lib/nvshmem_bootstrap_pmix.so.3": (
        "e48719713a8223e3e3c8eb3a949ee9546b94b7d4282c2f1df8eda3ff2168d169",
        56504,
        "cluster-bootstrap",
        ("PMIx (libpmix.so.2)",),
    ),
    "nvidia/nvshmem/lib/nvshmem_bootstrap_shmem.so.3": (
        "3bd997af7faec7fd3b3d3f415fb9d74cf66a5b19bb145ecda09c4fa52f75ecd5",
        19704,
        "cluster-bootstrap",
        ("OpenSHMEM (liboshmem.so.40)",),
    ),
    "nvidia/nvshmem/lib/nvshmem_transport_ibdevx.so.3": (
        "a1ed2a6d18bcd56ecbaedbef82cb1de993b16b3597213fb675838852a29a2d5b",
        1042080,
        "cluster-transport",
        ("Mellanox DevX (libmlx5.so.1)", "InfiniBand verbs"),
    ),
    "nvidia/nvshmem/lib/nvshmem_transport_ibgda.so.3": (
        "a0189481792c908b10d8c72018ae7e58a0f8f2b3902badfdc18cbde282f4386e",
        1076872,
        "cluster-transport",
        ("Mellanox GPUDirect Async (libmlx5.so.1)", "InfiniBand verbs"),
    ),
    "nvidia/nvshmem/lib/nvshmem_transport_ibrc.so.3": (
        "03e48811b134feb920b0440cf219944876166b8e34877a0f831c660726a13bc1",
        1042816,
        "cluster-transport",
        ("InfiniBand RC/libibverbs (runtime dlopen)",),
    ),
    "nvidia/nvshmem/lib/nvshmem_transport_libfabric.so.3": (
        "539fac610565bb5aad80959715a91de27fc4babb2466c1f163a87171003fe01c",
        1026672,
        "cluster-transport",
        ("libfabric (libfabric.so.1)",),
    ),
    "nvidia/nvshmem/lib/nvshmem_transport_ucx.so.3": (
        "1c79a8749a2c493f32c60a551ff7947c3c57d8b4e0d77b63f03e1c3d542c12cb",
        55912,
        "cluster-transport",
        ("UCX (libucp.so.0/libucs.so.0)",),
    ),
}

# Core host/device artifacts and UID bootstrap are retained. The UID plugin is
# sufficient for NVSHMEM's non-scheduler bootstrap on a normal workstation;
# local CUDA IPC/P2P paths remain in the core library rather than a transport
# plugin from the excluded set.
RETAINED: dict[str, tuple[str, int, str]] = {
    "nvidia/nvshmem/lib/libnvshmem_device.a": (
        "4a2446f8488f4770e4f643d0ea63563430b308f9ec07ec01fc5b216ff1b7d3ed",
        2974614,
        "device-code",
    ),
    "nvidia/nvshmem/lib/libnvshmem_device.bc": (
        "39ed77a212066c98689a2cf003a889ddd6433a4a90cdb9ced08642db5ff4526b",
        31218648,
        "device-code",
    ),
    "nvidia/nvshmem/lib/libnvshmem_host.so.3": (
        "c43004bb93053aa70603a204fe0c9052bdd29f38822d3048599183f8d5930d8f",
        41241312,
        "core-host",
    ),
    "nvidia/nvshmem/lib/nvshmem_bootstrap_uid.so.3": (
        "69b2b46a146adec27389c3bbdb46efd3d0853dc32eead1f6e08d6d906ad70c82",
        73704,
        "local-uid-bootstrap",
    ),
}
EXPECTED_PLUGIN_PATHS = frozenset(EXCLUDED) | frozenset(
    {"nvidia/nvshmem/lib/nvshmem_bootstrap_uid.so.3"}
)


class PolicyError(RuntimeError):
    """Raised when installed bytes do not match the reviewed policy."""


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def locate(prefix: Path) -> tuple[Path, Path]:
    candidates: list[tuple[Path, Path]] = []
    for site_packages in sorted(prefix.glob("lib/python*/site-packages")):
        dist_info = site_packages / DIST_INFO_NAME
        if dist_info.is_dir() and not dist_info.is_symlink():
            candidates.append((site_packages, dist_info))
    if len(candidates) != 1:
        raise PolicyError(
            f"expected one {DIST_INFO_NAME}, found {len(candidates)} under {prefix}"
        )
    return candidates[0]


def require_file(path: Path, digest: str, size: int) -> None:
    if not path.is_file() or path.is_symlink():
        raise PolicyError(f"required pinned runtime file is missing or unsafe: {path}")
    if path.stat().st_size != size or sha256(path) != digest:
        raise PolicyError(f"pinned runtime file hash/size mismatch: {path}")


def validate_distribution(site_packages: Path, dist_info: Path) -> tuple[Path, Path]:
    metadata = dist_info / "METADATA"
    record = dist_info / "RECORD"
    require_file(metadata, METADATA_SHA256, METADATA_SIZE)
    require_file(record, RECORD_SHA256, RECORD_SIZE)
    parsed = Parser().parsestr(metadata.read_text(encoding="utf-8"))
    if parsed.get("Name") != DIST_NAME or parsed.get("Version") != DIST_VERSION:
        raise PolicyError("NVSHMEM distribution identity disagrees with pruning policy")
    record_entries: dict[str, tuple[str, str]] = {}
    with record.open(newline="", encoding="utf-8") as stream:
        for row in csv.reader(stream):
            if len(row) != 3 or row[0] in record_entries:
                raise PolicyError("NVSHMEM RECORD is malformed or contains duplicates")
            record_entries[row[0]] = (row[1], row[2])
    for relative, (digest, size, *_) in {**EXCLUDED, **RETAINED}.items():
        encoded = base64.urlsafe_b64encode(bytes.fromhex(digest)).rstrip(b"=").decode()
        if record_entries.get(relative) != (f"sha256={encoded}", str(size)):
            raise PolicyError(f"NVSHMEM RECORD disagrees with pinned file: {relative}")
    return metadata, record


def actual_plugin_paths(site_packages: Path) -> set[str]:
    library_root = site_packages / "nvidia/nvshmem/lib"
    result = {
        path.relative_to(site_packages).as_posix()
        for pattern in ("nvshmem_bootstrap_*.so*", "nvshmem_transport_*.so*")
        for path in library_root.glob(pattern)
        if path.is_file() or path.is_symlink()
    }
    return result


def readme_text() -> str:
    return (
        "# Deliberately excluded NVSHMEM cluster plugins\n\n"
        "Portable Comfy is a local ComfyUI workstation runtime. The pinned "
        "NVSHMEM wheel also contains optional cluster bootstrap and network "
        "transport plugins tied to external MPI, PMI/PMIx, OpenSHMEM, "
        "InfiniBand/DevX, libfabric and UCX installations. Those plugins are "
        "removed only after exact distribution, path, size, RECORD and SHA-256 "
        "validation; their original identities are retained in the adjacent "
        "JSON manifest.\n\n"
        "The NVSHMEM core host/device libraries and UID bootstrap remain. Local "
        "CUDA IPC/P2P use needed by an ordinary workstation remains available. "
        "This build does not support NVSHMEM multi-node jobs or scheduler/HPC "
        "bootstrap and transports. Use a separately managed cluster runtime for "
        "those workloads.\n"
    )


def excluded_records() -> list[dict[str, object]]:
    return [
        {
            "category": category,
            "external_interfaces": list(external),
            "path": path,
            "upstream_sha256": digest,
            "upstream_size": size,
        }
        for path, (digest, size, category, external) in sorted(EXCLUDED.items())
    ]


def retained_records(
    *, finalized: bool, site_packages: Path
) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for path, (digest, size, purpose) in sorted(RETAINED.items()):
        record: dict[str, object] = {
            "path": path,
            "purpose": purpose,
            "upstream_sha256": digest,
            "upstream_size": size,
        }
        if finalized:
            payload = site_packages / path
            if not payload.is_file() or payload.is_symlink():
                raise PolicyError(f"retained NVSHMEM file is missing or unsafe: {path}")
            record.update(
                payload_sha256=sha256(payload), payload_size=payload.stat().st_size
            )
        records.append(record)
    return records


def load_manifest(manifest_root: Path) -> dict[str, object]:
    try:
        value = json.loads((manifest_root / MANIFEST_NAME).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise PolicyError(f"invalid runtime exclusions manifest: {error}") from error
    if not isinstance(value, dict):
        raise PolicyError("runtime exclusions manifest is not an object")
    return value


def expected_distribution(
    site_packages: Path, metadata: Path, record: Path
) -> dict[str, object]:
    return {
        "metadata_path": metadata.relative_to(site_packages).as_posix(),
        "metadata_sha256": METADATA_SHA256,
        "name": DIST_NAME,
        "record_path": record.relative_to(site_packages).as_posix(),
        "record_sha256": RECORD_SHA256,
        "version": DIST_VERSION,
    }


def validate_readme(manifest_root: Path, value: dict[str, object]) -> None:
    readme_value = value.get("readme")
    readme = manifest_root / README_NAME
    if (
        not isinstance(readme_value, dict)
        or set(readme_value) != {"path", "sha256", "size"}
        or readme_value.get("path") != README_NAME
        or not readme.is_file()
        or readme.is_symlink()
        or readme_value.get("sha256") != sha256(readme)
        or readme_value.get("size") != readme.stat().st_size
    ):
        raise PolicyError("runtime exclusions README is missing or altered")


def validate_fixed_manifest_fields(
    value: dict[str, object],
    *,
    site_packages: Path,
    metadata: Path,
    record: Path,
    finalized: bool,
) -> None:
    expected_keys = {
        "distribution",
        "excluded_files",
        "finalized",
        "lost_capabilities",
        "platform",
        "policy_id",
        "readme",
        "retained_capabilities",
        "retained_files",
        "schema_version",
    }
    if (
        set(value) != expected_keys
        or value.get("schema_version") != SCHEMA_VERSION
        or value.get("policy_id") != POLICY_ID
        or value.get("platform") != "linux-x86_64"
        or value.get("finalized") is not finalized
        or value.get("distribution")
        != expected_distribution(site_packages, metadata, record)
        or value.get("excluded_files") != excluded_records()
        or value.get("lost_capabilities") != list(LOST_CAPABILITIES)
        or value.get("retained_capabilities") != list(RETAINED_CAPABILITIES)
    ):
        raise PolicyError("runtime exclusions manifest has unsupported policy fields")


def prune(prefix: Path, manifest_root: Path) -> None:
    site_packages, dist_info = locate(prefix)
    metadata, record = validate_distribution(site_packages, dist_info)
    actual = actual_plugin_paths(site_packages)
    if actual != EXPECTED_PLUGIN_PATHS:
        difference = sorted(actual ^ EXPECTED_PLUGIN_PATHS)
        raise PolicyError(f"unreviewed or missing NVSHMEM plugin: {difference[0]}")
    for relative, (digest, size, *_) in {**EXCLUDED, **RETAINED}.items():
        require_file(site_packages / relative, digest, size)
    if manifest_root.is_symlink() or (
        manifest_root.exists() and not manifest_root.is_dir()
    ):
        raise PolicyError(f"runtime exclusions path is unsafe: {manifest_root}")
    if manifest_root.exists():
        shutil.rmtree(manifest_root)
    manifest_root.mkdir(parents=True)
    for relative in EXCLUDED:
        (site_packages / relative).unlink()
    readme = manifest_root / README_NAME
    readme.write_text(readme_text(), encoding="utf-8")
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "policy_id": POLICY_ID,
        "platform": "linux-x86_64",
        "finalized": False,
        "distribution": expected_distribution(site_packages, metadata, record),
        "excluded_files": excluded_records(),
        "retained_files": retained_records(
            finalized=False, site_packages=site_packages
        ),
        "lost_capabilities": list(LOST_CAPABILITIES),
        "retained_capabilities": list(RETAINED_CAPABILITIES),
        "readme": {
            "path": README_NAME,
            "sha256": sha256(readme),
            "size": readme.stat().st_size,
        },
    }
    (manifest_root / MANIFEST_NAME).write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def finalize(prefix: Path, manifest_root: Path) -> None:
    site_packages, dist_info = locate(prefix)
    validate_distribution(site_packages, dist_info)
    value = load_manifest(manifest_root)
    metadata = dist_info / "METADATA"
    record = dist_info / "RECORD"
    validate_fixed_manifest_fields(
        value,
        site_packages=site_packages,
        metadata=metadata,
        record=record,
        finalized=False,
    )
    if value.get("retained_files") != retained_records(
        finalized=False, site_packages=site_packages
    ):
        raise PolicyError(
            "runtime exclusions retained inventory changed before finalization"
        )
    if actual_plugin_paths(site_packages) != {
        "nvidia/nvshmem/lib/nvshmem_bootstrap_uid.so.3"
    }:
        raise PolicyError(
            "excluded or unreviewed NVSHMEM plugins remain before finalization"
        )
    validate_readme(manifest_root, value)
    value["retained_files"] = retained_records(
        finalized=True, site_packages=site_packages
    )
    value["finalized"] = True
    (manifest_root / MANIFEST_NAME).write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    verify(prefix, manifest_root)


def verify(prefix: Path, manifest_root: Path) -> None:
    site_packages, dist_info = locate(prefix)
    metadata, record = validate_distribution(site_packages, dist_info)
    value = load_manifest(manifest_root)
    validate_fixed_manifest_fields(
        value,
        site_packages=site_packages,
        metadata=metadata,
        record=record,
        finalized=True,
    )
    if actual_plugin_paths(site_packages) != {
        "nvidia/nvshmem/lib/nvshmem_bootstrap_uid.so.3"
    }:
        raise PolicyError("excluded or unreviewed NVSHMEM plugins remain in payload")
    for relative in EXCLUDED:
        if (site_packages / relative).exists() or (
            site_packages / relative
        ).is_symlink():
            raise PolicyError(f"excluded NVSHMEM plugin remains in payload: {relative}")
    if value.get("retained_files") != retained_records(
        finalized=True, site_packages=site_packages
    ):
        raise PolicyError("retained NVSHMEM file inventory mismatch")
    validate_readme(manifest_root, value)
    actual_manifest_files = {
        path.name
        for path in manifest_root.iterdir()
        if path.is_file() and not path.is_symlink()
    }
    if actual_manifest_files != {MANIFEST_NAME, README_NAME}:
        raise PolicyError("runtime exclusions directory contains unexpected files")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("prune", "finalize", "verify"))
    parser.add_argument("prefix", type=Path)
    parser.add_argument("--manifest-root", required=True, type=Path)
    arguments = parser.parse_args()
    prefix = arguments.prefix.resolve()
    manifest_root = arguments.manifest_root.resolve()
    try:
        if arguments.mode == "prune":
            prune(prefix, manifest_root)
        elif arguments.mode == "finalize":
            finalize(prefix, manifest_root)
        else:
            verify(prefix, manifest_root)
    except PolicyError as error:
        raise SystemExit(f"runtime plugin policy error: {error}") from error


if __name__ == "__main__":
    main()
