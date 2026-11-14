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
const start = NETWORK.lastIndexOf('<div class="card', NETWORK.indexOf("data-public-egress"));
const CARD = NETWORK.slice(start, NETWORK.lastIndexOf("</form>"));
const dummy = (title) => `<div class="card shadow-sm settings-card"><div class="card-header"><h2 class="h6 mb-0">${title}</h2></div><div class="card-body"><p>${title}</p></div></div>`;
const PAGE = `<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="stylesheet" href="/static/css/bootstrap.min.css"><link rel="stylesheet" href="/static/css/material-icons.css"><link rel="stylesheet" href="/static/css/gentelella-layout.css"><link rel="stylesheet" href="/static/css/ui.css">
<script src="/static/js/utils/status-badge.js"></script>
</head><body><main class="ui-page" id="host"><form class="ju-settings-grid" id="advanced-settings-form" data-settings-autosave>${dummy("Connection Quality")}${dummy("Provider Lookup")}${dummy("Geolocation")}${CARD}</form></main></body></html>`;
const VIEWPORTS = [
	{ name: "1280x720", width: 1280, height: 720, scale: 1 },
	{ name: "390x844", width: 390, height: 844, scale: 1 },
	{ name: "320x640", width: 320, height: 640, scale: 1 },
	{ name: "1280x720-effective-zoom-200pct", width: 640, height: 360, scale: 2 }, // viewport proxy for zoom: 640x360 CSS px at DPR 2
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
			try {
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
						if (window.__fail) return { ok: false, status: 500, json: async () => ({}) };
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
				// The reason takes the dash's place in the value slot: no extra line, nothing overflowing the row.
				assert.equal(await text(page, '[data-egress-family="ipv6"] [data-egress-message]'), "No answer in time.");
				assert.equal(await page.locator('[data-egress-family="ipv6"] [data-egress-message]').getAttribute("title"), "The echo services did not answer in time.");
				assert.equal(await page.locator('[data-egress-family="ipv6"] [data-egress-address]').isVisible(), false);
				assert.equal(await page.locator('[data-egress-family="ipv6"] [data-egress-detail]').isVisible(), false);
				if (process.env.JUSTUP_EGRESS_SHOT_DIR) await page.locator("[data-public-egress]").screenshot({ path: path.join(process.env.JUSTUP_EGRESS_SHOT_DIR, `egress-message-${viewport.name}-${theme}.png`) });
				assert.equal(await page.evaluate(() => {
					const row = document.querySelector('[data-egress-family="ipv6"]').getBoundingClientRect();
					const inside = ["[data-egress-message]", ".ju-status-item", "[data-egress-copy]"].every((sel) => {
						const r = document.querySelector(`[data-egress-family="ipv6"] ${sel}`).getBoundingClientRect();
						return r.left >= row.left - 1 && r.right <= row.right + 1;
					});
					return inside && document.documentElement.scrollWidth <= window.innerWidth;
				}), true);
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
				// A cached result is labelled; a failing request keeps the cached addresses available and copyable.
				await page.evaluate(() => {
					const cached = { ...window.__data("93.184.216.34") };
					cached.ipv4 = { status: "available", address: "93.184.216.34", observed_at: "2026-11-05T19:00:00Z", sources: ["a"], observed: ["93.184.216.34"], reason: null, geo: { country_code: "DE", country: "Germany", city: "Bonn", asn: null, as_org: null } };
					cached.cached = true;
					window.__next = cached;
				});
				await page.click("[data-egress-refresh]");
				await page.waitForSelector('[data-egress-family="ipv4"] [data-egress-geo]:not([hidden])');
				assert.equal(await text(page, '[data-egress-family="ipv4"] [data-egress-address]'), "93.184.216.34");
				assert.equal(await page.locator('[data-egress-family="ipv4"] [data-egress-copy]').isDisabled(), false);
				assert.equal(await page.locator("[data-egress-cached]").isVisible(), true);
				await page.evaluate(() => { window.__fail = true; });
				await page.click("[data-egress-refresh]");
				await page.waitForFunction(() => document.querySelector("[data-egress-live]").textContent === "Public address lookup failed.");
				for (const family of ["ipv4", "ipv6"]) {
					const row = `[data-egress-family="${family}"]`;
					assert.equal(await page.locator(row).getAttribute("data-egress-state"), "available");
					assert.equal(await text(page, `${row} [data-egress-badge]`), "Available");
					assert.equal(await page.locator(`${row} [data-egress-copy]`).isDisabled(), false);
				}
				assert.equal(await text(page, '[data-egress-family="ipv6"] [data-egress-address]'), LONG_V6);
				assert.equal(await page.locator('[data-egress-family="ipv4"] [data-egress-geo]').isVisible(), true); // geo line kept
				assert.doesNotMatch(await page.locator("[data-public-egress]").innerText(), /stale|last confirmed/i);
				// Geometry: no overflow, long value stays inside the card, desktop position right of Geolocation.
				const geometry = await page.evaluate(() => {
					const de = document.documentElement;
					const rect = (el) => el.getBoundingClientRect();
					const cards = Array.from(document.querySelectorAll("#advanced-settings-form > .card")).map(rect);
					const address = rect(document.querySelector('[data-egress-family="ipv6"] [data-egress-address]'));
					const copy = rect(document.querySelector('[data-egress-family="ipv6"] [data-egress-copy]'));
					const card = cards[3];
					const scrollers = Array.from(document.querySelectorAll("[data-public-egress], [data-public-egress] *")).filter((el) => !el.closest(".visually-hidden") && el.scrollWidth > el.clientWidth + 1 && getComputedStyle(el).overflowX !== "visible" && getComputedStyle(el).textOverflow !== "ellipsis");
					return { overflow: de.scrollWidth > de.clientWidth + 1, addressInside: address.right <= card.right + 1 && address.left >= card.left - 1, copyInside: copy.right <= card.right + 1, copySize: Math.min(copy.width, copy.height), cards, innerScroll: scrollers.length };
				});
				assert.equal(geometry.overflow, false, "page overflows horizontally");
				assert.equal(geometry.addressInside, true, "IPv6 value leaves the card");
				assert.equal(geometry.copyInside, true, "copy button leaves the card");
				assert.ok(geometry.copySize >= 24, "copy target smaller than 24 px");
				assert.equal(geometry.innerScroll, 0, "inner scroll region");
				// DESIGN.md DC-CARD-1 (no collapse handle): every tile header is a plain header without a toggle.
				const headers = await page.evaluate(() => Array.from(document.querySelectorAll("#advanced-settings-form > .card > .card-header")).map((h) => ({
					toggle: Boolean(h.closest("details") || h.matches("summary") || h.querySelector("button, [aria-expanded], [aria-controls]")),
					height: Math.round(h.getBoundingClientRect().height),
					after: getComputedStyle(h, "::after").content,
					cursor: getComputedStyle(h).cursor,
				})));
				assert.equal(headers.length, 4);
				for (const h of headers) {
					assert.equal(h.toggle, false, "tile header carries a collapse toggle");
					assert.ok(h.after === "none" || h.after === "normal", "tile header draws a handle");
					assert.notEqual(h.cursor, "pointer", "tile header looks operable");
				}
				assert.ok(new Set(headers.map((h) => h.height)).size === 1, "tile headers differ in height");
				if (viewport.width >= 1280) {
					const [, provider, geo, egress] = geometry.cards;
					assert.ok(egress.left > geo.right - 2 || egress.left > geo.left + geo.width / 2, "card is not right of Geolocation");
					assert.ok(Math.abs(egress.top - geo.top) < 2, "card is not level with Geolocation");
					assert.ok(Math.abs(egress.bottom - geo.bottom) < 2, "cards in the same grid row must have equal height");
					assert.ok(egress.top >= provider.top + 1, "card is not below Provider Lookup");
				} else if (viewport.width <= 390) {
					const [, , geo, egress] = geometry.cards;
					assert.ok(egress.top >= geo.bottom - 1, "single column expected on narrow viewports");
					assert.ok(egress.height > geo.height, "stacked cards retain their natural content height");
				}
				// Both families share one structure: same columns, status and copy aligned, copy never under the address on a wide card.
				const rows = await page.evaluate(() => ["ipv4", "ipv6"].map((family) => {
					const row = document.querySelector(`[data-egress-family="${family}"]`);
					const rect = (selector) => row.querySelector(selector).getBoundingClientRect();
					const [label, address, status, copy] = [".egress-label", "[data-egress-address]", ".ju-status-item", "[data-egress-copy]"].map(rect);
					return { label, address, status, copy, statusWrapped: row.querySelector("[data-egress-badge]").getClientRects().length > 1 };
				}));
				const [r4, r6] = rows;
				for (const key of ["label", "status", "copy"]) {
					assert.ok(Math.abs(r4[key].left - r6[key].left) < 1.5, `${key} column differs between IPv4 and IPv6`);
					assert.ok(Math.abs(r4[key].width - r6[key].width) < 1.5, `${key} width differs between IPv4 and IPv6`);
				}
				assert.ok(Math.abs(r4.address.left - r6.address.left) < 1.5, "address column differs");
				for (const r of rows) {
					assert.equal(r.statusWrapped, false, "status wraps");
					assert.ok(r.copy.left >= r.address.left, "copy left of the address");
					assert.ok(r.label.top <= Math.min(r.address.top, r.status.top) + 1, "label not on the first line");
				}
				if (viewport.width >= 1280) {
					for (const r of rows) assert.ok(r.copy.top < r.address.bottom && r.status.top < r.address.bottom, "copy or status below the address on a wide card");
				}
				// Same pattern on both families, narrow or wide: the copy button sits below the status or level with it for both.
				assert.equal(r4.copy.top > r4.status.bottom - 1, r6.copy.top > r6.status.bottom - 1, "families use different patterns");
				// Nothing in the card can trigger the settings autosave.
				assert.equal(await page.locator("[data-public-egress] input, [data-public-egress] [name]").count(), 0);
				assert.deepEqual(errors, []);
			} finally {
				await context.close();
			}
		});
	}
}

