from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def test_node_overlay_hook_processes_visible_nested_pth(tmp_path: Path) -> None:
    base_site = tmp_path / "base-site"
    overlay = tmp_path / "overlay"
    modules = overlay / "modules"
    base_site.mkdir()
    modules.mkdir(parents=True)

    (base_site / "portable_comfy_node_overlay.pth").write_text(
        "import os,site; p=os.environ.get('PORTABLE_COMFY_NODE_SITE_PACKAGES'); "
        "p and site.addsitedir(p)\n",
        encoding="utf-8",
    )
    (overlay / "portable-comfy-preflight.test.pth").write_text(
        "modules\n", encoding="utf-8"
    )
    (modules / "portable_overlay_test.py").write_text(
        "SENTINEL = 42\n", encoding="utf-8"
    )

    environment = os.environ.copy()
    environment["PORTABLE_COMFY_NODE_SITE_PACKAGES"] = str(overlay)
    subprocess.run(
        [
            sys.executable,
            "-S",
            "-c",
            (
                "import site; "
                f"site.addsitedir({str(base_site)!r}); "
                "import portable_overlay_test as value; "
                "assert value.SENTINEL == 42"
            ),
        ],
        check=True,
        env=environment,
    )


def test_portable_preflight_does_not_create_a_hidden_pth() -> None:
    script = (
        Path(__file__).resolve().parents[1] / "scripts/preflight_portable.sh"
    ).read_text(encoding="utf-8")

    assert '"$overlay/portable-comfy-preflight.XXXXXX"' in script
    assert '"$overlay/.portable-comfy-preflight.XXXXXX"' not in script
