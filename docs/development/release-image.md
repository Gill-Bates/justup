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

All assets are published atomically. `create-release` creates the GitHub release as a **draft**, uploads every asset (source archive, signature, source manifest, and per architecture the Trivy report, toolchain manifest, Nmap provenance, SBOM and NVD report), reads the stored bytes back and compares size, API digest and SHA-256 with the local files (`tools/release-publish.py`), and only then publishes. A published tag is never changed: if the release for the tag exists and is complete, the job does nothing; if it is published with a missing, different or additional asset, the job fails and leaves it alone (a new version is needed). An unpublished draft from an earlier failed run is resumed, and only its assets are replaced. Before the registry push, the `build` job already checks that the prepared source archive matches the hash the image was built from.

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

`/usr/share/doc/nmap/PROVENANCE.txt` in the image, the `nmap-provenance-<arch>.txt` release asset and the "Nmap composition" section of the release notes list the bundled library versions and the Debian packages. The bundled versions come from the signed source tarball, so they are identical on both architectures (`nmap_bundled_libraries` in `toolchain-<arch>.txt`, compared by the parity gate). When a CVE is published for libpcap, libdnet, liblinear or Nmap, compare it with these versions by hand; a green Trivy result says nothing about them. Moving libpcap to Debian was not done: Debian's libpcap is older than the bundled one and would add `libdbus-1-3` to the runtime image. Nmap and libpcap are additionally checked against NVD by CPE (see [NVD gate for the compiled components](#nvd-gate-for-the-compiled-components)); libdnet and liblinear remain unchecked.

The build proves the Debian copies of zlib and PCRE2 in two ways. PCRE2 is a direct `NEEDED` entry of the binary. zlib is not: the linker leaves `libz.so.1` out of Nmap's own list and it resolves only through `libcrypto`, so the check reads the banner (`libz-<version>` for Debian, `nmap-libz-<version>` for the bundled copy) and the resolved `libz.so.1` in the `ldd` output. The runtime stage compares the library list recorded by the builder (`runtime_dependencies`) with its own `ldd` result and fails on any difference, then adds `system_libraries` for the packages that own those libraries.

## Inputs that move with the build time

The `nmap-builder`, `libsignal-build`, `signal-cli-dist`, `jre-builder` and `runtime` stages run `apt-get update && apt-get upgrade` during each build.

The Nmap builder and the runtime stage upgrade independently. If Debian publishes a library update between the two, the builder/runtime library comparison fails the build; rebuild in that case.

Consequence: two builds with the same base digest, run at different times, can contain different Debian package versions. The base digest fixes the starting point, not the final package set. A bit-for-bit reproducible rebuild would additionally need pinned package sources or a Debian snapshot; the pipeline does not provide that.

The Trivy scan of the built images and the digest recorded per architecture show what a given release actually contains.

The release archives each architecture digest, `toolchain-<arch>.txt` (Python packages, signal-cli, libsignal, Java and Nmap versions, the Temurin package version, and the complete `dpkg-query -W` inventory), `nmap-provenance-<arch>.txt`, `nmap-sbom-<arch>.cdx.json`, `nmap-nvd-<arch>.json`, and the Trivy report. The Debian inventory and Temurin package version are recorded per architecture; unlike the shared toolchain inputs, their revisions need not match. The moving repository inputs (Debian and Adoptium repositories, `FLAG_ICONS_VERSION=main`) are not archived.

The Trivy gate covers dpkg and language packages only. The libraries compiled into Nmap and Nmap itself are covered by the NVD gate below, to the extent NVD publishes CPEs for them.

## NVD gate for the compiled components

`tools/nvd-gate.py` (tests: `tests/test_nvd_gate.py`) closes part of that gap in the `build` job, before anything is pushed:

1. `sbom` writes a CycloneDX 1.5 SBOM from the image's `PROVENANCE.txt`: Nmap with its exact verified version and the bundled libraries with their versions, each with a `pkg:generic` purl and, where NVD has an entry, an explicit CPE (`cpe:2.3:a:nmap:nmap:<version>`, `cpe:2.3:a:tcpdump:libpcap:<version>`).
2. `check` asks the NVD CVE API 2.0 (`virtualMatchString`, NVD evaluates the version ranges) for every component with a CPE. A CVE with a CVSS v4.0, v3.1 or v3.0 base score of 9.0 or higher blocks the release (exit 1). An unreachable or malformed NVD answer also fails the job (exit 3); it is never read as "no findings".
3. SBOM and report are release assets (`nmap-sbom-<arch>.cdx.json`, `nmap-nvd-<arch>.json`).

