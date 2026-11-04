//
// tests/layout/target-endpoints.spec.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//
// Target detail, "Network endpoints" card: it lists every configured endpoint (never the empty text while
// a check is configured) and the cards of the Dashboard tab stack with one gutter between them. The
// generic layout audit looks for overflow, clipping and overlap, so an oversized empty band between two
// cards (a card pushed into a 1fr grid row) passes it; this spec measures the gaps themselves.
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { test } from "./fixtures.mjs";

const repo = resolve(dirname(fileURLToPath(import.meta.url)), "../..");
const VIEWPORTS = [
	{ name: "1920x1080", width: 1920, height: 1080 },
	{ name: "1280x720", width: 1280, height: 720 },
	{ name: "390x844", width: 390, height: 844 },
];
const TARGETS = {
	tcp25: { name: "Endpoint TCP25", host: "tcp25.example.net", enable_tcp_connect: true, tcp_host: "tcp25.example.net", tcp_ports: "25" },
	multi: { name: "Endpoint Multi", host: "multi.example.net", enable_tcp_connect: true, tcp_host: "multi.example.net", tcp_ports: "22,25,443", enable_udp_scan: true, udp_ports: "53,161" },
	none: { name: "Endpoint None", host: "none.example.net", enable_http_check: true, http_url: "https://none.example.net/" },
};
const EXPECTED_ROWS = { tcp25: ["tcp:25"], multi: ["tcp:22", "tcp:25", "tcp:443", "udp:53", "udp:161"], none: [] };
const GUTTER = 24; // --target-detail-gap: 1.5rem
const TOLERANCE = 2;

test.describe.configure({ mode: "serial" });
let ids;

test.beforeAll(async ({ browser, app, adminState }) => {
	const context = await browser.newContext({ storageState: adminState });
	const page = await context.newPage();
	await page.goto(`${app.base}/`, { waitUntil: "load" });
	ids = await page.evaluate(async (targets) => {
		const created = {};
		for (const [key, body] of Object.entries(targets)) {
			const response = await window.apiFetch("/targets", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
			if (response.status !== 201) throw new Error(`${key}: HTTP ${response.status}`);
			created[key] = (await response.json()).id;
		}
		return created;
	}, TARGETS);
	await context.close();
	// A measured TCP:25 series, the one the charts plot.
	const script = `from datetime import datetime, timedelta, timezone
from app.db.tsdb import append_point
from app.utils.config import load_config
config = load_config()
now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
for index in range(60):
	at = now - timedelta(minutes=59 - index)
	append_point(config.tsdb_dir, target_id=${ids.tcp25}, metric="tcp_ms_25", value=19 + index % 5, retention_days=365, at=at)
	append_point(config.tsdb_dir, target_id=${ids.tcp25}, metric="tcp_up_25", value=1, retention_days=365, at=at)
`;
	const result = spawnSync(app.python, ["-c", script], { cwd: repo, env: { ...process.env, JUSTUP_DATA_DIR: app.data }, encoding: "utf8" });
	assert.equal(result.status, 0, result.stderr);
});

for (const viewport of VIEWPORTS) {
	for (const theme of ["light", "dark"]) {
		for (const key of Object.keys(TARGETS)) {
			test(`endpoint card and card gaps for ${key} at ${viewport.name} ${theme} @target-endpoints`, async ({ browser, app, adminState }) => {
				const context = await browser.newContext({ viewport: { width: viewport.width, height: viewport.height }, storageState: adminState });
				const page = await context.newPage();
				const errors = [];
				page.on("pageerror", (error) => errors.push(String(error)));
				try {
					await page.goto(`${app.base}/ui/targets/${ids[key]}`, { waitUntil: "load" });
					await page.evaluate((mode) => document.documentElement.setAttribute("data-bs-theme", mode), theme);
					await page.waitForFunction(() => document.querySelector("#combined-card") && !document.querySelector('.target-detail-page [aria-busy="true"]'), null, { timeout: 15000 });
					await page.waitForTimeout(800);
					const result = await page.evaluate(() => {
						const box = (element) => {
							const rect = element.getBoundingClientRect();
							return { top: rect.top, bottom: rect.bottom, left: rect.left, right: rect.right, height: rect.height, width: rect.width };
						};
						const card = document.getElementById("target-network-endpoints-title")?.closest("section");
						const toolbar = document.querySelector("#target-dashboard-pane > .target-toolbar");
						const combined = document.getElementById("combined-card");
						const kpi = document.querySelector("#target-dashboard-pane .target-kpi-row");
						return {
							rows: card ? [...card.querySelectorAll("tr[data-endpoint]")].map((row) => row.dataset.endpoint) : null,
							text: card ? card.innerText.replace(/\s+/g, " ") : "",
							card: card && box(card), toolbar: box(toolbar), combined: box(combined), kpi: box(kpi),
							scrollX: document.documentElement.scrollWidth - document.documentElement.clientWidth,
						};
					});
					assert.deepEqual(errors, []);
					assert.equal(result.scrollX <= 0, true, `horizontal overflow ${result.scrollX}px`);
					assert.doesNotMatch(result.text, /No port measurements yet/);
					assert.equal(Boolean(result.card), EXPECTED_ROWS[key].length > 0, "card shown exactly when an endpoint is configured");
					const chain = [result.kpi];
					if (result.card) {
						assert.deepEqual(result.rows, EXPECTED_ROWS[key]);
						assert.match(result.text, /Waiting for first measurement|Up|Down|Unknown/);
						// A configured endpoint with a status is never presented as waiting.
						assert.equal(result.card.width >= result.toolbar.width - TOLERANCE, true, `card spans the content width: ${result.card.width} vs ${result.toolbar.width}`);
						chain.push(result.card);
					}
					chain.push(result.toolbar, result.combined);
					for (let index = 1; index < chain.length; index += 1) {
						const gap = chain[index].top - chain[index - 1].bottom;
						assert.equal(gap >= -TOLERANCE && gap <= GUTTER + TOLERANCE, true, `gap ${index} between stacked cards is ${Math.round(gap)}px, expected at most one ${GUTTER}px gutter`);
					}
					if (viewport.width >= 1200 && viewport.height >= 800) {
						assert.equal(result.combined.height >= 300, true, `chart row keeps its height next to the endpoint card: ${Math.round(result.combined.height)}px`);
					}
				} finally {
					await context.close();
				}
			});
		}
	}
}
