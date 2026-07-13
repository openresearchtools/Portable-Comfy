from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/prune_runtime_plugins.py"
SPEC = importlib.util.spec_from_file_location("prune_runtime_plugins", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
policy = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = policy
SPEC.loader.exec_module(policy)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def record_hash(value: str) -> str:
    encoded = base64.urlsafe_b64encode(bytes.fromhex(value)).rstrip(b"=")
    return f"sha256={encoded.decode()}"


def make_pinned_fixture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path, Path]:
    prefix = tmp_path / "runtime/python"
    site_packages = prefix / "lib/python3.13/site-packages"
    dist_info = site_packages / policy.DIST_INFO_NAME
    dist_info.mkdir(parents=True)

    excluded_payload = b"optional MPI plugin"
    retained_payloads = {
        "nvidia/nvshmem/lib/libnvshmem_device.a": b"device archive",
        "nvidia/nvshmem/lib/libnvshmem_device.bc": b"device bitcode",
        "nvidia/nvshmem/lib/libnvshmem_host.so.3": b"host ELF fixture",
        "nvidia/nvshmem/lib/nvshmem_bootstrap_uid.so.3": b"UID plugin fixture",
    }
    excluded = {
        "nvidia/nvshmem/lib/nvshmem_bootstrap_mpi.so.3": (
            digest(excluded_payload),
            len(excluded_payload),
            "cluster-bootstrap",
            ("MPI (libmpi.so.40)",),
        )
    }
    retained = {
        path: (digest(payload), len(payload), purpose)
        for (path, payload), purpose in zip(
            retained_payloads.items(),
            ("device-code", "device-code", "core-host", "local-uid-bootstrap"),
            strict=True,
        )
    }
    monkeypatch.setattr(policy, "EXCLUDED", excluded)
    monkeypatch.setattr(policy, "RETAINED", retained)
    monkeypatch.setattr(
        policy,
        "EXPECTED_PLUGIN_PATHS",
        frozenset(excluded) | {"nvidia/nvshmem/lib/nvshmem_bootstrap_uid.so.3"},
    )

    for relative, payload in {
        next(iter(excluded)): excluded_payload,
        **retained_payloads,
    }.items():
        destination = site_packages / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(payload)

    metadata = (
        "Metadata-Version: 2.1\n"
        f"Name: {policy.DIST_NAME}\n"
        f"Version: {policy.DIST_VERSION}\n\n"
    ).encode()
    metadata_path = dist_info / "METADATA"
    metadata_path.write_bytes(metadata)
    monkeypatch.setattr(policy, "METADATA_SHA256", digest(metadata))
    monkeypatch.setattr(policy, "METADATA_SIZE", len(metadata))

    record_lines = []
    for relative, (file_digest, size, *_) in {**excluded, **retained}.items():
        record_lines.append(f"{relative},{record_hash(file_digest)},{size}\n")
    record_path = dist_info / "RECORD"
    record_path.write_text("".join(record_lines), encoding="utf-8")
    monkeypatch.setattr(policy, "RECORD_SHA256", policy.sha256(record_path))
    monkeypatch.setattr(policy, "RECORD_SIZE", record_path.stat().st_size)

    cufile_dist_info = site_packages / policy.CUFILE_DIST_INFO_NAME
    cufile_dist_info.mkdir()
    cufile_excluded_payload = b"optional cuFile RDMA plugin"
    cufile_retained_payload = b"cuFile core ELF fixture"
    cufile_excluded = {
        "nvidia/cu13/lib/libcufile_rdma.so.1": (
            digest(cufile_excluded_payload),
            len(cufile_excluded_payload),
            "rdma-transport",
            (
                "Mellanox mlx5 (libmlx5.so.1)",
                "RDMA connection manager (librdmacm.so.1)",
                "InfiniBand verbs (libibverbs.so.1)",
            ),
        )
    }
    cufile_retained = {
        "nvidia/cu13/lib/libcufile.so.0": (
            digest(cufile_retained_payload),
            len(cufile_retained_payload),
            "core-library",
        )
    }
    monkeypatch.setattr(policy, "CUFILE_EXCLUDED", cufile_excluded)
    monkeypatch.setattr(policy, "CUFILE_RETAINED", cufile_retained)
    monkeypatch.setattr(
        policy,
        "CUFILE_EXPECTED_LIBRARY_PATHS",
        frozenset(cufile_excluded) | frozenset(cufile_retained),
    )
    for relative, payload in {
        next(iter(cufile_excluded)): cufile_excluded_payload,
        next(iter(cufile_retained)): cufile_retained_payload,
    }.items():
        destination = site_packages / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(payload)

    cufile_metadata = (
        "Metadata-Version: 2.4\n"
        f"Name: {policy.CUFILE_DIST_NAME}\n"
        f"Version: {policy.CUFILE_DIST_VERSION}\n\n"
    ).encode()
    cufile_metadata_path = cufile_dist_info / "METADATA"
    cufile_metadata_path.write_bytes(cufile_metadata)
    monkeypatch.setattr(policy, "CUFILE_METADATA_SHA256", digest(cufile_metadata))
    monkeypatch.setattr(policy, "CUFILE_METADATA_SIZE", len(cufile_metadata))

    cufile_record_lines = []
    for relative, (file_digest, size, *_) in {
        **cufile_excluded,
        **cufile_retained,
    }.items():
        cufile_record_lines.append(f"{relative},{record_hash(file_digest)},{size}\n")
    cufile_record_path = cufile_dist_info / "RECORD"
    cufile_record_path.write_text("".join(cufile_record_lines), encoding="utf-8")
    monkeypatch.setattr(
        policy, "CUFILE_RECORD_SHA256", policy.sha256(cufile_record_path)
    )
    monkeypatch.setattr(policy, "CUFILE_RECORD_SIZE", cufile_record_path.stat().st_size)
    return prefix, site_packages, tmp_path / "runtime/LICENSES/runtime-exclusions"


