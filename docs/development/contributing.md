# Contributing

## Before a pull request

Run the gates locally (see [Setup](setup.md)):

```bash
ruff check .
ruff format --check .
pytest
npm run lint
npm test
docker build --check -f docker/Dockerfile .
```

## Conventions

- Ruff for lint and format (`line-length = 120`, Python 3.13).
- Routes contain no SQL; they call repositories in `app/db/`.
- Prefer module-level imports; use function-local imports only to avoid circular imports.
- Keep modules and functions small and focused; some large modules exist.
- Never swallow exceptions silently.

## Layout contract
The page shell lives in `app/static/css/gentelella-layout.css`; page grids live in `ui.css`. New pages inherit the contract and need no per-page width or height fixes.
- Shell: `.ju-shell` is topbar, content and footer. The footer stays in normal flow. The root never scrolls horizontally, and `html`/`body` must not use `overflow-x: hidden`.
- Fit pages: a page that should fill the space between topbar and footer sets `ju-shell--fit` (always) or `ju-shell--fit-lg` (from 992px) in the `shell_class` block of its template. Both only set `--ju-fit-height` and `--ju-fit-overflow`; overflowing content then scrolls inside `<main>`, not past the footer. Pages without a fit class flow and scroll with the document.
- Grids: `.ui-page` is the `ju-page` inline-size container. Column switches use `@container ju-page` with two thresholds, `40rem` (two columns) and `60rem` (wide), so they follow the content width and the sidebar state, not the viewport. Do not add viewport media queries for page grids or fixed pixel sizes per resolution.
- Allowed local overflow: tabs, tables in `.table-responsive`, `pre`, and map containers may scroll or clip on their own. The list is `CFG` in `tests/layout/run.mjs`; adding an entry needs a reason.
- Check: `node tests/layout/run.mjs --profile full --browsers chromium,webkit,firefox` starts the app on a free port with a throw-away data directory. It audits every page in the viewport, zoom, sidebar and theme matrix, captures browser console errors, and runs axe on each sample at the profile's first viewport. The full profile includes 1440x900 and 1536x864 (exit 77 = Playwright, axe, or browser missing). Set `PLAYWRIGHT_DIR` if Playwright or `@axe-core/playwright` is not resolvable from the repository. `pytest -q tests/test_ui_layout_contract.py` runs the quick profile (`JUSTUP_LAYOUT_PROFILE`, `JUSTUP_LAYOUT_BROWSERS`) and skips when unavailable; `JUSTUP_LAYOUT_REQUIRE_BROWSER=1` turns that skip into a failure. The runner starts the app through `tests/layout/app_stub.py`, which fixes the traceroute offline, so the map and hop table render without network access. Strict run: `JUSTUP_LAYOUT_REQUIRE_BROWSER=1 JUSTUP_LAYOUT_BROWSERS=chromium,webkit,firefox PLAYWRIGHT_DIR=/opt/ux-test-tools .venv/bin/python -m pytest -q tests/test_ui_layout_contract.py`. Playwright WebKit is not iOS Safari; check iOS on a real device. The check is local only, not part of CI.
## Versioning and release

- The version lives only in `pyproject.toml`.
- Direct dependencies are in `pyproject.toml`. Docker and CI install the hash lock
  `docker/requirements.lock` (wheels only). Update it with
  `python tools/relock-runtime.py --write`.
- Release: set the version, update `CHANGELOG.md`, push the tag `vX.Y.Z`.

justUp! is released under the MIT license; contributions are accepted under it.
