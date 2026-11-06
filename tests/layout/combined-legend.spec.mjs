//
// tests/layout/combined-legend.spec.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

// Combined response-time chart: one legend entry per series with its own palette color, no
// per-series peak entries, and a single keyboard-operable "Show peaks" switch (DESIGN.md,
// "Charts and data visualization").
import assert from "node:assert/strict";
import { test } from "./fixtures.mjs";
import { waitForVisualStability } from "./harness.mjs";
import { waitForChartReady } from "./test-support.mjs";

// Target 4 has HTTP, Ping and several TCP ports; ports 443 and 8443 carry measurements.
const TARGET_PATH = "/ui/targets/4";
const VIEWPORTS = [{ w: 1280, h: 720 }, { w: 390, h: 844 }, { w: 320, h: 640 }];
const THEMES = ["light", "dark"];

async function openTarget(browser, app, adminState, viewport) {
	const context = await browser.newContext({ viewport: { width: viewport.w, height: viewport.h }, storageState: adminState });
	const page = await context.newPage();
	const problems = [];
	page.on("pageerror", (error) => problems.push(String(error)));
	page.on("console", (message) => { if (message.type() === "error") problems.push(message.text()); });
	await page.goto(app.base + TARGET_PATH, { waitUntil: "load" });
	await waitForChartReady(page);
	await page.waitForFunction(() => document.querySelectorAll("#combined-legend .combined-legend-btn").length > 1, null, { timeout: 30000 });
	await page.locator("#combined-legend").scrollIntoViewIfNeeded();
	await waitForVisualStability(page);
	return { context, page, problems };
}

const setTheme = async (page, theme) => {
	await page.evaluate((mode) => document.documentElement.setAttribute("data-bs-theme", mode), theme);
	// The chart re-reads its tokens after the theme observer fires (throttled, next frame).
	await page.waitForFunction((mode) => {
		const token = getComputedStyle(document.documentElement).getPropertyValue("--ju-dv-1").trim().toLowerCase();
		const swatch = document.querySelector("#combined-legend .combined-swatch--line");
		return document.documentElement.getAttribute("data-bs-theme") === mode && swatch &&
			getComputedStyle(swatch).getPropertyValue("--swatch-color").trim().toLowerCase() === token;
	}, theme, { timeout: 10000 });
	await waitForVisualStability(page);
};

const legendState = (page) => page.evaluate(() => {
	const chart = window.Chart.getChart(document.getElementById("chart-combined"));
	const buttons = Array.from(document.querySelectorAll("#combined-legend .combined-legend-btn"));
	const tokens = Array.from({ length: 8 }, (_, i) => getComputedStyle(document.documentElement).getPropertyValue(`--ju-dv-${i + 1}`).trim().toLowerCase());
	return {
		names: buttons.map((button) => button.getAttribute("aria-label")),
		swatches: buttons.map((button) => {
			const swatch = button.querySelector(".combined-swatch--line");
			return swatch ? getComputedStyle(swatch).getPropertyValue("--swatch-color").trim().toLowerCase() : null;
		}).filter(Boolean),
		tokens,
		msCount: chart.data.datasets.filter((d) => d._kind === "ms").length,
		peakCount: chart.data.datasets.filter((d) => d._kind === "peak").length,
		msColors: chart.data.datasets.filter((d) => d._kind === "ms").map((d) => d.borderColor.toLowerCase()),
	};
});

const peakState = (page) => page.evaluate(() => {
	const chart = window.Chart.getChart(document.getElementById("chart-combined"));
	const datasets = chart.data.datasets;
	const visible = (kind) => datasets.map((d, i) => (d._kind === kind && chart.isDatasetVisible(i) ? d._srcKey : null)).filter(Boolean);
	return {
		visibleMs: visible("ms"), visiblePeaks: visible("peak"),
		xMin: chart.scales.x.min, xMax: chart.scales.x.max,
	};
});