def test_prune_finalize_and_verify_are_exact_and_checksum_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prefix, site_packages, manifest_root = make_pinned_fixture(tmp_path, monkeypatch)
    excluded = site_packages / next(iter(policy.EXCLUDED))
    cufile_excluded = site_packages / next(iter(policy.CUFILE_EXCLUDED))

    policy.prune(prefix, manifest_root)
    assert not excluded.exists()
    assert not cufile_excluded.exists()
    assert (site_packages / "nvidia/nvshmem/lib/nvshmem_bootstrap_uid.so.3").is_file()
    cufile = site_packages / "nvidia/cu13/lib/libcufile.so.0"
    assert cufile.is_file()

    # The native-closure pass runs between pruning and finalization and may
    # change an ELF's RUNPATH. The final manifest deliberately binds those
    # post-repair bytes while retaining the reviewed upstream wheel identity.
    host = site_packages / "nvidia/nvshmem/lib/libnvshmem_host.so.3"
    host.write_bytes(host.read_bytes() + b"-post-patchelf")
    cufile.write_bytes(cufile.read_bytes() + b"-post-patchelf")
    policy.finalize(prefix, manifest_root)
    policy.verify(prefix, manifest_root)

    manifest_path = manifest_root / policy.MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["finalized"] is True
    assert manifest["lost_capabilities"] == list(policy.LOST_CAPABILITIES)
    retained = {entry["path"]: entry for entry in manifest["retained_files"]}
    assert retained[host.relative_to(site_packages).as_posix()][
        "payload_sha256"
    ] == policy.sha256(host)
    cufile_manifest = json.loads(
        (manifest_root / policy.CUFILE_MANIFEST_NAME).read_text(encoding="utf-8")
    )
    assert cufile_manifest["finalized"] is True
    cufile_retained = {
        entry["path"]: entry for entry in cufile_manifest["retained_files"]
    }
    assert cufile_retained[cufile.relative_to(site_packages).as_posix()][
        "payload_sha256"
    ] == policy.sha256(cufile)

    manifest["lost_capabilities"] = []
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(policy.PolicyError, match="unsupported policy fields"):
        policy.verify(prefix, manifest_root)


