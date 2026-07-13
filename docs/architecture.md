# Architecture

Portable Comfy separates the frozen desktop launcher, one atomically
replaceable ComfyUI environment generation, and persistent user/node state.
That boundary permits Python and Torch updates without redownloading models or
silently deleting custom nodes.

The launcher archive contains the AppImage and persistent directory skeleton,
but no `ComfyUI/` environment. A first-run launcher is therefore useful on its
own as an installer: the user selects a complete Core bundle, and only then can
the server start. Launcher and Core releases can be replaced independently.

## Runtime and process model

The AppImage contains the frozen `portable_comfy` launcher and its Qt WebEngine
desktop renderer. It locates the portable root beside itself, validates the
layout, creates missing data directories and the workflow link, then starts:

```text
ComfyUI/runtime/python/bin/python3 ComfyUI/main.py
    --base-directory <portable-root>
    --front-end-root <portable-root>/ComfyUI/frontend
    --listen 127.0.0.1
    --port <selected-port>
```

The actual argument list is assembled without invoking a shell. ComfyUI is
placed in a new process group, its output is captured under `logs/`, and the
webview is shown after the health probe succeeds. Stop and application-close
operations signal the process group, wait for a bounded grace period, and then
force termination when necessary.

The launcher owns the process it starts. It must not kill an unrelated process
that was already listening on the configured port.

## Atomic environment and persistent data

The top-level `ComfyUI/` directory is one self-contained generation:

```text
ComfyUI/
├── main.py and pinned Core source
├── PORTABLE-COMFY-IDENTITY.json  # visible exact version identity
├── LICENSE                       # pinned Core license
├── frontend/                     # matching compiled frontend
│   ├── SOURCE-ComfyUI-frontend-<version>.tar.gz
│   └── LICENSES/npm/             # production dependency notices + inventory
└── runtime/
    ├── python/                   # source-built CPython + locked packages
    │   └── LICENSE.txt           # upstream PSF license beside CPython
    ├── requirements.lock         # exact committed constraints
    ├── installed-requirements.txt
    └── LICENSES/python-packages/ # wheel notices + packages.json inventory
```

The public **Core bundle** swaps that complete directory. In this project,
"Core bundle" explicitly includes the matching frontend, private interpreter,
Torch/CUDA and locked dependencies; it is not source-only. Core source and its
interpreter can never be combined accidentally with a different generation's
Torch or dependency set.

The top-level `custom_nodes/`, `custom_node_runtime/`, `models/`, `input/`,
`output/`, `temp/`, `workflows/` and `user/` directories are persistent. Node
repositories stay in `custom_nodes/`; node-only Python packages that must
survive an environment replacement stay in the shared system-site-packages
venv at `custom_node_runtime/`. ComfyUI itself runs with that venv's Python.
It inherits the active environment's locked packages and Torch, while normal
pip metadata, packages and console scripts installed by nodes remain in the
persistent venv. An update may still expose a real dependency or ABI
incompatibility even though it never deletes this venv.

Before staging when the venv contains node packages, the updater compares the candidate and
active Python, Torch, torchvision, torchaudio, CUDA and platform values. A
compatible Core/frontend/requirements update proceeds. An ABI-changing update
is refused until the user moves aside or rebuilds the venv; the updater does
not guess that arbitrary compiled extensions are compatible.

The launcher creates the venv offline with
`--system-site-packages --without-pip`, starts ComfyUI with its interpreter,
and leaves `PYTHONHOME`, `PYTHONPATH` and `PIP_TARGET` unset. Base pip therefore
performs ordinary venv installs, upgrades and uninstalls without mutating the
replaceable base. After relocation or a same-ABI update, the launcher rewrites
`pyvenv.cfg`, relative interpreter links, editable paths and console-script
entry points to the active base. Candidate preflight intentionally invokes the
candidate base Python directly, with no persistent venv on its import path.

The portable root's `workflows/` directory contains the real files.
`user/default/workflows` is a relative `../../workflows` symlink pointing back
to it. The launcher repairs that link only when it is absent or already targets
the expected directory; it must not overwrite unrelated user content.

## Full Core bundle contract

The reconstructed update archive has one outer directory named
`Portable-Comfy-core-v<version>/`. It may contain only:

```text
Portable-Comfy-core-v<version>/
├── ComfyUI/                       # complete candidate generation
└── manifest/
    ├── environment.json
    └── environment-checksums.sha256
```

