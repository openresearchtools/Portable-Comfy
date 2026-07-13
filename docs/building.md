# Building and testing

The repository has local launcher/manifest tests and one manual pinned
portable-runtime workflow. The CUDA workflow is manual because both final
artifacts and their intermediate trees are several gigabytes.

## Local tests

Use Python 3.13 and install the declared development/build extras:

```bash
python3.13 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install --editable '.[dev,build]'
python -m pytest
portable-comfy --root /tmp/Portable-Comfy --self-test
```

The self-test is non-interactive and must not require a running ComfyUI server
or display. The manifest tests build a tiny structural full-Core archive and
prove that persistent directories are excluded, every Core/runtime file is
bound, and tampering or unsafe links are rejected.

For a full build on Ubuntu, install the native CPython/AppImage prerequisites:

```bash
sudo apt-get update
sudo apt-get install --no-install-recommends \
  build-essential curl desktop-file-utils file git libbz2-dev libexpat1-dev \
  libffi-dev libgdbm-dev liblzma-dev libncursesw5-dev libreadline-dev \
  libsqlite3-dev libssl-dev patchelf pkg-config rsync squashfs-tools tk-dev \
  uuid-dev xz-utils zlib1g-dev
```

## Build scripts

The public shell entry points are:

```bash
scripts/build_portable.sh \
  --output-dir "$PWD/artifacts" \
  --work-dir /path/with/ample/free-space

scripts/build_environment_bundle.sh \
  --output-dir "$PWD/artifacts" \
  --work-dir /path/with/ample/free-space

scripts/preflight_portable.sh \
  artifacts/Portable-Comfy-linux-x86_64.tar.gz

scripts/preflight_environment.sh \
  artifacts/Portable-Comfy-core-v0.27.0.tar.gz

python3 scripts/split_archive.py \
  artifacts/Portable-Comfy-core-v0.27.0.tar.gz \
  --part-size 1900000000

# Non-interactive equivalent of the native Environment menu action:
./Portable-Comfy/Portable-Comfy.AppImage \
  --install-environment \
  artifacts/Portable-Comfy-core-v0.27.0.tar.gz.parts.json
```

The hosted workflow intentionally stops at non-GUI package verification. After
downloading both artifacts, extract the launcher, choose **Environment → Install
local environment…**, select the Core descriptor, and launch that exact AppImage
in a real desktop session. Native Wayland validation must remove `DISPLAY` and inspect the
WebEngine surface itself because Wayland does not permit unrelated processes to
enumerate global windows.

`build_environment_bundle.sh` can reuse the completed environment source
retained while building the standalone launcher instead of rebuilding CPython
and CUDA packages:

```bash
scripts/build_environment_bundle.sh \
  --source-root /build/environment-source/Portable-Comfy \
  --output-dir "$PWD/artifacts" \
  --work-dir /path/on/the/same/filesystem
```

On one filesystem it stages hard links to the immutable completed `ComfyUI/`
tree before archiving; it falls back to a copy across filesystems. It never
copies models or other persistent directories. The Actions workflow uses this
path so the environment is built once even though the AppImage is frozen before
that environment is removed from the standalone delivery tree.

`build_portable.sh` accepts `--skip-appimage` and `--skip-runtime`, and the
environment builder accepts `--structural`, for targeted packaging tests.
`--skip-runtime` requires `--skip-appimage`, because the real AppImage is frozen
with the portable CPython build. Those modes are not release-equivalent. A
normal build followed by `split_archive.py` produces:

```text
Portable-Comfy-linux-x86_64.tar.gz
Portable-Comfy-core-v0.27.0.tar.gz.parts.json
Portable-Comfy-core-v0.27.0.tar.gz.part0001
Portable-Comfy-core-v0.27.0.tar.gz.part0002
```

