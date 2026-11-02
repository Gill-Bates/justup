# Contributing

## Before a pull request

Run the gates locally (see [Setup](setup.md)):

```bash
ruff check .
ruff format --check .
pytest -n auto   # parallel; plain `pytest` runs serially
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
- Allowed local overflow: tabs, data tables in `.ju-table-viewport`, `.table-responsive`, `pre`, and map containers may scroll or clip on their own. The list is `CFG` in `tests/layout/run.mjs`; adding an entry needs a reason.
- Check: `node tests/layout/run.mjs --profile full --browsers chromium,webkit,firefox` starts the app on a free port with a throw-away data directory. It audits every page in the viewport, zoom, sidebar and theme matrix, captures browser console errors, and runs axe on each sample at the profile's first viewport. The full profile includes 1440x900 and 1536x864 (exit 77 = Playwright, axe, or browser missing). Set `PLAYWRIGHT_DIR` if Playwright or `@axe-core/playwright` is not resolvable from the repository. `pytest -q tests/test_ui_layout_contract.py` runs the quick profile (`JUSTUP_LAYOUT_PROFILE`, `JUSTUP_LAYOUT_BROWSERS`) and skips when unavailable; `JUSTUP_LAYOUT_REQUIRE_BROWSER=1` turns that skip into a failure. The runner starts the app through `tests/layout/app_stub.py`, which fixes the traceroute offline, so the map and hop table render without network access. Strict local run: `JUSTUP_LAYOUT_REQUIRE_BROWSER=1 JUSTUP_LAYOUT_BROWSERS=chromium,webkit,firefox PLAYWRIGHT_DIR=/opt/ux-test-tools .venv/bin/python -m pytest -q tests/test_ui_layout_contract.py`. CI runs the quick profile plus modal and SMTP regressions in strict Chromium mode. Playwright WebKit is not iOS Safari; check iOS on a real device.
## Table contract
Every data table follows one contract in `ui.css` ("Responsive data tables"); a table needs no page-specific width, ellipsis or column-hiding rules.
- Markup: wrap the table in `<div class="ju-table-viewport" role="region" tabindex="0" aria-label="...">` and give it `ju-data-table`. Add `rtable` and `data-label` on every `td` when the table may become cards. Helpers: `ju-cell-text` (free text, wraps anywhere, minimum width), `ju-cell-icon` (icon-only column), `actions-cell`.
- Table mode: content-sized columns, headers never wrap or clip, long values wrap. If the columns do not fit, the viewport scrolls horizontally; it is a focusable region, so keyboard and touch can reach it.
- Card mode: an `rtable` switches to label/value cards when its viewport is narrower than `40rem` (`@container ju-table`). The decision follows the available width, so an open sidebar, a dashboard widget or browser zoom are handled alike. No column is hidden; `ellipsis` and `table-layout: fixed` are not used.
- Bespoke card layouts (Targets, Users) are allowed only when the data needs them and live inside the same container query.
- Check: `tests/layout/audit.js` fails on `table-ellipsis`, `table-cell-clipped`, `table-cell-hidden`, `table-scroll-not-operable`, `table-viewport-unlabelled` and `table-mode-mismatch`; the runner seeds long names, many TCP ports and long e-mail addresses.
## Versioning and release

- The version lives only in `pyproject.toml`.
- Direct dependencies are in `pyproject.toml`. Docker and CI install the hash lock
  `docker/requirements.lock` (wheels only). Update it with
  `python tools/relock-runtime.py --write`.
- Release: set the version, update `CHANGELOG.md`, push the tag `vX.Y.Z`.

justUp! is released under the MIT license; contributions are accepted under it.
