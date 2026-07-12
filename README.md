# Portable Comfy

Portable Comfy is a one-click Linux desktop shell for a self-contained
[ComfyUI](https://github.com/Comfy-Org/ComfyUI) installation. A frozen
pywebview/Qt launcher owns the ComfyUI server process, while models, workflows,
custom nodes and user data remain in a movable directory beside the launcher.
No system Python or CUDA toolkit is used.

This repository is an early Linux x86-64 prototype. Builds are currently
published only as short-lived GitHub Actions artifacts, not as Releases.

## Portable layout

Extract the complete archive and keep the directory together:

```text
Portable-Comfy/
├── Portable-Comfy.AppImage       # double-click entry point
├── ComfyUI/                       # one atomic environment generation
│   ├── frontend/                  # matching compiled web frontend
│   └── runtime/
│       └── python/                # CPython, locked Core deps and Torch/CUDA
├── custom_nodes/                  # retained across environment updates
├── custom_node_runtime/           # persistent node-package overlay
│   ├── bin/
│   └── site-packages/
├── models/
├── input/
├── output/
├── temp/
├── workflows/
├── user/
├── logs/
├── config/
├── manifest/
├── state/                         # first-run status/update rollback data
└── LICENSES/
```

The launcher computes every path from the portable root; it never relies on
the shell's current working directory. It starts the bundled interpreter with
`--base-directory` pointing at that root and `--front-end-root` pointing at the
frontend shipped with the active environment generation. The convenient
top-level `workflows/` directory stores the real files;
`user/default/workflows` is a relative symlink to `../../workflows`.

Each ComfyUI environment includes source-built CPython 3.13.12. The initial pinned
baseline is ComfyUI v0.27.0, frontend 1.45.20, Torch 2.12.0+cu130,
torchvision 0.27.0+cu130 and torchaudio 2.11.0+cu130.
The builder pre-creates ComfyUI's standard model-category directories beneath
`models/`. The only bundled model files are eight small, pinned TAESD preview
encoder/decoder weights under `models/vae_approx/`; user checkpoints and other
generation models are not shipped.

## Desktop lifecycle

Opening the AppImage starts ComfyUI automatically, waits for its HTTP health
endpoint, and then loads the web interface. Closing the final application
window terminates the complete server process group so node-created child
processes do not remain behind.

The native application menu provides:

- **Server → Start, Stop, Restart** for explicit lifecycle control.
- **View → Reload** to reload the web interface without replacing the environment.
- **Environment → Install bundle…** for a local, validated environment update.
- **Help → About** for build and runtime information.

Server output is written beneath `logs/`. The launcher binds to loopback by
default; do not expose a ComfyUI instance containing untrusted custom nodes to
an untrusted network.

## Environment updates and persistent data

An environment archive replaces the complete `ComfyUI/` generation: pinned
Core source, its matching frontend, source-built Python, all locked Core
requirements and Torch/CUDA. The installer stops the server, validates every
file and safe relative link, stages the new tree, checks it with the candidate's
own interpreter, preserves the previous generation for rollback, starts the
candidate, and commits only after a successful health check. A failed start
restores the previous environment automatically. The launcher keeps the two
newest successful rollback generations under `state/rollback/`.
Before either rename it fsyncs an update journal. If power loss or termination
interrupts activation, the next single-instance startup restores the rollback
environment/manifest and preserves an uncommitted candidate under
`state/recovered/` for diagnosis.

The following are never part of an environment update and remain untouched:

- `custom_nodes/` and their repositories;
- `models/`, `workflows/`, input, output and user data;
- `custom_node_runtime/site-packages/`, the persistent node-package overlay;
- launcher configuration and logs.

Python, Torch, CUDA and the locked Core dependency set therefore update only
as part of one tested environment generation. See
[Architecture](docs/architecture.md) for the update boundary and manifest
rules.

If `custom_node_runtime/` already contains packages, the installer compares
the candidate's Python, Torch-family, CUDA and platform ABI fields with the
active manifest. It accepts compatible environment updates, but refuses an ABI
change until that persistent overlay is moved aside or rebuilt for the new
generation.

This version installs only a bundle the user selects locally. The validated
bundle format can later be served from GitHub Releases without changing the
transactional installer; no network update feed or Release is enabled yet.

## Custom nodes

ComfyUI discovers custom-node repositories from the top-level
`custom_nodes/` directory because the portable root is its base directory.
Frontend extensions declared by a node's `WEB_DIRECTORY` continue to load in
the embedded web interface.

Custom nodes run in the same Python process and use the interpreter at
`ComfyUI/runtime/python`; they do **not** each receive a virtual environment.
ComfyUI Manager 4.2.2 is included. Node-only packages that must survive an
environment swap belong in the top-level
`custom_node_runtime/site-packages/` overlay, never in the replaceable
environment's locked site-packages. A node may still conflict with another
node's packages or with a new environment baseline, so back up the portable
directory before installing unknown nodes. Environment updates never delete
the overlay.

When the portable root moves, the launcher detects the new Python prefix and
repairs pip-generated console-script shebangs and remaining text metadata
before starting ComfyUI. The interpreter and libpython themselves use
relative runtime-library paths.

The bundled interpreter makes Python packages portable; it cannot make every
arbitrary node self-contained. Nodes that invoke host programs, compile native
extensions without a wheel, or require a different GPU stack may still need
their documented host prerequisites.

## Host requirements

- Linux x86-64 with glibc 2.35 or newer; Ubuntu 22.04, 24.04 and 26.04 are the
  supported targets.
- A Turing-generation (compute capability 7.5) or newer NVIDIA GPU and a host
  NVIDIA driver from the R580 series or newer for the CUDA 13 build. CUDA 13
  removed library/offline-compilation support for Maxwell, Pascal and Volta.
  CUDA user-space libraries ship with Torch; the kernel driver remains a host
  responsibility.
- A Wayland desktop with XWayland compatibility (the Ubuntu 26.04 default), or
  an X11 session on older supported systems. The launcher uses the XCB path by
  default to avoid the Qt/Wayland EGL failure reproduced on NVIDIA; users may
  explicitly override the Qt platform. The renderer is bundled, so system
  WebKitGTK is not required.
- FUSE 2 for normal double-click AppImage mounting (`libfuse2` on Ubuntu 22.04
  or `libfuse2t64` on Ubuntu 24.04/26.04). If FUSE is unavailable, the same
  file can run with `--appimage-extract-and-run` at the cost of temporary
  extraction on every launch.
- Enough free storage for the extracted runtime, user models and outputs.
  Large generation models are not shipped; only the small TAESD preview
  weights described above are bundled.

The Actions smoke test uses a real XCB window beneath Xvfb and validates the
Qt WebEngine viewport through a smoke-only loopback DevTools capture. It proves
packaging, a real transactional environment swap with persistent-data
sentinels, mapped-window rendering, server ownership and shutdown without
claiming GPU inference. GPU inference must additionally be tested on an R580+
NVIDIA host.

## Development

Use Python 3.13:

```bash
python3.13 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install --editable '.[dev,build]'
python -m pytest
portable-comfy --root /tmp/Portable-Comfy --self-test
```

Build scripts and artifact verification are documented in
[Building and testing](docs/building.md). The fast `CI` workflow runs for pull
requests and pushes. The multi-gigabyte CUDA build is deliberately manual via
**Actions → Build portable artifacts → Run workflow**.

## License

Portable Comfy is licensed under GPL-3.0-only. Bundled components retain their
own licenses. The portable CPython runtime is built from upstream CPython
source rather than copied from a third-party standalone distribution.
