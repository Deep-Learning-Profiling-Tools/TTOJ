# Source and build scope

## What is published

This repository contains the TTOJ integration source: the Hydro plugin and
template overrides, bootstrap scripts, separate Python judge worker, TTNN
wrapper, Metalium host harnesses and kernel examples, bundled problem generators
and metadata, evaluation policy, research utilities, and build recipes.

The project release applies AGPL-3.0-or-later to TTOJ-authored code. Existing
third-party notices and licenses are retained for adapted source. See the root
LICENSE and NOTICE for the component details. Splitting services
or repositories does not by itself change the applicable license of a source
file.

## What is generated or kept private

The following are deployment artifacts rather than repository content:

- `.env`, SSH identities, and other credentials.
- MongoDB and Hydro file/configuration volumes, accounts, and stored passwords.
- Runtime submissions, attempt archives, profiling artifacts, telemetry,
  experiment results, and pseudonymization keys under `data/`.
- TT-Metal checkout, submodules, downloaded dependencies, compiled SDK and
  Python wheels under `profiling-build/`.
- Extracted MPI shared libraries and generated validation reports/logs.

The small bundled problem manifests are published examples. Publishing them is
not permission to publish submissions or private test data uploaded by operators
or participants. Source publication does not reset passwords or publish the
running site's database.

## Rebuilding the software

### Website and worker

`site/Dockerfile` installs `hydrooj@5.0.7` and
`@hydrooj/ui-default@4.58.5` from npm and copies the local site extension.
`worker/Dockerfile` installs the dependencies in `worker/requirements.txt` and
copies the local worker, policy, TTNN wrapper, and Metalium reference sources.
Run `docker compose build site worker` after configuring `.env` and Compose as
described in the README.

Upstream repository: [Hydro](https://github.com/hydro-dev/Hydro).
The upstream packages retain their licenses; the bottom-level MIT framework does
not change the license of the Hydro OJ application or default UI.

### Current profiling-enabled execution images

Run `./profiling-build/build.sh` from the project root. It retrieves the official
TT-Metal v0.79.0 source at commit
`de546d3b146758714d900f11b218c8f9c805f410`, initializes the pinned submodules,
builds the unmodified SDK and Python wheel in the official container, and then
creates:

- `tt-oj-profiler-runtime:0.79.0` for TTNN.
- `tt-oj-metalium-profiler:0.79.0` for the trusted Metalium harnesses.

The build uses the official base image digest
`sha256:ed828284c1b95eaaaac784aa777df16338d3788baa6af971c13aa4789fb75b5c`.
`profiling-build/provenance.json` records the SDK, toolchain, and profiler build
configuration. Both current execution paths use this SDK build; the device
profiler is enabled only for a separate diagnostic pass. The SDK commit is
pinned, but external package repositories and transitive dependencies are not
all frozen by a complete dependency lockfile. These scripts provide a build
recipe, not a claim of byte-for-byte reproducible binaries.

Upstream source and license:
[TT-Metal at the pinned commit](https://github.com/tenstorrent/tt-metal/tree/de546d3b146758714d900f11b218c8f9c805f410).

### Legacy non-profiler Metalium image

`sandbox/metalium/Dockerfile` describes an earlier apt-based image,
`tt-oj-metalium:0.79.0`. It is not the profiling-enabled image selected by the
current Compose configuration. Its pinned packages come from the official
Tenstorrent apt repository; the included `.asc` file is a public repository
signing key, not an SSH/private key.

Before building this optional image, generate its MPI dependencies from the
pinned official runtime:

```bash
./sandbox/metalium/prepare-mpi.sh
docker build -t tt-oj-metalium:0.79.0 sandbox/metalium
```

The helper extracts only the three required MPI shared libraries into the ignored
`sandbox/metalium/mpi/` directory. Its README records historical validation and
the old release package's profiler limitation; those historical image tags and
numbers are not the current measurement protocol.

## Deployed source access

The repository is the source publication location for TTOJ. A deployed site must
offer source matching its running version, including operator modifications and
necessary build/install scripts. Pin a release or commit when deploying and
update the source offer when changing the deployed code. A link to unmodified
upstream Hydro alone does not describe local TTOJ changes.

Keep runtime secrets and participant data outside source archives. Replace local
hostnames, paths, and credentials with configurable examples when sharing
deployment configuration; retain the configuration structure required to build
and install the software.
