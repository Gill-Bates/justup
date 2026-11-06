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

On a shared machine run the heavy ones (`pytest -n auto`, `npm test`, `node tests/layout/run.mjs ...`) as `tools/run-limited.sh <command>`; it limits concurrent suites and sizes workers from the machine. Locally Playwright takes its worker count from the same budget (`JUSTUP_LAYOUT_WORKERS` overrides it, CI uses 2). See [Setup](setup.md).

## Conventions

- Ruff for lint and format (`line-length = 120`, Python 3.13).
- Routes contain no SQL; they call repositories in `app/db/`.
- Prefer module-level imports; use function-local imports only to avoid circular imports.
- Keep modules and functions small and focused; some large modules exist.
- Never swallow exceptions silently.

## Layout contract
The page shell lives in `app/static/css/gentelella-layout.css`; page grids live in `ui.css`. New pages inherit the contract and need no per-page width or height fixes.
- Shell: every page extending `base.html` fills the visible viewport. The top bar and footer are non-shrinking flex rows; `<main>` takes the remaining height and owns vertical document-content scrolling. The footer stays visible in normal flex flow without covering content. No page opts into a fit mode. The root never scrolls horizontally or vertically, and `html`/`body` must not use `overflow-x: hidden` to hide width errors.
- Grids: `.ui-page` is the `ju-page` inline-size container. Column switches use `@container ju-page` with two thresholds, `40rem` (two columns) and `60rem` (wide), so they follow the content width and the sidebar state, not the viewport. Do not add viewport media queries for page grids or fixed pixel sizes per resolution.
- Allowed local overflow: tabs, data tables in `.ju-table-viewport`, `.table-responsive`, `pre`, and map containers may scroll or clip on their own. The list is `CFG` in `tests/layout/harness.mjs`; adding an entry needs a reason.
- Check: `node tests/layout/run.mjs --profile full --browsers chromium,webkit,firefox` starts the app on a free port with a throw-away data directory. Playwright Test reports each page/viewport as an independent test and audits the top and bottom of `<main>` across viewport, zoom, sidebar and theme states; axe runs on each page at the profile's first viewport. The quick profile covers 320x640, 390x844 and 1280x720@200%; the full profile adds the broader device and desktop matrix. Use `--matrix` for the page/viewport cases and audit self-test, `--gridstack` for dashboard interaction and persistence, `--self-test` for injected-defect checks, `--page` or `--pages` and `--viewports` to narrow a run. Traces and screenshots are retained on failure; set `JUSTUP_LAYOUT_OUTPUT` to place them in a chosen directory. `npm run test:layout -- --profile quick --browsers chromium,webkit` runs the quick profile locally; browsers must be installed with `npx playwright install chromium webkit`. The static CSS contract remains in `pytest -q tests/test_ui_layout_contract.py`. The suite starts the app through `tests/layout/app_stub.py`, which fixes traceroute offline, so the map and hop table render without network access. CI runs the quick page/viewport matrix and audit self-test in Chromium and WebKit, plus GridStack in strict Chromium mode. Playwright WebKit is not iOS Safari; check iOS on a real device.
- SMTP settings browser cases run independently in Chromium: `JUSTUP_PYTHON=.venv/bin/python JUSTUP_LAYOUT_BROWSERS=chromium npx playwright test --config tests/layout/playwright.config.mjs --grep @smtp-ui`. CI runs the same tagged suite in strict Chromium mode.
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
