//
// tools/run-node-tests.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//
// Local `npm run test:local`: node --test with a concurrency taken from the machine budget.
// CI keeps plain `npm test`.
import { spawnSync } from "node:child_process";
import { readdirSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { currentBudget } from "./test-budget.mjs";

const repo = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const dir = path.join(repo, "tests/js");

export function nodeTestConcurrency(budget, env = process.env) {
	const raw = env.JUSTUP_LIMIT_NODE;
	if (raw === undefined) return budget.nodeConcurrency;
	if (!/^[1-9][0-9]*$/.test(raw) || !Number.isSafeInteger(Number(raw))) {
		throw new Error("JUSTUP_LIMIT_NODE must be a positive integer");
	}
	return Math.min(budget.nodeConcurrency, Number(raw));
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
	const files = readdirSync(dir).filter((name) => name.endsWith(".test.mjs")).sort().map((name) => path.join("tests/js", name));
	const result = spawnSync(process.execPath, ["--test", `--test-concurrency=${nodeTestConcurrency(currentBudget())}`, ...files], { cwd: repo, stdio: "inherit" });
	if (result.error) throw result.error;
	process.exitCode = result.status ?? 2;
}
