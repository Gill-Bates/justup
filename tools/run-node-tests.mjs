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
const files = readdirSync(dir).filter((name) => name.endsWith(".test.mjs")).sort().map((name) => path.join("tests/js", name));
const result = spawnSync(process.execPath, ["--test", `--test-concurrency=${currentBudget().nodeConcurrency}`, ...files], { cwd: repo, stdio: "inherit" });
if (result.error) throw result.error;
process.exitCode = result.status ?? 2;