The schema-v2 manifest identifies the application and generation, pinned
Core/frontend revisions, Python, Torch, torchvision, torchaudio, CUDA and
platform values, plus the SHA-256-bound requirements lock. Its `files` list
covers every regular file and safe relative symlink beneath `ComfyUI/`.
Regular entries record size and SHA-256; symlink entries record the exact
relative target. The checksum list covers every regular payload file.
`ComfyUI/PORTABLE-COMFY-IDENTITY.json` repeats the generation ID and exact
Core, frontend and runtime identity in a visible place inside the replaceable
folder. The verifier requires those objects to equal the top manifest exactly,
so the complete archive and Core archive cannot silently describe the same
bytes as different generations.

The verifier rejects absolute/traversal paths, escaping or dangling links,
special files, unexpected top-level payload, missing/unlisted entries and any
checksum, identity or lock mismatch. Persistent names such as `models/`,
`custom_nodes/`, `workflows/` and `user/` are therefore impossible to smuggle
into a valid update bundle.

The verifier also requires the frontend source archive to match the pinned
frontend version and requires every package in the filtered production pnpm
closure to have a nonempty, manifested notice. Dependencies embedded directly
in compiled JavaScript or font assets are recorded separately with exact asset
hashes and notices. Runtime wheels have the same all-packages-noticed invariant
under `runtime/LICENSES/python-packages/`.

For transport, the logical `.tar.gz` is split into ordered files of at most
1.9 GB plus a JSON descriptor. The descriptor binds the logical archive name,
size and SHA-256 as well as each part's number, exact filename, size and SHA-256.
Selecting either the descriptor or any part discovers the complete sibling set;
all parts and the reconstructed archive are verified before archive parsing or
transactional staging begins. This keeps every future GitHub Release asset below
the 2 GB per-file limit without weakening the existing archive contract.

Installation is transactional:

1. Safely extract and validate the complete candidate before changing the
   active tree.
2. Repair the candidate interpreter for its staged location and run import and
   Core preflight checks with `ComfyUI/runtime/python/bin/python-portable` from
   that candidate.
3. Stop the owned server, durably write and fsync an activation journal, then
   atomically move the current `ComfyUI/` generation aside when one exists.
4. Atomically activate the candidate directory and its environment manifest.
5. Launch and health-check the candidate. Commit only after success; otherwise
   restore the previous complete generation and restart it.

After an interrupted activation, the next startup acquires the instance lock
and recovers from that journal before normal server startup. An update restores
the rollback environment and manifest files. An interrupted first installation
returns to the valid launcher-only state. In either case an uncommitted candidate
is moved to `state/recovered/` instead of being trusted or silently deleted.

The AppImage is outside this transaction. A launcher/UI update requires only a
new standalone launcher package; a full-Core update may change Core,
frontend, Python, Torch/CUDA and locked Core requirements together. The
manifest retains `bundle_type: environment` as the stable schema-v2 identity;
that internal field does not make the public Core artifact source-only.

The standalone launcher's top-level `LICENSES/` directory indexes its frozen
launcher notices. Core/runtime notices stay inside the separately installed
environment. Launcher package notices
cover PyWebView, PyInstaller, PyQt/Qt WebEngine and Chromium. Native Wayland,
PulseAudio, XCB and XKB libraries copied from the Ubuntu build host are not a
hand-maintained license subset: the build parses PyInstaller's final
`COLLECT-00.toc` and records every frozen source in `provenance.tsv`. Every
absolute source must be inside an identified build tree or owned by an
installed Debian package. `packages.tsv` binds each host package to its exact
version and copied Debian copyright file; the four dlopen-only Wayland inputs
are explicit manual provenance rows. An unknown absolute source fails the
build. The embedded AppImage type-2 runtime carries its pinned MIT notice.

## Python and CUDA portability

The interpreter is built from upstream CPython source into
`ComfyUI/runtime/python`. Python and libpython use relative runtime-library
paths; remaining text metadata and pip entry points are repaired after the
generation moves. GPU user-space libraries are provided by the pinned PyTorch
wheels. `libcuda` and the kernel driver remain on the host.

The initial generation uses Python 3.13.12, Torch 2.12.0+cu130, torchvision
0.27.0+cu130 and torchaudio 2.11.0+cu130. The manifest binds these values and
the complete requirements-lock digest to the exact ComfyUI tree.

## Custom-node trust and compatibility

Custom nodes are arbitrary Python executed inside the ComfyUI process. A node
can access the user's files, network and GPU with the same permissions as the
application. Portability does not sandbox it.

All conventional nodes share one interpreter, Torch installation and import
space through the persistent custom-node venv.
Dependency conflicts therefore remain possible. A future profiles feature may
materialize separate full environments, but pretending that arbitrary existing
nodes have isolated processes would be incompatible with how they exchange
models and tensors today.
