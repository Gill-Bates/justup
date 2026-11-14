//
// tests/layout/dashboard-widget-values.spec.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

// Browser run of the values the dashboard widgets render (the served scripts, mocked server answers):
//   D4         the uptime tile never shows 100.00 % for a series below 100 %, and shows what the server sent otherwise
//   DC-DASH-5  numeric hero values read --ju-dashboard-value-size; the larger values fit the registry minimum, which does not grow
//   DC-DASH-11 widget content adapts to its own container, and long labels, large values and empty data stay inside the tile
import assert from "node:assert/strict";
import { test } from "./fixtures.mjs";
import { waitForVisualStability } from "./harness.mjs";
const NOW = () => new Date().toISOString();
const monitorTargets = (name) => [
	{ name, is_enabled: true, enable_cert_expiration: true, enable_http_check: true, interval_seconds: 60, status: { cert_days_left: 12, cert_checked_at: NOW(), http_latency_ms: 812, updated_at: NOW() } },
	{ name: `${name}-2`, is_enabled: true, enable_cert_expiration: true, enable_http_check: true, interval_seconds: 60, status: { cert_days_left: 90, cert_checked_at: NOW(), http_latency_ms: 120, updated_at: "2020-01-01T00:00:00Z" } },
];
const SERVER = {
	alerts: { open: 2, acknowledged_24h: 1, started_24h: 5, resolved_24h: 3 },
	uptime: { uptime_percent: 99.2, coverage_percent: 100 },
	targets: monitorTargets("alpha.example"),
	quality: [{ ts: new Date(Date.now() - 600000).toISOString(), score: 91 }, { ts: NOW(), score: 94 }],
};
// Every widget visible, placed in rows at `size(widget)` (minimum size by default), so the registry minimum is what is measured.
async function open(browser, app, adminState, { viewport = { width: 1280, height: 720 }, server = SERVER, size = (widget) => ({ w: widget.minW, h: widget.minH }), textScale = 1 } = {}) {
	const context = await browser.newContext({ viewport, storageState: adminState });
	const page = await context.newPage();
	const errors = [];
	page.on("pageerror", (error) => errors.push(String(error)));
	await page.goto(`${app.base}/`, { waitUntil: "load" });
	const layout = await page.evaluate((spec) => {
		const pick = new Function("widget", `return (${spec})(widget)`);
		let x = 0; let y = 0; let rowHeight = 0;
		const widgets = window.JuDashboardRegistry.forRole(true).map((widget) => {
			const { w, h } = pick(widget);
			if (x + w > 12) { x = 0; y += rowHeight; rowHeight = 0; }
			const item = { id: widget.id, x, y, w, h, visible: true, height_mode: "manual" };
			x += w; rowHeight = Math.max(rowHeight, h);
			return item;
		});
		return { version: 2, widgets };
	}, size.toString());
	const answers = { server };
	await page.route(/\/me\/dashboard-layout$/, (route) => (route.request().method() === "GET" ? route.fulfill({ json: { layout } }) : route.continue()));
	await page.route("**/alerts/summary**", (route) => route.fulfill({ json: answers.server.alerts }));
	await page.route("**/settings/notification-queue/stats**", (route) => answers.server.queue ? route.fulfill({ json: answers.server.queue }) : route.fallback());
	await page.route("**/me/dashboard-layout/uptime", (route) => route.fulfill({ json: answers.server.uptime }));
	await page.route("**/me/dashboard-layout/monitors", (route) => route.fulfill({ json: { targets: answers.server.targets } }));
	await page.route("**/quality/history**", (route) => route.fulfill({ json: answers.server.quality }));
	const load = async () => {
		await page.reload({ waitUntil: "load" });
		await page.waitForFunction(() => {
			const grid = document.getElementById("dashboard-grid");
			return grid?.gridstack && !grid.classList.contains("dashboard-grid--loading");
		}, null, { timeout: 15000 });
		if (textScale !== 1) await page.addStyleTag({ content: `html:root { font-size: ${textScale * 100}% !important; }` });
		// Every monitoring tile has left its loading state: a number, metrics or a row exists and the status is settled.
		await page.waitForFunction(() => [...document.querySelectorAll(".dashboard-monitor-body")].every((body) => body.children.length > 0 && !body.querySelector("[aria-busy=true]")) &&
			[...document.querySelectorAll(".dashboard-monitor-status")].every((status) => !status.hasAttribute("aria-busy")), null, { timeout: 15000 });
		await waitForVisualStability(page);
	};
	await load();
	return { page, context, answers, load, errors, layout };
}
const uptimeText = (page) => page.evaluate(() => ({
	number: document.querySelector('[data-widget-id="uptime-day"] .dashboard-monitor-number')?.textContent.trim() ?? null,
	progress: document.querySelector('[data-widget-id="uptime-day"] progress')?.value ?? null,
	rows: [...document.querySelectorAll('[data-widget-id="uptime-day"] .dashboard-monitor-row')].map((row) => row.textContent.replace(/\s+/g, " ").trim()),
}));
// D4: the uptime figure in the browser, for values on both sides of the rounding edge.
test("the uptime tile renders truncated, never inflated, percentages in the browser @dashboard-values", async ({ browser, app, adminState }) => {
	const view = await open(browser, app, adminState);
	try {
		const cases = [
			[99.2, "99.20%"], [99.95, "99.95%"], [99.96, "99.96%"], [99.996, "99.99%"], [99.9999, "99.99%"], [100, "100.00%"], [0, "0.00%"],
		];
		for (const [sent, shown] of cases) {
			view.answers.server = { ...SERVER, uptime: { uptime_percent: sent, coverage_percent: 100 } };
			await view.load();
			const state = await uptimeText(view.page);
			assert.equal(state.number, shown, `the server sent ${sent}`);
			assert.equal(state.progress, sent, `the progress bar keeps the exact value for ${sent}`);
			if (sent < 100) assert.notEqual(parseFloat(state.number), 100, `${sent} was shown as 100 %`);
			assert.ok(state.rows.some((row) => row.includes("Measurement coverage over the last 24 hours") && row.includes("100.0%")), "the coverage row shows one decimal");
		}
		// Too little coverage: no figure at all, a word instead.
		view.answers.server = { ...SERVER, uptime: { uptime_percent: 99.9, coverage_percent: 80 } };
		await view.load();
		const sparse = await uptimeText(view.page);
		assert.equal(sparse.number, null, "no percentage below 95 % coverage");
		assert.ok(sparse.rows.some((row) => row.includes("Not enough uptime data")), `rows: ${JSON.stringify(sparse.rows)}`);
		assert.ok(sparse.rows.some((row) => row.includes("80.0%")), "the coverage row still shows the coverage");
		assert.deepEqual(view.errors, []);
	} finally {
		await view.context.close();
	}
});
const fontPx = (page, selector) => page.evaluate((sel) => [...document.querySelectorAll(sel)].map((el) => ({ id: el.closest("[data-widget-id]")?.dataset.widgetId, size: parseFloat(getComputedStyle(el).fontSize) })), selector);
const tokenPx = (page) => page.evaluate(() => {
	const probe = document.createElement("span");
	probe.style.fontSize = "var(--ju-dashboard-value-size)";
	document.getElementById("dashboard-grid").append(probe);
	const size = parseFloat(getComputedStyle(probe).fontSize);
	probe.remove();
	return size;
});
// DC-DASH-5: the hero values and the single large number of a monitoring tile read the dashboard value token; the tile keeps its size.
test("dashboard values read the value token and fit the registry minimum @dashboard-values", async ({ browser, app, adminState }) => {
	const view = await open(browser, app, adminState);
	try {
		const token = await tokenPx(view.page);
		assert.equal(token, 40, "--ju-dashboard-value-size resolves to 2.5rem at the default root size");
		const heroes = await fontPx(view.page, ".dashboard-widget-value");
		assert.ok(heroes.length >= 5, `hero values found: ${heroes.length}`);
		for (const hero of heroes) assert.equal(hero.size, token, `${hero.id}: hero value size`);
		// The Queue and Alerts counts (dd) have the Quality value's computed size, weight and line height, and stay inside their tile.
		const quality = await view.page.evaluate(() => { const s = getComputedStyle(document.getElementById("quality-score")); return { size: s.fontSize, weight: s.fontWeight, line: s.lineHeight }; });
		const counts = await view.page.evaluate(() => ["delivery-queue", "alerts-open"].flatMap((id) => [...document.querySelectorAll(`[data-widget-id="${id}"] .dashboard-monitor-metrics dd`)].map((dd) => {
			const s = getComputedStyle(dd); const box = dd.closest(".grid-stack-item").getBoundingClientRect(); const r = dd.getBoundingClientRect();
			return { id, size: s.fontSize, weight: s.fontWeight, line: s.lineHeight, inside: r.left >= box.left - 1 && r.right <= box.right + 1 && r.top >= box.top - 1 && r.bottom <= box.bottom + 1 };
		})));
		assert.ok(counts.length >= 6, `count values found: ${counts.length}`);
		for (const count of counts) {
			assert.equal(count.size, quality.size, `${count.id}: count font size equals the Quality value`);
			assert.equal(count.weight, quality.weight, `${count.id}: count weight`);
			assert.equal(count.line, quality.line, `${count.id}: count line height`);
			assert.ok(count.inside, `${count.id}: a count leaves its tile`);
		}
		// Supporting labels keep their own, smaller size.
		for (const label of await fontPx(view.page, ".dashboard-widget-label")) assert.ok(label.size < token, `${label.id}: a supporting label is not value-sized`);
		// The registry minimum is the geometry: no tile grew to hold its content, nothing is clipped, no value leaves the tile.
		const tiles = await view.page.evaluate(() => [...document.querySelectorAll(".grid-stack-item:not(.d-none)")].map((tile) => {
			const frame = tile.querySelector(".grid-stack-item-content");
			const box = tile.getBoundingClientRect();
			const registry = window.JuDashboardRegistry.registry.find((entry) => entry.id === tile.dataset.widgetId);
			const body = tile.querySelector(".dashboard-widget-body");
			const values = [...tile.querySelectorAll(".dashboard-widget-value, .dashboard-monitor-number")].map((value) => {
				const rect = value.getBoundingClientRect();
				const view = (value.closest(".dashboard-widget-body") ?? tile).getBoundingClientRect();
				return rect.left >= box.left - 1 && rect.right <= box.right + 1 && rect.top >= box.top - 1 && rect.bottom <= box.bottom + 1 && rect.bottom <= view.bottom + 1 && value.scrollWidth <= value.clientWidth + 1;
			});
			return { id: tile.dataset.widgetId, w: tile.gridstackNode.w, h: tile.gridstackNode.h, minW: registry.minW, minH: registry.minH, overflowX: frame.scrollWidth - frame.clientWidth, overflowY: frame.scrollHeight - frame.clientHeight, bodyY: body ? body.scrollHeight - body.clientHeight : 0, bodyOverflow: body ? getComputedStyle(body).overflowY : "", valuesInside: values.every(Boolean) };
		}));
		assert.ok(tiles.length >= 12, `visible tiles: ${tiles.length}`);
		for (const tile of tiles) {
			assert.equal(tile.w, tile.minW, `${tile.id}: width stays at the registry minimum`);
			assert.equal(tile.h, tile.minH, `${tile.id}: height stays at the registry minimum`);
			assert.ok(tile.overflowX <= 1 && tile.overflowY <= 2, `${tile.id}: content overflows its tile (${tile.overflowX}x${tile.overflowY})`);
			assert.ok(tile.valuesInside, `${tile.id}: a value leaves its tile`);
			// DC-DASH-10: a body may scroll at the minimum (registered region); the large value itself is visible without scrolling.
			if (tile.bodyY > 1) assert.equal(tile.bodyOverflow, "auto", `${tile.id}: a body that scrolls is the registered auto region`);
		}
		assert.deepEqual(view.errors, []);
	} finally {
		await view.context.close();
	}
});
// DC-DASH-5 on a monitoring tile whose own container is wide enough for the standard presentation (DC-DASH-11 compact step is not active).
test("a standard-width monitoring tile shows its number at the value token @dashboard-values", async ({ browser, app, adminState }) => {
	const view = await open(browser, app, adminState, { size: (widget) => ({ w: Math.max(widget.minW, 6), h: widget.minH }) });
	try {
		const token = await tokenPx(view.page);
		const numbers = await view.page.evaluate(() => [...document.querySelectorAll(".dashboard-monitor-number")].map((el) => ({
			id: el.closest("[data-widget-id]").dataset.widgetId, size: parseFloat(getComputedStyle(el).fontSize),
			container: el.closest(".dashboard-monitor-card").getBoundingClientRect().width / parseFloat(getComputedStyle(document.documentElement).fontSize),
		})));
		assert.ok(numbers.length >= 4, `monitoring numbers found: ${numbers.length}`);
		for (const item of numbers) {
			assert.ok(item.container > 22, `${item.id}: the tile is wider than the compact container step (${item.container}rem)`);
			assert.equal(item.size, token, `${item.id}: number size at standard width`);
		}
	} finally {
		await view.context.close();
	}
});
// DC-DASH-11: presentation follows the tile's own width, not the viewport.
test("a monitoring tile changes its presentation with its container at an unchanged viewport @dashboard-values", async ({ browser, app, adminState }) => {
	const narrow = await open(browser, app, adminState);
	const wide = await open(browser, app, adminState, { size: (widget) => ({ w: Math.max(widget.minW, 6), h: widget.minH }) });
	try {
		const read = (page) => page.evaluate(() => {
			const card = document.querySelector('[data-widget-id="uptime-day"] .dashboard-monitor-card');
			const body = document.querySelector('[data-widget-id="uptime-day"] .dashboard-monitor-number');
			return { containerType: getComputedStyle(card).containerType, viewport: innerWidth, number: parseFloat(getComputedStyle(body).fontSize), width: card.getBoundingClientRect().width };
		});
		const [a, b] = [await read(narrow.page), await read(wide.page)];
		assert.equal(a.containerType, "inline-size", "a monitoring card is a size container");
		assert.equal(a.viewport, b.viewport, "the viewport is the same");
		assert.ok(b.width > a.width, "the second tile is wider");
		assert.notEqual(a.number, b.number, "the presentation depends on the tile width alone");
	} finally {
		await narrow.context.close();
		await wide.context.close();
	}
});
// A 62 character host name without a break opportunity and a 100 character display name (the model limit) with spaces.
const STRESS_NAME = "a-rather-long-service-name.eu-central.prod.internal.example.com";
const SPACED_NAME = "Production payment gateway for the EU-West region, primary and secondary failover pair, read replica";
const STRESS = {
	alerts: { open: 12345, acknowledged_24h: 123456, started_24h: 234567, resolved_24h: 345678 },
	queue: { pending: 1234567, processing: 2345678, failed: 3456789, failed_recent: 3456789, stuck_processing: 0, processing_timeout_seconds: 300, oldest_pending: new Date(Date.now() - 60000).toISOString(), next_retry_at: null, services: { monitor_scheduler: { state: "running" } } },
	uptime: { uptime_percent: 100, coverage_percent: 100 },
	targets: [
		{ name: STRESS_NAME, is_enabled: true, enable_cert_expiration: true, enable_http_check: true, interval_seconds: 60, status: { cert_days_left: 12345, cert_checked_at: NOW(), http_latency_ms: 123456789, updated_at: NOW() } },
		{ name: SPACED_NAME, is_enabled: true, enable_cert_expiration: true, enable_http_check: true, interval_seconds: 60, status: { cert_days_left: 3, cert_checked_at: NOW(), http_latency_ms: 99999, updated_at: "2020-01-01T00:00:00Z" } },
	],
	quality: [{ ts: new Date(Date.now() - 600000).toISOString(), score: 100 }, { ts: NOW(), score: 100 }],
};
const EMPTY = { alerts: { open: 0, acknowledged_24h: 0, started_24h: 0, resolved_24h: 0 }, uptime: { uptime_percent: 99, coverage_percent: 10 }, targets: [], quality: [] };
// A widget body may scroll at the registry minimum (DC-DASH-10, registered region); then it has to be reachable by keyboard (DC-SCROLL-5).
const FOCUSABLE = "a[href], button:not([disabled]), input:not([disabled]):not([type=hidden]), select:not([disabled]), textarea:not([disabled]), summary, [tabindex]:not([tabindex='-1'])";
const fits = (page) => page.evaluate((focusable) => [...document.querySelectorAll(".grid-stack-item:not(.d-none)")].map((tile) => {
	const frame = tile.querySelector(".grid-stack-item-content");
	const body = tile.querySelector(".dashboard-widget-body");
	const box = tile.getBoundingClientRect();
	// The downtime table scrolls sideways inside its own viewport (a declared horizontal region); everything else stays inside.
	const outside = [...tile.querySelectorAll(".dashboard-widget-body *")].filter((el) => !el.closest(".ju-table-viewport")).filter((el) => {
		const rect = el.getBoundingClientRect();
		return rect.width > 0 && (rect.right > box.right + 1 || rect.left < box.left - 1);
	}).map((el) => el.className || el.tagName);
	const scrolls = Boolean(body) && ["auto", "scroll"].includes(getComputedStyle(body).overflowY) && body.scrollHeight - body.clientHeight > 2;
	const named = body && (body.hasAttribute("aria-label") || body.hasAttribute("aria-labelledby"));
	return {
		id: tile.dataset.widgetId, overflowX: frame.scrollWidth - frame.clientWidth, overflowY: frame.scrollHeight - frame.clientHeight,
		bodyX: body ? body.scrollWidth - body.clientWidth : 0, outside,
		scrolls, reachable: !scrolls || Boolean(body.querySelector(focusable)) || (body.getAttribute("tabindex") === "0" && named),
	};
}), FOCUSABLE);
for (const [name, server] of [["long labels and large values", STRESS], ["empty data", EMPTY]]) {
	test(`dashboard widgets at the registry minimum keep ${name} inside the tile @dashboard-values`, async ({ browser, app, adminState }) => {
		const view = await open(browser, app, adminState, { server: { ...SERVER, ...server } });
		try {
			const tiles = await fits(view.page);
			assert.ok(tiles.length >= 12, `visible tiles: ${tiles.length}`);
			for (const tile of tiles) {
				assert.ok(tile.overflowX <= 1, `${tile.id}: horizontal overflow ${tile.overflowX}px`);
				assert.ok(tile.overflowY <= 2, `${tile.id}: vertical overflow ${tile.overflowY}px`);
				assert.ok(tile.bodyX <= 1, `${tile.id}: body overflow ${tile.bodyX}px`);
				assert.ok(tile.reachable, `${tile.id}: the body scrolls but cannot be reached by keyboard (DC-SCROLL-5)`);
				assert.deepEqual(tile.outside, [], `${tile.id}: content outside the tile`);
			}
			if (server === STRESS) {
				const texts = await view.page.evaluate(() => Object.fromEntries(["alerts-open", "delivery-queue", "cert-expiry"].map((id) => [id, document.querySelector(`[data-widget-id="${id}"]`).textContent.replace(/\s+/g, " ")])));
				assert.ok(texts["alerts-open"].includes("345678") && texts["delivery-queue"].includes("1234567"), "large counts are rendered in full, not abbreviated or cut");
				assert.ok(texts["cert-expiry"].includes(STRESS_NAME), "a long name is rendered in full");
			}
			assert.deepEqual(view.errors, []);
		} finally {
			await view.context.close();
		}
	});
}
