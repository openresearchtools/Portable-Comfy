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
or display. The manifest tests build a tiny structural environment archive and
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
  artifacts/Portable-Comfy-environment-v0.27.0.tar.gz

scripts/smoke_artifact.sh \
  artifacts/Portable-Comfy-linux-x86_64.tar.gz \
  --timeout 120 \
  --allow-no-gpu
```

On an active Wayland session, exercise the no-Xwayland fallback explicitly:

```bash
scripts/smoke_artifact.sh \
  artifacts/Portable-Comfy-linux-x86_64.tar.gz \
  --timeout 120 \
  --allow-no-gpu \
  --native-wayland
```

That mode removes `DISPLAY`, verifies that AppRun selects native Wayland, and
uses the WebEngine surface itself for visual evidence because Wayland does not
permit an unrelated process to enumerate global windows.

`build_environment_bundle.sh` can reuse an already completed first-install
staging tree instead of rebuilding CPython and CUDA packages:

```bash
scripts/build_environment_bundle.sh \
  --source-root /build/Portable-Comfy \
  --output-dir "$PWD/artifacts" \
  --work-dir /path/on/the/same/filesystem
```

On one filesystem it stages hard links to the immutable completed `ComfyUI/`
tree before archiving; it falls back to a copy across filesystems. It never
copies models or other persistent directories. The Actions workflow uses this
path so the two artifacts contain the exact same environment bytes.

`build_portable.sh` accepts `--skip-appimage` and `--skip-runtime`, and the
environment builder accepts `--structural`, for targeted packaging tests.
Those modes are not release-equivalent. A normal build produces:

```text
Portable-Comfy-linux-x86_64.tar.gz
Portable-Comfy-environment-v0.27.0.tar.gz
```

The complete first-install archive contains the AppImage, atomic `ComfyUI/`
environment, persistent directory layout, and eight pinned TAESD preview
encoder/decoder weights under `models/vae_approx/`. It does not include user
checkpoints or other generation models.

The environment archive contains one outer versioned directory, the complete
`ComfyUI/` generation (Core, matching frontend, source-built Python, locked
requirements and Torch/CUDA), and `manifest/environment.json` plus its checksum
list. It excludes `models/`, `custom_nodes/`, `custom_node_runtime/`, workflows,
user/input/output/temp data, config and logs.

The named baseline and every currently resolved Core Python distribution are
exact pre-build pins in `packaging/runtime-constraints.txt`. Each environment
ships that exact file as `ComfyUI/runtime/requirements.lock`, records the
installed set separately, and binds the lock path and digest in its schema-v2
manifest. This prevents ordinary transitive-version drift. The artifacts are
not claimed to be byte-for-byte reproducible because all wheel bytes and build
tools are not yet hash-locked.

## GitHub Actions artifacts

Run `.github/workflows/build-artifacts.yml` manually from the repository's
Actions tab. It performs these gates:

1. Builds on Ubuntu 22.04 so generated native binaries retain the supported
   glibc 2.35 baseline, reclaiming unused hosted-runner SDKs first.
2. Builds the complete first-install archive, then creates the environment
   archive from that exact completed staging tree without reinstalling
   dependencies.
3. Structurally preflights both archives after relocation, exercises their
   bundled interpreters without launching a desktop, and records SHA-256 values
   in the job log.
4. Uploads both multi-gigabyte archives with one-day retention and no redundant
   outer compression.

The workflow has one build job. It does not install Xvfb or Weston, emulate a
desktop, launch Qt WebEngine, or run GUI smoke tests on a hosted runner.

GitHub's artifact service wraps uploaded files in its own downloadable
container; `compression-level: 0` avoids trying to recompress the inner
`.tar.gz`. No workflow creates a GitHub Release or pushes generated binaries to
the repository.

After a successful run, download the same short-lived files locally with the
GitHub CLI (replace `RUN_ID` with the workflow run ID):

```bash
gh run download RUN_ID --name Portable-Comfy-linux-x86_64.tar.gz
gh run download RUN_ID --name Portable-Comfy-core-v0.27.0.tar.gz
```

Actions artifacts consume account artifact storage even for a public
repository. One-day retention minimizes persistent usage but does not waive
storage or per-artifact service limits. A failed upload is a distribution
failure and must remain visible; the workflow must not silently omit CUDA or
substitute a smaller CPU package.

## What preflight and optional local smoke tests prove

The environment preflight verifies archive safety, the complete schema-v2 file
and symlink manifest, requirements-lock identity, candidate-runtime relocation,
pinned imports and a freshly compiled native extension. It runs the interpreter
inside the candidate `ComfyUI/`, never the active/full archive's runtime.

The optional local transactional-update smoke uses `EnvironmentUpdater` itself to stage
that downloaded archive, run the candidate's version/import, `pip check` and
ComfyUI quick tests, atomically swap the complete generation, start and
health-check it, and retain the old generation for rollback. Sentinels under
`models/`, `custom_nodes/`, `workflows/`, `user/`, `output/` and
`custom_node_runtime/` must remain byte-identical, and the activated runtime
must import a module installed in the persistent node venv. This local check is
not part of the artifact-building workflow.

The optional local first-install smoke test runs without an NVIDIA GPU and uses
`--allow-no-gpu` solely to check:

- complete-archive integrity and relocatable paths;
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