for (const viewport of VIEWPORTS) {
	test(`combined chart legend colors and Show peaks switch at ${viewport.w}x${viewport.h} @combined-legend`, async ({ browser, app, adminState }) => {
		const { context, page, problems } = await openTarget(browser, app, adminState, viewport);
		try {
			for (const theme of THEMES) {
				await setTheme(page, theme);
				const state = await legendState(page);
				assert.ok(state.msCount >= 4, `${theme}: HTTP, Ping and two TCP ports are plotted`);
				assert.equal(state.names.filter((name) => /peak/i.test(name)).length, 0, `${theme}: no peak legend entries (${state.names})`);
				assert.deepEqual(state.names.filter((name) => /^(HTTP|Ping|TCP \d+)$/.test(name)).length, state.msCount, `${theme}: one entry per series`);
				assert.equal(state.swatches.length, state.msCount, `${theme}: one color swatch per series`);
				assert.equal(new Set(state.swatches).size, state.swatches.length, `${theme}: swatch colors are pairwise different (${state.swatches})`);
				assert.deepEqual(state.swatches.slice().sort(), state.msColors.slice().sort(), `${theme}: swatches match the plotted line colors`);
				state.swatches.forEach((color) => assert.ok(state.tokens.includes(color), `${theme}: ${color} comes from a --ju-dv-N token`));
				assert.equal(await page.getByRole("switch", { name: "Show peaks" }).count(), 1, `${theme}: exactly one Show peaks switch`);
				assert.equal(await page.locator("#combined-peaks input").count(), 1);

				// Reveal every series, then compare the shared switch against the chart state.
				const buttons = page.locator("#combined-legend .combined-legend-btn");
				for (let i = 0; i < await buttons.count(); i += 1) {
					const button = buttons.nth(i);
					if (/^(HTTP|Ping|TCP \d+)$/.test(await button.getAttribute("aria-label")) && await button.getAttribute("aria-pressed") === "false") await button.click();
				}
				const peaks = page.getByRole("switch", { name: "Show peaks" });
				if (!await peaks.isChecked()) await peaks.check();
				const shown = await peakState(page);
				assert.equal(shown.visiblePeaks.length, shown.visibleMs.length, `${theme}: every visible series shows its peak`);

				// Keyboard: Tab from the last legend button reaches the switch with a visible focus ring.
				await buttons.last().focus();
				await page.keyboard.press("Tab");
				const focus = await page.evaluate(() => {
					const active = document.activeElement;
					const style = getComputedStyle(active);
					return { id: active.id, outline: style.outlineStyle, width: parseFloat(style.outlineWidth) };
				});
				assert.equal(focus.id, "combined-peaks-toggle", `${theme}: the switch follows the legend in tab order`);
				assert.ok(focus.outline !== "none" && focus.width >= 2, `${theme}: visible keyboard focus (${focus.outline} ${focus.width}px)`);
				await page.keyboard.press("Space");
				const hidden = await peakState(page);
				assert.deepEqual(hidden.visiblePeaks, [], `${theme}: the switch hides the peaks of all series`);
				assert.deepEqual(hidden.visibleMs, shown.visibleMs, `${theme}: series stay visible`);
				assert.equal(hidden.xMin, shown.xMin);
				assert.equal(hidden.xMax, shown.xMax);
				assert.match(await page.locator("#combined-summary").textContent(), /peaks hidden/);
				await page.keyboard.press("Space");
				const again = await peakState(page);
				assert.equal(again.visiblePeaks.length, again.visibleMs.length, `${theme}: peaks return together`);
				assert.deepEqual([again.xMin, again.xMax], [shown.xMin, shown.xMax], `${theme}: zoom range is kept`);

				// A series hidden in the legend takes its peak with it.
				await buttons.first().click();
				const withoutFirst = await peakState(page);
				assert.equal(withoutFirst.visiblePeaks.length, withoutFirst.visibleMs.length);
				assert.ok(withoutFirst.visibleMs.length < again.visibleMs.length);
				await buttons.first().click();

				// Reflow: no horizontal overflow, target size and legend stay inside the viewport.
				const geometry = await page.evaluate(() => {
					const root = document.documentElement;
					const label = document.querySelector("#combined-peaks .form-check-label").getBoundingClientRect();
					const input = document.getElementById("combined-peaks-toggle").getBoundingClientRect();
					const legend = document.getElementById("combined-legend");
					const legendRect = legend.getBoundingClientRect();
					return {
						pageOverflow: root.scrollWidth - root.clientWidth, legendOverflow: legend.scrollWidth - legend.clientWidth,
						legendRight: legendRect.right, labelHeight: label.height, inputWidth: input.width, inputHeight: input.height,
						switchRight: Math.max(label.right, input.right), viewport: root.clientWidth,
					};
				});
				assert.ok(geometry.pageOverflow <= 0, `${theme}: no horizontal page overflow (${geometry.pageOverflow})`);
				assert.ok(geometry.legendOverflow <= 1, `${theme}: legend wraps instead of overflowing`);
				assert.ok(geometry.legendRight <= geometry.viewport + 1 && geometry.switchRight <= geometry.viewport + 1, `${theme}: legend and switch stay in the viewport`);
				assert.ok(geometry.labelHeight >= 24 && geometry.inputWidth >= 16, `${theme}: switch target is at least 24 CSS px (${geometry.labelHeight})`);
			}
			assert.deepEqual(problems, [], "no page or console errors");
		} finally {
			await context.close();
		}
	});
}

