//
// tests/layout/endpoint-state.spec.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

// Endpoint state on the target detail page and the targets table: ICMP apart, TCP and UDP told apart by
// protocol, UNKNOWN never presented like DOWN (text, icon and shape, not colour alone), and the downtime
// history keeping measured downtime, measurement gaps and UNKNOWN results as three presentations.
// The target is disabled so the scheduler cannot overwrite the measurements written here.
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { test } from "./fixtures.mjs";
const repo = resolve(dirname(fileURLToPath(import.meta.url)), "../..");
const VIEWPORTS = [
	{ name: "1280x720", width: 1280, height: 720 },
	{ name: "390x844", width: 390, height: 844 },
	{ name: "320x640", width: 320, height: 640 },
];
const BODY = {
	name: "Endpoint State Mixed", host: "state.example.net", is_enabled: false, enable_ping: true,
	enable_tcp_connect: true, tcp_host: "state.example.net", tcp_ports: "25,587",
	enable_udp_scan: true, udp_ports: "53,161",
};
test.describe.configure({ mode: "serial" });
let targetId;
test.beforeAll(async ({ browser, app, adminState }) => {
	const context = await browser.newContext({ storageState: adminState });
	const page = await context.newPage();
	await page.goto(`${app.base}/`, { waitUntil: "load" });
	targetId = await page.evaluate(async (body) => {
		const response = await window.apiFetch("/targets", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
		if (response.status !== 201) throw new Error(`HTTP ${response.status}`);
		return (await response.json()).id;
	}, BODY);
	await context.close();
	const script = `from datetime import datetime, timedelta, timezone
from app.db import sqlite as sqlite_db
from app.db.tsdb import append_point
from app.utils.config import load_config
config = load_config()
conn = sqlite_db.connect(config.db_path)
tid = ${targetId}
update = sqlite_db.update_network_port_status
update(conn, tid, "tcp", 25, "up", latency_ms=12.0)
update(conn, tid, "tcp", 587, "down", "connection refused")
update(conn, tid, "udp", 53, "up", raw_state="open")
update(conn, tid, "udp", 161, "unknown", "uncertain result", raw_state="open|filtered")
conn.execute("UPDATE target_network_ports SET service_name = 'smtp', service_method = 'probed' WHERE target_id = ? AND protocol = 'tcp' AND port = 25", (tid,))
conn.execute("UPDATE target_network_ports SET service_name = 'snmp', service_method = 'table' WHERE target_id = ? AND protocol = 'udp' AND port = 161", (tid,))
conn.commit()
now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
def put(metric, minutes_ago, value):
	append_point(config.tsdb_dir, target_id=tid, metric=metric, value=value, retention_days=365, at=now - timedelta(minutes=minutes_ago))
# Measured downtime: TCP 587 refused for three minutes.
for minutes_ago, value in ((110, 1), (109, 1), (108, 0), (107, 0), (106, 0), (105, 1), (104, 1)):
	put("tcp_up_587", minutes_ago, value)
# Measurement gap: UDP 53 answers, then nothing for about 40 minutes.
for minutes_ago in (100, 99):
	put("udp_up_53", minutes_ago, 1)
put("udp_up_53", 60, 1)
# UNKNOWN result: UDP 161 only ever open|filtered.
put("udp_unknown_161", 50, 1)
`;
	const result = spawnSync(app.python, ["-c", script], { cwd: repo, env: { ...process.env, JUSTUP_DATA_DIR: app.data }, encoding: "utf8" });
	assert.equal(result.status, 0, result.stderr);
});
for (const viewport of VIEWPORTS) {
	for (const theme of ["light", "dark"]) {
		test(`detail: check badges and the three unmeasured/down presentations at ${viewport.name} ${theme} @endpoint-state`, async ({ browser, app, adminState }) => {
			const context = await browser.newContext({ viewport: { width: viewport.width, height: viewport.height }, storageState: adminState });
			const page = await context.newPage();
			const errors = [];
			page.on("pageerror", (error) => errors.push(String(error)));
			try {
				await page.goto(`${app.base}/ui/targets/${targetId}`, { waitUntil: "load" });
				await page.evaluate((mode) => document.documentElement.setAttribute("data-bs-theme", mode), theme);
				const hero = page.locator("#target-info-cards .target-kpi-badges");
				await hero.locator(".badge", { hasText: "UDP:161" }).waitFor({ state: "attached" });
				await page.locator("#downtime-history-body .downtime-item").first().waitFor({ state: "attached", timeout: 20000 });
				assert.equal(await page.locator("[data-endpoint], .target-endpoints-card").count(), 0, "no endpoint table");
				const labels = await hero.locator(".badge").evaluateAll((nodes) => nodes.map((node) => {
					const clone = node.cloneNode(true);
					clone.querySelectorAll(".material-icons, .visually-hidden").forEach((part) => part.remove());
					return clone.textContent.trim();
				}));
				assert.deepEqual(labels, ["PING", "TCP:25", "TCP:587", "UDP:53", "UDP:161"]);
				const badge = (label) => hero.locator(".badge", { hasText: label });
				assert.match(await badge("TCP:25").innerText(), /Up/);
				assert.match(await badge("TCP:587").innerText(), /Down/);
				const unknownText = await badge("UDP:161").innerText();
				assert.match(unknownText, /Unknown/);
				assert.doesNotMatch(unknownText, /\bDown\b/);
				assert.doesNotMatch(await badge("TCP:587").innerText(), /Unknown/);
				// UNKNOWN differs from DOWN by colour, word and edge shape; no badge holds an icon.
				const look = (label) => badge(label).evaluate((node) => ({
					icons: node.querySelectorAll(".material-icons, svg, img").length,
					background: getComputedStyle(node).backgroundColor,
					border: getComputedStyle(node).borderTopStyle,
				}));
				const down = await look("TCP:587");
				const unk = await look("UDP:161");
				assert.equal(down.icons + unk.icons, 0, "no icon inside a status badge");
				assert.notEqual(down.background, unk.background);
				assert.equal(unk.border, "dashed");
				assert.notEqual(down.border, "dashed");
				// The three presentations of the downtime history.
				const kinds = await page.locator("#downtime-history-body .downtime-item").evaluateAll((items) => items.map((item) => ({
					kind: [...item.classList].find((name) => name.startsWith("downtime-item--") && name !== "downtime-item--ongoing").slice(15),
					text: item.querySelector(".downtime-kind").innerText.trim(),
					marker: getComputedStyle(item.querySelector(".downtime-marker")).borderTopStyle + "|" + getComputedStyle(item.querySelector(".downtime-marker")).transform,
				})));
				const byKind = Object.fromEntries(kinds.map((item) => [item.kind, item]));
				assert.deepEqual(Object.keys(byKind).sort(), ["down", "gap", "unknown"]);
				assert.match(byKind.down.text, /Downtime/);
				assert.match(byKind.gap.text, /No measurements/);
				assert.match(byKind.unknown.text, /Unknown/);
				assert.equal(new Set(Object.values(byKind).map((item) => item.marker)).size, 3, "three distinct marker shapes");
				const summary = await page.locator("#downtime-history-body .downtime-stats").innerText();
				assert.match(summary, /^1\s+Downtime\b/);
				assert.match(summary, /2\s+Data gaps/);
				assert.deepEqual(errors, []);
				assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1), true, "no horizontal page overflow");
			} finally {
				await context.close();
			}
		});
	}
}
test("targets table: endpoint badges carry the state as text, colour and edge, without an icon @endpoint-state", async ({ browser, app, adminState }) => {
	const context = await browser.newContext({ viewport: { width: 1280, height: 720 }, storageState: adminState });
	const page = await context.newPage();
	try {
		await page.goto(`${app.base}/ui/targets`, { waitUntil: "load" });
		const row = page.locator("tr[data-target-id]", { hasText: BODY.name });
		await row.waitFor();
		const badge = (label) => row.locator(".ju-state-badge", { hasText: label }).first();
		assert.match(await badge("TCP:587").innerText(), /Down/);
		assert.match(await badge("UDP:161").innerText(), /Unknown/);
		assert.match(await badge("UDP:53").innerText(), /Up/);
		assert.equal(await row.locator(".ju-state-badge .material-icons, .ju-state-badge svg").count(), 0, "no icon inside a status badge");
		const size = (label) => badge(label).evaluate((node) => { const box = node.getBoundingClientRect(); return [box.height]; });
		assert.deepEqual(await size("TCP:587"), await size("UDP:161"), "UNKNOWN and DOWN badges are equally high");
		assert.equal(await badge("UDP:161").evaluate((node) => getComputedStyle(node).borderTopStyle), "dashed");
		assert.equal(await row.locator(".ju-state-badge", { hasText: "TCP:587" }).count(), 1, "TCP and UDP of one port number never share a badge");
	} finally {
		await context.close();
	}
});
test("availability chart: a UDP port is a dashed line, a TCP port of the same number is not @endpoint-state", async ({ browser, app, adminState }) => {
	const context = await browser.newContext({ viewport: { width: 1280, height: 720 }, storageState: adminState });
	const page = await context.newPage();
	try {
		await page.goto(`${app.base}/ui/targets/${targetId}`, { waitUntil: "load" });
		await page.waitForFunction(() => window.Chart && window.Chart.getChart("chart-uptime")?.data.datasets.length > 0, null, { timeout: 20000 });
		const lines = await page.evaluate(() => Object.fromEntries(window.Chart.getChart("chart-uptime").data.datasets.map((set) => [set.label, set.borderDash || []])));
		assert.deepEqual(lines["UDP:53"], [6, 4]);
		assert.deepEqual(lines["TCP:587"], []);
	} finally {
		await context.close();
	}
});
