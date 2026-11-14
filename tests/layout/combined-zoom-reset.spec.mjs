//
// tests/layout/combined-zoom-reset.spec.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

// Combined response-time chart: double-click and the "Reset zoom" button return the chart to its load
// state (all scales, series and peaks), identically (DESIGN.md, "Charts and data visualization").
import assert from "node:assert/strict";
import { expect } from "@playwright/test";
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
	detail.pendingByMetric = new Map();
	detail.seenMetrics = new Set();
	// Zero must hold across several polls, so a metric whose request has not reached the route yet is not missed.
	detail.drain = async () => {
		let quiet = 0;
		await expect.poll(() => { quiet = detail.pending === 0 ? quiet + 1 : 0; return quiet; }, { intervals: [50], timeout: 20000 }).toBeGreaterThanOrEqual(4);
	};
	await page.route("**/metrics/**", async (route) => {
		if (!detail.on || !route.request().url().includes("display=true")) return route.continue();
		const metric = new URL(route.request().url()).pathname.split("/").pop();
		detail.pending += 1;
		detail.seenMetrics.add(metric);
		detail.pendingByMetric.set(metric, (detail.pendingByMetric.get(metric) || 0) + 1);
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
			const pending = detail.pendingByMetric.get(metric) - 1;
			if (pending > 0) detail.pendingByMetric.set(metric, pending);
			else detail.pendingByMetric.delete(metric);
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

					// Repeated cycles must not make restored datasets grow across zoom detail loads.
					const selections = [
						[0.15, 0.4], [0.15, 0.4], [0.3, 0.55], [0.45, 0.7],
						[0.6, 0.85], [0.7, 0.95], [0.5, 0.75], [0.25, 0.5],
					];
					for (const [cycle, selection] of selections.entries()) {
						await drag(page, detail, ...selection);
						await reset();
						await settle(page);
						assert.deepEqual(await snapshot(page), load, `after cycle ${cycle + 1}`);
					}

					// Reset while the detail request is still in flight: its late answer must not repaint.
					detail.delayMs = 1500;
					const expectedMetrics = [...detail.seenMetrics];
					assert.ok(expectedMetrics.length > 0, "the zoom issued detail requests");
					detail.pendingByMetric.clear();
					await drag(page, detail, 0.9, 0.99, { settleAfter: false });
					await expect.poll(
						() => expectedMetrics.filter((metric) => detail.pendingByMetric.has(metric)).sort(),
						{ timeout: 10000, message: "every delayed detail metric is in flight" },
					).toEqual([...expectedMetrics].sort());
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

test("overview uses readable density and zoom reloads at plot resolution @combined-zoom-reset", async ({ browser, app, adminState }) => {
	const context = await browser.newContext({ viewport: { width: 1920, height: 900 }, storageState: adminState });
	const page = await context.newPage();
	try {
		const request = page.waitForRequest((item) => {
			const url = new URL(item.url());
			return url.pathname === "/targets/4/metrics/http_ms" && url.searchParams.get("display") === "true";
		});
		await page.goto(app.base + TARGET_PATH, { waitUntil: "load" });
		const plotWidth = Number(new URL((await request).url()).searchParams.get("plot_width"));
		await waitForChartReady(page);
		const containerWidth = await page.locator("#combined-plot").evaluate((element) => element.clientWidth);
		assert.ok(containerWidth >= 500, "desktop chart has a wide plot container");
		assert.equal(plotWidth, Math.max(120, Math.round(containerWidth / 6)), "the overview budgets about one point per 12 CSS px");
		await page.locator("#chart-combined").scrollIntoViewIfNeeded();
		await settle(page);
		const detailRequest = page.waitForRequest((item) => {
			const url = new URL(item.url());
			if (url.pathname !== "/targets/4/metrics/http_ms" || url.searchParams.get("display") !== "true") return false;
			return Date.parse(url.searchParams.get("until")) - Date.parse(url.searchParams.get("since")) < 20 * 60 * 60 * 1000;
		}, { timeout: 10000 });
		await drag(page, {}, 0.2, 0.5, { settleAfter: false });
		const zoomWidth = Number(new URL((await detailRequest).url()).searchParams.get("plot_width"));
		assert.ok(zoomWidth > plotWidth, "zoom requests retain the finer plot resolution");
	} finally {
		await context.close();
	}
});

test("live refresh after zoom reset follows an actual plot resize @combined-zoom-reset", async ({ browser, app, adminState }) => {
	const detail = { on: false, delayMs: 0 };
	const context = await browser.newContext({ viewport: { width: 360, height: 900 }, storageState: adminState });
	const page = await context.newPage();
	const metricRequests = [];
	page.on("request", (request) => {
		const url = new URL(request.url());
		if (url.pathname === "/targets/4/metrics/http_ms" && url.searchParams.get("display") === "true") metricRequests.push(url);
	});
	await page.addInitScript(() => {
		class TestEventSource extends EventTarget {
			constructor(url) { super(); this.url = url; window.__testEventSource = this; }
			close() {}
		}
		window.EventSource = TestEventSource;
	});
	await mockDetail(page, detail);
	try {
		const initialRequest = page.waitForRequest((request) => request.url().includes("/targets/4/metrics/http_ms?") && request.url().includes("display=true"));
		await page.goto(app.base + TARGET_PATH, { waitUntil: "load" });
		const initialWidth = new URL((await initialRequest).url()).searchParams.get("plot_width");
		await waitForChartReady(page);
		const initialPlotWidth = await page.locator("#combined-plot").evaluate((element) => element.clientWidth);
		assert.equal(Number(initialWidth), Math.max(120, Math.round(initialPlotWidth / 6)), "the mobile load uses the overview density");

		await page.locator("#chart-combined").scrollIntoViewIfNeeded();
		await settle(page);
		const buildBefore = await page.evaluate(() => window.Chart.getChart(document.getElementById("chart-combined"))._buildGeneration);
		detail.on = true;
		await drag(page, detail, 0.7, 0.95);
		await page.setViewportSize({ width: 1920, height: 900 });
		await page.waitForFunction((previousWidth) => document.getElementById("combined-plot").clientWidth !== previousWidth, initialPlotWidth);
		await page.waitForFunction(() => {
			const plot = document.getElementById("combined-plot");
			const width = plot.clientWidth;
			if (window.__stablePlotWidth === width) window.__stablePlotFrames += 1;
			else { window.__stablePlotWidth = width; window.__stablePlotFrames = 0; }
			return window.__stablePlotFrames >= 2;
		});
		const resizedPlotWidth = await page.locator("#combined-plot").evaluate((element) => element.clientWidth);
		assert.ok(resizedPlotWidth >= 500 && resizedPlotWidth !== Number(initialWidth), "the plot actually resized");
		const fullRangeReload = page.waitForRequest((request) => {
			const url = new URL(request.url());
			if (url.pathname !== "/targets/4/metrics/http_ms" || url.searchParams.get("display") !== "true") return false;
			const since = Date.parse(url.searchParams.get("since"));
			const until = Date.parse(url.searchParams.get("until"));
			return until - since > 20 * 60 * 60 * 1000;
		});
		await page.evaluate(() => {
			const event = new Event("target.measurement");
			Object.defineProperty(event, "data", { value: JSON.stringify({ target_id: 4 }) });
			window.__testEventSource.dispatchEvent(event);
		});
		await page.dblclick("#chart-combined");
		await settle(page);
		detail.on = false;
		const refreshRequest = await fullRangeReload;
		const refreshWidth = new URL(refreshRequest.url()).searchParams.get("plot_width");
		await expect.poll(() => page.evaluate(() => window.Chart.getChart(document.getElementById("chart-combined"))._buildGeneration)).toBeGreaterThan(buildBefore);
		await settle(page);
		assert.equal(Number(refreshWidth), Math.max(120, Math.round(resizedPlotWidth / 6)), "a live rebuild uses the current overview density after a real resize");
		assert.ok(metricRequests.length >= 3, "the event caused a full-range metric reload after zoom reset");
		const restored = await snapshot(page);
		assert.deepEqual(restored.scales.x, await page.evaluate(() => {
			const chart = window.Chart.getChart(document.getElementById("chart-combined"));
			return [chart._loadScaleLimits.x.min, chart._loadScaleLimits.x.max];
		}), "the live rebuild returns to the selected time range");
	} finally {
		await context.close();
	}
});
