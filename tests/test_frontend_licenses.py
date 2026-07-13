from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/collect_frontend_licenses.py"


def package(root: Path, name: str, version: str, *, notice: bool) -> Path:
    destination = root / name.replace("/", "_")
    destination.mkdir(parents=True)
    (destination / "package.json").write_text(
        json.dumps(
            {
                "name": name,
                "version": version,
                "license": "MIT",
                "author": "Example Author",
                "homepage": "https://example.invalid/package",
            }
        ),
        encoding="utf-8",
    )
    if notice:
        (destination / "LICENSE.md").write_text("Example MIT terms\n", encoding="utf-8")
    return destination


def test_collects_relocatable_frontend_dependency_notices(tmp_path: Path) -> None:
    source = tmp_path / "frontend-source"
    source.mkdir()
    licensed = package(source, "one", "1.0.0", notice=True)
    metadata_only = package(source, "@scope/two", "2.0.0", notice=False)
    inventory = tmp_path / "pnpm-licenses.json"
    inventory.write_text(
        json.dumps(
            {
                "MIT": [
                    {
                        "name": "one",
                        "versions": ["1.0.0"],
                        "paths": [str(licensed)],
                    },
                    {
                        "name": "@scope/two",
                        "versions": ["2.0.0"],
                        "paths": [str(metadata_only)],
                    },
                ]
            }
        ),
        encoding="utf-8",
    )
    destination = tmp_path / "notices"

    subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            str(inventory),
            str(source),
            str(destination),
            "--frontend-version",
            "1.2.3",
            "--frontend-commit",
            "a" * 40,
        ],
        check=True,
    )

    result = json.loads((destination / "packages.json").read_text(encoding="utf-8"))
    assert result["frontend"] == {"version": "1.2.3", "commit": "a" * 40}
    assert result["summary"] == {
        "distributions": 2,
        "with_notice_files": 1,
        "metadata_only": 1,
    }
    assert {item["name"] for item in result["packages"]} == {"one", "@scope/two"}
    for item in result["packages"]:
        assert not Path(item["metadata_file"]).is_absolute()
        assert (destination / item["metadata_file"]).is_file()
        for notice in item["notice_files"]:
            assert not Path(notice).is_absolute()
            assert (destination / notice).read_text() == "Example MIT terms\n"
    assert str(tmp_path) not in (destination / "packages.json").read_text()


def test_rejects_package_paths_outside_pinned_source(tmp_path: Path) -> None:
    source = tmp_path / "frontend-source"
    source.mkdir()
    outside = package(tmp_path / "outside", "escape", "1.0.0", notice=True)
    inventory = tmp_path / "pnpm-licenses.json"
    inventory.write_text(
        json.dumps(
            {
                "MIT": [
                    {
                        "name": "escape",
                        "versions": ["1.0.0"],
                        "paths": [str(outside)],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            str(inventory),
            str(source),
            str(tmp_path / "notices"),
            "--frontend-version",
            "1.2.3",
            "--frontend-commit",
            "a" * 40,
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "escapes the pinned frontend source" in result.stderr


def test_records_compiled_only_dependency_and_binds_exact_asset(
    tmp_path: Path,
) -> None:
    source = tmp_path / "frontend-source"
    source.mkdir()
    ordinary = package(source, "ordinary", "1.0.0", notice=True)
    inventory = tmp_path / "pnpm-licenses.json"
    inventory.write_text(
        json.dumps(
            {
                "MIT": [
                    {
                        "name": "ordinary",
                        "versions": ["1.0.0"],
                        "paths": [str(ordinary)],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    compiled = tmp_path / "compiled"
    (compiled / "fonts").mkdir(parents=True)
    asset = compiled / "fonts/example.woff2"
    asset.write_bytes(b"exact compiled font")
    asset_hash = hashlib.sha256(asset.read_bytes()).hexdigest()
    license_path = tmp_path / "FONT-LICENSE.txt"
    license_path.write_text("Example font terms\n", encoding="utf-8")
    destination = tmp_path / "notices"

    subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            str(inventory),
            str(source),
            str(destination),
            "--frontend-version",
            "1.2.3",
            "--frontend-commit",
            "a" * 40,
            "--compiled-root",
            str(compiled),
            "--additional-package",
            "@font/example",
            "2.0.0",
            "OFL-1.1",
            str(license_path),
            "--additional-asset",
            "@font/example",
            "2.0.0",
            "fonts/example.woff2",
            asset_hash,
        ],
        check=True,
    )

    result = json.loads((destination / "packages.json").read_text(encoding="utf-8"))
    extra = next(item for item in result["packages"] if item["name"] == "@font/example")
    assert extra["license"] == "OFL-1.1"
    assert extra["bundled_assets"] == [
        {
            "path": "fonts/example.woff2",
            "sha256": asset_hash,
            "size": len(b"exact compiled font"),
        }
    ]
    assert len(extra["notice_files"]) == 1
    assert (destination / extra["notice_files"][0]).read_text() == (
        "Example font terms\n"
    )


def test_workspace_fallback_is_not_used_for_different_license(
    tmp_path: Path,
) -> None:
    source = tmp_path / "frontend-source"
    source.mkdir()
    (source / "package.json").write_text(
        json.dumps(
            {
                "name": "frontend",
                "version": "1.2.3",
                "license": "GPL-3.0-only",
                "dependencies": {"@comfyorg/mit-types": "workspace:*"},
            }
        ),
        encoding="utf-8",
    )
    workspace = source / "packages/mit-types"
    workspace.mkdir(parents=True)
    (workspace / "package.json").write_text(
        json.dumps(
            {
                "name": "@comfyorg/mit-types",
                "version": "1.0.0",
                "license": "MIT",
            }
        ),
        encoding="utf-8",
    )
    ordinary = package(source, "ordinary", "1.0.0", notice=True)
    inventory = tmp_path / "pnpm-licenses.json"
    inventory.write_text(
        json.dumps(
            {
                "MIT": [
                    {
                        "name": "ordinary",
                        "versions": ["1.0.0"],
                        "paths": [str(ordinary)],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    root_license = tmp_path / "GPL.txt"
    root_license.write_text("GPL project terms\n", encoding="utf-8")
    destination = tmp_path / "notices"

    subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            str(inventory),
            str(source),
            str(destination),
            "--frontend-version",
            "1.2.3",
            "--frontend-commit",
            "a" * 40,
            "--workspace-license",
            str(root_license),
        ],
        check=True,
    )

    result = json.loads((destination / "packages.json").read_text(encoding="utf-8"))
    mit_package = next(
        item for item in result["packages"] if item["name"] == "@comfyorg/mit-types"
    )
    assert mit_package["license"] == "MIT"
    assert mit_package["notice_files"] == []
