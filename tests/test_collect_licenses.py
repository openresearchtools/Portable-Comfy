from __future__ import annotations

import email
import importlib.util
import json
from importlib.metadata import PackagePath
from pathlib import Path

import pytest


SCRIPT = Path(__file__).parents[1] / "scripts" / "collect_licenses.py"
SPEC = importlib.util.spec_from_file_location("collect_licenses", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class FakeDistribution:
    def __init__(
        self,
        root: Path,
        *,
        name: str,
        version: str = "1.0",
        metadata: str = "License-Expression: MIT\n",
        files: tuple[str, ...] = (),
    ) -> None:
        self.root = root
        self.version = version
        self.metadata = email.message_from_string(
            f"Metadata-Version: 2.4\nName: {name}\nVersion: {version}\n{metadata}\n"
        )
        self.files = [PackagePath(path) for path in files]

    def locate_file(self, path: PackagePath) -> Path:
        return self.root / path


def test_collects_wheel_notice_and_skips_license_helper_module(tmp_path: Path):
    root = tmp_path / "site"
    notice = root / "example-1.0.dist-info/licenses/LICENSE"
    helper = root / "example/licenses/_spdx.py"
    notice.parent.mkdir(parents=True)
    helper.parent.mkdir(parents=True)
    notice.write_text("Example license text\n", encoding="utf-8")
    helper.write_text("NOT_A_NOTICE = True\n", encoding="utf-8")
    distribution = FakeDistribution(
        root,
        name="Example",
        files=(
            "example-1.0.dist-info/licenses/LICENSE",
            "example/licenses/_spdx.py",
        ),
    )

    destination = tmp_path / "notices"
    MODULE.collect(
        destination,
        [distribution],
        required_license_files=["example"],
    )

    inventory = json.loads((destination / "packages.json").read_text())
    package = inventory["packages"][0]
    assert package["status"] == "license-files"
    assert package["license_files"] == [
        "Example-1.0/example-1.0.dist-info_licenses_LICENSE"
    ]
    assert not any(path.name == "_spdx.py" for path in destination.rglob("*"))


def test_materializes_complete_license_text_from_legacy_metadata(tmp_path: Path):
    text = "Copyright Example\n\n" + ("Redistribution terms. " * 20)
    distribution = FakeDistribution(
        tmp_path,
        name="Legacy",
        metadata=f"License: {text.replace(chr(10), chr(10) + '        ')}\n",
    )

    MODULE.collect(tmp_path / "notices", [distribution])

    inventory = json.loads((tmp_path / "notices/packages.json").read_text())
    package = inventory["packages"][0]
    assert package["status"] == "license-files"
    assert package["license_source"] == "core-metadata License field"
    copied = tmp_path / "notices" / package["license_files"][0]
    assert "Redistribution terms" in copied.read_text(encoding="utf-8")


def test_required_distribution_must_contribute_license_file(tmp_path: Path):
    distribution = FakeDistribution(tmp_path, name="Metadata_Only")

    with pytest.raises(RuntimeError, match="no redistributable license file"):
        MODULE.collect(
            tmp_path / "notices",
            [distribution],
            required_license_files=["metadata-only"],
        )


def test_adds_pinned_external_notice_omitted_by_wheel(tmp_path: Path):
    distribution = FakeDistribution(tmp_path, name="Missing_Notice")
    notice = tmp_path / "UPSTREAM-LICENSE.txt"
    notice.write_text("Pinned upstream terms\n", encoding="utf-8")

    MODULE.collect(
        tmp_path / "notices",
        [distribution],
        required_license_files=["missing-notice"],
        extra_license_files=[("missing-notice", notice)],
    )

    inventory = json.loads((tmp_path / "notices/packages.json").read_text())
    package = inventory["packages"][0]
    assert package["status"] == "license-files"
    assert package["external_license_sources"] == ["UPSTREAM-LICENSE.txt"]
    copied = tmp_path / "notices" / package["license_files"][0]
    assert copied.read_text(encoding="utf-8") == "Pinned upstream terms\n"


def test_required_distribution_must_be_installed(tmp_path: Path):
    with pytest.raises(RuntimeError, match="is not installed"):
        MODULE.collect(
            tmp_path / "notices",
            [],
            required_license_files=["missing"],
        )
