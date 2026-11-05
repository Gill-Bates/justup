//
// tests/layout/public-egress.spec.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//
// Settings > Network "Public outbound IPs": grid position, independent family states, refresh and copy
// from the keyboard, long IPv6 values, no horizontal overflow, light and dark.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { test as base } from "@playwright/test";
import { startStaticAppServer } from "./test-support.mjs";
const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");
const NETWORK = readFileSync(path.join(ROOT, "app", "templates", "settings", "network.html"), "utf8");
const start = NETWORK.lastIndexOf("<details", NETWORK.indexOf("data-public-egress"));
const CARD = NETWORK.slice(start, NETWORK.indexOf("</details>", start) + "</details>".length);
const dummy = (title) => `<details class="card shadow-sm settings-card settings-details" open><summary class="card-header"><h2 class="h6 mb-0">${title}</h2></summary><div class="card-body"><p>${title}</p></div></details>`;
const PAGE = `<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="stylesheet" href="/static/css/bootstrap.min.css"><link rel="stylesheet" href="/static/css/material-icons.css"><link rel="stylesheet" href="/static/css/gentelella-layout.css"><link rel="stylesheet" href="/static/css/ui.css">
</head><body><main class="ui-page" id="host"><form class="ju-settings-grid" id="advanced-settings-form" data-settings-autosave>${dummy("Connection Quality")}${dummy("Provider Lookup")}${dummy("Geolocation")}${CARD}</form></main></body></html>`;
const VIEWPORTS = [
	{ name: "1280x720", width: 1280, height: 720, scale: 1 },
	{ name: "390x844", width: 390, height: 844, scale: 1 },
	{ name: "320x640", width: 320, height: 640, scale: 1 },
	{ name: "1280x720 at 200%", width: 640, height: 360, scale: 2 },
];
const LONG_V6 = "2a02:8071:abcd:1234:5678:9abc:def0:1234";
const test = base.extend({
	base: [async ({}, use) => {
		const server = await startStaticAppServer({ root: ROOT, renderPage: () => PAGE });
		try {
			await use(server.baseUrl);
		} finally {
			await server.close();
		}
	}, { scope: "worker" }],
});
const text = (page, selector) => page.locator(selector).first().innerText();
for (const viewport of VIEWPORTS) {
	for (const theme of ["light", "dark"]) {
		test(`Public outbound IPs: states, refresh, copy, layout at ${viewport.name} ${theme} @public-egress`, async ({ browser, base }) => {
			const context = await browser.newContext({ viewport: { width: viewport.width, height: viewport.height }, deviceScaleFactor: viewport.scale });
			const page = await context.newPage();
			const errors = [];
			page.on("pageerror", (e) => errors.push(String(e)));
			page.on("console", (m) => { if (m.type() === "error" && !m.text().startsWith("Failed to load resource")) errors.push(m.text()); });
			await page.goto(base);
			await page.evaluate(async ({ t, v6 }) => {
				document.documentElement.setAttribute("data-bs-theme", t);
				window.__calls = [];
				window.__copied = [];
				Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText: async (value) => { window.__copied.push(value); } } });
				const data = (v4) => ({
					ipv4: { status: "available", address: v4, observed_at: "2026-11-05T19:00:00Z", sources: ["a", "b"], observed: [v4], reason: null },
					ipv6: { status: "failed", address: null, observed_at: null, sources: [], observed: [], reason: "timeout" },
					checked_at: "2026-11-05T19:00:00Z", cached: false, proxy_env: false,
				});
				window.__next = data("93.184.216.34");
				window.__data = data;
				window.__v6 = v6;
				window.apiFetch = async (url) => {
					window.__calls.push(url);
					return { ok: true, status: 200, json: async () => window.__next };
				};
				const module = await import("/static/js/settings.public-egress.js");
				module.initPublicEgress();
				module.initPublicEgress(); // a repeated HTMX settle must not bind twice
			}, { t: theme, v6: LONG_V6 });
			await page.waitForSelector('[data-egress-family="ipv4"][data-egress-state="available"]');
			// One request on the first view; IPv6 failure does not hide the IPv4 address.
			assert.deepEqual(await page.evaluate(() => window.__calls), ["/settings/network/public-egress"]);
			assert.equal(await text(page, '[data-egress-family="ipv4"] [data-egress-address]'), "93.184.216.34");
			assert.equal(await text(page, '[data-egress-family="ipv6"] [data-egress-address]'), "\u2014");
			assert.equal(await text(page, '[data-egress-family="ipv6"] [data-egress-badge]'), "Lookup failed");
			assert.equal(await page.locator('[data-egress-family="ipv6"] [data-egress-copy]').isDisabled(), true);
			assert.equal(await page.locator('[data-egress-family="ipv4"] [data-egress-copy]').isDisabled(), false);
			assert.notEqual((await text(page, "[data-egress-checked]")).trim(), "\u2026");
			// Keyboard: Refresh asks for a fresh result; a long IPv6 value appears.
			await page.evaluate((v6) => {
				window.__next = { ...window.__data("93.184.216.34"), ipv6: { status: "available", address: v6, observed_at: "2026-11-05T19:05:00Z", sources: ["a"], observed: [v6], reason: null } };
			}, LONG_V6);
			await page.focus("[data-egress-refresh]");
			await page.keyboard.press("Enter");
			await page.waitForSelector('[data-egress-family="ipv6"][data-egress-state="available"]');
			assert.deepEqual(await page.evaluate(() => window.__calls.at(-1)), "/settings/network/public-egress?refresh=true");
			assert.equal(await page.evaluate(() => window.__calls.length), 2);
			// Keyboard copy with an accessible confirmation.
			await page.focus('[data-egress-family="ipv6"] [data-egress-copy]');
			await page.keyboard.press("Enter");
			await page.waitForFunction(() => document.querySelector("[data-egress-live]").textContent === "IPv6 address copied.");
			assert.deepEqual(await page.evaluate(() => window.__copied), [LONG_V6]);
			// Geometry: no overflow, long value stays inside the card, desktop position right of Geolocation.
			const geometry = await page.evaluate(() => {
				const de = document.documentElement;
				const rect = (el) => el.getBoundingClientRect();
				const cards = Array.from(document.querySelectorAll("#advanced-settings-form > details")).map(rect);
				const address = rect(document.querySelector('[data-egress-family="ipv6"] [data-egress-address]'));
				const copy = rect(document.querySelector('[data-egress-family="ipv6"] [data-egress-copy]'));
				const card = cards[3];
				const scrollers = Array.from(document.querySelectorAll("[data-public-egress], [data-public-egress] *")).filter((el) => !el.closest(".visually-hidden") && el.scrollWidth > el.clientWidth + 1 && getComputedStyle(el).overflowX !== "visible");
				return { overflow: de.scrollWidth > de.clientWidth + 1, addressInside: address.right <= card.right + 1 && address.left >= card.left - 1, copyInside: copy.right <= card.right + 1, copySize: Math.min(copy.width, copy.height), cards, innerScroll: scrollers.length };
			});
			assert.equal(geometry.overflow, false, "page overflows horizontally");
			assert.equal(geometry.addressInside, true, "IPv6 value leaves the card");
			assert.equal(geometry.copyInside, true, "copy button leaves the card");
			assert.ok(geometry.copySize >= 24, "copy target smaller than 24 px");
			assert.equal(geometry.innerScroll, 0, "inner scroll region");
			if (viewport.width >= 1280) {
				const [, provider, geo, egress] = geometry.cards;
				assert.ok(egress.left > geo.right - 2 || egress.left > geo.left + geo.width / 2, "card is not right of Geolocation");
				assert.ok(Math.abs(egress.top - geo.top) < 2, "card is not level with Geolocation");
				assert.ok(egress.top >= provider.top + 1, "card is not below Provider Lookup");
			} else if (viewport.width <= 390) {
				const [, , geo, egress] = geometry.cards;
				assert.ok(egress.top >= geo.bottom - 1, "single column expected on narrow viewports");
			}
			// Nothing in the card can trigger the settings autosave.
			assert.equal(await page.locator("[data-public-egress] input, [data-public-egress] [name]").count(), 0);
			assert.deepEqual(errors, []);
			await context.close();
		});
	}
}
