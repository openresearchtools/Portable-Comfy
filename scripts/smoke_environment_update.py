#!/usr/bin/env python3
"""Exercise a real environment update against an extracted first-install tree."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from portable_comfy.paths import PortablePaths  # noqa: E402
from portable_comfy.supervisor import ServerSupervisor  # noqa: E402
from portable_comfy.updater import EnvironmentUpdater  # noqa: E402


SENTINELS = {
    "models/environment-update-smoke/model.sentinel": b"persistent model\n",
    "custom_nodes/environment_update_smoke/sentinel.txt": b"persistent node\n",
    "workflows/environment-update-smoke.json": b'{"persistent": "workflow"}\n',
    "user/environment-update-smoke.txt": b"persistent user data\n",
    "output/environment-update-smoke.txt": b"persistent output\n",
    "custom_node_runtime/site-packages/portable_update_smoke_node_dep.py": (
        b"SENTINEL = 'persistent node dependency'\n"
    ),
}


def write_sentinels(root: Path) -> dict[Path, bytes]:
    result: dict[Path, bytes] = {}
    for relative, content in SENTINELS.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        result[path] = content
    return result


def assert_sentinels(expected: dict[Path, bytes]) -> None:
    for path, content in expected.items():
        if not path.is_file() or path.read_bytes() != content:
            raise RuntimeError(
                f"environment update changed persistent sentinel: {path}"
            )


def active_runtime(paths: PortablePaths, runtime: dict[str, str]) -> None:
    script = (
        "import json,platform,torch,torchvision,torchaudio; "
        "import portable_update_smoke_node_dep as node_dep; "
        "print(json.dumps({'python':platform.python_version(),"
        "'torch':torch.__version__,'torchvision':torchvision.__version__,"
        "'torchaudio':torchaudio.__version__,'cuda':torch.version.cuda,"
        "'node_overlay':node_dep.SENTINEL}))"
    )
    completed = subprocess.run(
        [str(paths.python_executable()), "-s", "-c", script],
        cwd=paths.comfyui,
        env=paths.server_environment(),
        text=True,
        capture_output=True,
        timeout=300,
        check=False,
    )
    if completed.returncode:
        raise RuntimeError(
            "active environment runtime check failed: "
            + (completed.stderr or completed.stdout).strip()
        )
    actual = json.loads(completed.stdout.strip().splitlines()[-1])
    wanted = {
        key: runtime[key]
        for key in ("python", "torch", "torchvision", "torchaudio", "cuda")
    }
    if {key: actual[key] for key in wanted} != wanted:
        raise RuntimeError(f"active runtime mismatch: {actual!r} != {wanted!r}")
    if actual["node_overlay"] != "persistent node dependency":
        raise RuntimeError("active runtime did not import the persistent node overlay")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("portable_root", type=Path)
    parser.add_argument("environment_archive", type=Path)
    parser.add_argument("--timeout", type=float, default=120.0)
    args = parser.parse_args()

    paths = PortablePaths(args.portable_root)
    paths.create_layout()
    paths.validate_runtime()
    manager_config = paths.manager_config.read_bytes()
    manager_text = manager_config.decode("utf-8")
    if (
        "use_uv = false" not in manager_text
        or "use_unified_resolver = false" not in manager_text
    ):
        raise RuntimeError(
            "ComfyUI Manager is not configured for the persistent pip overlay"
        )

    sentinels = write_sentinels(paths.root)
    old_generation_marker = (
        paths.comfyui / "environment-update-smoke-old-generation.txt"
    )
    old_generation_marker.write_text("must move to rollback\n", encoding="utf-8")

    supervisor = ServerSupervisor(
        paths,
        start_timeout=args.timeout,
        cpu=True,
        disable_custom_nodes=True,
    )
    try:
        result = EnvironmentUpdater(paths, supervisor).install_bundle(
            args.environment_archive
        )
    finally:
        supervisor.stop(interrupt_timeout=5, terminate_timeout=3, kill_timeout=2)

    if result.restarted:
        raise RuntimeError(
            "update smoke started with a stopped server but reported restart"
        )
    if supervisor.is_running or (paths.state / "server.json").exists():
        raise RuntimeError("transactional update left its health-check server running")
    assert_sentinels(sentinels)
    if paths.manager_config.read_bytes() != manager_config:
        raise RuntimeError(
            "environment update changed persistent Manager configuration"
        )
    if old_generation_marker.exists():
        raise RuntimeError("old ComfyUI generation was not replaced")
    rollbacks = sorted((paths.state / "rollback").glob("ComfyUI-*"))
    if (
        not rollbacks
        or not (rollbacks[-1] / "environment-update-smoke-old-generation.txt").is_file()
    ):
        raise RuntimeError("previous complete ComfyUI generation was not retained")

    manifest = json.loads(paths.environment_manifest.read_text(encoding="utf-8"))
    if manifest.get("generation_id") != result.generation_id:
        raise RuntimeError(
            "activated manifest generation does not match updater result"
        )
    if not (paths.frontend / "index.html").is_file():
        raise RuntimeError("activated environment frontend is missing")
    if not paths.python_executable().is_file():
        raise RuntimeError("activated environment runtime is missing")
    active_runtime(paths, manifest["runtime"])
    print(
        json.dumps(
            {
                "generation_id": result.generation_id,
                "core_version": result.version,
                "persistent_sentinels": len(sentinels),
                "rollback": rollbacks[-1].name,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