The first archive is a standalone bootstrap: it contains the AppImage,
persistent directory layout and eight pinned TAESD preview encoder/decoder
weights under `models/vae_approx/`, but deliberately contains no `ComfyUI/`,
Python/Torch runtime or environment manifest. It does not include user
checkpoints or other generation models. On first launch the application accepts
a downloaded complete Core bundle rather than attempting to start a server.

The logical full-Core archive contains one outer versioned directory, the
complete `ComfyUI/` generation (Core, matching frontend, source-built Python, locked
requirements and Torch/CUDA), the exact frontend source snapshot and npm
dependency notices, and `manifest/environment.json` plus its checksum
list. "Core" here names the whole replaceable environment, not a source-only
payload. It excludes `models/`, `custom_nodes/`, `custom_node_runtime/`,
workflows, user/input/output/temp data, config and logs.

`split_archive.py` divides that logical archive into independently verified
1.9-GB-or-smaller files and writes a descriptor binding the logical archive and
every ordered part by exact name, byte size and SHA-256. It deletes the large
logical archive after verifying the split unless `--keep-archive` is requested.
Keep the descriptor and all part files in one directory; the application can
install the set when the descriptor or any one part is selected.

The named baseline and every currently resolved Core Python distribution are
exact pre-build pins in `packaging/runtime-constraints.txt`. Each environment
ships that exact file as `ComfyUI/runtime/requirements.lock`, records the
installed set separately, and binds the lock path and digest in its schema-v2
manifest. This prevents ordinary transitive-version drift. The artifacts are
not claimed to be byte-for-byte reproducible because all wheel bytes and build
tools are not yet hash-locked.

`packaging/versions.env` is the authoritative mapping for a generation. For a
future upstream ComfyUI release, update `COMFY_VERSION` and `COMFY_TAG` to the
release, pin `COMFY_COMMIT` to that tag's exact commit, and update its archive
digest. Pin the frontend version/commit that belongs with that Core release,
then select and lock the complete Python/Torch/CUDA set as one compatibility
unit. The complete Core payload exposes that mapping in its top
`manifest/environment.json` and byte-identical visible
`ComfyUI/PORTABLE-COMFY-IDENTITY.json`; a version label alone is never treated
as a source pin.

During source preparation, the builder parses the pinned snapshot's
`comfyui_version.py` and aborts unless its literal `__version__` equals
`COMFY_VERSION`. It also requires that snapshot's `requirements.txt` to contain
exactly `comfyui-frontend-package==${FRONTEND_VERSION}` before accepting the
separately pinned compiled frontend wheel.

Frontend preparation uses Node 24 and pnpm 11.1.1 only to resolve the filtered
production graph from the exact pinned source lock; lifecycle scripts are
disabled. The generated relocatable inventory contains 419 records for the
current frontend: 416 external/production-workspace packages plus UUID, Inter
and Material Design Icons that are present in the compiled wheel but absent
from that production dependency graph. Those three records bind their notices
to exact compiled asset hashes. Every record must reference at least one packed
or separately checksum-pinned upstream notice. The temporary pnpm store is not
shipped. The exact frontend source archive is shipped beside the compiled
assets.

## GitHub Actions artifacts

Run `.github/workflows/build-artifacts.yml` manually from the repository's
Actions tab. It performs these gates:

1. Builds on Ubuntu 22.04 so generated native binaries retain the supported
   glibc 2.35 baseline, reclaiming unused hosted-runner SDKs first.
2. Builds the standalone launcher while retaining its completed environment
   source tree, then creates the full-Core archive from those exact environment
   bytes without reinstalling dependencies.
3. Preflights the launcher and complete environment without launching a
   desktop, then splits the Core archive into 1.9-GB-or-smaller transport files
   and records SHA-256 values in the job log.
4. Uploads the launcher tarball and one multipart Core artifact containing the
   descriptor and parts, with one-day retention and no redundant compression.

