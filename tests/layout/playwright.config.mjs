//
// tests/layout/playwright.config.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

import { defineConfig } from "@playwright/test";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { currentBudget } from "../../tools/test-budget.mjs";

const browsers = (process.env.JUSTUP_LAYOUT_BROWSERS || "chromium").split(",").map((name) => name.trim()).filter(Boolean);
const supported = new Set(["chromium", "firefox", "webkit"]);
const unknown = browsers.filter((name) => !supported.has(name));
if (!browsers.length || unknown.length) throw new Error(`Unsupported layout browser selection: ${unknown.join(", ") || "empty"}`);

const reporters = [["list"]];
if (process.env.JUSTUP_LAYOUT_JUNIT) reporters.push(["junit", { outputFile: process.env.JUSTUP_LAYOUT_JUNIT }]);
if (process.env.JUSTUP_LAYOUT_JSON) reporters.push(["json", { outputFile: process.env.JUSTUP_LAYOUT_JSON }]);

// CI keeps 2 workers; locally the count comes from the shared budget (cores, cgroup, RAM, load).
// JUSTUP_LAYOUT_WORKERS overrides both.
const workers = (() => {
	const raw = process.env.JUSTUP_LAYOUT_WORKERS;
	if (raw !== undefined && raw !== "") {
		if (!/^[1-9][0-9]{0,3}$/.test(raw)) throw new Error(`JUSTUP_LAYOUT_WORKERS must be a positive integer, got ${JSON.stringify(raw)}`);
		return Number(raw);
	}
	return process.env.CI ? 2 : currentBudget().playwrightWorkers;
})();

if (!process.env.CI && !process.env.JUSTUP_RUN_LIMITED && !process.argv.includes("--list")) {
	console.error("Warning: local Playwright runs need tools/run-limited.sh to acquire a shared test slot.");
}

export default defineConfig({
	forbidOnly: true,
	testDir: path.dirname(fileURLToPath(import.meta.url)),
	testMatch: "*.spec.mjs",
	timeout: 180_000,
	globalTimeout: 1_500_000,
	expect: { timeout: 10_000 },
	fullyParallel: true,
	workers,
	retries: 0,
	reporter: reporters,
	outputDir: process.env.JUSTUP_LAYOUT_OUTPUT || path.join(os.tmpdir(), `justup-layout-${process.pid}`),
	use: {
		trace: "retain-on-failure",
		screenshot: "only-on-failure",
		video: "retain-on-failure",
	},
	projects: browsers.map((name) => ({
		name,
		use: {
			browserName: name,
			launchOptions: {
				...(name === "chromium" ? { ignoreDefaultArgs: ["--hide-scrollbars"], args: ["--disable-threaded-animation"] } : {}),
			},
		},
	})),
});