test("combined chart draws every API point of a series after a reload @combined-legend", async ({ browser, app, adminState }) => {
	const context = await browser.newContext({ viewport: { width: 1280, height: 900 }, storageState: adminState });
	try {
		const page = await context.newPage();
		let apiUrl = null;
		page.on("request", (request) => { if (/\/metrics\/http_ms\?/.test(request.url())) apiUrl = request.url(); });
		const ready = () => page.waitForFunction(() => {
			const chart = window.Chart?.getChart(document.getElementById("chart-combined"));
			return chart && chart.data.datasets.some((d) => d._kind === "ms");
		}, null, { timeout: 30000 });
		await page.goto(app.base + TARGET_PATH);
		await ready();
		await page.reload();
		await ready();
		await page.waitForFunction(() => document.querySelectorAll("#combined-legend .combined-legend-btn").length > 1);
		await waitForVisualStability(page);
		const drawn = await page.evaluate(() => {
			const chart = window.Chart.getChart(document.getElementById("chart-combined"));
			const ms = chart.data.datasets.find((d) => d._kind === "ms" && d._srcKey === "http");
			const peak = chart.data.datasets.find((d) => d._kind === "peak" && d._srcKey === "http");
			const count = (d) => d.data.filter((p) => p.y !== null).length;
			return { ms: count(ms), peak: count(peak) };
		});
		const apiPoints = await page.evaluate(async (url) => (await (await fetch(url)).json()).points.length, apiUrl);
		assert.ok(apiPoints > 1, `the API returns more than one point (${apiPoints})`);
		assert.equal(drawn.ms, apiPoints, "all API points reach the response-time line");
		assert.ok(drawn.peak >= 1 && drawn.peak < apiPoints, "only notable intervals become peaks, not every bucket maximum");
	} finally {
		await context.close();
	}
});

