//
// tests/layout/endpoint-state.spec.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

// Endpoint state on the target detail page and the targets table: ICMP apart, TCP and UDP told apart by
// protocol, UNKNOWN never presented like DOWN (text and dot shape, not colour alone; DC-STATE-1, DC-STATE-2), and the downtime
// history keeping measured downtime, measurement gaps and UNKNOWN results as three presentations.
// The target is disabled so the scheduler cannot overwrite the measurements written here.
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { expect } from "@playwright/test";
import { test } from "./fixtures.mjs";
import { readStateBadges } from "./test-support.mjs";
const repo = resolve(dirname(fileURLToPath(import.meta.url)), "../..");
const VIEWPORTS = [
	{ name: "1280x720", width: 1280, height: 720 },
	{ name: "390x844", width: 390, height: 844 },
	{ name: "320x640", width: 320, height: 640 },
];
const BODY = {
	name: "Endpoint State Mixed", host: "state.example.net", is_enabled: false, enable_ping: true,
	enable_tcp_connect: true, tcp_host: "state.example.net", tcp_ports: "25,53,587",
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
update(conn, tid, "tcp", 53, "up", latency_ms=5.0)
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
put("tcp_up_53", 60, 1)
# UNKNOWN result: UDP 161 only ever open|filtered.
put("udp_unknown_161", 50, 1)
`;
	const result = spawnSync(app.python, ["-c", script], { cwd: repo, env: { ...process.env, JUSTUP_DATA_DIR: app.data }, encoding: "utf8" });
	assert.equal(result.status, 0, result.stderr);
});
for (const viewport of VIEWPORTS) {
	for (const theme of ["light", "dark"]) {
		test(`detail: check states and the three unmeasured/down presentations at ${viewport.name} ${theme} @endpoint-state`, async ({ browser, app, adminState }) => {
			const context = await browser.newContext({ viewport: { width: viewport.width, height: viewport.height }, storageState: adminState });
			const page = await context.newPage();
			const errors = [];
			page.on("pageerror", (error) => errors.push(String(error)));
			try {
				await page.goto(`${app.base}/ui/targets/${targetId}`, { waitUntil: "load" });
				await page.evaluate((mode) => document.documentElement.setAttribute("data-bs-theme", mode), theme);
				const hero = page.locator("#target-info-cards .target-kpi-badges");
				await hero.locator(".ju-status-item", { hasText: "UDP:161" }).waitFor({ state: "attached" });
				await page.locator("#downtime-history-body .downtime-item").first().waitFor({ state: "attached", timeout: 20000 });
				assert.equal(await page.locator("[data-endpoint], .target-endpoints-card").count(), 0, "no endpoint table");
				const labels = (await readStateBadges(page, "#target-info-cards .target-kpi-badges .ju-status-item")).map((item) => item.label);
				assert.deepEqual(labels, ["PING", "TCP:25", "TCP:53", "TCP:587", "UDP:53", "UDP:161"]);
				const badge = (label) => hero.locator(".ju-status-item", { hasText: label });
				assert.match(await badge("TCP:25").innerText(), /Up/);
				assert.match(await badge("TCP:587").innerText(), /Down/);
				const unknownText = await badge("UDP:161").innerText();
				assert.match(unknownText, /Unknown/);
				assert.doesNotMatch(unknownText, /\bDown\b/);
				assert.doesNotMatch(await badge("TCP:587").innerText(), /Unknown/);
				// UNKNOWN differs from DOWN by word and dot shape (hollow ring vs filled square); no icon inside the item.
				const look = (label) => badge(label).evaluate((node) => {
					const dot = node.querySelector(".ju-status-dot");
					const style = getComputedStyle(dot);
					return {
						icons: node.querySelectorAll(".material-icons, svg, img").length,
						ariaHidden: dot.getAttribute("aria-hidden"),
						background: style.backgroundColor,
						radius: style.borderTopLeftRadius,
					};
				});
				const down = await look("TCP:587");
				const unk = await look("UDP:161");
				assert.equal(down.icons + unk.icons, 0, "no icon inside a status item");
				assert.deepEqual([down.ariaHidden, unk.ariaHidden], ["true", "true"], "the dot is decorative, the word is the text alternative");
				assert.notEqual(down.background, unk.background);
				assert.equal(unk.background, "rgba(0, 0, 0, 0)", "UNKNOWN is a hollow ring");
				assert.notEqual(down.radius, unk.radius, "DOWN and UNKNOWN differ in shape");
				// The three presentations of the downtime history.
				const kinds = await page.locator("#downtime-history-body .downtime-item").evaluateAll((items) => items.map((item) => ({
					kind: [...item.classList].find((name) => name.startsWith("downtime-item--") && name !== "downtime-item--ongoing").slice(15),
					text: item.querySelector(".downtime-kind").innerText.trim(),
					marker: (({ borderTopStyle, borderTopLeftRadius, transform }) => ({ borderTopStyle, borderTopLeftRadius, transform }))(getComputedStyle(item.querySelector(".downtime-marker"))),
				})));
				const byKind = Object.fromEntries(kinds.map((item) => [item.kind, item]));
				assert.deepEqual(Object.keys(byKind).sort(), ["down", "gap", "unknown"]);
				assert.match(byKind.down.text, /Downtime/);
				assert.match(byKind.gap.text, /No measurements/);
				assert.match(byKind.unknown.text, /Unknown/);
				assert.equal(new Set(Object.values(byKind).map((item) => JSON.stringify(item.marker))).size, 3, "three distinct marker shapes");
				// DC-EX-DOWNTIME-1: filled circle, dashed circle, diamond (rotated square).
				assert.equal(byKind.down.marker.borderTopStyle, "solid", "downtime marker is a filled circle");
				assert.equal(byKind.gap.marker.borderTopStyle, "dashed", "data-gap marker is a dashed circle");
				assert.equal(byKind.unknown.marker.transform === "none", false, "unknown marker is rotated into a diamond");
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
test("targets table: endpoint states are a coloured dot with a text alternative, no badge @endpoint-state", async ({ browser, app, adminState }) => {
	const context = await browser.newContext({ viewport: { width: 1280, height: 720 }, storageState: adminState });
	const page = await context.newPage();
	try {
		await page.goto(`${app.base}/ui/targets`, { waitUntil: "load" });
		const row = page.locator("tr[data-target-id]", { hasText: BODY.name });
		await row.waitFor();
		const badge = (label) => row.locator(".ju-status-item", { hasText: label }).first();
		assert.match(await badge("TCP:587").innerText(), /Down/);
		assert.match(await badge("UDP:161").innerText(), /Unknown/);
		assert.match(await badge("UDP:53").innerText(), /Up/);
		assert.equal(await row.locator(".ju-status-item .material-icons, .ju-status-item svg, .ju-status-item .badge").count(), 0, "no icon and no badge inside a status item");
		const size = (label) => badge(label).evaluate((node) => { const box = node.getBoundingClientRect(); return [box.height]; });
		assert.deepEqual(await size("TCP:587"), await size("UDP:161"), "UNKNOWN and DOWN items are equally high");
		const dotColor = (label) => badge(label).locator(".ju-status-dot").evaluate((node) => getComputedStyle(node).backgroundColor);
		assert.equal(await dotColor("UDP:161"), "rgba(0, 0, 0, 0)", "UNKNOWN is a hollow ring");
		assert.notEqual(await dotColor("TCP:587"), await dotColor("UDP:53"), "DOWN and UP dots differ in colour");
		assert.equal(await badge("TCP:587").locator(".ju-status-dot").getAttribute("aria-hidden"), "true", "the dot is decorative; the hidden word is the text alternative");
		assert.equal(await row.locator(".ju-status-item", { hasText: "TCP:53" }).count(), 1, "TCP and UDP of one port number never share an item");
		assert.equal(await row.locator(".ju-status-item", { hasText: "UDP:53" }).count(), 1, "TCP and UDP of one port number never share an item");
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
		assert.deepEqual(lines["TCP:53"], []);
	} finally {
		await context.close();
	}
});
// N46 / DC-SCROLL-7 / DC-LIVE-1: a live refresh whose answer equals the page writes nothing (no node replaced, no flash of the
// certificate value that script patches in), a changed answer takes the normal swap and keeps the scroll position of <main>.
test("target detail: an unchanged KPI answer leaves the cards untouched, a changed one swaps them @endpoint-state", async ({ browser, app, adminState }) => {
	const context = await browser.newContext({ viewport: { width: 1280, height: 720 }, storageState: adminState });
	const page = await context.newPage();
	try {
		await page.goto(`${app.base}/ui/targets/${targetId}`, { waitUntil: "load" });
		await page.waitForFunction(() => window.htmx && window.Chart && window.Chart.getChart("chart-uptime")?.data.datasets.length > 0, null, { timeout: 20000 });
		// hx-trigger throttles `justup-target-<id>` (events inside the 3 s window are dropped), so the event is repeated until a request ran.
		const refresh = () => page.evaluate(() => new Promise((resolve) => {
			const cards = document.getElementById("target-info-cards");
			const event = `justup-target-${cards.getAttribute("hx-get").split("/").pop()}`;
			const timer = setInterval(() => document.body.dispatchEvent(new CustomEvent(event)), 250);
			cards.addEventListener("htmx:afterRequest", () => { clearInterval(timer); resolve(); }, { once: true });
			document.body.dispatchEvent(new CustomEvent(event));
		}));
		await page.evaluate(() => {
			const main = document.getElementById("main-content");
			main.scrollTop = 120;
			window.__kpi = { nodes: [...document.querySelectorAll("#target-info-cards *")], writes: 0, top: main.scrollTop };
			const root = document.getElementById("target-info-cards");
			// htmx marks the host itself with `htmx-request` during a request; that is not a write to the cards.
			new MutationObserver((records) => { window.__kpi.writes += records.filter((record) => record.target !== root).length; }).observe(root, { subtree: true, childList: true, characterData: true, attributes: true });
		});
		await refresh();
		const same = await page.evaluate(() => ({ writes: window.__kpi.writes, kept: window.__kpi.nodes.every((node) => node.isConnected), top: document.getElementById("main-content").scrollTop, expected: window.__kpi.top }));
		assert.ok(same.kept, "an unchanged answer replaced KPI card nodes");
		assert.equal(same.writes, 0, "an unchanged answer wrote to the KPI cards");
		assert.equal(same.top, same.expected, "an unchanged answer moved the scroll position of <main>");
		await page.route(`**/ui/fragments/target-info-cards/${targetId}`, async (route) => {
			const response = await route.fetch();
			const { "content-length": _length, ...headers } = response.headers();
			await route.fulfill({ status: response.status(), headers, body: (await response.text()).replace(">Interval<", ">Interval changed<") });
		});
		await refresh();
		await expect(page.locator("#target-info-cards .target-kpi-label", { hasText: "Interval changed" })).toHaveCount(1);
		const swapped = await page.evaluate(() => ({ replaced: window.__kpi.nodes.some((node) => !node.isConnected), top: document.getElementById("main-content").scrollTop, expected: window.__kpi.top }));
		assert.ok(swapped.replaced, "a changed answer takes the normal swap");
		assert.equal(swapped.top, swapped.expected, "the structural swap keeps the scroll position of <main>");
	} finally {
		await context.close();
	}
});
// DC-STATE-4 on the S1/S3 surfaces: the hollow ring is "no confirmed state". Every UNKNOWN item is a ring, no UP or DOWN item is,
// and the ring appears on no other word, so it never signals DOWN and never mixes with the Info meaning of S4.
for (const surface of [{ name: "targets list", path: () => "/ui/targets" }, { name: "target detail", path: () => `/ui/targets/${targetId}` }]) {
	test(`${surface.name}: the hollow ring means Unknown and never Down @endpoint-state`, async ({ browser, app, adminState }) => {
		const context = await browser.newContext({ viewport: { width: 1280, height: 900 }, storageState: adminState });
		try {
			const page = await context.newPage();
			await page.goto(`${app.base}${surface.path()}`, { waitUntil: "load" });
			await page.locator(".ju-status-item", { hasText: "UDP:161" }).first().waitFor({ state: "attached" });
			const items = await page.evaluate(() => [...document.querySelectorAll("main .ju-status-item")].map((item) => ({
				label: item.textContent.replace(/\s+/g, " ").trim(),
				hollow: getComputedStyle(item.querySelector(".ju-status-dot")).backgroundColor === "rgba(0, 0, 0, 0)",
			})));
			// Check items read "Up", "Down", "Unknown"; a certificate item reads "Cert: unknown|ok|warning|critical|expired".
			const word = (item) => /\b(up|down|unknown)\b/i.exec(item.label)?.[1].toLowerCase();
			assert.deepEqual([...new Set(items.map(word))].filter(Boolean).sort(), ["down", "unknown", "up"], "the fixture shows all three check states");
			for (const item of items) assert.equal(item.hollow, word(item) === "unknown", `${item.label}: ring ${item.hollow}`);
		} finally {
			await context.close();
		}
	});
}
// DC-TLIST-2 at 390 px: the summary gives the total and the DOWN and UNKNOWN counts, the preview shows at most three dots in the order
// DOWN, UNKNOWN, certificate alerts, UP, `+N more` reveals every dot and `Show fewer` restores the preview.
test("targets list at 390px: counts, preview order and the +N more control @endpoint-state", async ({ browser, app, adminState }) => {
	const context = await browser.newContext({ viewport: { width: 390, height: 844 }, hasTouch: true, storageState: adminState });
	try {
		const page = await context.newPage();
		await page.goto(`${app.base}/ui/targets`, { waitUntil: "load" });
		const row = page.locator(`#targets-table tr[data-target-id="${targetId}"]`);
		await row.waitFor({ state: "attached", timeout: 20000 });
		const collapsed = page.locator("#targets-table .accordion-button[aria-expanded=false]");
		while (await collapsed.count()) {
			await collapsed.first().click();
			await page.waitForFunction(() => !document.querySelector("#targets-table .collapsing"));
		}
		const items = row.locator(".check-badges .ju-status-item");
		const wordOf = (text) => /\b(Up|Down|Unknown)\b/.exec(text)?.[1];
		const all = (await items.evaluateAll((nodes) => nodes.map((node) => ({ text: node.textContent.replace(/\s+/g, " ").trim(), shown: getComputedStyle(node).display !== "none" })))).map((item) => ({ ...item, word: wordOf(item.text) }));
		const total = all.length;
		const rank = { Down: 0, Unknown: 1, Up: 3 };
		assert.deepEqual(all.map((item) => rank[item.word]), all.map((item) => rank[item.word]).sort((a, b) => a - b), `checks are ordered DOWN, UNKNOWN, certificate alerts, UP: ${all.map((item) => item.text)}`);
		const summary = (await row.locator(".targets-check-summary").innerText()).replace(/\s+/g, " ");
		assert.match(summary, new RegExp(`\\b${total} Checks\\b`));
		for (const word of ["Down", "Unknown"]) assert.match(summary, new RegExp(`\\b${all.filter((item) => item.word === word).length} ${word}\\b`), `${word} count in "${summary}"`);
		assert.ok(total > 3 && all.filter((item) => item.shown).length === 3, "the fixture has more than three checks and the preview shows three");
		assert.deepEqual(all.filter((item) => item.shown).map((item) => item.text), all.slice(0, 3).map((item) => item.text), "the preview holds the first three of the ordered checks");
		assert.ok(all[0].word === "Down" && all.some((item) => item.word === "Unknown"), "the fixture has a DOWN and an UNKNOWN check");
		const toggle = row.locator(".targets-check-toggle");
		assert.equal(await toggle.innerText(), `+${total - 3} more`);
		await toggle.click();
		assert.equal(await items.evaluateAll((nodes) => nodes.filter((node) => getComputedStyle(node).display !== "none").length), total, "+N more reveals every dot");
		assert.match(await toggle.innerText(), /Show fewer/);
		await toggle.click();
		assert.equal(await items.evaluateAll((nodes) => nodes.filter((node) => getComputedStyle(node).display !== "none").length), 3, "Show fewer restores the preview");
	} finally {
		await context.close();
	}
});
