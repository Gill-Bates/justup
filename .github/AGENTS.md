<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-09-21 | Updated: 2026-10-30 -->
# .github

## Purpose
GitHub repository automation and assets: CI/CD workflows (PR gate, multi-arch Docker release, documentation deployment), Renovate dependency policy, README screenshots and logos, and the Copilot-style prompt files for the code-review suite (code review, comment check, ignore-file check, DRY review).

## Key Files
| File | Description |
|------|-------------|
| `actionlint.yaml` | actionlint config declaring the `ubuntu-26.04` and `ubuntu-26.04-arm` runner labels that older actionlint releases do not know |
| `renovate.json` | Renovate config: `config:best-practices`, weekly schedule (before 6am Monday, Europe/Berlin), max 3 open PRs; auto-merges minor/patch GitHub Actions with digest pinning and 3-day release age, pins Docker digests, groups Python deps from `pyproject.toml` via the `pep621` manager (not auto-merged, labelled `python`), keeps trivy-action always current, majors labelled `breaking-change`, vulnerability alerts labelled `security` |
| `../tools/verify-libsignal-native.py` | Read-only verifier run inside each release image; checks the architecture-specific libsignal JNI library packaged in the jar, not a JVM load or Signal delivery |

## Subdirectories
| Directory | Purpose |
|-----------|---------|
| `skills/` | Prompt files for the code-review suite: `01_CodeReview.md`, `02_CheckComments.md`, `03_CheckIgnoreFiles.md`, `04_DRY.md` (no AGENTS.md) |
| `workflows/` | GitHub Actions pipelines: `ci.yml` (PR gate), `docker-build.yml` (tagged release), `docs-build.yml` (Pages). See `workflows/AGENTS.md` |

## For AI Agents

### Working In This Directory
- Workflow actions are pinned (some by commit SHA for supply-chain safety); keep pins when editing.
- Do not commit secrets; workflows read Docker Hub credentials from repository secrets.
- Renovate config is strict JSON (4-space indent).
- `ci.yml` is the only workflow that gates a PR. `docker-build.yml` only runs on tags — keep that division when adding checks: deterministic ones belong in `ci.yml`; UX/UI browser audits run only locally, not in CI.

### Testing Requirements
- No local test runner for the workflows themselves; validate YAML syntax and rely on the PR run. Validate `renovate.json` with `npx renovate-config-validator` if available.
- The steps inside them are reproducible locally — see `workflows/AGENTS.md`. The release workflow checks each built image's packaged libsignal native library and compares installed toolchain versions across architectures; it does not prove that Signal starts or sends a message.

### Common Patterns
- Least-privilege `permissions:` blocks per workflow/job, `concurrency` groups, explicit `timeout-minutes`, named runner images rather than `ubuntu-latest`.

## Dependencies

### Internal
- `docker/Dockerfile`, `docker/entrypoint.sh`, `docs/mkdocs.yml`, `pyproject.toml`, `CHANGELOG.md`, `LICENSE`, `.trivyignore`, `.trivyignore.yaml`, `tests/`, `tools/`.

### External
- GitHub Actions, Docker Hub, GitHub Pages, Renovate, Trivy.

<!-- MANUAL: Any manually added notes below this line are preserved on regeneration -->
