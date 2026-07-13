#!/usr/bin/env python3
"""Exercise a first install or update against an extracted launcher tree."""

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
    "custom_node_runtime/environment-update-smoke.sentinel": (
        b"persistent node runtime\n"
    ),
}


def write_sentinels(
    root: Path, *, include_node_runtime: bool = True
) -> dict[Path, bytes]:
    result: dict[Path, bytes] = {}
    for relative, content in SENTINELS.items():
        if not include_node_runtime and relative.startswith("custom_node_runtime/"):
            continue
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        result[path] = content
    return result


def assert_sentinels(expected: dict[Path, bytes]) -> None:
    for path, content in expected.items():
        if not path.is_file() or path.read_bytes() != content:
            raise RuntimeError(f"full-Core update changed persistent sentinel: {path}")


def active_runtime(paths: PortablePaths, runtime: dict[str, str]) -> None:
    script = (
        "import json,platform,torch,torchvision,torchaudio; "
        "import portable_update_smoke_node_dep as node_dep; "
        "print(json.dumps({'python':platform.python_version(),"
        "'torch':torch.__version__,'torchvision':torchvision.__version__,"
        "'torchaudio':torchaudio.__version__,'cuda':torch.version.cuda,"
        "'node_runtime':node_dep.SENTINEL}))"
    )
    completed = subprocess.run(
        [str(paths.custom_node_python), "-s", "-c", script],
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
    if actual["node_runtime"] != "persistent node dependency":
        raise RuntimeError("active runtime did not import the persistent node venv")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("portable_root", type=Path)
    parser.add_argument(
        "core_archive",
        type=Path,
        help="full ComfyUI Core/frontend/Python/Torch/CUDA bundle",
    )
    parser.add_argument("--timeout", type=float, default=120.0)
    args = parser.parse_args()

    paths = PortablePaths(args.portable_root)
    paths.create_layout()
    bootstrap = not paths.comfyui.exists()
    if not bootstrap:
        paths.validate_runtime()
        paths.repair_runtime_metadata()
        paths.ensure_node_runtime()
    manager_config = paths.manager_config.read_bytes()
    manager_text = manager_config.decode("utf-8")
    if (
        "use_uv = false" not in manager_text
        or "use_unified_resolver = false" not in manager_text
    ):
        raise RuntimeError(
            "ComfyUI Manager is not configured for the persistent node venv"
        )

    sentinels = write_sentinels(paths.root, include_node_runtime=not bootstrap)
    node_dependency: Path | None = None
    old_generation_marker: Path | None = None
    if not bootstrap:
        node_dependency = (
            paths.custom_node_site_packages / "portable_update_smoke_node_dep.py"
        )
        node_dependency.write_text(
            "SENTINEL = 'persistent node dependency'\n", encoding="utf-8"
        )
        sentinels[node_dependency] = node_dependency.read_bytes()
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
        result = EnvironmentUpdater(paths, supervisor).install_bundle(args.core_archive)
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
        raise RuntimeError("full-Core update changed persistent Manager configuration")
    if old_generation_marker is not None and old_generation_marker.exists():
        raise RuntimeError("old ComfyUI generation was not replaced")
    rollbacks = sorted((paths.state / "rollback").glob("ComfyUI-*"))
    if bootstrap:
        if rollbacks:
            raise RuntimeError(
                "first install unexpectedly created a rollback generation"
            )
    elif (
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
    if bootstrap:
        paths.ensure_node_runtime()
        node_dependency = (
            paths.custom_node_site_packages / "portable_update_smoke_node_dep.py"
        )
        node_dependency.write_text(
            "SENTINEL = 'persistent node dependency'\n", encoding="utf-8"
        )
    active_runtime(paths, manifest["runtime"])
    print(
        json.dumps(
            {
                "generation_id": result.generation_id,
                "core_version": result.version,
                "first_install": bootstrap,
                "persistent_sentinels": len(sentinels),
                "rollback": None if bootstrap else rollbacks[-1].name,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
