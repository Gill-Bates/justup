//
// tests/layout/combined-zoom-reset.spec.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

// Combined response-time chart: double-click and the "Reset zoom" button return the chart to its load
// state (all scales, series and peaks), identically (DESIGN.md, "Charts and data visualization").
import assert from "node:assert/strict";
import { test } from "./fixtures.mjs";
import { waitForVisualStability } from "./harness.mjs";

const TARGET_PATH = "/ui/targets/4";
const THEMES = ["light", "dark"];

const snapshot = (page) => page.evaluate(() => {
	const chart = window.Chart.getChart(document.getElementById("chart-combined"));
	const scales = {};
	for (const [id, scale] of Object.entries(chart.scales)) scales[id] = [scale.min, scale.max];
	return {
		scales,
		points: chart.data.datasets.map((d) => d.data.length),
		visible: chart.data.datasets.map((d, i) => chart.isDatasetVisible(i)),
	};
});

async function settle(page) {
	// Zoom loads detail data after 300 ms and the reset animates for 250 ms.
	await page.waitForTimeout(1000);
	await waitForVisualStability(page);
}

// Drag over the given fraction of the plot area (not of the canvas, which includes the axes).
async function drag(page, from = 0.9, to = 0.99) {
	const area = await page.evaluate(() => {
		const canvas = document.getElementById("chart-combined");
		const rect = canvas.getBoundingClientRect();
		const { left, right, top, bottom } = window.Chart.getChart(canvas).chartArea;
		return { left: rect.left + left, width: right - left, y: rect.top + (top + bottom) / 2 };
	});
	await page.mouse.move(area.left + area.width * from, area.y);
	await page.mouse.down();
	await page.mouse.move(area.left + area.width * to, area.y, { steps: 8 });
	await page.mouse.up();
	await settle(page);
}

async function openTarget(browser, app, adminState) {
	const context = await browser.newContext({ viewport: { width: 1280, height: 900 }, storageState: adminState });
	const page = await context.newPage();
	const problems = [];
	page.on("pageerror", (error) => problems.push(String(error)));
	await page.goto(app.base + TARGET_PATH, { waitUntil: "load" });
	await page.waitForFunction(() => {
		const canvas = document.getElementById("chart-combined");
		const chart = canvas && window.Chart?.getChart(canvas);
		return chart && !/Loading data/i.test(canvas.getAttribute("aria-label") || "");
	}, null, { timeout: 30000 });
	await page.locator("#chart-combined").scrollIntoViewIfNeeded();
	await settle(page);
	return { context, page, problems };
}

// Zoom-detail requests (made while `detail.on`) return values four times as large and may be slow, so a
// zoomed chart has clearly different scales from the load state.
async function mockDetail(page, detail) {
	await page.route("**/metrics/**", async (route) => {
		if (!detail.on || !route.request().url().includes("display=true")) return route.continue();
		const response = await route.fetch();
		const body = await response.json();
		for (const point of body.points || []) for (const key of ["value", "min", "max"]) if (typeof point[key] === "number" && !/_up|status/.test(body.metric)) point[key] *= 4;
		if (detail.delayMs) await new Promise((resolve) => setTimeout(resolve, detail.delayMs));
		return route.fulfill({ response, json: body });
	});
}

const PEAKS = [true, false];
for (const theme of THEMES) {
	for (const peaks of PEAKS) {
		for (const via of ["dblclick", "button"]) {
			test(`reset by ${via} restores the load state, peaks ${peaks ? "on" : "off"}, ${theme} @combined-zoom-reset`, async ({ browser, app, adminState }) => {
				const detail = { on: false, delayMs: 0 };
				const context = await browser.newContext({ viewport: { width: 1280, height: 900 }, storageState: adminState });
				const page = await context.newPage();
				const problems = [];
				page.on("pageerror", (error) => problems.push(String(error)));
				await mockDetail(page, detail);
				try {
					await page.goto(app.base + TARGET_PATH, { waitUntil: "load" });
					await page.waitForFunction(() => {
						const canvas = document.getElementById("chart-combined");
						const chart = canvas && window.Chart?.getChart(canvas);
						return chart && !/Loading data/i.test(canvas.getAttribute("aria-label") || "");
					}, null, { timeout: 30000 });
					await page.evaluate((mode) => document.documentElement.setAttribute("data-bs-theme", mode), theme);
					await page.locator("#chart-combined").scrollIntoViewIfNeeded();
					if (!peaks) await page.uncheck("#combined-peaks-toggle");
					await settle(page);
					const load = await snapshot(page);
					const reset = () => (via === "dblclick" ? page.dblclick("#chart-combined") : page.click("#reset-zoom-btn"));

					// Zoom with larger detail values: the zoomed scales differ from the load scales.
					detail.on = true;
					await drag(page);
					const zoomed = await snapshot(page);
					assert.notDeepEqual(zoomed.scales.x, load.scales.x, "the drag zoomed the time axis");
					assert.ok(zoomed.scales.y[1] !== load.scales.y[1] || zoomed.points.join() !== load.points.join(), "zoom detail changed the chart");
					await reset();
					await settle(page);
					assert.deepEqual(await snapshot(page), load, "reset equals the load state");

					// Idempotent: a second reset and a second zoom/reset cycle give the same view.
					await reset();
					await settle(page);
					assert.deepEqual(await snapshot(page), load, "second reset");
					await drag(page, 0.7, 0.99);
					await reset();
					await settle(page);
					assert.deepEqual(await snapshot(page), load, "after a second cycle");

					// Reset while the detail request is still in flight: its late answer must not repaint.
					detail.delayMs = 1500;
					await drag(page);
					await reset();
					await page.waitForTimeout(2500);
					await waitForVisualStability(page);
					assert.deepEqual(await snapshot(page), load, "late detail response ignored");
					assert.deepEqual(problems, []);
				} finally {
					await context.close();
				}
			});
		}
	}
}
