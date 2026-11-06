//
// tests/layout/target-endpoints.spec.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

// Target detail, Dashboard tab: every enabled check is a badge in the Checks hero and there is no "Network
// endpoints" card. The cards of the tab stack with one gutter between them (no band left by the removed
// card), many ports wrap inside the hero without horizontal overflow or clipped badges, and a live
// (HTMX) refresh updates the badges without bringing a table back. The generic layout audit looks for
// overflow, clipping and overlap, so an oversized empty band between two cards passes it; this spec
// measures the gaps themselves.
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { test } from "./fixtures.mjs";
import { waitForVisualStability } from "./harness.mjs";
import { readStateBadges } from "./test-support.mjs";
const repo = resolve(dirname(fileURLToPath(import.meta.url)), "../..");
const VIEWPORTS = [
	{ name: "1920x1080", width: 1920, height: 1080 },
	{ name: "1280x800", width: 1280, height: 800 },
	{ name: "768x1024", width: 768, height: 1024 },
	{ name: "390x844", width: 390, height: 844 },
];
const MANY = Array.from({ length: 36 }, (_, index) => 2000 + index);
const TARGETS = {
	tcp25: { name: "Endpoint TCP25", host: "tcp25.example.net", enable_tcp_connect: true, tcp_host: "tcp25.example.net", tcp_ports: "25,587" },
	multi: { name: "Endpoint Multi", host: "multi.example.net", enable_ping: true, enable_tcp_connect: true, tcp_host: "multi.example.net", tcp_ports: "53,25", enable_udp_scan: true, udp_ports: "53,161" },
	none: { name: "Endpoint None", host: "none.example.net", enable_http_check: true, http_url: "https://none.example.net/" },
	many: { name: "Endpoint Many", host: "many.example.net", enable_tcp_connect: true, tcp_host: "many.example.net", tcp_ports: MANY.join(",") },
	live: { name: "Endpoint Live", host: "live.example.net", is_enabled: false, enable_ping: false, enable_tcp_connect: true, tcp_host: "live.example.net", tcp_ports: "25,587" },
};
const EXPECTED_BADGES = {
	tcp25: ["TCP:25", "TCP:587"],
	multi: ["PING", "TCP:25", "TCP:53", "UDP:53", "UDP:161"],
	none: ["HTTP", "CERT"],
	many: MANY.map((port) => `TCP:${port}`),
	live: ["TCP:25", "TCP:587"],
};
const GUTTER = 24; // --target-detail-gap: 1.5rem
const TOLERANCE = 2;
test.describe.configure({ mode: "serial" });
let ids;
const python = (app, script) => {
	const result = spawnSync(app.python, ["-c", script], { cwd: repo, env: { ...process.env, JUSTUP_DATA_DIR: app.data }, encoding: "utf8" });
	assert.equal(result.status, 0, result.stderr);
};
const setPort = (app, id, port, status) => python(app, `from app.db import sqlite as sqlite_db
from app.utils.config import load_config
conn = sqlite_db.connect(load_config().db_path)
sqlite_db.update_network_port_status(conn, ${id}, "tcp", ${port}, "${status}")
conn.commit()
`);
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
	setPort(app, ids.live, 25, "up");
	setPort(app, ids.live, 587, "down");
});
const badgeStates = async (page) => Object.fromEntries((await readStateBadges(page, "#target-info-cards .target-kpi-badges .badge")).map((badge) => [badge.label, badge.state]));
for (const viewport of VIEWPORTS) {
	for (const theme of ["light", "dark"]) {
		for (const key of ["tcp25", "multi", "none", "many"]) {
			test(`checks hero and card gaps for ${key} at ${viewport.name} ${theme} @target-endpoints`, async ({ browser, app, adminState }) => {
				const context = await browser.newContext({ viewport: { width: viewport.width, height: viewport.height }, storageState: adminState });
				const page = await context.newPage();
				const errors = [];
				page.on("pageerror", (error) => errors.push(String(error)));
				try {
					await page.goto(`${app.base}/ui/targets/${ids[key]}`, { waitUntil: "load" });
					await page.evaluate((mode) => document.documentElement.setAttribute("data-bs-theme", mode), theme);
					await page.waitForFunction(() => document.querySelector("#combined-card") && !document.querySelector('.target-detail-page [aria-busy="true"]'), null, { timeout: 15000 });
					await waitForVisualStability(page);
					const result = await page.evaluate(() => {
						const box = (element) => {
							const rect = element.getBoundingClientRect();
							return { top: rect.top, bottom: rect.bottom, left: rect.left, right: rect.right, height: rect.height, width: rect.width };
						};
						const labelOf = (badge) => {
							const clone = badge.cloneNode(true);
							clone.querySelectorAll(".material-icons, .visually-hidden").forEach((node) => node.remove());
							return clone.textContent.trim();
						};
						const hero = document.querySelector("#target-info-cards .target-kpi-badges");
						const heroBox = box(hero);
						const badges = [...hero.querySelectorAll(".badge")].map((badge) => ({ label: labelOf(badge), box: box(badge) }));
						return {
							table: Boolean(document.querySelector(".target-endpoints-card, #target-network-endpoints-title, [data-endpoint]")),
							text: document.body.innerText,
							badges,
							hero: heroBox,
							toolbar: box(document.querySelector("#target-dashboard-pane > .target-toolbar")),
							combined: box(document.getElementById("combined-card")),
							kpi: box(document.querySelector("#target-dashboard-pane .target-kpi-row")),
							scrollX: document.documentElement.scrollWidth - document.documentElement.clientWidth,
							heroScrollX: hero.scrollWidth - hero.clientWidth,
						};
					});
					assert.deepEqual(errors, []);
					assert.equal(result.table, false, "no endpoint card or table");
					assert.doesNotMatch(result.text, /Network endpoints|Waiting for first measurement|No port measurements yet/);
					assert.deepEqual(result.badges.map((badge) => badge.label), EXPECTED_BADGES[key]);
					assert.equal(result.scrollX <= 0, true, `horizontal page overflow ${result.scrollX}px`);
					assert.equal(result.heroScrollX <= 0, true, `horizontal hero overflow ${result.heroScrollX}px`);
					for (const badge of result.badges) {
						assert.equal(badge.box.left >= result.hero.left - 1 && badge.box.right <= result.hero.right + 1, true, `badge ${badge.label} sits inside the hero`);
					}
					if (key === "many") {
						const rows = new Set(result.badges.map((badge) => Math.round(badge.box.top)));
						assert.equal(rows.size > 1, true, "36 ports wrap onto several rows");
					}
					const chain = [result.kpi, result.toolbar, result.combined];
					for (let index = 1; index < chain.length; index += 1) {
						const gap = chain[index].top - chain[index - 1].bottom;
						assert.equal(gap >= -TOLERANCE && gap <= GUTTER + TOLERANCE, true, `gap ${index} between stacked cards is ${Math.round(gap)}px, expected at most one ${GUTTER}px gutter`);
					}
					if (viewport.width >= 1200 && viewport.height >= 800) {
						assert.equal(result.combined.height >= 300, true, `chart row keeps its height: ${Math.round(result.combined.height)}px`);
					}
				} finally {
					await context.close();
				}
			});
		}
	}
}
test("live refresh updates the badges and never brings an endpoint table back @target-endpoints", async ({ browser, app, adminState }) => {
	const context = await browser.newContext({ viewport: { width: 1280, height: 800 }, storageState: adminState });
	const page = await context.newPage();
	try {
		await page.goto(`${app.base}/ui/targets/${ids.live}`, { waitUntil: "load" });
		await page.locator("#target-info-cards .target-kpi-badges .badge").first().waitFor();
		assert.deepEqual(await badgeStates(page), { "TCP:25": "Up", "TCP:587": "Down" });
		setPort(app, ids.live, 587, "up");
		setPort(app, ids.live, 25, "unknown");
		await page.evaluate(() => window.htmx.trigger(document.body, "justup-live-sync"));
		await page.waitForFunction(() => [...document.querySelectorAll("#target-info-cards .target-kpi-badges .badge")].some((badge) => badge.textContent.includes("TCP:25") && badge.textContent.includes("Unknown")), null, { timeout: 15000 });
		assert.deepEqual(await badgeStates(page), { "TCP:25": "Unknown", "TCP:587": "Up" });
		assert.equal(await page.locator(".target-endpoints-card, [data-endpoint]").count(), 0);
	} finally {
		await context.close();
	}
});
