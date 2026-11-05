//
// tests/layout/run.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

import { spawnSync } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const repo = path.resolve(here, "../..");
const options = new Map();
const flags = new Set(["self-test", "gridstack", "focus", "matrix", "inspect", "help"]);
const allowed = new Set(["profile", "browsers", "pages", "page", "viewports", "shots", "json", ...flags]);

for (let index = 2; index < process.argv.length; index += 1) {
	const argument = process.argv[index];
	if (!argument.startsWith("--")) throw new Error(`Unexpected argument: ${argument}`);
	const name = argument.slice(2);
	if (!allowed.has(name)) throw new Error(`Unknown option: ${argument}`);
	if (flags.has(name)) options.set(name, true);
	else {
		const value = process.argv[index + 1];
		if (!value || value.startsWith("--")) throw new Error(`Missing value for ${argument}`);
		options.set(name, value);
		index += 1;
	}
}

if (options.has("help")) {
	console.log("node tests/layout/run.mjs [--profile quick|full] [--browsers chromium,webkit,firefox] [--pages FILTERS] [--page IDS] [--viewports NAMES] [--shots DIR] [--json FILE] [--self-test|--gridstack|--focus|--matrix]");
	process.exitCode = 0;
} else {
	if (["self-test", "gridstack", "focus", "matrix"].filter((name) => options.has(name)).length > 1) {
		throw new Error("Choose only one of --self-test, --gridstack, --focus, or --matrix");
	}
	const env = { ...process.env };
	for (const [option, variable] of [
		["profile", "JUSTUP_LAYOUT_PROFILE"],
		["browsers", "JUSTUP_LAYOUT_BROWSERS"],
		["pages", "JUSTUP_LAYOUT_PAGES"],
		["page", "JUSTUP_LAYOUT_PAGE"],
		["viewports", "JUSTUP_LAYOUT_VIEWPORTS"],
		["shots", "JUSTUP_LAYOUT_SHOTS"],
		["json", "JUSTUP_LAYOUT_JSON"],
	]) {
		if (options.has(option)) env[variable] = options.get(option);
	}
	if (options.has("inspect")) env.JUSTUP_LAYOUT_INSPECT = "1";
	const grepArgs = options.has("self-test")
		? ["--grep", "@audit-self-test"]
		: options.has("gridstack")
			? ["--grep", "@gridstack"]
			: options.has("focus")
				? ["--grep", "@focus|@targets-search"]
				: options.has("matrix")
					? ["--grep", "@layout|@audit-self-test"]
					: ["--grep", "@layout"];
	const cli = path.join(repo, "node_modules/@playwright/test/cli.js");
	const result = spawnSync(process.execPath, [cli, "test", "--config", path.join(here, "playwright.config.mjs"), ...grepArgs], {
		cwd: repo,
		env,
		stdio: "inherit",
	});
	if (result.error) throw result.error;
	process.exitCode = result.status ?? 2;
}
