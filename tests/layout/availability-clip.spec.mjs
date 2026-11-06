//
// tests/layout/availability-clip.spec.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

// Availability chart (DESIGN.md, "Charts and data visualization"): series are clipped to the plot area and never
// drawn over the axes or the card edge, after a drag zoom, after reset and after a live reload.
import assert from "node:assert/strict";
import { test } from "./fixtures.mjs";
import { waitForVisualStability } from "./harness.mjs";
import { waitForChartReady } from "./test-support.mjs";

const VIEWPORTS = [{ name: "1280x720", width: 1280, height: 900 }, { name: "390x844", width: 390, height: 844 }];
const TARGET_PATH = "/ui/targets/4";

// Pixels of a series color left or right of the plot area, level with it (where the y axis labels and the card edge
// are); legend hidden so its swatches cannot count.
const scan = (page) => page.evaluate(() => {
	const canvas = document.getElementById("chart-uptime");
	const chart = window.Chart.getChart(canvas);
	chart.options.plugins.legend.display = false;
	chart.update("none");
	const ratio = canvas.width / canvas.clientWidth;
	const area = chart.chartArea;
	const colors = [...new Set([...chart.data.datasets.map((d) => d.borderColor), "#dc3545"])].map((c) => {
		const probe = document.createElement("canvas").getContext("2d");
		probe.fillStyle = c;
		probe.fillRect(0, 0, 1, 1);
		return Array.from(probe.getImageData(0, 0, 1, 1).data.slice(0, 3));
	});
	const { data, width, height } = canvas.getContext("2d").getImageData(0, 0, canvas.width, canvas.height);
	const left = Math.floor(area.left * ratio) - 1, right = Math.ceil(area.right * ratio) + 1;
	const top = Math.floor(area.top * ratio) - 2, bottom = Math.ceil(area.bottom * ratio) + 2;
	let outside = 0, inside = 0; const bbox = { x1: 1e9, y1: 1e9, x2: -1, y2: -1 }, sample = [];
	for (let y = 0; y < height; y++) {
		for (let x = 0; x < width; x++) {
			const i = (y * width + x) * 4;
			if (data[i + 3] < 200) continue;
			if (!colors.some((c) => Math.abs(data[i] - c[0]) <= 10 && Math.abs(data[i + 1] - c[1]) <= 10 && Math.abs(data[i + 2] - c[2]) <= 10)) continue;
			if (x >= left && x <= right && y >= top && y <= bottom) inside += 1; else if (y >= top && y <= bottom) { outside += 1; bbox.x1 = Math.min(bbox.x1, x); bbox.y1 = Math.min(bbox.y1, y); bbox.x2 = Math.max(bbox.x2, x); bbox.y2 = Math.max(bbox.y2, y); if (sample.length < 3) sample.push([x, y, data[i], data[i + 1], data[i + 2]]); }
		}
	}
	chart.options.plugins.legend.display = true;
	chart.update("none");
	return { bbox, sample, area: [left, top, right, bottom], outside, inside, xMin: chart.scales.x.min, xMax: chart.scales.x.max };
});

async function open(browser, app, adminState, viewport, theme) {
	const context = await browser.newContext({ viewport: { width: viewport.width, height: viewport.height }, storageState: adminState });
	const page = await context.newPage();
	await page.goto(app.base + TARGET_PATH, { waitUntil: "load" });
	await page.evaluate((mode) => document.documentElement.setAttribute("data-bs-theme", mode), theme);
	await waitForChartReady(page, "chart-uptime", { withData: true });
	await page.locator("#chart-uptime").scrollIntoViewIfNeeded();
	await waitForVisualStability(page);
	return { context, page };
}

// Drag over a fraction of the plot area (the window that leaves the data points outside).
async function drag(page, from, to) {
	const area = await page.evaluate(() => {
		const canvas = document.getElementById("chart-uptime");
		const rect = canvas.getBoundingClientRect();
		const { left, right, top, bottom } = window.Chart.getChart(canvas).chartArea;
		return { left: rect.left + left, width: right - left, y: rect.top + (top + bottom) / 2 };
	});
	await page.mouse.move(area.left + area.width * from, area.y);
	await page.mouse.down();
	await page.mouse.move(area.left + area.width * to, area.y, { steps: 8 });
	await page.mouse.up();
	await page.waitForTimeout(900);
}

for (const viewport of VIEWPORTS) {
	for (const theme of ["light", "dark"]) {
		test(`availability series stay inside the plot area at ${viewport.name} ${theme} @availability-clip`, async ({ browser, app, adminState }) => {
			const { context, page } = await open(browser, app, adminState, viewport, theme);
			try {
				const loaded = await scan(page);
				assert.ok(loaded.inside > 0, "the chart draws series");
				assert.equal(loaded.outside, 0, `series pixels outside the plot after load: ${loaded.outside} ${JSON.stringify([loaded.bbox, loaded.sample, loaded.area])}`);

				// Zoom between the data points: the segments leave the window on both sides.
				await drag(page, 0.965, 0.985);
				const zoomed = await scan(page);
				assert.ok(zoomed.xMax - zoomed.xMin < loaded.xMax - loaded.xMin, "the drag zoomed");
				assert.equal(zoomed.outside, 0, `series pixels outside the plot after zoom: ${zoomed.outside}`);

				await page.dblclick("#chart-uptime");
				await page.waitForTimeout(900);
				const reset = await scan(page);
				assert.equal(reset.outside, 0, `series pixels outside the plot after reset: ${reset.outside}`);
				assert.equal(reset.xMin, loaded.xMin);
			} finally {
				await context.close();
			}
		});
	}
}