The AppImage build also treats PyInstaller's final `COLLECT-00.toc` as the
authoritative frozen-source ledger. `LICENSES/launcher-native-packages/`
contains `provenance.tsv` for every TOC input (plus the four manually added
Wayland libraries) and `packages.tsv` for every Debian-owned host input. An
absolute source outside the launcher venv, portable CPython, repository and
build-output trees must be owned by an installed Debian package or the build
fails; each such package contributes its version and Debian copyright file.
PyWebView's package hook contributes only its required JavaScript. Unused GTK,
Android, Cocoa and WinForms backends and cross-platform binary payloads are
excluded from the Qt-only launcher.

`LICENSES/Qt-6.11.1-attributions/` mirrors the two pinned official Qt license
indexes, all 281 linked Qt/Chromium attribution pages, and the exact 12-file
QtWebEngine module license set. Its checksum manifests and source commits are
validated both during the build and by standalone preflight.

The workflow has one build job. It does not install Xvfb or Weston, emulate a
desktop, launch Qt WebEngine, or run GUI smoke tests on a hosted runner.

GitHub's artifact service wraps uploaded files in its own downloadable
container; `compression-level: 0` avoids trying to recompress the inner
launcher `.tar.gz` or Core parts. No workflow creates a GitHub Release or pushes
generated binaries to the repository.

Before enabling public GitHub Releases, add the matching corresponding-source
delivery for the GPL/LGPL launcher dependencies to the release checklist. The
current workflow deliberately produces short-lived test artifacts only and
does not claim that those artifacts are a public update feed.

After a successful run, download the same short-lived files locally with the
GitHub CLI (replace `RUN_ID` with the workflow run ID):

```bash
gh run download RUN_ID --name Portable-Comfy-linux-x86_64.tar.gz \
  --dir downloaded/launcher
gh run download RUN_ID --name Portable-Comfy-core-v0.27.0-multipart \
  --dir downloaded/core
```

Actions artifacts consume account artifact storage even for a public
repository. One-day retention minimizes persistent usage but does not waive
storage or per-artifact service limits. A failed upload is a distribution
failure and must remain visible; the workflow must not silently omit CUDA or
substitute a smaller CPU package.

## What preflight and optional local smoke tests prove

The full-Core preflight verifies archive safety, the complete schema-v2 file
and symlink manifest, equality with the visible in-folder identity,
requirements-lock identity, candidate-runtime relocation, pinned imports and a
freshly compiled native extension. It runs the interpreter inside the candidate
`ComfyUI/`, never the active/full archive's runtime.

The optional local transactional-update smoke uses `EnvironmentUpdater` itself to stage
that downloaded archive, run the candidate's version/import, `pip check` and
ComfyUI quick tests, atomically swap the complete generation, start and
health-check it, and retain the old generation for rollback. Sentinels under
`models/`, `custom_nodes/`, `workflows/`, `user/`, `output/` and
`custom_node_runtime/` must remain byte-identical, and the activated runtime
must import a module installed in the persistent node venv. This local check is
not part of the artifact-building workflow.

After installing the downloaded multipart Core into an extracted standalone
launcher, the optional local smoke test can run without an NVIDIA GPU and uses
`--allow-no-gpu` solely to check:

- standalone-launcher and installed-Core integrity and relocatable paths;
- server startup and HTTP readiness;
- the actual AppImage creating a mapped XCB Qt WebEngine window beneath Xvfb;
- the same AppImage selecting native Wayland on Ubuntu 26.04 with no
  `DISPLAY`/Xwayland fallback;
- smoke-only loopback DevTools capture of the viewport to PNG plus usable-pixel
  validation after the compiled frontend reports ready;
- clean window closure and launcher-owned server shutdown with no surviving
  server state or process.

These checks do not prove CUDA inference, model correctness or third-party
custom-node compatibility. Before wider distribution, test the same artifacts
on every supported Ubuntu version and on a Turing-or-newer NVIDIA host with an
R580-or-newer driver.
