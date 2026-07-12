from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/smoke_environment_update.py"


def load_smoke_module():
    spec = importlib.util.spec_from_file_location("smoke_environment_update", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_environment_update_sentinels_cover_every_persistent_area(
    tmp_path: Path,
) -> None:
    smoke = load_smoke_module()
    expected = smoke.write_sentinels(tmp_path)
    smoke.assert_sentinels(expected)
    assert {path.relative_to(tmp_path).parts[0] for path in expected} == {
        "models",
        "custom_nodes",
        "workflows",
        "user",
        "output",
        "custom_node_runtime",
    }

    changed = tmp_path / "models/environment-update-smoke/model.sentinel"
    changed.write_text("changed\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="persistent sentinel"):
        smoke.assert_sentinels(expected)
