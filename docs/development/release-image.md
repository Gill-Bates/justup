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

## Nmap

Nmap is not pinned. The release workflow resolves the latest stable release once, verifies its PGP signature and source hash and builds both architectures from that same source (details: [Network monitoring with Nmap](../features/network-monitoring.md#docker-image-and-nmap-version)). A new Nmap release ships with the next justUp image build.

### Nmap source archive as a release asset

The runtime image contains no Nmap source archive (`tools/verify-runtime-image.py` rejects one). Instead, the `meta` job uploads the archive it verified, its signature and the Nmap signing keys as a run artifact. Before the GitHub release is written, `tools/nmap-release.py release-assets` in `create-release` checks the signature again, compares the archive hash with the hash the images were built with (`NMAP_SOURCE_SHA256`) and with `source_sha256`, version, URL and key fingerprint in every architecture's `PROVENANCE.txt`, and fails the release on any difference or missing file. The release then carries:

| Asset | Content |
|---|---|
| `nmap-<version>.tar.bz2` | The exact source archive the images were built from |
| `nmap-<version>.tar.bz2.asc` | Its upstream PGP signature |
| `nmap-source-manifest.txt` | Version, source URL, SHA-256, size, signing key fingerprint, licence name and URL (NPSL), verified image index digest |

This provides the archive and the hash evidence; it is not a legal assessment of redistributing Nmap under the NPSL. That review is a separate open task. Whether the assets upload correctly can only be seen in the first real release run.

### What the vulnerability scan sees of Nmap

Trivy (`scanners: vuln`) reports only packages from a package database (dpkg) or a language lockfile. The Nmap binary is compiled from source and copied into the image, so it has no dpkg entry and **Trivy cannot report CVEs in Nmap itself**. Its libraries are handled in two groups:

| Library | Origin in the image | Version source | Scanned by Trivy |
|---|---|---|---|
| OpenSSL (`libssl3t64`, `libcrypto`) | Debian package, dynamic | `system_libraries` in `PROVENANCE.txt` | yes |
| PCRE2 (`libpcre2-8-0`) | Debian package, dynamic | `system_libraries` | yes |
| zlib (`zlib1g`) | Debian package, dynamic | `system_libraries` | yes |
| libpcap | bundled in the Nmap source, static | `bundled_libraries` | **no** |
| libdnet (Nmap fork) | bundled, static | `bundled_libraries` | **no** |
| liblinear | bundled, static | `bundled_libraries` | **no** |
| libssh2 | not built (`--without-libssh2`; only NSE uses it, and NSE is not built) | n/a | n/a |
| Nmap itself | compiled from the verified source | `nmap_version`, `source_sha256` | **no** |

`/usr/share/doc/nmap/PROVENANCE.txt` in the image, the `nmap-provenance-<arch>.txt` release asset and the "Nmap composition" section of the release notes list the bundled library versions and the Debian packages. The bundled versions come from the signed source tarball, so they are identical on both architectures (`nmap_bundled_libraries` in `toolchain-<arch>.txt`, compared by the parity gate). When a CVE is published for libpcap, libdnet, liblinear or Nmap, compare it with these versions by hand; a green Trivy result says nothing about them. Moving libpcap to Debian was not done: Debian's libpcap is older than the bundled one and would add `libdbus-1-3` to the runtime image. Matching Nmap's components against NVD by CPE is a possible follow-up, not part of the release gate.

The build proves the Debian copies of zlib and PCRE2 in two ways. PCRE2 is a direct `NEEDED` entry of the binary. zlib is not: the linker leaves `libz.so.1` out of Nmap's own list and it resolves only through `libcrypto`, so the check reads the banner (`libz-<version>` for Debian, `nmap-libz-<version>` for the bundled copy) and the resolved `libz.so.1` in the `ldd` output. The runtime stage compares the library list recorded by the builder (`runtime_dependencies`) with its own `ldd` result and fails on any difference, then adds `system_libraries` for the packages that own those libraries.

## Inputs that move with the build time

The `nmap-builder`, `libsignal-build`, `signal-cli-dist`, `jre-builder` and `runtime` stages run `apt-get update && apt-get upgrade` during each build.

The Nmap builder and the runtime stage upgrade independently. If Debian publishes a library update between the two, the builder/runtime library comparison fails the build; rebuild in that case.

Consequence: two builds with the same base digest, run at different times, can contain different Debian package versions. The base digest fixes the starting point, not the final package set. A bit-for-bit reproducible rebuild would additionally need pinned package sources or a Debian snapshot; the pipeline does not provide that.

The Trivy scan of the built images and the digest recorded per architecture show what a given release actually contains.

The release archives each architecture digest, `toolchain-<arch>.txt` (Python packages, signal-cli, libsignal, Java and Nmap versions, the Temurin package version, and the complete `dpkg-query -W` inventory), `nmap-provenance-<arch>.txt`, and the Trivy report. The Debian inventory and Temurin package version are recorded per architecture; unlike the shared toolchain inputs, their revisions need not match. The moving repository inputs (Debian and Adoptium repositories, `FLAG_ICONS_VERSION=main`) are not archived.

The Trivy gate covers dpkg and language packages only. The libraries compiled into Nmap (libpcap, libdnet, liblinear) and Nmap itself are not part of that check; a CPE/NVD check for them is a separate open task.

## Updates

Renovate (`.github/renovate.json`) has a Docker rule with `pinDigests` and proposes digest and version updates for images referenced in the Dockerfile, such as `RUST_IMAGE`. The Python base digest of a release comes from the workflow, not from a pin in the repository, so there is no second pin to keep in sync.

## Published version tags are immutable

The release workflow does not overwrite an existing version tag on Docker Hub. If `<version>` is already published, a re-run only confirms it: the platform digests built in the run must equal the ones in the published index, otherwise the run stops. Updated dependencies (Nmap, signal-cli, base image) ship as a new justUp version. `latest` and the tag cleanup are decided again against the registry immediately before they happen.

## Official release versus manual build

Only the tag-driven workflow `docker-build.yml` produces a release: it publishes to Docker Hub after the CI gate, Trivy, the runtime-image purity check, multi-architecture toolchain parity and the manifest verification. A manual `docker buildx build --push` to another registry (for example a private one) skips all of these and is a convenience build, not a release proof.
