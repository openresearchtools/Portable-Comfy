# Architecture

Portable Comfy separates immutable application code, a replaceable ComfyUI
Core, and persistent user state. That boundary is what allows a Linux package
to be both one-click and maintainable without redownloading models or custom
nodes.

## Runtime and process model

The AppImage contains the frozen `portable_comfy` launcher and its Qt WebEngine
desktop renderer. It locates the portable root beside itself, validates the
layout, creates missing data directories and the workflow link, then starts:

```text
runtime/python/bin/python3 ComfyUI/main.py
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

## Data boundary

`ComfyUI/` is versioned application content. The top-level `custom_nodes/`,
`models/`, `input/`, `output/`, `temp/`, `workflows/` and `user/` directories
are persistent content. `runtime/python/` is also persistent because custom
node dependencies are installed into its shared `site-packages`.

The portable root's `workflows/` directory contains the real files.
`user/default/workflows` is a relative `../../workflows` symlink pointing
back to it. The launcher repairs that link only when it is absent or already
targets the expected directory; it must not overwrite unrelated user content.

## Core bundle contract

A Core bundle contains one complete, pinned `ComfyUI/` tree with its matching
compiled frontend plus a machine-readable update manifest and checksums. The
manifest identifies at least:

- the bundle schema and ComfyUI/frontend versions;
- the Core source revision;
- compatible Python, Torch/CUDA/platform and complete runtime-constraints
  digest;
- a file list constrained to the top-level `ComfyUI/` payload directory;
- SHA-256 checksums for every replaceable `ComfyUI/` payload file.

Installation is transactional:

1. Reject path traversal, absolute paths, unexpected links, checksum failures,
   schema mismatches and incompatible runtime requirements before activation.
2. Extract to a staging directory on the same filesystem as `ComfyUI/`.
3. Run the bundle's import/preflight check with `runtime/python/bin/python3`.
4. Stop the owned server, durably write and fsync an activation journal, then
   atomically move the current Core aside.
5. Atomically activate the staged directory, update manifests and launch it.
6. Commit and remove the journal only after the HTTP health check succeeds;
   otherwise restore the previous directory and restart it.

After an interrupted activation, the next startup acquires the instance lock
and recovers from that journal before normal server startup. It restores the
rollback Core and manifest files, while moving an uncommitted candidate to
`state/recovered/` instead of deleting it.

Only `ComfyUI/` participates in this transaction. Update code must never sync,
delete or replace the portable data directories or node-only dependencies.

## Runtime updates

Python 3.13.12, the Qt renderer, Torch 2.12.0+cu130, torchvision
0.27.0+cu130 and torchaudio 2.11.0+cu130 form the initial runtime ABI. A Core
bundle must declare compatibility with that baseline and the SHA-256 digest of
the complete pinned Python constraints file. Changing any part of it requires
a complete portable artifact and explicit migration; the Core menu is not
allowed to mutate these foundational packages.

The interpreter is built from upstream CPython source into a prefix located
inside the portable root. The launcher always invokes that interpreter by
absolute path and clears ambient virtual-environment/user-site settings. GPU
user-space libraries are provided by PyTorch wheels; `libcuda` and the kernel
driver remain on the host.

## Custom-node trust and compatibility

Custom nodes are arbitrary Python executed inside the ComfyUI process. A node
can access the user's files, network and GPU with the same permissions as the
application. Portability does not sandbox it.

All conventional nodes share one interpreter, Torch installation and import
space. Dependency conflicts therefore remain possible. A future profiles
feature may materialize separate full runtimes, but pretending that arbitrary
existing nodes have isolated environments would be incompatible with how they
exchange models and tensors today.