test("peaks render as red triangles with solid stems to the median and no connecting line @combined-legend", async ({ browser, app, adminState }) => {
	const { context, page, problems } = await openTarget(browser, app, adminState, VIEWPORTS[0]);
	try {
		for (const theme of THEMES) {
			await setTheme(page, theme);
			const config = await page.evaluate(() => {
				const chart = window.Chart.getChart(document.getElementById("chart-combined"));
				const token = getComputedStyle(document.documentElement).getPropertyValue("--ju-peak").trim().toLowerCase();
				const peaks = chart.data.datasets.filter((d) => d._kind === "peak");
				const ms = chart.data.datasets.filter((d) => d._kind === "ms");
				return {
					token, palette: chart._palette.peak.toLowerCase(), count: peaks.length,
					noLine: peaks.every((d) => d.showLine === false),
					triangles: peaks.every((d) => d.pointStyle === "triangle" && !d.pointRotation),
					red: peaks.every((d) => d.pointBackgroundColor.toLowerCase() === token),
					noNull: peaks.every((d) => d.data.every((p) => Number.isFinite(p.y) && Number.isFinite(p.median) && p.y > p.median)),
					solid: ms.concat(peaks).every((d) => !d.borderDash || d.borderDash.length === 0),
					singleAxis: Object.keys(chart.options.scales).includes("yPeak") === false && peaks.every((d) => d.yAxisID === "y"),
					tooltip: chart.options.plugins.tooltip.callbacks.label({ dataset: peaks[0], raw: peaks[0].data[0], parsed: { y: peaks[0].data[0].y } }),
					real: Math.round(peaks[0].data[0].max),
				};
			});
			assert.ok(config.count >= 4, `${theme}: a peak dataset per series`);
			assert.match(config.token, /^#[0-9a-f]{6}$/, `${theme}: --ju-peak is defined`);
			assert.equal(config.palette, config.token, `${theme}: the chart follows the theme token`);
			assert.ok(config.noLine && config.triangles && config.red, `${theme}: red upward triangles without a connecting line`);
			assert.ok(config.noNull && config.solid && config.singleAxis, `${theme}: stems above their median on one axis, solid curves`);
			assert.ok(config.tooltip.includes(`${config.real} ms`) && config.tooltip.includes("median"), `${theme}: tooltip keeps the real maximum and median (${config.tooltip})`);

			// Canvas pixels: only the two-peak port stays visible, so the row between its stems must hold no peak color.
			const pixels = await page.evaluate(() => {
				const canvas = document.getElementById("chart-combined");
				const chart = window.Chart.getChart(canvas);
				chart.data.datasets.forEach((d, i) => chart.setDatasetVisibility(i, d._kind === "status" || d._srcKey === "tcp:8443"));
				chart._showPeaks = true;
				chart.update("none");
				const probe = document.createElement("canvas").getContext("2d");
				probe.fillStyle = chart._palette.peak;
				probe.fillRect(0, 0, 1, 1);
				const rgb = Array.from(probe.getImageData(0, 0, 1, 1).data.slice(0, 3));
				const ratio = canvas.width / canvas.clientWidth;
				const { data, width } = canvas.getContext("2d").getImageData(0, 0, canvas.width, canvas.height);
				const isPeak = (x, y) => {
					const i = (Math.round(y) * width + Math.round(x)) * 4;
					return data[i + 3] > 200 && [0, 1, 2].every((k) => Math.abs(data[i + k] - rgb[k]) <= 24);
				};
				const peak = chart.data.datasets.find((d) => d._kind === "peak" && d._srcKey === "tcp:8443");
				const ys = chart.scales.y;
				const stems = peak.data.map((p) => ({ x: chart.scales.x.getPixelForValue(p.x), top: ys.getPixelForValue(p.y), bottom: ys.getPixelForValue(p.median) }));
				const stemRun = stems.map((st) => {
					let hit = 0, total = 0;
					for (let y = st.top + 5; y < st.bottom - 1; y += 1 / ratio) { total += 1; if (isPeak(st.x * ratio, y * ratio)) hit += 1; }
					return hit / Math.max(total, 1);
				});
				const [first, second] = stems;
				const midY = (Math.min(first.top, second.top) + Math.min(first.bottom, second.bottom)) / 2;
				let between = 0;
				for (let x = first.x + 6; x < second.x - 6; x += 1 / ratio) if (isPeak(x * ratio, midY * ratio)) between += 1;
				const area = chart.chartArea;
				return { stemRun, between, count: stems.length, inside: stems.every((st) => st.top >= area.top - 1 && st.bottom <= area.bottom + 1) };
			});
			assert.equal(pixels.count, 2, `${theme}: two separate peaks`);
			assert.ok(pixels.stemRun.every((share) => share > 0.8), `${theme}: each stem is drawn solid down to its median (${pixels.stemRun})`);
			assert.equal(pixels.between, 0, `${theme}: no line connects the two peaks`);
			assert.ok(pixels.inside, `${theme}: stems stay inside the plot area`);
			await page.evaluate(() => {
				const chart = window.Chart.getChart(document.getElementById("chart-combined"));
				chart.data.datasets.forEach((d, i) => chart.setDatasetVisibility(i, true));
				chart.update("none");
			});
		}
		assert.deepEqual(problems, [], "no page or console errors");
	} finally {
		await context.close();
	}
});
