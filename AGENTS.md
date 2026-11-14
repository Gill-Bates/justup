# AGENTS.md

Central rules for AI agents working in this repository. Subdirectory instructions (for example `.github/AGENTS.md`, `.github/workflows/AGENTS.md`) stay valid and add to these.

## Heavy local test runs

Run heavy local suites only through `tools/run-limited.sh`: Playwright (`npx playwright test`, `tests/layout/run.mjs`, `npm run test:layout`), complete `pytest` runs, complete Node runs (`npm test`) and anything comparably heavy.

```bash
tools/run-limited.sh pytest -n auto
tools/run-limited.sh node tests/layout/run.mjs --profile quick --browsers chromium
tools/run-limited.sh npm test
```

- Start only the tests and specs the change affects. Run full browser matrices after the changes are merged together, or for a justified final check.
- Run at most 2-3 implementation agents at the same time. Parallel work does not mean parallel full suites.
- Do not start a suite in a second session when a running one already gives the same integration evidence.
- Never end another agent's test run or its browser, Python or Node processes. No `pkill chrome`, `killall python` or similar.
- Do not bypass the wrapper with alternative invocations. This also applies to separate worktrees and to several sessions of the same user: all use the same lock directory outside the repository.

## How the wrapper sizes a run

Nothing is hard-coded. `tools/test-budget.mjs` is the only place that computes it, from the CPU affinity, the cgroup limit (`cpu.max` v2, `cpu.cfs_quota_us` v1), available RAM (`MemAvailable`, `memory.max`) and the 1-minute load average.

```text
cores        = min(affinity, cgroup CPU limit)
budgetCores  = max(1, floor(cores * CPU_SHARE))             # all heavy runs together, default 0.5
coresPerRun  = min(budgetCores, max(1, floor(cores * RUN_SHARE)))   # default 0.25
slots        = max(1, floor(budgetCores / coresPerRun))
runCores     = max(1, min(coresPerRun, floor(cores - load1)))       # shrinks on a busy machine
pytest -n / node --test-concurrency = runCores
Playwright workers = max(1, min(runCores, floor(availableRamMb * RAM_SHARE / RAM_PER_WORKER_MB)))
```

Every value is at least 1, so a run can always start. When the machine is already busy, the wrapper waits up to `JUSTUP_TEST_LOAD_WAIT` seconds after it got a slot and then starts with the reduced budget. The lock directory is `${TMPDIR:-/tmp}/justup-test-slots-<uid>` (mode 0700, owned by you, never a symlink; `XDG_RUNTIME_DIR` plays no part, only `JUSTUP_TEST_LOCK_DIR` overrides it). Checking and claiming a slot happen under a short mutex lock, over all existing slot files: a run starts only if fewer slots are held than the smaller of its own slot count and the counts recorded by all current holders, so sessions with different budgets keep one global limit (smallest value wins). Show the current numbers with `node tools/test-budget.mjs`.

Overrides, environment only and validated: `JUSTUP_TEST_SLOTS` (1 = one heavy run at a time), `JUSTUP_TEST_CPU_SHARE`, `JUSTUP_TEST_RUN_SHARE`, `JUSTUP_TEST_CORES_PER_RUN`, `JUSTUP_TEST_RAM_SHARE`, `JUSTUP_TEST_RAM_PER_WORKER_MB` (default 768, a rough estimate for browser plus stub app), `JUSTUP_TEST_LOAD_THRESHOLD`, `JUSTUP_TEST_LOAD_AWARE=0`, `JUSTUP_LAYOUT_WORKERS`, `JUSTUP_TEST_CORES` and `JUSTUP_TEST_RAM_MB` (pin the machine figures, used by the wrapper tests), plus `JUSTUP_TEST_WAIT`, `JUSTUP_TEST_LOAD_WAIT`, `JUSTUP_TEST_CGROUP=off` and `JUSTUP_TEST_LOCK_DIR`.

## Limits

- `nice`/`ionice` change priorities only. The hard CPU limit (`CPUQuota`) applies only when `systemd-run --scope` works for the user and the quota reads back from the new cgroup; otherwise the wrapper says so and only slots and worker caps are active. The wrapper never changes systemd or cgroup configuration.
- Without a verified CPU quota (no systemd, scope not creatable, quota not readable back) the wrapper prints a note and allows one heavy run at a time, unless `JUSTUP_TEST_SLOTS` is set explicitly. A failing `systemd-run` start is never retried as an unlimited second start.
- The slot is released only after the command's process group (and, with a verified scope, the scope) was cleaned up. A process that escapes with its own `setsid` is reachable only through the verified scope; without a scope it can survive.
- Nested calls of the wrapper skip locking, keep the inherited caps and start no extra workers.
- A `flock` wrapper cannot control processes that bypass it. No absolute limit for all host processes is claimed.
- GitHub Actions does not use the wrapper. `ci.yml` keeps `pytest -n auto` and 2 Playwright workers (`CI` set).

## Browser tooling and test selection

- Wrapper applies to every local command, including reproduction recipes copied from `docs/development/contributing.md` or `.github/workflows/AGENTS.md`: prefix `npx playwright test`, `node tests/layout/run.mjs` and `npm run test:layout` with `tools/run-limited.sh`. Only GitHub Actions runs them directly.
- Playwright for the layout suite is the repository install (`package.json`, `package-lock.json`; config `tests/layout/playwright.config.mjs`, always passed via `--config`). Do not run `npm install` or `playwright install` for it.
- `/opt/ux-test-tools` is a separate, host-level install for interactive UX checks (`playwright`, `@axe-core/playwright`, Chromium, WebKit, Firefox), created by the "Full browsers" block in `setup.conf`. It is not locked. Pinned in `setup.conf` (`--save-exact`); keep it at the repository's Playwright version. Do not use it for the committed layout suite and do not install tools into the repository.
- Check a selection before running it: `JUSTUP_LAYOUT_BROWSERS=chromium tools/run-limited.sh npx playwright test --config tests/layout/playwright.config.mjs --list --grep '@tag'`. Tags are listed in the `--grep` strings of `.github/workflows/ci.yml`; `node tests/layout/run.mjs --help` lists profiles (`quick`, `full`) and `--self-test`.
- Do not add `waitForTimeout` or raise timeouts to turn a run green. Replace a fixed wait with `expect.poll` or `waitForFunction` on a state signal when you touch a spec; keep a deliberate measurement window only with a one-line reason.
- Python: use `/opt/justup/.venv` (compiled `sslyze` included). Setup is documented in `setup.conf`.
