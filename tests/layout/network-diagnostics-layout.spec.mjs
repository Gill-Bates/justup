//
// tests/layout/network-diagnostics-layout.spec.mjs
// Copyright (C) 2026 Gill-Bates http://github.com/Gill-Bates
//

// Target detail, Network diagnostics tab on the full page. At desktop sizes (>= 1200x800) the tab fills
// the main area; a second block above the traceroute used to take the single flexible grid row and push
// "Network route" far down. The generic layout audit only looks for overflow and clipping, so this spec
// measures the vertical gaps themselves: pane top -> "Network route" -> KPI row -> hop table. The
// traceroute fragment is fulfilled from the real template; there is no Nmap run.
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { devices } from "@playwright/test";
import { test } from "./fixtures.mjs";
import { waitForVisualStability } from "./harness.mjs";
import { resolvePythonExecutable } from "./test-support.mjs";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");
const VIEWPORTS = [{ name: "1920x1080", width: 1920, height: 1080 }, { name: "1280x800", width: 1280, height: 800 }];
const THEMES = ["light", "dark"];
const MAX_TOP_GAP = 64; // pane top -> "Network route": padding only
const MAX_STACK_GAP = 40; // between route panel, KPI row and hop table: one gutter
const point = (hop, lat, lon, city, country, extra = {}) => ({ hop, lat, lon, city, country, ip: `192.0.2.${hop}`, ...extra });
const PAYLOAD = {
	points: [
		point(1, 50.11, 8.68, "Frankfurt", "DE", { map_role: "route", segment_to_next: "solid" }),
		point(2, 52.37, 4.9, "Amsterdam", "NL", { map_role: "route", segment_to_next: "solid" }),
		point(3, 51.3, 6.85, "Ratingen", "DE", { map_role: "route", _destination: true }),
	],
	hops: [1, 2, 3].map((hop) => ({ hop, ip_full: `192.0.2.${hop}`, hostname: `hop${hop}.example.net`, city: "Frankfurt", country: "de", latency_display: `${hop * 4} ms`, status: "ok", on_map: true })),
	kpis: { hop_count: 3, latency_display: "12 ms", city: "Ratingen", country: "de" },
};
let fragment;
const render = (payload) => execFileSync(resolvePythonExecutable(ROOT), [path.join(ROOT, "tests", "layout", "render_traceroute_fragment.py")], {
	input: JSON.stringify(payload), env: { ...process.env, PYTHONPATH: ROOT }, encoding: "utf8",
});
test.beforeAll(() => {
	fragment = render(PAYLOAD);
});

