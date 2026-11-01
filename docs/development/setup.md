# Development Setup

This page describes how to set up justUp! locally for development.

## Prerequisites

- Python ≥ 3.13
- Node.js (for JS/CSS lint and `node:test`)
- The system tools `ping` and `mtr` (for checks and traceroute)

On Debian/Ubuntu:

```bash
sudo apt-get install iputils-ping mtr-tiny
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
| Python tests | `pytest` |
| JS/CSS lint | `npm run lint` |
| JS tests | `npm test` |
| Container check | `docker build --check -f docker/Dockerfile .` |

## Build the documentation locally

```bash
.venv/bin/mkdocs serve -f docs/mkdocs.yml      # live preview
.venv/bin/mkdocs build -f docs/mkdocs.yml --strict
```

!!! note "strict build"
    `--strict` treats missing nav targets and broken links as errors. Always fix
    warnings by creating the page or correcting the link, never by
    lowering the `validation` settings.
