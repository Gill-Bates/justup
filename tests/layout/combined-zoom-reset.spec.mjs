//
// tests/layout/combined-zoom-reset.spec.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

// Combined response-time chart: double-click and the "Reset zoom" button return the chart to its load
// state (all scales, series and peaks), identically (DESIGN.md, "Charts and data visualization").
import assert from "node:assert/strict";
import { test } from "./fixtures.mjs";
import { waitForVisualStability } from "./harness.mjs";
import { waitForChartReady } from "./test-support.mjs";

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

// A reset or toggle has no request to wait for; the chart is settled once its animations are done.
const settle = (page) => waitForVisualStability(page);

// Drag over the given fraction of the plot area (not of the canvas, which includes the axes). Unless
// `settleAfter` is off, it returns once the zoom-detail answer has been delivered and painted.
async function drag(page, detail, from = 0.9, to = 0.99, { settleAfter = true } = {}) {
	const detailResponse = settleAfter ? page.waitForResponse((response) => response.url().includes("display=true")) : null;
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
	if (detailResponse) {
		await (await detailResponse).finished();
		await detail.drain();
		await settle(page);
	}
}

// Zoom-detail requests (made while `detail.on`) return values four times as large and may be slow, so a
// zoomed chart has clearly different scales from the load state.
// `detail.drain()` resolves once every intercepted detail request (one per metric) was answered or dropped.
async function mockDetail(page, detail) {
	detail.pending = 0;
	detail.drain = async () => { while (detail.pending > 0) await new Promise((resolve) => setTimeout(resolve, 25)); };
	await page.route("**/metrics/**", async (route) => {
		if (!detail.on || !route.request().url().includes("display=true")) return route.continue();
		detail.pending += 1;
		try {
			const response = await route.fetch();
			const body = await response.json();
			for (const point of body.points || []) for (const key of ["value", "min", "max"]) if (typeof point[key] === "number" && !/_up|status/.test(body.metric)) point[key] *= 4;
			if (detail.delayMs) await new Promise((resolve) => setTimeout(resolve, detail.delayMs));
			return await route.fulfill({ response, json: body });
		} catch {
			return undefined; // the page aborted the stale request
		} finally {
			detail.pending -= 1;
		}
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
					await waitForChartReady(page);
					await page.evaluate((mode) => document.documentElement.setAttribute("data-bs-theme", mode), theme);
					await page.locator("#chart-combined").scrollIntoViewIfNeeded();
					if (!peaks) await page.uncheck("#combined-peaks-toggle");
					await settle(page);
					const load = await snapshot(page);
					const reset = () => (via === "dblclick" ? page.dblclick("#chart-combined") : page.click("#reset-zoom-btn"));

					// Zoom with larger detail values: the zoomed scales differ from the load scales.
					detail.on = true;
					await drag(page, detail);
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
					await drag(page, detail, 0.7, 0.99);
					await reset();
					await settle(page);
					assert.deepEqual(await snapshot(page), load, "after a second cycle");

					// Reset while the detail request is still in flight: its late answer must not repaint.
					detail.delayMs = 1500;
					const requested = page.waitForRequest((request) => request.url().includes("display=true"));
					await drag(page, detail, 0.9, 0.99, { settleAfter: false });
					await requested;
					while (!detail.pending) await new Promise((resolve) => setTimeout(resolve, 10));
					await reset();
					await detail.drain();
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