const VIEWER = { username: "diagviewer", password: "Diag-Viewer-2026!" };
async function viewerContext(browser, app, adminState, viewport) {
	const setup = await browser.newContext({ storageState: adminState });
	try {
		const page = await setup.newPage();
		await page.goto(app.base + "/");
		const status = await page.evaluate(async (user) => (await window.apiFetch("/users", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ...user, is_admin: false }) })).status, VIEWER);
		assert.ok(status === 200 || status === 409, `viewer setup returned ${status}`);
	} finally {
		await setup.close();
	}
	const context = await browser.newContext({ viewport: { width: viewport.width, height: viewport.height } });
	const page = await context.newPage();
	await page.goto(app.base + "/ui/login");
	await page.fill("#username", VIEWER.username);
	await page.fill("#password", VIEWER.password);
	await page.click('#login-form button[type="submit"]');
	await page.waitForURL((url) => !/login|change-password/.test(url.pathname), { timeout: 10000 });
	return { context, page };
}
async function open(browser, app, adminState, viewport, theme, role, reply) {
	let context;
	let page;
	if (role === "viewer") ({ context, page } = await viewerContext(browser, app, adminState, viewport));
	else {
		context = await browser.newContext({ viewport: { width: viewport.width, height: viewport.height }, storageState: adminState });
		page = await context.newPage();
	}
	const errors = [];
	page.on("pageerror", (error) => errors.push(String(error)));
	let release;
	const gate = new Promise((resolve) => { release = resolve; });
	await page.route("**/ui/fragments/traceroute/*", async (route) => {
		if (reply === "loading") await gate;
		if (reply === "error") return route.fulfill({ status: 500, contentType: "text/plain", body: "boom" });
		return route.fulfill({ status: 200, contentType: "text/html", headers: { "X-Measured-At": String(Date.now()) }, body: fragment });
	});
	await page.goto(`${app.base}/ui/targets/1`, { waitUntil: "load" });
	await page.evaluate((mode) => document.documentElement.setAttribute("data-bs-theme", mode), theme);
	await page.click("#target-network-diagnostics-tab");
	return { context, page, errors, release };
}
const measure = (page) => page.evaluate(() => {
	const box = (el) => {
		if (!el) return null;
		const r = el.getBoundingClientRect();
		return { top: r.top, bottom: r.bottom, left: r.left, right: r.right, height: r.height };
	};
	// An ancestor that clips (not scrolls) must contain the element's box.
	const clippedBy = (el) => {
		const r = el.getBoundingClientRect();
		for (let node = el.parentElement; node && node !== document.documentElement; node = node.parentElement) {
			const style = getComputedStyle(node);
			if (!/hidden|clip/.test(style.overflowY)) continue;
			const c = node.getBoundingClientRect();
			if (r.top < c.top - 1 || r.bottom > c.bottom + 1) return node.id || node.className;
		}
		return "";
	};
	const pane = document.getElementById("target-network-diagnostics-pane");
	const route = document.getElementById("tr-route-title")?.closest("section");
	const kpis = document.querySelector("#target-network-diagnostics-pane .tr-kpis");
	const table = document.querySelector("#target-network-diagnostics-pane .hop-table");
	const scroll = table?.closest(".tr-table-scroll");
	const hops = table?.closest(".tr-hops-panel");
	const container = document.getElementById("traceroute-container");
	return {
		pane: box(pane), route: box(route), kpis: box(kpis), scroll: box(scroll), hops: box(hops), container: box(container),
		detection: box(document.getElementById("service-detection")),
		detectLabel: /Detect services/.test(document.body.textContent),
		clipped: [route, kpis, hops, scroll].filter(Boolean).map(clippedBy).filter(Boolean),
		scrollX: document.documentElement.scrollWidth - document.documentElement.clientWidth,
		tableScrollX: scroll ? scroll.scrollWidth - scroll.clientWidth : 0,
		text: container?.textContent.replace(/\s+/g, " ").trim() ?? "",
		alert: Boolean(container?.querySelector('[role="alert"]')),
		mapFrameOverflowY: (() => {
			const frame = document.querySelector("#target-network-diagnostics-pane .tr-map-frame");
			return frame ? getComputedStyle(frame).overflowY : null;
		})(),
	};
});

