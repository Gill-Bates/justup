//
// tests/layout/layout.spec.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

import assert from "node:assert/strict";
import { test } from "./fixtures.mjs";
import { auditDashboardGrid, auditPage, auditSelfTest, PAGES, VIEWPORTS } from "./harness.mjs";

const profile = process.env.JUSTUP_LAYOUT_PROFILE || "quick";
const pageFilter = process.env.JUSTUP_LAYOUT_PAGES?.split(",").map((name) => name.trim()).filter(Boolean);
const exactPages = process.env.JUSTUP_LAYOUT_PAGE?.split(",").map((name) => name.trim()).filter(Boolean);
const viewportFilter = process.env.JUSTUP_LAYOUT_VIEWPORTS?.split(",").map((name) => name.trim()).filter(Boolean);
const pages = exactPages
	? PAGES.filter((page) => exactPages.includes(page.id))
	: PAGES.filter((page) => !pageFilter || pageFilter.some((filter) => page.id.includes(filter)));
const viewports = viewportFilter
	? VIEWPORTS.filter((viewport) => viewportFilter.includes(viewport.name))
	: VIEWPORTS;

if (profile !== "quick" && profile !== "full") throw new Error(`Unknown layout profile: ${profile}`);
if (!pages.length) throw new Error("Empty layout page selection");
if (!viewports.length) throw new Error("Empty layout viewport selection");
for (const name of exactPages || []) {
	if (!PAGES.some((page) => page.id === name)) throw new Error(`Unknown layout page: ${name}`);
}
for (const name of pageFilter || []) {
	if (!PAGES.some((page) => page.id.includes(name))) throw new Error(`Unknown layout page filter: ${name}`);
}
for (const name of viewportFilter || []) {
	if (!VIEWPORTS.some((viewport) => viewport.name === name)) throw new Error(`Unknown layout viewport: ${name}`);
}

test("layout audit detects injected defects @audit-self-test", async ({ browser }) => {
	await auditSelfTest(browser);
});

for (const viewport of viewports) {
	for (const pageSpec of pages) {
		test(`${pageSpec.id} at ${viewport.name} @layout`, async ({ browser, browserName, app, adminState }) => {
			const findings = await auditPage(browser, app, adminState, browserName, viewport, pageSpec);
			assert.deepEqual(findings, [], `${browserName}/${viewport.name}/${pageSpec.id} layout findings`);
		});
	}
}

for (const phase of ["roles", "legacy", "sizes"]) {
	test(`dashboard GridStack ${phase} @gridstack`, async ({ browser, browserName, app, adminState }) => {
		test.setTimeout(1_200_000);
		await auditDashboardGrid(browser, app, adminState, browserName, phase);
	});
}