// An unreachable app: only the "Lookup failed" badge per row, no detail line and no gap left by it.
for (const viewport of [{ name: "1024x768", width: 1024, height: 768 }, { name: "390x844", width: 390, height: 844 }]) {
	test(`Public outbound IPs: unreachable app shows only the badge at ${viewport.name} @public-egress`, async ({ browser, base }) => {
		const context = await browser.newContext({ viewport: { width: viewport.width, height: viewport.height } });
		try {
			const page = await context.newPage();
			await page.goto(base);
			await page.evaluate(async () => {
				window.apiFetch = async () => { throw new TypeError("offline"); };
				const module = await import("/static/js/settings.public-egress.js");
				module.initPublicEgress();
			});
			await page.waitForSelector('[data-egress-family="ipv6"][data-egress-state="failed"]');
			for (const family of ["ipv4", "ipv6"]) {
				const row = `[data-egress-family="${family}"]`;
				assert.equal(await text(page, `${row} [data-egress-badge]`), "Lookup failed");
				assert.equal(await page.locator(`${row} [data-egress-detail]`).textContent(), "");
				assert.equal(await page.locator(`${row} [data-egress-detail]`).isVisible(), false);
			}
			const shot = process.env.JUSTUP_EGRESS_SHOT_DIR;
			if (shot) await page.locator("[data-public-egress]").screenshot({ path: path.join(shot, `egress-failed-${viewport.name}.png`) });
		} finally {
			await context.close();
		}
	});
}
// Flag, ASN and place under the address: each part only when known, one muted line, no line without data.
const GEO_FULL = { country_code: "DE", country: "Germany", city: "Bonn", asn: "AS3320", as_org: "Deutsche Telekom AG" };
const GEO_LONG = { country_code: "DE", country: "Germany", city: "Frankfurt am Main", asn: "AS3320", as_org: "Deutsche Telekom AG with a very long operator name that cannot fit" };
for (const viewport of [{ name: "1024x768", width: 1024, height: 768 }, { name: "390x844", width: 390, height: 844 }]) {
	for (const theme of ["light", "dark"]) {
		test(`Public outbound IPs: geo line under the address at ${viewport.name} ${theme} @public-egress`, async ({ browser, base }) => {
			const context = await browser.newContext({ viewport: { width: viewport.width, height: viewport.height } });
			try {
				const page = await context.newPage();
				await page.goto(base);
				await page.evaluate(async ({ t, full }) => {
					document.documentElement.setAttribute("data-bs-theme", t);
					window.__copied = [];
					Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText: async (value) => { window.__copied.push(value); } } });
					const family = (address, geo) => ({ status: "available", address, observed_at: "2026-11-05T19:00:00Z", sources: ["a"], observed: [address], reason: null, geo });
					window.__family = family;
					window.__next = { ipv4: family("93.184.216.34", full), ipv6: { status: "unavailable", address: null, observed_at: null, sources: [], observed: [], reason: "no_route", geo: null }, checked_at: "2026-11-05T19:00:00Z", cached: false, proxy_env: false };
					window.apiFetch = async () => ({ ok: true, status: 200, json: async () => window.__next });
					const module = await import("/static/js/settings.public-egress.js");
					module.initPublicEgress();
				}, { t: theme, full: GEO_FULL });
				await page.waitForSelector('[data-egress-family="ipv4"][data-egress-state="available"]');
				const geo4 = '[data-egress-family="ipv4"] [data-egress-geo]';
				const state = () => page.evaluate(() => {
					const rect = (el) => el.getBoundingClientRect();
					const row = document.querySelector('[data-egress-family="ipv4"]');
					const geo = row.querySelector("[data-egress-geo]");
					const address = rect(row.querySelector("[data-egress-address]"));
					const out = { overflow: document.documentElement.scrollWidth > document.documentElement.clientWidth + 1 };
					if (!geo.hidden) {
						const g = rect(geo);
						const t = geo.querySelector(".egress-geo-text");
						out.below = g.top >= address.bottom - 1;
						out.aligned = Math.abs(g.left - address.left) < 1.5;
						out.height = g.height;
						out.inside = g.right <= rect(document.querySelector("[data-public-egress]")).right + 1;
						out.truncated = t.scrollWidth > t.clientWidth;
						out.size = parseFloat(getComputedStyle(geo).fontSize);
					}
					return out;
				});
				// 1) flag, ASN and place
				assert.equal(await page.locator(geo4).isVisible(), true);
				assert.equal(await page.locator(`${geo4} .egress-geo-text`).innerText(), "AS3320 Deutsche Telekom AG \u00b7 Bonn, Germany");
				assert.equal(await page.locator(geo4).getAttribute("title"), "AS3320 Deutsche Telekom AG \u00b7 Bonn, Germany");
				assert.equal(await page.locator(`${geo4} .egress-flag img`).getAttribute("src"), "https://cdn.jsdelivr.net/npm/flag-icons@7.3.2/flags/4x3/de.svg");
				assert.equal(await page.locator(`${geo4} .egress-flag`).getAttribute("aria-hidden"), "true"); // the country name is in the text
				let shown = await state();
				assert.deepEqual([shown.below, shown.aligned, shown.inside, shown.overflow], [true, true, true, false]);
				assert.ok(shown.height < 24 && shown.size <= 12.5, "geo line is a single small line");
				assert.equal(await page.locator('[data-egress-family="ipv6"] [data-egress-geo]').isVisible(), false); // unavailable row: no line
				assert.equal(await text(page, '[data-egress-family="ipv6"] [data-egress-message]'), "No IPv6 connectivity.");
				await page.focus('[data-egress-family="ipv4"] [data-egress-copy]');
				await page.keyboard.press("Enter");
				await page.waitForFunction(() => document.querySelector("[data-egress-live]").textContent === "IPv4 address copied.");
				assert.deepEqual(await page.evaluate(() => window.__copied), ["93.184.216.34"]); // copy stays the IP only
				const shot = process.env.JUSTUP_EGRESS_SHOT_DIR;
				if (shot) await page.locator("[data-public-egress]").screenshot({ path: path.join(shot, `egress-geo-full-${viewport.name}-${theme}.png`) });
				// 2) a long operator name is cut with an ellipsis, the full text stays in the title
				await page.evaluate((long) => { window.__next = { ...window.__next, ipv4: window.__family("93.184.216.34", long) }; }, GEO_LONG);
				await page.click("[data-egress-refresh]");
				await page.waitForFunction((txt) => document.querySelector('[data-egress-family="ipv4"] [data-egress-geo]').title.includes(txt), "very long operator name");
				shown = await state();
				assert.equal(shown.truncated, true);
				assert.deepEqual([shown.inside, shown.overflow], [true, false]);
				assert.ok(shown.height < 24, "long geo text must not wrap");
				if (shot) await page.locator("[data-public-egress]").screenshot({ path: path.join(shot, `egress-geo-long-${viewport.name}-${theme}.png`) });
				// 2b) ASN database absent: flag and place only, no ASN text, still one line without overflow
				await page.evaluate(() => { window.__next = { ...window.__next, ipv4: window.__family("93.184.216.34", { country_code: "DE", country: "Germany", city: "Bonn", asn: null, as_org: null }) }; });
				await page.click("[data-egress-refresh]");
				await page.waitForFunction(() => document.querySelector('[data-egress-family="ipv4"] .egress-geo-text')?.textContent === "Bonn, Germany");
				assert.equal(await page.locator(geo4).getAttribute("title"), "Bonn, Germany");
				assert.equal(await page.locator(`${geo4} .egress-flag img`).getAttribute("src"), "https://cdn.jsdelivr.net/npm/flag-icons@7.3.2/flags/4x3/de.svg");
				shown = await state();
				assert.deepEqual([shown.below, shown.aligned, shown.inside, shown.overflow, shown.truncated], [true, true, true, false, false]);
				assert.ok(shown.height < 24, "geo line without ASN must stay one line");
				if (shot) await page.locator("[data-public-egress]").screenshot({ path: path.join(shot, `egress-geo-no-asn-${viewport.name}-${theme}.png`) });
				// 3) only a country
				await page.evaluate(() => { window.__next = { ...window.__next, ipv4: window.__family("93.184.216.34", { country_code: "FR", country: "France", city: null, asn: null, as_org: null }) }; });
				await page.click("[data-egress-refresh]");
				await page.waitForFunction(() => document.querySelector('[data-egress-family="ipv4"] .egress-geo-text')?.textContent === "France");
				assert.equal(await page.locator(`${geo4} .egress-flag img`).getAttribute("src"), "https://cdn.jsdelivr.net/npm/flag-icons@7.3.2/flags/4x3/fr.svg");
				if (shot) await page.locator("[data-public-egress]").screenshot({ path: path.join(shot, `egress-geo-country-${viewport.name}-${theme}.png`) });
				// 4) nothing known: no line, no placeholder, no gap
				const before = await page.evaluate(() => document.querySelector('[data-egress-family="ipv4"]').getBoundingClientRect().height);
				await page.evaluate(() => { window.__next = { ...window.__next, ipv4: window.__family("93.184.216.34", null) }; });
				await page.click("[data-egress-refresh]");
				await page.waitForFunction(() => document.querySelector('[data-egress-family="ipv4"] [data-egress-geo]').hidden);
				assert.equal(await page.locator(geo4).isVisible(), false);
				assert.equal(await page.locator(geo4).innerText(), "");
				const after = await page.evaluate(() => document.querySelector('[data-egress-family="ipv4"]').getBoundingClientRect().height);
				assert.ok(after < before - 8, "an empty geo line leaves a gap");
				assert.equal(await text(page, '[data-egress-family="ipv4"] [data-egress-address]'), "93.184.216.34");
				if (shot) await page.locator("[data-public-egress]").screenshot({ path: path.join(shot, `egress-geo-none-${viewport.name}-${theme}.png`) });
			} finally {
				await context.close();
			}
		});
	}
}
// DC-EGRESS-6 / DC-STATE-1: "Checking..." is state S6 (word only). The served markup shows no dot before the script ran,
// so a stalled first request cannot leave a neutral dot next to the word.
test("initial Checking state shows the word without a dot @public-egress", async ({ page, base }) => {
	await page.goto(base);
	const rows = page.locator("[data-egress-family]");
	assert.equal(await rows.count(), 2);
	for (let i = 0; i < 2; i += 1) {
		const row = rows.nth(i);
		assert.match(await row.locator("[data-egress-badge]").innerText(), /^Checking/);
		assert.equal(await row.locator("[data-egress-dot]").isVisible(), false, "no dot while checking");
	}
});