for (const viewport of VIEWPORTS) {
	for (const theme of THEMES) {
		for (const role of ["admin", "viewer"]) {
			test(`network diagnostics: Network route follows the top directly as ${role} at ${viewport.name} ${theme} @network-diagnostics-layout`, async ({ browser, app, adminState }) => {
				const { context, page, errors } = await open(browser, app, adminState, viewport, theme, role, "ok");
				try {
					await page.waitForSelector("#target-network-diagnostics-pane .hop-table", { state: "visible" });
					await waitForVisualStability(page);
					const m = await measure(page);
					assert.deepEqual(errors, []);
					assert.ok(m.route && m.kpis && m.scroll, "route panel, KPI row and hop table render");
					const topGap = m.route.top - m.pane.top;
					assert.ok(topGap >= 0 && topGap <= MAX_TOP_GAP, `gap above "Network route" is ${Math.round(topGap)}px, expected at most ${MAX_TOP_GAP}px`);
					const kpiGap = m.kpis.top - m.route.bottom;
					assert.ok(kpiGap >= -2 && kpiGap <= MAX_STACK_GAP, `gap between "Network route" and the KPIs is ${Math.round(kpiGap)}px`);
					const tableGap = m.hops.top - m.kpis.bottom;
					assert.ok(tableGap >= -2 && tableGap <= MAX_STACK_GAP, `gap between the KPIs and the hop table is ${Math.round(tableGap)}px`);
					for (const part of [m.route, m.kpis, m.hops]) {
						assert.ok(part.left >= m.pane.left - 1 && part.right <= m.pane.right + 1, "a part sticks out of the tab pane horizontally");
					}
					assert.deepEqual(m.clipped, [], "a card or the table is cut off by a clipping ancestor");
					// The pane rule (target-detail.css) owns the fill-mode frame; traceroute-map.css sets no scroll area of its own.
					assert.equal(m.mapFrameOverflowY, "visible", "the route map frame is not a scroll container");
					assert.ok(m.scrollX <= 0, `horizontal page overflow ${m.scrollX}px`);
					assert.equal(m.detectLabel, false, "service detection does not belong to Network diagnostics");
				} finally {
					await context.close();
				}
			});
		}
		test(`network diagnostics: loading state sits at the top at ${viewport.name} ${theme} @network-diagnostics-layout`, async ({ browser, app, adminState }) => {
			const { context, page, errors, release } = await open(browser, app, adminState, viewport, theme, "admin", "loading");
			try {
				await page.waitForFunction(() => /Loading network diagnostics/.test(document.getElementById("traceroute-container")?.textContent ?? ""));
				const m = await measure(page);
				assert.deepEqual(errors, []);
				assert.ok(m.container.top - m.pane.top <= MAX_TOP_GAP, `loading state starts ${Math.round(m.container.top - m.pane.top)}px below the pane top`);
				assert.match(m.text, /Loading network diagnostics/);
				assert.equal(m.detectLabel, false);
			} finally {
				release();
				await context.close();
			}
		});
		test(`network diagnostics: error state sits at the top at ${viewport.name} ${theme} @network-diagnostics-layout`, async ({ browser, app, adminState }) => {
			const { context, page } = await open(browser, app, adminState, viewport, theme, "admin", "error");
			try {
				await page.waitForSelector('#traceroute-container [role="alert"]', { state: "visible" });
				const m = await measure(page);
				assert.ok(m.alert);
				assert.ok(m.container.top - m.pane.top <= MAX_TOP_GAP, `error state starts ${Math.round(m.container.top - m.pane.top)}px below the pane top`);
				assert.ok(m.scrollX <= 0, `horizontal page overflow ${m.scrollX}px`);
			} finally {
				await context.close();
			}
		});
	}
}

// iPad sizes, both orientations, iPad Mini to iPad Pro 13" (Playwright has no 12.9"/13" descriptor: 1366x1024 is
// the Pro 12.9 / Air 13 landscape size and the first one that split table and map in the height-bound fill mode).
// Extra page height at 1366x1024 is accepted; the assertions below only require that nothing is clipped or covered.
const IPAD_VIEWPORTS = [
	...["iPad Mini", "iPad Mini landscape", "iPad (gen 7)", "iPad (gen 7) landscape", "iPad Pro 11", "iPad Pro 11 landscape"]
		.map((name) => ({ name, width: devices[name].viewport.width, height: devices[name].viewport.height })),
	{ name: "iPad Pro 12.9", width: 1024, height: 1366 }, { name: "iPad Pro 12.9 landscape", width: 1366, height: 1024 },
];
const IPAD_IPS = ["192.168.178.1", "62.155.246.34", "217.0.116.50", "212.227.121.182", "217.237.151.5", "80.150.168.1", "93.186.132.60",
	"2001:4dd0:ff00:1c::1", "87.106.1.12", "46.237.184.18", "195.20.226.9", "81.169.145.100", "217.160.0.250"];
