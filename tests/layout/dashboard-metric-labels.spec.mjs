//
// tests/layout/dashboard-metric-labels.spec.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

// Dashboard metrics keep numbers above whole-word labels; status heroes center the icon and number
// above their label within the existing tile (DESIGN.md, dashboard widgets).
import assert from "node:assert/strict";
import { test } from "./fixtures.mjs";
import { waitForVisualStability } from "./harness.mjs";

const VIEWPORTS = [
	{ name: "1280x720", width: 1280, height: 720 },
	{ name: "1024x768", width: 1024, height: 768 },
	{ name: "390x844", width: 390, height: 844 },
	{ name: "320x640", width: 320, height: 640 },
];

for (const viewport of VIEWPORTS) {
	for (const theme of ["light", "dark"]) {
		test(`dashboard metrics and status heroes at ${viewport.name} ${theme} @dashboard-metrics`, async ({ browser, app, adminState }) => {
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
				await waitForVisualStability(page);
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
				assert.ok(result["delivery-queue"], "the admin-only Delivery queue widget is on the dashboard");
				assert.equal(alerts.title, "Alerts");
				assert.equal(alerts.hasStatus, true);
				assert.equal(alerts.metrics, 3);
				for (const [id, tile] of Object.entries(result)) {
					assert.equal(tile.numbersFirst, true, `${id}: the number sits above its label`);
					assert.deepEqual(tile.brokenWords, [], `${id}: a label word is broken inside the word`);
					assert.ok(tile.bodyOverflowX <= 1, `${id}: horizontal overflow ${tile.bodyOverflowX}px`);
					assert.ok(tile.bodyOverflowY <= 2, `${id}: ghost scroll ${tile.bodyOverflowY}px`);
				}
				await page.waitForFunction(() => {
					const values = [...document.querySelectorAll(".dashboard-hero .dashboard-widget-value")];
					return values.length === 4 && values.every((value) => value.textContent.trim() !== "–");
				}, null, { timeout: 10000 });
				const heroes = await page.evaluate(() => ["online", "offline", "disabled", "total"].map((id) => {
					const card = document.getElementById(`dashboard-status-${id}`);
					const tile = card.closest(".grid-stack-item");
					const frame = tile.querySelector(".grid-stack-item-content");
					const metric = card.querySelector(".dashboard-hero-metric");
					const icon = metric.querySelector(".material-icons");
					const value = metric.querySelector(".dashboard-widget-value");
					const label = card.querySelector(".dashboard-widget-label");
					const body = card.querySelector(".dashboard-hero-body");
					const cardRect = card.getBoundingClientRect();
					const frameRect = frame.getBoundingClientRect();
					const iconRect = icon.getBoundingClientRect();
					const valueRect = value.getBoundingClientRect();
					const metricRect = metric.getBoundingClientRect();
					const labelRect = label.getBoundingClientRect();
					const iconStyle = getComputedStyle(icon);
					return {
						id,
						label: label.textContent.trim(),
						labelAlign: getComputedStyle(label).textAlign,
						labelTransform: getComputedStyle(label).textTransform,
						iconBackground: iconStyle.backgroundColor,
						iconBackgroundImage: iconStyle.backgroundImage,
						iconBeforeValue: iconRect.right <= valueRect.left,
						iconValueCenterOffset: Math.abs((iconRect.left + valueRect.right) / 2 - (cardRect.left + cardRect.right) / 2),
						iconValueVerticalOffset: Math.abs((iconRect.top + iconRect.bottom) / 2 - (valueRect.top + valueRect.bottom) / 2),
						labelBelowValue: labelRect.top >= metricRect.bottom - 2,
						cardHeight: cardRect.height,
						frameHeight: frameRect.height,
						rows: tile.getAttribute("gs-h"),
						bodyOverflow: body.scrollHeight - body.clientHeight,
					};
				}));
				for (const hero of heroes) {
					assert.equal(hero.label.toLowerCase(), hero.id);
					assert.equal(hero.labelAlign, "center", `${hero.id}: label is not centered`);
					assert.equal(hero.labelTransform, "uppercase", `${hero.id}: label is not uppercase`);
					assert.match(hero.iconBackground, /^(?:transparent|rgba\([^)]*,\s*0\))$/, `${hero.id}: icon has a background tile`);
					assert.equal(hero.iconBackgroundImage, "none", `${hero.id}: icon has a background image`);
					assert.equal(hero.iconBeforeValue, true, `${hero.id}: icon must precede the value`);
					assert.ok(hero.iconValueCenterOffset <= 2, `${hero.id}: icon and value are ${hero.iconValueCenterOffset}px off center`);
					assert.ok(hero.iconValueVerticalOffset <= 2, `${hero.id}: icon and value are not vertically centered`);
					assert.equal(hero.labelBelowValue, true, `${hero.id}: label must sit below the value`);
					assert.ok(Math.abs(hero.cardHeight - hero.frameHeight) <= 2, `${hero.id}: card does not fill its frame`);
					assert.ok(hero.bodyOverflow <= 2, `${hero.id}: hero body scrolls`);
					if (viewport.width >= 992) assert.equal(hero.rows, "2", `${hero.id}: hero tile grew beyond two rows`);
				}
				assert.deepEqual(errors, []);
			} finally {
				await context.close();
			}
		});
	}
}
