<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-09-21 | Updated: 2026-10-30 -->
# workflows

## Purpose
GitHub Actions for the justUp PR gate, multi-architecture Docker Hub release, and MkDocs Pages deployment.

## Files
| File | Role |
| --- | --- |
| `ci.yml` | Push and PR gate on `main`, manual dispatch, and reusable release gate. Runs Ruff, pytest (parallel, `-n auto`), entrypoint syntax, Dockerfile checks, manifest checks, actionlint with shellcheck, and a Trivy source secret/misconfiguration scan. |
| `docker-build.yml` | Runs only for version tags or manual dispatch on a tag. Validates the tag and ancestry, calls CI, builds and smoke-tests both architectures, scans images, publishes `DOCKERHUB_USERNAME/justup`, assembles and verifies the digest-based index, then promotes `latest` from the verified digest (`moves_latest`), cleans fragment tags, and creates a GitHub release. |
| `docs-build.yml` | Builds `docs/mkdocs.yml`, scans the resolved docs dependencies, checks links, and deploys from `main` to GitHub Pages. A successful release also triggers a refresh, built from the current head of `main` (never the release commit). |

## Working here
- The application version comes from `pyproject.toml`. The release tag must be `v` plus that version.
- Pin third-party actions to verified full commit SHAs with version comments. The Trivy action revision sets its scanner default; do not override it with `version: latest`.
- Keep `permissions` minimal, release concurrency serialized, and `is_newest` fail-closed. An older tag must never move `latest` or prune a newer release.
- `meta` declares the build matrix once (step `matrix`); `build` consumes it and `create-manifest` requires exactly those architectures in the digest and toolchain files before publishing. `tools/release-newest.py <tags-file> <version> <arch>...` takes the architectures from that matrix (fragment tags `<version>-<arch>` are not releases) and yields `is_newest` (no newer release of any kind; gates the tag cleanup) and `moves_latest` (stable and newest stable; gates `latest`). A prerelease never moves `latest`. `latest` is promoted only after the version index passed `verify_manifest.py` (platforms and pushed digests).
- All Docker Hub access (login, paginated listing, delete with optional absence polling) goes through `tools/dockerhub.py` (stdlib only, tested in `tests/test_dockerhub.py`); credentials via env, a listing error never yields a partial result. The release policy (keep the current version and `latest`) stays in the `cleanup-dockerhub-tags` job.
- `create-release` needs only `meta` and `create-manifest`: tag pruning runs after manifest verification but a pruning failure turns the run red without blocking or undoing the release. Trivy reports and the release asset upload are mandatory; only a missing CHANGELOG section falls back to a hint text.
- Docker images use the `justup` repository. The smoke test supplies a test-only `SECRET_KEY` and checks `/healthz` on port 8000. The Dockerfile supplies its own healthcheck.
- libsignal is built from source for both architectures (Dockerfile stage `libsignal-build`). After the smoke test, `tools/verify-libsignal-native.py` runs inside each image and checks the packaging only: exactly one non-empty `libsignal_jni_<arch>.so` in the libsignal-client jar with a matching ELF64 little-endian header. It does not prove that the JVM loads the library; native load and a real Signal send stay a manual gate.
- The toolchain manifest also carries `java_version` and `libsignal_client` (read from `/opt/COMPONENTS.txt`), so the parity gate compares them across architectures. Keep architecture-specific values (library file names, hashes) out of it.
- Resolve the shared Python base digest and signal-cli release in `meta`. The Dockerfile still resolves `FLAG_ICONS_VERSION=main` and apt packages during each build; those inputs are not frozen across architectures.
- The documentation config lives at `docs/mkdocs.yml`. Root `CHANGELOG.md` and `LICENSE` are copied into the docs tree by the workflow before a strict build.
- `.trivyignore` is the versioned exception file. Keep an empty file when there are no accepted findings. Used by the image and docs scans; do not delete it.
- `.trivyignore.yaml` holds the one accepted misconfiguration (`DS-0002`, `docker/Dockerfile` only, with `expired_at`) and is used by the `ci.yml` source scan only.

## Validation
- Run `actionlint .github/workflows/*.yml` after edits; it checks inline shell through shellcheck when shellcheck is installed.
- The focused local checks are `bash -n docker/entrypoint.sh`, `docker build --check -f docker/Dockerfile .`, and `mkdocs build -f docs/mkdocs.yml --strict` after copying the root docs files into a disposable docs tree.
- Image publishing, registry tag cleanup, GitHub releases, and Pages deployment run only in their protected GitHub Actions jobs.

<!-- MANUAL: Any manually added notes below this line are preserved on regeneration -->
