# Contributing

Contributions are welcome. This page summarizes the conventions that a
contribution must meet before it is accepted.

## Before a pull request

Make sure all gates are green locally (see [Setup](setup.md)):

```bash
ruff check .
ruff format --check .
pytest
npm run lint
npm test
docker build --check -f docker/Dockerfile .
```

## Code conventions

- **Ruff** for lint and format, `line-length = 120`, `target-version = "py313"`.
- Routes contain no SQL; they call repositories in `app/db/` (`ARC-01`).
- No function-local imports to work around circular dependencies (`ARC-03`).
- Guidelines: no module over ~600 lines, no function over ~80 lines without
  a justified exception (`ARC-02`).
- Exceptions are never silently swallowed; broad `except` only with a reason and
  logging.

## Tests

Test areas follow the current feature and API contracts; the
[specification](../spec/justup-spec.md) is a historical planning draft. Important
areas include:

- Auth: bearer API, cookie-protected UI/self-service routes, selective CSRF protection,
  role matrix, bootstrap gate, and lockout per IP/user
- Targets: wizard profiles, preview, duplicates, atomic recipient assignment, and
  the independent edit path
- Migrations: identical schema for fresh installation and upgrade
- Scheduler and Quality Gate
- Alert logic, spooler, checks (including SSRF/DNS rebinding)
- Measurement data rollups and backup/restore including maintenance mode and quiescing

## Versioning and release

- The version is exclusively in `pyproject.toml`; `app/utils/version.py`
  reads it from there. No `VERSION`, no `requirements.txt`.
- `pyproject.toml` holds direct dependencies. Docker and CI install the
  complete hash lock `docker/requirements.lock` wheel-only; the local
  SSLyze wheel and its provenance are in `docker/vendor/`. To update
  the runtime hashes, use `python tools/relock-runtime.py --write`; the
  reproducible SSLyze wheel rebuild is described in
  `docker/vendor/sslyze/build-wheel.sh`.
- Release: set the version in `pyproject.toml`, add to `CHANGELOG.md`, push the tag
  `vX.Y.Z` – the release pipeline does the rest.

!!! note "License"
    justUp! is released under the **MIT license**. Contributions are accepted under this
    license.