Limits, stated plainly:

- Trivy is not used for this. It matches packages by purl against distribution and ecosystem advisories and does not match generic components against NVD CPEs; a `pkg:generic` entry would produce no result, which would look like a pass. (Source: Trivy's SBOM output carries purls only, [aquasecurity/trivy discussion 5209](https://github.com/aquasecurity/trivy/discussions/5209); not re-verified against the Trivy version the action installs.)
- **libdnet and liblinear are not checked.** The NVD CPE dictionary has no entry for either (queried 2026-11-05); the report lists them as `NOT COVERED`. Review their CVEs by hand against `bundled_libraries`.
- NVD data lags: a CVE without a CVSS v3/v4 score yet ("Awaiting Analysis") is listed as a warning and does not block. Only CRITICAL (>= 9.0) blocks; HIGH does not.
- CPE assignment in NVD is hand-made. A CVE filed under a different vendor or product name is not found.
- NVD is a live dependency. A rate limit (HTTP 403/429) or outage stops the release; rerun it. An optional repository secret `NVD_API_KEY` raises the rate limit.
- A false positive is accepted only in `.nvdignore` (`CVE-YYYY-NNNN <reason>`; an entry without a reason is rejected). The file is empty.

What is proven locally: the unit tests drive the gate with a CRITICAL fixture and expect a blocked release, and a manual run against the live NVD API reported CVE-2011-1935 (9.8) for libpcap 1.1.2 and no CRITICAL CVE for Nmap 7.98 and libpcap 1.10.5. What only the first release run shows: the step in Actions, the runner's NVD rate limit and the artifact upload.

## Image size (diagnosis, not a release gate)

Docker Hub shows the **compressed** size of a platform image: the sum of the layer sizes in that platform's manifest. `docker images` and `docker image inspect` show the **unpacked** size. The two are different quantities and are not comparable, and a gzip level or a different compressor changes the compressed one without changing the content. The reported compressed difference of 206 MB against 250 MB is not explained: the repository holds no measurement of either value (not even its source, which platform or which tag), and no image was built to measure it. The unpacked comparison values the project has are not stored in the repository either.

Parts of the image that legitimately differ between `linux/amd64` and `linux/arm64` (unmeasured, to be checked against the numbers below): the libsignal JNI library and the Temurin `jlink` runtime (both architecture-specific binaries), the natively compiled Nmap binary and the Debian package set. The Dockerfile has no step that could make one architecture larger on purpose.

To measure instead of guessing, take the three numbers for each platform of the same published tag and compare them layer by layer:

```bash
# Compressed size as Docker Hub reports it, per platform (child manifest digest from the index)
docker buildx imagetools inspect --raw "$IMAGE@$CHILD_DIGEST" | jq '[.layers[].size] | add'
docker buildx imagetools inspect --raw "$IMAGE@$CHILD_DIGEST" | jq -r '.layers[] | "\(.size)\t\(.digest)"'
# Unpacked size and the per-layer origin of a locally built image (docker build --load, amd64 only)
docker image inspect "$LOCAL_IMAGE" --format '{{.Size}}'
docker history --no-trunc --format '{{.Size}}\t{{.CreatedBy}}' "$LOCAL_IMAGE"
```

A layer that is large in `docker history` but small in the manifest compresses well (text, JARs, Python bytecode); a large compressed layer points to incompressible content (the `jlink` runtime image modules, native libraries, wheels). Do not change the Dockerfile for size without such a per-layer measurement. Removing the bundled `liblinear` is deliberately deferred and is not part of this diagnosis. The `arm64` image is never built locally; its numbers come from the registry manifest.

## Updates

Renovate (`.github/renovate.json`) has a Docker rule with `pinDigests` and proposes digest and version updates for images referenced in the Dockerfile, such as `RUST_IMAGE`. The Python base digest of a release comes from the workflow, not from a pin in the repository, so there is no second pin to keep in sync.

## Published version tags are immutable

The release workflow does not overwrite an existing version tag on Docker Hub. If `<version>` is already published, a re-run only confirms it: the platform digests built in the run must equal the ones in the published index, otherwise the run stops. Updated dependencies (Nmap, signal-cli, base image) ship as a new justUp version. `latest` and the tag cleanup are decided again against the registry immediately before they happen.

## Official release versus manual build

Only the tag-driven workflow `docker-build.yml` produces a release: it publishes to Docker Hub after the CI gate, Trivy, the runtime-image purity check, multi-architecture toolchain parity and the manifest verification. A manual `docker buildx build --push` to another registry (for example a private one) skips all of these and is a convenience build, not a release proof.
