# Building and testing

The repository has a fast launcher test path and a slower pinned
portable-runtime build. The CUDA workflow is manual because its intermediate
tree and artifacts are several gigabytes.

## Local launcher tests

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
or display. It checks launcher configuration and portable-root invariants.

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

scripts/build_core_bundle.sh \
  --output-dir "$PWD/artifacts" \
  --work-dir /path/with/ample/free-space

scripts/preflight_portable.sh \
  artifacts/Portable-Comfy-linux-x86_64.tar.gz

scripts/smoke_artifact.sh \
  artifacts/Portable-Comfy-linux-x86_64.tar.gz \
  --timeout 120 \
  --allow-no-gpu
```

`build_portable.sh` also accepts `--skip-appimage` and `--skip-runtime` for
targeted packaging development. Those modes are not release-equivalent. The
normal build produces:

```text
Portable-Comfy-linux-x86_64.tar.gz
Portable-Comfy-core-v0.27.0.tar.gz
```

The complete archive includes the AppImage, source-built portable CPython,
CUDA-enabled Torch, pinned Core/frontend, persistent directory layout, and
eight pinned TAESD preview encoder/decoder weights under `models/vae_approx/`.
It does not include user checkpoints or other generation models. The Core
archive contains only the transactional replaceable payload.

The named baseline and every currently resolved Python distribution are exact
pre-build pins in `packaging/runtime-constraints.txt`; the completed artifact
also records the installed set in `manifest/runtime-requirements.lock`. This
prevents ordinary transitive-version drift, but the full runtime is not claimed
to be byte-for-byte reproducible because all wheel bytes/build tools are not
yet hash-locked. The Core-only archive is built deterministically from
checksummed source/frontend inputs.

## GitHub Actions artifacts

Run `.github/workflows/build-artifacts.yml` manually from the repository's
Actions tab. It performs these independent gates:

1. Builds on Ubuntu 22.04 so generated native binaries retain the supported
   glibc 2.35 baseline, and reclaims large unused SDKs to make room for the
   unpacked CUDA and Qt trees.
2. Builds the complete and Core-only archives using pinned versions.
3. Runs structural preflight and records SHA-256 checksums.
4. Uploads both archives with one-day retention and no additional compression.
5. Starts a fresh job, downloads both uploaded artifacts, verifies their
   checksums and Core manifest, and launches the complete artifact's server and
   frozen desktop shell beneath Xvfb.

GitHub's artifact service wraps uploaded files in its own downloadable
container; `compression-level: 0` ensures it does not waste CPU or temporary
space trying to recompress the inner `.tar.gz`. No workflow creates a GitHub
Release or pushes generated binaries to the repository.

After a successful run, download the same short-lived files locally with the
GitHub CLI (replace `RUN_ID` with the workflow run ID):

```bash
gh run download RUN_ID --name Portable-Comfy-linux-x86_64
gh run download RUN_ID --name Portable-Comfy-core-v0.27.0
```

Actions artifacts consume account artifact storage even for a public
repository. One-day retention minimizes persistent usage but does not waive
storage or per-artifact service limits. A failed upload is a distribution
failure and must remain visible; the workflow must not silently omit CUDA or
substitute a smaller CPU package.

## What the smoke test proves

The downloaded-artifact smoke job runs on a GitHub-hosted machine without an
NVIDIA GPU. `--allow-no-gpu` permits ComfyUI's CPU fallback solely to check:

- archive integrity and relocatable paths;
- the bundled interpreter, imports and a freshly compiled native extension
  after relocation to a path containing spaces;
- server startup and HTTP readiness;
- the actual AppImage creating a Qt WebEngine window under a virtual X server,
  loading the healthy compiled frontend, receiving its loaded event and
  closing cleanly;
- orderly launcher-driven shutdown.

It does not prove CUDA inference, model correctness or third-party custom-node
compatibility. Before wider distribution, test the same artifact on each
supported Ubuntu version and on a Turing-or-newer NVIDIA host with an
R580-or-newer driver.
