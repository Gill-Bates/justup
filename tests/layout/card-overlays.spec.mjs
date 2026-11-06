//
// tests/layout/card-overlays.spec.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

// Overlays that leave their card stay whole (DESIGN.md, "Cards and panels"): an open dropdown menu is hit-testable at
// its corners and a chart tooltip lies inside its canvas, so no card, plot or panel clips either.
import assert from "node:assert/strict";
import { test } from "./fixtures.mjs";
import { waitForVisualStability } from "./harness.mjs";
import { waitForChartReady } from "./test-support.mjs";

const TARGET_PATH = "/ui/targets/4";

test("an open dropdown menu is not clipped by its card @card-overlays", async ({ browser, app, adminState }) => {
	const context = await browser.newContext({ viewport: { width: 390, height: 844 }, storageState: adminState });
	const page = await context.newPage();
	try {
		await page.goto(app.base + TARGET_PATH, { waitUntil: "load" });
		await waitForVisualStability(page);
		await page.click("#target-more-toggle");
		const menu = page.locator(".target-detail-more-menu");
		await menu.waitFor({ state: "visible" });
		await page.waitForTimeout(300);
		const result = await menu.evaluate((el) => {
			const r = el.getBoundingClientRect();
			const probes = [[r.left + 2, r.top + 2], [r.right - 2, r.top + 2], [r.left + 2, r.bottom - 2], [r.right - 2, r.bottom - 2]];
			return { hit: probes.map(([x, y]) => { const hit = document.elementFromPoint(x, y); return !!hit && el.contains(hit); }), inside: r.left >= 0 && r.right <= innerWidth && r.top >= 0 };
		});
		assert.deepEqual(result.hit, [true, true, true, true], "every corner of the open menu is on top of the page");
		assert.equal(result.inside, true, "the menu lies inside the viewport");
	} finally {
		await context.close();
	}
});

for (const id of ["chart-combined", "chart-uptime"]) {
	test(`${id} tooltip lies inside its canvas @card-overlays`, async ({ browser, app, adminState }) => {
		const context = await browser.newContext({ viewport: { width: 1280, height: 720 }, storageState: adminState });
		const page = await context.newPage();
		try {
			await page.goto(app.base + TARGET_PATH, { waitUntil: "load" });
			await waitForChartReady(page, id, { withData: true });
			await page.locator(`#${id}`).scrollIntoViewIfNeeded();
			await waitForVisualStability(page);
			// Hover near the right end of the plot, where a wide tooltip is most likely to be cut off.
			const point = await page.evaluate((canvasId) => {
				const canvas = document.getElementById(canvasId);
				const rect = canvas.getBoundingClientRect();
				const { right, top, bottom } = window.Chart.getChart(canvas).chartArea;
				return { x: rect.left + right - 4, y: rect.top + (top + bottom) / 2 };
			}, id);
			for (const dx of [0, -6, -12, -24, -48]) {
				await page.mouse.move(point.x + dx, point.y, { steps: 4 });
				await page.waitForTimeout(250);
				const tip = await page.evaluate((canvasId) => {
					const canvas = document.getElementById(canvasId);
					const t = window.Chart.getChart(canvas).tooltip;
					if (!t || !t.opacity || !t.width) return null;
					return { x: t.x, y: t.y, w: t.width, h: t.height, cw: canvas.clientWidth, ch: canvas.clientHeight };
				}, id);
				if (!tip) continue;
				assert.ok(tip.x >= -0.5 && tip.y >= -0.5 && tip.x + tip.w <= tip.cw + 0.5 && tip.y + tip.h <= tip.ch + 0.5, `tooltip cut off: ${JSON.stringify(tip)}`);
			}
		} finally {
			await context.close();
		}
	});
}
