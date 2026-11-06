//
// tests/layout/combined-clip.spec.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

// Response-time and availability charts (DESIGN.md, "Charts and data visualization"): series are clipped to the
// plot area and never drawn over the axes or the card edge, also after repeated zoom, reset and zoom again.
import assert from "node:assert/strict";
import { test } from "./fixtures.mjs";
import { waitForVisualStability } from "./harness.mjs";
import { waitForChartReady } from "./test-support.mjs";

const VIEWPORTS = [{ name: "1280x720", width: 1280, height: 720 }, { name: "390x844", width: 390, height: 844 }];
const TARGET_PATH = "/ui/targets/4";
const CHARTS = ["chart-combined", "chart-uptime"];

// Pixels of a series or downtime color left or right of the plot area, level with it (where the axes and the card edge are).
const scan = async (page, id) => {
	// The tooltip is an overlay on the same canvas, so it must not count.
	await page.mouse.move(2, 2);
	await page.waitForTimeout(400);
	return measure(page, id);
};
const measure = (page, id) => page.evaluate((canvasId) => {
	const canvas = document.getElementById(canvasId);
	const chart = window.Chart.getChart(canvas);
	const legend = chart.options.plugins.legend;
	const legendShown = !!legend?.display;
	if (legendShown) { legend.display = false; chart.update("none"); }
	const palette = chart._palette || {};
	const ratio = canvas.width / canvas.clientWidth;
	const area = chart.chartArea;
	// Series lines plus the downtime band color (box annotations are part of the plot, not of the axes).
	const colors = [...new Set([...chart.data.datasets.map((d) => d.borderColor), palette.down, "#dc3545"].filter((c) => typeof c === "string"))].map((c) => {
		const probe = document.createElement("canvas").getContext("2d");
		probe.fillStyle = c;
		probe.fillRect(0, 0, 1, 1);
		return Array.from(probe.getImageData(0, 0, 1, 1).data.slice(0, 3));
	});
	const { data, width, height } = canvas.getContext("2d").getImageData(0, 0, canvas.width, canvas.height);
	// Two device pixels of slack: a line ending exactly on the plot edge antialiases into its neighbors.
	const left = Math.floor(area.left * ratio) - 2, right = Math.ceil(area.right * ratio) + 2;
	const top = Math.floor(area.top * ratio) - 2, bottom = Math.ceil(area.bottom * ratio) + 2;
	let outside = 0, inside = 0; const bbox = { x1: 1e9, y1: 1e9, x2: -1, y2: -1 };
	for (let y = 0; y < height; y++) {
		for (let x = 0; x < width; x++) {
			const i = (y * width + x) * 4;
			if (data[i + 3] < 200) continue;
			if (!colors.some((c) => Math.abs(data[i] - c[0]) <= 10 && Math.abs(data[i + 1] - c[1]) <= 10 && Math.abs(data[i + 2] - c[2]) <= 10)) continue;
			if (x >= left && x <= right && y >= top && y <= bottom) inside += 1;
			else if (y >= top && y <= bottom) { outside += 1; bbox.x1 = Math.min(bbox.x1, x); bbox.y1 = Math.min(bbox.y1, y); bbox.x2 = Math.max(bbox.x2, x); bbox.y2 = Math.max(bbox.y2, y); }
		}
	}
	if (legendShown) { legend.display = true; chart.update("none"); }
	return { bbox, area: [left, top, right, bottom], outside, inside, xMin: chart.scales.x.min, xMax: chart.scales.x.max };
}, id);

async function open(browser, app, adminState, viewport, theme, id) {
	const context = await browser.newContext({ viewport: { width: viewport.width, height: viewport.height }, deviceScaleFactor: 3, storageState: adminState });
	const page = await context.newPage();
	await page.goto(app.base + TARGET_PATH, { waitUntil: "load" });
	await page.evaluate((mode) => document.documentElement.setAttribute("data-bs-theme", mode), theme);
	await waitForChartReady(page, id, { withData: true });
	await page.locator(`#${id}`).scrollIntoViewIfNeeded();
	await waitForVisualStability(page);
	return { context, page };
}

async function drag(page, id, from, to) {
	const area = await page.evaluate((canvasId) => {
		const canvas = document.getElementById(canvasId);
		const rect = canvas.getBoundingClientRect();
		const { left, right, top, bottom } = window.Chart.getChart(canvas).chartArea;
		return { left: rect.left + left, width: right - left, y: rect.top + (top + bottom) / 2 };
	}, id);
	await page.mouse.move(area.left + area.width * from, area.y);
	await page.mouse.down();
	await page.mouse.move(area.left + area.width * to, area.y, { steps: 8 });
	await page.mouse.up();
	await page.waitForTimeout(1000);
}

// Zoom to a window inside the first downtime band, so the band runs over both plot edges (combined chart only).
async function zoomIntoBand(page, id) {
	const zoomed = await page.evaluate((canvasId) => {
		const chart = window.Chart.getChart(document.getElementById(canvasId));
		const band = Object.entries(chart.options.plugins.annotation?.annotations || {}).find(([key, a]) => key.startsWith("down") && Number.isFinite(a.xMin) && Number.isFinite(a.xMax));
		if (!band) return false;
		const { xMin, xMax } = band[1];
		const width = xMax - xMin;
		chart.zoomScale("x", { min: xMin + width * 0.3, max: xMax - width * 0.3 }, "none");
		return true;
	}, id);
	await page.waitForTimeout(1200);
	return zoomed;
}

for (const id of CHARTS) {
	for (const viewport of VIEWPORTS) {
		for (const theme of ["light", "dark"]) {
			test(`${id} series stay inside the plot area through zoom cycles at ${viewport.name} ${theme} @combined-clip`, async ({ browser, app, adminState }) => {
				const { context, page } = await open(browser, app, adminState, viewport, theme, id);
				try {
					const loaded = await scan(page, id);
					assert.ok(loaded.inside > 0, "the chart draws series");
					assert.equal(loaded.outside, 0, `series pixels outside the plot after load: ${JSON.stringify(loaded)}`);
					if (id === "chart-combined") {
						assert.ok(await zoomIntoBand(page, id), "the fixture has a downtime band to zoom into");
						const inBand = await scan(page, id);
						assert.equal(inBand.outside, 0, `pixels outside the plot with a zoom window inside a downtime band: ${JSON.stringify(inBand)}`);
						await page.dblclick(`#${id}`);
						await page.waitForTimeout(1000);
					}
					for (const [from, to] of [[0.9, 0.99], [0.4, 0.6], [0.965, 0.985]]) {
						await drag(page, id, from, to);
						const zoomed = await scan(page, id); 
						assert.ok(zoomed.xMax - zoomed.xMin < loaded.xMax - loaded.xMin, "the drag zoomed");
						assert.equal(zoomed.outside, 0, `series pixels outside the plot after zoom ${from}-${to}: ${JSON.stringify(zoomed)}`);
						await page.dblclick(`#${id}`);
						await page.waitForTimeout(1000);
						const reset = await scan(page, id);
						assert.equal(reset.outside, 0, `series pixels outside the plot after reset: ${JSON.stringify(reset)}`);
						assert.equal(reset.xMin, loaded.xMin);
					}
				} finally {
					await context.close();
				}
			});
		}
	}
}