def test_prune_rejects_unreviewed_plugin_before_deleting_anything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prefix, site_packages, manifest_root = make_pinned_fixture(tmp_path, monkeypatch)
    excluded = site_packages / next(iter(policy.EXCLUDED))
    extra = site_packages / "nvidia/nvshmem/lib/nvshmem_transport_unknown.so.3"
    extra.write_bytes(b"unknown runner-dependent transport")

    with pytest.raises(policy.PolicyError, match="unreviewed or missing"):
        policy.prune(prefix, manifest_root)
    assert excluded.is_file()
    assert not manifest_root.exists()


def test_verify_rejects_reintroduced_excluded_plugin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prefix, site_packages, manifest_root = make_pinned_fixture(tmp_path, monkeypatch)
    excluded = site_packages / next(iter(policy.EXCLUDED))
    original = excluded.read_bytes()
    policy.prune(prefix, manifest_root)
    policy.finalize(prefix, manifest_root)

    excluded.write_bytes(original)
    with pytest.raises(policy.PolicyError, match="plugins remain"):
        policy.verify(prefix, manifest_root)


def test_prune_rejects_unreviewed_cufile_library_before_deleting_anything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prefix, site_packages, manifest_root = make_pinned_fixture(tmp_path, monkeypatch)
    nvshmem_excluded = site_packages / next(iter(policy.EXCLUDED))
    cufile_excluded = site_packages / next(iter(policy.CUFILE_EXCLUDED))
    extra = site_packages / "nvidia/cu13/lib/libcufile_future.so.2"
    extra.write_bytes(b"unknown optional transport")

    with pytest.raises(policy.PolicyError, match="unreviewed or missing cuFile"):
        policy.prune(prefix, manifest_root)
    assert nvshmem_excluded.is_file()
    assert cufile_excluded.is_file()
    assert not manifest_root.exists()


def test_verify_rejects_reintroduced_cufile_rdma_library(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prefix, site_packages, manifest_root = make_pinned_fixture(tmp_path, monkeypatch)
    excluded = site_packages / next(iter(policy.CUFILE_EXCLUDED))
    original = excluded.read_bytes()
    policy.prune(prefix, manifest_root)
    policy.finalize(prefix, manifest_root)

    excluded.write_bytes(original)
    with pytest.raises(policy.PolicyError, match="cuFile libraries remain"):
        policy.verify(prefix, manifest_root)


def test_verify_rejects_changed_cufile_policy_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prefix, _site_packages, manifest_root = make_pinned_fixture(tmp_path, monkeypatch)
    policy.prune(prefix, manifest_root)
    policy.finalize(prefix, manifest_root)
    manifest_path = manifest_root / policy.CUFILE_MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["retained_capabilities"] = []
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(policy.PolicyError, match="cuFile exclusions manifest"):
        policy.verify(prefix, manifest_root)


def test_exact_cufile_wheel_policy_constants_are_pinned() -> None:
    assert policy.CUFILE_DIST_NAME == "nvidia-cufile"
    assert policy.CUFILE_DIST_VERSION == "1.15.1.6"
    assert policy.CUFILE_METADATA_SHA256 == (
        "35ef649dd5370d74512351b45aa9ddc81715ae8c806326c2c5ec61d80dc97aca"
    )
    assert policy.CUFILE_METADATA_SIZE == 1692
    assert policy.CUFILE_RECORD_SHA256 == (
        "169df085952dcba197c3f7893244b063cbd75f3cee5b9a2988d273f0eff87a54"
    )
    assert policy.CUFILE_RECORD_SIZE == 819
    assert policy.CUFILE_EXCLUDED == {
        "nvidia/cu13/lib/libcufile_rdma.so.1": (
            "088823e09cda19bbeae292e164e849ec72672339fbd86b3753eff78433e4eab9",
            43320,
            "rdma-transport",
            (
                "Mellanox mlx5 (libmlx5.so.1)",
                "RDMA connection manager (librdmacm.so.1)",
                "InfiniBand verbs (libibverbs.so.1)",
            ),
        )
    }
    assert policy.CUFILE_RETAINED == {
        "nvidia/cu13/lib/libcufile.so.0": (
            "1ecaf17d38957a41473a7fc8f29d569579e2a9079a26c4d9b89d8da330051483",
            3170800,
            "core-library",
        )
    }
