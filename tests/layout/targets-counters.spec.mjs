//
// tests/layout/targets-counters.spec.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

// Targets list check counters (DESIGN.md DC-TLIST-2): at 320 and 390 px the summary does not overflow,
// `Passed` names successful checks, and no counter text repeats within a row.
import assert from "node:assert/strict";
import { test } from "./fixtures.mjs";
import { cssViewport } from "./test-support.mjs";
for (const viewport of [cssViewport(320, 640), cssViewport(390, 844)]) {
	test(`@targets-counters summary fits and is not redundant at ${viewport.width}px`, async ({ browser, app, adminState }) => {
		const context = await browser.newContext({ viewport: { width: viewport.width, height: viewport.height }, storageState: adminState });
		const page = await context.newPage();
		await page.goto(app.base + "/ui/targets", { waitUntil: "load" });
		await page.waitForFunction(() => document.querySelectorAll(".targets-check-summary").length > 0, null, { timeout: 20000 });
		// Groups can start collapsed: the stored group state (initAccordionState, localStorage key per group) is
		// restored on load, and initTargetsTableMobileCollapse additionally folds open groups at <= 576 px. The
		// summaries are only visible in an open group, so open them the way the user does before measuring.
		await page.evaluate(() => {
			document.querySelectorAll("#targets-table .accordion-collapse").forEach((collapse) => {
				const button = document.querySelector(`[data-bs-target="#${collapse.id}"]`);
				collapse.classList.add("show");
				button.setAttribute("aria-expanded", "true");
				button.classList.remove("collapsed");
			});
		});
		const result = await page.evaluate(() => {
			const rows = [...document.querySelectorAll(".targets-check-summary")].filter((el) => el.offsetParent !== null);
			return {
				visible: rows.length,
				pageOverflow: document.documentElement.scrollWidth > document.documentElement.clientWidth,
				rows: rows.map((el) => ({
					overflow: el.scrollWidth > el.clientWidth + 1,
					texts: [...el.children].map((c) => c.textContent.trim()),
					passedTitle: el.querySelector("span[title='Successful checks']")?.textContent.trim() ?? null,
				})),
			};
		});
		assert.ok(result.visible > 0, "at least one summary is visible at a mobile width");
		assert.equal(result.pageOverflow, false, "no horizontal page overflow");
		for (const row of result.rows) {
			assert.equal(row.overflow, false, `summary overflows: ${row.texts}`);
			assert.equal(new Set(row.texts).size, row.texts.length, `redundant counters: ${row.texts}`);
			assert.ok(row.texts.every((t) => !/\bUp$/.test(t)), `ambiguous 'Up' label: ${row.texts}`);
			if (row.passedTitle) assert.match(row.passedTitle, /Passed$/);
		}
		await context.close();
	});
}
