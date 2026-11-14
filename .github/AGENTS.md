<!-- Generated: 2026-09-21 | Updated: 2026-11-06 -->
# .github

## Purpose
GitHub repository automation and assets: CI/CD workflows (PR gate with Python, container, frontend, manifest, workflow-lint, and source-scan jobs; multi-arch Docker release; documentation deployment; weekly security rescan of the published image), Renovate dependency policy, and the Copilot-style prompt files for the code-review suite (code review, comment check, ignore-file check, DRY review).

## Key Files
| File | Description |
|------|-------------|
| `actionlint.yaml` | actionlint config declaring the `ubuntu-26.04` and `ubuntu-26.04-arm` runner labels that older actionlint releases do not know |
| `renovate.json` | Renovate config: `config:best-practices`, weekly schedule (before 6am Monday, Europe/Berlin), max 3 open PRs; auto-merges minor/patch GitHub Actions with digest pinning and 3-day release age, pins Docker digests, groups Python deps from `pyproject.toml` via the `pep621` manager (not auto-merged, labelled `python`), keeps trivy-action always current, majors labelled `breaking-change`, vulnerability alerts labelled `security` |
| `../tools/verify-libsignal-native.py` | Read-only verifier run inside each release image; checks the libsignal JNI library packaged in the jar (name, ELF header) and dlopen()s it, not a JVM load or Signal delivery |
| `../tools/verify-runtime-image.py` | Runtime image purity check (no build tools, caches, source trees or object files; filesystem and layers), run before the push |
| `../tools/verify-nmap-worker.py`, `../tools/nmap-release.py` | Nmap release gate: `nmap-release.py` resolves and PGP-verifies the stable source release in `meta`; its `release-assets` command prepares the verified archive, signature and manifest as release assets; `verify-nmap-worker.py` runs inside each image (binary, capabilities, scan and trace through the worker socket) |
| `../tools/nvd-gate.py`, `../tools/release-publish.py` | Release gates: `nvd-gate.py` builds the Nmap SBOM and blocks on CRITICAL NVD CVEs for Nmap and libpcap (exceptions in `.nvdignore`); `release-publish.py` plans and verifies the draft-first GitHub release (assets by size, digest and SHA-256) |
| `../tools/verify-manifest.py`, `../tools/toolchain-parity.py`, `../tools/trivy-report.py`, `../tools/release-newest.py`, `../tools/dockerhub.py` | Tested release helpers: index and digest check, architecture parity with required fields, Trivy report validation and summary, newest-release decision, Docker Hub access (details in `workflows/AGENTS.md`) |

## Subdirectories
| Directory | Purpose |
|-----------|---------|
| `skills/` | Prompt files for the code-review suite: `01_CodeReview.md`, `02_CheckComments.md`, `03_CheckIgnoreFiles.md`, `04_DRY.md` (no AGENTS.md) |
| `workflows/` | GitHub Actions pipelines: `ci.yml` (PR gate), `docker-build.yml` (tagged release), `security.yml` (weekly read-only rescan of the published image), `docs-build.yml` (Pages). See `workflows/AGENTS.md` |

## For AI Agents

### Working In This Directory
- Every third-party workflow action is pinned to a full commit SHA with a version comment; keep pins when editing.
- Do not commit secrets; workflows read Docker Hub credentials from repository secrets.
- Renovate config is strict JSON (4-space indent).
- Agents neither push nor publish (the user pushes); no weakening of gates and no skipped tests to get a workflow green.
- `ci.yml` runs on pushes to all branches and gates PRs targeting `main`. The release workflow calls it again for the tagged commit. `docker-build.yml` only starts from version tags or a manual tag dispatch — keep that division when adding checks: deterministic checks, focused Chromium/WebKit UI regressions and the Chromium + WebKit shell viewport matrix belong in `ci.yml`; the full profile and Firefox remain local.

### Testing Requirements
- No local test runner for the workflows themselves; validate YAML syntax and rely on the PR run. Validate `renovate.json` with `npx renovate-config-validator` if available.
- The steps inside them are reproducible locally; job contents and commands are listed once in `workflows/AGENTS.md`. Run `actionlint .github/workflows/*.yml` after workflow edits.
- Local reproduction of the Playwright, pytest and Node steps goes through `tools/run-limited.sh` (see root `AGENTS.md`); the workflows themselves call the tools directly.
- Never run the arm64 image build locally (no emulation); `linux/arm64` is built and verified only on the native runner in Actions.
- The release workflow checks each built image's Nmap worker (privilege separation, scan, traceroute, no `mtr`), libsignal native library (packaging plus dlopen) and runtime-image purity, publishes only a verified candidate index, and compares installed toolchain versions across architectures; it does not prove that Signal starts or sends a message. `security.yml` rescans the published image weekly (untested until its first Actions run).

### Common Patterns
- Least-privilege `permissions:` blocks per workflow/job, `concurrency` groups, explicit `timeout-minutes`, named runner images rather than `ubuntu-latest`.

## Dependencies

### Internal
- `docker/Dockerfile`, `docker/entrypoint.sh`, `docker/nmap-worker.sh`, `docker/vendor/`, `docs/mkdocs.yml`, `pyproject.toml`, `package.json`, `CHANGELOG.md`, `LICENSE`, `.trivyignore`, `.trivyignore.yaml`, `app/static/` (lint targets), `tests/`, `tools/`.

### External
- GitHub Actions, Docker Hub, GitHub Pages, Renovate, Trivy, Node.js/npm (ESLint, Stylelint).

<!-- MANUAL: Any manually added notes below this line are preserved on regeneration -->
