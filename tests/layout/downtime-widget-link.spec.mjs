//
// tests/layout/downtime-widget-link.spec.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

// Dashboard "Downtime Overview (24h)": the whole row is one link to the target page, which opens with
// exactly the widget's range (inputs, preset select and the very first chart requests, no 24 h flash).
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { expect } from "@playwright/test";
import { test } from "./fixtures.mjs";
const repo = resolve(dirname(fileURLToPath(import.meta.url)), "../..");
const DAY = 86_400_000;
test.describe.configure({ mode: "serial" });
let targetId;
test.beforeAll(async ({ browser, app, adminState }) => {
	const context = await browser.newContext({ storageState: adminState });
	const page = await context.newPage();
	await page.goto(`${app.base}/`, { waitUntil: "load" });
	// Disabled, so the scheduler cannot overwrite the measurements written below.
	targetId = await page.evaluate(async () => {
		const body = { name: "Widget Link", host: "link.dt.example.net", is_enabled: false, enable_tcp_connect: true, tcp_host: "link.dt.example.net", tcp_ports: "25" };
		const response = await window.apiFetch("/targets", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
		if (response.status !== 201) throw new Error(`HTTP ${response.status}`);
		return (await response.json()).id;
	});
	await context.close();
	const script = `from datetime import datetime, timedelta, timezone
from app.db.tsdb import append_point
from app.utils.config import load_config
config = load_config()
now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
for step, value in enumerate((1, 0, 0, 0, 1)):
	append_point(config.tsdb_dir, target_id=${targetId}, metric="tcp_up_25", value=value, retention_days=365, at=now - timedelta(minutes=120 - step))
`;
	const result = spawnSync(app.python, ["-c", script], { cwd: repo, env: { ...process.env, JUSTUP_DATA_DIR: app.data }, encoding: "utf8" });
	assert.equal(result.status, 0, result.stderr);
});
/** Open a page that records every availability-series request (the first chart request of a load). */
async function open(browser, app, adminState) {
	const context = await browser.newContext({ viewport: { width: 1280, height: 900 }, storageState: adminState });
	const page = await context.newPage();
	const spans = [];
	spans.urls = [];
	page.on("request", (request) => {
		const url = new URL(request.url());
		if (!url.pathname.startsWith("/ui/fragments/availability-series/")) return;
		spans.urls.push(url.pathname + url.search);
		spans.push(Date.parse(url.searchParams.get("until")) - Date.parse(url.searchParams.get("since")));
	});
	return { context, page, spans };
}
async function openDashboardRow(page, app) {
	await page.route("**/dashboard/downtime**", (route) => route.fulfill({
		status: 200,
		contentType: "application/json",
		body: JSON.stringify({ targets: [{ target_id: targetId, name: "Widget Link", services: { tcp_up: { downtime_seconds: 120 } } }] }),
	}));
	await page.goto(`${app.base}/`, { waitUntil: "load" });
	const row = page.locator("#downtime-table-body tr").filter({
		has: page.locator(`a[href^="/ui/targets/${targetId}?"]`),
	});
	await row.waitFor({ timeout: 30_000 });
	return row;
}
/** Click the centre of a cell the way a user does; the row link's ::after overlays it, so Playwright's own hit-test would refuse. */
async function clickCell(cell, options = {}) {
	const hit = await cell.evaluate((el) => {
		const box = el.getBoundingClientRect();
		const top = document.elementFromPoint(box.x + box.width / 2, box.y + box.height / 2);
		return top && top.closest("a") ? top.closest("a").getAttribute("href") : null;
	});
	assert.match(hit ?? "", /^\/ui\/targets\/\d+\?range=24h$/, "the cell centre hits the row link");
	await cell.click({ force: true, ...options });
}
async function targetState(page) {
	await page.waitForFunction(() => !document.querySelector('.target-detail-page [aria-busy="true"]') && document.querySelector("#quick-range"), null, { timeout: 30_000 });
	return page.evaluate(() => ({
		url: location.pathname + location.search,
		preset: document.getElementById("quick-range").value,
		from: document.getElementById("range-from").value,
		to: document.getElementById("range-to").value,
	}));
}
const near = (span, expected) => Math.abs(span - expected) < 5 * 60_000;
test("clicking any cell of a widget row opens the target with the 24 h range @downtime-widget-link", async ({ browser, app, adminState }) => {
	const { context, page, spans } = await open(browser, app, adminState);
	try {
		const row = await openDashboardRow(page, app);
		assert.equal(await row.locator("a[href]").count(), 1, "one link per row");
		assert.match(await row.locator("a").getAttribute("href"), new RegExp(`^/ui/targets/${targetId}\\?range=24h$`));
		await clickCell(row.locator("td").last());
		await page.waitForURL(new RegExp(`/ui/targets/${targetId}(\\?|$)`));
		const state = await targetState(page);
		assert.equal(state.preset, "24h");
		assert.ok(spans.length > 0 && spans.every((span) => near(span, DAY)), `chart requests: ${spans}`);
		assert.doesNotMatch(state.url, /range=/, "the parameter is not kept in the address bar");
	} finally {
		await context.close();
	}
});
test("keyboard: the row link is focusable, shows a visible focus ring and Enter opens the target @downtime-widget-link", async ({ browser, app, adminState }) => {
	const { context, page } = await open(browser, app, adminState);
	try {
		const row = await openDashboardRow(page, app);
		const link = row.locator("a");
		// Press Tab without reading focus in between (WebKit's scroll-region stops react to polling);
		// the focus log must contain the row's single link as one of the stops.
		await page.evaluate(() => {
			window.__focusLog = [];
			document.addEventListener("focusin", (event) => window.__focusLog.push({ href: event.target.getAttribute("href"), ring: getComputedStyle(event.target, "::after").outlineStyle }), true);
		});
		await page.locator("#dashboard-edit-toggle").focus();
		for (let i = 0; i < 4; i++) await page.keyboard.press("Tab");
		const log = await page.evaluate(() => window.__focusLog);
		const stop = log.find((entry) => /^\/ui\/targets\/\d+\?range=24h$/.test(entry.href ?? ""));
		assert.ok(stop, `the row link is a tab stop: ${JSON.stringify(log)}`);
		assert.notEqual(stop.ring, "none", "the focus ring is drawn around the whole row");
		await link.focus();
		await page.keyboard.press("Enter");
		await page.waitForURL(new RegExp(`/ui/targets/${targetId}(\\?|$)`));
		assert.equal((await targetState(page)).preset, "24h");
	} finally {
		await context.close();
	}
});
test("middle click opens the target in a new tab @downtime-widget-link", async ({ browser, app, adminState }) => {
	const { context, page } = await open(browser, app, adminState);
	try {
		const row = await openDashboardRow(page, app);
		const popup = context.waitForEvent("page");
		await clickCell(row.locator("td").nth(1), { button: "middle" });
		const tab = await popup;
		await tab.waitForURL(new RegExp(`/ui/targets/${targetId}\\?range=24h`));
		assert.match(page.url(), /\/$/, "the dashboard stays where it is");
	} finally {
		await context.close();
	}
});
test("a range in the URL is applied before the first chart load; invalid input and no parameter give 24 h @downtime-widget-link", async ({ browser, app, adminState }) => {
	const cases = [
		{ query: "?range=7d", preset: "7d", span: 7 * DAY },
		{ query: "?range=6h&x=keep", preset: "6h", span: 6 * 3_600_000, keep: "x=keep" },
		{ query: `?from=${new Date(Date.now() - 3 * DAY).toISOString()}&to=${new Date(Date.now() - DAY).toISOString()}`, preset: "", span: 2 * DAY },
		{ query: "?range=%3Cscript%3Ealert(1)%3C%2Fscript%3E", preset: "24h", span: DAY },
		{ query: "?from=2020-01-01T00:00:00Z&to=2026-01-01T00:00:00Z", preset: "24h", span: DAY },
		{ query: "", preset: "24h", span: DAY },
	];
	for (const item of cases) {
		const { context, page, spans } = await open(browser, app, adminState);
		try {
			await page.goto(`${app.base}/ui/targets/${targetId}${item.query}`, { waitUntil: "load" });
			const state = await targetState(page);
			assert.equal(state.preset, item.preset, item.query);
			assert.ok(spans.length > 0 && spans.every((span) => near(span, item.span)), `${item.query}: chart requests ${spans}`);
			assert.equal(new Set(spans.urls).size, spans.urls.length, `${item.query}: no chart request is sent twice: ${spans.urls}`);
			assert.doesNotMatch(state.url, /range=|from=|to=/, item.query);
			if (item.keep) assert.ok(state.url.includes(item.keep), "other parameters survive");
			assert.equal(await page.locator("#main-content, body").first().evaluate((el) => el.innerHTML.includes("<script>alert")), false);
		} finally {
			await context.close();
		}
	}
});
test("a range from the URL holds only for that visit: zoom reset and PDF export follow the shown range, a reload gives 24 h @downtime-widget-link", async ({ browser, app, adminState }) => {
	const { context, page, spans } = await open(browser, app, adminState);
	const pdfSpans = [];
	// The request is recorded and refused, so no PDF job starts.
	await page.route("**/reports/targets/*/pdf/request**", (route) => {
		const url = new URL(route.request().url());
		pdfSpans.push(Date.parse(url.searchParams.get("until")) - Date.parse(url.searchParams.get("since")));
		return route.fulfill({ status: 500, contentType: "application/json", body: JSON.stringify({ detail: "refused by test" }) });
	});
	try {
		await page.goto(`${app.base}/ui/targets/${targetId}?range=7d`, { waitUntil: "load" });
		const shown = await targetState(page);
		assert.equal(shown.preset, "7d");
		assert.doesNotMatch(shown.url, /range=/);
		await page.waitForFunction(() => window.Chart.getChart(document.getElementById("chart-uptime"))?.scales?.x);
		const xScale = () => page.evaluate(() => {
			const { min, max } = window.Chart.getChart(document.getElementById("chart-uptime")).scales.x;
			return [min, max];
		});
		const [loadMin, loadMax] = await xScale();
		assert.ok(spans.length > 0 && spans.every((span) => near(span, 7 * DAY)), `chart requests: ${spans}`);
		// Zoom into the middle half, then reset: the chart returns to its load view, not to a URL value.
		await page.evaluate(([min, max]) => window.Chart.getChart(document.getElementById("chart-uptime")).zoomScale("x", { min: min + (max - min) / 4, max: max - (max - min) / 4 }, "none"), [loadMin, loadMax]);
		await expect.poll(async () => { const [a, b] = await xScale(); return b - a; }).toBeLessThan((loadMax - loadMin) * 0.75);
		await page.locator("#reset-zoom-btn").click();
		await expect.poll(xScale).toEqual([loadMin, loadMax]);
		const afterReset = await targetState(page);
		assert.deepEqual([afterReset.preset, afterReset.from, afterReset.to, afterReset.url], [shown.preset, shown.from, shown.to, shown.url]);
		await page.locator("#export-pdf-btn").click();
		await expect.poll(() => pdfSpans.length).toBeGreaterThan(0);
		assert.ok(pdfSpans.every((span) => near(span, 7 * DAY)), `PDF request spans: ${pdfSpans}`);
		spans.length = 0;
		await page.reload({ waitUntil: "load" });
		const reloaded = await targetState(page);
		assert.equal(reloaded.preset, "24h");
		assert.doesNotMatch(reloaded.url, /range=/);
		assert.ok(spans.length > 0 && spans.every((span) => near(span, DAY)), `reload chart requests: ${spans}`);
	} finally {
		await context.close();
	}
});