const IPAD_PAYLOAD = {
	points: PAYLOAD.points,
	hops: IPAD_IPS.map((ip, index) => ({
		hop: index + 1, ip_full: ip, is_ipv6: ip.includes(":"), hostname: index % 2 ? `ae${index}.core.example-carrier.net` : "",
		city: index % 3 ? "Frankfurt am Main" : "", country: index % 3 ? "de" : "", latency_display: `${index + 5}.2 ms`,
		status: "ok", on_map: index % 3 !== 0,
	})),
	kpis: { hop_count: 13, latency_display: "12 ms", city: "Ratingen", country: "de" },
	total_hops: 13,
};
const measureIpad = (page) => page.evaluate(() => {
	const scroll = document.querySelector(".tr-table-scroll");
	const table = scroll.querySelector("table");
	const ownScroll = (el) => /auto|scroll/.test(getComputedStyle(el).overflowY) && el.scrollHeight > el.clientHeight + 1;
	const panel = document.querySelector(".tr-map-panel");
	const inside = (inner, outer) => inner.left >= outer.left - 1 && inner.right <= outer.right + 1 && inner.bottom <= outer.bottom + 1;
	// Scrolling ancestors of the map between the map itself and its card (the card is the map's only allowed clip).
	const mapScrollers = [];
	for (let node = document.getElementById("traceroute-map"); node && node !== panel.parentElement; node = node.parentElement) {
		if (ownScroll(node)) mapScrollers.push(node.className || node.id);
	}
	const wrapped = [...table.querySelectorAll(".tr-ip:not(.tr-ip--v6)")].filter((el) => {
		const range = document.createRange();
		range.selectNodeContents(el);
		return range.getClientRects().length > 1;
	}).map((el) => el.textContent);
	const rows = [...table.querySelectorAll("tbody tr")].map((row) => [...row.children]);
	const tableBox = table.getBoundingClientRect();
	const scrollBox = scroll.getBoundingClientRect();
	const cutCells = rows.flat().filter((cell) => cell.scrollWidth > cell.clientWidth + 1).map((cell) => cell.textContent.trim());
	const outsideCells = rows.flat().filter((cell) => !inside(cell.getBoundingClientRect(), { left: scrollBox.left, right: scrollBox.right, bottom: Infinity })).map((cell) => cell.textContent.trim());
	const mapBox = document.querySelector(".tr-map-overview").getBoundingClientRect();
	const legendBox = document.querySelector(".tr-map-legend").getBoundingClientRect();
	const panelBox = panel.getBoundingClientRect();
	return {
		wrapped, cutCells, outsideCells, mapScrollers,
		tableOverflowX: Math.max(scroll.scrollWidth - scroll.clientWidth, Math.round(tableBox.width - scrollBox.width)),
		mapInside: inside(mapBox, panelBox) && inside(legendBox, panelBox), mapHeight: mapBox.height, mapWidth: mapBox.width,
		pageOverflowX: document.documentElement.scrollWidth - document.documentElement.clientWidth,
	};
});
for (const viewport of IPAD_VIEWPORTS) {
	test(`network diagnostics: iPad hop table and route map are complete at ${viewport.name} ${viewport.width}x${viewport.height} @network-diagnostics-layout`, async ({ browser, app, adminState }) => {
		const context = await browser.newContext({ viewport: { width: viewport.width, height: viewport.height }, storageState: adminState });
		const page = await context.newPage();
		const errors = [];
		page.on("pageerror", (error) => errors.push(String(error)));
		const body = render(IPAD_PAYLOAD);
		await page.route("**/ui/fragments/traceroute/*", (route) => route.fulfill({ status: 200, contentType: "text/html", headers: { "X-Measured-At": String(Date.now()) }, body }));
		try {
			await page.goto(`${app.base}/ui/targets/1`, { waitUntil: "load" });
			await page.click("#target-network-diagnostics-tab");
			await page.waitForSelector("#target-network-diagnostics-pane .hop-table", { state: "visible" });
			await page.waitForSelector("#traceroute-map .leaflet-pane", { state: "attached" });
			await waitForVisualStability(page);
			const m = await measureIpad(page);
			assert.deepEqual(errors, []);
			assert.deepEqual(m.wrapped, [], "an IPv4 address breaks across lines");
			assert.deepEqual(m.cutCells, [], "a table cell clips its content");
			assert.deepEqual(m.outsideCells, [], "a table cell lies outside the table viewport (clipped column, overlapped status)");
			assert.ok(m.tableOverflowX <= 1, `the hop table needs sideways scrolling by ${m.tableOverflowX}px at an iPad width`);
			assert.deepEqual(m.mapScrollers, [], "the route map card contains its own scroll area");
			assert.ok(m.mapInside, "map or legend sticks out of the map card");
			assert.ok(m.mapHeight >= 320 && m.mapWidth >= 300, `map is too small: ${m.mapWidth}x${m.mapHeight}`);
			assert.ok(m.pageOverflowX <= 0, `horizontal page overflow ${m.pageOverflowX}px`);
		} finally {
			await context.close();
		}
	});
}
