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
    return prefix, site_packages, tmp_path / "runtime/LICENSES/runtime-exclusions"


def test_prune_finalize_and_verify_are_exact_and_checksum_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prefix, site_packages, manifest_root = make_pinned_fixture(tmp_path, monkeypatch)
    excluded = site_packages / next(iter(policy.EXCLUDED))

    policy.prune(prefix, manifest_root)
    assert not excluded.exists()
    assert (site_packages / "nvidia/nvshmem/lib/nvshmem_bootstrap_uid.so.3").is_file()

    # The native-closure pass runs between pruning and finalization and may
    # change an ELF's RUNPATH. The final manifest deliberately binds those
    # post-repair bytes while retaining the reviewed upstream wheel identity.
    host = site_packages / "nvidia/nvshmem/lib/libnvshmem_host.so.3"
    host.write_bytes(host.read_bytes() + b"-post-patchelf")
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
