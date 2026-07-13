# Portable Comfy

Portable Comfy is a one-click Linux desktop shell for a self-contained
[ComfyUI](https://github.com/Comfy-Org/ComfyUI) installation. A frozen
pywebview/Qt launcher owns the ComfyUI server process, while models, workflows,
custom nodes and user data remain in a movable directory beside the launcher.
No system Python or CUDA toolkit is used.

This repository is an early Linux x86-64 prototype.

## Portable layout

Extract the standalone launcher archive and keep the directory together. The
download intentionally has no `ComfyUI/` directory; installing a complete Core
bundle creates it beside the AppImage:

```text
Portable-Comfy/
├── Portable-Comfy.AppImage       # double-click entry point
├── ComfyUI/                       # added/replaced by a complete Core bundle
│   ├── frontend/                  # matching compiled web frontend
│   └── runtime/
│       └── python/                # CPython, locked Core deps and Torch/CUDA
├── custom_nodes/                  # retained across full-Core updates
├── custom_node_runtime/           # persistent shared custom-node venv
│   ├── bin/
│   ├── lib/python3.13/site-packages/
│   └── pyvenv.cfg
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
The compiled frontend travels with its exact 1.45.20 source snapshot and a
lock-derived production dependency notice inventory, plus checksum-bound
notices for dependencies copied directly into the compiled output.
The builder pre-creates ComfyUI's standard model-category directories beneath
`models/`. The only bundled model files are eight small, pinned TAESD preview
encoder/decoder weights under `models/vae_approx/`; user checkpoints and other
generation models are not shipped.

## Desktop lifecycle

On a new launcher-only installation, opening the AppImage presents the local
environment installer instead of trying to start a missing server. After a Core
bundle has been installed, opening the AppImage starts ComfyUI automatically,
waits for its HTTP health endpoint, and then loads the web interface. Closing
the final application window terminates the complete server process group so
node-created child processes do not remain behind.

The native application menu provides:

- **Server → Start, Stop, Restart** for explicit lifecycle control.
- **View → Reload** to reload the web interface without replacing the environment.
- **Environment → Install local environment…** for a local, validated full-Core
  install or update, including multipart bundles.
- **Help → About** for the installed generation, exact Core/frontend
  commits, and Python/Torch/CUDA versions.

Server output is written beneath `logs/`. The launcher binds to loopback by
default; do not expose a ComfyUI instance containing untrusted custom nodes to
an untrusted network.

## Full Core bundles and persistent data

A logical `Portable-Comfy-core-v<version>.tar.gz` archive replaces the complete
`ComfyUI/` generation. It is distributed as one small JSON descriptor and
ordered `.part0001`, `.part0002`, … files, each no larger than 1.9 GB. Keep all
of those files together and select the descriptor or any part in the installer;
the launcher verifies each part and the reconstructed archive before staging it.
Here **Core bundle means the whole replaceable folder**:
pinned ComfyUI Core source, its matching compiled frontend, source-built
Python, all locked Core requirements, Torch and CUDA user-space libraries. It
also contains the exact frontend source snapshot and dependency notices; it is
not a source-only patch. The installer stops the server, validates every
file and safe relative link, stages the new tree, checks it with the candidate's
own interpreter, preserves the previous generation for rollback, starts the
candidate, and commits only after a successful health check. A failed start
restores the previous generation automatically. The launcher keeps the two
newest successful rollback generations under `state/rollback/`.
Before either rename it fsyncs an update journal. If power loss or termination
interrupts activation, the next single-instance startup restores the rollback
environment/manifest—or the launcher-only state during first installation—and
preserves an uncommitted candidate under `state/recovered/` for diagnosis.

The following are never part of a full-Core update and remain untouched:

- `custom_nodes/` and their repositories;
- `models/`, `workflows/`, input, output and user data;
- `custom_node_runtime/`, the persistent shared custom-node venv;
- launcher configuration and logs.

Python, Torch, CUDA and the locked Core dependency set therefore update only
as part of one tested environment generation. See
[Architecture](docs/architecture.md) for the update boundary and manifest
rules.

The same exact version identity is visible at
`ComfyUI/PORTABLE-COMFY-IDENTITY.json`, bound into the checksum manifest, and
shown in **Help → About**.

If `custom_node_runtime/` already contains packages, the installer compares
the candidate's Python, Torch-family, CUDA and platform ABI fields with the
active manifest. It accepts compatible full-Core updates, but refuses an ABI
change until that persistent venv is moved aside or rebuilt for the new
generation.

This version installs only a bundle the user selects locally. Actions artifacts
are downloaded manually for testing. The sub-2-GB part format can later be
served as ordinary GitHub Release assets without changing the transactional
installer; no network update feed or Release is enabled yet.

## Custom nodes

ComfyUI discovers custom-node repositories from the top-level
`custom_nodes/` directory because the portable root is its base directory.
Frontend extensions declared by a node's `WEB_DIRECTORY` continue to load in
the embedded web interface.

Custom nodes run in the same Python process and share the top-level
`custom_node_runtime/` virtual environment; they do **not** each receive an
isolated environment. That venv is created offline on first launch with
`--system-site-packages --without-pip`, so it inherits pip, Torch and locked
Core dependencies from the active `ComfyUI/runtime/python` while keeping
node-installed packages and their normal pip metadata outside replaceable
`ComfyUI/`. ComfyUI Manager 4.2.2 is included and its ordinary
`python -m pip` install, upgrade and uninstall operations target this venv. A
node may still conflict with another node's packages or with a new environment
baseline, so back up the portable directory before installing unknown nodes.
Full-Core updates never delete the venv.

When the portable root moves, the launcher detects the new Python prefix and
rebinds the venv to the active `ComfyUI/runtime/python`, repairs pip-generated
console-script shebangs and remaining text metadata, and validates pip before
starting ComfyUI. Candidate update preflight uses only the candidate base
interpreter and cannot import packages from the persistent venv. The
interpreter and libpython themselves use relative runtime-library paths.

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
- A Wayland desktop, with or without XWayland, or an X11 session on older
  supported systems. When `DISPLAY` exists the launcher uses the proven XCB
  path to avoid the Qt/Wayland EGL failure reproduced on NVIDIA. A hardened
  Wayland session with no `DISPLAY` automatically uses native Qt Wayland. The
  renderer is bundled, so system WebKitGTK is not required.
- FUSE 2 for normal double-click AppImage mounting (`libfuse2` on Ubuntu 22.04
  or `libfuse2t64` on Ubuntu 24.04/26.04). If FUSE is unavailable, the same
  file can run with `--appimage-extract-and-run` at the cost of temporary
  extraction on every launch.
- Enough free storage for the extracted runtime, user models and outputs.
  Large generation models are not shipped; only the small TAESD preview
  weights described above are bundled.

The optional local smoke script can exercise XCB or native Wayland on a real
desktop, but GitHub Actions does not emulate a display or launch the AppImage.
GPU inference and rendered-window behavior must be tested after downloading the
artifact on a supported host; CUDA inference additionally requires an R580+
NVIDIA driver.

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
[Building and testing](docs/building.md). The repository has one deliberately
manual workflow: **Actions → Build portable artifacts → Run workflow**. It
builds and non-interactively preflights the launcher archive and multipart Core
payload, without GUI smoke jobs on hosted runners.

## License

Portable Comfy is licensed under GPL-3.0. Bundled components retain their
own licenses. The portable CPython runtime is built from upstream CPython
source rather than copied from a third-party standalone distribution. The
standalone launcher archive keeps the project license at its top level and
provides an indexed `LICENSES/` directory. Core and frontend notices travel inside
the replaceable `ComfyUI/` environment. The frontend keeps its pinned source
archive and all lock-resolved npm notices under `frontend/LICENSES/npm/`;
CPython's PSF license sits beside the interpreter; installed Python/Torch/CUDA wheel notices sit under
`ComfyUI/runtime/LICENSES/`; and AppImage launcher notices cover PyWebView,
PyInstaller, PyQt/Qt WebEngine, Chromium, the embedded AppImage runtime and
native Ubuntu libraries copied into the launcher. The standalone notice tree
also contains the checksum-pinned Qt 6.11.1 attribution mirror and exact
QtWebEngine module license set. The native notice directory
includes a complete PyInstaller-source provenance ledger and an exact Debian
package/version/copyright ledger; an unclassified absolute build input is a
hard build failure.
