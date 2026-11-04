//
// tests/layout/downtime-history.spec.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

// Downtime card of the target details: compact events, one scroll area, no overlap or overflow in narrow cards.
// The targets are disabled so the scheduler cannot overwrite the measurements written here.
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdirSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { test } from "./fixtures.mjs";
const repo = resolve(dirname(fileURLToPath(import.meta.url)), "../..");
const VIEWPORTS = [
	{ name: "1280x720", width: 1280, height: 720 },
	{ name: "1024x768", width: 1024, height: 768 },
	{ name: "640x480", width: 640, height: 480 },
	{ name: "390x844", width: 390, height: 844 },
	{ name: "320x640", width: 320, height: 640 },
];
const PORTS = Array.from({ length: 12 }, (_, i) => 8001 + i).join(",");
const BODIES = {
	many: { name: "Downtime Many", host: "dt.example.net", is_enabled: false, enable_tcp_connect: true, tcp_host: "dt.example.net", tcp_ports: `25,${PORTS}` },
	few: { name: "Downtime Few", host: "dt.example.net", is_enabled: false, enable_tcp_connect: true, tcp_host: "dt.example.net", tcp_ports: "25" },
	gaps: { name: "Downtime Gaps", host: "dt.example.net", is_enabled: false, enable_tcp_connect: true, tcp_host: "dt.example.net", tcp_ports: "25" },
	nodata: { name: "Downtime NoData", host: "dt.example.net", is_enabled: false, enable_tcp_connect: true, tcp_host: "dt.example.net", tcp_ports: "25" },
};
test.describe.configure({ mode: "serial" });
const ids = {};
test.beforeAll(async ({ browser, app, adminState }) => {
	const context = await browser.newContext({ storageState: adminState });
	const page = await context.newPage();
	await page.goto(`${app.base}/`, { waitUntil: "load" });
	for (const [key, body] of Object.entries(BODIES)) {
		ids[key] = await page.evaluate(async (payload) => {
			const response = await window.apiFetch("/targets", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
			if (response.status !== 201) throw new Error(`HTTP ${response.status}`);
			return (await response.json()).id;
		}, body);
	}
	await context.close();
	const script = `from datetime import datetime, timedelta, timezone
from app.db.tsdb import append_point
from app.utils.config import load_config
config = load_config()
now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
def put(tid, metric, minutes_ago, value):
	append_point(config.tsdb_dir, target_id=tid, metric=metric, value=value, retention_days=365, at=now - timedelta(minutes=minutes_ago))
many, few, gaps = ${ids.many}, ${ids.few}, ${ids.gaps}
# Twelve short outages of all ports at once (long service list), one every 15 minutes, plus one gap.
for outage in range(12):
	base = 600 - outage * 15
	for port in [25] + list(range(8001, 8013)):
		for step, value in enumerate((1, 0, 0, 1)):
			put(many, f"tcp_up_{port}", base - step, value)
for minutes_ago in (900, 899):
	put(many, "tcp_up_25", minutes_ago, 1)
put(many, "tcp_up_25", 700, 1)
# One short outage and a long gap.
for step, value in enumerate((1, 0, 0, 0, 1)):
	put(few, "tcp_up_25", 300 - step, value)
# Only a gap.
for minutes_ago in (400, 399, 200):
	put(gaps, "tcp_up_25", minutes_ago, 1)
`;
	const result = spawnSync(app.python, ["-c", script], { cwd: repo, env: { ...process.env, JUSTUP_DATA_DIR: app.data }, encoding: "utf8" });
	assert.equal(result.status, 0, result.stderr);
});
async function open(browser, app, adminState, viewport, theme, key) {
	const context = await browser.newContext({ viewport: { width: viewport.width, height: viewport.height }, storageState: adminState });
	const page = await context.newPage();
	await page.goto(`${app.base}/ui/targets/${ids[key]}`, { waitUntil: "load" });
	await page.evaluate((mode) => document.documentElement.setAttribute("data-bs-theme", mode), theme);
	await page.locator("#downtime-history-body .downtime-panel").waitFor({ state: "attached", timeout: 20000 });
	return { context, page };
}
for (const viewport of VIEWPORTS) {
	for (const theme of ["light", "dark"]) {
		test(`downtime card: events fit, one scroll area at ${viewport.name} ${theme} @downtime-history`, async ({ browser, app, adminState }) => {
			const { context, page } = await open(browser, app, adminState, viewport, theme, "many");
			try {
				const shots = process.env.JUSTUP_DOWNTIME_SHOTS;
				if (shots) {
					mkdirSync(shots, { recursive: true });
					await page.locator("#downtime-history-card").screenshot({ path: `${shots}/after-many-${viewport.name}-${theme}.png` });
				}
				assert.match(await page.locator(".downtime-stats").innerText(), /\d+\s+Downtimes?/);
				const report = await page.evaluate(() => {
					const card = document.querySelector("#downtime-history-card");
					const rects = (el) => el.getBoundingClientRect();
					const overlap = (a, b) => a.left < b.right - 0.5 && b.left < a.right - 0.5 && a.top < b.bottom - 0.5 && b.top < a.bottom - 0.5;
					const problems = [];
					for (const item of card.querySelectorAll(".downtime-item")) {
						const box = rects(item);
						const parts = [...item.children].filter((c) => !c.matches(".downtime-marker") && c.textContent.trim()).map((c) => [c.className, rects(c)]);
						if (item.scrollWidth > item.clientWidth + 1) problems.push("item overflows");
						for (const [name, rect] of parts) if (rect.right > box.right + 1 || rect.left < box.left - 1) problems.push(`${name} outside item`);
						for (let i = 0; i < parts.length; i += 1) for (let j = i + 1; j < parts.length; j += 1) if (overlap(parts[i][1], parts[j][1])) problems.push(`${parts[i][0]} overlaps ${parts[j][0]}`);
					}
					const scrollers = [card, ...card.querySelectorAll("*")].filter((el) => /(auto|scroll)/.test(getComputedStyle(el).overflowY) && el.scrollHeight > el.clientHeight + 1);
					const scroll = card.querySelector(".downtime-scroll");
					return {
						problems, scrollers: scrollers.map((el) => el.className || el.id),
						summaryVisible: rects(card.querySelector(".downtime-head")).bottom <= rects(scroll).top + 1,
						pageOverflow: document.documentElement.scrollWidth > window.innerWidth + 1,
						markers: [...new Set([...card.querySelectorAll(".downtime-marker")].map((m) => getComputedStyle(m).borderTopStyle + getComputedStyle(m).transform))].length,
					};
				});
				assert.deepEqual(report.problems, []);
				assert.ok(report.scrollers.length <= 1 && report.scrollers.every((c) => String(c).includes("downtime-scroll")), `scroll areas: ${report.scrollers}`);
				assert.equal(report.summaryVisible, true);
				assert.equal(report.pageOverflow, false);
			} finally {
				await context.close();
			}
		});
	}
}
test("downtime card: compact event and mixed kinds @downtime-history", async ({ browser, app, adminState }) => {
	const { context, page } = await open(browser, app, adminState, VIEWPORTS[0], "light", "few");
	try {
		const item = page.locator(".downtime-item--down").first();
		await item.waitFor();
		const { height, lineHeight } = await item.evaluate((el) => ({ height: el.getBoundingClientRect().height, lineHeight: parseFloat(getComputedStyle(el).lineHeight) }));
		assert.ok(height <= lineHeight * 3 + 16, `simple event is ${height}px high`);
		assert.match(await item.innerText(), /Downtime[\s\S]*\d+ \w{3} · \d\d:\d\d[–]\d\d:\d\d[\s\S]*TCP:25/);
		assert.match(await page.locator(".downtime-info p").evaluate((p) => p.textContent), /neither as downtime nor as uptime/);
	} finally {
		await context.close();
	}
});
test("downtime card: gaps only claim no success, no data is neutral, range swap updates the header @downtime-history", async ({ browser, app, adminState }) => {
	const gaps = await open(browser, app, adminState, VIEWPORTS[0], "light", "gaps");
	try {
		const text = await gaps.page.locator("#downtime-history-body").innerText();
		assert.match(text, /0\s+Downtimes/);
		assert.match(text, /No measurements/);
		assert.doesNotMatch(text, /No outages/);
		const label = await gaps.page.locator("#downtime-header-label").innerText();
		await gaps.page.evaluate(async (id) => {
			const since = new Date(Date.now() - 3600_000).toISOString();
			await window.htmx.ajax("GET", `/ui/fragments/downtime-history/${id}?since=${since}&until=${new Date().toISOString()}`, { target: "#downtime-history-body", swap: "innerHTML" });
		}, ids.gaps);
		await gaps.page.locator("#downtime-history-body .downtime-empty").waitFor({ timeout: 10000 });
		assert.match(await gaps.page.locator("#downtime-history-body").innerText(), /No measurement data|No outages/);
		assert.notEqual(await gaps.page.locator("#downtime-header-label").innerText(), "");
		assert.ok(label.length > 0);
	} finally {
		await gaps.context.close();
	}
	const none = await open(browser, app, adminState, VIEWPORTS[0], "light", "nodata").catch(() => null);
	if (none) {
		try {
			const text = await none.page.locator("#downtime-history-body").innerText();
			assert.match(text, /No measurement data[\s\S]*No checks recorded in this range/);
			assert.doesNotMatch(text, /0\s+Downtimes/);
		} finally {
			await none.context.close();
		}
	}
});
