//
// tests/js/test-budget.test.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//
import assert from "node:assert/strict";
import test from "node:test";
import { computeBudget, gatherInputs } from "../../tools/test-budget.mjs";
import { nodeTestConcurrency } from "../../tools/run-node-tests.mjs";

const GB = 1024 * 1024;

// Injectable machine: files maps absolute paths to contents.
function machine({ cpus = 16, load = 0, memKb = 32 * GB, files = {} } = {}) {
	const all = { "/proc/meminfo": `MemTotal: ${memKb} kB\nMemAvailable:   ${memKb} kB\n`, "/proc/self/cgroup": "0::/\n", ...files };
	return { availableParallelism: () => cpus, loadavg: () => [load, 0, 0], readFile: (file) => all[file] ?? null };
}
const budget = (m, env = {}) => computeBudget(gatherInputs(m), env);

test("16 cores yield a half-machine budget split into runs of a quarter", () => {
	const b = budget(machine());
	assert.deepEqual([b.budgetCores, b.coresPerRun, b.slots, b.pytestWorkers, b.nodeConcurrency], [8, 4, 2, 4, 4]);
	assert.equal(b.playwrightWorkers, 4);
});

test("the local Node runner caps a fresh budget at the wrapper's limit", () => {
	const b = budget(machine());
	assert.equal(b.nodeConcurrency, 4);
	assert.equal(nodeTestConcurrency(b, { JUSTUP_LIMIT_NODE: "1" }), 1);
	assert.equal(nodeTestConcurrency(b, {}), 4);
	assert.throws(() => nodeTestConcurrency(b, { JUSTUP_LIMIT_NODE: "0" }), /positive integer/);
});

test("scales with core count", () => {
	assert.deepEqual(
		[8, 64].map((cpus) => { const b = budget(machine({ cpus })); return [b.budgetCores, b.coresPerRun, b.slots]; }),
		[[4, 2, 2], [32, 16, 2]],
	);
});

test("a single core never drops below one slot and one worker", () => {
	const b = budget(machine({ cpus: 1, load: 5 }));
	assert.deepEqual([b.slots, b.runCores, b.playwrightWorkers, b.pytestWorkers, b.nodeConcurrency], [1, 1, 1, 1, 1]);
});

test("small RAM limits Playwright workers but not below one", () => {
	assert.equal(budget(machine({ memKb: 3 * GB })).playwrightWorkers, 2);
	assert.equal(budget(machine({ memKb: Math.floor(0.2 * GB) })).playwrightWorkers, 1);
	assert.equal(budget(machine({ memKb: Math.floor(0.2 * GB) })).pytestWorkers, 4);
});

test("cgroup v2 cpu.max below the core count wins, also from a parent cgroup", () => {
	const own = machine({ files: { "/proc/self/cgroup": "0::/a/b\n", "/sys/fs/cgroup/a/cpu.max": "400000 100000\n", "/sys/fs/cgroup/a/b/cpu.max": "max 100000\n" } });
	const b = budget(own);
	assert.deepEqual([b.cores, b.budgetCores, b.coresPerRun, b.slots], [4, 2, 1, 2]);
});

test("cgroup v1 quota and cgroup memory limits are honoured", () => {
	const b = budget(machine({ files: {
		"/sys/fs/cgroup/cpu/cpu.cfs_quota_us": "200000", "/sys/fs/cgroup/cpu/cpu.cfs_period_us": "100000",
		"/sys/fs/cgroup/memory/memory.limit_in_bytes": String(1536 * 1048576), "/sys/fs/cgroup/memory/memory.usage_in_bytes": "0",
	} }));
	assert.equal(b.cores, 2);
	assert.equal(b.ramMb, 1536);
	assert.equal(b.playwrightWorkers, 1);
});

test("an unlimited cgroup (max / huge limit) changes nothing", () => {
	const b = budget(machine({ files: { "/sys/fs/cgroup/cpu.max": "max 100000", "/sys/fs/cgroup/memory.max": "max" } }));
	assert.equal(b.cores, 16);
});

test("high load shrinks the run, flags the machine busy and keeps one worker", () => {
	const loaded = budget(machine({ load: 14 }));
	assert.equal(loaded.runCores, 2);
	const saturated = budget(machine({ load: 60 }));
	assert.deepEqual([saturated.runCores, saturated.playwrightWorkers, saturated.busy, saturated.slots], [1, 1, true, 2]);
	assert.equal(budget(machine({ load: 60 }), { JUSTUP_TEST_LOAD_AWARE: "0" }).runCores, 4);
});

test("unknown RAM is ignored instead of blocking", () => {
	const m = machine(); m.readFile = () => null;
	assert.equal(budget(m).playwrightWorkers, 4);
});

test("overrides change the result and are validated", () => {
	assert.equal(budget(machine(), { JUSTUP_TEST_SLOTS: "1" }).slots, 1);
	assert.equal(budget(machine(), { JUSTUP_TEST_CPU_SHARE: "1" }).slots, 4);
	assert.equal(budget(machine(), { JUSTUP_TEST_CORES_PER_RUN: "99" }).coresPerRun, 8);
	assert.equal(budget(machine(), { JUSTUP_TEST_RAM_PER_WORKER_MB: "16384", JUSTUP_TEST_LOAD_AWARE: "0" }).playwrightWorkers, 1);
	for (const [name, value] of [["JUSTUP_TEST_SLOTS", "0"], ["JUSTUP_TEST_SLOTS", "1.5"], ["JUSTUP_TEST_CPU_SHARE", "2"], ["JUSTUP_TEST_CPU_SHARE", "abc"],
		["JUSTUP_TEST_RAM_PER_WORKER_MB", "-1"], ["JUSTUP_TEST_CORES_PER_RUN", "x"], ["JUSTUP_TEST_LOAD_AWARE", "yes"]]) {
		assert.throws(() => budget(machine(), { [name]: value }), new RegExp(`${name} must be`), `${name}=${value}`);
	}
});

test("GitHub-hosted runner profiles stay valid (the CI keeps its fixed commands)", () => {
	const small = budget(machine({ cpus: 2, memKb: 7 * GB }));
	assert.deepEqual([small.budgetCores, small.coresPerRun, small.slots, small.playwrightWorkers, small.pytestWorkers], [1, 1, 1, 1, 1]);
	const standard = budget(machine({ cpus: 4, memKb: 16 * GB }));
	assert.deepEqual([standard.budgetCores, standard.coresPerRun, standard.slots, standard.playwrightWorkers, standard.pytestWorkers], [2, 1, 2, 1, 1]);
	// Sharing the whole runner (CPU_SHARE=1) is what a single job per VM may ask for.
	assert.equal(budget(machine({ cpus: 4, memKb: 16 * GB }), { JUSTUP_TEST_CPU_SHARE: "1", JUSTUP_TEST_RUN_SHARE: "1" }).playwrightWorkers, 4);
});
