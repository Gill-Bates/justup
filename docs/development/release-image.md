# Release image: pinned inputs and moving inputs

Release images are not bit-for-bit reproducible.

## Fixed inputs

| Input | Where it is fixed |
|-------|-------------------|
| Python base image | The release workflow (`.github/workflows/docker-build.yml`, step "Resolve rolling toolchain versions") resolves `python:3.13-slim` once per release to `python:3.13-slim@sha256:…` and passes it as `PYTHON_BASE` to both architecture builds. Both architectures therefore build on the same base index, even if the tag moves during the run. |
| Rust toolchain of the libsignal stage | `RUST_IMAGE` in `docker/Dockerfile`, by tag and digest. |
| Python packages | `docker/requirements.lock` (hash-checked, wheel-only) plus the vendored SSLyze wheel in `docker/vendor/wheels/`. |
| signal-cli | Resolved once per release from the latest GitHub release and shared by both builds. Its asset is checked against the SHA-256 digest GitHub publishes. |
| Built architecture digests | The workflow records one image digest per architecture and builds the manifest from those digests. |

`ARG PYTHON_BASE=python:3.13-slim` in `docker/Dockerfile` is a tag only. A local `docker build` without `--build-arg PYTHON_BASE=…@sha256:…` therefore uses whatever the tag points to at that moment.

## Inputs that move with the build time

The runtime and `signal-cli-dist` stages run `apt-get update && apt-get upgrade` during each build.

Consequence: two builds with the same base digest, run at different times, can contain different Debian package versions. The base digest fixes the starting point, not the final package set. A bit-for-bit reproducible rebuild would additionally need pinned package sources or a Debian snapshot; the pipeline does not provide that.

The Trivy scan of the built images and the digest recorded per architecture show what a given release actually contains.

## Updates

Renovate (`.github/renovate.json`) has a Docker rule with `pinDigests` and proposes digest and version updates for images referenced in the Dockerfile, such as `RUST_IMAGE`. The Python base digest of a release comes from the workflow, not from a pin in the repository, so there is no second pin to keep in sync.
