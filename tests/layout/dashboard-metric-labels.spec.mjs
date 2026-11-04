//
// tests/layout/dashboard-metric-labels.spec.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

// Dashboard status widgets (Alerts, Delivery queue): title, status badge, three equal metrics with the
// number above its label, and labels that wrap between words, never inside one (DESIGN.md, dashboard widgets).
import assert from "node:assert/strict";
import { test } from "./fixtures.mjs";

const VIEWPORTS = [
	{ name: "1280x720", width: 1280, height: 720 },
	{ name: "390x844", width: 390, height: 844 },
	{ name: "320x640", width: 320, height: 640 },
];

for (const viewport of VIEWPORTS) {
	for (const theme of ["light", "dark"]) {
		test(`alerts and queue widgets keep whole words in their labels at ${viewport.name} ${theme} @dashboard-metrics`, async ({ browser, app, adminState }) => {
			const context = await browser.newContext({ viewport: { width: viewport.width, height: viewport.height }, storageState: adminState });
			const page = await context.newPage();
			const errors = [];
			page.on("pageerror", (error) => errors.push(String(error)));
			await page.route("**/alerts/summary**", (route) => route.fulfill({ json: { open: 2, acknowledged_24h: 1, started_24h: 5, resolved_24h: 3 } }));
			try {
				await page.goto(app.base + "/", { waitUntil: "load" });
				await page.evaluate((mode) => document.documentElement.setAttribute("data-bs-theme", mode), theme);
				await page.waitForFunction(() => {
					const grid = document.getElementById("dashboard-grid");
					const tile = document.querySelector('[data-widget-id="alerts-open"] .dashboard-monitor-metrics');
					return grid?.gridstack && !grid.classList.contains("dashboard-grid--loading") && tile;
				}, null, { timeout: 15000 });
				await page.waitForTimeout(800);
				const result = await page.evaluate(() => {
					const out = {};
					for (const id of ["alerts-open", "delivery-queue"]) {
						const tile = document.querySelector(`[data-widget-id="${id}"]`);
						if (!tile) continue;
						const brokenWords = [];
						for (const dt of tile.querySelectorAll(".dashboard-monitor-metrics dt")) {
							const text = dt.textContent;
							dt.textContent = "";
							for (const word of text.split(" ")) {
								const span = document.createElement("span");
								span.textContent = word;
								dt.append(span, " ");
							}
							for (const span of dt.querySelectorAll("span")) if (span.getClientRects().length > 1) brokenWords.push(span.textContent);
							dt.textContent = text;
						}
						const body = tile.querySelector(".dashboard-monitor-body");
						out[id] = {
							title: tile.querySelector(".dashboard-monitor-title")?.textContent.trim(),
							hasStatus: Boolean(tile.querySelector(".dashboard-monitor-status")),
							numbersFirst: [...tile.querySelectorAll(".dashboard-monitor-metrics > div")].every((cell) => cell.querySelector("dd").getBoundingClientRect().top < cell.querySelector("dt").getBoundingClientRect().top),
							metrics: tile.querySelectorAll(".dashboard-monitor-metrics > div").length,
							brokenWords,
							bodyOverflowY: body.scrollHeight - body.clientHeight,
							bodyOverflowX: body.scrollWidth - body.clientWidth,
						};
					}
					return out;
				});
				const alerts = result["alerts-open"];
				assert.ok(alerts, "the Alerts widget is on the dashboard");
				assert.equal(alerts.title, "Alerts");
				assert.equal(alerts.hasStatus, true);
				assert.equal(alerts.metrics, 3);
				for (const [id, tile] of Object.entries(result)) {
					assert.equal(tile.numbersFirst, true, `${id}: the number sits above its label`);
					assert.deepEqual(tile.brokenWords, [], `${id}: a label word is broken inside the word`);
					assert.ok(tile.bodyOverflowX <= 1, `${id}: horizontal overflow ${tile.bodyOverflowX}px`);
					assert.ok(tile.bodyOverflowY <= 2, `${id}: ghost scroll ${tile.bodyOverflowY}px`);
				}
				assert.deepEqual(errors, []);
			} finally {
				await context.close();
			}
		});
	}
}
