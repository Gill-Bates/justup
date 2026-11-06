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
	page.on("request", (request) => {
		const url = new URL(request.url());
		if (!url.pathname.startsWith("/ui/fragments/availability-series/")) return;
		spans.push(Date.parse(url.searchParams.get("until")) - Date.parse(url.searchParams.get("since")));
	});
	return { context, page, spans };
}
async function openDashboardRow(page, app) {
	await page.goto(`${app.base}/`, { waitUntil: "load" });
	const row = page.locator("#downtime-table-body tr").first();
	await row.waitFor({ timeout: 30_000 });
	return row;
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
		await row.locator("td").last().click();
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
		await link.focus();
		await page.keyboard.press("Tab");
		await page.keyboard.press("Shift+Tab");
		assert.equal(await link.evaluate((el) => document.activeElement === el), true);
		const outline = await link.evaluate((el) => getComputedStyle(el, "::after").outlineStyle);
		assert.notEqual(outline, "none", "the focus ring is drawn around the whole row");
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
		await row.locator("td").nth(1).click({ button: "middle" });
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
			assert.doesNotMatch(state.url, /range=|from=|to=/, item.query);
			if (item.keep) assert.ok(state.url.includes(item.keep), "other parameters survive");
			assert.equal(await page.locator("#main-content, body").first().evaluate((el) => el.innerHTML.includes("<script>alert")), false);
		} finally {
			await context.close();
		}
	}
});
