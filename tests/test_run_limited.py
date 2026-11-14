#!/usr/bin/env python3
#
# tests/test_run_limited.py
# Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
#

"""Contract of tools/run-limited.sh: shared slots, exit codes, signals and worker caps.

Only harmless sleep/sh jobs run here; no browser and no heavy suite is started.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

WRAPPER = Path(__file__).resolve().parents[1] / "tools" / "run-limited.sh"

pytestmark = pytest.mark.skipif(
    sys.platform != "linux" or shutil.which("flock") is None or shutil.which("setsid") is None,
    reason="run-limited.sh needs Linux flock and setsid",
)

# A job that announces itself, then waits for its own release file.
JOB = 'touch "$1/started-$2"; echo $$ > "$1/pid-$2"; while [ ! -e "$1/release-$2" ]; do sleep 0.1; done'


def _env(tmp_path: Path, **extra: str | None) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("JUSTUP_") and k != "PYTEST_ADDOPTS"}
    env.update(
        {
            "JUSTUP_TEST_LOCK_DIR": str(tmp_path / "locks"),
            "JUSTUP_TEST_CGROUP": "off",
            "JUSTUP_TEST_SLOTS": "2",
            "JUSTUP_TEST_CPU_SHARE": "1",
            "JUSTUP_TEST_CORES": "16",
            "JUSTUP_TEST_RAM_MB": "65536",
            "JUSTUP_TEST_CORES_PER_RUN": "4",
            "JUSTUP_TEST_LOAD_AWARE": "0",
            "JUSTUP_TEST_RAM_PER_WORKER_MB": "1",
            "JUSTUP_TEST_LOAD_WAIT": "0",
        }
    )
    for key, value in extra.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    return env


def _default_sigint() -> None:
    signal.signal(signal.SIGINT, signal.SIG_DFL)


def _start(tmp_path: Path, name: str, *, cwd: Path | None = None, **extra: str) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [str(WRAPPER), "sh", "-c", JOB, "job", str(tmp_path), name],
        env=_env(tmp_path, **extra),
        cwd=cwd,
        preexec_fn=_default_sigint,  # noqa: PLW1509 # a suite run from a background job would hand down SIG_IGN
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _wait_for(path: Path, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return True
        time.sleep(0.05)
    return False


def _release(tmp_path: Path, name: str) -> None:
    (tmp_path / f"release-{name}").touch()


def _run(tmp_path: Path, *args: str, **extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(WRAPPER), *args], env=_env(tmp_path, **extra), capture_output=True, text=True, timeout=30, check=False
    )


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


@pytest.fixture(autouse=True)
def _reap(tmp_path: Path):
    """Release every job even when an assertion fails, so no sleep loop outlives the test."""
    procs: list[subprocess.Popen[str]] = []
    original = subprocess.Popen.__init__

    def tracking(self, *a, **k):
        original(self, *a, **k)
        procs.append(self)

    subprocess.Popen.__init__ = tracking  # type: ignore[method-assign]
    try:
        yield
    finally:
        subprocess.Popen.__init__ = original  # type: ignore[method-assign]
        for name in "abcdef":
            (tmp_path / f"release-{name}").touch()
        for proc in procs:
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            for stream in (proc.stdout, proc.stderr):
                if stream:
                    stream.close()


def _started(tmp_path: Path) -> set[str]:
    return {p.name.removeprefix("started-") for p in tmp_path.glob("started-*")}


def _wait_started(tmp_path: Path, count: int, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if len(_started(tmp_path)) >= count:
            return True
        time.sleep(0.05)
    return False


def test_two_slots_third_waits_and_starts_after_release(tmp_path: Path) -> None:
    procs = {n: _start(tmp_path, n) for n in "abc"}
    assert _wait_started(tmp_path, 2)
    time.sleep(1.5)
    running = _started(tmp_path)
    assert len(running) == 2, "third job must wait while both slots are busy"
    waiting = ({"a", "b", "c"} - running).pop()
    first = min(running)
    _release(tmp_path, first)
    assert procs[first].wait(timeout=10) == 0
    assert _wait_for(tmp_path / f"started-{waiting}"), "third job must start once a slot is free"
    for name in "abc":
        _release(tmp_path, name)
    assert [p.wait(timeout=10) for p in procs.values()] == [0, 0, 0]


def test_slot_count_is_configurable_and_single_slot_serialises(tmp_path: Path) -> None:
    first = _start(tmp_path, "a", JUSTUP_TEST_SLOTS="1")
    assert _wait_for(tmp_path / "started-a")
    second = _start(tmp_path, "b", JUSTUP_TEST_SLOTS="1")
    time.sleep(1.5)
    assert not (tmp_path / "started-b").exists()
    _release(tmp_path, "a")
    assert first.wait(timeout=10) == 0
    assert _wait_for(tmp_path / "started-b")
    _release(tmp_path, "b")
    assert second.wait(timeout=10) == 0


def test_worktrees_and_sessions_with_or_without_xdg_share_default_lock_dir(tmp_path: Path) -> None:
    tmpdir = tmp_path / "tmp"
    tmpdir.mkdir()
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    procs = []
    for name, xdg in zip("abc", (None, str(runtime), None), strict=True):
        tree = tmp_path / f"tree-{name}"
        tree.mkdir()
        env = _env(tmp_path, JUSTUP_TEST_LOCK_DIR=None, TMPDIR=str(tmpdir), XDG_RUNTIME_DIR=xdg)
        procs.append(
            subprocess.Popen(
                [str(WRAPPER), "sh", "-c", JOB, "job", str(tmp_path), name],
                env=env,
                cwd=tree,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        )
    assert _wait_started(tmp_path, 2)
    time.sleep(1.5)
    assert len(_started(tmp_path)) == 2
    assert not list(runtime.iterdir())
    assert len(list(tmpdir.glob("justup-test-slots-*/slot-*.lock"))) >= 2
    for name in "abc":
        _release(tmp_path, name)
    assert [p.wait(timeout=10) for p in procs] == [0, 0, 0]


def test_exit_code_is_passed_through(tmp_path: Path) -> None:
    assert _run(tmp_path, "sh", "-c", "exit 7").returncode == 7
    assert _run(tmp_path, "sh", "-c", "echo out; echo err >&2").stdout == "out\n"
    assert "err\n" in _run(tmp_path, "sh", "-c", "echo err >&2").stderr


def test_no_orphan_lock_after_normal_and_failing_runs(tmp_path: Path) -> None:
    for _ in range(3):
        _run(tmp_path, "sh", "-c", "exit 3")
    result = _run(tmp_path, "true", JUSTUP_TEST_SLOTS="1", JUSTUP_TEST_WAIT="0")
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(("sig", "code"), [(signal.SIGINT, 130), (signal.SIGTERM, 143)])
def test_signal_stops_command_tree_and_frees_slot(tmp_path: Path, sig: signal.Signals, code: int) -> None:
    proc = _start(tmp_path, "a", JUSTUP_TEST_SLOTS="1")
    assert _wait_for(tmp_path / "pid-a")
    time.sleep(0.3)
    child = int((tmp_path / "pid-a").read_text())
    proc.send_signal(sig)
    assert proc.wait(timeout=15) == code
    assert not _pid_alive(child), "command must not outlive an interrupted wrapper"
    again = _run(tmp_path, "true", JUSTUP_TEST_SLOTS="1", JUSTUP_TEST_WAIT="0")
    assert again.returncode == 0, again.stderr


def test_interrupt_while_waiting_keeps_holder_running(tmp_path: Path) -> None:
    holder = _start(tmp_path, "a", JUSTUP_TEST_SLOTS="1")
    assert _wait_for(tmp_path / "started-a")
    waiter = _start(tmp_path, "b", JUSTUP_TEST_SLOTS="1")
    time.sleep(1.5)
    waiter.send_signal(signal.SIGTERM)
    assert waiter.wait(timeout=10) == 143
    assert not (tmp_path / "started-b").exists()
    assert holder.poll() is None
    busy = _run(tmp_path, "true", JUSTUP_TEST_SLOTS="1", JUSTUP_TEST_WAIT="0")
    assert busy.returncode == 124 and "no free test slot" in busy.stderr
    _release(tmp_path, "a")
    assert holder.wait(timeout=10) == 0


def test_holders_in_higher_slots_count_against_a_smaller_limit(tmp_path: Path) -> None:
    holders = [_start(tmp_path, n, JUSTUP_TEST_SLOTS="3") for n in "ab"]
    assert _wait_started(tmp_path, 2)
    newcomer = _run(tmp_path, "true", JUSTUP_TEST_SLOTS="1", JUSTUP_TEST_WAIT="1")
    assert newcomer.returncode == 124
    same = _run(tmp_path, "true", JUSTUP_TEST_SLOTS="3", JUSTUP_TEST_WAIT="0")
    assert same.returncode == 0, "a third slot of a 3-slot budget is still free"
    for name in "ab":
        _release(tmp_path, name)
    assert [h.wait(timeout=10) for h in holders] == [0, 0]


@pytest.mark.parametrize("how", ["sigint", "sigterm", "kill-leader"])
def test_whole_process_tree_is_gone_before_the_slot_is_free(tmp_path: Path, how: str) -> None:
    proc = subprocess.Popen(
        [
            str(WRAPPER),
            "sh",
            "-c",
            'sh -c \'sleep 300 & echo $! > "$0/gc"; echo $$ > "$0/leader"; wait\' "$1"',
            "x",
            str(tmp_path),
        ],
        env=_env(tmp_path, JUSTUP_TEST_SLOTS="1"),
        preexec_fn=_default_sigint,  # noqa: PLW1509
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    assert _wait_for(tmp_path / "gc") and _wait_for(tmp_path / "leader")
    time.sleep(0.3)
    grandchild, leader = int((tmp_path / "gc").read_text()), int((tmp_path / "leader").read_text())
    if how == "kill-leader":
        os.kill(leader, signal.SIGKILL)
    else:
        proc.send_signal(signal.SIGINT if how == "sigint" else signal.SIGTERM)
    proc.wait(timeout=20)
    assert not _pid_alive(grandchild), "descendants must be gone when the wrapper (and its slot) ends"
    again = _run(tmp_path, "true", JUSTUP_TEST_SLOTS="1", JUSTUP_TEST_WAIT="0")
    assert again.returncode == 0, again.stderr


def test_wait_timeout_reports_and_exits_124(tmp_path: Path) -> None:
    holder = _start(tmp_path, "a", JUSTUP_TEST_SLOTS="1")
    assert _wait_for(tmp_path / "started-a")
    result = _run(tmp_path, "true", JUSTUP_TEST_SLOTS="1", JUSTUP_TEST_WAIT="2")
    assert result.returncode == 124
    assert "all 1 test slots are busy" in result.stderr
    _release(tmp_path, "a")
    assert holder.wait(timeout=10) == 0


def test_missing_optional_programs_do_not_block(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for tool in (
        "bash",
        "env",
        "flock",
        "setsid",
        "stat",
        "mkdir",
        "chmod",
        "sleep",
        "kill",
        "id",
        "sh",
        "true",
        "node",
        "cat",
        "dirname",
    ):
        found = shutil.which(tool)
        assert found, tool
        (bin_dir / tool).symlink_to(found)
    result = _run(tmp_path, "sh", "-c", "echo ran", PATH=str(bin_dir), JUSTUP_TEST_CGROUP="auto")
    assert result.returncode == 0, result.stderr
    assert result.stdout == "ran\n"
    assert "nice not available" in result.stderr
    assert "systemd-run --scope unavailable" in result.stderr


def test_arguments_are_not_interpreted_by_a_shell(tmp_path: Path) -> None:
    marker = tmp_path / "pwned"
    nasty = [f"$(touch {marker})", f"`touch {marker}`", "; touch x", "a b", "*", "'quoted'", ""]
    result = _run(tmp_path, "printf", "[%s]", *nasty)
    assert result.returncode == 0
    assert result.stdout == "".join(f"[{item}]" for item in nasty)
    assert not marker.exists()


def test_invalid_settings_are_rejected(tmp_path: Path) -> None:
    bad = _run(tmp_path, "true", JUSTUP_TEST_SLOTS="0")
    assert bad.returncode == 125 and "JUSTUP_TEST_SLOTS must be" in bad.stderr
    assert _run(tmp_path, "true", JUSTUP_TEST_WAIT="soon").returncode == 125
    assert _run(tmp_path).returncode == 1


def test_lock_dir_must_be_private_and_not_a_symlink(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    assert _run(tmp_path, "true", JUSTUP_TEST_LOCK_DIR=str(link)).returncode == 125
    loose = tmp_path / "loose"
    loose.mkdir(mode=0o777)
    assert _run(tmp_path, "true", JUSTUP_TEST_LOCK_DIR=str(loose)).returncode == 0
    assert (loose.stat().st_mode & 0o777) == 0o700


def _adjusted(tmp_path: Path, *args: str, **extra: str) -> list[str]:
    script = f'source "{WRAPPER}"; validate_settings; load_budget; adjust_command "$@"; printf "%s\\n" "${{CMD[@]}}"'
    result = subprocess.run(
        ["bash", "-c", script, "bash", *args], env=_env(tmp_path, **extra), capture_output=True, text=True, check=True
    )
    return result.stdout.splitlines()


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (["pytest", "-n", "8", "tests"], ["pytest", "-n", "4", "tests"]),
        (["pytest", "-n8"], ["pytest", "-n4"]),
        (["python", "-m", "pytest", "--numprocesses=16"], ["python", "-m", "pytest", "--numprocesses=4"]),
        (["pytest", "--numprocesses", "16"], ["pytest", "--numprocesses", "4"]),
        (["pytest", "-n", "2"], ["pytest", "-n", "2"]),
        (["pytest", "-n", "0"], ["pytest", "-n", "0"]),
        (["pytest", "-n", "auto"], ["pytest", "-n", "4"]),
        (["pytest", "-n", "logical"], ["pytest", "-n", "4"]),
        (["pytest", "-nauto"], ["pytest", "-n4"]),
        (["pytest", "-nlogical"], ["pytest", "-n4"]),
        (["pytest", "-q", "tests/x.py"], ["pytest", "-q", "tests/x.py"]),
        (["npm", "test"], ["npm", "run", "test:local"]),
        (["node", "--test", "a.mjs"], ["node", "--test", "--test-concurrency=4", "a.mjs"]),
        (["node", "--test", "--test-concurrency=12"], ["node", "--test", "--test-concurrency=4"]),
        (["node", "--test", "--test-concurrency", "20", "a.mjs"], ["node", "--test", "--test-concurrency=4", "a.mjs"]),
        (["node", "--test-concurrency", "2", "--test"], ["node", "--test-concurrency=2", "--test"]),
        (["npx", "playwright", "test", "--workers=8"], ["npx", "playwright", "test", "--workers=4"]),
        (["npx", "playwright", "test", "--workers", "50%"], ["npx", "playwright", "test", "--workers", "4"]),
        (["npx", "playwright", "test", "-j", "9"], ["npx", "playwright", "test", "-j", "4"]),
        (["npx", "playwright", "test", "-j9"], ["npx", "playwright", "test", "-j4"]),
        (["echo", "-n", "99"], ["echo", "-n", "99"]),
    ],
)
def test_worker_caps_are_clamped_without_touching_other_commands(
    tmp_path: Path, args: list[str], expected: list[str]
) -> None:
    assert _adjusted(tmp_path, *args) == expected


def test_wrapper_exports_worker_limits_to_the_command(tmp_path: Path) -> None:
    out = _run(tmp_path, "sh", "-c", 'echo "$PYTEST_XDIST_AUTO_NUM_WORKERS $JUSTUP_LAYOUT_WORKERS"').stdout
    assert out == "4 4\n"
    echo = 'echo "$JUSTUP_LAYOUT_WORKERS"'
    assert _run(tmp_path, "sh", "-c", echo, JUSTUP_LAYOUT_WORKERS="2").stdout == "2\n"
    assert _run(tmp_path, "sh", "-c", echo, JUSTUP_LAYOUT_WORKERS="20").stdout == "4\n", "an override may only lower"


def test_playwright_config_without_workers_flag_respects_the_budget(tmp_path: Path) -> None:
    # The pytest CI job has no node_modules, so the real config is copied next to a minimal
    # @playwright/test stand-in; only defineConfig is needed to read the resolved worker count.
    repo = Path(__file__).resolve().parent.parent
    (tmp_path / "tests" / "layout").mkdir(parents=True)
    (tmp_path / "tools").mkdir()
    shutil.copy(repo / "tests" / "layout" / "playwright.config.mjs", tmp_path / "tests" / "layout")
    shutil.copy(repo / "tools" / "test-budget.mjs", tmp_path / "tools")
    stub = tmp_path / "node_modules" / "@playwright" / "test"
    stub.mkdir(parents=True)
    (stub / "package.json").write_text('{"name":"@playwright/test","type":"module","exports":"./index.mjs"}')
    (stub / "index.mjs").write_text("export const defineConfig = (config) => config;\n")
    config = tmp_path / "tests" / "layout" / "playwright.config.mjs"
    probe = f"import({str(config)!r}).then((m) => console.log(m.default.workers))"
    out = _run(tmp_path, "node", "-e", probe, JUSTUP_TEST_CORES_PER_RUN="2", CI=None).stdout.strip()
    assert out == "2"


def test_npm_test_is_started_as_the_budgeted_local_script(tmp_path: Path) -> None:
    fake = tmp_path / "fakebin"
    fake.mkdir()
    (fake / "npm").write_text('#!/bin/sh\necho "npm $*"\n')
    (fake / "npm").chmod(0o755)
    result = _run(tmp_path, "npm", "test", PATH=f"{fake}:{os.environ['PATH']}")
    assert result.returncode == 0 and result.stdout == "npm run test:local\n"
    package = (Path(__file__).resolve().parents[1] / "package.json").read_text()
    assert '"test:local": "node tools/run-node-tests.mjs"' in package


def test_npm_runner_keeps_the_wrappers_smaller_node_budget(tmp_path: Path) -> None:
    fake = tmp_path / "fakebin"
    fake.mkdir()
    module = (WRAPPER.parents[1] / "tools" / "run-node-tests.mjs").as_uri()
    (fake / "npm").write_text(
        "#!/bin/sh\n"
        "node --input-type=module -e '"
        f'import {{ nodeTestConcurrency }} from "{module}"; '
        "console.log(nodeTestConcurrency({nodeConcurrency: 4}, process.env))'\n"
    )
    (fake / "npm").chmod(0o755)
    result = _run(
        tmp_path,
        "npm",
        "test",
        PATH=f"{fake}:{os.environ['PATH']}",
        JUSTUP_TEST_CORES_PER_RUN="1",
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "1\n"


def test_without_verified_cpu_quota_only_one_run_is_allowed(tmp_path: Path) -> None:
    holder = _start(tmp_path, "a", JUSTUP_TEST_SLOTS=None)
    assert _wait_for(tmp_path / "started-a")
    result = _run(tmp_path, "true", JUSTUP_TEST_SLOTS=None, JUSTUP_TEST_WAIT="0")
    assert result.returncode == 124
    assert "allowing one heavy run at a time" in result.stderr
    _release(tmp_path, "a")
    assert holder.wait(timeout=10) == 0


def test_smaller_slot_count_of_a_holder_wins(tmp_path: Path) -> None:
    holder = _start(tmp_path, "a", JUSTUP_TEST_SLOTS="1")
    assert _wait_for(tmp_path / "started-a")
    result = _run(tmp_path, "true", JUSTUP_TEST_SLOTS="2", JUSTUP_TEST_WAIT="1")
    assert result.returncode == 124, "a session computing more slots must not exceed the holder's limit"
    _release(tmp_path, "a")
    assert holder.wait(timeout=10) == 0
    assert _run(tmp_path, "true", JUSTUP_TEST_SLOTS="2", JUSTUP_TEST_WAIT="0").returncode == 0


def test_busy_machine_is_waited_for_then_run_continues(tmp_path: Path) -> None:
    started = time.monotonic()
    result = _run(
        tmp_path, "true", JUSTUP_TEST_LOAD_AWARE="1", JUSTUP_TEST_LOAD_THRESHOLD="0.1", JUSTUP_TEST_LOAD_WAIT="2"
    )
    assert result.returncode == 0, result.stderr
    if "machine is busy" in result.stderr:
        assert time.monotonic() - started >= 2


def test_nested_wrapper_call_skips_locking_but_keeps_caps(tmp_path: Path) -> None:
    echo = 'echo "$JUSTUP_LAYOUT_WORKERS"'
    nested = {"JUSTUP_RUN_LIMITED": "1", "JUSTUP_LIMIT_PW": "2", "JUSTUP_TEST_SLOTS": "1", "JUSTUP_TEST_WAIT": "0"}
    holder = _start(tmp_path, "a", JUSTUP_TEST_SLOTS="1")
    assert _wait_for(tmp_path / "started-a")
    result = _run(tmp_path, str(WRAPPER), "sh", "-c", echo, JUSTUP_LAYOUT_WORKERS="9", **nested)
    assert result.returncode == 0 and result.stdout == "2\n", "no deadlock on a busy slot, cap still applied"
    args = _run(tmp_path, str(WRAPPER), "pytest", "--version", "-n", "50", **nested)
    assert "-n" not in args.stdout
    _release(tmp_path, "a")
    assert holder.wait(timeout=10) == 0
