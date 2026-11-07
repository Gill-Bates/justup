# Development Setup

## Prerequisites

- Python ≥ 3.13
- Node.js (for JS/CSS lint and `node:test`)
- The system tools `ping` (for checks) and `nmap` (for traceroute and port scans)

On Debian/Ubuntu:

```bash
sudo apt-get install iputils-ping nmap
```

## Python environment

```bash
python -m venv .venv
source .venv/bin/activate
pip install --require-hashes --only-binary=:all: \
  --find-links docker/vendor/wheels -r docker/requirements.lock
python tools/pyproject-deps.py dev > /tmp/justup-dev-requirements.txt
pip install --only-binary=:all: -r /tmp/justup-dev-requirements.txt
python tools/pyproject-deps.py docs > /tmp/justup-docs-requirements.txt
pip install --only-binary=:all: -r /tmp/justup-docs-requirements.txt
```

The direct runtime dependencies are in `pyproject.toml`; Docker and CI
install the complete, hash-checked wheel state from
`docker/requirements.lock`. `sslyze==6.3.1+justup.1` does not exist on PyPI: the
own, provenance-documented wheel is obtained via `--find-links docker/vendor/wheels`.
Dev tools are pinned in the `dev` extra; MkDocs tools are in the
`docs` extra. `tools/pyproject-deps.py` prints the respective direct dependencies.

## Frontend tools

```bash
npm install
```

For one-off interactive UX checks, the "Full browsers" block in `setup.conf`
installs Playwright and axe-core's Playwright integration (versions pinned in that block) under
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

`npm run test:local` runs the Node tests with the budgeted concurrency. Overrides (environment only): `JUSTUP_TEST_SLOTS`, `JUSTUP_TEST_CPU_SHARE`, `JUSTUP_TEST_CORES_PER_RUN`, `JUSTUP_TEST_RAM_PER_WORKER_MB`, `JUSTUP_LAYOUT_WORKERS`; formula and full list in `AGENTS.md`. A CPU quota is applied only if `systemd-run --scope` works for your user; otherwise only worker caps are active and only one heavy run at a time is allowed (unless `JUSTUP_TEST_SLOTS` is set). CI does not use the wrapper.

## Build the documentation locally

```bash
.venv/bin/mkdocs serve -f docs/mkdocs.yml      # live preview
.venv/bin/mkdocs build -f docs/mkdocs.yml --strict
```

!!! note "strict build"
    `--strict` treats missing nav targets and broken links as errors. Always fix
    warnings by creating the page or correcting the link, never by
    lowering the `validation` settings.
