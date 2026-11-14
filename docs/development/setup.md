# Development Setup

## Prerequisites

- Python ≥ 3.13
- Node.js 24 (for JS/CSS lint and `node:test`; the major CI pins in `ci.yml`, keep it in step)
- The system tools `ping` (for checks) and `nmap` (for traceroute and port scans)

On Debian/Ubuntu:

```bash
sudo apt-get install iputils-ping nmap
```

## Python environment

```bash
python -m venv .venv
source .venv/bin/activate
python tools/pyproject-deps.py > /tmp/justup-runtime-requirements.txt
pip install --only-binary=:all: \
  --find-links docker/vendor/wheels -r /tmp/justup-runtime-requirements.txt
python tools/pyproject-deps.py dev > /tmp/justup-dev-requirements.txt
pip install --only-binary=:all: -r /tmp/justup-dev-requirements.txt
python tools/pyproject-deps.py docs > /tmp/justup-docs-requirements.txt
pip install --only-binary=:all: -r /tmp/justup-docs-requirements.txt
```

The direct runtime dependencies are in `pyproject.toml`; each install resolves the
newest compatible wheels available at that time. `sslyze==6.3.1+justup.1` does not exist on PyPI:
the own, provenance-documented wheel is obtained via `--find-links docker/vendor/wheels`.
Docker and CI verify its SHA-256 against `docker/vendor/PROVENANCE.json` before installation.
Dev tools remain pinned in the `dev` extra; MkDocs tools are in the
`docs` extra. `tools/pyproject-deps.py` prints the respective direct dependencies.

## Frontend tools

```bash
npm install
```

For one-off interactive UX checks, the "Full browsers" block in `setup.conf`
installs Playwright (1.64.1, the repository's version) and axe-core's Playwright integration (pinned in that block) under
`/opt/ux-test-tools`. The committed layout suite uses this repository's
`node_modules` and `package-lock.json` instead. The block's `playwright install
--with-deps chromium webkit firefox` also installs operating-system packages;
it can request elevated privileges and changes the host, in addition to
downloading browser binaries.

## Run locally

```bash
# Set SECRET_KEY (Fernet key)
export SECRET_KEY="$(python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"
python run.py
```

### Startup check

Builds the app, runs the real startup (bootstrap, schema creation, scheduler, workers) and shutdown against a throw-away data directory, and requests `/healthz`. It opens no port and never touches `data/`:

```bash
export SECRET_KEY="$(python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"
JUSTUP_DATA_DIR="$(mktemp -d)" python -c '
from fastapi.testclient import TestClient
from app.main import create_app
with TestClient(create_app()) as c:
    assert c.get("/healthz").status_code == 200
'
```

Exit code 0 means the app started and stopped cleanly. A schema or configuration error ends with a non-zero code and a `FATAL` line. Delete the temporary directory afterwards.

## Quality gates

The same checks as in CI:

| Gate | Command |
| --- | --- |
| Python lint | `ruff check .` |
| Format | `ruff format --check .` |
| Python tests | `pytest -n auto` (parallel via pytest-xdist; omit `-n` for a single test or a debugger) |
| JS/CSS lint | `npm run lint` |
| JS tests | `TZ=Europe/Berlin tools/run-limited.sh npm test` |
| Container check | `docker build --check -f docker/Dockerfile .` |

### Heavy local runs

Several sessions can share one machine, so run heavy suites through the wrapper. It takes a free slot from a lock directory outside the repository (`${TMPDIR:-/tmp}/justup-test-slots-<uid>`) and sizes slots, cores and workers from this machine (affinity, cgroup limit, free RAM, load):

```bash
node tools/test-budget.mjs                      # show the computed budget
tools/run-limited.sh pytest -n auto             # -n is capped to the budget
tools/run-limited.sh npm test                   # runs npm run test:local
tools/run-limited.sh node tests/layout/run.mjs --profile quick --browsers chromium
```

A local `npx playwright test` outside the wrapper only prints a warning that no shared test slot was acquired; it is not blocked. `npm run test:local` runs the Node tests with the budgeted concurrency. Overrides (environment only): `JUSTUP_TEST_SLOTS`, `JUSTUP_TEST_CPU_SHARE`, `JUSTUP_TEST_CORES_PER_RUN`, `JUSTUP_TEST_RAM_PER_WORKER_MB`, `JUSTUP_LAYOUT_WORKERS`; formula and full list in `AGENTS.md`. A CPU quota is applied only if `systemd-run --scope` works for your user; otherwise only worker caps are active and only one heavy run at a time is allowed (unless `JUSTUP_TEST_SLOTS` is set). CI does not use the wrapper.

## Build the documentation locally

```bash
.venv/bin/mkdocs serve -f docs/mkdocs.yml      # live preview
.venv/bin/mkdocs build -f docs/mkdocs.yml --strict
```

!!! note "strict build"
    `--strict` treats missing nav targets and broken links as errors. Always fix
    warnings by creating the page or correcting the link, never by
    lowering the `validation` settings.
