//
// tests/layout/availability-colors.spec.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

// Availability chart colours (DESIGN.md DC-CHART-3, DC-CHART-4, DC-CHART-6): series take categorical
// --ju-dv-N tokens of the active theme, follow a runtime theme switch, and keep 3:1 contrast on the panel.
import assert from "node:assert/strict";
import { test } from "./fixtures.mjs";
import { waitForVisualStability } from "./harness.mjs";
import { waitForChartReady } from "./test-support.mjs";

const TARGET_PATH = "/ui/targets/4";

const state = (page) => page.evaluate(() => {
	const root = getComputedStyle(document.documentElement);
	const probe = document.createElement("canvas").getContext("2d");
	const rgb = (c) => { probe.fillStyle = "#000"; probe.fillStyle = c; probe.fillRect(0, 0, 1, 1); return Array.from(probe.getImageData(0, 0, 1, 1).data.slice(0, 3)); };
	const luminance = (c) => { const [r, g, b] = rgb(c).map((v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; }); return 0.2126 * r + 0.7152 * g + 0.0722 * b; };
	const ratio = (a, b) => { const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x); return (hi + 0.05) / (lo + 0.05); };
	const chart = window.Chart.getChart(document.getElementById("chart-uptime"));
	const tokens = Array.from({ length: 8 }, (_, i) => root.getPropertyValue(`--ju-dv-${i + 1}`).trim());
	const panel = root.getPropertyValue("--ju-panel-bg").trim();
	const datasets = chart.data.datasets;
	return {
		tokens: tokens.map((t) => rgb(t).join(",")),
		colors: datasets.map((d) => rgb(d.borderColor).join(",")),
		labels: datasets.map((d) => d.label),
		slots: datasets.map((d) => d._dvSlot),
		contrast: datasets.map((d) => ratio(d.borderColor, panel)),
		legendColor: rgb(chart.legend.options.labels.color).join(","),
		text: rgb(root.getPropertyValue("--bs-body-color").trim()).join(","),
	};
});

for (const theme of ["light", "dark"]) {
	test(`availability series use theme tokens and follow a theme switch, starting ${theme} @availability-clip`, async ({ browser, app, adminState }) => {
		const context = await browser.newContext({ viewport: { width: 1280, height: 900 }, storageState: adminState });
		try {
			const page = await context.newPage();
			await page.goto(app.base + TARGET_PATH, { waitUntil: "load" });
			await page.evaluate((mode) => document.documentElement.setAttribute("data-bs-theme", mode), theme);
			await waitForChartReady(page, "chart-uptime", { withData: true });
			for (const mode of [theme, theme === "light" ? "dark" : "light"]) {
				await page.evaluate((m) => document.documentElement.setAttribute("data-bs-theme", m), mode);
				// The chart re-themes after the observer fires; wait for the first series to carry the new token.
				await page.waitForFunction(() => {
					const chart = window.Chart.getChart(document.getElementById("chart-uptime"));
					const token = getComputedStyle(document.documentElement).getPropertyValue(`--ju-dv-${chart.data.datasets[0]._dvSlot + 1}`).trim().toLowerCase();
					return chart.data.datasets[0].borderColor.toLowerCase() === token;
				}, null, { timeout: 10000 });
				await waitForVisualStability(page);
				const s = await state(page);
				assert.ok(s.colors.length >= 2, `${mode}: several series are plotted`);
				s.colors.forEach((c, i) => assert.equal(c, s.tokens[s.slots[i]], `${mode}: ${s.labels[i]} uses its --ju-dv token`));
				assert.equal(new Set(s.slots).size, s.slots.length, `${mode}: series slots are pairwise different`);
				s.contrast.forEach((c, i) => assert.ok(c >= 3, `${mode}: ${s.labels[i]} has 3:1 contrast on the panel (${c.toFixed(2)})`));
				assert.equal(s.legendColor, s.text, `${mode}: legend text follows the theme`);
			}
		} finally {
			await context.close();
		}
	});
}
