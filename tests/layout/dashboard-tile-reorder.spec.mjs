//
// tests/layout/dashboard-tile-reorder.spec.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

// GridStack re-appends every tile after each fit (batchUpdate(false) -> _sortDom). dashboard-grid.js ignores such
// pure tile reorders (isTileReorder); reacting to them would refit forever and drop keyboard focus to <body>.
import assert from "node:assert/strict";
import { test } from "./fixtures.mjs";
import { waitForVisualStability } from "./harness.mjs";

test("dashboard fit does not loop on tile reorders and keeps keyboard focus @dashboard-refit", async ({ browser, app, adminState }) => {
	const context = await browser.newContext({ viewport: { width: 1280, height: 720 }, storageState: adminState });
	const page = await context.newPage();
	try {
		await page.goto(app.base + "/", { waitUntil: "load" });
		await page.waitForFunction(() => {
			const root = document.getElementById("dashboard-grid");
			return root?.gridstack && !root.classList.contains("dashboard-grid--loading") && !document.getElementById("dashboard-edit-toggle")?.disabled;
		}, null, { timeout: 15000 });
		await page.getByRole("button", { name: "Edit dashboard", exact: true }).click();
		await waitForVisualStability(page);
		const handle = page.locator('[data-widget-id="status-online"]').getByRole("button", { name: /^Move or resize widget: Online\./ });
		await handle.focus();
		assert.equal(await handle.evaluate((el) => el === document.activeElement), true, "the handle takes focus");
		const result = await page.evaluate(() => new Promise((resolve) => {
			const root = document.getElementById("dashboard-grid");
			const grid = root.gridstack;
			const handleEl = document.activeElement;
			const original = grid.batchUpdate.bind(grid);
			let fits = 0;
			grid.batchUpdate = (flag) => { if (flag === false) fits += 1; return original(flag); };
			let reorders = 0;
			new MutationObserver((records) => { reorders += records.filter((record) => record.target === root && record.removedNodes.length).length; })
				.observe(root, { childList: true });
			// One real trigger for a fit; everything after it would be the feedback loop.
			window.dispatchEvent(new Event("resize"));
			let frames = 0;
			const tick = () => {
				frames += 1;
				if (frames < 90) return requestAnimationFrame(tick);
				resolve({ fits, reorders, focusKept: document.activeElement === handleEl, active: document.activeElement?.tagName });
			};
			requestAnimationFrame(tick);
		}));
		assert.ok(result.fits >= 1, `the resize did not run a fit: ${JSON.stringify(result)}`);
		assert.ok(result.fits <= 3, `the fit keeps re-running after tile reorders (isTileReorder): ${JSON.stringify(result)}`);
		assert.ok(result.reorders <= 3 * 20, `tiles keep being re-appended: ${JSON.stringify(result)}`);
		assert.equal(result.focusKept, true, `keyboard focus left the handle: ${JSON.stringify(result)}`);
	} finally {
		await context.close();
	}
});
