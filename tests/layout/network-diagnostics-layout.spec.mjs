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
import { test } from "./fixtures.mjs";
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
test.beforeAll(() => {
	fragment = execFileSync(resolvePythonExecutable(ROOT), [path.join(ROOT, "tests", "layout", "render_traceroute_fragment.py")], {
		input: JSON.stringify(PAYLOAD), env: { ...process.env, PYTHONPATH: ROOT }, encoding: "utf8",
	});
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
	};
});

for (const viewport of VIEWPORTS) {
	for (const theme of THEMES) {
		for (const role of ["admin", "viewer"]) {
			test(`network diagnostics: Network route follows the top directly as ${role} at ${viewport.name} ${theme} @network-diagnostics-layout`, async ({ browser, app, adminState }) => {
				const { context, page, errors } = await open(browser, app, adminState, viewport, theme, role, "ok");
				try {
					await page.waitForSelector("#target-network-diagnostics-pane .hop-table", { state: "visible" });
					await page.waitForTimeout(400);
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
