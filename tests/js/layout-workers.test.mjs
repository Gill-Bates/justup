//
// tests/js/layout-workers.test.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { currentBudget } from "../../tools/test-budget.mjs";

const config = fileURLToPath(new URL("../layout/playwright.config.mjs", import.meta.url));
const probe = `import(${JSON.stringify(config)}).then((m) => console.log(JSON.stringify(m.default.workers)))`;

function workers(env) {
	const clean = { ...process.env };
	delete clean.CI;
	delete clean.JUSTUP_LAYOUT_WORKERS;
	for (const name of Object.keys(clean)) if (name.startsWith("JUSTUP_TEST_")) delete clean[name];
	return spawnSync(process.execPath, ["--input-type=module", "-e", probe], { env: { ...clean, ...env }, encoding: "utf8" });
}

test("CI keeps two Playwright workers, local runs use the machine budget", () => {
	assert.equal(workers({ CI: "true" }).stdout.trim(), "2");
	const env = { JUSTUP_TEST_LOAD_AWARE: "0" };
	assert.equal(workers(env).stdout.trim(), String(currentBudget(env).playwrightWorkers));
	assert.equal(workers({ ...env, JUSTUP_TEST_CORES_PER_RUN: "1" }).stdout.trim(), "1");
});

test("JUSTUP_LAYOUT_WORKERS overrides the default with a positive integer", () => {
	assert.equal(workers({ JUSTUP_LAYOUT_WORKERS: "5" }).stdout.trim(), "5");
	assert.equal(workers({ CI: "true", JUSTUP_LAYOUT_WORKERS: "1" }).stdout.trim(), "1");
});

test("invalid JUSTUP_LAYOUT_WORKERS values are rejected with a clear message", () => {
	for (const value of ["0", "-2", "1.5", "abc", "50%", "3 "]) {
		const result = workers({ JUSTUP_LAYOUT_WORKERS: value });
		assert.notEqual(result.status, 0, `value ${JSON.stringify(value)} must fail`);
		assert.match(result.stderr, /JUSTUP_LAYOUT_WORKERS must be a positive integer/);
	}
});
