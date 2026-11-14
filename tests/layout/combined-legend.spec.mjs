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
		const swatch = document.querySelector("#combined-legend .combined-legend-btn .combined-swatch[data-series]");
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
			const swatch = button.querySelector(".combined-swatch[data-series]");
			return swatch ? getComputedStyle(swatch).getPropertyValue("--swatch-color").trim().toLowerCase() : null;
		}).filter(Boolean),
		swatchMarkers: buttons.map((button) => button.querySelector(".combined-swatch[data-series]")?.getAttribute("data-marker")).filter(Boolean),
		msMarkers: chart.data.datasets.filter((d) => d._kind === "ms").map((d) => d.pointStyle),
		msMarkerRadius: chart.data.datasets.filter((d) => d._kind === "ms").map((d) => d.pointRadius),
		msLineDash: chart.data.datasets.filter((d) => d._kind === "ms").map((d) => (d.borderDash || []).length),
		pairs: chart.data.datasets.filter((d) => d._kind === "ms").map((d) => `${d.borderColor.toLowerCase()}/${d.pointStyle}`),
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

test("single HTTP metric uses online green and touch/keyboard points at phone widths @combined-single", async ({ browser, app, adminState }) => {
	for (const viewport of VIEWPORTS.slice(1)) {
		const context = await browser.newContext({ viewport: { width: viewport.w, height: viewport.h }, hasTouch: true, storageState: adminState });
		try {
			const page = await context.newPage();
			await page.route("**/metrics/http_ms?**", async (route) => {
				const response = await route.fetch();
				const body = await response.json();
				const now = Date.now();
				body.points = [100, 260, 120].map((value, i) => ({
					ts: new Date(now - (3 - i) * 60_000).toISOString(), value, max: value, count: 1,
				}));
				await route.fulfill({ response, json: body });
			});
			await page.goto(`${app.base}/ui/targets/1`, { waitUntil: "load" });
			await waitForChartReady(page, "chart-combined", { withData: true });
			for (const theme of THEMES) {
				await page.evaluate((mode) => document.documentElement.setAttribute("data-bs-theme", mode), theme);
				await page.waitForFunction(() => {
					const canvas = document.getElementById("chart-combined");
					const chart = window.Chart.getChart(canvas);
					const green = getComputedStyle(document.documentElement).getPropertyValue("--ju-st-online").trim().toLowerCase();
					return chart?.data.datasets.find((d) => d._kind === "ms")?.borderColor.toLowerCase() === green;
				});
				await page.locator("#chart-combined").scrollIntoViewIfNeeded();
				await waitForVisualStability(page);
				const state = await page.evaluate(() => {
					const canvas = document.getElementById("chart-combined");
					const chart = window.Chart.getChart(canvas);
					const ms = chart.data.datasets.find((d) => d._kind === "ms");
					const dot = chart.getDatasetMeta(chart.data.datasets.indexOf(ms)).data[1];
					const rect = canvas.getBoundingClientRect();
					const card = document.getElementById("combined-card").getBoundingClientRect();
					return {
						seriesCount: chart.data.datasets.filter((d) => d._kind === "ms").length,
						color: ms.borderColor.toLowerCase(), green: getComputedStyle(document.documentElement).getPropertyValue("--ju-st-online").trim().toLowerCase(),
						pointRadius: ms.pointRadius, hitRadius: ms.pointHitRadius, x: rect.left + dot.x, y: rect.top + dot.y,
						cardRight: card.right, pageOverflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
					};
				});
				assert.equal(state.seriesCount, 1);
				assert.equal(state.color, state.green);
				assert.ok(state.pointRadius > 0 && 2 * (state.pointRadius + state.hitRadius) >= 24);
				assert.ok(state.pageOverflow <= 0 && state.cardRight <= viewport.w + 1, `${theme}: chart stays in ${viewport.w}px viewport`);
				// Chrome sends a synthetic mouseout shortly after a tap and Chart.js clears the tooltip on it, so
				// the active elements are read in the tap's click, after the chart's own click handler ran.
				await page.evaluate(() => {
					const canvas = document.getElementById("chart-combined");
					window.__tapActive = null;
					canvas.addEventListener("click", () => {
						window.__tapActive = window.Chart.getChart(canvas).tooltip.getActiveElements().length;
					}, { once: true });
				});
				await page.touchscreen.tap(state.x, state.y);
				await page.waitForFunction(() => window.__tapActive !== null);
				const touched = await page.evaluate(() => window.__tapActive);
				assert.ok(touched >= 1, `${theme}: tapping the point reveals a tooltip`);
				await page.locator("#chart-combined").focus();
				await page.keyboard.press("Home");
				assert.match(await page.locator("#chart-combined-point-announcement").textContent(), /HTTP.*100 ms.*Point 1 of 3/);
				await page.keyboard.press("ArrowRight");
				assert.match(await page.locator("#chart-combined-point-announcement").textContent(), /HTTP.*260 ms.*Point 2 of 3/);
			}
		} finally {
			await context.close();
		}
	}
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
				assert.equal(state.names.filter((name) => /^(HTTP|Ping|TCP \d+)$/.test(name)).length, state.msCount, `${theme}: one entry per series`);
				assert.equal(state.swatches.length, state.msCount, `${theme}: one color swatch per series`);
				assert.equal(new Set(state.swatches).size, state.swatches.length, `${theme}: swatch colors are pairwise different (${state.swatches})`);
				assert.deepEqual(state.swatches.slice().sort(), state.msColors.slice().sort(), `${theme}: swatches match the plotted line colors`);
				state.swatches.forEach((color) => assert.ok(state.tokens.includes(color), `${theme}: ${color} comes from a --ju-dv-N token`));
				// DC-CHART-5..7: the second cue besides colour is the marker shape; legend swatch, dataset and solid style agree.
				assert.deepEqual(state.swatchMarkers, state.msMarkers, `${theme}: legend swatch shape equals the dataset pointStyle`);
				assert.equal(new Set(state.msMarkers).size, state.msMarkers.length, `${theme}: marker shapes are pairwise different (${state.msMarkers})`);
				assert.equal(new Set(state.pairs).size, state.pairs.length, `${theme}: (colour, shape) pairs are pairwise different`);
				assert.ok(state.msMarkerRadius.every((r) => r >= 3), `${theme}: marker radius is at least 3 px`);
				assert.ok(state.msLineDash.every((n) => n === 0), `${theme}: measurement series are solid`);
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
		assert.ok(apiUrl, "no /metrics/http_ms request was observed");
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

for (const viewport of VIEWPORTS) {
	test(`status donut legend shows code and class only, y axis shows its unit at ${viewport.w}x${viewport.h} @combined-legend`, async ({ browser, app, adminState }) => {
		const { context, page, problems } = await openTarget(browser, app, adminState, viewport);
		try {
			await page.waitForFunction(() => document.querySelectorAll("#status-donut-legend li").length > 0, null, { timeout: 30000 });
			for (const theme of THEMES) {
				await setTheme(page, theme);
				const state = await page.evaluate(() => {
					const items = Array.from(document.querySelectorAll("#status-donut-legend li"));
					const chart = window.Chart.getChart(document.getElementById("chart-combined"));
					const y = chart.scales.y;
					const rects = items.map((li) => li.getBoundingClientRect());
					return {
						keyExists: !!document.getElementById("combined-status-key"),
						classKeyText: Array.from(document.querySelectorAll("#combined-card .combined-swatch")).filter((s) => !s.closest("#combined-legend") && !s.closest("#status-donut-legend")).length,
						rows: items.map((li) => ({
							name: li.querySelector(".combined-donut-legend-name")?.textContent.trim(),
							hasValue: !!li.querySelector(".combined-donut-legend-value"),
						})),
						overlap: rects.some((a, i) => rects.some((b, j) => i < j && a.bottom > b.top + 1 && a.top < b.bottom - 1)),
						titleShown: !!y.options.title.display,
						titleText: y.options.title.text,
						ticks: y.ticks.map((tick) => tick.label).filter((label) => label !== ""),
					};
				});
				assert.equal(state.keyExists, false, `${theme}: the class key above the donut is gone`);
				assert.equal(state.classKeyText, 0, `${theme}: no second swatch legend besides toggles and donut`);
				assert.ok(state.rows.length > 0 && state.rows.every((row) => /^(HTTP \d{3}|Error)/.test(row.name) && !row.hasValue), `${theme}: every row has only code and class, no count/share value (${JSON.stringify(state.rows)})`);
				assert.equal(state.overlap, false, `${theme}: legend rows do not overlap`);
				if (state.titleShown) assert.match(state.titleText, /\(ms\)/, `${theme}: axis title names the unit`);
				else assert.ok(state.ticks.length > 0 && state.ticks.every((label) => /\bms$/.test(label)), `${theme}: ticks carry the unit (${state.ticks})`);
			}
		} finally {
			await context.close();
		}
		assert.deepEqual(problems, [], "no console or page errors");
	});
}
// DC-CHART-10: a peak is drawn at most 3x the highest median of its series, so one outlier cannot flatten the curve; the
// tooltip still names the real maximum and says that the marker sits at the axis limit.
test("a peak is drawn at most three medians high and its tooltip names the real maximum @combined-legend", async ({ browser, app, adminState }) => {
	const { context, page, problems } = await openTarget(browser, app, adminState, VIEWPORTS[0]);
	try {
		await page.locator("#chart-combined").scrollIntoViewIfNeeded();
		const state = await page.evaluate(() => {
			const chart = window.Chart.getChart(document.getElementById("chart-combined"));
			const sets = chart.data.datasets;
			return sets.map((set, index) => ({ set, index })).filter(({ set }) => set._kind === "peak").map(({ set, index }) => {
				const series = sets.find((other) => other._kind === "ms" && other._srcKey === set._srcKey);
				const medians = series.data.map((point) => point.y).filter(Number.isFinite);
				const peaks = set.data.map((point) => ({ y: point.y, max: point.max, capped: point.capped }));
				return { key: set._srcKey, index, visible: chart.isDatasetVisible(index), cap: 3 * Math.max(...medians), peaks };
			});
		});
		assert.ok(state.length >= 4, "HTTP, Ping and both TCP ports carry peaks (hidden series keep their data)");
		for (const series of state) {
			assert.ok(series.peaks.length > 0, `${series.key}: the fixture has peaks`);
			for (const peak of series.peaks) {
				assert.ok(peak.y <= series.cap + 1e-6, `${series.key}: a peak is drawn at ${peak.y}, above 3x the highest median (${series.cap})`);
				assert.ok(peak.max >= peak.y, `${series.key}: the real maximum ${peak.max} is not below the drawn height ${peak.y}`);
				assert.equal(peak.capped, peak.y < peak.max, `${series.key}: capped flag follows the drawn height`);
			}
		}
		const capped = state.filter((series) => series.visible).flatMap((series) => series.peaks.filter((peak) => peak.capped).map((peak) => ({ ...peak, index: series.index, cap: series.cap })));
		assert.ok(capped.length > 0, "the visible HTTP series holds an outlier above the cap (spike of 1400 ms)");
		const worst = capped.sort((a, b) => b.max - a.max)[0];
		assert.equal(worst.y, worst.cap, "an outlier sits exactly at the cap");
		const position = await page.evaluate(({ index, max }) => {
			const chart = window.Chart.getChart(document.getElementById("chart-combined"));
			const meta = chart.getDatasetMeta(index);
			const at = chart.data.datasets[index].data.findIndex((point) => point.max === max);
			const rect = chart.canvas.getBoundingClientRect();
			return { x: rect.left + meta.data[at].x, y: rect.top + meta.data[at].y };
		}, worst);
		await page.mouse.move(position.x, position.y);
		await page.waitForFunction(() => window.Chart.getChart(document.getElementById("chart-combined")).tooltip?.opacity > 0);
		const lines = await page.evaluate(() => window.Chart.getChart(document.getElementById("chart-combined")).tooltip.body.flatMap((body) => body.lines));
		const text = lines.find((line) => line.includes(String(Math.round(worst.max))));
		assert.ok(text, `a tooltip line names the real maximum ${worst.max} ms (${JSON.stringify(lines)})`);
		assert.match(text, /drawn at the axis limit/);
	} finally {
		await context.close();
	}
	assert.deepEqual(problems, [], "no console or page errors");
});
