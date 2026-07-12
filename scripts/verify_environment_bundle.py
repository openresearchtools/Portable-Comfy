#!/usr/bin/env python3
"""Strictly validate an atomic Portable Comfy environment payload."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
from pathlib import Path, PurePosixPath


HEX = re.compile(r"^[0-9a-f]{64}$")
COMMIT = re.compile(r"^[0-9a-f]{40}$")
VERSION = re.compile(
    r"^[0-9]+(?:\.[0-9]+){2}(?:[-+._]?[0-9A-Za-z][0-9A-Za-z._+-]{0,63})?$"
)
FROZEN_REQUIREMENT = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^=\s]+)$")
DISTRIBUTION_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
GENERATION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,191}$")
STRICT_TOP_LEVEL = {"ComfyUI", "manifest", "LICENSES"}
IDENTITY_NAME = "PORTABLE-COMFY-IDENTITY.json"
RUNTIME_LICENSE_INVENTORY = "ComfyUI/runtime/LICENSES/python-packages/packages.json"
RUNTIME_INSTALLED_REQUIREMENTS = "ComfyUI/runtime/installed-requirements.txt"
CORE_LICENSE_FILES = (
    "ComfyUI/LICENSE",
    "ComfyUI/frontend/LICENSE",
    "ComfyUI/frontend/THIRD_PARTY_NOTICES.md",
)
RUNTIME_LICENSE_FILES = (
    "ComfyUI/runtime/python/LICENSE.txt",
    RUNTIME_LICENSE_INVENTORY,
)
REQUIRED_RUNTIME_LICENSE_PACKAGES = frozenset(
    {
        "comfyui-frontend-package",
        "torch",
        "torchvision",
        "torchaudio",
        "nvidia-cublas",
        "nvidia-cuda-runtime",
        "nvidia-cudnn-cu13",
    }
)


def sha256(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def fail(message: str) -> None:
    raise SystemExit(f"invalid complete Core bundle: {message}")


def safe_payload_path(path_text: object) -> PurePosixPath:
    if not isinstance(path_text, str):
        fail("files entry path is not a string")
    if "\\" in path_text or "\x00" in path_text:
        fail(f"unsafe environment path: {path_text!r}")
    path = PurePosixPath(path_text)
    if (
        path.is_absolute()
        or ".." in path.parts
        or len(path.parts) < 2
        or path.parts[0] != "ComfyUI"
    ):
        fail(f"unsafe environment path: {path_text}")
    return path


def safe_link_target(root: Path, path: Path, target: object) -> str:
    if (
        not isinstance(target, str)
        or not target
        or "\\" in target
        or "\x00" in target
        or os.path.isabs(target)
    ):
        fail(f"unsafe link target for {path.relative_to(root)}")
    resolved = (path.parent / target).resolve(strict=False)
    comfyui = (root / "ComfyUI").resolve()
    try:
        resolved.relative_to(comfyui)
    except ValueError:
        fail(f"link escapes ComfyUI: {path.relative_to(root)} -> {target}")
    if not resolved.exists():
        fail(f"link target is missing: {path.relative_to(root)} -> {target}")
    return target


def load_manifest(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        fail(str(error))
    if not isinstance(value, dict):
        fail("environment manifest is not an object")
    return value


def require_license_files(
    root: Path,
    expected: dict[str, dict[str, object]],
    *,
    runtime: bool,
) -> None:
    required = CORE_LICENSE_FILES + (RUNTIME_LICENSE_FILES if runtime else ())
    for path_text in required:
        entry = expected.get(path_text)
        if (
            entry is None
            or entry.get("type") != "file"
            or not isinstance(entry.get("size"), int)
            or entry["size"] <= 0
        ):
            fail(f"required redistribution notice is missing or empty: {path_text}")
    if not runtime:
        return

    installed_entry = expected.get(RUNTIME_INSTALLED_REQUIREMENTS)
    if (
        installed_entry is None
        or installed_entry.get("type") != "file"
        or not isinstance(installed_entry.get("size"), int)
        or installed_entry["size"] <= 0
    ):
        fail("installed runtime requirements freeze is missing or empty")

    inventory_path = root / RUNTIME_LICENSE_INVENTORY
    try:
        inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        fail(f"invalid runtime package license inventory: {error}")
    if (
        not isinstance(inventory, dict)
        or inventory.get("schema_version") != 2
        or not isinstance(inventory.get("packages"), list)
        or not inventory["packages"]
    ):
        fail("runtime package license inventory has an unsupported schema")
    package_count = len(inventory["packages"])
    if inventory.get("summary") != {
        "distributions": package_count,
        "with_license_files": package_count,
        "metadata_only": 0,
        "unidentified": 0,
    }:
        fail("runtime package license inventory is not comprehensive")

    licensed: set[str] = set()
    inventory_versions: dict[str, str] = {}
    inventory_parent = PurePosixPath(RUNTIME_LICENSE_INVENTORY).parent
    for package in inventory["packages"]:
        if not isinstance(package, dict):
            fail("runtime package license inventory contains a malformed package")
        name = package.get("name")
        version = package.get("version")
        files = package.get("license_files")
        if (
            not isinstance(name, str)
            or not DISTRIBUTION_NAME.fullmatch(name)
            or not isinstance(version, str)
            or not version
            or "=" in version
            or any(character.isspace() for character in version)
            or not isinstance(files, list)
        ):
            fail("runtime package license inventory contains malformed fields")
        normalized_name = re.sub(r"[-_.]+", "-", name).lower()
        if normalized_name in inventory_versions:
            fail(
                "runtime package license inventory contains duplicate names: "
                f"{normalized_name}"
            )
        inventory_versions[normalized_name] = version
        if not files:
            fail(f"runtime package has no bundled license file: {normalized_name}")
        licensed.add(normalized_name)
        for relative_text in files:
            if not isinstance(relative_text, str):
                fail("runtime package license inventory has a malformed path")
            relative = PurePosixPath(relative_text)
            if (
                relative.is_absolute()
                or not relative.parts
                or ".." in relative.parts
                or "\\" in relative_text
                or "\x00" in relative_text
                or relative.as_posix() != relative_text
            ):
                fail("runtime package license inventory has an unsafe path")
            payload_path = (inventory_parent / relative).as_posix()
            entry = expected.get(payload_path)
            if (
                entry is None
                or entry.get("type") != "file"
                or not isinstance(entry.get("size"), int)
                or entry["size"] <= 0
            ):
                fail(
                    f"runtime package license file is missing or empty: {payload_path}"
                )
    missing = sorted(REQUIRED_RUNTIME_LICENSE_PACKAGES - licensed)
    if missing:
        fail("runtime package has no bundled license file: " + missing[0])
    frozen = read_frozen_requirements(root / RUNTIME_INSTALLED_REQUIREMENTS)
    if frozen != inventory_versions:
        fail("installed runtime requirements and package license inventory disagree")


def read_frozen_requirements(path: Path) -> dict[str, str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as error:
        fail(f"cannot read installed runtime requirements: {error}")
    if not lines:
        fail("installed runtime requirements are empty")
    frozen: dict[str, str] = {}
    for line_number, line in enumerate(lines, start=1):
        match = FROZEN_REQUIREMENT.fullmatch(line)
        if match is None:
            fail(
                "installed runtime requirements line is not exact "
                f"NAME==VERSION at line {line_number}"
            )
        name, version = match.groups()
        normalized_name = re.sub(r"[-_.]+", "-", name).lower()
        if normalized_name in frozen:
            fail(
                "installed runtime requirements contain a duplicate package: "
                f"{normalized_name}"
            )
        frozen[normalized_name] = version
    return frozen


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("--manifest", default="manifest/environment.json")
    parser.add_argument("--checksums", default="manifest/environment-checksums.sha256")
    parser.add_argument(
        "--portable-root",
        action="store_true",
        help="allow first-install files outside the atomic environment payload",
    )
    parser.add_argument(
        "--structural",
        action="store_true",
        help="allow an intentionally omitted runtime in development staging trees",
    )
    args = parser.parse_args()
    root = args.root.resolve()
    manifest_path = root / args.manifest
    checksums_path = root / args.checksums
    if (
        not manifest_path.is_file()
        or manifest_path.is_symlink()
        or not checksums_path.is_file()
        or checksums_path.is_symlink()
        or not manifest_path.parent.is_dir()
        or manifest_path.parent.is_symlink()
    ):
        fail("environment manifest or checksum list is missing")
    if not args.portable_root:
        unexpected = sorted(
            path.name for path in root.iterdir() if path.name not in STRICT_TOP_LEVEL
        )
        if unexpected:
            fail(f"unexpected top-level payload: {unexpected[0]}")
        metadata_root = root / "manifest"
        metadata_files = set()
        if metadata_root.is_dir() and not metadata_root.is_symlink():
            for path in metadata_root.rglob("*"):
                mode = path.lstat().st_mode
                if stat.S_ISDIR(mode):
                    continue
                if not stat.S_ISREG(mode):
                    fail(
                        f"invalid environment metadata entry: {path.relative_to(root)}"
                    )
                metadata_files.add(path.relative_to(root).as_posix())
        if metadata_files != {
            args.manifest,
            args.checksums,
        }:
            fail("environment metadata contains missing or unexpected files")

    manifest = load_manifest(manifest_path)
    if manifest.get("schema_version") != 2:
        fail("unsupported schema_version")
    if (
        manifest.get("bundle_type") != "environment"
        or manifest.get("app_id") != "portable-comfy"
    ):
        fail("wrong bundle identity")
    generation_id = manifest.get("generation_id")
    if not isinstance(generation_id, str) or not GENERATION.fullmatch(generation_id):
        fail("missing or malformed generation_id")
    for group in ("core", "frontend", "runtime"):
        if not isinstance(manifest.get(group), dict):
            fail(f"missing {group} object")
    core = manifest["core"]
    frontend = manifest["frontend"]
    runtime = manifest["runtime"]
    assert (
        isinstance(core, dict)
        and isinstance(frontend, dict)
        and isinstance(runtime, dict)
    )
    if set(core) != {"version", "tag", "commit"}:
        fail("Core identity fields are missing or unexpected")
    if (
        not isinstance(core.get("version"), str)
        or not VERSION.fullmatch(core["version"])
        or core.get("tag") != f"v{core['version']}"
        or not isinstance(core.get("commit"), str)
        or not COMMIT.fullmatch(core["commit"])
    ):
        fail("Core version, tag, or commit is malformed")
    if set(frontend) != {"version", "commit"}:
        fail("frontend identity fields are missing or unexpected")
    if (
        not isinstance(frontend.get("version"), str)
        or not VERSION.fullmatch(frontend["version"])
        or not isinstance(frontend.get("commit"), str)
        or not COMMIT.fullmatch(frontend["commit"])
    ):
        fail("frontend version or commit is malformed")
    if not args.portable_root:
        expected_root = f"Portable-Comfy-core-v{core['version']}"
        if root.name != expected_root:
            fail(
                "Core bundle root does not match manifest core.version: "
                f"expected {expected_root}, found {root.name}"
            )
    for group_name, group, fields in (
        ("core", core, ("version", "tag", "commit")),
        ("frontend", frontend, ("version", "commit")),
        (
            "runtime",
            runtime,
            (
                "python",
                "torch",
                "torchvision",
                "torchaudio",
                "cuda",
                "platform",
                "requirements_lock_path",
                "requirements_lock_sha256",
            ),
        ),
    ):
        for field in fields:
            if not isinstance(group.get(field), str) or not group[field]:
                fail(f"missing {group_name}.{field}")
    if not HEX.fullmatch(str(runtime["requirements_lock_sha256"])):
        fail("malformed runtime requirements digest")
    requirements_path = safe_payload_path(runtime["requirements_lock_path"])

    entries = manifest.get("files")
    if not isinstance(entries, list) or not entries:
        fail("files list is missing or empty")
    expected: dict[str, dict[str, object]] = {}
    for item in entries:
        if not isinstance(item, dict):
            fail("non-object files entry")
        path = safe_payload_path(item.get("path"))
        path_text = path.as_posix()
        if path_text in expected:
            fail(f"duplicate environment path: {path_text}")
        kind = item.get("type")
        if kind == "file":
            digest = item.get("sha256")
            size = item.get("size")
            if (
                not isinstance(digest, str)
                or not HEX.fullmatch(digest)
                or not isinstance(size, int)
                or isinstance(size, bool)
                or size < 0
                or set(item) != {"path", "type", "sha256", "size"}
            ):
                fail(f"malformed file entry: {path_text}")
        elif kind == "symlink":
            target = item.get("target")
            if (
                not isinstance(target, str)
                or not target
                or "\\" in target
                or "\x00" in target
                or os.path.isabs(target)
                or set(item) != {"path", "type", "target"}
            ):
                fail(f"malformed symlink entry: {path_text}")
        else:
            fail(f"unsupported entry type: {path_text}")
        expected[path_text] = item

    comfyui = root / "ComfyUI"
    if not comfyui.is_dir() or comfyui.is_symlink():
        fail("ComfyUI payload directory is missing or is a link")
    actual: set[str] = set()
    for path in comfyui.rglob("*"):
        mode = path.lstat().st_mode
        if stat.S_ISDIR(mode):
            continue
        relative = path.relative_to(root).as_posix()
        item = expected.get(relative)
        if item is None:
            fail(f"unlisted payload entry: {relative}")
        actual.add(relative)
        if stat.S_ISLNK(mode):
            if item["type"] != "symlink":
                fail(f"entry type mismatch: {relative}")
            target = safe_link_target(root, path, item["target"])
            if os.readlink(path) != target:
                fail(f"link target mismatch: {relative}")
        elif stat.S_ISREG(mode):
            if item["type"] != "file":
                fail(f"entry type mismatch: {relative}")
            if path.stat().st_size != item["size"] or sha256(path) != item["sha256"]:
                fail(f"checksum or size mismatch: {relative}")
        else:
            fail(f"special payload entry is forbidden: {relative}")
    missing = set(expected) - actual
    if missing:
        fail(f"manifest references missing entry: {sorted(missing)[0]}")

    checksum_entries: dict[str, str] = {}
    try:
        checksum_lines = checksums_path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as error:
        fail(str(error))
    for line in checksum_lines:
        digest, separator, path_text = line.partition("  ")
        if (
            separator != "  "
            or not HEX.fullmatch(digest)
            or path_text in checksum_entries
        ):
            fail("malformed environment checksum list")
        safe_payload_path(path_text)
        checksum_entries[path_text] = digest
    wanted_checksums = {
        path: str(item["sha256"])
        for path, item in expected.items()
        if item["type"] == "file"
    }
    if checksum_entries != wanted_checksums:
        fail("environment checksum list disagrees with manifest")

    require_license_files(root, expected, runtime=not args.structural)

    identity_path = comfyui / IDENTITY_NAME
    identity_entry = expected.get(f"ComfyUI/{IDENTITY_NAME}")
    if identity_entry is None or identity_entry["type"] != "file":
        fail(f"{IDENTITY_NAME} is absent from the payload manifest")
    try:
        identity = json.loads(identity_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        fail(f"invalid {IDENTITY_NAME}: {error}")
    wanted_identity = {
        "schema_version": 1,
        "app_id": manifest["app_id"],
        "generation_id": manifest["generation_id"],
        "core": core,
        "frontend": frontend,
        "runtime": runtime,
    }
    if identity != wanted_identity:
        fail(f"{IDENTITY_NAME} disagrees with the environment manifest")

    requirements_relative = requirements_path.as_posix()
    requirements_entry = expected.get(requirements_relative)
    if (
        requirements_entry is None
        or requirements_entry["type"] != "file"
        or requirements_entry["sha256"] != runtime["requirements_lock_sha256"]
    ):
        fail("runtime requirements lock is absent or has the wrong digest")

    required = [comfyui / "main.py", comfyui / "frontend/index.html"]
    if not args.structural:
        required.extend(
            [
                comfyui / "runtime/python/bin/python-portable",
                comfyui / "runtime/python/bin/repair-portable-entrypoints",
            ]
        )
    if any(not path.is_file() for path in required):
        fail("required Core, frontend, or runtime entrypoint is missing")
    file_count = sum(item["type"] == "file" for item in expected.values())
    link_count = len(expected) - file_count
    print(
        f"verified environment {generation_id}: {file_count} files, {link_count} links"
    )


if __name__ == "__main__":
    main()
